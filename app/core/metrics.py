"""Prometheus metrics (Task 15.5). One registry per process, exposed at ``GET /metrics``.

Labels are small fixed sets (outcome, model, backend, route template). They never hold repository names, user input
or IDs, so cardinality stays bounded and nothing sensitive leaves through the metrics endpoint.
"""

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

REGISTRY = CollectorRegistry()
_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)

HTTP_REQUESTS = Counter("atlas_http_requests_total", "HTTP requests", ["method", "route", "status_class"],
                        registry=REGISTRY)
HTTP_LATENCY = Histogram("atlas_http_request_duration_seconds", "API request latency", ["method", "route"],
                         buckets=_BUCKETS, registry=REGISTRY)
WORKFLOW_DURATION = Histogram("atlas_agent_workflow_duration_seconds", "Debugging agent workflow duration",
                              ["status"], buckets=_BUCKETS, registry=REGISTRY)
LLM_REQUESTS = Counter("atlas_llm_requests_total", "Model requests", ["model", "outcome"], registry=REGISTRY)
LLM_LATENCY = Histogram("atlas_llm_request_duration_seconds", "Model request latency", ["model"], buckets=_BUCKETS,
                        registry=REGISTRY)
LLM_TOKENS = Counter("atlas_llm_tokens_total", "Model tokens", ["model", "kind"], registry=REGISTRY)
LLM_COST = Counter("atlas_llm_cost_usd_total", "Model cost in USD (tokens x list price)", ["model"],
                   registry=REGISTRY)
PATCH_EXECUTIONS = Counter("atlas_patch_executions_total", "Patch executions by final status", ["status"],
                           registry=REGISTRY)
PATCH_ATTEMPTS = Counter("atlas_patch_attempts_total", "Patch attempts by status", ["status"], registry=REGISTRY)
PATCH_REGRESSIONS = Counter("atlas_patch_regressions_total", "Attempts whose patch broke a passing test",
                            registry=REGISTRY)
TEST_DURATION = Histogram("atlas_test_run_duration_seconds", "Test execution duration", ["backend", "status"],
                          buckets=_BUCKETS, registry=REGISTRY)
WORKER_TIMEOUTS = Counter("atlas_worker_timeouts_total", "Test runs that timed out", ["backend"], registry=REGISTRY)
CONTAINER_CLEANUP_FAILURES = Counter("atlas_container_cleanup_failures_total",
                                     "Worker containers that could not be removed", registry=REGISTRY)
RETRIEVAL_LATENCY = Histogram("atlas_retrieval_duration_seconds", "Code retrieval latency", ["outcome"],
                              buckets=_BUCKETS, registry=REGISTRY)
EMAILS = Counter("atlas_emails_total", "Emails by template and outcome (sent or the failure category)",
                 ["template", "outcome"], registry=REGISTRY)
EVALUATION_RUNS = Counter("atlas_evaluation_runs_total", "Evaluation runs by final status", ["status"],
                          registry=REGISTRY)


def status_class(code: int) -> str:
    return f"{code // 100}xx"


def render() -> bytes:
    return generate_latest(REGISTRY)
