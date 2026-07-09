from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest

from typhoon_tests.config import load_suite_config_for_path
from typhoon_tests import reference_archives as archives


def make_suite(
    root: Path,
    *,
    suite_name: str = "sample",
    section: str = "surfaces/metal",
    reference_data: bytes = b"reference",
) -> tuple[Path, Path]:
    suite = root / suite_name
    test_dir = suite / section
    test_dir.mkdir(parents=True)
    (suite / "typhoon-suite.toml").write_text(
        f"""[suite]
name = "{suite_name}"

[reference]
dir = "reference"
pattern = "{{path}}.png"
missing = "fail"
""",
        encoding="utf-8",
    )
    usd = test_dir / "case.usda"
    usd.write_text("#usda 1.0\n", encoding="utf-8")
    reference = suite / "reference" / section / "case.png"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(reference_data)
    load_suite_config_for_path.cache_clear()
    return usd, reference


def test_repository_manifest_matches_tests_and_references_are_untracked() -> None:
    project_root = Path(__file__).resolve().parents[1]
    manifest = archives.load_manifest(project_root)

    assert manifest is not None
    archives._validate_manifest_paths(project_root, manifest)
    reference_paths = sorted(archives._manifest_files(manifest))
    assert reference_paths
    tracked = subprocess.run(
        ["git", "ls-files", "--", *reference_paths],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert tracked == []


def test_discovers_one_group_per_nested_reference_subsection(tmp_path: Path) -> None:
    _, reference = make_suite(tmp_path)

    groups, roots = archives.discover_reference_groups(tmp_path)

    assert groups == {"sample/surfaces/metal": [reference.resolve()]}
    assert roots == [(tmp_path / "sample" / "reference").resolve()]


def test_build_archive_is_deterministic_and_uses_repository_paths(
    tmp_path: Path,
) -> None:
    _, reference = make_suite(tmp_path)
    first, first_record = archives.build_archive(
        tmp_path, "sample/surfaces/metal", [reference], tmp_path / "first"
    )
    second, second_record = archives.build_archive(
        tmp_path, "sample/surfaces/metal", [reference], tmp_path / "second"
    )

    assert first.read_bytes() == second.read_bytes()
    assert first_record == second_record
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == ["sample/reference/surfaces/metal/case.png"]
        assert archive.read(archive.namelist()[0]) == b"reference"


def manifest_for_archive(
    root: Path, archive_path: Path, record: dict[str, object]
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "repository": "owner/repository",
        "groups": {
            "sample/surfaces/metal": {
                **record,
                "release": "reference-data-test",
            }
        },
    }


def test_hydrate_downloads_verifies_and_restores_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, reference = make_suite(tmp_path)
    release_archive, record = archives.build_archive(
        tmp_path, "sample/surfaces/metal", [reference], tmp_path / "release"
    )
    manifest = manifest_for_archive(tmp_path, release_archive, record)
    archives.write_json_atomic(tmp_path / archives.MANIFEST_NAME, manifest)
    reference.unlink()

    monkeypatch.setattr(
        archives,
        "_download",
        lambda _url, destination: shutil.copyfile(release_archive, destination),
    )

    assert archives.hydrate_references(tmp_path) == 1
    assert reference.read_bytes() == b"reference"
    state = json.loads((tmp_path / archives.STATE_NAME).read_text())
    assert state["manifest_sha256"] == archives.manifest_digest(manifest)
    assert archives.hydrate_references(tmp_path) == 0


def test_hydrate_rejects_modified_local_reference_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, reference = make_suite(tmp_path)
    release_archive, record = archives.build_archive(
        tmp_path, "sample/surfaces/metal", [reference], tmp_path / "release"
    )
    manifest = manifest_for_archive(tmp_path, release_archive, record)
    archives.write_json_atomic(tmp_path / archives.MANIFEST_NAME, manifest)
    archives.write_json_atomic(
        tmp_path / archives.STATE_NAME,
        {
            "schema_version": 1,
            "manifest_sha256": archives.manifest_digest(manifest),
            "files": {record["files"][0]["path"]: record["files"][0]["sha256"]},
        },
    )
    reference.write_bytes(b"local edit")
    monkeypatch.setattr(
        archives,
        "_download",
        lambda _url, destination: shutil.copyfile(release_archive, destination),
    )

    with pytest.raises(archives.ReferenceArchiveError, match="local references differ"):
        archives.hydrate_references(tmp_path)
    archives.hydrate_references(tmp_path, force=True)
    assert reference.read_bytes() == b"reference"


def test_hydrate_rejects_archive_content_not_declared_by_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, reference = make_suite(tmp_path)
    release_archive, record = archives.build_archive(
        tmp_path, "sample/surfaces/metal", [reference], tmp_path / "release"
    )
    bad_archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_archive, "w") as archive:
        archive.writestr("../outside.png", b"reference")
    record["archive_sha256"] = archives.sha256_file(bad_archive)
    record["archive_size"] = bad_archive.stat().st_size
    manifest = manifest_for_archive(tmp_path, release_archive, record)
    archives.write_json_atomic(tmp_path / archives.MANIFEST_NAME, manifest)
    reference.unlink()
    monkeypatch.setattr(
        archives,
        "_download",
        lambda _url, destination: shutil.copyfile(bad_archive, destination),
    )

    with pytest.raises(archives.ReferenceArchiveError, match="contents do not match"):
        archives.hydrate_references(tmp_path)
    assert not (tmp_path.parent / "outside.png").exists()


