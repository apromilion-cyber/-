import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_distribution", ROOT / "scripts/check_distribution.py"
)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def test_reviewed_manifest_covers_public_modules_and_examples():
    reviewed = release.reviewed_files(ROOT)
    assert "docs/release.md" in reviewed
    assert {p.relative_to(ROOT).as_posix() for p in (ROOT / "examples").glob("*.json")} <= reviewed
    assert not any(p.startswith(".venv/") for p in reviewed)


@pytest.mark.parametrize("line", ["include *.json", "include ../secret", "include missing.json"])
def test_manifest_rejects_wildcards_traversal_or_missing_files(tmp_path, line):
    (tmp_path / "MANIFEST.in").write_text(line)
    with pytest.raises(release.DistributionError):
        release.reviewed_files(tmp_path)


@pytest.mark.parametrize("name", [".env", "credentials.json", "personal-backup.json"])
def test_manifest_cannot_include_local_secret_or_backup_files(tmp_path, name):
    (tmp_path / name).write_text("fictional")
    (tmp_path / "MANIFEST.in").write_text(f"include {name}")
    with pytest.raises(release.DistributionError, match="환경/키/백업"):
        release.reviewed_files(tmp_path)


def test_parent_directory_symlink_is_rejected(tmp_path):
    target = tmp_path / "fictional-target"
    target.mkdir()
    (target / "fixture.txt").write_text("fictional")
    (tmp_path / "alias").symlink_to(target, target_is_directory=True)
    (tmp_path / "MANIFEST.in").write_text("include alias/fixture.txt")
    with pytest.raises(release.DistributionError, match="심볼릭 링크"):
        release.reviewed_files(tmp_path)


@pytest.mark.parametrize("name", ["../secret", "/secret", "dir/../../secret", "dir\\secret"])
def test_unsafe_archive_paths_rejected(name):
    with pytest.raises(release.DistributionError):
        release.check_path(name)


def test_known_credentials_are_rejected_without_echoing_contents():
    for content in (
        b"-----BEGIN " + b"PRIVATE KEY-----\nprivate material",
        b"github_pat_" + b"a" * 30,
        b'{"private_key":"' + b"a" * 30 + b'"}',
        b"https://docs.google.com/spreadsheets/d/" + b"x" * 30 + b"/edit",
    ):
        with pytest.raises(release.DistributionError) as exc:
            release.check_content("fixture.txt", content)
        assert "fixture.txt" in str(exc.value)
        assert "private material" not in str(exc.value)
        assert "a" * 30 not in str(exc.value)


def test_fictional_examples_have_no_known_credentials():
    for path in (ROOT / "examples").glob("*.json"):
        release.check_content(path.name, path.read_bytes())


def test_wrong_metadata_is_rejected():
    with pytest.raises(release.DistributionError, match="메타데이터"):
        release.check_metadata(
            b"Name: wrong\nVersion: 1\nRequires-Python: >=3.11\n",
            {"name": "personal-travel-db", "version": "0.1.0", "requires-python": ">=3.11"},
        )


def test_unreviewed_archive_file_is_rejected(tmp_path):
    artifact = tmp_path / "fixture.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("private-data.json", "{}")
    with pytest.raises(release.DistributionError, match="허용 목록"):
        release.check_distribution(artifact, ROOT)


def test_tar_link_and_duplicate_rejected(tmp_path):
    for variant in ("link", "duplicate"):
        artifact = tmp_path / f"{variant}.tar.gz"
        with tarfile.open(artifact, "w:gz") as archive:
            entry = tarfile.TarInfo("personal_travel_db-0.1.0/README.md")
            if variant == "link":
                entry.type = tarfile.SYMTYPE
                entry.linkname = "/private"
                archive.addfile(entry)
            else:
                entry.size = 1
                archive.addfile(entry, io.BytesIO(b"x"))
                archive.addfile(entry, io.BytesIO(b"x"))
        with pytest.raises(release.DistributionError, match="링크/특수 파일/중복"):
            release.check_distribution(artifact, ROOT)
