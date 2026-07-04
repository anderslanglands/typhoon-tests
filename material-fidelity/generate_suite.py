from __future__ import annotations

import json
import os
import shutil
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import MaterialX as mx

from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdRender, UsdShade


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
SOURCE_ROOT = Path("/home/anders/code/material-samples")

VIEWER_ROOT = SOURCE_ROOT / "viewer"
MATERIAL_ROOT = SOURCE_ROOT / "materials/surfaces/open_pbr_surface"
MATERIAL_GLOB = "*/*.mtlx"
MATERIAL_SOURCES = tuple(sorted(MATERIAL_ROOT.glob(MATERIAL_GLOB)))

ASSET_ROOT = ROOT / "_assets"
MATERIAL_ASSET_ROOT = ASSET_ROOT / "Materials" / "open_pbr_surface"
TEST_ROOT = ROOT / "surfaces" / "open_pbr_surface"
REFERENCE_ROOT = ROOT / "reference" / "surfaces" / "open_pbr_surface"
GEOMETRY_LAYER = ASSET_ROOT / "Geometry" / "shaderball.usda"
BASE_LAYER = ASSET_ROOT / "base.usda"
HDR_SOURCE = VIEWER_ROOT / "san_giuseppe_bridge_2k.hdr"
GLB_SOURCE = VIEWER_ROOT / "ShaderBall.glb"

IDEAL_MESH_SPHERE_RADIUS = 2.0
CAMERA_APERTURE_FOR_45_DEGREE_FOV = 41.421356


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


def main() -> None:
    _ensure_sources_exist()
    _copy_shared_assets()
    gltf = _read_glb(GLB_SOURCE)
    _write_geometry_layer(gltf, GEOMETRY_LAYER)
    _write_base_layer(BASE_LAYER)
    for material_source in MATERIAL_SOURCES:
        case_name = _material_case_name(material_source)
        _write_test_layer(TEST_ROOT / f"{case_name}.usda", material_source)
    _write_suite_config()
    _write_readme()


def _ensure_sources_exist() -> None:
    if not MATERIAL_SOURCES:
        raise FileNotFoundError(MATERIAL_ROOT / MATERIAL_GLOB)
    paths = (HDR_SOURCE, GLB_SOURCE) + tuple(
        path
        for material_source in MATERIAL_SOURCES
        for path in (material_source, material_source.with_name("materialx-osl.png"))
    )
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(path)
    for material_source in MATERIAL_SOURCES:
        for asset_source in _material_asset_sources(material_source):
            if not asset_source.is_file():
                raise FileNotFoundError(asset_source)


def _copy_shared_assets() -> None:
    (ASSET_ROOT / "Lights").mkdir(parents=True, exist_ok=True)
    (ASSET_ROOT / "Geometry").mkdir(parents=True, exist_ok=True)
    reference_root = REFERENCE_ROOT
    reference_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HDR_SOURCE, ASSET_ROOT / "Lights" / HDR_SOURCE.name)
    for material_source in MATERIAL_SOURCES:
        case_name = _material_case_name(material_source)
        reference_source = material_source.with_name("materialx-osl.png")
        reference_layer = reference_root / f"{case_name}_materialx-osl.png"
        shutil.copy2(reference_source, reference_layer)
        for asset_source in _material_asset_sources(material_source):
            relative_source = asset_source.relative_to(material_source.parent)
            destination = MATERIAL_ASSET_ROOT / case_name / relative_source
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(asset_source, destination)


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
    node_records: list[tuple[ET.Element, UsdShade.Shader, str | None]] = []
    node_graphs: dict[str, UsdShade.NodeGraph] = {}

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
        for output_element in node_def.getOutputs():
            shader.CreateOutput(
                output_element.getName(),
                _sdf_type_for_mtlx(output_element.getType()),
            )
        nodes[(scope, node_name)] = shader
        node_records.append((element, shader, scope))

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
        for element in graph_element:
            if element.tag == "output":
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


    for element, shader, scope in node_records:
        for input_element in element.findall("input"):
            name = input_element.attrib["name"]
            value_type = input_element.attrib["type"]
            shader_input = shader.CreateInput(name, _sdf_type_for_mtlx(value_type))
            if "value" in input_element.attrib:
                value = _parse_mtlx_input_value(
                    input_element, material_path, layer_path
                )
                shader_input.Set(value)
            elif "nodename" in input_element.attrib:
                source = nodes[(scope, input_element.attrib["nodename"])]
                output_name = input_element.attrib.get("output", "out")
                source_output = source.GetOutput(output_name)
                if not source_output:
                    source_output = source.CreateOutput(
                        output_name, _sdf_type_for_mtlx(value_type)
                    )
                shader_input.ConnectToSource(source_output)
            elif "nodegraph" in input_element.attrib:
                source_graph = node_graphs[input_element.attrib["nodegraph"]]
                source_output = source_graph.GetOutput(input_element.attrib["output"])
                shader_input.ConnectToSource(source_output)
            color_space = input_element.attrib.get("colorspace")
            if color_space:
                shader_input.GetAttr().SetColorSpace(color_space)

    for graph_element in root.findall("nodegraph"):
        graph_name = graph_element.attrib["name"]
        node_graph = node_graphs[graph_name]
        for output_element in graph_element.findall("output"):
            graph_output = node_graph.CreateOutput(
                output_element.attrib["name"],
                _sdf_type_for_mtlx(output_element.attrib["type"]),
            )
            source = nodes[(graph_name, output_element.attrib["nodename"])]
            output_name = output_element.attrib.get("output", "out")
            graph_output.ConnectToSource(source.GetOutput(output_name))

    surface_shader = nodes[(None, shader_element.attrib["name"])]
    surface_output = surface_shader.GetOutput("out")
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


