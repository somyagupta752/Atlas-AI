"""Graph retrieval: expand seed entities through the Task 10 knowledge graph.

Seeds come from two places:
- the query itself, by exact name lookup in the graph (``get_user()`` in a bug
  report finds the ``get_user`` Function node), and
- top keyword/vector hits, passed in by the retrieval service.

From each seed, simple paths of up to ``max_depth`` hops are followed in both
directions, never through the Repository node. Relationship direction is kept
on every step, because "who calls X" and "what X calls" answer different
questions.

Neighbors are not equally relevant. Each step has a weight by type and
direction (a direct caller outweighs the containing file), each hop multiplies
again, and an ``inferred`` step (type evidence, not proof) costs a further
factor. An entity reached by several paths keeps its best one.

A query can ask for a call direction: "who calls X" wants callers, "what does
X call" wants callees. When exactly one direction is asked for, CALLS steps in
the other direction weigh half as much. The phrasing rules are small regular
expressions, so the behaviour stays deterministic and explainable.

Score scale (graph results only)::

    seed found by name:  1.5 x name weight
    neighbor:            seed weight x product(step weights) x 0.7 per inferred step
                         (x 0.5 per CALLS step against the requested direction)
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.knowledge_graph.models import CodeGraph, NodeLabel, RelationshipType
from app.knowledge_graph.queries import MAX_EXPANSION_DEPTH
from app.retrieval.models import (
    CodeChunk,
    Confidence,
    GraphEvidence,
    GraphStep,
    RetrievalResult,
    RetrievalSource,
    UnresolvedEvidence,
)
from app.retrieval.text import query_identifiers, strong_identifiers

DEFAULT_RELATIONSHIP_TYPES: tuple[str, ...] = tuple(str(t) for t in RelationshipType)
DEFAULT_MAX_DEPTH = 2
DEFAULT_LIMIT_PER_SEED = 60
SEED_NAME_SCORE = 1.5
WEAK_NAME_FACTOR = 0.6  # a plain word ("average") matched a name, not code-shaped text
INFERRED_FACTOR = 0.7
EXTERNAL_SEED_DECAY = 0.85  # the i-th seed from other strategies (1-based) weighs 0.85 ** i

DISFAVORED_CALL_DIRECTION_FACTOR = 0.5
_CALLER_INTENT = re.compile(
    r"\b(?:who|what|which)\s+(?:calls|uses|invokes)\b|\bcallers?\b|\bcalled\s+(?:by|from)\b"
    r"|\bused\s+by\b|\bwhere\s+is\s+\S+\s+(?:called|used)\b",
    re.IGNORECASE,
)
_CALLEE_INTENT = re.compile(
    r"\bwhat\s+does\s+.+?\s+(?:call|use|invoke|depend\s+on)\b|\bcallees?\b"
    r"|\bdependenc(?:y|ies)\s+of\b|\bdepends\s+on\b|\bcalls\s+made\s+by\b",
    re.IGNORECASE,
)


def call_direction_preference(query: str) -> str | None:
    """``"incoming"`` for caller questions, ``"outgoing"`` for callee questions, else None."""
    callers = bool(_CALLER_INTENT.search(query))
    callees = bool(_CALLEE_INTENT.search(query))
    if callers == callees:
        return None
    return "incoming" if callers else "outgoing"


# (relationship type, traversal direction) -> (relation name, weight)
STEP_WEIGHTS: dict[tuple[str, str], tuple[str, float]] = {
    ("CALLS", "incoming"): ("caller", 1.0),
    ("CALLS", "outgoing"): ("callee", 0.9),
    ("HAS_METHOD", "incoming"): ("containing class", 0.8),
    ("HAS_METHOD", "outgoing"): ("method", 0.6),
    ("EXTENDS", "outgoing"): ("base class", 0.8),
    ("EXTENDS", "incoming"): ("subclass", 0.7),
    ("IMPORTS", "incoming"): ("importing file", 0.6),
    ("IMPORTS", "outgoing"): ("imported file", 0.5),
    ("CONTAINS", "incoming"): ("containing file", 0.5),
    ("CONTAINS", "outgoing"): ("contained entity", 0.4),
}


@dataclass(frozen=True)
class GraphEntity:
    id: str
    label: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RawPath:
    seed_id: str
    node_ids: list[str]
    steps: list[dict[str, Any]]  # type, source_id, target_id, resolution_status
    entity: GraphEntity


class GraphContextSource(Protocol):
    """Read access to one store's graph. Every call is scoped to ``repository_id``."""

    def find_entities_by_name(
        self, repository_id: str, names: list[str], limit: int
    ) -> list[dict[str, Any]]: ...

    def expand_neighborhood(
        self,
        repository_id: str,
        seed_ids: list[str],
        max_depth: int,
        relationship_types: list[str],
        limit_per_seed: int,
    ) -> list[dict[str, Any]]: ...

    def unresolved_for_entities(
        self, repository_id: str, entity_ids: list[str], limit: int
    ) -> list[dict[str, Any]]: ...


