# typhoon-tests

This repo contains USD render regression suites for Typhoon. Test execution is pytest-based: `.usda` files are collected as render tests, and each suite is configured by a `typhoon-suite.toml` file placed in the suite directory.

## Build, Run, And View

Build the browser EXR decoder after changing `tools/exr_wasm/` or when setting up a fresh checkout:

```bash
pixi run build
```

Run the non-render Python tests:

```bash
pixi run pytest tests -q
```

Run the usdlux render suite against a local OpenUSD/Typhoon checkout:

```bash
pixi run pytest usdlux --typhoon-provider /home/anders/code/openusd-omniverse
```

Render runs write numbered directories under `_output/`. Start the local report server before opening reports, because the EXR/WASM viewer uses browser `fetch()` and the expanded-row usdview button needs the server endpoint:

```bash
pixi run view --typhoon-provider /home/anders/code/openusd-omniverse
```

Without `--typhoon-provider`, the report still serves normally, but the usdview button opens the packaged `openusd-typhoon` environment instead of the local checkout.

Then open the top-level index or a specific run:

```text
http://localhost:8000/
http://localhost:8000/run-0009/index.html
```

Regenerate comparisons from existing rendered EXRs without rerendering scenes:

```bash
pixi run regenerate-comparisons --run _output/run-0009
```

Regenerate only the HTML reports and copied viewer assets from existing JSON:

```bash
pixi run regenerate-html --run _output/run-0009
```

## Other Common Runs

Run against the packaged `openusd-typhoon` conda package:

```bash
pixi run test
```

Run only the MaterialX suite:

```bash
pixi run test-materialx
```

Run the usdlux suite against a local OpenUSD/Typhoon checkout instead of the conda package:

```bash
pixi run test-local /home/anders/code/openusd-omniverse
pixi run pytest usdlux --typhoon-provider /home/anders/code/openusd-omniverse
```

`--typhoon-provider` accepts either an OpenUSD checkout directory or the checkout's `pixi.toml`. Provider runs execute `pixi run --manifest-path <provider>/pixi.toml --clean-env usdrender ...` so inherited plugin and Python paths do not override the provider checkout while the provider's `usdrender` Pixi task still supplies its renderer flags. When omitted, pytest calls the installed `usdrender` from the active Pixi environment.

## Selecting Tests

The runner uses normal pytest selection. The examples below use `usdlux` and a local OpenUSD/Typhoon checkout; omit `--typhoon-provider` to use the packaged `openusd-typhoon` renderer.

```bash
# List collected usdlux tests, including frame-expanded node IDs, without rendering.
pixi run pytest usdlux --collect-only -q

# Run one USDA file. For animated cases, this runs every configured frame.
pixi run pytest usdlux/iesLibPreview.usda --typhoon-provider /home/anders/code/openusd-omniverse

# Run one specific frame by pytest node ID.
pixi run pytest usdlux/iesLibPreview.usda::iesLibPreview__frame_0001 --typhoon-provider /home/anders/code/openusd-omniverse

# Run several specific frames by pytest node ID.
pixi run pytest usdlux/iesLibPreview.usda::iesLibPreview__frame_0001 usdlux/iesLibPreview.usda::iesLibPreview__frame_0002 --typhoon-provider /home/anders/code/openusd-omniverse

# Run one suite directory.
pixi run pytest usdlux --typhoon-provider /home/anders/code/openusd-omniverse

# Filter by pytest expression against collected test names.
pixi run pytest usdlux -k iesLibPreview --typhoon-provider /home/anders/code/openusd-omniverse
pixi run pytest usdlux -k 'iesLibPreview and (frame_0001 or frame_0002)' --typhoon-provider /home/anders/code/openusd-omniverse

# Run only USDA render tests, excluding Python unit tests.
pixi run pytest -m typhoon_usd

# Stop after the first failure.
pixi run pytest usdlux -x --typhoon-provider /home/anders/code/openusd-omniverse

# Print dry-run render commands to the terminal.
pixi run pytest usdlux --typhoon-dry-run -s --typhoon-provider /home/anders/code/openusd-omniverse
```

Pixi convenience tasks are available for the current MaterialX suite and report maintenance:

