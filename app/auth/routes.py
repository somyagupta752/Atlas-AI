"""POST /auth/register, /login, /refresh, /logout, /password-reset(+/confirm), GET /auth/me, and repository members."""

import uuid
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.access import (
    authorize_repository,
    current_user,
    get_settings_dep,
    membership_role,
)
from app.auth.mail import EmailSender, deliver_password_reset, email_sender_for
from app.auth.models import Repository, RepositoryMembership, Role, User
from app.auth.ratelimit import RateLimiter
from app.auth.service import (
    AuthError,
    AuthService,
    PasswordPolicyError,
    RegistrationError,
)
from app.config import Settings
from app.database import get_db

router = APIRouter(tags=["auth"])
COOKIE = "atlas_refresh"
COOKIE_PATH = "/auth"

_limiters: dict[tuple[int, int], RateLimiter] = {}


def get_limiter(settings: Settings = Depends(get_settings_dep)) -> RateLimiter:
    key = (settings.auth_rate_limit_attempts, settings.auth_rate_limit_window_seconds)
    if key not in _limiters:
        _limiters[key] = RateLimiter(*key)
    return _limiters[key]


def get_email_sender(settings: Settings = Depends(get_settings_dep)) -> EmailSender:
    return email_sender_for(settings)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(_Body):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)


class EmailBody(_Body):
    email: str = Field(max_length=320)


class ResetConfirm(_Body):
    token: str = Field(min_length=10, max_length=200)
    new_password: str = Field(max_length=256)


class MemberBody(_Body):
    email: str = Field(max_length=320)
    role: Role


def user_out(user: User) -> dict[str, Any]:
    return {"id": str(user.id), "email": user.email, "is_active": user.is_active, "is_admin": user.is_admin,
            "created_at": user.created_at}


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(COOKIE, token, max_age=settings.auth_refresh_token_days * 86_400, httponly=True,
                        secure=settings.auth_cookie_secure, samesite="strict", path=COOKIE_PATH)


def _guard(limiter: RateLimiter, *keys: str) -> None:
    if limiter.blocked(*keys):
        raise HTTPException(429, "Too many attempts; try again later",
                            headers={"Retry-After": str(limiter.retry_after(*keys))})


@router.post("/auth/register", status_code=201)
def register(body: Credentials, request: Request, db: Session = Depends(get_db),
             settings: Settings = Depends(get_settings_dep), limiter: RateLimiter = Depends(get_limiter)):
    if not settings.auth_registration_open:
        raise HTTPException(403, "Registration is closed")
    key = f"register:{_client(request)}"
    _guard(limiter, key)
    limiter.record(key)
    try:
        user = AuthService(db, settings).register(body.email, body.password)
        db.commit()
    except (RegistrationError, PasswordPolicyError) as exc:
        db.rollback()
        raise HTTPException(409 if "exists" in str(exc) else 422, str(exc)) from None
    return user_out(user)


@router.post("/auth/login")
def login(body: Credentials, request: Request, response: Response, db: Session = Depends(get_db),
          settings: Settings = Depends(get_settings_dep), limiter: RateLimiter = Depends(get_limiter)):
    ip_key, account_key = f"login-ip:{_client(request)}", f"login:{body.email.strip().lower()}"
    _guard(limiter, ip_key, account_key)
    service = AuthService(db, settings)
    try:
        user = service.authenticate(body.email, body.password)
    except AuthError as exc:
        limiter.record(ip_key, account_key)
        db.rollback()
        raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from None
    limiter.reset(account_key)
    access, refresh = service.issue_session(user)
    db.commit()
    _set_cookie(response, refresh, settings)
    response.headers["Cache-Control"] = "no-store"
    return {"access_token": access, "token_type": "bearer", "expires_in": settings.auth_access_token_minutes * 60,
            "user": user_out(user)}


@router.post("/auth/refresh")
def refresh(request: Request, response: Response, db: Session = Depends(get_db),
            settings: Settings = Depends(get_settings_dep), limiter: RateLimiter = Depends(get_limiter)):
    key = f"refresh:{_client(request)}"
    _guard(limiter, key)
    service = AuthService(db, settings)
    try:
        user, access, new_refresh = service.rotate(request.cookies.get(COOKIE))
        db.commit()
    except AuthError as exc:
        limiter.record(key)
        db.commit()  # keeps the family revocation made on token reuse
        response.delete_cookie(COOKIE, path=COOKIE_PATH)
        raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from None
    _set_cookie(response, new_refresh, settings)
    response.headers["Cache-Control"] = "no-store"
    return {"access_token": access, "token_type": "bearer", "expires_in": settings.auth_access_token_minutes * 60,
            "user": user_out(user)}


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db),
           settings: Settings = Depends(get_settings_dep)):
    AuthService(db, settings).logout(request.cookies.get(COOKIE))
    db.commit()
    response.delete_cookie(COOKIE, path=COOKIE_PATH)
    response.status_code = 204
    return response


@router.post("/auth/logout-all", status_code=204)
def logout_all(response: Response, user: User = Depends(current_user), db: Session = Depends(get_db),
               settings: Settings = Depends(get_settings_dep)):
    """End every session of the caller, on every device, immediately."""
    AuthService(db, settings).logout_everywhere(user.id)
    db.commit()
    response.delete_cookie(COOKIE, path=COOKIE_PATH)
    response.status_code = 204
    return response


@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return user_out(user)


RESET_ACCEPTED = {"detail": "If the account exists, a reset message has been sent"}


