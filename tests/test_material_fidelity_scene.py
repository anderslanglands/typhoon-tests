from __future__ import annotations

import math
import re
import runpy
from pathlib import Path

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom


ROOT = Path(__file__).resolve().parents[1]
MATERIAL_FIDELITY = ROOT / "material-fidelity"
TEST_SCENE = (
    MATERIAL_FIDELITY
    / "surfaces"
    / "open_pbr_surface"
    / "open_pbr_surface_sweep_base_metalness_0_50.usda"
)
CAMERA_PATH = Sdf.Path("/World/Camera")
EXPECTED_SHADERBALL_RADIUS = 2.0
EXPECTED_FOV_DEGREES = 45.0
EXPECTED_RESOLUTION = (256, 256)
EXPECTED_CASE_COUNTS = {
    "nodes/adjustment": 19,
    "nodes/application": 2,
    "nodes/channel": 47,
    "nodes/compositing": 9,
    "nodes/conditional": 6,
    "nodes/geometric": 77,
    "nodes/logical": 4,
    "nodes/math": 131,
    "nodes/noise": 102,
    "nodes/patterns": 13,
    "nodes/pbr": 29,
    "nodes/procedurals": 20,
    "nodes/textures": 45,
    "showcase/gltf_pbr": 5,
    "showcase/open_pbr_surface": 8,
    "showcase/standard_surface": 14,
    "surfaces/gltf_pbr": 77,
    "surfaces/open_pbr_surface": 78,
    "surfaces/standard_surface": 103,
}
ASSET_PATH_PATTERN = re.compile(r"@([^@]+)@")


def _vec3_tuple(value: Gf.Vec3d) -> tuple[float, float, float]:
    return (float(value[0]), float(value[1]), float(value[2]))


def _assert_vec3_close(actual: Gf.Vec3d, expected: Gf.Vec3d) -> None:
    assert _vec3_tuple(actual) == pytest.approx(_vec3_tuple(expected), abs=1e-6)


def _merge_bounds(
    bounds: tuple[Gf.Vec3d, Gf.Vec3d] | None, point: Gf.Vec3d
) -> tuple[Gf.Vec3d, Gf.Vec3d]:
    if bounds is None:
        return point, point
    bounds_min, bounds_max = bounds
    return (
        Gf.Vec3d(*(min(bounds_min[axis], point[axis]) for axis in range(3))),
        Gf.Vec3d(*(max(bounds_max[axis], point[axis]) for axis in range(3))),
    )


def _mesh_point_bounds(stage: Usd.Stage, root_path: str) -> tuple[Gf.Vec3d, Gf.Vec3d]:
    root = stage.GetPrimAtPath(root_path)
    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    bounds: tuple[Gf.Vec3d, Gf.Vec3d] | None = None

    for prim in Usd.PrimRange(root):
        if not prim.IsA(UsdGeom.Mesh):
            continue
        points = UsdGeom.Mesh(prim).GetPointsAttr().Get() or []
        local_to_world = xform_cache.GetLocalToWorldTransform(prim)
        for point in points:
            bounds = _merge_bounds(bounds, local_to_world.Transform(Gf.Vec3d(*point)))

    assert bounds is not None
    return bounds


def _normalized(value: Gf.Vec3d) -> Gf.Vec3d:
    length = value.GetLength()
    assert length > 0
    return Gf.Vec3d(value[0] / length, value[1] / length, value[2] / length)


def _fov_degrees(aperture: float, focal_length: float) -> float:
    return math.degrees(2.0 * math.atan(aperture / (2.0 * focal_length)))


def _material_case_paths() -> list[Path]:
    return sorted(
        path
        for path in MATERIAL_FIDELITY.rglob("*.usda")
        if "_assets" not in path.parts
    )


def test_material_fidelity_case_inventory_matches_source_references() -> None:
    counts = {}
    for path in _material_case_paths():
        relative = path.relative_to(MATERIAL_FIDELITY)
        section = relative.parent.as_posix()
        counts[section] = counts.get(section, 0) + 1

    assert counts == EXPECTED_CASE_COUNTS


def test_material_fidelity_cases_have_unique_outputs_and_resolved_assets() -> None:
    product_names = set()
    for path in _material_case_paths():
        text = path.read_text(encoding="utf-8")
        relative_stem = path.relative_to(MATERIAL_FIDELITY).with_suffix("").as_posix()
        product_name = f"material-fidelity/{relative_stem}.exr"

        assert f'token productName = "{product_name}"' in text
        assert product_name not in product_names
        product_names.add(product_name)

        assert "string source = \"materials/" in text
        assert text.count(".mtlx") == 1
        assert ".mtlx@" not in text

        for asset_path in ASSET_PATH_PATTERN.findall(text):
            resolved_path = (path.parent / asset_path).resolve()
            assert resolved_path.is_file(), f"{path}: unresolved asset {asset_path}"


