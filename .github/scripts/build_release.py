#!/usr/bin/env python3
"""Build and verify a deterministic MoviePilot plugin release archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = PROJECT_ROOT / "plugins.v3" / "lunatvsource"
MANIFEST_PATH = PROJECT_ROOT / "package.v3.json"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
    }
)
EXCLUDED_FILENAMES = frozenset({".DS_Store"})
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})
REQUIRED_FILES = (
    "__init__.py",
    "ai.py",
    "classification.py",
    "cms.py",
    "downloader.py",
    "m3u8_engine.py",
    "proxy.py",
    "naming.py",
    "package.json",
    "fallback_sources.json",
    "dist/index.html",
    "dist/assets/remoteEntry.js",
    "vendor/n_m3u8dl_re/LICENSE",
    "vendor/n_m3u8dl_re/README.md",
    "vendor/n_m3u8dl_re/N_m3u8DL-RE_v0.5.1-beta_linux-x64_20251029.tar.gz",
    "vendor/n_m3u8dl_re/N_m3u8DL-RE_v0.5.1-beta_linux-arm64_20251029.tar.gz",
)


def _is_excluded(relative: PurePosixPath) -> bool:
    return (
        any(part in EXCLUDED_DIRECTORIES for part in relative.parts)
        or relative.name in EXCLUDED_FILENAMES
        or relative.suffix.lower() in EXCLUDED_SUFFIXES
    )


def _archive_entries(source_dir: Path) -> list[tuple[str, Path, bool]]:
    source_dir = source_dir.expanduser()
    if source_dir.is_symlink() or not source_dir.is_dir():
        raise ValueError(f"插件源码目录无效：{source_dir}")
    source_dir = source_dir.resolve()

    entries: list[tuple[str, Path, bool]] = []
    for current, dirnames, filenames in os.walk(source_dir, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_directories = []
        for dirname in sorted(dirnames):
            path = current_path / dirname
            relative = PurePosixPath(path.relative_to(source_dir).as_posix())
            if _is_excluded(relative):
                continue
            if path.is_symlink():
                raise ValueError(f"发布包不允许符号链接：{relative}")
            kept_directories.append(dirname)
            entries.append((f"{relative.as_posix()}/", path, True))
        dirnames[:] = kept_directories

        for filename in sorted(filenames):
            path = current_path / filename
            relative = PurePosixPath(path.relative_to(source_dir).as_posix())
            if _is_excluded(relative):
                continue
            file_stat = path.lstat()
            if stat.S_ISLNK(file_stat.st_mode):
                raise ValueError(f"发布包不允许符号链接：{relative}")
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValueError(f"发布包只允许普通文件：{relative}")
            entries.append((relative.as_posix(), path, False))

    return sorted(entries, key=lambda item: item[0])


def _validate_required_files(
    names: Iterable[str],
    required_files: Sequence[str],
) -> None:
    present = {name.rstrip("/") for name in names}
    missing = [name for name in required_files if name not in present]
    if missing:
        raise ValueError(f"发布包缺少必需文件：{', '.join(missing)}")


def verify_release_archive(
    archive_path: Path,
    *,
    required_files: Sequence[str] = REQUIRED_FILES,
) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ValueError("发布包包含重复路径")
        for info in infos:
            relative = PurePosixPath(info.filename.rstrip("/"))
            if (
                not relative.parts
                or relative.is_absolute()
                or ".." in relative.parts
                or relative.parts[0] == "lunatvsource"
            ):
                raise ValueError(f"发布包路径不合法：{info.filename}")
            if _is_excluded(relative):
                raise ValueError(f"发布包包含排除项：{info.filename}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"发布包不允许符号链接：{info.filename}")
            expected_mode = 0o755 if info.is_dir() else 0o644
            if stat.S_IMODE(mode) != expected_mode:
                raise ValueError(
                    f"发布包权限不正确：{info.filename} "
                    f"应为 {expected_mode:o}，实际为 {stat.S_IMODE(mode):o}"
                )
            if info.date_time != FIXED_ZIP_TIME:
                raise ValueError(f"发布包时间戳不固定：{info.filename}")
        _validate_required_files(names, required_files)
        corrupt = archive.testzip()
        if corrupt:
            raise ValueError(f"发布包文件校验失败：{corrupt}")


def build_release_archive(
    source_dir: Path,
    archive_path: Path,
    *,
    required_files: Sequence[str] = REQUIRED_FILES,
) -> str:
    entries = _archive_entries(source_dir)
    _validate_required_files((name for name, _path, _is_dir in entries), required_files)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = archive_path.with_name(f".{archive_path.name}.tmp")
    temporary_path.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(
            temporary_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for archive_name, path, is_directory in entries:
                info = zipfile.ZipInfo(archive_name, date_time=FIXED_ZIP_TIME)
                info.create_system = 3
                info.compress_type = (
                    zipfile.ZIP_STORED if is_directory else zipfile.ZIP_DEFLATED
                )
                mode = (
                    stat.S_IFDIR | 0o755
                    if is_directory
                    else stat.S_IFREG | 0o644
                )
                info.external_attr = mode << 16
                if is_directory:
                    info.external_attr |= 0x10
                archive.writestr(
                    info,
                    b"" if is_directory else path.read_bytes(),
                    compresslevel=9,
                )
        temporary_path.replace(archive_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    verify_release_archive(archive_path, required_files=required_files)
    return hashlib.sha256(archive_path.read_bytes()).hexdigest()


def _release_version() -> str:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    version = str(manifest["LunaTVSource"]["version"]).strip()
    if not version:
        raise ValueError("package.v3.json 缺少 LunaTVSource.version")
    return version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT,
        help="ZIP 与 SHA-256 文件输出目录（默认仓库根目录）",
    )
    args = parser.parse_args()

    version = _release_version()
    archive_path = args.output_dir.resolve() / f"lunatvsource_v{version}.zip"
    checksum_path = archive_path.with_suffix(f"{archive_path.suffix}.sha256")
    digest = build_release_archive(PLUGIN_DIR, archive_path)
    checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
    print(archive_path)
    print(checksum_path)
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
