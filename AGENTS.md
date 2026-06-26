# Agent Instructions

## General Workflow

- When a one-off task needs a Python package that is not already available, write a temporary PEP 723 script with a `# /// script` dependency block and run it with `uv run` so dependencies are installed in an ephemeral environment. Remove the temporary script after use.
- When rebasing a branch on another, first check if the current branch's changes can be cleanly replayed on top of the rebase target and do that preferentially. If there are conflicts, do not resolve them without discussing the conflicts with Anders.
- When implementing tests, always launch an adversarial review agent when the tests are done. The review should check whether the tests actually test useful behavior, whether they only encode current behavior, whether coverage has holes, and whether the tests make invalid assumptions about intent or environment.
- When Anders asks to launch one or more reviewers, make them adversarial, collate their findings into a single report, and do not start fixing anything before discussing the findings with Anders.

## MaterialX USD Suite

- The generated MaterialX USD render suite lives under `materialx/` in this repo. Do not write generated USD test assets into `/home/anders/code/MaterialX`.
- `materialx/README.md` contains the complete source material list and generation notes. Keep it in sync when changing the suite.
- `materialx/base.usda` is the shared layer. It owns `/World/Sphere`, `/World/DomeLight`, `/World/Camera`, `/Render/Settings`, `/Render/Settings/Product`, and `/Render/Settings/Product/Var`.
- `/World/Sphere` is a converted USD mesh version of the MaterialX `sphere.obj`; it must not reference the OBJ directly at render time.
- The base camera is authored for a 45 degree square FOV with `focalLength = 50`, `horizontalAperture = 41.421356`, and `verticalAperture = 41.421356`.
- The base dome light uses `materialx/assets/Lights/san_giuseppe_bridge.hdr` as `inputs:texture:file`. The MaterialX irradiance HDR is copied to `materialx/assets/Lights/irradiance/san_giuseppe_bridge.hdr` and preserved as `materialx:irradianceIBL:file`.
- Per-material test USDAs sublayer `@./base.usda@`, bind `/World/Sphere`, define materials under `/Looks`, and only over the base render product to set a unique output filename of the form `materialx.<material-test-name>.exr`.
- Keep referenced images and HDR files copied under `materialx/assets/`, with USDA asset paths rewritten to those local copies.

## Rendering MaterialX Tests

- Use the root Pixi tasks, not a separate shell script, to render MaterialX test USDAs.
- `pixi run render-materialx-one materialx/open_pbr_carpaint_Car_Paint.usda` renders one test layer.
- `pixi run render-materialx-all` renders all generated test layers except `base.usda`.
- `pixi run compare-materialx-renders` generates `materialx/comparison/index.html`, a dark HTML comparison page with MaterialX GLSL references, local USD render previews, and FLIP perceptual diff maps.
- These tasks call the OpenUSD Pixi workspace with `pixi run --manifest-path /home/anders/code/openusd-omniverse/pixi.toml usdrender ...`. Pixi does not consume another `pixi.toml` as a package dependency here; the manifest-path handoff is the intended setup.
- The OpenUSD `usdrender` task already supplies `--renderer Embree --complexity high`; this repo's wrapper adds `--disableCameraLight`, the USDA path, and `--outputRoot`.
- Default output root is `materialx/renders`.
- MaterialX comparison references are expected at `/home/anders/code/MaterialX/build/glsl-render-tests/flat-glsl-pngs/<key>_glsl.png`, and local renders are expected at `materialx/renders/materialx.<key>.exr`.
- The comparison generator uses the Linux-only Pixi dependencies `numpy`, `pillow`, and PyPI `flip-evaluator`.
- Useful environment overrides:
  - `MATERIALX_RENDER_DRY_RUN=1` prints commands without rendering.
  - `MATERIALX_RENDER_FAIL_FAST=1` stops the all-render task at the first failed render.
  - `MATERIALX_RENDER_OUTPUT_ROOT=/path/to/renders` changes the output directory.
  - `OPENUSD_PIXI=/path/to/openusd/pixi.toml` changes the OpenUSD Pixi manifest.
