import sys

from app.test_runner import run_tests


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
