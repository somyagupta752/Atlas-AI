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


def get_settings() -> Settings:
    return Settings()
