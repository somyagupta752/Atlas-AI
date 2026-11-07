"""The central list of tools the agent may use.

Tools are registered once, at startup. The registry rejects duplicate names,
publishes only the schemas of permitted tools, and validates arguments before
anything runs. It knows nothing about the LLM.
"""

from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from app.tools.base import BaseTool, ToolContext, ToolOutput


class ToolRegistryError(Exception):
    pass


class DuplicateToolError(ToolRegistryError):
    pass


class UnknownToolError(ToolRegistryError):
    pass


class InvalidToolArgumentsError(ToolRegistryError):
    def __init__(self, tool: str, errors: list[str]) -> None:
        super().__init__(f"Invalid arguments for {tool}: " + "; ".join(errors))
        self.errors = errors


def describe_validation_error(exc: ValidationError) -> list[str]:
    """Field-level messages without echoing the (untrusted, possibly large) input."""
    messages = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in error["loc"]) or "arguments"
        messages.append(f"{location}: {error['msg']}")
    return messages[:10]


class ToolRegistry:
    def __init__(self, tools: Iterable[BaseTool] = ()) -> None:
        self._tools: dict[str, BaseTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: BaseTool) -> None:
        if not getattr(tool, "name", None) or not getattr(tool, "description", None):
            raise ToolRegistryError("A tool needs a name and a description")
        if tool.name in self._tools:
            raise DuplicateToolError(f"A tool named {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise UnknownToolError(f"Unknown tool: {str(name)[:100]!r}")
        return tool

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def get_schemas(self, allowed: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Schemas of registered tools, optionally restricted. Sorted, so prompts cache well."""
        permitted = set(self._tools) if allowed is None else set(allowed) & set(self._tools)
        return [self._tools[name].schema() for name in sorted(permitted)]

    def validate(self, name: str, arguments: Any) -> tuple[BaseTool, Any]:
        tool = self.get(name)
        if not isinstance(arguments, dict):
            raise InvalidToolArgumentsError(name, ["arguments must be a JSON object"])
        try:
            return tool, tool.validate(arguments)
        except ValidationError as exc:
            raise InvalidToolArgumentsError(name, describe_validation_error(exc)) from None

    def execute(self, name: str, arguments: Any, context: ToolContext) -> ToolOutput:
        """Validate, then run. Raises registry errors or the tool's own errors."""
        tool, validated = self.validate(name, arguments)
        return tool.execute(validated, context)


def create_default_registry() -> ToolRegistry:
    from app.tools.get_callees import GetCalleesTool
    from app.tools.get_callers import GetCallersTool
    from app.tools.get_symbol import GetSymbolTool
    from app.tools.read_file import ReadFileTool
    from app.tools.retrieve_evidence import RetrieveEvidenceTool
    from app.tools.run_tests import RunTestsTool
    from app.tools.search_code import SearchCodeTool

    return ToolRegistry(
        [
            RetrieveEvidenceTool(),
            SearchCodeTool(),
            ReadFileTool(),
            GetSymbolTool(),
            GetCallersTool(),
            GetCalleesTool(),
            RunTestsTool(),
        ]
    )


DEFAULT_TOOL_NAMES = (
    "get_callees",
    "get_callers",
    "get_symbol",
    "read_file",
    "retrieve_evidence",
    "run_tests",
    "search_code",
)
