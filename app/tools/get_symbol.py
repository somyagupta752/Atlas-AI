from pathlib import PurePosixPath

from pydantic import Field

from app.agents.schemas import SourceLocation
from app.tools.base import (
    BaseTool,
    ToolArguments,
    ToolContext,
    ToolInputError,
    ToolOutput,
    read_text,
    redact_secrets,
    resolve_in_repository,
    validate_relative_path,
)

MAX_MATCHES = 10
MAX_SOURCE_LINES = 150


class GetSymbolArguments(ToolArguments):
    qualified_name: str = Field(
        min_length=1,
        max_length=300,
        description="Function or class name: 'get_user', 'UserService.get_user' or 'app.services.user.UserService'.",
    )
    file_path: str | None = Field(default=None, max_length=500, description="Optional file to restrict the lookup to.")
    include_source: bool = Field(default=True, description="Include the definition's source lines.")


def module_name(file_path: str) -> str:
    parts = list(PurePosixPath(file_path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def symbol_matches(query: str, qualified_name: str, name: str, file_path: str) -> bool:
    """Exact qualified name, bare name, a dotted suffix, or module-qualified name."""
    clean = qualified_name.replace(".<locals>", "")
    if query in (qualified_name, clean):
        return True
    if "." not in query:
        return query == name
    if clean.endswith("." + query):
        return True
    return f"{module_name(file_path)}.{clean}" == query


class GetSymbolTool(BaseTool):
    name = "get_symbol"
    description = (
        "Looks up a function, method or class definition by name using static AST analysis "
        "(the code is never imported or run). Returns each matching definition's kind, file, "
        "start and end lines, parent class, parameters, decorators, return annotation or base "
        "classes, and its source lines. Accepts a bare name ('get_user'), a qualified name "
        "('UserService.get_user') or a module path ('app.services.user.UserService')."
    )
    Arguments = GetSymbolArguments

    def execute(self, arguments: GetSymbolArguments, context: ToolContext) -> ToolOutput:
        restrict = validate_relative_path(arguments.file_path) if arguments.file_path else None
        query = arguments.qualified_name.strip()
        found: list[dict] = []
        for parsed in context.analysis.files:
            if restrict and parsed.file_path != restrict:
                continue
            for cls in parsed.classes:
                if symbol_matches(query, cls.qualified_name, cls.name, parsed.file_path):
                    found.append(
                        {
                            "type": "class",
                            "qualified_name": cls.qualified_name,
                            "file_path": parsed.file_path,
                            "start_line": cls.start_line,
                            "end_line": cls.end_line,
                            "parent_class": cls.parent_class,
                            "bases": cls.bases,
                            "decorators": cls.decorators,
                            "methods": cls.methods,
                        }
                    )
            for fn in parsed.functions:
                if symbol_matches(query, fn.qualified_name, fn.name, parsed.file_path):
                    found.append(
                        {
                            "type": str(fn.kind),
                            "qualified_name": fn.qualified_name,
                            "file_path": parsed.file_path,
                            "start_line": fn.start_line,
                            "end_line": fn.end_line,
                            "parent_class": fn.parent_class,
                            "parameters": [
                                {"name": p.name, "annotation": p.annotation, "default": p.default}
                                for p in fn.parameters
                            ],
                            "return_annotation": fn.return_annotation,
                            "decorators": fn.decorators,
                            "is_async": fn.is_async,
                        }
                    )
        if not found:
            raise ToolInputError(f"No function or class named {query!r} was found")
        found.sort(key=lambda d: (d["file_path"], d["start_line"]))
        ambiguous = len(found) > 1
        shown = found[:MAX_MATCHES]

        locations = []
        for definition in shown:
            end = definition["end_line"]
            if arguments.include_source:
                end = min(end, definition["start_line"] + MAX_SOURCE_LINES - 1)
                definition["source"], definition["source_truncated"] = self._source(context, definition, end)
            locations.append(
                SourceLocation(file_path=definition["file_path"], start_line=definition["start_line"], end_line=end)
            )
        return ToolOutput(
            data={
                "query": query,
                "definitions": shown,
                "total_matches": len(found),
                "ambiguous": ambiguous,
                "note": "Static analysis: describes the code as written, not runtime behaviour.",
            },
            locations=locations,
        )

    @staticmethod
    def _source(context: ToolContext, definition: dict, end: int) -> tuple[str, bool]:
        path, _ = resolve_in_repository(context.repository_root, definition["file_path"])
        lines = read_text(path).splitlines()
        start = definition["start_line"]
        rendered = [f"{n:>5} | {lines[n - 1]}" for n in range(start, min(end, len(lines)) + 1)]
        return redact_secrets("\n".join(rendered)), end < definition["end_line"]
