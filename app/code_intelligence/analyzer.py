"""Coordinate scanning, parsing and extraction for a whole repository.

Each file is handled independently: a read, decode, syntax or extraction
failure becomes a structured ``ParseError`` on that file, and the remaining
files are still analyzed. Files are processed sequentially.
"""

from pathlib import Path

from app.code_intelligence.extractor import extract_structure
from app.code_intelligence.parser import (
    SourceParseError,
    SourceReadError,
    parse_source,
    read_source,
)
from app.code_intelligence.scanner import scan_python_files
from app.code_intelligence.schemas import (
    ErrorType,
    FileStatus,
    ParsedFile,
    ParseError,
    RepositoryAnalysis,
)


def analyze_source(source: str, file_path: str) -> ParsedFile:
    """Parse and extract one module from source text."""
    try:
        tree = parse_source(source, file_path)
    except SourceParseError as exc:
        return _failed(file_path, exc.to_error())
    try:
        return extract_structure(tree, file_path)
    except Exception as exc:  # noqa: BLE001 - one bad file must not sink the repository
        error = ParseError(
            error_type=ErrorType.ANALYSIS_ERROR,
            message=f"{type(exc).__name__}: {exc}",
            file_path=file_path,
        )
        return _failed(file_path, error)


def analyze_file(root: Path, relative_path: str) -> ParsedFile:
    """Read and analyze ``root / relative_path``, reporting failures as data."""
    try:
        source = read_source(root / relative_path, display_name=relative_path)
    except SourceReadError as exc:
        return _failed(relative_path, exc.to_error())
    return analyze_source(source, relative_path)


def analyze_repository(repository_path: str | Path) -> RepositoryAnalysis:
    """Analyze every Python file in a repository without executing any of it.

    Raises:
        RepositoryNotFoundError: if the path does not exist.
        RepositoryNotADirectoryError: if the path is not a directory.
    """
    scan = scan_python_files(repository_path)
    files = [analyze_file(scan.root, relative_path) for relative_path in scan.files]
    successful = sum(1 for parsed in files if parsed.status == FileStatus.SUCCESS)
    scan_errors = [
        ParseError(error_type=ErrorType.SCAN_ERROR, message=issue.message, file_path=issue.path)
        for issue in scan.errors
    ]
    return RepositoryAnalysis(
        repository_path=str(scan.root),
        total_files=len(files),
        successful_files=successful,
        failed_files=len(files) - successful,
        files=files,
        errors=scan_errors,
    )


def _failed(file_path: str, error: ParseError) -> ParsedFile:
    return ParsedFile(file_path=file_path, status=FileStatus.ERROR, errors=[error])
