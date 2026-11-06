"""Static, evidence-based resolution of imports, calls and base classes.

The resolver answers "what does this name refer to?" using only Task 9 output:
definitions, imports, name bindings and parameters. It never imports or runs
repository code, and it never matches a target by name alone. When the evidence
is missing or conflicting it returns ``Unknown`` with a reason, and the builder
records an unresolved reference instead of a relationship.

Supported:
- Absolute, relative and aliased imports, including ``import a.b`` binding ``a``.
- Re-exports: ``from pkg import X`` where ``pkg/__init__.py`` itself imports ``X``.
- Python scoping: local -> enclosing functions -> module -> builtins. Class bodies
  are not visible from their methods, as in Python.
- ``self.method()``, ``cls.method()``, ``super().method()`` through the C3 MRO.
- Constructor calls (``UserService()`` resolves to ``UserService.__init__``).
- Simple type inference. ``x = Service()``, ``x: Service`` and ``self.x = Service()``
  give ``x`` the type ``Service``, but only when every binding agrees.

Deliberately unsupported: dynamic imports, ``getattr``, monkey-patching,
decorators that replace functions, return-type inference, ``global`` statements
and runtime dispatch to subclass overrides.
"""

import ast
import builtins
import keyword
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from app.code_intelligence.schemas import (
    AssignmentInfo,
    ClassInfo,
    FunctionInfo,
    FunctionKind,
    ImportInfo,
    ImportType,
    ParameterInfo,
    ParsedFile,
    RepositoryAnalysis,
)
from app.knowledge_graph.models import (
    UnresolvedReason,
    class_node_id,
    file_node_id,
    function_node_id,
)

BUILTIN_NAMES = frozenset(dir(builtins))

# --- resolved values --------------------------------------------------------


@dataclass(frozen=True)
class ModuleValue:
    name: str


@dataclass(frozen=True)
class ClassValue:
    class_id: str


@dataclass(frozen=True)
class FunctionValue:
    function_id: str


@dataclass(frozen=True)
class InstanceValue:
    class_id: str


@dataclass(frozen=True)
class SuperValue:
    class_id: str  # the class whose method called super()


@dataclass(frozen=True)
class Unknown:
    reason: UnresolvedReason


Value = ModuleValue | ClassValue | FunctionValue | InstanceValue | SuperValue | Unknown


@dataclass(frozen=True)
class Resolution:
    value: Value
    inferred: bool = False  # True when the answer depends on type inference

    @property
    def resolved(self) -> bool:
        return not isinstance(self.value, Unknown)


def _unknown(reason: UnresolvedReason) -> Resolution:
    return Resolution(Unknown(reason))


# --- repository index -------------------------------------------------------


@dataclass
class ClassEntity:
    node_id: str
    info: ClassInfo
    file: "FileIndex"


@dataclass
class FunctionEntity:
    node_id: str
    info: FunctionInfo
    file: "FileIndex"


@dataclass
class FileIndex:
    path: str
    node_id: str
    parsed: ParsedFile
    module_name: str | None
    package_parts: list[str] | None  # package used as the base of relative imports
    classes: dict[str, list[ClassEntity]] = field(default_factory=dict)
    functions: dict[str, list[FunctionEntity]] = field(default_factory=dict)
    imports: dict[str | None, list[ImportInfo]] = field(default_factory=dict)
    assignments: dict[tuple[str | None, str], list[AssignmentInfo]] = field(default_factory=dict)
    # (class qualified name, attribute) -> ``self.attribute = ...`` bindings in its methods
    instance_attributes: dict[tuple[str, str], list[AssignmentInfo]] = field(default_factory=dict)
    star_import_scopes: set[str | None] = field(default_factory=set)


