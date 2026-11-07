import pytest

from app.patching import applier
from app.patching.applier import ApplierError, apply_patch
from app.patching.models import PatchLimits
from app.patching.validator import PatchScope, validate_patch
from app.patching.workspace import snapshot_tree
from tests.patching.conftest import AVERAGE_FIX

FILES = {
    "pkg/a.py": "def a():\n    return 1\n",
    "pkg/b.py": "def b():\n    return 2\n",
    "pkg/c.py": "def c():\n    return 3\n",
}
MULTI = (
    "--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1,2 +1,2 @@\n def a():\n-    return 1\n+    return 10\n"
    "--- a/pkg/b.py\n+++ b/pkg/b.py\n@@ -1,2 +1,2 @@\n def b():\n-    return 2\n+    return 20\n"
)


@pytest.fixture
def repo(write_repo):
    return write_repo(FILES)


def validated(diff, root, *allowed, limits=None):
    result, patch = validate_patch(diff, PatchScope(root, frozenset(allowed)), limits or PatchLimits())
    assert patch is not None, result.errors
    return patch


def test_valid_patch_applies(average_repo, workspaces):
    workspace = workspaces.create(average_repo, execution_id="e", label="patched-1")
    result = apply_patch(workspace, validated(AVERAGE_FIX, average_repo, "solution.py"))
    assert result.applied and result.files_changed == ["solution.py"] and not result.errors
    assert (workspace.repo / "solution.py").read_text().splitlines()[1:3] == ["    if not numbers:", "        return 0.0"]
    assert workspace.patch_applied


def test_multiple_file_changes(repo, workspaces):
    workspace = workspaces.create(repo, execution_id="e", label="patched-1")
    result = apply_patch(workspace, validated(MULTI, repo, "pkg/a.py", "pkg/b.py"))
    assert result.applied and result.files_changed == ["pkg/a.py", "pkg/b.py"]
    assert "return 10" in (workspace.repo / "pkg/a.py").read_text()
    assert (workspace.repo / "pkg/c.py").read_text() == FILES["pkg/c.py"]


def test_conflicting_patch_is_detected_and_changes_nothing(repo, workspaces):
    conflict = "--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1,2 +1,2 @@\n def a():\n-    return 999\n+    return 10\n"
    result, patch = validate_patch(conflict, PatchScope(repo, frozenset({"pkg/a.py"})), PatchLimits())
    assert result.valid  # structure and scope are fine; the content does not match
    workspace = workspaces.create(repo, execution_id="e", label="patched-1")
    before = snapshot_tree(workspace.repo).root_hash
    outcome = apply_patch(workspace, patch)
    assert not outcome.applied and outcome.error_kind == "conflict"
    assert "do not match" in outcome.errors[0] and "patch failed" in outcome.stderr
    assert snapshot_tree(workspace.repo).root_hash == before


def test_partial_conflict_in_a_multi_file_patch_is_atomic(repo, workspaces):
    diff = MULTI.replace("-    return 2", "-    return 999")
    workspace = workspaces.create(repo, execution_id="e", label="patched-1")
    outcome = apply_patch(workspace, validated(diff, repo, "pkg/a.py", "pkg/b.py"))
    assert not outcome.applied and outcome.error_kind == "conflict"
    assert (workspace.repo / "pkg/a.py").read_text() == FILES["pkg/a.py"]


def test_missing_target_file_in_the_workspace(repo, workspaces):
    patch = validated(MULTI, repo, "pkg/a.py", "pkg/b.py")
    workspace = workspaces.create(repo, execution_id="e", label="patched-1")
    (workspace.repo / "pkg/b.py").unlink()
    outcome = apply_patch(workspace, patch)
    assert not outcome.applied and outcome.error_kind == "conflict"


def test_new_file_creation_when_allowed(repo, workspaces):
    diff = "--- /dev/null\n+++ b/pkg/d.py\n@@ -0,0 +1,2 @@\n+def d():\n+    return 4\n"
    patch = validated(diff, repo, "pkg/a.py", limits=PatchLimits(allow_new_files=True))
    workspace = workspaces.create(repo, execution_id="e", label="patched-1")
    outcome = apply_patch(workspace, patch)
    assert outcome.applied and (workspace.repo / "pkg/d.py").read_text() == "def d():\n    return 4\n"
    assert not (repo / "pkg/d.py").exists()


def test_unexpected_modification_is_detected(repo, workspaces, monkeypatch):
    real = applier._run_git

    def sneaky(workspace, args, timeout):
        completed = real(workspace, args, timeout)
        if not args:  # the real apply: also touch a file outside the patch
            (workspace.repo / "pkg/c.py").write_text("tampered\n")
        return completed

    monkeypatch.setattr(applier, "_run_git", sneaky)
    workspace = workspaces.create(repo, execution_id="e", label="patched-1")
    outcome = apply_patch(workspace, validated(MULTI, repo, "pkg/a.py", "pkg/b.py"))
    assert not outcome.applied and outcome.error_kind == "unexpected_changes"
    assert outcome.unexpected_changes == ["pkg/c.py"]
    assert (repo / "pkg/c.py").read_text() == FILES["pkg/c.py"]


