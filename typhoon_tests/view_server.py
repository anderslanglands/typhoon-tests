from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
from typing import Any
from urllib.parse import unquote, urlparse

from .config import find_suite_config

USDVIEW_ENDPOINT = "/__typhoon__/usdview"
UPDATE_THRESHOLDS_ENDPOINT = "/__typhoon__/thresholds"
UPDATE_REFERENCES_ENDPOINT = "/__typhoon__/references"
USD_SUFFIXES = {".usd", ".usda", ".usdc"}
LDR_IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff"}
IMAGE_SUFFIXES = {".exr", *LDR_IMAGE_SUFFIXES}
MAX_JSON_PAYLOAD_BYTES = 1024 * 1024


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
        endpoint = urlparse(self.path).path
        try:
            payload = self._read_json_payload()
            if endpoint == USDVIEW_ENDPOINT:
                command = launch_usdview(
                    payload,
                    project_root=self.server.project_root,
                    typhoon_provider=self.server.typhoon_provider,
                )
                self._send_json(200, {"ok": True, "command": command})
                return

            output_root = Path(str(self.directory)).resolve()
            if endpoint == UPDATE_THRESHOLDS_ENDPOINT:
                result = update_thresholds(
                    payload,
                    project_root=self.server.project_root,
                    output_root=output_root,
                    referer=self.headers.get("Referer", ""),
                )
                self._send_json(200, {"ok": True, **result})
                return

            if endpoint == UPDATE_REFERENCES_ENDPOINT:
                result = update_references(
                    payload,
                    project_root=self.server.project_root,
                    output_root=output_root,
                    referer=self.headers.get("Referer", ""),
                )
                self._send_json(200, {"ok": True, **result})
                return
        except ViewServerError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # pragma: no cover - defensive server boundary
            self._send_json(500, {"ok": False, "error": str(exc)})
            return

        self.send_error(404, "not found")

    def _read_json_payload(self) -> dict[str, Any]:
        length_text = self.headers.get("Content-Length") or "0"
        try:
            length = int(length_text)
        except ValueError as exc:
            raise ViewServerError("invalid content length") from exc
        if length <= 0:
            raise ViewServerError("missing request body")
        if length > MAX_JSON_PAYLOAD_BYTES:
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



def update_thresholds(
    payload: dict[str, Any],
    *,
    project_root: Path,
    output_root: Path,
    referer: str = "",
) -> dict[str, Any]:
    run_dir = _resolve_run_dir(payload.get("run"), output_root=output_root, referer=referer)
    results = _read_run_results(run_dir)
    updates = []

    for item in _payload_rows(payload):
        suite = _payload_optional_text(item, "suite")
        key = _payload_text(item, "key")
        row = _find_report_row(results, suite=suite, key=key)
        usd_path = _resolve_project_path(
            _report_text(row, "usd"),
            project_root=project_root,
            suffixes=USD_SUFFIXES,
            must_exist=True,
        )
        flip_mean = _report_float(row, "flip_mean")
        threshold = round_up_threshold(flip_mean)
        config_path, config_text = build_case_threshold_update(usd_path, threshold)
        if any(update["config_path"] == config_path for update in updates):
            raise ViewServerError(f"selected rows share threshold config: {config_path}")
        updates.append(
            {
                "row": row,
                "key": key,
                "flip_mean": flip_mean,
                "threshold": threshold,
                "config_path": config_path,
                "config_text": config_text,
            }
        )

    updated_rows = []
    for update in updates:
        update["config_path"].write_text(update["config_text"], encoding="utf-8")

    for update in updates:
        row = update["row"]
        threshold = update["threshold"]
        row["flip_threshold"] = threshold
        row["status"] = (
            "failed-threshold" if update["flip_mean"] > threshold else "passed"
        )
        updated_rows.append(
            {
                "suite": row.get("suite"),
                "key": update["key"],
                "threshold": threshold,
                "status": row.get("status"),
            }
        )

    _write_run_results(run_dir, results)
    return {"updated": len(updated_rows), "rows": updated_rows}


