"""create evaluation baselines

Revision ID: c5d2b9e7f1a3
Revises: b3f1a8c2d9e4
Create Date: 2026-11-08 20:00:00

A baseline is a pointer plus a copy of the run's configuration and metrics at designation time.
Rows are append-only (the existing ``evaluation_reject_change`` trigger).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c5d2b9e7f1a3"
down_revision: str | Sequence[str] | None = "b3f1a8c2d9e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "evaluation_baselines",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("evaluation_run_id", sa.UUID(), sa.ForeignKey("evaluation_runs.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("benchmark_version", sa.String(100), nullable=False),
        sa.Column("configuration_snapshot", JSONB, nullable=False),
        sa.Column("baseline_metrics", JSONB, nullable=False),
        sa.Column("note", sa.String(500), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_evaluation_baselines_created_at", "evaluation_baselines", ["created_at"])
    op.create_index("ix_evaluation_baselines_benchmark_version", "evaluation_baselines", ["benchmark_version"])
    op.execute("""CREATE TRIGGER evaluation_baselines_append_only BEFORE UPDATE OR DELETE
                  ON evaluation_baselines FOR EACH ROW EXECUTE FUNCTION evaluation_reject_change();""")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS evaluation_baselines_append_only ON evaluation_baselines")
    op.drop_table("evaluation_baselines")
