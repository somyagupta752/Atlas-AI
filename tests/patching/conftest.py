"""Shared builders for patching tests. FakeLLM only: no network, no cost.

Every repository is a temporary copy, so no test can modify Atlas AI's own source.
"""

import uuid

import pytest

from app.agents.agent import StaticRepositoryResolver
from app.agents.llm import FakeLLM, LLMResponse, tool_call
from app.agents.schemas import Evidence, RootCauseAnalysis, TestRunRecord
from app.patching.models import PatchLimits
from app.patching.service import (
    InMemoryInvestigationSource,
    LoadedInvestigation,
    PatchingService,
)
from app.patching.workspace import WorkspaceManager

AVERAGE_FIX = """\
--- a/solution.py
+++ b/solution.py
@@ -1,2 +1,4 @@
 def calculate_average(numbers):
+    if not numbers:
+        return 0.0
     return sum(numbers) / len(numbers)
"""
# Still divides by zero for an empty list.
AVERAGE_NO_FIX = """\
--- a/solution.py
+++ b/solution.py
@@ -1,2 +1,2 @@
 def calculate_average(numbers):
-    return sum(numbers) / len(numbers)
+    return sum(numbers) / max(len(numbers), 0)
"""
# Fixes the empty case and breaks every other average.
AVERAGE_REGRESSION = """\
--- a/solution.py
+++ b/solution.py
@@ -1,2 +1,2 @@
 def calculate_average(numbers):
-    return sum(numbers) / len(numbers)
+    return sum(numbers) / (len(numbers) + 1)
"""
AVERAGE_SYNTAX_ERROR = AVERAGE_FIX.replace("if not numbers:", "if not numbers")
AVERAGE_TEST_TAMPER = """\
--- a/test_solution.py
+++ b/test_solution.py
@@ -14,4 +14,4 @@


 def test_average_of_empty_list():
-    assert calculate_average([]) == 0.0
+    assert True
"""
AVERAGE_UNRELATED = AVERAGE_FIX + """\
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 hello
+world
"""
AVERAGE_NOOP = """\
--- a/solution.py
+++ b/solution.py
@@ -1,2 +1,2 @@
 def calculate_average(numbers):
-    return sum(numbers) / len(numbers)
+    return sum(numbers) / len(numbers)
"""


def submit(diff: str, files=("solution.py",), **fields) -> LLMResponse:
    arguments = {
        "description": "Return 0.0 for an empty list.",
        "rationale": "len([]) is 0, so the division raises ZeroDivisionError.",
        "unified_diff": diff,
        "affected_files": list(files),
        "assumptions": ["The bug report says an empty list should average to 0.0."],
        "risks": [],
        **fields,
    }
    return tool_call("submit_patch", arguments)


def make_analysis(investigation_id: str, repository_id: str = "avg", **overrides) -> RootCauseAnalysis:
    value = {
        "investigation_id": investigation_id,
        "repository_id": repository_id,
        "status": "completed",
        "summary": "calculate_average divides by len(numbers), which is 0 for an empty list.",
        "root_cause": "calculate_average divides by zero when the input list is empty.",
        "confidence": 0.9,
        "affected_files": ["solution.py", "test_solution.py"],
        "evidence": [
            Evidence(id="E1", source_type="source_code", observation_ids=["obs-2"], file_path="solution.py",
                     qualified_name="calculate_average", start_line=1, end_line=2,
                     observation="Returns sum(numbers) / len(numbers) with no empty check."),
        ],
        "tests_run": [
            TestRunRecord(observation_id="obs-3", test_path="test_solution.py", exit_code=1, timed_out=False,
                          duration_seconds=0.1, outcome="failed"),
        ],
        "suggested_fix": "Return 0.0 when numbers is empty.",
    }
    value.update(overrides)
    return RootCauseAnalysis(**value)


FAST = PatchLimits(baseline_runs=1)


@pytest.fixture
def average_repo(bench_repo):
    return bench_repo("bug_001_average")


@pytest.fixture
def workspaces(tmp_path):
    return WorkspaceManager(tmp_path / "workspaces")


@pytest.fixture
def build_service(average_repo, workspaces):
    """``build(script) -> (service, llm, investigation_id)`` on a copy of bug_001_average."""

    def build(script, *, limits=FAST, patches=None, analysis=None, status="completed", engine=None, repo=None,
              repository_id="avg", investigation_id=None, manager=None, trusted=None):
        llm = script if isinstance(script, FakeLLM) else FakeLLM(script)
        root = repo or average_repo
        iid = investigation_id or str(uuid.uuid4())
        analysis = analysis or make_analysis(iid, repository_id)
        source = InMemoryInvestigationSource(
            {iid: LoadedInvestigation(iid, repository_id, status, "The average crashes on an empty list.", analysis)}
        )
        service = PatchingService(
            llm, source, StaticRepositoryResolver({repository_id: root}),
            workspaces=manager or workspaces, patches=patches, limits=limits, engine=engine,
            trusted_test_roots=[root] if trusted is None else trusted,
        )
        return service, llm, iid

    return build
