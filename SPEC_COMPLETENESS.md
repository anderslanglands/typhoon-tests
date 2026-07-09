# MaterialX in USD Specification Test Completeness

This report statically compares current `ND_*` definitions from the checked-out MaterialX `stdlib`, `pbrlib`, and `nprlib` libraries against every USD fixture in `material-fidelity`, restricted to node families present in the AOUSD MaterialX specification under `~/code/materials-node-definitions/specification`.

## Summary

- AOUSD MaterialX node families: 174
- Typed NodeDefs in those families: 744
- Covered NodeDefs: 721/744 (96.9%)
- Uncovered families: 16
- Partial families: 2
- Complete families: 156
- Omitted local MaterialX-library families absent from the AOUSD MaterialX spec: `ramp`, `ramp_gradient`, `surfacematerial`, `volumematerial`
- Fixture-referenced omitted local families: `ramp`, `ramp_gradient`
- USD fixtures also reference 2 `ND_*` ids outside this MaterialX library set: `ND_open_pbr_surface_surfaceshader`, `ND_standard_surface_surfaceshader`
- Closure helper families can still be over-counted when a NodeDef appears only as support inside another fixture; the static scan counts exact NodeDef ids but does not prove every helper-family use has a dedicated behavioral assertion.

Status is based on exact NodeDef ids. `uncovered` means no variants in that family appear in the fixtures, `partial` means some but not all variants appear, and `complete` means every variant appears at least once. This is a static coverage view, not a reference-readiness audit; newly added fixtures can be counted before their reference renders exist. It is also an upper bound on meaningful coverage because incidental helper nodes in larger graphs count as covered.

## Coverage Table

