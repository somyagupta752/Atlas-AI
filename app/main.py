from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy.orm import Session

from app.benchmark import BenchmarkExecutionResult, run_benchmark
from app.benchmark_registry import get_benchmark
from app.code_intelligence import (
    RepositoryNotADirectoryError,
    RepositoryNotFoundError,
    analyze_repository,
)
from app.code_intelligence.schemas import RepositoryAnalysis
from app.config import get_settings
from app.database import get_db
from app.repository import BenchmarkExecutionRepository
from app.schemas import (
    AnalyzeRepositoryRequest,
    BenchmarkExecutionRecord,
    RunBenchmarkRequest,
)

# Server-controlled: clients cannot choose their own timeout.
BENCHMARK_TIMEOUT_SECONDS = 10

app = FastAPI(title="Atlas AI")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.post("/benchmarks/run")
def run_benchmark_endpoint(
    request: RunBenchmarkRequest, db: Session = Depends(get_db)
) -> BenchmarkExecutionResult:
    case = get_benchmark(request.case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"Unknown benchmark: {request.case_id}")
    result = run_benchmark(case, timeout=BENCHMARK_TIMEOUT_SECONDS)
    BenchmarkExecutionRepository(db).save(result)
    return result


@app.get("/benchmarks/{case_id}/runs")
def list_benchmark_runs(
    case_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[BenchmarkExecutionRecord]:
    if get_benchmark(case_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown benchmark: {case_id}")
    return BenchmarkExecutionRepository(db).list_by_case(case_id, limit=limit)


def get_allowed_repository_roots() -> list[Path]:
    """Dependency: directories the analyzer may read. Empty means analysis is disabled."""
    return get_settings().allowed_repository_roots


def is_within_allowed_roots(target: Path, allowed_roots: list[Path]) -> bool:
    """Whether ``target`` is inside one of ``allowed_roots``. Fails closed on an empty list.

    Both sides are resolved first, so symlinks and ".." cannot escape a root, and a
    symlinked allowed root is compared by its real location.
    """
    resolved = target.expanduser().resolve()
    return any(resolved.is_relative_to(root.expanduser().resolve()) for root in allowed_roots)


@app.post("/repositories/analyze")
def analyze_repository_endpoint(
    request: AnalyzeRepositoryRequest,
    allowed_roots: list[Path] = Depends(get_allowed_repository_roots),
) -> RepositoryAnalysis:
    # Checked before existence, so a rejected caller cannot probe which paths exist.
    if not allowed_roots:
        raise HTTPException(
            status_code=403,
            detail="Repository analysis is disabled: ALLOWED_REPOSITORY_ROOTS is not configured",
        )
    target = Path(request.repository_path).expanduser().resolve()
    if not is_within_allowed_roots(target, allowed_roots):
        raise HTTPException(status_code=403, detail="Repository path is not in an allowed root")
    try:
        return analyze_repository(target)
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryNotADirectoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
