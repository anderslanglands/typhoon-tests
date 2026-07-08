from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import html
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
from typing import Any

import pytest

from .config import (
    CaseConfig,
    FrameValue,
    SuiteConfig,
    format_pattern,
    find_suite_config,
    load_case_config,
    load_suite_config_for_path,
    lookup_case_value,
    parse_frame_spec,
)
from .images import compare_images


RUN_DIR_RE = re.compile(r"^run-(\d+)$")
CAMERA_REL_RE = re.compile(r"\brel\s+camera\s*=\s*<([^>]+)>")

REPORT_STATIC_DIR = Path(__file__).resolve().parent / "static"
REPORT_ASSET_NAMES = ("typhoon-exr-viewer.js", "typhoon_exr_wasm.wasm")
REPORT_FAVICON_SVG_SOURCE = Path(__file__).resolve().parents[1] / "img" / "typhoon-color.svg"
REPORT_FAVICON_PNG_SOURCE = Path(__file__).resolve().parents[1] / "img" / "typhoon-color.png"
REPORT_FAVICON_OUTPUT = Path("img") / "typhoon-color.svg"
REPORT_FAVICON_FALLBACK = Path("favicon.ico")

IGNORED_DIRS = {
    ".git",
    ".pixi",
    "_output",
    "__pycache__",
    "comparison",
    "reference",
    "renders",
}

DEFAULT_PROVIDER_LABEL = "package"


@dataclass(frozen=True)
class RunContext:
    output_base: Path
    run_dir: Path
    run_number: int
    started_at: str
    provider: str | None = None


@dataclass(frozen=True)
class TyphoonOptions:
    provider: Path | None
    run_context: RunContext
    reference_dir: Path | None
    require_references: bool
    require_thresholds: bool
    dry_run: bool


@dataclass(frozen=True)
class TyphoonCase:
    path: Path
    suite: SuiteConfig
    case_config: CaseConfig
    key: str
    case_name: str
    relative_path: str
    sections: tuple[str, ...]
    skip: str | None
    xfail: str | None
    suspect: bool
    flip_threshold: float | None
    frame: FrameValue | None = None


