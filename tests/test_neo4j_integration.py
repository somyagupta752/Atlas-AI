"""Real Neo4j: Cypher, constraints, transactions and the API end to end.

Needs the docker compose ``neo4j`` service and NEO4J_* settings. Each test
uses its own random repository IDs and deletes them afterwards, so the tests
never touch other data in the database. Set REQUIRE_NEO4J=1 to fail, rather
than skip, when Neo4j is unavailable.
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.code_intelligence import analyze_repository
from app.config import get_settings
from app.knowledge_graph import queries
from app.knowledge_graph.builder import build_code_graph
from app.knowledge_graph.neo4j_client import (
    Neo4jConfig,
    create_driver,
    verify_connectivity,
)
from app.knowledge_graph.neo4j_repository import Neo4jGraphRepository
from app.knowledge_graph.repository import GraphNotFoundError, GraphStoreError
from app.main import app, get_allowed_repository_roots

pytestmark = pytest.mark.neo4j

V1 = {
    "app/__init__.py": "",
    "app/base.py": "class BaseService:\n    def save(self):\n        pass\n",
    "app/user.py": """
        from app.base import BaseService

        class UserService(BaseService):
            def find(self):
                self.save()

            def legacy(self):
                pass

        def unused():
            print("x")
        """,
    "app/api.py": """
        from app.user import UserService

        def handler():
            UserService().find()
        """,
}
V2 = {  # legacy() and unused() removed; handler no longer calls find()
    "app/__init__.py": "",
    "app/base.py": V1["app/base.py"],
    "app/user.py": """
        from app.base import BaseService

        class UserService(BaseService):
            def find(self):
                self.save()
        """,
    "app/api.py": "from app.user import UserService\n\ndef handler():\n    UserService()\n",
}


def _unavailable(reason: str):
    if os.environ.get("REQUIRE_NEO4J") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def driver():
    config = Neo4jConfig.from_settings(get_settings())
    if config is None:
        _unavailable("NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD are not set")
    neo4j_driver = create_driver(config)
    try:
        verify_connectivity(neo4j_driver)
    except GraphStoreError as exc:
        neo4j_driver.close()
        _unavailable(f"Neo4j is not reachable: {exc}")
    yield neo4j_driver
    neo4j_driver.close()


@pytest.fixture
def graph_repo(driver):
    repo = Neo4jGraphRepository(driver, get_settings().neo4j_database)
    created: list[str] = []
    repo.track = created.append  # tests register IDs for cleanup
    yield repo
    for repository_id in created:
        repo.delete_graph(repository_id)


@pytest.fixture
def repo_id(graph_repo):
    value = f"itest-{uuid.uuid4().hex[:12]}"
    graph_repo.track(value)
    return value


def _build(write_repo, files, repository_id, name):
    return build_code_graph(analyze_repository(write_repo(files, name)), repository_id)


def _count(driver, query, **params):
    with driver.session(database=get_settings().neo4j_database) as session:
        return session.run(query, params).single()[0]


def test_schema_has_uniqueness_constraints(graph_repo, driver):
    graph_repo.ensure_schema()
    graph_repo.ensure_schema()  # idempotent

    with driver.session(database=get_settings().neo4j_database) as session:
        names = {record["name"] for record in session.run("SHOW CONSTRAINTS")}
    assert {"code_entity_id", "unresolved_reference_id"} <= names


def test_round_trip_is_lossless(graph_repo, repo_id, write_repo):
    graph = _build(write_repo, V1, repo_id, "v1")

    result = graph_repo.save_graph(graph)

    assert result.nodes_created == len(graph.nodes)
    assert result.relationships_created == len(graph.relationships)
    assert result.unresolved_created == len(graph.unresolved_references)
    assert graph_repo.get_graph(repo_id) == graph


def test_repeated_save_is_idempotent(graph_repo, repo_id, write_repo, driver):
    graph = _build(write_repo, V1, repo_id, "v1")
    graph_repo.save_graph(graph)

    again = graph_repo.save_graph(graph)

    assert again.model_dump() == dict.fromkeys(again.model_dump(), 0)
    assert _count(
        driver, "MATCH (n:CodeEntity {repository_id: $r}) RETURN count(n)", r=repo_id
    ) == len(graph.nodes)
    assert _count(
        driver, "MATCH (:CodeEntity {repository_id: $r})-[x]->() RETURN count(x)", r=repo_id
    ) == len(graph.relationships)


def test_replacement_removes_stale_nodes_relationships_and_references(
    graph_repo, repo_id, write_repo
):
    graph_repo.save_graph(_build(write_repo, V1, repo_id, "v1"))
    v2 = _build(write_repo, V2, repo_id, "v2")

    result = graph_repo.save_graph(v2)

    stored = graph_repo.get_graph(repo_id)
    assert stored == v2
    ids = {n.id for n in stored.nodes}
    assert f"function:{repo_id}:app/user.py::UserService.legacy" not in ids
    assert f"function:{repo_id}:app/user.py::unused" not in ids
    calls = {(r.source_id, r.target_id) for r in stored.relationships if r.type == "CALLS"}
    find = f"function:{repo_id}:app/user.py::UserService.find"
    assert (f"function:{repo_id}:app/api.py::handler", find) not in calls  # stale edge between survivors
    assert result.nodes_deleted == 2
    assert result.unresolved_deleted >= 1  # print() inside unused()


def test_failed_transaction_keeps_previous_graph(graph_repo, repo_id, write_repo, monkeypatch):
    v1 = _build(write_repo, V1, repo_id, "v1")
    graph_repo.save_graph(v1)
    # Fail after nodes and relationships were already upserted inside the transaction.
    monkeypatch.setattr(queries, "DELETE_STALE_NODES", "THIS IS NOT CYPHER")

    with pytest.raises(GraphStoreError):
        graph_repo.save_graph(_build(write_repo, V2, repo_id, "v2"))

    assert graph_repo.get_graph(repo_id) == v1


def test_failed_first_build_leaves_nothing(graph_repo, repo_id, write_repo, monkeypatch):
    monkeypatch.setattr(queries, "DELETE_STALE_UNRESOLVED", "THIS IS NOT CYPHER")

    with pytest.raises(GraphStoreError):
        graph_repo.save_graph(_build(write_repo, V1, repo_id, "v1"))

    with pytest.raises(GraphNotFoundError):
        graph_repo.get_graph(repo_id)


def test_reusable_queries(graph_repo, repo_id, write_repo):
    graph_repo.save_graph(_build(write_repo, V1, repo_id, "v1"))
    prefix = f"{repo_id}:app"

    classes = [n.id for n in graph_repo.find_classes(repo_id)]
    methods = [n.properties["name"] for n in graph_repo.find_methods(repo_id, "UserService")]
    callers = graph_repo.find_callers(f"function:{prefix}/base.py::BaseService.save")
    importers = graph_repo.find_importers(f"file:{prefix}/user.py")
    subclasses = graph_repo.find_subclasses(f"class:{prefix}/base.py::BaseService")

    assert classes == [f"class:{prefix}/base.py::BaseService", f"class:{prefix}/user.py::UserService"]
    assert methods == ["find", "legacy"]
    assert [n.id for n in callers] == [f"function:{prefix}/user.py::UserService.find"]
    assert [n.id for n in importers] == [f"file:{prefix}/api.py"]
    assert [n.id for n in subclasses] == [f"class:{prefix}/user.py::UserService"]
    assert subclasses[0].properties["qualified_name"] == "UserService"


def test_repositories_are_isolated(graph_repo, write_repo):
    first, second = f"itest-{uuid.uuid4().hex[:12]}", f"itest-{uuid.uuid4().hex[:12]}"
    graph_repo.track(first)
    graph_repo.track(second)
    graph_repo.save_graph(_build(write_repo, V1, first, "one"))
    other = _build(write_repo, V1, second, "two")
    graph_repo.save_graph(other)

    assert graph_repo.delete_graph(first) is True

    assert graph_repo.get_graph(second) == other
    with pytest.raises(GraphNotFoundError):
        graph_repo.get_stats(first)


def test_stats_and_delete(graph_repo, repo_id, write_repo, driver):
    graph = _build(write_repo, V1, repo_id, "v1")
    graph_repo.save_graph(graph)

    stats = graph_repo.get_stats(repo_id)

    assert stats.total_nodes == len(graph.nodes)
    assert stats.total_relationships == len(graph.relationships)
    assert stats.unresolved_references == len(graph.unresolved_references)
    assert stats.nodes_by_label == {"Class": 2, "File": 4, "Function": 5, "Repository": 1}
    assert graph_repo.delete_graph(repo_id) is True
    assert _count(
        driver, "MATCH (u:UnresolvedReference {repository_id: $r}) RETURN count(u)", r=repo_id
    ) == 0
    assert graph_repo.delete_graph(repo_id) is False


def test_api_end_to_end_with_real_lifespan(graph_repo, repo_id, write_repo, tmp_path):
    root = write_repo(V1, "api")
    app.dependency_overrides[get_allowed_repository_roots] = lambda: [tmp_path]
    try:
        with TestClient(app) as client:  # runs startup (driver + schema) and shutdown
            built = client.post(
                "/repositories/graph/build",
                json={"repository_path": str(root), "repository_id": repo_id},
            )
            rebuilt = client.post(
                "/repositories/graph/build",
                json={"repository_path": str(root), "repository_id": repo_id},
            )
            graph = client.get(f"/repositories/{repo_id}/graph")
            stats = client.get(f"/repositories/{repo_id}/graph/stats")
            deleted = client.delete(f"/repositories/{repo_id}/graph")
            missing = client.get(f"/repositories/{repo_id}/graph")
    finally:
        app.dependency_overrides.clear()

    assert built.status_code == 200
    assert built.json()["nodes_created"] == built.json()["total_nodes"] == 12
    assert rebuilt.json()["nodes_created"] == 0
    assert len(graph.json()["nodes"]) == 12
    assert stats.json()["relationships_by_type"]["EXTENDS"] == 1
    assert deleted.status_code == 204
    assert missing.status_code == 404
