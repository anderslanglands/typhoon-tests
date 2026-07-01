from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import struct

import numpy as np


@dataclass(frozen=True)
class ImageComparison:
    reference_image: Path
    render_image: Path
    diff_exr: Path
    flip_mean: float


DARK_HDR_EXPOSURE_MAX = 1.0e-6
DARK_HDR_FLIP_PARAMETERS = {
    "startExposure": -10.0,
    "stopExposure": 10.0,
    "numExposures": 1,
}


def compare_images(
    *,
    reference_path: Path,
    render_path: Path,
    artifact_dir: Path,
    key: str,
) -> ImageComparison:
    flip_evaluator = import_flip_evaluator()

    reference_rgb = read_rgb(flip_evaluator, reference_path)
    render_rgb = read_rgb(flip_evaluator, render_path)
    reference_for_flip, render_for_flip, dynamic_range = comparison_inputs(
        reference_path,
        reference_rgb,
        render_path,
        render_rgb,
    )

    if reference_for_flip.shape[:2] != render_for_flip.shape[:2]:
        raise RuntimeError(
            "resolution mismatch for "
            f"{key}: reference {reference_for_flip.shape[:2]} "
            f"render {render_for_flip.shape[:2]}"
        )

    evaluate_kwargs = {
        "inputsRGB": True,
        "applyMagma": True,
        "computeMeanError": True,
    }
    flip_parameters = flip_evaluation_parameters(
        reference_for_flip, render_for_flip, dynamic_range
    )
    if flip_parameters:
        evaluate_kwargs["parameters"] = flip_parameters

    flip_map, mean_flip, _ = flip_evaluator.evaluate(
        reference_for_flip,
        render_for_flip,
        dynamic_range,
        **evaluate_kwargs,
    )

    reference_image = copy_reference_image(
        reference_path=reference_path,
        artifact_dir=artifact_dir,
        key=key,
    )
    diff_exr = artifact_dir / "flip" / f"{key}.exr"
    write_rgb_exr(diff_exr, np.asarray(flip_map, dtype=np.float32)[..., :3])

    return ImageComparison(
        reference_image=reference_image,
        render_image=render_path,
        diff_exr=diff_exr,
        flip_mean=float(mean_flip),
    )


def copy_reference_image(*, reference_path: Path, artifact_dir: Path, key: str) -> Path:
    suffix = reference_path.suffix.lower() or ".image"
    reference_image = artifact_dir / "reference" / f"{key}{suffix}"
    reference_image.parent.mkdir(parents=True, exist_ok=True)
    if reference_path.resolve() != reference_image.resolve():
        shutil.copy2(reference_path, reference_image)
    return reference_image


def write_rgb_exr(path: Path, rgb: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pixels = np.asarray(rgb, dtype=np.float32)
    if pixels.ndim != 3 or pixels.shape[-1] < 3:
        raise ValueError(f"expected RGB image with shape (height, width, channels), got {pixels.shape}")
    pixels = np.ascontiguousarray(pixels[..., :3], dtype=np.float32)
    height, width, _ = pixels.shape
    if width <= 0 or height <= 0:
        raise ValueError("cannot write an empty EXR image")

    header = bytearray()
    header += b"\x76\x2f\x31\x01"
    header += struct.pack("<I", 2)
    _write_exr_attr(header, "channels", "chlist", _exr_channel_list())
    _write_exr_attr(header, "compression", "compression", b"\0")
    data_window = struct.pack("<iiii", 0, 0, width - 1, height - 1)
    _write_exr_attr(header, "dataWindow", "box2i", data_window)
    _write_exr_attr(header, "displayWindow", "box2i", data_window)
    _write_exr_attr(header, "lineOrder", "lineOrder", b"\0")
    _write_exr_attr(header, "pixelAspectRatio", "float", struct.pack("<f", 1.0))
    _write_exr_attr(header, "screenWindowCenter", "v2f", struct.pack("<ff", 0.0, 0.0))
    _write_exr_attr(header, "screenWindowWidth", "float", struct.pack("<f", 1.0))
    header += b"\0"

    line_data_size = width * 3 * 4
    chunk_size = 8 + line_data_size
    first_chunk_offset = len(header) + height * 8
    offsets = b"".join(
        struct.pack("<Q", first_chunk_offset + y * chunk_size) for y in range(height)
    )

    with path.open("wb") as file:
        file.write(header)
        file.write(offsets)
        for y in range(height):
            file.write(struct.pack("<iI", y, line_data_size))
            line = pixels[y]
            for channel in (2, 1, 0):
                file.write(np.ascontiguousarray(line[:, channel], dtype="<f4").tobytes())


def _write_exr_attr(header: bytearray, name: str, type_name: str, value: bytes) -> None:
    header += name.encode("ascii") + b"\0"
    header += type_name.encode("ascii") + b"\0"
    header += struct.pack("<I", len(value))
    header += value


def _exr_channel_list() -> bytes:
    channels = bytearray()
    for name in ("B", "G", "R"):
        channels += name.encode("ascii") + b"\0"
        channels += struct.pack("<iB3xii", 2, 0, 1, 1)
    channels += b"\0"
    return bytes(channels)


def import_flip_evaluator() -> object:
    try:
        import flip_evaluator
    except ImportError as exc:
        raise RuntimeError(
            "flip-evaluator is required for Typhoon image comparisons"
        ) from exc
    return flip_evaluator


def flip_evaluation_parameters(
    reference_rgb: np.ndarray, render_rgb: np.ndarray, dynamic_range: str
) -> dict[str, float | int]:
    if dynamic_range != "HDR":
        return {}

    combined = np.concatenate([reference_rgb.ravel(), render_rgb.ravel()])
    if not np.isfinite(combined).all():
        return {}
    if combined.size == 0 or np.any(combined < 0.0):
        return {}
    if float(combined.max()) <= DARK_HDR_EXPOSURE_MAX:
        return dict(DARK_HDR_FLIP_PARAMETERS)
    return {}


def read_rgb(flip_evaluator: object, path: Path) -> np.ndarray:
    image = np.asarray(flip_evaluator.load(str(path)), dtype=np.float32)
    if image.ndim == 2:
        image = np.stack([image, image, image], axis=-1)
    if image.shape[-1] == 1:
        image = np.repeat(image, 3, axis=-1)
    return image[..., :3]


def comparison_inputs(
    reference_path: Path,
    reference_rgb: np.ndarray,
    render_path: Path,
    render_rgb: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, str]:
    if reference_path.suffix.lower() == ".exr" and render_path.suffix.lower() == ".exr":
        return reference_rgb, render_rgb, "HDR"
    return ldr_rgb_for_path(reference_path, reference_rgb), ldr_rgb_for_path(
        render_path, render_rgb
    ), "LDR"


def ldr_rgb_for_path(path: Path, rgb: np.ndarray) -> np.ndarray:
    if path.suffix.lower() == ".exr":
        return np.clip(linear_to_srgb(rgb), 0.0, 1.0)
    return np.clip(rgb, 0.0, 1.0)


def linear_to_srgb(linear: np.ndarray) -> np.ndarray:
    return np.where(
        linear <= 0.0031308,
        linear * 12.92,
        1.055 * np.power(np.maximum(linear, 0.0031308), 1.0 / 2.4) - 0.055,
    )
