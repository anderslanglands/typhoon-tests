from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import html
import json
import re
import shlex
import subprocess
from typing import Any

import pytest

from .config import (
    CaseConfig,
    SuiteConfig,
    format_pattern,
    find_suite_config,
    load_case_config,
    load_suite_config_for_path,
    lookup_case_value,
)
from .images import compare_images


RUN_DIR_RE = re.compile(r"^run-(\d+)$")

IGNORED_DIRS = {
    ".git",
    ".pixi",
    "_output",
    "__pycache__",
    "assets",
    "comparison",
    "reference",
    "renders",
}


@dataclass(frozen=True)
class RunContext:
    output_base: Path
    run_dir: Path
    run_number: int
    started_at: str


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
    skip: str | None
    xfail: str | None
    flip_threshold: float | None


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
    return path.is_dir() and path.name in IGNORED_DIRS


def pytest_collect_file(file_path: Any, parent: pytest.Collector) -> pytest.File | None:
    path = Path(str(file_path))
    root = Path(str(parent.config.rootpath)).resolve()
    collect_unconfigured = bool(parent.config.getoption("--typhoon-collect-unconfigured"))
    if not should_collect_usda(path, root, collect_unconfigured):
        return None
    return TyphoonUsdFile.from_parent(parent, path=path)


def should_collect_usda(path: Path, root: Path, collect_unconfigured: bool) -> bool:
    if path.suffix != ".usda":
        return False
    if any(part in IGNORED_DIRS for part in path.parts):
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
        case = build_case(path)
        item = TyphoonUsdItem.from_parent(self, name=case.key, case=case)
        item.add_marker(pytest.mark.typhoon_usd)
        if case.xfail:
            item.add_marker(pytest.mark.xfail(reason=case.xfail, strict=False))
        return [item]


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
    suite = load_suite_config_for_path(str(path.resolve()))
    case_config = load_case_config(path)
    skip = case_config.skip or lookup_case_value(suite.skip, path, suite.root)
    xfail = case_config.xfail or lookup_case_value(suite.xfail, path, suite.root)
    threshold = case_config.flip_threshold
    if threshold is None:
        threshold = lookup_case_value(suite.thresholds, path, suite.root)
    if threshold is None:
        threshold = suite.default_flip_threshold

    return TyphoonCase(
        path=path,
        suite=suite,
        case_config=case_config,
        key=case_key(path, suite.root),
        skip=skip,
        xfail=xfail,
        flip_threshold=threshold,
    )


def case_key(path: Path, suite_root: Path) -> str:
    try:
        rel = path.relative_to(suite_root).with_suffix("")
    except ValueError:
        rel = Path(path.stem)
    parts = [sanitize_key_part(part) for part in rel.parts]
    return "__".join(part for part in parts if part) or sanitize_key_part(path.stem)


def sanitize_key_part(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value)


def options_from_config(config: pytest.Config) -> TyphoonOptions:
    provider = config.getoption("--typhoon-provider")
    return TyphoonOptions(
        provider=Path(provider).expanduser().resolve() if provider else None,
        run_context=get_run_context(config),
        reference_dir=_optional_path(config.getoption("--typhoon-reference-dir")),
        require_references=bool(config.getoption("--typhoon-require-references")),
        require_thresholds=bool(config.getoption("--typhoon-require-thresholds")),
        dry_run=bool(config.getoption("--typhoon-dry-run")),
    )


def get_run_context(config: pytest.Config) -> RunContext:
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

    context = allocate_run_context(output_base)
    config._typhoon_run_context = context  # type: ignore[attr-defined]
    return context


