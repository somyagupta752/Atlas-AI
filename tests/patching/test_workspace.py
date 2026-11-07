"""Workspace isolation. Core invariant: no patch attempt can modify the original repository."""

import hashlib
import json
import os
import subprocess
import threading
import time
from collections import namedtuple

import pytest

from app.patching.applier import apply_patch
from app.patching.models import PatchLimits
from app.patching.validator import PatchScope, validate_patch
from app.patching.workspace import (
    InsufficientDiskSpaceError,
    SourceTooLargeError,
    WorkspaceError,
    WorkspaceManager,
    snapshot_tree,
)
from tests.patching.conftest import AVERAGE_FIX

Usage = namedtuple("Usage", "total used free")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(root):
    return {
        p.relative_to(root).as_posix(): (os.readlink(p) if p.is_symlink() else sha(p))
        for p in sorted(root.rglob("*")) if p.is_file() or p.is_symlink()
    }


def test_patch_changes_the_copy_and_never_the_original(average_repo, workspaces):
    original = average_repo / "solution.py"
    before_file, before_tree = sha(original), tree_hashes(average_repo)

    workspace = workspaces.create(average_repo, execution_id="exec-1", label="patched-1")
    _, validated = validate_patch(AVERAGE_FIX, PatchScope(average_repo, frozenset({"solution.py"})), PatchLimits())
    result = apply_patch(workspace, validated)

    assert result.applied, result.errors
    assert sha(workspace.repo / "solution.py") != before_file
    assert "if not numbers" in (workspace.repo / "solution.py").read_text()
    assert sha(original) == before_file
    assert tree_hashes(average_repo) == before_tree
    assert snapshot_tree(average_repo).root_hash == workspace.snapshot.root_hash


def test_copy_hash_matches_the_original_snapshot(average_repo, workspaces):
    workspace = workspaces.create(average_repo, execution_id="e", label="baseline")
    assert workspace.snapshot.root_hash == snapshot_tree(average_repo).root_hash
    assert workspace.snapshot.root_hash == snapshot_tree(workspace.repo).root_hash
    assert workspace.snapshot.file_count == 2


def test_excluded_directories_and_credentials_are_not_copied(write_repo, workspaces):
    root = write_repo(
        {
            "app.py": "x = 1\n",
            ".git/config": "[core]\n",
            ".venv/lib/site.py": "",
            "node_modules/pkg/index.js": "",
            "pkg/__pycache__/a.cpython-312.pyc": "",
            ".pytest_cache/v": "",
            "build/out.py": "",
            "pkg.egg-info/PKG-INFO": "",
            ".env": "API_KEY=secret\n",
            ".env.local": "API_KEY=secret\n",
            "deploy/id_rsa": "KEY",
            "certs/server.pem": "KEY",
            "credentials.json": "{}",
            ".env.example": "API_KEY=\n",
        }
    )
    workspace = workspaces.create(root, execution_id="e", label="baseline")
    copied = sorted(p.relative_to(workspace.repo).as_posix() for p in workspace.repo.rglob("*") if p.is_file())
    assert copied == [".env.example", "app.py"]
    assert any("credential" in s for s in workspace.snapshot.skipped)


