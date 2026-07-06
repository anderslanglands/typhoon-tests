from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import struct
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import MaterialX as mx

from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdRender, UsdShade


ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = Path("/home/anders/code/material-samples")

VIEWER_ROOT = SOURCE_ROOT / "viewer"
MATERIAL_ROOT = SOURCE_ROOT / "materials"

ASSET_ROOT = ROOT / "_assets"
MATERIAL_ASSET_ROOT = ASSET_ROOT / "Materials"
REFERENCE_ROOT = ROOT / "reference"
GEOMETRY_LAYER = ASSET_ROOT / "Geometry" / "shaderball.usda"
BASE_LAYER = ASSET_ROOT / "base.usda"
HDR_SOURCE = VIEWER_ROOT / "san_giuseppe_bridge_2k.hdr"
GLB_SOURCE = VIEWER_ROOT / "ShaderBall.glb"

IDEAL_MESH_SPHERE_RADIUS = 2.0
SOURCE_DISTANCE_TO_METERS = 0.01

# The source shaderball materials use centimetre-sized material distances,
# while the generated USD suite declares metre scene units. Keep ratios such
# as Standard Surface's RGB subsurface_radius unchanged and convert the scalar
# distance which gives them their physical scale.
_DISTANCE_INPUTS = {
    "standard_surface": {"subsurface_scale", "transmission_depth"},
    "open_pbr_surface": {"subsurface_radius", "transmission_depth"},
}

# These upstream input fixtures otherwise do not exercise the control named by
# the case: scattering is disabled at zero transmission depth, and white
# transmission_color makes transmission_depth inert.
_FIXTURE_INPUT_OVERRIDES = {
    "input_transmission_scatter": {
        ("standard_surface", "transmission_depth"): 5.0,
    },
    "input_transmission_scatter_anisotropy": {
        ("standard_surface", "transmission_depth"): 5.0,
        ("open_pbr_surface", "transmission_depth"): 5.0,
    },
    "input_transmission_depth": {
        ("standard_surface", "transmission_color"): Gf.Vec3f(0.25, 0.55, 0.9),
    },
}
CAMERA_APERTURE_FOR_45_DEGREE_FOV = 41.421356
EXPECTED_MISSING_REFERENCE_COUNT = 36
EXPECTED_VALIDATION_FAILURE_COUNT = 29

_MISSING_REFERENCE_PATTERNS = (
    "nodes/convert_invalid_implicit_*/*.mtlx",
    "nodes/image_format_avif/*.mtlx",
    "surfaces/standard_surface/showcase_graph_pbr_helpers/*.mtlx",
    "surfaces/standard_surface/standard_surface_sweep_thin_film_thickness_*/*.mtlx",
)

_EXCLUDED_SOURCE_PATTERNS = (
    # Duplicate authored graphs already covered by another source fixture.
    "nodes/unifiednoise2d_type2/*.mtlx",
    "nodes/unifiednoise3d_type2/*.mtlx",
    "nodes/worleynoise2d/*.mtlx",
    "nodes/worleynoise3d/*.mtlx",
    "showcase/standard_surface/onyx_hextiled_no_scale/*.mtlx",
    "showcase/standard_surface/wood_grain/*.mtlx",
    "surfaces/standard_surface/marble_solid/*.mtlx",
    # Occlusion is not observable in the current beauty-render fixture.
    "surfaces/gltf_pbr/occlusion_half/*.mtlx",
    "surfaces/gltf_pbr/occlusion_one/*.mtlx",
    "surfaces/gltf_pbr/occlusion_zero/*.mtlx",
    # Dispersion fixtures were already removed because this shader path lacks it.
    "surfaces/gltf_pbr/feature_dispersion/*.mtlx",
    "surfaces/gltf_pbr/gltf_pbr_sweep_dispersion_*/*.mtlx",
)

_KNOWN_VALIDATION_FAILURE_PATTERNS = (
    "nodes/artistic_ior/*.mtlx",
    "nodes/convert_invalid_implicit_*/*.mtlx",
    "nodes/gltf_normalmap_isolate_scale_float/*.mtlx",
    "nodes/normalize_degenerate_zero_vector/*.mtlx",
    "nodes/switch/*.mtlx",
    "surfaces/gltf_pbr/gltf_normalmap/*.mtlx",
    "surfaces/standard_surface/color3_vec3_cm/*.mtlx",
    "surfaces/standard_surface/ior/*.mtlx",
    "surfaces/standard_surface/logic_composite_nodes/*.mtlx",
    "surfaces/standard_surface/opacity*/*.mtlx",
    "surfaces/standard_surface/showcase_opacity_specular_ior/*.mtlx",
    "surfaces/standard_surface/texture_opacity/*.mtlx",
    "surfaces/standard_surface/thin_film_ior_clamp/*.mtlx",
    "surfaces/standard_surface/thin_film_rainbow/*.mtlx",
    "surfaces/standard_surface/transmission*/*.mtlx",
)


COMPONENT_FORMATS = {
    5120: ("b", 1),
    5121: ("B", 1),
    5122: ("h", 2),
    5123: ("H", 2),
    5125: ("I", 4),
    5126: ("f", 4),
}

ACCESSOR_ARITY = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}


@dataclass(frozen=True)
class GltfBuffer:
    json: dict[str, Any]
    binary: bytes


@dataclass(frozen=True)
class MaterialCase:
    source: Path
    test_path: Path
    reference_path: Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--output-root", type=Path, default=ROOT)
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace generated USDAs and source PNG references",
    )
    args = parser.parse_args()
    _configure_paths(args.source_root.expanduser(), args.output_root.expanduser())

    cases = _discover_material_cases()
    _ensure_sources_exist(cases)
    validation_failures = _validate_material_sources(cases)
    _copy_shared_assets(cases, force=args.force)
    if args.force or not GEOMETRY_LAYER.exists():
        _write_geometry_layer(_read_glb(GLB_SOURCE), GEOMETRY_LAYER)
    if args.force or not BASE_LAYER.exists():
        _write_base_layer(BASE_LAYER)

    generated = 0
    preserved = 0
    for case in cases:
        if case.test_path.exists() and not args.force:
            preserved += 1
            continue
        _write_test_layer(case.test_path, case.source)
        generated += 1
    print(f"Generated {generated} tests; preserved {preserved} existing tests.")
    print(
        f"Accepted {validation_failures} declared source validation failures."
    )


