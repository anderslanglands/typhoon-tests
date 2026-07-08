from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import typhoon_tests.extract_failures as extract


def write_run(output_root: Path, run_number: int, rows: list[dict[str, object]]) -> Path:
    run_dir = output_root / f"run-{run_number:04d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        row.setdefault("suite", "sample")
        row.setdefault("run_number", run_number)
        row.setdefault("run_dir", str(run_dir))
        row.setdefault("started_at", "2026-06-30T00:00:00+00:00")
        row.setdefault("provider", "package")
    (run_dir / "typhoon-report.json").write_text(
        json.dumps(rows, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "run-summary.json").write_text(
        json.dumps(
            {
                "run_name": run_dir.name,
                "run_number": run_number,
                "started_at": "2026-06-30T00:00:00+00:00",
                "provider": "package",
                "run_dir": str(run_dir),
                "total": len(rows),
                "compared": 0,
                "missing_references": 0,
                "failed": sum(
                    1 for row in rows if str(row.get("status", "")).startswith("failed")
                ),
                "dry_run": 0,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return run_dir


def test_extract_failures_copies_only_failed_rows_and_run_local_artifacts(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "_output"
    source_run = output_root / "run-0001"
    external_reference = tmp_path / "suite" / "reference" / "failed.png"
    external_reference.parent.mkdir(parents=True)
    external_reference.write_bytes(b"external-reference")

    failed_render = source_run / "nested" / "failed.exr"
    failed_reference = source_run / "reference" / "failed.png"
    failed_diff = source_run / "flip" / "failed.exr"
    passed_render = source_run / "nested" / "passed.exr"
    passed_reference = source_run / "reference" / "passed.png"
    passed_diff = source_run / "flip" / "passed.exr"
    for path, payload in (
        (failed_render, b"failed-render"),
        (failed_reference, b"failed-reference"),
        (failed_diff, b"failed-diff"),
        (passed_render, b"passed-render"),
        (passed_reference, b"passed-reference"),
        (passed_diff, b"passed-diff"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    write_run(
        output_root,
        1,
        [
            {
                "key": "failed",
                "status": "failed-threshold",
                "comparison": "flip",
                "flip_mean": 0.2,
                "flip_threshold": 0.1,
                "reference": str(external_reference),
                "render_output": str(failed_render),
                "render_image": str(failed_render),
                "reference_image": str(failed_reference),
                "diff_exr": str(failed_diff),
                "output_root": str(source_run),
                "artifact_root": str(source_run),
                "command": [
                    "usdrender",
                    str(tmp_path / "suite" / "failed.usda"),
                    "--outputRoot",
                    str(source_run),
                ],
            },
            {
                "key": "passed",
                "status": "passed",
                "comparison": "flip",
                "flip_mean": 0.01,
                "flip_threshold": 0.1,
                "render_output": str(passed_render),
                "render_image": str(passed_render),
                "reference_image": str(passed_reference),
                "diff_exr": str(passed_diff),
            },
            {
                "key": "failed-render",
                "status": "failed-render",
                "render_output": str(source_run / "missing.exr"),
                "output_root": str(source_run),
                "artifact_root": str(source_run),
                "command": ["usdrender", "--outputRoot", str(source_run)],
            },
        ],
    )
    source_report_before = (source_run / "typhoon-report.json").read_text(encoding="utf-8")
    source_artifacts_before = {
        path: path.read_bytes()
        for path in (
            failed_render,
            failed_reference,
            failed_diff,
            passed_render,
            passed_reference,
            passed_diff,
        )
    }

    result = extract.extract_failures(
        output_root=output_root,
        run="1",
        started_at="2026-07-01T00:00:00+00:00",
    )

    new_run = output_root / "run-0002"
    assert result == extract.ExtractedFailures(
        source_run=source_run.resolve(),
        run_dir=new_run.resolve(),
        count=2,
    )
    report = json.loads((new_run / "typhoon-report.json").read_text(encoding="utf-8"))
    assert [row["key"] for row in report] == ["failed", "failed-render"]

    failed = report[0]
    assert failed["run_number"] == 2
    assert failed["run_dir"] == str(new_run.resolve())
    assert failed["started_at"] == "2026-07-01T00:00:00+00:00"
    assert failed["reference"] == str(external_reference)
    assert failed["render_output"] == str((new_run / "nested" / "failed.exr").resolve())
    assert failed["render_image"] == str((new_run / "nested" / "failed.exr").resolve())
    assert failed["reference_image"] == str((new_run / "reference" / "failed.png").resolve())
    assert failed["diff_exr"] == str((new_run / "flip" / "failed.exr").resolve())
    assert failed["output_root"] == str(new_run.resolve())
    assert failed["artifact_root"] == str(new_run.resolve())
    assert failed["command"][-1] == str(new_run.resolve())
    assert (new_run / "nested" / "failed.exr").read_bytes() == b"failed-render"
    assert (new_run / "reference" / "failed.png").read_bytes() == b"failed-reference"
    assert (new_run / "flip" / "failed.exr").read_bytes() == b"failed-diff"

    failed_render_row = report[1]
    assert failed_render_row["render_output"] == str((new_run / "missing.exr").resolve())
    assert not (new_run / "missing.exr").exists()
    assert failed_render_row["command"][-1] == str(new_run.resolve())

    assert not (new_run / "nested" / "passed.exr").exists()
    assert not (new_run / "reference" / "passed.png").exists()
    assert not (new_run / "flip" / "passed.exr").exists()
    html = (new_run / "index.html").read_text(encoding="utf-8")
    assert "failed-render" in html
    assert "passed.exr" not in html
    summary = json.loads((new_run / "run-summary.json").read_text(encoding="utf-8"))
    assert summary["total"] == 2
    assert summary["failed"] == 2
    assert summary["compared"] == 1
    assert "run-0002/index.html" in (output_root / "index.html").read_text(
        encoding="utf-8"
    )
    assert (source_run / "typhoon-report.json").read_text(
        encoding="utf-8"
    ) == source_report_before
    assert {
        path: path.read_bytes()
        for path in (
            failed_render,
            failed_reference,
            failed_diff,
            passed_render,
            passed_reference,
            passed_diff,
        )
    } == source_artifacts_before


def test_extract_failures_preserves_partial_sparse_and_legacy_artifact_rows(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "_output"
    source_run = output_root / "run-0001"
    missing_reference_render = source_run / "missing-reference.exr"
    legacy_reference_png = source_run / "reference" / "legacy.png"
    legacy_render_png = source_run / "render" / "legacy.png"
    legacy_diff_png = source_run / "diff" / "legacy.png"
    for path, payload in (
        (missing_reference_render, b"missing-reference-render"),
        (legacy_reference_png, b"legacy-reference"),
        (legacy_render_png, b"legacy-render"),
        (legacy_diff_png, b"legacy-diff"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    write_run(
        output_root,
        1,
        [
            {
                "key": "missing-reference",
                "status": "failed-missing-reference",
                "comparison": "missing-reference",
                "render_output": str(missing_reference_render),
                "render_image": str(missing_reference_render),
                "reference": str(tmp_path / "suite" / "reference" / "missing.png"),
            },
            {
                "key": "config",
                "status": "failed-config",
                "render_output": None,
                "render_image": None,
                "command": [],
            },
            {
                "key": "legacy-pngs",
                "status": "failed-compare",
                "reference_png": str(legacy_reference_png),
                "render_png": str(legacy_render_png),
                "diff_png": str(legacy_diff_png),
            },
        ],
    )

    extract.extract_failures(
        output_root=output_root,
        run="1",
        started_at="2026-07-01T00:00:00+00:00",
    )

    new_run = output_root / "run-0002"
    report = json.loads((new_run / "typhoon-report.json").read_text(encoding="utf-8"))
    assert [row["key"] for row in report] == [
        "missing-reference",
        "config",
        "legacy-pngs",
    ]
    assert report[0]["render_output"] == str((new_run / "missing-reference.exr").resolve())
    assert report[0]["render_image"] == str((new_run / "missing-reference.exr").resolve())
    assert (new_run / "missing-reference.exr").read_bytes() == b"missing-reference-render"
    assert report[1]["render_output"] is None
    assert report[1]["render_image"] is None
    assert report[1]["command"] == []
    assert report[2]["reference_png"] == str((new_run / "reference" / "legacy.png").resolve())
    assert report[2]["render_png"] == str((new_run / "render" / "legacy.png").resolve())
    assert report[2]["diff_png"] == str((new_run / "diff" / "legacy.png").resolve())
    assert (new_run / "reference" / "legacy.png").read_bytes() == b"legacy-reference"
    assert (new_run / "render" / "legacy.png").read_bytes() == b"legacy-render"
    assert (new_run / "diff" / "legacy.png").read_bytes() == b"legacy-diff"


def test_extract_failures_defaults_to_latest_before_allocating_new_run(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "_output"
    older = write_run(output_root, 1, [{"key": "older", "status": "failed-render"}])
    latest = write_run(output_root, 2, [{"key": "latest", "status": "failed-render"}])

    result = extract.extract_failures(
        output_root=output_root,
        started_at="2026-07-01T00:00:00+00:00",
    )

    report = json.loads(
        (output_root / "run-0003" / "typhoon-report.json").read_text(encoding="utf-8")
    )
    assert result.source_run == latest.resolve()
    assert result.source_run != older.resolve()
    assert result.run_dir == (output_root / "run-0003").resolve()
    assert [row["key"] for row in report] == ["latest"]


def test_extract_failures_does_not_allocate_run_when_source_has_no_failures(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "_output"
    write_run(output_root, 1, [{"key": "passed", "status": "passed"}])

    with pytest.raises(extract.ExtractFailuresError, match="no failed cases"):
        extract.extract_failures(output_root=output_root, run="1")

    assert not (output_root / "run-0002").exists()


def test_extract_failures_module_cli(tmp_path: Path) -> None:
    output_root = tmp_path / "_output"
    source_run = write_run(output_root, 1, [{"key": "failed", "status": "failed-render"}])
    repo_root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "typhoon_tests.extract_failures",
            "--output-root",
            str(output_root),
            "1",
        ],
        cwd=repo_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    new_run = output_root / "run-0002"
    assert completed.returncode == 0
    assert completed.stderr == ""
    assert f"extracted 1 failures from {source_run.resolve()} to {new_run.resolve()}" in (
        completed.stdout
    )
    assert "failed" in (new_run / "index.html").read_text(encoding="utf-8")


def test_extract_failures_pixi_task_forwards_optional_run_argument() -> None:
    pixi = shutil.which("pixi")
    if pixi is None:
        pytest.skip("pixi executable is not available")
    repo_root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [pixi, "run", "--dry-run", "extract-failures", "1"],
        cwd=repo_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 0
    assert "python -m typhoon_tests.extract_failures 1" in output