def test_material_fidelity_conversion_preserves_nontrivial_graph_semantics() -> None:
    artistic_ior = (
        MATERIAL_FIDELITY / "nodes" / "pbr" / "artistic_ior.usda"
    ).read_text(encoding="utf-8")
    assert "aluminum_ior_tint.outputs:ior>" in artistic_ior
    assert "aluminum_ior_tint.outputs:out>" not in artistic_ior

    standard_ior = (
        MATERIAL_FIDELITY / "surfaces" / "standard_surface" / "ior.usda"
    ).read_text(encoding="utf-8")
    assert "float inputs:specular_IOR = 2.4" in standard_ior
    assert "inputs:ior" not in standard_ior

    opacity = (
        MATERIAL_FIDELITY / "surfaces" / "standard_surface" / "opacity.usda"
    ).read_text(encoding="utf-8")
    assert "color3f inputs:opacity = (0.5, 0.5, 0.5)" in opacity

    thin_film = (
        MATERIAL_FIDELITY
        / "surfaces"
        / "standard_surface"
        / "thin_film_ior_clamp.usda"
    ).read_text(encoding="utf-8")
    assert "float inputs:thin_film_IOR = 3.5" in thin_film

    normal_scale = (
        MATERIAL_FIDELITY
        / "nodes"
        / "textures"
        / "gltf_normalmap_isolate_scale_float.usda"
    ).read_text(encoding="utf-8")
    assert "float2 inputs:scale = (1.3, 1.3)" in normal_scale

    standard_subsurface_scale = (
        MATERIAL_FIDELITY
        / "surfaces"
        / "standard_surface"
        / "input_subsurface_scale.usda"
    ).read_text(encoding="utf-8")
    assert "float inputs:subsurface_scale = 0.022" in standard_subsurface_scale

    standard_subsurface_radius = (
        MATERIAL_FIDELITY
        / "surfaces"
        / "standard_surface"
        / "input_subsurface_radius.usda"
    ).read_text(encoding="utf-8")
    assert (
        "color3f inputs:subsurface_radius = (1.5, 0.7, 0.3)"
        in standard_subsurface_radius
    )
    assert "float inputs:subsurface_scale = 0.01" in standard_subsurface_radius

    transmission_scatter = (
        MATERIAL_FIDELITY
        / "surfaces"
        / "standard_surface"
        / "input_transmission_scatter.usda"
    ).read_text(encoding="utf-8")
    assert "float inputs:transmission_depth = 0.05" in transmission_scatter
    assert (
        "color3f inputs:transmission_scatter = (0.5, 0.2, 0.1)"
        in transmission_scatter
    )

    transmission_scatter_anisotropy = (
        MATERIAL_FIDELITY
        / "surfaces"
        / "standard_surface"
        / "input_transmission_scatter_anisotropy.usda"
    ).read_text(encoding="utf-8")
    assert "float inputs:transmission_depth = 0.05" in transmission_scatter_anisotropy
    assert (
        "float inputs:transmission_scatter_anisotropy = 0.9"
        in transmission_scatter_anisotropy
    )

    transmission_depth = (
        MATERIAL_FIDELITY
        / "surfaces"
        / "standard_surface"
        / "input_transmission_depth.usda"
    ).read_text(encoding="utf-8")
    assert (
        "color3f inputs:transmission_color = (0.25, 0.55, 0.9)"
        in transmission_depth
    )
    assert "float inputs:transmission_depth = 0.05" in transmission_depth

    open_pbr_subsurface = (
        MATERIAL_FIDELITY
        / "surfaces"
        / "open_pbr_surface"
        / "feature_subsurface.usda"
    ).read_text(encoding="utf-8")
    assert "float inputs:subsurface_radius = 0.01" in open_pbr_subsurface

    open_pbr_subsurface_radius = (
        MATERIAL_FIDELITY
        / "surfaces"
        / "open_pbr_surface"
        / "input_subsurface_radius.usda"
    ).read_text(encoding="utf-8")
    assert "float inputs:subsurface_radius = 0.02" in open_pbr_subsurface_radius

    open_pbr_transmission_scatter = (
        MATERIAL_FIDELITY
        / "surfaces"
        / "open_pbr_surface"
        / "input_transmission_scatter_anisotropy.usda"
    ).read_text(encoding="utf-8")
    assert "float inputs:transmission_depth = 0.05" in open_pbr_transmission_scatter
    assert (
        "float inputs:transmission_scatter_anisotropy = 0.85"
        in open_pbr_transmission_scatter
    )

    compound_graph = (
        MATERIAL_FIDELITY
        / "showcase"
        / "standard_surface"
        / "brick_procedural.usda"
    ).read_text(encoding="utf-8")
    assert "NG_BrickPattern.inputs:brick_color>" in compound_graph

    matrix_graph = (
        MATERIAL_FIDELITY
        / "nodes"
        / "math"
        / "matrix33_transformmatrix_2d_scale.usda"
    ).read_text(encoding="utf-8")
    assert (
        "matrix3d inputs:mat = ( (1.6, 0, 0), (0, 0.7, 0), (0, 0, 1) )"
        in matrix_graph
    )

    switch = (
        MATERIAL_FIDELITY / "nodes" / "conditional" / "switch.usda"
    ).read_text(encoding="utf-8")
    assert "float inputs:which.connect = </Looks/switch/selector_float.outputs:out>" in switch
    assert 'uniform token info:id = "ND_floor_float"' in switch
    assert "color3f inputs:in10 = (0.2500, 0.8500, 0.4500)" in switch

    texture_opacity = (
        MATERIAL_FIDELITY
        / "surfaces"
        / "standard_surface"
        / "texture_opacity.usda"
    ).read_text(encoding="utf-8")
    assert (
        "inputs:opacity.connect = </Looks/texture_opacity/convert_1.outputs:out>"
        in texture_opacity
    )
    assert 'uniform token info:id = "ND_convert_float_color3"' in texture_opacity


