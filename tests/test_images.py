from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
from PIL import Image

import typhoon_tests.images as images
from typhoon_tests.images import (
    compare_images,
    linear_to_srgb,
    preview_rgb_for_path,
    save_png,
)


def test_exr_preview_transfer_defers_clamp_to_png_write(tmp_path) -> None:
    linear = np.array([[[-0.25, 0.5, 4.0]]], dtype=np.float32)

    preview = preview_rgb_for_path(Path("render.exr"), linear)

    assert preview[0, 0, 0] < 0.0
    assert preview[0, 0, 2] > 1.0

    png_path = tmp_path / "preview.png"
    save_png(png_path, preview)

    pixel = np.asarray(Image.open(png_path))[0, 0]
    expected = (
        np.clip(linear_to_srgb(linear[0, 0]), 0.0, 1.0) * 255.0 + 0.5
    ).astype(np.uint8)
    np.testing.assert_array_equal(pixel, expected)


def test_save_png_clamps_and_sanitizes_non_finite_values(tmp_path) -> None:
    png_path = tmp_path / "nonfinite.png"

    save_png(
        png_path,
        np.array([[[-np.inf, np.nan, np.inf]]], dtype=np.float32),
    )

    pixel = np.asarray(Image.open(png_path))[0, 0]
    np.testing.assert_array_equal(pixel, np.array([0, 0, 255], dtype=np.uint8))


def test_compare_images_runs_flip_on_float_data_and_writes_srgb_previews(
    tmp_path,
    monkeypatch,
) -> None:
    reference_path = tmp_path / "reference.exr"
    render_path = tmp_path / "render.exr"
    reference_path.write_bytes(b"placeholder")
    render_path.write_bytes(b"placeholder")
    loaded = {
        str(reference_path): np.array(
            [[[2.0, 0.25, -1.0], [-np.inf, 0.0, 0.0]]], dtype=np.float32
        ),
        str(render_path): np.array(
            [[[4.0, 0.5, np.nan], [np.inf, 0.0, 0.0]]], dtype=np.float32
        ),
    }
    expected_reference_for_flip = loaded[str(reference_path)].copy()
    expected_render_for_flip = loaded[str(render_path)].copy()
    captured = {}

    def load(path: str) -> np.ndarray:
        return loaded[path]

    def evaluate(
        reference: np.ndarray,
        test: np.ndarray,
        dynamic_range: str,
        *,
        inputsRGB: bool,
        applyMagma: bool,
        computeMeanError: bool,
    ):
        captured.update(
            reference=reference.copy(),
            test=test.copy(),
            dynamic_range=dynamic_range,
            inputsRGB=inputsRGB,
            applyMagma=applyMagma,
            computeMeanError=computeMeanError,
        )
        return np.array([[[0.0, 0.5, 1.0]]], dtype=np.float32), 0.25, {}

    monkeypatch.setitem(
        sys.modules,
        "flip_evaluator",
        SimpleNamespace(load=load, evaluate=evaluate),
    )
    saved_png_inputs = {}
    original_save_png = images.save_png

    def capture_save_png(path: Path, rgb: np.ndarray) -> None:
        saved_png_inputs[path.parent.name] = np.asarray(rgb).copy()
        original_save_png(path, rgb)

    monkeypatch.setattr(images, "save_png", capture_save_png)

    comparison = compare_images(
        reference_path=reference_path,
        render_path=render_path,
        artifact_dir=tmp_path / "artifacts",
        key="case",
    )

    np.testing.assert_allclose(
        captured["reference"],
        expected_reference_for_flip,
        equal_nan=True,
    )
    np.testing.assert_allclose(
        captured["test"],
        expected_render_for_flip,
        equal_nan=True,
    )
    assert captured["dynamic_range"] == "HDR"
    assert captured["inputsRGB"] is True
    assert captured["applyMagma"] is True
    assert captured["computeMeanError"] is True
    assert comparison.flip_mean == 0.25

    assert saved_png_inputs["reference"][0, 0, 2] < 0.0
    assert np.isneginf(saved_png_inputs["reference"][0, 1, 0])
    assert saved_png_inputs["render"][0, 0, 0] > 1.0
    assert np.isnan(saved_png_inputs["render"][0, 0, 2])
    assert np.isposinf(saved_png_inputs["render"][0, 1, 0])

    render_pixel = np.asarray(Image.open(comparison.render_png))[0, 0]
    expected_render = (
        np.nan_to_num(
            np.clip(linear_to_srgb(np.array([4.0, 0.5, np.nan])), 0.0, 1.0),
            nan=0.0,
            posinf=1.0,
            neginf=0.0,
        )
        * 255.0
        + 0.5
    ).astype(np.uint8)
    np.testing.assert_array_equal(render_pixel, expected_render)

    reference_pixel = np.asarray(Image.open(comparison.reference_png))[0, 0]
    expected_reference = (
        np.clip(linear_to_srgb(np.array([2.0, 0.25, 0.0])), 0.0, 1.0)
        * 255.0
        + 0.5
    ).astype(np.uint8)
    np.testing.assert_array_equal(reference_pixel, expected_reference)


