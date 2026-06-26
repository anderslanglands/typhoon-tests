#!/usr/bin/env python3
import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path


def truthy_env(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}


def paths() -> tuple[Path, Path, Path]:
    root = Path(os.environ["PIXI_PROJECT_ROOT"])
    materialx_dir = root / "materialx"
    output_root = Path(os.environ.get("MATERIALX_RENDER_OUTPUT_ROOT", materialx_dir / "renders")).resolve()
    openusd_pixi = Path(os.environ.get("OPENUSD_PIXI", Path.home() / "code" / "openusd-omniverse" / "pixi.toml")).resolve()
    return materialx_dir, output_root, openusd_pixi


def render_cmd(openusd_pixi: Path, usd_file: Path, output_root: Path) -> list[str]:
    return [
        "pixi",
        "run",
        "--manifest-path",
        str(openusd_pixi),
        "usdrender",
        "--disableCameraLight",
        str(usd_file),
        "--outputRoot",
        str(output_root),
    ]


def run_command(cmd: list[str], dry_run: bool) -> int:
    if dry_run:
        print(" ".join(shlex.quote(part) for part in cmd))
        return 0
    return subprocess.call(cmd)


def render_one(usd_arg: str) -> int:
    materialx_dir, output_root, openusd_pixi = paths()
    root = materialx_dir.parent
    usd_file = Path(usd_arg)
    if not usd_file.is_absolute():
        usd_file = (root / usd_file).resolve()

    dry_run = truthy_env("MATERIALX_RENDER_DRY_RUN")
    if not dry_run:
        output_root.mkdir(parents=True, exist_ok=True)

    return run_command(render_cmd(openusd_pixi, usd_file, output_root), dry_run)


def render_all() -> int:
    materialx_dir, output_root, openusd_pixi = paths()
    dry_run = truthy_env("MATERIALX_RENDER_DRY_RUN")
    fail_fast = truthy_env("MATERIALX_RENDER_FAIL_FAST")

    usd_files = sorted(path for path in materialx_dir.glob("*.usda") if path.name != "base.usda")
    if not usd_files:
        print(f"No test USDA files found in {materialx_dir}", file=sys.stderr)
        return 1

    if not dry_run:
        output_root.mkdir(parents=True, exist_ok=True)

    print(f"OpenUSD manifest: {openusd_pixi}")
    print(f"Output root: {output_root}")
    print(f"Test files: {len(usd_files)}")

    failures: list[tuple[str, int]] = []
    for index, usd_file in enumerate(usd_files, start=1):
        print(f"[{index:03d}/{len(usd_files):03d}] {usd_file.name}")
        cmd = render_cmd(openusd_pixi, usd_file, output_root)
        if dry_run:
            print("  " + " ".join(shlex.quote(part) for part in cmd))
            continue

        status = subprocess.call(cmd)
        if status:
            failures.append((usd_file.name, status))
            print(f"  failed with exit code {status}", file=sys.stderr)
            if fail_fast:
                break

    if failures:
        print(f"\nFailed renders ({len(failures)}):", file=sys.stderr)
        for name, status in failures:
            print(f"  {name}: {status}", file=sys.stderr)
        return 1

    print(f"\nRendered {len(usd_files)} test USDAs into {output_root}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Render generated MaterialX USDA tests through the OpenUSD Pixi workspace.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    one = subparsers.add_parser("one", help="render one USDA")
    one.add_argument("usd")
    subparsers.add_parser("all", help="render all generated MaterialX test USDAs")

    args = parser.parse_args(argv)
    if args.command == "one":
        return render_one(args.usd)
    if args.command == "all":
        return render_all()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
