from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str

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


def get_settings() -> Settings:
    return Settings()
