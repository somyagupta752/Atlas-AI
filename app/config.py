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


def get_settings() -> Settings:
    return Settings()
