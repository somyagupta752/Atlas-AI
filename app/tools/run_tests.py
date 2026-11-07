"""Run one approved test file or test through pytest, without a shell.

The model chooses a test path (and optionally a test name). It never chooses
the command. The command is always::

    <this python> -m pytest -q -p no:cacheprovider <resolved test path>[::<name>]

with the repository root as working directory, a minimal environment (no API
keys, no database URL), bytecode writing disabled and a hard timeout.

This is a process runner, not a sandbox: test code runs with this process's
user permissions. It is refused unless the repository is under
``AGENT_TRUSTED_TEST_ROOTS``. Untrusted repositories need isolated workers.
"""

import os
import re
import sys
from pathlib import PurePosixPath

from pydantic import Field

from app.agents.schemas import SourceLocation
from app.test_runner import run_tests
from app.tools.base import (
    BaseTool,
    ToolArguments,
    ToolContext,
    ToolInputError,
    ToolOutput,
    ToolPermissionError,
    redact_secrets,
    resolve_in_repository,
    truncate_text,
)

TEST_FILE_RE = re.compile(r"^(test_.*|.*_test)\.py$")
TEST_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(::[A-Za-z_][A-Za-z0-9_]*)?$")
# The final pytest line, with or without "=====" padding: "1 failed, 3 passed in 0.01s".
SUMMARY_RE = re.compile(
    r"^=*\s*((?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|warnings?|deselected),? ?)+|no tests ran)",
    re.MULTILINE,
)
# Only what pytest needs. Everything else, including credentials, stays out.
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")


class RunTestsArguments(ToolArguments):
    test_path: str = Field(
        min_length=1, max_length=500, description="Test file relative to the repository root, e.g. 'tests/test_user.py'."
    )
    test_name: str | None = Field(
        default=None,
        max_length=200,
        description="Optional single test: 'test_name' or 'TestClass::test_name'.",
    )


def child_environment() -> dict[str, str]:
    env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    return env


class RunTestsTool(BaseTool):
    name = "run_tests"
    description = (
        "Runs one existing pytest test file (or one test in it) from the repository and returns "
        "the exit code, a pass/fail summary, stdout, stderr, the duration and whether it timed "
        "out. Accepts a test file path, not a command: only files named test_*.py or *_test.py "
        "are allowed. Use it to check whether a test reproduces or contradicts a hypothesis. "
        "It never modifies files."
    )
    Arguments = RunTestsArguments

    def execute(self, arguments: RunTestsArguments, context: ToolContext) -> ToolOutput:
        if not context.test_execution_allowed:
            raise ToolPermissionError("Test execution is not approved for this repository")
        path, relative = resolve_in_repository(context.repository_root, arguments.test_path)
        if not TEST_FILE_RE.match(PurePosixPath(relative).name):
            raise ToolPermissionError("Only pytest files named test_*.py or *_test.py can be run")
        target = str(path)
        if arguments.test_name:
            if not TEST_NAME_RE.match(arguments.test_name):
                raise ToolInputError("test_name must look like 'test_x' or 'TestClass::test_x'")
            target = f"{target}::{arguments.test_name}"

        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header", target]
        result = run_tests(
            command,
            timeout=context.tool_timeout_seconds,
            cwd=context.repository_root,
            env=child_environment(),
        )
        root = str(context.repository_root)
        budget = context.max_output_bytes // 3
        stdout, out_cut = truncate_text(redact_secrets(result.stdout.replace(root, ".")), budget, keep="tail")
        stderr, err_cut = truncate_text(redact_secrets(result.stderr.replace(root, ".")), budget // 2, keep="tail")
        summary_match = [match.strip(" ,") for match in SUMMARY_RE.findall(result.stdout)]
        if result.timed_out:
            outcome = "timed_out"
        elif result.exit_code == 0:
            outcome = "passed"
        elif result.exit_code == 1:
            outcome = "failed"
        else:
            outcome = "error"  # collection error, usage error, no tests collected
        display = relative + (f"::{arguments.test_name}" if arguments.test_name else "")
        return ToolOutput(
            data={
                "test_path": display,
                "command": f"python -m pytest -q {display}",
                "outcome": outcome,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_seconds": round(result.duration_seconds, 3),
                "summary": summary_match[-1] if summary_match else None,
                "stdout": stdout,
                "stderr": stderr,
                "output_truncated": out_cut or err_cut,
            },
            locations=[SourceLocation(file_path=relative), *traceback_locations(stdout + "\n" + stderr, context)],
        )


# "test_x.py:17: ZeroDivisionError" (pytest) and 'File "./pkg/x.py", line 5' (Python tracebacks).
_PYTEST_LOCATION = re.compile(r"^(?:\./)?([\w./-]+\.py):(\d+):", re.MULTILINE)
_PYTHON_LOCATION = re.compile(r'File "(?:\./)?([\w./-]+\.py)", line (\d+)')


def traceback_locations(output: str, context: ToolContext) -> list[SourceLocation]:
    """File lines that the returned test output names, for repository files only.

    The model sees these lines in the traceback, so evidence may cite them.
    """
    found: dict[tuple[str, int], SourceLocation] = {}
    for pattern in (_PYTEST_LOCATION, _PYTHON_LOCATION):
        for path, line in pattern.findall(output):
            try:
                _, relative = resolve_in_repository(context.repository_root, path)
            except (ToolInputError, ToolPermissionError):
                continue  # outside the repository, a library or a secret file
            number = int(line)
            found.setdefault((relative, number), SourceLocation(file_path=relative, start_line=number, end_line=number))
    return list(found.values())[:50]
