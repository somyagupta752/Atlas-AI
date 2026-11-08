import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.agents.llm import LLMConfigurationError, create_llm_provider
from app.agents.routes import limit_debug_request_size
from app.agents.routes import router as debugging_router
from app.agents.tool_registry import create_default_registry
from app.auth.access import (
    authorize_repository,
    current_user,
    membership_role,
    repository_access,
)
from app.auth.models import ROLE_RANK, Role, User
from app.auth.routes import router as auth_router
from app.benchmark import BenchmarkExecutionResult, run_benchmark
from app.benchmark_registry import get_benchmark
from app.code_intelligence import (
    RepositoryNotADirectoryError,
    RepositoryNotFoundError,
    analyze_repository,
)
from app.code_intelligence.schemas import RepositoryAnalysis
from app.config import get_settings
from app.core.logging import configure_logging, register_secret_values
from app.core.middleware import RequestContextMiddleware
from app.database import get_db
from app.dependencies import (
    get_allowed_repository_roots,
    get_embedding_provider_optional,
    get_graph_context_source,
)
from app.evaluation.baselines import router as baselines_router
from app.evaluation.routes import install_error_handlers
from app.evaluation.routes import router as evaluation_router
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
from app.patching.routes import router as patching_router
from app.repository import BenchmarkExecutionRepository
from app.retrieval.embeddings import (
    EmbeddingConfigurationError,
    EmbeddingError,
    EmbeddingProvider,
    create_embedding_provider,
)
from app.retrieval.graph_search import GraphContextSource, GraphSearcher
from app.retrieval.indexing import (
    IndexingError,
    IndexResult,
    RepositoryIndexConflictError,
    RepositoryIndexer,
)
from app.retrieval.keyword_search import KeywordSearcher
from app.retrieval.models import RetrievalResponse
from app.retrieval.repository import ChunkRepository, IndexState
from app.retrieval.service import (
    InvalidRetrievalRequestError,
    RepositoryNotIndexedError,
    RetrievalService,
)
from app.retrieval.vector_search import VectorSearcher
from app.schemas import (
    AnalyzeRepositoryRequest,
    BenchmarkExecutionRecord,
    BuildGraphRequest,
    IndexRepositoryRequest,
    RunBenchmarkRequest,
    SearchRepositoryRequest,
)

logger = logging.getLogger(__name__)

# Server-controlled: clients cannot choose their own timeout.
BENCHMARK_TIMEOUT_SECONDS = 10



def _cleanup_workers(settings, when: str) -> None:
    """Remove this deployment's leftover worker containers and workspaces. Never blocks or fails startup."""
    try:
        if settings.patch_test_backend == "container":
            from app.patching.container import cleanup_orphans

            report = cleanup_orphans(settings.instance_id)
            if report["removed"] or report["failed"]:
                logger.warning("worker cleanup at %s: removed=%d failed=%s", when, len(report["removed"]),
                               report["failed"])
        if when == "startup":
            from app.patching.workspace import WorkspaceManager

            removed = WorkspaceManager(settings.patch_workspace_dir).purge_all_inactive()
            if removed:
                logger.warning("removed %d abandoned workspace(s) at startup", removed)
    except Exception:
        logger.exception("worker cleanup at %s failed", when)


