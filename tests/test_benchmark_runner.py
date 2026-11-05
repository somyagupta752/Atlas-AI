import sys

from app.test_runner import run_tests


def test_runner_detects_bug_001_failure():
    result = run_tests(
        [
            sys.executable,
            "-m",
            "pytest",
            "-v",
            "benchmarks/bug_001_average/",
        ],
        timeout=60,
    )

    assert result.timed_out is False
    assert result.exit_code is not None
    assert result.exit_code != 0
    assert "ZeroDivisionError" in result.stdout + result.stderr
    assert result.duration_seconds >= 0
