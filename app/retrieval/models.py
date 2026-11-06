"""Data models for code retrieval.

Three layers, kept apart on purpose:

- ``CodeChunk``: what was indexed. Identity, location and source. No scores.
- ``RetrievalResult``: one candidate from one strategy, with that strategy's
  rank and score and the reasons it matched.
- ``RetrievedEvidence``: a fused, ranked result with every strategy's
  provenance. Relevance is not proof of cause, and nothing here claims it is.

Chunk IDs derive from graph entity IDs, so they are stable for unchanged source
and never collide across repositories::

    chunk:function:atlas-ai:app/services/user.py::UserService.get_user
    chunk:file:atlas-ai:app/services/user.py            (module-level code)
    chunk:function:atlas-ai:app/big.py::handler#part2   (a split, oversized function)
"""

import hashlib
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.knowledge_graph.models import RepositoryId


class ChunkType(StrEnum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    NESTED_FUNCTION = "nested_function"


class RetrievalSource(StrEnum):
    KEYWORD = "keyword"
    VECTOR = "vector"
    GRAPH = "graph"


class Confidence(StrEnum):
    CONFIRMED = "confirmed"  # every step is structural or resolved from definitions/imports
    INFERRED = "inferred"  # at least one step relies on type evidence


def chunk_id_for_entity(entity_id: str, part: int | None = None) -> str:
    base = f"chunk:{entity_id}"
    return f"{base}#part{part}" if part and part > 1 else base


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class CodeChunk(BaseModel):
    """One searchable unit of source code. Line numbers are 1-based and inclusive."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    repository_id: RepositoryId
    file_path: str = Field(min_length=1)
    entity_id: str | None = None  # the Task 10 graph node this chunk belongs to
    entity_type: ChunkType
    qualified_name: str | None = None  # "UserService.get_user"; None for module chunks
    content: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _lines_ordered(self) -> "CodeChunk":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self

    @property
    def content_hash(self) -> str:
        return sha256_text(self.content)

    @property
    def symbol_name(self) -> str | None:
        """Last segment of the qualified name: ``get_user`` for ``UserService.get_user``."""
        if not self.qualified_name:
            return None
        return self.qualified_name.rsplit(".", 1)[-1]

    @property
    def dotted_name(self) -> str | None:
        """Import-style name: ``app.services.user.UserService.get_user``."""
        module = self.metadata.get("module")
        if not module:
            return self.qualified_name
        if not self.qualified_name:
            return module
        return f"{module}.{self.qualified_name.replace('.<locals>', '')}"


class GraphStep(BaseModel):
    """One relationship on the path from a seed to a retrieved entity, as stored."""

    relationship_type: str
    source_id: str
    target_id: str
    # "outgoing": traversed source -> target; "incoming": traversed against the arrow.
    direction: Literal["outgoing", "incoming"]
    resolution_status: str | None = None  # resolved | inferred; None for structural edges


class GraphEvidence(BaseModel):
    """Why graph search retrieved an entity: the seed it came from and the path."""

    seed_entity_id: str
    entity_id: str
    relation: str  # e.g. "caller", "callee", "containing class", "importing file"
    depth: int = Field(ge=0)
    confidence: Confidence
    path: list[GraphStep] = Field(default_factory=list)
    score: float = 0.0


class UnresolvedEvidence(BaseModel):
    """A reference in a retrieved entity that the graph could not resolve (Task 10)."""

    source_entity_id: str
    kind: str
    reason: str
    expression: str
    line_number: int


class RetrievalResult(BaseModel):
    """One candidate returned by a single retrieval strategy."""

    chunk_id: str
    repository_id: str
    file_path: str
    content: str
    start_line: int
    end_line: int
    retrieval_source: RetrievalSource
    score: float  # the strategy's own scale; documented per strategy, never compared across
    rank: int = Field(default=0, ge=0)  # 1-based position in the strategy's list
    qualified_name: str | None = None
    entity_id: str | None = None
    entity_type: ChunkType | None = None
    match_reasons: list[str] = Field(default_factory=list)
    graph_evidence: list[GraphEvidence] = Field(default_factory=list)
    unresolved_references: list[UnresolvedEvidence] = Field(default_factory=list)


class StrategyRank(BaseModel):
    source: RetrievalSource
    rank: int
    score: float


class MergedEvidence(BaseModel):
    """A lower-ranked result whose lines lie inside a returned chunk.

    Its code is already in the container's ``content``; only its identity and
    provenance are kept, so nothing about why it was retrieved is lost.
    """

    chunk_id: str
    qualified_name: str | None
    start_line: int
    end_line: int
    retrieval_sources: list[RetrievalSource]
    reason: str


class RetrievedEvidence(BaseModel):
    """A final, fused result. ``score`` is the Reciprocal Rank Fusion score."""

    chunk_id: str
    repository_id: str
    file_path: str
    qualified_name: str | None
    entity_id: str | None
    entity_type: ChunkType | None
    start_line: int
    end_line: int
    content: str
    score: float
    retrieval_sources: list[RetrievalSource]
    strategy_ranks: list[StrategyRank]
    reason: str
    match_reasons: list[str] = Field(default_factory=list)
    graph_relationships: list[GraphEvidence] = Field(default_factory=list)
    unresolved_references: list[UnresolvedEvidence] = Field(default_factory=list)
    # Lower-ranked chunks whose lines lie inside this one; folded in, not repeated.
    merged: list[MergedEvidence] = Field(default_factory=list)


class StrategyReport(BaseModel):
    source: RetrievalSource
    status: Literal["ok", "skipped", "unavailable", "error"]
    candidates: int = 0
    detail: str | None = None


EVIDENCE_NOTE = (
    "Results are ranked by retrieval relevance. Relevance is evidence to investigate, "
    "not proof that a chunk causes the reported problem."
)


class RetrievalResponse(BaseModel):
    query: str
    repository_id: str
    results: list[RetrievedEvidence]
    total_results: int
    strategies: list[StrategyReport] = Field(default_factory=list)
    index_version: int | None = None
    note: str = EVIDENCE_NOTE
