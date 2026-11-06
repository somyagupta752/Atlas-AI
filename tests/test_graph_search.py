import pytest

from app.code_intelligence import analyze_repository
from app.knowledge_graph.builder import build_code_graph
from app.retrieval.chunker import chunk_repository
from app.retrieval.graph_search import (
    GraphSearcher,
    InMemoryGraphContextSource,
    call_direction_preference,
)
from app.retrieval.indexing import load_source_files
from app.retrieval.models import Confidence, RetrievalSource

FILES = {
    "app/__init__.py": "",
    "app/repo.py": """
        class UserRepository:
            def find_by_id(self, user_id):
                return {"id": user_id}
        """,
    "app/service.py": """
        from app.repo import UserRepository

        class BaseService:
            def log(self):
                pass

        class UserService(BaseService):
            def __init__(self):
                self.repository = UserRepository()

            def get_user(self, user_id):
                self.log()
                return self.repository.find_by_id(user_id)
        """,
    "app/api.py": """
        from app.service import UserService

        def get_user_endpoint(user_id):
            service = UserService()
            return service.get_user(user_id)
        """,
    "app/loop.py": """
        def ping(n):
            return pong(n - 1)

        def pong(n):
            if n:
                return ping(n)
            return external.call()
        """,
}

GET_USER = "function:demo:app/service.py::UserService.get_user"
FIND_BY_ID = "function:demo:app/repo.py::UserRepository.find_by_id"
ENDPOINT = "function:demo:app/api.py::get_user_endpoint"
USER_SERVICE = "class:demo:app/service.py::UserService"
BASE_SERVICE = "class:demo:app/service.py::BaseService"
PING = "function:demo:app/loop.py::ping"
PONG = "function:demo:app/loop.py::pong"


class ChunkList:
    """In-memory ChunkLookup over chunks, optionally with some entities missing."""

    def __init__(self, chunks, drop=()):
        self.chunks = [c for c in chunks if c.entity_id not in set(drop)]

    def chunks_for_entities(self, repository_id, entity_ids):
        wanted = set(entity_ids)
        return [c for c in self.chunks if c.repository_id == repository_id and c.entity_id in wanted]


@pytest.fixture
def built(write_repo):
    root = write_repo(FILES)
    graph = build_code_graph(analyze_repository(root), "demo")
    files, _ = load_source_files(root)
    return graph, chunk_repository("demo", files)


@pytest.fixture
def searcher(built):
    graph, chunks = built
    return GraphSearcher(InMemoryGraphContextSource(graph), ChunkList(chunks))


def related(searcher, seed, **kwargs):
    return {r.entity.id: r for r in searcher.get_related_entities("demo", seed, **kwargs)}


def test_callers_and_callees_are_retrieved_with_direction(searcher):
    found = related(searcher, GET_USER, max_depth=1)

    caller = found[ENDPOINT].evidence
    assert caller.relation == "caller"
    assert caller.path[0].relationship_type == "CALLS"
    assert caller.path[0].direction == "incoming"
    assert (caller.path[0].source_id, caller.path[0].target_id) == (ENDPOINT, GET_USER)

    callee = found[FIND_BY_ID].evidence
    assert callee.relation == "callee"
    assert callee.path[0].direction == "outgoing"


def test_containing_class_and_file_are_retrieved(searcher):
    found = related(searcher, GET_USER, max_depth=1)

    assert found[USER_SERVICE].evidence.relation == "containing class"
    assert "file:demo:app/service.py" not in found  # a method is contained by its class


def test_depth_controls_how_far_the_expansion_goes(searcher):
    shallow = related(searcher, GET_USER, max_depth=1)
    deep = related(searcher, GET_USER, max_depth=2)

    assert BASE_SERVICE not in shallow
    assert deep[BASE_SERVICE].evidence.relation == "containing class -> base class"
    assert deep[BASE_SERVICE].evidence.depth == 2
    assert "file:demo:app/service.py" in deep
    assert all(r.evidence.depth <= 1 for r in shallow.values())
    with pytest.raises(ValueError):
        searcher.get_related_entities("demo", GET_USER, max_depth=4)
    with pytest.raises(ValueError):
        searcher.get_related_entities("demo", GET_USER, max_depth=0)


def test_direct_neighbors_outrank_distant_ones(searcher):
    ranked = searcher.get_related_entities("demo", GET_USER, max_depth=2)
    scores = {r.entity.id: r.evidence.score for r in ranked}

    # One hop beats two; an inferred caller (1.0 x 0.7) still beats a two-hop base class.
    assert scores[USER_SERVICE] > scores[BASE_SERVICE]
    assert scores[ENDPOINT] > scores[BASE_SERVICE]
    assert [r.evidence.score for r in ranked] == sorted(scores.values(), reverse=True)