class ChunkLookup(Protocol):
    def chunks_for_entities(self, repository_id: str, entity_ids: Iterable[str]) -> list[CodeChunk]: ...


@dataclass
class RelatedEntity:
    entity: GraphEntity
    evidence: GraphEvidence
    supporting_paths: int = 1


@dataclass
class GraphSearchOutcome:
    results: list[RetrievalResult]
    seeds: list[str]
    # Graph entities that ranked but have no indexed chunk (e.g. graph newer than index).
    entities_without_chunks: list[str]


class GraphSearcher:
    def __init__(self, source: GraphContextSource, chunks: ChunkLookup) -> None:
        self._source = source
        self._chunks = chunks

    # --- building blocks ---------------------------------------------------

    def find_seeds(self, repository_id: str, query: str, limit: int = 10) -> dict[str, float]:
        """Entities named in the query, with a weight: code-shaped names count more."""
        names = query_identifiers(query)
        if not names:
            return {}
        strong = strong_identifiers(query)
        records = self._source.find_entities_by_name(repository_id, names, limit)
        seeds: dict[str, float] = {}
        for record in records:
            properties = record.get("properties") or {}
            candidates = {
                str(properties.get(key, "")).lower()
                for key in ("name", "qualified_name", "module")
                if properties.get(key)
            }
            weight = 1.0 if candidates & strong else WEAK_NAME_FACTOR
            seeds[record["id"]] = max(weight, seeds.get(record["id"], 0.0))
        return seeds

    def get_related_entities(
        self,
        repository_id: str,
        entity_ids: str | Sequence[str] | dict[str, float],
        max_depth: int = DEFAULT_MAX_DEPTH,
        relationship_types: Sequence[str] = DEFAULT_RELATIONSHIP_TYPES,
        limit_per_seed: int = DEFAULT_LIMIT_PER_SEED,
        exclude: Iterable[str] | None = None,
        prefer_call_direction: str | None = None,
    ) -> list[RelatedEntity]:
        """Neighbors of the seeds, best path per entity, highest score first.

        ``entity_ids`` may map each seed to a weight; otherwise every seed
        weighs 1.0. Entities in ``exclude`` are not returned; by default that is
        every seed. A seed can still be returned as a neighbor of another seed
        when it is not excluded, which is how graph search confirms a
        relationship between two hits from other strategies.
        """
        if not 1 <= max_depth <= MAX_EXPANSION_DEPTH:
            raise ValueError(f"max_depth must be between 1 and {MAX_EXPANSION_DEPTH}")
        seeds = _seed_weights(entity_ids)
        if not seeds:
            return []
        records = self._source.expand_neighborhood(
            repository_id, list(seeds), max_depth, list(relationship_types), limit_per_seed
        )

        excluded = set(seeds) if exclude is None else set(exclude)
        best: dict[str, RelatedEntity] = {}
        for record in records:
            path = _raw_path(record)
            if path.entity.id in excluded or path.entity.id == path.seed_id:
                continue
            evidence = _score_path(path, seeds[path.seed_id], prefer_call_direction)
            if evidence is None:
                continue
            current = best.get(path.entity.id)
            if current is None:
                best[path.entity.id] = RelatedEntity(path.entity, evidence)
            else:
                current.supporting_paths += 1
                if _better(evidence, current.evidence):
                    current.entity, current.evidence = path.entity, evidence
        return sorted(best.values(), key=lambda r: (-r.evidence.score, r.evidence.depth, r.entity.id))

    def unresolved_references(
        self, repository_id: str, entity_ids: Iterable[str], limit: int = 200
    ) -> dict[str, list[UnresolvedEvidence]]:
        ids = sorted(set(entity_ids))
        if not ids:
            return {}
        grouped: dict[str, list[UnresolvedEvidence]] = defaultdict(list)
        for record in self._source.unresolved_for_entities(repository_id, ids, limit):
            grouped[record["source_id"]].append(
                UnresolvedEvidence(
                    source_entity_id=record["source_id"],
                    kind=str(record["kind"]),
                    reason=str(record["reason"]),
                    expression=str(record["expression"]),
                    line_number=int(record["line_number"]),
                )
            )
        return dict(grouped)

    # --- the strategy --------------------------------------------------------

    def search(
        self,
        repository_id: str,
        query: str,
        limit: int = 20,
        seed_entity_ids: Sequence[str] = (),
        max_depth: int = DEFAULT_MAX_DEPTH,
        relationship_types: Sequence[str] = DEFAULT_RELATIONSHIP_TYPES,
    ) -> GraphSearchOutcome:
        """Seeds named in the query, then their neighborhood, as ranked chunks.

        Seeds found by name are results themselves (the graph found them).
        Seeds handed in from other strategies are not results on their own:
        they appear only when the graph relates them to another seed, so graph
        search adds relationship evidence rather than echoing other strategies.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        named = self.find_seeds(repository_id, query) if query and query.strip() else {}
        seeds = dict(named)
        for position, entity_id in enumerate(dict.fromkeys(seed_entity_ids), start=1):
            seeds.setdefault(entity_id, EXTERNAL_SEED_DECAY**position)
        if not seeds:
            return GraphSearchOutcome([], [], [])

        related = self.get_related_entities(
            repository_id,
            seeds,
            max_depth,
            relationship_types,
            exclude=named,
            prefer_call_direction=call_direction_preference(query or ""),
        )

        ranked: list[tuple[str, float, list[GraphEvidence]]] = [
            (
                entity_id,
                SEED_NAME_SCORE * weight,
                [
                    GraphEvidence(
                        seed_entity_id=entity_id,
                        entity_id=entity_id,
                        relation="named in query",
                        depth=0,
                        confidence=Confidence.CONFIRMED,
                        score=SEED_NAME_SCORE * weight,
                    )
                ],
            )
            for entity_id, weight in named.items()
        ]
        ranked += [(r.entity.id, r.evidence.score, [r.evidence]) for r in related]
        ranked.sort(key=lambda item: (-item[1], item[0]))

        top_ids = [entity_id for entity_id, _, _ in ranked[: limit * 2]]
        chunk_by_entity = _first_chunk_per_entity(
            self._chunks.chunks_for_entities(repository_id, top_ids)
        )
        unresolved = self.unresolved_references(repository_id, top_ids)

        results: list[RetrievalResult] = []
        missing: list[str] = []
        for entity_id, score, evidence in ranked[: limit * 2]:
            chunk = chunk_by_entity.get(entity_id)
            if chunk is None or chunk.repository_id != repository_id:
                missing.append(entity_id)
                continue
            if len(results) >= limit:
                continue
            results.append(
                RetrievalResult(
                    chunk_id=chunk.id,
                    repository_id=chunk.repository_id,
                    file_path=chunk.file_path,
                    content=chunk.content,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    retrieval_source=RetrievalSource.GRAPH,
                    score=score,
                    rank=len(results) + 1,
                    qualified_name=chunk.qualified_name,
                    entity_id=entity_id,
                    entity_type=chunk.entity_type,
                    match_reasons=[_describe(e) for e in evidence],
                    graph_evidence=evidence,
                    unresolved_references=unresolved.get(entity_id, []),
                )
            )
        return GraphSearchOutcome(results, list(seeds), missing)


# --- in-memory source ----------------------------------------------------------


class InMemoryGraphContextSource:
    """The same reads over a ``CodeGraph``. For tests and offline benchmarks."""

    def __init__(self, graph: CodeGraph) -> None:
        self._graph = graph
        self._nodes = {node.id: node for node in graph.nodes}
        self._edges: dict[str, list[tuple[str, Any]]] = defaultdict(list)
        for rel in sorted(graph.relationships, key=lambda r: r.id):
            self._edges[rel.source_id].append((rel.target_id, rel))
            self._edges[rel.target_id].append((rel.source_id, rel))

    def find_entities_by_name(
        self, repository_id: str, names: list[str], limit: int
    ) -> list[dict[str, Any]]:
        if repository_id != self._graph.repository_id:
            return []
        wanted = {name.lower() for name in names}
        found = []
        for node in sorted(self._nodes.values(), key=lambda n: n.id):
            props = node.properties
            if node.label in (NodeLabel.FUNCTION, NodeLabel.CLASS):
                keys = {str(props.get("name", "")).lower(), str(props.get("qualified_name", "")).lower()}
            elif node.label == NodeLabel.FILE:
                keys = {str(props.get("module", "")).lower()}
            else:
                continue
            if keys & wanted:
                found.append(_record(node))
        return found[:limit]

    def expand_neighborhood(
        self,
        repository_id: str,
        seed_ids: list[str],
        max_depth: int,
        relationship_types: list[str],
        limit_per_seed: int,
    ) -> list[dict[str, Any]]:
        if repository_id != self._graph.repository_id:
            return []
        allowed = set(relationship_types)
        records = []
        for seed_id in seed_ids:
            seed = self._nodes.get(seed_id)
            if seed is None or seed.label == NodeLabel.REPOSITORY:
                continue
            paths = self._simple_paths(seed_id, max_depth, allowed)
            paths.sort(key=lambda p: (len(p[1]), p[0][-1]))
            for node_ids, rels in paths[:limit_per_seed]:
                end = self._nodes[node_ids[-1]]
                records.append(
                    {
                        "seed_id": seed_id,
                        "node_ids": node_ids,
                        "steps": [
                            {
                                "type": str(rel.type),
                                "source_id": rel.source_id,
                                "target_id": rel.target_id,
                                "resolution_status": rel.properties.get("resolution_status"),
                            }
                            for rel in rels
                        ],
                        "entity_id": end.id,
                        "label": str(end.label),
                        "properties": dict(end.properties),
                    }
                )
        return records

    def _simple_paths(
        self, seed_id: str, max_depth: int, allowed: set[str]
    ) -> list[tuple[list[str], list[Any]]]:
        """Every cycle-free path of 1..max_depth hops, never through a Repository node."""
        paths: list[tuple[list[str], list[Any]]] = []
        stack: list[tuple[list[str], list[Any]]] = [([seed_id], [])]
        while stack:
            node_ids, rels = stack.pop()
            if rels:
                paths.append((node_ids, rels))
            if len(rels) == max_depth:
                continue
            for neighbor_id, rel in self._edges.get(node_ids[-1], []):
                if (
                    str(rel.type) not in allowed
                    or self._nodes[neighbor_id].label == NodeLabel.REPOSITORY
                    or neighbor_id in node_ids  # simple paths only: no cycles
                ):
                    continue
                stack.append(([*node_ids, neighbor_id], [*rels, rel]))
        return paths

    def unresolved_for_entities(
        self, repository_id: str, entity_ids: list[str], limit: int
    ) -> list[dict[str, Any]]:
        if repository_id != self._graph.repository_id:
            return []
        wanted = set(entity_ids)
        refs = sorted(
            (u for u in self._graph.unresolved_references if u.source_id in wanted),
            key=lambda u: (u.source_id, u.line_number, u.id),
        )
        return [
            {
                "source_id": u.source_id,
                "kind": str(u.kind),
                "reason": str(u.reason),
                "expression": u.expression,
                "line_number": u.line_number,
            }
            for u in refs[:limit]
        ]


# --- helpers ---------------------------------------------------------------------


def _record(node: Any) -> dict[str, Any]:
    return {"id": node.id, "label": str(node.label), "properties": dict(node.properties)}


def _seed_weights(entity_ids: str | Sequence[str] | dict[str, float]) -> dict[str, float]:
    if isinstance(entity_ids, str):
        return {entity_ids: 1.0}
    if isinstance(entity_ids, dict):
        return dict(entity_ids)
    return {entity_id: 1.0 for entity_id in entity_ids}


def _raw_path(record: dict[str, Any]) -> RawPath:
    return RawPath(
        seed_id=record["seed_id"],
        node_ids=list(record["node_ids"]),
        steps=list(record["steps"]),
        entity=GraphEntity(record["entity_id"], record["label"], dict(record.get("properties") or {})),
    )


def _score_path(
    path: RawPath, seed_weight: float, prefer_call_direction: str | None = None
) -> GraphEvidence | None:
    steps: list[GraphStep] = []
    names: list[str] = []
    score = seed_weight
    inferred = False
    for index, step in enumerate(path.steps):
        direction = "outgoing" if step["source_id"] == path.node_ids[index] else "incoming"
        weight_entry = STEP_WEIGHTS.get((step["type"], direction))
        if weight_entry is None:
            return None
        name, weight = weight_entry
        status = step.get("resolution_status")
        score *= weight
        if prefer_call_direction and step["type"] == "CALLS" and direction != prefer_call_direction:
            score *= DISFAVORED_CALL_DIRECTION_FACTOR
        if status == "inferred":
            score *= INFERRED_FACTOR
            inferred = True
        names.append(name)
        steps.append(
            GraphStep(
                relationship_type=step["type"],
                source_id=step["source_id"],
                target_id=step["target_id"],
                direction=direction,
                resolution_status=status,
            )
        )
    return GraphEvidence(
        seed_entity_id=path.seed_id,
        entity_id=path.entity.id,
        relation=" -> ".join(names),
        depth=len(steps),
        confidence=Confidence.INFERRED if inferred else Confidence.CONFIRMED,
        path=steps,
        score=score,
    )


def _better(candidate: GraphEvidence, current: GraphEvidence) -> bool:
    return (-candidate.score, candidate.depth, candidate.seed_entity_id) < (
        -current.score,
        current.depth,
        current.seed_entity_id,
    )


def _first_chunk_per_entity(chunks: list[CodeChunk]) -> dict[str, CodeChunk]:
    """An entity split into parts is represented by its first part (the signature)."""
    first: dict[str, CodeChunk] = {}
    for chunk in sorted(chunks, key=lambda c: (c.start_line, c.id)):
        if chunk.entity_id and chunk.entity_id not in first:
            first[chunk.entity_id] = chunk
    return first


def _short(entity_id: str) -> str:
    return entity_id.split("::", 1)[-1] if "::" in entity_id else entity_id.rsplit(":", 1)[-1]


def _describe(evidence: GraphEvidence) -> str:
    if evidence.depth == 0:
        return "graph entity named in the query"
    return (
        f"graph: {evidence.relation} of {_short(evidence.seed_entity_id)} "
        f"({evidence.depth} hop{'s' if evidence.depth > 1 else ''}, {evidence.confidence})"
    )


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_RELATIONSHIP_TYPES",
    "GraphContextSource",
    "GraphSearchOutcome",
    "GraphSearcher",
    "InMemoryGraphContextSource",
    "RelatedEntity",
]
