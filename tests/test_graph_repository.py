"""In-memory repository, the service layer, the graph API, and Neo4j wiring with mocks.

The mocked Neo4j tests check *how* the driver is used (transactions, batching,
error translation). They prove nothing about the Cypher itself; that lives in
test_neo4j_integration.py, which runs against a real server.
"""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from neo4j.exceptions import ServiceUnavailable

from app.knowledge_graph import queries
from app.knowledge_graph.models import CodeGraph, GraphNode, NodeLabel
from app.knowledge_graph.neo4j_repository import Neo4jGraphRepository
from app.knowledge_graph.repository import (
    GraphNotFoundError,
    GraphStoreError,
    InMemoryGraphRepository,
    InvalidGraphError,
)
from app.knowledge_graph.service import GraphService, RepositoryIdConflictError
from app.main import app, get_allowed_repository_roots, get_graph_repository

FILES_V1 = {
    "a.py": "from b import helper\n\ndef run():\n    helper()\n",
    "b.py": "def helper():\n    pass\n\ndef old():\n    pass\n",
}
FILES_V2 = {
    "a.py": "from b import helper\n\ndef run():\n    helper()\n",
    "b.py": "def helper():\n    pass\n",
}


def _invalid_graph() -> CodeGraph:
    return CodeGraph(
        repository_id="demo",
        nodes=[GraphNode(id="file:demo:a.py", label=NodeLabel.FILE, properties={})],
    )


# --- in-memory repository ---------------------------------------------------


def test_save_get_and_stats(make_graph):
    graph = make_graph(FILES_V1)
    repo = InMemoryGraphRepository()

    result = repo.save_graph(graph)

    assert result.nodes_created == len(graph.nodes)
    assert result.relationships_created == len(graph.relationships)
    assert repo.get_graph("demo") == graph
    stats = repo.get_stats("demo")
    assert stats.total_nodes == len(graph.nodes)
    assert stats.nodes_by_label["Function"] == 3
    assert stats.relationships_by_type["CALLS"] == 1


def test_update_replaces_graph_and_reports_stale_items(make_graph):
    repo = InMemoryGraphRepository()
    repo.save_graph(make_graph(FILES_V1, name="v1"))

    result = repo.save_graph(make_graph(FILES_V2, name="v2"))

    assert result.nodes_created == 0
    assert result.nodes_deleted == 1  # b.py::old
    assert result.relationships_deleted == 1  # its CONTAINS edge
    ids = {n.id for n in repo.get_graph("demo").nodes}
    assert "function:demo:b.py::old" not in ids


def test_repeated_save_is_idempotent(make_graph):
    graph = make_graph(FILES_V1)
    repo = InMemoryGraphRepository()
    repo.save_graph(graph)

    result = repo.save_graph(graph)

    assert result.model_dump() == dict.fromkeys(result.model_dump(), 0)
    assert repo.get_graph("demo") == graph


def test_delete(make_graph):
    repo = InMemoryGraphRepository()
    repo.save_graph(make_graph(FILES_V1))

    assert repo.delete_graph("demo") is True
    assert repo.delete_graph("demo") is False
    with pytest.raises(GraphNotFoundError):
        repo.get_graph("demo")
    with pytest.raises(GraphNotFoundError):
        repo.get_stats("demo")


def test_invalid_graph_is_rejected_and_previous_graph_kept(make_graph):
    repo = InMemoryGraphRepository()
    good = make_graph(FILES_V1)
    repo.save_graph(good)

    with pytest.raises(InvalidGraphError) as excinfo:
        repo.save_graph(_invalid_graph())

    assert {e.code for e in excinfo.value.result.errors} >= {"MISSING_REPOSITORY_NODE"}
    assert repo.get_graph("demo") == good


