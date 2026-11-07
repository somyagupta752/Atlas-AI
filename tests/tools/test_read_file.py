import os

import pytest

from app.tools.base import ToolInputError, ToolPermissionError
from app.tools.read_file import ReadFileTool

TOOL = ReadFileTool()


def run(context, **arguments):
    return TOOL.execute(TOOL.validate(arguments), context)


@pytest.fixture
def repo(write_repo):
    return write_repo(
        {
            "app/service.py": "".join(f"line {n}\n" for n in range(1, 21)),
            "config.py": 'API_KEY = "sk-ant-abcdefghijklmnop"\npassword = "hunter22secret"\n',
            ".env": "SECRET=1\n",
            "empty.py": "",
        }
    ).resolve()


def test_reads_file_with_line_numbers(repo, make_context):
    output = run(make_context(repo), file_path="app/service.py")

    assert output.data["total_lines"] == 20
    assert output.data["content"].splitlines()[0] == "    1 | line 1"
    assert output.data["truncated"] is False
    assert output.locations[0].start_line == 1 and output.locations[0].end_line == 20


def test_reads_line_range(repo, make_context):
    output = run(make_context(repo), file_path="app/service.py", start_line=5, end_line=7)

    assert output.data["content"].splitlines() == ["    5 | line 5", "    6 | line 6", "    7 | line 7"]
    assert (output.data["start_line"], output.data["end_line"]) == (5, 7)


def test_start_beyond_end_of_file(repo, make_context):
    with pytest.raises(ToolInputError, match="beyond the end"):
        run(make_context(repo), file_path="app/service.py", start_line=50)


def test_output_is_limited(write_repo, make_context):
    root = write_repo({"big.py": "".join(f"x = {'y' * 200}\n" for _ in range(2000))}).resolve()
    output = run(make_context(root, max_output_bytes=5000), file_path="big.py")

    assert output.data["truncated"] is True
    assert len(output.data["content"].encode()) <= 5000


def test_missing_file(repo, make_context):
    with pytest.raises(ToolInputError, match="No such file"):
        run(make_context(repo), file_path="nope.py")


@pytest.mark.parametrize("path", ["../outside.py", "app/../../outside.py", "/etc/passwd", "C:/windows/x.py"])
def test_path_traversal_and_absolute_paths_are_rejected(repo, make_context, path):
    with pytest.raises(ToolPermissionError):
        run(make_context(repo), file_path=path)


def test_symlink_escape_is_rejected(repo, make_context, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("private\n")
    os.symlink(secret, repo / "link.py")

    with pytest.raises(ToolPermissionError, match="outside the repository"):
        run(make_context(repo), file_path="link.py")


def test_symlinked_directory_escape_is_rejected(repo, make_context, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "data.py").write_text("x = 1\n")
    os.symlink(outside, repo / "linked")

    with pytest.raises(ToolPermissionError):
        run(make_context(repo), file_path="linked/data.py")


def test_secret_files_are_refused(repo, make_context):
    with pytest.raises(ToolPermissionError, match="secrets"):
        run(make_context(repo), file_path=".env")


def test_secret_values_are_redacted(repo, make_context):
    content = run(make_context(repo), file_path="config.py").data["content"]

    assert "sk-ant-abcdefghijklmnop" not in content
    assert "hunter22secret" not in content
    assert "[REDACTED]" in content


def test_empty_file(repo, make_context):
    output = run(make_context(repo), file_path="empty.py")
    assert output.data["total_lines"] == 0


def test_directory_is_not_a_file(repo, make_context):
    with pytest.raises(ToolInputError, match="directory"):
        run(make_context(repo), file_path="app")
