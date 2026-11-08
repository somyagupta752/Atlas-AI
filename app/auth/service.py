"""Account and session logic. Raises plain exceptions; the routes map them to HTTP."""

import logging
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.models import PasswordResetToken, RefreshToken, User
from app.config import Settings
from app.core import security

logger = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})+$")
_PROCESS_KEY = secrets.token_urlsafe(48)
# Two browser tabs (or React's double effect in development) can present the same refresh token within moments of each
# other. A spent token presented within this window, whose replacement has not been used yet, is a concurrent
# request, not theft: it gets its own new token in the same family. After the window it is treated as theft and the
# whole family is revoked. A thief therefore gets, at best, this many seconds, and only if the real user has not yet used
# the replacement.
ROTATION_GRACE_SECONDS = 10


class AuthError(Exception):
    """Invalid credentials or token. The message is safe to return."""


class RegistrationError(Exception):
    pass


class PasswordPolicyError(ValueError):
    pass


def signing_key(settings: Settings) -> str:
    if settings.auth_secret_key is not None:
        return settings.auth_secret_key.get_secret_value()
    if settings.environment in ("staging", "production"):
        raise RuntimeError("AUTH_SECRET_KEY is required in staging and production")
    return _PROCESS_KEY  # development and test: sessions end with the process


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if len(email) > 254 or not EMAIL_PATTERN.match(email):
        raise RegistrationError("Enter a valid email address")
    return email


def check_password(password: str, email: str, settings: Settings) -> None:
    if len(password) < settings.auth_password_min_length:
        raise PasswordPolicyError(f"The password must have at least {settings.auth_password_min_length} characters")
    if len(password) > 128:
        raise PasswordPolicyError("The password must have at most 128 characters")
    if password.lower() == email.lower() or password.lower().startswith(email.split("@")[0].lower()) and len(
            set(password)) < 6:
        raise PasswordPolicyError("The password is too easy to guess")
    if len(set(password)) < 5:
        raise PasswordPolicyError("The password is too easy to guess")


