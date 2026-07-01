# Agent Instructions

## General Workflow

- When a one-off task needs a Python package that is not already available, write a temporary PEP 723 script with a `# /// script` dependency block and run it with `uv run` so dependencies are installed in an ephemeral environment. Remove the temporary script after use.
- When rebasing a branch on another, first check if the current branch's changes can be cleanly replayed on top of the rebase target and do that preferentially. If there are conflicts, do not resolve them without discussing the conflicts with Anders.
- When implementing tests, always launch an adversarial review agent when the tests are done. The review should check whether the tests actually test useful behavior, whether they only encode current behavior, whether coverage has holes, and whether the tests make invalid assumptions about intent or environment.
- When Anders asks to launch one or more reviewers, make them adversarial, collate their findings into a single report, and do not start fixing anything before discussing the findings with Anders.

## Test Runner Overview

- Test execution is pytest-based. The plugin is loaded by `conftest.py` and implemented under `typhoon_tests/`.
- USDA render tests are collected from `.usda` files that have an ancestor `typhoon-suite.toml`. Unconfigured USDA directories require `--typhoon-collect-unconfigured`.
- Package mode is the default: `pixi run test` calls the installed `usdrender` from the `openusd-typhoon` conda package.
- Local source mode uses `--typhoon-provider /path/to/openusd-omniverse`; the provider path may be a checkout directory or a direct path to its `pixi.toml`.
- In package mode, the runner calls `usdrender --complexity high --renderer Embree`. In provider mode, it calls `pixi run --manifest-path <provider>/pixi.toml --clean-env usdrender` so inherited plugin and Python paths do not override the provider checkout while the OpenUSD Pixi task still provides the base renderer flags.

## Running Tests

Common commands:

```bash
pixi run test
pixi run test-materialx
pixi run test-local /home/anders/code/openusd-omniverse
pixi run pytest materialx --typhoon-provider /home/anders/code/openusd-omniverse
```

Filtering and discovery:

```bash
pixi run pytest --collect-only -q
pixi run pytest materialx/open_pbr_carpaint_Car_Paint.usda
pixi run pytest materialx -k carpaint
pixi run pytest materialx -k 'open_pbr and not glass'
pixi run pytest -m typhoon_usd
pixi run pytest materialx -x
pixi run pytest materialx --typhoon-dry-run -s
```

Pixi convenience tasks:

```bash
pixi run render-materialx-one materialx/open_pbr_carpaint_Car_Paint.usda
pixi run render-materialx-all
pixi run regenerate-html
pixi run regenerate-html --run _output/run-0003
pixi run regenerate-html --all
pixi run regenerate-comparisons
pixi run regenerate-comparisons --run _output/run-0003
pixi run build
pixi run view
```

## Outputs

- Every pytest render run writes into the next numbered `_output/run-NNNN` directory. The next run number is chosen by listing existing `_output/run-NNNN` directories and incrementing the highest number.
- `_output/` is gitignored. Do not commit generated run outputs.
- Each run directory contains EXR renders, copied reference image artifacts, FLIP diff EXRs, report viewer assets, `typhoon-report.json`, `run-summary.json`, and `index.html`.
- `_output/index.html` is updated after each run with run summaries and timestamps.
- `--typhoon-output-root=/path/to/output` changes the base directory that receives numbered `run-NNNN` directories.
- `--typhoon-dry-run` prints commands without rendering, but still allocates a numbered run directory and writes report/index files.
- `pixi run regenerate-html` regenerates the latest run HTML and top-level index from saved JSON without rerunning renders. Use `--run _output/run-0003`, `--all`, or `--output-root /path/to/output` for non-default cases.
- The HTML regeneration task reads `typhoon-report.json`, rewrites `index.html` and `run-summary.json`, refreshes the top-level `index.html`, and copies the EXR viewer assets into the run. It does not rerun `usdrender`, recompute FLIP, or modify rendered image artifacts.
- `pixi run regenerate-comparisons` recomputes comparison EXRs and FLIP metrics from existing render outputs without rerunning `usdrender`. It defaults to the latest run and accepts `--run _output/run-0003`, `--all`, or `--output-root /path/to/output`.
- `pixi run build` rebuilds the browser EXR decoder from `tools/exr_wasm/` and copies it into `typhoon_tests/static/`.
- Per-run HTML report columns are sortable and default to Mean FLIP descending. Status cells use `passed`, `no-ref`, `dry-run`, `failed-threshold`, `failed-render`, or another `failed-*` value. `passed` is green, `no-ref` uses the table background, `failed-threshold` is red, and failed statuses other than `failed-render` and `failed-threshold` are pink.

