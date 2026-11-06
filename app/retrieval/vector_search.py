"""Semantic retrieval with pgvector cosine distance.

pgvector's ``<=>`` returns cosine *distance*, in [0, 2]: 0 means the same
direction, 1 means orthogonal, 2 means opposite. Lower is more similar. This
module reports cosine *similarity* instead, so higher is always better::

    score = similarity = 1 - cosine_distance        (range [-1, 1])

Only vectors with the query provider's ``model_id`` are compared. Vectors from
another model live in a different space and would give meaningless distances.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.retrieval.embeddings import EmbeddingProvider, validate_vectors
from app.retrieval.models import ChunkType, RetrievalResult, RetrievalSource
from app.retrieval.repository import CHUNK_COLUMNS, format_vector

_SEARCH_SQL = f"""
SELECT {CHUNK_COLUMNS}, embedding <=> CAST(:query_embedding AS vector) AS distance
FROM code_chunks
WHERE repository_id = :repository_id
  AND embedding_model = :embedding_model
  AND embedding IS NOT NULL
ORDER BY embedding <=> CAST(:query_embedding AS vector), id
LIMIT :limit
"""


def distance_to_similarity(distance: float) -> float:
    return 1.0 - distance


class VectorSearcher:
    def __init__(self, session: Session, provider: EmbeddingProvider) -> None:
        self._session = session
        self._provider = provider

    def search(self, repository_id: str, query: str, limit: int = 20) -> list[RetrievalResult]:
        """Closest chunks in ``repository_id``. No indexed embeddings: no results."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if not query or not query.strip():
            return []
        embedding = validate_vectors(
            [self._provider.embed_query(query)], 1, self._provider.dimensions
        )[0]
        # With an HNSW index and a WHERE filter, keep scanning until `limit` rows
        # pass the filter (pgvector >= 0.8). Scoped to this transaction only.
        self._session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        rows = self._session.execute(
            text(_SEARCH_SQL),
            {
                "repository_id": repository_id,
                "embedding_model": self._provider.model_id,
                "query_embedding": format_vector(embedding),
                "limit": limit,
            },
        )
        results = []
        for rank, row in enumerate(rows, start=1):
            similarity = distance_to_similarity(float(row.distance))
            results.append(
                RetrievalResult(
                    chunk_id=row.id,
                    repository_id=row.repository_id,
                    file_path=row.file_path,
                    content=row.content,
                    start_line=row.start_line,
                    end_line=row.end_line,
                    retrieval_source=RetrievalSource.VECTOR,
                    score=similarity,
                    rank=rank,
                    qualified_name=row.qualified_name,
                    entity_id=row.entity_id,
                    entity_type=ChunkType(row.entity_type),
                    match_reasons=[f"semantic similarity {similarity:.3f}"],
                )
            )
        return results
