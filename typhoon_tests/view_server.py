from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import subprocess
from typing import Any
from urllib.parse import urlparse

USDVIEW_ENDPOINT = "/__typhoon__/usdview"
USD_SUFFIXES = {".usd", ".usda", ".usdc"}


class ViewServerError(ValueError):
    pass


class TyphoonViewServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[SimpleHTTPRequestHandler],
        *,
        project_root: Path,
        typhoon_provider: Path | None = None,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.project_root = project_root.resolve()
        self.typhoon_provider = typhoon_provider.resolve() if typhoon_provider else None


class TyphoonViewHandler(SimpleHTTPRequestHandler):
    server: TyphoonViewServer

    def do_POST(self) -> None:
        if urlparse(self.path).path != USDVIEW_ENDPOINT:
            self.send_error(404, "not found")
            return

        try:
            payload = self._read_json_payload()
            command = launch_usdview(
                payload,
                project_root=self.server.project_root,
                typhoon_provider=self.server.typhoon_provider,
            )
        except ViewServerError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # pragma: no cover - defensive server boundary
            self._send_json(500, {"ok": False, "error": str(exc)})
            return

        self._send_json(200, {"ok": True, "command": command})

    def _read_json_payload(self) -> dict[str, Any]:
        length_text = self.headers.get("Content-Length") or "0"
        try:
            length = int(length_text)
        except ValueError as exc:
            raise ViewServerError("invalid content length") from exc
        if length <= 0:
            raise ViewServerError("missing request body")
        if length > 8192:
            raise ViewServerError("request body is too large")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ViewServerError("request body must be JSON") from exc
        if not isinstance(payload, dict):
            raise ViewServerError("request body must be a JSON object")
        return payload

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = (json.dumps(payload) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_usdview_command(
    payload: dict[str, Any],
    *,
    project_root: Path,
    typhoon_provider: Path | None = None,
) -> list[str]:
    usd_path = _resolve_usd_path(payload.get("usd"), project_root=project_root)
    camera_path = _validate_camera_path(payload.get("camera"))
    frame = _format_frame(payload.get("frame"))

    command = _usdview_command_prefix(typhoon_provider)
    command.extend(["--renderer", "Embree", "--disableCameraLight"])
    if camera_path:
        command.extend(["--camera", camera_path])
    command.extend(["--complexity", "high"])
    if frame:
        command.extend(["--cf", frame])
    command.append(str(usd_path))
    return command


def launch_usdview(
    payload: dict[str, Any],
    *,
    project_root: Path,
    typhoon_provider: Path | None = None,
) -> list[str]:
    command = build_usdview_command(
        payload,
        project_root=project_root,
        typhoon_provider=typhoon_provider,
    )
    subprocess.Popen(
        command,
        cwd=str(project_root.resolve()),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return command


def _usdview_command_prefix(typhoon_provider: Path | None) -> list[str]:
    if typhoon_provider is None:
        return ["pixi", "run", "usdview"]
    manifest = typhoon_provider
    if manifest.is_dir():
        manifest = manifest / "pixi.toml"
    if not manifest.is_file():
        raise ViewServerError(
            "typhoon provider must point to an OpenUSD checkout or pixi.toml; "
            f"missing manifest: {manifest}"
        )
    return [
        "pixi",
        "run",
        "--manifest-path",
        str(manifest),
        "--clean-env",
        "usdview",
    ]


def _resolve_usd_path(value: object, *, project_root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ViewServerError("usd must be a path string")
    if "\x00" in value:
        raise ViewServerError("usd path is invalid")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root / path
    path = path.resolve()
    if path.suffix.lower() not in USD_SUFFIXES:
        raise ViewServerError("usd path must end in .usd, .usda, or .usdc")
    if not path.is_file():
        raise ViewServerError(f"usd path does not exist: {path}")
    return path


def _validate_camera_path(value: object) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ViewServerError("camera must be a USD prim path string")
    if not value.startswith("/") or any(char.isspace() or char == "\x00" for char in value):
        raise ViewServerError("camera must be an absolute USD prim path without whitespace")
    return value


def _format_frame(value: object) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, bool):
        raise ViewServerError("frame must be numeric")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ViewServerError("frame must be finite")
        return str(int(value)) if value.is_integer() else f"{value:g}"
    if isinstance(value, str):
        if any(char.isspace() or char == "\x00" for char in value):
            raise ViewServerError("frame must not contain whitespace")
        try:
            frame = float(value)
        except ValueError as exc:
            raise ViewServerError("frame must be numeric") from exc
        if not math.isfinite(frame):
            raise ViewServerError("frame must be finite")
        return str(int(frame)) if frame.is_integer() else f"{frame:g}"
    raise ViewServerError("frame must be numeric")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve Typhoon reports and launch local usdview requests.")
    parser.add_argument("--directory", default="_output", help="Output directory to serve.")
    parser.add_argument("--bind", default="127.0.0.1", help="Address to bind.")
    parser.add_argument("--port", default=8000, type=int, help="Port to bind.")
    parser.add_argument("--project-root", default=".", help="Repository root used to run pixi commands.")
    parser.add_argument(
        "--typhoon-provider",
        default=None,
        help="OpenUSD/Typhoon checkout or pixi.toml used for usdview launches.",
    )
    args = parser.parse_args(argv)

    directory = Path(args.directory).expanduser().resolve()
    project_root = Path(args.project_root).expanduser().resolve()
    provider = Path(args.typhoon_provider).expanduser() if args.typhoon_provider else None
    handler = partial(TyphoonViewHandler, directory=str(directory))
    with TyphoonViewServer(
        (args.bind, args.port),
        handler,
        project_root=project_root,
        typhoon_provider=provider,
    ) as server:
        print(f"Serving Typhoon reports from {directory} at http://{args.bind}:{args.port}/")
        print("Press Ctrl-C to stop.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
