import logging

from pydantic import Field

from app.agents.schemas import SourceLocation
from app.tools.base import (
    BaseTool,
    ToolArguments,
    ToolContext,
    ToolExecutionError,
    ToolOutput,
    redact_secrets,
)

logger = logging.getLogger(__name__)

MAX_CONTENT_LINES = 60


class RetrieveEvidenceArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=1000, description="A question or description in natural language.")
    limit: int = Field(default=5, ge=1, le=10)


class RetrieveEvidenceTool(BaseTool):
    name = "retrieve_evidence"
    description = (
        "Hybrid code retrieval over the repository's index: keyword, vector and code-graph search "
        "fused by rank. Use it first, to find code relevant to a bug description or a question "
        "such as 'Where is the user loaded?'. Returns ranked code chunks with file path, line "
        "range, qualified name, source, the strategies that found each chunk and related graph "
        "entities. Relevance is not proof that a chunk causes the bug."
    )
    Arguments = RetrieveEvidenceArguments

    def execute(self, arguments: RetrieveEvidenceArguments, context: ToolContext) -> ToolOutput:
        if context.retrieval is None:
            raise ToolExecutionError("Retrieval is not available for this repository; use search_code instead")
        from app.retrieval.service import (
            InvalidRetrievalRequestError,
            RepositoryNotIndexedError,
        )

        try:
            response = context.retrieval.retrieve_code(context.repository_id, arguments.query, arguments.limit)
        except RepositoryNotIndexedError:
            raise ToolExecutionError("The repository has no retrieval index; use search_code instead") from None
        except InvalidRetrievalRequestError as exc:
            raise ToolExecutionError(str(exc)) from None

        results = []
        locations = []
        for item in response.results:
            lines = item.content.splitlines()
            shown = lines[:MAX_CONTENT_LINES]
            end_line = item.start_line + len(shown) - 1 if shown else item.start_line
            results.append(
                {
                    "rank": len(results) + 1,
                    "file_path": item.file_path,
                    "qualified_name": item.qualified_name,
                    "entity_type": str(item.entity_type) if item.entity_type else None,
                    "start_line": item.start_line,
                    "end_line": item.end_line,
                    "score": round(item.score, 5),
                    "retrieval_sources": [str(s) for s in item.retrieval_sources],
                    "reason": item.reason,
                    "graph_context": [
                        {"relation": g.relation, "seed": g.seed_entity_id, "confidence": str(g.confidence)}
                        for g in item.graph_relationships[:5]
                    ],
                    "content": redact_secrets(
                        "\n".join(f"{item.start_line + i:>5} | {line}" for i, line in enumerate(shown))
                    ),
                    "content_truncated": len(lines) > len(shown),
                }
            )
            locations.append(SourceLocation(file_path=item.file_path, start_line=item.start_line, end_line=end_line))
        return ToolOutput(
            data={
                "query": response.query,
                "results": results,
                "strategies": [
                    {"source": str(s.source), "status": s.status, "candidates": s.candidates}
                    for s in response.strategies
                ],
                "note": response.note,
            },
            locations=locations,
        )