def test_material_fidelity_connection_types_match_except_invalid_cases() -> None:
    mismatches = []
    for path in _material_case_paths():
        stage = Usd.Stage.Open(str(path), load=Usd.Stage.LoadNone)
        looks = stage.GetPrimAtPath("/Looks")
        assert looks
        for prim in Usd.PrimRange(looks):
            for attribute in prim.GetAttributes():
                for source_path in attribute.GetConnections():
                    source = stage.GetAttributeAtPath(source_path)
                    assert source, f"{path}: missing connection target {source_path}"
                    if attribute.GetTypeName() != source.GetTypeName():
                        mismatches.append(
                            (
                                path.relative_to(MATERIAL_FIDELITY).as_posix(),
                                attribute.GetPath(),
                                attribute.GetTypeName(),
                                source.GetPath(),
                                source.GetTypeName(),
                            )
                        )

    assert mismatches
    assert all(
        Path(path).stem.startswith("convert_invalid_implicit_")
        for path, *_ in mismatches
    ), mismatches


def test_material_fidelity_composed_scene_matches_reference_camera() -> None:
    stage = Usd.Stage.Open(str(TEST_SCENE))
    camera = UsdGeom.Camera(stage.GetPrimAtPath(CAMERA_PATH))

    assert camera.GetProjectionAttr().Get() == UsdGeom.Tokens.perspective
    assert tuple(camera.GetClippingRangeAttr().Get()) == pytest.approx((0.05, 1000.0))
    assert _fov_degrees(
        camera.GetHorizontalApertureAttr().Get(), camera.GetFocalLengthAttr().Get()
    ) == pytest.approx(EXPECTED_FOV_DEGREES, abs=1e-5)
    assert _fov_degrees(
        camera.GetVerticalApertureAttr().Get(), camera.GetFocalLengthAttr().Get()
    ) == pytest.approx(EXPECTED_FOV_DEGREES, abs=1e-5)

    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    camera_to_world = xform_cache.GetLocalToWorldTransform(camera.GetPrim())
    camera_position = camera_to_world.Transform(Gf.Vec3d(0.0, 0.0, 0.0))
    camera_right_raw = camera_to_world.Transform(Gf.Vec3d(1.0, 0.0, 0.0)) - camera_position
    camera_up_raw = camera_to_world.Transform(Gf.Vec3d(0.0, 1.0, 0.0)) - camera_position
    camera_forward_raw = (
        camera_to_world.Transform(Gf.Vec3d(0.0, 0.0, -1.0)) - camera_position
    )
    camera_right = _normalized(camera_right_raw)
    camera_up = _normalized(camera_up_raw)
    camera_forward = _normalized(camera_forward_raw)
    target_direction = _normalized(Gf.Vec3d(0.0, 0.0, 0.0) - camera_position)

    assert camera_right_raw.GetLength() == pytest.approx(1.0, abs=1e-6)
    assert camera_up_raw.GetLength() == pytest.approx(1.0, abs=1e-6)
    assert camera_forward_raw.GetLength() == pytest.approx(1.0, abs=1e-6)
    assert Gf.Dot(camera_right, camera_up) == pytest.approx(0.0, abs=1e-6)
    assert Gf.Dot(camera_right, camera_forward) == pytest.approx(0.0, abs=1e-6)
    assert Gf.Dot(camera_up, camera_forward) == pytest.approx(0.0, abs=1e-6)

    _assert_vec3_close(camera_position, Gf.Vec3d(0.0, 0.0, 5.0))
    _assert_vec3_close(camera_right, Gf.Vec3d(1.0, 0.0, 0.0))
    _assert_vec3_close(camera_forward, target_direction)
    _assert_vec3_close(camera_up, Gf.Vec3d(0.0, 1.0, 0.0))

    settings_path = Sdf.Path("/Render/Settings")
    product_path = Sdf.Path("/Render/Settings/Product")
    settings = stage.GetPrimAtPath(settings_path)
    product = stage.GetPrimAtPath(product_path)
    assert stage.GetMetadata("renderSettingsPrimPath") == settings_path
    assert settings.GetRelationship("camera").GetTargets() == [CAMERA_PATH]
    assert settings.GetRelationship("products").GetTargets() == [product_path]
    assert product.GetRelationship("camera").GetTargets() == [CAMERA_PATH]
    assert tuple(settings.GetAttribute("resolution").Get()) == EXPECTED_RESOLUTION


