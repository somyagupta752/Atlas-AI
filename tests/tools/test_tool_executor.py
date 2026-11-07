import json
import time

import pytest

from app.agents.llm import ToolCall
from app.agents.state import InvestigationState
from app.agents.tool_executor import ToolExecutor
from app.agents.tool_registry import ToolRegistry, create_default_registry
from app.tools.base import BaseTool, ToolArguments, ToolExecutionError, ToolOutput


class NoArguments(ToolArguments):
    pass


def make_tool(name, behaviour):
    class _Tool(BaseTool):
        description = f"Test tool {name}."
        Arguments = NoArguments

        def execute(self, arguments, context):
            return behaviour(context)

    _Tool.name = name
    return _Tool()


@pytest.fixture
def state():
    return InvestigationState(repository_id="demo", bug_report="x")


@pytest.fixture
def context(write_repo, make_context):
    return make_context(write_repo({"a.py": "x = 1\n"}).resolve(), tool_timeout_seconds=0.5, max_output_bytes=2000)


def call(name, arguments=None, call_id="c1"):
    return ToolCall(id=call_id, name=name, arguments={} if arguments is None else arguments)


def test_valid_call_returns_structured_observation(state, context):
    observation = ToolExecutor(create_default_registry()).execute(
        call("read_file", {"file_path": "a.py"}), context, state
    )
    assert observation.status == "ok"
    assert observation.id == "obs-1"
    envelope = json.loads(observation.content)
    assert envelope["observation_id"] == "obs-1"
    assert envelope["result"]["content"] == "    1 | x = 1"
    assert observation.locations[0].file_path == "a.py"


def test_unknown_tool(state, context):
    observation = ToolExecutor(create_default_registry()).execute(call("run_shell", {"cmd": "ls"}), context, state)
    assert observation.status == "unknown_tool"
    assert "Available:" in observation.error


def test_registered_but_not_permitted(state, context):
    executor = ToolExecutor(create_default_registry(), allowed_tools=["read_file"])
    observation = executor.execute(call("run_tests", {"test_path": "test_a.py"}), context, state)
    assert observation.status == "permission_denied"
    assert [s["name"] for s in executor.schemas()] == ["read_file"]


@pytest.mark.parametrize(
    "arguments", [{}, {"file_path": "a.py", "mode": "w"}, {"file_path": 3}, "a.py", ["a.py"]]
)
def test_missing_extra_and_wrong_type_arguments(state, context, arguments):
    observation = ToolExecutor(create_default_registry()).execute(call("read_file", arguments), context, state)
    assert observation.status == "invalid_arguments"


def test_invalid_path_becomes_observation(state, context):
    observation = ToolExecutor(create_default_registry()).execute(
        call("read_file", {"file_path": "../../etc/passwd"}), context, state
    )
    assert observation.status == "permission_denied"
    assert "traversal" in observation.error


def test_tool_exception_is_contained(state, context):
    def boom(_):
        raise RuntimeError("secret detail /private/path")

    observation = ToolExecutor(ToolRegistry([make_tool("boom", boom)])).execute(call("boom"), context, state)
    assert observation.status == "internal_error"
    assert "RuntimeError" in observation.error
    assert "secret detail" not in observation.content


def test_tool_error(state, context):
    def fail(_):
        raise ToolExecutionError("index unavailable")

    observation = ToolExecutor(ToolRegistry([make_tool("fail", fail)])).execute(call("fail"), context, state)
    assert (observation.status, observation.error) == ("tool_error", "index unavailable")


def test_tool_timeout(state, context):
    def slow(_):
        time.sleep(5)

    started = time.monotonic()
    observation = ToolExecutor(ToolRegistry([make_tool("slow", slow)])).execute(call("slow"), context, state)
    assert observation.status == "timeout"
    assert time.monotonic() - started < 4


def test_oversized_output_is_truncated(state, context):
    def big(_):
        return ToolOutput(data={"blob": "x" * 50_000})

    observation = ToolExecutor(ToolRegistry([make_tool("big", big)])).execute(call("big"), context, state)
    assert observation.truncated is True
    assert len(observation.content.encode()) <= context.max_output_bytes
    assert observation.data is None  # only what the model saw is kept


def test_invalid_result_type(state, context):
    observation = ToolExecutor(ToolRegistry([make_tool("bad", lambda _: {"not": "ToolOutput"})])).execute(
        call("bad"), context, state
    )
    assert observation.status == "internal_error"
