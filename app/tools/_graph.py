"""Shared graph lookup for get_callers and get_callees (Task 10 graph).

The Neo4j graph is used when it is configured and holds this repository.
Otherwise the graph is built in memory from the current files with the same
Task 10 builder, so the tools still work, and the result says which was used.
"""

import logging
from typing import Any, Literal

from pydantic import Field

from app.agents.schemas import SourceLocation
from app.knowledge_graph.repository import GraphStoreError
from app.tools.base import (
    BaseTool,
    ToolArguments,
    ToolContext,
    ToolInputError,
    ToolOutput,
    validate_relative_path,
)

logger = logging.getLogger(__name__)

MAX_SEEDS = 5
MAX_NEIGHBOURS = 50


class CallGraphArguments(ToolArguments):
    qualified_name: str = Field(
        min_length=1, max_length=300, description="Function or method: 'get_user' or 'UserService.get_user'."
    )
    file_path: str | None = Field(default=None, max_length=500, description="Optional file that defines it.")


def _in_memory_source(context: ToolContext) -> Any:
    source = context._cache.get("in_memory_graph")
    if source is None:
        from app.knowledge_graph.builder import build_code_graph
        from app.retrieval.graph_search import InMemoryGraphContextSource

        source = InMemoryGraphContextSource(build_code_graph(context.analysis, context.repository_id))
        context._cache["in_memory_graph"] = source
    return source


def _find_functions(source: Any, context: ToolContext, name: str, file_path: str | None) -> list[dict[str, Any]]:
    records = source.find_entities_by_name(context.repository_id, [name], 50)
    found = [r for r in records if r.get("label") == "Function"]
    if file_path:
        found = [r for r in found if r["properties"].get("file_path") == file_path]
    return found


class CallGraphTool(BaseTool):
    direction: Literal["callers", "callees"]
    Arguments = CallGraphArguments

    def execute(self, arguments: CallGraphArguments, context: ToolContext) -> ToolOutput:
        file_path = validate_relative_path(arguments.file_path) if arguments.file_path else None
        name = arguments.qualified_name.strip()
        source, provenance, seeds = None, "in_memory", []
        if context.graph is not None:
            try:
                seeds = _find_functions(context.graph, context, name, file_path)
                if seeds:
                    source, provenance = context.graph, "neo4j"
            except GraphStoreError:
                logger.warning("Graph store unavailable; using an in-memory graph")
        if source is None:
            source = _in_memory_source(context)
            seeds = _find_functions(source, context, name, file_path)
        if not seeds:
            raise ToolInputError(f"No function named {name!r} is in the code graph")
        seeds = sorted(seeds, key=lambda r: r["id"])[:MAX_SEEDS]
        seed_ids = [s["id"] for s in seeds]
        records = source.expand_neighborhood(context.repository_id, seed_ids, 1, ["CALLS"], MAX_NEIGHBOURS)

        results: list[dict[str, Any]] = []
        locations: list[SourceLocation] = []
        for record in records:
            step = record["steps"][0]
            if self.direction == "callers" and step["target_id"] != record["seed_id"]:
                continue
            if self.direction == "callees" and step["source_id"] != record["seed_id"]:
                continue
            props = record["properties"]
            results.append(
                {
                    "of": _qualified(seeds, record["seed_id"]),
                    "qualified_name": props.get("qualified_name"),
                    "file_path": props.get("file_path"),
                    "start_line": props.get("line_start"),
                    "end_line": props.get("line_end"),
                    "kind": props.get("kind"),
                    "relationship": "CALLS",
                    "resolution_status": step.get("resolution_status"),
                }
            )
            if props.get("file_path") and props.get("line_start") and props.get("line_end"):
                locations.append(
                    SourceLocation(
                        file_path=props["file_path"], start_line=props["line_start"], end_line=props["line_end"]
                    )
                )
        targets = [
            {
                "qualified_name": s["properties"].get("qualified_name"),
                "file_path": s["properties"].get("file_path"),
                "start_line": s["properties"].get("line_start"),
                "end_line": s["properties"].get("line_end"),
            }
            for s in seeds
        ]
        for target in targets:
            if target["file_path"] and target["start_line"] and target["end_line"]:
                locations.append(
                    SourceLocation(
                        file_path=target["file_path"], start_line=target["start_line"], end_line=target["end_line"]
                    )
                )
        data: dict[str, Any] = {
            "targets": targets,
            self.direction: results,
            "total": len(results),
            "graph_source": provenance,
            "note": (
                "Static call graph. 'inferred' edges rely on type evidence. Calls the graph could "
                "not resolve are not edges."
            ),
        }
        if self.direction == "callees":
            data["unresolved_calls"] = [
                {
                    "expression": u["expression"],
                    "reason": u["reason"],
                    "line": u["line_number"],
                }
                for u in source.unresolved_for_entities(context.repository_id, seed_ids, MAX_NEIGHBOURS)
                if u.get("kind") == "call"
            ]
        return ToolOutput(data=data, locations=locations)


def _qualified(seeds: list[dict[str, Any]], seed_id: str) -> str | None:
    seed = next((s for s in seeds if s["id"] == seed_id), None)
    return seed["properties"].get("qualified_name") if seed else None
