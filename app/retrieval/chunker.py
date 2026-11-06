"""AST-aware chunking: one chunk per class, function, method and module segment.

Chunk boundaries come from Task 9's analysis, never from a token count, so a
function is never cut away from its signature unless it is larger than the
configured maximum. Chunks overlap on purpose: a class chunk contains its
methods, and a method chunk stands alone. The class keeps the context that a
method lacks, such as where ``self.repository`` is assigned.

Rules:
- Function and method chunks start at their first decorator, so route
  decorators such as ``@app.get("/users")`` stay with the handler. The ``def``
  line is kept in ``metadata["definition_line"]``.
- A class larger than the maximum becomes a header chunk: the class line up to
  its first method, with the method names in metadata. Its methods are chunks
  of their own anyway.
- A function larger than the maximum is split into consecutive line windows
  (``#part2``, ``#part3`` ...). Every window keeps the signature in metadata.
- Module-level code (imports, constants, ``app = FastAPI()``) becomes module
  chunks: each run of consecutive top-level statements that are not
  definitions.
- A file with a syntax error yields line-window module chunks with the parse
  error in metadata. The code is still searchable, which matters for bugs.
- An empty or whitespace-only file yields no chunks.

Entity IDs come from the Task 10 ``RepositoryIndex``, so ``entity_id`` matches
the graph node exactly, including the ``@L<line>`` suffix for redefinitions.
"""

import ast
import re
from dataclasses import dataclass, field
from typing import Any

from app.code_intelligence.parser import SourceParseError, parse_source
from app.code_intelligence.schemas import (
    ClassInfo,
    FileStatus,
    FunctionInfo,
    FunctionKind,
    ParsedFile,
    RepositoryAnalysis,
)
from app.knowledge_graph.resolver import FileIndex, RepositoryIndex
from app.retrieval.models import ChunkType, CodeChunk, chunk_id_for_entity

DEFAULT_MAX_CHUNK_LINES = 150
DEFAULT_MAX_CHUNK_CHARS = 12_000

_LINE_BREAK = re.compile(r"\r\n|\r|\n")  # the line breaks Python's tokenizer counts

_FUNCTION_CHUNK_TYPES: dict[FunctionKind, ChunkType] = {
    FunctionKind.FUNCTION: ChunkType.FUNCTION,
    FunctionKind.METHOD: ChunkType.METHOD,
    FunctionKind.NESTED_FUNCTION: ChunkType.NESTED_FUNCTION,
}


@dataclass(frozen=True)
class ChunkerConfig:
    max_lines: int = DEFAULT_MAX_CHUNK_LINES
    max_chars: int = DEFAULT_MAX_CHUNK_CHARS

    def __post_init__(self) -> None:
        if self.max_lines < 1 or self.max_chars < 1:
            raise ValueError("chunk limits must be positive")


@dataclass(frozen=True)
class SourceFile:
    """A file's text and its Task 9 analysis, produced from the same read."""

    path: str
    source: str
    parsed: ParsedFile


def chunk_repository(
    repository_id: str, files: list[SourceFile], config: ChunkerConfig | None = None
) -> list[CodeChunk]:
    """Chunk every file. Output is deterministic and sorted by path, then line."""
    config = config or ChunkerConfig()
    analysis = RepositoryAnalysis(
        repository_path="",
        total_files=len(files),
        successful_files=sum(1 for f in files if f.parsed.status == FileStatus.SUCCESS),
        failed_files=sum(1 for f in files if f.parsed.status == FileStatus.ERROR),
        files=[f.parsed for f in files],
    )
    index = RepositoryIndex(analysis, repository_id)
    by_path = {f.path: f for f in files}

    chunks: dict[str, CodeChunk] = {}
    for file_index in index.files:
        for chunk in _FileChunker(repository_id, by_path[file_index.path], file_index, config).run():
            chunks.setdefault(chunk.id, chunk)  # IDs are unique by construction; never duplicate
    return sorted(chunks.values(), key=lambda c: (c.file_path, c.start_line, c.end_line, c.id))