```bash
pixi run build
pixi run view
pixi run render-materialx-one materialx/open_pbr_carpaint_Car_Paint.usda
pixi run render-materialx-all
pixi run regenerate-html
pixi run regenerate-html --run _output/run-0003
pixi run regenerate-html --all
pixi run regenerate-comparisons
pixi run regenerate-comparisons --run _output/run-0003
```

## Output Runs

Each pytest render run writes to the next numbered directory under `_output/`:

```text
_output/run-0001
_output/run-0002
_output/run-0003
```

The next run number is chosen by listing existing `run-NNNN` directories and incrementing the highest number. `_output/` is gitignored.

A run directory contains the renderer outputs, comparison artifacts, and report data:

```text
_output/run-0001/index.html
_output/run-0001/typhoon-report.json
_output/run-0001/run-summary.json
_output/run-0001/<rendered-products>.exr
_output/run-0001/reference/<key>.<ext>
_output/run-0001/flip/<key>.exr
_output/run-0001/assets/typhoon-exr-viewer.js
_output/run-0001/assets/typhoon_exr_wasm.wasm
```

The top-level run index is updated after each run:

```text
_output/index.html
```

`_output/index.html` links to every run report and records when each run started, how many tests rendered, how many comparisons ran, how many references were missing, and how many failures were recorded.

Use another output base when you do not want to write into the repo-local `_output/` directory:

```bash
pixi run pytest materialx --typhoon-output-root /tmp/typhoon-output
```

`--typhoon-dry-run` does not invoke `usdrender`, but it still allocates a numbered run directory and writes report/index files containing the commands that would have run.

Regenerate report HTML from existing JSON without rerunning renders:

```bash
# Regenerate the latest run and the top-level run index.
pixi run regenerate-html

# Regenerate a specific run.
pixi run regenerate-html --run _output/run-0003

# Regenerate every run under _output.
pixi run regenerate-html --all

# Use a non-default output base.
pixi run regenerate-html --output-root /tmp/typhoon-output --run run-0003
```

The `regenerate-html` task reads `typhoon-report.json`, rewrites that run's `index.html` and `run-summary.json`, refreshes the top-level `_output/index.html`, and copies the EXR viewer assets into the run. It does not rerun `usdrender`, recompute FLIP, or modify rendered image artifacts.

To recompute comparison EXRs and FLIP metrics from existing render outputs without rerunning `usdrender`, use `pixi run regenerate-comparisons`. It defaults to the latest run and also accepts `--run`, `--all`, and `--output-root`.

The per-run report decodes EXRs in the browser with `assets/typhoon_exr_wasm.wasm`. Use `pixi run build` after changing the Rust decoder under `tools/exr_wasm/`, and view reports through `pixi run view` rather than `file://`. Expanded result rows include an `Open in usdview` button that launches `pixi run usdview --renderer Embree --disableCameraLight --camera <camera> --complexity high --cf <frame> <usd>` through the local view server. Pass `--typhoon-provider /path/to/openusd-omniverse` to `pixi run view` to launch usdview from a local provider checkout.

The per-run HTML report has sortable columns and defaults to Mean FLIP descending. Status values are:

- `passed`: render and comparison completed successfully.
- `no-ref`: render completed, but no reference image was available and missing references were allowed.
- `dry-run`: command generation completed without invoking the renderer.
- `failed-threshold`: comparison completed, but mean FLIP exceeded the configured threshold.
- `failed-render`: the renderer exited with an error.
- `failed-*`: other setup, output, reference, threshold, or comparison failures.

Status cells are color-coded in the report: `passed` is green, `no-ref` uses the table background, `failed-threshold` is red, and failed statuses other than `failed-render` and `failed-threshold` are pink.

## References And Comparisons

References live inside each suite, normally in a `reference/` subdirectory. The MaterialX suite uses:

```text
materialx/reference/<key>_glsl.png
```

`materialx/typhoon-suite.toml` maps each USDA stem to its reference image:

```toml
[reference]
dir = "reference"
pattern = "{stem}_glsl.png"
missing = "allow"
```

When a reference exists, pytest computes a mean FLIP value from the floating-point image data, copies the reference image into the current run directory, and writes a browser-viewable FLIP diff EXR. The per-run report loads the copied reference image, rendered EXR, and FLIP EXR directly in the browser. If no threshold is configured, the FLIP value is reported but does not fail the test.