def test_inferred_relationships_are_marked_and_weigh_less(searcher):
    found = related(searcher, GET_USER, max_depth=1)

    # self.repository = UserRepository() is type evidence, not proof.
    assert found[FIND_BY_ID].evidence.confidence == Confidence.INFERRED
    assert found[FIND_BY_ID].evidence.path[0].resolution_status == "inferred"
    assert found[ENDPOINT].evidence.confidence in (Confidence.CONFIRMED, Confidence.INFERRED)
    assert found[FIND_BY_ID].evidence.score < 0.9  # callee weight x inferred factor


def test_cycles_do_not_loop_or_duplicate(searcher):
    ranked = searcher.get_related_entities("demo", PING, max_depth=3)
    ids = [r.entity.id for r in ranked]

    assert len(ids) == len(set(ids))
    assert PING not in ids  # the seed is never its own neighbor
    pong = next(r for r in ranked if r.entity.id == PONG)
    assert pong.evidence.depth == 1
    assert pong.supporting_paths >= 2  # reached as callee and as caller
    for result in ranked:
        nodes = [result.evidence.seed_entity_id] + [
            s.target_id if s.direction == "outgoing" else s.source_id for s in result.evidence.path
        ]
        assert len(nodes) == len(set(nodes))


def test_relationship_types_can_be_restricted(searcher):
    found = related(searcher, GET_USER, max_depth=1, relationship_types=["CALLS"])

    assert set(found) == {ENDPOINT, FIND_BY_ID, "function:demo:app/service.py::BaseService.log"}
    assert all(r.evidence.path[0].relationship_type == "CALLS" for r in found.values())


def test_unresolved_references_are_preserved(searcher):
    unresolved = searcher.unresolved_references("demo", [PONG])

    refs = unresolved[PONG]
    assert any(ref.expression == "external.call" for ref in refs)
    assert all(ref.reason for ref in refs)


def test_search_finds_the_named_function_and_its_neighborhood(searcher):
    outcome = searcher.search("demo", "500 error inside get_user()", limit=10)
    names = [r.qualified_name for r in outcome.results]

    assert names[0] == "UserService.get_user"
    assert outcome.results[0].graph_evidence[0].relation == "named in query"
    assert "get_user_endpoint" in names
    assert "UserRepository.find_by_id" in names
    assert all(r.retrieval_source == RetrievalSource.GRAPH for r in outcome.results)
    assert [r.rank for r in outcome.results] == list(range(1, len(names) + 1))


def test_external_seeds_are_expanded_but_not_returned_on_their_own(searcher):
    outcome = searcher.search("demo", "no names here", seed_entity_ids=[FIND_BY_ID], limit=10)
    ids = [r.entity_id for r in outcome.results]

    assert FIND_BY_ID not in ids
    assert GET_USER in ids  # its caller
    assert outcome.seeds == [FIND_BY_ID]


def test_related_external_seeds_are_returned_with_the_relationship(searcher):
    outcome = searcher.search(
        "demo", "no names here", seed_entity_ids=[GET_USER, ENDPOINT], limit=10
    )
    by_id = {r.entity_id: r for r in outcome.results}

    # Each seed is evidence for the other: the graph confirms the call between them.
    assert by_id[ENDPOINT].graph_evidence[0].relation == "caller"
    assert by_id[GET_USER].graph_evidence[0].relation == "callee"


def test_entities_without_indexed_chunks_are_reported_not_returned(built):
    graph, chunks = built
    searcher = GraphSearcher(InMemoryGraphContextSource(graph), ChunkList(chunks, drop=[ENDPOINT]))

    outcome = searcher.search("demo", "get_user", limit=10)

    assert ENDPOINT in outcome.entities_without_chunks
    assert ENDPOINT not in [r.entity_id for r in outcome.results]


def test_another_repository_sees_nothing(searcher):
    assert searcher.search("other-repo", "get_user", limit=5).results == []
    assert searcher.get_related_entities("other-repo", GET_USER) == []


def test_no_seeds_returns_empty(searcher):
    outcome = searcher.search("demo", "nothing matches here", limit=5)

    assert outcome.results == []
    assert outcome.seeds == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Who calls authorize_repository_path?", "incoming"),
        ("callers of get_user", "incoming"),
        ("where is get_user used", "incoming"),
        ("What does GraphService.build_graph call?", "outgoing"),
        ("dependencies of get_user", "outgoing"),
        ("500 error inside get_user()", None),
        ("who calls get_user and what does it call", None),
    ],
)
def test_call_direction_preference(query, expected):
    assert call_direction_preference(query) == expected


def test_requested_call_direction_ranks_first(searcher):
    callers_first = related(searcher, GET_USER, max_depth=1, prefer_call_direction="incoming")
    callees_first = related(searcher, GET_USER, max_depth=1, prefer_call_direction="outgoing")

    assert callers_first[ENDPOINT].evidence.score > callees_first[ENDPOINT].evidence.score
    assert callees_first[FIND_BY_ID].evidence.score > callers_first[FIND_BY_ID].evidence.score
