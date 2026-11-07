"""Read-only debugging tools for the agent.

Every tool is scoped to one approved repository root through ``ToolContext``.
None of them modifies files. ``run_tests`` is the only one that executes code,
and it accepts a test path, never a command.
"""

from app.tools.base import (
    BaseTool,
    ToolContext,
    ToolExecutionError,
    ToolInputError,
    ToolOutput,
    ToolPermissionError,
)

__all__ = [
    "BaseTool",
    "ToolContext",
    "ToolExecutionError",
    "ToolInputError",
    "ToolOutput",
    "ToolPermissionError",
]
