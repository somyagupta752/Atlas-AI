import subprocess
import time
from pathlib import Path

from pydantic import BaseModel


class TestRunResult(BaseModel):
    __test__ = False  # not a pytest test class, despite the name

    command: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool


def _to_text(output: str | bytes | None) -> str:
    # TimeoutExpired can carry bytes (or None) even when text=True was used.
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output


def run_tests(
    command: list[str],
    timeout: float,
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> TestRunResult:
    """Run ``command`` (an argument list, never a shell string) with a timeout.

    ``env`` replaces the environment entirely when given, so callers can keep
    secrets such as API keys out of the child process.
    """
    start = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            # Nonzero exit codes are data we classify, not errors to raise.
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return TestRunResult(
            command=command,
            exit_code=None,
            stdout=_to_text(exc.stdout),
            stderr=_to_text(exc.stderr),
            duration_seconds=time.monotonic() - start,
            timed_out=True,
        )
    return TestRunResult(
        command=command,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        duration_seconds=time.monotonic() - start,
        timed_out=False,
    )
