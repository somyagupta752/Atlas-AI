"""15.2: accounts, sessions, refresh rotation, reset, rate limiting."""

import re

import pytest
from sqlalchemy import select, text

from app.auth.models import PasswordResetToken, RefreshToken, User
from app.auth.ratelimit import RateLimiter
from app.auth.routes import get_limiter
from app.core import security
from app.main import app
from tests.auth.conftest import PASSWORD, make_settings, new_client, signup

pytestmark = pytest.mark.real_auth


def test_register_stores_an_argon2id_hash_never_the_password(client, db_session):
    r = client.post("/auth/register", json={"email": "Ann@Example.com", "password": PASSWORD})
    assert r.status_code == 201 and r.json()["email"] == "ann@example.com"
    assert "password" not in r.text and "hash" not in r.text
    stored = db_session.scalar(select(User.hashed_password).where(User.email == "ann@example.com"))
    assert stored.startswith("$argon2id$") and PASSWORD not in stored


@pytest.mark.parametrize("email,password,status", [
    ("not-an-email", PASSWORD, 422), ("a@b", PASSWORD, 422), ("ok@example.com", "short", 422),
    ("ok@example.com", "aaaaaaaaaaaaaaaa", 422), ("ok@example.com", "x" * 129, 422),
])
def test_register_rejects_bad_input(client, email, password, status):
    assert client.post("/auth/register", json={"email": email, "password": password}).status_code == status


def test_duplicate_email_is_a_conflict_case_insensitively(client):
    client.post("/auth/register", json={"email": "dup@example.com", "password": PASSWORD})
    assert client.post("/auth/register", json={"email": "DUP@example.com", "password": PASSWORD}).status_code == 409


def test_closed_registration(env, client):
    env["settings"] = make_settings(auth_registration_open=False)
    assert client.post("/auth/register", json={"email": "x@example.com", "password": PASSWORD}).status_code == 403


def test_unknown_fields_are_rejected(client):
    assert client.post("/auth/register", json={"email": "x@example.com", "password": PASSWORD,
                                               "is_admin": True}).status_code == 422


def test_login_and_me(client):
    c, headers, user = signup("me@example.com")
    assert c.get("/auth/me", headers=headers).json()["email"] == "me@example.com"
    assert user["is_admin"] is False


def test_login_failures_are_indistinguishable(client):
    client.post("/auth/register", json={"email": "real@example.com", "password": PASSWORD})
    wrong = client.post("/auth/login", json={"email": "real@example.com", "password": "wrong-password-1"})
    unknown = client.post("/auth/login", json={"email": "nobody@example.com", "password": "wrong-password-1"})
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()
    assert "access_token" not in wrong.text


def test_inactive_users_cannot_log_in_or_use_old_tokens(env, db_session):
    c, headers, _user = signup("gone@example.com")
    db_session.execute(text("UPDATE users SET is_active = false WHERE email = 'gone@example.com'"))
    db_session.expire_all()
    assert c.get("/auth/me", headers=headers).status_code == 401
    assert c.post("/auth/login", json={"email": "gone@example.com", "password": PASSWORD}).status_code == 401


def test_refresh_cookie_is_httponly_strict_and_scoped(client):
    r = client.post("/auth/register", json={"email": "ck@example.com", "password": PASSWORD})
    assert r.status_code == 201
    login = client.post("/auth/login", json={"email": "ck@example.com", "password": PASSWORD})
    cookie = login.headers["set-cookie"]
    assert "atlas_refresh=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Path=/auth" in cookie
    assert "refresh" not in login.text.replace("expires_in", "")  # the refresh token is never in the body
    assert login.headers["cache-control"] == "no-store"


def test_secure_flag_is_on_by_default(env, client):
    env["settings"] = make_settings(auth_cookie_secure=True)
    client.post("/auth/register", json={"email": "sec@example.com", "password": PASSWORD})
    cookie = client.post("/auth/login", json={"email": "sec@example.com", "password": PASSWORD}).headers["set-cookie"]
    assert "Secure" in cookie


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c", "Bearer"])
def test_bad_access_tokens_are_401(client, token):
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert client.get("/auth/me").status_code == 401


