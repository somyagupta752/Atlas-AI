import textwrap

from app.code_intelligence.extractor import extract_structure
from app.code_intelligence.parser import parse_source
from app.code_intelligence.schemas import (
    FileStatus,
    FunctionKind,
    ImportType,
    ParameterKind,
    ParsedFile,
)


def _extract(source: str, file_path: str = "module.py") -> ParsedFile:
    source = textwrap.dedent(source)
    return extract_structure(parse_source(source, file_path), file_path)


def _function(parsed: ParsedFile, qualified_name: str):
    return next(f for f in parsed.functions if f.qualified_name == qualified_name)


# --- classes --------------------------------------------------------------


def test_single_class_with_methods_and_locations():
    parsed = _extract(
        """
        class UserService:
            def get_user(self, user_id):
                return user_id

            def delete_user(self, user_id):
                return user_id
        """
    )

    assert parsed.status == FileStatus.SUCCESS
    [cls] = parsed.classes
    assert cls.name == "UserService"
    assert cls.methods == ["get_user", "delete_user"]
    assert (cls.start_line, cls.end_line) == (2, 7)
    assert cls.bases == []


def test_multiple_classes():
    parsed = _extract(
        """
        class A:
            pass

        class B:
            pass
        """
    )

    assert [c.name for c in parsed.classes] == ["A", "B"]


def test_class_decorators_are_preserved_as_text():
    parsed = _extract(
        """
        @dataclass(frozen=True)
        class Point:
            x: int
        """
    )

    assert parsed.classes[0].decorators == ["dataclass(frozen=True)"]


# --- inheritance ----------------------------------------------------------


def test_single_inheritance():
    parsed = _extract(
        """
        class BaseService:
            pass

        class UserService(BaseService):
            pass
        """
    )

    [relation] = parsed.inheritance
    assert relation.class_name == "UserService"
    assert relation.base == "BaseService"
    assert relation.relationship == "EXTENDS"
    assert relation.line == 5


def test_multiple_and_unresolvable_bases_are_preserved():
    parsed = _extract(
        """
        class UserService(BaseService, mixins.LoggingMixin, Generic[T], metaclass=ABCMeta):
            pass
        """
    )

    assert [r.base for r in parsed.inheritance] == [
        "BaseService",
        "mixins.LoggingMixin",
        "Generic[T]",
    ]
    assert parsed.classes[0].bases == ["BaseService", "mixins.LoggingMixin", "Generic[T]"]


# --- functions ------------------------------------------------------------


def test_module_function_and_method_with_same_name_are_distinct():
    parsed = _extract(
        """
        def calculate():
            pass

        class Calculator:
            def calculate(self):
                pass
        """
    )

    module_level = _function(parsed, "calculate")
    method = _function(parsed, "Calculator.calculate")
    assert module_level.kind == FunctionKind.FUNCTION
    assert module_level.parent_class is None
    assert method.kind == FunctionKind.METHOD
    assert method.parent_class == "Calculator"


def test_function_location_and_return_annotation():
    parsed = _extract(
        """
        def calculate_total(price: float, quantity: int) -> float:
            return price * quantity
        """
    )

    [function] = parsed.functions
    assert function.name == "calculate_total"
    assert function.return_annotation == "float"
    assert (function.start_line, function.end_line) == (2, 3)
    assert function.is_async is False


def test_async_function_and_async_method():
    parsed = _extract(
        """
        async def fetch():
            pass

        class Client:
            async def get(self):
                pass
        """
    )

    assert _function(parsed, "fetch").is_async is True
    assert _function(parsed, "Client.get").is_async is True
    assert _function(parsed, "Client.get").kind == FunctionKind.METHOD


def test_decorated_function_reports_decorators_and_def_line():
    parsed = _extract(
        """
        class Repo:
            @staticmethod
            @functools.lru_cache(maxsize=32)
            def build():
                pass
        """
    )

    function = _function(parsed, "Repo.build")
    assert function.decorators == ["staticmethod", "functools.lru_cache(maxsize=32)"]
    assert function.start_line == 5


# --- parameters -----------------------------------------------------------


def test_parameters_cover_every_kind_with_defaults_and_annotations():
    parsed = _extract(
        """
        def f(a, /, b: int, c: str = "x", *args: int, d, e: bool = False, **kwargs: object):
            pass
        """
    )

    params = [(p.name, p.kind, p.annotation, p.default) for p in parsed.functions[0].parameters]
    assert params == [
        ("a", ParameterKind.POSITIONAL_ONLY, None, None),
        ("b", ParameterKind.POSITIONAL_OR_KEYWORD, "int", None),
        ("c", ParameterKind.POSITIONAL_OR_KEYWORD, "str", "'x'"),
        ("args", ParameterKind.VAR_POSITIONAL, "int", None),
        ("d", ParameterKind.KEYWORD_ONLY, None, None),
        ("e", ParameterKind.KEYWORD_ONLY, "bool", "False"),
        ("kwargs", ParameterKind.VAR_KEYWORD, "object", None),
    ]


def test_defaults_align_with_last_positional_parameters():
    parsed = _extract("def f(a, b=1, c=[]):\n    pass\n")

    defaults = {p.name: p.default for p in parsed.functions[0].parameters}
    assert defaults == {"a": None, "b": "1", "c": "[]"}


def test_complex_annotations_are_kept_as_source_text():
    parsed = _extract("def f(x: dict[str, list[int]] | None = None) -> 'User':\n    pass\n")

    [param] = parsed.functions[0].parameters
    assert param.annotation == "dict[str, list[int]] | None"
    assert parsed.functions[0].return_annotation == "'User'"


