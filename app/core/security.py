"""Password hashing and token primitives. No database, no HTTP.

* Passwords: Argon2id with the library's current defaults. A hash that needs rehashing is detected at login.
* Access tokens: short-lived signed JWTs (HS256) carrying only the user id. They are kept in browser memory,
  never in local storage.
* Refresh and reset tokens: 256-bit random values. Only their SHA-256 is stored, so a database leak does not
  yield usable tokens.
"""

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # Argon2id
# Verified against when the account does not exist, so a missing user costs the same time as a wrong password.
_DUMMY_HASH = _hasher.hash("atlas-dummy-password-for-timing")

ALGORITHM = "HS256"
ISSUER = "atlas-ai"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str | None) -> bool:
    try:
        return _hasher.verify(hashed or _DUMMY_HASH, password) and hashed is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    return _hasher.check_needs_rehash(hashed)


def new_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def create_access_token(user_id: uuid.UUID, secret: str, minutes: int, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    claims = {"sub": str(user_id), "typ": "access", "iss": ISSUER, "iat": now,
              "exp": now + timedelta(minutes=minutes), "jti": secrets.token_hex(8)}
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def decode_access_claims(token: str, secret: str) -> tuple[uuid.UUID, int] | None:
    """``(user id, issued-at seconds)``, or None for any invalid, expired or wrong-type token. Never raises."""
    try:
        claims = jwt.decode(token, secret, algorithms=[ALGORITHM], issuer=ISSUER,
                            options={"require": ["exp", "sub", "iss", "iat"]})
        if claims.get("typ") != "access":
            return None
        return uuid.UUID(claims["sub"]), int(claims["iat"])
    except (jwt.PyJWTError, ValueError, KeyError, TypeError):
        return None


def decode_access_token(token: str, secret: str) -> uuid.UUID | None:
    """The user id, or None for any invalid, expired or wrong-type token. Never raises."""
    decoded = decode_access_claims(token, secret)
    return decoded[0] if decoded else None
