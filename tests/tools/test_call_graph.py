"""get_callers and get_callees, on the in-memory graph and on a graph source."""

import pytest

from app.code_intelligence import analyze_repository
from app.knowledge_graph.builder import build_code_graph
from app.knowledge_graph.repository import GraphStoreError
from app.retrieval.graph_search import InMemoryGraphContextSource
from app.tools.base import ToolInputError
from app.tools.get_callees import GetCalleesTool
from app.tools.get_callers import GetCallersTool

CALLERS = GetCallersTool()
CALLEES = GetCalleesTool()


def run(tool, context, **arguments):
    return tool.execute(tool.validate(arguments), context)


@pytest.fixture
def root(bench_repo):
    return bench_repo("bug_002_missing_record")


def test_callers_of_service_method(root, make_context):
    output = run(CALLERS, make_context(root), qualified_name="UserService.get_user")

    names = {c["qualified_name"] for c in output.data["callers"]}
    assert "get_user_profile" in names
    assert output.data["graph_source"] == "in_memory"
    assert any(loc.file_path == "users_api/routes.py" for loc in output.locations)


def test_callees_of_service_method(root, make_context):
    output = run(CALLEES, make_context(root), qualified_name="UserService.get_user")

    assert [c["qualified_name"] for c in output.data["callees"]] == ["UserRepository.find_by_id"]
    assert output.data["callees"][0]["resolution_status"] in ("resolved", "inferred")


def test_unresolved_calls_are_reported(bench_repo, make_context):
    root = bench_repo("bug_001_average")
    output = run(CALLEES, make_context(root), qualified_name="calculate_average")

    expressions = {u["expression"] for u in output.data["unresolved_calls"]}
    assert {"sum", "len"} <= expressions


def test_unknown_function(root, make_context):
    with pytest.raises(ToolInputError):
        run(CALLERS, make_context(root), qualified_name="nothing_here")


def test_uses_graph_source_when_it_has_the_repository(root, make_context):
    graph = InMemoryGraphContextSource(build_code_graph(analyze_repository(root), "demo"))
    output = run(CALLERS, make_context(root, graph=graph), qualified_name="UserService.get_user")
    assert output.data["graph_source"] == "neo4j"


def test_falls_back_when_graph_store_is_down(root, make_context):
    class Down:
        def find_entities_by_name(self, *args):
            raise GraphStoreError("down")

    output = run(CALLERS, make_context(root, graph=Down()), qualified_name="UserService.get_user")
    assert output.data["graph_source"] == "in_memory"
    assert output.data["total"] >= 1
