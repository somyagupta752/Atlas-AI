import pytest

from app.tools.base import ToolInputError
from app.tools.get_symbol import GetSymbolTool

TOOL = GetSymbolTool()


def run(context, **arguments):
    return TOOL.execute(TOOL.validate(arguments), context)


@pytest.fixture
def context(bench_repo, make_context):
    return make_context(bench_repo("bug_002_missing_record"))


def test_method_by_qualified_name(context):
    output = run(context, qualified_name="UserService.get_user")

    [definition] = output.data["definitions"]
    assert definition["type"] == "method"
    assert definition["file_path"] == "users_api/service.py"
    assert definition["parent_class"] == "UserService"
    assert [p["name"] for p in definition["parameters"]] == ["self", "user_id"]
    assert "return self.repository.find_by_id(user_id)" in definition["source"]
    assert output.locations[0].file_path == "users_api/service.py"


def test_class_by_module_path(context):
    [definition] = run(context, qualified_name="users_api.repository.UserRepository").data["definitions"]
    assert definition["type"] == "class"
    assert "find_by_id" in definition["methods"]


def test_bare_name(context):
    output = run(context, qualified_name="get_user_profile")
    assert output.data["definitions"][0]["file_path"] == "users_api/routes.py"
    assert output.data["ambiguous"] is False


def test_unknown_symbol(context):
    with pytest.raises(ToolInputError):
        run(context, qualified_name="NoSuchThing")


def test_restricted_to_file(context):
    with pytest.raises(ToolInputError):
        run(context, qualified_name="get_user", file_path="users_api/routes.py")
