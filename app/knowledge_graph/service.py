"""Orchestration for graph builds. API handlers call this; it knows nothing of HTTP."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.code_intelligence import analyze_repository
from app.knowledge_graph.builder import build_code_graph, derive_repository_id
from app.knowledge_graph.models import CodeGraph
from app.knowledge_graph.repository import (
    GraphNotFoundError,
    GraphRepository,
    GraphStats,
    ensure_valid,
)


class RepositoryIdConflictError(Exception):
    """The repository ID is already used by a graph built from a different directory."""


class GraphBuildResult(BaseModel):
    repository_id: str
    status: Literal["completed"] = "completed"
    files_analyzed: int
    files_failed: int
    total_nodes: int
    total_relationships: int
    nodes_created: int
    nodes_deleted: int
    relationships_created: int
    relationships_deleted: int
    unresolved_references: int


class GraphService:
    def __init__(self, repository: GraphRepository) -> None:
        self._repository = repository

    def build_graph(self, repository_path: Path, repository_id: str | None = None) -> GraphBuildResult:
        """Analyze, build, validate and persist; return what changed.

        Raises:
            RepositoryNotFoundError / RepositoryNotADirectoryError: bad path.
            RepositoryIdConflictError: the ID belongs to another directory.
            InvalidGraphError: the built graph failed validation (nothing is stored).
            GraphStoreError: the graph store failed (the previous graph is kept).
        """
        analysis = analyze_repository(repository_path)
        repo_id = repository_id or derive_repository_id(analysis.repository_path)
        self._check_ownership(repo_id, analysis.repository_path)

        graph = build_code_graph(analysis, repo_id)
        ensure_valid(graph)
        saved = self._repository.save_graph(graph)
        return GraphBuildResult(
            repository_id=repo_id,
            files_analyzed=analysis.total_files,
            files_failed=analysis.failed_files,
            total_nodes=len(graph.nodes),
            total_relationships=len(graph.relationships),
            nodes_created=saved.nodes_created,
            nodes_deleted=saved.nodes_deleted,
            relationships_created=saved.relationships_created,
            relationships_deleted=saved.relationships_deleted,
            unresolved_references=len(graph.unresolved_references),
        )

    def get_graph(self, repository_id: str) -> CodeGraph:
        return self._repository.get_graph(repository_id)

    def get_stats(self, repository_id: str) -> GraphStats:
        return self._repository.get_stats(repository_id)

    def delete_graph(self, repository_id: str) -> bool:
        return self._repository.delete_graph(repository_id)

    def _check_ownership(self, repository_id: str, root_path: str) -> None:
        # Two checkouts named "api" must not silently overwrite each other's graph.
        try:
            existing = self._repository.get_stats(repository_id)
        except GraphNotFoundError:
            return
        if existing.root_path is not None and existing.root_path != root_path:
            raise RepositoryIdConflictError(
                f"Repository id {repository_id!r} is already used by another directory. "
                "Pass a different repository_id, or delete that graph first."
            )
