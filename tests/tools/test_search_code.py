import pytest

from app.tools.base import ToolPermissionError
from app.tools.search_code import SearchCodeTool

TOOL = SearchCodeTool()


def run(context, **arguments):
    return TOOL.execute(TOOL.validate(arguments), context)


@pytest.fixture
def repo(write_repo):
    return write_repo(
        {
            "app/service.py": "def get_user(user_id):\n    return find_by_id(user_id)\n",
            "app/routes.py": "from app.service import get_user\n\nuser = get_user(1)\n",
            ".venv/lib/x.py": "get_user = 1\n",
            ".env": "get_user=secret\n",
        },
        name="repo-a",
    ).resolve()


def test_finds_matches_with_locations(repo, make_context):
    output = run(make_context(repo), query="get_user")

    found = {(m["file_path"], m["line"]) for m in output.data["matches"]}
    assert found == {("app/service.py", 1), ("app/routes.py", 1), ("app/routes.py", 3)}
    assert {loc.file_path for loc in output.locations} == {"app/service.py", "app/routes.py"}


def test_no_result(repo, make_context):
    output = run(make_context(repo), query="does_not_exist")
    assert output.data["matches"] == [] and output.data["total_matches"] == 0


def test_excluded_directories_and_secret_files_are_not_searched(repo, make_context):
    files = {m["file_path"] for m in run(make_context(repo), query="get_user").data["matches"]}
    assert ".venv/lib/x.py" not in files and ".env" not in files


def test_case_insensitive(repo, make_context):
    output = run(make_context(repo), query="GET_USER", case_sensitive=False)
    assert output.data["total_matches"] == 3


def test_restricted_to_path(repo, make_context):
    output = run(make_context(repo), query="get_user", path="app/service.py")
    assert {m["file_path"] for m in output.data["matches"]} == {"app/service.py"}


def test_max_results(repo, make_context):
    output = run(make_context(repo), query="get_user", max_results=1)
    assert output.data["total_matches"] == 1 and output.data["truncated"] is True


def test_repository_isolation(repo, write_repo, make_context):
    other = write_repo({"other.py": "def get_user(): pass\n"}, name="repo-b").resolve()

    files = {m["file_path"] for m in run(make_context(repo), query="get_user").data["matches"]}
    assert "other.py" not in files
    with pytest.raises(ToolPermissionError):
        run(make_context(repo), query="get_user", path=f"../{other.name}")