def test_symlinks_never_escape_the_repository(write_repo, workspaces, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    root = write_repo({"app.py": "x = 1\n", "lib/inner.py": "y = 2\n"})
    os.symlink(secret, root / "escape.py")
    os.symlink(tmp_path, root / "escape_dir")
    os.symlink(root / "lib" / "inner.py", root / "alias.py")
    os.symlink("/etc/passwd", root / "passwd.py")

    workspace = workspaces.create(root, execution_id="e", label="baseline")
    files = {p.relative_to(workspace.repo).as_posix(): p for p in workspace.repo.rglob("*")}
    assert "escape.py" not in files and "escape_dir" not in files and "passwd.py" not in files
    assert files["alias.py"].read_text() == "y = 2\n" and not files["alias.py"].is_symlink()
    assert not any(p.is_symlink() for p in workspace.repo.rglob("*"))
    assert all("TOP SECRET" not in p.read_text() for p in workspace.repo.rglob("*") if p.is_file())
    assert any("symlink" in s for s in workspace.snapshot.skipped)


def test_workspace_layout_permissions_and_marker(average_repo, workspaces):
    workspace = workspaces.create(average_repo, execution_id="exec-123", label="baseline")
    assert workspace.repo.is_dir() and (workspace.meta / "home").is_dir() and (workspace.meta / "tmp").is_dir()
    assert oct(workspace.path.stat().st_mode & 0o777) == "0o700"
    marker = json.loads((workspace.path / "workspace.json").read_text())
    assert marker == {"workspace_id": workspace.workspace_id, "execution_id": "exec-123", "label": "baseline"}
    assert str(workspace.path) not in json.dumps(workspace.describe())


def test_cleanup_and_retention(average_repo, tmp_path):
    deleting = WorkspaceManager(tmp_path / "a")
    workspace = deleting.create(average_repo, execution_id="e", label="patched-1")
    assert deleting.release(workspace, success=False) is False
    assert not workspace.path.exists() and workspace.cleaned and deleting.active() == []

    keeping = WorkspaceManager(tmp_path / "b", retention="keep_failed")
    failed = keeping.create(average_repo, execution_id="e", label="patched-1")
    passed = keeping.create(average_repo, execution_id="e", label="patched-2")
    assert keeping.release(failed, success=False) is True and failed.path.exists()
    assert keeping.release(passed, success=True) is False and not passed.path.exists()


def test_purge_expired_only_removes_old_marked_workspaces(average_repo, tmp_path):
    manager = WorkspaceManager(tmp_path / "ws", retention="keep", max_age_seconds=60)
    old = manager.create(average_repo, execution_id="e", label="patched-1")
    manager.release(old, success=False)
    foreign = manager.base_dir / "not-a-workspace"
    foreign.mkdir()
    os.utime(old.path, (time.time() - 3600, time.time() - 3600))
    os.utime(foreign, (time.time() - 3600, time.time() - 3600))
    assert manager.purge_expired() == 1
    assert not old.path.exists() and foreign.exists()


def test_cleanup_refuses_directories_that_are_not_workspaces(average_repo, workspaces, tmp_path):
    workspace = workspaces.create(average_repo, execution_id="e", label="baseline")
    (workspace.path / "workspace.json").unlink()
    with pytest.raises(WorkspaceError):
        workspaces.cleanup(workspace)
    assert workspace.path.exists()
    workspace.path = tmp_path  # outside the base
    with pytest.raises(WorkspaceError):
        workspaces.cleanup(workspace)
    assert average_repo.exists()


def test_concurrent_creation_gives_unique_isolated_workspaces(average_repo, workspaces):
    created, errors = [], []

    def make(index):
        try:
            created.append(workspaces.create(average_repo, execution_id=f"exec-{index}", label="patched-1"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=make, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len({w.path for w in created}) == 8
    assert len({w.snapshot.root_hash for w in created}) == 1
    (created[0].repo / "solution.py").write_text("changed\n")
    assert (created[1].repo / "solution.py").read_text() != "changed\n"


def test_missing_source_and_limits(tmp_path, average_repo):
    manager = WorkspaceManager(tmp_path / "ws")
    with pytest.raises(WorkspaceError):
        manager.create(tmp_path / "missing", execution_id="e", label="baseline")
    with pytest.raises(SourceTooLargeError):
        WorkspaceManager(tmp_path / "ws2", max_files=1).create(average_repo, execution_id="e", label="baseline")
    with pytest.raises(SourceTooLargeError):
        WorkspaceManager(tmp_path / "ws3", max_total_bytes=10).create(average_repo, execution_id="e", label="baseline")


def test_insufficient_disk_space_is_refused_before_copying(tmp_path, average_repo):
    manager = WorkspaceManager(tmp_path / "ws", disk_usage=lambda _: Usage(10**9, 10**9, 1024))
    with pytest.raises(InsufficientDiskSpaceError):
        manager.create(average_repo, execution_id="e", label="baseline")
    assert list((tmp_path / "ws").iterdir()) == []


def test_workspace_base_inside_the_source_is_refused(average_repo):
    with pytest.raises(WorkspaceError):
        WorkspaceManager(average_repo / "ws").create(average_repo, execution_id="e", label="baseline")


def test_source_revision_is_recorded_for_git_repositories(write_repo, workspaces):
    root = write_repo({"app.py": "x = 1\n"})
    env = {"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com"}
    for command in (["git", "init", "-q"], ["git", "add", "."], ["git", "commit", "-qm", "init"]):
        subprocess.run(command, cwd=root, env=env, check=True)
    workspace = workspaces.create(root, execution_id="e", label="baseline")
    assert workspace.snapshot.revision and len(workspace.snapshot.revision) == 40
    assert workspace.snapshot.dirty is False
    assert not (workspace.repo / ".git").exists()