@router.post("/auth/password-reset", status_code=202)
def password_reset(body: EmailBody, request: Request, background: BackgroundTasks, db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings_dep), limiter: RateLimiter = Depends(get_limiter),
                   sender: EmailSender = Depends(get_email_sender)):
    """Always answers 202 with the same body, whether or not the account exists or the email could be sent.

    The message is sent AFTER the response, in the background: neither the time taken nor an error from the mail server can
    tell the caller whether an account exists.
    """
    keys = (f"reset-ip:{_client(request)}", f"reset:{body.email.strip().lower()}")
    _guard(limiter, *keys)
    limiter.record(*keys)
    if not settings.auth_password_reset_enabled:
        return RESET_ACCEPTED
    issued = AuthService(db, settings).request_reset(body.email)
    db.commit()
    if issued is not None:
        background.add_task(deliver_password_reset, sender, settings, issued[0], issued[1])
    return RESET_ACCEPTED


@router.post("/auth/password-reset/confirm", status_code=204)
def password_reset_confirm(body: ResetConfirm, request: Request, db: Session = Depends(get_db),
                           settings: Settings = Depends(get_settings_dep),
                           limiter: RateLimiter = Depends(get_limiter)):
    key = f"reset-confirm:{_client(request)}"
    _guard(limiter, key)
    limiter.record(key)
    try:
        AuthService(db, settings).confirm_reset(body.token, body.new_password)
        db.commit()
    except AuthError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from None
    except PasswordPolicyError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from None
    return Response(status_code=204)


# --- the caller's repositories ---------------------------------------------------------------


@router.get("/repositories")
def my_repositories(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Repositories the caller can see, with their role. An operator sees every registered repository."""
    if user.is_admin:
        rows = [(r, "admin") for r in db.scalars(select(Repository).order_by(Repository.slug))]
    else:
        rows = [(r, m.role) for r, m in db.execute(
            select(Repository, RepositoryMembership).join(RepositoryMembership, RepositoryMembership.repository_id ==
                                                           Repository.id)
            .where(RepositoryMembership.user_id == user.id).order_by(Repository.slug)).all()]
    return {"items": [{"repository_id": r.slug, "role": role, "created_at": r.created_at} for r, role in rows]}


@router.get("/repositories/{repository_id}")
def repository_detail(repository_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    repository = authorize_repository(db, user, repository_id, Role.VIEWER)
    role = "admin" if user.is_admin else membership_role(db, user, repository).value
    return {"repository_id": repository.slug, "role": role, "created_at": repository.created_at,
            "capabilities": CAPABILITIES[role]}


CAPABILITIES = {
    "viewer": ["read index status", "search code", "read graph", "read diagnoses and patches"],
    "editor": ["read index status", "search code", "read graph", "read diagnoses and patches", "index and re-index",
               "build the graph", "start diagnoses", "generate and verify patches"],
    "owner": ["read index status", "search code", "read graph", "read diagnoses and patches", "index and re-index",
              "build the graph", "start diagnoses", "generate and verify patches", "delete the index and graph",
              "manage members"],
}
CAPABILITIES["admin"] = CAPABILITIES["owner"]


# --- repository membership (owners manage who can do what) -------------------------------------


@router.get("/repositories/{repository_id}/members")
def list_members(repository_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    repository = authorize_repository(db, user, repository_id, Role.OWNER)
    rows = db.execute(select(RepositoryMembership, User).join(User, User.id == RepositoryMembership.user_id)
                      .where(RepositoryMembership.repository_id == repository.id)
                      .order_by(RepositoryMembership.created_at)).all()
    return {"repository_id": repository.slug,
            "members": [{"user_id": str(u.id), "email": u.email, "role": m.role} for m, u in rows]}


@router.put("/repositories/{repository_id}/members")
def set_member(repository_id: str, body: MemberBody, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    repository = authorize_repository(db, user, repository_id, Role.OWNER)
    target = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if target is None:
        raise HTTPException(404, "No user with this email")
    membership = db.scalar(select(RepositoryMembership).where(
        RepositoryMembership.repository_id == repository.id, RepositoryMembership.user_id == target.id))
    if membership is not None and membership.role == Role.OWNER.value and body.role != Role.OWNER:
        _require_another_owner(db, repository, target)
    if membership is None:
        db.add(RepositoryMembership(repository_id=repository.id, user_id=target.id, role=body.role.value))
    else:
        membership.role = body.role.value
    db.commit()
    return {"user_id": str(target.id), "email": target.email, "role": body.role.value}


@router.delete("/repositories/{repository_id}/members/{member_id}", status_code=204)
def remove_member(repository_id: str, member_id: uuid.UUID, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    repository = authorize_repository(db, user, repository_id, Role.OWNER)
    membership = db.scalar(select(RepositoryMembership).where(
        RepositoryMembership.repository_id == repository.id, RepositoryMembership.user_id == member_id))
    if membership is None:
        raise HTTPException(404, "Member not found")
    if membership.role == Role.OWNER.value:
        _require_another_owner(db, repository, db.get(User, member_id))
    db.delete(membership)
    db.commit()
    return Response(status_code=204)


def _require_another_owner(db: Session, repository: Repository, leaving: User) -> None:
    owners = db.scalars(select(RepositoryMembership.user_id).where(
        RepositoryMembership.repository_id == repository.id, RepositoryMembership.role == Role.OWNER.value)).all()
    if [o for o in owners if o != leaving.id] == []:
        raise HTTPException(409, "A repository must keep at least one owner")
