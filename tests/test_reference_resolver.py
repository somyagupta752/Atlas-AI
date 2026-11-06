"""Resolution behaviour, checked through the graph the builder produces."""

import pytest

from app.knowledge_graph.models import RelationshipType
from app.knowledge_graph.resolver import module_names_for_path


def _short(node_id):
    return node_id.split(":", 2)[-1]


def _calls(graph):
    return {
        (_short(r.source_id), _short(r.target_id), r.properties["resolution_status"])
        for r in graph.relationships
        if r.type == RelationshipType.CALLS
    }


def _imports(graph):
    return {
        (_short(r.source_id), _short(r.target_id), r.properties.get("imported_entity_id"))
        for r in graph.relationships
        if r.type == RelationshipType.IMPORTS
    }


def _unresolved(graph, kind):
    return {(u.expression, u.reason) for u in graph.unresolved_references if u.kind == kind}


PACKAGE = {
    "app/__init__.py": "",
    "app/services/__init__.py": "from .user_service import UserService\n",
    "app/services/user_service.py": """
        class UserService:
            def find_user(self, user_id):
                return user_id

        def helper():
            pass
        """,
}


# --- imports ----------------------------------------------------------------


def test_absolute_from_import_resolves_to_class_in_other_file(make_graph):
    graph = make_graph(
        {**PACKAGE, "app/api.py": "from app.services.user_service import UserService\n"}
    )

    assert _imports(graph) >= {
        ("app/api.py", "app/services/user_service.py", "class:demo:app/services/user_service.py::UserService")
    }


def test_plain_and_aliased_module_imports(make_graph):
    graph = make_graph(
        {
            **PACKAGE,
            "main.py": """
                import app.services.user_service
                import app.services.user_service as us

                def run():
                    app.services.user_service.helper()
                    us.helper()
                """,
        }
    )

    targets = {(s, t) for s, t, _ in _imports(graph) if s == "main.py"}
    assert targets == {("main.py", "app/services/user_service.py")}
    assert _calls(graph) == {
        ("main.py::run", "app/services/user_service.py::helper", "resolved")
    }


def test_relative_imports_at_several_levels(make_graph):
    graph = make_graph(
        {
            **PACKAGE,
            "app/api/__init__.py": "",
            "app/api/users.py": """
                from ..services.user_service import helper
                from . import views

                def run():
                    helper()
                    views.render()
                """,
            "app/api/views.py": "def render():\n    pass\n",
        }
    )

    assert {(s, t) for s, t, _ in _imports(graph) if s == "app/api/users.py"} == {
        ("app/api/users.py", "app/services/user_service.py"),
        ("app/api/users.py", "app/api/views.py"),
    }
    assert _calls(graph) == {
        ("app/api/users.py::run", "app/services/user_service.py::helper", "resolved"),
        ("app/api/users.py::run", "app/api/views.py::render", "resolved"),
    }


def test_aliased_class_import_and_local_instance(make_graph):
    graph = make_graph(
        {
            **PACKAGE,
            "main.py": """
                from app.services.user_service import UserService as Service

                def run():
                    service = Service()
                    service.find_user(1)
                """,
        }
    )

    assert ("main.py::run", "app/services/user_service.py::UserService.find_user", "inferred") in (
        _calls(graph)
    )


def test_re_export_through_package_init(make_graph):
    graph = make_graph(
        {
            **PACKAGE,
            "main.py": "from app.services import UserService\n\ndef run():\n    UserService().find_user(1)\n",
        }
    )

    [(_, target, entity)] = [i for i in _imports(graph) if i[0] == "main.py"]
    assert target == "app/services/__init__.py"
    assert entity == "class:demo:app/services/user_service.py::UserService"


def test_src_layout_modules_resolve():
    assert module_names_for_path("src/app/x.py") == ["app.x", "src.app.x"]
    assert module_names_for_path("app/__init__.py") == ["app"]
    assert module_names_for_path("my-scripts/run.py") == []
    assert module_names_for_path("class/x.py") == []


def test_unresolved_imports_are_classified(make_graph):
    graph = make_graph(
        {
            **PACKAGE,
            "app/main.py": """
                import os
                import pandas as pd
                from app.missing import thing
                from .... import too_far
                """,
        }
    )

    assert _unresolved(graph, "import") == {
        ("import os", "external_module"),
        ("import pandas", "external_module"),
        ("from app.missing import thing", "module_not_found"),
        ("from .... import too_far", "relative_import_beyond_root"),
    }


def test_function_local_import_does_not_leak_to_other_functions(make_graph):
    graph = make_graph(
        {
            **PACKAGE,
            "main.py": """
                def first():
                    from app.services.user_service import helper
                    helper()

                def second():
                    helper()
                """,
        }
    )

    assert _calls(graph) == {("main.py::first", "app/services/user_service.py::helper", "resolved")}
    assert ("helper", "undefined_name") in _unresolved(graph, "call")


# --- calls --------------------------------------------------------------------


def test_receiver_of_unknown_type_is_not_guessed(make_graph):
    graph = make_graph(
        {
            "a.py": "class A:\n    def calculate(self):\n        pass\n",
            "b.py": "class B:\n    def calculate(self):\n        pass\n",
            "c.py": "def run(service):\n    service.calculate()\n",
        }
    )

    assert _calls(graph) == set()
    assert _unresolved(graph, "call") == {("service.calculate", "unknown_type")}


