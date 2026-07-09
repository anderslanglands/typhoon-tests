# MaterialX in USD Specification Test Completeness

This report statically compares every current `ND_*` definition from the checked-out MaterialX `stdlib`, `pbrlib`, and `nprlib` libraries against every USD fixture in `material-fidelity`, including connected and directly authored inputs.

## Executive summary

The suite is deep in a few areas, but its overall node coverage is incomplete:

- 178 MaterialX node families have concrete `ND_*` definitions in the current checked-out libraries.
- Those families comprise 748 typed NodeDefs.
- 379 of those 748 exact NodeDefs appear in the tests: 50.7%.
- At the family level:

  - 25 families are completely untested.
  - 57 have only some typed overloads tested.
  - 96 have every specified overload represented.

These are optimistic numbers: any occurrence counted, including incidental use in a showcase graph. Direct, targeted coverage is lower.

## Completely untested nodes

### PBR closures and constructors

This is the largest and highest-priority gap:

- `conductor_bsdf`
- `generalized_schlick_bsdf`
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

Direct fixtures now cover the core diffuse/transmission/sheen/hair BSDFs, `dielectric_bsdf`, `absorption_vdf`, `anisotropic_vdf`, and both `layer` NodeDefs. The remaining lower-level closure and constructor NodeDefs still lack direct coverage, and the independent `opacity` and `thin_walled` behavior of `ND_surface` is not yet tested.

### Texture, convolution, shader, and material nodes

- `triplanarprojection` — all six typed NodeDefs absent
- `displacement` — both typed NodeDefs absent
- `surface_unlit`
- `surfacematerial`
- `volumematerial`

`triplanarprojection` is particularly significant: none of its three texture axes, per-axis layers, position/normal blending, `upaxis`, filtering, animation controls, or fallback behavior is exercised.
`latlongimage` now has a targeted fixture that maps the suite dome HDR onto the plane through the `viewdir` input.
`blur` now has direct fixtures for all six typed variants, using the image-format PNG checker/grid texture on the plane.

### Geometry, adjustment, NPR, and helper nodes

- `hsvadjust`
- `facingratio`
- `gooch_shade`
- `flake2d`
- `flake3d`
- `mincomponent`
- `maxcomponent`

`geomcolor`, `geompropvalue`, and `geompropvalueuniform` now have direct plane fixtures for every typed overload, using typed custom primvars instead of USD display-color primvars. The uniform string fixture reads a scalar `primvars:testString = "clamp"` value and uses it as the image `uaddressmode` while sampling plane UVs offset by 0.5, and the filename fixture reads a scalar asset primvar to drive the image file input.

## Poorly tested nodes

| Area | Current coverage | Important omissions |
|---|---:|---|
| `image` | 6/6 overloads | All typed outputs now appear, and there is a targeted `<UDIM>` fixture. There is still no meaningful layer test, unreadable/missing file fallback, channel expansion/truncation, UVTILE/interface/host substitution, or animated sequence boundary behavior. |
| `tiledimage` | 3/6 | No color4/vector2/vector4. Only a basic targeted tiling probe; no strong real-world-size, fallback, filtering, or sequence coverage. |
| `hextiledimage` | 1/2 | Color3 is tested unusually well, but color4 is wholly absent. |
| `heighttonormal` | 1/1 | `in` and `scale` are exercised, but no explicit custom `texcoord`. |
| `switch` | 2/16 | Only color3 output, with float and integer selectors. Inputs 6–10 are never authored; all float, color4, vector, and matrix forms are absent. |
| `ifequal` | 2/30 | Almost all result and comparison-type combinations are absent. |
| `ifgreater` | 4/20 | Sparse overload coverage. |
| `ifgreatereq` | 2/20 | Sparse overload coverage. |
| `mix` | 12/17 | All float/color/vector value variants and the surfaceshader variant now have direct EXR fixtures; the remaining gaps are displacementshader, volumeshader, BSDF, EDF, and VDF. |
| `range`, `remap`, `smoothstep` | 3/11 each | Good degenerate float/vector4 cases, but most color/vector and float-amount overloads absent. |
| noise families | usually 1/7 | Excellent parameter/default/large-coordinate tests for the principal float or color form, but most output types remain absent. |
| `ramp4` | 1/6 | Only one result type. |
| `ramplr`, `ramptb`, `splitlr`, `splittb` | 2/6 each | Mostly float/color3; other vector/color forms absent. |
| `normalize` | 1/3 | Only vector3; color3/vector2 absent. Zero-vector fixture is also a declared validation failure. |
| transcendental math | commonly 1/4 | `sin`, `cos`, `tan`, `exp`, `sqrt`, and `atan2` omit most vector forms. |
| adjustment nodes | commonly 1/2 or 1/11 | Usually color3 or float only; color4/vector and float-amount variants are sparse. |
| `dot` | 5/16 | Current local MaterialX libraries define typed `dot` NodeDefs; only float, integer, boolean, color3, and vector3 appear in fixtures. |

