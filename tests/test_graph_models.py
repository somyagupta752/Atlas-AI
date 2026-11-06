import json

import pytest
from pydantic import ValidationError

from app.knowledge_graph.models import (
    CodeGraph,
    GraphNode,
    GraphRelationship,
    NodeLabel,
    ReferenceKind,
    RelationshipType,
    UnresolvedReason,
    UnresolvedReference,
    class_node_id,
    file_node_id,
    function_node_id,
    relationship_id,
    repository_node_id,
)


def test_valid_node_and_relationship():
    node = GraphNode(id="file:demo:a.py", label=NodeLabel.FILE, properties={"path": "a.py"})
    rel = GraphRelationship(
        id="CONTAINS:x->y", type=RelationshipType.CONTAINS, source_id="x", target_id="y"
    )

    assert node.label == "File"
    assert rel.properties == {}


@pytest.mark.parametrize("bad_id", ["", "   ", " leading", "trailing "])
def test_empty_or_padded_ids_are_rejected(bad_id):
    with pytest.raises(ValidationError):
        GraphNode(id=bad_id, label=NodeLabel.FILE)
    with pytest.raises(ValidationError):
        GraphRelationship(id="r", type=RelationshipType.CALLS, source_id=bad_id, target_id="t")
    with pytest.raises(ValidationError):
        GraphRelationship(id="r", type=RelationshipType.CALLS, source_id="s", target_id=bad_id)


def test_unknown_label_and_type_are_rejected():
    with pytest.raises(ValidationError):
        GraphNode(id="n", label="Variable")
    with pytest.raises(ValidationError):
        GraphRelationship(id="r", type="USES", source_id="s", target_id="t")


@pytest.mark.parametrize(
    "value",
    [None, {"nested": "map"}, [1, "mixed"], [[1]], object()],
)
def test_properties_must_be_neo4j_storable(value):
    with pytest.raises(ValidationError):
        GraphNode(id="n", label=NodeLabel.FILE, properties={"key": value})


@pytest.mark.parametrize(
    "value", ["text", 3, 2.5, True, ["a", "b"], [1, 2], [], [True, False]]
)
def test_storable_property_values_keep_their_type(value):
    node = GraphNode(id="n", label=NodeLabel.FILE, properties={"key": value})

    assert node.properties["key"] == value
    assert type(node.properties["key"]) is type(value)


@pytest.mark.parametrize("key", ["id", "build_id"])
def test_reserved_property_keys_are_rejected(key):
    with pytest.raises(ValidationError, match="reserved"):
        GraphNode(id="n", label=NodeLabel.FILE, properties={key: "x"})


def test_repository_id_format_is_enforced():
    CodeGraph(repository_id="atlas-ai.v2_1")
    for bad in ["", "Atlas", "has space", "-leading", "a" * 101]:
        with pytest.raises(ValidationError):
            CodeGraph(repository_id=bad)


def test_graph_serializes_to_json_and_back():
    graph = CodeGraph(
        repository_id="demo",
        nodes=[GraphNode(id="repository:demo", label=NodeLabel.REPOSITORY, properties={"n": 1})],
        relationships=[
            GraphRelationship(
                id="r", type=RelationshipType.CALLS, source_id="a", target_id="b",
                properties={"line_numbers": [3, 7]},
            )
        ],
        unresolved_references=[
            UnresolvedReference(
                id="u", kind=ReferenceKind.CALL, reason=UnresolvedReason.BUILTIN,
                source_id="a", file_path="a.py", line_number=3, expression="print",
            )
        ],
    )

    payload = graph.model_dump_json()

    assert json.loads(payload)["relationships"][0]["type"] == "CALLS"
    assert CodeGraph.model_validate_json(payload) == graph


def test_identity_scheme_scopes_ids_by_repository_and_file():
    assert repository_node_id("atlas-ai") == "repository:atlas-ai"
    assert file_node_id("atlas-ai", "app/services/user.py") == "file:atlas-ai:app/services/user.py"
    assert (
        class_node_id("atlas-ai", "app/services/user.py", "UserService")
        == "class:atlas-ai:app/services/user.py::UserService"
    )
    assert (
        function_node_id("atlas-ai", "app/services/user.py", "UserService.find_user")
        == "function:atlas-ai:app/services/user.py::UserService.find_user"
    )
    assert class_node_id("a", "x.py", "C") != class_node_id("a", "y.py", "C")
    assert class_node_id("a", "x.py", "C") != class_node_id("b", "x.py", "C")
    assert relationship_id(RelationshipType.CALLS, "s", "t") == "CALLS:s->t"
