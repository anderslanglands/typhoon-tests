from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
from types import SimpleNamespace

import pytest

import typhoon_tests.pytest_plugin as plugin
import typhoon_tests.report_html as report_html
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


class ReportHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sort_buttons: list[dict[str, str]] = []
        self.status_cells: list[dict[str, str]] = []
        self.rows: list[list[str]] = []
        self._button: dict[str, str] | None = None
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
        if tag == "tr":
            self._row = []
        if tag == "td" and self._row is not None:
            self._cell_text = ""
        if tag == "td" and "status-cell" in attr_map.get("class", "").split():
            self._status_cell = {
                "class": attr_map.get("class", ""),
                "sort": attr_map.get("data-sort-value", ""),
                "text": "",
            }

    def handle_data(self, data: str) -> None:
        if self._button is not None:
            self._button["label"] += data
        if self._cell_text is not None:
            self._cell_text += data
        if self._status_cell is not None:
            self._status_cell["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "button" and self._button is not None:
            self._button["label"] = self._button["label"].strip()
            self.sort_buttons.append(self._button)
            self._button = None
        if tag == "td" and self._cell_text is not None and self._row is not None:
            self._row.append(self._cell_text.strip())
            self._cell_text = None
        if tag == "td" and self._status_cell is not None:
            self._status_cell["text"] = self._status_cell["text"].strip()
            self.status_cells.append(self._status_cell)
            self._status_cell = None
        if tag == "tr" and self._row is not None:
            if self._row:
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


def test_provider_mode_uses_openusd_pixi_task_without_duplicate_base_flags(
    tmp_path: Path,
) -> None:
    usd = write_suite(tmp_path)
    provider = tmp_path / "openusd"
    provider.mkdir()
    (provider / "pixi.toml").write_text("[workspace]\nname = 'openusd'\n", encoding="utf-8")
    case = plugin.build_case(usd)
    opts = options(tmp_path, provider=provider)

    cmd = plugin.build_render_command(case, opts, opts.run_context.run_dir)

    assert cmd[:5] == [
        "pixi",
        "run",
        "--manifest-path",
        str(provider / "pixi.toml"),
        "usdrender",
    ]
    assert "--disableCameraLight" in cmd
    assert "--complexity" not in cmd
    assert "--renderer" not in cmd


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
            reference_png=tmp_path / "reference.png",
            render_png=tmp_path / "render.png",
            diff_png=tmp_path / "diff.png",
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


def test_require_thresholds_fails_compared_case_without_threshold(
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
            reference_png=tmp_path / "reference.png",
            render_png=tmp_path / "render.png",
            diff_png=tmp_path / "diff.png",
        ),
    )

    with pytest.raises(TyphoonRenderError, match="missing FLIP threshold") as excinfo:
        plugin.run_typhoon_case(case, opts)

    assert excinfo.value.result is not None
    assert excinfo.value.result["status"] == "failed-missing-threshold"


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


def test_unconfigured_usdas_are_not_collected_by_default(tmp_path: Path) -> None:
    usd = tmp_path / "examples" / "loose.usda"
    usd.parent.mkdir()
    usd.write_text("#usda 1.0\n", encoding="utf-8")

    assert not plugin.should_collect_usda(usd, tmp_path, collect_unconfigured=False)
    assert plugin.should_collect_usda(usd, tmp_path, collect_unconfigured=True)


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
                "reference_png": str(context.run_dir / "reference" / "a.png"),
                "render_png": str(context.run_dir / "render" / "a.png"),
                "diff_png": str(context.run_dir / "flip" / "a.png"),
            },
            {
                "suite": "sample",
                "key": "g",
                "status": "passed",
                "comparison": "flip",
                "flip_mean": 0.2,
                "flip_threshold": 0.25,
                "render_output": str(context.run_dir / "g.exr"),
                "reference_png": str(context.run_dir / "reference" / "g.png"),
                "render_png": str(context.run_dir / "render" / "g.png"),
                "diff_png": str(context.run_dir / "flip" / "g.png"),
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
        run_dir / "reference" / "case.png",
        run_dir / "render" / "case.png",
        run_dir / "flip" / "case.png",
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
