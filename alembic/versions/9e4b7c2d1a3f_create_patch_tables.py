"""create patch proposal and verification tables

Revision ID: 9e4b7c2d1a3f
Revises: 7d3a9c1e4b2f
Create Date: 2026-11-08 12:00:00

``patch_proposals`` keeps every generation attempt, failed ones included: they
are evaluation data. Rows are grouped by ``execution_id`` (one generate-and-
verify request) and ordered by ``attempt_number``. ``patch_verifications``
holds one verification per attempt. Neither cascades on delete: removing an
investigation must not silently erase its patch history.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "9e4b7c2d1a3f"
down_revision: str | Sequence[str] | None = "7d3a9c1e4b2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "patch_proposals",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("investigation_id", sa.UUID(), sa.ForeignKey("investigations.id"), nullable=False),
        sa.Column("execution_id", sa.UUID(), nullable=False),
        sa.Column("repository_id", sa.String(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("unified_diff", sa.Text(), nullable=False),
        sa.Column("diff_sha256", sa.String(), nullable=True),
        sa.Column("affected_files", JSONB, nullable=False),
        sa.Column("assumptions", JSONB, nullable=False),
        sa.Column("risks", JSONB, nullable=False),
        sa.Column("validation_result", JSONB, nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("status_history", JSONB, nullable=False),
        sa.Column("provenance", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("execution_id", "attempt_number", name="uq_patch_proposals_execution_attempt"),
    )
    op.create_index("ix_patch_proposals_investigation", "patch_proposals", ["investigation_id", "created_at"])
    op.create_index("ix_patch_proposals_execution_id", "patch_proposals", ["execution_id"])
    op.create_index("ix_patch_proposals_repository_id", "patch_proposals", ["repository_id"])
    op.create_index("ix_patch_proposals_status", "patch_proposals", ["status"])

    op.create_table(
        "patch_verifications",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("patch_id", sa.UUID(), sa.ForeignKey("patch_proposals.id"), nullable=False, unique=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("checks", JSONB, nullable=False),
        sa.Column("before_results", JSONB, nullable=False),
        sa.Column("after_results", JSONB, nullable=False),
        sa.Column("comparison", JSONB, nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("failure_reasons", JSONB, nullable=False),
        sa.Column("environment", JSONB, nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_patch_verifications_status", "patch_verifications", ["status"])


def downgrade() -> None:
    op.drop_index("ix_patch_verifications_status", table_name="patch_verifications")
    op.drop_table("patch_verifications")
    op.drop_index("ix_patch_proposals_status", table_name="patch_proposals")
    op.drop_index("ix_patch_proposals_repository_id", table_name="patch_proposals")
    op.drop_index("ix_patch_proposals_execution_id", table_name="patch_proposals")
    op.drop_index("ix_patch_proposals_investigation", table_name="patch_proposals")
    op.drop_table("patch_proposals")