def _recover_evaluation_runs() -> None:
    """Close evaluations a previous process left active. Never blocks startup."""
    from datetime import UTC, datetime

    from app.database import SessionLocal
    from app.evaluation.routes import get_dispatcher
    from app.evaluation.service import recover_interrupted_runs

    try:
        with SessionLocal() as session:
            closed = recover_interrupted_runs(session, get_dispatcher().is_active, datetime.now(UTC))
        if closed:
            logger.warning("Closed %d interrupted evaluation run(s)", closed)
    except Exception:
        logger.exception("Could not recover interrupted evaluation runs")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the Neo4j driver and the embedding provider once per process."""
    settings = get_settings()
    configure_logging(json_logs=settings.log_format == "json" or (settings.log_format == "auto" and
                                                                  settings.is_deployed),
                      level=settings.log_level)
    register_secret_values(
        *(v.get_secret_value() for v in (settings.anthropic_api_key, settings.auth_secret_key, settings.neo4j_password,
                                         settings.embedding_api_key, settings.metrics_token, settings.database_url)
          if v is not None))
    logger.info("starting", extra={"event": "startup", **settings.public_summary()})
    _recover_evaluation_runs()
    app.state.graph_repository = None
    _cleanup_workers(settings, "startup")
    app.state.embedding_provider = None
    app.state.tool_registry = create_default_registry()
    app.state.llm_provider = None
    try:
        app.state.llm_provider = create_llm_provider(settings)
    except LLMConfigurationError as exc:
        # Investigation endpoints return 503; everything else keeps working.
        logger.warning("Debugging agent LLM not available: %s", exc)
    try:
        app.state.embedding_provider = create_embedding_provider(settings)
    except EmbeddingConfigurationError as exc:
        # Indexing returns 503 and search runs without the vector strategy.
        logger.warning("Embedding provider not available: %s", exc)
    config = Neo4jConfig.from_settings(settings)
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
        # Graceful shutdown: stop active evaluations between cases, then remove any worker still running.
        try:
            from app.evaluation.routes import get_dispatcher

            get_dispatcher().cancel_all(wait_seconds=10)
        except Exception:
            logger.exception("could not cancel active evaluations")
        _cleanup_workers(settings, "shutdown")
        if driver is not None:
            driver.close()
        close = getattr(app.state.embedding_provider, "close", None)
        if callable(close):
            close()


_docs = get_settings().api_docs_enabled
app = FastAPI(title="Atlas AI", lifespan=lifespan, docs_url="/docs" if _docs else None,
              redoc_url="/redoc" if _docs else None, openapi_url="/openapi.json" if _docs else None)
app.middleware("http")(limit_debug_request_size)
app.add_middleware(RequestContextMiddleware)  # outermost: every request, even a rejected one, gets an ID


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Defence in depth for the API and the docs pages: no sniffing, no framing, no referrer leaks."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith(("/evaluations", "/debug", "/repositories")):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


_settings = get_settings()
if _settings.cors_allowed_origins:
    from fastapi.middleware.cors import CORSMiddleware

    # Only the approved frontend origins; credentials are needed for the httpOnly refresh cookie.
    app.add_middleware(
        CORSMiddleware, allow_origins=_settings.cors_allowed_origins, allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Authorization", "Content-Type"],
        max_age=600)
app.include_router(auth_router)
app.include_router(debugging_router)
app.include_router(patching_router)
app.include_router(baselines_router)
app.include_router(evaluation_router)
install_error_handlers(app)


@app.exception_handler(GraphStoreError)
def graph_store_unavailable(request: Request, exc: GraphStoreError) -> JSONResponse:
    # Generic on purpose: never echo connection details or credentials to clients.
    logger.error("Graph store error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "Graph database unavailable"})


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process answers. Dependencies are checked by /ready."""
    return {"status": "healthy"}


@app.get("/ready")
def ready(request: Request) -> JSONResponse:
    """Readiness: database, graph store and worker backend. Fixed words only, no hosts or error text."""
    from app.core.observability import readiness
    from app.database import SessionLocal

    ok, checks = readiness(get_settings(), request.app.state, SessionLocal)
    return JSONResponse(status_code=200 if ok else 503, content={"status": "ready" if ok else "not_ready",
                                                                  "checks": checks})


def metrics_access(request: Request) -> None:
    """Prometheus scrapes with ``Authorization: Bearer <METRICS_TOKEN>``. Without a configured token: 404."""
    import hmac

    token = get_settings().metrics_token
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if token is None or not given or not hmac.compare_digest(given, token.get_secret_value()):
        raise HTTPException(status_code=404, detail="Not found")


@app.get("/metrics", dependencies=[Depends(metrics_access)], include_in_schema=False)
def prometheus_metrics() -> Response:
    from app.core import metrics

    return Response(metrics.render(), media_type="text/plain; version=0.0.4")


