import pytest

from app.retrieval.embeddings import HashingEmbeddingProvider
from app.retrieval.keyword_search import KeywordSearcher
from app.retrieval.service import RetrievalService
from app.retrieval.vector_search import VectorSearcher
from app.tools.base import ToolExecutionError
from app.tools.retrieve_evidence import RetrieveEvidenceTool

TOOL = RetrieveEvidenceTool()


def run(context, **arguments):
    return TOOL.execute(TOOL.validate(arguments), context)


@pytest.fixture
def retrieval(db_session, chunk_repo):
    return RetrievalService(
        chunk_repo, KeywordSearcher(db_session), VectorSearcher(db_session, HashingEmbeddingProvider())
    )


def test_returns_ranked_chunks_with_locations(index_repo, retrieval, make_context):
    _result, root = index_repo(
        {"stats.py": "def calculate_average(numbers):\n    return sum(numbers) / len(numbers)\n"},
        repository_id="agent-retrieval",
    )
    output = run(make_context(root.resolve(), "agent-retrieval", retrieval=retrieval), query="average calculation")

    top = output.data["results"][0]
    assert top["qualified_name"] == "calculate_average"
    assert top["file_path"] == "stats.py"
    assert "keyword" in top["retrieval_sources"] or "vector" in top["retrieval_sources"]
    assert "1 | def calculate_average" in top["content"]
    assert output.locations[0].file_path == "stats.py"
    assert "not proof" in output.data["note"]


def test_unindexed_repository(retrieval, write_repo, make_context):
    root = write_repo({"x.py": "x = 1\n"}).resolve()
    with pytest.raises(ToolExecutionError, match="no retrieval index"):
        run(make_context(root, "never-indexed", retrieval=retrieval), query="x")


def test_retrieval_unavailable(write_repo, make_context):
    root = write_repo({"x.py": "x = 1\n"}).resolve()
    with pytest.raises(ToolExecutionError, match="not available"):
        run(make_context(root), query="x")