def _parse_mtlx_input_value(
    input_element: ET.Element, material_path: Path, layer_path: Path
) -> Any:
    value_type = input_element.attrib["type"]
    value_text = input_element.attrib["value"]
    if value_type != "filename":
        return _parse_mtlx_value(value_type, value_text)

    source = (material_path.parent / value_text).resolve()
    relative_source = source.relative_to(material_path.parent)
    destination = (
        MATERIAL_ASSET_ROOT
        / _material_case_name(material_path)
        / relative_source
    )
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
    if value_type in {"float", "integer"}:
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
    if value_type == "filename":
        return Sdf.AssetPath(value_text)
    if value_type == "string":
        return value_text
    raise ValueError(f"unsupported MaterialX value type {value_type!r}")


def _sdf_type_for_mtlx(value_type: str) -> Sdf.ValueTypeName:
    if value_type == "surfaceshader":
        return Sdf.ValueTypeNames.Token
    if value_type == "float":
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
    if value_type == "filename":
        return Sdf.ValueTypeNames.Asset
    if value_type == "string":
        return Sdf.ValueTypeNames.String
    raise ValueError(f"unsupported MaterialX value type {value_type!r}")


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


def _write_suite_config() -> None:
    (ROOT / "typhoon-suite.toml").write_text(
        """[suite]
name = "material-fidelity"

[render]
args = ["--disableCameraLight"]
output_pattern = "material-fidelity/{path}.exr"

[reference]
dir = "reference"
pattern = "{path}_materialx-osl.png"
missing = "fail"

[comparison]
""",
        encoding="utf-8",
    )


def _write_readme() -> None:
    (ROOT / "README.md").write_text(
        f"""# Material Fidelity USD Render Test Suite

Generated from `~/code/material-samples`.

- Base layer: `_assets/base.usda`
- Geometry layer: `_assets/Geometry/shaderball.usda`
- IBL: `viewer/san_giuseppe_bridge_2k.hdr`
- Materials: `{MATERIAL_ROOT.relative_to(SOURCE_ROOT)}/{MATERIAL_GLOB}`
- Test cases: `surfaces/open_pbr_surface/*.usda`
- Render output pattern: `material-fidelity/<test-path>.exr`

The suite follows the `materialx/` layout, but shared scene assets are kept under
`_assets/`. Per-material USDAs sublayer `../../_assets/base.usda`, bind
`/World/Geometry`, define pure UsdShade material graphs under
`/Looks`, and over `/Render/Settings/Product` to set a unique output filename.

The checked-in USDA files are generated by:

```bash
pixi run python material-fidelity/generate_suite.py
```

Each reference image is copied from its source material's `materialx-osl.png`.
The scene framing follows the material-fidelity reference renderer: center
`ShaderBall.glb`, scale it to bounding-sphere radius `2.0`, and render with a
45 degree camera at `(0, 0, 5)` looking at the origin. With no suite or per-case
override, comparisons use the test runner's built-in FLIP threshold of `0.04`.
""",
        encoding="utf-8",
    )


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
