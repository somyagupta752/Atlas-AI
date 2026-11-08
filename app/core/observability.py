"""Instrumentation helpers: wrap an LLM provider, time test runs, scope a workflow ID, check readiness."""

import contextlib
import functools
import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

from sqlalchemy import text

from app.core import metrics
from app.core.logging import log_event, workflow_id_var

logger = logging.getLogger("atlas.observability")


@contextlib.contextmanager
def workflow_scope(workflow_id: str) -> Iterator[None]:
    """Everything logged inside carries this workflow ID (an investigation, an execution, an evaluation run)."""
    token = workflow_id_var.set(workflow_id)
    try:
        yield
    finally:
        workflow_id_var.reset(token)


class InstrumentedLLM:
    """Records every model call: count, latency, tokens and cost. Delegates everything else to the provider."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def model(self) -> str:
        return self._inner.model

    def generate(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any) -> Any:
        from app.agents.evaluation import cost_usd

        model = self._inner.model
        started = time.perf_counter()
        try:
            response = self._inner.generate(messages, tools, **kwargs)
        except Exception as exc:
            elapsed = time.perf_counter() - started
            metrics.LLM_REQUESTS.labels(model, type(exc).__name__).inc()
            metrics.LLM_LATENCY.labels(model).observe(elapsed)
            log_event(logger, "llm_request_failed", level=logging.WARNING, model=model, error_type=type(exc).__name__,
                      duration_ms=round(elapsed * 1000, 1))
            raise
        elapsed = time.perf_counter() - started
        usage = response.usage
        cost = cost_usd(response.model or model, usage)
        metrics.LLM_REQUESTS.labels(model, "ok").inc()
        metrics.LLM_LATENCY.labels(model).observe(elapsed)
        for kind, count in (("input", usage.input_tokens), ("output", usage.output_tokens),
                            ("cache_read", usage.cache_read_input_tokens),
                            ("cache_write", usage.cache_creation_input_tokens)):
            if count:
                metrics.LLM_TOKENS.labels(model, kind).inc(count)
        if cost:
            metrics.LLM_COST.labels(model).inc(cost)
        log_event(logger, "llm_request_completed", model=model, input_tokens=usage.input_tokens,
                  output_tokens=usage.output_tokens, cost_usd=cost, duration_ms=round(elapsed * 1000, 1),
                  tool_calls=len(response.tool_calls))
        return response


def instrument_test_run(backend: str) -> Callable:
    """Decorator for a test runner's ``run``: duration, timeouts and a log line per run."""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(self, workspace, targets, *, label, kind, timeout):
            started = time.perf_counter()
            result = fn(self, workspace, targets, label=label, kind=kind, timeout=timeout)
            elapsed = time.perf_counter() - started
            metrics.TEST_DURATION.labels(backend, result.status).observe(elapsed)
            timed_out = result.status == "timed_out" or (result.error or "").startswith("timed out")
            if timed_out:
                metrics.WORKER_TIMEOUTS.labels(backend).inc()
            log_event(logger, "test_run_completed", backend=backend, label=label, kind=kind, status=result.status,
                      tests=len(result.tests), duration_ms=round(elapsed * 1000, 1), timed_out=timed_out)
            return result

        return wrapper

    return decorator


# --- readiness ---------------------------------------------------------------------------------


def readiness(settings: Any, state: Any, session_factory: Callable) -> tuple[bool, dict[str, str]]:
    """``(ready, checks)``. Check values are fixed words; no host names, errors or credentials are returned."""
    checks: dict[str, str] = {}
    try:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:  # noqa: BLE001 - any failure means not ready; details must not leave the process
        checks["database"] = "unavailable"
    repository = getattr(state, "graph_repository", None)
    if repository is None:
        checks["neo4j"] = "not_configured"
    else:
        try:
            from app.knowledge_graph.neo4j_client import verify_connectivity

            verify_connectivity(repository._driver)
            checks["neo4j"] = "ok"
        except Exception:  # noqa: BLE001
            checks["neo4j"] = "unavailable"
    if settings.patch_test_backend == "container":
        try:
            from app.patching.container import ContainerLimits, ContainerTestRunner

            runner = ContainerTestRunner(container=ContainerLimits(image=settings.patch_container_image))
            checks["worker_backend"] = "ok" if runner.available() else "unavailable"
        except Exception:  # noqa: BLE001
            checks["worker_backend"] = "unavailable"
    else:
        checks["worker_backend"] = "local"
    ready = checks["database"] == "ok" and checks["neo4j"] in ("ok", "not_configured") and checks[
        "worker_backend"] in ("ok", "local")
    return ready, checks
