"""Graph persistence contract, plus an in-memory implementation for tests.

Every implementation must follow the same rules:
- ``save_graph`` validates first and refuses an invalid graph.
- Saving *replaces* the repository's graph. Stale nodes, relationships and
  unresolved references from the previous build disappear.
- Replacement is all-or-nothing. If the save fails, the previous graph stays.
"""

from collections import Counter
from threading import Lock
from typing import Protocol

from pydantic import BaseModel, Field

from app.knowledge_graph.models import CodeGraph, repository_node_id
from app.knowledge_graph.validator import GraphValidationResult, validate_code_graph


class GraphNotFoundError(Exception):
    """No graph is stored for the repository."""


class InvalidGraphError(Exception):
    """The graph failed validation and was not stored."""

    def __init__(self, result: GraphValidationResult) -> None:
        super().__init__(f"Graph failed validation with {len(result.errors)} error(s)")
        self.result = result


class GraphStoreError(Exception):
    """The graph store failed or is unreachable. Messages never contain credentials."""


class SaveResult(BaseModel):
    nodes_created: int = 0
    nodes_deleted: int = 0
    relationships_created: int = 0
    relationships_deleted: int = 0
    unresolved_created: int = 0
    unresolved_deleted: int = 0


class GraphStats(BaseModel):
    repository_id: str
    root_path: str | None = None
    total_nodes: int
    total_relationships: int
    unresolved_references: int
    nodes_by_label: dict[str, int] = Field(default_factory=dict)
    relationships_by_type: dict[str, int] = Field(default_factory=dict)
    unresolved_by_reason: dict[str, int] = Field(default_factory=dict)


class GraphRepository(Protocol):
    def save_graph(self, graph: CodeGraph) -> SaveResult:
        """Validate and atomically replace the stored graph for ``graph.repository_id``."""
        ...

    def get_graph(self, repository_id: str) -> CodeGraph:
        """Raise ``GraphNotFoundError`` if nothing is stored."""
        ...

    def get_stats(self, repository_id: str) -> GraphStats:
        """Raise ``GraphNotFoundError`` if nothing is stored."""
        ...

    def delete_graph(self, repository_id: str) -> bool:
        """Delete the graph. Return False if there was nothing to delete."""
        ...


def ensure_valid(graph: CodeGraph) -> None:
    result = validate_code_graph(graph)
    if not result.valid:
        raise InvalidGraphError(result)


def compute_stats(graph: CodeGraph) -> GraphStats:
    """Count a graph's contents. Shared by every repository implementation."""
    root = next(
        (n for n in graph.nodes if n.id == repository_node_id(graph.repository_id)), None
    )
    root_path = root.properties.get("root_path") if root else None
    return GraphStats(
        repository_id=graph.repository_id,
        root_path=root_path if isinstance(root_path, str) else None,
        total_nodes=len(graph.nodes),
        total_relationships=len(graph.relationships),
        unresolved_references=len(graph.unresolved_references),
        nodes_by_label=dict(sorted(Counter(str(n.label) for n in graph.nodes).items())),
        relationships_by_type=dict(
            sorted(Counter(str(r.type) for r in graph.relationships).items())
        ),
        unresolved_by_reason=dict(
            sorted(Counter(str(u.reason) for u in graph.unresolved_references).items())
        ),
    )


class InMemoryGraphRepository:
    """Thread-safe dict-backed repository. Stores deep copies, so callers cannot mutate it."""

    def __init__(self) -> None:
        self._graphs: dict[str, CodeGraph] = {}
        self._lock = Lock()

    def save_graph(self, graph: CodeGraph) -> SaveResult:
        ensure_valid(graph)
        replacement = graph.model_copy(deep=True)
        with self._lock:
            previous = self._graphs.get(graph.repository_id)
            self._graphs[graph.repository_id] = replacement  # single assignment: atomic
        return _diff(previous, replacement)

    def get_graph(self, repository_id: str) -> CodeGraph:
        with self._lock:
            graph = self._graphs.get(repository_id)
        if graph is None:
            raise GraphNotFoundError(repository_id)
        return graph.model_copy(deep=True)

    def get_stats(self, repository_id: str) -> GraphStats:
        return compute_stats(self.get_graph(repository_id))

    def delete_graph(self, repository_id: str) -> bool:
        with self._lock:
            return self._graphs.pop(repository_id, None) is not None


def _diff(previous: CodeGraph | None, current: CodeGraph) -> SaveResult:
    def ids(items: list) -> set[str]:
        return {item.id for item in items}

    old_nodes = ids(previous.nodes) if previous else set()
    old_rels = ids(previous.relationships) if previous else set()
    old_refs = ids(previous.unresolved_references) if previous else set()
    new_nodes = ids(current.nodes)
    new_rels = ids(current.relationships)
    new_refs = ids(current.unresolved_references)
    return SaveResult(
        nodes_created=len(new_nodes - old_nodes),
        nodes_deleted=len(old_nodes - new_nodes),
        relationships_created=len(new_rels - old_rels),
        relationships_deleted=len(old_rels - new_rels),
        unresolved_created=len(new_refs - old_refs),
        unresolved_deleted=len(old_refs - new_refs),
    )


__all__ = [
    "GraphNotFoundError",
    "GraphRepository",
    "GraphStats",
    "GraphStoreError",
    "InMemoryGraphRepository",
    "InvalidGraphError",
    "SaveResult",
    "compute_stats",
    "ensure_valid",
]
