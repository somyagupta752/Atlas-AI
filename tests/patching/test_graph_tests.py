"""Graph-based test discovery. The graph suggests; the selector decides."""

import os
import uuid

import pytest

from app.code_intelligence import analyze_repository
from app.knowledge_graph.builder import build_code_graph
from app.knowledge_graph.repository import GraphStoreError
from app.patching.graph_tests import Suggestion, discover_tests
from app.patching.models import PatchLimits
from app.patching.test_selector import select_tests
from app.retrieval.graph_search import InMemoryGraphContextSource
from tests.patching.conftest import AVERAGE_FIX, make_analysis, submit
from tests.test_neo4j_integration import driver, graph_repo  # noqa: F401  (fixtures)

# test_flow imports only `api`, which imports `service`, which imports the affected `store`:
# two import hops, so import association misses it. The call graph reaches it in three.
DEEP = {
    "store.py": "def load(key):\n    return {'a': 1}.get(key)\n",
    "service.py": "from store import load\n\n\ndef fetch(key):\n    return load(key)\n",
    "api.py": "from service import fetch\n\n\ndef handle(key):\n    return fetch(key)\n",
    "test_flow.py": "from api import handle\n\n\ndef test_handle():\n    assert handle('a') == 1\n",
    "test_unrelated.py": "def test_nothing():\n    assert True\n",
}


def targets(selection):
    return [item.target for item in selection.items]


def test_graph_finds_a_test_that_import_association_misses(write_repo):
    root = write_repo(DEEP)
    analysis = make_analysis("i", affected_files=["store.py"], tests_run=[], evidence=[])
    plain = select_tests(root, analysis, affected_files=["store.py"])
    assert "test_flow.py" not in targets(plain)

    suggestions, report = discover_tests(root, "deep", ["store.py"], ["load"])
    assert report.source == "in_memory" and report.seeds == 1
    assert [s.target for s in suggestions] == ["test_flow.py"]
    assert "test_handle -> handle -> fetch -> load (3 hops" in suggestions[0].reason
    with_graph = select_tests(root, analysis, affected_files=["store.py"], suggested=suggestions)
    item = next(i for i in with_graph.items if i.target == "test_flow.py")
    assert item.reasons[0].startswith("graph (in-memory)") and item.priority == 4
    assert "test_unrelated.py" not in targets(with_graph)


def test_depth_limit(write_repo):
    root = write_repo(DEEP)
    assert discover_tests(root, "deep", ["store.py"], max_depth=2)[0] == []


def test_benchmark_constructor_and_helper_paths(bench_repo):
    root = bench_repo("bug_002_missing_record")
    suggestions, _ = discover_tests(root, "b2", ["users_api/routes.py"], ["get_user_profile"])
    (direct,) = suggestions
    assert direct.target == "tests/test_routes.py" and direct.priority == 3 and direct.score == 1.0
    assert "test_existing_user_returns_profile, test_missing_user_returns_404" in direct.reason
    suggestions, _ = discover_tests(root, "b2", ["users_api/repository.py"], ["UserRepository.find_by_id"])
    assert "make_service -> UserRepository.__init__" in suggestions[0].reason  # through a test helper


def test_ranking_limit_and_determinism(write_repo):
    files = {"core.py": "def work():\n    return 1\n\n\ndef helper():\n    return work()\n"}
    for i in range(6):
        files[f"test_direct_{i}.py"] = "from core import work\n\n\ndef test_it():\n    assert work() == 1\n"
        files[f"test_indirect_{i}.py"] = "from core import helper\n\n\ndef test_it():\n    assert helper() == 1\n"
    root = write_repo(files)
    first, _ = discover_tests(root, "r", ["core.py"], ["work"], max_suggestions=8)
    again, _ = discover_tests(root, "r", ["core.py"], ["work"], max_suggestions=8)
    assert first == again and len(first) == 8
    # helper() is itself in the modifiable file, so indirect tests are direct callers of a seed (weight 0.6).
    assert [s.target for s in first[:6]] == [f"test_direct_{i}.py" for i in range(6)]
    assert all(s.score == 1.0 for s in first[:6]) and all(s.score == 0.6 for s in first[6:])


def test_graph_suggestions_never_outrank_the_investigation(average_repo):
    analysis = make_analysis("i")
    forged = Suggestion(target="test_solution.py", reason="graph: forged", priority=0, score=99)
    selection = select_tests(average_repo, analysis, affected_files=["solution.py"], suggested=[forged])
    (item,) = selection.items
    assert item.priority == 0 and item.reasons[0].startswith("failed during the investigation")
    alone = select_tests(average_repo, make_analysis("i", tests_run=[], evidence=[], affected_files=["solution.py"]),
                         affected_files=[], suggested=[forged])
    assert alone.items[0].priority == 3 and not alone.items[0].reproduces_bug


def test_unsafe_graph_suggestions_are_rejected_by_the_selector(average_repo, tmp_path):
    (tmp_path / "test_outside.py").write_text("def test_x(): pass\n")
    os.symlink(tmp_path / "test_outside.py", average_repo / "test_link.py")
    bad = [Suggestion(t, "graph: x", 3) for t in
           ("/etc/test_x.py", "../test_outside.py", "solution.py", "test_link.py", ".env", "test_missing.py")]
    analysis = make_analysis("i", tests_run=[], evidence=[], affected_files=["solution.py"])
    selection = select_tests(average_repo, analysis, affected_files=[], suggested=bad)
    assert selection.items == [] and len(selection.warnings) == 6


