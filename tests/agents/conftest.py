"""Shared builders for agent tests. Everything runs on FakeLLM: no network, no cost."""

import json

import pytest

from app.agents.agent import DebuggingAgent, StaticRepositoryResolver
from app.agents.llm import FakeLLM, final_answer, tool_call
from app.agents.schemas import AgentLimits
from app.agents.tool_registry import create_default_registry


def diagnosis(**overrides) -> dict:
    """A valid completed diagnosis for bug_001_average, given the standard script below."""
    value = {
        "status": "completed",
        "summary": "calculate_average divides by len(numbers), which is 0 for an empty list.",
        "root_cause": "calculate_average divides by zero when the input list is empty.",
        "confidence": 0.9,
        "hypotheses": [
            {"id": "H1", "description": "Empty input causes division by zero.", "status": "supported",
             "supporting_evidence": ["E1", "E2"], "contradicting_evidence": []},
            {"id": "H2", "description": "sum() fails on an empty list.", "status": "rejected",
             "supporting_evidence": [], "contradicting_evidence": ["E2"]},
        ],
        "evidence": [
            {"id": "E1", "source_type": "source_code", "observation_ids": ["obs-2"],
             "file_path": "solution.py", "qualified_name": "calculate_average", "start_line": 1, "end_line": 2,
             "observation": "Returns sum(numbers) / len(numbers) with no empty check.",
             "supports_hypothesis_ids": ["H1"], "contradicts_hypothesis_ids": []},
            {"id": "E2", "source_type": "execution", "observation_ids": ["obs-3"],
             "file_path": "test_solution.py", "qualified_name": None, "start_line": None, "end_line": None,
             "observation": "test_average_of_empty_list fails with ZeroDivisionError, not a sum() error.",
             "supports_hypothesis_ids": ["H1"], "contradicts_hypothesis_ids": ["H2"]},
        ],
        "affected_files": ["solution.py"],
        "suggested_fix": "Return 0.0 (or raise a clear error) when numbers is empty.",
        "unresolved_questions": [],
    }
    value.update(overrides)
    return value


def standard_script(final: str | None = None) -> list:
    """search -> read -> run tests -> final diagnosis."""
    return [
        tool_call("search_code", {"query": "calculate_average"}, "call_search"),
        tool_call("read_file", {"file_path": "solution.py"}, "call_read"),
        tool_call("run_tests", {"test_path": "test_solution.py"}, "call_tests"),
        final_answer(final if final is not None else json.dumps(diagnosis())),
    ]


@pytest.fixture
def average_repo(bench_repo):
    return bench_repo("bug_001_average")


@pytest.fixture
def build_agent(average_repo):
    def build(script, limits=None, repository=None, **kwargs):
        llm = script if isinstance(script, FakeLLM) else FakeLLM(script)
        agent = DebuggingAgent(
            llm,
            create_default_registry(),
            StaticRepositoryResolver({"bench-average": average_repo}),
            limits=limits or AgentLimits(),
            repository=repository,
            **{"trusted_test_roots": [average_repo], **kwargs},
        )
        return agent, llm

    return build
