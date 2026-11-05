from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy.orm import Session

from app.benchmark import BenchmarkExecutionResult, run_benchmark
from app.benchmark_registry import get_benchmark
from app.database import get_db
from app.repository import BenchmarkExecutionRepository
from app.schemas import BenchmarkExecutionRecord, RunBenchmarkRequest

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
