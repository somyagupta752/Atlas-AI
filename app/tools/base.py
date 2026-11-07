"""The tool interface, the per-investigation context, and shared safety helpers.

Security rules every tool relies on:

- ``resolve_in_repository`` is the only way to turn a model-supplied path into
  a filesystem path. It rejects absolute paths, ``..`` segments, symlinks that
  leave the root and files that may hold secrets.
- ``redact_secrets`` removes obvious credentials from any text a tool returns.
- Tools raise ``ToolInputError`` (bad request), ``ToolPermissionError``
  (not allowed) or ``ToolExecutionError`` (failed). The executor turns each
  into a structured observation. Messages must never contain secrets.
"""

import fnmatch
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from app.agents.schemas import SourceLocation

if TYPE_CHECKING:
    from app.code_intelligence.schemas import RepositoryAnalysis
    from app.retrieval.graph_search import GraphContextSource
    from app.retrieval.models import RetrievalResponse


class ToolInputError(Exception):
    """The arguments are well-typed but unusable (missing file, bad range)."""


class ToolPermissionError(Exception):
    """The request is outside what this investigation may do."""


class ToolExecutionError(Exception):
    """The tool ran and failed, or a dependency is unavailable."""


class RetrievalPort(Protocol):
    def retrieve_code(self, repository_id: str, query: str, limit: int) -> "RetrievalResponse": ...


@dataclass
class ToolContext:
    """Everything a tool may use during one investigation. Nothing else is reachable."""

    repository_id: str
    repository_root: Path  # resolved, approved root
    retrieval: RetrievalPort | None = None
    graph: "GraphContextSource | None" = None
    test_execution_allowed: bool = False
    tool_timeout_seconds: float = 15.0
    max_output_bytes: int = 20_000
    # Cooperative deadline for long in-process scans (monotonic seconds).
    deadline: float | None = None
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.repository_root = self.repository_root.resolve()

    @cached_property
    def analysis(self) -> "RepositoryAnalysis":
        """Task 9 analysis of the repository, computed once per investigation."""
        from app.code_intelligence import analyze_repository

        return analyze_repository(self.repository_root)

    def check_deadline(self) -> None:
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise ToolExecutionError("Tool exceeded its time limit")


@dataclass
class ToolOutput:
    data: dict[str, Any]
    # File ranges shown to the model. Evidence may only cite these.
    locations: list[SourceLocation] = field(default_factory=list)