The previously missing compositing families now all have direct fixtures. `premult`, `unpremult`, `plus`, `minus`, `difference`, `burn`, `dodge`, `screen`, `overlay`, `disjointover`, `in`, `mask`, `matte`, `out`, `over`, `inside`, and `outside` are fully covered at the NodeDef level. `mix` has direct EXR fixtures for every float/color/vector value variant and the surfaceshader variant, but remains partial because displacement, volume, and closure variants need separate structural fixtures.

The basic arithmetic and constant families are now fully covered at the NodeDef level: `add` 19/19, `multiply` 19/19, `divide` 13/13, `subtract` 16/16, and `constant` 12/12. These fixtures are direct tests rather than incidental graph usage: value-output variants use minimal literal-input graphs, vector4/color4/matrix outputs are packed into RGB so every component contributes to the rendered pixels, and each layer has `customLayerData.doc` explaining the expected value. The generic `constant` fixture has been renamed to the typed `constant_color3`, and `constant_string` now uses the same shifted-grid image sampling graph as `geompropvalueuniform_string`, with `ND_constant_string` outputting the literal string `"clamp"` into `ND_image_color3.uaddressmode`. The `divide_degenerate_zero` fixture remains a targeted UV-driven zero-divisor case rather than a flat numeric color test.

### Specific texture-edge gaps

The `image` tests cover all four U/V address modes, `closest`/`linear`, and a targeted `<UDIM>` substitution case with standard `1001`, `1002`, `1011`, and `1012` tile assets, which is good. They do not cover:

- `cubic` filtering.
- Different U and V address modes in the same test.
- Named layers or absent layers.
- Failed URI resolution and the `default` result.
- Output channel truncation and padding rules.
- `<UVTILE>`, interface-token, host-attribute, `{frame}`, or padded-frame substitution.
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

## Former No-NodeDef Notes

The previous pass listed several headings as lacking generated NodeDefs. In the current checked-out MaterialX libraries, those families now have concrete `ND_*` definitions:

- `dielectric_bsdf` is covered directly.
- `dot` is partially covered.
- `time` is covered directly.
- `conductor_bsdf`, `generalized_schlick_bsdf`, `flake2d`, `flake3d`, `mincomponent`, and `maxcomponent` are still completely untested.

This section is retained to make that library/spec drift explicit. The current static counts above are based on the checked-out libraries, not the older no-NodeDef classification.

## Test-suite quality observations

The tests are golden-image comparisons. The suite renders each fixture and compares it with a reference image. This is useful for renderer fidelity but has limitations:

- It does not directly assert NodeDef identity, input types, uniformity, defaults, allowed tokens, output types, or validation behavior.
- A node can be present but visually inert, especially in large surface/showcase graphs.
- The generator already acknowledges this risk and overrides several upstream fixtures whose named controls were otherwise inert.
- Some behavior is deliberately excluded because a beauty render cannot observe it, including glTF occlusion and dispersion.
- There are 29 accepted source-validation failures, including `switch` and zero-vector normalization cases.

## Recommended priority

1. Remaining PBR EDF/helper constructors, `volume`/`light`, and `surface`/`volume` constructor behavior.
2. `triplanarprojection` and image fallback/layer/UVTILE/sequence tests.
3. Conditional and remaining non-arithmetic operator overload coverage, especially `switch`, `ifequal`, `mix`, and matrix-specific helpers.
4. Remaining component helper, adjustment, and NPR nodes.
5. Structural conformance tests alongside image comparisons, so USD typing/default/token errors cannot pass merely because the render looks plausible.

## Methodology and interpretation

- NodeDef inputs were extracted from the current checked-out `stdlib_defs.mtlx`, `pbrlib_defs.mtlx`, and `nprlib_defs.mtlx` files.
- Test usage was extracted from all `.usda` files below `material-fidelity` by matching exact `info:id` values and both authored and connected `inputs:*` properties.
- Exact typed NodeDef coverage and logical node-family coverage were calculated separately.
- Any occurrence was counted, even if incidental to a larger graph. Consequently, the reported coverage is an upper bound on meaningful targeted coverage.
- This was a static completeness review; it did not execute the render suite or assess current pass/fail results.
