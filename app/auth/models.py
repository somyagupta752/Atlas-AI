"""Users, the repository registry, memberships and session tokens."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    literal_column,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Role(StrEnum):
    VIEWER = "viewer"
    EDITOR = "editor"
    OWNER = "owner"


ROLE_RANK = {Role.VIEWER: 1, Role.EDITOR: 2, Role.OWNER: 3}


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("uq_users_email_lower", func.lower(literal_column("email")), unique=True),)

    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(String(320))
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Operators only. Bypasses repository roles; never granted through the API.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Access tokens issued before this instant (whole seconds) are refused. Set by logout-all, password reset and
    # anything else that must end sessions immediately, instead of waiting for the 15-minute token to expire.
    tokens_valid_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Repository(Base):
    """The registry of repositories. ``slug`` is the existing ``repository_id`` string used everywhere else."""

    __tablename__ = "repositories"

    id: Mapped[uuid.UUID] = _pk()
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    memberships: Mapped[list["RepositoryMembership"]] = relationship(back_populates="repository")


class RepositoryMembership(Base):
    __tablename__ = "repository_memberships"
    __table_args__ = (
        UniqueConstraint("repository_id", "user_id", name="uq_repository_memberships_repo_user"),
        CheckConstraint("role IN ('owner', 'editor', 'viewer')", name="ck_repository_memberships_role"),
        Index("ix_repository_memberships_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    repository: Mapped[Repository] = relationship(back_populates="memberships")


class RefreshToken(Base):
    """One refresh token. Tokens of one login share ``family_id``; reuse of a spent token revokes the family."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_family_id", "family_id"), Index("ix_refresh_tokens_user_id", "user_id"))

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                                               index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
