from pydantic import Field

from app.agents.schemas import SourceLocation
from app.tools.base import (
    BaseTool,
    ToolArguments,
    ToolContext,
    ToolOutput,
    iter_repository_files,
    redact_secrets,
    resolve_in_repository,
)

SEARCHABLE_SUFFIXES = (
    ".py", ".pyi", ".md", ".rst", ".txt", ".toml", ".cfg", ".ini", ".json", ".yaml", ".yml",
)
MAX_MATCH_CHARS = 240


class SearchCodeArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=200, description="Exact text to find, e.g. an identifier.")
    path: str | None = Field(
        default=None, max_length=500, description="Optional directory or file to search, relative to the root."
    )
    case_sensitive: bool = Field(default=True)
    max_results: int = Field(default=50, ge=1, le=100)


class SearchCodeTool(BaseTool):
    name = "search_code"
    description = (
        "Searches the repository's source and text files for an exact text pattern (an identifier, "
        "an error message, a string literal) and returns each matching file path, line number and "
        "line text. Literal substring match, not a regular expression and not semantic search: "
        "use retrieve_evidence for questions in natural language. Optionally restrict to a "
        "directory or file with 'path'."
    )
    Arguments = SearchCodeArguments

    def execute(self, arguments: SearchCodeArguments, context: ToolContext) -> ToolOutput:
        root = context.repository_root
        scope_prefix = None
        single_file = None
        if arguments.path:
            resolved, relative = resolve_in_repository(root, arguments.path, allow_directory=True)
            if resolved.is_dir():
                scope_prefix = "" if relative == "." else relative.rstrip("/") + "/"
            else:
                single_file = relative

        needle = arguments.query if arguments.case_sensitive else arguments.query.lower()
        matches: list[dict] = []
        files_with_matches: set[str] = set()
        files_searched = 0
        truncated = False
        for path, relative in iter_repository_files(root, context):
            if single_file is not None and relative != single_file:
                continue
            if scope_prefix and not relative.startswith(scope_prefix):
                continue
            if not relative.endswith(SEARCHABLE_SUFFIXES):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            files_searched += 1
            for number, line in enumerate(text.splitlines(), start=1):
                haystack = line if arguments.case_sensitive else line.lower()
                if needle in haystack:
                    if len(matches) >= arguments.max_results:
                        truncated = True
                        break
                    snippet = line.strip()
                    if len(snippet) > MAX_MATCH_CHARS:
                        snippet = snippet[:MAX_MATCH_CHARS] + "..."
                    matches.append({"file_path": relative, "line": number, "text": redact_secrets(snippet)})
                    files_with_matches.add(relative)
            if truncated:
                break

        return ToolOutput(
            data={
                "query": arguments.query,
                "matches": matches,
                "total_matches": len(matches),
                "files_with_matches": len(files_with_matches),
                "files_searched": files_searched,
                "truncated": truncated,
            },
            locations=[
                SourceLocation(file_path=m["file_path"], start_line=m["line"], end_line=m["line"])
                for m in matches
            ],
        )
