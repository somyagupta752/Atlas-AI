"""PostgreSQL persistence for indexed chunks and repository index state.

Consistency rule: a repository's chunks are replaced in exactly one
transaction (``replace_index``). Readers see the previous index or the new
one, never a mixture. Everything that can fail slowly or externally, such as
parsing and embedding, happens before that transaction starts.

All SQL is parameterized. Vectors travel as pgvector text literals
(``'[0.1,0.2,...]'``) through a bound parameter and an explicit cast.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.retrieval.models import ChunkType, CodeChunk, sha256_text
from app.retrieval.text import LEXICAL_VERSION, lexical_terms

INDEX_STATUS_INDEXING = "indexing"
INDEX_STATUS_READY = "ready"
INDEX_STATUS_FAILED = "failed"

CHUNK_COLUMNS = (
    "id, repository_id, file_path, entity_id, entity_type, qualified_name, "
    "content, start_line, end_line, metadata"
)


class IndexState(BaseModel):
    repository_id: str
    root_path: str
    status: str
    index_version: int
    source_revision: str | None = None
    content_fingerprint: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    chunk_count: int = 0
    file_count: int = 0
    failed_file_count: int = 0
    last_error: str | None = None
    last_attempt_at: datetime | None = None
    last_indexed_at: datetime | None = None

    @property
    def is_searchable(self) -> bool:
        """True once one index has completed. A later failed attempt does not change it."""
        return self.index_version > 0


@dataclass(frozen=True)
class StoredChunk:
    chunk: CodeChunk
    embedding: list[float]
    embedding_hash: str
    embedding_model: str


@dataclass(frozen=True)
class ReplaceStats:
    index_version: int
    inserted: int
    updated: int
    unchanged: int
    deleted: int


def format_vector(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def parse_vector(value: str) -> list[float]:
    return [float(item) for item in json.loads(value)]


def symbol_terms(chunk: CodeChunk) -> str:
    """Terms for the high-weight part of the search vector: names, not bodies."""
    names = [chunk.qualified_name or "", chunk.dotted_name or "", chunk.file_path]
    return " ".join(lexical_terms(" ".join(names)))


def content_terms(chunk: CodeChunk) -> str:
    extra = " ".join(str(d) for d in chunk.metadata.get("decorators", []))
    return " ".join(lexical_terms(f"{chunk.content}\n{extra}"))


def record_hash(stored: StoredChunk) -> str:
    """Fingerprint of every stored field. Equal hash means the row needs no write."""
    chunk = stored.chunk
    payload = json.dumps(
        {
            "chunk": chunk.model_dump(mode="json"),
            "embedding_hash": stored.embedding_hash,
            "embedding_model": stored.embedding_model,
            "lexical_version": LEXICAL_VERSION,
        },
        sort_keys=True,
    )
    return sha256_text(payload)


def row_to_chunk(row: Any) -> CodeChunk:
    return CodeChunk(
        id=row.id,
        repository_id=row.repository_id,
        file_path=row.file_path,
        entity_id=row.entity_id,
        entity_type=ChunkType(row.entity_type),
        qualified_name=row.qualified_name,
        content=row.content,
        start_line=row.start_line,
        end_line=row.end_line,
        metadata=row.metadata or {},
    )


_UPSERT_CHUNK = text(
    """
    INSERT INTO code_chunks (
        id, repository_id, file_path, entity_id, entity_type, qualified_name,
        symbol_name, dotted_name, content, start_line, end_line, metadata,
        content_hash, embedding_hash, record_hash, embedding_model, embedding,
        symbol_terms, content_terms, index_version
    ) VALUES (
        :id, :repository_id, :file_path, :entity_id, :entity_type, :qualified_name,
        :symbol_name, :dotted_name, :content, :start_line, :end_line, CAST(:metadata AS JSONB),
        :content_hash, :embedding_hash, :record_hash, :embedding_model, CAST(:embedding AS vector),
        :symbol_terms, :content_terms, :index_version
    )
    ON CONFLICT (id) DO UPDATE SET
        file_path = EXCLUDED.file_path,
        entity_id = EXCLUDED.entity_id,
        entity_type = EXCLUDED.entity_type,
        qualified_name = EXCLUDED.qualified_name,
        symbol_name = EXCLUDED.symbol_name,
        dotted_name = EXCLUDED.dotted_name,
        content = EXCLUDED.content,
        start_line = EXCLUDED.start_line,
        end_line = EXCLUDED.end_line,
        metadata = EXCLUDED.metadata,
        content_hash = EXCLUDED.content_hash,
        embedding_hash = EXCLUDED.embedding_hash,
        record_hash = EXCLUDED.record_hash,
        embedding_model = EXCLUDED.embedding_model,
        embedding = EXCLUDED.embedding,
        symbol_terms = EXCLUDED.symbol_terms,
        content_terms = EXCLUDED.content_terms,
        index_version = EXCLUDED.index_version,
        updated_at = now()
    WHERE code_chunks.repository_id = EXCLUDED.repository_id
    """
)


class ChunkRepository:
    """All SQL for ``code_chunks`` and ``repository_indexes``. Every query is repository-scoped."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- index state -----------------------------------------------------

    def get_index_state(self, repository_id: str) -> IndexState | None:
        row = self._session.execute(
            text("SELECT * FROM repository_indexes WHERE repository_id = :repository_id"),
            {"repository_id": repository_id},
        ).mappings().first()
        return IndexState.model_validate(dict(row)) if row else None

    def begin_attempt(self, repository_id: str, root_path: str) -> None:
        """Record that indexing started. The previous index stays searchable meanwhile."""
        self._commit(
            text(
                """
                INSERT INTO repository_indexes (repository_id, root_path, status, last_attempt_at)
                VALUES (:repository_id, :root_path, :status, now())
                ON CONFLICT (repository_id) DO UPDATE
                SET status = EXCLUDED.status, last_attempt_at = now(), last_error = NULL
                """
            ),
            {"repository_id": repository_id, "root_path": root_path, "status": INDEX_STATUS_INDEXING},
        )

    def record_failure(self, repository_id: str, root_path: str, message: str) -> None:
        """Mark the attempt failed. Chunks and ``index_version`` are left untouched."""
        self._commit(
            text(
                """
                INSERT INTO repository_indexes (repository_id, root_path, status, last_error, last_attempt_at)
                VALUES (:repository_id, :root_path, :status, :message, now())
                ON CONFLICT (repository_id) DO UPDATE
                SET status = EXCLUDED.status, last_error = EXCLUDED.last_error
                """
            ),
            {
                "repository_id": repository_id,
                "root_path": root_path,
                "status": INDEX_STATUS_FAILED,
                "message": message[:2000],
            },
        )

    # --- embeddings reuse ----------------------------------------------

    def existing_embeddings(
        self, repository_id: str, embedding_hashes: Iterable[str]
    ) -> dict[str, list[float]]:
        """Stored vectors by ``embedding_hash``. The hash includes the model ID."""
        hashes = sorted(set(embedding_hashes))
        if not hashes:
            return {}
        rows = self._session.execute(
            text(
                """
                SELECT DISTINCT ON (embedding_hash) embedding_hash, embedding::text AS embedding
                FROM code_chunks
                WHERE repository_id = :repository_id
                  AND embedding_hash = ANY(CAST(:hashes AS TEXT[]))
                  AND embedding IS NOT NULL
                """
            ),
            {"repository_id": repository_id, "hashes": hashes},
        )
        return {row.embedding_hash: parse_vector(row.embedding) for row in rows}

    # --- atomic replacement --------------------------------------------

    def replace_index(
        self,
        repository_id: str,
        root_path: str,
        chunks: Sequence[StoredChunk],
        *,
        file_count: int,
        failed_file_count: int,
        source_revision: str | None,
        content_fingerprint: str,
        embedding_model: str,
        embedding_dimensions: int,
    ) -> ReplaceStats:
        """Replace the repository's chunks and publish a new index version, atomically.

        Unchanged rows (same ``record_hash``) are not rewritten. Chunks that are
        no longer produced are deleted in the same transaction.
        """
        if any(stored.chunk.repository_id != repository_id for stored in chunks):
            raise ValueError("every chunk must belong to the repository being replaced")
        params = {"repository_id": repository_id}
        try:
            # Serialize concurrent replacements of the same repository.
            self._session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:repository_id, 0))"), params
            )
            existing = {
                row.id: row.record_hash
                for row in self._session.execute(
                    text("SELECT id, record_hash FROM code_chunks WHERE repository_id = :repository_id"),
                    params,
                )
            }
            version = self._session.execute(
                text(
                    "SELECT coalesce(max(index_version), 0) + 1 FROM repository_indexes "
                    "WHERE repository_id = :repository_id"
                ),
                params,
            ).scalar_one()

            rows, inserted, updated, unchanged = [], 0, 0, 0
            for stored in chunks:
                row_hash = record_hash(stored)
                previous = existing.get(stored.chunk.id)
                if previous == row_hash:
                    unchanged += 1
                    continue
                if previous is None:
                    inserted += 1
                else:
                    updated += 1
                rows.append(self._row(stored, row_hash, version))
            if rows:
                self._session.execute(_UPSERT_CHUNK, rows)

            keep = [stored.chunk.id for stored in chunks]
            deleted = self._session.execute(
                text(
                    "DELETE FROM code_chunks WHERE repository_id = :repository_id "
                    "AND NOT (id = ANY(CAST(:keep AS TEXT[])))"
                ),
                {**params, "keep": keep},
            ).rowcount

            self._session.execute(
                text(
                    """
                    INSERT INTO repository_indexes (
                        repository_id, root_path, status, index_version, source_revision,
                        content_fingerprint, embedding_model, embedding_dimensions, chunk_count,
                        file_count, failed_file_count, last_error, last_attempt_at, last_indexed_at
                    ) VALUES (
                        :repository_id, :root_path, :status, :version, :source_revision,
                        :fingerprint, :model, :dimensions, :chunk_count,
                        :file_count, :failed_file_count, NULL, now(), now()
                    )
                    ON CONFLICT (repository_id) DO UPDATE SET
                        root_path = EXCLUDED.root_path,
                        status = EXCLUDED.status,
                        index_version = EXCLUDED.index_version,
                        source_revision = EXCLUDED.source_revision,
                        content_fingerprint = EXCLUDED.content_fingerprint,
                        embedding_model = EXCLUDED.embedding_model,
                        embedding_dimensions = EXCLUDED.embedding_dimensions,
                        chunk_count = EXCLUDED.chunk_count,
                        file_count = EXCLUDED.file_count,
                        failed_file_count = EXCLUDED.failed_file_count,
                        last_error = NULL,
                        last_indexed_at = now()
                    """
                ),
                {
                    **params,
                    "root_path": root_path,
                    "status": INDEX_STATUS_READY,
                    "version": version,
                    "source_revision": source_revision,
                    "fingerprint": content_fingerprint,
                    "model": embedding_model,
                    "dimensions": embedding_dimensions,
                    "chunk_count": len(chunks),
                    "file_count": file_count,
                    "failed_file_count": failed_file_count,
                },
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return ReplaceStats(version, inserted, updated, unchanged, deleted)

    def delete_index(self, repository_id: str) -> bool:
        params = {"repository_id": repository_id}
        try:
            self._session.execute(
                text("DELETE FROM code_chunks WHERE repository_id = :repository_id"), params
            )
            deleted = self._session.execute(
                text("DELETE FROM repository_indexes WHERE repository_id = :repository_id"), params
            ).rowcount
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return deleted > 0

    # --- reads -----------------------------------------------------------

    def chunks_for_entities(self, repository_id: str, entity_ids: Iterable[str]) -> list[CodeChunk]:
        ids = sorted(set(entity_ids))
        if not ids:
            return []
        rows = self._session.execute(
            text(
                f"SELECT {CHUNK_COLUMNS} FROM code_chunks "
                "WHERE repository_id = :repository_id AND entity_id = ANY(CAST(:ids AS TEXT[])) "
                "ORDER BY entity_id, start_line, id"
            ),
            {"repository_id": repository_id, "ids": ids},
        )
        return [row_to_chunk(row) for row in rows]

    def get_chunks(self, repository_id: str, chunk_ids: Iterable[str]) -> list[CodeChunk]:
        ids = sorted(set(chunk_ids))
        if not ids:
            return []
        rows = self._session.execute(
            text(
                f"SELECT {CHUNK_COLUMNS} FROM code_chunks "
                "WHERE repository_id = :repository_id AND id = ANY(CAST(:ids AS TEXT[])) ORDER BY id"
            ),
            {"repository_id": repository_id, "ids": ids},
        )
        return [row_to_chunk(row) for row in rows]

    def count_chunks(self, repository_id: str) -> int:
        return self._session.execute(
            text("SELECT count(*) FROM code_chunks WHERE repository_id = :repository_id"),
            {"repository_id": repository_id},
        ).scalar_one()

    # --- helpers ---------------------------------------------------------

    def _commit(self, statement: Any, params: dict[str, Any]) -> None:
        try:
            self._session.execute(statement, params)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    @staticmethod
    def _row(stored: StoredChunk, row_hash: str, version: int) -> dict[str, Any]:
        chunk = stored.chunk
        return {
            "id": chunk.id,
            "repository_id": chunk.repository_id,
            "file_path": chunk.file_path,
            "entity_id": chunk.entity_id,
            "entity_type": str(chunk.entity_type),
            "qualified_name": chunk.qualified_name,
            "symbol_name": chunk.symbol_name,
            "dotted_name": chunk.dotted_name,
            "content": chunk.content,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
            "metadata": json.dumps(chunk.metadata, sort_keys=True),
            "content_hash": chunk.content_hash,
            "embedding_hash": stored.embedding_hash,
            "record_hash": row_hash,
            "embedding_model": stored.embedding_model,
            "embedding": format_vector(stored.embedding),
            "symbol_terms": symbol_terms(chunk),
            "content_terms": content_terms(chunk),
            "index_version": version,
        }