def test_expired_wrong_key_and_wrong_type_tokens_are_rejected(env, client, db_session):
    from datetime import UTC, datetime, timedelta

    import jwt

    _, _, user = signup("tok@example.com")
    key = env["settings"].auth_secret_key
    from app.auth.service import signing_key

    secret = signing_key(env["settings"])
    expired = security.create_access_token(user["id"], secret, 1, datetime.now(UTC) - timedelta(minutes=5))
    forged = security.create_access_token(user["id"], "another-secret-another-secret-123", 5)
    refresh_typed = jwt.encode({"sub": user["id"], "typ": "refresh", "iss": "atlas-ai",
                                "exp": datetime.now(UTC) + timedelta(minutes=5)}, secret, algorithm="HS256")
    none_alg = jwt.encode({"sub": user["id"], "typ": "access", "iss": "atlas-ai"}, None, algorithm="none")
    assert key is None
    for token in (expired, forged, refresh_typed, none_alg):
        assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_refresh_rotates_the_token_and_a_stolen_spent_token_revokes_the_family(env, db_session):
    c = new_client()
    c.post("/auth/register", json={"email": "rot@example.com", "password": PASSWORD})
    c.post("/auth/login", json={"email": "rot@example.com", "password": PASSWORD})
    first = c.cookies.get("atlas_refresh")
    r = c.post("/auth/refresh")
    assert r.status_code == 200 and r.json()["access_token"]
    second = c.cookies.get("atlas_refresh")
    assert second and second != first
    # Well after the rotation, someone replays the spent token: theft. The whole login family dies, including
    # the legitimate newer token.
    db_session.execute(text("UPDATE refresh_tokens SET revoked_at = now() - interval '5 minutes' "
                            "WHERE revoked_at IS NOT NULL"))
    thief = new_client()
    thief.cookies.set("atlas_refresh", first, path="/auth")
    assert thief.post("/auth/refresh").status_code == 401
    assert c.post("/auth/refresh").status_code == 401


def test_two_tabs_refreshing_at_once_are_not_mistaken_for_theft(env):
    """Two requests present the same token moments apart. Both succeed; the login survives."""
    c = new_client()
    c.post("/auth/register", json={"email": "tabs@example.com", "password": PASSWORD})
    c.post("/auth/login", json={"email": "tabs@example.com", "password": PASSWORD})
    original = c.cookies.get("atlas_refresh")
    assert c.post("/auth/refresh").status_code == 200  # tab one
    other_tab = new_client()
    other_tab.cookies.set("atlas_refresh", original, path="/auth")
    second = other_tab.post("/auth/refresh")  # tab two, still holding the token tab one just spent
    assert second.status_code == 200 and second.json()["access_token"]
    assert c.post("/auth/refresh").status_code == 200  # the first tab is still signed in


def test_the_grace_window_closes_once_the_replacement_has_been_used(env):
    """If the replacement token was already spent, two parties hold live chains: that is theft, not a race."""
    c = new_client()
    c.post("/auth/register", json={"email": "race@example.com", "password": PASSWORD})
    c.post("/auth/login", json={"email": "race@example.com", "password": PASSWORD})
    original = c.cookies.get("atlas_refresh")
    c.post("/auth/refresh")  # original spent, replacement issued
    c.post("/auth/refresh")  # replacement spent too
    thief = new_client()
    thief.cookies.set("atlas_refresh", original, path="/auth")
    assert thief.post("/auth/refresh").status_code == 401
    assert c.post("/auth/refresh").status_code == 401  # the family was revoked


def test_logout_revokes_the_refresh_token(env):
    c = new_client()
    c.post("/auth/register", json={"email": "out@example.com", "password": PASSWORD})
    c.post("/auth/login", json={"email": "out@example.com", "password": PASSWORD})
    token = c.cookies.get("atlas_refresh")
    assert c.post("/auth/logout").status_code == 204
    replay = new_client()
    replay.cookies.set("atlas_refresh", token, path="/auth")
    assert replay.post("/auth/refresh").status_code == 401
    assert new_client().post("/auth/logout").status_code == 204  # logging out twice is harmless


def test_refresh_without_a_cookie_and_expired_tokens(env, db_session):
    assert new_client().post("/auth/refresh").status_code == 401
    c = new_client()
    c.post("/auth/register", json={"email": "exp@example.com", "password": PASSWORD})
    c.post("/auth/login", json={"email": "exp@example.com", "password": PASSWORD})
    db_session.execute(text("UPDATE refresh_tokens SET expires_at = now() - interval '1 minute'"))
    assert c.post("/auth/refresh").status_code == 401


