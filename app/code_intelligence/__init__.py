"""Deterministic Python code intelligence built on the standard-library ``ast`` module.

Pipeline: scanner (find files) -> parser (source to AST) -> extractor (AST to
structures) -> analyzer (orchestrate a repository). Nothing here imports or
executes the analyzed code, and nothing here depends on FastAPI.
"""

from app.code_intelligence.analyzer import (
    analyze_file,
    analyze_repository,
    analyze_source,
)
from app.code_intelligence.scanner import (
    InvalidRepositoryError,
    RepositoryNotADirectoryError,
    RepositoryNotFoundError,
)

__all__ = [
    "InvalidRepositoryError",
    "RepositoryNotADirectoryError",
    "RepositoryNotFoundError",
    "analyze_file",
    "analyze_repository",
    "analyze_source",
]