# --- Neo4j: fresh, stale, unavailable --------------------------------------------------------


class Store(InMemoryGraphContextSource):
    """Stands in for Neo4jGraphRepository: the same read protocol over a stored graph."""


def stored_graph(root, repository_id="deep"):
    return Store(build_code_graph(analyze_repository(root), repository_id))


def test_fresh_neo4j_graph_is_used(write_repo):
    root = write_repo(DEEP)
    suggestions, report = discover_tests(root, "deep", ["store.py"], ["load"], graph=stored_graph(root))
    assert report.source == "neo4j" and suggestions[0].reason.startswith("graph (neo4j)")


def test_stale_seed_falls_back_to_the_current_source(write_repo):
    root = write_repo(DEEP)
    graph = stored_graph(root)
    (root / "store.py").write_text("# moved\n" + DEEP["store.py"])  # line numbers changed since the build
    suggestions, report = discover_tests(root, "deep", ["store.py"], ["load"], graph=graph)
    assert report.source == "in_memory" and report.stale_entities == ["store.py::load"]
    assert "stale" in report.warnings[0] and [s.target for s in suggestions] == ["test_flow.py"]


def test_stale_test_falls_back(write_repo):
    root = write_repo(DEEP)
    graph = stored_graph(root)
    (root / "test_flow.py").write_text("from api import handle\n\n\n\ndef test_handle():\n    assert handle('a') == 1\n")
    _, report = discover_tests(root, "deep", ["store.py"], ["load"], graph=graph)
    assert report.source == "in_memory" and report.stale_entities == ["test_flow.py::test_handle"]


def test_new_function_missing_from_the_graph_is_stale(write_repo):
    root = write_repo(DEEP)
    graph = stored_graph(root)
    (root / "store.py").write_text(DEEP["store.py"] + "\n\ndef save(key):\n    return key\n")
    _, report = discover_tests(root, "deep", ["store.py"], graph=graph)
    assert report.source == "in_memory" and report.stale_entities


class Broken:
    def find_entities_by_name(self, *args):
        raise GraphStoreError("Neo4j is down")


def test_unavailable_or_unknown_repository_falls_back(write_repo):
    root = write_repo(DEEP)
    _, report = discover_tests(root, "deep", ["store.py"], graph=Broken())
    assert report.source == "in_memory" and "unavailable" in report.warnings[0]
    _, report = discover_tests(root, "deep", ["store.py"], graph=stored_graph(root, "other-repo"))
    assert report.source == "in_memory" and "no entities" in report.warnings[0]


def test_fallback_can_be_disabled(write_repo):
    root = write_repo(DEEP)
    suggestions, report = discover_tests(root, "deep", ["store.py"], graph=Broken(), allow_in_memory=False)
    assert suggestions == [] and report.source == "unavailable"


# --- service ------------------------------------------------------------------------------


def test_service_records_the_graph_report(build_service):
    service, _, iid = build_service([submit(AVERAGE_FIX)])
    execution = service.generate_and_verify_patch(iid)
    assert execution.status == "verified"
    graph = execution.test_selection.graph
    assert graph["source"] == "in_memory" and graph["suggestions"][0]["target"] == "test_solution.py"
    item = execution.test_selection.items[0]
    assert item.priority == 0 and any(r.startswith("graph (in-memory)") for r in item.reasons)


def test_graph_failure_never_blocks_verification(build_service, monkeypatch):
    from app.patching import service as service_module

    def explode(*args, **kwargs):
        raise RuntimeError("graph bug")

    monkeypatch.setattr(service_module, "discover_tests", explode)
    service, _, iid = build_service([submit(AVERAGE_FIX)])
    execution = service.generate_and_verify_patch(iid)
    assert execution.status == "verified"
    assert execution.test_selection.graph["source"] == "unavailable"


def test_graph_discovery_can_be_disabled(build_service):
    service, _, iid = build_service([submit(AVERAGE_FIX)], limits=PatchLimits(baseline_runs=1,
                                                                              graph_test_discovery=False))
    assert service.generate_and_verify_patch(iid).test_selection.graph["source"] == "disabled"


# --- real Neo4j ---------------------------------------------------------------------------

@pytest.mark.neo4j
def test_real_neo4j_matches_the_in_memory_reference(graph_repo, write_repo):  # noqa: F811
    root = write_repo(DEEP)
    repository_id = f"itest-{uuid.uuid4().hex[:12]}"
    graph_repo.track(repository_id)
    graph_repo.save_graph(build_code_graph(analyze_repository(root), repository_id))
    neo4j, report = discover_tests(root, repository_id, ["store.py"], ["load"], graph=graph_repo)
    memory, _ = discover_tests(root, repository_id, ["store.py"], ["load"])
    assert report.source == "neo4j"
    assert [(s.target, s.score, s.priority) for s in neo4j] == [(s.target, s.score, s.priority) for s in memory]
