"""Adversarial validation cases. A generated diff is untrusted input."""

import os

import pytest

from app.patching.models import PatchLimits
from app.patching.validator import (
    PatchScope,
    ValidatedPatch,
    derive_allowed_files,
    is_test_path,
    path_errors,
    validate_patch,
)
from tests.patching.conftest import (
    AVERAGE_FIX,
    AVERAGE_NOOP,
    AVERAGE_TEST_TAMPER,
    AVERAGE_UNRELATED,
)


def diff_for(path: str, old: str = "x = 1", new: str = "x = 2") -> str:
    return f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{old}\n+{new}\n"


@pytest.fixture
def repo(write_repo):
    return write_repo(
        {
            "solution.py": "def calculate_average(numbers):\n    return sum(numbers) / len(numbers)\n",
            "test_solution.py": "from solution import calculate_average\n",
            "pkg/a.py": "x = 1\n",
            "pkg/b.py": "x = 1\n",
            "README.md": "hello\n",
            ".env": "API_KEY=x\n",
            "docker-compose.yml": "services: {}\n",
        }
    )


def scope(repo, *allowed):
    return PatchScope(root=repo, allowed_files=frozenset(allowed or ("solution.py",)))


def check(diff, repo, *allowed, limits=None, **kwargs):
    result, validated = validate_patch(diff, scope(repo, *allowed), limits or PatchLimits(), **kwargs)
    assert result.valid == (validated is not None)
    return result


def test_normal_source_modification_is_accepted(repo):
    result, validated = validate_patch(AVERAGE_FIX, scope(repo), PatchLimits())
    assert result.valid and not result.errors
    assert result.affected_files == ["solution.py"] and (result.added_lines, result.removed_lines) == (2, 0)
    assert isinstance(validated, ValidatedPatch) and validated.files == ("solution.py",)
    assert result.diff_sha256 == validated.sha256 and len(validated.sha256) == 64


@pytest.mark.parametrize(
    ("path", "fragment"),
    [
        ("/etc/passwd", "absolute"),
        ("../../secret.txt", "traversal"),
        ("pkg/../../secret.py", "traversal"),
        (".env", "credential"),
        ("config/.env.production", "credential"),
        (".git/config", ".git"),
        (".GIT/hooks/pre-commit.py", ".git"),
        (".venv/lib/site.py", "virtualenvs"),
        ("node_modules/x.py", "virtualenvs"),
        ("keys/id_rsa", "credential"),
        ("README.md", "only Python"),
        ("docker-compose.yml", "only Python"),
        ("pkg/./a.py", "not normalised"),
        ("pkg//a.py", "not normalised"),
        ("pkg/a b.py", "unsupported characters"),
        ("~/x.py", "absolute"),
    ],
)
def test_unsafe_paths_are_rejected(path, fragment):
    errors = path_errors(path)
    assert errors and any(fragment.lower() in e.lower() for e in errors), errors


@pytest.mark.parametrize(
    ("path", "fragments"),
    [
        ("..\\..\\secret.py", ["Windows-style", "traversal"]),
        ("C:\\Users\\x.py", ["Windows-style", "absolute"]),
        ("C:/Users/x.py", ["absolute"]),
        ("\\\\server\\share\\x.py", ["Windows-style", "absolute"]),
        ("pkg\\a.py", ["Windows-style"]),
        ("pkg\\..\\..\\.env", ["Windows-style", "traversal"]),
    ],
)
def test_windows_style_paths_are_rejected(path, fragments):
    errors = " ".join(path_errors(path))
    for fragment in fragments:
        assert fragment.lower() in errors.lower(), errors


def test_paths_in_a_diff_are_rejected_end_to_end(repo):
    result = check(diff_for("../../secret.py"), repo)
    assert any("traversal" in e for e in result.errors)
    result = check(diff_for("/etc/hosts.py"), repo)
    assert any("absolute" in e for e in result.errors)
    result = check("--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-API_KEY=x\n+API_KEY=y\n", repo)
    assert any("credential" in e for e in result.errors)
    result = check("--- a/.git/config\n+++ b/.git/config\n@@ -1 +1 @@\n-a\n+b\n", repo)
    assert any(".git" in e for e in result.errors)