class ToolArguments(BaseModel):
    """Base for argument models: strict types, no extra fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


class BaseTool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    Arguments: ClassVar[type[ToolArguments]]

    @property
    def arguments_schema(self) -> dict[str, Any]:
        schema = self.Arguments.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return schema

    def schema(self) -> dict[str, Any]:
        """The provider-neutral definition sent to the LLM."""
        return {"name": self.name, "description": self.description, "parameters": self.arguments_schema}

    def validate(self, arguments: Any) -> ToolArguments:
        """Raises ``pydantic.ValidationError``."""
        return self.Arguments.model_validate(arguments)

    @abstractmethod
    def execute(self, arguments: Any, context: ToolContext) -> ToolOutput:
        """Run with validated arguments. Raise the ``Tool*Error`` types for failures."""


# --- paths ------------------------------------------------------------------------

IGNORED_DIRECTORIES = frozenset(
    {
        ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", ".tox", ".nox", ".eggs", "node_modules",
        "site-packages", "build", "dist", ".idea", ".vscode",
    }
)

# Files that commonly hold credentials. They are never read or searched.
SECRET_FILE_PATTERNS = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".netrc", ".pgpass", ".npmrc", ".pypirc",
    ".htpasswd", "*.kdbx",
)
# Broad name patterns, applied to non-source files only, so ``secrets.py`` stays readable
# (and redacted) while ``credentials.json`` or ``client_secret.txt`` do not.
SECRET_DATA_FILE_PATTERNS = ("*credentials*", "*secret*")
SOURCE_SUFFIXES = (".py", ".pyi", ".md", ".rst")
# ``.env.example`` documents variable names, not values. Still redacted on read.
SECRET_FILE_EXCEPTIONS = (".env.example", ".env.sample", ".env.template")

MAX_FILE_BYTES = 1_000_000


def is_secret_file(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    if name in SECRET_FILE_EXCEPTIONS:
        return False
    if any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_FILE_PATTERNS):
        return True
    return not name.endswith(SOURCE_SUFFIXES) and any(
        fnmatch.fnmatch(name, pattern) for pattern in SECRET_DATA_FILE_PATTERNS
    )


def validate_relative_path(raw: str) -> str:
    """Normalise a model-supplied path. Raises ``ToolPermissionError``/``ToolInputError``."""
    if not raw or not raw.strip():
        raise ToolInputError("Path must not be empty")
    if "\x00" in raw:
        raise ToolPermissionError("Path contains a NUL byte")
    candidate = raw.strip().replace("\\", "/")
    path = PurePosixPath(candidate)
    if path.is_absolute() or re.match(r"^[A-Za-z]:", candidate):
        raise ToolPermissionError("Only paths relative to the repository root are allowed")
    if ".." in path.parts:
        raise ToolPermissionError("Path traversal ('..') is not allowed")
    if any(part in IGNORED_DIRECTORIES for part in path.parts[:-1]):
        raise ToolPermissionError("Path is inside an excluded directory")
    normalised = path.as_posix()
    return "." if normalised in ("", ".") else normalised.removeprefix("./")


def resolve_in_repository(root: Path, raw: str, *, allow_directory: bool = False) -> tuple[Path, str]:
    """Resolve ``raw`` inside ``root``. Returns ``(absolute_path, relative_posix_path)``.

    The resolved path (all symlinks followed) must stay inside the root.
    """
    relative = validate_relative_path(raw)
    if relative != "." and is_secret_file(relative):
        raise ToolPermissionError("This file may contain secrets and cannot be read")
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ToolPermissionError("Path cannot be resolved") from exc
    if not resolved.is_relative_to(root):
        raise ToolPermissionError("Path resolves outside the repository")
    if resolved != root and is_secret_file(resolved.name):
        raise ToolPermissionError("This file may contain secrets and cannot be read")
    if not resolved.exists():
        raise ToolInputError(f"No such file in the repository: {relative}")
    if resolved.is_dir() and not allow_directory:
        raise ToolInputError(f"Path is a directory, not a file: {relative}")
    return resolved, resolved.relative_to(root).as_posix() or "."


def iter_repository_files(root: Path, context: ToolContext | None = None):
    """Yield ``(absolute, relative)`` for searchable text files, never leaving the root."""
    import os

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if context is not None:
            context.check_deadline()
        dirnames[:] = sorted(
            d for d in dirnames if d not in IGNORED_DIRECTORIES and not d.endswith(".egg-info")
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            relative = path.relative_to(root).as_posix()
            if is_secret_file(relative):
                continue
            try:
                if path.is_symlink() and not path.resolve().is_relative_to(root):
                    continue
                if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except (OSError, RuntimeError):
                continue
            yield path, relative


def read_text(path: Path) -> str:
    """Read a text file. Binary or oversized files raise ``ToolInputError``."""
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ToolInputError(f"File is larger than {MAX_FILE_BYTES} bytes")
        raw = path.read_bytes()
    except OSError as exc:
        raise ToolExecutionError(f"File cannot be read: {exc.strerror or type(exc).__name__}") from None
    if b"\x00" in raw[:8192]:
        raise ToolInputError("File looks binary")
    return raw.decode("utf-8", errors="replace")


# --- output hygiene ----------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\b(sk-ant-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # key = "value" style assignments for names that suggest a secret.
    re.compile(
        r"(?i)((?:api[_-]?key|secret|password|passwd|token|access[_-]?key|private[_-]?key)"
        r"[A-Za-z0-9_]*\s*[:=]\s*)(['\"])([^'\"\n]{6,})\2"
    ),
    # Credentials embedded in URLs: scheme://user:password@host
    re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)([^@\s/]{3,})(@)"),
]

REDACTED = "[REDACTED]"


def redact_secrets(text: str) -> str:
    text = _SECRET_PATTERNS[0].sub(REDACTED, text)
    text = _SECRET_PATTERNS[1].sub(REDACTED, text)
    text = _SECRET_PATTERNS[2].sub(REDACTED, text)
    text = _SECRET_PATTERNS[3].sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(2)}", text)
    text = _SECRET_PATTERNS[4].sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}", text)
    return text


def truncate_text(text: str, max_bytes: int, *, keep: str = "head") -> tuple[str, bool]:
    """Cut ``text`` to ``max_bytes`` of UTF-8. ``keep="tail"`` keeps the end (test output)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    marker = "\n...[truncated]...\n"
    budget = max(0, max_bytes - len(marker))
    if keep == "tail":
        return marker + encoded[-budget:].decode("utf-8", errors="ignore"), True
    return encoded[:budget].decode("utf-8", errors="ignore") + marker, True
