import pytest
from fastapi.testclient import TestClient

from app.agents.llm import FakeLLM, tool_call
from app.agents.routes import (
    get_agent_retrieval,
    get_llm_provider,
    get_trusted_test_roots,
)
from app.database import get_db
from app.main import app, get_allowed_repository_roots, get_graph_context_source
from app.retrieval.embeddings import HashingEmbeddingProvider
from app.retrieval.indexing import RepositoryIndexer
from app.retrieval.keyword_search import KeywordSearcher
from app.retrieval.service import RetrievalService
from app.retrieval.vector_search import VectorSearcher
from tests.agents.conftest import standard_script


@pytest.fixture
def llm_holder():
    return {"llm": FakeLLM(standard_script())}


@pytest.fixture
def client(db_session, chunk_repo, tmp_path, llm_holder):
    retrieval = RetrievalService(chunk_repo, KeywordSearcher(db_session), VectorSearcher(db_session, HashingEmbeddingProvider()))
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_llm_provider] = lambda: llm_holder["llm"]
    app.dependency_overrides[get_allowed_repository_roots] = lambda: [tmp_path]
    app.dependency_overrides[get_trusted_test_roots] = lambda: [tmp_path]
    app.dependency_overrides[get_agent_retrieval] = lambda: retrieval
    app.dependency_overrides[get_graph_context_source] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def indexed(bench_repo, chunk_repo):
    root = bench_repo("bug_001_average")
    RepositoryIndexer(chunk_repo, HashingEmbeddingProvider()).index_repository("api-average", root)
    return root


def investigate(client, **body):
    return client.post("/debug/investigate", json={"repository_id": "api-average", "bug_report": "crash", **body})


def test_investigate_then_get_and_observations(client, indexed, llm_holder):
    llm_holder["llm"] = FakeLLM([tool_call("retrieve_evidence", {"query": "average calculation crashes"}), *standard_script()])
    # Retrieval adds one observation first, so the diagnosis must cite shifted IDs.
    import json

    from tests.agents.conftest import diagnosis

    answer = diagnosis()
    answer["evidence"][0]["observation_ids"] = ["obs-3"]
    answer["evidence"][1]["observation_ids"] = ["obs-4"]
    llm_holder["llm"].script[-1].text = json.dumps(answer)

    response = investigate(client, bug_report="The average calculation crashes when the input list is empty.")
    assert response.status_code == 200, response.text
    body = response.json()
    for key in ["investigation_id", "status", "summary", "root_cause", "confidence", "hypotheses", "evidence",
                "affected_files", "suggested_fix", "tests_run", "unresolved_questions"]:
        assert key in body
    assert body["status"] == "completed" and body["tests_run"][0]["outcome"] == "failed"

    stored = client.get(f"/debug/investigations/{body['investigation_id']}")
    assert stored.status_code == 200
    assert stored.json()["status"] == "completed" and stored.json()["result"]["root_cause"] == body["root_cause"]

    observations = client.get(f"/debug/investigations/{body['investigation_id']}/observations").json()
    tools = [e["payload"]["tool_name"] for e in observations["events"] if e["event_type"] == "observation"]
    assert tools == ["retrieve_evidence", "search_code", "read_file", "run_tests"]
    timeline = client.get(f"/debug/investigations/{body['investigation_id']}/observations?timeline=true").json()
    assert timeline["events"][0]["event_type"] == "investigation_created"


def test_unknown_repository(client):
    assert investigate(client, repository_id="never-indexed").status_code == 404


@pytest.mark.parametrize(
    "body",
    [{"repository_id": "api-average", "bug_report": ""}, {"repository_id": "api-average", "bug_report": "   "},
     {"repository_id": "Bad ID", "bug_report": "x"}, {"repository_id": "api-average"},
     {"repository_id": "api-average", "bug_report": "x" * 10_001}],
)
def test_invalid_requests(client, body):
    assert client.post("/debug/investigate", json=body).status_code == 422


def test_request_size_limit(client):
    response = client.post("/debug/investigate", content=b"{" + b" " * 70_000 + b"}", headers={"content-type": "application/json"})
    assert response.status_code == 413


def test_repository_outside_allowed_roots(client, indexed):
    app.dependency_overrides[get_allowed_repository_roots] = lambda: []  # noqa: PIE807 - FastAPI inspects the callable
    assert investigate(client).status_code == 403


def test_llm_not_configured(client, indexed):
    del app.dependency_overrides[get_llm_provider]
    app.state.llm_provider = None
    assert investigate(client).status_code == 503


def test_llm_timeout_returns_504_with_investigation_id(client, indexed, llm_holder):
    from app.agents.llm import LLMTimeoutError

    llm_holder["llm"] = FakeLLM([LLMTimeoutError("slow")])
    response = investigate(client)
    assert response.status_code == 504
    body = response.json()
    assert body["failure_reason"] == "llm_timeout" and "slow" not in str(body)
    assert client.get(f"/debug/investigations/{body['investigation_id']}").json()["status"] == "failed"


def test_limit_reached_is_returned_as_a_result(client, indexed, llm_holder):
    llm_holder["llm"] = FakeLLM([tool_call("search_code", {"query": f"q{i}"}) for i in range(30)])
    from app.agents.routes import get_agent_limits
    from app.agents.schemas import AgentLimits

    app.dependency_overrides[get_agent_limits] = lambda: AgentLimits(max_iterations=3)
    response = investigate(client)
    assert response.status_code == 200 and response.json()["status"] == "limit_reached"


def test_get_unknown_and_malformed_investigation(client):
    assert client.get("/debug/investigations/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/debug/investigations/not-a-uuid").status_code == 422
    assert client.get("/debug/investigations/00000000-0000-0000-0000-000000000000/observations").status_code == 404


def test_observations_are_scoped_by_repository(client, indexed):
    body = investigate(client).json()
    other = client.get(f"/debug/investigations/{body['investigation_id']}/observations?repository_id=another-repo")
    assert other.status_code == 404
