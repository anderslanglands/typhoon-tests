# typhoon-tests

This repo contains USD render test assets for Typhoon-focused validation. The current generated suite is the MaterialX USD render test suite under `materialx/`.

## MaterialX USD Suite

The MaterialX suite is generated from `/home/anders/code/MaterialX/resources/Materials/TestSuite/_options.mtlx` and contains 143 per-material USDA test layers plus the shared `materialx/base.usda` layer.

`materialx/base.usda` provides the shared scene:

- `/World/Sphere`: USD mesh converted from the MaterialX `sphere.obj`.
- `/World/DomeLight`: latlong IBL using `materialx/assets/Lights/san_giuseppe_bridge.hdr`.
- `/World/Camera`: 45 degree square FOV, `focalLength = 50`, `horizontalAperture = 41.421356`, `verticalAperture = 41.421356`.
- `/Render/Settings`, `/Render/Settings/Product`, and `/Render/Settings/Product/Var`.

Each material test USDA sublayers `base.usda`, binds `/World/Sphere`, defines its material under `/Looks`, and sets a unique render product filename:

```text
materialx.<material-test-name>.exr
```

Referenced images and HDR maps are copied under `materialx/assets/`, and asset paths in the USDA files point to those local copies. See `materialx/README.md` for the full material list, excluded source files, and generation notes.

## Rendering

Rendering is exposed through Pixi tasks in this repo. The tasks reuse the OpenUSD Pixi workspace at `/home/anders/code/openusd-omniverse/pixi.toml` by invoking `pixi run --manifest-path`; Pixi is not treating that workspace as a published package dependency.

Render all MaterialX test USDAs:

```bash
pixi run render-materialx-all
```

Render one MaterialX test USDA:

```bash
pixi run render-materialx-one materialx/open_pbr_carpaint_Car_Paint.usda
```

The OpenUSD `usdrender` task already supplies `--renderer Embree --complexity high`. The local wrapper adds `--disableCameraLight`, the test USDA path, and `--outputRoot`.

Default output root:

```text
materialx/renders
```

Useful overrides:

```bash
MATERIALX_RENDER_DRY_RUN=1 pixi run render-materialx-all
MATERIALX_RENDER_FAIL_FAST=1 pixi run render-materialx-all
MATERIALX_RENDER_OUTPUT_ROOT=/tmp/materialx-renders pixi run render-materialx-all
OPENUSD_PIXI=/path/to/openusd/pixi.toml pixi run render-materialx-all
```

## Validation

Useful no-render checks:

```bash
pixi task list
MATERIALX_RENDER_DRY_RUN=1 pixi run render-materialx-one materialx/open_pbr_carpaint_Car_Paint.usda
MATERIALX_RENDER_DRY_RUN=1 pixi run render-materialx-all
```
## Image Comparison

Generate a dark HTML comparison page between the MaterialX GLSL PNG references and the local USD EXR renders:

```bash
pixi run compare-materialx-renders
```

The page is written to:

```text
materialx/comparison/index.html
```

The generator matches files by test key:

```text
/home/anders/code/MaterialX/build/glsl-render-tests/flat-glsl-pngs/<key>_glsl.png
materialx/renders/materialx.<key>.exr
```

It writes browser-viewable PNG copies/previews under `materialx/comparison/assets/` and uses `flip-evaluator` to generate perceptual FLIP diff maps alongside the reference and rendered images.

Useful options:

```bash
pixi run compare-materialx-renders --limit 5
pixi run compare-materialx-renders --key open_pbr_carpaint_Car_Paint
pixi run compare-materialx-renders --tonemap reinhard
pixi run compare-materialx-renders --output-dir /tmp/materialx-comparison
```