class RepositoryIndex:
    """Lookup tables and stable node IDs for every entity in an analysis."""

    def __init__(self, analysis: RepositoryAnalysis, repository_id: str) -> None:
        self.repository_id = repository_id
        self.files: list[FileIndex] = []
        self.classes_by_id: dict[str, ClassEntity] = {}
        self.functions_by_id: dict[str, FunctionEntity] = {}
        self.modules: dict[str, list[FileIndex]] = defaultdict(list)
        self.package_prefixes: set[str] = set()
        self.top_level_names: set[str] = set()

        for parsed in sorted(analysis.files, key=lambda f: f.file_path):
            self._add_file(parsed)

    def is_known_module(self, name: str) -> bool:
        return name in self.modules or name in self.package_prefixes

    def is_namespace_only(self, name: str) -> bool:
        """A directory without ``__init__.py``. At runtime an installed regular
        package of the same name takes precedence, so it cannot prove absence."""
        return name not in self.modules and name in self.package_prefixes

    def missing_module_reason(self, name: str) -> UnresolvedReason:
        # Only a regular local top-level package proves a module is missing locally.
        if name.split(".")[0] in self.modules:
            return UnresolvedReason.MODULE_NOT_FOUND
        return UnresolvedReason.EXTERNAL_MODULE

    def _add_file(self, parsed: ParsedFile) -> None:
        path = parsed.file_path
        names = module_names_for_path(path)
        file = FileIndex(
            path=path,
            node_id=file_node_id(self.repository_id, path),
            parsed=parsed,
            module_name=names[0] if names else None,
            package_parts=_package_parts(path),
        )
        self.files.append(file)
        for name in names:
            self.modules[name].append(file)
            parts = name.split(".")
            self.top_level_names.add(parts[0])
            self.package_prefixes.update(".".join(parts[:i]) for i in range(1, len(parts)))

        self._add_entities(file, parsed)
        for imp in parsed.imports:
            file.imports.setdefault(imp.scope, []).append(imp)
            if imp.name == "*":
                file.star_import_scopes.add(imp.scope)
        for assignment in parsed.assignments:
            key = (assignment.scope, assignment.target)
            file.assignments.setdefault(key, []).append(assignment)
        self._index_instance_attributes(file)

    def _add_entities(self, file: FileIndex, parsed: ParsedFile) -> None:
        def counts(items: list[ClassInfo] | list[FunctionInfo]) -> dict[str, int]:
            totals: dict[str, int] = defaultdict(int)
            for item in items:
                totals[item.qualified_name] += 1
            return totals

        def node_qualifier(item: ClassInfo | FunctionInfo, totals: dict[str, int]) -> str:
            # Redefinitions in one file (property setters, if/else defs) need distinct IDs.
            if totals[item.qualified_name] > 1:
                return f"{item.qualified_name}@L{item.start_line}"
            return item.qualified_name

        class_totals = counts(parsed.classes)
        for info in parsed.classes:
            node_id = class_node_id(self.repository_id, file.path, node_qualifier(info, class_totals))
            entity = ClassEntity(node_id, info, file)
            file.classes.setdefault(info.qualified_name, []).append(entity)
            self.classes_by_id[node_id] = entity

        function_totals = counts(parsed.functions)
        for info in parsed.functions:
            qualifier = node_qualifier(info, function_totals)
            node_id = function_node_id(self.repository_id, file.path, qualifier)
            entity = FunctionEntity(node_id, info, file)
            file.functions.setdefault(info.qualified_name, []).append(entity)
            self.functions_by_id[node_id] = entity

    def _index_instance_attributes(self, file: FileIndex) -> None:
        for (scope, target), bindings in file.assignments.items():
            if scope is None or "." not in target:
                continue
            receiver, _, attribute = target.partition(".")
            if "." in attribute:
                continue
            for method in file.functions.get(scope, []):
                info = method.info
                if (
                    info.kind == FunctionKind.METHOD
                    and info.parent_class is not None
                    and _first_parameter(info) == receiver
                    and not _is_static(info)
                ):
                    key = (info.parent_class, attribute)
                    file.instance_attributes.setdefault(key, []).extend(bindings)
                    break


