from app.knowledge_graph.models import (
    CodeGraph,
    GraphNode,
    GraphRelationship,
    NodeLabel,
    ReferenceKind,
    RelationshipType,
    UnresolvedReason,
    UnresolvedReference,
)
from app.knowledge_graph.validator import validate_code_graph

REPO = GraphNode(
    id="repository:demo",
    label=NodeLabel.REPOSITORY,
    properties={"repository_id": "demo", "name": "demo", "root_path": "/demo"},
)
FILE = GraphNode(
    id="file:demo:a.py",
    label=NodeLabel.FILE,
    properties={"repository_id": "demo", "path": "a.py", "language": "python"},
)


def _function(name: str) -> GraphNode:
    return GraphNode(
        id=f"function:demo:a.py::{name}",
        label=NodeLabel.FUNCTION,
        properties={
            "repository_id": "demo",
            "file_path": "a.py",
            "name": name,
            "qualified_name": name,
            "kind": "function",
            "line_start": 1,
            "line_end": 2,
        },
    )


F, G = _function("f"), _function("g")


def _call(rel_id: str, source: str, target: str, **overrides) -> GraphRelationship:
    properties = {"line_number": 1, "call_expression": "g", "resolution_status": "resolved"}
    properties.update(overrides)
    return GraphRelationship(
        id=rel_id, type=RelationshipType.CALLS, source_id=source, target_id=target,
        properties=properties,
    )


def _graph(nodes=None, relationships=None, references=None) -> CodeGraph:
    return CodeGraph(
        repository_id="demo",
        nodes=nodes if nodes is not None else [REPO, FILE, F, G],
        relationships=relationships or [],
        unresolved_references=references or [],
    )


def _codes(graph: CodeGraph) -> list[str]:
    return [error.code for error in validate_code_graph(graph).errors]


def test_valid_graph():
    graph = _graph(relationships=[_call("c1", F.id, G.id)])

    result = validate_code_graph(graph)

    assert result.valid
    assert result.errors == []


def test_duplicate_node_and_relationship_ids():
    graph = _graph(
        nodes=[REPO, FILE, F, G, F],
        relationships=[_call("c1", F.id, G.id), _call("c1", G.id, F.id)],
    )

    assert _codes(graph) == ["DUPLICATE_NODE_ID", "DUPLICATE_RELATIONSHIP_ID"]


def test_missing_source_and_target_nodes_are_reported_not_removed():
    rel = _call("rel-123", "function:demo:a.py::ghost", "function:demo:a.py::phantom")
    graph = _graph(relationships=[rel])

    result = validate_code_graph(graph)

    assert not result.valid
    assert [(e.code, e.relationship_id) for e in result.errors] == [
        ("MISSING_SOURCE_NODE", "rel-123"),
        ("MISSING_TARGET_NODE", "rel-123"),
    ]
    assert graph.relationships == [rel]  # nothing was silently dropped


def test_invalid_endpoints_for_relationship_type():
    rel = GraphRelationship(
        id="bad", type=RelationshipType.HAS_METHOD, source_id=FILE.id, target_id=F.id
    )

    assert _codes(_graph(relationships=[rel])) == ["INVALID_RELATIONSHIP_ENDPOINTS"]


def test_self_reference_rejected_except_for_recursion():
    imports_self = GraphRelationship(
        id="loop", type=RelationshipType.IMPORTS, source_id=FILE.id, target_id=FILE.id,
        properties={"line_number": 1, "import_name": "a", "resolution_status": "resolved"},
    )
    recursion = _call("rec", F.id, F.id)

    assert _codes(_graph(relationships=[imports_self, recursion])) == ["SELF_REFERENCE"]


def test_labels_and_types_bypassing_pydantic_are_caught():
    bogus_node = GraphNode.model_construct(id="x", label="Variable", properties={})
    bogus_rel = GraphRelationship.model_construct(
        id="r", type="USES", source_id=F.id, target_id=G.id, properties={}
    )

    assert _codes(_graph(nodes=[REPO, FILE, F, G, bogus_node], relationships=[bogus_rel])) == [
        "INVALID_NODE_LABEL",
        "INVALID_RELATIONSHIP_TYPE",
    ]


def test_missing_required_properties():
    bare_class = GraphNode(id="class:demo:a.py::C", label=NodeLabel.CLASS, properties={"name": "C"})
    bare_call = GraphRelationship(
        id="c", type=RelationshipType.CALLS, source_id=F.id, target_id=G.id
    )

    errors = validate_code_graph(
        _graph(nodes=[REPO, FILE, F, G, bare_class], relationships=[bare_call])
    ).errors

    assert [(e.code, e.node_id or e.relationship_id) for e in errors] == [
        ("MISSING_REQUIRED_PROPERTY", "class:demo:a.py::C"),
        ("MISSING_REQUIRED_PROPERTY", "c"),
    ]
    assert "qualified_name" in errors[0].message


def test_missing_repository_node_and_foreign_nodes():
    foreign = GraphNode(
        id="file:other:a.py",
        label=NodeLabel.FILE,
        properties={"repository_id": "other", "path": "a.py", "language": "python"},
    )

    assert _codes(_graph(nodes=[FILE, foreign])) == ["REPOSITORY_MISMATCH", "MISSING_REPOSITORY_NODE"]


def test_unresolved_reference_integrity():
    ref = UnresolvedReference(
        id="u1", kind=ReferenceKind.CALL, reason=UnresolvedReason.BUILTIN,
        source_id="function:demo:a.py::ghost", file_path="a.py", line_number=1, expression="print",
    )

    assert _codes(_graph(references=[ref, ref])) == [
        "DUPLICATE_REFERENCE_ID",
        "MISSING_REFERENCE_SOURCE",
        "MISSING_REFERENCE_SOURCE",
    ]


def test_built_graphs_validate(make_graph):
    graph = make_graph(
        {
            "a.py": "import b\n\nclass A(b.B):\n    def go(self):\n        self.go()\n",
            "b.py": "class B:\n    pass\n",
        }
    )

    assert validate_code_graph(graph).valid
