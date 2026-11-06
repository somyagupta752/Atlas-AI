"""Discover Python files inside a single repository root."""

import os
from dataclasses import dataclass, field
from pathlib import Path

IGNORED_DIRECTORIES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".eggs",
        "node_modules",
        "site-packages",
        "build",
        "dist",
    }
)


class InvalidRepositoryError(Exception):
    """The supplied repository path cannot be analyzed."""


class RepositoryNotFoundError(InvalidRepositoryError):
    """The repository path does not exist."""


class RepositoryNotADirectoryError(InvalidRepositoryError):
    """The repository path exists but is not a directory."""


@dataclass(frozen=True)
class ScanIssue:
    """A directory that could not be listed. ``path`` is relative to the root."""

    path: str
    message: str


@dataclass
class ScanResult:
    root: Path
    files: list[str] = field(default_factory=list)
    errors: list[ScanIssue] = field(default_factory=list)


def resolve_repository_root(repository_path: str | Path) -> Path:
    """Return the absolute, symlink-free repository root.

    Raises:
        RepositoryNotFoundError: if the path does not exist.
        RepositoryNotADirectoryError: if the path is not a directory.
    """
    root = Path(repository_path).expanduser().resolve()
    if not root.exists():
        raise RepositoryNotFoundError(f"Repository not found: {repository_path}")
    if not root.is_dir():
        raise RepositoryNotADirectoryError(f"Repository path is not a directory: {repository_path}")
    return root


def is_ignored_directory(name: str) -> bool:
    """Whether a directory with this name is skipped during scanning."""
    return name in IGNORED_DIRECTORIES or name.endswith(".egg-info")


def scan_python_files(repository_path: str | Path) -> ScanResult:
    """Recursively find ``.py`` files under ``repository_path``.

    The scan never leaves the root: symlinked directories are not followed, and
    symlinked files are skipped when they point outside the root. Unreadable
    directories are reported in ``ScanResult.errors`` instead of aborting the scan.

    Returns:
        A ``ScanResult`` whose ``files`` are sorted, root-relative POSIX paths.
    """
    root = resolve_repository_root(repository_path)
    result = ScanResult(root=root)

    def on_error(error: OSError) -> None:
        location = Path(error.filename) if error.filename else root
        result.errors.append(
            ScanIssue(path=_relative(location, root), message=error.strerror or str(error))
        )

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error, followlinks=False):
        # Pruning in place stops os.walk from descending into ignored directories.
        dirnames[:] = sorted(name for name in dirnames if not is_ignored_directory(name))
        current = Path(dirpath)
        for name in filenames:
            if name.endswith(".py") and _is_regular_file_inside(current / name, root):
                result.files.append(_relative(current / name, root))

    result.files.sort()
    return result


def _is_regular_file_inside(path: Path, root: Path) -> bool:
    try:
        return path.is_file() and path.resolve().is_relative_to(root)
    except (OSError, RuntimeError):  # RuntimeError: symlink loop on Python < 3.13
        return False


def _relative(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
    return relative or "."
