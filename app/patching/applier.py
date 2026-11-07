"""Apply a validated patch to one isolated workspace with ``git apply``.

The model never supplies a command. The application controls every part:

    git apply [--check | --numstat --summary] -p1 --whitespace=nowarn -- <meta>/patch.diff

- the patch file is the validator's canonical rendering, not the model's text;
- the working directory is the workspace copy, which has no ``.git``;
  ``GIT_CEILING_DIRECTORIES`` stops git from finding a repository above it;
- the environment is minimal; system and global git config are disabled.

``git apply`` is atomic (all hunks or none), refuses ``..`` paths and refuses to
write through symlinks. On top of that the applier:

1. checks the workspace is a fresh ``patched`` workspace (never the baseline,
   never one that already received a patch);
2. cross-checks git's own reading of the patch (files, added/removed counts)
   against the validator's parse, so a parser disagreement cannot slip through;
3. dry-runs with ``--check`` to detect conflicts before touching anything;
4. hashes the whole workspace before and after, and fails if any file outside
   the patch changed, appeared or disappeared;
5. confirms each patched file contains the hunks' new text, in order.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

from app.patching.models import PatchApplyResult
from app.patching.validator import ValidatedPatch
from app.patching.workspace import Workspace, iter_files
from app.tools.base import redact_secrets, truncate_text


class ApplierError(Exception):
    pass


def _git_env(workspace: Workspace) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(workspace.meta / "home"),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CEILING_DIRECTORIES": str(workspace.path),
        "GIT_TERMINAL_PROMPT": "0",
    }


def _clean(text: str, workspace: Workspace) -> str:
    text = text.replace(str(workspace.repo), ".").replace(str(workspace.meta), "<meta>")
    return truncate_text(redact_secrets(text), 8_000, keep="head")[0]


def _run_git(workspace: Workspace, args: list[str], timeout: float) -> subprocess.CompletedProcess:
    git = shutil.which("git")
    if git is None:
        raise ApplierError("git is not available")
    return subprocess.run(
        [git, "apply", *args, "-p1", "--whitespace=nowarn", "--", str(workspace.meta / "patch.diff")],
        cwd=workspace.repo,
        env=_git_env(workspace),
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _numstat_matches(output: str, patch: ValidatedPatch) -> list[str]:
    """Compare git's view of the patch with the parser's. Returns disagreements."""
    expected = {f.path: (f.added, f.removed) for f in patch.parsed.files}
    seen: dict[str, tuple[int, int]] = {}
    problems: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        if line.startswith(" "):
            summary = line.strip()
            if summary.startswith(("rename", "copy", "mode change")):
                problems.append(f"git reports an unsupported operation: {summary[:120]}")
            continue
        parts = line.split("\t")
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            problems.append("git reports a binary or unreadable change")
            continue
        seen[parts[2]] = (int(parts[0]), int(parts[1]))
    if seen != expected:
        problems.append(f"git and the validator disagree about the patch (git: {sorted(seen)}, validator: {sorted(expected)})")
    return problems


def _content_problems(workspace: Workspace, patch: ValidatedPatch, before: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for file_patch in patch.parsed.files:
        path = workspace.repo / file_patch.path
        if file_patch.operation == "delete":
            if path.exists() or path.is_symlink():
                problems.append(f"{file_patch.path}: should have been deleted")
            continue
        try:
            lines = path.read_text(encoding="utf-8").split("\n")
        except (OSError, UnicodeDecodeError):
            problems.append(f"{file_patch.path}: unreadable after the patch")
            continue
        if lines and lines[-1] == "":
            lines.pop()
        position = 0
        for hunk in file_patch.hunks:
            block = hunk.new_block
            found = next(
                (i for i in range(position, len(lines) - len(block) + 1) if lines[i : i + len(block)] == block), None
            )
            if found is None:
                problems.append(f"{file_patch.path}: the patched file does not contain the hunk at line {hunk.new_start}")
                break
            position = found + len(block)
        if file_patch.operation == "modify" and file_patch.path in before:
            original = (workspace.meta / "originals" / file_patch.path)
            if original.is_file():
                old_lines = original.read_text(encoding="utf-8").split("\n")
                if old_lines and old_lines[-1] == "":
                    old_lines.pop()
                if len(lines) != len(old_lines) + file_patch.added - file_patch.removed:
                    problems.append(f"{file_patch.path}: the line count after the patch is not what the diff implies")
    return problems


def apply_patch(workspace: Workspace, patch: ValidatedPatch, *, timeout: float = 30.0) -> PatchApplyResult:
    """Apply ``patch`` inside ``workspace``. Never raises for patch problems: they are results."""
    if not isinstance(patch, ValidatedPatch):
        raise TypeError("apply_patch only accepts a ValidatedPatch")
    if workspace.is_baseline:
        raise ApplierError("The baseline workspace never receives a patch")
    if workspace.patch_applied:
        raise ApplierError("This workspace already received a patch; create a fresh one")
    if workspace.cleaned or not workspace.repo.is_dir():
        raise ApplierError("The workspace no longer exists")
    start = time.monotonic()

    def result(**kwargs) -> PatchApplyResult:
        return PatchApplyResult(duration_seconds=round(time.monotonic() - start, 3), **kwargs)

    # Defence in depth: the validator checked the source tree; check the copy too.
    repo = workspace.repo.resolve()
    for relative in patch.files:
        target = repo / relative
        current = repo
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                return result(applied=False, error_kind="invalid", errors=[f"{relative}: path goes through a symlink"])
        if not target.resolve().is_relative_to(repo):
            return result(applied=False, error_kind="invalid", errors=[f"{relative}: resolves outside the workspace"])

    workspace.patch_applied = True  # even a failed attempt makes this workspace unusable
    (workspace.meta / "patch.diff").write_text(patch.canonical_diff, encoding="utf-8")
    before = dict(iter_files(workspace.repo))
    for file_patch in patch.parsed.files:  # keep originals for the line-count check
        source = workspace.repo / file_patch.path
        if file_patch.operation == "modify" and source.is_file() and not source.is_symlink():
            copy = workspace.meta / "originals" / file_patch.path
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, copy)

    try:
        stat = _run_git(workspace, ["--numstat", "--summary"], timeout)
        if stat.returncode != 0:
            return result(applied=False, error_kind="conflict", errors=["git could not read the patch"],
                          stderr=_clean(stat.stderr, workspace))
        disagreements = _numstat_matches(stat.stdout, patch)
        if disagreements:
            return result(applied=False, error_kind="invalid", errors=disagreements)
        check = _run_git(workspace, ["--check", "--verbose"], timeout)
        if check.returncode != 0:
            return result(
                applied=False,
                error_kind="conflict",
                errors=["The patch does not apply to the current source: its context or removed lines do not match"],
                stdout=_clean(check.stdout, workspace),
                stderr=_clean(check.stderr, workspace),
            )
        applied = _run_git(workspace, [], timeout)
    except ApplierError as exc:
        return result(applied=False, error_kind="environment", errors=[str(exc)])
    except subprocess.TimeoutExpired:
        return result(applied=False, error_kind="environment", errors=["git apply timed out"])
    if applied.returncode != 0:
        return result(applied=False, error_kind="conflict", errors=["git apply failed"],
                      stderr=_clean(applied.stderr, workspace))

    after = dict(iter_files(workspace.repo))
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    expected = set(patch.files)
    unexpected = sorted(set(changed) - expected)
    untouched = sorted(expected - set(changed))
    if unexpected or untouched:
        errors = [f"Unexpected change to {p}" for p in unexpected] + [f"{p} was not changed" for p in untouched]
        return result(applied=False, error_kind="unexpected_changes", errors=errors, unexpected_changes=unexpected,
                      files_changed=changed)
    problems = _content_problems(workspace, patch, before)
    if problems:
        return result(applied=False, error_kind="content_mismatch", errors=problems, files_changed=changed)
    return result(applied=True, files_changed=changed, stdout=_clean(applied.stdout, workspace))
