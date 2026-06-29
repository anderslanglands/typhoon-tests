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


def compare_images(
    *,
    reference_path: Path,
    render_path: Path,
    artifact_dir: Path,
    key: str,
    tonemap: str,
    transfer: str,
) -> ImageComparison:
    try:
        import flip_evaluator
    except ImportError as exc:
        raise RuntimeError(
            "flip-evaluator is required for Typhoon image comparisons"
        ) from exc

    reference_rgb = read_rgb(flip_evaluator, reference_path)
    render_rgb = read_rgb(flip_evaluator, render_path)
    preview_rgb = render_preview(render_rgb, tonemap, transfer)

    if reference_rgb.shape[:2] != preview_rgb.shape[:2]:
        raise RuntimeError(
            "resolution mismatch for "
            f"{key}: reference {reference_rgb.shape[:2]} render {preview_rgb.shape[:2]}"
        )

    reference_png = artifact_dir / "reference" / f"{key}.png"
    render_png = artifact_dir / "render" / f"{key}.png"
    diff_png = artifact_dir / "flip" / f"{key}.png"
    for directory in (reference_png.parent, render_png.parent, diff_png.parent):
        directory.mkdir(parents=True, exist_ok=True)

    if reference_path.suffix.lower() == ".png":
        shutil.copy2(reference_path, reference_png)
    else:
        save_png(reference_png, np.clip(reference_rgb, 0.0, 1.0))

    save_png(render_png, preview_rgb)
    flip_map, mean_flip, _ = flip_evaluator.evaluate(
        reference_rgb,
        preview_rgb,
        "LDR",
        inputsRGB=True,
        applyMagma=True,
        computeMeanError=True,
    )
    save_png(diff_png, np.asarray(flip_map, dtype=np.float32)[..., :3])

    return ImageComparison(
        reference_png=reference_png,
        render_png=render_png,
        diff_png=diff_png,
        flip_mean=float(mean_flip),
    )


def read_rgb(flip_evaluator: object, path: Path) -> np.ndarray:
    image = np.asarray(flip_evaluator.load(str(path)), dtype=np.float32)
    if image.ndim == 2:
        image = np.stack([image, image, image], axis=-1)
    if image.shape[-1] == 1:
        image = np.repeat(image, 3, axis=-1)
    rgb = image[..., :3]
    return np.nan_to_num(rgb, nan=0.0, posinf=1.0, neginf=0.0)


def render_preview(render_rgb: np.ndarray, tonemap: str, transfer: str) -> np.ndarray:
    rgb = np.clip(render_rgb, 0.0, None)
    if tonemap == "reinhard":
        rgb = rgb / (1.0 + rgb)
    elif tonemap == "clamp":
        rgb = np.clip(rgb, 0.0, 1.0)
    else:
        raise ValueError(f"unknown tonemap: {tonemap}")

    if transfer == "linear-to-srgb":
        rgb = linear_to_srgb(rgb)
    elif transfer == "identity":
        pass
    else:
        raise ValueError(f"unknown transfer: {transfer}")

    return np.clip(rgb, 0.0, 1.0)


def linear_to_srgb(linear: np.ndarray) -> np.ndarray:
    linear = np.clip(linear, 0.0, None)
    return np.where(
        linear <= 0.0031308,
        linear * 12.92,
        1.055 * np.power(linear, 1.0 / 2.4) - 0.055,
    )


def save_png(path: Path, rgb: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    u8 = (np.clip(rgb, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    Image.fromarray(u8).save(path)
