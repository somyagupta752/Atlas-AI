"""Patch validation: the security boundary between a model-generated diff and the filesystem.

A generated diff is untrusted input. ``validate_patch`` checks it against the
source tree (read-only) and an explicit allowlist of modifiable files, and
returns every error and warning it finds rather than stopping at the first.

Only a valid patch yields a ``ValidatedPatch``. It can only be constructed
here, and the applier accepts nothing else, so an unvalidated diff cannot be
applied by accident.

Rules:

- paths: relative, POSIX ``/`` separators only, a conservative character set,
  no ``..``/``.``/empty segments, no drive letters, no ``~``;
- protected: ``.git``, credential and ``.env`` files, virtualenvs, caches and
  build output, anything reached through a symlink;
- scope: modified files must be on the allowlist derived from the
  investigation; tests may not be changed unless the policy says so;
- content: supported file types only (Python), text files only, size limits,
  no duplicate files, no renames, no binary data, no no-op hunks;
- limits: file count, changed lines and diff size.
"""

import fnmatch
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.patching.diff_parser import (
    DiffParseError,
    ParsedDiff,
    anchor_context,
    parse_unified_diff,
    render_unified_diff,
)
from app.patching.models import PatchLimits, PatchValidationResult
from app.tools.base import IGNORED_DIRECTORIES, is_secret_file

SUPPORTED_SUFFIXES = frozenset({".py", ".pyi"})
SAFE_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@+-]*(/[A-Za-z0-9_][A-Za-z0-9_.@+-]*)*$")
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py", "conftest.py")
TEST_DIRECTORIES = frozenset({"tests", "test", "testing"})
PROTECTED_DIRECTORIES = frozenset({d.lower() for d in IGNORED_DIRECTORIES} | {".git", ".github", ".circleci"})


def is_test_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    return any(fnmatch.fnmatch(name, p) for p in TEST_FILE_PATTERNS) or any(
        part.lower() in TEST_DIRECTORIES for part in parts[:-1]
    )


def path_errors(path: str) -> list[str]:
    """Problems with a patch path as a string. Empty means syntactically safe."""
    if not path:
        return ["Empty file path"]
    if any(ord(ch) < 32 or ch == "\x7f" for ch in path):
        return [f"{path!r}: control characters are not allowed in paths"]
    errors: list[str] = []
    normalised = path
    if "\\" in path:
        errors.append(f"{path}: Windows-style path separators ('\\') are not allowed; use '/'")
        normalised = path.replace("\\", "/")
    if normalised.startswith(("/", "~", "//")) or re.match(r"^[A-Za-z]:", normalised):
        errors.append(f"{path}: absolute paths are not allowed")
    segments = normalised.split("/")
    if ".." in segments:
        errors.append(f"{path}: path traversal ('..') is not allowed")
    elif "." in segments or "" in segments[1:-1] or normalised.endswith("/"):
        errors.append(f"{path}: the path is not normalised")
    lowered = [s.lower() for s in segments]
    if any(s in PROTECTED_DIRECTORIES for s in lowered[:-1]) or (lowered and lowered[-1] == ".git"):
        errors.append(f"{path}: files under .git, virtualenvs, caches or build output cannot be modified")
    if any(is_secret_file(s) for s in segments if s):
        errors.append(f"{path}: credential and environment files cannot be modified")
    if not errors and not SAFE_PATH.match(normalised):
        errors.append(f"{path}: the path contains unsupported characters")
    if not errors and PurePosixPath(normalised).suffix not in SUPPORTED_SUFFIXES:
        errors.append(f"{path}: only Python source files ({', '.join(sorted(SUPPORTED_SUFFIXES))}) can be modified")
    return errors


def filesystem_errors(root: Path, path: str, operation: str, max_file_bytes: int) -> list[str]:
    """Problems with ``path`` inside ``root``. No component may be a symlink."""
    root = root.resolve()
    current = root
    parts = PurePosixPath(path).parts
    for depth, part in enumerate(parts):
        current = current / part
        try:
            is_link = current.is_symlink()
            exists = current.exists() or is_link
        except OSError:
            return [f"{path}: cannot be inspected"]
        if is_link:
            return [f"{path}: the path goes through a symlink, which is not allowed"]
        if not exists:
            if operation == "add" and depth == len(parts) - 1:
                return [] if current.parent.is_dir() else [f"{path}: the parent directory does not exist"]
            return [f"{path}: the file does not exist in the repository"]
    try:
        resolved = current.resolve(strict=True)
    except (OSError, RuntimeError):
        return [f"{path}: cannot be resolved"]
    if not resolved.is_relative_to(root):
        return [f"{path}: resolves outside the repository"]
    if operation == "add":
        return [f"{path}: the patch creates a file that already exists"]
    if not resolved.is_file():
        return [f"{path}: not a regular file"]
    size = resolved.stat().st_size
    if size > max_file_bytes:
        return [f"{path}: the file is larger than {max_file_bytes} bytes"]
    raw = resolved.read_bytes()
    if b"\x00" in raw[:8192]:
        return [f"{path}: binary files cannot be modified"]
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return [f"{path}: the file is not valid UTF-8 text"]
    return []


@dataclass(frozen=True)
class PatchScope:
    """What a patch may touch: the source tree it is checked against and the allowlist."""

    root: Path
    allowed_files: frozenset[str]


_TOKEN = object()


