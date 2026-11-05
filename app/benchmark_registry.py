import sys
from pathlib import Path

from app.benchmark import BenchmarkCase

# Absolute path, so the benchmark works regardless of the server's working directory.
_BENCHMARKS_DIR = Path(__file__).resolve().parent.parent / "benchmarks"

# Allowlist of benchmarks the API may execute. Clients choose an ID, never a command.
BENCHMARKS: dict[str, BenchmarkCase] = {
    "bug-001-average": BenchmarkCase(
        case_id="bug-001-average",
        description="Average function fails on empty input",
        command=[
            sys.executable,
            "-m",
            "pytest",
            "-v",
            str(_BENCHMARKS_DIR / "bug_001_average"),
        ],
        expected_failure_text="ZeroDivisionError",
    ),
}


def get_benchmark(case_id: str) -> BenchmarkCase | None:
    return BENCHMARKS.get(case_id)