def update_references(
    payload: dict[str, Any],
    *,
    project_root: Path,
    output_root: Path,
    referer: str = "",
) -> dict[str, Any]:
    from .images import compare_images

    run_dir = _resolve_run_dir(payload.get("run"), output_root=output_root, referer=referer)
    results = _read_run_results(run_dir)
    updates = []

    for item in _payload_rows(payload):
        suite = _payload_optional_text(item, "suite")
        key = _payload_text(item, "key")
        row = _find_report_row(results, suite=suite, key=key)
        if row.get("frame") is not None:
            raise ViewServerError(
                "reference updates for frame-expanded cases are not supported"
            )
        old_reference_path = _resolve_allowed_path(
            _report_text(row, "reference"),
            allowed_roots=(project_root,),
            suffixes=IMAGE_SUFFIXES,
            must_exist=False,
        )
        render_path = _resolve_allowed_path(
            _report_text(row, "render_image", fallback_key="render_output"),
            allowed_roots=(project_root, output_root),
            suffixes=IMAGE_SUFFIXES,
            must_exist=True,
        )
        reference_path = _resolve_allowed_path(
            str(old_reference_path.with_name(render_path.name)),
            allowed_roots=(project_root,),
            suffixes=IMAGE_SUFFIXES,
            must_exist=False,
        )
        old_reference_image = None
        if row.get("reference_image"):
            old_reference_image = _resolve_allowed_path(
                str(row["reference_image"]),
                allowed_roots=(run_dir / "reference",),
                suffixes=IMAGE_SUFFIXES,
                must_exist=False,
            )
        usd_path = _resolve_project_path(
            _report_text(row, "usd"),
            project_root=project_root,
            suffixes=USD_SUFFIXES,
            must_exist=True,
        )
        config_path, config_text = build_case_reference_update(
            usd_path, reference_path
        )
        if any(update["old_reference_path"] == old_reference_path for update in updates):
            raise ViewServerError(
                f"selected rows share reference path: {old_reference_path}"
            )
        if any(update["reference_path"] == reference_path for update in updates):
            raise ViewServerError(
                f"selected renders share reference target: {reference_path}"
            )
        if any(update["config_path"] == config_path for update in updates):
            raise ViewServerError(
                f"selected rows share reference config: {config_path}"
            )
        updates.append(
            {
                "row": row,
                "key": key,
                "artifact_key": artifact_key(row),
                "old_reference_path": old_reference_path,
                "old_reference_image": old_reference_image,
                "reference_path": reference_path,
                "render_path": render_path,
                "config_path": config_path,
                "config_text": config_text,
            }
        )

    _validate_reference_update_paths(
        updates, results, project_root=project_root, run_dir=run_dir
    )

    updated_rows = []
    with tempfile.TemporaryDirectory(
        prefix="typhoon-reference-update-", dir=run_dir
    ) as temp_name:
        temp_root = Path(temp_name)
        staged_updates = []
        for index, update in enumerate(updates):
            render_path = update["render_path"]
            staged_reference = (
                temp_root / "references" / f"{index}{render_path.suffix.lower()}"
            )
            staged_reference.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(render_path, staged_reference)
            comparison = compare_images(
                reference_path=staged_reference,
                render_path=render_path,
                artifact_dir=temp_root / "artifacts",
                key=update["artifact_key"],
            )
            staged_updates.append(
                {
                    **update,
                    "staged_reference": staged_reference,
                    "comparison": comparison,
                }
            )

        updated_rows = _commit_reference_updates(
            staged_updates,
            results=results,
            run_dir=run_dir,
            output_root=output_root,
            backup_root=temp_root / "backups",
        )

    return {"updated": len(updated_rows), "rows": updated_rows}


