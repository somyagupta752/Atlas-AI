"""Integrity checks for a ``CodeGraph``.

The validator reports problems and never repairs them: an invalid relationship
is listed, not dropped. Persistence refuses to store a graph that has any error.

The checks do not trust that models were built through Pydantic validation,
because ``model_construct`` bypasses it. Labels and types are re-checked here.
"""

from collections import Counter
from enum import StrEnum

from pydantic import BaseModel, Field

from app.knowledge_graph.models import (
    CodeGraph,
    NodeLabel,
    RelationshipType,
    repository_node_id,
)

REQUIRED_NODE_PROPERTIES: dict[NodeLabel, frozenset[str]] = {
    NodeLabel.REPOSITORY: frozenset({"repository_id", "name", "root_path"}),
    NodeLabel.FILE: frozenset({"repository_id", "path", "language"}),
    NodeLabel.CLASS: frozenset(
        {"repository_id", "file_path", "name", "qualified_name", "line_start", "line_end"}
    ),
    NodeLabel.FUNCTION: frozenset(
        {"repository_id", "file_path", "name", "qualified_name", "kind", "line_start", "line_end"}
    ),
}

REQUIRED_RELATIONSHIP_PROPERTIES: dict[RelationshipType, frozenset[str]] = {
    RelationshipType.CONTAINS: frozenset(),
    RelationshipType.HAS_METHOD: frozenset(),
    RelationshipType.IMPORTS: frozenset({"line_number", "import_name", "resolution_status"}),
    RelationshipType.CALLS: frozenset({"line_number", "call_expression", "resolution_status"}),
    RelationshipType.EXTENDS: frozenset({"line_number", "base_expression", "resolution_status"}),
}

ALLOWED_ENDPOINTS: dict[RelationshipType, frozenset[tuple[NodeLabel, NodeLabel]]] = {
    RelationshipType.CONTAINS: frozenset(
        {
            (NodeLabel.REPOSITORY, NodeLabel.FILE),
            (NodeLabel.FILE, NodeLabel.CLASS),
            (NodeLabel.FILE, NodeLabel.FUNCTION),
        }
    ),
    RelationshipType.HAS_METHOD: frozenset({(NodeLabel.CLASS, NodeLabel.FUNCTION)}),
    RelationshipType.IMPORTS: frozenset({(NodeLabel.FILE, NodeLabel.FILE)}),
    RelationshipType.CALLS: frozenset({(NodeLabel.FUNCTION, NodeLabel.FUNCTION)}),
    RelationshipType.EXTENDS: frozenset({(NodeLabel.CLASS, NodeLabel.CLASS)}),
}

# Recursion is real, so a function may call itself. Nothing else may point at itself.
SELF_REFERENCE_ALLOWED = frozenset({RelationshipType.CALLS})


class ValidationErrorCode(StrEnum):
    DUPLICATE_NODE_ID = "DUPLICATE_NODE_ID"
    DUPLICATE_RELATIONSHIP_ID = "DUPLICATE_RELATIONSHIP_ID"
    DUPLICATE_REFERENCE_ID = "DUPLICATE_REFERENCE_ID"
    MISSING_SOURCE_NODE = "MISSING_SOURCE_NODE"
    MISSING_TARGET_NODE = "MISSING_TARGET_NODE"
    MISSING_REFERENCE_SOURCE = "MISSING_REFERENCE_SOURCE"
    INVALID_NODE_LABEL = "INVALID_NODE_LABEL"
    INVALID_RELATIONSHIP_TYPE = "INVALID_RELATIONSHIP_TYPE"
    INVALID_RELATIONSHIP_ENDPOINTS = "INVALID_RELATIONSHIP_ENDPOINTS"
    SELF_REFERENCE = "SELF_REFERENCE"
    MISSING_REQUIRED_PROPERTY = "MISSING_REQUIRED_PROPERTY"
    MISSING_REPOSITORY_NODE = "MISSING_REPOSITORY_NODE"
    REPOSITORY_MISMATCH = "REPOSITORY_MISMATCH"


class GraphValidationError(BaseModel):
    code: ValidationErrorCode
    message: str
    node_id: str | None = None
    relationship_id: str | None = None
    reference_id: str | None = None


class GraphValidationResult(BaseModel):
    valid: bool
    errors: list[GraphValidationError] = Field(default_factory=list)