class AuthService:
    def __init__(self, db: Session, settings: Settings) -> None:
        self.db, self.settings = db, settings

    # --- accounts --------------------------------------------------------------------------

    def register(self, email: str, password: str) -> User:
        email = normalize_email(email)
        check_password(password, email, self.settings)
        if self.db.scalar(select(User).where(User.email == email)) is not None:
            raise RegistrationError("An account with this email already exists")
        user = User(email=email, hashed_password=security.hash_password(password))
        try:
            with self.db.begin_nested():
                self.db.add(user)
                self.db.flush()
        except IntegrityError:
            raise RegistrationError("An account with this email already exists") from None
        return user

    def authenticate(self, email: str, password: str) -> User:
        user = self.db.scalar(select(User).where(User.email == email.strip().lower()))
        valid = security.verify_password(password, user.hashed_password if user else None)
        if not (user and valid and user.is_active):
            raise AuthError("Incorrect email or password")
        if security.needs_rehash(user.hashed_password):
            user.hashed_password = security.hash_password(password)
        return user

    # --- sessions ----------------------------------------------------------------------------

    def issue_session(self, user: User, family_id: uuid.UUID | None = None) -> tuple[str, str]:
        """``(access_token, refresh_token)``. A new login starts a new family."""
        now = datetime.now(UTC)
        refresh = security.new_opaque_token()
        row = RefreshToken(user_id=user.id, family_id=family_id or uuid.uuid4(),
                           token_hash=security.hash_token(refresh),
                           expires_at=now + timedelta(days=self.settings.auth_refresh_token_days))
        self.db.add(row)
        self.db.flush()
        self._last_refresh_row = row
        access = security.create_access_token(user.id, signing_key(self.settings),
                                              self.settings.auth_access_token_minutes, now)
        return access, refresh

    def rotate(self, refresh_token: str | None) -> tuple[User, str, str]:
        """Exchange a refresh token for a new pair. A token that was already used revokes its whole family."""
        if not refresh_token:
            raise AuthError("Not signed in")
        now = datetime.now(UTC)
        row = self.db.scalar(select(RefreshToken).where(RefreshToken.token_hash == security.hash_token(refresh_token))
                             .with_for_update())
        if row is None:
            raise AuthError("Not signed in")
        if row.revoked_at is not None:
            if self._is_concurrent_reuse(row, now):
                user = self.db.get(User, row.user_id)
                if user is not None and user.is_active and row.expires_at > now:
                    access, new_refresh = self.issue_session(user, row.family_id)
                    return user, access, new_refresh
            # Reuse of a spent token: someone holds a stolen copy. End every session of this login.
            self.revoke_family(row.family_id)
            logger.warning("refresh token reuse detected; family revoked")
            raise AuthError("Not signed in")
        if row.expires_at <= now:
            raise AuthError("Not signed in")
        user = self.db.get(User, row.user_id)
        if user is None or not user.is_active:
            raise AuthError("Not signed in")
        access, new_refresh = self.issue_session(user, row.family_id)
        row.revoked_at = now
        row.replaced_by_id = self._last_refresh_row.id
        return user, access, new_refresh

    def _is_concurrent_reuse(self, row: RefreshToken, now: datetime) -> bool:
        if row.replaced_by_id is None or now - row.revoked_at > timedelta(seconds=ROTATION_GRACE_SECONDS):
            return False
        replacement = self.db.get(RefreshToken, row.replaced_by_id)
        # If the replacement was already used (revoked), two parties hold live chains: that is theft, not a race.
        return replacement is not None and replacement.revoked_at is None

    def logout_everywhere(self, user_id: uuid.UUID) -> None:
        """End every session of this user now: refresh tokens, and access tokens issued before this second."""
        self.revoke_all(user_id)
        user = self.db.get(User, user_id)
        if user is not None:
            user.tokens_valid_after = datetime.now(UTC).replace(microsecond=0)

    def logout(self, refresh_token: str | None) -> None:
        if not refresh_token:
            return
        row = self.db.scalar(select(RefreshToken).where(RefreshToken.token_hash == security.hash_token(refresh_token)))
        if row is not None:
            self.revoke_family(row.family_id)

    def revoke_family(self, family_id: uuid.UUID) -> None:
        self.db.execute(update(RefreshToken).where(RefreshToken.family_id == family_id,
                                                   RefreshToken.revoked_at.is_(None))
                        .values(revoked_at=datetime.now(UTC)))

    def revoke_all(self, user_id: uuid.UUID) -> None:
        self.db.execute(update(RefreshToken).where(RefreshToken.user_id == user_id,
                                                   RefreshToken.revoked_at.is_(None))
                        .values(revoked_at=datetime.now(UTC)))

    # --- password reset -----------------------------------------------------------------------

    def request_reset(self, email: str) -> tuple[str, str] | None:
        """Create a reset token for an active account and return ``(address, token)``; None when there is no such account.

        The caller emails the token. Only its SHA-256 is stored. Any older unused link for the account stops working, so only
        the newest message is usable. The caller must answer identically whichever way this returns.
        """
        user = self.db.scalar(select(User).where(User.email == email.strip().lower()))
        if user is None or not user.is_active:
            return None
        now = datetime.now(UTC)
        self.db.execute(update(PasswordResetToken).where(PasswordResetToken.user_id == user.id,
                                                         PasswordResetToken.used_at.is_(None)).values(used_at=now))
        token = security.new_opaque_token()
        self.db.add(PasswordResetToken(
            user_id=user.id, token_hash=security.hash_token(token),
            expires_at=now + timedelta(minutes=self.settings.auth_password_reset_minutes)))
        self.db.flush()
        return user.email, token

    def confirm_reset(self, token: str, new_password: str) -> None:
        """Set a new password with a reset token. Exactly one of any number of simultaneous attempts can succeed.

        The password policy is checked first (a refused password does not spend the link). The link is then claimed with a
        single UPDATE ... WHERE used_at IS NULL AND expires_at > now(): the database lets only one transaction change that row
        from unused to used, whatever the isolation level, so a second attempt finds nothing to claim.
        """
        token_hash = security.hash_token(token)
        now = datetime.now(UTC)
        row = self.db.execute(
            select(PasswordResetToken.user_id).where(
                PasswordResetToken.token_hash == token_hash, PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > now)).first()
        if row is None:
            raise AuthError("The reset link is invalid or has expired")
        user = self.db.get(User, row[0])
        check_password(new_password, user.email, self.settings)
        claimed = self.db.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.token_hash == token_hash, PasswordResetToken.used_at.is_(None),
                   PasswordResetToken.expires_at > now)
            .values(used_at=now).returning(PasswordResetToken.id)).first()
        if claimed is None:  # another request redeemed it between our check and our claim
            raise AuthError("The reset link is invalid or has expired")
        user.hashed_password = security.hash_password(new_password)
        # Every other unused link for this account dies with the password it was meant to replace.
        self.db.execute(update(PasswordResetToken).where(PasswordResetToken.user_id == user.id,
                                                         PasswordResetToken.used_at.is_(None)).values(used_at=now))
        self.logout_everywhere(user.id)  # a reset ends every session, including access tokens already issued
