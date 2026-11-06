import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
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
from app.knowledge_graph.models import REPOSITORY_ID_PATTERN, CodeGraph
from app.knowledge_graph.neo4j_client import (
    Neo4jConfig,
    create_driver,
    verify_connectivity,
)
from app.knowledge_graph.neo4j_repository import Neo4jGraphRepository
from app.knowledge_graph.repository import (
    GraphNotFoundError,
    GraphRepository,
    GraphStats,
    GraphStoreError,
    InvalidGraphError,
)
from app.knowledge_graph.service import (
    GraphBuildResult,
    GraphService,
    RepositoryIdConflictError,
)
from app.repository import BenchmarkExecutionRepository
from app.schemas import (
    AnalyzeRepositoryRequest,
    BenchmarkExecutionRecord,
    BuildGraphRequest,
    RunBenchmarkRequest,
)

logger = logging.getLogger(__name__)

# Server-controlled: clients cannot choose their own timeout.
BENCHMARK_TIMEOUT_SECONDS = 10



@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create one pooled Neo4j driver for the process and close it on shutdown."""
    app.state.graph_repository = None
    config = Neo4jConfig.from_settings(get_settings())
    driver = create_driver(config) if config else None
    if driver is not None and config is not None:
        repository = Neo4jGraphRepository(driver, config.database)
        app.state.graph_repository = repository
        try:
            verify_connectivity(driver)
            repository.ensure_schema()
        except GraphStoreError as exc:
            # Keep serving: graph calls retry the connection and return 503 meanwhile.
            logger.warning("Neo4j not ready at startup: %s", exc)
    else:
        logger.info("Neo4j is not configured; graph endpoints will return 503")
    try:
        yield
    finally:
        if driver is not None:
            driver.close()


app = FastAPI(title="Atlas AI", lifespan=lifespan)


@app.exception_handler(GraphStoreError)
def graph_store_unavailable(request: Request, exc: GraphStoreError) -> JSONResponse:
    # Generic on purpose: never echo connection details or credentials to clients.
    logger.error("Graph store error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "Graph database unavailable"})


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
    target = authorize_repository_path(request.repository_path, allowed_roots)
    try:
        return analyze_repository(target)
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryNotADirectoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def authorize_repository_path(repository_path: str, allowed_roots: list[Path]) -> Path:
    """Resolve the path and enforce the allowlist, failing closed. Raises HTTP 403.

    Runs before any existence check, so a rejected caller cannot probe which paths exist.
    """
    if not allowed_roots:
        raise HTTPException(
            status_code=403,
            detail="Repository analysis is disabled: ALLOWED_REPOSITORY_ROOTS is not configured",
        )
    target = Path(repository_path).expanduser().resolve()
    if not is_within_allowed_roots(target, allowed_roots):
        raise HTTPException(status_code=403, detail="Repository path is not in an allowed root")
    return target


# --- code knowledge graph ---------------------------------------------------

RepositoryIdParam = Annotated[str, PathParam(pattern=REPOSITORY_ID_PATTERN)]


def get_graph_repository(request: Request) -> GraphRepository:
    """Dependency: the process-wide graph repository created at startup."""
    repository = getattr(request.app.state, "graph_repository", None)
    if repository is None:
        raise HTTPException(status_code=503, detail="Graph database is not configured")
    return repository


def get_graph_service(
    repository: GraphRepository = Depends(get_graph_repository),
) -> GraphService:
    return GraphService(repository)


@app.post("/repositories/graph/build")
def build_repository_graph(
    request: BuildGraphRequest,
    allowed_roots: list[Path] = Depends(get_allowed_repository_roots),
    service: GraphService = Depends(get_graph_service),
) -> GraphBuildResult:
    target = authorize_repository_path(request.repository_path, allowed_roots)
    try:
        return service.build_graph(target, request.repository_id)
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryNotADirectoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RepositoryIdConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidGraphError as exc:
        # A builder bug, not a client error. Report it structurally; nothing was stored.
        raise HTTPException(
            status_code=500,
            detail={"message": str(exc), "errors": exc.result.model_dump()["errors"]},
        ) from exc


@app.get("/repositories/{repository_id}/graph")
def get_repository_graph(
    repository_id: RepositoryIdParam, service: GraphService = Depends(get_graph_service)
) -> CodeGraph:
    try:
        return service.get_graph(repository_id)
    except GraphNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No graph for {repository_id}") from exc


@app.get("/repositories/{repository_id}/graph/stats")
def get_repository_graph_stats(
    repository_id: RepositoryIdParam, service: GraphService = Depends(get_graph_service)
) -> GraphStats:
    try:
        return service.get_stats(repository_id)
    except GraphNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No graph for {repository_id}") from exc


@app.delete("/repositories/{repository_id}/graph", status_code=204)
def delete_repository_graph(
    repository_id: RepositoryIdParam, service: GraphService = Depends(get_graph_service)
) -> Response:
    if not service.delete_graph(repository_id):
        raise HTTPException(status_code=404, detail=f"No graph for {repository_id}")
    return Response(status_code=204)
