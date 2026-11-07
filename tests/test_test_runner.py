import os
import sys
import time

import pytest

from app.test_runner import ResourceLimits, run_process, run_tests


def test_passing_command():
    result = run_tests([sys.executable, "-c", "print('ok')"], timeout=10)

    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"
    assert result.timed_out is False


def test_failing_command():
    result = run_tests(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
        timeout=10,
    )

    assert result.exit_code == 3
    assert "boom" in result.stderr
    assert result.timed_out is False


def test_timeout():
    result = run_tests([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)

    assert result.timed_out is True
    assert result.exit_code is None
    assert result.duration_seconds < 5


# --- run_process: controlled execution for patch verification ------------------------


def test_run_process_uses_only_the_given_environment_and_directory(tmp_path):
    os.environ["ATLAS_TEST_SECRET"] = "should-not-leak"
    try:
        result = run_process(
            [sys.executable, "-c", "import os; print(os.getcwd()); print(os.environ.get('ATLAS_TEST_SECRET'))"],
            timeout=10, cwd=tmp_path, env={"PATH": os.environ["PATH"]},
        )
    finally:
        del os.environ["ATLAS_TEST_SECRET"]
    cwd, secret = result.stdout.split()
    assert os.path.realpath(cwd) == os.path.realpath(tmp_path) and secret == "None"


def test_run_process_caps_output_and_keeps_the_tail(tmp_path):
    script = "import sys; sys.stdout.write('x' * 500_000 + 'END')"
    result = run_process([sys.executable, "-c", script], timeout=10, cwd=tmp_path, env={}, max_output_bytes=1_000)
    assert result.stdout_truncated and result.stdout.endswith("END") and len(result.stdout) < 1_100


def test_run_process_timeout_kills_the_whole_process_group(tmp_path):
    marker = tmp_path / "child-survived"
    child = tmp_path / "child.py"
    child.write_text(f"import time\ntime.sleep(2)\nopen({str(marker)!r}, 'w').close()\n")
    parent = tmp_path / "parent.py"
    parent.write_text(f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, {str(child)!r}])\ntime.sleep(30)\n")
    started = time.monotonic()
    result = run_process([sys.executable, str(parent)], timeout=0.5, cwd=tmp_path, env={})
    assert result.timed_out and result.exit_code is None and time.monotonic() - started < 5
    time.sleep(2.5)
    assert not marker.exists()


def test_run_process_applies_resource_limits(tmp_path):
    script = f"open({str(tmp_path / 'big')!r}, 'wb').write(b'x' * 5_000_000)"
    limited = run_process([sys.executable, "-c", script], timeout=10, cwd=tmp_path, env={},
                          limits=ResourceLimits(file_bytes=1_000_000))
    assert limited.exit_code != 0
    assert (tmp_path / "big").stat().st_size <= 1_000_000


def test_run_process_rejects_shell_strings(tmp_path):
    with pytest.raises(ValueError):
        run_process("echo hi", timeout=1, cwd=tmp_path, env={})  # type: ignore[arg-type]