def test_parser_and_git_disagreement_is_rejected(repo, workspaces, monkeypatch):
    real = applier._run_git

    def lying(workspace, args, timeout):
        completed = real(workspace, args, timeout)
        if "--numstat" in args:
            completed.stdout = "1\t1\tpkg/a.py\n1\t1\tpkg/other.py\n"
        return completed

    monkeypatch.setattr(applier, "_run_git", lying)
    workspace = workspaces.create(repo, execution_id="e", label="patched-1")
    outcome = apply_patch(workspace, validated(MULTI, repo, "pkg/a.py", "pkg/b.py"))
    assert not outcome.applied and outcome.error_kind == "invalid" and "disagree" in outcome.errors[0]


def test_content_mismatch_is_detected(repo, workspaces, monkeypatch):
    real = applier._run_git

    def wrong(workspace, args, timeout):
        completed = real(workspace, args, timeout)
        if not args:
            (workspace.repo / "pkg/a.py").write_text("def a():\n    return 11\n")
        return completed

    monkeypatch.setattr(applier, "_run_git", wrong)
    workspace = workspaces.create(repo, execution_id="e", label="patched-1")
    outcome = apply_patch(workspace, validated(MULTI, repo, "pkg/a.py", "pkg/b.py"))
    assert not outcome.applied and outcome.error_kind == "content_mismatch"


def test_git_unavailable_is_an_environment_error(repo, workspaces, monkeypatch):
    monkeypatch.setattr(applier.shutil, "which", lambda _: None)
    workspace = workspaces.create(repo, execution_id="e", label="patched-1")
    outcome = apply_patch(workspace, validated(MULTI, repo, "pkg/a.py", "pkg/b.py"))
    assert not outcome.applied and outcome.error_kind == "environment"


def test_only_validated_patches_and_fresh_patched_workspaces(repo, workspaces):
    patch = validated(MULTI, repo, "pkg/a.py", "pkg/b.py")
    workspace = workspaces.create(repo, execution_id="e", label="patched-1")
    with pytest.raises(TypeError):
        apply_patch(workspace, MULTI)
    baseline = workspaces.create(repo, execution_id="e", label="baseline")
    with pytest.raises(ApplierError):
        apply_patch(baseline, patch)
    assert apply_patch(workspace, patch).applied
    with pytest.raises(ApplierError, match="already received a patch"):
        apply_patch(workspace, patch)
    workspaces.cleanup(workspace)
    with pytest.raises(ApplierError):
        apply_patch(workspace, patch)


def test_a_failed_application_still_consumes_the_workspace(repo, workspaces):
    conflict = "--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1,2 +1,2 @@\n def a():\n-    return 999\n+    return 10\n"
    _, patch = validate_patch(conflict, PatchScope(repo, frozenset({"pkg/a.py"})), PatchLimits())
    workspace = workspaces.create(repo, execution_id="e", label="patched-1")
    assert not apply_patch(workspace, patch).applied
    with pytest.raises(ApplierError):
        apply_patch(workspace, validated(MULTI, repo, "pkg/a.py", "pkg/b.py"))


def test_symlink_planted_in_the_workspace_is_refused(repo, workspaces, tmp_path):
    import os

    target = tmp_path / "victim.py"
    target.write_text("def a():\n    return 1\n")
    patch = validated(MULTI, repo, "pkg/a.py", "pkg/b.py")
    workspace = workspaces.create(repo, execution_id="e", label="patched-1")
    (workspace.repo / "pkg/a.py").unlink()
    os.symlink(target, workspace.repo / "pkg/a.py")
    outcome = apply_patch(workspace, patch)
    assert not outcome.applied and "symlink" in outcome.errors[0]
    assert target.read_text() == "def a():\n    return 1\n"


def test_hunk_without_trailing_context_applies_in_the_middle_of_a_file(write_repo, workspaces):
    root = write_repo({"m.py": "".join(f"x{i} = {i}\n" for i in range(1, 11))}, name="mid")
    diff = "--- a/m.py\n+++ b/m.py\n@@ -4,2 +4,2 @@\n x4 = 4\n-x5 = 5\n+x5 = 50\n"
    workspace = workspaces.create(root, execution_id="e", label="patched-1")
    outcome = apply_patch(workspace, validated(diff, root, "m.py"))
    assert outcome.applied, outcome.errors
    assert (workspace.repo / "m.py").read_text().splitlines()[4] == "x5 = 50"