def test_hydrate_rejects_archive_name_that_escapes_download_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, reference = make_suite(tmp_path)
    release_archive, record = archives.build_archive(
        tmp_path, "sample/surfaces/metal", [reference], tmp_path / "release"
    )
    record["archive"] = "../../outside.zip"
    manifest = manifest_for_archive(tmp_path, release_archive, record)
    archives.write_json_atomic(tmp_path / archives.MANIFEST_NAME, manifest)
    reference.unlink()
    downloaded = False

    def fake_download(_url: str, _destination: Path) -> None:
        nonlocal downloaded
        downloaded = True

    monkeypatch.setattr(archives, "_download", fake_download)

    with pytest.raises(archives.ReferenceArchiveError, match="unsafe archive name"):
        archives.hydrate_references(tmp_path)
    assert downloaded is False


def test_hydrate_rolls_back_installed_files_when_state_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, reference = make_suite(tmp_path, reference_data=b"new reference")
    release_archive, record = archives.build_archive(
        tmp_path, "sample/surfaces/metal", [reference], tmp_path / "release"
    )
    manifest = manifest_for_archive(tmp_path, release_archive, record)
    archives.write_json_atomic(tmp_path / archives.MANIFEST_NAME, manifest)
    reference.write_bytes(b"old reference")
    old_manifest = {
        **manifest,
        "groups": {
            "sample/surfaces/metal": {
                **manifest["groups"]["sample/surfaces/metal"],
                "files": [
                    {
                        "path": record["files"][0]["path"],
                        "sha256": archives.sha256_file(reference),
                        "size": reference.stat().st_size,
                    }
                ],
            }
        },
    }
    archives.write_json_atomic(
        tmp_path / archives.STATE_NAME,
        {
            "schema_version": 1,
            "manifest_sha256": archives.manifest_digest(old_manifest),
            "files": {record["files"][0]["path"]: archives.sha256_file(reference)},
        },
    )
    monkeypatch.setattr(
        archives,
        "_download",
        lambda _url, destination: shutil.copyfile(release_archive, destination),
    )
    real_write = archives.write_json_atomic

    def fail_state_write(path: Path, data: object) -> None:
        if path.name == archives.STATE_NAME:
            raise OSError("disk full")
        real_write(path, data)

    monkeypatch.setattr(archives, "write_json_atomic", fail_state_write)

    with pytest.raises(OSError, match="disk full"):
        archives.hydrate_references(tmp_path)
    assert reference.read_bytes() == b"old reference"


def test_update_publishes_only_changed_groups_and_removes_deleted_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usd, reference = make_suite(tmp_path)
    published: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        archives,
        "publish_release",
        lambda _root, _repository, release, paths: published.append(
            (release, [path.name for path in paths])
        ),
    )

    first = archives.update_references(
        tmp_path, commit=False, repository="owner/repository"
    )
    assert first["changed"] == ["sample/surfaces/metal"]
    assert len(published) == 1

    unchanged = archives.update_references(tmp_path, commit=False)
    assert unchanged == {"changed": [], "removed": [], "release": None}
    assert len(published) == 1

    reference.write_bytes(b"updated")
    changed = archives.update_references(tmp_path, commit=False)
    assert changed["changed"] == ["sample/surfaces/metal"]
    assert len(published) == 2

    usd.unlink()
    removed = archives.update_references(tmp_path, commit=False)
    assert removed["changed"] == []
    assert removed["removed"] == ["sample/surfaces/metal"]
    assert len(published) == 2
    assert archives.load_manifest(tmp_path)["groups"] == {}
    assert not reference.exists()


def test_update_references_skips_missing_reference_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usd, reference = make_suite(tmp_path)
    missing_usd = usd.with_name("missing.usda")
    missing_usd.write_text("#usda 1.0\n", encoding="utf-8")
    missing_reference = reference.with_name("missing.png")
    assert not missing_reference.exists()
    published: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        archives,
        "publish_release",
        lambda _root, _repository, release, paths: published.append(
            (release, [path.name for path in paths])
        ),
    )

    result = archives.update_references(
        tmp_path, commit=False, repository="owner/repository"
    )

    assert result["changed"] == ["sample/surfaces/metal"]
    assert len(published) == 1
    manifest = archives.load_manifest(tmp_path)
    assert manifest is not None
    files = manifest["groups"]["sample/surfaces/metal"]["files"]
    assert [record["path"] for record in files] == [
        "sample/reference/surfaces/metal/case.png"
    ]
    assert missing_reference.exists() is False


