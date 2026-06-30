from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .images import compare_images
from .pytest_plugin import is_failure_result, write_run_outputs
from .report_html import (
    REPORT_NAME,
    ReportRegenerationError,
    build_run_context,
    discover_run_dirs,
    read_json_list,
    resolve_run_dir,
)


SKIP_STATUSES = {
    "dry-run",
    "failed-command",
    "failed-config",
    "failed-launch",
    "failed-render",
    "failed-missing-render",
    "failed-missing-reference",
}

COMPARISON_KEYS = {
    "comparison",
    "flip_mean",
    "reference_png",
    "render_png",
    "diff_png",
}


def regenerate_comparisons(
    *,
    output_root: Path | str = "_output",
    run: str | Path | None = None,
    all_runs: bool = False,
) -> list[tuple[Path, int, int]]:
    output_base = Path(output_root).expanduser().resolve()
    if all_runs and run is not None:
        raise ReportRegenerationError("pass either --all or --run, not both")

    if all_runs:
        run_dirs = discover_run_dirs(output_base)
        if not run_dirs:
            raise ReportRegenerationError(f"no run directories found under {output_base}")
    else:
        run_dirs = [resolve_run_dir(output_base, run)]

    summaries = []
    for run_dir in run_dirs:
        compared, skipped = regenerate_run_comparisons(run_dir)
        summaries.append((run_dir, compared, skipped))
    return summaries


def regenerate_run_comparisons(run_dir: Path) -> tuple[int, int]:
    run_dir = run_dir.expanduser().resolve()
    report_path = run_dir / REPORT_NAME
    if not report_path.is_file():
        raise ReportRegenerationError(f"missing {REPORT_NAME}: {report_path}")

    results = read_json_list(report_path)
    context = build_run_context(run_dir, results)
    compared = 0
    skipped = 0

    for row in results:
        if not should_regenerate(row):
            skipped += 1
            continue

        render_path = Path(str(row["render_output"])).expanduser()
        reference_path = Path(str(row["reference"])).expanduser()
        if not render_path.is_file():
            raise ReportRegenerationError(
                f"missing render output for {row.get('key')}: {render_path}"
            )
        if not reference_path.is_file():
            raise ReportRegenerationError(
                f"missing reference for {row.get('key')}: {reference_path}"
            )

        for key in COMPARISON_KEYS:
            row.pop(key, None)

        comparison = compare_images(
            reference_path=reference_path,
            render_path=render_path,
            artifact_dir=run_dir,
            key=str(row.get("key", render_path.stem)),
        )
        row.update(
            {
                "status": comparison_status(row, comparison.flip_mean),
                "comparison": "flip",
                "flip_mean": comparison.flip_mean,
                "reference_png": str(comparison.reference_png),
                "render_png": str(comparison.render_png),
                "diff_png": str(comparison.diff_png),
            }
        )
        compared += 1

    write_run_outputs(context, results)
    return compared, skipped


def should_regenerate(row: dict[str, object]) -> bool:
    status = str(row.get("status") or "")
    if status in SKIP_STATUSES:
        return False
    if not row.get("render_output") or not row.get("reference"):
        return False
    return True


def comparison_status(row: dict[str, object], flip_mean: float) -> str:
    threshold = row.get("flip_threshold")
    if threshold is None and row.get("status") == "failed-missing-threshold":
        return "failed-missing-threshold"
    if threshold is not None and flip_mean > float(threshold):
        return "failed-threshold"
    if is_failure_result(row):
        return "passed"
    return "passed"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Regenerate Typhoon comparison PNGs, FLIP metrics, JSON reports, "
            "and HTML from existing render outputs. Does not rerender EXRs."
        ),
    )
    parser.add_argument(
        "--output-root",
        default="_output",
        help="Output base containing run-NNNN directories. Defaults to _output.",
    )
    parser.add_argument(
        "--run",
        default=None,
        help=(
            "Run to regenerate. Accepts a run directory, run-NNNN name, or number. "
            "Defaults to the latest run under --output-root."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Regenerate comparisons for every run under --output-root.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        summaries = regenerate_comparisons(
            output_root=args.output_root,
            run=args.run,
            all_runs=args.all,
        )
    except ReportRegenerationError as exc:
        parser.exit(2, f"error: {exc}\n")

    for run_dir, compared, skipped in summaries:
        print(f"regenerated {compared} comparisons for {run_dir} ({skipped} skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
