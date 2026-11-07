import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
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


# --- controlled execution (Task 13) ---------------------------------------------------
#
# ``run_tests`` above stays as it is for the benchmark runner and the agent's
# run_tests tool. ``run_process`` adds what patch verification needs:
#
# - an explicit working directory and a caller-built environment (never inherited);
# - its own process group, killed as a whole on timeout and after exit, so test
#   code cannot leave background processes behind;
# - POSIX resource limits (CPU seconds, memory, file size, open files) set by a
#   small launcher before ``exec``; ``preexec_fn`` is unsafe in threaded servers;
# - output written to temporary files and capped when read back, so a test that
#   prints gigabytes cannot exhaust memory.
#
# This is still not a sandbox: there is no filesystem or network isolation.
# Untrusted repositories need container workers.

_LAUNCHER = """
import json, os, resource, sys
limits = json.loads(sys.argv[1])
for name, value in limits.items():
    res = getattr(resource, name, None)
    if res is None or value is None:
        continue
    try:
        soft, hard = resource.getrlimit(res)
        value = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(res, (value, value))
    except (ValueError, OSError):
        pass  # not supported on this platform (RLIMIT_AS on macOS)
os.execvp(sys.argv[2], sys.argv[2:])
"""


@dataclass(frozen=True)
class ResourceLimits:
    cpu_seconds: int | None = None
    memory_bytes: int | None = None
    file_bytes: int | None = None
    open_files: int | None = 256

    def as_rlimits(self) -> dict[str, int | None]:
        return {
            "RLIMIT_CPU": self.cpu_seconds,
            "RLIMIT_AS": self.memory_bytes,
            "RLIMIT_FSIZE": self.file_bytes,
            "RLIMIT_NOFILE": self.open_files,
        }


class ProcessResult(BaseModel):
    command: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    duration_seconds: float
    timed_out: bool


def _read_capped(handle, max_bytes: int) -> tuple[str, bool]:
    """The last ``max_bytes`` of a temporary output file. Test failures are at the end."""
    handle.flush()
    size = handle.seek(0, os.SEEK_END)
    truncated = size > max_bytes
    handle.seek(max(0, size - max_bytes))
    text = handle.read().decode("utf-8", errors="replace")
    return (("...[truncated]...\n" + text) if truncated else text), truncated


def _kill_group(process: subprocess.Popen) -> None:
    if os.name != "posix":
        process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_process(
    command: list[str],
    *,
    timeout: float,
    cwd: str | Path,
    env: dict[str, str],
    max_output_bytes: int = 100_000,
    limits: ResourceLimits | None = None,
) -> ProcessResult:
    """Run ``command`` (an argument list, never a shell string) under the controls above."""
    if not isinstance(command, list | tuple) or not command or not all(isinstance(p, str) for p in command):
        raise ValueError("command must be a non-empty list of strings")
    argv = list(command)
    if limits is not None and os.name == "posix":
        argv = [sys.executable, "-I", "-c", _LAUNCHER, json.dumps(limits.as_rlimits()), *argv]
    start = time.monotonic()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,  # own process group: killpg reaches every child
        )
        timed_out = False
        try:
            exit_code: int | None = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(process)
            process.wait()
            exit_code = None
        finally:
            _kill_group(process)  # stray background children
        duration = time.monotonic() - start
        stdout, stdout_cut = _read_capped(out, max_output_bytes)
        stderr, stderr_cut = _read_capped(err, max_output_bytes // 2)
    return ProcessResult(
        command=list(command),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_cut,
        stderr_truncated=stderr_cut,
        duration_seconds=duration,
        timed_out=timed_out,
    )
