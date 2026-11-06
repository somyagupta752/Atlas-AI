import pytest

from app.code_intelligence import analyze_repository
from app.knowledge_graph.builder import build_code_graph
from app.knowledge_graph.repository import GraphStoreError
from app.retrieval.embeddings import EmbeddingProviderError, HashingEmbeddingProvider
from app.retrieval.graph_search import GraphSearcher, InMemoryGraphContextSource
from app.retrieval.keyword_search import KeywordSearcher
from app.retrieval.models import EVIDENCE_NOTE, RetrievalSource
from app.retrieval.service import (
    InvalidRetrievalRequestError,
    RepositoryNotIndexedError,
    RetrievalService,
)
from app.retrieval.vector_search import VectorSearcher

K, V, G = RetrievalSource.KEYWORD, RetrievalSource.VECTOR, RetrievalSource.GRAPH

FILES = {
    "app/__init__.py": "",
    "app/repository.py": """
        class UserRepository:
            def find_by_id(self, user_id):
                return self.session.get(user_id)
        """,
    "app/service.py": """
        from app.repository import UserRepository

        class UserService:
            def __init__(self):
                self.repository = UserRepository()

            def get_user(self, user_id):
                user = self.repository.find_by_id(user_id)
                return user.profile
        """,
    "app/api.py": """
        from app.service import UserService

        def get_profile_endpoint(user_id):
            service = UserService()
            return service.get_user(user_id)
        """,
    "app/billing.py": """
        def compute_invoice_total(lines):
            return sum(line.amount for line in lines)
        """,
}


class BrokenGraph:
    def find_entities_by_name(self, *args):
        raise GraphStoreError("Neo4j is unavailable (ServiceUnavailable)")

    expand_neighborhood = unresolved_for_entities = find_entities_by_name


class FailingProvider(HashingEmbeddingProvider):
    def embed_query(self, text):
        raise EmbeddingProviderError("Embedding provider returned HTTP 503")


@pytest.fixture
def indexed(index_repo, write_repo):
    _, root = index_repo(FILES, "svc-a")
    return build_code_graph(analyze_repository(root), "svc-a")


def make_service(db_session, chunk_repo, graph=None, provider=None, graph_source=None):
    provider = provider or HashingEmbeddingProvider()
    source = graph_source or (InMemoryGraphContextSource(graph) if graph is not None else None)
    return RetrievalService(
        chunk_repo,
        KeywordSearcher(db_session),
        VectorSearcher(db_session, provider),
        GraphSearcher(source, chunk_repo) if source is not None else None,
    )


def test_bug_report_retrieves_the_function_its_callee_and_its_caller(db_session, chunk_repo, indexed):
    service = make_service(db_session, chunk_repo, indexed)
    query = "Users get a 500 error when fetching their profile. The error occurs inside get_user()."

    response = service.retrieve_code("svc-a", query, limit=10)
    names = [r.qualified_name for r in response.results]
    # A method inside a returned class chunk is folded into it, with its provenance.
    folded = {m.qualified_name: m for r in response.results for m in r.merged}
    names += list(folded)

    assert names[0] == "UserService.get_user"
    top = response.results[0]
    assert top.retrieval_sources == [K, V, G]  # found by all three strategies
    assert {r.source for r in top.strategy_ranks} == {K, V, G}
    assert "UserRepository.find_by_id" in names  # what it calls
    if "UserRepository.find_by_id" in folded:
        assert "callee of UserService.get_user" in folded["UserRepository.find_by_id"].reason
    assert "get_profile_endpoint" in names  # who calls it
    assert response.total_results == len(response.results) <= 10
    assert response.note == EVIDENCE_NOTE
    assert response.index_version == 1
    assert [s.status for s in response.strategies] == ["ok", "ok", "ok"]


def test_graph_evidence_explains_why_neighbors_were_retrieved(db_session, chunk_repo, indexed):
    response = make_service(db_session, chunk_repo, indexed).retrieve_code(
        "svc-a", "error inside get_user()", limit=10
    )
    caller = next(r for r in response.results if r.qualified_name == "get_profile_endpoint")

    assert G in caller.retrieval_sources
    relations = {e.relation for e in caller.graph_relationships}
    assert "caller" in relations
    assert "caller of UserService.get_user" in caller.reason
    for result in response.results:
        assert result.content
        assert 1 <= result.start_line <= result.end_line
        assert result.file_path.startswith("app/")


