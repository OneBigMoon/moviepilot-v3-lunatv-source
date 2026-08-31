from pathlib import Path


def test_release_workflow_publishes_assets_before_native_immutability_locks_them():
    project_root = Path(__file__).resolve().parents[1]
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
