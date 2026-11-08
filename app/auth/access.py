"""Repository-level authorization.

Every repository endpoint calls ``authorize_repository``: the user is authenticated (the caller depends on
``current_user``), the repository exists, the user is a member, and the role allows the operation.
A non-member gets 404, so the existence of someone else's repository is not revealed. A member whose role is too
low gets 403.
"""

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi import Path as PathParam
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.models import ROLE_RANK, Repository, RepositoryMembership, Role, User
from app.auth.service import signing_key
from app.config import Settings, get_settings
from app.core import security
from app.database import get_db
from app.knowledge_graph.models import REPOSITORY_ID_PATTERN

UUID_PATTERN = r"^[0-9a-fA-F-]{36}$"


def get_settings_dep() -> Settings:
    return get_settings()


def current_user(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep)
                 ) -> User:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    claims = security.decode_access_claims(token, signing_key(settings)) if scheme.lower() == "bearer" else None
    user = db.get(User, claims[0]) if claims else None
    if user is None or not user.is_active or (
            user.tokens_valid_after is not None and claims[1] < int(user.tokens_valid_after.timestamp())):
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    return user


def membership_role(db: Session, user: User, repository: Repository) -> Role | None:
    row = db.scalar(select(RepositoryMembership.role).where(
        RepositoryMembership.repository_id == repository.id, RepositoryMembership.user_id == user.id))
    return Role(row) if row else None


def authorize_repository(db: Session, user: User, slug: str, minimum: Role, *, create: bool = False) -> Repository:
    """Return the repository, or raise 404 (not a member, or missing) / 403 (role too low).

    ``create=True`` registers an unknown repository with the caller as owner. That is how a repository comes
    into existence: the first user to index or build it owns it.
    """
    repository = db.scalar(select(Repository).where(Repository.slug == slug))
    if repository is None:
        if user.is_admin and not create:
            # An operator can reach repositories that predate the registry (see the migration notes).
            return Repository(slug=slug)
        if not create:
            raise HTTPException(404, "Repository not found")
        repository = _claim(db, user, slug)
        db.commit()  # a request session is not committed on exit; ownership must persist
    if user.is_admin:
        return repository
    role = membership_role(db, user, repository)
    if role is None:
        raise HTTPException(404, "Repository not found")
    if ROLE_RANK[role] < ROLE_RANK[minimum]:
        raise HTTPException(403, f"This operation needs the {minimum.value} role")
    return repository


def _claim(db: Session, user: User, slug: str) -> Repository:
    repository = Repository(slug=slug, created_by_id=user.id)
    try:
        with db.begin_nested():
            db.add(repository)
            db.flush()
            db.add(RepositoryMembership(repository_id=repository.id, user_id=user.id, role=Role.OWNER.value))
            db.flush()
    except IntegrityError:  # someone claimed it first: fall through to the normal membership check
        repository = db.scalar(select(Repository).where(Repository.slug == slug))
    return repository


def repository_access(minimum: Role, *, create: bool = False):
    """Dependency for routes with a ``{repository_id}`` path parameter."""

    def dependency(repository_id: Annotated[str, PathParam(pattern=REPOSITORY_ID_PATTERN)],
                   user: User = Depends(current_user), db: Session = Depends(get_db)) -> Repository:
        return authorize_repository(db, user, repository_id, minimum, create=create)

    return dependency


def visible_to(owner_id: uuid.UUID | None, user: User) -> bool:
    """Evaluation records: your own, or system records (no owner), or everything for an operator."""
    return user.is_admin or owner_id is None or owner_id == user.id


def investigation_access(minimum: Role):
    """Dependency for routes with an ``{investigation_id}`` path parameter: authorize via its repository."""
    from app.models import InvestigationRecord

    def dependency(investigation_id: Annotated[str, PathParam(pattern=UUID_PATTERN)], user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
        try:
            record = db.get(InvestigationRecord, uuid.UUID(investigation_id))
        except ValueError:
            record = None
        if record is None:
            raise HTTPException(404, "Investigation not found")
        authorize_repository(db, user, record.repository_id, minimum)

    return dependency


def patch_access(minimum: Role):
    """Dependency for routes with a ``{patch_id}`` path parameter."""
    from app.models import PatchProposalRecord

    def dependency(patch_id: Annotated[str, PathParam(pattern=UUID_PATTERN)], user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
        try:
            record = db.get(PatchProposalRecord, uuid.UUID(patch_id))
        except ValueError:
            record = None
        if record is None:
            raise HTTPException(404, "Patch not found")
        authorize_repository(db, user, record.repository_id, minimum)

    return dependency
