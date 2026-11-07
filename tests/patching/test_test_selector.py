import os

from app.agents.schemas import Evidence, TestRunRecord
from app.patching.test_selector import module_names, select_tests, validate_test_target
from tests.patching.conftest import make_analysis


def run(path, outcome="failed"):
    return TestRunRecord(observation_id="obs-1", test_path=path, exit_code=1, timed_out=False, duration_seconds=0.1,
                         outcome=outcome)


def by_target(selection):
    return {item.target: item for item in selection.items}


def test_failing_investigation_tests_reproduce_the_bug(average_repo):
    analysis = make_analysis("i", tests_run=[run("test_solution.py::test_average_of_empty_list")])
    selection = select_tests(average_repo, analysis, affected_files=["solution.py"])
    item = by_target(selection)["test_solution.py"]
    assert item.reproduces_bug and item.bug_test_names == ["test_average_of_empty_list"]
    assert item.reasons[0] == "failed during the investigation (failed)"
    assert "imports affected module solution.py" in item.reasons
    assert len(selection.items) == 1  # deduplicated across sources
    assert selection.full_suite


def test_association_by_import_naming_and_one_hop(write_repo):
    root = write_repo(
        {
            "users_api/__init__.py": "",
            "users_api/repository.py": "class UserRepository: ...\n",
            "users_api/service.py": "from users_api.repository import UserRepository\n",
            "users_api/routes.py": "from users_api.service import UserService\n",
            "tests/test_routes.py": "from users_api.routes import get_user_profile\n",
            "tests/test_service.py": "from users_api import service\n",
            "tests/test_repository.py": "import json\n",
            "tests/test_unrelated.py": "import os\n",
            "tests/test_api_flow.py": "from users_api.routes import get_user_profile\n",
        }
    )
    analysis = make_analysis("i", affected_files=["users_api/repository.py"], tests_run=[], evidence=[])
    selection = by_target(select_tests(root, analysis, affected_files=["users_api/repository.py"]))
    assert "named after affected file users_api/repository.py" in selection["tests/test_repository.py"].reasons
    assert any("one hop" in r for r in selection["tests/test_service.py"].reasons)
    assert "tests/test_unrelated.py" not in selection
    assert not any(item.reproduces_bug for item in selection.values())


def test_diagnosis_and_evidence_test_files_are_selected(average_repo):
    analysis = make_analysis(
        "i", tests_run=[],
        evidence=[Evidence(id="E9", source_type="test", observation_ids=["obs-1"], file_path="test_solution.py",
                           observation="the empty-list test")],
    )
    item = by_target(select_tests(average_repo, analysis, affected_files=["solution.py"]))["test_solution.py"]
    assert {"named in the diagnosis", "cited as evidence E9"} <= set(item.reasons)
    assert not item.reproduces_bug


def test_unsafe_or_invalid_targets_are_dropped_with_warnings(average_repo, tmp_path):
    (tmp_path / "test_outside.py").write_text("def test_x(): pass\n")
    os.symlink(tmp_path / "test_outside.py", average_repo / "test_link.py")
    analysis = make_analysis("i", affected_files=["solution.py"], tests_run=[
        run("/etc/test_passwd.py"), run("../test_outside.py"), run("solution.py"), run("test_missing.py"),
        run("test_link.py"), run("test_solution.py::bad name"), run(".env"),
    ])
    selection = select_tests(average_repo, analysis, affected_files=["solution.py"], suggested=["../../test_x.py"])
    assert [i.target for i in selection.items] == ["test_solution.py"]
    assert not selection.items[0].reproduces_bug
    assert len(selection.warnings) == 8


def test_count_limit_keeps_the_highest_priority_tests(write_repo):
    files = {"mod.py": "x = 1\n", **{f"test_{i}.py": "import mod\n" for i in range(5)}}
    root = write_repo(files)
    analysis = make_analysis("i", affected_files=["mod.py"], tests_run=[run("test_4.py")], evidence=[])
    selection = select_tests(root, analysis, affected_files=["mod.py"], max_test_files=2, include_full_suite=False)
    assert selection.truncated and [i.target for i in selection.items] == ["test_4.py", "test_0.py"]
    assert not selection.full_suite


def test_suggested_tests_are_validated_like_any_other(average_repo):
    analysis = make_analysis("i", tests_run=[], evidence=[], affected_files=["solution.py"])
    selection = select_tests(average_repo, analysis, affected_files=[], suggested=["test_solution.py", "/abs.py"])
    assert [(i.target, i.reasons) for i in selection.items] == [("test_solution.py", ["suggested"])]
    assert len(selection.warnings) == 1


def test_validate_test_target(average_repo):
    assert validate_test_target(average_repo, "test_solution.py::test_x[1-2]") == ("test_solution.py", "test_x[1-2]")
    assert module_names("pkg/sub/__init__.py") == {"pkg.sub", "sub"}