def _configure_paths(source_root: Path, output_root: Path) -> None:
    global ROOT, SOURCE_ROOT, VIEWER_ROOT, MATERIAL_ROOT
    global ASSET_ROOT, MATERIAL_ASSET_ROOT, REFERENCE_ROOT
    global GEOMETRY_LAYER, BASE_LAYER, HDR_SOURCE, GLB_SOURCE

    ROOT = output_root.resolve()
    SOURCE_ROOT = source_root.resolve()
    VIEWER_ROOT = SOURCE_ROOT / "viewer"
    MATERIAL_ROOT = SOURCE_ROOT / "materials"
    ASSET_ROOT = ROOT / "_assets"
    MATERIAL_ASSET_ROOT = ASSET_ROOT / "Materials"
    REFERENCE_ROOT = ROOT / "reference"
    GEOMETRY_LAYER = ASSET_ROOT / "Geometry" / "shaderball.usda"
    BASE_LAYER = ASSET_ROOT / "base.usda"
    HDR_SOURCE = VIEWER_ROOT / "san_giuseppe_bridge_2k.hdr"
    GLB_SOURCE = VIEWER_ROOT / "ShaderBall.glb"


def _discover_material_cases() -> tuple[MaterialCase, ...]:
    cases = []
    missing_references = []
    for source in sorted(MATERIAL_ROOT.glob("**/*.mtlx")):
        if _matches_source_pattern(source, _EXCLUDED_SOURCE_PATTERNS):
            continue
        if not source.with_name("materialx-osl.png").is_file():
            missing_references.append(source)
            continue
        relative_parent = source.parent.relative_to(MATERIAL_ROOT)
        case_name = _material_case_name(source)
        if relative_parent.parts[0] == "nodes":
            relative_parent = Path("nodes") / _node_section(source)
        else:
            relative_parent = relative_parent.parent
        cases.append(
            MaterialCase(
                source=source,
                test_path=ROOT / relative_parent / f"{case_name}.usda",
                reference_path=(
                    REFERENCE_ROOT
                    / relative_parent
                    / f"{case_name}_materialx-osl.png"
                ),
            )
        )
    unexpected_missing = [
        source
        for source in missing_references
        if not _matches_source_pattern(source, _MISSING_REFERENCE_PATTERNS)
    ]
    if (
        len(missing_references) != EXPECTED_MISSING_REFERENCE_COUNT
        or unexpected_missing
    ):
        paths = ", ".join(
            str(path.relative_to(MATERIAL_ROOT)) for path in unexpected_missing
        )
        raise ValueError(
            f"expected {EXPECTED_MISSING_REFERENCE_COUNT} declared materials "
            f"without OSL references, found {len(missing_references)}; "
            f"unexpected: {paths or 'none'}"
        )
    return tuple(cases)


def _matches_source_pattern(source: Path, patterns: tuple[str, ...]) -> bool:
    relative_source = source.relative_to(MATERIAL_ROOT).as_posix()
    return any(fnmatch.fnmatchcase(relative_source, pattern) for pattern in patterns)


def _validate_material_sources(cases: tuple[MaterialCase, ...]) -> int:
    failures = []
    unexpected = []
    for case in cases:
        valid, message = _load_mtlx_document(case.source).validate()
        if valid:
            continue
        failures.append((case.source, message))
        if not _matches_source_pattern(
            case.source, _KNOWN_VALIDATION_FAILURE_PATTERNS
        ):
            unexpected.append((case.source, message))
    if len(failures) != EXPECTED_VALIDATION_FAILURE_COUNT or unexpected:
        details = "; ".join(
            f"{path.relative_to(MATERIAL_ROOT)}: {message.strip()}"
            for path, message in unexpected
        )
        raise ValueError(
            f"expected {EXPECTED_VALIDATION_FAILURE_COUNT} declared MaterialX "
            f"validation failures, found {len(failures)}; "
            f"unexpected: {details or 'none'}"
        )
    return len(failures)


def _node_section(material_path: Path) -> str:
    root = ET.parse(material_path).getroot()
    tested_element = next(
        (
            element
            for element in root.iter()
            if element.attrib.get("name") == "node_under_test"
        ),
        None,
    )
    node_name = (
        tested_element.tag
        if tested_element is not None
        else _infer_tested_node(material_path.parent.name)
    )
    for section, node_names in _NODE_SECTIONS:
        if node_name in node_names:
            return section
    raise ValueError(f"cannot classify {material_path}: tested node {node_name!r}")


def _infer_tested_node(case_name: str) -> str:
    aliases = {
        "artistic_ior": "artistic_ior",
        "gltf_normalmap": "gltf_normalmap",
        "image_": "image",
        "logic_and": "and",
        "logic_not": "not",
        "logic_or": "or",
        "logic_xor": "xor",
        "matrix33_creatematrix": "creatematrix",
        "matrix33_transformmatrix": "transformmatrix",
        "matrix44_creatematrix": "creatematrix",
        "matrix44_transformmatrix": "transformmatrix",
        "uv": "texcoord",
    }
    for prefix, node_name in aliases.items():
        if case_name.startswith(prefix):
            return node_name

    matches = [
        node_name
        for _, node_names in _NODE_SECTIONS
        for node_name in node_names
        if case_name == node_name or case_name.startswith(f"{node_name}_")
    ]
    if matches:
        return max(matches, key=len)
    raise ValueError(f"cannot infer tested node from case name {case_name!r}")