def _commit_reference_updates(
    updates: list[dict[str, Any]],
    *,
    results: list[dict[str, Any]],
    run_dir: Path,
    output_root: Path,
    backup_root: Path,
) -> list[dict[str, Any]]:
    managed_paths = {
        run_dir / "typhoon-report.json",
        run_dir / "run-summary.json",
        run_dir / "index.html",
        output_root / "index.html",
    }
    for update in updates:
        reference_path = update["reference_path"]
        artifact_path_key = update["artifact_key"]
        reference_suffix = reference_path.suffix.lower()
        update["final_reference_image"] = (
            run_dir / "reference" / f"{artifact_path_key}{reference_suffix}"
        )
        update["final_diff_exr"] = (
            run_dir / "flip" / f"{artifact_path_key}.exr"
        )
        managed_paths.update(
            {
                update["old_reference_path"],
                reference_path,
                update["config_path"],
                update["final_reference_image"],
                update["final_diff_exr"],
            }
        )
        if update["old_reference_image"] is not None:
            managed_paths.add(update["old_reference_image"])

    backups = _backup_files(managed_paths, backup_root)
    updated_rows = []
    try:
        for update in updates:
            reference_path = update["reference_path"]
            comparison = update["comparison"]
            final_reference_image = update["final_reference_image"]
            final_diff_exr = update["final_diff_exr"]

            reference_path.parent.mkdir(parents=True, exist_ok=True)
            final_reference_image.parent.mkdir(parents=True, exist_ok=True)
            final_diff_exr.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(update["staged_reference"], reference_path)
            shutil.copyfile(comparison.reference_image, final_reference_image)
            shutil.copyfile(comparison.diff_exr, final_diff_exr)
            _write_text_atomic(update["config_path"], update["config_text"])

            row = update["row"]
            row.update(
                {
                    "status": _comparison_status(row, comparison.flip_mean),
                    "comparison": "flip",
                    "flip_mean": comparison.flip_mean,
                    "reference": str(reference_path),
                    "reference_image": str(final_reference_image),
                    "render_image": str(comparison.render_image),
                    "diff_exr": str(final_diff_exr),
                }
            )
            updated_rows.append(
                {
                    "suite": row.get("suite"),
                    "key": update["key"],
                    "status": row.get("status"),
                    "flip_mean": comparison.flip_mean,
                    "reference": str(reference_path),
                }
            )

        _write_run_results(run_dir, results)
        for update in updates:
            old_reference_path = update["old_reference_path"]
            reference_path = update["reference_path"]
            if old_reference_path != reference_path and old_reference_path.is_file():
                old_reference_path.unlink()
            old_reference_image = update["old_reference_image"]
            final_reference_image = update["final_reference_image"]
            if (
                old_reference_image is not None
                and old_reference_image != final_reference_image
                and old_reference_image.is_file()
            ):
                old_reference_image.unlink()
    except Exception:
        _restore_files(backups)
        raise

    return updated_rows


def _backup_files(
    paths: set[Path], backup_root: Path
) -> dict[Path, Path | None]:
    backup_root.mkdir(parents=True, exist_ok=True)
    backups = {}
    for index, path in enumerate(sorted(paths, key=str)):
        if path.is_file():
            backup = backup_root / str(index)
            shutil.copy2(path, backup)
            backups[path] = backup
        else:
            backups[path] = None
    return backups


def _restore_files(backups: dict[Path, Path | None]) -> None:
    for path, backup in backups.items():
        if backup is None:
            if path.is_file():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, path)



def _validate_reference_update_paths(
    updates: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    project_root: Path,
    run_dir: Path,
) -> None:
    selected_rows = {id(update["row"]) for update in updates}
    claimed_artifacts: dict[Path, tuple[str, str]] = {}
    claimed_paths: dict[Path, tuple[str, str]] = {}
    for update in updates:
        owner = (str(update["row"].get("suite") or ""), update["key"])
        for path in (update["old_reference_path"], update["reference_path"]):
            previous_owner = claimed_paths.get(path)
            if previous_owner is not None and previous_owner != owner:
                raise ViewServerError(f"selected rows share reference path: {path}")
            claimed_paths[path] = owner
        old_reference_image = update["old_reference_image"]
        if old_reference_image is not None:
            previous_owner = claimed_artifacts.get(old_reference_image)
            if previous_owner is not None and previous_owner != owner:
                raise ViewServerError(
                    f"selected rows share reference artifact: {old_reference_image}"
                )
            claimed_artifacts[old_reference_image] = owner
        target = update["reference_path"]
        if target != update["old_reference_path"] and target.exists():
            raise ViewServerError(f"reference target already exists: {target}")

    for row in results:
        if id(row) in selected_rows or not row.get("reference"):
            continue
        path = Path(str(row["reference"])).expanduser()
        if not path.is_absolute():
            path = project_root / path
        path = path.resolve()
        if path in claimed_paths:
            raise ViewServerError(f"report rows share reference path: {path}")
        reference_image = row.get("reference_image")
        if reference_image:
            artifact_path = Path(str(reference_image)).expanduser()
            if not artifact_path.is_absolute():
                artifact_path = run_dir / artifact_path
            artifact_path = artifact_path.resolve()
            if artifact_path in claimed_artifacts:
                raise ViewServerError(
                    f"report rows share reference artifact: {artifact_path}"
                )


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        if path.is_file():
            shutil.copymode(path, temp_path)
        else:
            temp_path.chmod(0o644)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()



