"""FastAPI dependencies shared by more than one router.

Kept out of ``main.py`` so routers can import them without a circular import.
``main.py`` re-exports them, so ``app.dependency_overrides`` keys are unchanged.
"""

from pathlib import Path

from fastapi import Request

from app.config import get_settings
from app.retrieval.embeddings import EmbeddingProvider
from app.retrieval.graph_search import GraphContextSource


def get_allowed_repository_roots() -> list[Path]:
    """Dependency: directories the analyzer may read. Empty means analysis is disabled."""
    return get_settings().allowed_repository_roots


def get_embedding_provider_optional(request: Request) -> EmbeddingProvider | None:
    return getattr(request.app.state, "embedding_provider", None)


def get_graph_context_source(request: Request) -> GraphContextSource | None:
    """The graph store, when it supports retrieval reads. None disables graph retrieval."""
    repository = getattr(request.app.state, "graph_repository", None)
    return repository if hasattr(repository, "expand_neighborhood") else None