def module_names_for_path(path: str) -> list[str]:
    """Dotted module names a file can be imported as; the first one is primary.

    ``app/x.py`` -> ``["app.x"]``; ``app/__init__.py`` -> ``["app"]``;
    ``src/app/x.py`` -> ``["app.x", "src.app.x"]`` (src layout).
    Paths that are not valid identifiers (``my-scripts/run.py``) are not importable.
    """
    if not path.endswith(".py"):
        return []
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(_is_identifier(part) for part in parts):
        return []
    names = [".".join(parts)]
    if parts[0] == "src" and len(parts) > 1:
        names.insert(0, ".".join(parts[1:]))
    return names


def _package_parts(path: str) -> list[str] | None:
    directories = path.split("/")[:-1]
    if directories and directories[0] == "src":
        directories = directories[1:]
    if not all(_is_identifier(part) for part in directories):
        return None
    return directories


def _is_identifier(value: str) -> bool:
    return value.isidentifier() and not keyword.iskeyword(value)


def _first_parameter(info: FunctionInfo) -> str | None:
    return info.parameters[0].name if info.parameters else None


def _is_static(info: FunctionInfo) -> bool:
    return "staticmethod" in info.decorators


def _is_classmethod(info: FunctionInfo) -> bool:
    return "classmethod" in info.decorators


# --- scopes -----------------------------------------------------------------

ScopeKind = Literal["module", "class", "function"]


@dataclass(frozen=True)
class Scope:
    kind: ScopeKind
    qualified_name: str | None  # None for the module scope
    entity: ClassEntity | FunctionEntity | None = None

    def child(self, name: str) -> str:
        if self.qualified_name is None:
            return name
        separator = ".<locals>." if self.kind == "function" else "."
        return f"{self.qualified_name}{separator}{name}"


MODULE_SCOPE = Scope("module", None)
Chain = tuple[Scope, ...]  # innermost scope first, module scope last

_EXTERNAL_BASE = "?"  # prefix for MRO entries that stand for an unresolved base class


def _qualname_segments(qualified_name: str) -> list[tuple[str, ScopeKind | None]]:
    """Split ``A.m.<locals>.g`` into its scopes, outermost first.

    A segment followed by ``<locals>`` is a function and one followed by a plain
    name is a class. The kind of the last segment is unknown (None).
    """
    tokens = qualified_name.split(".")
    segments: list[tuple[str, ScopeKind | None]] = []
    prefix: list[str] = []
    index = 0
    while index < len(tokens):
        prefix.append(tokens[index])
        if index + 1 < len(tokens) and tokens[index + 1] == "<locals>":
            segments.append((".".join(prefix), "function"))
            prefix.append("<locals>")
            index += 2
        else:
            kind: ScopeKind | None = "class" if index + 1 < len(tokens) else None
            segments.append((".".join(prefix), kind))
            index += 1
    return segments


def parent_qualified_name(qualified_name: str) -> str | None:
    segments = _qualname_segments(qualified_name)
    return segments[-2][0] if len(segments) > 1 else None


def _pick_by_line[T: (ClassEntity, FunctionEntity)](entities: list[T], line: int) -> T | None:
    if not entities:
        return None
    containing = [e for e in entities if e.info.start_line <= line <= e.info.end_line]
    return containing[-1] if containing else entities[0]


# --- resolver ---------------------------------------------------------------


@dataclass(frozen=True)
class ImportTarget:
    """Where an import statement points: a file, or the reason it cannot be known."""

    file: FileIndex | None
    reason: UnresolvedReason | None = None
    entity_id: str | None = None  # the class/function a from-import binds, when known