def validate_code_graph(graph: CodeGraph) -> GraphValidationResult:
    """Check identity, referential integrity, schema and repository ownership."""
    errors: list[GraphValidationError] = []
    labels: dict[str, str] = {}

    for node_id, count in Counter(n.id for n in graph.nodes).items():
        if count > 1:
            errors.append(_error("DUPLICATE_NODE_ID", f"Node id used {count} times", node=node_id))

    for node in graph.nodes:
        labels.setdefault(node.id, str(node.label))
        if node.label not in NodeLabel.__members__.values():
            errors.append(_error("INVALID_NODE_LABEL", f"Unknown label {node.label!r}", node=node.id))
            continue
        missing = REQUIRED_NODE_PROPERTIES[NodeLabel(node.label)] - node.properties.keys()
        if missing:
            errors.append(
                _error(
                    "MISSING_REQUIRED_PROPERTY",
                    f"{node.label} node is missing {sorted(missing)}",
                    node=node.id,
                )
            )
        owner = node.properties.get("repository_id")
        if owner is not None and owner != graph.repository_id:
            errors.append(
                _error(
                    "REPOSITORY_MISMATCH",
                    f"Node belongs to repository {owner!r}, not {graph.repository_id!r}",
                    node=node.id,
                )
            )

    expected_root = repository_node_id(graph.repository_id)
    if labels.get(expected_root) != NodeLabel.REPOSITORY:
        errors.append(
            _error(
                "MISSING_REPOSITORY_NODE",
                f"Graph has no Repository node {expected_root!r}",
                node=expected_root,
            )
        )

    for rel_id, count in Counter(r.id for r in graph.relationships).items():
        if count > 1:
            errors.append(
                _error("DUPLICATE_RELATIONSHIP_ID", f"Relationship id used {count} times", rel=rel_id)
            )

    for rel in graph.relationships:
        errors.extend(_check_relationship(rel.id, rel.type, rel.source_id, rel.target_id, labels))
        if rel.type in REQUIRED_RELATIONSHIP_PROPERTIES:
            missing = REQUIRED_RELATIONSHIP_PROPERTIES[RelationshipType(rel.type)]
            missing = missing - rel.properties.keys()
            if missing:
                errors.append(
                    _error(
                        "MISSING_REQUIRED_PROPERTY",
                        f"{rel.type} relationship is missing {sorted(missing)}",
                        rel=rel.id,
                    )
                )

    for ref_id, count in Counter(u.id for u in graph.unresolved_references).items():
        if count > 1:
            errors.append(
                _error("DUPLICATE_REFERENCE_ID", f"Reference id used {count} times", ref=ref_id)
            )
    for ref in graph.unresolved_references:
        if ref.source_id not in labels:
            errors.append(
                _error(
                    "MISSING_REFERENCE_SOURCE",
                    f"Unresolved reference source {ref.source_id!r} does not exist",
                    ref=ref.id,
                )
            )

    return GraphValidationResult(valid=not errors, errors=errors)


def _check_relationship(
    rel_id: str, rel_type: str, source_id: str, target_id: str, labels: dict[str, str]
) -> list[GraphValidationError]:
    errors: list[GraphValidationError] = []
    if rel_type not in RelationshipType.__members__.values():
        return [_error("INVALID_RELATIONSHIP_TYPE", f"Unknown type {rel_type!r}", rel=rel_id)]
    relationship_type = RelationshipType(rel_type)

    source_label = labels.get(source_id)
    target_label = labels.get(target_id)
    if source_label is None:
        errors.append(
            _error("MISSING_SOURCE_NODE", f"Relationship source {source_id!r} does not exist", rel=rel_id)
        )
    if target_label is None:
        errors.append(
            _error("MISSING_TARGET_NODE", f"Relationship target {target_id!r} does not exist", rel=rel_id)
        )
    if source_id == target_id and relationship_type not in SELF_REFERENCE_ALLOWED:
        errors.append(
            _error("SELF_REFERENCE", f"{rel_type} relationship points at its own source", rel=rel_id)
        )
    if source_label is not None and target_label is not None:
        endpoints = (source_label, target_label)
        if endpoints not in ALLOWED_ENDPOINTS[relationship_type]:
            errors.append(
                _error(
                    "INVALID_RELATIONSHIP_ENDPOINTS",
                    f"{rel_type} cannot connect {source_label} to {target_label}",
                    rel=rel_id,
                )
            )
    return errors


def _error(
    code: str,
    message: str,
    *,
    node: str | None = None,
    rel: str | None = None,
    ref: str | None = None,
) -> GraphValidationError:
    return GraphValidationError(
        code=ValidationErrorCode(code),
        message=message,
        node_id=node,
        relationship_id=rel,
        reference_id=ref,
    )