def allocate_run_context(output_base: Path, started_at: str | None = None) -> RunContext:
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
    render_output = resolve_render_output(case, output_root)
    reference = resolve_reference(case, options)

    result: dict[str, Any] = {
        "suite": case.suite.name,
        "key": case.key,
        "usd": str(case.path),
        "command": [],
        "output_root": str(output_root),
        "render_output": str(render_output),
        "artifact_root": str(artifact_root),
        "reference": str(reference) if reference else None,
        "flip_threshold": case.flip_threshold,
        "status": "pending",
        "run_number": options.run_context.run_number,
        "run_dir": str(options.run_context.run_dir),
        "started_at": options.run_context.started_at,
    }

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

    artifact_root.mkdir(parents=True, exist_ok=True)
    try:
        comparison = compare_images(
            reference_path=reference,
            render_path=render_output,
            artifact_dir=artifact_root,
            key=case.key,
            tonemap=case.suite.tonemap,
            transfer=case.suite.transfer,
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
            "reference_png": str(comparison.reference_png),
            "render_png": str(comparison.render_png),
            "diff_png": str(comparison.diff_png),
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
            f"render: {comparison.render_png}\n"
            f"diff: {comparison.diff_png}",
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
        cmd = ["pixi", "run", "--manifest-path", str(manifest), "usdrender"]

    cmd.extend(case.suite.render_args)
    cmd.extend(case.case_config.render_args)
    cmd.extend([str(case.path), "--outputRoot", str(output_root)])
    return cmd


def resolve_output_root(case: TyphoonCase, options: TyphoonOptions) -> Path:
    return options.run_context.run_dir


def resolve_artifact_root(case: TyphoonCase, options: TyphoonOptions) -> Path:
    return options.run_context.run_dir


def resolve_render_output(case: TyphoonCase, output_root: Path) -> Path:
    if case.case_config.render_output:
        return (output_root / case.case_config.render_output).resolve()
    return (
        output_root
        / format_pattern(case.suite.render_output_pattern, case.path, case.suite)
    ).resolve()


def resolve_reference(case: TyphoonCase, options: TyphoonOptions) -> Path | None:
    if case.case_config.reference:
        reference = Path(case.case_config.reference).expanduser()
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
        / format_pattern(case.suite.reference_pattern, case.path, case.suite)
    ).resolve()


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
    (context.output_base / "index.html").write_text(
        build_output_index(context.output_base),
        encoding="utf-8",
    )


