"""15.5: structured logs, correlation IDs, redaction, metrics, health and readiness."""

import json
import logging
import threading

import pytest
from fastapi.testclient import TestClient

from app.agents.llm import FakeLLM, LLMResponse, LLMUsage
from app.core import metrics
from app.core.logging import (
    JsonFormatter,
    accept_request_id,
    log_event,
    register_secret_values,
    request_id_var,
    run_in_context,
    scrub,
    workflow_id_var,
)
from app.core.observability import InstrumentedLLM, readiness, workflow_scope
from app.main import app

log = logging.getLogger("test.obs")


def sample(name, **labels):
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


def format_record(**extra):
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "message here", (), None)
    for k, v in extra.items():
        setattr(record, k, v)
    return json.loads(JsonFormatter().format(record))


# --- format and correlation --------------------------------------------------------------------


def test_a_log_line_is_one_json_object_with_the_required_fields():
    out = format_record(event="patch_verification_completed", outcome="verified", duration_ms=4200)
    assert {"timestamp", "level", "event", "request_id", "workflow_id", "outcome", "duration_ms"} <= set(out)
    assert out["event"] == "patch_verification_completed" and out["timestamp"].endswith("Z")
    assert out["level"] == "INFO" and out["duration_ms"] == 4200


def test_ids_come_from_context_and_nest():
    token = request_id_var.set("req_abc12345")
    try:
        assert format_record()["request_id"] == "req_abc12345" and format_record()["workflow_id"] is None
        with workflow_scope("wf_one"):
            assert format_record()["workflow_id"] == "wf_one"
        assert format_record()["workflow_id"] is None
    finally:
        request_id_var.reset(token)


def test_ids_survive_into_threads_only_when_the_context_is_copied():
    seen = {}
    token = request_id_var.set("req_thread01")
    w = workflow_id_var.set("wf_thread01")
    try:
        t1 = threading.Thread(target=lambda: seen.update(plain=format_record()["request_id"]))
        t2 = threading.Thread(target=run_in_context(lambda: seen.update(copied=format_record()["request_id"])))
        for t in (t1, t2):
            t.start()
            t.join()
    finally:
        workflow_id_var.reset(w)
        request_id_var.reset(token)
    assert seen == {"plain": None, "copied": "req_thread01"}


def test_caller_supplied_request_ids_are_accepted_only_if_safe():
    assert accept_request_id("req_valid-123") == "req_valid-123"
    for bad in (None, "", "short", "has space in it", "x" * 100, "evil\r\nSet-Cookie: a=b", '{"json":1}'):
        assert accept_request_id(bad).startswith("req_") and accept_request_id(bad) != bad


# --- redaction ---------------------------------------------------------------------------------


def test_secrets_code_and_credentials_never_reach_the_log():
    register_secret_values("super-secret-signing-key-value")
    out = format_record(
        event="x", api_key="sk-ant-api03-abcdefghijklmnopqrstuvwx", unified_diff="--- a/secret.py\n+password = 'x'",
        stdout="print(open('/etc/passwd').read())", note="signing with super-secret-signing-key-value now",
        url="postgresql://user:hunter2hunter2@db/x", authorization="Bearer abcdef", bug_report="my private bug")
    text = json.dumps(out)
    for leaked in ("abcdefghijklmnopqrstuvwx", "secret.py", "etc/passwd", "super-secret-signing-key-value",
                   "hunter2hunter2", "Bearer abcdef", "my private bug"):
        assert leaked not in text, leaked
    assert "size=" in out["unified_diff"]  # the size is kept, the content is not


def test_long_strings_and_nested_values_are_bounded_and_scrubbed():
    assert len(scrub("a" * 5000)) < 600
    nested = scrub({"a": {"password": "p", "ok": 1}, "list": ["sk-ant-api03-abcdefghijklmnopqrstuvwx"] * 100})
    assert nested["a"]["password"].startswith("[REDACTED]") and len(nested["list"]) == 50
    assert "abcdefghijklmnopqrstuvwx" not in json.dumps(nested)


def test_exception_logs_carry_the_type_not_the_traceback_or_secret_message():
    try:
        raise ValueError("token sk-ant-api03-abcdefghijklmnopqrstuvwx leaked")
    except ValueError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    out = json.loads(JsonFormatter().format(record))
    assert out["exception"]["type"] == "ValueError" and "abcdefghijklmnopqrstuvwx" not in json.dumps(out)
    assert "Traceback" not in json.dumps(out)


# --- HTTP ----------------------------------------------------------------------------------------


def test_every_response_has_a_request_id_and_an_access_log_line(json_logs):
    client = TestClient(app)
    response = client.get("/health", headers={"X-Request-ID": "req_from_caller1"})
    assert response.headers["x-request-id"] == "req_from_caller1"
    events = json_logs.events("http_request")
    assert events and events[-1]["request_id"] == "req_from_caller1" and events[-1]["route"] == "/health"
    assert events[-1]["status"] == 200 and events[-1]["duration_ms"] >= 0
    generated = client.get("/health").headers["x-request-id"]
    assert generated.startswith("req_") and generated != "req_from_caller1"


def test_access_logs_use_route_templates_and_never_the_query_or_body(json_logs):
    client = TestClient(app)
    client.get("/evaluations/00000000-0000-0000-0000-000000000000?secret=hunter2hunter2")
    client.post("/auth/login", json={"email": "a@example.com", "password": "pa55word-do-not-log"})
    raw = json_logs.raw
    assert "hunter2hunter2" not in raw and "pa55word-do-not-log" not in raw
    assert "00000000-0000-0000-0000-000000000000" not in raw
    assert any(e["route"] == "/evaluations/{run_id}" for e in json_logs.events("http_request"))


