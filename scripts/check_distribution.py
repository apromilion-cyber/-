"""Offline inspection of reviewed distribution files; never reads Secrets or contacts APIs."""

import argparse
import configparser
import hashlib
import json
import re
import stat
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath


class DistributionError(ValueError):
    pass


def reviewed_files(root):
    allowed = set()
    for line in (root / "MANIFEST.in").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or line == "global-exclude *":
            continue
        words = line.split()
        if len(words) != 2 or words[0] != "include" or any(c in words[1] for c in "*?["):
            raise DistributionError("MANIFEST.in에는 명시적 include 경로만 허용합니다")
        path = PurePosixPath(words[1])
        if path.is_absolute() or ".." in path.parts or words[1] in allowed:
            raise DistributionError("배포 목록의 경로가 잘못되었거나 중복되었습니다")
        if (
            {".git", ".venv", "__pycache__", "dist", "build"} & set(path.parts)
            or path.name.startswith(".env")
            or path.suffix == ".json"
            and (
                "backup" in path.name.lower()
                or path.name.startswith(("credentials", "service-account"))
            )
        ):
            raise DistributionError("환경/키/백업 파일은 배포 목록에 넣을 수 없습니다")
        linked = any(
            (root.joinpath(*path.parts[:i])).is_symlink() for i in range(1, len(path.parts) + 1)
        )
        if linked or not (root / path).is_file():
            raise DistributionError("배포 목록에 누락 파일 또는 심볼릭 링크가 있습니다")
        allowed.add(words[1])
    modules = {p.relative_to(root).as_posix() for p in (root / "src/travel_db").glob("*.py")}
    if not modules or not modules <= allowed:
        raise DistributionError("Python 모듈이 소스 배포 목록에서 빠졌습니다")
    return allowed


def check_content(name, content):
    # Report names/codes only; never print matched credential content.
    patterns = (
        rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----",
        rb"(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}",
        rb'"private_key"\s*:\s*"[^"\s]{20,}',
        rb"docs\.google\.com/(?:spreadsheets|document|presentation)/d/[A-Za-z0-9_-]{20,}",
    )
    if any(re.search(pattern, content) for pattern in patterns):
        raise DistributionError(f"알려진 인증 정보 형식 발견: {name}")


def check_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise DistributionError("아카이브의 경로가 안전하지 않습니다")


def check_metadata(content, project):
    metadata = BytesParser().parsebytes(content)

    def normalize(value):
        return re.sub(r"[-_.]+", "-", value).lower()

    if (
        normalize(metadata.get("Name", "")) != normalize(project["name"])
        or metadata.get("Version") != project["version"]
        or metadata.get("Requires-Python") != project["requires-python"]
    ):
        raise DistributionError("배포 메타데이터가 pyproject.toml과 다릅니다")


def check_distribution(path, root):
    path, root = Path(path), Path(root)
    allowed = reviewed_files(root)
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    stem = project["name"].replace("-", "_") + "-" + project["version"]
    files = {}
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            if (
                len(archive.infolist()) > 256
                or sum(m.file_size for m in archive.infolist()) > 20_000_000
            ):
                raise DistributionError("아카이브 크기/파일 수가 제한을 초과했습니다")
            for member in archive.infolist():
                check_path(member.filename)
                if member.is_dir():
                    continue
                if (
                    member.filename in files
                    or member.file_size > 5_000_000
                    or stat.S_IFMT(member.external_attr >> 16) == stat.S_IFLNK
                ):
                    raise DistributionError("아카이브 파일이 중복되거나 너무 큽니다")
                files[member.filename] = archive.read(member)
        modules = {p.removeprefix("src/") for p in allowed if p.startswith("src/")}
        info = stem + ".dist-info/"
        metadata_files = {
            info + name
            for name in (
                "METADATA",
                "WHEEL",
                "entry_points.txt",
                "top_level.txt",
                "RECORD",
            )
        }
        if set(files) != modules | metadata_files:
            raise DistributionError("wheel의 파일이 허용 목록과 다릅니다")
        for name in modules:
            if files[name] != (root / "src" / name).read_bytes():
                raise DistributionError(f"wheel 코드가 작업 파일과 다릅니다: {name}")
        check_metadata(files[info + "METADATA"], project)
        entrypoints = configparser.ConfigParser()
        entrypoints.read_string(files[info + "entry_points.txt"].decode())
        if (
            "console_scripts" not in entrypoints
            or dict(entrypoints["console_scripts"]) != project["scripts"]
        ):
            raise DistributionError("wheel CLI 진입점이 다릅니다")
        kind = "wheel"
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            if (
                len(archive.getmembers()) > 256
                or sum(m.size for m in archive.getmembers()) > 20_000_000
            ):
                raise DistributionError("아카이브 크기/파일 수가 제한을 초과했습니다")
            for member in archive.getmembers():
                check_path(member.name)
                if member.isdir():
                    continue
                relative = member.name.removeprefix(stem + "/")
                if not member.isfile() or relative in files or member.size > 5_000_000:
                    raise DistributionError(
                        "소스 아카이브에 링크/특수 파일/중복/큰 파일이 있습니다"
                    )
                if not member.name.startswith(stem + "/"):
                    raise DistributionError("소스 아카이브의 최상위 경로가 다릅니다")
                files[relative] = archive.extractfile(member).read()
        metadata_files = {
            "PKG-INFO",
            "setup.cfg",
            f"src/{project['name'].replace('-', '_')}.egg-info/SOURCES.txt",
        }
        if set(files) != allowed | metadata_files:
            raise DistributionError("소스 배포의 파일이 허용 목록과 다릅니다")
        for name in allowed:
            if files[name] != (root / name).read_bytes():
                raise DistributionError(f"소스 배포 내용이 작업 파일과 다릅니다: {name}")
        check_metadata(files["PKG-INFO"], project)
        kind = "sdist"
    else:
        raise DistributionError("wheel 또는 .tar.gz 소스 배포 파일을 지정하세요")
    for name, content in files.items():
        check_content(name, content)
    return {
        "valid": True,
        "kind": kind,
        "artifact": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "file_count": len(files),
    }


def main():
    parser = argparse.ArgumentParser(
        description="인증 없는 배포 아카이브 허용 파일/메타데이터 검사"
    )
    parser.add_argument("artifacts", nargs="+")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    try:
        results = [check_distribution(path, args.root) for path in args.artifacts]
    except (DistributionError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"artifacts": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