def summarize_results(context: RunContext, results: list[dict[str, Any]]) -> dict[str, Any]:
    compared = [row for row in results if row.get("comparison") == "flip"]
    missing = [row for row in results if row.get("comparison") == "missing-reference"]
    failures = [row for row in results if is_failure_result(row)]
    dry_runs = [row for row in results if row.get("status") == "dry-run"]
    return {
        "run_name": context.run_dir.name,
        "run_number": context.run_number,
        "started_at": context.started_at,
        "run_dir": str(context.run_dir),
        "total": len(results),
        "compared": len(compared),
        "missing_references": len(missing),
        "failed": len(failures),
        "dry_run": len(dry_runs),
    }


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
      const tables = document.querySelectorAll("table[data-sortable-table]");
      for (const table of tables) {
        const tbody = table.tBodies[0];
        if (!tbody) continue;
        const buttons = table.querySelectorAll("th button[data-sort-column]");
        const initialButton = table.querySelector("th button[data-sort-direction]");
        let activeColumn = initialButton ? Number(initialButton.dataset.sortColumn) : -1;
        let activeDirection = initialButton?.dataset.sortDirection === "desc" ? -1 : 1;
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
        const sortRows = (column, type, direction) => {
          const rows = Array.from(tbody.rows);
          rows.sort((leftRow, rightRow) => {
            const left = readValue(leftRow, column, type);
            const right = readValue(rightRow, column, type);
            if (left < right) return -1 * direction;
            if (left > right) return 1 * direction;
            return 0;
          });
          tbody.append(...rows);
        };
        if (initialButton) {
          sortRows(
            activeColumn,
            initialButton.dataset.sortType || "text",
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
    })();
  </script>"""


def html_report_sort_key(row: dict[str, Any]) -> tuple[bool, float, str, str]:
    flip = row.get("flip_mean")
    if flip is None:
        return (True, 0.0, str(row.get("suite")), str(row.get("key")))
    return (False, -float(flip), str(row.get("suite")), str(row.get("key")))


def build_html_report(results: list[dict[str, Any]], context: RunContext) -> str:
    summary = summarize_results(context, results)
    rows = sorted(results, key=html_report_sort_key)

    def esc(value: object) -> str:
        return html.escape(str(value), quote=True)

    body_rows = []
    for row in rows:
        flip = row.get("flip_mean")
        threshold = row.get("flip_threshold")
        status = status_label(row.get("status", ""))
        image_cells = ""
        if row.get("comparison") == "flip":
            image_cells = "".join(
                f'<a href="{esc(relpath(Path(str(row[key])), context.run_dir))}">'
                f'<img src="{esc(relpath(Path(str(row[key])), context.run_dir))}" alt="{esc(label)}"></a>'
                for key, label in (
                    ("reference_png", "reference"),
                    ("render_png", "render"),
                    ("diff_png", "flip diff"),
                )
                if row.get(key)
            )
        status_css = " ".join(part for part in ("status-cell", status_class(status)) if part)
        cells = [
            sortable_cell(esc(row.get("suite", "")), sort_value=row.get("suite", "")),
            sortable_cell(esc(row.get("key", "")), sort_value=row.get("key", "")),
            sortable_cell(esc(status), sort_value=status, css_class=status_css),
            sortable_cell(
                "" if flip is None else f"{float(flip):.6f}",
                sort_value="" if flip is None else float(flip),
            ),
            sortable_cell(
                "" if threshold is None else f"{float(threshold):.6f}",
                sort_value="" if threshold is None else float(threshold),
            ),
            sortable_cell(
                esc(relpath(Path(str(row.get("render_output", ""))), context.run_dir)),
                sort_value=relpath(Path(str(row.get("render_output", ""))), context.run_dir),
            ),
            sortable_cell(image_cells, sort_value="1" if image_cells else "0"),
        ]
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    headers = "".join(
        [
            sortable_header("Suite", 0),
            sortable_header("Case", 1),
            sortable_header("Status", 2),
            sortable_header("Mean FLIP", 3, "number", "desc"),
            sortable_header("Threshold", 4, "number"),
            sortable_header("Render", 5),
            sortable_header("Images", 6, "number"),
        ]
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Typhoon {esc(summary['run_name'])}</title>
  <style>
    body {{ margin: 0; font: 14px/1.45 system-ui, sans-serif; background: #111; color: #eee; }}
    main {{ max-width: 1680px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0 0 16px; font-size: 24px; }}
    a {{ color: #8ec5ff; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .summary {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; color: #bbb; }}
    .summary strong {{ color: #fff; }}
    table {{ width: 100%; border-collapse: collapse; background: #181818; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid #303030; text-align: left; vertical-align: top; }}
    th {{ background: #202020; position: sticky; top: 0; }}
    th button {{ all: unset; display: block; width: 100%; cursor: pointer; color: inherit; }}
    th button::after {{ color: #999; font-size: 12px; margin-left: 6px; }}
    th button[data-sort-direction="asc"]::after {{ content: " \\2191"; }}
    th button[data-sort-direction="desc"]::after {{ content: " \\2193"; }}
    td:nth-child(6) {{ word-break: break-all; color: #bbb; }}
    td:last-child {{ min-width: 240px; }}
    .status-cell {{ font-weight: 700; white-space: nowrap; }}
    .status-passed {{ background: #14532d; color: #dcfce7; }}
    .status-no-ref {{ background: #181818; color: #bbb; }}
    .status-failed-threshold {{ background: #7f1d1d; color: #fee2e2; }}
    .status-failed-other {{ background: #831843; color: #fce7f3; }}
    img {{ width: 76px; height: 76px; object-fit: contain; background: #050505; margin-right: 6px; border: 1px solid #333; }}
  </style>
</head>
<body>
  <main>
    <h1>Typhoon {esc(summary['run_name'])}</h1>
    <div class="summary">
      <span><strong>{summary['total']}</strong> rendered</span>
      <span><strong>{summary['compared']}</strong> compared</span>
      <span><strong>{summary['missing_references']}</strong> missing references</span>
      <span><strong>{summary['failed']}</strong> failed</span>
      <span><strong>{summary['dry_run']}</strong> dry-run</span>
      <span>{esc(summary['started_at'])}</span>
    </div>
    <table data-sortable-table>
      <thead>
        <tr>
          {headers}
        </tr>
      </thead>
      <tbody>
        {''.join(body_rows)}
      </tbody>
    </table>
  </main>
{sortable_table_script()}
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
            sortable_header("Missing References", 4, "number"),
            sortable_header("Failed", 5, "number"),
            sortable_header("Dry-run", 6, "number"),
        ]
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Typhoon Runs</title>
  <style>
    body {{ margin: 0; font: 14px/1.45 system-ui, sans-serif; background: #111; color: #eee; }}
    main {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
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
      <thead>
        <tr>
          {headers}
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
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


def _optional_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(value).expanduser().resolve()


def format_command(cmd: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def tail(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]
