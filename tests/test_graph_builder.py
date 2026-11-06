import pytest

from app.code_intelligence.schemas import RepositoryAnalysis
from app.knowledge_graph.builder import build_code_graph, derive_repository_id
from app.knowledge_graph.models import NodeLabel, RelationshipType
from app.knowledge_graph.validator import validate_code_graph

SPEC_EXAMPLE = {
    "app/__init__.py": "",
    "app/api/__init__.py": "",
    "app/api/users.py": """
        from app.services.user_service import UserService

        class UserController:
            def __init__(self):
                self.service = UserService()

            def get_user(self, user_id: int):
                return self.service.find_user(user_id)
        """,
    "app/services/__init__.py": "",
    "app/services/user_service.py": """
        class UserService:
            def find_user(self, user_id: int):
                return {"id": user_id}
        """,
}


def _edges(graph, rel_type):
    def short(node_id):
        return node_id.split(":", 2)[-1]

    return {
        (short(r.source_id), short(r.target_id))
        for r in graph.relationships
        if r.type == rel_type
    }


def _labels(graph):
    return {n.id: n.label for n in graph.nodes}


def test_spec_example_hierarchy_calls_and_imports(make_graph):
    graph = make_graph(SPEC_EXAMPLE)

    assert validate_code_graph(graph).valid
    contains = _edges(graph, RelationshipType.CONTAINS)
    assert ("app/api/users.py", "app/api/users.py::UserController") in contains
    assert ("demo", "app/api/users.py") in contains
    assert ("app/api/users.py::UserController", "app/api/users.py::UserController.get_user") in (
        _edges(graph, RelationshipType.HAS_METHOD)
    )
    assert _edges(graph, RelationshipType.CALLS) == {
        (
            "app/api/users.py::UserController.get_user",
            "app/services/user_service.py::UserService.find_user",
        )
    }
    assert _edges(graph, RelationshipType.IMPORTS) == {
        ("app/api/users.py", "app/services/user_service.py")
    }


def test_node_properties_and_labels(make_graph):
    graph = make_graph(SPEC_EXAMPLE)
    nodes = {n.id: n for n in graph.nodes}

    repo = nodes["repository:demo"]
    assert repo.label == NodeLabel.REPOSITORY
    assert repo.properties["name"] == "repo"
    file = nodes["file:demo:app/services/user_service.py"]
    assert file.properties["language"] == "python"
    assert file.properties["module"] == "app.services.user_service"
    method = nodes["function:demo:app/services/user_service.py::UserService.find_user"]
    assert method.properties["kind"] == "method"
    assert method.properties["parent_class"] == "UserService"
    assert (method.properties["line_start"], method.properties["line_end"]) == (3, 4)
    assert method.properties["parameters"] == ["self", "user_id"]


def test_every_function_has_exactly_one_parent(make_graph):
    graph = make_graph(
        {
            "m.py": """
                def top():
                    def inner():
                        pass

                class A:
                    def method(self):
                        pass

                    class Nested:
                        def deep(self):
                            pass
                """
        }
    )
    functions = [n.id for n in graph.nodes if n.label == NodeLabel.FUNCTION]
    parents = {
        r.target_id
        for r in graph.relationships
        if r.type in (RelationshipType.CONTAINS, RelationshipType.HAS_METHOD)
    }

    assert len(functions) == 4
    for function_id in functions:
        incoming = [
            r
            for r in graph.relationships
            if r.target_id == function_id
            and r.type in (RelationshipType.CONTAINS, RelationshipType.HAS_METHOD)
        ]
        assert len(incoming) == 1, function_id
    assert set(functions) <= parents


def test_empty_repository_has_only_the_repository_node(make_graph):
    graph = make_graph({})

    assert [n.label for n in graph.nodes] == [NodeLabel.REPOSITORY]
    assert graph.relationships == []
    assert validate_code_graph(graph).valid


def test_file_with_syntax_error_is_kept_as_a_file_node(make_graph):
    graph = make_graph({"ok.py": "def f():\n    pass\n", "bad.py": "def broken(:\n"})
    nodes = {n.id: n for n in graph.nodes}

    bad = nodes["file:demo:bad.py"]
    assert bad.properties["parse_status"] == "error"
    assert bad.properties["parse_errors"]
    assert "function:demo:ok.py::f" in nodes


def test_identical_class_names_in_different_files_stay_distinct(make_graph):
    graph = make_graph(
        {
            "a/user.py": "class UserService:\n    def run(self):\n        pass\n",
            "b/user.py": "class UserService:\n    def run(self):\n        pass\n",
        }
    )
    ids = {n.id for n in graph.nodes if n.label == NodeLabel.CLASS}

    assert ids == {"class:demo:a/user.py::UserService", "class:demo:b/user.py::UserService"}
    assert validate_code_graph(graph).valid