def test_symlink_escape_is_rejected(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.py").write_text("x = 1\n")
    os.symlink(outside, repo / "linked")
    os.symlink(outside / "victim.py", repo / "victim.py")
    for path in ("linked/victim.py", "victim.py"):
        result = check(diff_for(path), repo, path)
        assert any("symlink" in e for e in result.errors), result.errors
    assert (outside / "victim.py").read_text() == "x = 1\n"


def test_binary_rename_and_malformed_hunks_are_rejected(repo):
    binary = "diff --git a/solution.py b/solution.py\nGIT binary patch\nliteral 1\nx\n"
    assert any("binary" in e for e in check(binary, repo).errors)
    rename = "--- a/solution.py\n+++ b/other.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert any("Renames" in e for e in check(rename, repo).errors)
    malformed = "--- a/solution.py\n+++ b/solution.py\n@@ -1,2 @@\n a\n"
    assert any("malformed" in e for e in check(malformed, repo).errors)


def test_modification_outside_the_allowed_scope_is_rejected(repo):
    result = check(diff_for("pkg/a.py"), repo, "solution.py")
    assert any("outside the approved file scope" in e for e in result.errors)
    assert check(diff_for("pkg/a.py"), repo, "pkg/a.py").valid


def test_unrelated_config_and_docs_changes_are_rejected(repo):
    result = check(AVERAGE_UNRELATED, repo)
    assert not result.valid and any("README.md" in e for e in result.errors)
    assert result.affected_files == ["README.md", "solution.py"]


def test_test_modifications_are_rejected_unless_policy_allows(repo, write_repo):
    average = write_repo(
        {"solution.py": "", "test_solution.py": "\n" * 13 + "\n\ndef test_average_of_empty_list():\n"
         "    assert calculate_average([]) == 0.0\n"}, name="avg"
    )
    result = check(AVERAGE_TEST_TAMPER, average, "solution.py", "test_solution.py")
    assert any("modifies a test" in e for e in result.errors)
    allowed = check(AVERAGE_TEST_TAMPER, average, "test_solution.py", limits=PatchLimits(allow_test_changes=True))
    assert allowed.valid, allowed.errors


@pytest.mark.parametrize("path", ["test_x.py", "x_test.py", "conftest.py", "tests/helpers.py", "pkg/test/util.py"])
def test_test_paths(path):
    assert is_test_path(path)


def test_new_and_deleted_files_follow_the_policy(repo):
    new = "--- /dev/null\n+++ b/pkg/c.py\n@@ -0,0 +1 @@\n+x = 1\n"
    assert any("creates a new file" in e for e in check(new, repo, "pkg/a.py").errors)
    assert check(new, repo, "pkg/a.py", limits=PatchLimits(allow_new_files=True)).valid
    elsewhere = "--- /dev/null\n+++ b/other/c.py\n@@ -0,0 +1 @@\n+x = 1\n"
    assert not check(elsewhere, repo, "pkg/a.py", limits=PatchLimits(allow_new_files=True)).valid
    delete = "--- a/pkg/a.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n"
    assert any("deletes a file" in e for e in check(delete, repo, "pkg/a.py").errors)
    assert check(delete, repo, "pkg/a.py", limits=PatchLimits(allow_deleted_files=True)).valid


def test_limits_are_enforced(repo):
    two = diff_for("pkg/a.py") + diff_for("pkg/b.py")
    result = check(two, repo, "pkg/a.py", "pkg/b.py", limits=PatchLimits(max_files=1))
    assert any("limit is 1" in e for e in result.errors)
    big = "--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1 +1,30 @@\n-x = 1\n" + "".join(f"+x = {i}\n" for i in range(30))
    result = check(big, repo, "pkg/a.py", limits=PatchLimits(max_changed_lines=10))
    assert any("changes 31 lines" in e for e in result.errors)
    huge = diff_for("pkg/a.py", new="x = '" + "a" * 3000 + "'")
    result = check(huge, repo, "pkg/a.py", limits=PatchLimits(max_diff_bytes=1000))
    assert any("larger than 1000 bytes" in e for e in result.errors)


def test_duplicate_files_and_noop_patches_are_rejected(repo):
    twice = diff_for("pkg/a.py") + diff_for("pkg/a.py")
    assert any("more than once" in e for e in check(twice, repo, "pkg/a.py").errors)
    assert any("does not change the file" in e for e in check(AVERAGE_NOOP, repo).errors)


def test_missing_and_oversized_files(repo):
    assert any("does not exist" in e for e in check(diff_for("pkg/missing.py"), repo, "pkg/missing.py").errors)
    (repo / "pkg" / "big.py").write_text("x = 1\n" + "#" * 5000)
    result = check(diff_for("pkg/big.py"), repo, "pkg/big.py", limits=PatchLimits(max_file_bytes=1000))
    assert any("larger than 1000 bytes" in e for e in result.errors)
    (repo / "pkg" / "bin.py").write_bytes(b"x = 1\n\x00\x01")
    assert any("binary" in e for e in check(diff_for("pkg/bin.py"), repo, "pkg/bin.py").errors)


def test_all_errors_are_returned_together(repo):
    diff = diff_for("../a.py") + diff_for(".env") + diff_for("pkg/b.py")
    result = check(diff, repo, "solution.py")
    assert len(result.errors) >= 3


def test_claimed_files_mismatch_is_a_warning(repo):
    result = check(AVERAGE_FIX, repo, claimed_files=["solution.py", "other.py"])
    assert result.valid and any("does not match the diff" in w for w in result.warnings)


def test_validated_patch_cannot_be_forged(repo):
    result, _ = validate_patch(AVERAGE_FIX, scope(repo), PatchLimits())
    with pytest.raises(TypeError):
        ValidatedPatch(object(), None, result)


def test_derive_allowed_files(repo):
    allowed, excluded = derive_allowed_files(
        repo, ["solution.py", "./solution.py", "test_solution.py", ".env", "README.md", "missing.py", "../x.py"],
        PatchLimits(),
    )
    assert allowed == ["solution.py"]
    assert len(excluded) == 5
