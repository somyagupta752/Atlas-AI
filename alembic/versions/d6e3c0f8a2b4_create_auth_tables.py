"""create users, repositories, memberships, session tokens; add evaluation owners

Revision ID: d6e3c0f8a2b4
Revises: c5d2b9e7f1a3
Create Date: 2026-11-08 22:00:00

``repositories`` is the registry of the existing string ``repository_id`` (stored as ``slug``). Existing
repositories (indexed or graphed before this migration) have no row and therefore no owner: an operator
must register them (``python -m app.auth.admin claim <slug> <email>``). Until then only admins can reach them.
``owner_id`` on the evaluation tables is nullable: NULL means a system record such as a historical import.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d6e3c0f8a2b4"
down_revision: str | Sequence[str] | None = "c5d2b9e7f1a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default="false", nullable=False),
        _created(),
    )
    op.create_index("uq_users_email_lower", "users", [sa.text("lower(email)")], unique=True)
    op.create_table(
        "repositories",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("created_by_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        _created(),
    )
    op.create_table(
        "repository_memberships",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("repository_id", sa.UUID(), sa.ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        _created(),
        sa.UniqueConstraint("repository_id", "user_id", name="uq_repository_memberships_repo_user"),
        sa.CheckConstraint("role IN ('owner', 'editor', 'viewer')", name="ck_repository_memberships_role"),
    )
    op.create_index("ix_repository_memberships_user_id", "repository_memberships", ["user_id"])
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", sa.UUID(), nullable=True),
        _created(),
    )
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        _created(),
    )
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])
    for table in ("evaluation_configurations", "evaluation_runs", "evaluation_baselines"):
        op.add_column(table, sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"),
                                       nullable=True))
        op.create_index(f"ix_{table}_owner_id", table, ["owner_id"])


def downgrade() -> None:
    for table in ("evaluation_baselines", "evaluation_runs", "evaluation_configurations"):
        op.drop_index(f"ix_{table}_owner_id", table_name=table)
        op.drop_column(table, "owner_id")
    op.drop_table("password_reset_tokens")
    op.drop_table("refresh_tokens")
    op.drop_table("repository_memberships")
    op.drop_table("repositories")
    op.drop_table("users")
