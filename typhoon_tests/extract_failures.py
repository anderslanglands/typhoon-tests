from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Any, Sequence

from .pytest_plugin import (
    RunContext,
    allocate_run_context,
    is_failure_result,
    provider_label,
    write_run_outputs,
)
from .report_html import (
    REPORT_NAME,
    ReportRegenerationError,
    build_run_context,
    read_json_list,
    resolve_run_dir,
)


RUN_LOCAL_FILE_FIELDS = (
    "render_output",
    "render_image",
    "reference_image",
    "diff_exr",
    "reference_png",
    "render_png",
    "diff_png",
)

RUN_LOCAL_PATH_FIELDS = (
    "output_root",
    "artifact_root",
)


class ExtractFailuresError(ReportRegenerationError):
    pass


@dataclass(frozen=True)
class ExtractedFailures:
    source_run: Path
    run_dir: Path
    count: int


def extract_failures(
    *,
    output_root: Path | str = "_output",
    run: str | Path | None = None,
    started_at: str | None = None,
) -> ExtractedFailures:
    configured_output_base = Path(output_root).expanduser().resolve()
    source_run = resolve_run_dir(configured_output_base, run)
    report_path = source_run / REPORT_NAME
    if not report_path.is_file():
        raise ExtractFailuresError(f"missing {REPORT_NAME}: {report_path}")

    source_results = read_json_list(report_path)
    failures = [row for row in source_results if is_failure_result(row)]
    if not failures:
        raise ExtractFailuresError(f"no failed cases found in {source_run}")

    source_context = build_run_context(source_run, source_results)
    target_context = allocate_run_context(
        source_run.parent,
        started_at=started_at,
        provider=source_context.provider,
    )
    target_results = [
        extract_failure_row(row, source_run, target_context) for row in failures
    ]
    write_run_outputs(target_context, target_results)
    return ExtractedFailures(
        source_run=source_run,
        run_dir=target_context.run_dir,
        count=len(target_results),
    )


def extract_failure_row(
    row: dict[str, Any],
    source_run: Path,
    target_context: RunContext,
) -> dict[str, Any]:
    extracted = dict(row)
    target_run = target_context.run_dir

    for field in RUN_LOCAL_FILE_FIELDS:
        if extracted.get(field):
            extracted[field] = copy_run_local_file(
                extracted[field],
                source_run,
                target_run,
            )

    for field in RUN_LOCAL_PATH_FIELDS:
        if extracted.get(field):
            extracted[field] = rewrite_run_local_path(
                extracted[field],
                source_run,
                target_run,
            )

    command = extracted.get("command")
    if isinstance(command, list):
        extracted["command"] = [
            rewrite_absolute_run_path(part, source_run, target_run)
            if isinstance(part, str)
            else part
            for part in command
        ]

    extracted["run_number"] = target_context.run_number
    extracted["run_dir"] = str(target_run)
    extracted["started_at"] = target_context.started_at
    extracted["provider"] = provider_label(target_context.provider)
    return extracted


def copy_run_local_file(value: object, source_run: Path, target_run: Path) -> str:
    source_path, relative_path = resolve_run_local_path(value, source_run)
    if source_path is None or relative_path is None:
        return str(value)

    destination = (target_run / relative_path).resolve()
    if source_path.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
    return str(destination)


def rewrite_run_local_path(value: object, source_run: Path, target_run: Path) -> str:
    _source_path, relative_path = resolve_run_local_path(value, source_run)
    if relative_path is None:
        return str(value)
    return str((target_run / relative_path).resolve())


def rewrite_absolute_run_path(value: str, source_run: Path, target_run: Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        return value
    try:
        relative_path = path.resolve().relative_to(source_run.resolve())
    except ValueError:
        return value
    return str((target_run / relative_path).resolve())


def resolve_run_local_path(
    value: object,
    source_run: Path,
) -> tuple[Path | None, Path | None]:
    path = Path(str(value)).expanduser()
    source_root = source_run.resolve()
    if path.is_absolute():
        source_path = path.resolve()
        try:
            relative_path = source_path.relative_to(source_root)
        except ValueError:
            return None, None
        return source_path, relative_path

    source_path = (source_root / path).resolve()
    try:
        relative_path = source_path.relative_to(source_root)
    except ValueError:
        return None, None
    return source_path, relative_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a new numbered Typhoon run containing only failed rows copied "
            "from an existing run. Existing render artifacts are copied; tests are "
            "not rerendered."
        ),
    )
    parser.add_argument(
        "run",
        nargs="?",
        help=(
            "Run to extract. Accepts a run directory, run-NNNN name, or number. "
            "Defaults to the latest run under --output-root."
        ),
    )
    parser.add_argument(
        "--output-root",
        default="_output",
        help="Output base containing run-NNNN directories. Defaults to _output.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        extracted = extract_failures(output_root=args.output_root, run=args.run)
    except ReportRegenerationError as exc:
        parser.exit(2, f"error: {exc}\n")

    print(
        "extracted "
        f"{extracted.count} failures from {extracted.source_run} "
        f"to {extracted.run_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