class Resolver:
    def __init__(self, index: RepositoryIndex) -> None:
        self.index = index
        self._active: set[tuple[str, str | None, str]] = set()
        self._mro_cache: dict[str, list[str]] = {}
        self._mro_active: set[str] = set()
        self._bases_cache: dict[str, list[Resolution]] = {}

    # --- scopes -------------------------------------------------------------

    def entity_at(
        self, file: FileIndex, qualified_name: str, line: int
    ) -> ClassEntity | FunctionEntity | None:
        """The function or class called ``qualified_name`` that encloses ``line``."""
        functions = file.functions.get(qualified_name, [])
        classes = file.classes.get(qualified_name, [])
        candidates: list[ClassEntity | FunctionEntity] = [*functions, *classes]
        containing = [c for c in candidates if c.info.start_line <= line <= c.info.end_line]
        if containing:
            return containing[-1]
        return candidates[0] if candidates else None

    def scope_chain(self, file: FileIndex, qualified_name: str | None, line: int) -> Chain:
        """Scopes searched for a name used in ``qualified_name``, innermost first."""
        if qualified_name is None:
            return (MODULE_SCOPE,)
        chain: list[Scope] = []
        segments = _qualname_segments(qualified_name)
        for position, (name, kind) in enumerate(reversed(segments)):
            innermost = position == 0
            if innermost:
                entity = self.entity_at(file, name, line)
                if isinstance(entity, ClassEntity):
                    chain.append(Scope("class", name, entity))  # class body sees its own names
                elif isinstance(entity, FunctionEntity):
                    chain.append(Scope("function", name, entity))
            elif kind == "function":
                function = _pick_by_line(file.functions.get(name, []), line)
                chain.append(Scope("function", name, function))
            # Enclosing class scopes are skipped: methods cannot see class-level names.
        chain.append(MODULE_SCOPE)
        return tuple(chain)

    # --- names --------------------------------------------------------------

    def resolve_expression(self, file: FileIndex, chain: Chain, expression: str) -> Resolution:
        """Resolve a dotted expression such as ``self.repo.find`` or ``Service``."""
        try:
            node = ast.parse(expression, mode="eval").body
        except SyntaxError:
            return _unknown(UnresolvedReason.DYNAMIC_EXPRESSION)
        return self._resolve_node(file, chain, node)

    def _resolve_node(self, file: FileIndex, chain: Chain, node: ast.expr) -> Resolution:
        if isinstance(node, ast.Name):
            return self.lookup(file, chain, node.id)
        if isinstance(node, ast.Attribute):
            return self._attribute(self._resolve_node(file, chain, node.value), node.attr)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "super"
            and not node.args
        ):
            return self._super(file, chain)
        return _unknown(UnresolvedReason.DYNAMIC_EXPRESSION)

    def lookup(self, file: FileIndex, chain: Chain, name: str) -> Resolution:
        for position, scope in enumerate(chain):
            if self._has_bindings(file, scope, name):
                return self._interpret(file, chain[position:], name)
        if name in BUILTIN_NAMES:
            return _unknown(UnresolvedReason.BUILTIN)
        if any(scope.qualified_name in file.star_import_scopes for scope in chain):
            return _unknown(UnresolvedReason.STAR_IMPORT)
        return _unknown(UnresolvedReason.UNDEFINED_NAME)

    def _has_bindings(self, file: FileIndex, scope: Scope, name: str) -> bool:
        qualified = scope.child(name)
        return bool(
            file.classes.get(qualified)
            or file.functions.get(qualified)
            or self._imports_binding(file, scope, name)
            or file.assignments.get((scope.qualified_name, name))
            or self._parameters_named(scope, name)
        )

    def _interpret(self, file: FileIndex, chain: Chain, name: str) -> Resolution:
        """Combine every binding of ``name`` in ``chain[0]``; disagreement is ambiguity."""
        scope = chain[0]
        key = (file.path, scope.qualified_name, name)
        if key in self._active:
            return _unknown(UnresolvedReason.CIRCULAR_REFERENCE)
        self._active.add(key)
        try:
            qualified = scope.child(name)
            candidates: list[Resolution] = [
                Resolution(ClassValue(entity.node_id)) for entity in file.classes.get(qualified, [])
            ]
            candidates += [
                Resolution(FunctionValue(entity.node_id))
                for entity in file.functions.get(qualified, [])
            ]
            candidates += [
                self._import_binding_value(file, imp)
                for imp in self._imports_binding(file, scope, name)
            ]
            assignments = file.assignments.get((scope.qualified_name, name), [])
            parameters = self._parameters_named(scope, name)
            if assignments or parameters:
                candidates.append(self._infer_variable(file, chain, assignments, parameters))
            return _merge(candidates)
        finally:
            self._active.discard(key)

    @staticmethod
    def _imports_binding(file: FileIndex, scope: Scope, name: str) -> list[ImportInfo]:
        return [
            imp for imp in file.imports.get(scope.qualified_name, []) if _bound_name(imp) == name
        ]

    @staticmethod
    def _parameters_named(scope: Scope, name: str) -> list[ParameterInfo]:
        if scope.kind != "function" or not isinstance(scope.entity, FunctionEntity):
            return []
        return [p for p in scope.entity.info.parameters if p.name == name]

    # --- type inference -----------------------------------------------------

    def _infer_variable(
        self,
        file: FileIndex,
        chain: Chain,
        assignments: list[AssignmentInfo],
        parameters: list[ParameterInfo],
    ) -> Resolution:
        scope = chain[0]
        evidence: list[Resolution | None] = []
        for parameter in parameters:
            evidence.append(self._parameter_evidence(file, chain, scope, parameter))
        evidence.extend(self._assignment_evidence(file, chain, assignments))
        return _agree(evidence)

    def _parameter_evidence(
        self, file: FileIndex, chain: Chain, scope: Scope, parameter: ParameterInfo
    ) -> Resolution | None:
        function = scope.entity
        assert isinstance(function, FunctionEntity)
        info = function.info
        if (
            info.kind == FunctionKind.METHOD
            and parameter.name == _first_parameter(info)
            and not _is_static(info)
        ):
            owner = self.owner_class(function)
            if owner is None:
                return None
            if _is_classmethod(info):
                return Resolution(ClassValue(owner.node_id), inferred=True)
            return Resolution(InstanceValue(owner.node_id), inferred=True)
        if parameter.annotation is None:
            return None
        # Annotations are evaluated in the scope that encloses the function.
        return self._instance_from_annotation(file, chain[1:], parameter.annotation)

    def _assignment_evidence(
        self, file: FileIndex, chain: Chain, assignments: list[AssignmentInfo]
    ) -> list[Resolution | None]:
        annotated = [a for a in assignments if a.annotation is not None]
        if annotated:
            # A declared type wins over individual assignments, as in type checkers.
            return [self._instance_from_annotation(file, chain, a.annotation) for a in annotated]
        evidence: list[Resolution | None] = []
        for assignment in assignments:
            if assignment.value_call is not None:
                called = self.resolve_expression(file, chain, assignment.value_call)
                if isinstance(called.value, ClassValue):
                    instance = InstanceValue(called.value.class_id)
                    evidence.append(Resolution(instance, inferred=True))
                else:
                    evidence.append(None)  # a function's return type is not inferred
            elif assignment.value_name is not None:
                # ``self.repo = repo`` copies the type of ``repo``, if that is known.
                copied = self.resolve_expression(file, chain, assignment.value_name)
                is_instance = isinstance(copied.value, InstanceValue)
                evidence.append(Resolution(copied.value, inferred=True) if is_instance else None)
            else:
                evidence.append(None)
        return evidence

    def _instance_from_annotation(
        self, file: FileIndex, chain: Chain, annotation: str | None
    ) -> Resolution | None:
        if annotation is None:
            return None
        text = annotation
        try:
            node = ast.parse(text, mode="eval").body
        except SyntaxError:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value  # string annotation: "UserService"
        resolved = self.resolve_expression(file, chain or (MODULE_SCOPE,), text)
        if isinstance(resolved.value, ClassValue):
            return Resolution(InstanceValue(resolved.value.class_id), inferred=True)
        return None

    def owner_class(self, function: FunctionEntity) -> ClassEntity | None:
        parent = function.info.parent_class
        if parent is None:
            return None
        return _pick_by_line(function.file.classes.get(parent, []), function.info.start_line)

    # --- attributes ---------------------------------------------------------

    def _attribute(self, base: Resolution, attribute: str) -> Resolution:
        value = base.value
        if isinstance(value, Unknown):
            return base
        if isinstance(value, ModuleValue):
            result = self._module_attribute(value.name, attribute)
        elif isinstance(value, ClassValue):
            result = self._class_attribute(value.class_id, attribute, instance=False)
        elif isinstance(value, InstanceValue):
            result = self._class_attribute(value.class_id, attribute, instance=True)
        elif isinstance(value, SuperValue):
            result = self._class_attribute(value.class_id, attribute, instance=True, skip_self=True)
        else:
            return _unknown(UnresolvedReason.ATTRIBUTE_NOT_FOUND)
        return Resolution(result.value, inferred=result.inferred or base.inferred)

    def _module_attribute(self, module: str, attribute: str) -> Resolution:
        files = self.index.modules.get(module, [])
        if len(files) > 1:
            return _unknown(UnresolvedReason.AMBIGUOUS)
        if files and self._has_bindings(files[0], MODULE_SCOPE, attribute):
            return self._interpret(files[0], (MODULE_SCOPE,), attribute)
        submodule = f"{module}.{attribute}"
        if self.index.is_known_module(submodule):
            return Resolution(ModuleValue(submodule))
        if self.index.is_namespace_only(module):
            return _unknown(UnresolvedReason.EXTERNAL_MODULE)
        return _unknown(UnresolvedReason.SYMBOL_NOT_FOUND)

    def _class_attribute(
        self, class_id: str, attribute: str, *, instance: bool, skip_self: bool = False
    ) -> Resolution:
        mro = self.mro(class_id)
        for entry in mro[1:] if skip_self else mro:
            if entry.startswith(_EXTERNAL_BASE):
                return _unknown(UnresolvedReason.INHERITED_FROM_EXTERNAL)
            entity = self.index.classes_by_id[entry]
            file = entity.file
            qualified = f"{entity.info.qualified_name}.{attribute}"
            members: list[Resolution] = [
                Resolution(FunctionValue(f.node_id)) for f in file.functions.get(qualified, [])
            ]
            members += [Resolution(ClassValue(c.node_id)) for c in file.classes.get(qualified, [])]
            class_bindings = file.assignments.get((entity.info.qualified_name, attribute), [])
            instance_bindings = (
                file.instance_attributes.get((entity.info.qualified_name, attribute), [])
                if instance
                else []
            )
            if members and not class_bindings and not instance_bindings:
                return Resolution(_merge(members).value, inferred=instance)
            if members:
                return _unknown(UnresolvedReason.AMBIGUOUS)  # method shadowed by an attribute
            if class_bindings or instance_bindings:
                return self._infer_attribute(entity, class_bindings, instance_bindings)
        return _unknown(UnresolvedReason.ATTRIBUTE_NOT_FOUND)

    def _infer_attribute(
        self,
        entity: ClassEntity,
        class_bindings: list[AssignmentInfo],
        instance_bindings: list[AssignmentInfo],
    ) -> Resolution:
        file = entity.file
        class_chain = self.scope_chain(file, entity.info.qualified_name, entity.info.start_line)
        pairs: list[tuple[AssignmentInfo, Chain]] = [(b, class_chain) for b in class_bindings]
        pairs += [(b, self.scope_chain(file, b.scope, b.line)) for b in instance_bindings]
        annotated = [(b, chain) for b, chain in pairs if b.annotation is not None]
        # A declared attribute type (``repo: Repository``) wins over individual assignments.
        evidence: list[Resolution | None] = []
        for binding, chain in annotated or pairs:
            evidence.extend(self._assignment_evidence(file, chain, [binding]))
        result = _agree(evidence)
        if isinstance(result.value, Unknown):
            return result
        return Resolution(result.value, inferred=True)

    def _super(self, file: FileIndex, chain: Chain) -> Resolution:
        scope = chain[0]
        if scope.kind != "function" or not isinstance(scope.entity, FunctionEntity):
            return _unknown(UnresolvedReason.DYNAMIC_EXPRESSION)
        owner = self.owner_class(scope.entity)
        if owner is None:
            return _unknown(UnresolvedReason.DYNAMIC_EXPRESSION)
        return Resolution(SuperValue(owner.node_id), inferred=True)

    # --- inheritance --------------------------------------------------------

    def class_bases(self, class_id: str) -> list[Resolution]:
        """Resolve each base expression of a class, in declaration order."""
        if class_id in self._bases_cache:
            return self._bases_cache[class_id]
        entity = self.index.classes_by_id[class_id]
        info = entity.info
        # Base expressions are evaluated in the scope that encloses the class.
        chain = self.scope_chain(
            entity.file, parent_qualified_name(info.qualified_name), info.start_line
        )
        results: list[Resolution] = []
        for base in info.bases:
            resolved = self.resolve_expression(entity.file, chain, base)
            value = resolved.value
            if isinstance(value, ClassValue) and value.class_id == class_id:
                resolved = _unknown(UnresolvedReason.CIRCULAR_REFERENCE)
            elif not isinstance(value, ClassValue | Unknown):
                resolved = _unknown(UnresolvedReason.NOT_A_CLASS)
            results.append(resolved)
        self._bases_cache[class_id] = results
        return results

    def mro(self, class_id: str) -> list[str]:
        """C3 linearization. Unresolved bases appear as opaque ``?`` entries."""
        if class_id in self._mro_cache:
            return self._mro_cache[class_id]
        if class_id in self._mro_active:
            return [class_id, f"{_EXTERNAL_BASE}cycle:{class_id}"]
        self._mro_active.add(class_id)
        try:
            bases: list[str] = []
            for position, base in enumerate(self.class_bases(class_id)):
                if isinstance(base.value, ClassValue):
                    bases.append(base.value.class_id)
                else:
                    bases.append(f"{_EXTERNAL_BASE}{class_id}#{position}")
            sequences = [
                [base] if base.startswith(_EXTERNAL_BASE) else list(self.mro(base))
                for base in bases
            ]
            merged = _c3_merge([*sequences, list(bases)])
            result = [class_id, *merged] if merged is not None else [class_id, f"{_EXTERNAL_BASE}"]
        finally:
            self._mro_active.discard(class_id)
        self._mro_cache[class_id] = result
        return result

    # --- calls --------------------------------------------------------------

    def resolve_call(self, file: FileIndex, chain: Chain, expression: str) -> Resolution:
        """Resolve a call expression to the function that runs, or a reason why not."""
        callee = self.resolve_expression(file, chain, expression)
        value = callee.value
        if isinstance(value, FunctionValue | Unknown):
            return callee
        if isinstance(value, ClassValue):
            init = self._class_attribute(value.class_id, "__init__", instance=False)
            return self._callable_member(init, callee, UnresolvedReason.CLASS_WITHOUT_INIT)
        if isinstance(value, InstanceValue):
            call = self._class_attribute(value.class_id, "__call__", instance=True)
            return self._callable_member(call, callee, UnresolvedReason.NOT_CALLABLE)
        return _unknown(UnresolvedReason.NOT_CALLABLE)

    @staticmethod
    def _callable_member(
        member: Resolution, callee: Resolution, missing: UnresolvedReason
    ) -> Resolution:
        if isinstance(member.value, FunctionValue):
            return Resolution(member.value, inferred=member.inferred or callee.inferred)
        if isinstance(member.value, Unknown) and (
            member.value.reason == UnresolvedReason.ATTRIBUTE_NOT_FOUND
        ):
            return _unknown(missing)
        if isinstance(member.value, Unknown):
            return member
        return _unknown(UnresolvedReason.NOT_CALLABLE)

    # --- imports ------------------------------------------------------------

    def absolute_module(self, file: FileIndex, imp: ImportInfo) -> str | None:
        """The absolute module an import refers to; None if a relative import escapes."""
        if not imp.is_relative:
            return imp.module
        base = file.package_parts
        if base is None or imp.level > len(base):
            return None
        parts = base[: len(base) - (imp.level - 1)]
        if imp.module:
            parts = [*parts, *imp.module.split(".")]
        return ".".join(parts) if parts else None

    def _import_binding_value(self, file: FileIndex, imp: ImportInfo) -> Resolution:
        if imp.import_type == ImportType.IMPORT:
            module = imp.module or ""
            target = module if imp.alias else module.split(".")[0]
            if self.index.is_known_module(target):
                return Resolution(ModuleValue(target))
            return _unknown(self.index.missing_module_reason(target))
        module = self.absolute_module(file, imp)
        if module is None:
            return _unknown(UnresolvedReason.RELATIVE_IMPORT_BEYOND_ROOT)
        if not self.index.is_known_module(module):
            return _unknown(self.index.missing_module_reason(module))
        return self._module_attribute(module, imp.name or "")

    def import_target(self, file: FileIndex, imp: ImportInfo) -> ImportTarget:
        """The file an import statement loads, for IMPORTS relationships."""
        if imp.import_type == ImportType.IMPORT:
            return self._module_file(imp.module or "")
        module = self.absolute_module(file, imp)
        if module is None:
            return ImportTarget(None, UnresolvedReason.RELATIVE_IMPORT_BEYOND_ROOT)
        files = self.index.modules.get(module, [])
        name = imp.name or ""
        if name != "*":
            if len(files) == 1 and self._has_bindings(files[0], MODULE_SCOPE, name):
                return ImportTarget(files[0], entity_id=self._entity_id(module, name))
            submodule = f"{module}.{name}"
            if submodule in self.index.modules:
                return self._module_file(submodule)
        if files:
            return self._module_file(module)
        if self.index.is_namespace_only(module):
            # Nothing local matched: likely an installed package that shares the name.
            return ImportTarget(None, UnresolvedReason.EXTERNAL_MODULE)
        return ImportTarget(None, self.index.missing_module_reason(module))

    def _module_file(self, module: str) -> ImportTarget:
        files = self.index.modules.get(module, [])
        if len(files) == 1:
            return ImportTarget(files[0])
        if files:
            return ImportTarget(None, UnresolvedReason.AMBIGUOUS)
        if module in self.index.package_prefixes:
            return ImportTarget(None, UnresolvedReason.NAMESPACE_PACKAGE)
        return ImportTarget(None, self.index.missing_module_reason(module))

    def _entity_id(self, module: str, name: str) -> str | None:
        value = self._module_attribute(module, name).value
        if isinstance(value, ClassValue):
            return value.class_id
        if isinstance(value, FunctionValue):
            return value.function_id
        return None


