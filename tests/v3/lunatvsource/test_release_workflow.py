import importlib.util
import stat
import zipfile
from pathlib import Path

import pytest


def _release_builder():
    project_root = Path(__file__).resolve().parents[3]
    script = project_root / ".github" / "scripts" / "build_release.py"
    spec = importlib.util.spec_from_file_location("lunatv_release_builder", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_workflow_publishes_assets_before_native_immutability_locks_them():
    project_root = Path(__file__).resolve().parents[3]
    workflow = (project_root / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    release_create = 'gh release create "$GITHUB_REF_NAME"'
    draft_flag = "--draft"
    publish_draft = 'gh release edit "$GITHUB_REF_NAME" --draft=false'
    immutable_check = "--json isImmutable"
    mutable_cleanup = 'gh release delete "$GITHUB_REF_NAME" --yes'

    assert workflow.index(release_create) < workflow.index(draft_flag)
    assert workflow.index(draft_flag) < workflow.index(publish_draft)
    assert workflow.index(publish_draft) < workflow.index(immutable_check)
    assert workflow.index(immutable_check) < workflow.index(mutable_cleanup)
    assert "removed mutable release and kept tag" in workflow
    assert "tests/v3/lunatvsource/test_manifest.py::test_manifest_version_and_history_match_release_metadata" in workflow
    assert "python .github/scripts/build_release.py" in workflow
    assert "zip -qr" not in workflow


def test_release_builder_is_deterministic_and_normalizes_archive_metadata(
    tmp_path: Path,
):
    builder = _release_builder()
    source = tmp_path / "source"
    (source / "dist").mkdir(parents=True)
    (source / "module.py").write_text("value = 1\n", encoding="utf-8")
    (source / "dist" / "index.html").write_text("<main/>\n", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "module.pyc").write_bytes(b"cache")
    (source / "node_modules").mkdir()
    (source / "node_modules" / "ignored.js").write_text("ignored", encoding="utf-8")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    required = ("module.py", "dist/index.html")

    first_digest = builder.build_release_archive(
        source,
        first,
        required_files=required,
    )
    second_digest = builder.build_release_archive(
        source,
        second,
        required_files=required,
    )

    assert first_digest == second_digest
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == ["dist/", "dist/index.html", "module.py"]
        for info in archive.infolist():
            assert info.date_time == builder.FIXED_ZIP_TIME
            expected_mode = 0o755 if info.is_dir() else 0o644
            assert stat.S_IMODE(info.external_attr >> 16) == expected_mode


def test_release_builder_rejects_included_symlinks(tmp_path: Path):
    builder = _release_builder()
    source = tmp_path / "source"
    source.mkdir()
    target = source / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    (source / "linked.py").symlink_to(target)

    with pytest.raises(ValueError, match="符号链接"):
        builder.build_release_archive(
            source,
            tmp_path / "release.zip",
            required_files=("module.py",),
        )
