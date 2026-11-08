"""Validated settings for development, test, staging and production (Task 15.3).

* Every value comes from the process environment, a ``.env`` file (development and test only) or a mounted
  secrets directory. Nothing secret is written in code.
* ``ENVIRONMENT`` selects the rules. Development and test are permissive. Staging and production refuse to start
  with a missing secret, an insecure default, the local test backend, a trusted-roots list, a wildcard origin,
  plain-http cookies or the host benchmark endpoint.
* A validation failure raises ``ConfigurationError`` naming the settings that are wrong. It never contains a value.
"""

import logging
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

SECRETS_DIR = Path(os.environ.get("ATLAS_SECRETS_DIR", "/run/secrets"))
# Values that mean "nobody changed the example". Compared case-insensitively against a whole secret.
INSECURE_SECRETS = frozenset({
    "", "changeme", "change-me", "password", "secret", "admin", "atlas", "test", "dev", "default", "example",
    "atlas_dev_password", "atlas-dev-password", "neo4j", "postgres", "your-secret-here",
})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres", "db", "neo4j", "host.docker.internal"})


class ConfigurationError(RuntimeError):
    """The configuration is unusable. The message lists setting names and rules, never values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", secrets_dir=str(SECRETS_DIR) if SECRETS_DIR.is_dir() else None)

    # development | test | staging | production. 15.3 adds per-environment validation.
    environment: Literal["development", "test", "staging", "production"] = "development"

    # Holds a password, so it is a secret: it is masked in reprs and in validation errors.
    database_url: SecretStr

    # --- authentication (15.2) ---
    # Signs access tokens. Required in staging and production. In development an unset key is replaced by a
    # random per-process key, so sessions end when the process restarts.
    auth_secret_key: SecretStr | None = None
    auth_access_token_minutes: int = Field(default=15, ge=1, le=120)
    auth_refresh_token_days: int = Field(default=14, ge=1, le=90)
    auth_password_min_length: int = Field(default=12, ge=8, le=128)
    auth_password_reset_minutes: int = Field(default=30, ge=5, le=240)
    # The refresh cookie is Secure unless this is false (plain-http local development only).
    auth_cookie_secure: bool = True
    # Anyone may register while true. Set false once the intended users exist.
    auth_registration_open: bool = True
    # Failed or repeated attempts per client address and per account within the window.
    auth_rate_limit_attempts: int = Field(default=8, ge=1, le=1000)
    auth_rate_limit_window_seconds: int = Field(default=300, ge=1, le=86_400)
    # --- password-reset email (15.5) ---
    # Reset links are emailed; there is no other delivery and no token is ever logged. Any SMTP server works, including the
    # SMTP interfaces of transactional providers: set the host, port and security mode they document, put their SMTP
    # credentials in SMTP_USERNAME and the SMTP_PASSWORD secret.
    auth_password_reset_enabled: bool = True
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    # starttls: connect in plain text and upgrade (usually port 587). ssl: TLS from the first byte (usually 465).
    # none: no encryption - only for a local test mail server; refused for any other host in staging and production.
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    # The address messages come from. Providers usually require a verified sender or domain.
    smtp_from: str | None = None
    smtp_from_name: str = Field(default="Atlas AI", max_length=80)
    smtp_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    # Trust this CA bundle instead of the system roots (an internal relay with a private CA). Certificates and host names are
    # always verified; there is no switch to turn that off.
    smtp_ca_file: Path | None = None
    # Where the dashboard is served, used to build the link in the email (https://atlas.example.com). No trailing slash.
    public_base_url: str | None = None

    # Origins allowed to call the API from a browser. Empty: no cross-origin access.
    cors_allowed_origins: list[str] = []
    # POST /benchmarks/run executes pytest on the host. Off by default; never available in production.
    benchmarks_endpoint_enabled: bool = False

    # Names this deployment's worker containers (label atlas.instance). Startup removes only containers with
    # this label, so two deployments on one Docker host never touch each other's workers.
    instance_id: str = Field(default="default", pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")

    # json | text | auto (json in staging and production). One JSON object per line on stdout.
    log_format: Literal["json", "text", "auto"] = "auto"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Bearer token for GET /metrics. Unset: the endpoint answers 404.
    metrics_token: SecretStr | None = None

    # Interactive API docs and the OpenAPI document. None: on in development and test, off in staging and production.
    docs_enabled: bool | None = None

    # Directories that POST /repositories/analyze may read, as a JSON list, e.g.
    # ALLOWED_REPOSITORY_ROOTS='["/Users/me/code"]'. Fails closed: when empty or
    # unset, every analysis request is rejected.
    allowed_repository_roots: list[Path] = []

    # Neo4j for the code knowledge graph. Graph endpoints return 503 while unset.
    neo4j_uri: str | None = None
    neo4j_username: str | None = None
    neo4j_password: SecretStr | None = None
    neo4j_database: str = "neo4j"

    # Embeddings for code retrieval. "hashing" runs locally and sends nothing anywhere.
    # "openai_compatible" calls an HTTP /embeddings endpoint (OpenAI, Ollama, vLLM, ...).
    embedding_provider: Literal["hashing", "openai_compatible"] = "hashing"
    embedding_model: str = "atlas-hashing-v1"
    # Must match the code_chunks.embedding column (see app.models.EMBEDDING_DIMENSIONS).
    embedding_dimensions: int = 1024
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = None
    # Repository source code leaves this machine only when this is true. Endpoints on
    # localhost are always allowed; any other host is refused while it is false.
    embedding_allow_external: bool = False
    embedding_batch_size: int = Field(default=64, ge=1, le=2048)
    embedding_timeout_seconds: float = Field(default=30.0, gt=0)
    # Send a "dimensions" field (OpenAI text-embedding-3 models accept it).
    embedding_send_dimensions: bool = False

    # LLM for the debugging agent. The investigation endpoints return 503 while unset.
    llm_provider: Literal["anthropic", "disabled"] = "anthropic"
    # Read from ANTHROPIC_API_KEY. Never logged, never returned.
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    llm_max_tokens: int = Field(default=16000, ge=1024, le=64000)
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_connect_timeout_seconds: float = Field(default=10.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    # Re-run a request declined by a safety classifier on a recommended fallback model.
    llm_server_fallbacks: bool = True
    # The agent sends repository source code to the LLM provider. Refused while false.
    llm_allow_external: bool = False

    # Debugging agent limits. Starting values, not universal constants.
    agent_max_iterations: int = Field(default=12, ge=1, le=100)
    agent_max_tool_calls: int = Field(default=20, ge=1, le=200)
    agent_tool_timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    agent_max_tool_output_bytes: int = Field(default=20_000, ge=1_000, le=1_000_000)
    agent_max_duration_seconds: float = Field(default=120.0, gt=0, le=3600)
    # Repositories under these directories may have their tests run by the agent.
    # Fails closed: empty means run_tests refuses every request. Running tests is
    # code execution, not a sandbox: list trusted repositories only.
    agent_trusted_test_roots: list[Path] = []

    # Patch generation and verification (Task 13). Patches are verified only in
    # repositories under AGENT_TRUSTED_TEST_ROOTS: verification runs their tests.
    patch_max_attempts: int = Field(default=3, ge=1, le=10)
    # Verification time (baseline and checks) per request. LLM latency is bounded
    # separately by PATCH_LLM_TIMEOUT_SECONDS per attempt.
    patch_max_total_seconds: float = Field(default=180.0, gt=0, le=3600)
    patch_max_files: int = Field(default=5, ge=1, le=50)
    patch_max_changed_lines: int = Field(default=500, ge=1, le=10_000)
    patch_max_check_output_bytes: int = Field(default=100_000, ge=1_000, le=10_000_000)
    patch_check_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    patch_llm_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    # Global LLM budget per request, across every attempt and repair call. Before each
    # call its worst-case cost is reserved; a call that might not fit is not made.
    patch_max_llm_requests: int = Field(default=6, ge=1, le=100)
    patch_max_llm_seconds: float = Field(default=300.0, gt=0, le=7200)
    patch_max_llm_cost_usd: float = Field(default=1.0, gt=0, le=100)
    # Graph-based test discovery: Neo4j when it is current, else an in-memory graph of the source.
    patch_graph_test_discovery: bool = True
    patch_graph_max_depth: int = Field(default=3, ge=1, le=6)
    patch_max_graph_test_files: int = Field(default=10, ge=1, le=100)
    # Where repository tests run. "local": a child process of Atlas (not a sandbox).
    # "container": one disposable Docker container per run (docker/test-runner.Dockerfile).
    patch_test_backend: Literal["local", "container"] = "local"
    patch_container_image: str = "atlas-ai-test-runner:py3.12.13-pytest9.2.0"
    patch_container_cpus: float = Field(default=1.0, gt=0, le=16)
    patch_container_memory_mb: int = Field(default=1024, ge=64, le=65536)
    patch_container_pids_limit: int = Field(default=256, ge=16, le=10000)
    # With the container backend only: verify repositories outside AGENT_TRUSTED_TEST_ROOTS
    # (still inside ALLOWED_REPOSITORY_ROOTS). A container is a boundary, not a guarantee.
    patch_container_allow_untrusted: bool = False
    patch_baseline_runs: int = Field(default=2, ge=1, le=5)
    patch_timeout_policy: Literal["reject", "inconclusive"] = "reject"
    patch_require_ruff: bool = True
    # Where isolated workspaces are created. Default: <system temp>/atlas-ai/patch-workspaces.
    patch_workspace_dir: Path | None = None
    # delete: always remove; keep_failed: keep non-verified attempts for inspection; keep: never remove.
    patch_workspace_retention: Literal["delete", "keep_failed", "keep"] = "delete"
    patch_workspace_max_age_hours: float = Field(default=24.0, gt=0)




    @field_validator("smtp_host", "smtp_username", "smtp_from", "public_base_url", mode="before")
    @classmethod
    def _blank_text_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else (value.strip() if isinstance(value, str) else value)

    @field_validator("anthropic_api_key", "neo4j_password", "embedding_api_key", "auth_secret_key", "metrics_token",
                     "smtp_password", mode="before")
    @classmethod
    def _empty_secret_is_unset(cls, value: object) -> object:
        """A mounted secret file may exist but be empty (an optional secret left blank): that means "not set"."""
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _validate_environment(self) -> "Settings":
        problems = environment_problems(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def is_deployed(self) -> bool:
        return self.environment in ("staging", "production")

    @property
    def api_docs_enabled(self) -> bool:
        return self.docs_enabled if self.docs_enabled is not None else not self.is_deployed

    def public_summary(self) -> dict[str, object]:
        """Non-secret facts about the running configuration, safe to log or show."""
        return {
            "environment": self.environment, "test_backend": self.patch_test_backend,
            "llm_provider": self.llm_provider, "llm_model": self.llm_model,
            "llm_external_allowed": self.llm_allow_external, "embedding_provider": self.embedding_provider,
            "neo4j_configured": self.neo4j_uri is not None, "docs_enabled": self.api_docs_enabled,
            "registration_open": self.auth_registration_open, "cors_origins": len(self.cors_allowed_origins),
        }


def _insecure(secret: SecretStr | None) -> bool:
    if secret is None:
        return True
    value = secret.get_secret_value()
    return value.strip().lower() in INSECURE_SECRETS


def _url_password(url: str) -> str | None:
    try:
        return urlsplit(url).password
    except ValueError:
        return None


def _host(url: str | None) -> str | None:
    try:
        return urlsplit(url).hostname if url else None
    except ValueError:
        return None


def environment_problems(s: "Settings") -> list[str]:
    """Rule violations for ``s.environment``, as messages that name settings and never show values."""
    problems: list[str] = []
    db_password = _url_password(s.database_url.get_secret_value())
    if s.is_deployed:
        if s.auth_secret_key is None or len(s.auth_secret_key.get_secret_value()) < 32 or _insecure(s.auth_secret_key):
            problems.append("AUTH_SECRET_KEY must be set to a random value of at least 32 characters")
        if db_password is None or db_password.lower() in INSECURE_SECRETS or len(db_password) < 12:
            problems.append("DATABASE_URL must contain a strong password (at least 12 characters, not a default)")
        if s.neo4j_uri is not None and (s.neo4j_password is None or _insecure(s.neo4j_password)
                                        or len(s.neo4j_password.get_secret_value()) < 12):
            problems.append("NEO4J_PASSWORD must be a strong password when NEO4J_URI is set")
        if s.patch_test_backend != "container":
            problems.append("PATCH_TEST_BACKEND must be 'container': the local backend runs repository code as "
                            "the API process")
        if s.agent_trusted_test_roots:
            problems.append("AGENT_TRUSTED_TEST_ROOTS must be empty: trusted roots run tests on the host")
        if s.patch_test_backend == "container" and not s.patch_container_allow_untrusted:
            # With no trusted roots, a container is the only permitted way to run a repository's tests. Without this
            # flag every patch request would be refused as "untrusted".
            problems.append("PATCH_CONTAINER_ALLOW_UNTRUSTED must be true: with AGENT_TRUSTED_TEST_ROOTS empty, "
                            "container isolation is the only way repository tests may run")
        if s.patch_workspace_dir is None:
            problems.append("PATCH_WORKSPACE_DIR must be set to a directory that exists at the same path on the "
                            "Docker host (worker containers bind-mount it)")
        if not s.auth_cookie_secure:
            problems.append("AUTH_COOKIE_SECURE must be true")
        if s.benchmarks_endpoint_enabled:
            problems.append("BENCHMARKS_ENDPOINT_ENABLED must be false: it runs pytest on the host")
        if any(o == "*" or not o.startswith("https://") for o in s.cors_allowed_origins):
            problems.append("CORS_ALLOWED_ORIGINS must list explicit https:// origins, never '*'")
        if s.llm_provider == "anthropic" and s.llm_allow_external and (
                s.anthropic_api_key is None or len(s.anthropic_api_key.get_secret_value()) < 20):
            problems.append("ANTHROPIC_API_KEY is required when LLM_ALLOW_EXTERNAL is true")
        if s.embedding_provider == "openai_compatible" and s.embedding_allow_external and s.embedding_api_key is None:
            problems.append("EMBEDDING_API_KEY is required for an external embedding endpoint")
    problems.extend(_mail_problems(s))
    if s.environment == "production" and s.docs_enabled:
        problems.append("DOCS_ENABLED must not be true in production")
    if s.environment == "development":
        # A development process pointed at a remote database is a deployment that forgot ENVIRONMENT.
        host = _host(s.database_url.get_secret_value())
        if host and "." in host and host not in LOCAL_HOSTS and not host.endswith((".local", ".localhost")):
            problems.append("ENVIRONMENT is 'development' but DATABASE_URL points at a remote host; set "
                            "ENVIRONMENT=staging or production for a deployment")
    return problems


EMAIL_ADDRESS = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})+$")


def _mail_problems(s: "Settings") -> list[str]:
    """Rules for the reset email. They apply to every environment where they can be checked, and more strictly when deployed."""
    problems: list[str] = []
    if s.smtp_username and s.smtp_password is None:
        problems.append("SMTP_PASSWORD is required when SMTP_USERNAME is set")
    if s.smtp_password is not None and not s.smtp_username:
        problems.append("SMTP_USERNAME is required when SMTP_PASSWORD is set")
    if s.smtp_from is not None and not EMAIL_ADDRESS.match(s.smtp_from):
        problems.append("SMTP_FROM must be a plain email address such as atlas@example.com")
    if s.public_base_url is not None and (
            not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d{1,5})?", s.public_base_url)):
        problems.append("PUBLIC_BASE_URL must be an origin such as https://atlas.example.com (no path, no trailing slash)")
    if s.is_deployed and s.auth_password_reset_enabled:
        if not s.smtp_host:
            problems.append("SMTP_HOST is required: password reset sends email (or set AUTH_PASSWORD_RESET_ENABLED=false)")
        if not s.smtp_from:
            problems.append("SMTP_FROM is required when password reset is enabled")
        if not s.public_base_url or not s.public_base_url.startswith("https://"):
            problems.append("PUBLIC_BASE_URL must be an https:// origin when password reset is enabled")
        if s.smtp_security == "none" and (_host_of(s.smtp_host) not in LOCAL_HOSTS):
            problems.append("SMTP_SECURITY must be 'starttls' or 'ssl' for a mail server that is not local")
    return problems


def _host_of(host: str | None) -> str | None:
    return host.strip().lower() if host else None


def load_settings() -> Settings:
    """Build the settings, turning any validation error into a ``ConfigurationError`` without values."""
    try:
        return Settings()
    except ValidationError as exc:
        lines = []
        for error in exc.errors(include_input=False, include_url=False, include_context=False):
            where = ".".join(str(part).upper() for part in error["loc"]) or "settings"
            lines.append(f"{where}: {error['msg'].removeprefix('Value error, ')}")
        raise ConfigurationError("Invalid configuration: " + " | ".join(lines)) from None


def get_settings() -> Settings:
    return load_settings()