# --- helpers ----------------------------------------------------------------


def _bound_name(imp: ImportInfo) -> str | None:
    if imp.import_type == ImportType.IMPORT:
        return imp.alias or (imp.module or "").split(".")[0]
    if imp.name == "*":
        return None
    return imp.alias or imp.name


def _merge(candidates: list[Resolution]) -> Resolution:
    """One distinct answer wins; several distinct answers are ambiguous."""
    if not candidates:
        return _unknown(UnresolvedReason.UNDEFINED_NAME)
    distinct = {candidate.value for candidate in candidates}
    if len(distinct) == 1:
        return Resolution(candidates[0].value, inferred=any(c.inferred for c in candidates))
    return _unknown(UnresolvedReason.AMBIGUOUS)


def _agree(evidence: list[Resolution | None]) -> Resolution:
    """Every binding must give the same type; any binding without evidence spoils it."""
    if not evidence or any(item is None for item in evidence):
        return _unknown(UnresolvedReason.UNKNOWN_TYPE)
    distinct = {item.value for item in evidence if item is not None}
    if len(distinct) != 1:
        return _unknown(UnresolvedReason.AMBIGUOUS)
    return Resolution(distinct.pop(), inferred=True)


def _c3_merge(sequences: list[list[str]]) -> list[str] | None:
    """Standard C3 merge; None when no consistent order exists."""
    sequences = [list(seq) for seq in sequences if seq]
    result: list[str] = []
    while sequences:
        for sequence in sequences:
            head = sequence[0]
            if not any(head in other[1:] for other in sequences):
                break
        else:
            return None
        result.append(head)
        sequences = [[item for item in seq if item != head] for seq in sequences]
        sequences = [seq for seq in sequences if seq]
    return result