@pytest.mark.real_auth
def test_request_metrics_count_by_route_and_status_class():
    client = TestClient(app)
    before = sample("atlas_http_requests_total", method="GET", route="/health", status_class="2xx")
    client.get("/health")
    client.get("/health")
    assert sample("atlas_http_requests_total", method="GET", route="/health", status_class="2xx") == before + 2
    unauth = sample("atlas_http_requests_total", method="GET", route="/auth/me", status_class="4xx")
    client.get("/auth/me")
    assert sample("atlas_http_requests_total", method="GET", route="/auth/me", status_class="4xx") == unauth + 1
    assert sample("atlas_http_request_duration_seconds_count", method="GET", route="/health") >= 2


# --- health, readiness, metrics endpoint -----------------------------------------------------------


def test_health_does_not_touch_dependencies(monkeypatch):
    monkeypatch.setattr("app.database.SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert TestClient(app).get("/health").json() == {"status": "healthy"}


def test_ready_reports_fixed_words_and_hides_failure_details(monkeypatch):
    client = TestClient(app)
    ok = client.get("/ready")
    assert ok.status_code in (200, 503) and set(ok.json()["checks"]) == {"database", "neo4j", "worker_backend"}
    assert ok.json()["checks"]["database"] == "ok"

    class Broken:
        def __enter__(self):
            raise RuntimeError("password=hunter2 host=10.0.0.5")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("app.database.SessionLocal", lambda: Broken())
    down = client.get("/ready")
    assert down.status_code == 503 and down.json()["status"] == "not_ready"
    assert down.json()["checks"]["database"] == "unavailable"
    assert "hunter2" not in down.text and "10.0.0.5" not in down.text


def test_readiness_function_marks_each_dependency(monkeypatch):
    from types import SimpleNamespace

    from app.core.config import Settings

    settings = Settings(_env_file=None, database_url="postgresql://x/y", patch_test_backend="container",
                        patch_container_image="atlas-no-such-image:0")
    state = SimpleNamespace(graph_repository=None)
    from app.database import SessionLocal

    ready, checks = readiness(settings, state, SessionLocal)
    assert checks == {"database": "ok", "neo4j": "not_configured", "worker_backend": "unavailable"} and not ready


def test_metrics_endpoint_needs_the_scrape_token(monkeypatch):
    from pydantic import SecretStr

    from app.core.config import get_settings

    client = TestClient(app)
    assert client.get("/metrics").status_code == 404  # no token configured

    def with_token():
        return get_settings().model_copy(update={"metrics_token": SecretStr("scrape-token-0123456789")})

    monkeypatch.setattr("app.main.get_settings", with_token)
    assert client.get("/metrics").status_code == 404
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 404
    ok = client.get("/metrics", headers={"Authorization": "Bearer scrape-token-0123456789"})
    assert ok.status_code == 200 and "atlas_http_requests_total" in ok.text
    assert "scrape-token" not in ok.text and "sk-ant" not in ok.text


# --- LLM and workflow instrumentation -----------------------------------------------------------------


def test_llm_calls_record_count_tokens_cost_and_latency(json_logs):
    reply = LLMResponse(text="ok", usage=LLMUsage(input_tokens=1000, output_tokens=200), model="claude-opus-5-5")
    llm = InstrumentedLLM(FakeLLM([reply], model="claude-opus-5-5"))
    before = {
        "req": sample("atlas_llm_requests_total", model="claude-opus-5-5", outcome="ok"),
        "in": sample("atlas_llm_tokens_total", model="claude-opus-5-5", kind="input"),
        "cost": sample("atlas_llm_cost_usd_total", model="claude-opus-5-5"),
    }
    with workflow_scope("wf_llm00001"):
        llm.generate([{"role": "user", "content": "x"}], [])
    assert sample("atlas_llm_requests_total", model="claude-opus-5-5", outcome="ok") == before["req"] + 1
    assert sample("atlas_llm_tokens_total", model="claude-opus-5-5", kind="input") == before["in"] + 1000
    assert sample("atlas_llm_cost_usd_total", model="claude-opus-5-5") == pytest.approx(before["cost"] + 0.008)
    event = json_logs.events("llm_request_completed")[-1]
    assert event["workflow_id"] == "wf_llm00001" and event["output_tokens"] == 200 and event["cost_usd"] == 0.008
    assert llm.model == "claude-opus-5-5" and llm.name == "fake"  # delegation


def test_failed_llm_calls_are_counted_by_error_type(json_logs):
    from app.agents.llm import LLMTimeoutError

    llm = InstrumentedLLM(FakeLLM([LLMTimeoutError("slow")], model="claude-opus-5-5"))
    before = sample("atlas_llm_requests_total", model="claude-opus-5-5", outcome="LLMTimeoutError")
    with pytest.raises(LLMTimeoutError):
        llm.generate([], [])
    assert sample("atlas_llm_requests_total", model="claude-opus-5-5", outcome="LLMTimeoutError") == before + 1
    assert json_logs.events("llm_request_failed")[-1]["error_type"] == "LLMTimeoutError"


def test_log_event_helper_attaches_fields(json_logs):
    log_event(log, "custom_event", repository_id="repo_1", outcome="ok")
    event = json_logs.events("custom_event")[-1]
    assert event["repository_id"] == "repo_1" and event["outcome"] == "ok"
