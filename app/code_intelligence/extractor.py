"""Extract classes, functions, imports, calls and inheritance from a parsed module.

All extraction happens in one ``ast.NodeVisitor`` pass. The visitor keeps a
stack of enclosing scopes so it can tell a module function from a method, build
``__qualname__``-style names, and attribute each call to its caller.

Expressions that Python evaluates in the *enclosing* scope (decorators, default
values, annotations, base classes) are visited before the new scope is entered,
so a call in ``@app.get("/")`` belongs to the module, not to the decorated function.
"""

import ast
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

from app.code_intelligence.schemas import (
    CallInfo,
    ClassInfo,
    FileStatus,
    FunctionInfo,
    FunctionKind,
    ImportInfo,
    ImportType,
    InheritanceInfo,
    ParameterInfo,
    ParameterKind,
    ParsedFile,
)


def extract_structure(tree: ast.Module, file_path: str) -> ParsedFile:
    """Return every code structure found in ``tree`` as a successful ``ParsedFile``.

    Args:
        tree: A module produced by ``parser.parse_source``.
        file_path: Repository-relative path recorded on the result.
    """
    extractor = _StructureExtractor()
    extractor.visit(tree)
    return ParsedFile(
        file_path=file_path,
        status=FileStatus.SUCCESS,
        classes=extractor.classes,
        functions=extractor.functions,
        imports=extractor.imports,
        calls=extractor.calls,
        inheritance=extractor.inheritance,
    )


@dataclass
class _Scope:
    kind: Literal["class", "function"]
    qualified_name: str
    class_info: ClassInfo | None = None


class _StructureExtractor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.classes: list[ClassInfo] = []
        self.functions: list[FunctionInfo] = []
        self.imports: list[ImportInfo] = []
        self.calls: list[CallInfo] = []
        self.inheritance: list[InheritanceInfo] = []
        self._scopes: list[_Scope] = []

    # --- definitions -------------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_all(node.decorator_list)
        self._visit_all(node.bases)
        self._visit_all(node.keywords)

        parent = self._parent_scope()
        qualified_name = self._qualify(node.name)
        info = ClassInfo(
            name=node.name,
            qualified_name=qualified_name,
            bases=[ast.unparse(base) for base in node.bases],
            decorators=[ast.unparse(d) for d in node.decorator_list],
            parent_class=parent.qualified_name if parent and parent.kind == "class" else None,
            start_line=node.lineno,
            end_line=_end_line(node),
        )
        self.classes.append(info)
        self.inheritance.extend(
            InheritanceInfo(class_name=qualified_name, base=ast.unparse(base), line=base.lineno)
            for base in node.bases
        )

        with self._enter("class", qualified_name, info):
            self._visit_all(node.body)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, is_async=True)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool) -> None:
        self._visit_all(node.decorator_list)
        self._visit_all(node.args.defaults)
        self._visit_all(d for d in node.args.kw_defaults if d is not None)
        self._visit_all(arg.annotation for arg in _all_arguments(node.args) if arg.annotation)
        if node.returns is not None:
            self.visit(node.returns)

        parent = self._parent_scope()
        parent_class: str | None = None
        if parent is None:
            kind = FunctionKind.FUNCTION
        elif parent.kind == "class":
            kind = FunctionKind.METHOD
            parent_class = parent.qualified_name
            if parent.class_info is not None:
                parent.class_info.methods.append(node.name)
        else:
            kind = FunctionKind.NESTED_FUNCTION

        qualified_name = self._qualify(node.name)
        self.functions.append(
            FunctionInfo(
                name=node.name,
                qualified_name=qualified_name,
                kind=kind,
                parameters=_parameters(node.args),
                return_annotation=_unparse_optional(node.returns),
                decorators=[ast.unparse(d) for d in node.decorator_list],
                is_async=is_async,
                parent_class=parent_class,
                start_line=node.lineno,
                end_line=_end_line(node),
            )
        )

        with self._enter("function", qualified_name):
            self._visit_all(node.body)

    # --- imports -----------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.extend(
            ImportInfo(
                import_type=ImportType.IMPORT,
                module=alias.name,
                name=None,
                alias=alias.asname,
                line=node.lineno,
            )
            for alias in node.names
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        level = node.level or 0
        self.imports.extend(
            ImportInfo(
                import_type=ImportType.FROM,
                module=node.module,
                name=alias.name,
                alias=alias.asname,
                is_relative=level > 0,
                level=level,
                line=node.lineno,
            )
            for alias in node.names
        )

    # --- calls -------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name: str | None = None
        receiver: str | None = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
            receiver = ast.unparse(func.value)

        caller = self._scopes[-1].qualified_name if self._scopes else None
        self.calls.append(
            CallInfo(
                caller=caller,
                expression=ast.unparse(func),
                name=name,
                receiver=receiver,
                line=node.lineno,
            )
        )
        self.generic_visit(node)  # nested calls in the callee and the arguments

    # --- scope helpers -----------------------------------------------------

    def _parent_scope(self) -> _Scope | None:
        return self._scopes[-1] if self._scopes else None

    def _qualify(self, name: str) -> str:
        parent = self._parent_scope()
        if parent is None:
            return name
        separator = ".<locals>." if parent.kind == "function" else "."
        return f"{parent.qualified_name}{separator}{name}"

    @contextmanager
    def _enter(
        self,
        kind: Literal["class", "function"],
        qualified_name: str,
        class_info: ClassInfo | None = None,
    ) -> Iterator[None]:
        self._scopes.append(_Scope(kind, qualified_name, class_info))
        try:
            yield
        finally:
            self._scopes.pop()

    def _visit_all(self, nodes: Iterable[ast.AST]) -> None:
        for node in nodes:
            self.visit(node)


def _all_arguments(args: ast.arguments) -> list[ast.arg]:
    extras = [arg for arg in (args.vararg, args.kwarg) if arg is not None]
    return [*args.posonlyargs, *args.args, *args.kwonlyargs, *extras]


def _parameters(args: ast.arguments) -> list[ParameterInfo]:
    """Parameters in declaration order, with defaults matched to their arguments."""
    positional = [*args.posonlyargs, *args.args]
    # ``defaults`` belongs to the *last* N positional parameters.
    padding: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    positional_defaults = [*padding, *args.defaults]

    parameters: list[ParameterInfo] = []
    for index, (arg, default) in enumerate(zip(positional, positional_defaults, strict=True)):
        kind = (
            ParameterKind.POSITIONAL_ONLY
            if index < len(args.posonlyargs)
            else ParameterKind.POSITIONAL_OR_KEYWORD
        )
        parameters.append(_parameter(arg, kind, default))
    if args.vararg is not None:
        parameters.append(_parameter(args.vararg, ParameterKind.VAR_POSITIONAL))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        parameters.append(_parameter(arg, ParameterKind.KEYWORD_ONLY, default))
    if args.kwarg is not None:
        parameters.append(_parameter(args.kwarg, ParameterKind.VAR_KEYWORD))
    return parameters


def _parameter(
    arg: ast.arg, kind: ParameterKind, default: ast.expr | None = None
) -> ParameterInfo:
    return ParameterInfo(
        name=arg.arg,
        kind=kind,
        annotation=_unparse_optional(arg.annotation),
        default=_unparse_optional(default),
    )


def _unparse_optional(node: ast.AST | None) -> str | None:
    return ast.unparse(node) if node is not None else None


def _end_line(node: ast.stmt) -> int:
    return node.end_lineno or node.lineno