_NODE_SECTIONS = (
    (
        "textures",
        {
            "image",
            "tiledimage",
            "latlongimage",
            "hextiledimage",
            "triplanarprojection",
            "gltf_normalmap",
        },
    ),
    (
        "procedurals",
        {"constant", "ramplr", "ramptb", "ramp4", "splitlr", "splittb", "ramp"},
    ),
    (
        "noise",
        {
            "noise2d",
            "noise3d",
            "fractal2d",
            "fractal3d",
            "cellnoise2d",
            "cellnoise3d",
            "worleynoise2d",
            "worleynoise3d",
            "unifiednoise2d",
            "unifiednoise3d",
            "flake2d",
            "flake3d",
        },
    ),
    (
        "patterns",
        {
            "checkerboard",
            "line",
            "circle",
            "cloverleaf",
            "hexagon",
            "grid",
            "crosshatch",
            "tiledcircles",
            "tiledcloverleafs",
            "tiledhexagons",
        },
    ),
    (
        "geometric",
        {
            "position",
            "normal",
            "tangent",
            "bitangent",
            "binormal",
            "bump",
            "texcoord",
            "geomcolor",
            "geompropvalue",
            "geompropvalueuniform",
            "viewdirection",
        },
    ),
    ("application", {"frame", "time"}),
    (
        "math",
        {
            "add",
            "subtract",
            "multiply",
            "divide",
            "modulo",
            "fract",
            "invert",
            "absval",
            "sign",
            "floor",
            "ceil",
            "round",
            "power",
            "safepower",
            "sin",
            "cos",
            "tan",
            "asin",
            "acos",
            "atan2",
            "sqrt",
            "ln",
            "exp",
            "clamp",
            "trianglewave",
            "min",
            "max",
            "mincomponent",
            "maxcomponent",
            "normalize",
            "magnitude",
            "distance",
            "dotproduct",
            "crossproduct",
            "transformpoint",
            "transformvector",
            "transformnormal",
            "transformmatrix",
            "normalmap",
            "hextilednormalmap",
            "creatematrix",
            "transpose",
            "determinant",
            "invertmatrix",
            "rotate2d",
            "rotate3d",
            "reflect",
            "refract",
            "place2d",
            "dot",
        },
    ),
    ("logical", {"and", "or", "xor", "not"}),
    (
        "adjustment",
        {
            "contrast",
            "remap",
            "range",
            "smoothstep",
            "luminance",
            "rgbtohsv",
            "hsvtorgb",
            "hsvadjust",
            "saturate",
            "colorcorrect",
        },
    ),
    (
        "compositing",
        {
            "premult",
            "unpremult",
            "plus",
            "minus",
            "difference",
            "burn",
            "dodge",
            "screen",
            "overlay",
            "disjointover",
            "in",
            "mask",
            "matte",
            "out",
            "over",
            "inside",
            "outside",
            "mix",
        },
    ),
    ("conditional", {"ifgreater", "ifgreatereq", "ifequal", "switch"}),
    (
        "channel",
        {
            "extract",
            "convert",
            "combine2",
            "combine3",
            "combine4",
            "separate2",
            "separate3",
            "separate4",
        },
    ),
    ("convolution", {"blur", "heighttonormal"}),
    ("pbr", {"artistic_ior", "blackbody"}),
)


def _ensure_sources_exist(cases: tuple[MaterialCase, ...]) -> None:
    if not cases:
        raise FileNotFoundError(MATERIAL_ROOT / "**/*.mtlx")
    paths = (HDR_SOURCE, GLB_SOURCE) + tuple(
        path
        for case in cases
        for path in (case.source, case.source.with_name("materialx-osl.png"))
    )
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(path)
    for case in cases:
        for asset_source in _material_asset_sources(case.source):
            if not asset_source.is_file():
                raise FileNotFoundError(asset_source)


def _copy_shared_assets(cases: tuple[MaterialCase, ...], force: bool) -> None:
    (ASSET_ROOT / "Lights").mkdir(parents=True, exist_ok=True)
    (ASSET_ROOT / "Geometry").mkdir(parents=True, exist_ok=True)
    _copy_file(HDR_SOURCE, ASSET_ROOT / "Lights" / HDR_SOURCE.name, force)
    for case in cases:
        reference_source = case.source.with_name("materialx-osl.png")
        case_name = _material_case_name(case.source)
        exr_reference = case.reference_path.with_name(f"{case_name}.exr")
        png_selected = _png_reference_is_selected(case)
        if exr_reference.is_file() and not png_selected:
            pass
        elif case.reference_path.is_file():
            if force:
                _copy_file(reference_source, case.reference_path, True)
            _ensure_png_reference_override(case)
        else:
            _copy_file(reference_source, case.reference_path, False)
            _ensure_png_reference_override(case)
        for asset_source in _material_asset_sources(case.source):
            destination = _material_asset_path(asset_source)
            _copy_file(asset_source, destination, force)


def _png_reference_is_selected(case: MaterialCase) -> bool:
    config_path = case.test_path.with_suffix(".typhoon.toml")
    if not config_path.is_file():
        return False
    with config_path.open("rb") as stream:
        data = tomllib.load(stream)
    reference = data.get("reference")
    relative_reference = case.reference_path.relative_to(ROOT).as_posix()
    return isinstance(reference, dict) and reference.get("path") == relative_reference


def _ensure_png_reference_override(case: MaterialCase) -> None:
    config_path = case.test_path.with_suffix(".typhoon.toml")
    relative_reference = case.reference_path.relative_to(ROOT).as_posix()
    if config_path.is_file():
        with config_path.open("rb") as stream:
            data = tomllib.load(stream)
        reference = data.get("reference")
        if isinstance(reference, dict) and reference.get("path") == relative_reference:
            return
        if reference is not None:
            raise ValueError(f"cannot replace [reference] in {config_path}")
        prefix = config_path.read_text(encoding="utf-8").rstrip() + "\n\n"
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        prefix = ""
    config_text = (
        f"{prefix}[reference]\npath = {json.dumps(relative_reference)}\n"
    )
    temporary = config_path.with_name(f".{config_path.name}.tmp")
    temporary.write_text(config_text, encoding="utf-8")
    os.replace(temporary, config_path)


def _copy_file(source: Path, destination: Path, force: bool) -> None:
    if destination.exists() and not force:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _material_asset_path(source: Path) -> Path:
    relative_source = source.relative_to(MATERIAL_ROOT)
    if relative_source.parts[:2] == ("surfaces", "open_pbr_surface"):
        relative_source = Path("open_pbr_surface") / relative_source.relative_to(
            Path("surfaces/open_pbr_surface")
        )
    return MATERIAL_ASSET_ROOT / relative_source


def _material_case_name(material_path: Path) -> str:
    return material_path.parent.name


def _material_asset_sources(material_path: Path) -> tuple[Path, ...]:
    root = ET.parse(material_path).getroot()
    sources = []
    for input_element in root.iter("input"):
        if input_element.attrib.get("type") != "filename":
            continue
        value = input_element.attrib.get("value")
        if value:
            sources.append((material_path.parent / value).resolve())
    return tuple(sources)



