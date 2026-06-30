from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import typhoon_tests.regenerate_comparisons as regen


def test_regenerate_comparisons_defaults_to_latest_run_and_rewrites_reports(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output_root = tmp_path / "_output"
    older = output_root / "run-0001"
    latest = output_root / "run-0002"
    older.mkdir(parents=True)
    latest.mkdir()
    render = latest / "case.exr"
    reference = tmp_path / "reference.exr"
    render.write_bytes(b"render")
    reference.write_bytes(b"reference")
    (latest / "typhoon-report.json").write_text(
        json.dumps(
            [
                {
                    "suite": "sample",
                    "key": "case",
                    "status": "passed",
                    "render_output": str(render),
                    "reference": str(reference),
                    "flip_threshold": 0.1,
                    "run_number": 2,
                    "run_dir": str(latest),
                    "started_at": "2026-06-30T00:00:00+00:00",
                },
                {
                    "suite": "sample",
                    "key": "dry",
                    "status": "dry-run",
                    "render_output": None,
                    "reference": None,
                    "run_number": 2,
                    "run_dir": str(latest),
                    "started_at": "2026-06-30T00:00:00+00:00",
                },
                {
                    "suite": "sample",
                    "key": "strict",
                    "status": "failed-missing-threshold",
                    "render_output": str(render),
                    "reference": str(reference),
                    "flip_threshold": None,
                    "run_number": 2,
                    "run_dir": str(latest),
                    "started_at": "2026-06-30T00:00:00+00:00",
                },
                {
                    "suite": "sample",
                    "key": "missing-render",
                    "status": "failed-missing-render",
                    "render_output": str(latest / "missing.exr"),
                    "reference": str(reference),
                    "run_number": 2,
                    "run_dir": str(latest),
                    "started_at": "2026-06-30T00:00:00+00:00",
                },
                {
                    "suite": "sample",
                    "key": "missing-reference",
                    "status": "failed-missing-reference",
                    "render_output": str(render),
                    "reference": str(latest / "missing-reference.exr"),
                    "run_number": 2,
                    "run_dir": str(latest),
                    "started_at": "2026-06-30T00:00:00+00:00",
                },
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (latest / "run-summary.json").write_text(
        json.dumps(
            {
                "run_name": "run-0002",
                "run_number": 2,
                "started_at": "2026-06-30T00:00:00+00:00",
                "total": 5,
                "compared": 1,
                "missing_references": 0,
                "failed": 2,
                "dry_run": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls = []

    def fake_compare_images(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            reference_png=latest / "reference" / "case.png",
            render_png=latest / "render" / "case.png",
            diff_png=latest / "flip" / "case.png",
            flip_mean=0.2,
        )

    monkeypatch.setattr(regen, "compare_images", fake_compare_images)

    summaries = regen.regenerate_comparisons(output_root=output_root)

    assert summaries == [(latest.resolve(), 2, 3)]
    assert calls == [
        {
            "reference_path": reference,
            "render_path": render,
            "artifact_dir": latest.resolve(),
            "key": "case",
        },
        {
            "reference_path": reference,
            "render_path": render,
            "artifact_dir": latest.resolve(),
            "key": "strict",
        },
    ]
    report = json.loads((latest / "typhoon-report.json").read_text(encoding="utf-8"))
    assert report[0]["status"] == "failed-threshold"
    assert report[0]["comparison"] == "flip"
    assert report[0]["flip_mean"] == 0.2
    assert report[0]["reference_png"] == str(latest / "reference" / "case.png")
    assert report[0]["render_png"] == str(latest / "render" / "case.png")
    assert report[0]["diff_png"] == str(latest / "flip" / "case.png")
    assert report[1]["status"] == "dry-run"
    assert report[2]["status"] == "failed-missing-threshold"
    assert report[2]["comparison"] == "flip"
    assert report[3]["status"] == "failed-missing-render"
    assert "comparison" not in report[3]
    assert report[4]["status"] == "failed-missing-reference"
    assert "comparison" not in report[4]

    summary = json.loads((latest / "run-summary.json").read_text(encoding="utf-8"))
    assert summary["total"] == 5
    assert summary["compared"] == 2
    assert summary["failed"] == 4
    assert summary["dry_run"] == 1
    assert (latest / "index.html").is_file()
    assert (output_root / "index.html").is_file()
