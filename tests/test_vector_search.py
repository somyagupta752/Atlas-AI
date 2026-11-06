import math

import pytest

from app.models import EMBEDDING_DIMENSIONS
from app.retrieval.embeddings import FakeEmbeddingProvider
from app.retrieval.models import ChunkType, CodeChunk, RetrievalSource
from app.retrieval.repository import StoredChunk
from app.retrieval.vector_search import VectorSearcher, distance_to_similarity

DIM = EMBEDDING_DIMENSIONS


def axis(*weights: float) -> list[float]:
    """A vector with the given leading components, the rest zero."""
    return [*weights, *([0.0] * (DIM - len(weights)))]


def chunk(repository_id: str, name: str) -> CodeChunk:
    return CodeChunk(
        id=f"chunk:function:{repository_id}:app/m.py::{name}",
        repository_id=repository_id,
        file_path="app/m.py",
        entity_id=f"function:{repository_id}:app/m.py::{name}",
        entity_type=ChunkType.FUNCTION,
        qualified_name=name,
        content=f"def {name}():\n    pass",
        start_line=1,
        end_line=2,
    )


def store(chunk_repo, repository_id: str, vectors: dict[str, list[float]], model: str) -> None:
    chunk_repo.begin_attempt(repository_id, f"/tmp/{repository_id}")
    chunk_repo.replace_index(
        repository_id,
        f"/tmp/{repository_id}",
        [StoredChunk(chunk(repository_id, name), v, f"hash-{name}", model) for name, v in vectors.items()],
        file_count=1,
        failed_file_count=0,
        source_revision=None,
        content_fingerprint="x",
        embedding_model=model,
        embedding_dimensions=DIM,
    )


@pytest.fixture
def provider():
    return FakeEmbeddingProvider(vectors={"where is the db session": axis(1.0, 0.1)})


def test_results_are_ordered_by_cosine_similarity(chunk_repo, db_session, provider):
    store(
        chunk_repo,
        "vec-a",
        {
            "get_db": axis(1.0, 0.0),
            "open_session": axis(0.8, 0.6),
            "render_chart": axis(0.0, 0.0, 1.0),
        },
        provider.model_id,
    )

    results = VectorSearcher(db_session, provider).search("vec-a", "where is the db session", limit=3)

    assert [r.qualified_name for r in results] == ["get_db", "open_session", "render_chart"]
    assert [r.rank for r in results] == [1, 2, 3]
    assert all(r.retrieval_source == RetrievalSource.VECTOR for r in results)
    expected = (1.0 * 1.0 + 0.1 * 0.0) / math.sqrt(1.0 + 0.01)
    assert results[0].score == pytest.approx(expected, abs=1e-5)
    assert results[2].score == pytest.approx(0.0, abs=1e-5)
    assert results[0].score > results[1].score > results[2].score


def test_similarity_is_one_minus_distance():
    assert distance_to_similarity(0.0) == 1.0
    assert distance_to_similarity(1.0) == 0.0
    assert distance_to_similarity(2.0) == -1.0


def test_limit_is_applied(chunk_repo, db_session, provider):
    store(chunk_repo, "vec-a", {f"f{i}": axis(1.0, i / 10) for i in range(5)}, provider.model_id)

    assert len(VectorSearcher(db_session, provider).search("vec-a", "anything", limit=2)) == 2


def test_results_never_come_from_another_repository(chunk_repo, db_session, provider):
    store(chunk_repo, "vec-a", {"get_db": axis(0.0, 1.0)}, provider.model_id)
    store(chunk_repo, "vec-b", {"get_db": axis(1.0, 0.1)}, provider.model_id)  # the better match

    results = VectorSearcher(db_session, provider).search("vec-a", "where is the db session", limit=10)

    assert [r.repository_id for r in results] == ["vec-a"]
    assert results[0].chunk_id == "chunk:function:vec-a:app/m.py::get_db"


def test_vectors_from_another_model_are_never_compared(chunk_repo, db_session, provider):
    store(chunk_repo, "vec-a", {"get_db": axis(1.0)}, "other:model:1024")

    assert VectorSearcher(db_session, provider).search("vec-a", "where is the db session", limit=5) == []


def test_repository_without_embeddings_returns_nothing(db_session, provider):
    assert VectorSearcher(db_session, provider).search("never-indexed", "anything", limit=5) == []


def test_empty_query_returns_nothing_without_calling_the_provider(db_session, provider):
    assert VectorSearcher(db_session, provider).search("vec-a", "  ", limit=5) == []
    assert provider.query_calls == []


def test_invalid_limit(db_session, provider):
    with pytest.raises(ValueError):
        VectorSearcher(db_session, provider).search("vec-a", "x", limit=0)
