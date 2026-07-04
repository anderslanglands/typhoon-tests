from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tools" / "exr_wasm" / "Cargo.toml"
WASM_TARGET = (
    ROOT
    / "tools"
    / "exr_wasm"
    / "target"
    / "wasm32-unknown-unknown"
    / "release"
    / "typhoon_exr_wasm.wasm"
)
STATIC_WASM = ROOT / "typhoon_tests" / "static" / "typhoon_exr_wasm.wasm"


def main() -> int:
    cargo = shutil.which("cargo")
    if cargo is None:
        print("error: cargo is required to build the EXR WASM viewer", file=sys.stderr)
        return 2

    subprocess.run(
        [
            cargo,
            "build",
            "--manifest-path",
            str(MANIFEST),
            "--release",
            "--target",
            "wasm32-unknown-unknown",
        ],
        check=True,
    )
    if not WASM_TARGET.is_file():
        print(f"error: cargo did not produce {WASM_TARGET}", file=sys.stderr)
        return 1

    STATIC_WASM.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WASM_TARGET, STATIC_WASM)
    print(f"wrote {STATIC_WASM}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
