# MaterialX in USD Specification Test Completeness

This report statically compares every `ND_*` definition in the MaterialX in USD specification against every USD fixture in `material-fidelity`, including connected and directly authored inputs.

## Executive summary

The suite is deep in a few areas, but its overall node coverage is incomplete:

- 174 MaterialX node families are described by the specification.
- 165 have concrete `ND_*` definitions, comprising 680 typed NodeDefs.
- Only 222 of those 680 exact NodeDefs appear in the tests: 32.6%.
- At the family level:

  - 54 families are completely untested.
  - 66 have only some typed overloads tested.
  - 45 have every specified overload represented.

These are optimistic numbers: any occurrence counted, including incidental use in a showcase graph. Direct, targeted coverage is lower.

## Completely untested nodes

### PBR closures and constructors

This is the largest and highest-priority gap:

- `absorption_vdf`
- `anisotropic_vdf`
- `chiang_hair_roughness`
- `chiang_hair_absorption_from_color`
- `deon_hair_absorption_from_melanin`
- `conical_edf`
- `generalized_schlick_edf`
- `measured_edf`
- `glossiness_anisotropy`
- `roughness_anisotropy`
- `roughness_dual`
- `volume`
- `light`
- `layer`

Direct fixtures now cover the six BSDFs removed from this list and connect them through `ND_surface`. The remaining lower-level closure NodeDefs still lack direct coverage, and the independent `opacity` and `thin_walled` behavior of `ND_surface` is not yet tested.

### Texture, convolution, and shader nodes

- `latlongimage`
- `triplanarprojection` — all six typed NodeDefs absent
- `blur` — all six typed NodeDefs absent
- `displacement`
- `surface_unlit`

`triplanarprojection` is particularly significant: none of its three texture axes, per-axis layers, position/normal blending, `upaxis`, filtering, animation controls, or fallback behavior is exercised.

### Compositing

- `disjointover`
- `in`
- `inside`
- `mask`
- `matte`
- `out`
- `outside`
- `over`
- `plus`
- `premult`

The suite tests `burn`, `difference`, `dodge`, `minus`, `mix`, `overlay`, `screen`, and `unpremult`, but most are tested only through one of three output types.

### Geometry, adjustment, and NPR

- `geomcolor`
- `geompropvalue`
- `geompropvalueuniform`
- `hsvadjust`
- `facingratio`
- `gooch_shade`

## Poorly tested nodes

| Area | Current coverage | Important omissions |
|---|---:|---|
| `image` | 3/6 overloads | No color4, vector2, or vector4. No meaningful layer test, unreadable/missing file fallback, channel expansion/truncation, UDIM/UVTILE/interface/host substitution, or animated sequence boundary behavior. |
| `tiledimage` | 3/6 | No color4/vector2/vector4. Only a basic targeted tiling probe; no strong real-world-size, fallback, filtering, or sequence coverage. |
| `hextiledimage` | 1/2 | Color3 is tested unusually well, but color4 is wholly absent. |
| `heighttonormal` | 1/1 | `in` and `scale` are exercised, but no explicit custom `texcoord`. |
| `switch` | 2/16 | Only color3 output, with float and integer selectors. Inputs 6–10 are never authored; all float, color4, vector, and matrix forms are absent. |
| `ifequal` | 2/30 | Almost all result and comparison-type combinations are absent. |
| `ifgreater` | 4/20 | Sparse overload coverage. |
| `ifgreatereq` | 2/20 | Sparse overload coverage. |
| `mix` | 2/17 | Most value, closure, displacement, and volume variants absent. |
| `add` | 4/19 | Most vector/color/matrix and mixed float variants absent. |
| `multiply` | 6/19 | Better than `add`, but most typed variants still absent. |
| `divide` | 2/13 | Zero-divisor case exists, but only for a narrow subset of types. |
| `subtract` | 4/16 | Most overloads absent. |
| `constant` | 5/12 | Float, color4, vector4, matrices, string, and filename coverage is missing. |
| `range`, `remap`, `smoothstep` | 3/11 each | Good degenerate float/vector4 cases, but most color/vector and float-amount overloads absent. |
| noise families | usually 1/7 | Excellent parameter/default/large-coordinate tests for the principal float or color form, but most output types remain absent. |
| `ramp4` | 1/6 | Only one result type. |
| `ramplr`, `ramptb`, `splitlr`, `splittb` | 2/6 each | Mostly float/color3; other vector/color forms absent. |
| `normalize` | 1/3 | Only vector3; color3/vector2 absent. Zero-vector fixture is also a declared validation failure. |
| transcendental math | commonly 1/4 | `sin`, `cos`, `tan`, `exp`, `sqrt`, and `atan2` omit most vector forms. |
| adjustment nodes | commonly 1/2 or 1/11 | Usually color3 or float only; color4/vector and float-amount variants are sparse. |

