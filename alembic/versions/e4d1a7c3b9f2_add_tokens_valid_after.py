"""users.tokens_valid_after: end sessions immediately

Revision ID: e4d1a7c3b9f2
Revises: d6e3c0f8a2b4
Create Date: 2026-11-09 09:00:00

Access tokens are short-lived and stateless. This column lets "log out everywhere" and a password reset invalidate the
ones already issued, without keeping a list of them.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e4d1a7c3b9f2"
down_revision: str | Sequence[str] | None = "d6e3c0f8a2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("tokens_valid_after", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "tokens_valid_after")