def test_stored_graph_is_isolated_from_caller_mutation(make_graph):
    graph = make_graph(FILES_V1)
    repo = InMemoryGraphRepository()
    repo.save_graph(graph)

    graph.nodes.clear()
    repo.get_graph("demo").relationships.clear()

    assert len(repo.get_graph("demo").nodes) > 0
    assert len(repo.get_graph("demo").relationships) > 0


# --- service ----------------------------------------------------------------


def test_service_builds_and_reports(write_repo):
    service = GraphService(InMemoryGraphRepository())

    result = service.build_graph(write_repo(FILES_V1), "demo")

    assert result.status == "completed"
    assert (result.files_analyzed, result.files_failed) == (2, 0)
    assert result.nodes_created == result.total_nodes
    assert result.unresolved_references == 0


def test_service_refuses_to_overwrite_another_directorys_graph(write_repo):
    service = GraphService(InMemoryGraphRepository())
    service.build_graph(write_repo(FILES_V1, name="one"), "shared")

    with pytest.raises(RepositoryIdConflictError):
        service.build_graph(write_repo(FILES_V1, name="two"), "shared")


# --- API --------------------------------------------------------------------


@pytest.fixture
def graph_api(tmp_path):
    repository = InMemoryGraphRepository()
    app.dependency_overrides[get_graph_repository] = lambda: repository
    app.dependency_overrides[get_allowed_repository_roots] = lambda: [tmp_path]
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_api_build_get_stats_delete(graph_api, write_repo):
    root = write_repo(FILES_V1)

    built = graph_api.post(
        "/repositories/graph/build", json={"repository_path": str(root), "repository_id": "demo"}
    )
    assert built.status_code == 200
    body = built.json()
    assert body["repository_id"] == "demo"
    assert body["status"] == "completed"
    assert body["nodes_created"] == body["total_nodes"] > 0

    graph = graph_api.get("/repositories/demo/graph")
    assert graph.status_code == 200
    assert len(graph.json()["nodes"]) == body["total_nodes"]

    stats = graph_api.get("/repositories/demo/graph/stats").json()
    assert stats["total_relationships"] == body["total_relationships"]
    assert stats["root_path"] == str(root.resolve())

    assert graph_api.delete("/repositories/demo/graph").status_code == 204
    assert graph_api.get("/repositories/demo/graph").status_code == 404
    assert graph_api.delete("/repositories/demo/graph").status_code == 404


def test_api_derives_repository_id_from_directory(graph_api, write_repo):
    root = write_repo(FILES_V1, name="My_Project")

    body = graph_api.post("/repositories/graph/build", json={"repository_path": str(root)}).json()

    assert body["repository_id"] == "my_project"


def test_api_rejects_bad_input(graph_api, tmp_path):
    def build(payload):
        return graph_api.post("/repositories/graph/build", json=payload).status_code

    assert build({"repository_path": str(tmp_path / "missing")}) == 404
    assert build({"repository_path": "/etc"}) == 403
    assert build({"repository_path": str(tmp_path), "repository_id": "Bad Id"}) == 422
    assert graph_api.get("/repositories/Bad Id/graph").status_code == 422
    assert graph_api.get("/repositories/unknown/graph/stats").status_code == 404


def test_api_conflict_is_409(graph_api, write_repo):
    one, two = write_repo(FILES_V1, name="one"), write_repo(FILES_V1, name="two")
    graph_api.post("/repositories/graph/build", json={"repository_path": str(one), "repository_id": "x"})

    response = graph_api.post(
        "/repositories/graph/build", json={"repository_path": str(two), "repository_id": "x"}
    )

    assert response.status_code == 409


def test_api_without_graph_database_is_503(tmp_path):
    app.state.graph_repository = None
    app.dependency_overrides[get_allowed_repository_roots] = lambda: [tmp_path]
    try:
        response = TestClient(app).get("/repositories/demo/graph")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503


def test_api_store_failure_is_503_without_details(graph_api):
    failing = MagicMock()
    failing.get_graph.side_effect = GraphStoreError("bolt://neo4j:secret@host")
    app.dependency_overrides[get_graph_repository] = lambda: failing

    response = graph_api.get("/repositories/demo/graph")

    assert response.status_code == 503
    assert "secret" not in response.text
    assert response.json() == {"detail": "Graph database unavailable"}


