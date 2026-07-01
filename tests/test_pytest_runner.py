from __future__ import annotations

from functools import partial
from html.parser import HTMLParser
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import tomllib
from types import SimpleNamespace

import pytest

import typhoon_tests.pytest_plugin as plugin
import typhoon_tests.report_html as report_html
import typhoon_tests.view_server as view_server
from typhoon_tests.pytest_plugin import RunContext, TyphoonOptions, TyphoonRenderError


def write_suite(tmp_path: Path, *, extra: str = "") -> Path:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "typhoon-suite.toml").write_text(
        """
[suite]
name = "sample"

[render]
args = ["--disableCameraLight"]
output_pattern = "rendered.{stem}.exr"

[comparison]
"""
        + extra,
        encoding="utf-8",
    )
    usd = suite / "case.usda"
    usd.write_text("#usda 1.0\n", encoding="utf-8")
    return usd


def run_context(tmp_path: Path, run_number: int = 1) -> RunContext:
    output_base = tmp_path / "_output"
    run_dir = output_base / f"run-{run_number:04d}"
    return RunContext(
        output_base=output_base,
        run_dir=run_dir,
        run_number=run_number,
        started_at="2026-06-30T00:00:00+00:00",
    )


def options(tmp_path: Path, **overrides: object) -> TyphoonOptions:
    values = {
        "provider": None,
        "run_context": run_context(tmp_path),
        "reference_dir": None,
        "require_references": False,
        "require_thresholds": False,
        "dry_run": True,
    }
    values.update(overrides)
    return TyphoonOptions(**values)