def test_only_token_hashes_are_stored(env, db_session):
    c = new_client()
    c.post("/auth/register", json={"email": "h@example.com", "password": PASSWORD})
    c.post("/auth/login", json={"email": "h@example.com", "password": PASSWORD})
    raw = c.cookies.get("atlas_refresh")
    hashes = db_session.scalars(select(RefreshToken.token_hash)).all()
    assert raw not in hashes and security.hash_token(raw) in hashes


def test_password_reset_flow(env, client, db_session):
    signup("reset@example.com")
    unknown = client.post("/auth/password-reset", json={"email": "nobody@example.com"})
    known = client.post("/auth/password-reset", json={"email": "reset@example.com"})
    assert unknown.status_code == known.status_code == 202 and unknown.json() == known.json()
    assert [e for e, _ in env["outbox"].sent] == ["reset@example.com"]
    token = env["outbox"].sent[0][1]
    assert token not in known.text
    assert db_session.scalar(select(PasswordResetToken.token_hash)) != token  # hashed at rest

    assert client.post("/auth/password-reset/confirm", json={"token": token, "new_password": "short"}
                       ).status_code == 422
    new_password = "a brand new long password"
    assert client.post("/auth/password-reset/confirm", json={"token": token, "new_password": new_password}
                       ).status_code == 204
    assert client.post("/auth/password-reset/confirm", json={"token": token, "new_password": new_password + "x"}
                       ).status_code == 400  # single use
    assert client.post("/auth/login", json={"email": "reset@example.com", "password": PASSWORD}).status_code == 401
    assert client.post("/auth/login", json={"email": "reset@example.com", "password": new_password}).status_code == 200


def test_a_reset_ends_existing_sessions(env, client):
    c = new_client()
    c.post("/auth/register", json={"email": "sess@example.com", "password": PASSWORD})
    c.post("/auth/login", json={"email": "sess@example.com", "password": PASSWORD})
    client.post("/auth/password-reset", json={"email": "sess@example.com"})
    client.post("/auth/password-reset/confirm", json={"token": env["outbox"].sent[0][1],
                                                      "new_password": "another long password!"})
    assert c.post("/auth/refresh").status_code == 401


def test_expired_and_unknown_reset_tokens(env, client, db_session):
    signup("late@example.com")
    client.post("/auth/password-reset", json={"email": "late@example.com"})
    db_session.execute(text("UPDATE password_reset_tokens SET expires_at = now() - interval '1 minute'"))
    token = env["outbox"].sent[0][1]
    for t in (token, "x" * 40):
        assert client.post("/auth/password-reset/confirm", json={"token": t, "new_password": PASSWORD + "!"}
                           ).status_code == 400


def test_login_is_rate_limited_per_account_and_per_client(env, client):
    limiter = RateLimiter(3, 60)
    app.dependency_overrides[get_limiter] = lambda: limiter
    client.post("/auth/register", json={"email": "rl@example.com", "password": PASSWORD})
    bad = {"email": "rl@example.com", "password": "wrong-password-1"}
    assert [client.post("/auth/login", json=bad).status_code for _ in range(3)] == [401, 401, 401]
    blocked = client.post("/auth/login", json=bad)
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    # even the right password is refused while blocked
    assert client.post("/auth/login", json={"email": "rl@example.com", "password": PASSWORD}).status_code == 429


def test_a_successful_login_clears_the_account_counter(env, client):
    limiter = RateLimiter(3, 60)
    app.dependency_overrides[get_limiter] = lambda: limiter
    client.post("/auth/register", json={"email": "ok@example.com", "password": PASSWORD})
    client.post("/auth/login", json={"email": "ok@example.com", "password": "wrong-password-1"})
    assert client.post("/auth/login", json={"email": "ok@example.com", "password": PASSWORD}).status_code == 200
    assert limiter.blocked("login:ok@example.com") is False


def test_limiter_window_expires():
    now = [0.0]
    limiter = RateLimiter(2, 10, clock=lambda: now[0])
    limiter.record("k")
    limiter.record("k")
    assert limiter.blocked("k")
    now[0] = 11
    assert not limiter.blocked("k")