def _read_glb(path: Path) -> GltfBuffer:
    data = path.read_bytes()
    magic, version, declared_length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or version != 2:
        raise ValueError(f"{path} is not a glTF 2.0 GLB")
    if declared_length != len(data):
        raise ValueError(f"{path} has inconsistent GLB length")

    offset = 12
    json_payload: bytes | None = None
    binary_payload: bytes | None = None
    while offset < len(data):
        chunk_length, chunk_type = struct.unpack_from("<I4s", data, offset)
        offset += 8
        payload = data[offset : offset + chunk_length]
        offset += chunk_length
        if chunk_type == b"JSON":
            json_payload = payload
        elif chunk_type == b"BIN\0":
            binary_payload = payload

    if json_payload is None or binary_payload is None:
        raise ValueError(f"{path} must contain JSON and BIN chunks")

    gltf_json = json.loads(json_payload.decode("utf-8").rstrip("\0 "))
    return GltfBuffer(gltf_json, binary_payload)


def _write_geometry_layer(gltf: GltfBuffer, path: Path) -> None:
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    stage.SetMetadata("metersPerUnit", 1.0)

    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())
    geometry = UsdGeom.Xform.Define(stage, "/World/Geometry")

    scene_index = gltf.json.get("scene", 0)
    scene = gltf.json["scenes"][scene_index]
    bounds_min, bounds_max = _scene_bounds(gltf, scene)
    _apply_geometry_normalization(geometry, bounds_min, bounds_max)
    # Bake GLB node transforms so MaterialX object-space inputs match the
    # flattened coordinates used by the reference renderers.
    identity = Gf.Matrix4d(1.0)
    for node_index in scene.get("nodes", []):
        _write_node(stage, gltf, node_index, geometry.GetPath(), identity)

    stage.GetRootLayer().Save()
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")


def _scene_bounds(
    gltf: GltfBuffer, scene: dict[str, Any]
) -> tuple[Gf.Vec3d, Gf.Vec3d]:
    bounds: tuple[Gf.Vec3d, Gf.Vec3d] | None = None
    identity = Gf.Matrix4d(1.0)
    for node_index in scene.get("nodes", []):
        bounds = _merge_bounds(bounds, _node_bounds(gltf, node_index, identity))
    if bounds is None:
        raise ValueError("GLB scene contains no mesh geometry")
    return bounds


def _node_bounds(
    gltf: GltfBuffer,
    node_index: int,
    parent_transform: Gf.Matrix4d,
) -> tuple[Gf.Vec3d, Gf.Vec3d] | None:
    node = gltf.json["nodes"][node_index]
    node_transform = _node_transform_matrix(node) * parent_transform
    bounds: tuple[Gf.Vec3d, Gf.Vec3d] | None = None

    mesh_index = node.get("mesh")
    if mesh_index is not None:
        mesh = gltf.json["meshes"][mesh_index]
        for primitive in mesh.get("primitives", []):
            attributes = primitive["attributes"]
            points = _accessor_data(gltf, attributes["POSITION"])
            bounds = _merge_bounds(
                bounds, _transformed_bounds(points, node_transform)
            )

    for child_index in node.get("children", []):
        bounds = _merge_bounds(
            bounds, _node_bounds(gltf, child_index, node_transform)
        )
    return bounds


def _node_transform_matrix(node: dict[str, Any]) -> Gf.Matrix4d:
    if "matrix" in node:
        return Gf.Matrix4d(*node["matrix"])

    matrix = Gf.Matrix4d(1.0)
    if "scale" in node:
        matrix *= Gf.Matrix4d().SetScale(Gf.Vec3d(*node["scale"]))
    if "rotation" in node:
        x, y, z, w = node["rotation"]
        quat = Gf.Quatd(w, Gf.Vec3d(x, y, z))
        matrix *= Gf.Matrix4d().SetRotate(quat)
    if "translation" in node:
        matrix *= Gf.Matrix4d().SetTranslate(Gf.Vec3d(*node["translation"]))
    return matrix


def _transformed_bounds(
    points: list[Any], transform: Gf.Matrix4d
) -> tuple[Gf.Vec3d, Gf.Vec3d]:
    bounds: tuple[Gf.Vec3d, Gf.Vec3d] | None = None
    for point in points:
        transformed = transform.Transform(Gf.Vec3d(*point))
        bounds = _merge_bounds(bounds, (transformed, transformed))
    if bounds is None:
        raise ValueError("mesh primitive contains no points")
    return bounds


def _merge_bounds(
    first: tuple[Gf.Vec3d, Gf.Vec3d] | None,
    second: tuple[Gf.Vec3d, Gf.Vec3d] | None,
) -> tuple[Gf.Vec3d, Gf.Vec3d] | None:
    if second is None:
        return first
    if first is None:
        return second
    return (
        Gf.Vec3d(*(min(first[0][axis], second[0][axis]) for axis in range(3))),
        Gf.Vec3d(*(max(first[1][axis], second[1][axis]) for axis in range(3))),
    )


def _apply_geometry_normalization(
    geometry: UsdGeom.Xform, bounds_min: Gf.Vec3d, bounds_max: Gf.Vec3d
) -> None:
    center = (bounds_min + bounds_max) * 0.5
    diagonal = bounds_max - bounds_min
    sphere_radius = diagonal.GetLength() * 0.5
    if sphere_radius <= 0:
        raise ValueError("GLB scene bounds have zero radius")

    scale = IDEAL_MESH_SPHERE_RADIUS / sphere_radius
    geometry.AddScaleOp().Set(Gf.Vec3f(scale, scale, scale))
    geometry.AddTranslateOp().Set(Gf.Vec3d(-center[0], -center[1], -center[2]))


def _write_node(
    stage: Usd.Stage,
    gltf: GltfBuffer,
    node_index: int,
    parent_path: Sdf.Path,
    parent_transform: Gf.Matrix4d,
) -> None:
    node = gltf.json["nodes"][node_index]
    node_transform = _node_transform_matrix(node) * parent_transform
    node_name = _sanitize_identifier(node.get("name", f"Node_{node_index}"))
    node_path = parent_path.AppendChild(node_name)
    UsdGeom.Xform.Define(stage, node_path)

    mesh_index = node.get("mesh")
    if mesh_index is not None:
        mesh = gltf.json["meshes"][mesh_index]
        for primitive_index, primitive in enumerate(mesh.get("primitives", [])):
            prim_name = _sanitize_identifier(mesh.get("name", node_name))
            if len(mesh.get("primitives", [])) > 1:
                prim_name = f"{prim_name}_{primitive_index}"
            _write_mesh_primitive(
                stage,
                gltf,
                primitive,
                node_path.AppendChild(f"{prim_name}Shape"),
                node_transform,
            )

    for child_index in node.get("children", []):
        _write_node(stage, gltf, child_index, node_path, node_transform)


