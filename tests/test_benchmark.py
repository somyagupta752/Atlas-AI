import sys

from app.benchmark import BenchmarkCase, run_benchmark


def _case(code: str, expected: str = "ZeroDivisionError") -> BenchmarkCase:
    return BenchmarkCase(
        case_id="test-case",
        description="synthetic case",
        command=[sys.executable, "-c", code],
        expected_failure_text=expected,
    )


def test_reproduced_when_expected_error_and_nonzero_exit():
    result = run_benchmark(_case("1 / 0"), timeout=10)

    assert result.outcome == "reproduced"
    assert result.case_id == "test-case"
    assert result.test_run.exit_code != 0


def test_not_reproduced_when_command_succeeds():
    result = run_benchmark(_case("print('fine')"), timeout=10)

    assert result.outcome == "not_reproduced"
    assert result.test_run.exit_code == 0


def test_not_reproduced_when_expected_text_printed_but_exit_zero():
    result = run_benchmark(_case("print('ZeroDivisionError')"), timeout=10)

    assert result.outcome == "not_reproduced"


def test_not_reproduced_on_unrelated_error():
    result = run_benchmark(_case("raise ValueError('nope')"), timeout=10)

    assert result.outcome == "not_reproduced"
    assert result.test_run.exit_code != 0


def test_timed_out():
    result = run_benchmark(_case("import time; time.sleep(5)"), timeout=0.5)

    assert result.outcome == "timed_out"
    assert result.test_run.timed_out is True


def test_real_bug_001_average_is_reproduced():
    case = BenchmarkCase(
        case_id="bug-001-average",
        description="Average function fails on empty input",
        command=[sys.executable, "-m", "pytest", "-v", "benchmarks/bug_001_average/"],
        expected_failure_text="ZeroDivisionError",
    )

    result = run_benchmark(case, timeout=60)

    assert result.outcome == "reproduced"