def test_identical_function_names_in_different_modules_are_not_linked(make_graph):
    graph = make_graph(
        {
            "a.py": "def calculate():\n    pass\n",
            "b.py": "def calculate():\n    pass\n",
            "c.py": "from a import calculate\n\ndef run():\n    calculate()\n",
        }
    )

    assert _edges(graph, RelationshipType.CALLS) == {("c.py::run", "a.py::calculate")}


def test_redefined_names_in_one_file_get_unique_ids(make_graph):
    graph = make_graph(
        {
            "m.py": """
                class Box:
                    @property
                    def size(self):
                        return 1

                    @size.setter
                    def size(self, value):
                        pass
                """
        }
    )
    ids = sorted(n.id for n in graph.nodes if n.label == NodeLabel.FUNCTION)

    assert ids == ["function:demo:m.py::Box.size@L4", "function:demo:m.py::Box.size@L8"]
    assert validate_code_graph(graph).valid


def test_repeated_builds_are_identical(make_graph, write_repo):
    from app.code_intelligence import analyze_repository

    root = write_repo(SPEC_EXAMPLE, name="stable")
    first = build_code_graph(analyze_repository(root), "demo")
    second = build_code_graph(analyze_repository(root), "demo")

    assert first == second
    assert [n.id for n in first.nodes] == sorted(n.id for n in first.nodes)
    assert [r.id for r in first.relationships] == sorted(r.id for r in first.relationships)


def test_recursive_call_is_a_self_loop(make_graph):
    graph = make_graph({"m.py": "def fact(n):\n    return n * fact(n - 1)\n"})

    assert _edges(graph, RelationshipType.CALLS) == {("m.py::fact", "m.py::fact")}
    assert validate_code_graph(graph).valid


def test_calls_are_aggregated_per_pair(make_graph):
    graph = make_graph(
        {"m.py": "def helper():\n    pass\n\ndef run():\n    helper()\n    helper()\n"}
    )
    [call] = [r for r in graph.relationships if r.type == RelationshipType.CALLS]

    assert call.properties["call_count"] == 2
    assert call.properties["line_numbers"] == [5, 6]
    assert call.properties["line_number"] == 5
    assert call.properties["call_expression"] == "helper"
    assert call.properties["resolution_status"] == "resolved"


def test_module_level_calls_are_preserved_as_unresolved(make_graph):
    graph = make_graph({"m.py": "def setup():\n    pass\n\nsetup()\n"})

    [ref] = graph.unresolved_references
    assert (ref.reason, ref.source_id, ref.line_number) == (
        "non_function_caller",
        "file:demo:m.py",
        4,
    )
    assert _edges(graph, RelationshipType.CALLS) == set()


def test_circular_imports_produce_both_edges(make_graph):
    graph = make_graph(
        {
            "a.py": "import b\n\ndef fa():\n    b.fb()\n",
            "b.py": "import a\n\ndef fb():\n    a.fa()\n",
        }
    )

    assert _edges(graph, RelationshipType.IMPORTS) == {("a.py", "b.py"), ("b.py", "a.py")}
    assert _edges(graph, RelationshipType.CALLS) == {("a.py::fa", "b.py::fb"), ("b.py::fb", "a.py::fa")}


def test_multiple_inheritance_keeps_order_and_unresolved_bases(make_graph):
    graph = make_graph(
        {
            "m.py": """
                from pydantic import BaseModel

                class BaseService:
                    pass

                class LoggingMixin:
                    pass

                class UserService(BaseService, LoggingMixin, BaseModel):
                    pass
                """
        }
    )
    extends = sorted(
        (r.properties["position"], r.target_id)
        for r in graph.relationships
        if r.type == RelationshipType.EXTENDS
    )

    assert extends == [(0, "class:demo:m.py::BaseService"), (1, "class:demo:m.py::LoggingMixin")]
    [ref] = [u for u in graph.unresolved_references if u.kind == "inheritance"]
    assert (ref.expression, ref.reason) == ("BaseModel", "external_module")


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/code/Atlas-AI", "atlas-ai"), ("/x/My Project!", "my-project"), ("/", "repository")],
)
def test_derive_repository_id(path, expected):
    assert derive_repository_id(path) == expected


def test_invalid_repository_id_is_rejected():
    analysis = RepositoryAnalysis(
        repository_path="/x", total_files=0, successful_files=0, failed_files=0
    )
    with pytest.raises(ValueError, match="Invalid repository id"):
        build_code_graph(analysis, "Not Valid")