### Specific texture-edge gaps

The `image` tests cover all four U/V address modes and `closest`/`linear`, which is good. They do not cover:

- `cubic` filtering.
- Different U and V address modes in the same test.
- Named layers or absent layers.
- Failed URI resolution and the `default` result.
- Output channel truncation and padding rules.
- `<UDIM>`, `<UVTILE>`, interface-token, host-attribute, `{frame}`, or padded-frame substitution.
- `frameoffset` with a real sequence.
- `clamp`, `periodic`, and `mirror` as `frameendaction`.

One fixture authors `framerange = "1,1"`, whereas the specification defines the syntax as `"minframe-maxframe"`, such as `"10-99"`. That fixture therefore does not convincingly validate conforming frame-range behavior.

### Math edge cases still worth adding

Existing degenerate cases are a strong part of the suite: inverted bounds, invalid inverse-trig inputs, nonpositive log inputs, negative square roots, zero divisors, zero normalization, and negative-base fractional powers all appear.

Remaining useful cases include:

- `refract`: total internal reflection, grazing incidence, `ior` equal to 1, zero/negative IOR policy, non-normalized inputs.
- `reflect`: non-normalized and zero normals.
- Matrix inverse: singular and nearly singular matrices.
- `atan2`: both inputs zero and axis/quadrant boundaries.
- `modulo`: negative operands and zero divisor across types.
- `power`/`safepower`: zero-to-zero, zero with negative exponent, infinities/NaNs if the implementation exposes them.
- Vector/color versions of the already-tested scalar degeneracies.

## Nodes with no specification NodeDef

Nine specification headings explicitly lack generated NodeDefs:

- `dielectric_bsdf`
- `conductor_bsdf`
- `generalized_schlick_bsdf`
- `flake2d`
- `flake3d`
- `mincomponent`
- `maxcomponent`
- `dot`
- `time`

`dot` and `time` have tests using invented/library IDs such as `ND_dot_color3` and `ND_time_float`, even though this specification says no corresponding NodeDef can be generated. The remaining seven have no direct tests.

This should be resolved explicitly: either those tests target a future/upstream MaterialX library rather than this specification, or the specification and test-suite versions are out of sync.

## Test-suite quality observations

The tests are golden-image comparisons. The suite renders each fixture and compares it with a reference image. This is useful for renderer fidelity but has limitations:

- It does not directly assert NodeDef identity, input types, uniformity, defaults, allowed tokens, output types, or validation behavior.
- A node can be present but visually inert, especially in large surface/showcase graphs.
- The generator already acknowledges this risk and overrides several upstream fixtures whose named controls were otherwise inert.
- Some behavior is deliberately excluded because a beauty render cannot observe it, including glTF occlusion and dispersion.
- There are 29 accepted source-validation failures, including `switch` and zero-vector normalization cases.

## Recommended priority

1. Direct PBR BSDF/EDF/VDF and `surface`/`volume` constructor tests.
2. `triplanarprojection`, `blur`, `latlongimage`, and image fallback/layer/sequence tests.
3. Conditional and operator overload coverage, especially `switch`, `ifequal`, `mix`, and matrix variants.
4. Missing geometry/property and NPR nodes.
5. Structural conformance tests alongside image comparisons, so USD typing/default/token errors cannot pass merely because the render looks plausible.

## Methodology and interpretation

- Specification inputs were extracted from `MaterialX.StandardNodes.md`, `MaterialX.PBRSpec.md`, and `MaterialX.NPRSpec.md`.
- Test usage was extracted from all `.usda` files below `material-fidelity` by matching exact `info:id` values and both authored and connected `inputs:*` properties.
- Exact typed NodeDef coverage and logical node-family coverage were calculated separately.
- Any occurrence was counted, even if incidental to a larger graph. Consequently, the reported coverage is an upper bound on meaningful targeted coverage.
- This was a static completeness review; it did not execute the render suite or assess current pass/fail results.