def _write_mesh_primitive(
    stage: Usd.Stage,
    gltf: GltfBuffer,
    primitive: dict[str, Any],
    mesh_path: Sdf.Path,
    transform: Gf.Matrix4d,
) -> None:
    if primitive.get("mode", 4) != 4:
        raise ValueError(f"only triangle GLB primitives are supported: {mesh_path}")

    attributes = primitive["attributes"]
    points = [
        transform.Transform(Gf.Vec3d(*point))
        for point in _accessor_data(gltf, attributes["POSITION"])
    ]
    indices = [int(value) for value in _accessor_data(gltf, primitive["indices"])]
    if len(indices) % 3:
        raise ValueError(f"triangle index count is not divisible by three: {mesh_path}")

    mesh = UsdGeom.Mesh.Define(stage, mesh_path)
    mesh.CreateSubdivisionSchemeAttr("none")
    mesh.CreateDoubleSidedAttr(False)
    mesh.CreatePointsAttr([Gf.Vec3f(*point) for point in points])
    mesh.CreateFaceVertexCountsAttr([3] * (len(indices) // 3))
    mesh.CreateFaceVertexIndicesAttr(indices)

    bounds_min, bounds_max = _bounds(points)
    mesh.CreateExtentAttr([Gf.Vec3f(*bounds_min), Gf.Vec3f(*bounds_max)])

    normals_index = attributes.get("NORMAL")
    if normals_index is not None:
        normal_transform = transform.GetInverse().GetTranspose()
        normals = [
            normal_transform.TransformDir(Gf.Vec3d(*normal)).GetNormalized()
            for normal in _accessor_data(gltf, normals_index)
        ]
        mesh.CreateNormalsAttr([Gf.Vec3f(normal) for normal in normals])
        mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)

    st_index = attributes.get("TEXCOORD_0")
    if st_index is not None:
        texcoords = _accessor_data(gltf, st_index)
        primvars = UsdGeom.PrimvarsAPI(mesh)
        st = primvars.CreatePrimvar(
            "st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex
        )
        st.Set([Gf.Vec2f(*texcoord) for texcoord in texcoords])

    tangent_index = attributes.get("TANGENT")
    if tangent_index is not None:
        tangents = _accessor_data(gltf, tangent_index)
        handedness = -1.0 if transform.GetDeterminant3() < 0.0 else 1.0
        transformed_tangents = []
        for value in tangents:
            direction = transform.TransformDir(
                Gf.Vec3d(*value[:3])
            ).GetNormalized()
            transformed_tangents.append(
                Gf.Vec4f(
                    direction[0],
                    direction[1],
                    direction[2],
                    value[3] * handedness,
                )
            )
        primvars = UsdGeom.PrimvarsAPI(mesh)
        tangent = primvars.CreatePrimvar(
            "tangent", Sdf.ValueTypeNames.Float4Array, UsdGeom.Tokens.vertex
        )
        tangent.Set(transformed_tangents)


def _accessor_data(gltf: GltfBuffer, accessor_index: int) -> list[Any]:
    accessor = gltf.json["accessors"][accessor_index]
    view = gltf.json["bufferViews"][accessor["bufferView"]]
    if view.get("buffer", 0) != 0:
        raise ValueError("only a single GLB binary buffer is supported")

    component_format, component_size = COMPONENT_FORMATS[accessor["componentType"]]
    arity = ACCESSOR_ARITY[accessor["type"]]
    element_size = component_size * arity
    stride = view.get("byteStride", element_size)
    base_offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    unpack = struct.Struct("<" + component_format * arity).unpack_from

    values: list[Any] = []
    for index in range(accessor["count"]):
        value = unpack(gltf.binary, base_offset + index * stride)
        values.append(value[0] if arity == 1 else value)
    return values


def _bounds(points: list[Any]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    mins = [min(point[axis] for point in points) for axis in range(3)]
    maxs = [max(point[axis] for point in points) for axis in range(3)]
    return tuple(mins), tuple(maxs)


def _write_base_layer(path: Path) -> None:
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    stage.SetMetadata("metersPerUnit", 1.0)
    stage.SetMetadata("renderSettingsPrimPath", "/Render/Settings")
    stage.GetRootLayer().subLayerPaths = ["./Geometry/shaderball.usda"]

    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())

    dome = UsdLux.DomeLight.Define(stage, "/World/DomeLight")
    dome.CreateIntensityAttr(1.0)
    dome.CreateTextureFileAttr(Sdf.AssetPath("Lights/san_giuseppe_bridge_2k.hdr"))
    dome.CreateTextureFormatAttr("latlong")

    camera = UsdGeom.Camera.Define(stage, "/World/Camera")
    camera.CreateFocalLengthAttr(50.0)
    camera.CreateHorizontalApertureAttr(CAMERA_APERTURE_FOR_45_DEGREE_FOV)
    camera.CreateVerticalApertureAttr(CAMERA_APERTURE_FOR_45_DEGREE_FOV)
    camera.CreateClippingRangeAttr(Gf.Vec2f(0.05, 1000.0))
    camera_xform = UsdGeom.Xformable(camera)
    camera_xform.AddTranslateOp().Set(Gf.Vec3d(0.0, 0.0, 5.0))

    _write_render_settings(stage, "material-fidelity.exr")
    stage.GetRootLayer().Save()
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")


def _write_test_layer(path: Path, material_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    stage.SetMetadata("metersPerUnit", 1.0)
    stage.SetMetadata("renderSettingsPrimPath", "/Render/Settings")
    base_layer_path = Path(os.path.relpath(BASE_LAYER, path.parent)).as_posix()
    stage.GetRootLayer().subLayerPaths = [base_layer_path]
    world = stage.OverridePrim("/World")
    stage.SetDefaultPrim(world)

    test_stem = _material_case_name(material_path)
    material_name = _sanitize_identifier(test_stem)
    material = _write_usdshade_material(stage, material_name, material_path, path)

    geometry = stage.OverridePrim("/World/Geometry")
    UsdShade.MaterialBindingAPI.Apply(geometry)
    UsdShade.MaterialBindingAPI(geometry).Bind(material)

    product = UsdRender.Product.Get(stage, "/Render/Settings/Product")
    test_path = path.relative_to(ROOT).with_suffix("").as_posix()
    product.GetProductNameAttr().Set(f"material-fidelity/{test_path}.exr")
    stage.GetRootLayer().Save()
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")


_MTLX_LIBRARY: mx.Document | None = None


def _load_mtlx_document(path: Path) -> mx.Document:
    global _MTLX_LIBRARY
    if _MTLX_LIBRARY is None:
        library = mx.createDocument()
        mx.loadLibraries(
            mx.getDefaultDataLibraryFolders(),
            mx.getDefaultDataSearchPath(),
            library,
        )
        _MTLX_LIBRARY = library

    document = mx.createDocument()
    document.importLibrary(_MTLX_LIBRARY)
    mx.readFromXmlFile(document, str(path), mx.FileSearchPath(str(path.parent)))
    return document


def _write_usdshade_material(
    stage: Usd.Stage,
    material_name: str,
    material_path: Path,
    layer_path: Path,
) -> UsdShade.Material:
    looks = stage.DefinePrim("/Looks", "Scope")
    material = UsdShade.Material.Define(stage, looks.GetPath().AppendChild(material_name))

    root = ET.parse(material_path).getroot()
    document = _load_mtlx_document(material_path)
    shader_element, material_element = _find_surface_shader(root)
    nodes: dict[tuple[str | None, str], UsdShade.Shader] = {}
    node_records: list[
        tuple[ET.Element, UsdShade.Shader, str | None, mx.NodeDef]
    ] = []
    node_graphs: dict[str, UsdShade.NodeGraph] = {}
    conversion_index = 0

    def define_node(
        element: ET.Element,
        parent_path: Sdf.Path,
        scope: str | None,
        mtlx_node: mx.Node,
    ) -> None:
        node_name = element.attrib["name"]
        shader = UsdShade.Shader.Define(
            stage, parent_path.AppendChild(_sanitize_identifier(node_name))
        )
        node_def = mtlx_node.getNodeDef()
        if node_def is None:
            raise ValueError(
                f"no MaterialX nodedef for {element.tag} node {node_name!r}"
            )
        shader.CreateIdAttr(node_def.getName())
        for output_element in node_def.getActiveOutputs():
            shader.CreateOutput(
                output_element.getName(),
                _sdf_type_for_mtlx(output_element.getType()),
            )
        nodes[(scope, node_name)] = shader
        node_records.append((element, shader, scope, node_def))

    def connect(
        destination: UsdShade.Input | UsdShade.Output,
        source: UsdShade.Input | UsdShade.Output,
        parent_path: Sdf.Path,
    ) -> None:
        nonlocal conversion_index
        if destination.GetTypeName() == source.GetTypeName():
            destination.ConnectToSource(source)
            return
        relative_source = material_path.relative_to(MATERIAL_ROOT).as_posix()
        if relative_source.startswith("nodes/convert_invalid_implicit_"):
            destination.ConnectToSource(source)
            return

        source_type = _mtlx_type_for_sdf(source.GetTypeName())
        target_type = _mtlx_type_for_sdf(destination.GetTypeName())
        node_def_name = f"ND_convert_{source_type}_{target_type}"
        if document.getNodeDef(node_def_name) is None:
            raise ValueError(
                f"{material_path}: cannot connect {source.GetTypeName()} to "
                f"{destination.GetTypeName()}; no {node_def_name}"
            )
        conversion_index += 1
        converter = UsdShade.Shader.Define(
            stage, parent_path.AppendChild(f"convert_{conversion_index}")
        )
        converter.CreateIdAttr(node_def_name)
        converter_input = converter.CreateInput("in", source.GetTypeName())
        converter_input.ConnectToSource(source)
        converter_output = converter.CreateOutput("out", destination.GetTypeName())
        destination.ConnectToSource(converter_output)

    for element in root:
        if element.tag in {"nodegraph", "surfacematerial"}:
            continue
        mtlx_node = document.getNode(element.attrib["name"])
        define_node(element, material.GetPath(), None, mtlx_node)

    for graph_element in root.findall("nodegraph"):
        graph_name = graph_element.attrib["name"]
        node_graph = UsdShade.NodeGraph.Define(
            stage, material.GetPath().AppendChild(_sanitize_identifier(graph_name))
        )
        node_graphs[graph_name] = node_graph
        mtlx_graph = document.getNodeGraph(graph_name)
        for input_element in graph_element.findall("input"):
            graph_input = node_graph.CreateInput(
                input_element.attrib["name"],
                _sdf_type_for_mtlx(input_element.attrib["type"]),
            )
            if "value" in input_element.attrib:
                graph_input.Set(
                    _parse_mtlx_input_value(
                        input_element, material_path, layer_path
                    )
                )
            color_space = input_element.attrib.get("colorspace")
            if color_space:
                graph_input.GetAttr().SetColorSpace(color_space)
        for element in graph_element:
            if element.tag in {"input", "output"}:
                continue
            define_node(
                element,
                node_graph.GetPath(),
                graph_name,
                mtlx_graph.getNode(element.attrib["name"]),
            )

    for graph_element in root.findall("nodegraph"):
        node_graph = node_graphs[graph_element.attrib["name"]]
        for output_element in graph_element.findall("output"):
            node_graph.CreateOutput(
                output_element.attrib["name"],
                _sdf_type_for_mtlx(output_element.attrib["type"]),
            )


    for element, shader, scope, node_def in node_records:
        for input_element in element.findall("input"):
            name, value_type = _normalized_input_spec(
                element, input_element, node_def
            )
            shader_input = shader.CreateInput(name, _sdf_type_for_mtlx(value_type))
            if "value" in input_element.attrib:
                value = _parse_mtlx_input_value(
                    input_element, material_path, layer_path
                )
                value = _coerce_mtlx_value(
                    value, input_element.attrib["type"], value_type
                )
                value = _adapt_fixture_input_value(
                    element, input_element, material_path, value
                )
                shader_input.Set(value)
            elif "nodename" in input_element.attrib:
                source = nodes[(scope, input_element.attrib["nodename"])]
                source_output = _resolve_source_output(
                    source, input_element, value_type, material_path
                )
                connect(shader_input, source_output, shader.GetPath().GetParentPath())
            elif "nodegraph" in input_element.attrib:
                source_graph = node_graphs[input_element.attrib["nodegraph"]]
                source_output = source_graph.GetOutput(input_element.attrib["output"])
                connect(shader_input, source_output, shader.GetPath().GetParentPath())
            elif "interfacename" in input_element.attrib:
                if scope is None:
                    raise ValueError(
                        f"{material_path}: top-level node input cannot use "
                        f"interfacename={input_element.attrib['interfacename']!r}"
                    )
                source_input = node_graphs[scope].GetInput(
                    input_element.attrib["interfacename"]
                )
                connect(shader_input, source_input, shader.GetPath().GetParentPath())
            color_space = input_element.attrib.get("colorspace")
            if color_space:
                shader_input.GetAttr().SetColorSpace(color_space)
        _author_missing_fixture_overrides(element, shader, material_path)
        _author_meter_scaled_subsurface_default(element, shader)

    for graph_element in root.findall("nodegraph"):
        graph_name = graph_element.attrib["name"]
        node_graph = node_graphs[graph_name]
        for output_element in graph_element.findall("output"):
            graph_output = node_graph.CreateOutput(
                output_element.attrib["name"],
                _sdf_type_for_mtlx(output_element.attrib["type"]),
            )
            source = nodes[(graph_name, output_element.attrib["nodename"])]
            source_output = _resolve_source_output(
                source,
                output_element,
                output_element.attrib["type"],
                material_path,
            )
            connect(graph_output, source_output, node_graph.GetPath())

    surface_shader = nodes[(None, shader_element.attrib["name"])]
    surface_output = surface_shader.GetOutput("out")
    if not surface_output:
        output_names = [output.GetBaseName() for output in surface_shader.GetOutputs()]
        raise ValueError(
            f"{material_path}: surface shader has no 'out' output; "
            f"available outputs: {output_names}"
        )
    material.CreateSurfaceOutput("mtlx").ConnectToSource(surface_output)
    material.CreateSurfaceOutput().ConnectToSource(surface_output)
    material.GetPrim().SetCustomDataByKey(
        "materialFidelity:source",
        str(material_path.relative_to(SOURCE_ROOT)),
    )
    material.GetPrim().SetCustomDataByKey(
        "materialFidelity:sourceMaterial",
        material_element.attrib["name"],
    )
    return material


_LEGACY_INPUT_NAMES = {
    ("standard_surface", "ior"): "specular_IOR",
    ("standard_surface", "thin_film_ior"): "thin_film_IOR",
}


def _adapt_fixture_input_value(
    node_element: ET.Element,
    input_element: ET.Element,
    material_path: Path,
    value: Any,
) -> Any:
    input_name = input_element.attrib["name"]
    override = _FIXTURE_INPUT_OVERRIDES.get(_material_case_name(material_path), {}).get(
        (node_element.tag, input_name)
    )
    if override is not None:
        value = override

    subsurface_distance_input = {
        "standard_surface": "subsurface_scale",
        "open_pbr_surface": "subsurface_radius",
    }.get(node_element.tag)
    if (
        input_name == subsurface_distance_input
        and not _has_positive_literal_subsurface_weight(node_element)
    ):
        return value

    if input_name in _DISTANCE_INPUTS.get(node_element.tag, set()):
        return float(value) * SOURCE_DISTANCE_TO_METERS
    return value


def _has_positive_literal_subsurface_weight(node_element: ET.Element) -> bool:
    weight_name = {
        "standard_surface": "subsurface",
        "open_pbr_surface": "subsurface_weight",
    }.get(node_element.tag)
    if weight_name is None:
        return False
    weight = node_element.find(f"input[@name='{weight_name}']")
    return weight is not None and float(weight.attrib.get("value", "0")) > 0.0


def _author_missing_fixture_overrides(
    node_element: ET.Element,
    shader: UsdShade.Shader,
    material_path: Path,
) -> None:
    overrides = _FIXTURE_INPUT_OVERRIDES.get(_material_case_name(material_path), {})
    for (node_tag, input_name), value in overrides.items():
        if node_tag != node_element.tag:
            continue
        if node_element.find(f"input[@name='{input_name}']") is not None:
            continue
        if input_name in _DISTANCE_INPUTS.get(node_tag, set()):
            value = float(value) * SOURCE_DISTANCE_TO_METERS
        value_type = (
            Sdf.ValueTypeNames.Color3f
            if isinstance(value, Gf.Vec3f)
            else Sdf.ValueTypeNames.Float
        )
        shader.CreateInput(input_name, value_type).Set(value)


def _author_meter_scaled_subsurface_default(
    node_element: ET.Element, shader: UsdShade.Shader
) -> None:
    if not _has_positive_literal_subsurface_weight(node_element):
        return
    distance_input = {
        "standard_surface": "subsurface_scale",
        "open_pbr_surface": "subsurface_radius",
    }[node_element.tag]
    if node_element.find(f"input[@name='{distance_input}']") is None:
        shader.CreateInput(distance_input, Sdf.ValueTypeNames.Float).Set(
            SOURCE_DISTANCE_TO_METERS
        )


def _normalized_input_spec(
    node_element: ET.Element,
    input_element: ET.Element,
    node_def: mx.NodeDef,
) -> tuple[str, str]:
    source_name = input_element.attrib["name"]
    name = _LEGACY_INPUT_NAMES.get(
        (node_element.tag, source_name), source_name
    )
    declared_input = node_def.getActiveInput(name)
    if declared_input is None:
        return name, input_element.attrib["type"]
    return name, declared_input.getType()


def _coerce_mtlx_value(value: Any, source_type: str, target_type: str) -> Any:
    if source_type == target_type:
        return value
    if source_type == "float" and target_type == "vector2":
        return Gf.Vec2f(value, value)
    if source_type == "float" and target_type == "vector3":
        return Gf.Vec3f(value, value, value)
    if source_type == "float" and target_type == "vector4":
        return Gf.Vec4f(value, value, value, value)
    if source_type == "float" and target_type == "color3":
        return Gf.Vec3f(value, value, value)
    if source_type == "float" and target_type == "color4":
        return Gf.Vec4f(value, value, value, value)
    raise ValueError(
        f"cannot convert MaterialX value from {source_type!r} to {target_type!r}"
    )


def _resolve_source_output(
    source: UsdShade.Shader,
    connection_element: ET.Element,
    value_type: str,
    material_path: Path,
) -> UsdShade.Output:
    explicit_name = connection_element.attrib.get("output")
    if explicit_name:
        output = source.GetOutput(explicit_name)
        if output:
            return output
        raise ValueError(
            f"{material_path}: {source.GetPath()} has no output {explicit_name!r}"
        )

    default_output = source.GetOutput("out")
    if default_output:
        return default_output

    expected_type = _sdf_type_for_mtlx(value_type)
    shader_id = source.GetIdAttr().Get()
    if shader_id == "ND_artistic_ior":
        ior_output = source.GetOutput("ior")
        if ior_output and ior_output.GetTypeName() == expected_type:
            return ior_output
    raise ValueError(
        f"{material_path}: connection to {source.GetPath()} has no default "
        f"output for {value_type!r}"
    )


def _parse_mtlx_input_value(
    input_element: ET.Element, material_path: Path, layer_path: Path
) -> Any:
    value_type = input_element.attrib["type"]
    value_text = input_element.attrib["value"]
    if value_type != "filename":
        return _parse_mtlx_value(value_type, value_text)

    source = (material_path.parent / value_text).resolve()
    destination = _material_asset_path(source)
    relative_destination = os.path.relpath(destination, layer_path.parent)
    return Sdf.AssetPath(Path(relative_destination).as_posix())


def _find_surface_shader(root: ET.Element) -> tuple[ET.Element, ET.Element]:
    elements_by_name = {
        element.attrib["name"]: element
        for element in root
        if "name" in element.attrib
    }
    for material_element in root.findall("surfacematerial"):
        for input_element in material_element.findall("input"):
            if input_element.attrib.get("name") == "surfaceshader":
                shader_name = input_element.attrib.get("nodename")
                if shader_name in elements_by_name:
                    return elements_by_name[shader_name], material_element
    raise ValueError("no surfacematerial surfaceshader connection found")


def _parse_mtlx_value(value_type: str, value_text: str) -> Any:
    values = [part.strip() for part in value_text.split(",")]
    if value_type in {"float", "angle", "integer"}:
        value = values[0]
        return int(value) if value_type == "integer" else float(value)
    if value_type == "boolean":
        return values[0].lower() in {"1", "true"}
    if value_type == "color3":
        return Gf.Vec3f(*(float(value) for value in values))
    if value_type == "color4":
        return Gf.Vec4f(*(float(value) for value in values))
    if value_type == "vector2":
        return Gf.Vec2f(*(float(value) for value in values))
    if value_type == "vector3":
        return Gf.Vec3f(*(float(value) for value in values))
    if value_type == "vector4":
        return Gf.Vec4f(*(float(value) for value in values))
    if value_type == "matrix33":
        return Gf.Matrix3d(*(float(value) for value in values))
    if value_type == "matrix44":
        return Gf.Matrix4d(*(float(value) for value in values))
    if value_type == "filename":
        return Sdf.AssetPath(value_text)
    if value_type == "string":
        return value_text
    raise ValueError(f"unsupported MaterialX value type {value_type!r}")


def _sdf_type_for_mtlx(value_type: str) -> Sdf.ValueTypeName:
    if value_type == "surfaceshader":
        return Sdf.ValueTypeNames.Token
    if value_type in {"float", "angle"}:
        return Sdf.ValueTypeNames.Float
    if value_type == "integer":
        return Sdf.ValueTypeNames.Int
    if value_type == "boolean":
        return Sdf.ValueTypeNames.Bool
    if value_type == "color3":
        return Sdf.ValueTypeNames.Color3f
    if value_type == "color4":
        return Sdf.ValueTypeNames.Color4f
    if value_type == "vector2":
        return Sdf.ValueTypeNames.Float2
    if value_type == "vector3":
        return Sdf.ValueTypeNames.Float3
    if value_type == "vector4":
        return Sdf.ValueTypeNames.Float4
    if value_type == "matrix33":
        return Sdf.ValueTypeNames.Matrix3d
    if value_type == "matrix44":
        return Sdf.ValueTypeNames.Matrix4d
    if value_type == "filename":
        return Sdf.ValueTypeNames.Asset
    if value_type == "string":
        return Sdf.ValueTypeNames.String
    raise ValueError(f"unsupported MaterialX value type {value_type!r}")


def _mtlx_type_for_sdf(value_type: Sdf.ValueTypeName) -> str:
    mappings = {
        Sdf.ValueTypeNames.Float: "float",
        Sdf.ValueTypeNames.Int: "integer",
        Sdf.ValueTypeNames.Bool: "boolean",
        Sdf.ValueTypeNames.Color3f: "color3",
        Sdf.ValueTypeNames.Color4f: "color4",
        Sdf.ValueTypeNames.Float2: "vector2",
        Sdf.ValueTypeNames.Float3: "vector3",
        Sdf.ValueTypeNames.Float4: "vector4",
        Sdf.ValueTypeNames.Matrix3d: "matrix33",
        Sdf.ValueTypeNames.Matrix4d: "matrix44",
    }
    try:
        return mappings[value_type]
    except KeyError as exc:
        raise ValueError(f"unsupported UsdShade conversion type {value_type}") from exc


def _write_render_settings(stage: Usd.Stage, product_name: str) -> None:
    settings = UsdRender.Settings.Define(stage, "/Render/Settings")
    settings.GetPrim().ApplyAPI("TyphoonRenderSettingsAPI")
    settings.GetPrim().CreateAttribute("ty:convergedSamplesPerPixel", Sdf.ValueTypeNames.Int).Set(64)
    settings.CreateResolutionAttr(Gf.Vec2i(512, 512))
    settings.CreateCameraRel().SetTargets([Sdf.Path("/World/Camera")])

    product = UsdRender.Product.Define(stage, "/Render/Settings/Product")
    product.CreateCameraRel().SetTargets([Sdf.Path("/World/Camera")])
    product.CreateProductTypeAttr("raster")
    product.CreateProductNameAttr(product_name)

    render_var = UsdRender.Var.Define(stage, "/Render/Settings/Product/Var")
    render_var.CreateDataTypeAttr("color4f")
    render_var.CreateSourceNameAttr("color")
    render_var.CreateSourceTypeAttr("raw")

    product.CreateOrderedVarsRel().SetTargets([render_var.GetPath()])
    settings.CreateProductsRel().SetTargets([product.GetPath()])


def _sanitize_identifier(value: str) -> str:
    result = []
    for character in value:
        if character.isalnum() or character == "_":
            result.append(character)
        else:
            result.append("_")
    text = "".join(result).strip("_")
    if not text:
        return "Prim"
    if text[0].isdigit():
        return f"_{text}"
    return text


if __name__ == "__main__":
    main()