def test_keyword_only_match_is_returned_without_vector(db_session, chunk_repo, indexed):
    response = make_service(db_session, chunk_repo, indexed).retrieve_code(
        "svc-a", "compute_invoice_total", limit=3, strategies={K}
    )

    assert response.results[0].qualified_name == "compute_invoice_total"
    assert response.results[0].retrieval_sources == [K]
    assert [s.status for s in response.strategies] == ["ok", "skipped", "skipped"]


def test_vector_only_match_is_returned_without_keyword(db_session, chunk_repo, indexed):
    response = make_service(db_session, chunk_repo, indexed).retrieve_code(
        "svc-a", "invoice totals", limit=3, strategies={V}
    )

    assert response.results[0].qualified_name == "compute_invoice_total"
    assert response.results[0].retrieval_sources == [V]


def test_each_strategy_finds_things_the_others_do_not(db_session, chunk_repo, indexed):
    service = make_service(db_session, chunk_repo, indexed)
    query = "get_user"

    keyword = {r.chunk_id for r in service.retrieve_code("svc-a", query, 20, {K}).results}
    graph = {r.chunk_id for r in service.retrieve_code("svc-a", query, 20, {G}).results}
    hybrid = {r.chunk_id for r in service.retrieve_code("svc-a", query, 20).results}

    assert graph - keyword  # the graph adds callers/callees that do not mention get_user
    assert hybrid >= (keyword | graph) - {c for c in keyword | graph if c not in hybrid}


def test_graph_outage_degrades_to_keyword_and_vector(db_session, chunk_repo, indexed):
    service = make_service(db_session, chunk_repo, graph_source=BrokenGraph())

    response = service.retrieve_code("svc-a", "get_user", limit=5)

    assert response.results
    graph_report = next(s for s in response.strategies if s.source == G)
    assert graph_report.status == "unavailable"
    assert "Neo4j" not in (graph_report.detail or "")


def test_embedding_outage_degrades_to_keyword_and_graph(db_session, chunk_repo, indexed):
    service = make_service(db_session, chunk_repo, indexed, provider=FailingProvider())

    response = service.retrieve_code("svc-a", "get_user", limit=5)

    assert response.results[0].qualified_name == "UserService.get_user"
    vector_report = next(s for s in response.strategies if s.source == V)
    assert vector_report.status == "error"


def test_missing_vector_and_graph_components_are_reported(db_session, chunk_repo, indexed):
    service = RetrievalService(chunk_repo, KeywordSearcher(db_session))

    response = service.retrieve_code("svc-a", "get_user", limit=5)

    assert [s.status for s in response.strategies] == ["ok", "unavailable", "unavailable"]
    assert response.results


def test_results_never_contain_another_repositorys_chunks(db_session, chunk_repo, indexed, index_repo):
    index_repo({"app/service.py": "def get_user(user_id):\n    return None\n"}, "svc-b")
    service = make_service(db_session, chunk_repo, indexed)

    for repository_id in ("svc-a", "svc-b"):
        response = service.retrieve_code(repository_id, "get_user user profile", limit=20)
        assert {r.repository_id for r in response.results} == {repository_id}
        assert all(repository_id in r.chunk_id for r in response.results)


def test_repository_without_an_index_is_rejected(db_session, chunk_repo):
    with pytest.raises(RepositoryNotIndexedError):
        make_service(db_session, chunk_repo).retrieve_code("never-indexed", "get_user")


def test_repository_with_no_indexed_content_returns_no_results(db_session, chunk_repo, index_repo):
    index_repo({"README.md": "docs only"}, "svc-empty")

    response = make_service(db_session, chunk_repo).retrieve_code("svc-empty", "get_user")

    assert response.results == []
    assert response.total_results == 0


@pytest.mark.parametrize(
    ("repository_id", "query", "limit"),
    [("svc-a", "", 5), ("svc-a", "   ", 5), ("svc-a", "q", 0), ("svc-a", "q", 51), ("Bad ID", "q", 5)],
)
def test_invalid_requests(db_session, chunk_repo, indexed, repository_id, query, limit):
    with pytest.raises(InvalidRetrievalRequestError):
        make_service(db_session, chunk_repo, indexed).retrieve_code(repository_id, query, limit)


def test_retrieval_is_deterministic(db_session, chunk_repo, indexed):
    service = make_service(db_session, chunk_repo, indexed)
    first = service.retrieve_code("svc-a", "where is the user profile loaded", limit=8)
    second = service.retrieve_code("svc-a", "where is the user profile loaded", limit=8)

    assert first.model_dump() == second.model_dump()