def test_conflicting_assignments_are_ambiguous(make_graph):
    graph = make_graph(
        {
            "m.py": """
                class A:
                    def go(self):
                        pass

                class B:
                    def go(self):
                        pass

                def run(flag):
                    x = A()
                    if flag:
                        x = B()
                    x.go()
                """
        }
    )

    assert ("x.go", "ambiguous") in _unresolved(graph, "call")
    assert not any(target.endswith(".go") for _, target, _ in _calls(graph))


def test_annotated_parameter_and_injected_attribute(make_graph):
    graph = make_graph(
        {
            "repo.py": "class Repository:\n    def find(self):\n        pass\n",
            "svc.py": """
                from repo import Repository

                class Service:
                    def __init__(self, repository: Repository):
                        self.repository = repository

                    def get(self):
                        return self.repository.find()

                def standalone(repo: "Repository"):
                    repo.find()
                """,
        }
    )

    assert _calls(graph) == {
        ("svc.py::Service.get", "repo.py::Repository.find", "inferred"),
        ("svc.py::standalone", "repo.py::Repository.find", "inferred"),
    }


def test_self_super_and_inherited_methods(make_graph):
    graph = make_graph(
        {
            "m.py": """
                class Base:
                    def __init__(self):
                        pass

                    def save(self):
                        pass

                class Child(Base):
                    def __init__(self):
                        super().__init__()

                    def run(self):
                        self.save()
                        self.missing()

                def build():
                    Child()
                """
        }
    )

    assert _calls(graph) == {
        ("m.py::Child.__init__", "m.py::Base.__init__", "inferred"),
        ("m.py::Child.run", "m.py::Base.save", "inferred"),
        ("m.py::build", "m.py::Child.__init__", "resolved"),
    }
    assert ("self.missing", "attribute_not_found") in _unresolved(graph, "call")


def test_method_possibly_from_external_base_is_unresolved(make_graph):
    graph = make_graph(
        {
            "m.py": """
                from pydantic import BaseModel

                class User(BaseModel):
                    def run(self):
                        self.model_dump()
                """
        }
    )

    assert _unresolved(graph, "call") == {("self.model_dump", "inherited_from_external")}


def test_nested_function_is_found_through_enclosing_scope(make_graph):
    graph = make_graph(
        {
            "m.py": """
                def outer():
                    def inner():
                        pass
                    inner()

                def other():
                    inner()
                """
        }
    )

    assert _calls(graph) == {("m.py::outer", "m.py::outer.<locals>.inner", "resolved")}
    assert ("inner", "undefined_name") in _unresolved(graph, "call")


def test_parameter_shadows_module_function(make_graph):
    graph = make_graph(
        {"m.py": "def helper():\n    pass\n\ndef run(helper):\n    helper()\n"}
    )

    assert _calls(graph) == set()
    assert _unresolved(graph, "call") == {("helper", "unknown_type")}


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("def f():\n    print(1)\n", "builtin"),
        ("def f():\n    handlers['x']()\n", "dynamic_expression"),
        ("def f():\n    make()()\n", "dynamic_expression"),
        ("def f():\n    nothing_here()\n", "undefined_name"),
        ("from lib import *\n\ndef f():\n    maybe()\n", "star_import"),
        ("class Empty:\n    pass\n\ndef f():\n    Empty()\n", "class_without_init"),
        ("import json\n\ndef f():\n    json.dumps({})\n", "external_module"),
    ],
)
def test_unresolved_call_reasons(make_graph, source, reason):
    graph = make_graph({"m.py": source})

    assert reason in {r for _, r in _unresolved(graph, "call")}
    assert _calls(graph) == set()


def test_inheritance_cycle_does_not_hang(make_graph):
    graph = make_graph(
        {
            "a.py": "from b import B\n\nclass A(B):\n    def run(self):\n        self.go()\n",
            "b.py": "from a import A\n\nclass B(A):\n    pass\n",
        }
    )

    assert any(r.type == RelationshipType.EXTENDS for r in graph.relationships)
    assert ("self.go", "inherited_from_external") in _unresolved(graph, "call")


def test_local_namespace_directory_does_not_shadow_installed_package(make_graph):
    graph = make_graph(
        {
            "alembic/env.py": "from alembic import context\n\ndef run():\n    context.configure()\n",
            "alembic/versions/v1.py": "from alembic import op\n",
            "tests/helpers.py": "def make():\n    pass\n",
            "tests/test_x.py": "from tests.helpers import make\n\ndef test():\n    make()\n",
        }
    )

    assert _unresolved(graph, "import") == {
        ("from alembic import context", "external_module"),
        ("from alembic import op", "external_module"),
    }
    assert ("context.configure", "external_module") in _unresolved(graph, "call")
    # A namespace package that does contain the module still resolves.
    assert ("tests/test_x.py::test", "tests/helpers.py::make", "resolved") in _calls(graph)


def test_comprehension_and_lambda_variables_are_local_not_undefined(make_graph):
    graph = make_graph(
        {
            "m.py": """
                def run(items):
                    names = [item.strip() for item in items]
                    key = lambda value: value.lower()
                    return names, key
                """
        }
    )

    reasons = {r for _, r in _unresolved(graph, "call")}
    assert "undefined_name" not in reasons
    assert _unresolved(graph, "call") == {
        ("item.strip", "unknown_type"),
        ("value.lower", "unknown_type"),
    }
