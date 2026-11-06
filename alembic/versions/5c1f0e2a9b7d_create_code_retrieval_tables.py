"""create code retrieval tables (pgvector, full-text search)

Revision ID: 5c1f0e2a9b7d
Revises: acaa63060bc4
Create Date: 2026-11-07 12:00:00

The embedding column is VECTOR(1024). The dimension belongs to the index
configuration: a model with another dimension needs a new migration.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "5c1f0e2a9b7d"
down_revision: str | Sequence[str] | None = "acaa63060bc4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSIONS = 1024
SEARCH_VECTOR_EXPRESSION = (
    "setweight(to_tsvector('simple', coalesce(symbol_terms, '')), 'A') || "
    "setweight(to_tsvector('simple', coalesce(content_terms, '')), 'B')"
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "repository_indexes",
        sa.Column("repository_id", sa.String(), primary_key=True),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("index_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("source_revision", sa.String(), nullable=True),
        sa.Column("content_fingerprint", sa.String(), nullable=True),
        sa.Column("embedding_model", sa.String(), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("file_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_file_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_indexed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "code_chunks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("repository_id", sa.String(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("qualified_name", sa.Text(), nullable=True),
        sa.Column("symbol_name", sa.Text(), nullable=True),
        sa.Column("dotted_name", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("embedding_hash", sa.String(), nullable=False),
        sa.Column("record_hash", sa.String(), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=True),  # retyped to VECTOR below
        sa.Column("symbol_terms", sa.Text(), nullable=False),
        sa.Column("content_terms", sa.Text(), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(SEARCH_VECTOR_EXPRESSION, persisted=True),
            nullable=False,
        ),
        sa.Column("index_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("start_line >= 1 AND end_line >= start_line", name="ck_code_chunks_lines"),
    )
    # Alembic has no VECTOR type; set the real column type explicitly.
    op.execute(
        f"ALTER TABLE code_chunks ALTER COLUMN embedding TYPE VECTOR({EMBEDDING_DIMENSIONS}) "
        f"USING embedding::vector({EMBEDDING_DIMENSIONS})"
    )

    op.create_index("ix_code_chunks_repository_id", "code_chunks", ["repository_id"])
    op.create_index("ix_code_chunks_repository_file", "code_chunks", ["repository_id", "file_path"])
    op.create_index(
        "ix_code_chunks_repository_entity", "code_chunks", ["repository_id", "entity_id"]
    )
    op.create_index(
        "ix_code_chunks_repository_embedding_hash",
        "code_chunks",
        ["repository_id", "embedding_hash"],
    )
    op.create_index(
        "ix_code_chunks_repository_symbol",
        "code_chunks",
        ["repository_id", sa.text("lower(symbol_name)")],
    )
    op.create_index(
        "ix_code_chunks_search_vector", "code_chunks", ["search_vector"], postgresql_using="gin"
    )
    op.create_index(
        "ix_code_chunks_embedding_hnsw",
        "code_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_table("code_chunks")
    op.drop_table("repository_indexes")
    # The vector extension is left installed: other objects may depend on it.