def artifact_key(row: dict[str, Any]) -> str:
    key = str(row.get("key") or "")
    suite = str(row.get("suite") or "")
    for label, value in (("suite", suite), ("key", key)):
        if not value and label == "suite":
            continue
        if (
            not value
            or value in {".", ".."}
            or Path(value).name != value
            or "\\" in value
        ):
            raise ViewServerError(f"invalid report {label}: {value!r}")
    return f"{suite}/{key}" if suite else key


def round_up_threshold(value: float) -> float:
    return math.ceil((value - 1.0e-12) * 1000.0) / 1000.0


def write_case_threshold(usd_path: Path, threshold: float) -> Path:
    config_path, config_text = build_case_threshold_update(usd_path, threshold)
    config_path.write_text(config_text, encoding="utf-8")
    return config_path


def build_case_threshold_update(usd_path: Path, threshold: float) -> tuple[Path, str]:
    config_path = _case_config_path(usd_path)
    data: dict[str, Any] = {}
    if config_path.is_file():
        with config_path.open("rb") as file:
            loaded = tomllib.load(file)
        if not isinstance(loaded, dict):
            raise ViewServerError(f"invalid case config: {config_path}")
        data = loaded
    comparison = data.setdefault("comparison", {})
    if not isinstance(comparison, dict):
        raise ViewServerError(f"[comparison] must be a table in {config_path}")
    comparison["flip_threshold"] = threshold
    return config_path, format_toml(data)


def build_case_reference_update(
    usd_path: Path, reference_path: Path
) -> tuple[Path, str]:
    suite_config_path = find_suite_config(usd_path)
    if suite_config_path is None:
        raise ViewServerError(f"no suite config found for {usd_path}")

    config_path = _case_config_path(usd_path)
    data: dict[str, Any] = {}
    if config_path.is_file():
        with config_path.open("rb") as file:
            loaded = tomllib.load(file)
        if not isinstance(loaded, dict):
            raise ViewServerError(f"invalid case config: {config_path}")
        data = loaded
    reference = data.setdefault("reference", {})
    if not isinstance(reference, dict):
        raise ViewServerError(f"[reference] must be a table in {config_path}")
    relative_path = os.path.relpath(reference_path, suite_config_path.parent)
    reference["path"] = Path(relative_path).as_posix()
    return config_path, format_toml(data)



def _case_config_path(usd_path: Path) -> Path:
    primary = usd_path.with_suffix(".typhoon.toml")
    secondary = usd_path.with_name(usd_path.name + ".typhoon.toml")
    if primary.is_file():
        return primary
    if secondary.is_file():
        return secondary
    return primary


