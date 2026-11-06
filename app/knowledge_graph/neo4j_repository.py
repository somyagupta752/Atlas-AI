"""Neo4j implementation of ``GraphRepository``.

Atomic replacement. One save is one write transaction:

1. Lock the repository node (MERGE + SET), which serializes concurrent saves.
2. Upsert nodes, relationships and unresolved references in UNWIND batches,
   stamping each with a fresh ``build_id``.
3. Delete everything in the repository that does not carry the new ``build_id``.

The transaction commits only if every step succeeds. On any failure the
transaction rolls back and the previous graph remains, so the old graph is
never deleted before its replacement is ready. MERGE on stable IDs makes
repeated saves idempotent.

The whole graph is held in one transaction. That is fine for typical
repositories. Very large monorepos may need a versioned strategy instead.
"""

from collections.abc import Callable
from threading import Lock
from typing import Any
from uuid import uuid4

from neo4j import Driver, ManagedTransaction
from neo4j.exceptions import DriverError, Neo4jError

from app.knowledge_graph import queries
from app.knowledge_graph.models import (
    CodeGraph,
    GraphNode,
    GraphRelationship,
    NodeLabel,
    RelationshipType,
    UnresolvedReference,
    repository_node_id,
)
from app.knowledge_graph.repository import (
    GraphNotFoundError,
    GraphStats,
    GraphStoreError,
    SaveResult,
    ensure_valid,
)

DEFAULT_BATCH_SIZE = 1000
_INTERNAL_PROPERTIES = frozenset({"id", "build_id"})