def test_update_references_rejects_missing_previously_archived_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, reference = make_suite(tmp_path)
    monkeypatch.setattr(
        archives,
        "publish_release",
        lambda _root, _repository, _release, _paths: None,
    )
    archives.update_references(tmp_path, commit=False, repository="owner/repository")
    manifest_before = (tmp_path / archives.MANIFEST_NAME).read_text(encoding="utf-8")
    reference.unlink()

    with pytest.raises(
        archives.ReferenceArchiveError,
        match="archived reference image is missing locally",
    ):
        archives.update_references(tmp_path, commit=False)

    assert (
        tmp_path / archives.MANIFEST_NAME
    ).read_text(encoding="utf-8") == manifest_before


def test_update_cleans_partial_release_and_restores_manifest_on_commit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _reference = make_suite(tmp_path)
    published: list[str] = []
    deleted: list[str] = []
    monkeypatch.setattr(
        archives,
        "publish_release",
        lambda _root, _repository, release, _paths: published.append(release),
    )
    monkeypatch.setattr(
        archives,
        "delete_release",
        lambda _root, _repository, release: deleted.append(release),
    )
    monkeypatch.setattr(
        archives,
        "_git_index_path",
        lambda root: root / ".git" / "index",
    )
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_bytes(b"original index")
    monkeypatch.setattr(
        archives,
        "_commit_update",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("commit failed")),
    )

    with pytest.raises(OSError, match="commit failed"):
        archives.update_references(
            tmp_path, commit=True, repository="owner/repository"
        )

    assert deleted == published
    assert not (tmp_path / archives.MANIFEST_NAME).exists()
    assert not (tmp_path / archives.STATE_NAME).exists()
    assert (tmp_path / ".git" / "index").read_bytes() == b"original index"


def test_publish_release_only_cleans_up_release_created_by_this_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "references.zip"
    archive.write_bytes(b"archive")
    deleted: list[str] = []
    calls = 0

    monkeypatch.setattr(archives.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        archives.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "abc123\n", ""),
    )
    monkeypatch.setattr(
        archives,
        "delete_release",
        lambda _root, _repository, release: deleted.append(release),
    )

    def fail_create(_command: list[str], _root: Path) -> None:
        raise subprocess.CalledProcessError(1, "gh release create")

    monkeypatch.setattr(archives, "_run", fail_create)
    with pytest.raises(subprocess.CalledProcessError):
        archives.publish_release(
            tmp_path, "owner/repository", "reference-data-test", [archive]
        )
    assert deleted == []

    def fail_upload(_command: list[str], _root: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise subprocess.CalledProcessError(1, "gh release upload")

    monkeypatch.setattr(archives, "_run", fail_upload)
    with pytest.raises(subprocess.CalledProcessError):
        archives.publish_release(
            tmp_path, "owner/repository", "reference-data-test", [archive]
        )
    assert deleted == ["reference-data-test"]


def test_commit_update_excludes_unrelated_root_changes(tmp_path: Path) -> None:
    suite = tmp_path / "sample"
    suite.mkdir()
    (suite / "typhoon-suite.toml").write_text("[suite]\nname = \"sample\"\n")
    reference = suite / "reference" / "old.png"
    reference.parent.mkdir()
    reference.write_bytes(b"tracked reference")
    (tmp_path / ".gitignore").write_text("reference/\n")
    (tmp_path / archives.MANIFEST_NAME).write_text("{}\n")
    (tmp_path / "pixi.lock").write_text("original\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-f", reference], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    (suite / "case.usda").write_text("#usda 1.0\n")
    (tmp_path / archives.MANIFEST_NAME).write_text('{"schema_version": 1}\n')
    (tmp_path / "pixi.lock").write_text("unrelated\n")

    archives._commit_update(
        tmp_path,
        [suite],
        changed=1,
        removed=0,
        reference_roots=[suite / "reference"],
    )

    committed = subprocess.run(
        ["git", "show", "--pretty=format:", "--name-only", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert set(filter(None, committed)) == {
        archives.MANIFEST_NAME,
        "sample/case.usda",
        "sample/reference/old.png",
    }
    assert reference.read_bytes() == b"tracked reference"
    assert subprocess.run(
        ["git", "ls-files", "--error-unmatch", "sample/reference/old.png"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
    ).returncode == 1
    assert subprocess.run(
        ["git", "status", "--short", "pixi.lock"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "M pixi.lock"