Expected run layout:

```text
_output/index.html
_output/run-0001/index.html
_output/run-0001/typhoon-report.json
_output/run-0001/run-summary.json
_output/run-0001/<rendered-products>.exr
_output/run-0001/reference/<key>.<ext>
_output/run-0001/flip/<key>.exr
_output/run-0001/assets/typhoon-exr-viewer.js
_output/run-0001/assets/typhoon_exr_wasm.wasm
```

## References And Thresholds

- References should live inside each suite, normally in `reference/`.
- MaterialX references live in `materialx/reference/<key>_glsl.png`; they were copied from `/home/anders/code/MaterialX/build/glsl-render-tests/flat-glsl-pngs`.
- `--typhoon-reference-dir` overrides the reference directory for all suites in a run.
- Missing references are allowed by default when the suite config uses `missing = "allow"`. Use `--typhoon-require-references` or suite-level `missing = "fail"` for strict reference coverage.
- If no FLIP threshold is configured, comparisons are reported but do not fail the test. Use `--typhoon-require-thresholds` in strict regression jobs.
- Thresholds can be configured suite-wide with `[comparison].default_flip_threshold`, per case with `[thresholds]`, or in an adjacent `<test>.typhoon.toml` file.

## Adding Or Updating Suites

To add a suite:

1. Create a suite directory, for example `lights/`.
2. Add `.usda` files that author predictable render product filenames.
3. Add references under `lights/reference/`.
4. Add `lights/typhoon-suite.toml`.
5. Run `pixi run pytest lights --collect-only -q`.
6. Run `pixi run pytest lights --typhoon-dry-run -s` and verify the generated commands.
7. Run `pixi run pytest lights` and inspect `_output/index.html`.
8. Add skips, xfails, and thresholds only where they describe intentional suite behavior.

Typical `typhoon-suite.toml`:

```toml
[suite]
name = "my-suite"

[render]
args = ["--disableCameraLight"]
output_pattern = "my-suite.{stem}.exr"

[reference]
dir = "reference"
pattern = "{stem}.png"
missing = "fail"

[comparison]
default_flip_threshold = 0.015

[skip]
known_broken_case = "blocked on missing texture support"

[xfail]
known_renderer_mismatch = "expected to fail until issue #123 is fixed"

[thresholds]
noisy_glass_case = 0.035
```

Per-test overrides go next to the USDA as `<test>.typhoon.toml`:

```toml
[render]
args = ["--disableCameraLight", "--some-renderer-setting", "value"]

[reference]
path = "reference/custom-reference.png"

[comparison]
flip_threshold = 0.025

[test]
xfail = "known mismatch in this scene"
```

## MaterialX USD Suite

- The generated MaterialX USD render suite lives under `materialx/` in this repo. Do not write generated USD test assets into `/home/anders/code/MaterialX`.
- `materialx/README.md` contains the complete source material list and generation notes. Keep it in sync when changing the suite.
- `materialx/typhoon-suite.toml` defines render args, output naming, reference lookup, skips, and comparison defaults.
- `materialx/base.usda` is the shared layer. It owns `/World/Sphere`, `/World/DomeLight`, `/World/Camera`, `/Render/Settings`, `/Render/Settings/Product`, and `/Render/Settings/Product/Var`.
- `/World/Sphere` is a converted USD mesh version of the MaterialX `sphere.obj`; it must not reference the OBJ directly at render time.
- The base camera is authored for a 45 degree square FOV with `focalLength = 50`, `horizontalAperture = 41.421356`, and `verticalAperture = 41.421356`.
- The base dome light uses `materialx/assets/Lights/san_giuseppe_bridge.hdr` as `inputs:texture:file`. The MaterialX irradiance HDR is copied to `materialx/assets/Lights/irradiance/san_giuseppe_bridge.hdr` and preserved as `materialx:irradianceIBL:file`.
- Per-material test USDAs sublayer `@./base.usda@`, bind `/World/Sphere`, define materials under `/Looks`, and only over the base render product to set a unique output filename of the form `materialx.<material-test-name>.exr`.
- Keep referenced images and HDR files copied under `materialx/assets/`, with USDA asset paths rewritten to those local copies.
- Keep GLSL reference PNGs under `materialx/reference/` with names matching `materialx/typhoon-suite.toml`.

## Legacy Scripts

- `materialx/render_usdas.py` and `materialx/generate_comparison_html.py` remain for compatibility with the old `materialx/renders` layout.
- The legacy comparison script does not consume numbered `_output/run-NNNN` directories unless its paths are overridden.
- New suites should use pytest collection and `typhoon-suite.toml`.