def chunk_source(
    repository_id: str, file_path: str, source: str, config: ChunkerConfig | None = None
) -> list[CodeChunk]:
    """Analyze and chunk a single file's source text."""
    from app.code_intelligence import analyze_source

    parsed = analyze_source(source, file_path)
    return chunk_repository(repository_id, [SourceFile(file_path, source, parsed)], config)


def split_lines(source: str) -> list[str]:
    """Split like the Python tokenizer, so index ``n - 1`` is AST line ``n``."""
    lines = _LINE_BREAK.split(source)
    if lines and lines[-1] == "":
        lines.pop()  # a trailing newline does not start a new line
    return lines


@dataclass
class _FileChunker:
    repository_id: str
    file: SourceFile
    index: FileIndex
    config: ChunkerConfig
    lines: list[str] = field(init=False)
    base_metadata: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        self.lines = split_lines(self.file.source)
        self.base_metadata = {"module": self.index.module_name} if self.index.module_name else {}

    def run(self) -> list[CodeChunk]:
        if not self.file.source.strip():
            return []
        tree = self._tree()
        if self.file.parsed.status == FileStatus.ERROR or tree is None:
            return self._unparsed_file_chunks()

        decorator_starts = _decorator_starts(tree)
        chunks: list[CodeChunk] = []
        for entities in self.index.classes.values():
            for entity in entities:
                chunks.extend(self._class_chunks(entity.node_id, entity.info, decorator_starts))
        for entities in self.index.functions.values():
            for entity in entities:
                chunks.extend(self._function_chunks(entity.node_id, entity.info, decorator_starts))
        chunks.extend(self._module_chunks(tree))
        return chunks

    def _tree(self) -> ast.Module | None:
        if self.file.parsed.status == FileStatus.ERROR:
            return None
        try:
            return parse_source(self.file.source, self.file.path)
        except SourceParseError:
            return None

    # --- entities ----------------------------------------------------------

    def _class_chunks(
        self, entity_id: str, info: ClassInfo, decorator_starts: dict[int, int]
    ) -> list[CodeChunk]:
        start = decorator_starts.get(info.start_line, info.start_line)
        end = info.end_line
        metadata = {
            **self.base_metadata,
            "definition_line": info.start_line,
            "bases": info.bases,
            "decorators": info.decorators,
            "methods": info.methods,
        }
        if info.parent_class:
            metadata["parent_class"] = info.parent_class

        if not self._fits(start, end):
            method_starts = [
                decorator_starts.get(f.start_line, f.start_line)
                for f in self.file.parsed.functions
                if f.parent_class == info.qualified_name
            ]
            header_end = min(method_starts) - 1 if method_starts else end
            header_end = self._trim_trailing_blank(start, max(header_end, info.start_line))
            metadata.update({"truncated": True, "full_end_line": end})
            end = header_end

        return self._windowed(entity_id, ChunkType.CLASS, info.qualified_name, start, end, metadata)

    def _function_chunks(
        self, entity_id: str, info: FunctionInfo, decorator_starts: dict[int, int]
    ) -> list[CodeChunk]:
        start = decorator_starts.get(info.start_line, info.start_line)
        metadata: dict[str, Any] = {
            **self.base_metadata,
            "definition_line": info.start_line,
            "signature": self.lines[info.start_line - 1].strip(),
            "parameters": [p.name for p in info.parameters],
            "decorators": info.decorators,
            "is_async": info.is_async,
        }
        if info.parent_class:
            metadata["parent_class"] = info.parent_class
        if info.return_annotation:
            metadata["return_annotation"] = info.return_annotation
        chunk_type = _FUNCTION_CHUNK_TYPES[info.kind]
        return self._windowed(entity_id, chunk_type, info.qualified_name, start, info.end_line, metadata)

    # --- module-level code -------------------------------------------------

    def _module_chunks(self, tree: ast.Module) -> list[CodeChunk]:
        """One chunk group per run of consecutive top-level non-definition statements."""
        runs: list[tuple[int, int]] = []
        current: tuple[int, int] | None = None
        for statement in tree.body:
            if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                if current:
                    runs.append(current)
                current = None
                continue
            end = statement.end_lineno or statement.lineno
            current = (current[0], end) if current else (statement.lineno, end)
        if current:
            runs.append(current)

        metadata = {**self.base_metadata, "segment": "module"}
        chunks: list[CodeChunk] = []
        for start, end in runs:
            chunks.extend(
                self._windowed(
                    self.index.node_id, ChunkType.MODULE, None, start, end, metadata, len(chunks)
                )
            )
        return chunks

    def _unparsed_file_chunks(self) -> list[CodeChunk]:
        metadata = {
            **self.base_metadata,
            "segment": "unparsed_file",
            "parse_status": str(FileStatus.ERROR),
            "parse_errors": [e.message for e in self.file.parsed.errors],
        }
        start = self._first_non_blank(1, len(self.lines))
        if start is None:
            return []
        return self._windowed(self.index.node_id, ChunkType.MODULE, None, start, len(self.lines), metadata)

    # --- windows -----------------------------------------------------------

    def _windowed(
        self,
        entity_id: str,
        chunk_type: ChunkType,
        qualified_name: str | None,
        start: int,
        end: int,
        metadata: dict[str, Any],
        part_offset: int = 0,
    ) -> list[CodeChunk]:
        windows = self._windows(start, end)
        chunks = []
        for number, (window_start, window_end) in enumerate(windows, start=1):
            content = "\n".join(self.lines[window_start - 1 : window_end])
            if not content.strip():
                continue
            part = part_offset + number
            chunk_metadata = dict(metadata)
            if len(windows) > 1:
                chunk_metadata.update({"part": number, "total_parts": len(windows)})
            chunks.append(
                CodeChunk(
                    id=chunk_id_for_entity(entity_id, part),
                    repository_id=self.repository_id,
                    file_path=self.file.path,
                    entity_id=entity_id,
                    entity_type=chunk_type,
                    qualified_name=qualified_name,
                    content=content,
                    start_line=window_start,
                    end_line=window_end,
                    metadata=chunk_metadata,
                )
            )
        return chunks

    def _windows(self, start: int, end: int) -> list[tuple[int, int]]:
        """Consecutive line ranges within both limits. A single over-long line stands alone."""
        windows: list[tuple[int, int]] = []
        window_start, chars = start, 0
        for line_number in range(start, end + 1):
            line_chars = len(self.lines[line_number - 1]) + 1
            lines_so_far = line_number - window_start
            if lines_so_far and (
                lines_so_far >= self.config.max_lines or chars + line_chars > self.config.max_chars
            ):
                windows.append((window_start, line_number - 1))
                window_start, chars = line_number, 0
            chars += line_chars
        windows.append((window_start, end))
        return windows

    def _fits(self, start: int, end: int) -> bool:
        if end - start + 1 > self.config.max_lines:
            return False
        return sum(len(line) + 1 for line in self.lines[start - 1 : end]) <= self.config.max_chars

    def _trim_trailing_blank(self, start: int, end: int) -> int:
        while end > start and not self.lines[end - 1].strip():
            end -= 1
        return end

    def _first_non_blank(self, start: int, end: int) -> int | None:
        for line_number in range(start, end + 1):
            if self.lines[line_number - 1].strip():
                return line_number
        return None


def _decorator_starts(tree: ast.Module) -> dict[int, int]:
    """Map each decorated definition's ``def``/``class`` line to its first decorator line."""
    starts: dict[int, int] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and node.decorator_list
        ):
            starts[node.lineno] = min(d.lineno for d in node.decorator_list)
    return starts
