"""Real authentication against real PostgreSQL (rollback-wrapped). No operator override."""

import pytest
from fastapi.testclient import TestClient

from app.agents.llm import FakeLLM
from app.agents.routes import get_llm_provider
from app.auth.access import get_settings_dep
from app.auth.mail import EmailDeliveryError, OutgoingEmail
from app.auth.ratelimit import RateLimiter
from app.auth.routes import get_email_sender, get_limiter
from app.config import Settings
from app.database import get_db
from app.knowledge_graph.repository import InMemoryGraphRepository
from app.main import (
    app,
    get_allowed_repository_roots,
    get_embedding_provider_optional,
    get_graph_repository,
)
from app.retrieval.embeddings import HashingEmbeddingProvider

PASSWORD = "correct horse battery staple"


class Outbox:
    """A fake mail server: keeps every message it is asked to send, or fails like a broken provider when told to."""

    def __init__(self):
        self.messages: list[OutgoingEmail] = []
        self.fail_with: EmailDeliveryError | None = None
        self.attempts = 0

    def send(self, message: OutgoingEmail) -> None:
        self.attempts += 1
        if self.fail_with is not None:
            raise self.fail_with
        self.messages.append(message)

    @property
    def sent(self) -> list[tuple[str, str]]:
        """``(recipient, token)`` pairs, the token read from the link in the message exactly as a person would follow it."""
        return [(m.to, token_in(m)) for m in self.messages]


def token_in(message: OutgoingEmail) -> str:
    import re

    found = re.search(r"/reset-password#token=([A-Za-z0-9_-]+)", message.text)
    assert found, "the message contains no reset link"
    return found.group(1)


def make_settings(**kw):
    values = {"database_url": "postgresql+psycopg://x/y", "auth_cookie_secure": False,
              "auth_rate_limit_attempts": 1000, "environment": "test"}
    values.update(kw)
    return Settings(_env_file=None, **values)


@pytest.fixture
def env(db_session, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    state = {"settings": make_settings(), "limiter": None, "outbox": Outbox()}

    def limiter():
        if state["limiter"] is None:
            state["limiter"] = RateLimiter(1000, 60)
        return state["limiter"]

    graph = InMemoryGraphRepository()
    app.dependency_overrides.update({
        get_db: lambda: db_session,
        get_settings_dep: lambda: state["settings"],
        get_limiter: limiter,
        get_email_sender: lambda: state["outbox"],
        get_allowed_repository_roots: lambda: [allowed],
        get_embedding_provider_optional: lambda: HashingEmbeddingProvider(),
        get_graph_repository: lambda: graph,
        get_llm_provider: lambda: FakeLLM([]),
    })
    state.update(allowed=allowed, tmp=tmp_path)
    yield state
    app.dependency_overrides.clear()


@pytest.fixture
def client(env):
    return TestClient(app)


def new_client():
    return TestClient(app)


def signup(email, password=PASSWORD):
    """A fresh client with its own cookie jar, registered and logged in. Returns ``(client, headers, user)``."""
    c = new_client()
    assert c.post("/auth/register", json={"email": email, "password": password}).status_code == 201
    login = c.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    body = login.json()
    return c, {"Authorization": f"Bearer {body['access_token']}"}, body["user"]
