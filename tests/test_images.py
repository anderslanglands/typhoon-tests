from __future__ import annotations

from pathlib import Path
import json
import shutil
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import numpy as np
import pytest

from typhoon_tests.images import (
    compare_images,
    linear_to_srgb,
    write_rgb_exr,
)


def test_write_rgb_exr_round_trips_float_pixels_with_flip_loader(tmp_path) -> None:
    import flip_evaluator

    exr_path = tmp_path / "float-data.exr"
    pixels = np.array(
        [
            [[0.0, 0.25, 1.0], [4.0, -1.0, 0.5]],
            [[0.125, 0.5, 2.0], [8.0, 16.0, 32.0]],
        ],
        dtype=np.float32,
    )

    write_rgb_exr(exr_path, pixels)

    loaded = np.asarray(flip_evaluator.load(str(exr_path)), dtype=np.float32)[..., :3]
    np.testing.assert_allclose(loaded, pixels)


def test_static_wasm_decoder_reads_generated_exr(tmp_path) -> None:
    if shutil.which("node") is None:
        pytest.skip("node is required to validate the static WASM decoder")

    wasm_path = (
        Path(__file__).resolve().parents[1]
        / "typhoon_tests"
        / "static"
        / "typhoon_exr_wasm.wasm"
    )
    assert wasm_path.is_file()

    exr_path = tmp_path / "generated.exr"
    pixels = np.array([[[0.0, 0.25, 1.0], [4.0, -1.0, 0.5]]], dtype=np.float32)
    write_rgb_exr(exr_path, pixels)

    decoder = tmp_path / "decode.mjs"
    decoder.write_text(
        textwrap.dedent(
            """
            import fs from 'node:fs/promises';
            const wasmBytes = await fs.readFile(process.argv[2]);
            const exrBytes = new Uint8Array(await fs.readFile(process.argv[3]));
            const { instance } = await WebAssembly.instantiate(wasmBytes, {});
            const exports = instance.exports;
            const ptr = exports.typhoon_exr_alloc(exrBytes.byteLength);
            new Uint8Array(exports.memory.buffer, ptr, exrBytes.byteLength).set(exrBytes);
            const ok = exports.typhoon_exr_decode(ptr, exrBytes.byteLength);
            exports.typhoon_exr_dealloc(ptr, exrBytes.byteLength);
            if (!ok) {
              const errorPtr = exports.typhoon_exr_error_ptr();
              const errorLen = exports.typhoon_exr_error_len();
              const error = new TextDecoder().decode(new Uint8Array(exports.memory.buffer, errorPtr, errorLen));
              throw new Error(error);
            }
            const pixelsPtr = exports.typhoon_exr_pixels_ptr();
            const pixelsLen = exports.typhoon_exr_pixels_len();
            const decoded = Array.from(new Float32Array(exports.memory.buffer, pixelsPtr, pixelsLen));
            console.log(JSON.stringify({
              width: exports.typhoon_exr_width(),
              height: exports.typhoon_exr_height(),
              pixels: decoded,
            }));
            """
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["node", str(decoder), str(wasm_path), str(exr_path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    decoded = json.loads(completed.stdout)
    assert decoded["width"] == 2
    assert decoded["height"] == 1
    np.testing.assert_allclose(
        np.array(decoded["pixels"], dtype=np.float32).reshape(1, 2, 3),
        pixels,
    )


def test_compare_images_runs_flip_on_float_data_and_writes_exr_diff(
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
        return np.array([[[0.0, 0.5, 1.0], [1.0, 0.0, 0.25]]], dtype=np.float32), 0.25, {}

    monkeypatch.setitem(
        sys.modules,
        "flip_evaluator",
        SimpleNamespace(load=load, evaluate=evaluate),
    )

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
    assert comparison.reference_image == tmp_path / "artifacts" / "reference" / "case.exr"
    assert comparison.reference_image.read_bytes() == b"placeholder"
    assert comparison.render_image == render_path
    assert comparison.diff_exr == tmp_path / "artifacts" / "flip" / "case.exr"
    assert comparison.diff_exr.read_bytes().startswith(b"\x76\x2f\x31\x01")
    assert not (tmp_path / "artifacts" / "render").exists()


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


def test_png_reference_comparisons_use_ldr_inputs(tmp_path, monkeypatch) -> None:
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

    comparison = compare_images(
        reference_path=reference_path,
        render_path=render_path,
        artifact_dir=tmp_path / "artifacts",
        key="case",
    )

    assert comparison.reference_image == tmp_path / "artifacts" / "reference" / "case.png"
    assert comparison.reference_image.read_bytes() == b"placeholder"
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
