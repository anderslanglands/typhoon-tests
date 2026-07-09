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


def run_context(
    tmp_path: Path,
    run_number: int = 1,
    provider: str | None = None,
) -> RunContext:
    output_base = tmp_path / "_output"
    run_dir = output_base / f"run-{run_number:04d}"
    return RunContext(
        output_base=output_base,
        run_dir=run_dir,
        run_number=run_number,
        started_at="2026-06-30T00:00:00+00:00",
        provider=provider,
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
            "flip_mean": 0.01 if status == "passed" else None,
            "flip_threshold": 0.02 if status == "passed" else None,
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
    assert report[0]["provider"] == str(provider_arg.resolve())
    summary = json.loads(
        (tmp_path / "_output" / "run-0001" / "run-summary.json").read_text(encoding="utf-8")
    )
    assert summary["provider"] == str(provider_arg.resolve())
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


def test_renderer_output_is_captured_as_combined_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usd = write_suite(tmp_path)
    case = plugin.build_case(usd)
    opts = options(tmp_path, dry_run=False)
    render_output = opts.run_context.run_dir / "rendered.case.exr"

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.STDOUT
        render_output.parent.mkdir(parents=True, exist_ok=True)
        render_output.write_bytes(b"not a real exr")
        return SimpleNamespace(returncode=0, stdout="stdout line\nstderr line\n", stderr=None)

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)

    result = plugin.run_typhoon_case(case, opts)

    assert result["status"] == "no-ref"
    assert result["renderer_output"] == "stdout line\nstderr line\n"


def test_renderer_output_is_recorded_on_renderer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usd = write_suite(tmp_path)
    case = plugin.build_case(usd)
    opts = options(tmp_path, dry_run=False)

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.STDOUT
        return SimpleNamespace(returncode=2, stdout="stderr line\n", stderr=None)

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)

    with pytest.raises(plugin.TyphoonRenderError, match="output:\nstderr line") as excinfo:
        plugin.run_typhoon_case(case, opts)

    assert excinfo.value.result["status"] == "failed-render"
    assert excinfo.value.result["returncode"] == 2
    assert excinfo.value.result["renderer_output"] == "stderr line\n"


def test_usda_source_highlighter_escapes_and_wraps_tokens() -> None:
    highlighted = plugin.highlight_usda_source(
        '#usda 1.0\n'
        'def Scope "Saved & <Source>"\n'
        '{\n'
        '    string note = "</script>"\n'
        '    rel target = </World/Looks/Mat>\n'
        '    asset file = @./textures/diffuse.png@\n'
        '    float roughness = 0.25\n'
        '}\n'
    )

    assert '<span class="usd-token usd-comment">#usda 1.0</span>' in highlighted
    assert '<span class="usd-token usd-keyword">def</span>' in highlighted
    assert '<span class="usd-token usd-type">Scope</span>' in highlighted
    assert (
        '<span class="usd-token usd-string">&quot;Saved &amp; &lt;Source&gt;&quot;</span>'
        in highlighted
    )
    assert '<span class="usd-token usd-string">&quot;&lt;/script&gt;&quot;</span>' in highlighted
    assert '<span class="usd-token usd-keyword">rel</span>' in highlighted
    assert '<span class="usd-token usd-path">&lt;/World/Looks/Mat&gt;</span>' in highlighted
    assert '<span class="usd-token usd-asset">@./textures/diffuse.png@</span>' in highlighted
    assert '<span class="usd-token usd-number">0.25</span>' in highlighted
    assert '</script>' not in highlighted


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

    assert [case.key for case in cases] == ["case++frame++1_5"]
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
        "case++frame++0001",
        "case++frame++0003",
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
    assert "case.usda::case++frame++0001" in completed.stdout
    assert "case.usda::case++frame++0002" in completed.stdout
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

    assert [case.key for case in cases] == ["case++frame++0005"]
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
    assert "case.usda::case++frame++0005" in completed.stdout
    assert "case.usda::case++frame++0001" not in completed.stdout


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
    usd_doc = "Fixture doc explains <case> & ampersand."
    usd.write_text(
        '#usda 1.0\n'
        '(\n'
        '    customLayerData = {\n'
        f'        string doc = "{usd_doc}"\n'
        '    }\n'
        '    defaultPrim = "Render"\n'
        ')\n'
        'def Scope "Render"\n{\n    def RenderSettings "Settings"\n    {\n        rel camera = </cameras/camera1>\n    }\n}\n',
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
    assert report[0]["usd_source_name"] == "case.usda"
    assert report[0]["usd_source"] == usd.read_text(encoding="utf-8")
    assert report[0]["usd_doc"] == usd_doc
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
            "case_name": "case",
            "command": [],
            "flip_threshold": 0.04,
            "frame": None,
            "key": "case",
            "relative_path": "case.usda",
            "output_root": str(tmp_path / "_output" / "run-0001"),
            "reference": None,
            "reference_image": None,
            "provider": "package",
            "render_image": None,
            "render_output": None,
            "run_dir": str(tmp_path / "_output" / "run-0001"),
            "run_number": 1,
            "sections": [],
            "started_at": report[0]["started_at"],
            "status": "failed-config",
            "suspect": False,
            "suite": "sample",
            "usd": str(usd),
            "usd_source": "#usda 1.0\n",
            "usd_source_name": "case.usda",
        }
    ]
    assert summary["total"] == 1
    assert summary["failed"] == 1
    assert summary["suspect"] == 0
    assert summary["provider"] == "package"


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


