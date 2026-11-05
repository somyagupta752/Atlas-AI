from typing import Literal

from pydantic import BaseModel

from app.test_runner import TestRunResult, run_tests


class BenchmarkCase(BaseModel):
    """Describes what to run and which failure it should reproduce."""

    case_id: str
    description: str
    command: list[str]
    expected_failure_text: str


BenchmarkOutcome = Literal["reproduced", "not_reproduced", "timed_out"]


class BenchmarkExecutionResult(BaseModel):
    case_id: str
    outcome: BenchmarkOutcome
    test_run: TestRunResult


def run_benchmark(case: BenchmarkCase, timeout: float) -> BenchmarkExecutionResult:
    test_run = run_tests(case.command, timeout=timeout)

    # A timeout is inconclusive: the command never finished, so we cannot
    # know whether the expected failure would have occurred.
    if test_run.timed_out:
        outcome: BenchmarkOutcome = "timed_out"
    else:
        output = test_run.stdout + test_run.stderr
        failed = test_run.exit_code is not None and test_run.exit_code != 0
        if failed and case.expected_failure_text in output:
            outcome = "reproduced"
        else:
            outcome = "not_reproduced"

    return BenchmarkExecutionResult(
        case_id=case.case_id, outcome=outcome, test_run=test_run
    )