def test_near_black_hdr_comparisons_use_explicit_exposure_range(
    tmp_path, monkeypatch
) -> None:
    reference_path = tmp_path / "reference.exr"
    render_path = tmp_path / "render.exr"
    reference_path.write_bytes(b"placeholder")
    render_path.write_bytes(b"placeholder")
    loaded = {
        str(reference_path): np.array([[[2.0e-14, 0.0, 0.0]]], dtype=np.float32),
        str(render_path): np.zeros((1, 1, 3), dtype=np.float32),
    }
    captured = {}

    def load(path: str) -> np.ndarray:
        return loaded[path]

    def evaluate(
        reference: np.ndarray,
        test: np.ndarray,
        dynamic_range: str,
        *,
        inputsRGB: bool,
        applyMagma: bool,
        computeMeanError: bool,
        parameters: dict[str, float | int],
    ):
        captured.update(
            reference=reference.copy(),
            test=test.copy(),
            dynamic_range=dynamic_range,
            parameters=parameters.copy(),
        )
        return np.zeros((1, 1, 3), dtype=np.float32), 0.0, parameters

    monkeypatch.setitem(
        sys.modules,
        "flip_evaluator",
        SimpleNamespace(load=load, evaluate=evaluate),
    )

    compare_images(
        reference_path=reference_path,
        render_path=render_path,
        artifact_dir=tmp_path / "artifacts",
        key="case",
    )

    assert captured["dynamic_range"] == "HDR"
    np.testing.assert_allclose(captured["reference"], loaded[str(reference_path)])
    np.testing.assert_allclose(captured["test"], loaded[str(render_path)])
    assert captured["parameters"] == {
        "startExposure": -10.0,
        "stopExposure": 10.0,
        "numExposures": 1,
    }


def test_hdr_exposure_fallback_ignores_nonfinite_or_negative_inputs(
    tmp_path, monkeypatch
) -> None:
    reference_path = tmp_path / "reference.exr"
    render_path = tmp_path / "render.exr"
    reference_path.write_bytes(b"placeholder")
    render_path.write_bytes(b"placeholder")
    captured_parameters = []

    def evaluate(
        reference: np.ndarray,
        test: np.ndarray,
        dynamic_range: str,
        *,
        inputsRGB: bool,
        applyMagma: bool,
        computeMeanError: bool,
        parameters: dict[str, float | int] | None = None,
    ):
        captured_parameters.append(parameters)
        return np.zeros((1, 1, 3), dtype=np.float32), 0.0, parameters or {}

    monkeypatch.setitem(
        sys.modules,
        "flip_evaluator",
        SimpleNamespace(load=lambda path: loaded[path], evaluate=evaluate),
    )

    cases = [
        (
            np.array([[[np.inf, 0.0, 0.0]]], dtype=np.float32),
            np.zeros((1, 1, 3), dtype=np.float32),
        ),
        (
            np.array([[[-1.0e-7, 0.0, 0.0]]], dtype=np.float32),
            np.zeros((1, 1, 3), dtype=np.float32),
        ),
    ]
    for reference_rgb, render_rgb in cases:
        loaded = {str(reference_path): reference_rgb, str(render_path): render_rgb}
        compare_images(
            reference_path=reference_path,
            render_path=render_path,
            artifact_dir=tmp_path / "artifacts",
            key=f"case_{len(captured_parameters)}",
        )

    assert captured_parameters == [None, None]


def test_png_reference_comparisons_use_ldr_preview_inputs(tmp_path, monkeypatch) -> None:
    reference_path = tmp_path / "reference.png"
    render_path = tmp_path / "render.exr"
    reference_path.write_bytes(b"placeholder")
    render_path.write_bytes(b"placeholder")
    loaded = {
        str(reference_path): np.array([[[0.25, 2.0, 0.0]]], dtype=np.float32),
        str(render_path): np.array([[[0.5, 4.0, 0.0]]], dtype=np.float32),
    }
    captured = {}

    def load(path: str) -> np.ndarray:
        return loaded[path]

    def evaluate(
        reference: np.ndarray,
        test: np.ndarray,
        dynamic_range: str,
        *,
        inputsRGB: bool,
        applyMagma: bool,
        computeMeanError: bool,
    ):
        captured.update(
            reference=reference.copy(),
            test=test.copy(),
            dynamic_range=dynamic_range,
            inputsRGB=inputsRGB,
        )
        return np.zeros((1, 1, 3), dtype=np.float32), 0.5, {}

    monkeypatch.setitem(
        sys.modules,
        "flip_evaluator",
        SimpleNamespace(load=load, evaluate=evaluate),
    )

    compare_images(
        reference_path=reference_path,
        render_path=render_path,
        artifact_dir=tmp_path / "artifacts",
        key="case",
    )

    assert captured["dynamic_range"] == "LDR"
    assert captured["inputsRGB"] is True
    np.testing.assert_allclose(
        captured["reference"],
        np.clip(loaded[str(reference_path)], 0.0, 1.0),
    )
    np.testing.assert_allclose(
        captured["test"],
        np.clip(linear_to_srgb(loaded[str(render_path)]), 0.0, 1.0),
    )
