"""Turn a Task 9 ``RepositoryAnalysis`` into an in-memory ``CodeGraph``.

The builder has no storage dependencies. Its output is deterministic: the same
analysis always yields the same IDs, and nodes, relationships and unresolved
references are sorted by ID.

Containment: the Repository CONTAINS each File. A File CONTAINS every class and
every non-method function it declares, including nested ones. A Class HAS_METHOD
each method it defines. As a result, every Function has exactly one parent.

Relationships are created only for references the resolver can prove. Every
other reference becomes an ``UnresolvedReference``.
"""

import re
from collections import defaultdict
from pathlib import PurePath

from app.code_intelligence.schemas import (
    FileStatus,
    FunctionKind,
    ImportType,
    RepositoryAnalysis,
)
from app.knowledge_graph.models import (
    CodeGraph,
    GraphNode,
    GraphRelationship,
    NodeLabel,
    Properties,
    ReferenceKind,
    RelationshipType,
    ResolutionStatus,
    UnresolvedReason,
    UnresolvedReference,
    is_valid_repository_id,
    relationship_id,
    repository_node_id,
)
from app.knowledge_graph.resolver import (
    ClassEntity,
    ClassValue,
    FileIndex,
    FunctionEntity,
    FunctionValue,
    RepositoryIndex,
    Resolver,
    Unknown,
)


def derive_repository_id(repository_path: str) -> str:
    """A readable ID from the directory name: ``/code/Atlas-AI`` -> ``atlas-ai``."""
    name = PurePath(repository_path).name.lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", name).strip("-._")[:100]
    return slug or "repository"


def build_code_graph(analysis: RepositoryAnalysis, repository_id: str | None = None) -> CodeGraph:
    """Build the code graph for one analyzed repository.

    Args:
        analysis: Output of ``analyze_repository``.
        repository_id: Stable ID for the repository. Derived from the directory
            name when omitted.
    """
    repo_id = repository_id or derive_repository_id(analysis.repository_path)
    if not is_valid_repository_id(repo_id):
        raise ValueError(f"Invalid repository id: {repo_id!r}")
    return _GraphBuilder(analysis, repo_id).build()


