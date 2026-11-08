import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.dialects.postgresql import base as postgresql_base
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from app.database import Base


class BenchmarkExecution(Base):
    """One recorded run of a benchmark. Definitions stay in the Python registry."""

    __tablename__ = "benchmark_executions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    case_id: Mapped[str] = mapped_column(String, index=True)
    outcome: Mapped[str] = mapped_column(String)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout: Mapped[str] = mapped_column(Text)
    stderr: Mapped[str] = mapped_column(Text)
    duration_seconds: Mapped[float] = mapped_column(Float)
    timed_out: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# --- code retrieval ---------------------------------------------------------

# The embedding column's dimension is part of the schema. Changing the embedding
# model to one with another dimension needs a new migration, never a silent mix.
EMBEDDING_DIMENSIONS = 1024

# Generated full-text vector. Symbol terms (names) rank above content terms.
SEARCH_VECTOR_EXPRESSION = (
    "setweight(to_tsvector('simple', coalesce(symbol_terms, '')), 'A') || "
    "setweight(to_tsvector('simple', coalesce(content_terms, '')), 'B')"
)


class Vector(UserDefinedType):
    """pgvector ``VECTOR(n)``. Values are written and read as text literals in SQL."""

    cache_ok = True

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self.dimensions = dimensions

    def get_col_spec(self, **kw: object) -> str:
        return f"VECTOR({self.dimensions})"


# Let schema reflection (and Alembic's drift check) recognise the column type.
postgresql_base.ischema_names["vector"] = Vector


class RepositoryIndexRecord(Base):
    """Indexing state of one repository. ``index_version`` 0 means never indexed."""

    __tablename__ = "repository_indexes"

    repository_id: Mapped[str] = mapped_column(String, primary_key=True)
    root_path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String)  # indexing | ready | failed
    index_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    source_revision: Mapped[str | None] = mapped_column(String, nullable=True)
    content_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String, nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    file_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed_file_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CodeChunkRecord(Base):
    """One searchable chunk of source code, with its embedding and full-text vector."""

    __tablename__ = "code_chunks"
    __table_args__ = (
        Index("ix_code_chunks_repository_id", "repository_id"),
        Index("ix_code_chunks_repository_file", "repository_id", "file_path"),
        Index("ix_code_chunks_repository_entity", "repository_id", "entity_id"),
        Index("ix_code_chunks_repository_embedding_hash", "repository_id", "embedding_hash"),
        Index(
            "ix_code_chunks_repository_symbol",
            "repository_id",
            text("lower(symbol_name)"),
        ),
        Index("ix_code_chunks_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_code_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    repository_id: Mapped[str] = mapped_column(String)
    file_path: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_type: Mapped[str] = mapped_column(String)
    qualified_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    symbol_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    dotted_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    chunk_metadata: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    content_hash: Mapped[str] = mapped_column(String)
    embedding_hash: Mapped[str] = mapped_column(String)
    record_hash: Mapped[str] = mapped_column(String)
    embedding_model: Mapped[str] = mapped_column(String)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS), nullable=True
    )
    symbol_terms: Mapped[str] = mapped_column(Text, default="")
    content_terms: Mapped[str] = mapped_column(Text, default="")
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR, Computed(SEARCH_VECTOR_EXPRESSION, persisted=True)
    )
    index_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# --- debugging agent ----------------------------------------------------------


class InvestigationRecord(Base):
    """One debugging investigation. ``result`` holds the full RootCauseAnalysis."""

    __tablename__ = "investigations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    repository_id: Mapped[str] = mapped_column(String, index=True)
    bug_report: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InvestigationEventRecord(Base):
    """One step of an investigation's timeline. ``sequence`` orders events within it."""

    __tablename__ = "investigation_events"
    __table_args__ = (
        UniqueConstraint("investigation_id", "sequence", name="uq_investigation_events_sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# --- patch generation and verification ------------------------------------------


class PatchProposalRecord(Base):
    """One patch attempt. Every attempt is kept, including failed ones.

    ``status`` follows the lifecycle in ``app.patching.models``; ``status_history``
    records each transition with its time. ``provenance`` names the model, prompt
    version, usage and the source snapshot the patch was generated against.
    """

    __tablename__ = "patch_proposals"
    __table_args__ = (
        UniqueConstraint("execution_id", "attempt_number", name="uq_patch_proposals_execution_attempt"),
        Index("ix_patch_proposals_investigation", "investigation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investigations.id")
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    repository_id: Mapped[str] = mapped_column(String, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    unified_diff: Mapped[str] = mapped_column(Text)
    diff_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    affected_files: Mapped[list] = mapped_column(JSONB)
    assumptions: Mapped[list] = mapped_column(JSONB)
    risks: Mapped[list] = mapped_column(JSONB)
    validation_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String, index=True)
    status_history: Mapped[list] = mapped_column(JSONB)
    provenance: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PatchVerificationRecord(Base):
    """The verification of one patch attempt: every check and both sides of the comparison."""

    __tablename__ = "patch_verifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    patch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("patch_proposals.id"), unique=True
    )
    status: Mapped[str] = mapped_column(String, index=True)
    checks: Mapped[list] = mapped_column(JSONB)
    before_results: Mapped[dict] = mapped_column(JSONB)
    after_results: Mapped[dict] = mapped_column(JSONB)
    comparison: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    summary: Mapped[str] = mapped_column(Text)
    failure_reasons: Mapped[list] = mapped_column(JSONB)
    environment: Mapped[dict] = mapped_column(JSONB)
    duration_seconds: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# Registers the evaluation tables on Base.metadata (Alembic and the schema-match test read it).
from app.auth import models as _auth_models  # noqa: F401
from app.evaluation import models as _evaluation_models  # noqa: F401