class ValidatedPatch:
    """A diff that passed validation. Only ``validate_patch`` can create one."""

    __slots__ = ("canonical_diff", "files", "parsed", "sha256", "validation")

    def __init__(self, token: object, parsed: ParsedDiff, validation: PatchValidationResult) -> None:
        if token is not _TOKEN or not validation.valid:
            raise TypeError("ValidatedPatch can only be created by validate_patch")
        self.parsed = parsed
        self.canonical_diff = render_unified_diff(parsed)
        self.sha256 = hashlib.sha256(self.canonical_diff.encode()).hexdigest()
        self.files = tuple(parsed.paths)
        self.validation = validation


def canonical_sha256(unified_diff: str) -> str | None:
    try:
        return hashlib.sha256(render_unified_diff(parse_unified_diff(unified_diff)).encode()).hexdigest()
    except DiffParseError:
        return None


def validate_patch(
    unified_diff: str,
    scope: PatchScope,
    limits: PatchLimits,
    *,
    claimed_files: list[str] | None = None,
) -> tuple[PatchValidationResult, ValidatedPatch | None]:
    """Validate ``unified_diff``. Returns the result and, only when valid, a ``ValidatedPatch``."""
    errors: list[str] = []
    warnings: list[str] = []

    size = len(unified_diff.encode("utf-8", errors="replace")) if isinstance(unified_diff, str) else 0
    if size > limits.max_diff_bytes:
        return PatchValidationResult(valid=False, errors=[f"The patch is larger than {limits.max_diff_bytes} bytes"]), None
    try:
        parsed = parse_unified_diff(unified_diff)
    except DiffParseError as exc:
        return PatchValidationResult(valid=False, errors=exc.errors, warnings=exc.warnings), None
    warnings.extend(parsed.warnings)

    seen: set[str] = set()
    for file_patch in parsed.files:
        path = file_patch.path
        if path in seen:
            errors.append(f"{path}: the file is modified more than once in the patch")
            continue
        seen.add(path)
        syntax = path_errors(path)
        errors.extend(syntax)
        if syntax:
            continue
        operation = file_patch.operation
        if operation == "add" and not limits.allow_new_files:
            errors.append(f"{path}: the patch creates a new file, which is not allowed")
            continue
        if operation == "delete" and not limits.allow_deleted_files:
            errors.append(f"{path}: the patch deletes a file, which is not allowed")
            continue
        if is_test_path(path) and not limits.allow_test_changes:
            errors.append(
                f"{path}: the patch modifies a test. Tests must not be changed to make them pass; fix the source instead"
            )
            continue
        if operation == "add":
            parent = PurePosixPath(path).parent.as_posix()
            if not any(PurePosixPath(a).parent.as_posix() == parent for a in scope.allowed_files):
                errors.append(f"{path}: new files may only be created next to an allowed file")
                continue
        elif path not in scope.allowed_files:
            errors.append(f"{path}: outside the approved file scope ({', '.join(sorted(scope.allowed_files)) or 'none'})")
            continue
        errors.extend(filesystem_errors(scope.root, path, operation, limits.max_file_bytes))
        noop = [h for h in file_patch.hunks if h.is_noop]
        if noop and len(noop) == len(file_patch.hunks):
            errors.append(f"{path}: the patch does not change the file (removed and added lines are identical)")
        elif noop:
            warnings.append(f"{path}: {len(noop)} hunk(s) change nothing")

    if len(parsed.files) > limits.max_files:
        errors.append(f"The patch modifies {len(parsed.files)} files; the limit is {limits.max_files}")
    changed = parsed.added + parsed.removed
    if changed > limits.max_changed_lines:
        errors.append(f"The patch changes {changed} lines; the limit is {limits.max_changed_lines}")
    if claimed_files is not None:
        claimed = {c.strip().removeprefix("./") for c in claimed_files}
        if claimed != set(parsed.paths):
            warnings.append(
                "The proposal's affected_files list does not match the diff; the diff is authoritative "
                f"(diff: {sorted(parsed.paths)}, claimed: {sorted(claimed)})"
            )

    result = PatchValidationResult(
        valid=not errors,
        errors=errors,
        warnings=warnings,
        affected_files=sorted(set(parsed.paths)),
        added_lines=parsed.added,
        removed_lines=parsed.removed,
    )
    if errors:
        return result, None
    sources = {
        f.path: (scope.root / f.path).read_text(encoding="utf-8") for f in parsed.files if f.operation == "modify"
    }
    parsed, notes = anchor_context(parsed, sources)
    result.warnings.extend(notes)
    validated = ValidatedPatch(_TOKEN, parsed, result)
    result.diff_sha256 = validated.sha256
    return result, validated


def derive_allowed_files(root: Path, candidates: list[str], limits: PatchLimits) -> tuple[list[str], list[str]]:
    """The allowlist: candidate paths that are safe, existing, non-test source files.

    Returns ``(allowed, excluded_with_reasons)``. Candidates come from the
    investigation (affected files and source evidence), never from the patch.
    """
    allowed: list[str] = []
    excluded: list[str] = []
    for raw in dict.fromkeys(c.strip().removeprefix("./") for c in candidates if c and c.strip()):
        problems = path_errors(raw)
        if not problems and is_test_path(raw) and not limits.allow_test_changes:
            problems = [f"{raw}: test file (read-only)"]
        if not problems:
            problems = filesystem_errors(root, raw, "modify", limits.max_file_bytes)
        if problems:
            excluded.append(problems[0])
        else:
            allowed.append(raw)
    return sorted(allowed), excluded