class _GraphBuilder:
    def __init__(self, analysis: RepositoryAnalysis, repository_id: str) -> None:
        self.analysis = analysis
        self.repository_id = repository_id
        self.index = RepositoryIndex(analysis, repository_id)
        self.resolver = Resolver(self.index)
        self.nodes: list[GraphNode] = []
        self.relationships: list[GraphRelationship] = []
        self.unresolved: list[UnresolvedReference] = []
        self._used_ids: dict[str, int] = defaultdict(int)

    def build(self) -> CodeGraph:
        repo_node = repository_node_id(self.repository_id)
        self.nodes.append(
            GraphNode(
                id=repo_node,
                label=NodeLabel.REPOSITORY,
                properties={
                    "repository_id": self.repository_id,
                    "name": PurePath(self.analysis.repository_path).name,
                    "root_path": self.analysis.repository_path,
                },
            )
        )
        for file in self.index.files:
            self._add_file(file, repo_node)
        for file in self.index.files:
            self._add_imports(file)
            self._add_calls(file)
            self._add_inheritance(file)

        return CodeGraph(
            repository_id=self.repository_id,
            nodes=sorted(self.nodes, key=lambda n: n.id),
            relationships=sorted(self.relationships, key=lambda r: r.id),
            unresolved_references=sorted(self.unresolved, key=lambda u: u.id),
        )

    # --- nodes and containment ---------------------------------------------

    def _add_file(self, file: FileIndex, repo_node: str) -> None:
        properties: Properties = {
            "repository_id": self.repository_id,
            "path": file.path,
            "language": "python",
            "parse_status": str(file.parsed.status),
        }
        if file.module_name:
            properties["module"] = file.module_name
        if file.parsed.status == FileStatus.ERROR:
            properties["parse_errors"] = [error.message for error in file.parsed.errors]
        self.nodes.append(GraphNode(id=file.node_id, label=NodeLabel.FILE, properties=properties))
        self._relate(RelationshipType.CONTAINS, repo_node, file.node_id)

        for entities in file.classes.values():
            for entity in entities:
                self._add_class(entity)
        for entities in file.functions.values():
            for entity in entities:
                self._add_function(entity)

    def _add_class(self, entity: ClassEntity) -> None:
        info = entity.info
        properties: Properties = {
            "repository_id": self.repository_id,
            "file_path": entity.file.path,
            "name": info.name,
            "qualified_name": info.qualified_name,
            "line_start": info.start_line,
            "line_end": info.end_line,
            "bases": info.bases,
            "decorators": info.decorators,
        }
        self.nodes.append(GraphNode(id=entity.node_id, label=NodeLabel.CLASS, properties=properties))
        self._relate(RelationshipType.CONTAINS, entity.file.node_id, entity.node_id)

    def _add_function(self, entity: FunctionEntity) -> None:
        info = entity.info
        properties: Properties = {
            "repository_id": self.repository_id,
            "file_path": entity.file.path,
            "name": info.name,
            "qualified_name": info.qualified_name,
            "kind": str(info.kind),
            "line_start": info.start_line,
            "line_end": info.end_line,
            "is_async": info.is_async,
            "parameters": [parameter.name for parameter in info.parameters],
            "decorators": info.decorators,
        }
        if info.return_annotation is not None:
            properties["return_annotation"] = info.return_annotation
        if info.parent_class is not None:
            properties["parent_class"] = info.parent_class
        self.nodes.append(
            GraphNode(id=entity.node_id, label=NodeLabel.FUNCTION, properties=properties)
        )

        owner = self.resolver.owner_class(entity) if info.kind == FunctionKind.METHOD else None
        if owner is not None:
            self._relate(RelationshipType.HAS_METHOD, owner.node_id, entity.node_id)
        else:
            self._relate(RelationshipType.CONTAINS, entity.file.node_id, entity.node_id)

    # --- imports -----------------------------------------------------------

    def _add_imports(self, file: FileIndex) -> None:
        for imp in file.parsed.imports:
            target = self.resolver.import_target(file, imp)
            imported = imp.module if imp.import_type == ImportType.IMPORT else imp.name
            written = _written_import(imp.module, imp.name, imp.level, imp.import_type)
            if target.file is None:
                self._unresolved(
                    ReferenceKind.IMPORT,
                    target.reason or UnresolvedReason.MODULE_NOT_FOUND,
                    file.node_id,
                    file.path,
                    imp.line,
                    written,
                )
                continue
            if target.file.node_id == file.node_id:
                continue  # a module importing itself adds no information
            properties: Properties = {
                "line_number": imp.line,
                "import_type": str(imp.import_type),
                "import_name": imported or "",
                "is_relative": imp.is_relative,
                "resolution_status": str(ResolutionStatus.RESOLVED),
            }
            if imp.module:
                properties["module"] = imp.module
            if imp.alias:
                properties["alias"] = imp.alias
            if target.entity_id:
                properties["imported_entity_id"] = target.entity_id
            self._relate(
                RelationshipType.IMPORTS,
                file.node_id,
                target.file.node_id,
                properties,
                discriminator=f"{imp.line}:{imported}",
            )

    # --- calls -------------------------------------------------------------

    def _add_calls(self, file: FileIndex) -> None:
        # One CALLS relationship per caller/callee pair, with every call site listed.
        aggregated: dict[tuple[str, str], list[tuple[int, str, bool]]] = {}
        for call in file.parsed.calls:
            caller = (
                self.resolver.entity_at(file, call.caller, call.line)
                if call.caller is not None
                else None
            )
            if not isinstance(caller, FunctionEntity):
                source = caller.node_id if caller is not None else file.node_id
                self._unresolved(
                    ReferenceKind.CALL,
                    UnresolvedReason.NON_FUNCTION_CALLER,
                    source,
                    file.path,
                    call.line,
                    call.expression,
                )
                continue

            chain = self.resolver.scope_chain(file, call.caller, call.line)
            resolution = self.resolver.resolve_call(file, chain, call.expression)
            value = resolution.value
            if isinstance(value, FunctionValue):
                sites = aggregated.setdefault((caller.node_id, value.function_id), [])
                sites.append((call.line, call.expression, resolution.inferred))
            else:
                assert isinstance(value, Unknown)
                self._unresolved(
                    ReferenceKind.CALL,
                    value.reason,
                    caller.node_id,
                    file.path,
                    call.line,
                    call.expression,
                )

        for (source, target), sites in aggregated.items():
            status = (
                ResolutionStatus.INFERRED
                if all(inferred for _, _, inferred in sites)
                else ResolutionStatus.RESOLVED
            )
            self._relate(
                RelationshipType.CALLS,
                source,
                target,
                {
                    "line_number": sites[0][0],
                    "line_numbers": [line for line, _, _ in sites],
                    "call_expression": sites[0][1],
                    "call_count": len(sites),
                    "resolution_status": str(status),
                },
            )

    # --- inheritance -------------------------------------------------------

    def _add_inheritance(self, file: FileIndex) -> None:
        for entities in file.classes.values():
            for entity in entities:
                bases = self.resolver.class_bases(entity.node_id)
                for position, (expression, resolution) in enumerate(
                    zip(entity.info.bases, bases, strict=True)
                ):
                    value = resolution.value
                    if isinstance(value, ClassValue):
                        self._relate(
                            RelationshipType.EXTENDS,
                            entity.node_id,
                            value.class_id,
                            {
                                "line_number": entity.info.start_line,
                                "base_expression": expression,
                                "position": position,
                                "resolution_status": str(
                                    ResolutionStatus.INFERRED
                                    if resolution.inferred
                                    else ResolutionStatus.RESOLVED
                                ),
                            },
                        )
                    else:
                        assert isinstance(value, Unknown)
                        self._unresolved(
                            ReferenceKind.INHERITANCE,
                            value.reason,
                            entity.node_id,
                            file.path,
                            entity.info.start_line,
                            expression,
                        )

    # --- helpers -----------------------------------------------------------

    def _unique(self, base: str) -> str:
        count = self._used_ids[base]
        self._used_ids[base] += 1
        return base if count == 0 else f"{base}#{count + 1}"

    def _relate(
        self,
        relationship_type: RelationshipType,
        source_id: str,
        target_id: str,
        properties: Properties | None = None,
        discriminator: str = "",
    ) -> None:
        rel_id = self._unique(
            relationship_id(relationship_type, source_id, target_id, discriminator)
        )
        self.relationships.append(
            GraphRelationship(
                id=rel_id,
                type=relationship_type,
                source_id=source_id,
                target_id=target_id,
                properties=properties or {},
            )
        )

    def _unresolved(
        self,
        kind: ReferenceKind,
        reason: UnresolvedReason,
        source_id: str,
        file_path: str,
        line: int,
        expression: str,
    ) -> None:
        ref_id = self._unique(f"unresolved:{kind}:{source_id}@{line}:{expression}")
        self.unresolved.append(
            UnresolvedReference(
                id=ref_id,
                kind=kind,
                reason=reason,
                source_id=source_id,
                file_path=file_path,
                line_number=line,
                expression=expression,
            )
        )


def _written_import(module: str | None, name: str | None, level: int, kind: ImportType) -> str:
    if kind == ImportType.IMPORT:
        return f"import {module}"
    return f"from {'.' * level}{module or ''} import {name}"
