"""Graph retrieval Cypher against a real Neo4j.

The in-memory source is the reference: for the same graph, the Neo4j queries
must return the same neighbors, paths, seeds and unresolved references. Needs
the docker compose ``neo4j`` service; skips without it (REQUIRE_NEO4J=1 fails).
"""

import uuid

import pytest

from app.code_intelligence import analyze_repository
from app.knowledge_graph.builder import build_code_graph
from app.retrieval.chunker import chunk_repository
from app.retrieval.graph_search import GraphSearcher, InMemoryGraphContextSource
from app.retrieval.indexing import load_source_files
from tests.test_graph_search import FILES, ChunkList
from tests.test_neo4j_integration import driver, graph_repo  # noqa: F401  (fixtures)

pytestmark = pytest.mark.neo4j


@pytest.fixture
def stored(graph_repo, write_repo):  # noqa: F811
    repository_id = f"itest-{uuid.uuid4().hex[:12]}"
    graph_repo.track(repository_id)
    root = write_repo(FILES, repository_id)
    graph = build_code_graph(analyze_repository(root), repository_id)
    graph_repo.save_graph(graph)
    files, _ = load_source_files(root)
    chunks = ChunkList(chunk_repository(repository_id, files))
    return (
        repository_id,
        GraphSearcher(graph_repo, chunks),
        GraphSearcher(InMemoryGraphContextSource(graph), chunks),
    )


def summary(related):
    return sorted(
        (r.entity.id, r.evidence.relation, r.evidence.depth, r.evidence.confidence, round(r.evidence.score, 6))
        for r in related
    )


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_neighborhood_matches_the_in_memory_reference(stored, depth):
    repository_id, neo4j, memory = stored
    seed = f"function:{repository_id}:app/service.py::UserService.get_user"

    assert summary(neo4j.get_related_entities(repository_id, seed, max_depth=depth)) == summary(
        memory.get_related_entities(repository_id, seed, max_depth=depth)
    )


def test_cycles_match_the_in_memory_reference(stored):
    repository_id, neo4j, memory = stored
    seed = f"function:{repository_id}:app/loop.py::ping"

    assert summary(neo4j.get_related_entities(repository_id, seed, max_depth=3)) == summary(
        memory.get_related_entities(repository_id, seed, max_depth=3)
    )


def test_relationship_filter_and_direction(stored):
    repository_id, neo4j, _ = stored
    seed = f"function:{repository_id}:app/service.py::UserService.get_user"

    related = neo4j.get_related_entities(repository_id, seed, max_depth=2, relationship_types=["CALLS"])

    assert related
    assert all(step.relationship_type == "CALLS" for r in related for step in r.evidence.path)
    caller = next(r for r in related if r.entity.id.endswith("get_user_endpoint"))
    assert caller.evidence.path[0].direction == "incoming"


def test_search_seeds_and_unresolved_references_match(stored):
    repository_id, neo4j, memory = stored
    query = "500 error inside get_user()"

    assert neo4j.find_seeds(repository_id, query) == memory.find_seeds(repository_id, query)
    ids = [f"function:{repository_id}:app/loop.py::pong"]
    assert neo4j.unresolved_references(repository_id, ids) == memory.unresolved_references(repository_id, ids)
    neo_results = [(r.chunk_id, r.rank) for r in neo4j.search(repository_id, query, limit=10).results]
    mem_results = [(r.chunk_id, r.rank) for r in memory.search(repository_id, query, limit=10).results]
    assert neo_results == mem_results


def test_queries_never_cross_repositories(stored, graph_repo, write_repo):  # noqa: F811
    repository_id, neo4j, _ = stored
    other = f"itest-{uuid.uuid4().hex[:12]}"
    graph_repo.track(other)
    graph_repo.save_graph(build_code_graph(analyze_repository(write_repo(FILES, other)), other))

    seed = f"function:{repository_id}:app/service.py::UserService.get_user"
    related = neo4j.get_related_entities(repository_id, seed, max_depth=3)
    assert related
    assert all(f":{repository_id}:" in r.entity.id for r in related)

    # A seed from another repository is ignored, not expanded.
    foreign = f"function:{other}:app/service.py::UserService.get_user"
    assert neo4j.get_related_entities(repository_id, foreign, max_depth=2) == []
    assert neo4j.find_seeds("no-such-repo", "get_user") == {}


def test_invalid_depth_and_relationship_types_are_rejected(graph_repo):  # noqa: F811
    with pytest.raises(ValueError):
        graph_repo.expand_neighborhood("x", ["a"], 4, ["CALLS"], 10)
    with pytest.raises(ValueError):
        graph_repo.expand_neighborhood("x", ["a"], 1, ["CALLS]->() DETACH DELETE n //"], 10)
