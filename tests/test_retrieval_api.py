import pytest
from fastapi.testclient import TestClient

from app.code_intelligence import analyze_repository
from app.database import get_db
from app.knowledge_graph.builder import build_code_graph
from app.main import (
    app,
    get_allowed_repository_roots,
    get_embedding_provider_optional,
    get_graph_context_source,
)
from app.retrieval.embeddings import FakeEmbeddingProvider, HashingEmbeddingProvider
from app.retrieval.graph_search import InMemoryGraphContextSource

FILES = {
    "app/__init__.py": "",
    "app/service.py": """
        class UserService:
            def get_user(self, user_id):
                return self.load(user_id)

            def load(self, user_id):
                return {"id": user_id}
        """,
}


@pytest.fixture
def state():
    return {"provider": HashingEmbeddingProvider(), "graph": None}


@pytest.fixture
def client(db_session, tmp_path, state):
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_allowed_repository_roots] = lambda: [tmp_path]
    app.dependency_overrides[get_embedding_provider_optional] = lambda: state["provider"]
    app.dependency_overrides[get_graph_context_source] = lambda: state["graph"]
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def repo(write_repo):
    return write_repo(FILES, "api-repo")


def index(client, repository_id, root):
    return client.post(f"/repositories/{repository_id}/index", json={"repository_path": str(root)})


def test_index_then_search(client, repo, state):
    state["graph"] = InMemoryGraphContextSource(build_code_graph(analyze_repository(repo), "api-a"))

    indexed = index(client, "api-a", repo)
    assert indexed.status_code == 200
    body = indexed.json()
    assert body["status"] == "completed"
    assert body["index_version"] == 1
    assert body["chunks_total"] > 0

    response = client.post("/repositories/api-a/search", json={"query": "error in get_user()", "limit": 5})
    assert response.status_code == 200
    result = response.json()
    assert result["repository_id"] == "api-a"
    assert result["query"] == "error in get_user()"
    assert result["total_results"] == len(result["results"]) <= 5
    top = result["results"][0]
    assert top["qualified_name"] == "UserService.get_user"
    assert top["file_path"] == "app/service.py"
    assert set(top["retrieval_sources"]) == {"keyword", "vector", "graph"}
    assert {"chunk_id", "start_line", "end_line", "content", "score", "reason"} <= set(top)


def test_responses_never_expose_embeddings_or_credentials(client, repo):
    index(client, "api-a", repo)
    response = client.post("/repositories/api-a/search", json={"query": "get_user"})
    status = client.get("/repositories/api-a/index/status")

    for text in (response.text, status.text):
        assert '"embedding"' not in text
        assert "password" not in text.lower()
        assert "atlas_dev" not in text


def test_index_status(client, repo):
    assert client.get("/repositories/api-a/index/status").status_code == 404

    index(client, "api-a", repo)
    body = client.get("/repositories/api-a/index/status").json()

    assert body["status"] == "ready"
    assert body["index_version"] == 1
    assert body["chunk_count"] > 0
    assert body["embedding_model"].startswith("hashing:")


def test_delete_index(client, repo):
    index(client, "api-a", repo)

    assert client.delete("/repositories/api-a/index").status_code == 204
    assert client.delete("/repositories/api-a/index").status_code == 404
    assert client.post("/repositories/api-a/search", json={"query": "x"}).status_code == 404


@pytest.mark.parametrize("query", ["", "   "])
def test_empty_query_is_rejected(client, repo, query):
    index(client, "api-a", repo)

    assert client.post("/repositories/api-a/search", json={"query": query}).status_code == 422


@pytest.mark.parametrize("limit", [0, -1, 51, "ten"])
def test_invalid_limits_are_rejected(client, repo, limit):
    index(client, "api-a", repo)

    response = client.post("/repositories/api-a/search", json={"query": "get_user", "limit": limit})
    assert response.status_code == 422


def test_unknown_repository_returns_404(client):
    response = client.post("/repositories/never-indexed/search", json={"query": "get_user"})

    assert response.status_code == 404


@pytest.mark.parametrize("repository_id", ["UPPER", "has space", "-leading", "a" * 101])
def test_invalid_repository_ids_are_rejected(client, repo, repository_id):
    assert client.post(f"/repositories/{repository_id}/search", json={"query": "x"}).status_code in (404, 422)
    assert index(client, repository_id, repo).status_code in (404, 422)


def test_paths_outside_the_allowlist_are_forbidden(client, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")

    assert index(client, "api-a", outside).status_code == 403
    assert index(client, "api-a", "/etc").status_code == 403


def test_empty_allowlist_fails_closed(client, repo):
    app.dependency_overrides[get_allowed_repository_roots] = lambda: []  # noqa: PIE807 - FastAPI introspects the callable

    assert index(client, "api-a", repo).status_code == 403


def test_relative_and_missing_paths(client, tmp_path):
    assert client.post("/repositories/api-a/index", json={"repository_path": "relative/path"}).status_code == 422
    assert index(client, "api-a", tmp_path / "missing").status_code == 404


def test_id_bound_to_another_directory_is_a_conflict(client, repo, write_repo):
    index(client, "api-a", repo)
    other = write_repo({"x.py": "def x():\n    pass\n"}, "other")

    assert index(client, "api-a", other).status_code == 409


def test_embedding_failure_returns_503_and_keeps_the_previous_index(client, repo, state):
    index(client, "api-a", repo)
    (repo / "app/service.py").write_text("def changed():\n    return 2\n")
    state["provider"] = FakeEmbeddingProvider(model=state["provider"].model_id, fail_on_call=1)

    response = index(client, "api-a", repo)

    assert response.status_code == 503
    assert response.json()["detail"]["previous_index"] == "unchanged"
    status = client.get("/repositories/api-a/index/status").json()
    assert status["status"] == "failed"
    assert status["index_version"] == 1


def test_indexing_without_an_embedding_provider_returns_503(client, repo, state):
    state["provider"] = None

    assert index(client, "api-a", repo).status_code == 503


def test_search_without_an_embedding_provider_still_works(client, repo, state):
    index(client, "api-a", repo)
    state["provider"] = None

    response = client.post("/repositories/api-a/search", json={"query": "get_user"})

    assert response.status_code == 200
    vector = next(s for s in response.json()["strategies"] if s["source"] == "vector")
    assert vector["status"] == "unavailable"