| Node family | Status | Coverage | Uncovered type variants |
|---|---|---:|---|
| `chiang_hair_absorption_from_color` | uncovered | 0/1 | `ND_chiang_hair_absorption_from_color` |
| `chiang_hair_roughness` | uncovered | 0/1 | `ND_chiang_hair_roughness` |
| `conductor_bsdf` | uncovered | 0/1 | `ND_conductor_bsdf` |
| `conical_edf` | uncovered | 0/1 | `ND_conical_edf` |
| `deon_hair_absorption_from_melanin` | uncovered | 0/1 | `ND_deon_hair_absorption_from_melanin` |
| `flake2d` | uncovered | 0/1 | `ND_flake2d` |
| `flake3d` | uncovered | 0/1 | `ND_flake3d` |
| `generalized_schlick_bsdf` | uncovered | 0/1 | `ND_generalized_schlick_bsdf` |
| `generalized_schlick_edf` | uncovered | 0/1 | `ND_generalized_schlick_edf` |
| `glossiness_anisotropy` | uncovered | 0/1 | `ND_glossiness_anisotropy` |
| `gooch_shade` | uncovered | 0/1 | `ND_gooch_shade` |
| `measured_edf` | uncovered | 0/1 | `ND_measured_edf` |
| `roughness_anisotropy` | uncovered | 0/1 | `ND_roughness_anisotropy` |
| `roughness_dual` | uncovered | 0/1 | `ND_roughness_dual` |
| `surface_unlit` | uncovered | 0/1 | `ND_surface_unlit` |
| `triplanarprojection` | uncovered | 0/6 | `color3`, `color4`, `float`, `vector2`, `vector3`, `vector4` |
| `displacement` | partial | 1/2 | `vector3` |
| `hextiledimage` | partial | 1/2 | `color4` |
| `absorption_vdf` | complete | 1/1 | - |
| `absval` | complete | 6/6 | - |
| `acos` | complete | 4/4 | - |
| `add` | complete | 19/19 | - |
| `and` | complete | 1/1 | - |
| `anisotropic_vdf` | complete | 1/1 | - |
| `artistic_ior` | complete | 1/1 | - |
| `asin` | complete | 4/4 | - |
| `atan2` | complete | 4/4 | - |
| `bitangent` | complete | 1/1 | - |
| `blackbody` | complete | 1/1 | - |
| `blur` | complete | 6/6 | - |
| `bump` | complete | 1/1 | - |
| `burley_diffuse_bsdf` | complete | 1/1 | - |
| `burn` | complete | 3/3 | - |
| `ceil` | complete | 7/7 | - |
| `cellnoise2d` | complete | 1/1 | - |
| `cellnoise3d` | complete | 1/1 | - |
| `checkerboard` | complete | 1/1 | - |
| `chiang_hair_bsdf` | complete | 1/1 | - |
| `circle` | complete | 1/1 | - |
| `clamp` | complete | 11/11 | - |
| `cloverleaf` | complete | 1/1 | - |
| `colorcorrect` | complete | 2/2 | - |
| `combine2` | complete | 4/4 | - |
| `combine3` | complete | 2/2 | - |
| `combine4` | complete | 2/2 | - |
| `constant` | complete | 12/12 | - |
| `contrast` | complete | 11/11 | - |
| `convert` | complete | 47/47 | - |
| `cos` | complete | 4/4 | - |
| `creatematrix` | complete | 3/3 | - |
| `crosshatch` | complete | 1/1 | - |
| `crossproduct` | complete | 1/1 | - |
| `determinant` | complete | 2/2 | - |
| `dielectric_bsdf` | complete | 1/1 | - |
| `difference` | complete | 3/3 | - |
| `disjointover` | complete | 1/1 | - |
| `distance` | complete | 3/3 | - |
| `divide` | complete | 13/13 | - |
| `dodge` | complete | 3/3 | - |
| `dot` | complete | 16/16 | - |
| `dotproduct` | complete | 3/3 | - |
| `exp` | complete | 4/4 | - |
| `extract` | complete | 7/7 | - |
| `facingratio` | complete | 1/1 | - |
| `floor` | complete | 7/7 | - |
| `fract` | complete | 6/6 | - |
| `fractal2d` | complete | 11/11 | - |
| `fractal3d` | complete | 11/11 | - |
| `frame` | complete | 1/1 | - |
| `geomcolor` | complete | 3/3 | - |
| `geompropvalue` | complete | 8/8 | - |
| `geompropvalueuniform` | complete | 2/2 | - |
| `grid` | complete | 1/1 | - |
| `heighttonormal` | complete | 1/1 | - |
| `hexagon` | complete | 1/1 | - |
| `hextilednormalmap` | complete | 1/1 | - |
| `hsvadjust` | complete | 2/2 | - |
| `hsvtorgb` | complete | 2/2 | - |
| `ifequal` | complete | 30/30 | - |
| `ifgreater` | complete | 20/20 | - |
| `ifgreatereq` | complete | 20/20 | - |
| `image` | complete | 6/6 | - |
| `in` | complete | 1/1 | - |
| `inside` | complete | 3/3 | - |
| `invert` | complete | 11/11 | - |
| `invertmatrix` | complete | 2/2 | - |
| `latlongimage` | complete | 1/1 | - |
| `layer` | complete | 2/2 | - |
| `luminance` | complete | 2/2 | - |
| `light` | complete | 1/1 | - |
| `line` | complete | 1/1 | - |
| `ln` | complete | 4/4 | - |
| `magnitude` | complete | 3/3 | - |
| `mask` | complete | 1/1 | - |
| `matte` | complete | 1/1 | - |
| `max` | complete | 11/11 | - |
| `maxcomponent` | complete | 5/5 | - |
| `min` | complete | 11/11 | - |
| `mincomponent` | complete | 5/5 | - |
| `minus` | complete | 3/3 | - |
| `mix` | complete | 17/17 | - |
| `modulo` | complete | 11/11 | - |
| `multiply` | complete | 19/19 | - |
| `normal` | complete | 1/1 | - |
| `normalize` | complete | 3/3 | - |
| `noise2d` | complete | 11/11 | - |
| `noise3d` | complete | 11/11 | - |
| `normalmap` | complete | 2/2 | - |
| `not` | complete | 1/1 | - |
| `or` | complete | 1/1 | - |
| `oren_nayar_diffuse_bsdf` | complete | 1/1 | - |
| `out` | complete | 1/1 | - |
| `outside` | complete | 3/3 | - |
| `over` | complete | 1/1 | - |
| `overlay` | complete | 3/3 | - |
| `place2d` | complete | 1/1 | - |
| `plus` | complete | 3/3 | - |
| `position` | complete | 1/1 | - |
| `power` | complete | 11/11 | - |
| `premult` | complete | 1/1 | - |
| `ramp4` | complete | 6/6 | - |
| `ramplr` | complete | 6/6 | - |
| `ramptb` | complete | 6/6 | - |
| `randomcolor` | complete | 2/2 | - |
| `randomfloat` | complete | 2/2 | - |
| `range` | complete | 11/11 | - |
| `reflect` | complete | 1/1 | - |
| `refract` | complete | 1/1 | - |
| `remap` | complete | 11/11 | - |
| `rgbtohsv` | complete | 2/2 | - |
| `rotate2d` | complete | 1/1 | - |
| `rotate3d` | complete | 1/1 | - |
| `round` | complete | 7/7 | - |
| `safepower` | complete | 11/11 | - |
| `saturate` | complete | 2/2 | - |
| `screen` | complete | 3/3 | - |
| `separate2` | complete | 1/1 | - |
| `separate3` | complete | 2/2 | - |
| `separate4` | complete | 2/2 | - |
| `sheen_bsdf` | complete | 1/1 | - |
| `sign` | complete | 6/6 | - |
| `sin` | complete | 4/4 | - |
| `splitlr` | complete | 6/6 | - |
| `splittb` | complete | 6/6 | - |
| `smoothstep` | complete | 11/11 | - |
| `sqrt` | complete | 4/4 | - |
| `subsurface_bsdf` | complete | 1/1 | - |
| `subtract` | complete | 16/16 | - |
| `switch` | complete | 16/16 | - |
| `surface` | complete | 1/1 | - |
| `tan` | complete | 4/4 | - |
| `tangent` | complete | 1/1 | - |
| `texcoord` | complete | 2/2 | - |
| `tiledcircles` | complete | 1/1 | - |
| `tiledcloverleafs` | complete | 1/1 | - |
| `tiledhexagons` | complete | 1/1 | - |
| `tiledimage` | complete | 6/6 | - |
| `time` | complete | 1/1 | - |
| `transformmatrix` | complete | 4/4 | - |
| `transformnormal` | complete | 1/1 | - |
| `transformpoint` | complete | 1/1 | - |
| `transformvector` | complete | 1/1 | - |
| `translucent_bsdf` | complete | 1/1 | - |
| `transpose` | complete | 2/2 | - |
| `trianglewave` | complete | 1/1 | - |
| `unifiednoise2d` | complete | 1/1 | - |
| `unifiednoise3d` | complete | 1/1 | - |
| `uniform_edf` | complete | 1/1 | - |
| `unpremult` | complete | 1/1 | - |
| `viewdirection` | complete | 1/1 | - |
| `volume` | complete | 1/1 | - |
| `worleynoise2d` | complete | 3/3 | - |
| `worleynoise3d` | complete | 3/3 | - |
| `xor` | complete | 1/1 | - |

## Methodology

- AOUSD MaterialX node families were extracted from `###`-level code headings in `MaterialX*.md` files under `~/code/materials-node-definitions/specification`.
- NodeDefs were read from the current checked-out MaterialX `stdlib`, `pbrlib`, and `nprlib` library files, then filtered to the AOUSD family set above.
- Fixture usage was extracted from all `.usda` files below `material-fidelity` by matching exact `uniform token info:id = "ND_*"` values.
- Variant labels in the table are the suffix after `ND_<family>_`; unsuffixed NodeDefs are shown by their full `ND_*` id.
- Complete rows intentionally show `-` for uncovered variants.
- This was a static completeness check; it did not execute renders or assess pass/fail results.
