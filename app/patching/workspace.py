"""Isolated, disposable working copies. The original repository is never written.

Layout of one workspace (a fresh directory per patch attempt, mode 0700)::

    <base>/<execution>-<label>-<random>/
        repo/   copy of the approved repository; patches and tests run here
        meta/   Atlas-owned files: the canonical diff, JUnit reports, HOME, TMPDIR
        workspace.json   ownership marker, checked before deletion

Copy rules:

- ``.git``, virtualenvs, ``node_modules``, caches and build output are excluded;
- credential and ``.env`` files are excluded;
- symlinks are never followed out of the repository. A symlink to a file inside
  the repository is copied as a regular file; any other symlink is skipped;
- the copy is hashed while it is written. The same walk over the original gives
  the same ``root_hash``, so the service can prove the original is unchanged.

A workspace belongs to one execution and one label. A ``patched`` workspace
accepts exactly one patch; the baseline workspace accepts none.

A temporary directory is not a security sandbox: test code can still reach the
rest of the filesystem and the network. Untrusted repositories need container workers.
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.patching.models import SourceSnapshot
from app.tools.base import IGNORED_DIRECTORIES, is_secret_file

EXCLUDED_DIRECTORIES = frozenset(IGNORED_DIRECTORIES | {"htmlcov", ".hypothesis", ".cache"})
MARKER = "workspace.json"
Retention = Literal["delete", "keep_failed", "keep"]


class WorkspaceError(Exception):
    """A workspace could not be created or used. Messages never contain file contents."""


class InsufficientDiskSpaceError(WorkspaceError):
    pass


class SourceTooLargeError(WorkspaceError):
    pass


@dataclass(frozen=True)
class _Entry:
    relative: str
    source: Path  # the file whose bytes are copied (a symlink's resolved target)
    size: int
    executable: bool


def _scan(root: Path, max_files: int) -> tuple[list[_Entry], list[str]]:
    """The files a workspace copy contains, and what was skipped (with reasons)."""
    root = root.resolve()
    entries: list[_Entry] = []
    skipped: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        keep = []
        for name in sorted(dirnames):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if name in EXCLUDED_DIRECTORIES or name.endswith(".egg-info"):
                continue  # expected exclusions, not worth reporting
            if path.is_symlink():
                skipped.append(f"{relative}: directory symlink not copied")
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if is_secret_file(relative):
                skipped.append(f"{relative}: credential file not copied")
                continue
            try:
                info = path.lstat()
                source = path
                if stat.S_ISLNK(info.st_mode):
                    target = path.resolve(strict=True)
                    if not target.is_relative_to(root) or is_secret_file(target.name):
                        skipped.append(f"{relative}: symlink leaves the repository, not copied")
                        continue
                    info = target.stat()
                    source = target
                if not stat.S_ISREG(info.st_mode):
                    skipped.append(f"{relative}: not a regular file, not copied")
                    continue
            except (OSError, RuntimeError):
                skipped.append(f"{relative}: unreadable, not copied")
                continue
            entries.append(_Entry(relative, source, info.st_size, bool(info.st_mode & stat.S_IXUSR)))
            if len(entries) > max_files:
                raise SourceTooLargeError(f"The repository has more than {max_files} files")
    return entries, skipped


def _digest(lines: list[str]) -> str:
    return hashlib.sha256("".join(sorted(lines)).encode()).hexdigest()


def _hash_line(relative: str, file_hash: str, executable: bool) -> str:
    return f"{relative}\0{file_hash}\0{int(executable)}\n"


def snapshot_tree(root: Path, *, max_files: int = 50_000, with_revision: bool = False) -> SourceSnapshot:
    """Hash the files a workspace copy of ``root`` would contain. Reads only."""
    entries, skipped = _scan(root, max_files)
    lines = []
    for entry in entries:
        digest = hashlib.sha256()
        with entry.source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                digest.update(chunk)
        lines.append(_hash_line(entry.relative, digest.hexdigest(), entry.executable))
    revision, dirty = git_revision(root) if with_revision else (None, None)
    return SourceSnapshot(
        root_hash=_digest(lines),
        file_count=len(entries),
        total_bytes=sum(e.size for e in entries),
        revision=revision,
        dirty=dirty,
        skipped=skipped[:200],
    )


def git_revision(root: Path) -> tuple[str | None, bool | None]:
    """``(HEAD, has uncommitted changes under root)`` when ``root`` is in a git repository."""
    git = shutil.which("git")
    if git is None:
        return None, None
    env = {"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "LANG": "C"}
    try:
        head = subprocess.run(
            [git, "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, env=env, check=False
        )
        if head.returncode != 0:
            return None, None
        status = subprocess.run(
            [git, "-C", str(root), "status", "--porcelain", "--", "."],
            capture_output=True, text=True, timeout=10, env=env, check=False,
        )
        return head.stdout.strip() or None, (bool(status.stdout.strip()) if status.returncode == 0 else None)
    except (OSError, subprocess.SubprocessError):
        return None, None


@dataclass
class Workspace:
    workspace_id: str
    execution_id: str
    label: str
    path: Path  # the workspace directory
    snapshot: SourceSnapshot
    created_at: float = field(default_factory=time.time)
    patch_applied: bool = False
    cleaned: bool = False

    @property
    def repo(self) -> Path:
        return self.path / "repo"

    @property
    def meta(self) -> Path:
        return self.path / "meta"

    @property
    def is_baseline(self) -> bool:
        return self.label == "baseline"

    def describe(self, retained: bool | None = None) -> dict:
        """Safe to persist and return: no filesystem paths."""
        return {
            "workspace_id": self.workspace_id,
            "label": self.label,
            "snapshot_hash": self.snapshot.root_hash,
            "file_count": self.snapshot.file_count,
            "patch_applied": self.patch_applied,
            "retained": (not self.cleaned) if retained is None else retained,
        }


class WorkspaceManager:
    def __init__(
        self,
        base_dir: Path | None = None,
        *,
        retention: Retention = "delete",
        max_age_seconds: float = 24 * 3600,
        max_files: int = 20_000,
        max_total_bytes: int = 500 * 1024**2,
        min_free_bytes: int = 200 * 1024**2,
        disk_usage=shutil.disk_usage,
    ) -> None:
        self.base_dir = (base_dir or Path(tempfile.gettempdir()) / "atlas-ai" / "patch-workspaces").expanduser()
        self.retention = retention
        self.max_age_seconds = max_age_seconds
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self.min_free_bytes = min_free_bytes
        self._disk_usage = disk_usage
        self._lock = threading.Lock()
        self._active: dict[str, Workspace] = {}

    # --- lifecycle --------------------------------------------------------------------

    def create(self, source_root: Path, *, execution_id: str, label: str) -> Workspace:
        """Copy ``source_root`` into a new, unique workspace. Raises ``WorkspaceError``."""
        source_root = source_root.resolve()
        if not source_root.is_dir():
            raise WorkspaceError("The source repository does not exist")
        base = self._ensure_base()
        if source_root.is_relative_to(base) or base.is_relative_to(source_root):
            raise WorkspaceError("The workspace directory must be outside the source repository")
        entries, skipped = _scan(source_root, self.max_files)
        total = sum(e.size for e in entries)
        if total > self.max_total_bytes:
            raise SourceTooLargeError(f"The repository is larger than {self.max_total_bytes} bytes")
        try:
            free = self._disk_usage(base).free
        except OSError as exc:
            raise WorkspaceError("Cannot determine free disk space") from exc
        if free < total + self.min_free_bytes:
            raise InsufficientDiskSpaceError("Not enough free disk space for an isolated workspace")

        safe_label = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in label)[:40]
        path = Path(tempfile.mkdtemp(prefix=f"{execution_id[:8]}-{safe_label}-", dir=base))
        workspace_id = path.name
        try:
            (path / "repo").mkdir(mode=0o700)
            for name in ("meta", "meta/home", "meta/tmp"):
                (path / name).mkdir(mode=0o700)
            lines = [self._copy(entry, path / "repo") for entry in entries]
            revision, dirty = git_revision(source_root)
            snapshot = SourceSnapshot(
                root_hash=_digest(lines),
                file_count=len(entries),
                total_bytes=total,
                revision=revision,
                dirty=dirty,
                skipped=skipped[:200],
            )
            (path / MARKER).write_text(
                json.dumps({"workspace_id": workspace_id, "execution_id": execution_id, "label": label})
            )
        except OSError as exc:
            shutil.rmtree(path, ignore_errors=True)
            raise WorkspaceError(f"The workspace could not be created: {type(exc).__name__}") from exc
        workspace = Workspace(workspace_id, execution_id, label, path, snapshot)
        with self._lock:
            self._active[workspace_id] = workspace
        return workspace

    @staticmethod
    def _copy(entry: _Entry, repo: Path) -> str:
        target = repo / entry.relative
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        with entry.source.open("rb") as source, os.fdopen(os.open(target, flags, 0o600), "wb") as out:
            for chunk in iter(lambda: source.read(1 << 16), b""):
                digest.update(chunk)
                out.write(chunk)
        os.chmod(target, 0o700 if entry.executable else 0o600)
        return _hash_line(entry.relative, digest.hexdigest(), entry.executable)

    def release(self, workspace: Workspace, *, success: bool) -> bool:
        """Apply the retention policy. Returns True when the workspace was kept."""
        keep = self.retention == "keep" or (self.retention == "keep_failed" and not success)
        if not keep:
            self.cleanup(workspace)
        else:
            with self._lock:
                self._active.pop(workspace.workspace_id, None)
        return keep

    def cleanup(self, workspace: Workspace) -> None:
        if workspace.cleaned:
            return
        self._remove(workspace.path, workspace.workspace_id)
        workspace.cleaned = True
        with self._lock:
            self._active.pop(workspace.workspace_id, None)

    def purge_all_inactive(self) -> int:
        """Delete every workspace this process is not using. For startup, after a crash or restart."""
        return self.purge_expired(now=float("inf"))

    def purge_expired(self, now: float | None = None) -> int:
        """Delete retained workspaces older than ``max_age_seconds``. Returns the count."""
        if not self.base_dir.is_dir():
            return 0
        now = time.time() if now is None else now
        removed = 0
        with self._lock:
            active = set(self._active)
        for path in self.base_dir.iterdir():
            if path.name in active or not path.is_dir() or path.is_symlink():
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age <= self.max_age_seconds:
                continue
            try:
                removed += self._remove(path, path.name, missing_ok=True)
            except (WorkspaceError, OSError):
                continue  # not ours, or already gone
        return removed

    def active(self) -> list[Workspace]:
        with self._lock:
            return list(self._active.values())

    # --- internals --------------------------------------------------------------------

    def _ensure_base(self) -> Path:
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise WorkspaceError("The workspace directory cannot be created") from exc
        if self.base_dir.is_symlink():
            raise WorkspaceError("The workspace directory must not be a symlink")
        return self.base_dir.resolve()

    def _remove(self, path: Path, workspace_id: str, *, missing_ok: bool = False) -> bool:
        """Delete ``path`` only if it is a direct child of the base with a matching marker."""
        base = self.base_dir.resolve()
        if path.is_symlink() or path.resolve().parent != base:
            raise WorkspaceError("Refusing to delete a directory outside the workspace base")
        try:
            marker = json.loads((path / MARKER).read_text())
        except (OSError, ValueError):
            if missing_ok:
                return False
            raise WorkspaceError("Refusing to delete a directory without a workspace marker") from None
        if marker.get("workspace_id") != workspace_id:
            raise WorkspaceError("Workspace marker does not match")
        shutil.rmtree(path, ignore_errors=False)
        return True


def iter_files(root: Path) -> Iterator[tuple[str, str]]:
    """``(relative path, sha256)`` for every regular file under ``root``, symlinks included as links."""
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                yield relative, "symlink:" + os.readlink(path)
                continue
            try:
                yield relative, hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                yield relative, "unreadable"
