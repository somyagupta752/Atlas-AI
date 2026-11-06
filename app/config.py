from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str

    # Directories that POST /repositories/analyze may read, as a JSON list, e.g.
    # ALLOWED_REPOSITORY_ROOTS='["/Users/me/code"]'. Fails closed: when empty or
    # unset, every analysis request is rejected.
    allowed_repository_roots: list[Path] = []


def get_settings() -> Settings:
    return Settings()