@app.post("/benchmarks/run")
def run_benchmark_endpoint(
    request: RunBenchmarkRequest, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> BenchmarkExecutionResult:
    # Runs pytest on the host (a committed benchmark only). Operators enable it; it is never on in production.
    settings = get_settings()
    if not settings.benchmarks_endpoint_enabled or settings.environment == "production":
        raise HTTPException(status_code=404, detail="Not found")
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Only an operator may run host benchmarks")
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
    user: User = Depends(current_user),
) -> list[BenchmarkExecutionRecord]:
    if get_benchmark(case_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown benchmark: {case_id}")
    return BenchmarkExecutionRepository(db).list_by_case(case_id, limit=limit)


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
    user: User = Depends(current_user),
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
viewer_access = repository_access(Role.VIEWER)  # one dependency object, reused where the repository itself is needed


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
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> GraphBuildResult:
    target = authorize_repository_path(request.repository_path, allowed_roots)
    authorize_repository(db, user, request.repository_id, Role.EDITOR, create=True)
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


@app.get("/repositories/{repository_id}/graph", dependencies=[Depends(repository_access(Role.VIEWER))])
def get_repository_graph(
    repository_id: RepositoryIdParam, service: GraphService = Depends(get_graph_service)
) -> CodeGraph:
    try:
        return service.get_graph(repository_id)
    except GraphNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No graph for {repository_id}") from exc


@app.get("/repositories/{repository_id}/graph/stats", dependencies=[Depends(repository_access(Role.VIEWER))])
def get_repository_graph_stats(
    repository_id: RepositoryIdParam, service: GraphService = Depends(get_graph_service)
) -> GraphStats:
    try:
        return service.get_stats(repository_id)
    except GraphNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No graph for {repository_id}") from exc


@app.delete("/repositories/{repository_id}/graph", status_code=204,
            dependencies=[Depends(repository_access(Role.OWNER))])
def delete_repository_graph(
    repository_id: RepositoryIdParam, service: GraphService = Depends(get_graph_service)
) -> Response:
    if not service.delete_graph(repository_id):
        raise HTTPException(status_code=404, detail=f"No graph for {repository_id}")
    return Response(status_code=204)


# --- code retrieval -----------------------------------------------------------


def get_repository_indexer(
    db: Session = Depends(get_db),
    provider: EmbeddingProvider | None = Depends(get_embedding_provider_optional),
) -> RepositoryIndexer:
    if provider is None:
        raise HTTPException(status_code=503, detail="Embedding provider is not configured")
    return RepositoryIndexer(
        ChunkRepository(db), provider, batch_size=get_settings().embedding_batch_size
    )


def get_retrieval_service(
    db: Session = Depends(get_db),
    provider: EmbeddingProvider | None = Depends(get_embedding_provider_optional),
    graph_source: GraphContextSource | None = Depends(get_graph_context_source),
) -> RetrievalService:
    chunks = ChunkRepository(db)
    return RetrievalService(
        chunks,
        KeywordSearcher(db),
        VectorSearcher(db, provider) if provider is not None else None,
        GraphSearcher(graph_source, chunks) if graph_source is not None else None,
    )


@app.post("/repositories/{repository_id}/index")
def index_repository_endpoint(
    repository_id: RepositoryIdParam,
    request: IndexRepositoryRequest,
    allowed_roots: list[Path] = Depends(get_allowed_repository_roots),
    indexer: RepositoryIndexer = Depends(get_repository_indexer),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> IndexResult:
    target = authorize_repository_path(request.repository_path, allowed_roots)
    authorize_repository(db, user, repository_id, Role.EDITOR, create=True)
    try:
        return indexer.index_repository(repository_id, target)
    except RepositoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RepositoryNotADirectoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RepositoryIndexConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IndexingError as exc:
        status = 503 if isinstance(exc.__cause__, EmbeddingError) else 500
        raise HTTPException(
            status_code=status,
            detail={"message": str(exc), "previous_index": "unchanged"},
        ) from exc


@app.get("/repositories/{repository_id}/index/status")
def get_index_status(
    repository_id: RepositoryIdParam,
    repository=Depends(viewer_access),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> IndexState:
    state = ChunkRepository(db).get_index_state(repository_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"No index for {repository_id}")
    role = membership_role(db, user, repository)
    if not user.is_admin and (role is None or ROLE_RANK[role] < ROLE_RANK[Role.EDITOR]):
        # A viewer reads code and results; the server's directory layout is not theirs to know.
        state = state.model_copy(update={"root_path": ""})
    return state


@app.delete("/repositories/{repository_id}/index", status_code=204,
            dependencies=[Depends(repository_access(Role.OWNER))])
def delete_index(repository_id: RepositoryIdParam, db: Session = Depends(get_db)) -> Response:
    if not ChunkRepository(db).delete_index(repository_id):
        raise HTTPException(status_code=404, detail=f"No index for {repository_id}")
    return Response(status_code=204)


@app.post("/repositories/{repository_id}/search", dependencies=[Depends(repository_access(Role.VIEWER))])
def search_repository(
    repository_id: RepositoryIdParam,
    request: SearchRepositoryRequest,
    service: RetrievalService = Depends(get_retrieval_service),
) -> RetrievalResponse:
    try:
        return service.retrieve_code(repository_id, request.query, request.limit)
    except InvalidRetrievalRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RepositoryNotIndexedError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