def test_material_fidelity_composed_scene_normalizes_shaderball() -> None:
    stage = Usd.Stage.Open(str(TEST_SCENE))
    bounds_min, bounds_max = _mesh_point_bounds(stage, "/World/Geometry")
    center = (bounds_min + bounds_max) * 0.5
    radius = (bounds_max - bounds_min).GetLength() * 0.5

    _assert_vec3_close(center, Gf.Vec3d(0.0, 0.0, 0.0))
    assert radius == pytest.approx(EXPECTED_SHADERBALL_RADIUS, abs=1e-6)


def test_material_fidelity_generator_writes_base_resolution(tmp_path: Path) -> None:
    generator = runpy.run_path(str(MATERIAL_FIDELITY / "generate_suite.py"))
    base_layer = tmp_path / "_assets" / "base.usda"

    generator["_write_base_layer"](base_layer)

    stage = Usd.Stage.Open(str(base_layer))
    settings = stage.GetPrimAtPath("/Render/Settings")
    assert tuple(settings.GetAttribute("resolution").Get()) == EXPECTED_RESOLUTION


def test_material_fidelity_generator_tracks_png_until_exr_exists(
    tmp_path: Path,
) -> None:
    generator = runpy.run_path(str(MATERIAL_FIDELITY / "generate_suite.py"))
    output_root = tmp_path / "output"
    material_root = tmp_path / "source" / "materials"
    source = material_root / "nodes" / "case" / "case.mtlx"
    source.parent.mkdir(parents=True)
    source.write_text("<materialx />\n", encoding="utf-8")
    source_png = source.with_name("materialx-osl.png")
    source_png.write_bytes(b"source-png")
    hdr_source = tmp_path / "source" / "viewer" / "light.hdr"
    hdr_source.parent.mkdir(parents=True)
    hdr_source.write_bytes(b"hdr")

    case_type = generator["MaterialCase"]
    case = case_type(
        source=source,
        test_path=output_root / "nodes" / "noise" / "case.usda",
        reference_path=(
            output_root
            / "reference"
            / "nodes"
            / "noise"
            / "case_materialx-osl.png"
        ),
    )
    copy_assets = generator["_copy_shared_assets"]
    copy_assets.__globals__.update(
        {
            "ROOT": output_root,
            "MATERIAL_ROOT": material_root,
            "ASSET_ROOT": output_root / "_assets",
            "HDR_SOURCE": hdr_source,
            "_material_asset_sources": lambda _source: (),
        }
    )

    copy_assets((case,), force=False)

    config_path = case.test_path.with_suffix(".typhoon.toml")
    assert case.reference_path.read_bytes() == b"source-png"
    assert config_path.read_text(encoding="utf-8") == (
        '[reference]\npath = "reference/nodes/noise/case_materialx-osl.png"\n'
    )

    source_png.write_bytes(b"forced-source-png")
    copy_assets((case,), force=True)
    assert case.reference_path.read_bytes() == b"forced-source-png"
    assert config_path.is_file()

    config_path.unlink()
    copy_assets((case,), force=False)
    assert config_path.is_file()

    config_path.unlink()
    case.reference_path.with_name("case.exr").write_bytes(b"updated-exr")
    source_png.write_bytes(b"newer-source-png")
    copy_assets((case,), force=True)

    assert case.reference_path.read_bytes() == b"forced-source-png"
    assert not config_path.exists()
