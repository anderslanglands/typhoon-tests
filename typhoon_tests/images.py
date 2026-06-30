from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class ImageComparison:
    reference_png: Path
    render_png: Path
    diff_png: Path
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
    try:
        import flip_evaluator
    except ImportError as exc:
        raise RuntimeError(
            "flip-evaluator is required for Typhoon image comparisons"
        ) from exc

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

    reference_png = artifact_dir / "reference" / f"{key}.png"
    render_png = artifact_dir / "render" / f"{key}.png"
    diff_png = artifact_dir / "flip" / f"{key}.png"
    for directory in (reference_png.parent, render_png.parent, diff_png.parent):
        directory.mkdir(parents=True, exist_ok=True)

    if reference_path.suffix.lower() == ".png":
        shutil.copy2(reference_path, reference_png)
    else:
        save_png(reference_png, preview_rgb_for_path(reference_path, reference_rgb))

    save_png(render_png, preview_rgb_for_path(render_path, render_rgb))

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
    save_png(diff_png, np.asarray(flip_map, dtype=np.float32)[..., :3])

    return ImageComparison(
        reference_png=reference_png,
        render_png=render_png,
        diff_png=diff_png,
        flip_mean=float(mean_flip),
    )


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


def preview_rgb_for_path(path: Path, rgb: np.ndarray) -> np.ndarray:
    if path.suffix.lower() == ".exr":
        return preview_rgb(rgb)
    return np.clip(rgb, 0.0, 1.0)


def preview_rgb(linear_rgb: np.ndarray) -> np.ndarray:
    return linear_to_srgb(linear_rgb)


def linear_to_srgb(linear: np.ndarray) -> np.ndarray:
    return np.where(
        linear <= 0.0031308,
        linear * 12.92,
        1.055 * np.power(np.maximum(linear, 0.0031308), 1.0 / 2.4) - 0.055,
    )


def save_png(path: Path, rgb: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clamped = np.nan_to_num(
        np.clip(rgb, 0.0, 1.0), nan=0.0, posinf=1.0, neginf=0.0
    )
    u8 = (clamped * 255.0 + 0.5).astype(np.uint8)
    Image.fromarray(u8).save(path)