# --- Neo4j repository with a mocked driver ------------------------------------


def _mock_driver(written=None):
    """A driver whose execute_write/read run the work function against a mock tx."""
    tx = MagicMock()

    def run(query, *args, **kwargs):
        result = MagicMock()
        params = {**(args[0] if args else {}), **kwargs}
        batch = params.get("relationships")
        count = len(batch) if batch is not None else 0
        result.single.return_value = {"written": count if written is None else written}
        counters = result.consume.return_value.counters
        counters.nodes_created = counters.nodes_deleted = 0
        counters.relationships_created = counters.relationships_deleted = 0
        return result

    tx.run.side_effect = run
    session = MagicMock()
    session.execute_write.side_effect = lambda work, *a: work(tx, *a)
    session.execute_read.side_effect = lambda work, *a: work(tx, *a)
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    return driver, session, tx


def test_neo4j_save_uses_one_write_transaction_and_batches(make_graph):
    graph = make_graph(FILES_V1)
    driver, session, tx = _mock_driver()
    repo = Neo4jGraphRepository(driver, "neo4j", batch_size=2)

    repo.save_graph(graph)

    assert session.execute_write.call_count == 1
    statements = [c.args[0] for c in tx.run.call_args_list]
    assert statements[0] == queries.LOCK_REPOSITORY
    # Batched UNWIND writes: far fewer queries than nodes + relationships.
    function_batches = [s for s in statements if s == queries.UPSERT_NODES[NodeLabel.FUNCTION]]
    assert len(function_batches) == 2  # 3 functions, batch size 2
    assert statements[-3:] == [
        queries.DELETE_STALE_RELATIONSHIPS,
        queries.DELETE_STALE_NODES,
        queries.DELETE_STALE_UNRESOLVED,
    ]


def test_neo4j_query_count_does_not_grow_with_graph_size(make_graph):
    source = "\n".join(f"def f{i}():\n    f{i + 1}()\n" for i in range(200))
    graph = make_graph({"big.py": source})
    driver, _, tx = _mock_driver()

    Neo4jGraphRepository(driver).save_graph(graph)

    assert len(graph.nodes) + len(graph.relationships) > 500
    # lock + 3 node labels + 2 relationship types + 1 unresolved batch + 3 stale deletes
    assert tx.run.call_count == 10


def test_neo4j_queries_are_parameterized(make_graph):
    graph = make_graph(FILES_V1)
    driver, _, tx = _mock_driver()

    Neo4jGraphRepository(driver).save_graph(graph)

    for call in tx.run.call_args_list:
        query = call.args[0]
        assert "file:demo" not in query and "a.py" not in query


def test_neo4j_validates_before_touching_the_database():
    driver, session, _ = _mock_driver()

    with pytest.raises(InvalidGraphError):
        Neo4jGraphRepository(driver).save_graph(_invalid_graph())

    driver.session.assert_not_called()
    session.execute_write.assert_not_called()


def test_neo4j_partial_relationship_write_aborts_the_transaction(make_graph):
    driver, _, _ = _mock_driver(written=0)

    with pytest.raises(GraphStoreError, match="Wrote 0"):
        Neo4jGraphRepository(driver).save_graph(make_graph(FILES_V1))


def test_neo4j_driver_errors_are_translated_without_details(make_graph):
    driver, session, _ = _mock_driver()
    session.execute_write.side_effect = ServiceUnavailable("bolt://user:secret@db:7687")
    repo = Neo4jGraphRepository(driver)
    repo._schema_ready = True

    with pytest.raises(GraphStoreError) as excinfo:
        repo.save_graph(make_graph(FILES_V1))

    assert "secret" not in str(excinfo.value)
    assert "ServiceUnavailable" in str(excinfo.value)