Useful strictness options:

```bash
# Fail if a configured reference is missing.
pixi run pytest materialx --typhoon-require-references

# Fail compared tests that do not have a FLIP threshold configured.
pixi run pytest materialx --typhoon-require-thresholds

# Temporarily use references from another directory.
pixi run pytest materialx --typhoon-reference-dir /path/to/reference-pngs
```

## Suite Configuration

A suite directory should contain `typhoon-suite.toml`. Without that file, `.usda` files are not collected by default. For ad hoc directories, opt in explicitly:

```bash
pixi run pytest /path/to/usd-directory --typhoon-collect-unconfigured
```

Typical suite config:

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

[frames]
animated_case = "1:24"
```

Available config concepts:

- `[suite].name`: suite label used in reports.
- `[render].args`: extra args appended to `usdrender` for every case in the suite.
- `[render].output_pattern`: expected render product path relative to the run directory. It can use `{stem}`, `{name}`, `{suffix}`, `{suite}`, and `{frame}` for frame-expanded cases.
- `[reference].dir`: reference directory, relative to the suite directory unless absolute.
- `[reference].pattern`: reference filename pattern using the same fields as `output_pattern`.
- `[reference].missing`: `allow` for render-only smoke behavior or `fail` for strict suites.
- `[comparison].default_flip_threshold`: default mean FLIP threshold for every compared case.
- `[frames]`: optional frame specs keyed by relative path, filename, or stem. A value like `"1:10"` collects frames 1 through 10, and `"1:10x2"` uses a stride of 2.
- `[skip]`, `[xfail]`, `[thresholds]`: keyed by relative path, filename, or stem.

For one-off per-test overrides, put a sibling `<test>.typhoon.toml` next to the USDA:

```toml
[render]
args = ["--disableCameraLight", "--some-renderer-setting", "value"]

[reference]
path = "reference/custom-reference.png"

[comparison]
flip_threshold = 0.025

[test]
xfail = "known mismatch in this scene"

[frames]
range = "1001:1010"
```

## Adding A New Suite

1. Create a new directory for the suite, for example `lights/`.
2. Add one or more `.usda` files. Each file should author render products that write predictable filenames.
3. Add references under `lights/reference/`. Prefer committing stable PNG references with the suite.
4. Add `lights/typhoon-suite.toml` with suite name, render args, output pattern, reference pattern, and comparison settings.
5. Run a dry-run collection check:

```bash
pixi run pytest lights --collect-only -q
pixi run pytest lights --typhoon-dry-run -s
```

6. Run the suite and inspect the generated report:

```bash
pixi run pytest lights
open _output/index.html
```

7. Add per-test skips, xfails, or threshold overrides only for cases that need them.
8. Keep references deterministic. If a reference changes intentionally, update the image in the suite's `reference/` directory and mention why in the change.

## MaterialX USD Suite

The current generated suite lives under `materialx/` and is generated from `/home/anders/code/MaterialX/resources/Materials/TestSuite/_options.mtlx`. It contains 143 per-material USDA test layers plus the shared `materialx/base.usda` layer.

`materialx/base.usda` provides the shared scene:

- `/World/Sphere`: USD mesh converted from the MaterialX `sphere.obj`.
- `/World/DomeLight`: latlong IBL using `materialx/assets/Lights/san_giuseppe_bridge.hdr`.
- `/World/Camera`: 45 degree square FOV, `focalLength = 50`, `horizontalAperture = 41.421356`, `verticalAperture = 41.421356`.
- `/Render/Settings`, `/Render/Settings/Product`, and `/Render/Settings/Product/Var`.

Each material test USDA sublayers `base.usda`, binds `/World/Sphere`, defines its material under `/Looks`, and sets a unique render product filename:

```text
materialx.<material-test-name>.exr
```

Referenced images and HDR maps are copied under `materialx/assets/`, and asset paths in the USDA files point to those local copies. GLSL reference PNGs used for comparison are copied under `materialx/reference/`.

## Legacy Scripts

The legacy scripts remain for compatibility with the old `materialx/renders` workflow:

```bash
pixi run compare-materialx-renders
```

The legacy comparison generator does not consume numbered `_output/run-NNNN` directories unless its paths are overridden. New suites should use pytest collection and `typhoon-suite.toml`.
