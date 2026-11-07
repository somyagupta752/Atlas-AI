import pytest
from pydantic import Field

from app.agents.tool_registry import (
    DEFAULT_TOOL_NAMES,
    DuplicateToolError,
    InvalidToolArgumentsError,
    ToolRegistry,
    UnknownToolError,
    create_default_registry,
)
from app.tools.base import BaseTool, ToolArguments, ToolOutput


class EchoArguments(ToolArguments):
    text: str = Field(min_length=1)
    times: int = 1


class EchoTool(BaseTool):
    name = "echo"
    description = "Returns the text it is given, repeated."
    Arguments = EchoArguments

    def execute(self, arguments, context):
        return ToolOutput(data={"text": arguments.text * arguments.times})


def test_register_and_get():
    registry = ToolRegistry([EchoTool()])
    assert registry.get("echo").name == "echo"
    assert registry.names == ["echo"]


def test_duplicate_names_are_rejected():
    registry = ToolRegistry([EchoTool()])
    with pytest.raises(DuplicateToolError):
        registry.register(EchoTool())


def test_unknown_tool():
    with pytest.raises(UnknownToolError):
        ToolRegistry([EchoTool()]).get("run_shell")


def test_schemas_are_provider_neutral_and_filtered():
    registry = create_default_registry()
    schemas = registry.get_schemas()
    assert [s["name"] for s in schemas] == sorted(DEFAULT_TOOL_NAMES)
    for schema in schemas:
        assert set(schema) == {"name", "description", "parameters"}
        assert schema["parameters"]["type"] == "object"
        assert schema["parameters"]["additionalProperties"] is False
        assert len(schema["description"]) > 80  # descriptions guide tool choice
    assert [s["name"] for s in registry.get_schemas(["read_file", "not_registered"])] == ["read_file"]


@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        ({}, "text: Field required"),
        ({"text": "a", "extra": 1}, "extra: Extra inputs are not permitted"),
        ({"text": "a", "times": "3"}, "times: Input should be a valid integer"),
        ({"text": ""}, "text: String should have at least 1 character"),
        ("not an object", "arguments must be a JSON object"),
    ],
)
def test_arguments_are_validated(arguments, fragment):
    with pytest.raises(InvalidToolArgumentsError) as info:
        ToolRegistry([EchoTool()]).validate("echo", arguments)
    assert any(fragment in e for e in info.value.errors)


def test_execute_validates_first(make_context, tmp_path):
    registry = ToolRegistry([EchoTool()])
    assert registry.execute("echo", {"text": "ab", "times": 2}, make_context(tmp_path)).data == {"text": "abab"}