def test_registration_and_reset_are_rate_limited(env, client):
    limiter = RateLimiter(2, 60)
    app.dependency_overrides[get_limiter] = lambda: limiter
    codes = [client.post("/auth/register", json={"email": f"u{i}@example.com", "password": PASSWORD}).status_code
             for i in range(3)]
    assert codes == [201, 201, 429]
    resets = [client.post("/auth/password-reset", json={"email": "x@example.com"}).status_code for _ in range(3)]
    assert resets[-1] == 429


def test_cors_is_restricted_to_approved_origins():
    from fastapi.testclient import TestClient

    # No origins configured in tests: no CORS headers are ever added.
    r = TestClient(app).options("/auth/login", headers={"Origin": "https://evil.example",
                                                         "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_middleware_allows_only_listed_origins():
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.testclient import TestClient

    mini = FastAPI()
    mini.add_middleware(CORSMiddleware, allow_origins=["https://app.example"], allow_credentials=True,
                        allow_methods=["GET", "POST"], allow_headers=["Authorization"])

    @mini.get("/x")
    def x():
        return {}

    ok = TestClient(mini).get("/x", headers={"Origin": "https://app.example"})
    bad = TestClient(mini).get("/x", headers={"Origin": "https://evil.example"})
    assert ok.headers["access-control-allow-origin"] == "https://app.example"
    assert "access-control-allow-origin" not in bad.headers
    assert re.match(r"^https://", ok.headers["access-control-allow-origin"])


# --- session expiry and token revocation -----------------------------------------------------------------------------


def test_an_expired_access_token_is_refused_and_a_refresh_gets_a_working_one(env):
    from datetime import UTC, datetime, timedelta

    from app.auth.service import signing_key

    c, _headers, user = signup("expiry@example.com")
    expired = security.create_access_token(user["id"], signing_key(env["settings"]), 1,
                                           datetime.now(UTC) - timedelta(minutes=30))
    assert c.get("/auth/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    refreshed = c.post("/auth/refresh")
    assert refreshed.status_code == 200
    assert c.get("/auth/me", headers={"Authorization": f"Bearer {refreshed.json()['access_token']}"}).status_code == 200


def test_logout_everywhere_ends_access_tokens_and_every_refresh_token(env):
    import time

    phone, phone_headers, _user = signup("everywhere@example.com")
    laptop = new_client()
    laptop_login = laptop.post("/auth/login", json={"email": "everywhere@example.com", "password": PASSWORD}).json()
    laptop_headers = {"Authorization": f"Bearer {laptop_login['access_token']}"}
    assert laptop.get("/auth/me", headers=laptop_headers).status_code == 200
    time.sleep(1.1)  # tokens carry whole-second issue times; revocation applies to tokens issued before that second
    assert phone.post("/auth/logout-all", headers=phone_headers).status_code == 204
    for client_, headers in ((phone, phone_headers), (laptop, laptop_headers)):
        assert client_.get("/auth/me", headers=headers).status_code == 401  # access tokens die at once
        assert client_.post("/auth/refresh").status_code == 401             # and so do refresh tokens
    # signing in again works, and the new token is accepted
    again = new_client().post("/auth/login", json={"email": "everywhere@example.com", "password": PASSWORD})
    assert again.status_code == 200
    token = {"Authorization": f"Bearer {again.json()['access_token']}"}
    assert new_client().get("/auth/me", headers=token).status_code == 200


def test_a_password_reset_revokes_access_tokens_already_issued(env, client):
    import time

    _, old_headers, _ = signup("resetrevoke@example.com")
    assert client.get("/auth/me", headers=old_headers).status_code == 200
    time.sleep(1.1)
    client.post("/auth/password-reset", json={"email": "resetrevoke@example.com"})
    client.post("/auth/password-reset/confirm", json={"token": env["outbox"].sent[0][1],
                                                      "new_password": "a completely new password"})
    assert client.get("/auth/me", headers=old_headers).status_code == 401
    fresh = client.post("/auth/login", json={"email": "resetrevoke@example.com",
                                             "password": "a completely new password"}).json()
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {fresh['access_token']}"}).status_code == 200


def test_logout_everywhere_needs_a_login(client):
    assert client.post("/auth/logout-all").status_code == 401