def run_pytest_with_plugin(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{repo_root}{os.pathsep}{pythonpath}" if pythonpath else str(repo_root)
    )
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "typhoon_tests.pytest_plugin", *args],
        cwd=tmp_path,
        check=False,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class ReportHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sort_buttons: list[dict[str, str]] = []
        self.status_cells: list[dict[str, str]] = []
        self.rows: list[list[str]] = []
        self.detail_rows: list[dict[str, str]] = []
        self.exr_viewers: list[dict[str, str]] = []
        self.viewer_canvases: list[dict[str, str]] = []
        self.thumbnail_canvases: list[dict[str, str]] = []
        self.usdview_buttons: list[dict[str, str]] = []
        self.module_scripts: list[str] = []
        self._button: dict[str, str] | None = None
        self._usdview_button: dict[str, str] | None = None
        self._status_cell: dict[str, str] | None = None
        self._row: list[str] | None = None
        self._cell_text: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}
        if tag == "button" and "data-sort-column" in attr_map:
            self._button = {
                "column": attr_map["data-sort-column"],
                "type": attr_map.get("data-sort-type", "text"),
                "direction": attr_map.get("data-sort-direction", ""),
                "label": "",
            }
        if tag == "button" and "data-usdview-open" in attr_map:
            self._usdview_button = {
                "usd": attr_map.get("data-usd-path", ""),
                "camera": attr_map.get("data-camera-path", ""),
                "frame": attr_map.get("data-frame", ""),
                "label": "",
            }
        if tag == "tr":
            classes = attr_map.get("class", "").split()
            if "result-detail-row" in classes:
                self.detail_rows.append(
                    {
                        "id": attr_map.get("id", ""),
                        "hidden": str("hidden" in attr_map).lower(),
                    }
                )
                self._row = None
            elif "result-row" in classes:
                self._row = []
            else:
                self._row = None
        if tag == "td" and self._row is not None:
            self._cell_text = ""
        if tag == "td" and "status-cell" in attr_map.get("class", "").split():
            self._status_cell = {
                "class": attr_map.get("class", ""),
                "sort": attr_map.get("data-sort-value", ""),
                "text": "",
            }
        if tag == "div" and "data-exr-viewer" in attr_map:
            self.exr_viewers.append(
                {
                    "reference": attr_map.get("data-reference-src", ""),
                    "render": attr_map.get("data-render-src", ""),
                    "flip": attr_map.get("data-flip-src", ""),
                }
            )
        if tag == "canvas" and "data-thumbnail-canvas" in attr_map:
            self.thumbnail_canvases.append(
                {
                    "src": attr_map.get("data-thumbnail-src", ""),
                    "transfer": attr_map.get("data-thumbnail-transfer", ""),
                    "label": attr_map.get("aria-label", ""),
                }
            )
        if tag == "canvas" and any(
            name in attr_map
            for name in ("data-main-canvas", "data-zoom-canvas", "data-flip-canvas")
        ):
            self.viewer_canvases.append(
                {
                    "main": str("data-main-canvas" in attr_map).lower(),
                    "zoom": str("data-zoom-canvas" in attr_map).lower(),
                    "flip": str("data-flip-canvas" in attr_map).lower(),
                    "label": attr_map.get("aria-label", ""),
                }
            )
        if tag == "script" and attr_map.get("type") == "module":
            self.module_scripts.append(attr_map.get("src", ""))

    def handle_data(self, data: str) -> None:
        if self._button is not None:
            self._button["label"] += data
        if self._usdview_button is not None:
            self._usdview_button["label"] += data
        if self._cell_text is not None:
            self._cell_text += data
        if self._status_cell is not None:
            self._status_cell["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "button" and self._button is not None:
            self._button["label"] = self._button["label"].strip()
            self.sort_buttons.append(self._button)
            self._button = None
        if tag == "button" and self._usdview_button is not None:
            self._usdview_button["label"] = self._usdview_button["label"].strip()
            self.usdview_buttons.append(self._usdview_button)
            self._usdview_button = None
        if tag == "td" and self._cell_text is not None and self._row is not None:
            self._row.append(self._cell_text.strip())
            self._cell_text = None
        if tag == "td" and self._status_cell is not None:
            self._status_cell["text"] = self._status_cell["text"].strip()
            self.status_cells.append(self._status_cell)
            self._status_cell = None
        if tag == "tr":
            if self._row is not None and self._row:
                self.rows.append(self._row)
            self._row = None


def parse_report(html: str) -> ReportHtmlParser:
    parser = ReportHtmlParser()
    parser.feed(html)
    return parser


def write_report_run(
    output_base: Path,
    run_number: int,
    *,
    key: str,
    status: str = "passed",
) -> Path:
    run_dir = output_base / f"run-{run_number:04d}"
    run_dir.mkdir(parents=True)
    results = [
        {
            "suite": "sample",
            "key": key,
            "status": status,
            "comparison": "flip" if status == "passed" else None,
            "render_output": str(run_dir / f"{key}.exr"),
            "started_at": "2026-06-30T00:00:00+00:00",
        }
    ]
    (run_dir / "typhoon-report.json").write_text(
        json.dumps(results, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "run-summary.json").write_text(
        json.dumps(
            {
                "run_name": run_dir.name,
                "run_number": run_number,
                "started_at": "2026-06-30T00:00:00+00:00",
                "total": 0,
                "compared": 0,
                "missing_references": 0,
                "failed": 0,
                "dry_run": 0,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return run_dir


def test_allocate_run_context_increments_existing_run_directories(tmp_path: Path) -> None:
    output_base = tmp_path / "_output"
    (output_base / "run-0001").mkdir(parents=True)
    (output_base / "run-0003").mkdir()
    (output_base / "notes").mkdir()

    context = plugin.allocate_run_context(
        output_base,
        started_at="2026-06-30T00:00:00+00:00",
    )

    assert context.run_number == 4
    assert context.run_dir == output_base.resolve() / "run-0004"
    assert context.run_dir.is_dir()



def test_next_run_number_handles_more_than_four_digits(tmp_path: Path) -> None:
    output_base = tmp_path / "_output"
    (output_base / "run-9999").mkdir(parents=True)
    (output_base / "run-10000").mkdir()

    assert plugin.next_run_number(output_base) == 10001


def test_invalid_provider_failure_has_reportable_result(tmp_path: Path) -> None:
    usd = write_suite(tmp_path)
    case = plugin.build_case(usd)
    opts = options(tmp_path, provider=tmp_path / "missing-openusd", dry_run=False)

    with pytest.raises(TyphoonRenderError) as excinfo:
        plugin.run_typhoon_case(case, opts)

    assert excinfo.value.result is not None
    assert excinfo.value.result["status"] == "failed-command"
    assert excinfo.value.result["run_dir"] == str(opts.run_context.run_dir)


def test_package_mode_calls_installed_usdrender_with_base_flags(tmp_path: Path) -> None:
    usd = write_suite(tmp_path)
    case = plugin.build_case(usd)
    opts = options(tmp_path)

    cmd = plugin.build_render_command(case, opts, opts.run_context.run_dir)

    assert cmd[:5] == ["usdrender", "--complexity", "high", "--renderer", "Embree"]
    assert "--disableCameraLight" in cmd
    assert cmd[-2:] == ["--outputRoot", str(opts.run_context.run_dir)]


@pytest.mark.parametrize("provider_path_kind", ["directory", "manifest"])
def test_provider_mode_uses_openusd_pixi_task_with_clean_environment(
    tmp_path: Path,
    provider_path_kind: str,
) -> None:
    usd = write_suite(tmp_path)
    provider = tmp_path / "openusd"
    provider.mkdir()
    manifest = provider / "pixi.toml"
    manifest.write_text("[workspace]\nname = 'openusd'\n", encoding="utf-8")
    provider_arg = provider if provider_path_kind == "directory" else manifest
    case = plugin.build_case(usd)
    opts = options(tmp_path, provider=provider_arg)

    cmd = plugin.build_render_command(case, opts, opts.run_context.run_dir)

    assert cmd[:6] == [
        "pixi",
        "run",
        "--manifest-path",
        str(manifest),
        "--clean-env",
        "usdrender",
    ]
    assert "--disableCameraLight" in cmd
    assert "--complexity" not in cmd
    assert "--renderer" not in cmd


@pytest.mark.parametrize("provider_path_kind", ["directory", "manifest"])
def test_provider_dry_run_reports_clean_environment_command(
    tmp_path: Path,
    provider_path_kind: str,
) -> None:
    usd = write_suite(tmp_path)
    provider = tmp_path / "openusd"
    provider.mkdir()
    manifest = provider / "pixi.toml"
    manifest.write_text("[workspace]\nname = 'openusd'\n", encoding="utf-8")
    provider_arg = provider if provider_path_kind == "directory" else manifest

    completed = run_pytest_with_plugin(
        tmp_path,
        str(usd),
        "--typhoon-provider",
        str(provider_arg),
        "--typhoon-dry-run",
        "-s",
        "-q",
    )

    assert completed.returncode == 0, completed.stderr
    report_path = tmp_path / "_output" / "run-0001" / "typhoon-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert len(report) == 1
    assert report[0]["key"] == "case"
    assert report[0]["status"] == "dry-run"
    command = report[0]["command"]
    assert command[:6] == [
        "pixi",
        "run",
        "--manifest-path",
        str(manifest),
        "--clean-env",
        "usdrender",
    ]
    assert "--disableCameraLight" in command
    assert "--complexity" not in command
    assert "--renderer" not in command
    assert plugin.format_command(command) in completed.stdout


def test_frame_spec_parsing_supports_ranges_lists_strides_and_fractional_frames() -> None:
    assert plugin.parse_frame_spec("1:3") == (1, 2, 3)
    assert plugin.parse_frame_spec("3:1") == (3, 2, 1)
    assert plugin.parse_frame_spec("1:5x2,8") == (1, 3, 5, 8)
    assert plugin.parse_frame_spec("1:2x0.5") == (1, 1.5, 2)


@pytest.mark.parametrize(
    "spec",
    ["", "1x2", "1:3x", "1:3x0", "1:3x-1", "1:x"],
)
def test_frame_spec_parsing_rejects_invalid_specs(spec: str) -> None:
    with pytest.raises(ValueError):
        plugin.parse_frame_spec(spec)


def test_integer_frame_format_rejects_fractional_frames(tmp_path: Path) -> None:
    suite = plugin.SuiteConfig(root=tmp_path, name="sample")

    with pytest.raises(ValueError, match="invalid format pattern"):
        plugin.format_pattern(
            "{stem}-embree.{frame:04d}.exr",
            tmp_path / "case.usda",
            suite,
            1.5,
        )


def test_fractional_frame_cases_format_keys_paths_and_commands(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "typhoon-suite.toml").write_text(
        """
[suite]
name = "sample"

[render]
output_pattern = "{stem}.{frame}.exr"

[frames]
case = "1.5"
""",
        encoding="utf-8",
    )
    usd = suite / "case.usda"
    usd.write_text("#usda 1.0\n", encoding="utf-8")

    cases = plugin.build_cases(usd)
    opts = options(tmp_path)
    cmd = plugin.build_render_command(cases[0], opts, opts.run_context.run_dir)

    assert [case.key for case in cases] == ["case__frame_1_5"]
    assert plugin.resolve_render_output(cases[0], opts.run_context.run_dir) == (
        opts.run_context.run_dir / "case.1.5.exr"
    ).resolve()
    frame_arg = cmd.index("--frames")
    assert cmd[frame_arg : frame_arg + 2] == ["--frames", "1.5"]


def test_configured_frame_ranges_expand_cases_and_format_paths(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    reference_dir = tmp_path / "refs"
    suite.mkdir()
    reference_dir.mkdir()
    (suite / "typhoon-suite.toml").write_text(
        """
[suite]
name = "sample"

[render]
output_pattern = "{stem}-embree.{frame:04d}.exr"

[reference]
dir = "../refs"
pattern = "{stem}-embree.{frame:04d}.exr"

[frames]
case = "1:3x2"
""",
        encoding="utf-8",
    )
    usd = suite / "case.usda"
    usd.write_text("#usda 1.0\n", encoding="utf-8")

    cases = plugin.build_cases(usd)
    opts = options(tmp_path)

    assert [case.key for case in cases] == [
        "case__frame_0001",
        "case__frame_0003",
    ]
    assert [case.frame for case in cases] == [1, 3]
    assert plugin.resolve_render_output(
        cases[1], opts.run_context.run_dir
    ) == (opts.run_context.run_dir / "case-embree.0003.exr").resolve()
    assert plugin.resolve_reference(cases[1], opts) == (
        reference_dir / "case-embree.0003.exr"
    ).resolve()

    cmd = plugin.build_render_command(cases[1], opts, opts.run_context.run_dir)

    frame_arg = cmd.index("--frames")
    assert cmd[frame_arg : frame_arg + 2] == ["--frames", "3"]
    assert cmd[-3:] == [str(usd), "--outputRoot", str(opts.run_context.run_dir)]


def test_pytest_collection_expands_configured_frames(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "typhoon-suite.toml").write_text(
        """
[suite]
name = "sample"

[frames]
case = "1:2"
""",
        encoding="utf-8",
    )
    usd = suite / "case.usda"
    usd.write_text("#usda 1.0\n", encoding="utf-8")
    completed = run_pytest_with_plugin(
        tmp_path,
        str(usd),
        "--collect-only",
        "-q",
    )

    assert completed.returncode == 0, completed.stderr
    assert "case.usda::case__frame_0001" in completed.stdout
    assert "case.usda::case__frame_0002" in completed.stdout
    assert "2 tests collected" in completed.stdout


def test_invalid_frame_spec_fails_pytest_collection(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "typhoon-suite.toml").write_text(
        """
[suite]
name = "sample"

[frames]
case = "1:3x0"
""",
        encoding="utf-8",
    )
    usd = suite / "case.usda"
    usd.write_text("#usda 1.0\n", encoding="utf-8")

    completed = run_pytest_with_plugin(
        tmp_path,
        str(usd),
        "--collect-only",
        "-q",
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "invalid frame range for" in output
    assert "zero stride" in output


def test_case_frame_override_formats_case_specific_paths(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "typhoon-suite.toml").write_text(
        """
[suite]
name = "sample"

[frames]
case = "1:3"
""",
        encoding="utf-8",
    )
    (suite / "case.typhoon.toml").write_text(
        """
[frames]
range = "5"

[render]
output = "renders/{stem}.{frame:04d}.exr"

[reference]
path = "refs/{stem}.{frame:04d}.exr"
""",
        encoding="utf-8",
    )
    usd = suite / "case.usda"
    usd.write_text("#usda 1.0\n", encoding="utf-8")

    cases = plugin.build_cases(usd)
    opts = options(tmp_path)

    assert [case.key for case in cases] == ["case__frame_0005"]
    assert plugin.resolve_render_output(cases[0], opts.run_context.run_dir) == (
        opts.run_context.run_dir / "renders" / "case.0005.exr"
    ).resolve()
    assert plugin.resolve_reference(cases[0], opts) == (
        suite / "refs" / "case.0005.exr"
    ).resolve()

    cmd = plugin.build_render_command(cases[0], opts, opts.run_context.run_dir)
    frame_arg = cmd.index("--frames")
    assert cmd[frame_arg : frame_arg + 2] == ["--frames", "5"]

    completed = run_pytest_with_plugin(
        tmp_path,
        str(usd),
        "--collect-only",
        "-q",
    )
    assert completed.returncode == 0, completed.stderr
    assert "case.usda::case__frame_0005" in completed.stdout
    assert "case.usda::case__frame_0001" not in completed.stdout


def test_frame_pattern_without_frame_config_records_failed_config(
    tmp_path: Path,
) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "typhoon-suite.toml").write_text(
        """
[suite]
name = "sample"

[render]
output_pattern = "{stem}-embree.{frame:04d}.exr"
""",
        encoding="utf-8",
    )
    usd = suite / "case.usda"
    usd.write_text("#usda 1.0\n", encoding="utf-8")
    case = plugin.build_case(usd)
    opts = options(tmp_path)

    with pytest.raises(TyphoonRenderError, match="uses .*frame") as excinfo:
        plugin.run_typhoon_case(case, opts)

    assert excinfo.value.result is not None
    assert excinfo.value.result["status"] == "failed-config"
    assert excinfo.value.result["command"] == []
    assert excinfo.value.result["render_output"] is None


def test_pytest_dry_run_report_records_usd_camera(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "typhoon-suite.toml").write_text(
        """
[suite]
name = "sample"

[render]
output_pattern = "{stem}.exr"
""",
        encoding="utf-8",
    )
    usd = suite / "case.usda"
    usd.write_text(
        '#usda 1.0\ndef Scope "Render"\n{\n    def RenderSettings "Settings"\n    {\n        rel camera = </cameras/camera1>\n    }\n}\n',
        encoding="utf-8",
    )

    completed = run_pytest_with_plugin(
        tmp_path,
        str(usd),
        "--typhoon-dry-run",
        "-q",
    )

    assert completed.returncode == 0, completed.stderr
    report_path = tmp_path / "_output" / "run-0001" / "typhoon-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert len(report) == 1
    assert report[0]["key"] == "case"
    assert report[0]["usd"] == str(usd)
    assert report[0]["camera"] == "/cameras/camera1"


def test_pytest_failed_config_writes_report_output(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "typhoon-suite.toml").write_text(
        """
[suite]
name = "sample"

[render]
output_pattern = "{stem}-embree.{frame:04d}.exr"
""",
        encoding="utf-8",
    )
    usd = suite / "case.usda"
    usd.write_text("#usda 1.0\n", encoding="utf-8")

    completed = run_pytest_with_plugin(
        tmp_path,
        str(usd),
        "--typhoon-dry-run",
        "-q",
    )

    assert completed.returncode == 1
    report_path = tmp_path / "_output" / "run-0001" / "typhoon-report.json"
    summary_path = tmp_path / "_output" / "run-0001" / "run-summary.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert report == [
        {
            "artifact_root": str(tmp_path / "_output" / "run-0001"),
            "camera": "",
            "command": [],
            "flip_threshold": 0.04,
            "frame": None,
            "key": "case",
            "output_root": str(tmp_path / "_output" / "run-0001"),
            "reference": None,
            "reference_image": None,
            "render_image": None,
            "render_output": None,
            "run_dir": str(tmp_path / "_output" / "run-0001"),
            "run_number": 1,
            "started_at": report[0]["started_at"],
            "status": "failed-config",
            "suite": "sample",
            "usd": str(usd),
        }
    ]
    assert summary["total"] == 1
    assert summary["failed"] == 1


def test_dry_run_reports_run_directory_without_rendering(tmp_path: Path) -> None:
    usd = write_suite(tmp_path)
    case = plugin.build_case(usd)
    opts = options(tmp_path)

    result = plugin.run_typhoon_case(case, opts)

    assert result["status"] == "dry-run"
    assert result["output_root"] == str(opts.run_context.run_dir)
    assert result["render_output"].endswith("run-0001/rendered.case.exr")
    assert result["command"][0] == "usdrender"


def test_successful_comparison_records_passed_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference_dir = tmp_path / "refs"
    reference_dir.mkdir()
    (reference_dir / "case.png").write_bytes(b"not a real png")
    usd = write_suite(
        tmp_path,
        extra="""
[reference]
dir = "../refs"
pattern = "{stem}.png"
""",
    )
    case = plugin.build_case(usd)
    opts = options(tmp_path, dry_run=False)
    render_output = opts.run_context.run_dir / "rendered.case.exr"

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        render_output.parent.mkdir(parents=True, exist_ok=True)
        render_output.write_bytes(b"not a real exr")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    monkeypatch.setattr(
        plugin,
        "compare_images",
        lambda **kwargs: SimpleNamespace(
            flip_mean=0.01,
            reference_image=tmp_path / "reference.exr",
            render_image=tmp_path / "render.exr",
            diff_exr=tmp_path / "diff.exr",
        ),
    )

    result = plugin.run_typhoon_case(case, opts)

    assert result["status"] == "passed"
    assert result["comparison"] == "flip"
    assert result["flip_mean"] == 0.01


def test_missing_reference_allowed_records_no_ref_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usd = write_suite(
        tmp_path,
        extra="""
[reference]
dir = "../missing-refs"
pattern = "{stem}.png"
""",
    )
    case = plugin.build_case(usd)
    opts = options(tmp_path, dry_run=False)
    render_output = opts.run_context.run_dir / "rendered.case.exr"

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        render_output.parent.mkdir(parents=True, exist_ok=True)
        render_output.write_bytes(b"not a real exr")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    result = plugin.run_typhoon_case(case, opts)

    assert result["status"] == "no-ref"
    assert result["comparison"] == "missing-reference"
    assert result["render_image"] == str(render_output)
    assert "render_png" not in result


def test_require_thresholds_accepts_builtin_default_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference_dir = tmp_path / "refs"
    reference_dir.mkdir()
    (reference_dir / "case.png").write_bytes(b"not a real png")
    usd = write_suite(
        tmp_path,
        extra="""
[reference]
dir = "../refs"
pattern = "{stem}.png"
""",
    )
    case = plugin.build_case(usd)
    opts = options(tmp_path, require_references=True, require_thresholds=True, dry_run=False)
    render_output = opts.run_context.run_dir / "rendered.case.exr"

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        render_output.parent.mkdir(parents=True, exist_ok=True)
        render_output.write_bytes(b"not a real exr")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    monkeypatch.setattr(
        plugin,
        "compare_images",
        lambda **kwargs: SimpleNamespace(
            flip_mean=0.01,
            reference_image=tmp_path / "reference.exr",
            render_image=tmp_path / "render.exr",
            diff_exr=tmp_path / "diff.exr",
        ),
    )

    result = plugin.run_typhoon_case(case, opts)

    assert result["status"] == "passed"
    assert result["flip_threshold"] == 0.04


def test_builtin_default_flip_threshold_fails_above_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference_dir = tmp_path / "refs"
    reference_dir.mkdir()
    (reference_dir / "case.png").write_bytes(b"not a real png")
    usd = write_suite(
        tmp_path,
        extra="""
[reference]
dir = "../refs"
pattern = "{stem}.png"
""",
    )
    case = plugin.build_case(usd)
    opts = options(tmp_path, dry_run=False)
    render_output = opts.run_context.run_dir / "rendered.case.exr"

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        render_output.parent.mkdir(parents=True, exist_ok=True)
        render_output.write_bytes(b"not a real exr")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    monkeypatch.setattr(
        plugin,
        "compare_images",
        lambda **kwargs: SimpleNamespace(
            flip_mean=0.041,
            reference_image=tmp_path / "reference.exr",
            render_image=tmp_path / "render.exr",
            diff_exr=tmp_path / "diff.exr",
        ),
    )

    with pytest.raises(TyphoonRenderError, match="exceeds threshold 0.040000") as excinfo:
        plugin.run_typhoon_case(case, opts)

    assert excinfo.value.result is not None
    assert excinfo.value.result["status"] == "failed-threshold"
    assert excinfo.value.result["flip_threshold"] == 0.04


def test_suite_default_flip_threshold_overrides_builtin_default_at_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference_dir = tmp_path / "refs"
    reference_dir.mkdir()
    (reference_dir / "case.png").write_bytes(b"not a real png")
    usd = write_suite(
        tmp_path,
        extra="""
default_flip_threshold = 0.05

[reference]
dir = "../refs"
pattern = "{stem}.png"
""",
    )
    case = plugin.build_case(usd)
    opts = options(tmp_path, dry_run=False)
    render_output = opts.run_context.run_dir / "rendered.case.exr"

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        render_output.parent.mkdir(parents=True, exist_ok=True)
        render_output.write_bytes(b"not a real exr")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    monkeypatch.setattr(
        plugin,
        "compare_images",
        lambda **kwargs: SimpleNamespace(
            flip_mean=0.045,
            reference_image=tmp_path / "reference.exr",
            render_image=tmp_path / "render.exr",
            diff_exr=tmp_path / "diff.exr",
        ),
    )

    result = plugin.run_typhoon_case(case, opts)

    assert result["status"] == "passed"
    assert result["flip_threshold"] == 0.05


def test_suite_threshold_table_overrides_builtin_default_for_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference_dir = tmp_path / "refs"
    reference_dir.mkdir()
    (reference_dir / "case.png").write_bytes(b"not a real png")
    usd = write_suite(
        tmp_path,
        extra="""
[reference]
dir = "../refs"
pattern = "{stem}.png"

[thresholds]
case = 0.05
""",
    )
    case = plugin.build_case(usd)
    opts = options(tmp_path, dry_run=False)
    render_output = opts.run_context.run_dir / "rendered.case.exr"

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        render_output.parent.mkdir(parents=True, exist_ok=True)
        render_output.write_bytes(b"not a real exr")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    monkeypatch.setattr(
        plugin,
        "compare_images",
        lambda **kwargs: SimpleNamespace(
            flip_mean=0.045,
            reference_image=tmp_path / "reference.exr",
            render_image=tmp_path / "render.exr",
            diff_exr=tmp_path / "diff.exr",
        ),
    )

    result = plugin.run_typhoon_case(case, opts)

    assert result["status"] == "passed"
    assert result["flip_threshold"] == 0.05


def test_adjacent_case_config_threshold_overrides_builtin_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference_dir = tmp_path / "refs"
    reference_dir.mkdir()
    (reference_dir / "case.png").write_bytes(b"not a real png")
    usd = write_suite(
        tmp_path,
        extra="""
[reference]
dir = "../refs"
pattern = "{stem}.png"
""",
    )
    usd.with_suffix(".typhoon.toml").write_text(
        """
[comparison]
flip_threshold = 0.05
""",
        encoding="utf-8",
    )
    case = plugin.build_case(usd)
    opts = options(tmp_path, dry_run=False)
    render_output = opts.run_context.run_dir / "rendered.case.exr"

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        render_output.parent.mkdir(parents=True, exist_ok=True)
        render_output.write_bytes(b"not a real exr")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    monkeypatch.setattr(
        plugin,
        "compare_images",
        lambda **kwargs: SimpleNamespace(
            flip_mean=0.045,
            reference_image=tmp_path / "reference.exr",
            render_image=tmp_path / "render.exr",
            diff_exr=tmp_path / "diff.exr",
        ),
    )

    result = plugin.run_typhoon_case(case, opts)

    assert result["status"] == "passed"
    assert result["flip_threshold"] == 0.05


def test_strict_missing_reference_records_failed_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usd = write_suite(
        tmp_path,
        extra="""
[reference]
dir = "../missing-refs"
pattern = "{stem}.png"
""",
    )
    case = plugin.build_case(usd)
    opts = options(tmp_path, require_references=True, dry_run=False)
    render_output = opts.run_context.run_dir / "rendered.case.exr"

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        render_output.parent.mkdir(parents=True, exist_ok=True)
        render_output.write_bytes(b"not a real exr")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    with pytest.raises(TyphoonRenderError) as excinfo:
        plugin.run_typhoon_case(case, opts)

    assert excinfo.value.result is not None
    assert excinfo.value.result["status"] == "failed-missing-reference"
    assert excinfo.value.result["render_image"] == str(render_output)
    assert "render_png" not in excinfo.value.result


def test_unconfigured_usdas_are_not_collected_by_default(tmp_path: Path) -> None:
    usd = tmp_path / "examples" / "loose.usda"
    usd.parent.mkdir()
    usd.write_text("#usda 1.0\n", encoding="utf-8")

    assert not plugin.should_collect_usda(usd, tmp_path, collect_unconfigured=False)
    assert plugin.should_collect_usda(usd, tmp_path, collect_unconfigured=True)


def test_cases_use_builtin_default_flip_threshold(tmp_path: Path) -> None:
    usd = write_suite(tmp_path)

    assert plugin.build_case(usd).flip_threshold == 0.04


def test_suite_default_flip_threshold_overrides_builtin_default(tmp_path: Path) -> None:
    usd = write_suite(
        tmp_path,
        extra="""
default_flip_threshold = 0.125
""",
    )

    assert plugin.build_case(usd).flip_threshold == 0.125


def test_nested_cases_with_duplicate_stems_get_distinct_keys(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "typhoon-suite.toml").write_text(
        "[suite]\nname = 'sample'\n",
        encoding="utf-8",
    )
    first = suite / "a" / "case.usda"
    second = suite / "b" / "case.usda"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("#usda 1.0\n", encoding="utf-8")
    second.write_text("#usda 1.0\n", encoding="utf-8")

    assert plugin.build_case(first).key == "a__case"
    assert plugin.build_case(second).key == "b__case"


def test_run_outputs_write_per_run_report_and_top_level_index(tmp_path: Path) -> None:
    context = run_context(tmp_path, run_number=7)
    context.run_dir.mkdir(parents=True)
    results = [
        {"suite": "sample", "key": "a", "status": "failed-missing-threshold"},
        {
            "suite": "sample",
            "key": "b",
            "status": "passed",
            "comparison": "flip",
            "render_output": str(context.run_dir / "b.exr"),
        },
    ]

    plugin.write_run_outputs(context, results)

    assert (context.run_dir / "typhoon-report.json").is_file()
    assert (context.run_dir / "run-summary.json").is_file()
    assert (context.run_dir / "index.html").is_file()
    assert (context.run_dir / "assets" / "typhoon-exr-viewer.js").read_bytes() == (
        plugin.REPORT_STATIC_DIR / "typhoon-exr-viewer.js"
    ).read_bytes()
    assert (context.run_dir / "assets" / "typhoon_exr_wasm.wasm").read_bytes() == (
        plugin.REPORT_STATIC_DIR / "typhoon_exr_wasm.wasm"
    ).read_bytes()
    output_index = (context.output_base / "index.html").read_text(encoding="utf-8")
    assert "run-0007/index.html" in output_index
    assert "2026-06-30T00:00:00+00:00" in output_index


def test_html_counts_strict_failures(tmp_path: Path) -> None:
    context = run_context(tmp_path)
    html = plugin.build_html_report(
        [
            {"suite": "sample", "key": "a", "status": "failed-missing-threshold"},
            {"suite": "sample", "key": "b", "status": "failed-missing-reference"},
            {"suite": "sample", "key": "c", "status": "passed", "comparison": "flip"},
        ],
        context,
    )

    assert "<strong>2</strong> failed" in html


def test_html_report_styles_statuses_and_makes_columns_sortable(tmp_path: Path) -> None:
    context = run_context(tmp_path)
    html = plugin.build_html_report(
        [
            {
                "suite": "sample",
                "key": "a",
                "status": "passed",
                "comparison": "flip",
                "flip_mean": 0.01,
                "flip_threshold": 0.02,
                "render_output": str(context.run_dir / "a.exr"),
                "reference_image": str(context.run_dir / "reference" / "a.exr"),
                "render_image": str(context.run_dir / "a.exr"),
                "diff_exr": str(context.run_dir / "flip" / "a.exr"),
            },
            {
                "suite": "sample",
                "key": "g",
                "status": "passed",
                "comparison": "flip",
                "flip_mean": 0.2,
                "flip_threshold": 0.25,
                "render_output": str(context.run_dir / "g.exr"),
                "reference_image": str(context.run_dir / "reference" / "g.exr"),
                "render_image": str(context.run_dir / "g.exr"),
                "diff_exr": str(context.run_dir / "flip" / "g.exr"),
            },
            {
                "suite": "sample",
                "key": "b",
                "status": "no-ref",
                "render_output": str(context.run_dir / "b.exr"),
            },
            {
                "suite": "sample",
                "key": "c",
                "status": "failed-threshold",
                "render_output": str(context.run_dir / "c.exr"),
            },
            {
                "suite": "sample",
                "key": "d",
                "status": "failed-command",
                "render_output": str(context.run_dir / "d.exr"),
            },
            {
                "suite": "sample",
                "key": "e",
                "status": "failed-render",
                "render_output": str(context.run_dir / "e.exr"),
            },
            {
                "suite": "sample",
                "key": "f",
                "status": "dry-run",
                "render_output": str(context.run_dir / "f.exr"),
            },
        ],
        context,
    )
    parser = parse_report(html)

    assert parser.sort_buttons == [
        {"label": "Suite", "column": "0", "type": "text", "direction": ""},
        {"label": "Case", "column": "1", "type": "text", "direction": ""},
        {"label": "Status", "column": "2", "type": "text", "direction": ""},
        {"label": "Mean FLIP", "column": "3", "type": "number", "direction": "desc"},
        {"label": "Threshold", "column": "4", "type": "number", "direction": ""},
        {"label": "Render", "column": "5", "type": "text", "direction": ""},
        {"label": "Images", "column": "6", "type": "number", "direction": ""},
    ]
    assert [row[1] for row in parser.rows[:2]] == ["g", "a"]
    assert [row[3] for row in parser.rows[:2]] == ["0.200000", "0.010000"]
    status_cells = {cell["text"]: cell for cell in parser.status_cells}
    assert status_cells["passed"] == {
        "text": "passed",
        "sort": "passed",
        "class": "status-cell status-passed",
    }
    assert status_cells["no-ref"] == {
        "text": "no-ref",
        "sort": "no-ref",
        "class": "status-cell status-no-ref",
    }
    assert status_cells["failed-threshold"] == {
        "text": "failed-threshold",
        "sort": "failed-threshold",
        "class": "status-cell status-failed-threshold",
    }
    assert status_cells["failed-command"] == {
        "text": "failed-command",
        "sort": "failed-command",
        "class": "status-cell status-failed-other",
    }
    assert status_cells["failed-render"] == {
        "text": "failed-render",
        "sort": "failed-render",
        "class": "status-cell",
    }
    assert status_cells["dry-run"] == {
        "text": "dry-run",
        "sort": "dry-run",
        "class": "status-cell",
    }
    assert ".status-passed { background: #14532d;" in html
    assert ".status-no-ref { background: #181818;" in html
    assert ".status-failed-threshold { background: #7f1d1d;" in html
    assert ".status-failed-other { background: #831843;" in html
    assert 'th button[data-sort-direction="asc"]::after { content: " \\2191"; }' in html
    assert 'th button[data-sort-direction="desc"]::after { content: " \\2193"; }' in html
    assert 'setSortDirection(button, direction);' in html
    assert 'if (initialButton)' in html


def test_html_report_rows_expand_with_exr_canvas_viewer(tmp_path: Path) -> None:
    context = run_context(tmp_path)
    usd = tmp_path / "suite" / "case.usda"
    usd.parent.mkdir()
    usd.write_text(
        '#usda 1.0\ndef Scope "Render"\n{\n    def RenderSettings "Settings"\n    {\n        rel camera = </cameras/camera1>\n    }\n}\n',
        encoding="utf-8",
    )
    html = plugin.build_html_report(
        [
            {
                "suite": "sample",
                "key": "case",
                "status": "passed",
                "comparison": "flip",
                "flip_mean": 0.01,
                "flip_threshold": 0.02,
                "render_output": str(context.run_dir / "case.exr"),
                "usd": str(usd),
                "frame": 4,
                "reference_image": str(context.run_dir / "reference" / "case.png"),
                "render_image": str(context.run_dir / "case.exr"),
                "diff_exr": str(context.run_dir / "flip" / "case.exr"),
            },
            {
                "suite": "sample",
                "key": "render-only",
                "status": "no-ref",
                "comparison": "missing-reference",
                "render_output": str(context.run_dir / "render-only.exr"),
                "render_image": str(context.run_dir / "render-only.exr"),
            },
        ],
        context,
    )
    parser = parse_report(html)

    assert len(parser.rows) == 2
    assert parser.detail_rows == [
        {"id": "result-detail-0", "hidden": "true"},
        {"id": "result-detail-1", "hidden": "true"},
    ]
    assert parser.usdview_buttons == [
        {
            "usd": str(usd),
            "camera": "/cameras/camera1",
            "frame": "4",
            "label": "Open in usdview",
        }
    ]
    assert parser.exr_viewers == [
        {
            "reference": "reference/case.png",
            "render": "case.exr",
            "flip": "flip/case.exr",
        },
        {"reference": "", "render": "render-only.exr", "flip": ""},
    ]
    assert parser.module_scripts == ["assets/typhoon-exr-viewer.js"]
    assert parser.thumbnail_canvases == [
        {
            "src": "reference/case.png",
            "transfer": "linear",
            "label": "case Reference thumbnail",
        },
        {"src": "case.exr", "transfer": "linear", "label": "case Render thumbnail"},
        {"src": "flip/case.exr", "transfer": "display", "label": "case FLIP thumbnail"},
        {
            "src": "render-only.exr",
            "transfer": "linear",
            "label": "render-only Render thumbnail",
        },
    ]
    assert [canvas["main"] for canvas in parser.viewer_canvases] == [
        "true",
        "false",
        "false",
        "true",
        "false",
        "false",
    ]
    assert [canvas["zoom"] for canvas in parser.viewer_canvases] == [
        "false",
        "true",
        "false",
        "false",
        "true",
        "false",
    ]
    assert [canvas["flip"] for canvas in parser.viewer_canvases] == [
        "false",
        "false",
        "true",
        "false",
        "false",
        "true",
    ]
    assert (
        '<tr id="result-row-0" class="result-row" '
        'data-detail-row="result-detail-0" aria-expanded="false">'
    ) in html
    assert '<tr id="result-detail-0" class="result-detail-row" hidden>' in html
    assert '<td colspan="7"><div class="detail-panel">' in html
    assert '(press 1 and 2 to toggle)' in html
    assert '<figcaption>16x Zoom</figcaption>' in html
    assert '<figcaption>FLIP</figcaption>' in html
    assert '<th>Linear float RGB</th><th>sRGB8</th>' in html
    assert 'data-pixel-linear="reference"' in html
    assert 'data-pixel-srgb="render"' in html
    assert '<img' not in html
    assert 'class="thumbnail-strip" data-thumbnail-viewer' in html
    assert 'class="thumbnail-link" href="reference/case.png"' in html
    assert 'data-thumbnail-src="flip/case.exr"' in html
    assert 'const rowGroups = () =>' in html
    assert 'if (row.classList.contains("result-detail-row")) continue;' in html
    assert 'if (group.detail) sortedRows.push(group.detail);' in html
    assert 'row.addEventListener("click", (event) =>' in html
    assert 'row.setAttribute("aria-expanded", expanded ? "false" : "true");' in html
    assert '.viewer-grid {' in html
    assert '.pixel-readout {' in html
    assert 'data-usdview-open' in html
    assert 'Open in usdview' in html
    assert 'canvas {' in html

    viewer_js = (Path(__file__).resolve().parents[1] / "typhoon_tests" / "static" / "typhoon-exr-viewer.js").read_text(
        encoding="utf-8"
    )
    assert 'loadImageSource(viewer.dataset.referenceSrc, "linear")' in viewer_js
    assert 'function decodeBrowserImage(src)' in viewer_js
    assert 'function isExrSource(src)' in viewer_js
    assert 'function drawThumbnail(canvas, image' in viewer_js
    assert 'function initializeThumbnailStrip(strip)' in viewer_js
    assert 'new IntersectionObserver' in viewer_js
    assert 'drawZoom(zoomCanvas, state.active' in viewer_js
    assert 'event.key === "1"' in viewer_js
    assert 'setActiveImage(hoveredViewer, "reference");' in viewer_js
    assert 'event.key === "2"' in viewer_js
    assert 'setActiveImage(hoveredViewer, "render");' in viewer_js
    assert 'fetch("/__typhoon__/usdview"' in viewer_js
    assert 'data-usdview-open' in viewer_js


def test_html_report_usdview_button_uses_report_camera_without_reparsing_usd(
    tmp_path: Path,
) -> None:
    context = run_context(tmp_path)
    missing_usd = tmp_path / "missing" / "case.usda"

    html = plugin.build_html_report(
        [
            {
                "suite": "sample",
                "key": "case",
                "status": "dry-run",
                "usd": str(missing_usd),
                "camera": "/Saved/Camera",
                "frame": 7,
            }
        ],
        context,
    )

    parser = parse_report(html)
    assert parser.usdview_buttons == [
        {
            "usd": str(missing_usd),
            "camera": "/Saved/Camera",
            "frame": "7",
            "label": "Open in usdview",
        }
    ]


def test_html_report_normalizes_legacy_status_labels(tmp_path: Path) -> None:
    context = run_context(tmp_path)
    html = plugin.build_html_report(
        [
            {
                "suite": "sample",
                "key": "a",
                "status": "compared",
                "render_output": str(context.run_dir / "a.exr"),
            },
            {
                "suite": "sample",
                "key": "b",
                "status": "rendered",
                "render_output": str(context.run_dir / "b.exr"),
            },
        ],
        context,
    )

    assert [cell["text"] for cell in parse_report(html).status_cells] == [
        "passed",
        "no-ref",
    ]


def test_regenerate_html_defaults_to_latest_run(tmp_path: Path) -> None:
    output_base = tmp_path / "_output"
    older = write_report_run(output_base, 1, key="older")
    latest = write_report_run(output_base, 2, key="latest")

    written = report_html.regenerate_html(output_root=output_base)

    assert latest / "index.html" in written
    viewer_asset = latest / "assets" / "typhoon-exr-viewer.js"
    wasm_asset = latest / "assets" / "typhoon_exr_wasm.wasm"
    assert viewer_asset in written
    assert wasm_asset in written
    assert viewer_asset.read_bytes() == (
        plugin.REPORT_STATIC_DIR / "typhoon-exr-viewer.js"
    ).read_bytes()
    assert wasm_asset.read_bytes() == (
        plugin.REPORT_STATIC_DIR / "typhoon_exr_wasm.wasm"
    ).read_bytes()
    assert output_base / "index.html" in written
    assert (latest / "index.html").is_file()
    assert "latest" in (latest / "index.html").read_text(encoding="utf-8")
    assert not (older / "index.html").exists()
    summary = json.loads((latest / "run-summary.json").read_text(encoding="utf-8"))
    assert summary["total"] == 1
    assert summary["compared"] == 1


def test_regenerate_html_accepts_specific_run_number(tmp_path: Path) -> None:
    output_base = tmp_path / "_output"
    first = write_report_run(output_base, 1, key="first")
    second = write_report_run(output_base, 2, key="second")

    report_html.regenerate_html(output_root=output_base, run="1")

    assert (first / "index.html").is_file()
    assert "first" in (first / "index.html").read_text(encoding="utf-8")
    assert not (second / "index.html").exists()
    output_index = (output_base / "index.html").read_text(encoding="utf-8")
    assert "run-0001/index.html" in output_index


def test_regenerate_html_all_runs(tmp_path: Path) -> None:
    output_base = tmp_path / "_output"
    first = write_report_run(output_base, 1, key="first")
    second = write_report_run(output_base, 2, key="second", status="dry-run")

    report_html.regenerate_html(output_root=output_base, all_runs=True)

    assert "first" in (first / "index.html").read_text(encoding="utf-8")
    assert "dry-run" in (second / "index.html").read_text(encoding="utf-8")
    output_index = (output_base / "index.html").read_text(encoding="utf-8")
    assert "run-0001/index.html" in output_index
    assert "run-0002/index.html" in output_index


def test_regenerate_html_accepts_run_name_and_path_forms(tmp_path: Path) -> None:
    output_base = tmp_path / "_output"
    first = write_report_run(output_base, 1, key="first")
    second = write_report_run(output_base, 2, key="second")

    report_html.regenerate_html(output_root=output_base, run="run-0002")
    assert "second" in (second / "index.html").read_text(encoding="utf-8")
    assert not (first / "index.html").exists()

    report_html.regenerate_html(output_root=output_base, run=first)
    assert "first" in (first / "index.html").read_text(encoding="utf-8")


def test_regenerate_html_leaves_existing_artifacts_untouched(tmp_path: Path) -> None:
    output_base = tmp_path / "_output"
    run_dir = write_report_run(output_base, 1, key="case")
    artifacts = [
        run_dir / "case.exr",
        run_dir / "reference" / "case.exr",
        run_dir / "flip" / "case.exr",
    ]
    before = {}
    for index, artifact in enumerate(artifacts):
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(f"artifact-{index}".encode("ascii"))
        before[artifact] = (artifact.read_bytes(), artifact.stat().st_mtime_ns)

    report_html.regenerate_html(output_root=output_base, run=run_dir)

    assert {
        artifact: (artifact.read_bytes(), artifact.stat().st_mtime_ns)
        for artifact in artifacts
    } == before


def test_regenerate_html_module_cli_and_pixi_task(tmp_path: Path) -> None:
    output_base = tmp_path / "_output"
    run_dir = write_report_run(output_base, 1, key="cli_case")
    repo_root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "typhoon_tests.report_html",
            "--output-root",
            str(output_base),
        ],
        cwd=repo_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert completed.returncode == 0
    assert f"wrote {run_dir / 'index.html'}" in completed.stdout
    assert completed.stderr == ""
    assert "cli_case" in (run_dir / "index.html").read_text(encoding="utf-8")

    pixi = tomllib.loads((repo_root / "pixi.toml").read_text(encoding="utf-8"))
    assert pixi["target"]["linux-64"]["tasks"]["regenerate-html"] == {
        "cmd": "python -m typhoon_tests.report_html"
    }
    assert pixi["target"]["linux-64"]["tasks"]["build"] == {
        "cmd": "python -m typhoon_tests.build_exr_wasm"
    }
    assert pixi["target"]["linux-64"]["tasks"]["view"] == {
        "cmd": "python -m typhoon_tests.view_server --directory _output --port 8000"
    }


def test_view_server_launches_usdview_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usd = tmp_path / "scene.usda"
    usd.write_text("#usda 1.0\n", encoding="utf-8")
    popen_calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(command: list[str], **kwargs: object) -> object:
        popen_calls.append((command, kwargs))
        return object()

    monkeypatch.setattr(view_server.subprocess, "Popen", fake_popen)

    command = view_server.launch_usdview(
        {"usd": str(usd), "camera": "/cameras/camera1", "frame": 12},
        project_root=tmp_path,
    )

    assert command == [
        "pixi",
        "run",
        "usdview",
        "--renderer",
        "Embree",
        "--disableCameraLight",
        "--camera",
        "/cameras/camera1",
        "--complexity",
        "high",
        "--cf",
        "12",
        str(usd.resolve()),
    ]
    assert popen_calls == [
        (
            command,
            {
                "cwd": str(tmp_path.resolve()),
                "stdout": view_server.subprocess.DEVNULL,
                "stderr": view_server.subprocess.DEVNULL,
                "start_new_session": True,
            },
        )
    ]


def test_view_server_launches_usdview_command_with_typhoon_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usd = tmp_path / "scene.usda"
    usd.write_text("#usda 1.0\n", encoding="utf-8")
    provider = tmp_path / "openusd"
    provider.mkdir()
    manifest = provider / "pixi.toml"
    manifest.write_text("[workspace]\nname = 'openusd'\n", encoding="utf-8")
    popen_calls: list[list[str]] = []

    def fake_popen(command: list[str], **_kwargs: object) -> object:
        popen_calls.append(command)
        return object()

    monkeypatch.setattr(view_server.subprocess, "Popen", fake_popen)

    command = view_server.launch_usdview(
        {"usd": str(usd), "camera": "/cameras/camera1", "frame": 12},
        project_root=tmp_path,
        typhoon_provider=provider,
    )

    assert command == [
        "pixi",
        "run",
        "--manifest-path",
        str(manifest),
        "--clean-env",
        "usdview",
        "--renderer",
        "Embree",
        "--disableCameraLight",
        "--camera",
        "/cameras/camera1",
        "--complexity",
        "high",
        "--cf",
        "12",
        str(usd.resolve()),
    ]
    assert popen_calls == [command]


def test_view_server_endpoint_launches_and_rejects_invalid_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usd = tmp_path / "scene.usda"
    usd.write_text("#usda 1.0\n", encoding="utf-8")
    popen_calls: list[list[str]] = []

    def fake_popen(command: list[str], **_kwargs: object) -> object:
        popen_calls.append(command)
        return object()

    monkeypatch.setattr(view_server.subprocess, "Popen", fake_popen)
    provider = tmp_path / "openusd"
    provider.mkdir()
    manifest = provider / "pixi.toml"
    manifest.write_text("[workspace]\nname = 'openusd'\n", encoding="utf-8")
    handler = partial(
        view_server.TyphoonViewHandler,
        directory=str(tmp_path),
    )
    server = view_server.TyphoonViewServer(
        ("127.0.0.1", 0),
        handler,
        project_root=tmp_path,
        typhoon_provider=provider,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            connection.request(
                "POST",
                view_server.USDVIEW_ENDPOINT,
                body=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            body = json.loads(response.read().decode("utf-8"))
            return response.status, body
        finally:
            connection.close()

    try:
        status, body = post(
            {"usd": str(usd), "camera": "/cameras/camera1", "frame": "12"}
        )
        assert status == 200
        assert body["ok"] is True
        assert popen_calls == [
            [
                "pixi",
                "run",
                "--manifest-path",
                str(manifest),
                "--clean-env",
                "usdview",
                "--renderer",
                "Embree",
                "--disableCameraLight",
                "--camera",
                "/cameras/camera1",
                "--complexity",
                "high",
                "--cf",
                "12",
                str(usd.resolve()),
            ]
        ]

        status, body = post(
            {"usd": str(usd), "camera": "/cameras/bad\x00path", "frame": "12"}
        )
        assert status == 400
        assert body["ok"] is False
        assert popen_calls == [popen_calls[0]]

        status, body = post(
            {"usd": f"{usd}\x00", "camera": "/cameras/camera1", "frame": "12"}
        )
        assert status == 400
        assert body["ok"] is False
        assert popen_calls == [popen_calls[0]]

        status, body = post(
            {"usd": str(usd), "camera": "/cameras/camera1", "frame": "12\x00"}
        )
        assert status == 400
        assert body["ok"] is False
        assert popen_calls == [popen_calls[0]]

        status, body = post(
            {"usd": str(usd), "camera": "/cameras/camera1", "frame": "not-a-frame"}
        )
        assert status == 400
        assert body["ok"] is False
        assert popen_calls == [popen_calls[0]]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_regenerate_html_cli_reports_errors_without_traceback(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    missing_output = tmp_path / "missing-output"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "typhoon_tests.report_html",
            "--output-root",
            str(missing_output),
        ],
        cwd=repo_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "no run directories found" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_regenerate_html_rejects_incompatible_run_selection(tmp_path: Path) -> None:
    output_base = tmp_path / "_output"
    write_report_run(output_base, 1, key="case")

    with pytest.raises(report_html.ReportRegenerationError, match="either --all or --run"):
        report_html.regenerate_html(output_root=output_base, run="1", all_runs=True)


def test_regenerate_html_pixi_task_dry_run_forwards_arguments(tmp_path: Path) -> None:
    if shutil.which("pixi") is None:
        pytest.skip("pixi is not installed")

    repo_root = Path(__file__).resolve().parents[1]
    output_base = tmp_path / "_output"
    completed = subprocess.run(
        [
            "pixi",
            "run",
            "--dry-run",
            "regenerate-html",
            "--output-root",
            str(output_base),
            "--run",
            "run-0001",
        ],
        cwd=repo_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 0
    assert (
        "python -m typhoon_tests.report_html "
        f"--output-root {output_base} --run run-0001"
        in output
    )


def test_regenerate_html_rejects_missing_report_json(tmp_path: Path) -> None:
    run_dir = tmp_path / "_output" / "run-0001"
    run_dir.mkdir(parents=True)

    with pytest.raises(report_html.ReportRegenerationError, match="missing typhoon-report"):
        report_html.regenerate_html(output_root=run_dir.parent, run="run-0001")


def test_regenerate_html_rejects_malformed_report_json(tmp_path: Path) -> None:
    run_dir = tmp_path / "_output" / "run-0001"
    run_dir.mkdir(parents=True)
    (run_dir / "typhoon-report.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(report_html.ReportRegenerationError, match="invalid JSON"):
        report_html.regenerate_html(output_root=run_dir.parent, run="run-0001")


def test_regenerate_html_rejects_wrong_report_shape(tmp_path: Path) -> None:
    run_dir = tmp_path / "_output" / "run-0001"
    run_dir.mkdir(parents=True)
    (run_dir / "typhoon-report.json").write_text(
        json.dumps({"not": "a list"}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(report_html.ReportRegenerationError, match="JSON list"):
        report_html.regenerate_html(output_root=run_dir.parent, run="run-0001")


def test_regenerate_html_rejects_malformed_summary_json(tmp_path: Path) -> None:
    run_dir = write_report_run(tmp_path / "_output", 1, key="case")
    (run_dir / "run-summary.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(report_html.ReportRegenerationError, match="invalid JSON"):
        report_html.regenerate_html(output_root=run_dir.parent, run="run-0001")
