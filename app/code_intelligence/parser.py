"""Read Python files and turn source text into an AST without executing it.

Reading and parsing fail in different ways, so they raise different exceptions:
``SourceReadError`` for disk and encoding problems, ``SourceParseError`` for
invalid Python. ``ast.parse`` only builds a tree; it never runs the code.
"""

import ast
import io
import tokenize
import warnings
from pathlib import Path

from app.code_intelligence.schemas import ErrorType, ParseError

DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024


class SourceReadError(Exception):
    """The file could not be read or decoded."""

    def __init__(self, file_path: str, message: str, error_type: ErrorType) -> None:
        super().__init__(message)
        self.file_path = file_path
        self.message = message
        self.error_type = error_type

    def to_error(self) -> ParseError:
        return ParseError(error_type=self.error_type, message=self.message, file_path=self.file_path)


class SourceParseError(Exception):
    """The source text is not valid Python."""

    def __init__(
        self, file_path: str, message: str, line: int | None = None, column: int | None = None
    ) -> None:
        super().__init__(message)
        self.file_path = file_path
        self.message = message
        self.line = line
        self.column = column

    def to_error(self) -> ParseError:
        return ParseError(
            error_type=ErrorType.SYNTAX_ERROR,
            message=self.message,
            file_path=self.file_path,
            line=self.line,
            column=self.column,
        )


def read_source(
    path: Path, display_name: str | None = None, *, max_bytes: int = DEFAULT_MAX_FILE_BYTES
) -> str:
    """Read a Python file as text, honouring a PEP 263 ``# coding:`` declaration.

    Args:
        path: File to read.
        display_name: Name used in errors, normally the repository-relative path.
        max_bytes: Files larger than this are rejected instead of loaded into memory.

    Raises:
        SourceReadError: on I/O failure, oversize files, or undecodable bytes.
    """
    name = display_name or str(path)
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise SourceReadError(name, exc.strerror or str(exc), ErrorType.READ_ERROR) from exc
    if len(data) > max_bytes:
        raise SourceReadError(
            name, f"File exceeds the {max_bytes}-byte size limit", ErrorType.READ_ERROR
        )

    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        return data.decode(encoding)
    except (SyntaxError, LookupError, UnicodeDecodeError) as exc:
        raise SourceReadError(name, str(exc), ErrorType.DECODE_ERROR) from exc


def parse_source(source: str, file_path: str = "<unknown>") -> ast.Module:
    """Parse Python source into an AST. The source is never executed.

    Raises:
        SourceParseError: if the source is not valid Python. The error keeps the
            file path and the 1-based line and column reported by the compiler.
    """
    try:
        with warnings.catch_warnings():
            # Code such as "\d" in a normal string emits SyntaxWarning while parsing.
            # It is the analyzed repository's problem, not ours, so keep stderr quiet.
            warnings.simplefilter("ignore")
            return ast.parse(source, filename=file_path)
    except SyntaxError as exc:  # includes IndentationError and TabError
        raise SourceParseError(file_path, exc.msg, exc.lineno, exc.offset) from exc
    except (ValueError, RecursionError) as exc:  # null bytes, absurdly deep nesting
        raise SourceParseError(file_path, str(exc) or type(exc).__name__) from exc