def format_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    scalar_items = [(key, value) for key, value in data.items() if not isinstance(value, dict)]
    for key, value in scalar_items:
        lines.append(f"{key} = {format_toml_value(value)}")
    if scalar_items:
        lines.append("")

    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        lines.append(f"[{section}]")
        for key, value in values.items():
            if isinstance(value, dict):
                raise ViewServerError("nested TOML tables are not supported for case config updates")
            lines.append(f"{key} = {format_toml_value(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:.12g}"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(format_toml_value(item) for item in value) + "]"
    if value is None:
        raise ViewServerError("cannot write null TOML values")
    raise ViewServerError(f"unsupported TOML value type: {type(value).__name__}")


def _read_run_results(run_dir: Path) -> list[dict[str, Any]]:
    from .report_html import REPORT_NAME, read_json_list

    report_path = run_dir / REPORT_NAME
    if not report_path.is_file():
        raise ViewServerError(f"missing report: {report_path}")
    return read_json_list(report_path)


def _write_run_results(run_dir: Path, results: list[dict[str, Any]]) -> None:
    from .pytest_plugin import write_run_outputs
    from .report_html import build_run_context

    write_run_outputs(build_run_context(run_dir, results), results)


def _comparison_status(row: dict[str, Any], flip_mean: float) -> str:
    threshold = row.get("flip_threshold")
    if threshold is not None and flip_mean > float(threshold):
        return "failed-threshold"
    return "passed"


def _payload_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ViewServerError("rows must be a non-empty list")
    if len(rows) > 1000:
        raise ViewServerError("too many selected rows")
    if not all(isinstance(item, dict) for item in rows):
        raise ViewServerError("each row must be an object")
    return rows


def _payload_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ViewServerError(f"{key} must be a non-empty string")
    if "\x00" in value:
        raise ViewServerError(f"{key} is invalid")
    return value


def _payload_optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ViewServerError(f"{key} must be a string")
    if "\x00" in value:
        raise ViewServerError(f"{key} is invalid")
    return value


def _report_text(row: dict[str, Any], key: str, *, fallback_key: str | None = None) -> str:
    value = row.get(key)
    if (not isinstance(value, str) or not value) and fallback_key is not None:
        value = row.get(fallback_key)
    if not isinstance(value, str) or not value:
        raise ViewServerError(f"report row {key} must be a non-empty string")
    if "\x00" in value:
        raise ViewServerError(f"report row {key} is invalid")
    return value


def _report_float(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool):
        raise ViewServerError(f"report row {key} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ViewServerError(f"report row {key} must be numeric") from exc
    if not math.isfinite(number):
        raise ViewServerError(f"report row {key} must be finite")
    return number


def _find_report_row(
    results: list[dict[str, Any]],
    *,
    key: str,
    suite: str | None = None,
    usd_path: Path | None = None,
) -> dict[str, Any]:
    matches = []
    for row in results:
        if str(row.get("key") or "") != key:
            continue
        if suite is not None and str(row.get("suite") or "") != suite:
            continue
        if usd_path is not None:
            row_usd = row.get("usd")
            if not row_usd:
                continue
            try:
                if Path(str(row_usd)).expanduser().resolve() != usd_path:
                    continue
            except OSError:
                continue
        matches.append(row)
    label = f"{suite}/{key}" if suite else key
    if len(matches) != 1:
        raise ViewServerError(f"expected exactly one report row for {label}, found {len(matches)}")
    return matches[0]


def _resolve_run_dir(value: object, *, output_root: Path, referer: str = "") -> Path:
    token = value if isinstance(value, str) and value else _run_path_from_referer(referer)
    if not token:
        raise ViewServerError("run path is required")
    if "\x00" in token:
        raise ViewServerError("run path is invalid")
    parsed = urlparse(token)
    raw_path = parsed.path or token
    if raw_path.endswith("/index.html"):
        raw_path = raw_path[: -len("index.html")]
    path = Path(unquote(raw_path).lstrip("/"))
    if path.name == "index.html":
        path = path.parent
    run_dir = (output_root / path).resolve()
    _require_relative_to(run_dir, output_root, "run path escapes output root")
    if not run_dir.is_dir():
        raise ViewServerError(f"run directory does not exist: {run_dir}")
    return run_dir


def _run_path_from_referer(value: str) -> str:
    if not value:
        return ""
    return urlparse(value).path


def _resolve_project_path(
    value: str,
    *,
    project_root: Path,
    suffixes: set[str],
    must_exist: bool,
) -> Path:
    return _resolve_allowed_path(
        value,
        allowed_roots=(project_root,),
        suffixes=suffixes,
        must_exist=must_exist,
    )


def _resolve_allowed_path(
    value: str,
    *,
    allowed_roots: tuple[Path, ...],
    suffixes: set[str],
    must_exist: bool,
) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = allowed_roots[0] / path
    path = path.resolve()
    if path.suffix.lower() not in suffixes:
        raise ViewServerError(f"unsupported file suffix: {path.suffix}")
    if must_exist and not path.is_file():
        raise ViewServerError(f"file does not exist: {path}")
    if not any(_is_relative_to(path, root.resolve()) for root in allowed_roots):
        raise ViewServerError(f"path is outside allowed roots: {path}")
    return path


def _require_relative_to(path: Path, root: Path, message: str) -> None:
    if not _is_relative_to(path, root.resolve()):
        raise ViewServerError(message)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


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
