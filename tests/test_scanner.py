import os
from pathlib import Path

import pytest

from app.code_intelligence.scanner import (
    RepositoryNotADirectoryError,
    RepositoryNotFoundError,
    scan_python_files,
)


def _write(root: Path, relative: str, content: str = "") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_finds_python_files_in_nested_directories(tmp_path):
    for relative in ["main.py", "services/user.py", "services/payment.py", "tests/test_user.py"]:
        _write(tmp_path, relative)
    _write(tmp_path, "README.md")
    _write(tmp_path, "services/notes.txt")

    result = scan_python_files(tmp_path)

    assert result.files == [
        "main.py",
        "services/payment.py",
        "services/user.py",
        "tests/test_user.py",
    ]
    assert result.errors == []


def test_returns_relative_posix_paths(tmp_path):
    _write(tmp_path, "a/b/c/deep.py")

    assert scan_python_files(tmp_path).files == ["a/b/c/deep.py"]


@pytest.mark.parametrize(
    "ignored",
    [".git", ".venv", "venv", "__pycache__", "node_modules", "build", "dist", "pkg.egg-info"],
)
def test_ignores_generated_and_tooling_directories(tmp_path, ignored):
    _write(tmp_path, "app.py")
    _write(tmp_path, f"{ignored}/hidden.py")
    _write(tmp_path, f"src/{ignored}/nested_hidden.py")

    assert scan_python_files(tmp_path).files == ["app.py"]


def test_output_is_deterministic_and_sorted(tmp_path):
    for relative in ["z.py", "b/x.py", "a.py", "b/a.py", "m/n/o.py"]:
        _write(tmp_path, relative)

    first = scan_python_files(tmp_path).files
    second = scan_python_files(tmp_path).files

    assert first == second == sorted(first)


def test_empty_repository_returns_no_files(tmp_path):
    assert scan_python_files(tmp_path).files == []


def test_missing_directory_raises(tmp_path):
    with pytest.raises(RepositoryNotFoundError):
        scan_python_files(tmp_path / "does-not-exist")


def test_file_instead_of_directory_raises(tmp_path):
    file_path = _write(tmp_path, "main.py")

    with pytest.raises(RepositoryNotADirectoryError):
        scan_python_files(file_path)


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX perms, non-root")
def test_unreadable_directory_is_reported_not_fatal(tmp_path):
    _write(tmp_path, "ok.py")
    locked = tmp_path / "locked"
    _write(tmp_path, "locked/secret.py")
    locked.chmod(0)
    try:
        result = scan_python_files(tmp_path)
    finally:
        locked.chmod(0o755)

    assert result.files == ["ok.py"]
    assert [issue.path for issue in result.errors] == ["locked"]


def test_does_not_follow_symlinks_outside_the_root(tmp_path):
    outside = tmp_path / "outside"
    _write(outside, "external.py")
    repo = tmp_path / "repo"
    _write(repo, "inside.py")
    (repo / "linked_dir").symlink_to(outside, target_is_directory=True)
    (repo / "linked_file.py").symlink_to(outside / "external.py")

    assert scan_python_files(repo).files == ["inside.py"]


def test_skips_directories_named_like_python_files(tmp_path):
    (tmp_path / "weird.py").mkdir()
    _write(tmp_path, "real.py")

    assert scan_python_files(tmp_path).files == ["real.py"]
