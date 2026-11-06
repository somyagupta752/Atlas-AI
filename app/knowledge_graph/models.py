"""Storage-independent models for the code knowledge graph.

Identity scheme. Every ID embeds the repository ID, so two repositories (or two
files) that both define ``UserService`` never collide::

    repository:atlas-ai
    file:atlas-ai:app/services/user.py
    class:atlas-ai:app/services/user.py::UserService
    function:atlas-ai:app/services/user.py::UserService.find_user

Python allows a name to be defined twice in one file (``@property`` getter and
setter, ``if``/``else`` definitions). Such duplicates get an ``@L<line>`` suffix,
so the IDs stay unique and are still stable for unchanged source.

Property values are restricted to what Neo4j can store natively: strings,
integers, floats, booleans, and homogeneous lists of those. Missing values are
omitted rather than stored as ``None``.
"""

import re
from enum import StrEnum
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    field_validator,
)

REPOSITORY_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,99}$"
_REPOSITORY_ID_RE = re.compile(REPOSITORY_ID_PATTERN)

# Keys the persistence layer manages itself. Builders must not set them.
RESERVED_PROPERTY_KEYS = frozenset({"id", "build_id"})

NonEmptyId = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S(.*\S)?$")]
RepositoryId = Annotated[str, StringConstraints(pattern=REPOSITORY_ID_PATTERN)]

PropertyValue = (
    StrictStr
    | StrictBool
    | StrictInt
    | StrictFloat
    | list[StrictStr]
    | list[StrictBool]
    | list[StrictInt]
    | list[StrictFloat]
)
Properties = dict[str, PropertyValue]


class NodeLabel(StrEnum):
    REPOSITORY = "Repository"
    FILE = "File"
    CLASS = "Class"
    FUNCTION = "Function"


class RelationshipType(StrEnum):
    CONTAINS = "CONTAINS"
    HAS_METHOD = "HAS_METHOD"
    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    EXTENDS = "EXTENDS"


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"  # follows directly from definitions and imports
    INFERRED = "inferred"  # relies on type evidence, e.g. ``x = Service()`` then ``x.run()``


class ReferenceKind(StrEnum):
    IMPORT = "import"
    CALL = "call"
    INHERITANCE = "inheritance"


class UnresolvedReason(StrEnum):
    EXTERNAL_MODULE = "external_module"  # module is not part of this repository
    MODULE_NOT_FOUND = "module_not_found"  # looks local, but no such module exists
    RELATIVE_IMPORT_BEYOND_ROOT = "relative_import_beyond_root"
    NAMESPACE_PACKAGE = "namespace_package"  # a package directory with no file to point at
    SYMBOL_NOT_FOUND = "symbol_not_found"
    AMBIGUOUS = "ambiguous"  # several candidate targets; refusing to guess
    BUILTIN = "builtin"
    UNDEFINED_NAME = "undefined_name"
    STAR_IMPORT = "star_import"  # name may come from ``from x import *``
    UNKNOWN_TYPE = "unknown_type"  # receiver's type cannot be inferred statically
    INHERITED_FROM_EXTERNAL = "inherited_from_external"  # could live in an unresolved base
    ATTRIBUTE_NOT_FOUND = "attribute_not_found"
    CLASS_WITHOUT_INIT = "class_without_init"
    NOT_CALLABLE = "not_callable"
    NOT_A_CLASS = "not_a_class"
    DYNAMIC_EXPRESSION = "dynamic_expression"  # e.g. handlers[key]() or get()()
    NON_FUNCTION_CALLER = "non_function_caller"  # call at module or class-body level
    CIRCULAR_REFERENCE = "circular_reference"


class GraphNode(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: NonEmptyId
    label: NodeLabel
    properties: Properties = Field(default_factory=dict)

    @field_validator("properties")
    @classmethod
    def _no_reserved_keys(cls, value: Properties) -> Properties:
        return _check_reserved(value)


class GraphRelationship(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: NonEmptyId
    type: RelationshipType
    source_id: NonEmptyId
    target_id: NonEmptyId
    properties: Properties = Field(default_factory=dict)

    @field_validator("properties")
    @classmethod
    def _no_reserved_keys(cls, value: Properties) -> Properties:
        return _check_reserved(value)


class UnresolvedReference(BaseModel):
    """A reference the resolver refused to turn into a relationship.

    Kept apart from relationships so the graph never contains a guessed edge.
    """

    model_config = ConfigDict(frozen=True)

    id: NonEmptyId
    kind: ReferenceKind
    reason: UnresolvedReason
    source_id: NonEmptyId  # the node where the reference appears
    file_path: str
    line_number: int
    expression: str  # the reference as written, e.g. "self.client.fetch" or "pandas"


class CodeGraph(BaseModel):
    repository_id: RepositoryId
    nodes: list[GraphNode] = Field(default_factory=list)
    relationships: list[GraphRelationship] = Field(default_factory=list)
    unresolved_references: list[UnresolvedReference] = Field(default_factory=list)


def _check_reserved(properties: Properties) -> Properties:
    reserved = RESERVED_PROPERTY_KEYS.intersection(properties)
    if reserved:
        raise ValueError(f"reserved property keys: {sorted(reserved)}")
    return properties


# --- identity -------------------------------------------------------------


def is_valid_repository_id(value: str) -> bool:
    return bool(_REPOSITORY_ID_RE.match(value))


def repository_node_id(repository_id: str) -> str:
    return f"repository:{repository_id}"


def file_node_id(repository_id: str, path: str) -> str:
    return f"file:{repository_id}:{path}"


def class_node_id(repository_id: str, path: str, qualified_name: str) -> str:
    return f"class:{repository_id}:{path}::{qualified_name}"


def function_node_id(repository_id: str, path: str, qualified_name: str) -> str:
    return f"function:{repository_id}:{path}::{qualified_name}"


def relationship_id(
    relationship_type: RelationshipType, source_id: str, target_id: str, discriminator: str = ""
) -> str:
    base = f"{relationship_type}:{source_id}->{target_id}"
    return f"{base}:{discriminator}" if discriminator else base
