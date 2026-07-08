from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .pytest_plugin import (
    RUN_DIR_RE,
    RunContext,
    build_html_report,
    build_output_index,
    copy_report_assets,
    copy_report_favicon,
    summarize_results,
    provider_label,
    read_text_file,
)


REPORT_NAME = "typhoon-report.json"
SUMMARY_NAME = "run-summary.json"
PROJECT_ROOT_MARKERS = ("pyproject.toml", "pixi.toml", ".git")


class ReportRegenerationError(RuntimeError):
    pass


def regenerate_html(
    *,
    output_root: Path | str = "_output",
    run: str | Path | None = None,
    all_runs: bool = False,
) -> list[Path]:
    output_base = Path(output_root).expanduser().resolve()
    if all_runs and run is not None:
        raise ReportRegenerationError("pass either --all or --run, not both")

    if all_runs:
        run_dirs = discover_run_dirs(output_base)
        if not run_dirs:
            raise ReportRegenerationError(f"no run directories found under {output_base}")
    else:
        run_dirs = [resolve_run_dir(output_base, run)]

    written: list[Path] = []
    for run_dir in run_dirs:
        written.extend(regenerate_run_html(run_dir))

    output_index = output_base / "index.html"
    output_index.write_text(build_output_index(output_base), encoding="utf-8")
    written.append(output_index)
    written.extend(copy_report_favicon(output_base))
    return written


def regenerate_run_html(run_dir: Path) -> list[Path]:
    run_dir = run_dir.expanduser().resolve()
    report_path = run_dir / REPORT_NAME
    if not report_path.is_file():
        raise ReportRegenerationError(f"missing {REPORT_NAME}: {report_path}")

    results = read_json_list(report_path)
    populate_missing_usda_sources(results, run_dir)
    context = build_run_context(run_dir, results)
    summary = summarize_results(context, results)

    summary_path = run_dir / SUMMARY_NAME
    index_path = run_dir / "index.html"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    index_path.write_text(build_html_report(results, context), encoding="utf-8")
    return [summary_path, index_path, *copy_report_assets(run_dir)]


def populate_missing_usda_sources(
    results: list[dict[str, Any]],
    run_dir: Path,
) -> None:
    allowed_roots = source_backfill_roots(run_dir)
    for row in results:
        if isinstance(row.get("usd_source"), str) and row.get("usd_source_name"):
            continue
        source_path = legacy_usda_source_path(row.get("usd"), allowed_roots)
        if source_path is None:
            continue
        row["usd_source_name"] = source_path.name
        if not isinstance(row.get("usd_source"), str):
            row["usd_source"] = read_text_file(source_path)


def source_backfill_roots(run_dir: Path) -> tuple[Path, ...]:
    roots: list[Path] = []
    for candidate in (
        find_project_root(Path.cwd()) or Path.cwd().resolve(),
        find_project_root(run_dir),
    ):
        if candidate is None:
            continue
        root = candidate.resolve()
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def find_project_root(start: Path) -> Path | None:
    path = start.expanduser().resolve()
    if path.is_file():
        path = path.parent
    for candidate in (path, *path.parents):
        if any((candidate / marker).exists() for marker in PROJECT_ROOT_MARKERS):
            return candidate
    return None


def legacy_usda_source_path(
    value: object,
    allowed_roots: tuple[Path, ...],
) -> Path | None:
    if not value:
        return None
    raw_path = Path(str(value)).expanduser()
    candidates = (
        [raw_path]
        if raw_path.is_absolute()
        else [root / raw_path for root in allowed_roots]
    )
    for candidate in candidates:
        path = candidate.resolve()
        if path.suffix != ".usda" or not path.is_file():
            continue
        if any(is_relative_to(path, root) for root in allowed_roots):
            return path
    return None


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_run_dir(output_base: Path, run: str | Path | None) -> Path:
    if run is None:
        return latest_run_dir(output_base)

    value = Path(run).expanduser()
    if value.is_absolute() or len(value.parts) > 1:
        return value.resolve()

    token = str(run)
    if token.isdigit():
        return (output_base / f"run-{int(token):04d}").resolve()
    return (output_base / token).resolve()


def latest_run_dir(output_base: Path) -> Path:
    run_dirs = discover_run_dirs(output_base)
    if not run_dirs:
        raise ReportRegenerationError(f"no run directories found under {output_base}")
    return run_dirs[-1]


def discover_run_dirs(output_base: Path) -> list[Path]:
    output_base = output_base.expanduser().resolve()
    if not output_base.is_dir():
        return []

    run_dirs = []
    for child in output_base.iterdir():
        if not child.is_dir():
            continue
        match = RUN_DIR_RE.match(child.name)
        if match:
            run_dirs.append((int(match.group(1)), child.resolve()))
    return [path for _, path in sorted(run_dirs)]


def build_run_context(run_dir: Path, results: list[dict[str, Any]]) -> RunContext:
    summary = read_json_dict(run_dir / SUMMARY_NAME, required=False)
    match = RUN_DIR_RE.match(run_dir.name)
    run_number = int(summary.get("run_number") or (match.group(1) if match else 0))
    started_at = str(summary.get("started_at") or first_started_at(results))
    provider = summary.get("provider") or first_provider(results)
    return RunContext(
        output_base=run_dir.parent,
        run_dir=run_dir,
        run_number=run_number,
        started_at=started_at,
        provider=provider_label(provider),
    )


def first_started_at(results: list[dict[str, Any]]) -> str:
    for result in results:
        started_at = result.get("started_at")
        if started_at:
            return str(started_at)
    return ""


def first_provider(results: list[dict[str, Any]]) -> str:
    for result in results:
        provider = result.get("provider")
        if provider:
            return str(provider)
    return ""


def read_json_list(path: Path) -> list[dict[str, Any]]:
    data = read_json(path)
    if not isinstance(data, list):
        raise ReportRegenerationError(f"expected {path} to contain a JSON list")
    if not all(isinstance(item, dict) for item in data):
        raise ReportRegenerationError(f"expected {path} to contain result objects")
    return data


def read_json_dict(path: Path, *, required: bool = True) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise ReportRegenerationError(f"missing JSON file: {path}")
        return {}
    data = read_json(path)
    if not isinstance(data, dict):
        raise ReportRegenerationError(f"expected {path} to contain a JSON object")
    return data


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReportRegenerationError(f"invalid JSON in {path}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Regenerate Typhoon HTML reports from saved typhoon-report.json files.",
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
        help="Regenerate every run under --output-root.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        written = regenerate_html(
            output_root=args.output_root,
            run=args.run,
            all_runs=args.all,
        )
    except ReportRegenerationError as exc:
        parser.exit(2, f"error: {exc}\n")

    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
