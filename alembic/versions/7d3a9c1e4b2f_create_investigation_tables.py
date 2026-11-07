"""create debugging investigation tables

Revision ID: 7d3a9c1e4b2f
Revises: 5c1f0e2a9b7d
Create Date: 2026-11-07 18:00:00

``investigations`` holds one row per investigation, inserted before the agent
loop starts and updated once at the end. ``investigation_events`` is the
append-only timeline; ``sequence`` orders it, because timestamps can tie.
Deleting an investigation deletes its events.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7d3a9c1e4b2f"
down_revision: str | Sequence[str] | None = "5c1f0e2a9b7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "investigations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("repository_id", sa.String(), nullable=False),
        sa.Column("bug_report", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_investigations_repository_id", "investigations", ["repository_id"])
    op.create_index("ix_investigations_status", "investigations", ["status"])

    op.create_table(
        "investigation_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "investigation_id",
            sa.UUID(),
            sa.ForeignKey("investigations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("investigation_id", "sequence", name="uq_investigation_events_sequence"),
    )
    op.create_index(
        "ix_investigation_events_investigation_id", "investigation_events", ["investigation_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_investigation_events_investigation_id", table_name="investigation_events")
    op.drop_table("investigation_events")
    op.drop_index("ix_investigations_status", table_name="investigations")
    op.drop_index("ix_investigations_repository_id", table_name="investigations")
    op.drop_table("investigations")