class Neo4jGraphRepository:
    def __init__(
        self, driver: Driver, database: str = "neo4j", batch_size: int = DEFAULT_BATCH_SIZE
    ) -> None:
        self._driver = driver
        self._database = database
        self._batch_size = batch_size
        self._schema_ready = False
        self._schema_lock = Lock()

    # --- schema -------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create constraints and indexes once per process. Safe to call repeatedly."""
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self._wrap_errors(), self._driver.session(database=self._database) as session:
                for statement in queries.SCHEMA_STATEMENTS:
                    session.run(statement).consume()
            self._schema_ready = True

    # --- GraphRepository ----------------------------------------------------

    def save_graph(self, graph: CodeGraph) -> SaveResult:
        ensure_valid(graph)  # before touching the database
        self.ensure_schema()
        build_id = uuid4().hex
        return self._write(self._replace_graph, graph, build_id)

    def get_graph(self, repository_id: str) -> CodeGraph:
        def read(tx: ManagedTransaction) -> CodeGraph | None:
            if not _repository_exists(tx, repository_id):
                return None
            params = {"repository_id": repository_id}
            nodes = [
                GraphNode(
                    id=record["id"],
                    label=NodeLabel(record["labels"][0]),
                    properties=_public(record["properties"]),
                )
                for record in tx.run(queries.GET_NODES, params)
            ]
            relationships = [
                GraphRelationship(
                    id=record["id"],
                    type=RelationshipType(record["type"]),
                    source_id=record["source_id"],
                    target_id=record["target_id"],
                    properties=_public(record["properties"]),
                )
                for record in tx.run(queries.GET_RELATIONSHIPS, params)
            ]
            references = [
                UnresolvedReference.model_validate(
                    {k: v for k, v in record["properties"].items() if k not in {"build_id", "repository_id"}}
                )
                for record in tx.run(queries.GET_UNRESOLVED, params)
            ]
            return CodeGraph(
                repository_id=repository_id,
                nodes=nodes,
                relationships=relationships,
                unresolved_references=references,
            )

        graph = self._read(read)
        if graph is None:
            raise GraphNotFoundError(repository_id)
        return graph

    def get_stats(self, repository_id: str) -> GraphStats:
        def read(tx: ManagedTransaction) -> GraphStats | None:
            root = tx.run(
                queries.GET_REPOSITORY_ROOT,
                repository_node_id=repository_node_id(repository_id),
            ).single()
            if root is None:
                return None
            params = {"repository_id": repository_id}
            nodes = {r["label"]: r["count"] for r in tx.run(queries.COUNT_NODES_BY_LABEL, params)}
            rels = {
                r["type"]: r["count"] for r in tx.run(queries.COUNT_RELATIONSHIPS_BY_TYPE, params)
            }
            reasons = {
                r["reason"]: r["count"] for r in tx.run(queries.COUNT_UNRESOLVED_BY_REASON, params)
            }
            return GraphStats(
                repository_id=repository_id,
                root_path=root["root_path"],
                total_nodes=sum(nodes.values()),
                total_relationships=sum(rels.values()),
                unresolved_references=sum(reasons.values()),
                nodes_by_label=nodes,
                relationships_by_type=rels,
                unresolved_by_reason=reasons,
            )

        stats = self._read(read)
        if stats is None:
            raise GraphNotFoundError(repository_id)
        return stats

    def delete_graph(self, repository_id: str) -> bool:
        def delete(tx: ManagedTransaction) -> bool:
            params = {"repository_id": repository_id}
            deleted = tx.run(queries.DELETE_GRAPH_NODES, params).consume().counters.nodes_deleted
            tx.run(queries.DELETE_GRAPH_UNRESOLVED, params).consume()
            return deleted > 0

        return self._write(delete)

    # --- code questions -----------------------------------------------------

    def find_classes(self, repository_id: str) -> list[GraphNode]:
        return self._find_nodes(
            queries.CLASSES_IN_REPOSITORY, repository_node_id=repository_node_id(repository_id)
        )

    def find_methods(self, repository_id: str, qualified_name: str) -> list[GraphNode]:
        return self._find_nodes(
            queries.METHODS_OF_CLASS, repository_id=repository_id, qualified_name=qualified_name
        )

    def find_callers(self, function_id: str) -> list[GraphNode]:
        return self._find_nodes(queries.CALLERS_OF_FUNCTION, function_id=function_id)

    def find_importers(self, file_id: str) -> list[GraphNode]:
        return self._find_nodes(queries.FILES_IMPORTING_FILE, file_id=file_id)

    def find_subclasses(self, class_id: str) -> list[GraphNode]:
        return self._find_nodes(queries.SUBCLASSES_OF_CLASS, class_id=class_id)

    # --- internals ----------------------------------------------------------

    def _replace_graph(
        self, tx: ManagedTransaction, graph: CodeGraph, build_id: str
    ) -> SaveResult:
        # Runs inside execute_write, which may retry it, so it must be idempotent.
        repository_id = graph.repository_id
        result = SaveResult()
        lock = tx.run(
            queries.LOCK_REPOSITORY,
            repository_node_id=repository_node_id(repository_id),
            build_id=build_id,
        ).consume()
        result.nodes_created += lock.counters.nodes_created  # first build creates the root here

        for label in NodeLabel:
            nodes = [
                {"id": n.id, "properties": n.properties} for n in graph.nodes if n.label == label
            ]
            for batch in self._batches(nodes):
                counters = tx.run(
                    queries.UPSERT_NODES[label], nodes=batch, build_id=build_id
                ).consume().counters
                result.nodes_created += counters.nodes_created

        for rel_type in RelationshipType:
            relationships = [
                {
                    "id": r.id,
                    "source_id": r.source_id,
                    "target_id": r.target_id,
                    "properties": r.properties,
                }
                for r in graph.relationships
                if r.type == rel_type
            ]
            for batch in self._batches(relationships):
                cursor = tx.run(
                    queries.UPSERT_RELATIONSHIPS[rel_type], relationships=batch, build_id=build_id
                )
                written = cursor.single(strict=True)["written"]
                if written != len(batch):
                    # Validation makes this unreachable; raising rolls back if it ever happens.
                    raise GraphStoreError(f"Wrote {written} of {len(batch)} {rel_type} relationships")
                result.relationships_created += cursor.consume().counters.relationships_created

        references = [
            {"id": u.id, "properties": u.model_dump(mode="json", exclude={"id"})}
            for u in graph.unresolved_references
        ]
        for batch in self._batches(references):
            counters = tx.run(
                queries.UPSERT_UNRESOLVED,
                references=batch,
                repository_id=repository_id,
                build_id=build_id,
            ).consume().counters
            result.unresolved_created += counters.nodes_created

        params = {"repository_id": repository_id, "build_id": build_id}
        stale_rels = tx.run(queries.DELETE_STALE_RELATIONSHIPS, params).consume().counters
        stale_nodes = tx.run(queries.DELETE_STALE_NODES, params).consume().counters
        stale_refs = tx.run(queries.DELETE_STALE_UNRESOLVED, params).consume().counters
        result.relationships_deleted = (
            stale_rels.relationships_deleted + stale_nodes.relationships_deleted
        )
        result.nodes_deleted = stale_nodes.nodes_deleted
        result.unresolved_deleted = stale_refs.nodes_deleted
        return result

    def _find_nodes(self, query: str, **params: Any) -> list[GraphNode]:
        def read(tx: ManagedTransaction) -> list[GraphNode]:
            nodes = []
            for record in tx.run(query, params):
                node = record["node"]
                label = next(label for label in node.labels if label != "CodeEntity")
                nodes.append(
                    GraphNode(
                        id=node["id"], label=NodeLabel(label), properties=_public(dict(node))
                    )
                )
            return nodes

        return self._read(read)

    def _batches(self, items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        return [items[i : i + self._batch_size] for i in range(0, len(items), self._batch_size)]

    def _read[T](self, work: Callable[[ManagedTransaction], T]) -> T:
        with self._wrap_errors(), self._driver.session(database=self._database) as session:
            return session.execute_read(work)

    def _write[T](self, work: Callable[..., T], *args: Any) -> T:
        with self._wrap_errors(), self._driver.session(database=self._database) as session:
            return session.execute_write(work, *args)

    @staticmethod
    def _wrap_errors() -> "_ErrorWrapper":
        return _ErrorWrapper()


class _ErrorWrapper:
    """Translate driver errors into ``GraphStoreError`` so callers stay driver-agnostic."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> bool:
        if exc is not None and isinstance(exc, DriverError | Neo4jError):
            # Only the type name: driver messages can include server addresses.
            raise GraphStoreError(f"Neo4j operation failed ({type(exc).__name__})") from exc
        return False


def _repository_exists(tx: ManagedTransaction, repository_id: str) -> bool:
    record = tx.run(
        queries.GET_REPOSITORY_ROOT, repository_node_id=repository_node_id(repository_id)
    ).single()
    return record is not None


def _public(properties: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in properties.items() if k not in _INTERNAL_PROPERTIES}
