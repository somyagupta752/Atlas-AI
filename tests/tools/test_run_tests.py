import os

import pytest

from app.tools.base import ToolInputError, ToolPermissionError
from app.tools.run_tests import RunTestsTool, child_environment

TOOL = RunTestsTool()


def run(context, **arguments):
    return TOOL.execute(TOOL.validate(arguments), context)


@pytest.fixture
def context(bench_repo, make_context):
    return make_context(bench_repo("bug_001_average"), test_execution_allowed=True)


def test_failing_test_reproduces_the_bug(context):
    output = run(context, test_path="test_solution.py")

    assert output.data["outcome"] == "failed"
    assert output.data["exit_code"] == 1
    assert output.data["timed_out"] is False
    assert "ZeroDivisionError" in output.data["stdout"]
    assert "1 failed, 3 passed" in output.data["summary"]
    assert output.locations[0].file_path == "test_solution.py"


def test_passing_single_test(context):
    output = run(context, test_path="test_solution.py", test_name="test_average_of_single_number")
    assert output.data["outcome"] == "passed"
    assert output.data["exit_code"] == 0


def test_timeout(write_repo, make_context):
    root = write_repo({"test_slow.py": "import time\n\ndef test_slow():\n    time.sleep(30)\n"}).resolve()
    output = run(make_context(root, test_execution_allowed=True, tool_timeout_seconds=1), test_path="test_slow.py")

    assert output.data["timed_out"] is True
    assert output.data["outcome"] == "timed_out"
    assert output.data["duration_seconds"] < 10


def test_does_not_write_into_the_repository(context):
    before = sorted(p.relative_to(context.repository_root) for p in context.repository_root.rglob("*"))
    run(context, test_path="test_solution.py")
    after = sorted(p.relative_to(context.repository_root) for p in context.repository_root.rglob("*"))
    assert before == after


@pytest.mark.parametrize("path", ["../x/test_a.py", "/tmp/test_a.py"])
def test_invalid_path(context, path):
    with pytest.raises(ToolPermissionError):
        run(context, test_path=path)


def test_non_test_file_is_refused(context):
    with pytest.raises(ToolPermissionError, match="test_"):
        run(context, test_path="solution.py")


def test_missing_test_file(context):
    with pytest.raises(ToolInputError):
        run(context, test_path="test_missing.py")


@pytest.mark.parametrize("name", ["test_x; rm -rf /", "test_x or True", "-k", "test_x\ntest_y"])
def test_test_name_cannot_inject_arguments(context, name):
    with pytest.raises(ToolInputError):
        run(context, test_path="test_solution.py", test_name=name)


def test_refused_unless_repository_is_trusted(bench_repo, make_context):
    context = make_context(bench_repo("bug_001_average"), test_execution_allowed=False)
    with pytest.raises(ToolPermissionError, match="not approved"):
        run(context, test_path="test_solution.py")


def test_no_command_argument_exists():
    assert "command" not in TOOL.arguments_schema["properties"]
    assert TOOL.arguments_schema["additionalProperties"] is False


def test_child_environment_has_no_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    env = child_environment()
    assert "ANTHROPIC_API_KEY" not in env and "DATABASE_URL" not in env
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env.get("PATH") == os.environ.get("PATH")


def test_traceback_lines_become_citable_locations(context):
    output = run(context, test_path="test_solution.py")
    lines = {(loc.file_path, loc.start_line) for loc in output.locations if loc.start_line}
    assert ("solution.py", 2) in lines  # where ZeroDivisionError is raised
    assert any(path == "test_solution.py" for path, _ in lines)


def test_traceback_locations_ignore_files_outside_the_repository(context):
    from app.tools.run_tests import traceback_locations

    text = 'File "/usr/lib/python3/x.py", line 3\n../escape.py:4: Error\nsolution.py:2: ZeroDivisionError'
    assert [(loc.file_path, loc.start_line) for loc in traceback_locations(text, context)] == [("solution.py", 2)]
