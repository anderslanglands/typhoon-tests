from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import zipfile

from .config import SUITE_CONFIG_NAME, load_suite_config_for_path
from .pytest_plugin import RunContext, TyphoonOptions, build_cases, resolve_reference


MANIFEST_NAME = "reference-releases.json"
STATE_NAME = ".typhoon-reference-state.json"
SCHEMA_VERSION = 1
IMAGE_SUFFIXES = {
    ".bmp",
    ".exr",
    ".gif",
    ".hdr",
    ".jpeg",
    ".jpg",
    ".png",
    ".tga",
    ".tif",
    ".tiff",
    ".webp",
}
IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".pixi",
    "_output",
    "__pycache__",
    "comparison",
    "reference",
    "renders",
}


class ReferenceArchiveError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(data: object) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def manifest_digest(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(manifest)).hexdigest()


def load_manifest(project_root: Path) -> dict[str, Any] | None:
    path = project_root / MANIFEST_NAME
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ReferenceArchiveError(f"unsupported reference manifest: {path}")
    if not isinstance(data.get("repository"), str) or not isinstance(data.get("groups"), dict):
        raise ReferenceArchiveError(f"invalid reference manifest: {path}")
    return data


def write_json_atomic(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(canonical_json(data))
    os.replace(temporary, path)


def discover_suite_roots(project_root: Path) -> list[Path]:
    roots = []
    for config_path in project_root.rglob(SUITE_CONFIG_NAME):
        relative = config_path.relative_to(project_root)
        if any(
            part.startswith("_") or part in IGNORED_DIRECTORY_NAMES
            for part in relative.parts[:-1]
        ):
            continue
        roots.append(config_path.parent.resolve())
    return sorted(roots, key=lambda path: path.relative_to(project_root).as_posix())


def _reference_options(project_root: Path) -> TyphoonOptions:
    return TyphoonOptions(
        provider=None,
        run_context=RunContext(
            output_base=project_root / "_output",
            run_dir=project_root / "_output" / "reference-discovery",
            run_number=0,
            started_at="",
        ),
        reference_dir=None,
        require_references=False,
        require_thresholds=False,
        dry_run=True,
    )


def discover_reference_groups(
    project_root: Path,
) -> tuple[dict[str, list[Path]], list[Path]]:
    project_root = project_root.resolve()
    options = _reference_options(project_root)
    groups: dict[str, set[Path]] = defaultdict(set)
    reference_roots: set[Path] = set()
    claimed_paths: dict[Path, str] = {}

    for suite_root in discover_suite_roots(project_root):
        config_path = suite_root / SUITE_CONFIG_NAME
        for usd_path in sorted(suite_root.rglob("*.usda")):
            relative = usd_path.relative_to(suite_root)
            if any(
                part.startswith("_") or part in IGNORED_DIRECTORY_NAMES
                for part in relative.parts[:-1]
            ):
                continue
            suite = load_suite_config_for_path(str(usd_path.resolve()))
            if suite.root.resolve() != suite_root or not config_path.is_file():
                continue
            if suite.reference_dir:
                reference_roots.add(Path(suite.reference_dir).resolve())
            for case in build_cases(usd_path):
                if case.skip:
                    continue
                reference = resolve_reference(case, options)
                if reference is None:
                    continue
                reference = reference.resolve()
                try:
                    reference.relative_to(project_root)
                except ValueError as exc:
                    raise ReferenceArchiveError(
                        f"reference is outside the repository: {reference}"
                    ) from exc
                if reference.suffix.lower() not in IMAGE_SUFFIXES:
                    raise ReferenceArchiveError(f"unsupported reference image: {reference}")

                reference_root = (
                    Path(suite.reference_dir).resolve()
                    if suite.reference_dir
                    else reference.parent
                )
                try:
                    section = reference.parent.relative_to(reference_root).as_posix()
                except ValueError:
                    section = reference.parent.relative_to(suite_root).as_posix()
                group_id = f"{suite.name}/{section}" if section != "." else suite.name
                previous = claimed_paths.get(reference)
                if previous is not None and previous != group_id:
                    raise ReferenceArchiveError(
                        "reference belongs to multiple archive groups: "
                        f"{reference} ({previous}, {group_id})"
                    )
                claimed_paths[reference] = group_id
                groups[group_id].add(reference)

    return (
        {group: sorted(paths) for group, paths in sorted(groups.items())},
        sorted(reference_roots),
    )


def file_record(path: Path, project_root: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ReferenceArchiveError(f"reference image is missing: {path}")
    return {
        "path": path.relative_to(project_root).as_posix(),
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
    }


def build_archive(
    project_root: Path,
    group_id: str,
    paths: Iterable[Path],
    output_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    records = [file_record(path, project_root) for path in paths]
    group_hash = hashlib.sha256(canonical_json(records)).hexdigest()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "--", group_id).strip("-") or "references"
    archive_path = output_dir / f"{slug}--{group_hash[:12]}.zip"
    output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for record in records:
            info = zipfile.ZipInfo(record["path"], date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (project_root / record["path"]).read_bytes())
    return archive_path, {
        "archive": archive_path.name,
        "archive_sha256": sha256_file(archive_path),
        "archive_size": archive_path.stat().st_size,
        "files": records,
    }


def release_asset_url(repository: str, release: str, archive: str) -> str:
    return (
        f"https://github.com/{repository}/releases/download/"
        f"{quote(release, safe='')}/{quote(archive, safe='')}"
    )


def _download(url: str, destination: Path) -> None:
    request = Request(url, headers={"User-Agent": "typhoon-tests-reference-sync/1"})
    try:
        with urlopen(request, timeout=120) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)
    except (HTTPError, URLError) as exc:
        raise ReferenceArchiveError(f"failed to download {url}: {exc}") from exc


def _state_files(state: dict[str, Any] | None) -> dict[str, str]:
    if state is None:
        return {}
    files = state.get("files", {})
    if not isinstance(files, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in files.items()
    ):
        raise ReferenceArchiveError(f"invalid hydration state in {STATE_NAME}")
    return files


def load_state(project_root: Path) -> dict[str, Any] | None:
    path = project_root / STATE_NAME
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ReferenceArchiveError(f"invalid hydration state: {path}")
    return data


def _manifest_files(manifest: dict[str, Any]) -> dict[str, str]:
    files: dict[str, str] = {}
    for group_id, group in manifest["groups"].items():
        records = group.get("files")
        if not isinstance(records, list):
            raise ReferenceArchiveError(f"invalid file list for reference group {group_id}")
        for record in records:
            path = record.get("path") if isinstance(record, dict) else None
            digest = record.get("sha256") if isinstance(record, dict) else None
            if not isinstance(path, str) or not isinstance(digest, str):
                raise ReferenceArchiveError(f"invalid file record for reference group {group_id}")
            if path in files and files[path] != digest:
                raise ReferenceArchiveError(f"conflicting reference file in manifest: {path}")
            files[path] = digest
    return files


def _validate_group_metadata(group_id: str, group: object) -> dict[str, Any]:
    if not isinstance(group, dict):
        raise ReferenceArchiveError(f"invalid reference group: {group_id}")
    archive = group.get("archive")
    if (
        not isinstance(archive, str)
        or not archive
        or Path(archive).name != archive
        or Path(archive).suffix != ".zip"
    ):
        raise ReferenceArchiveError(f"unsafe archive name for {group_id}: {archive!r}")
    if not isinstance(group.get("release"), str) or not group["release"]:
        raise ReferenceArchiveError(f"invalid release name for {group_id}")
    digest = group.get("archive_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ReferenceArchiveError(f"invalid archive checksum for {group_id}")
    size = group.get("archive_size")
    if not isinstance(size, int) or size < 0:
        raise ReferenceArchiveError(f"invalid archive size for {group_id}")
    return group


def _validate_manifest_paths(project_root: Path, manifest: dict[str, Any]) -> None:
    discovered, reference_roots = discover_reference_groups(project_root)
    expected = {path.resolve() for paths in discovered.values() for path in paths}
    actual = {
        (project_root / relative).resolve()
        for relative in _manifest_files(manifest)
    }
    allowed = {
        path
        for path in actual
        if any(path.is_relative_to(root) for root in reference_roots)
    }
    if actual != allowed:
        unsafe = sorted(actual - allowed, key=str)[0]
        raise ReferenceArchiveError(
            f"manifest reference is outside configured reference directories: {unsafe}"
        )
    if not actual.issubset(expected):
        extra = sorted(actual - expected, key=str)
        details = []
        if extra:
            details.append(f"unexpected {extra[0].relative_to(project_root)}")
        raise ReferenceArchiveError(
            "manifest does not match collected tests: " + ", ".join(details)
        )


def _validate_local_state(project_root: Path, state: dict[str, Any], force: bool) -> None:
    if force:
        return
    modified = []
    for relative, expected in _state_files(state).items():
        path = project_root / relative
        if not path.is_file() or sha256_file(path) != expected:
            modified.append(relative)
    if modified:
        sample = "\n".join(f"  {path}" for path in modified[:10])
        raise ReferenceArchiveError(
            "local references differ from the last download; publish them with "
            f"`pixi run update-references` or use --force to discard them:\n{sample}"
        )


def _safe_archive_records(
    group_id: str, group: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for record in group.get("files", []):
        if not isinstance(record, dict):
            raise ReferenceArchiveError(f"invalid file record for {group_id}")
        relative = record.get("path")
        if not isinstance(relative, str):
            raise ReferenceArchiveError(f"invalid archive path for {group_id}")
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or relative in records:
            raise ReferenceArchiveError(f"unsafe archive path for {group_id}: {relative}")
        if not isinstance(record.get("sha256"), str) or not isinstance(
            record.get("size"), int
        ):
            raise ReferenceArchiveError(f"invalid checksum record for {group_id}")
        records[relative] = record
    return records


def hydrate_references(project_root: Path, *, force: bool = False) -> int:
    project_root = project_root.resolve()
    manifest = load_manifest(project_root)
    if manifest is None:
        raise ReferenceArchiveError(f"missing {MANIFEST_NAME}")
    _validate_manifest_paths(project_root, manifest)
    state = load_state(project_root)
    if state is not None:
        _validate_local_state(project_root, state, force)
    elif not force:
        existing = [
            path
            for path in _manifest_files(manifest)
            if (project_root / path).exists()
        ]
        if existing:
            raise ReferenceArchiveError(
                f"references exist without {STATE_NAME}; use --force to replace them"
            )

    expected_files = _manifest_files(manifest)
    downloaded = 0
    with tempfile.TemporaryDirectory(prefix="typhoon-reference-download-") as temp_name:
        temporary = Path(temp_name)
        staged = temporary / "files"
        for group_id, group in sorted(manifest["groups"].items()):
            group = _validate_group_metadata(group_id, group)
            records = _safe_archive_records(group_id, group)
            if all(
                (project_root / relative).is_file()
                and sha256_file(project_root / relative) == record["sha256"]
                for relative, record in records.items()
            ):
                continue
            archive_path = temporary / group["archive"]
            url = release_asset_url(manifest["repository"], group["release"], group["archive"])
            print(f"Downloading {group_id} ({group['archive_size']} bytes)")
            _download(url, archive_path)
            if sha256_file(archive_path) != group["archive_sha256"]:
                raise ReferenceArchiveError(f"archive checksum mismatch: {group_id}")
            with zipfile.ZipFile(archive_path) as archive:
                member_list = [
                    info for info in archive.infolist() if not info.is_dir()
                ]
                members = {info.filename: info for info in member_list}
                if len(members) != len(member_list):
                    raise ReferenceArchiveError(
                        f"archive contains duplicate paths: {group_id}"
                    )
                if set(members) != set(records):
                    raise ReferenceArchiveError(
                        f"archive contents do not match manifest: {group_id}"
                    )
                for relative, record in records.items():
                    destination = staged / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(members[relative]) as source, destination.open(
                        "wb"
                    ) as output:
                        while chunk := source.read(1024 * 1024):
                            output.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                    if size != record["size"] or digest.hexdigest() != record["sha256"]:
                        raise ReferenceArchiveError(f"reference checksum mismatch: {relative}")
            downloaded += 1

        old_files = set(_state_files(state)) if state else set()
        replacements = {
            relative: staged / relative
            for relative in expected_files
            if (staged / relative).is_file()
        }
        removals = old_files - set(expected_files)
        touched = {project_root / relative for relative in removals}
        touched.update(project_root / relative for relative in replacements)
        backups = _backup_paths(touched, temporary / "backups")
        old_state_bytes = (
            (project_root / STATE_NAME).read_bytes()
            if (project_root / STATE_NAME).is_file()
            else None
        )
        try:
            for relative in sorted(removals):
                (project_root / relative).unlink(missing_ok=True)
            for relative, source in sorted(replacements.items()):
                destination = project_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary_destination = destination.with_name(
                    f".{destination.name}.tmp"
                )
                shutil.copyfile(source, temporary_destination)
                os.replace(temporary_destination, destination)
            write_json_atomic(
                project_root / STATE_NAME,
                {
                    "schema_version": SCHEMA_VERSION,
                    "manifest_sha256": manifest_digest(manifest),
                    "files": expected_files,
                },
            )
        except Exception:
            _restore_paths(backups)
            _restore_optional_file(project_root / STATE_NAME, old_state_bytes)
            raise
    print(
        f"References are current ({len(expected_files)} files, "
        f"{downloaded} archives downloaded)"
    )
    return downloaded


def repository_from_remote(project_root: Path) -> str:
    completed = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    remote = completed.stdout.strip()
    match = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?$", remote)
    if not match:
        raise ReferenceArchiveError(f"origin is not a GitHub repository: {remote}")
    return match.group(1)


def _run(command: list[str], project_root: Path) -> None:
    subprocess.run(command, cwd=project_root, check=True)


def _backup_paths(paths: set[Path], backup_root: Path) -> dict[Path, Path | None]:
    backup_root.mkdir(parents=True, exist_ok=True)
    backups: dict[Path, Path | None] = {}
    for index, path in enumerate(sorted(paths, key=str)):
        if path.is_file():
            backup = backup_root / str(index)
            shutil.copy2(path, backup)
            backups[path] = backup
        else:
            backups[path] = None
    return backups


def _restore_paths(backups: dict[Path, Path | None]) -> None:
    for path, backup in backups.items():
        if backup is None:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, path)


def _git_index_path(project_root: Path) -> Path:
    relative = subprocess.run(
        ["git", "rev-parse", "--git-path", "index"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    path = Path(relative)
    return path if path.is_absolute() else project_root / path


def delete_release(project_root: Path, repository: str, release: str) -> None:
    completed = subprocess.run(
        [
            "gh",
            "release",
            "delete",
            release,
            "--repo",
            repository,
            "--cleanup-tag",
            "--yes",
        ],
        cwd=project_root,
        check=False,
    )
    if completed.returncode != 0:
        raise ReferenceArchiveError(
            f"failed to clean up partial GitHub release {release}"
        )


def publish_release(
    project_root: Path,
    repository: str,
    release: str,
    archives: list[Path],
) -> None:
    if shutil.which("gh") is None:
        raise ReferenceArchiveError("GitHub CLI (`gh`) is required to publish references")
    notes = (
        "Immutable Typhoon reference-image archives. Pointers and checksums "
        "are stored in reference-releases.json."
    )
    created = False
    try:
        _run(
            [
                "gh",
                "release",
                "create",
                release,
                "--repo",
                repository,
                "--draft",
                "--title",
                f"Reference data {release.removeprefix('reference-data-')}",
                "--notes",
                notes,
            ],
            project_root,
        )
        created = True
        _run(
            [
                "gh",
                "release",
                "upload",
                release,
                *(str(path) for path in archives),
                "--repo",
                repository,
            ],
            project_root,
        )
        _run(
            [
                "gh",
                "release",
                "edit",
                release,
                "--repo",
                repository,
                "--draft=false",
            ],
            project_root,
        )
    except Exception:
        if created:
            delete_release(project_root, repository, release)
        raise


def _release_name(group_records: dict[str, dict[str, Any]]) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(
        "".join(
            record["archive_sha256"]
            for _, record in sorted(group_records.items())
        ).encode("ascii")
    ).hexdigest()[:8]
    return f"reference-data-{timestamp}-{digest}-{secrets.token_hex(4)}"


def _check_hydration_state(project_root: Path, manifest: dict[str, Any] | None) -> None:
    if manifest is None:
        return
    state = load_state(project_root)
    if state is None or state.get("manifest_sha256") != manifest_digest(manifest):
        raise ReferenceArchiveError(
            "references are not hydrated from the current manifest; run "
            "`pixi run download-references` first"
        )


def _unexpected_reference_files(
    reference_roots: list[Path], expected: set[Path]
) -> list[Path]:
    unexpected = []
    for root in reference_roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if (
                path.is_file()
                and path.suffix.lower() in IMAGE_SUFFIXES
                and path.resolve() not in expected
            ):
                unexpected.append(path.resolve())
    return sorted(unexpected)


def _commit_update(
    project_root: Path,
    suite_roots: list[Path],
    changed: int,
    removed: int,
    reference_roots: Iterable[Path] = (),
) -> None:
    manifest_path = project_root / MANIFEST_NAME
    paths = [str(manifest_path.relative_to(project_root))]
    paths.extend(str(path.relative_to(project_root)) for path in suite_roots)
    staged_before = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z"],
        cwd=project_root,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8").split("\0")
    allowed_roots = tuple(f"{path.rstrip('/')}" for path in paths)
    unrelated_staged = [
        path
        for path in staged_before
        if path
        and not any(
            path == allowed or path.startswith(f"{allowed}/")
            for allowed in allowed_roots
        )
    ]
    if unrelated_staged:
        raise ReferenceArchiveError(
            "unrelated staged changes would be included in the automatic commit: "
            + ", ".join(unrelated_staged)
        )
    for reference_root in reference_roots:
        try:
            relative = reference_root.relative_to(project_root)
        except ValueError as exc:
            raise ReferenceArchiveError(
                f"reference root is outside repository: {reference_root}"
            ) from exc
        _run(
            [
                "git",
                "rm",
                "-r",
                "--cached",
                "--ignore-unmatch",
                "--",
                str(relative),
            ],
            project_root,
        )
    _run(["git", "add", "-A", "--", *paths], project_root)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", *paths], cwd=project_root
    )
    if staged.returncode == 0:
        print("No suite or manifest changes to commit")
        return
    if staged.returncode != 1:
        raise ReferenceArchiveError("failed to inspect staged reference changes")
    summary = "Update reference release pointers"
    body = (
        f"- Publish {changed} changed reference archive{'s' if changed != 1 else ''}.\n"
        f"- Remove {removed} obsolete archive pointer{'s' if removed != 1 else ''}.\n"
        "- Commit associated test-suite additions, removals, and configuration changes."
    )
    _run(["git", "commit", "-m", summary, "-m", body], project_root)


def update_references(
    project_root: Path,
    *,
    dry_run: bool = False,
    commit: bool = True,
    repository: str | None = None,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    old_manifest = load_manifest(project_root)
    _check_hydration_state(project_root, old_manifest)
    repository = (
        repository
        or (old_manifest or {}).get("repository")
        or repository_from_remote(project_root)
    )
    discovered_groups, reference_roots = discover_reference_groups(project_root)
    old_files = _manifest_files(old_manifest or {"groups": {}})
    missing_archived = [
        path
        for paths in discovered_groups.values()
        for path in paths
        if not path.is_file()
        and path.relative_to(project_root).as_posix() in old_files
    ]
    if missing_archived:
        sample = "\n".join(
            f"  {path.relative_to(project_root)}"
            for path in sorted(missing_archived, key=str)[:10]
        )
        raise ReferenceArchiveError(
            "archived reference image is missing locally; run "
            f"`pixi run download-references --force` or restore it before updating:\n{sample}"
        )
    groups = {
        group_id: [path for path in paths if path.is_file()]
        for group_id, paths in discovered_groups.items()
    }
    groups = {group_id: paths for group_id, paths in groups.items() if paths}
    expected = {path for paths in groups.values() for path in paths}
    unexpected = _unexpected_reference_files(reference_roots, expected)
    for path in unexpected:
        print(f"Warning: unreferenced image is not archived: {path.relative_to(project_root)}")

    old_groups = (old_manifest or {}).get("groups", {})
    built: dict[str, dict[str, Any]] = {}
    archive_paths: dict[str, Path] = {}
    with tempfile.TemporaryDirectory(prefix="typhoon-reference-release-") as temp_name:
        output_dir = Path(temp_name)
        for group_id, paths in groups.items():
            archive, record = build_archive(project_root, group_id, paths, output_dir)
            built[group_id] = record
            archive_paths[group_id] = archive

        changed_groups = {
            group_id: record
            for group_id, record in built.items()
            if group_id not in old_groups
            or record["files"] != old_groups[group_id].get("files")
        }
        removed_groups = sorted(set(old_groups) - set(built))
        if not changed_groups and not removed_groups:
            print("Reference archives are unchanged")
            if commit and not dry_run:
                _commit_update(project_root, discover_suite_roots(project_root), 0, 0)
            return {"changed": [], "removed": [], "release": None}

        release = _release_name(changed_groups) if changed_groups else None
        if dry_run:
            print(f"Would publish release {release}" if release else "No release is required")
            for group_id in changed_groups:
                print(f"  changed: {group_id}")
            for group_id in removed_groups:
                print(f"  removed: {group_id}")
            return {"changed": sorted(changed_groups), "removed": removed_groups, "release": release}

        new_groups: dict[str, dict[str, Any]] = {}
        for group_id, record in built.items():
            if group_id in changed_groups:
                new_groups[group_id] = {**record, "release": release}
            else:
                new_groups[group_id] = old_groups[group_id]
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "repository": repository,
            "groups": new_groups,
        }
        manifest_path = project_root / MANIFEST_NAME
        state_path = project_root / STATE_NAME
        old_manifest_bytes = manifest_path.read_bytes() if manifest_path.is_file() else None
        old_state_bytes = state_path.read_bytes() if state_path.is_file() else None
        obsolete_paths = {
            project_root / relative
            for relative in _manifest_files(old_manifest or {"groups": {}})
            if relative not in _manifest_files(manifest)
        }
        obsolete_backups = _backup_paths(
            obsolete_paths, output_dir / "obsolete-backups"
        )
        index_path = _git_index_path(project_root) if commit else None
        old_index_bytes = (
            index_path.read_bytes()
            if index_path is not None and index_path.is_file()
            else None
        )
        release_published = False
        try:
            if release:
                publish_release(
                    project_root,
                    repository,
                    release,
                    [
                        archive_paths[group_id]
                        for group_id in sorted(changed_groups)
                    ],
                )
                release_published = True
            write_json_atomic(manifest_path, manifest)
            write_json_atomic(
                state_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "manifest_sha256": manifest_digest(manifest),
                    "files": _manifest_files(manifest),
                },
            )
            for path in obsolete_paths:
                path.unlink(missing_ok=True)
            if commit:
                _commit_update(
                    project_root,
                    discover_suite_roots(project_root),
                    len(changed_groups),
                    len(removed_groups),
                    reference_roots,
                )
        except Exception:
            _restore_optional_file(manifest_path, old_manifest_bytes)
            _restore_optional_file(state_path, old_state_bytes)
            _restore_paths(obsolete_backups)
            if index_path is not None:
                _restore_optional_file(index_path, old_index_bytes)
            if release and release_published:
                delete_release(project_root, repository, release)
            raise
        return {"changed": sorted(changed_groups), "removed": removed_groups, "release": release}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage release-backed test reference images")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    subparsers = parser.add_subparsers(dest="command", required=True)
    download = subparsers.add_parser("download", help="download and verify all reference archives")
    download.add_argument("--force", action="store_true", help="discard local reference changes")
    update = subparsers.add_parser("update", help="publish changed reference archives")
    update.add_argument("--dry-run", action="store_true")
    update.add_argument("--no-commit", action="store_true")
    update.add_argument("--repository", help="GitHub repository in owner/name form")
    return parser


def _restore_optional_file(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        temporary = path.with_name(f".{path.name}.restore")
        temporary.write_bytes(content)
        os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "download":
            hydrate_references(args.project_root, force=args.force)
        else:
            update_references(
                args.project_root,
                dry_run=args.dry_run,
                commit=not args.no_commit,
                repository=args.repository,
            )
    except (ReferenceArchiveError, subprocess.CalledProcessError, OSError, zipfile.BadZipFile) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