def test_underscore_directories_are_never_collected(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    hidden = suite / "sections" / "_assets" / "support.usda"
    hidden.parent.mkdir(parents=True)
    (suite / "typhoon-suite.toml").write_text("[suite]\nname = \"sample\"\n", encoding="utf-8")
    hidden.write_text("#usda 1.0\n", encoding="utf-8")

    assert not plugin.should_collect_usda(hidden, tmp_path, collect_unconfigured=False)
    assert not plugin.should_collect_usda(hidden, tmp_path, collect_unconfigured=True)
    assert not plugin.should_collect_usda(hidden, hidden.parent, collect_unconfigured=True)


def test_plain_assets_directory_is_collectable(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    usd = suite / "assets" / "case.usda"
    usd.parent.mkdir(parents=True)
    (suite / "typhoon-suite.toml").write_text("[suite]\nname = \"sample\"\n", encoding="utf-8")
    usd.write_text("#usda 1.0\n", encoding="utf-8")

    assert plugin.should_collect_usda(usd, tmp_path, collect_unconfigured=False)



def test_underscore_usda_filename_remains_collectable(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "typhoon-suite.toml").write_text("[suite]\nname = \"sample\"\n", encoding="utf-8")
    usd = suite / "_case.usda"
    usd.write_text("#usda 1.0\n", encoding="utf-8")

    assert plugin.should_collect_usda(usd, tmp_path, collect_unconfigured=False)


def test_pytest_prunes_underscore_directories_without_skips(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    hidden = suite / "_assets" / "support.usda"
    visible = suite / "section" / "case.usda"
    hidden.parent.mkdir(parents=True)
    visible.parent.mkdir(parents=True)
    (suite / "typhoon-suite.toml").write_text("[suite]\nname = \"sample\"\n", encoding="utf-8")
    hidden.write_text("#usda 1.0\n", encoding="utf-8")
    visible.write_text("#usda 1.0\n", encoding="utf-8")

    completed = run_pytest_with_plugin(tmp_path, str(suite), "--collect-only", "-q")

    assert completed.returncode == 0, completed.stderr
    assert "section/case.usda::section+case" in completed.stdout
    assert "support" not in completed.stdout
    assert "1 test collected" in completed.stdout


def test_suite_skip_still_applies_to_visible_case(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "typhoon-suite.toml").write_text(
        "[suite]\nname = \"sample\"\n[skip]\ncase = \"intentional\"\n",
        encoding="utf-8",
    )
    usd = suite / "case.usda"
    usd.write_text("#usda 1.0\n", encoding="utf-8")

    assert plugin.build_case(usd).skip == "intentional"


def test_adjacent_case_config_marks_case_suspect(tmp_path: Path) -> None:
    usd = write_suite(tmp_path)
    case_config = usd.with_suffix(".typhoon.toml")
    case_config.write_text(
        "[test]\nsuspect = true\n",
        encoding="utf-8",
    )

    case = plugin.build_case(usd)
    result = plugin.run_typhoon_case(case, options(tmp_path))

    assert case.suspect is True
    assert result["status"] == "dry-run"
    assert result["suspect"] is True

    case_config.write_text(
        "[test]\nsuspect = false\n",
        encoding="utf-8",
    )

    assert plugin.build_case(usd).suspect is False


def test_case_config_rejects_non_boolean_suspect(tmp_path: Path) -> None:
    usd = write_suite(tmp_path)
    usd.with_suffix(".typhoon.toml").write_text(
        '[test]\nsuspect = "yes"\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="expected bool"):
        plugin.build_case(usd)


def test_cases_use_builtin_default_flip_threshold(tmp_path: Path) -> None:
    usd = write_suite(tmp_path)

    case = plugin.build_case(usd)
    assert case.flip_threshold == 0.04
    assert case.suspect is False


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

    assert plugin.build_case(first).key == "a+case"
    first_case = plugin.build_case(first)
    assert first_case.case_name == "case"
    assert first_case.relative_path == "a/case.usda"
    assert first_case.sections == ("a",)
    assert plugin.format_pattern("{path}.png", first, first_case.suite) == "a/case.png"
    defaults = plugin.SuiteConfig(root=suite, name="sample")
    assert plugin.format_pattern(defaults.render_output_pattern, first, defaults) == "a/case.exr"
    assert plugin.format_pattern(defaults.reference_pattern, second, defaults) == "b/case.png"
    assert plugin.build_case(second).key == "b+case"


def test_nested_case_keys_are_injective_for_paths_and_underscores(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    first = suite / "a" / "b__case.usda"
    second = suite / "a__b" / "case.usda"
    spaced = suite / "a b" / "case.usda"
    underscored = suite / "a_b" / "case.usda"

    keys = {
        plugin.case_key(first, suite),
        plugin.case_key(second, suite),
        plugin.case_key(spaced, suite),
        plugin.case_key(underscored, suite),
    }

    assert len(keys) == 4
    assert plugin.case_key(suite / "a" / "case.usda", suite) == "a+case"
    assert plugin.case_key(suite / "a" / "case.usda", suite) != plugin.case_key(
        suite / "a__case.usda", suite
    )
    assert plugin.case_key(suite / "case.usda", suite, 1) != plugin.case_key(
        suite / "case++frame++0001.usda", suite
    )
    assert plugin.case_key(suite / "case.usda", suite, 1) != plugin.case_key(
        suite / "case" / "frame" / "0001.usda", suite
    )
    assert plugin.case_key(suite / "case.usda", suite, 1.234567) != plugin.case_key(
        suite / "case.usda", suite, 1.234568
    )



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


def test_html_report_top_nav_shows_provider(tmp_path: Path) -> None:
    provider = 'local<foo&"bar'
    html = plugin.build_html_report([], run_context(tmp_path, provider=provider))

    assert (
        'Provider <strong title="local&lt;foo&amp;&quot;bar">'
        'local&lt;foo&amp;&quot;bar</strong>'
    ) in html


def test_html_report_top_nav_defaults_to_package_provider(tmp_path: Path) -> None:
    html = plugin.build_html_report([], run_context(tmp_path))

    assert 'Provider <strong title="package">package</strong>' in html


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
                "suspect": True,
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
                "render_output": str(context.run_dir / "material-fidelity" / "surfaces" / "g.exr"),
                "reference_image": str(context.run_dir / "reference" / "g.exr"),
                "render_image": str(context.run_dir / "material-fidelity" / "surfaces" / "g.exr"),
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
        {"label": "Status", "column": "0", "type": "text", "direction": ""},
        {"label": "Review", "column": "1", "type": "number", "direction": ""},
        {"label": "Mean FLIP", "column": "2", "type": "number", "direction": ""},
        {"label": "Threshold", "column": "3", "type": "number", "direction": ""},
        {"label": "Render", "column": "4", "type": "text", "direction": "asc"},
        {"label": "Images", "column": "5", "type": "number", "direction": ""},
    ]
    assert [row[4] for row in parser.rows] == [
        "a.exr",
        "b.exr",
        "c.exr",
        "d.exr",
        "e.exr",
        "f.exr",
        "g.exr",
    ]
    assert [row[1] for row in parser.rows if row[1]] == ["suspect"]
    assert [row[2] for row in parser.rows if row[2]] == ["0.010", "0.200"]
    assert [row[3] for row in parser.rows if row[3]] == ["0.020", "0.250"]
    assert "<strong>1</strong> suspect" in html
    assert "Mean FLIP <strong>0.105</strong>" in html
    assert "Min <strong>0.010</strong>" in html
    assert "Max <strong>0.200</strong>" in html
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
    expected_palette = {
        "--ty-base00": "#212121",
        "--ty-base01": "#15171c",
        "--ty-base02": "#555555",
        "--ty-base03": "#6c6d70",
        "--ty-base04": "#83868b",
        "--ty-base05": "#9a9fa6",
        "--ty-base06": "#b2b8c2",
        "--ty-base07": "#ffffff",
        "--ty-base08": "#b14956",
        "--ty-base09": "#c6725a",
        "--ty-base0A": "#5485c0",
        "--ty-base0B": "#91b377",
        "--ty-base0C": "#7fcccb",
        "--ty-base0D": "#7b8fa4",
        "--ty-base0E": "#a5779e",
        "--ty-base0F": "#58242b",
        "--ty-base10": "#383838",
        "--ty-base11": "#1c1c1c",
        "--ty-base12": "#ec5f67",
        "--ty-base13": "#fdc253",
        "--ty-base14": "#88e985",
        "--ty-base15": "#58c2c0",
        "--ty-base16": "#5485c0",
        "--ty-base17": "#bf83c0",
    }
    for name, color in expected_palette.items():
        assert f"{name}: {color};" in html
    expected_aliases = {
        "--ty-red": "var(--ty-base08)",
        "--ty-red-bright": "var(--ty-base12)",
        "--ty-red-dim": "var(--ty-base0F)",
        "--ty-yellow": "var(--ty-base09)",
        "--ty-yellow-bright": "var(--ty-base13)",
        "--ty-green": "var(--ty-base0B)",
        "--ty-green-bright": "var(--ty-base14)",
        "--ty-cyan": "var(--ty-base0C)",
        "--ty-cyan-bright": "var(--ty-base15)",
        "--ty-blue": "var(--ty-base0D)",
        "--ty-blue-bright": "var(--ty-base16)",
        "--ty-magenta": "var(--ty-base0E)",
        "--ty-magenta-bright": "var(--ty-base17)",
    }
    for name, value in expected_aliases.items():
        assert f"{name}: {value};" in html
    assert ".status-passed { background: var(--ty-green);" in html
    assert ".status-no-ref { background: #181818;" in html
    assert ".status-failed-threshold { background: var(--ty-red);" in html
    assert ".status-failed-other { background: var(--ty-red-dim);" in html
    assert 'th button[data-sort-direction="asc"]::after { content: " \\2191"; }' in html
    assert 'th button[data-sort-direction="desc"]::after { content: " \\2193"; }' in html
    assert 'setSortDirection(button, direction);' in html
    assert 'if (activeButton)' in html
    assert 'const restoredSort = restoredSorts.get' in html


def test_html_report_groups_nested_sections_and_legacy_root_cases(tmp_path: Path) -> None:
    context = run_context(tmp_path)
    context.run_dir.mkdir(parents=True)
    html = plugin.build_html_report(
        [
            {
                "suite": "sample",
                "key": "surfaces__open_pbr__metal",
                "case_name": "metal",
                "sections": ["Surfaces", "Open PBR"],
                "status": "failed-threshold",
                "comparison": "flip",
                "flip_mean": 0.2,
                "flip_threshold": 0.04,
            },
            {
                "suite": "sample",
                "key": "surfaces__unsafe__glass",
                "case_name": "glass",
                "sections": ["Surfaces", "<Unsafe>"],
                "status": "passed",
                "comparison": "flip",
                "flip_mean": 0.01,
                "flip_threshold": 0.04,
            },
            {"suite": "sample", "key": "legacy-root", "status": "dry-run"},
        ],
        context,
    )
    parser = parse_report(html)

    assert "data-section-path=\"sample\"" in html
    assert "data-section-path=\"sample/Surfaces/Open PBR\"" in html
    assert (
        "data-section-depth=\"0\" "
        "style=\"--section-offset: 0px; --table-header-offset: 44px; "
        "--section-z-index: 1000\""
    ) in html
    assert (
        "data-section-depth=\"2\" "
        "style=\"--section-offset: 88px; --table-header-offset: 132px; "
        "--section-z-index: 998\""
    ) in html
    assert "position: sticky; z-index: var(--section-z-index);" in html
    assert "top: calc(var(--report-sticky-top) + var(--section-offset));" in html
    assert "top: calc(var(--report-sticky-top) + var(--table-header-offset, 0px));" in html
    assert ":root { --report-sticky-top: 118px; }" in html
    assert ":root { --report-sticky-top: 164px; }" in html
    assert "new ResizeObserver(updateStickyTop).observe(topNav);" in html
    assert "`${navBottom + 8}px`" in html
    assert "data-section-path=\"sample/Surfaces/&lt;Unsafe&gt;\"" in html
    assert "<span class=\"section-name\">&lt;Unsafe&gt;</span>" in html
    assert "3 tests | 1 failed | max FLIP 0.200" in html
    assert html.count("<table data-sortable-table data-sort-table-key=") == 3
    assert (
        'data-sort-table-key="[&quot;sample&quot;,&quot;Surfaces&quot;,'
        '&quot;Open PBR&quot;]"'
    ) in html
    assert (
        'data-section-id="[&quot;sample&quot;,&quot;Surfaces&quot;,'
        '&quot;Open PBR&quot;]"'
    ) in html
    assert html.count("data-select-all") == 3
    assert len(parser.rows) == 3
    assert all(len(row) == 7 for row in parser.rows)



def test_html_report_rows_expand_with_exr_canvas_viewer(tmp_path: Path) -> None:
    context = run_context(tmp_path)
    usd = tmp_path / "suite" / "case.usda"
    usd.parent.mkdir()
    usd.write_text(
        '#usda 1.0\ndef Scope "Render"\n{\n    def RenderSettings "Settings"\n    {\n        rel camera = </cameras/camera1>\n    }\n}\n',
        encoding="utf-8",
    )
    saved_usda_source = '#usda 1.0\ndef Scope "Saved & <Source>"\n{\n    string note = "</script>"\n}\n'

    html = plugin.build_html_report(
        [
            {
                "suite": "sample",
                "key": "case",
                "status": "passed",
                "suspect": True,
                "comparison": "flip",
                "flip_mean": 0.01,
                "flip_threshold": 0.02,
                "render_output": str(context.run_dir / "case.exr"),
                "usd": str(usd),
                "usd_source_name": usd.name,
                "usd_source": saved_usda_source,
                "usd_doc": 'Saved fixture doc <value> & "</script>"',
                "camera": "/cameras/camera1",
                "frame": 4,
                "reference_image": str(context.run_dir / "reference" / "case.png"),
                "render_image": str(context.run_dir / "case.exr"),
                "diff_exr": str(context.run_dir / "flip" / "case.exr"),
                "renderer_output": 'stdout <line>\nstderr "</script>"\n',
            },
            {
                "suite": "sample",
                "key": "render-only",
                "status": "no-ref",
                "comparison": "missing-reference",
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
        {"src": "flip/case.exr", "transfer": "magma", "label": "case FLIP thumbnail"},
        {
            "src": "render-only.exr",
            "transfer": "linear",
            "label": "render-only Render thumbnail",
        },
    ]
    assert [canvas["main"] for canvas in parser.viewer_canvases] == [
        "true",
        "false",
        "true",
        "false",
    ]
    assert [canvas["zoom"] for canvas in parser.viewer_canvases] == [
        "false",
        "true",
        "false",
        "true",
    ]
    assert [canvas["flip"] for canvas in parser.viewer_canvases] == [
        "false",
        "false",
        "false",
        "false",
    ]
    assert [canvas["label"] for canvas in parser.viewer_canvases] == [
        "case comparison image",
        "case 16x zoom image",
        "render-only comparison image",
        "render-only 16x zoom image",
    ]
    assert (
        '<tr id="result-row-0" class="result-row suspect-row" '
        'data-detail-row="result-detail-0" '
        'data-case-id="[&quot;sample&quot;,&quot;case&quot;]" '
        'aria-expanded="false">'
    ) in html
    assert '<tr id="result-detail-0" class="result-detail-row" hidden>' in html
    assert '<td colspan="7"><div class="detail-panel">' in html
    detail0 = html[
        html.index('<tr id="result-detail-0"') : html.index('<tr id="result-detail-1"')
    ]
    assert '<section class="fixture-doc" aria-label="Fixture documentation">' in detail0
    assert 'Saved fixture doc &lt;value&gt; &amp; &quot;&lt;/script&gt;&quot;' in detail0
    assert detail0.index('<div class="comparison-viewer"') < detail0.index(
        '<section class="fixture-doc"'
    ) < detail0.index('<details class="renderer-output">')
    assert '<details class="renderer-output"><summary>Renderer output</summary>' in detail0
    assert 'stdout &lt;line&gt;' in detail0
    assert 'stderr &quot;&lt;/script&gt;&quot;' in detail0
    assert '<details class="usda-source"><summary>case.usda</summary>' in detail0
    assert f"<summary>{usd}</summary>" not in detail0
    assert '<span class="usd-token usd-comment">#usda 1.0</span>' in detail0
    assert '<span class="usd-token usd-keyword">def</span>' in detail0
    assert '<span class="usd-token usd-type">Scope</span>' in detail0
    assert (
        '<span class="usd-token usd-string">&quot;Saved &amp; &lt;Source&gt;&quot;</span>'
        in detail0
    )
    assert '<span class="usd-token usd-string">&quot;&lt;/script&gt;&quot;</span>' in detail0
    detail1 = html[html.index('<tr id="result-detail-1"') :]
    assert 'class="fixture-doc"' not in detail1
    assert 'def Scope &quot;Render&quot;' not in detail0
    assert '</script>' not in detail0
    assert '.fixture-doc {' in html
    assert '.renderer-output pre, .usda-source pre {' in html
    assert '.usd-comment { color: var(--ty-base04);' in html
    assert '.usd-keyword { color: var(--ty-base17);' in html
    assert '(press 1, 2, and 3 to toggle)' in html
    assert '<figcaption>16x Zoom</figcaption>' in html
    assert '<figcaption>FLIP</figcaption>' not in html
    assert 'data-flip-canvas' not in html
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
    assert '.image-panel figcaption { min-height: 40px; }' in html
    assert 'overflow: hidden; text-overflow: ellipsis; white-space: nowrap;' in html
    assert '.pixel-readout {' in html
    assert 'data-usdview-open' in html
    assert 'Open in usdview' in html
    assert 'data-select-all' in html
    assert 'data-result-select' in html
    assert 'data-update-threshold' in html
    assert 'data-update-reference' in html
    assert 'data-row-update-threshold' in html
    assert 'data-row-update-reference' in html
    assert 'data-row-update-suspect' in html
    assert 'data-suspect-target="false"' in html
    assert 'Update threshold' in html
    assert 'Update reference' in html
    assert 'Clear suspect' in html
    assert 'canvas {' in html

    viewer_js = (Path(__file__).resolve().parents[1] / "typhoon_tests" / "static" / "typhoon-exr-viewer.js").read_text(
        encoding="utf-8"
    )
    assert "loadReferenceSource(referenceSrc)" in viewer_js
    assert 'const render = renderAtReferenceSize(renderSource, reference);' in viewer_js
    assert 'state.render = renderAtReferenceSize(state.renderSource, state.reference);' in viewer_js
    assert 'loadImageSource(viewer.dataset.flipSrc, "magma")' in viewer_js
    assert 'function magmaColor(value)' in viewer_js
    assert 'image?.transfer === "magma" ? formatFloat(values[0])' in viewer_js
    assert 'value.toExponential(3) : value.toFixed(3)' in viewer_js
    assert 'target.title = targetText;' in viewer_js
    assert 'function decodeBrowserImage(src)' in viewer_js
    assert 'function isExrSource(src)' in viewer_js
    assert 'function drawThumbnail(canvas, image' in viewer_js
    assert 'function initializeThumbnailStrip(strip)' in viewer_js
    assert 'new IntersectionObserver' in viewer_js
    assert 'drawZoom(zoomCanvas, state.active' in viewer_js
    assert 'querySelector("[data-flip-canvas]")' not in viewer_js
    assert 'event.key === "1"' in viewer_js
    assert 'setActiveImage(hoveredViewer, "reference");' in viewer_js
    assert 'event.key === "2"' in viewer_js
    assert 'setActiveImage(hoveredViewer, "render");' in viewer_js
    assert 'event.key === "3"' in viewer_js
    assert 'setActiveImage(hoveredViewer, "flip");' in viewer_js
    assert 'fetch("/__typhoon__/usdview"' in viewer_js
    assert 'data-usdview-open' in viewer_js
    assert 'data-select-all' in viewer_js
    assert 'data-result-select' in viewer_js
    assert 'runReportAction("/__typhoon__/thresholds"' in viewer_js
    assert 'runReportAction("/__typhoon__/references"' in viewer_js
    assert '"/__typhoon__/suspects"' in viewer_js


def test_html_report_derives_fixture_doc_from_saved_usda_source(tmp_path: Path) -> None:
    context = run_context(tmp_path)
    saved_usda_source = (
        '#usda 1.0\n'
        '(\n'
        '    customLayerData = {\n'
        '        string doc = "Saved source doc <fallback>"\n'
        '    }\n'
        ')\n'
    )

    html = plugin.build_html_report(
        [
            {
                "suite": "sample",
                "key": "case",
                "status": "no-ref",
                "comparison": "missing-reference",
                "usd_source_name": "case.usda",
                "usd_source": saved_usda_source,
                "render_image": str(context.run_dir / "case.exr"),
            }
        ],
        context,
    )

    assert (
        '<section class="fixture-doc" aria-label="Fixture documentation">'
        'Saved source doc &lt;fallback&gt;</section>'
    ) in html


def test_nested_report_tables_sort_independently_with_detail_rows(tmp_path: Path) -> None:
    if shutil.which("node") is None:
        pytest.skip("node is required to validate report table sorting")

    script = tmp_path / "report-sort-test.mjs"
    viewer_js = (
        Path(__file__).resolve().parents[1]
        / "typhoon_tests"
        / "static"
        / "typhoon-exr-viewer.js"
    )
    viewer_module = tmp_path / "typhoon-exr-viewer.mjs"
    viewer_module.write_text(viewer_js.read_text(encoding="utf-8"), encoding="utf-8")
    sortable_js = plugin.sortable_table_script().removeprefix("  <script>\n").removesuffix("</script>")
    script.write_text(
        """
        import { pathToFileURL } from "node:url";
        const details = new Map();
        function makeRow(id, value, detailId = "", isDetail = false) {
          const row = {
            id,
            cells: [{ dataset: { sortValue: String(value) }, textContent: String(value) }],
            dataset: detailId ? { detailRow: detailId } : {},
            classList: { contains: (name) => isDetail && name === "result-detail-row" },
            addEventListener() {},
            getAttribute: () => "false",
            setAttribute() {},
          };
          if (isDetail) details.set(id, row);
          return row;
        }
        function makeTable(name, low, high) {
          const lowDetail = makeRow(`${name}-low-detail`, low, "", true);
          const highDetail = makeRow(`${name}-high-detail`, high, "", true);
          const tbody = {
            rows: [
              makeRow(`${name}-low`, low, lowDetail.id), lowDetail,
              makeRow(`${name}-high`, high, highDetail.id), highDetail,
            ],
            append(...rows) { this.rows = rows; },
          };
          const button = {
            dataset: { sortColumn: "0", sortType: "number", sortDirection: "desc" },
            addEventListener() {},
          };
          return {
            dataset: { sortTableKey: name },
            tBodies: [tbody],
            querySelectorAll: () => [button],
            querySelector: () => button,
          };
        }
        const tables = [makeTable("first", 1, 3), makeTable("second", 2, 4)];
        const rootStyle = {};
        let restoredScroll = null;
        let viewerInitializedAtScroll = false;
        const restoredDetail = { hidden: true };
        details.set("restored-detail", restoredDetail);
        const restoredRow = {
          dataset: { caseId: "expanded-case", detailRow: "restored-detail" },
          expanded: "false",
          addEventListener() {},
          getAttribute() { return this.expanded; },
          setAttribute(_name, value) { this.expanded = value; },
        };
        const collapsedSection = {
          dataset: { sectionId: '["suite/with/slash"]' }, open: true,
        };
        const nestedSection = {
          dataset: { sectionId: '["suite","with","slash"]' }, open: false,
        };
        const restoredViewer = {
          dataset: {},
          querySelector() { return null; },
        };
        const topNav = { getBoundingClientRect: () => ({ bottom: 91 }) };
        const storage = {
          getItem() {
            return JSON.stringify({
              sorts: [{ key: "first", column: 0, direction: "asc" }],
              expanded: ["expanded-case"],
              selected: [],
              sections: {
                '["suite/with/slash"]': false,
                '["suite","with","slash"]': true,
              },
              scrollX: 0,
              scrollY: 321,
            });
          },
          removeItem() {},
        };
        globalThis.window = {
          location: { pathname: "/run-0001/index.html" },
          addEventListener() {},
          requestAnimationFrame(callback) { callback(); },
          scrollTo(x, y) {
            viewerInitializedAtScroll = restoredViewer.dataset.exrInitialized === "true";
            restoredScroll = [x, y];
          },
          setTimeout(callback) { callback(); },
          sessionStorage: storage,
        };
        globalThis.ResizeObserver = class {
          constructor(callback) { this.callback = callback; }
          observe() { this.callback(); }
        };
        globalThis.document = {
          documentElement: {
            style: { setProperty(name, value) { rootStyle[name] = value; } },
          },
          querySelector(selector) {
            return selector === ".top-nav" ? topNav : null;
          },
              querySelectorAll(selector) {
                if (selector === "table[data-sortable-table]") return tables;
                if (selector === "tr.result-row[data-detail-row]") return [restoredRow];
                if (selector === "details[data-section-id]") {
                  return [collapsedSection, nestedSection];
                }
                if (selector === "tr.result-detail-row:not([hidden]) [data-exr-viewer]") {
                  return [restoredViewer];
                }
                return [];
              },
              getElementById(id) { return details.get(id) || null; },
              addEventListener() {},
              baseURI: "https://example.test/run-0001/index.html",
            };
            """
            + sortable_js
            + """
            await import(pathToFileURL(process.argv[2]).href);
            await new Promise((resolve) => setTimeout(resolve, 0));
            console.log(JSON.stringify({
          rows: tables.map((table) => table.tBodies[0].rows.map((row) => row.id)),
              stickyTop: rootStyle["--report-sticky-top"],
              restoredScroll,
              viewerInitializedAtScroll,
              expanded: restoredRow.expanded,
              detailHidden: restoredDetail.hidden,
              sectionOpen: [collapsedSection.open, nestedSection.open],
            }));
        """,
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["node", str(script), str(viewer_module)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "rows": [
            ["first-low", "first-low-detail", "first-high", "first-high-detail"],
            ["second-high", "second-high-detail", "second-low", "second-low-detail"],
        ],
        "stickyTop": "99px",
        "restoredScroll": [0, 321],
        "viewerInitializedAtScroll": True,
        "expanded": "true",
        "detailHidden": False,
        "sectionOpen": [False, True],
    }



def test_report_select_all_controls_rows_within_section_tables(tmp_path: Path) -> None:
    if shutil.which("node") is None:
        pytest.skip("node is required to validate report selection")

    viewer_js = Path(__file__).resolve().parents[1] / "typhoon_tests" / "static" / "typhoon-exr-viewer.js"
    viewer_module = tmp_path / "typhoon-exr-viewer.mjs"
    viewer_module.write_text(viewer_js.read_text(encoding="utf-8"), encoding="utf-8")
    script = tmp_path / "report-selection-test.mjs"
    script.write_text(
        """
        import { pathToFileURL } from "node:url";
        function control() {
          return {
            checked: false,
            indeterminate: false,
            hidden: true,
            handlers: {},
            addEventListener(name, handler) { this.handlers[name] = handler; },
            closest() { return null; },
          };
        }
        function table(rows, selectAll) {
          return {
            querySelectorAll(selector) {
              if (selector === "[data-result-select]") return rows;
              if (selector === "[data-select-all]") return [selectAll];
              return [];
            },
          };
        }
        const selectAllA = control();
        const selectAllB = control();
        const actions = control();
        const thresholdButton = control();
        const referenceButton = control();
        const rowsA = [control(), control()];
        const rowsB = [control(), control()];
        const tableA = table(rowsA, selectAllA);
        const tableB = table(rowsB, selectAllB);
        selectAllA.closest = () => null;
        selectAllB.closest = (selector) => selector === "table[data-sortable-table]" ? tableB : null;
        const rows = [...rowsA, ...rowsB];
        globalThis.window = { setTimeout };
        globalThis.document = {
          baseURI: "file:///",
          querySelector(selector) {
            if (selector === "[data-selection-actions]") return actions;
            if (selector === "[data-update-threshold]") return thresholdButton;
            if (selector === "[data-update-reference]") return referenceButton;
            return null;
          },
          querySelectorAll(selector) {
            if (selector === "table[data-sortable-table]") return [tableA, tableB];
            if (selector === "[data-select-all]") return [selectAllA, selectAllB];
            if (selector === "[data-result-select]") return rows;
            if (selector === "[data-result-select]:checked") return rows.filter((row) => row.checked);
            return [];
          },
          getElementById: () => null,
          addEventListener() {},
        };
        await import(pathToFileURL(process.argv[2]).href);

        selectAllA.checked = true;
        selectAllA.handlers.change();
        const afterFirstSection = {
          selected: rows.map((row) => row.checked),
          selectAllA: [selectAllA.checked, selectAllA.indeterminate],
          selectAllB: [selectAllB.checked, selectAllB.indeterminate],
          thresholdLabel: thresholdButton.textContent,
          referenceLabel: referenceButton.textContent,
        };

        rowsB[0].checked = true;
        rowsB[0].handlers.change();
        const afterPartialSecondSection = {
          selected: rows.map((row) => row.checked),
          selectAllA: [selectAllA.checked, selectAllA.indeterminate],
          selectAllB: [selectAllB.checked, selectAllB.indeterminate],
          thresholdLabel: thresholdButton.textContent,
          referenceLabel: referenceButton.textContent,
        };

        selectAllB.checked = true;
        selectAllB.handlers.change();
        const afterSecondSection = {
          selected: rows.map((row) => row.checked),
          actionsHidden: actions.hidden,
          selectAllA: [selectAllA.checked, selectAllA.indeterminate],
          selectAllB: [selectAllB.checked, selectAllB.indeterminate],
          thresholdLabel: thresholdButton.textContent,
          referenceLabel: referenceButton.textContent,
        };

        console.log(JSON.stringify({
          afterFirstSection,
          afterPartialSecondSection,
          afterSecondSection,
        }));
        """,
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["node", str(script), str(viewer_module)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "afterFirstSection": {
            "selected": [True, True, False, False],
            "selectAllA": [True, False],
            "selectAllB": [False, False],
            "thresholdLabel": "Update threshold (2)",
            "referenceLabel": "Update reference (2)",
        },
        "afterPartialSecondSection": {
            "selected": [True, True, True, False],
            "selectAllA": [True, False],
            "selectAllB": [False, True],
            "thresholdLabel": "Update threshold (3)",
            "referenceLabel": "Update reference (3)",
        },
        "afterSecondSection": {
            "selected": [True, True, True, True],
            "actionsHidden": False,
            "selectAllA": [True, False],
            "selectAllB": [True, False],
            "thresholdLabel": "Update threshold (4)",
            "referenceLabel": "Update reference (4)",
        },
    }


def test_report_row_action_targets_only_its_row_and_preserves_ui_state(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        pytest.skip("node is required to validate report row actions")

    viewer_js = (
        Path(__file__).resolve().parents[1]
        / "typhoon_tests"
        / "static"
        / "typhoon-exr-viewer.js"
    )
    viewer_module = tmp_path / "typhoon-exr-viewer.mjs"
    viewer_module.write_text(viewer_js.read_text(encoding="utf-8"), encoding="utf-8")
    script = tmp_path / "report-row-action-test.mjs"
    script.write_text(
        """
        import { pathToFileURL } from "node:url";
        function control(dataset, checked = false) {
          return {
            dataset,
            checked,
            disabled: false,
            handlers: {},
            addEventListener(name, handler) { this.handlers[name] = handler; },
          };
        }
        const selectedControl = control({
          caseId: "selected-case", suite: "sample", key: "selected",
          usdPath: "/selected.usda", referencePath: "/selected.png",
          renderPath: "/selected.exr", flipMean: "0.4",
        }, true);
        const rowControl = control({
          caseId: "target-case", suite: "sample", key: "target",
          usdPath: "/target.usda", referencePath: "/target.png",
          renderPath: "/target.exr", flipMean: "0.2",
        });
        const detailStatus = { textContent: "" };
        const detailActions = {
          querySelector(selector) {
            return selector === "[data-detail-action-status]" ? detailStatus : null;
          },
        };
        const resultRow = {
          querySelector(selector) {
            return selector === "[data-result-select]" ? rowControl : null;
          },
        };
        const detailRow = { previousElementSibling: resultRow };
        function rowButton() {
          const button = control({});
          button.closest = (selector) => {
            if (selector === "tr.result-detail-row") return detailRow;
            if (selector === ".detail-actions") return detailActions;
            return null;
          };
          return button;
        }
        const thresholdRowButton = rowButton();
        const referenceRowButton = rowButton();
        const suspectRowButton = rowButton();
        suspectRowButton.dataset.suspectTarget = "true";
        const clearSuspectRowButton = rowButton();
        clearSuspectRowButton.dataset.suspectTarget = "false";
        const sortButton = {
          dataset: { sortColumn: "2", sortDirection: "desc" },
        };
        const table = {
          dataset: { sortTableKey: "sample/surfaces" },
          querySelector: () => sortButton,
        };
        const expandedRow = { dataset: { caseId: "target-case" } };
        let storedState = null;
        const requests = [];
        let reloadCount = 0;
        let storageBlocked = false;
        const storage = {
          setItem(_key, value) { storedState = JSON.parse(value); },
        };
        globalThis.fetch = async (endpoint, options) => {
          requests.push({ endpoint, body: JSON.parse(options.body) });
          return { ok: true, status: 200, async json() { return { ok: true, updated: 1 }; } };
        };
        globalThis.window = {
          location: {
            pathname: "/run-0001/index.html",
            href: "https://example.test/run-0001/index.html",
            reload() { reloadCount += 1; },
          },
          scrollX: 7,
          scrollY: 413,
          setTimeout(callback) { callback(); },
        };
        Object.defineProperty(window, "sessionStorage", {
          get() {
            if (storageBlocked) throw new DOMException("blocked", "SecurityError");
            return storage;
          },
        });
        globalThis.document = {
          baseURI: "https://example.test/run-0001/index.html",
          querySelector() { return null; },
          querySelectorAll(selector) {
            if (selector === "[data-row-update-threshold]") return [thresholdRowButton];
            if (selector === "[data-row-update-reference]") return [referenceRowButton];
            if (selector === "[data-row-update-suspect]") return [suspectRowButton, clearSuspectRowButton];
            if (selector === "[data-result-select]") return [selectedControl, rowControl];
            if (selector === "[data-result-select]:checked") return [selectedControl];
            if (selector === "table[data-sortable-table]") return [table];
            if (selector === 'tr.result-row[aria-expanded="true"]') return [expandedRow];
            return [];
          },
          getElementById() { return null; },
          addEventListener() {},
        };
        await import(pathToFileURL(process.argv[2]).href);
        thresholdRowButton.handlers.click({ preventDefault() {}, stopPropagation() {} });
        await new Promise((resolve) => setTimeout(resolve, 0));
        storageBlocked = true;
        referenceRowButton.handlers.click({ preventDefault() {}, stopPropagation() {} });
        await new Promise((resolve) => setTimeout(resolve, 0));
        suspectRowButton.handlers.click({ preventDefault() {}, stopPropagation() {} });
        await new Promise((resolve) => setTimeout(resolve, 0));
        clearSuspectRowButton.handlers.click({ preventDefault() {}, stopPropagation() {} });
        await new Promise((resolve) => setTimeout(resolve, 0));
        console.log(JSON.stringify({
          requests,
          storedState,
          status: detailStatus.textContent,
          reloadCount,
        }));
        """,
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["node", str(script), str(viewer_module)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "requests": [
            {
                "endpoint": "/__typhoon__/thresholds",
                "body": {
                    "run": "/run-0001/",
                    "rows": [
                        {
                            "suite": "sample",
                            "key": "target",
                            "usd": "/target.usda",
                            "reference": "/target.png",
                            "render": "/target.exr",
                            "flipMean": "0.2",
                        }
                    ],
                },
            },
            {
                "endpoint": "/__typhoon__/references",
                "body": {
                    "run": "/run-0001/",
                    "rows": [
                        {
                            "suite": "sample",
                            "key": "target",
                            "usd": "/target.usda",
                            "reference": "/target.png",
                            "render": "/target.exr",
                            "flipMean": "0.2",
                        }
                    ],
                },
            },
            {
                "endpoint": "/__typhoon__/suspects",
                "body": {
                    "run": "/run-0001/",
                    "rows": [
                        {
                            "suite": "sample",
                            "key": "target",
                            "usd": "/target.usda",
                            "reference": "/target.png",
                            "render": "/target.exr",
                            "flipMean": "0.2",
                            "suspect": True,
                        }
                    ],
                },
            },
            {
                "endpoint": "/__typhoon__/suspects",
                "body": {
                    "run": "/run-0001/",
                    "rows": [
                        {
                            "suite": "sample",
                            "key": "target",
                            "usd": "/target.usda",
                            "reference": "/target.png",
                            "render": "/target.exr",
                            "flipMean": "0.2",
                            "suspect": False,
                        }
                    ],
                },
            },
        ],
        "storedState": {
            "sorts": [
                {
                    "key": "sample/surfaces",
                    "column": 2,
                    "direction": "desc",
                }
            ],
            "expanded": ["target-case"],
            "selected": ["selected-case"],
            "sections": {},
            "scrollX": 7,
            "scrollY": 413,
        },
        "status": "Updated 1 row; reloading...",
        "reloadCount": 4,
    }



def test_exr_viewer_applies_magma_to_scalar_flip_values(tmp_path: Path) -> None:
    if shutil.which("node") is None:
        pytest.skip("node is required to validate the browser EXR viewer helpers")

    viewer_js = Path(__file__).resolve().parents[1] / "typhoon_tests" / "static" / "typhoon-exr-viewer.js"
    viewer_module = tmp_path / "typhoon-exr-viewer.mjs"
    viewer_module.write_text(viewer_js.read_text(encoding="utf-8"), encoding="utf-8")
    script = tmp_path / "viewer-magma-test.mjs"
    script.write_text(
        """
        import { pathToFileURL } from 'node:url';
        globalThis.window = {};
        globalThis.document = {
          documentElement: {
            style: { setProperty(name, value) { rootStyle[name] = value; } },
          },
          querySelector(selector) {
            return selector === ".top-nav" ? topNav : null;
          },
          baseURI: 'file:///',
          querySelector: () => null,
          querySelectorAll: () => [],
          getElementById: () => null,
          addEventListener: () => {},
        };
        const viewer = await import(pathToFileURL(process.argv[2]).href);
        const magma = viewer.magmaColor(0.5);
        const bytes = viewer.srgbBytesFor({ transfer: 'magma' }, 0.5, 0.5, 0.5);
        const readout = viewer.formatPixelValues({ transfer: 'magma' }, [0.5, 0.25, 0.75]);
        console.log(JSON.stringify({ magma, bytes, readout }));
        """,
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["node", str(script), str(viewer_module)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    result = json.loads(completed.stdout)
    assert result["readout"] == "0.500"
    assert result["bytes"] == [183, 55, 121]
    assert result["bytes"][0] != result["bytes"][1]
    assert result["magma"] == pytest.approx([0.716387, 0.214982, 0.47529])


def test_exr_viewer_scales_render_to_reference_dimensions(tmp_path: Path) -> None:
    if shutil.which("node") is None:
        pytest.skip("node is required to validate the browser EXR viewer helpers")

    viewer_js = (
        Path(__file__).resolve().parents[1]
        / "typhoon_tests"
        / "static"
        / "typhoon-exr-viewer.js"
    )
    viewer_module = tmp_path / "typhoon-exr-viewer.mjs"
    viewer_module.write_text(viewer_js.read_text(encoding="utf-8"), encoding="utf-8")
    script = tmp_path / "viewer-resize-test.mjs"
    script.write_text(
        """
        import { pathToFileURL } from 'node:url';
        globalThis.window = {};
        globalThis.document = {
          documentElement: { style: { setProperty() {} } },
          baseURI: 'file:///',
          querySelector: () => null,
          querySelectorAll: () => [],
          getElementById: () => null,
          addEventListener: () => {},
        };
        const viewer = await import(pathToFileURL(process.argv[2]).href);
        const sourcePixels = new Float32Array([
          1, 0, 0,
          0, 0, 1,
        ]);
        const render = {
          src: 'render.exr', width: 2, height: 1,
          pixels: sourcePixels, transfer: 'linear',
        };
        const reference = {
          src: 'reference.exr', width: 4, height: 2,
          pixels: new Float32Array(4 * 2 * 3), transfer: 'linear',
        };
        const resized = viewer.renderAtReferenceSize(render, reference);
        const unchanged = viewer.renderAtReferenceSize(render, {
          width: 2, height: 1,
        });
        const aspectChanged = viewer.renderAtReferenceSize(render, {
          width: 1, height: 2,
        });
        const highResolution = {
          src: 'large.exr', width: 4, height: 1,
          pixels: new Float32Array([
            1, 0, 0, 0, 1, 0, 0, 0, 1, 1, 1, 1,
          ]), transfer: 'linear',
        };
        const areaFiltered = viewer.renderAtReferenceSize(highResolution, {
          width: 1, height: 1,
        });
        console.log(JSON.stringify({
          width: resized.width,
          height: resized.height,
          pixels: Array.from(resized.pixels),
          sourcePixels: Array.from(sourcePixels),
          sourceIdentityPreserved: unchanged === render,
          transfer: resized.transfer,
          aspectWidth: aspectChanged.width,
          aspectHeight: aspectChanged.height,
          aspectPixels: Array.from(aspectChanged.pixels),
          areaFilteredPixels: Array.from(areaFiltered.pixels),
          src: resized.src,
        }));
        """,
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["node", str(script), str(viewer_module)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    result = json.loads(completed.stdout)
    assert result["width"] == 4
    assert result["height"] == 2
    assert result["pixels"] == pytest.approx(
        [1, 0, 0, 0.75, 0, 0.25, 0.25, 0, 0.75, 0, 0, 1] * 2
    )
    assert result["sourcePixels"] == [1, 0, 0, 0, 0, 1]
    assert result["sourceIdentityPreserved"] is True
    assert result["aspectWidth"] == 1
    assert result["aspectHeight"] == 2
    assert result["aspectPixels"] == pytest.approx(
        [0.5, 0, 0.5, 0.5, 0, 0.5]
    )
    assert result["areaFilteredPixels"] == pytest.approx([0.5, 0.5, 0.5])
    assert result["transfer"] == "linear"
    assert result["src"] == "render.exr"


def test_exr_viewer_shows_render_when_initial_reference_fails(tmp_path: Path) -> None:
    if shutil.which("node") is None:
        pytest.skip("node is required to validate the browser EXR viewer runtime")

    viewer_js = (
        Path(__file__).resolve().parents[1]
        / "typhoon_tests"
        / "static"
        / "typhoon-exr-viewer.js"
    )
    viewer_module = tmp_path / "typhoon-exr-viewer.mjs"
    viewer_module.write_text(viewer_js.read_text(encoding="utf-8"), encoding="utf-8")
    script = tmp_path / "viewer-reference-failure-test.mjs"
    script.write_text(
        """
        import { pathToFileURL } from 'node:url';

        class FakeImage {
          constructor() {
            this.naturalWidth = 3;
            this.naturalHeight = 2;
          }
          set src(value) {
            queueMicrotask(() => {
              if (value.includes('missing')) this.onerror();
              else this.onload();
            });
          }
        }

        function makeDecodeCanvas() {
          const canvas = { width: 0, height: 0 };
          canvas.getContext = () => ({
            drawImage() {},
            getImageData() {
              const data = new Uint8ClampedArray(canvas.width * canvas.height * 4);
              for (let index = 0; index < data.length; index += 4) {
                data[index] = 128;
                data[index + 1] = 64;
                data[index + 2] = 32;
                data[index + 3] = 255;
              }
              return { data };
            },
          });
          return canvas;
        }

        function makeDisplayCanvas() {
          return {
            width: 0,
            height: 0,
            addEventListener() {},
            getContext() {
              return {
                createImageData: (width, height) => ({
                  data: new Uint8ClampedArray(width * height * 4),
                }),
                putImageData() {},
                strokeRect() {},
              };
            },
          };
        }

        globalThis.Image = FakeImage;
        globalThis.window = {};
        globalThis.document = {
          documentElement: { style: { setProperty() {} } },
          baseURI: 'file:///',
          createElement: () => makeDecodeCanvas(),
          querySelector: () => null,
          querySelectorAll: () => [],
          getElementById: () => null,
          addEventListener: () => {},
        };
        const module = await import(pathToFileURL(process.argv[2]).href);
        const status = { textContent: '' };
        const mainCanvas = makeDisplayCanvas();
        const zoomCanvas = makeDisplayCanvas();
        const elements = new Map([
          ['[data-exr-status]', status],
          ['[data-main-canvas]', mainCanvas],
          ['[data-zoom-canvas]', zoomCanvas],
        ]);
        const viewer = {
          dataset: {
            referenceSrc: 'missing.png',
            renderSrc: 'render.png',
            flipSrc: '',
          },
          querySelector: (selector) => elements.get(selector) || null,
        };

        await module.initializeViewer(viewer);
        console.log(JSON.stringify({
          activeName: viewer._typhoonExrState.activeName,
          reference: viewer._typhoonExrState.reference,
          renderWidth: viewer._typhoonExrState.render.width,
          renderHeight: viewer._typhoonExrState.render.height,
          canvasWidth: mainCanvas.width,
          canvasHeight: mainCanvas.height,
          status: status.textContent,
        }));
        """,
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["node", str(script), str(viewer_module)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    result = json.loads(completed.stdout)
    assert result == {
        "activeName": "render",
        "reference": None,
        "renderWidth": 3,
        "renderHeight": 2,
        "canvasWidth": 3,
        "canvasHeight": 2,
        "status": "failed to load missing.png",
    }


def test_exr_viewer_keyboard_three_selects_flip_in_main_canvas(tmp_path: Path) -> None:
    if shutil.which("node") is None:
        pytest.skip("node is required to validate the browser EXR viewer runtime")

    viewer_js = Path(__file__).resolve().parents[1] / "typhoon_tests" / "static" / "typhoon-exr-viewer.js"
    viewer_module = tmp_path / "typhoon-exr-viewer.mjs"
    viewer_module.write_text(viewer_js.read_text(encoding="utf-8"), encoding="utf-8")
    script = tmp_path / "viewer-keyboard-test.mjs"
    script.write_text(
        """
        import { pathToFileURL } from 'node:url';

        function makeImage(values, transfer) {
          return { width: 1, height: 1, pixels: new Float32Array(values), transfer };
        }

        function makeCanvas() {
          return {
            width: 0,
            height: 0,
            puts: [],
            getContext: () => ({
              createImageData: (width, height) => ({ data: new Uint8ClampedArray(width * height * 4) }),
              putImageData(imageData) { this.canvas.puts.push(Array.from(imageData.data.slice(0, 4))); },
              strokeRect() {},
              canvas: null,
            }),
          };
        }

        const mainCanvas = makeCanvas();
        const zoomCanvas = makeCanvas();
        mainCanvas.getContext = () => ({
          createImageData: (width, height) => ({ data: new Uint8ClampedArray(width * height * 4) }),
          putImageData: (imageData) => mainCanvas.puts.push(Array.from(imageData.data.slice(0, 4))),
          strokeRect() {},
        });
        zoomCanvas.getContext = () => ({
          createImageData: (width, height) => ({ data: new Uint8ClampedArray(width * height * 4) }),
          putImageData: (imageData) => zoomCanvas.puts.push(Array.from(imageData.data.slice(0, 4))),
          strokeRect() {},
        });

        const elements = new Map([
          ['[data-main-canvas]', mainCanvas],
          ['[data-zoom-canvas]', zoomCanvas],
          ['[data-comparison-mode]', { textContent: '' }],
          ['[data-reference-readout-label]', { textContent: '' }],
          ['[data-comparison-target]', { textContent: '', title: '' }],
          ['[data-pixel-coordinate]', { textContent: '' }],
          ['[data-pixel-linear="active"]', { textContent: '' }],
          ['[data-pixel-srgb="active"]', { textContent: '' }],
          ['[data-pixel-linear="reference"]', { textContent: '' }],
          ['[data-pixel-srgb="reference"]', { textContent: '' }],
          ['[data-pixel-linear="render"]', { textContent: '' }],
          ['[data-pixel-srgb="render"]', { textContent: '' }],
          ['[data-pixel-linear="flip"]', { textContent: '' }],
          ['[data-pixel-srgb="flip"]', { textContent: '' }],
        ]);
        const viewerListeners = new Map();
        const fakeViewer = {
          dataset: {
            exrInitialized: 'true',
            referenceLabel: 'Reference',
            comparisonTarget: 'Compare: Reference',
          },
          _typhoonExrState: {
            reference: makeImage([0.1, 0.1, 0.1], 'linear'),
            render: makeImage([0.2, 0.2, 0.2], 'linear'),
            flip: makeImage([0.5, 0.5, 0.5], 'magma'),
            active: null,
            activeName: 'reference',
            pointer: [3, 4],
          },
          addEventListener: (name, handler) => viewerListeners.set(name, handler),
          querySelector: (selector) => elements.get(selector) || null,
        };
        fakeViewer._typhoonExrState.active = fakeViewer._typhoonExrState.reference;

        let keydownHandler = null;
        globalThis.window = {};
        globalThis.document = {
          documentElement: {
            style: { setProperty(name, value) { rootStyle[name] = value; } },
          },
          querySelector(selector) {
            return selector === ".top-nav" ? topNav : null;
          },
          baseURI: 'file:///',
          querySelector: () => null,
          querySelectorAll: (selector) => selector === '[data-exr-viewer]' ? [fakeViewer] : [],
          getElementById: () => null,
          addEventListener: (name, handler) => {
            if (name === 'keydown') keydownHandler = handler;
          },
        };

        await import(pathToFileURL(process.argv[2]).href);
        viewerListeners.get('mouseenter')();
        let prevented = false;
        keydownHandler({ key: '3', preventDefault: () => { prevented = true; } });

        console.log(JSON.stringify({
          activeName: fakeViewer._typhoonExrState.activeName,
          mode: elements.get('[data-comparison-mode]').textContent,
          activeLinear: elements.get('[data-pixel-linear="active"]').textContent,
          activeSrgb: elements.get('[data-pixel-srgb="active"]').textContent,
          mainPixel: mainCanvas.puts.at(-1),
          zoomPixel: zoomCanvas.puts.at(-1),
          pointer: fakeViewer._typhoonExrState.pointer,
          prevented,
        }));
        """,
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["node", str(script), str(viewer_module)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    result = json.loads(completed.stdout)
    assert result == {
        "activeName": "flip",
        "mode": "FLIP",
        "activeLinear": "0.500",
        "activeSrgb": "183  55  121",
        "mainPixel": [183, 55, 121, 255],
        "zoomPixel": [183, 55, 121, 255],
        "pointer": [0, 0],
        "prevented": True,
    }


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


def test_regenerate_html_preserves_provider_in_nav(tmp_path: Path) -> None:
    output_base = tmp_path / "_output"
    run_dir = write_report_run(output_base, 1, key="case")
    provider = str(tmp_path / "openusd")
    summary_path = run_dir / "run-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["provider"] = provider
    summary_path.write_text(json.dumps(summary) + "\n", encoding="utf-8")

    report_html.regenerate_html(output_root=output_base, run="run-0001")

    html = (run_dir / "index.html").read_text(encoding="utf-8")
    regenerated_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert f'Provider <strong title="{provider}">{provider}</strong>' in html
    assert regenerated_summary["provider"] == provider


def test_regenerate_html_defaults_to_latest_run(tmp_path: Path) -> None:
    output_base = tmp_path / "_output"
    older = write_report_run(output_base, 1, key="older", status="dry-run")
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
    output_index = (output_base / "index.html").read_text(encoding="utf-8")
    assert "--ty-base08: #b14956;" in output_index
    assert output_index.count('<td data-sort-value="0.01">0.010</td>') == 3
    assert not (older / "index.html").exists()
    summary = json.loads((latest / "run-summary.json").read_text(encoding="utf-8"))
    assert summary["total"] == 1
    assert summary["compared"] == 1


def test_regenerate_html_backfills_legacy_usda_source_from_usd_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_base = tmp_path / "_output"
    run_dir = write_report_run(output_base, 1, key="legacy")
    usd = tmp_path / "suite" / "legacy.usda"
    usd.parent.mkdir()
    usd.write_text('#usda 1.0\ndef Scope "Legacy & <Fixture>"\n', encoding="utf-8")
    report = json.loads((run_dir / "typhoon-report.json").read_text(encoding="utf-8"))
    report[0]["usd"] = str(usd)
    report_path = run_dir / "typhoon-report.json"
    report_path.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    report_before = report_path.read_text(encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    report_html.regenerate_html(output_root=output_base, run="1")

    html = (run_dir / "index.html").read_text(encoding="utf-8")
    assert '<details class="usda-source"><summary>legacy.usda</summary>' in html
    assert '<span class="usd-token usd-string">&quot;Legacy &amp; &lt;Fixture&gt;&quot;</span>' in html
    assert f"<summary>{usd}</summary>" not in html
    assert report_path.read_text(encoding="utf-8") == report_before


def test_regenerate_html_backfills_legacy_usda_source_from_project_root_subdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pixi.toml").write_text("[workspace]\nname = 'fixture'\n", encoding="utf-8")
    output_base = tmp_path / "_output"
    run_dir = write_report_run(output_base, 1, key="legacy")
    usd = tmp_path / "suite" / "legacy.usda"
    usd.parent.mkdir()
    usd.write_text('#usda 1.0\ndef Scope "ProjectRoot"\n', encoding="utf-8")
    report = json.loads((run_dir / "typhoon-report.json").read_text(encoding="utf-8"))
    report[0]["usd"] = str(usd)
    (run_dir / "typhoon-report.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    subdir = tmp_path / "tools"
    subdir.mkdir()
    monkeypatch.chdir(subdir)

    report_html.regenerate_html(output_root=output_base, run="1")

    html = (run_dir / "index.html").read_text(encoding="utf-8")
    assert '<details class="usda-source"><summary>legacy.usda</summary>' in html
    assert '<span class="usd-token usd-string">&quot;ProjectRoot&quot;</span>' in html


def test_regenerate_html_backfills_legacy_usda_source_with_custom_output_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_base = tmp_path / "artifacts" / "_output"
    run_dir = write_report_run(output_base, 1, key="legacy")
    usd = tmp_path / "suite" / "legacy.usda"
    usd.parent.mkdir()
    usd.write_text('#usda 1.0\ndef Scope "CustomOutput"\n', encoding="utf-8")
    report = json.loads((run_dir / "typhoon-report.json").read_text(encoding="utf-8"))
    report[0]["usd"] = str(usd)
    (run_dir / "typhoon-report.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    report_html.regenerate_html(output_root=output_base, run="1")

    html = (run_dir / "index.html").read_text(encoding="utf-8")
    assert '<details class="usda-source"><summary>legacy.usda</summary>' in html
    assert '<span class="usd-token usd-string">&quot;CustomOutput&quot;</span>' in html


def test_regenerate_html_does_not_backfill_legacy_usda_source_outside_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_base = tmp_path / "_output"
    run_dir = write_report_run(output_base, 1, key="legacy")
    outside = tmp_path / "outside"
    outside.mkdir()
    usd = outside / "legacy.usda"
    usd.write_text('#usda 1.0\ndef Scope "Outside"\n', encoding="utf-8")
    report = json.loads((run_dir / "typhoon-report.json").read_text(encoding="utf-8"))
    report[0]["usd"] = str(usd)
    (run_dir / "typhoon-report.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.chdir(project_root)

    report_html.regenerate_html(output_root=output_base, run="1")

    html = (run_dir / "index.html").read_text(encoding="utf-8")
    assert 'class="usda-source"' not in html
    assert "Outside" not in html


def test_regenerate_html_uses_saved_usda_source_from_report(tmp_path: Path) -> None:
    output_base = tmp_path / "_output"
    run_dir = write_report_run(output_base, 1, key="case")
    usd = tmp_path / "suite" / "case.usda"
    usd.parent.mkdir()
    usd.write_text('#usda 1.0\ndef Scope "Current"\n', encoding="utf-8")
    saved_source = '#usda 1.0\ndef Scope "Saved <Fixture>"\n'
    report = json.loads((run_dir / "typhoon-report.json").read_text(encoding="utf-8"))
    report[0].update(
        {
            "usd": str(usd),
            "usd_source_name": "case.usda",
            "usd_source": saved_source,
        }
    )
    (run_dir / "typhoon-report.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )

    report_html.regenerate_html(output_root=output_base, run="1")

    html = (run_dir / "index.html").read_text(encoding="utf-8")
    assert '<details class="usda-source"><summary>case.usda</summary>' in html
    assert '<span class="usd-token usd-string">&quot;Saved &lt;Fixture&gt;&quot;</span>' in html
    assert 'def Scope &quot;Current&quot;' not in html


def test_regenerate_html_derives_fixture_doc_from_saved_usda_source(tmp_path: Path) -> None:
    output_base = tmp_path / "_output"
    run_dir = write_report_run(output_base, 1, key="case")
    saved_source = (
        '#usda 1.0\n'
        '(\n'
        '    customLayerData = {\n'
        '        string doc = "Regenerated doc <value>"\n'
        '    }\n'
        ')\n'
    )
    report = json.loads((run_dir / "typhoon-report.json").read_text(encoding="utf-8"))
    report[0].update(
        {
            "usd_source_name": "case.usda",
            "usd_source": saved_source,
        }
    )
    (run_dir / "typhoon-report.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )

    report_html.regenerate_html(output_root=output_base, run="1")

    html = (run_dir / "index.html").read_text(encoding="utf-8")
    assert (
        '<section class="fixture-doc" aria-label="Fixture documentation">'
        'Regenerated doc &lt;value&gt;</section>'
    ) in html


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
    tasks = pixi["tasks"]
    assert tasks["regenerate-html"] == {
        "cmd": "python -m typhoon_tests.report_html"
    }
    assert tasks["extract-failures"] == {
        "cmd": "python -m typhoon_tests.extract_failures"
    }
    assert tasks["build"] == {
        "cmd": "python -m typhoon_tests.build_exr_wasm"
    }
    assert tasks["view"] == {
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


def test_view_server_update_endpoints_mutate_source_files_on_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    output_base = tmp_path / "_output"
    run_dir = output_base / "run-0001"
    run_dir.mkdir(parents=True)
    suite_dir = project_root / "suite"
    suite_dir.mkdir(parents=True)
    (suite_dir / "typhoon-suite.toml").write_text(
        "[suite]\nname = \"sample\"\n", encoding="utf-8"
    )
    reference_usd = suite_dir / "reference-case.usda"
    reference_usd.write_text("#usda 1.0\n", encoding="utf-8")

    threshold_usd = suite_dir / "threshold-case.usda"
    threshold_usd.write_text("#usda 1.0\n", encoding="utf-8")
    reference = suite_dir / "reference" / "reference-case.exr"
    reference.parent.mkdir()
    reference.write_bytes(b"old-reference")
    render = run_dir / "reference-case.exr"
    render.write_bytes(b"new-render")

    results = [
        {
            "suite": "sample",
            "key": "threshold-case",
            "status": "failed-threshold",
            "comparison": "flip",
            "flip_mean": 0.1234,
            "flip_threshold": 0.04,
            "render_output": str(run_dir / "threshold-case.exr"),
            "usd": str(threshold_usd),
            "started_at": "2026-06-30T00:00:00+00:00",
        },
        {
            "suite": "sample",
            "usd": str(reference_usd),
            "key": "reference-case",
            "status": "failed-threshold",
            "comparison": "flip",
            "flip_mean": 0.2,
            "flip_threshold": 0.05,
            "reference": str(reference),
            "reference_image": str(run_dir / "reference" / "reference-case.exr"),
            "render_image": str(render),
            "render_output": str(render),
            "diff_exr": str(run_dir / "flip" / "reference-case.exr"),
            "started_at": "2026-06-30T00:00:00+00:00",
        },
    ]
    (run_dir / "typhoon-report.json").write_text(json.dumps(results) + "\n", encoding="utf-8")
    (run_dir / "run-summary.json").write_text(
        json.dumps(
            {
                "run_name": "run-0001",
                "run_number": 1,
                "started_at": "2026-06-30T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_compare_images(*, reference_path: Path, render_path: Path, artifact_dir: Path, key: str) -> object:
        reference_image = artifact_dir / "reference" / f"{key}.exr"
        diff_exr = artifact_dir / "flip" / f"{key}.exr"
        reference_image.parent.mkdir(parents=True, exist_ok=True)
        diff_exr.parent.mkdir(parents=True, exist_ok=True)
        reference_image.write_bytes(reference_path.read_bytes())
        diff_exr.write_bytes(b"diff")
        return SimpleNamespace(
            reference_image=reference_image,
            render_image=render_path,
            diff_exr=diff_exr,
            flip_mean=0.0,
        )

    import typhoon_tests.images as images

    monkeypatch.setattr(images, "compare_images", fake_compare_images)
    handler = partial(view_server.TyphoonViewHandler, directory=str(output_base))
    server = view_server.TyphoonViewServer(
        ("127.0.0.1", 0),
        handler,
        project_root=project_root,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(endpoint: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            connection.request(
                "POST",
                endpoint,
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
            view_server.UPDATE_THRESHOLDS_ENDPOINT,
            {
                "run": "/run-0001/",
                "rows": [{"suite": "sample", "key": "threshold-case"}],
            },
        )
        assert status == 200
        assert body["ok"] is True
        threshold_config = suite_dir / "threshold-case.typhoon.toml"
        assert "flip_threshold = 0.124" in threshold_config.read_text(encoding="utf-8")

        status, body = post(
            view_server.UPDATE_SUSPECTS_ENDPOINT,
            {
                "run": "/run-0001/",
                "rows": [
                    {
                        "suite": "sample",
                        "key": "threshold-case",
                        "suspect": True,
                    }
                ],
            },
        )
        assert status == 200
        assert body == {
            "ok": True,
            "updated": 1,
            "rows": [{"suite": "sample", "key": "threshold-case", "suspect": True}],
        }
        threshold_config_text = threshold_config.read_text(encoding="utf-8")
        assert "flip_threshold = 0.124" in threshold_config_text
        assert "[test]" in threshold_config_text
        assert "suspect = true" in threshold_config_text

        status, body = post(
            view_server.UPDATE_REFERENCES_ENDPOINT,
            {
                "run": "/run-0001/",
                "rows": [{"suite": "sample", "key": "reference-case"}],
            },
        )
        assert status == 200
        assert body["ok"] is True
        assert reference.read_bytes() == b"new-render"
        assert (run_dir / "reference" / "sample" / "reference-case.exr").read_bytes() == b"new-render"
        updated_report = json.loads((run_dir / "typhoon-report.json").read_text(encoding="utf-8"))
        assert updated_report[0]["flip_threshold"] == 0.124
        assert updated_report[1]["reference"] == str(reference.resolve())
        assert updated_report[1]["status"] == "passed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)



def test_view_server_update_thresholds_updates_case_config_and_report(tmp_path: Path) -> None:
    output_base = tmp_path / "_output"
    run_dir = output_base / "run-0001"
    run_dir.mkdir(parents=True)
    suite = tmp_path / "suite"
    suite.mkdir()
    usd = suite / "case.usda"
    usd.write_text("#usda 1.0\n", encoding="utf-8")
    secondary_config = suite / "case.usda.typhoon.toml"
    secondary_config.write_text('[render]\nargs = ["--existing"]\n', encoding="utf-8")
    other_usd = suite / "other.usda"
    other_usd.write_text("#usda 1.0\n", encoding="utf-8")
    results = [
        {
            "suite": "sample",
            "key": "case",
            "status": "failed-threshold",
            "comparison": "flip",
            "flip_mean": 0.16064319014549255,
            "flip_threshold": 0.04,
            "render_output": str(run_dir / "case.exr"),
            "usd": str(usd),
            "started_at": "2026-06-30T00:00:00+00:00",
        },
        {
            "suite": "other",
            "key": "case",
            "status": "failed-threshold",
            "comparison": "flip",
            "flip_mean": 0.9,
            "flip_threshold": 0.04,
            "render_output": str(run_dir / "other.exr"),
            "usd": str(other_usd),
            "started_at": "2026-06-30T00:00:00+00:00",
        },
        {
            "suite": "sample",
            "key": "case-frame-2",
            "status": "failed-threshold",
            "comparison": "flip",
            "flip_mean": 0.25,
            "flip_threshold": 0.04,
            "render_output": str(run_dir / "case-frame-2.exr"),
            "usd": str(usd),
            "started_at": "2026-06-30T00:00:00+00:00",
        },
    ]
    (run_dir / "typhoon-report.json").write_text(json.dumps(results) + "\n", encoding="utf-8")
    (run_dir / "run-summary.json").write_text(
        json.dumps(
            {
                "run_name": "run-0001",
                "run_number": 1,
                "started_at": "2026-06-30T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(view_server.ViewServerError, match="found 0"):
        view_server.update_thresholds(
            {
                "run": "/run-0001/",
                "rows": [
                    {
                        "suite": "sample",
                        "key": "case",
                    },
                    {
                        "suite": "missing",
                        "key": "case",
                    },
                ],
            },
            project_root=tmp_path,
            output_root=output_base,
        )
    assert secondary_config.read_text(encoding="utf-8") == '[render]\nargs = ["--existing"]\n'
    unchanged_report = json.loads((run_dir / "typhoon-report.json").read_text(encoding="utf-8"))
    assert unchanged_report[0]["flip_threshold"] == 0.04

    with pytest.raises(view_server.ViewServerError, match="share threshold config"):
        view_server.update_thresholds(
            {
                "run": "/run-0001/",
                "rows": [
                    {
                        "suite": "sample",
                        "key": "case",
                    },
                    {
                        "suite": "sample",
                        "key": "case-frame-2",
                    },
                ],
            },
            project_root=tmp_path,
            output_root=output_base,
        )
    assert secondary_config.read_text(encoding="utf-8") == '[render]\nargs = ["--existing"]\n'

    result = view_server.update_thresholds(
        {
            "run": "/run-0001/",
            "rows": [
                {
                    "suite": "sample",
                    "key": "case",
                    "usd": str(other_usd),
                    "flipMean": "999",
                }
            ],
        },
        project_root=tmp_path,
        output_root=output_base,
    )

    assert result["updated"] == 1
    assert not (suite / "case.typhoon.toml").exists()
    case_config = secondary_config.read_text(encoding="utf-8")
    assert 'args = ["--existing"]' in case_config
    assert "[comparison]" in case_config
    assert "flip_threshold = 0.161" in case_config
    updated_report = json.loads((run_dir / "typhoon-report.json").read_text(encoding="utf-8"))
    assert updated_report[0]["flip_threshold"] == 0.161
    assert updated_report[0]["status"] == "passed"
    assert updated_report[1]["flip_threshold"] == 0.04
    assert updated_report[1]["status"] == "failed-threshold"
    assert updated_report[2]["flip_threshold"] == 0.04
    assert updated_report[2]["status"] == "failed-threshold"
    assert "Update threshold" in (run_dir / "index.html").read_text(encoding="utf-8")



def test_view_server_update_suspects_updates_case_config_and_report(tmp_path: Path) -> None:
    output_base = tmp_path / "_output"
    run_dir = output_base / "run-0001"
    run_dir.mkdir(parents=True)
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "typhoon-suite.toml").write_text(
        "[suite]\nname = \"sample\"\n", encoding="utf-8"
    )
    usd = suite / "case.usda"
    usd.write_text("#usda 1.0\n", encoding="utf-8")
    case_config = suite / "case.typhoon.toml"
    case_config.write_text("[comparison]\nflip_threshold = 0.05\n", encoding="utf-8")
    results = [
        {
            "suite": "sample",
            "key": "case",
            "status": "passed",
            "usd": str(usd),
            "render_output": str(run_dir / "case.exr"),
            "started_at": "2026-06-30T00:00:00+00:00",
            "suspect": False,
        }
    ]
    (run_dir / "typhoon-report.json").write_text(json.dumps(results) + "\n", encoding="utf-8")
    (run_dir / "run-summary.json").write_text(
        json.dumps(
            {
                "run_name": "run-0001",
                "run_number": 1,
                "started_at": "2026-06-30T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(view_server.ViewServerError, match="suspect must be a boolean"):
        view_server.update_suspects(
            {
                "run": "/run-0001/",
                "rows": [{"suite": "sample", "key": "case", "suspect": "yes"}],
            },
            project_root=tmp_path,
            output_root=output_base,
        )
    assert case_config.read_text(encoding="utf-8") == "[comparison]\nflip_threshold = 0.05\n"

    with pytest.raises(view_server.ViewServerError, match="share suspect config"):
        view_server.update_suspects(
            {
                "run": "/run-0001/",
                "rows": [
                    {"suite": "sample", "key": "case", "suspect": True},
                    {"suite": "sample", "key": "case", "suspect": False},
                ],
            },
            project_root=tmp_path,
            output_root=output_base,
        )
    assert case_config.read_text(encoding="utf-8") == "[comparison]\nflip_threshold = 0.05\n"

    result = view_server.update_suspects(
        {
            "run": "/run-0001/",
            "rows": [{"suite": "sample", "key": "case", "suspect": True}],
        },
        project_root=tmp_path,
        output_root=output_base,
    )

    assert result == {
        "updated": 1,
        "rows": [{"suite": "sample", "key": "case", "suspect": True}],
    }
    assert case_config.read_text(encoding="utf-8") == (
        "[comparison]\nflip_threshold = 0.05\n\n[test]\nsuspect = true\n"
    )
    updated_report = json.loads((run_dir / "typhoon-report.json").read_text(encoding="utf-8"))
    assert updated_report[0]["suspect"] is True
    html = (run_dir / "index.html").read_text(encoding="utf-8")
    assert "Clear suspect" in html
    assert "suspect-badge" in html
    assert "<strong>1</strong> suspect" in html

    result = view_server.update_suspects(
        {
            "run": "/run-0001/",
            "rows": [{"suite": "sample", "key": "case", "suspect": False}],
        },
        project_root=tmp_path,
        output_root=output_base,
    )

    assert result == {
        "updated": 1,
        "rows": [{"suite": "sample", "key": "case", "suspect": False}],
    }
    assert case_config.read_text(encoding="utf-8") == "[comparison]\nflip_threshold = 0.05\n"
    updated_report = json.loads((run_dir / "typhoon-report.json").read_text(encoding="utf-8"))
    assert updated_report[0]["suspect"] is False
    html = (run_dir / "index.html").read_text(encoding="utf-8")
    assert "Mark suspect" in html
    assert "<strong>0</strong> suspect" in html


def test_view_server_update_references_copies_render_and_refreshes_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    output_base = tmp_path / "_output"
    run_dir = output_base / "run-0001"
    run_dir.mkdir(parents=True)
    suite_dir = project_root / "suite"
    suite_dir.mkdir(parents=True)
    (suite_dir / "typhoon-suite.toml").write_text(
        "[suite]\nname = \"sample\"\n", encoding="utf-8"
    )
    case_usd = suite_dir / "case.usda"
    other_usd = suite_dir / "other.usda"
    external_usd = suite_dir / "external.usda"
    shared_usd = suite_dir / "shared.usda"
    for usd_path in (case_usd, other_usd, external_usd, shared_usd):
        usd_path.write_text("#usda 1.0\n", encoding="utf-8")
    case_config = case_usd.with_suffix(".typhoon.toml")
    case_config.write_text(
        "[comparison]\nflip_threshold = 0.05\n", encoding="utf-8"
    )
    reference = project_root / "suite" / "reference" / "case_materialx-osl.png"
    reference.parent.mkdir(parents=True)
    old_run_reference = run_dir / "reference" / "case.png"
    old_run_reference.parent.mkdir(parents=True)
    old_run_reference.write_bytes(b"old-run-reference")
    reference.write_bytes(b"old-reference")
    render = run_dir / "case.exr"
    render.write_bytes(b"new-render")
    other_reference = project_root / "suite" / "reference" / "other.exr"
    other_reference.write_bytes(b"other-reference")
    other_render = run_dir / "other.exr"
    other_render.write_bytes(b"other-render")
    payload_reference = project_root / "suite" / "reference" / "payload.exr"
    payload_reference.write_bytes(b"payload-reference")
    payload_render = run_dir / "payload.exr"
    payload_render.write_bytes(b"payload-render")
    external_reference = tmp_path / "external" / "outside.exr"
    external_reference.parent.mkdir()
    external_reference.write_bytes(b"external-reference")
    external_render = run_dir / "external.exr"
    external_render.write_bytes(b"external-render")
    results = [
        {
            "suite": "sample",
            "usd": str(case_usd),
            "key": "case",
            "status": "failed-threshold",
            "comparison": "flip",
            "flip_mean": 0.2,
            "flip_threshold": 0.05,
            "reference": str(reference),
            "reference_image": str(old_run_reference),
            "render_image": str(render),
            "render_output": str(render),
            "diff_exr": str(run_dir / "flip" / "case.exr"),
            "started_at": "2026-06-30T00:00:00+00:00",
        },
        {
            "suite": "other",
            "usd": str(other_usd),
            "key": "case",
            "status": "failed-threshold",
            "comparison": "flip",
            "flip_mean": 0.4,
            "flip_threshold": 0.05,
            "reference": str(other_reference),
            "reference_image": str(run_dir / "reference" / "other.exr"),
            "render_image": str(other_render),
            "render_output": str(other_render),
            "diff_exr": str(run_dir / "flip" / "other.exr"),
            "started_at": "2026-06-30T00:00:00+00:00",
        },
        {
            "suite": "external",
            "usd": str(external_usd),
            "key": "case",
            "status": "failed-threshold",
            "comparison": "flip",
            "flip_mean": 0.6,
            "flip_threshold": 0.05,
            "reference": str(external_reference),
            "reference_image": str(run_dir / "reference" / "external.exr"),
            "render_image": str(external_render),
            "render_output": str(external_render),
            "diff_exr": str(run_dir / "flip" / "external.exr"),
            "started_at": "2026-06-30T00:00:00+00:00",
        },
        {
            "suite": "shared-reference",
            "usd": str(shared_usd),
            "key": "case",
            "status": "failed-threshold",
            "comparison": "flip",
            "flip_mean": 0.7,
            "flip_threshold": 0.05,
            "reference": str(reference),
            "reference_image": str(run_dir / "reference" / "shared-reference" / "case.exr"),
            "render_image": str(other_render),
            "render_output": str(other_render),
            "diff_exr": str(run_dir / "flip" / "shared-reference" / "case.exr"),
            "started_at": "2026-06-30T00:00:00+00:00",
        },
    ]
    (run_dir / "typhoon-report.json").write_text(json.dumps(results) + "\n", encoding="utf-8")
    (run_dir / "run-summary.json").write_text(
        json.dumps(
            {
                "run_name": "run-0001",
                "run_number": 1,
                "started_at": "2026-06-30T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_compare_images(*, reference_path: Path, render_path: Path, artifact_dir: Path, key: str) -> object:
        reference_image = artifact_dir / "reference" / f"{key}.exr"
        diff_exr = artifact_dir / "flip" / f"{key}.exr"
        reference_image.parent.mkdir(parents=True, exist_ok=True)
        diff_exr.parent.mkdir(parents=True, exist_ok=True)
        reference_image.write_bytes(reference_path.read_bytes())
        diff_exr.write_bytes(b"diff")
        return SimpleNamespace(
            reference_image=reference_image,
            render_image=render_path,
            diff_exr=diff_exr,
            flip_mean=0.0,
        )

    import typhoon_tests.images as images

    monkeypatch.setattr(images, "compare_images", fake_compare_images)

    with pytest.raises(view_server.ViewServerError, match="outside allowed roots"):
        view_server.update_references(
            {
                "run": "/run-0001/",
                "rows": [
                    {
                        "suite": "sample",
                        "key": "case",
                    },
                    {
                        "suite": "external",
                        "key": "case",
                    },
                ],
            },
            project_root=project_root,
            output_root=output_base,
        )
    assert reference.read_bytes() == b"old-reference"
    assert not (run_dir / "reference" / "sample" / "case.exr").exists()

    results[0]["frame"] = 1
    (run_dir / "typhoon-report.json").write_text(
        json.dumps(results) + "\n", encoding="utf-8"
    )
    with pytest.raises(view_server.ViewServerError, match="frame-expanded"):
        view_server.update_references(
            {
                "run": "/run-0001/",
                "rows": [{"suite": "sample", "key": "case"}],
            },
            project_root=project_root,
            output_root=output_base,
        )
    del results[0]["frame"]
    (run_dir / "typhoon-report.json").write_text(
        json.dumps(results) + "\n", encoding="utf-8"
    )
    with pytest.raises(view_server.ViewServerError, match="invalid report suite"):
        view_server.artifact_key({"suite": "../escape", "key": "case"})

    with pytest.raises(view_server.ViewServerError, match="share reference path"):
        view_server.update_references(
            {
                "run": "/run-0001/",
                "rows": [
                    {
                        "suite": "sample",
                        "key": "case",
                    },
                    {
                        "suite": "shared-reference",
                        "key": "case",
                    },
                ],
            },
            project_root=project_root,
            output_root=output_base,
        )
    assert reference.read_bytes() == b"old-reference"
    assert not (run_dir / "reference" / "sample" / "case.exr").exists()

    shared_reference = reference.with_name("shared.exr")
    shared_reference.write_bytes(b"shared-reference")
    results[3]["reference"] = str(shared_reference)
    (run_dir / "typhoon-report.json").write_text(
        json.dumps(results) + "\n", encoding="utf-8"
    )

    config_before = case_config.read_bytes()
    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(
            view_server,
            "_write_run_results",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write failed")),
        )
        with pytest.raises(OSError, match="write failed"):
            view_server.update_references(
                {
                    "run": "/run-0001/",
                    "rows": [{"suite": "sample", "key": "case"}],
                },
                project_root=project_root,
                output_root=output_base,
            )
    updated_reference = reference.with_name(render.name)
    assert reference.read_bytes() == b"old-reference"
    assert not updated_reference.exists()
    assert old_run_reference.read_bytes() == b"old-run-reference"
    assert case_config.read_bytes() == config_before

    updated_reference.write_bytes(b"existing-target")
    with pytest.raises(view_server.ViewServerError, match="target already exists"):
        view_server.update_references(
            {
                "run": "/run-0001/",
                "rows": [{"suite": "sample", "key": "case"}],
            },
            project_root=project_root,
            output_root=output_base,
        )
    assert reference.read_bytes() == b"old-reference"
    assert updated_reference.read_bytes() == b"existing-target"
    updated_reference.unlink()

    result = view_server.update_references(
        {
            "run": "/run-0001/",
            "rows": [
                {
                    "suite": "sample",
                    "key": "case",
                    "reference": str(payload_reference),
                    "render": str(payload_render),
                }
            ],
        },
        project_root=project_root,
        output_root=output_base,
    )

    assert result["updated"] == 1
    updated_reference = reference.with_name(render.name)
    assert not reference.exists()
    assert updated_reference.read_bytes() == render.read_bytes()
    assert not old_run_reference.exists()
    assert payload_reference.read_bytes() == b"payload-reference"
    assert other_reference.read_bytes() == b"other-reference"
    updated_report = json.loads((run_dir / "typhoon-report.json").read_text(encoding="utf-8"))
    assert updated_report[0]["status"] == "passed"
    assert updated_report[0]["flip_mean"] == 0.0
    assert updated_report[0]["reference"] == str(updated_reference.resolve())
    assert updated_report[0]["reference_image"] == str(run_dir / "reference" / "sample" / "case.exr")
    assert updated_report[0]["diff_exr"] == str(run_dir / "flip" / "sample" / "case.exr")
    assert updated_report[1]["status"] == "failed-threshold"
    assert updated_report[2]["status"] == "failed-threshold"
    assert updated_report[3]["status"] == "failed-threshold"
    assert (run_dir / "reference" / "sample" / "case.exr").read_bytes() == b"new-render"
    case_config_text = case_config.read_text(encoding="utf-8")
    assert "[comparison]" in case_config_text
    assert "flip_threshold = 0.05" in case_config_text
    assert "[reference]" in case_config_text
    assert 'path = "reference/case.exr"' in case_config_text
    resolved_case = plugin.build_cases(case_usd)[0]
    assert plugin.resolve_reference(resolved_case, options(tmp_path)) == updated_reference.resolve()


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


def test_reference_update_removes_override_when_exr_is_suite_default(
    tmp_path: Path,
) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "typhoon-suite.toml").write_text(
        """[suite]
name = "sample"

[reference]
dir = "reference"
pattern = "{path}.exr"
""",
        encoding="utf-8",
    )
    usd_path = suite / "case.usda"
    usd_path.write_text("#usda 1.0\n", encoding="utf-8")
    config_path = usd_path.with_suffix(".typhoon.toml")
    config_path.write_text(
        '[reference]\npath = "reference/case_materialx-osl.png"\n\n'
        "[comparison]\nflip_threshold = 0.05\n",
        encoding="utf-8",
    )
    reference_path = suite / "reference" / "case.exr"

    actual_path, config_text = view_server.build_case_reference_update(
        usd_path, reference_path
    )
    view_server._write_text_atomic(actual_path, config_text)

    assert actual_path == config_path
    assert config_text == "[comparison]\nflip_threshold = 0.05\n"
    assert config_path.read_text(encoding="utf-8") == config_text

    empty_usd = suite / "empty.usda"
    empty_usd.write_text("#usda 1.0\n", encoding="utf-8")
    empty_config = empty_usd.with_suffix(".typhoon.toml")
    empty_config.write_text(
        '[reference]\npath = "reference/empty_materialx-osl.png"\n',
        encoding="utf-8",
    )
    empty_reference = suite / "reference" / "empty.exr"
    actual_path, config_text = view_server.build_case_reference_update(
        empty_usd, empty_reference
    )
    view_server._write_text_atomic(actual_path, config_text)
    assert config_text == ""
    assert not empty_config.exists()
