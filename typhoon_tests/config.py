from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
import tomllib


SUITE_CONFIG_NAME = "typhoon-suite.toml"


@dataclass(frozen=True)
class SuiteConfig:
    root: Path
    name: str
    render_output_pattern: str = "{stem}.exr"
    artifact_dir: str = "comparison"
    reference_dir: str | None = None
    reference_pattern: str = "{stem}.png"
    default_flip_threshold: float | None = None
    missing_references: str = "allow"
    tonemap: str = "clamp"
    transfer: str = "linear-to-srgb"
    render_args: tuple[str, ...] = ()
    skip: dict[str, str] = field(default_factory=dict)
    xfail: dict[str, str] = field(default_factory=dict)
    thresholds: dict[str, float | None] = field(default_factory=dict)


@dataclass(frozen=True)
class CaseConfig:
    skip: str | None = None
    xfail: str | None = None
    flip_threshold: float | None = None
    render_output: str | None = None
    reference: str | None = None
    render_args: tuple[str, ...] = ()


def find_suite_config(path: Path) -> Path | None:
    for parent in (path.parent, *path.parents):
        candidate = parent / SUITE_CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


@lru_cache(maxsize=None)
def load_suite_config_for_path(path_text: str) -> SuiteConfig:
    path = Path(path_text).resolve()
    config_path = find_suite_config(path)
    if config_path is None:
        return SuiteConfig(root=path.parent, name=path.parent.name or "default")

    with config_path.open("rb") as file:
        data = tomllib.load(file)

    root = config_path.parent
    suite = _table(data, "suite")
    render = _table(data, "render")
    reference = _table(data, "reference")
    comparison = _table(data, "comparison")

    name = _string(suite.get("name"), root.name or "default")
    reference_dir = _optional_string(
        reference.get("dir", suite.get("reference_dir"))
    )
    if reference_dir:
        reference_dir = str(_resolve_path(root, reference_dir))

    return SuiteConfig(
        root=root,
        name=name,
        render_output_pattern=_string(
            render.get("output_pattern", suite.get("render_output_pattern")),
            "{stem}.exr",
        ),
        artifact_dir=_string(
            comparison.get("artifact_dir", suite.get("artifact_dir")),
            "comparison",
        ),
        reference_dir=reference_dir,
        reference_pattern=_string(
            reference.get("pattern", suite.get("reference_pattern")),
            "{stem}.png",
        ),
        default_flip_threshold=_optional_float(
            comparison.get(
                "default_flip_threshold",
                suite.get("default_flip_threshold"),
            )
        ),
        missing_references=_string(
            reference.get("missing", suite.get("missing_references")),
            "allow",
        ),
        tonemap=_string(comparison.get("tonemap"), "clamp"),
        transfer=_string(comparison.get("transfer"), "linear-to-srgb"),
        render_args=tuple(_string_list(render.get("args", []))),
        skip=_string_map(data.get("skip", {})),
        xfail=_string_map(data.get("xfail", {})),
        thresholds=_threshold_map(data.get("thresholds", {})),
    )


def load_case_config(path: Path) -> CaseConfig:
    data: dict[str, Any] = {}
    for candidate in (
        path.with_suffix(".typhoon.toml"),
        path.with_name(path.name + ".typhoon.toml"),
    ):
        if candidate.is_file():
            with candidate.open("rb") as file:
                data = tomllib.load(file)
            break

    test = _table(data, "test")
    render = _table(data, "render")
    reference = _table(data, "reference")
    comparison = _table(data, "comparison")

    return CaseConfig(
        skip=_optional_string(test.get("skip", data.get("skip"))),
        xfail=_optional_string(test.get("xfail", data.get("xfail"))),
        flip_threshold=_optional_float(
            comparison.get("flip_threshold", data.get("flip_threshold"))
        ),
        render_output=_optional_string(
            render.get("output", data.get("render_output"))
        ),
        reference=_optional_string(reference.get("path", data.get("reference"))),
        render_args=tuple(_string_list(render.get("args", data.get("render_args", [])))),
    )


def lookup_case_value(mapping: dict[str, Any], path: Path, suite_root: Path) -> Any:
    rel = path.relative_to(suite_root).as_posix()
    for key in (rel, path.name, path.stem):
        if key in mapping:
            return mapping[key]
    return None


def format_pattern(pattern: str, path: Path, suite: SuiteConfig) -> str:
    return pattern.format(
        stem=path.stem,
        name=path.name,
        suffix=path.suffix,
        suite=suite.name,
    )


def _resolve_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    return value if isinstance(value, dict) else {}


def _string(value: Any, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise TypeError(f"expected string, got {type(value).__name__}")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"expected string, got {type(value).__name__}")
    return value


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    raise TypeError(f"expected number, got {type(value).__name__}")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise TypeError(f"expected list, got {type(value).__name__}")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"expected string list item, got {type(item).__name__}")
        result.append(item)
    return result


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TypeError(f"expected table, got {type(value).__name__}")
    result: dict[str, str] = {}
    for key, item in value.items():
        if item is True:
            result[str(key)] = "marked in suite config"
        elif isinstance(item, str):
            result[str(key)] = item
        else:
            raise TypeError(
                f"expected skip/xfail value for {key!r} to be string or true"
            )
    return result


def _threshold_map(value: Any) -> dict[str, float | None]:
    if not isinstance(value, dict):
        raise TypeError(f"expected table, got {type(value).__name__}")
    result: dict[str, float | None] = {}
    for key, item in value.items():
        result[str(key)] = _optional_float(item)
    return result
