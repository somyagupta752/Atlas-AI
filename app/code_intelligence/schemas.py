"""Typed response models for Python code intelligence.

These models describe *syntax*, not runtime behaviour. A call such as
``service.calculate()`` is recorded exactly as written; nothing here claims to
know which class actually owns ``calculate``.

Conventions:
- Paths are relative to the repository root and use forward slashes.
- Line numbers are 1-based, as reported by Python's ``ast`` module.
- Expressions (bases, annotations, defaults, decorators) are preserved as source
  text produced by ``ast.unparse``. They are never evaluated.
- Missing optional values are ``None``; collections are always lists.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ParameterKind(StrEnum):
    POSITIONAL_ONLY = "positional_only"
    POSITIONAL_OR_KEYWORD = "positional_or_keyword"
    VAR_POSITIONAL = "var_positional"  # *args
    KEYWORD_ONLY = "keyword_only"
    VAR_KEYWORD = "var_keyword"  # **kwargs


class FunctionKind(StrEnum):
    FUNCTION = "function"  # defined at module level
    METHOD = "method"  # defined directly in a class body
    NESTED_FUNCTION = "nested_function"  # defined inside another function


class ImportType(StrEnum):
    IMPORT = "import"  # import os / import pandas as pd
    FROM = "from"  # from app.database import get_db


class ErrorType(StrEnum):
    SCAN_ERROR = "scan_error"  # a directory could not be listed
    READ_ERROR = "read_error"  # the file could not be read from disk
    DECODE_ERROR = "decode_error"  # the bytes are not valid text in the declared encoding
    SYNTAX_ERROR = "syntax_error"  # the text is not valid Python
    ANALYSIS_ERROR = "analysis_error"  # parsing worked but extraction failed


class FileStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


class ParameterInfo(BaseModel):
    name: str
    kind: ParameterKind
    annotation: str | None = None
    default: str | None = None


class FunctionInfo(BaseModel):
    """A ``def`` or ``async def``, whether a module function, method or nested function."""

    name: str
    qualified_name: str  # follows __qualname__: "Outer.method.<locals>.helper"
    kind: FunctionKind
    parameters: list[ParameterInfo] = Field(default_factory=list)
    return_annotation: str | None = None
    decorators: list[str] = Field(default_factory=list)
    is_async: bool = False
    parent_class: str | None = None  # qualified name of the owning class, methods only
    start_line: int
    end_line: int


class ClassInfo(BaseModel):
    name: str
    qualified_name: str
    bases: list[str] = Field(default_factory=list)
    decorators: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)  # names of methods defined in the body
    parent_class: str | None = None  # set when the class is nested directly in a class
    start_line: int
    end_line: int


class ImportInfo(BaseModel):
    import_type: ImportType
    module: str | None  # None for "from . import x"
    name: str | None  # None for plain "import x"; "*" for star imports
    alias: str | None = None
    is_relative: bool = False
    level: int = 0  # number of leading dots in a relative import
    scope: str | None = None  # qualified name of the enclosing function/class; None at module level
    line: int


class AssignmentInfo(BaseModel):
    """A name binding: ``x = ...``, ``self.x = ...``, ``for x in``, ``with ... as x`` and similar.

    Only the evidence a static resolver can use is kept: the target as written and,
    for a plain ``target = something(...)``, the called expression. Values are never
    evaluated.
    """

    scope: str | None  # qualified name of the enclosing function/class; None at module level
    target: str  # "service" or "self.service"
    value_call: str | None = None  # "UserService" for ``service = UserService()``
    value_name: str | None = None  # "repo" for ``self.repo = repo``
    annotation: str | None = None  # "UserService" for ``service: UserService = ...``
    line: int


class CallInfo(BaseModel):
    """A syntactic call site. The runtime target is not resolved."""

    caller: str | None  # qualified name of the enclosing function/class; None at module level
    expression: str  # the called expression as written, e.g. "self.repository.find_by_id"
    name: str | None  # final name segment, e.g. "find_by_id"; None for calls like f()()
    receiver: str | None = None  # the object an attribute is called on, e.g. "self.repository"
    line: int


class InheritanceInfo(BaseModel):
    class_name: str  # qualified name of the subclass
    base: str  # base expression as written; may not resolve to anything in this repository
    relationship: Literal["EXTENDS"] = "EXTENDS"
    line: int


class ParseError(BaseModel):
    error_type: ErrorType
    message: str
    file_path: str
    line: int | None = None
    column: int | None = None


class ParsedFile(BaseModel):
    file_path: str
    status: FileStatus
    classes: list[ClassInfo] = Field(default_factory=list)
    functions: list[FunctionInfo] = Field(default_factory=list)
    imports: list[ImportInfo] = Field(default_factory=list)
    calls: list[CallInfo] = Field(default_factory=list)
    inheritance: list[InheritanceInfo] = Field(default_factory=list)
    assignments: list[AssignmentInfo] = Field(default_factory=list)
    errors: list[ParseError] = Field(default_factory=list)


class RepositoryAnalysis(BaseModel):
    repository_path: str
    total_files: int
    successful_files: int
    failed_files: int
    files: list[ParsedFile] = Field(default_factory=list)
    errors: list[ParseError] = Field(default_factory=list)  # repository-level (scan) errors
