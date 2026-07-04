from __future__ import annotations

import math
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
EXPECTED_RESOLUTION = (512, 512)


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