class TyphoonRenderError(AssertionError):
    def __init__(self, message: str, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.result = result


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("typhoon")
    group.addoption(
        "--typhoon-provider",
        action="store",
        default=None,
        metavar="OPENUSD_CHECKOUT",
        help=(
            "Path to a local OpenUSD/Typhoon checkout containing pixi.toml. "
            "If omitted, tests use the installed openusd-typhoon package."
        ),
    )
    group.addoption(
        "--typhoon-output-root",
        action="store",
        default=None,
        help=(
            "Directory containing numbered run outputs. Defaults to _output "
            "under the pytest root."
        ),
    )
    group.addoption(
        "--typhoon-artifact-root",
        action="store",
        default=None,
        help="Deprecated alias for --typhoon-output-root.",
    )
    group.addoption(
        "--typhoon-reference-dir",
        action="store",
        default=None,
        help="Override the reference image directory for all suites.",
    )
    group.addoption(
        "--typhoon-require-references",
        action="store_true",
        default=False,
        help="Fail tests when a configured reference image is missing.",
    )
    group.addoption(
        "--typhoon-require-thresholds",
        action="store_true",
        default=False,
        help="Fail compared tests that do not have a FLIP threshold configured.",
    )
    group.addoption(
        "--typhoon-collect-unconfigured",
        action="store_true",
        default=False,
        help="Collect .usda files without an ancestor typhoon-suite.toml.",
    )
    group.addoption(
        "--typhoon-dry-run",
        action="store_true",
        default=False,
        help="Print render commands without executing them.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "typhoon_usd: USD render regression test collected by typhoon-tests",
    )
    config._typhoon_results = []  # type: ignore[attr-defined]
    config._typhoon_run_context = None  # type: ignore[attr-defined]


def pytest_ignore_collect(collection_path: Any, config: pytest.Config) -> bool:
    path = Path(str(collection_path))
    return path.is_dir() and is_ignored_directory(path)


def is_ignored_directory(path: Path) -> bool:
    return path.name.startswith("_") or path.name in IGNORED_DIRS


def pytest_collect_file(file_path: Any, parent: pytest.Collector) -> pytest.File | None:
    path = Path(str(file_path))
    root = Path(str(parent.config.rootpath)).resolve()
    collect_unconfigured = bool(parent.config.getoption("--typhoon-collect-unconfigured"))
    if not should_collect_usda(path, root, collect_unconfigured):
        return None
    return TyphoonUsdFile.from_parent(parent, path=path)


def path_has_ignored_directory(path: Path, _root: Path) -> bool:
    return any(
        part.startswith("_") or part in IGNORED_DIRS
        for part in path.parent.parts
    )


def should_collect_usda(path: Path, root: Path, collect_unconfigured: bool) -> bool:
    if path.suffix != ".usda":
        return False
    if path_has_ignored_directory(path, root):
        return False
    if find_suite_config(path.resolve()) is not None:
        return True
    return collect_unconfigured


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    results = getattr(session.config, "_typhoon_results", [])
    if not results:
        return

    context = getattr(session.config, "_typhoon_run_context", None)
    if context is None:
        return

    write_run_outputs(context, results)


class TyphoonUsdFile(pytest.File):
    def collect(self) -> list[pytest.Item]:
        path = Path(str(self.path))
        items = []
        for case in build_cases(path):
            item = TyphoonUsdItem.from_parent(self, name=case.key, case=case)
            item.add_marker(pytest.mark.typhoon_usd)
            if case.xfail:
                item.add_marker(pytest.mark.xfail(reason=case.xfail, strict=False))
            items.append(item)
        return items


class TyphoonUsdItem(pytest.Item):
    def __init__(self, *, case: TyphoonCase, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.case = case

    def runtest(self) -> None:
        if self.case.skip:
            pytest.skip(self.case.skip)
        try:
            result = run_typhoon_case(self.case, options_from_config(self.config))
        except TyphoonRenderError as exc:
            if exc.result is not None:
                self.config._typhoon_results.append(exc.result)  # type: ignore[attr-defined]
            raise
        self.config._typhoon_results.append(result)  # type: ignore[attr-defined]

    def reportinfo(self) -> tuple[Path, int, str]:
        return self.case.path, 0, f"typhoon render: {self.case.key}"

    def repr_failure(self, excinfo: pytest.ExceptionInfo[BaseException]) -> str:
        if isinstance(excinfo.value, TyphoonRenderError):
            return str(excinfo.value)
        return super().repr_failure(excinfo)


def build_case(path: Path) -> TyphoonCase:
    return build_cases(path)[0]


def build_cases(path: Path) -> list[TyphoonCase]:
    suite = load_suite_config_for_path(str(path.resolve()))
    case_config = load_case_config(path)
    skip = case_config.skip or lookup_case_value(suite.skip, path, suite.root)
    xfail = case_config.xfail or lookup_case_value(suite.xfail, path, suite.root)
    threshold = case_config.flip_threshold
    if threshold is None:
        threshold = lookup_case_value(suite.thresholds, path, suite.root)
    if threshold is None:
        threshold = suite.default_flip_threshold

    frames = resolve_case_frames(path, suite, case_config)
    try:
        suite_relative_path = path.relative_to(suite.root)
        relative_path = suite_relative_path.as_posix()
        sections = tuple(suite_relative_path.parent.parts)
    except ValueError:
        relative_path = path.name
        sections = ()
    return [
        TyphoonCase(
            path=path,
            suite=suite,
            case_config=case_config,
            key=case_key(path, suite.root, frame),
            case_name=path.stem,
            relative_path=relative_path,
            sections=sections,
            skip=skip,
            xfail=xfail,
            suspect=case_config.suspect,
            flip_threshold=threshold,
            frame=frame,
        )
        for frame in frames
    ]


def resolve_case_frames(
    path: Path,
    suite: SuiteConfig,
    case_config: CaseConfig,
) -> tuple[FrameValue | None, ...]:
    frame_spec = case_config.frame_range or lookup_case_value(
        suite.frames, path, suite.root
    )
    if frame_spec is None:
        return (None,)
    try:
        return parse_frame_spec(frame_spec)
    except ValueError as exc:
        raise ValueError(f"invalid frame range for {path}: {exc}") from exc


def encode_key_part(value: str) -> str:
    encoded = []
    for byte in value.encode("utf-8"):
        character = chr(byte)
        if character.isascii() and (character.isalnum() or character in "._-"):
            encoded.append(character)
        else:
            encoded.append(f"~{byte:02x}")
    return "".join(encoded)


def case_key(
    path: Path,
    suite_root: Path,
    frame: FrameValue | None = None,
) -> str:
    try:
        rel = path.relative_to(suite_root).with_suffix("")
    except ValueError:
        rel = Path(path.stem)
    parts = [encode_key_part(part) for part in rel.parts]
    key = "+".join(part for part in parts if part) or sanitize_key_part(path.stem)
    if frame is not None:
        key = f"{key}++frame++{frame_key(frame)}"
    return key


def frame_key(frame: FrameValue) -> str:
    if isinstance(frame, int):
        return f"{frame:04d}"
    return encode_key_part(format_frame_argument(frame).replace(".", "_"))


def sanitize_key_part(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value)


def provider_label(provider: object | None) -> str:
    if provider is None:
        return DEFAULT_PROVIDER_LABEL
    value = str(provider)
    return value if value else DEFAULT_PROVIDER_LABEL


def first_result_provider(results: list[dict[str, Any]]) -> str | None:
    for row in results:
        provider = row.get("provider")
        if provider:
            return str(provider)
    return None


def options_from_config(config: pytest.Config) -> TyphoonOptions:
    provider = config.getoption("--typhoon-provider")
    provider_path = Path(provider).expanduser().resolve() if provider else None
    return TyphoonOptions(
        provider=provider_path,
        run_context=get_run_context(config, provider_path),
        reference_dir=_optional_path(config.getoption("--typhoon-reference-dir")),
        require_references=bool(config.getoption("--typhoon-require-references")),
        require_thresholds=bool(config.getoption("--typhoon-require-thresholds")),
        dry_run=bool(config.getoption("--typhoon-dry-run")),
    )


def get_run_context(config: pytest.Config, provider: Path | None = None) -> RunContext:
    context = getattr(config, "_typhoon_run_context", None)
    if context is not None:
        return context

    output_base_arg = config.getoption("--typhoon-output-root") or config.getoption(
        "--typhoon-artifact-root"
    )
    if output_base_arg:
        output_base = Path(output_base_arg).expanduser().resolve()
    else:
        output_base = Path(str(config.rootpath)).resolve() / "_output"

    context = allocate_run_context(output_base, provider=provider_label(provider))
    config._typhoon_run_context = context  # type: ignore[attr-defined]
    return context


def allocate_run_context(
    output_base: Path,
    started_at: str | None = None,
    provider: str | None = None,
) -> RunContext:
    output_base = output_base.resolve()
    output_base.mkdir(parents=True, exist_ok=True)
    if started_at is None:
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    while True:
        run_number = next_run_number(output_base)
        run_dir = output_base / f"run-{run_number:04d}"
        try:
            run_dir.mkdir()
        except FileExistsError:
            continue
        return RunContext(
            output_base=output_base,
            run_dir=run_dir,
            run_number=run_number,
            started_at=started_at,
            provider=provider_label(provider),
        )


def next_run_number(output_base: Path) -> int:
    numbers = []
    if output_base.is_dir():
        for child in output_base.iterdir():
            if not child.is_dir():
                continue
            match = RUN_DIR_RE.match(child.name)
            if match:
                numbers.append(int(match.group(1)))
    return max(numbers, default=0) + 1


def run_typhoon_case(case: TyphoonCase, options: TyphoonOptions) -> dict[str, Any]:
    output_root = resolve_output_root(case, options)
    artifact_root = resolve_artifact_root(case, options)

    result: dict[str, Any] = {
        "suite": case.suite.name,
        "key": case.key,
        "case_name": case.case_name,
        "relative_path": case.relative_path,
        "sections": list(case.sections),
        "usd": str(case.path),
        "camera": discover_usd_camera(case.path),
        "command": [],
        "output_root": str(output_root),
        "render_output": None,
        "render_image": None,
        "artifact_root": str(artifact_root),
        "reference": None,
        "reference_image": None,
        "flip_threshold": case.flip_threshold,
        "suspect": case.suspect,
        "frame": case.frame,
        "status": "pending",
        "run_number": options.run_context.run_number,
        "run_dir": str(options.run_context.run_dir),
        "started_at": options.run_context.started_at,
        "provider": provider_label(options.provider or options.run_context.provider),
    }

    try:
        render_output = resolve_render_output(case, output_root)
        reference = resolve_reference(case, options)
    except ValueError as exc:
        result["status"] = "failed-config"
        raise TyphoonRenderError(str(exc), result) from exc
    result["render_output"] = str(render_output)
    result["reference"] = str(reference) if reference else None

    try:
        cmd = build_render_command(case, options, output_root)
    except TyphoonRenderError as exc:
        result["status"] = "failed-command"
        raise TyphoonRenderError(str(exc), result) from exc
    result["command"] = cmd

    if options.dry_run:
        print(format_command(cmd))
        result["status"] = "dry-run"
        return result

    output_root.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        result["status"] = "failed-launch"
        raise TyphoonRenderError(
            f"failed to launch renderer: {exc}\ncommand: {format_command(cmd)}",
            result,
        ) from exc

    result["returncode"] = completed.returncode
    if completed.returncode != 0:
        result["status"] = "failed-render"
        raise TyphoonRenderError(
            "renderer failed\n"
            f"command: {format_command(cmd)}\n"
            f"exit code: {completed.returncode}\n"
            f"stdout:\n{tail(completed.stdout)}\n"
            f"stderr:\n{tail(completed.stderr)}",
            result,
        )

    if not render_output.is_file():
        result["status"] = "failed-missing-render"
        raise TyphoonRenderError(
            "renderer completed but expected output was not written\n"
            f"expected: {render_output}\n"
            f"command: {format_command(cmd)}",
            result,
        )

    result["render_image"] = str(render_output)

    if reference is None or not reference.is_file():
        result["comparison"] = "missing-reference"
        if options.require_references or case.suite.missing_references == "fail":
            result["status"] = "failed-missing-reference"
            raise TyphoonRenderError(
                "reference image is missing\n"
                f"expected: {reference}\n"
                "pass --typhoon-reference-dir to override references or remove "
                "--typhoon-require-references",
                result,
            )
        result["status"] = "no-ref"
        return result

    result["reference_image"] = str(reference)

    artifact_root.mkdir(parents=True, exist_ok=True)
    try:
        comparison = compare_images(
            reference_path=reference,
            render_path=render_output,
            artifact_dir=artifact_root,
            key=case.key,
        )
    except Exception as exc:
        result["status"] = "failed-compare"
        raise TyphoonRenderError(
            f"image comparison failed for {case.key}: {exc}",
            result,
        ) from exc

    result.update(
        {
            "status": "passed",
            "comparison": "flip",
            "flip_mean": comparison.flip_mean,
            "reference_image": str(comparison.reference_image),
            "render_image": str(comparison.render_image),
            "diff_exr": str(comparison.diff_exr),
        }
    )

    if case.flip_threshold is None and options.require_thresholds:
        result["status"] = "failed-missing-threshold"
        raise TyphoonRenderError(
            f"missing FLIP threshold for compared case {case.key}",
            result,
        )

    if case.flip_threshold is not None and comparison.flip_mean > case.flip_threshold:
        result["status"] = "failed-threshold"
        raise TyphoonRenderError(
            f"mean FLIP {comparison.flip_mean:.6f} exceeds threshold "
            f"{case.flip_threshold:.6f} for {case.key}\n"
            f"render: {comparison.render_image}\n"
            f"diff: {comparison.diff_exr}",
            result,
        )

    return result


def build_render_command(
    case: TyphoonCase,
    options: TyphoonOptions,
    output_root: Path,
) -> list[str]:
    if options.provider is None:
        cmd = ["usdrender", "--complexity", "high", "--renderer", "Embree"]
    else:
        manifest = options.provider
        if manifest.is_dir():
            manifest = manifest / "pixi.toml"
        if not manifest.is_file():
            raise TyphoonRenderError(
                "--typhoon-provider must point to an OpenUSD checkout or pixi.toml; "
                f"missing manifest: {manifest}"
            )
        cmd = [
            "pixi",
            "run",
            "--manifest-path",
            str(manifest),
            "--clean-env",
            "usdrender",
        ]

    cmd.extend(case.suite.render_args)
    cmd.extend(case.case_config.render_args)
    if case.frame is not None:
        cmd.extend(["--frames", format_frame_argument(case.frame)])
    cmd.extend([str(case.path), "--outputRoot", str(output_root)])
    return cmd


def resolve_output_root(case: TyphoonCase, options: TyphoonOptions) -> Path:
    return options.run_context.run_dir


def resolve_artifact_root(case: TyphoonCase, options: TyphoonOptions) -> Path:
    return options.run_context.run_dir


def resolve_render_output(case: TyphoonCase, output_root: Path) -> Path:
    if case.case_config.render_output:
        return (
            output_root
            / format_pattern(
                case.case_config.render_output,
                case.path,
                case.suite,
                case.frame,
            )
        ).resolve()
    return (
        output_root
        / format_pattern(
            case.suite.render_output_pattern,
            case.path,
            case.suite,
            case.frame,
        )
    ).resolve()


def resolve_reference(case: TyphoonCase, options: TyphoonOptions) -> Path | None:
    if case.case_config.reference:
        reference = Path(
            format_pattern(
                case.case_config.reference,
                case.path,
                case.suite,
                case.frame,
            )
        ).expanduser()
        if not reference.is_absolute():
            reference = case.suite.root / reference
        return reference.resolve()

    reference_dir = options.reference_dir
    if reference_dir is None and case.suite.reference_dir:
        reference_dir = Path(case.suite.reference_dir)
    if reference_dir is None:
        return None
    return (
        reference_dir
        / format_pattern(
            case.suite.reference_pattern, case.path, case.suite, case.frame
        )
    ).resolve()


def copy_report_assets(run_dir: Path) -> list[Path]:
    asset_dir = run_dir / "assets"
    copied: list[Path] = []
    for name in REPORT_ASSET_NAMES:
        source = REPORT_STATIC_DIR / name
        if not source.is_file():
            continue
        asset_dir.mkdir(parents=True, exist_ok=True)
        destination = asset_dir / name
        shutil.copy2(source, destination)
        copied.append(destination)
    return copied


def copy_report_favicon(output_base: Path) -> list[Path]:
    copied: list[Path] = []
    if REPORT_FAVICON_SVG_SOURCE.is_file():
        svg_destination = output_base / REPORT_FAVICON_OUTPUT
        svg_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPORT_FAVICON_SVG_SOURCE, svg_destination)
        copied.append(svg_destination)

    if REPORT_FAVICON_PNG_SOURCE.is_file():
        ico_destination = output_base / REPORT_FAVICON_FALLBACK
        ico_destination.write_bytes(
            build_ico_from_png(REPORT_FAVICON_PNG_SOURCE.read_bytes())
        )
        copied.append(ico_destination)
    return copied


def build_ico_from_png(png_data: bytes) -> bytes:
    width, height = png_dimensions(png_data)
    width_byte = 0 if width >= 256 else width
    height_byte = 0 if height >= 256 else height
    image_offset = 6 + 16
    return b"".join(
        [
            struct.pack("<HHH", 0, 1, 1),
            struct.pack(
                "<BBBBHHII",
                width_byte,
                height_byte,
                0,
                0,
                1,
                32,
                len(png_data),
                image_offset,
            ),
            png_data,
        ]
    )


def png_dimensions(png_data: bytes) -> tuple[int, int]:
    png_signature = b"\x89PNG\r\n\x1a\n"
    if not png_data.startswith(png_signature) or png_data[12:16] != b"IHDR":
        return (0, 0)
    return struct.unpack(">II", png_data[16:24])


def write_run_outputs(context: RunContext, results: list[dict[str, Any]]) -> None:
    context.run_dir.mkdir(parents=True, exist_ok=True)
    report_path = context.run_dir / "typhoon-report.json"
    report_path.write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = summarize_results(context, results)
    (context.run_dir / "run-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (context.run_dir / "index.html").write_text(
        build_html_report(results, context),
        encoding="utf-8",
    )
    copy_report_assets(context.run_dir)
    copy_report_favicon(context.output_base)
    (context.output_base / "index.html").write_text(
        build_output_index(context.output_base),
        encoding="utf-8",
    )


def summarize_results(context: RunContext, results: list[dict[str, Any]]) -> dict[str, Any]:
    compared = [row for row in results if row.get("comparison") == "flip"]
    flip_values = numeric_flip_values(compared)
    missing = [row for row in results if row.get("comparison") == "missing-reference"]
    failures = [row for row in results if is_failure_result(row)]
    dry_runs = [row for row in results if row.get("status") == "dry-run"]
    suspect = [row for row in results if row.get("suspect") is True]
    summary = {
        "run_name": context.run_dir.name,
        "run_number": context.run_number,
        "started_at": context.started_at,
        "provider": provider_label(context.provider or first_result_provider(results)),
        "run_dir": str(context.run_dir),
        "total": len(results),
        "compared": len(compared),
        "missing_references": len(missing),
        "failed": len(failures),
        "dry_run": len(dry_runs),
        "suspect": len(suspect),
        "flip_mean": None,
        "flip_min": None,
        "flip_max": None,
    }
    if flip_values:
        summary.update(
            {
                "flip_mean": sum(flip_values) / len(flip_values),
                "flip_min": min(flip_values),
                "flip_max": max(flip_values),
            }
        )
    return summary


def numeric_flip_values(rows: list[dict[str, Any]]) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = row.get("flip_mean")
        if value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return values


def status_label(value: object) -> str:
    status = str(value or "")
    return {"compared": "passed", "rendered": "no-ref"}.get(status, status)


def status_class(value: object) -> str:
    status = status_label(value)
    if status == "passed":
        return "status-passed"
    if status == "no-ref":
        return "status-no-ref"
    if status == "failed-threshold":
        return "status-failed-threshold"
    if status.startswith("failed-") and status != "failed-render":
        return "status-failed-other"
    return ""


def sortable_cell(
    content: str,
    *,
    sort_value: object | None = None,
    css_class: str = "",
) -> str:
    attrs = []
    if sort_value is not None:
        attrs.append(f'data-sort-value="{html.escape(str(sort_value), quote=True)}"')
    if css_class:
        attrs.append(f'class="{html.escape(css_class, quote=True)}"')
    rendered_attrs = " " + " ".join(attrs) if attrs else ""
    return f"<td{rendered_attrs}>{content}</td>"


def optional_number_cell(value: object) -> str:
    if value is None:
        return sortable_cell("", sort_value="")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return sortable_cell("", sort_value="")
    return sortable_cell(f"{number:.3f}", sort_value=number)


def format_flip_stat(value: object) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "n/a"


def report_palette_css() -> str:
    return """    :root {
      --ty-base00: #212121;
      --ty-base01: #15171c;
      --ty-base02: #555555;
      --ty-base03: #6c6d70;
      --ty-base04: #83868b;
      --ty-base05: #9a9fa6;
      --ty-base06: #b2b8c2;
      --ty-base07: #ffffff;
      --ty-base08: #b14956;
      --ty-base09: #c6725a;
      --ty-base0A: #5485c0;
      --ty-base0B: #91b377;
      --ty-base0C: #7fcccb;
      --ty-base0D: #7b8fa4;
      --ty-base0E: #a5779e;
      --ty-base0F: #58242b;
      --ty-base10: #383838;
      --ty-base11: #1c1c1c;
      --ty-base12: #ec5f67;
      --ty-base13: #fdc253;
      --ty-base14: #88e985;
      --ty-base15: #58c2c0;
      --ty-base16: #5485c0;
      --ty-base17: #bf83c0;
      --ty-red: var(--ty-base08);
      --ty-red-bright: var(--ty-base12);
      --ty-red-dim: var(--ty-base0F);
      --ty-yellow: var(--ty-base09);
      --ty-yellow-bright: var(--ty-base13);
      --ty-green: var(--ty-base0B);
      --ty-green-bright: var(--ty-base14);
      --ty-cyan: var(--ty-base0C);
      --ty-cyan-bright: var(--ty-base15);
      --ty-blue: var(--ty-base0D);
      --ty-blue-bright: var(--ty-base16);
      --ty-magenta: var(--ty-base0E);
      --ty-magenta-bright: var(--ty-base17);
    }
"""


def sortable_header(
    label: str,
    column: int,
    sort_type: str = "text",
    sort_direction: str | None = None,
) -> str:
    attrs = [
        'type="button"',
        f'data-sort-column="{column}"',
        f'data-sort-type="{html.escape(sort_type, quote=True)}"',
    ]
    if sort_direction is not None:
        attrs.append(
            f'data-sort-direction="{html.escape(sort_direction, quote=True)}"'
        )
    return (
        "<th>"
        f"<button {' '.join(attrs)}>"
        f"{html.escape(label)}"
        "</button>"
        "</th>"
    )


def sortable_table_script() -> str:
    return """  <script>
    (() => {
      const reportStateKey = typeof window !== "undefined" && window.location
        ? `typhoon-report-ui:${window.location.pathname}`
        : "";
      let restoredState = null;
      if (reportStateKey) {
        try {
          const storage = window.sessionStorage;
          restoredState = JSON.parse(storage.getItem(reportStateKey) || "null");
          storage.removeItem(reportStateKey);
        } catch (_error) {
          restoredState = null;
        }
      }
      const restoredSorts = new Map(
        Array.isArray(restoredState?.sorts)
          ? restoredState.sorts.map((item) => [item.key, item])
          : []
      );
      const topNav = typeof document.querySelector === "function"
        ? document.querySelector(".top-nav")
        : null;
      if (topNav) {
        const updateStickyTop = () => {
          const navBottom = Math.ceil(topNav.getBoundingClientRect().bottom);
          document.documentElement.style.setProperty(
            "--report-sticky-top", `${navBottom + 8}px`
          );
        };
        updateStickyTop();
        if (typeof ResizeObserver !== "undefined") {
          new ResizeObserver(updateStickyTop).observe(topNav);
        }
        if (typeof window !== "undefined") {
          window.addEventListener("resize", updateStickyTop);
        }
      }
      const tables = document.querySelectorAll("table[data-sortable-table]");
      for (const table of tables) {
        const tbody = table.tBodies[0];
        if (!tbody) continue;
        const buttons = table.querySelectorAll("th button[data-sort-column]");
        const initialButton = table.querySelector("th button[data-sort-direction]");
        const restoredSort = restoredSorts.get(table.dataset?.sortTableKey || "");
        const restoredButton = restoredSort
          ? Array.from(buttons).find(
              (button) => Number(button.dataset.sortColumn) === Number(restoredSort.column)
            )
          : null;
        const activeButton = restoredButton || initialButton;
        let activeColumn = activeButton ? Number(activeButton.dataset.sortColumn) : -1;
        let activeDirection = restoredButton
          ? (restoredSort.direction === "desc" ? -1 : 1)
          : (initialButton?.dataset.sortDirection === "desc" ? -1 : 1);
        const readValue = (row, column, type) => {
          const cell = row.cells[column];
          if (!cell) return "";
          const raw = cell.dataset.sortValue ?? cell.textContent.trim();
          if (type === "number") {
            if (raw === "") return Number.NEGATIVE_INFINITY;
            const parsed = Number(raw);
            return Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed;
          }
          return raw.toLowerCase();
        };
        const setSortDirection = (button, direction) => {
          for (const other of buttons) {
            delete other.dataset.sortDirection;
          }
          button.dataset.sortDirection = direction === 1 ? "asc" : "desc";
        };
        const rowGroups = () => {
          const groups = [];
          for (const row of Array.from(tbody.rows)) {
            if (row.classList.contains("result-detail-row")) continue;
            const detailId = row.dataset.detailRow;
            const detail = detailId ? document.getElementById(detailId) : null;
            groups.push({ row, detail });
          }
          return groups;
        };
        const sortRows = (column, type, direction) => {
          const groups = rowGroups();
          groups.sort((leftGroup, rightGroup) => {
            const left = readValue(leftGroup.row, column, type);
            const right = readValue(rightGroup.row, column, type);
            if (left < right) return -1 * direction;
            if (left > right) return 1 * direction;
            return 0;
          });
          const sortedRows = [];
          for (const group of groups) {
            sortedRows.push(group.row);
            if (group.detail) sortedRows.push(group.detail);
          }
          tbody.append(...sortedRows);
        };
        if (activeButton) {
          setSortDirection(activeButton, activeDirection);
          sortRows(
            activeColumn,
            activeButton.dataset.sortType || "text",
            activeDirection,
          );
        }
        for (const button of buttons) {
          button.addEventListener("click", () => {
            const column = Number(button.dataset.sortColumn);
            const type = button.dataset.sortType || "text";
            const direction = activeColumn === column ? -activeDirection : 1;
            activeColumn = column;
            activeDirection = direction;
            setSortDirection(button, direction);
            sortRows(column, type, direction);
          });
        }
      }
      for (const row of document.querySelectorAll("tr.result-row[data-detail-row]")) {
        if (Array.isArray(restoredState?.expanded)
            && restoredState.expanded.includes(row.dataset.caseId || "")) {
          const detail = document.getElementById(row.dataset.detailRow);
          row.setAttribute("aria-expanded", "true");
          if (detail) detail.hidden = false;
        }
        row.addEventListener("click", (event) => {
          if (event.target.closest("a, button, input, select, textarea")) return;
          const detail = document.getElementById(row.dataset.detailRow);
          if (!detail) return;
          const expanded = row.getAttribute("aria-expanded") === "true";
          row.setAttribute("aria-expanded", expanded ? "false" : "true");
          detail.hidden = expanded;
        });
      }
      const navRows = () =>
        Array.from(document.querySelectorAll("tr.result-row[data-detail-row]"))
          .filter((row) => row.offsetParent !== null);
      const isRowOpen = (row) => row.getAttribute("aria-expanded") === "true";
      const setRowOpen = (row, open) => {
        if (isRowOpen(row) === open) return;
        row.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      };
      const openOnlyRow = (target) => {
        for (const row of document.querySelectorAll("tr.result-row[data-detail-row]")) {
          if (row !== target && isRowOpen(row)) setRowOpen(row, false);
        }
        setRowOpen(target, true);
      };
      const moveViewer = (delta) => {
        const rows = navRows();
        if (!rows.length) return;
        const current = rows.find(isRowOpen);
        let index;
        if (current) {
          index = rows.indexOf(current) + delta;
        } else {
          index = delta > 0 ? 0 : rows.length - 1;
        }
        index = Math.max(0, Math.min(rows.length - 1, index));
        const target = rows[index];
        openOnlyRow(target);
        const detail = document.getElementById(target.dataset.detailRow);
        const focus = (detail && detail.querySelector("[data-exr-viewer]")) || detail || target;
        if (focus && typeof focus.scrollIntoView === "function") {
          focus.scrollIntoView({ block: "center", behavior: "smooth" });
        }
      };
      document.addEventListener("keydown", (event) => {
        if (event.defaultPrevented) return;
        if (event.metaKey || event.ctrlKey || event.altKey) return;
        const target = event.target;
        if (target && target.closest
            && target.closest("input, textarea, select, [contenteditable='true']")) {
          return;
        }
        let delta = 0;
        if (event.key === "ArrowDown" || event.key === "j") delta = 1;
        else if (event.key === "ArrowUp" || event.key === "k") delta = -1;
        else return;
        event.preventDefault();
        moveViewer(delta);
      });
      if (Array.isArray(restoredState?.selected)) {
        for (const checkbox of document.querySelectorAll("[data-result-select]")) {
          checkbox.checked = restoredState.selected.includes(checkbox.dataset.caseId || "");
        }
      }
      if (restoredState?.sections && typeof restoredState.sections === "object") {
        for (const section of document.querySelectorAll("details[data-section-id]")) {
          const sectionId = section.dataset.sectionId || "";
          if (Object.hasOwn(restoredState.sections, sectionId)) {
            section.open = restoredState.sections[sectionId] === true;
          }
        }
      }
      if (restoredState && typeof window !== "undefined") {
        window.__typhoonRestoredReportState = restoredState;
      }
    })();
  </script>"""


def html_report_sort_key(row: dict[str, Any]) -> tuple[str, str, str]:
    render_value = row.get("render_output") or row.get("render_image")
    render_filename = Path(str(render_value)).name if render_value else ""
    return (
        render_filename.lower(),
        str(row.get("suite", "")).lower(),
        str(row.get("key", "")).lower(),
    )


def report_row_sections(row: dict[str, Any]) -> tuple[str, ...]:
    value = row.get("sections")
    if not isinstance(value, list) or not all(isinstance(part, str) for part in value):
        return ()
    return tuple(part for part in value if part)


def build_report_sections(
    row_markup: list[tuple[dict[str, Any], str]],
    headers: str,
) -> str:
    suites: dict[str, dict[str, Any]] = {}
    for row, markup in row_markup:
        suite_name = str(row.get("suite") or "default")
        node = suites.setdefault(suite_name, {"rows": [], "children": {}})
        for section_name in report_row_sections(row):
            node = node["children"].setdefault(
                section_name, {"rows": [], "children": {}}
            )
        node["rows"].append((row, markup))

    table_count = 0

    def descendants(node: dict[str, Any]) -> list[dict[str, Any]]:
        result = [row for row, _markup in node["rows"]]
        for child in node["children"].values():
            result.extend(descendants(child))
        return result

    def summary_stats(node: dict[str, Any]) -> str:
        rows = descendants(node)
        failed = sum(1 for row in rows if is_failure_result(row))
        suspect = sum(1 for row in rows if row.get("suspect") is True)
        flip_values = numeric_flip_values(rows)
        count = len(rows)
        parts = [f"{count} test" + ("" if count == 1 else "s")]
        if failed:
            parts.append(f"{failed} failed")
        if suspect:
            parts.append(f"{suspect} suspect")
        if flip_values:
            parts.append(f"max FLIP {max(flip_values):.3f}")
        return " | ".join(parts)

    def table_markup(
        rows: list[tuple[dict[str, Any], str]], path: tuple[str, ...]
    ) -> str:
        nonlocal table_count
        table_headers = headers
        if table_count:
            table_headers = table_headers.replace(
                "<label>Select <input type=\"checkbox\" data-select-all></label>",
                "Select",
            )
        table_count += 1
        body = "".join(markup for _row, markup in rows)
        table_key = html.escape(
            json.dumps(path, separators=(",", ":")), quote=True
        )
        return (
            f'<table data-sortable-table data-sort-table-key="{table_key}">'
            f"<thead><tr>{table_headers}</tr></thead>"
            f"<tbody>{body}</tbody>"
            "</table>"
        )

    def section_markup(
        name: str, node: dict[str, Any], depth: int, path: tuple[str, ...]
    ) -> str:
        escaped_name = html.escape(name, quote=True)
        escaped_path = html.escape("/".join(path), quote=True)
        escaped_section_id = html.escape(
            json.dumps(path, separators=(",", ":")), quote=True
        )
        direct_table = table_markup(node["rows"], path) if node["rows"] else ""
        children = "".join(
            section_markup(child_name, child, depth + 1, (*path, child_name))
            for child_name, child in sorted(node["children"].items())
        )
        section_class = "result-suite" if depth == 0 else "result-section"
        return (
            f"<details class=\"{section_class}\" data-section-path=\"{escaped_path}\" "
            f"data-section-id=\"{escaped_section_id}\" "
            f"data-section-depth=\"{depth}\" "
            f"style=\"--section-offset: {depth * 44}px; "
            f"--table-header-offset: {(depth + 1) * 44}px; "
            f"--section-z-index: {max(5, 1000 - depth)}\" open>"
            f"<summary><span class=\"section-name\">{escaped_name}</span>"
            f"<span class=\"section-stats\">{summary_stats(node)}</span></summary>"
            f"<div class=\"section-content\">{direct_table}{children}</div>"
            "</details>"
        )

    return "".join(
        section_markup(suite_name, node, 0, (suite_name,))
        for suite_name, node in sorted(suites.items())
    )


def discover_usd_camera(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    match = CAMERA_REL_RE.search(text)
    if match is None:
        return ""
    camera = match.group(1).strip()
    return camera if camera.startswith("/") else f"/{camera}"


def format_report_frame(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:g}"
    return str(value)


def report_case_id(row: dict[str, Any]) -> str:
    return json.dumps(
        [str(row.get("suite", "")), str(row.get("key", ""))],
        separators=(",", ":"),
    )


def json_script_payload(value: object) -> str:
    return json.dumps(value, sort_keys=True).replace("</", "<\\/")


def build_run_comparison_manifest(
    results: list[dict[str, Any]],
    context: RunContext,
) -> dict[str, Any]:
    case_ids = {report_case_id(row) for row in results}
    runs = []
    if not context.output_base.is_dir():
        return {"current_run": context.run_dir.name, "runs": runs}

    current_run = context.run_dir.resolve()
    candidate_dirs = []
    for run_dir in context.output_base.iterdir():
        if not run_dir.is_dir() or run_dir.resolve() == current_run:
            continue
        match = RUN_DIR_RE.match(run_dir.name)
        if match is None:
            continue
        candidate_dirs.append((int(match.group(1)), run_dir))

    for _run_number, run_dir in sorted(candidate_dirs, reverse=True):
        report_path = run_dir / "typhoon-report.json"
        if not report_path.is_file():
            continue
        try:
            other_results = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(other_results, list):
            continue

        cases = {}
        for row in other_results:
            if not isinstance(row, dict):
                continue
            case_id = report_case_id(row)
            if case_id not in case_ids:
                continue
            image_value = row.get("render_image") or row.get("render_output")
            if not image_value:
                continue
            image_path = Path(str(image_value))
            if not image_path.is_absolute():
                image_path = run_dir / image_path
            if not image_path.is_file():
                continue
            cases[case_id] = relative_url_path(image_path, context.run_dir)

        if cases:
            runs.append({"name": run_dir.name, "cases": cases})

    return {"current_run": context.run_dir.name, "runs": runs}


def build_html_report(results: list[dict[str, Any]], context: RunContext) -> str:
    summary = summarize_results(context, results)
    rows = sorted(results, key=html_report_sort_key)
    comparison_manifest = build_run_comparison_manifest(results, context)

    def esc(value: object) -> str:
        return html.escape(str(value), quote=True)

    def rel_field(row: dict[str, Any], name: str) -> str:
        value = row.get(name)
        if not value:
            return ""
        return relpath(Path(str(value)), context.run_dir)

    def image_artifacts(row: dict[str, Any]) -> list[tuple[str, str, str]]:
        artifacts = []
        for field, label, transfer in (
            ("reference_image", "Reference", "linear"),
            ("render_image", "Render", "linear"),
            ("diff_exr", "FLIP", "magma"),
        ):
            path = rel_field(row, field)
            if path:
                artifacts.append((path, label, transfer))
        return artifacts

    def thumbnail_markup(row: dict[str, Any], escaped_key: str) -> str:
        artifacts = image_artifacts(row)
        if not artifacts:
            return ""
        items = []
        case_id = report_case_id(row)
        for path, label, transfer in artifacts:
            reference_link_attrs = ""
            reference_canvas_attrs = ""
            if label == "Reference":
                reference_link_attrs = (
                    " data-reference-thumbnail-link"
                    f' data-default-href="{esc(path)}"'
                    f' data-default-title="{esc(label)}"'
                )
                reference_canvas_attrs = (
                    " data-reference-thumbnail-canvas"
                    f' data-default-thumbnail-src="{esc(path)}"'
                    f' data-default-thumbnail-transfer="{esc(transfer)}"'
                    f' data-default-thumbnail-label="{esc(label)}"'
                )
            items.append(
                f'<a class="thumbnail-link" href="{esc(path)}" title="{esc(label)}"'
                f'{reference_link_attrs}>'
                f'<canvas class="thumbnail-canvas" data-thumbnail-canvas '
                f'data-thumbnail-src="{esc(path)}" '
                f'data-thumbnail-transfer="{esc(transfer)}" '
                f'{reference_canvas_attrs} '
                f'aria-label="{escaped_key} {esc(label)} thumbnail"></canvas>'
                "</a>"
            )
        return (
            f'<div class="thumbnail-strip" data-thumbnail-viewer data-case-id="{esc(case_id)}" '
            f'data-case-label="{escaped_key}">'
            + "".join(items)
            + '<span class="thumbnail-status" data-thumbnail-status></span>'
            + "</div>"
        )

    def usdview_action_markup(row: dict[str, Any]) -> str:
        usd_path = row.get("usd")
        if not usd_path:
            return ""
        camera_path = row.get("camera") or discover_usd_camera(Path(str(usd_path)))
        frame = format_report_frame(row.get("frame"))
        suspect = row.get("suspect") is True
        suspect_label = "Clear suspect" if suspect else "Mark suspect"
        suspect_target = "false" if suspect else "true"
        return (
            '<div class="detail-actions">'
            '<button type="button" class="usdview-button" data-usdview-open '
            f'data-usd-path="{esc(usd_path)}" '
            f'data-camera-path="{esc(camera_path)}" '
            f'data-frame="{esc(frame)}">Open in usdview</button>'
            '<button type="button" class="report-action-button" '
            'data-row-update-threshold>Update threshold</button>'
            '<button type="button" class="report-action-button" '
            'data-row-update-reference>Update reference</button>'
            '<button type="button" class="report-action-button" '
            f'data-row-update-suspect data-suspect-target="{suspect_target}">'
            f'{esc(suspect_label)}</button>'
            '<span class="usdview-status" data-detail-action-status></span>'
            '</div>'
        )

    def selection_cell(row: dict[str, Any]) -> str:
        case_id = report_case_id(row)
        return (
            '<input type="checkbox" data-result-select '
            f'data-case-id="{esc(case_id)}" '
            f'data-suite="{esc(row.get("suite") or "")}" '
            f'data-key="{esc(row.get("key") or "")}" '
            f'data-usd-path="{esc(row.get("usd") or "")}" '
            f'data-reference-path="{esc(row.get("reference") or "")}" '
            f'data-render-path="{esc(row.get("render_image") or row.get("render_output") or "")}" '
            f'data-flip-mean="{esc("" if row.get("flip_mean") is None else row.get("flip_mean"))}" '
            f'aria-label="Select {esc(row.get("key") or "")}">'
        )

    def viewer_markup(row: dict[str, Any], escaped_key: str) -> str:
        reference_src = rel_field(row, "reference_image")
        render_src = rel_field(row, "render_image")
        flip_src = rel_field(row, "diff_exr")
        if not (reference_src or render_src or flip_src):
            return '<div class="detail-empty">No EXR images.</div>'
        initial_label = "Reference" if reference_src else ("Render" if render_src else "FLIP")
        case_id = report_case_id(row)
        return (
            f'<div class="comparison-viewer" data-exr-viewer '
            f'data-case-id="{esc(case_id)}" '
            f'data-reference-src="{esc(reference_src)}" '
            f'data-default-reference-src="{esc(reference_src)}" '
            f'data-reference-label="Reference" '
            f'data-render-src="{esc(render_src)}" '
            f'data-flip-src="{esc(flip_src)}">'
            '<div class="viewer-grid">'
            '<figure class="image-panel image-panel-main">'
            f'<figcaption><span data-comparison-mode>{initial_label}</span> '
            '<span class="hint">(press 1, 2, and 3 to toggle)</span>'
            '<span class="comparison-target" data-comparison-target></span></figcaption>'
            f'<canvas data-main-canvas aria-label="{escaped_key} comparison image"></canvas>'
            '</figure>'
            '<figure class="image-panel image-panel-zoom">'
            '<figcaption>16x Zoom</figcaption>'
            f'<canvas data-zoom-canvas aria-label="{escaped_key} 16x zoom image"></canvas>'
            '</figure>'
            '<section class="pixel-readout" aria-label="Pixel values">'
            '<h2>Pixel</h2>'
            '<div class="pixel-coordinate" data-pixel-coordinate></div>'
            '<table>'
            '<thead><tr><th>Image</th><th>Linear float RGB</th><th>sRGB8</th></tr></thead>'
            '<tbody>'
            '<tr><th data-reference-readout-label>Reference</th><td data-pixel-linear="reference"></td><td data-pixel-srgb="reference"></td></tr>'
            '<tr><th>Render</th><td data-pixel-linear="render"></td><td data-pixel-srgb="render"></td></tr>'
            '<tr><th>Active</th><td data-pixel-linear="active"></td><td data-pixel-srgb="active"></td></tr>'
            '<tr><th>FLIP</th><td data-pixel-linear="flip"></td><td data-pixel-srgb="flip"></td></tr>'
            '</tbody>'
            '</table>'
            '<div class="exr-status" data-exr-status></div>'
            '</section>'
            '</div>'
            '</div>'
        )

    body_rows: list[tuple[dict[str, Any], str]] = []
    for index, row in enumerate(rows):
        flip = row.get("flip_mean")
        threshold = row.get("flip_threshold")
        status = status_label(row.get("status", ""))
        suspect = row.get("suspect") is True
        row_id = f"result-row-{index}"
        detail_id = f"result-detail-{index}"
        escaped_key = esc(row.get("key", ""))
        escaped_case_id = esc(report_case_id(row))
        thumbnails = thumbnail_markup(row, escaped_key)
        detail_content = usdview_action_markup(row) + viewer_markup(row, escaped_key)
        render_output = rel_field(row, "render_output") or rel_field(row, "render_image")
        render_filename = Path(render_output).name if render_output else ""
        status_css = " ".join(part for part in ("status-cell", status_class(status)) if part)
        suspect_marker = '<span class="suspect-badge">suspect</span>' if suspect else ""
        row_class = "result-row suspect-row" if suspect else "result-row"
        cells = [
            sortable_cell(esc(status), sort_value=status, css_class=status_css),
            sortable_cell(
                suspect_marker,
                sort_value="1" if suspect else "0",
                css_class="suspect-cell",
            ),
            sortable_cell(
                "" if flip is None else f"{float(flip):.3f}",
                sort_value="" if flip is None else float(flip),
            ),
            sortable_cell(
                "" if threshold is None else f"{float(threshold):.3f}",
                sort_value="" if threshold is None else float(threshold),
            ),
            sortable_cell(
                esc(render_filename),
                sort_value=render_filename,
            ),
            sortable_cell(thumbnails, sort_value="1" if thumbnails else "0"),
            sortable_cell(selection_cell(row), sort_value="0", css_class="select-cell"),
        ]
        body_rows.append(
            (
                row,
                f'<tr id="{row_id}" class="{row_class}" data-detail-row="{detail_id}" '
                f'data-case-id="{escaped_case_id}" '
                f'aria-expanded="false">'
                + "".join(cells)
                + "</tr>"
                + f'<tr id="{detail_id}" class="result-detail-row" hidden>'
                + f'<td colspan="7"><div class="detail-panel">{detail_content}</div></td>'
                + "</tr>"
            )
        )

    headers = "".join(
        [
            sortable_header("Status", 0),
            sortable_header("Review", 1, "number"),
            sortable_header("Mean FLIP", 2, "number"),
            sortable_header("Threshold", 3, "number"),
            sortable_header("Render", 4, sort_direction="asc"),
            sortable_header("Images", 5, "number"),
            (
                '<th class="select-header">'
                '<label>Select <input type="checkbox" data-select-all></label>'
                '</th>'
            ),
        ]
    )

    comparison_runs = comparison_manifest["runs"]
    comparison_disabled = "" if comparison_runs else " disabled"
    comparison_options = "".join(
        f'<option value="{esc(run["name"])}">{esc(run["name"])}</option>'
        for run in comparison_runs
    )
    comparison_payload = json_script_payload(comparison_manifest)
    nav_flip_mean = format_flip_stat(summary.get("flip_mean"))
    nav_flip_min = format_flip_stat(summary.get("flip_min"))
    nav_flip_max = format_flip_stat(summary.get("flip_max"))
    nav_provider = provider_label(summary.get("provider"))

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" type="image/svg+xml" href="../img/typhoon-color.svg">
  <link rel="alternate icon" type="image/x-icon" href="../favicon.ico">
  <title>Typhoon {esc(summary['run_name'])}</title>
  <style>
{report_palette_css()}    :root {{ --report-sticky-top: 74px; }}
    body {{ margin: 0; font: 14px/1.45 system-ui, sans-serif; background: #111; color: #eee; }}
    main {{ max-width: 1880px; margin: 0 auto; padding: 18px 24px 24px; }}
    .result-suite, .result-section {{ margin: 0 0 12px; }}
    .result-suite > summary, .result-section > summary {{ position: sticky; z-index: var(--section-z-index); top: calc(var(--report-sticky-top) + var(--section-offset)); display: flex; align-items: center; gap: 12px; height: 44px; padding: 9px 11px; cursor: pointer; background: #1b1b1b; border-bottom: 1px solid #333; box-shadow: 0 3px 8px rgba(0, 0, 0, 0.28); box-sizing: border-box; }}
    .result-suite > summary {{ font-size: 17px; font-weight: 700; background: #202020; }}
    .result-section > summary {{ font-size: 14px; font-weight: 700; }}
    .section-content {{ padding: 10px 0 0 14px; border-left: 1px solid #303030; }}
    .section-name {{ flex: 1 1 auto; min-width: 0; overflow: hidden; color: #f3f4f6; text-overflow: ellipsis; white-space: nowrap; }}
    .section-stats {{ flex: 0 1 auto; min-width: 0; overflow: hidden; color: #aaa; font-size: 12px; font-weight: 400; text-overflow: ellipsis; white-space: nowrap; }}
    h1 {{ margin: 0 0 16px; font-size: 24px; }}
    a {{ color: #8ec5ff; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .top-nav {{ position: sticky; z-index: 2000; top: 10px; display: flex; align-items: center; gap: 16px; min-height: 48px; margin: 10px 24px 0; padding: 8px 12px; background: rgba(24, 24, 24, 0.94); border: 1px solid #333; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35); backdrop-filter: blur(10px); box-sizing: border-box; }}
    .top-nav-brand {{ display: inline-flex; align-items: center; gap: 10px; min-width: 0; }}
    .top-nav-logo {{ width: 28px; height: 28px; flex: 0 0 auto; background: center / contain no-repeat url("../img/typhoon-color.svg"); }}
    .top-nav-run {{ color: #fff; font-weight: 700; white-space: nowrap; }}
    .top-nav-stats {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; min-width: 0; color: #bbb; }}
    .top-nav-stat {{ white-space: nowrap; }}
    .top-nav-stat strong {{ color: #fff; font-weight: 700; }}
    .top-nav-provider {{ display: inline-flex; align-items: center; gap: 4px; min-width: 0; max-width: 360px; }}
    .top-nav-provider strong {{ display: inline-block; max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; }}
    .top-nav-controls {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-left: auto; color: #bbb; }}
    .top-nav-controls label {{ color: #eee; font-weight: 700; }}
    .top-nav-controls select {{ color: #eee; background: #181818; border: 1px solid #4a5568; border-radius: 4px; padding: 5px 8px; font: inherit; }}
    .top-nav-controls select:disabled {{ color: #777; border-color: #333; }}
    .run-comparison-status {{ min-height: 20px; color: #aaa; }}
    .top-nav-link {{ color: #dbeafe; white-space: nowrap; }}
    table {{ width: 100%; border-collapse: collapse; background: #181818; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid #303030; text-align: left; vertical-align: top; }}
    th {{ background: #202020; position: sticky; top: calc(var(--report-sticky-top) + var(--table-header-offset, 0px)); z-index: 4; }}
    th button {{ all: unset; display: block; width: 100%; cursor: pointer; color: inherit; }}
    th button::after {{ color: #999; font-size: 12px; margin-left: 6px; }}
    th button[data-sort-direction="asc"]::after {{ content: " \\2191"; }}
    th button[data-sort-direction="desc"]::after {{ content: " \\2193"; }}
    tr.result-row {{ cursor: pointer; }}
    tr.result-row:hover td:not(.status-cell) {{ background: #202020; }}
    tr.result-row[aria-expanded="true"] td {{ border-bottom-color: #4a4a4a; }}
    .result-detail-row td {{ padding: 0 10px 18px; background: #101010; border-bottom: 1px solid #3a3a3a; }}
    .suspect-row td:not(.status-cell) {{ background: #1f1d15; }}
    .suspect-cell {{ white-space: nowrap; }}
    .suspect-badge {{ display: inline-block; padding: 2px 7px; border: 1px solid var(--ty-yellow-bright); color: var(--ty-yellow-bright); background: rgba(253, 194, 83, 0.12); border-radius: 999px; font-size: 12px; font-weight: 700; line-height: 1.3; }}
    .detail-panel {{ padding-top: 16px; }}
    .detail-actions {{ display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }}
    .usdview-button {{ appearance: none; border: 1px solid #4a5568; background: #243244; color: #e5f0ff; border-radius: 4px; padding: 7px 10px; font: inherit; cursor: pointer; }}
    .usdview-button:hover {{ background: #2f4058; border-color: #6b7f99; }}
    .usdview-button:disabled {{ opacity: 0.55; cursor: wait; }}
    .usdview-status {{ color: #bbb; min-height: 20px; }}
    .detail-empty {{ color: #888; }}
    .viewer-grid {{ display: grid; grid-template-columns: minmax(220px, 1fr) minmax(220px, 1fr) minmax(280px, 0.82fr); gap: 12px; align-items: start; }}
    figure {{ margin: 0; min-width: 0; }}
    figcaption {{ margin: 0 0 6px; color: #ddd; font-weight: 700; }}
    .image-panel figcaption {{ min-height: 40px; }}
    .hint {{ color: #aaa; font-weight: 400; }}
    .comparison-target {{ display: block; margin-top: 2px; color: #aaa; font-size: 12px; font-weight: 400; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    canvas {{ display: block; width: 100%; height: auto; max-height: 70vh; object-fit: contain; background: #050505; border: 1px solid #333; image-rendering: pixelated; box-sizing: border-box; }}
    .pixel-readout {{ min-width: 0; background: #181818; border: 1px solid #333; padding: 10px; box-sizing: border-box; }}
    .pixel-readout h2 {{ margin: 0 0 6px; font-size: 14px; }}
    .pixel-coordinate {{ min-height: 20px; margin-bottom: 8px; color: #bbb; }}
    .pixel-readout table {{ background: transparent; table-layout: fixed; }}
    .pixel-readout th, .pixel-readout td {{ padding: 5px 6px; border-bottom: 1px solid #303030; word-break: break-word; }}
    .pixel-readout th:first-child {{ width: 72px; }}
    .exr-status {{ min-height: 20px; margin-top: 8px; color: #fca5a5; }}
    td:nth-child(5) {{ word-break: break-all; color: #bbb; }}
    .select-header label {{ display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }}
    .select-cell {{ text-align: center; vertical-align: middle; }}
    .selection-actions {{ display: inline-flex; align-items: center; gap: 8px; }}
    .report-action-button {{ appearance: none; border: 1px solid #4a5568; background: #243244; color: #e5f0ff; border-radius: 4px; padding: 6px 9px; font: inherit; cursor: pointer; }}
    .report-action-button:hover {{ background: #2f4058; border-color: #6b7f99; }}
    .report-action-button:disabled {{ opacity: 0.55; cursor: wait; }}
    .report-action-status {{ min-height: 20px; color: #bbb; }}
    .thumbnail-strip {{ display: flex; gap: 6px; align-items: center; min-width: 252px; }}
    .thumbnail-link {{ display: inline-flex; align-items: center; justify-content: center; width: 76px; height: 76px; background: #050505; border: 1px solid #333; box-sizing: border-box; }}
    .thumbnail-link:hover {{ border-color: #777; }}
    .thumbnail-canvas {{ display: block; width: auto; height: auto; max-width: 74px; max-height: 74px; border: 0; background: #050505; image-rendering: auto; }}
    .thumbnail-status {{ color: #fca5a5; font-size: 12px; }}
    .status-cell {{ font-weight: 700; white-space: nowrap; }}
    .status-passed {{ background: var(--ty-green); color: #111; }}
    .status-no-ref {{ background: #181818; color: #bbb; }}
    .status-failed-threshold {{ background: var(--ty-red); color: #111; }}
    .status-failed-other {{ background: var(--ty-red-dim); color: #fff; }}
    @media (max-width: 1100px) {{
      .viewer-grid {{ grid-template-columns: 1fr; }}
      .top-nav {{ flex-wrap: wrap; }}
      .top-nav-controls {{ margin-left: 0; }}
      :root {{ --report-sticky-top: 118px; }}
    }}
    @media (max-width: 820px) {{
      main {{ padding: 10px; }}
      .section-content {{ padding-left: 6px; }}
      .top-nav {{ margin: 10px 10px 0; gap: 8px 12px; }}
      .top-nav-stats {{ order: 3; width: 100%; gap: 8px 12px; }}
      .top-nav-controls {{ order: 4; width: 100%; }}
      .top-nav-link {{ margin-left: 0; }}
      :root {{ --report-sticky-top: 164px; }}
    }}
  </style>
</head>
<body>
  <nav class="top-nav" aria-label="Run navigation">
    <a class="top-nav-brand" href="../index.html" aria-label="Results index">
      <span class="top-nav-logo" aria-hidden="true"></span>
      <span class="top-nav-run">{esc(summary['run_name'])}</span>
    </a>
    <div class="top-nav-stats" aria-label="Run statistics">
      <span class="top-nav-stat"><strong>{summary['total']}</strong> rendered</span>
      <span class="top-nav-stat"><strong>{summary['missing_references']}</strong> missing refs</span>
      <span class="top-nav-stat"><strong>{summary['failed']}</strong> failed</span>
      <span class="top-nav-stat"><strong>{summary.get('suspect', 0)}</strong> suspect</span>
      <span class="top-nav-stat top-nav-provider">Provider <strong title="{esc(nav_provider)}">{esc(nav_provider)}</strong></span>
      <span class="top-nav-stat">Mean FLIP <strong>{nav_flip_mean}</strong></span>
      <span class="top-nav-stat">Min <strong>{nav_flip_min}</strong></span>
      <span class="top-nav-stat">Max <strong>{nav_flip_max}</strong></span>
      <span class="top-nav-stat">Started <strong>{esc(summary['started_at'])}</strong></span>
    </div>
    <div class="top-nav-controls" data-run-comparison-controls>
      <div class="selection-actions" data-selection-actions hidden>
        <button type="button" class="report-action-button" data-update-threshold>Update threshold</button>
        <button type="button" class="report-action-button" data-update-reference>Update reference</button>
        <span class="report-action-status" data-report-action-status></span>
      </div>
      <label for="run-comparison-select">Compare against</label>
      <select id="run-comparison-select" data-run-comparison-select{comparison_disabled}>
        <option value="">Reference</option>
        {comparison_options}
      </select>
      <span class="run-comparison-status" data-run-comparison-status></span>
    </div>
    <a class="top-nav-link" href="../index.html">Results index</a>
  </nav>
  <main>
    {build_report_sections(body_rows, headers)}
  </main>
{sortable_table_script()}
  <script type="application/json" id="typhoon-run-comparisons">{comparison_payload}</script>
  <script type="module" src="assets/typhoon-exr-viewer.js"></script>
</body>
</html>
"""

def build_output_index(output_base: Path) -> str:
    summaries = read_run_summaries(output_base)

    def esc(value: object) -> str:
        return html.escape(str(value), quote=True)

    rows = []
    for summary in sorted(summaries, key=lambda item: int(item["run_number"]), reverse=True):
        run_name = str(summary["run_name"])
        rows.append(
            "<tr>"
            + "".join(
                [
                    sortable_cell(
                        f'<a href="{esc(run_name)}/index.html">{esc(run_name)}</a>',
                        sort_value=run_name,
                    ),
                    sortable_cell(
                        esc(summary.get("started_at", "")),
                        sort_value=summary.get("started_at", ""),
                    ),
                    sortable_cell(
                        str(int(summary.get("total", 0))),
                        sort_value=int(summary.get("total", 0)),
                    ),
                    sortable_cell(
                        str(int(summary.get("compared", 0))),
                        sort_value=int(summary.get("compared", 0)),
                    ),
                    optional_number_cell(summary.get("flip_mean")),
                    optional_number_cell(summary.get("flip_min")),
                    optional_number_cell(summary.get("flip_max")),
                    sortable_cell(
                        str(int(summary.get("missing_references", 0))),
                        sort_value=int(summary.get("missing_references", 0)),
                    ),
                    sortable_cell(
                        str(int(summary.get("failed", 0))),
                        sort_value=int(summary.get("failed", 0)),
                    ),
                    sortable_cell(
                        str(int(summary.get("dry_run", 0))),
                        sort_value=int(summary.get("dry_run", 0)),
                    ),
                ]
            )
            + "</tr>"
        )

    headers = "".join(
        [
            sortable_header("Run", 0),
            sortable_header("Started", 1),
            sortable_header("Total", 2, "number"),
            sortable_header("Compared", 3, "number"),
            sortable_header("Mean FLIP", 4, "number"),
            sortable_header("Min FLIP", 5, "number"),
            sortable_header("Max FLIP", 6, "number"),
            sortable_header("Missing References", 7, "number"),
            sortable_header("Failed", 8, "number"),
            sortable_header("Dry-run", 9, "number"),
        ]
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" type="image/svg+xml" href="img/typhoon-color.svg">
  <link rel="alternate icon" type="image/x-icon" href="favicon.ico">
  <title>Typhoon Runs</title>
  <style>
{report_palette_css()}    :root {{ --report-sticky-top: 74px; }}
    body {{ margin: 0; font: 14px/1.45 system-ui, sans-serif; background: #111; color: #eee; }}
    main {{ max-width: 1400px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0 0 16px; font-size: 24px; }}
    a {{ color: #8ec5ff; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    table {{ width: 100%; border-collapse: collapse; background: #181818; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid #303030; text-align: left; }}
    th {{ background: #202020; }}
    th button {{ all: unset; display: block; width: 100%; cursor: pointer; color: inherit; }}
    th button::after {{ color: #999; font-size: 12px; margin-left: 6px; }}
    th button[data-sort-direction="asc"]::after {{ content: " \\2191"; }}
    th button[data-sort-direction="desc"]::after {{ content: " \\2193"; }}
  </style>
</head>
<body>
  <main>
    <h1>Typhoon Runs</h1>
    <table data-sortable-table>
      <thead><tr>{headers}</tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
  </main>
{sortable_table_script()}
</body>
</html>
"""


def read_run_summaries(output_base: Path) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    if not output_base.is_dir():
        return summaries
    for run_dir in output_base.iterdir():
        if not run_dir.is_dir():
            continue
        match = RUN_DIR_RE.match(run_dir.name)
        if not match:
            continue
        summary_path = run_dir / "run-summary.json"
        if summary_path.is_file():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
        else:
            summary = {
                "run_name": run_dir.name,
                "run_number": int(match.group(1)),
                "started_at": "",
                "total": 0,
                "compared": 0,
                "missing_references": 0,
                "failed": 0,
                "dry_run": 0,
            }
        summary.setdefault("run_name", run_dir.name)
        summary.setdefault("run_number", int(match.group(1)))
        summaries.append(summary)
    return summaries


def is_failure_result(row: dict[str, Any]) -> bool:
    return str(row.get("status", "")).startswith("failed")


def relpath(path: Path, start: Path) -> str:
    if not str(path):
        return ""
    try:
        return path.resolve().relative_to(start.resolve()).as_posix()
    except ValueError:
        return str(path)


def relative_url_path(path: Path, start: Path) -> str:
    if not str(path):
        return ""
    try:
        return Path(os.path.relpath(path.resolve(), start.resolve())).as_posix()
    except ValueError:
        return str(path)


def _optional_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(value).expanduser().resolve()


def format_frame_argument(frame: FrameValue) -> str:
    if isinstance(frame, int):
        return str(frame)
    if frame.is_integer():
        return str(int(frame))
    return repr(frame)


def format_command(cmd: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def tail(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]