# --- nested structures ----------------------------------------------------


def test_nested_function_is_not_a_method():
    parsed = _extract(
        """
        class Service:
            def run(self):
                def helper():
                    pass
                return helper()
        """
    )

    helper = _function(parsed, "Service.run.<locals>.helper")
    assert helper.kind == FunctionKind.NESTED_FUNCTION
    assert helper.parent_class is None
    assert parsed.classes[0].methods == ["run"]


def test_nested_class_and_its_methods():
    parsed = _extract(
        """
        class Outer:
            class Inner:
                def method(self):
                    pass

            def outer_method(self):
                pass
        """
    )

    outer, inner = parsed.classes
    assert inner.qualified_name == "Outer.Inner"
    assert inner.parent_class == "Outer"
    assert inner.methods == ["method"]
    assert outer.methods == ["outer_method"]
    assert _function(parsed, "Outer.Inner.method").parent_class == "Outer.Inner"


def test_class_defined_inside_function():
    parsed = _extract(
        """
        def factory():
            class Local:
                def m(self):
                    pass
            return Local
        """
    )

    [cls] = parsed.classes
    assert cls.qualified_name == "factory.<locals>.Local"
    assert cls.parent_class is None
    assert _function(parsed, "factory.<locals>.Local.m").kind == FunctionKind.METHOD


# --- imports --------------------------------------------------------------


def test_plain_and_aliased_imports():
    parsed = _extract(
        """
        import os
        import pandas as pd
        import os.path, json as j
        """
    )

    imports = [(i.import_type, i.module, i.name, i.alias, i.line) for i in parsed.imports]
    assert imports == [
        (ImportType.IMPORT, "os", None, None, 2),
        (ImportType.IMPORT, "pandas", None, "pd", 3),
        (ImportType.IMPORT, "os.path", None, None, 4),
        (ImportType.IMPORT, "json", None, "j", 4),
    ]
    assert not any(i.is_relative for i in parsed.imports)


def test_from_imports():
    parsed = _extract(
        """
        from app.database import get_db
        from app.services import UserService as US, OrderService
        from typing import *
        """
    )

    first = parsed.imports[0]
    assert first.model_dump() == {
        "import_type": "from",
        "module": "app.database",
        "name": "get_db",
        "alias": None,
        "is_relative": False,
        "level": 0,
        "line": 2,
    }
    assert [(i.name, i.alias) for i in parsed.imports[1:]] == [
        ("UserService", "US"),
        ("OrderService", None),
        ("*", None),
    ]


def test_relative_imports():
    parsed = _extract(
        """
        from . import utils
        from ..models import User
        """
    )

    current, parent = parsed.imports
    assert (current.module, current.name, current.is_relative, current.level) == (
        None,
        "utils",
        True,
        1,
    )
    assert (parent.module, parent.name, parent.is_relative, parent.level) == (
        "models",
        "User",
        True,
        2,
    )


# --- calls ----------------------------------------------------------------


def test_method_calls_through_attributes():
    parsed = _extract(
        """
        class UserService:
            def get_user(self, user_id):
                return self.repository.find_by_id(user_id)

            def delete_user(self, user_id):
                self.repository.delete(user_id)
        """
    )

    calls = [(c.caller, c.name, c.expression, c.receiver, c.line) for c in parsed.calls]
    assert calls == [
        ("UserService.get_user", "find_by_id", "self.repository.find_by_id", "self.repository", 4),
        ("UserService.delete_user", "delete", "self.repository.delete", "self.repository", 7),
    ]


def test_direct_attribute_and_chained_call_patterns():
    parsed = _extract(
        """
        def run():
            calculate()
            self.calculate()
            service.calculate()
            module.service.calculate()
        """
    )

    assert [(c.expression, c.name, c.receiver) for c in parsed.calls] == [
        ("calculate", "calculate", None),
        ("self.calculate", "calculate", "self"),
        ("service.calculate", "calculate", "service"),
        ("module.service.calculate", "calculate", "module.service"),
    ]
    assert {c.caller for c in parsed.calls} == {"run"}


def test_calls_on_call_results_and_nested_arguments():
    parsed = _extract("result = get_factory()().build(load(path))\n")

    expressions = [c.expression for c in parsed.calls]
    assert expressions == ["get_factory()().build", "get_factory()", "get_factory", "load"]
    anonymous = next(c for c in parsed.calls if c.expression == "get_factory()")
    assert anonymous.name is None
    assert all(c.caller is None for c in parsed.calls)


def test_decorator_and_default_calls_belong_to_the_enclosing_scope():
    parsed = _extract(
        """
        class Api:
            @route("/users")
            def users(self, limit=default_limit()):
                fetch()
        """
    )

    callers = {c.expression: c.caller for c in parsed.calls}
    assert callers == {"route": "Api", "default_limit": "Api", "fetch": "Api.users"}


def test_calls_inside_nested_function_are_attributed_to_it():
    parsed = _extract(
        """
        def outer():
            def inner():
                work()
            inner()
        """
    )

    callers = {c.expression: c.caller for c in parsed.calls}
    assert callers == {"work": "outer.<locals>.inner", "inner": "outer"}


def test_empty_module_extracts_nothing():
    parsed = _extract("")

    assert parsed.status == FileStatus.SUCCESS
    assert parsed.classes == parsed.functions == parsed.imports == parsed.calls == []
