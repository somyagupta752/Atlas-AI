import uuid

import pytest
from fastapi.testclient import TestClient

from app.agents.llm import FakeLLM, LLMTimeoutError
from app.agents.routes import get_llm_provider, get_trusted_test_roots
from app.database import get_db
from app.main import app, get_allowed_repository_roots
from app.models import InvestigationRecord
from app.patching.routes import get_patch_limits, get_workspace_manager
from app.patching.workspace import WorkspaceManager
from app.retrieval.embeddings import HashingEmbeddingProvider
from app.retrieval.indexing import RepositoryIndexer
from tests.patching.conftest import (
    AVERAGE_FIX,
    AVERAGE_NO_FIX,
    AVERAGE_TEST_TAMPER,
    FAST,
    make_analysis,
    submit,
)

REPOSITORY = "api-patch-average"


@pytest.fixture
def holder():
    return {"llm": FakeLLM([submit(AVERAGE_FIX)]), "trusted": None}


@pytest.fixture
def root(bench_repo, chunk_repo):
    path = bench_repo("bug_001_average")
    RepositoryIndexer(chunk_repo, HashingEmbeddingProvider()).index_repository(REPOSITORY, path)
    return path


@pytest.fixture
def client(db_session, tmp_path, holder, root):
    manager = WorkspaceManager(tmp_path / "workspaces")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_llm_provider] = lambda: holder["llm"]
    app.dependency_overrides[get_allowed_repository_roots] = lambda: [tmp_path]
    app.dependency_overrides[get_trusted_test_roots] = lambda: holder["trusted"] or [tmp_path]
    app.dependency_overrides[get_patch_limits] = lambda: FAST
    app.dependency_overrides[get_workspace_manager] = lambda: manager
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def investigation(db_session):
    def make(status="completed", repository_id=REPOSITORY):
        iid = str(uuid.uuid4())
        analysis = make_analysis(iid, repository_id) if status == "completed" else None
        db_session.add(InvestigationRecord(
            id=uuid.UUID(iid), repository_id=repository_id, bug_report="The average crashes on an empty list.",
            status=status, root_cause=analysis.root_cause if analysis else None,
            result=analysis.model_dump(mode="json") if analysis else None,
        ))
        db_session.commit()
        return iid

    return make


def post(client, iid, **body):
    return client.post(f"/debug/investigations/{iid}/patches", json={"max_attempts": 3, "run_full_test_suite": True,
                                                                     **body})


def test_generate_verify_and_read_back(client, investigation, root):
    iid = investigation()
    response = post(client, iid)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "verified" and body["investigation_id"] == iid
    assert body["affected_files"] == ["solution.py"] and body["unified_diff"] == AVERAGE_FIX
    assert body["verification"] == {
        "patch_validation": "passed", "patch_application": "passed", "syntax_check": "passed",
        "original_bug_test": "passed", "regression_tests": "passed", "ruff": "passed",
        "full_test_suite": "passed", "original_repository_unchanged": "passed",
    }
    assert body["original_repository_modified"] is False
    assert "not modified" in body["summary"]
    assert "if not numbers" not in (root / "solution.py").read_text()
    patch_id = body["patch_id"]

    detail = client.get(f"/debug/patches/{patch_id}").json()
    assert detail["status"] == "verified" and detail["unified_diff"] == AVERAGE_FIX
    assert detail["validation_result"]["valid"] and detail["verification"]["status"] == "verified"
    assert detail["provenance"]["model"] == "fake-model" and "limits" not in detail["provenance"]
    assert [h["status"] for h in detail["status_history"]][-1] == "verified"

    history = client.get(f"/debug/investigations/{iid}/patches").json()
    assert [p["patch_id"] for p in history["patches"]] == [patch_id]

    verification = client.get(f"/debug/patches/{patch_id}/verification").json()
    assert {c["name"] for c in verification["checks"]} >= {"original_bug_test", "regression_tests", "ruff"}
    assert verification["comparison"]["fixed"] == ["test_solution.py::test_average_of_empty_list"]
    assert verification["before_results"]["bug_tests"] and verification["after_results"]["targeted"]
    assert str(root) not in verification["after_results"]["targeted"]["stdout"]


def test_history_lists_every_attempt(client, investigation, holder):
    holder["llm"] = FakeLLM([submit(AVERAGE_TEST_TAMPER), submit(AVERAGE_NO_FIX), submit(AVERAGE_FIX)])
    iid = investigation()
    body = post(client, iid).json()
    assert [a["status"] for a in body["attempts"]] == ["validation_failed", "rejected", "verified"]
    assert body["attempts"][0]["validation_errors"]
    history = client.get(f"/debug/investigations/{iid}/patches").json()["patches"]
    assert [(p["attempt_number"], p["status"]) for p in history] == [
        (1, "validation_failed"), (2, "rejected"), (3, "verified")]
    first = history[0]["patch_id"]
    assert client.get(f"/debug/patches/{first}/verification").status_code == 404
    assert client.get(f"/debug/patches/{first}").json()["verification"] is None


def test_not_found_and_not_eligible(client, investigation):
    assert post(client, str(uuid.uuid4())).status_code == 404
    assert post(client, investigation(status="inconclusive")).status_code == 409
    assert client.get(f"/debug/patches/{uuid.uuid4()}").status_code == 404
    assert client.get(f"/debug/investigations/{uuid.uuid4()}/patches").status_code == 404
    assert client.post("/debug/investigations/not-a-uuid/patches", json={}).status_code == 422


def test_cross_repository_access_is_not_found(client, investigation):
    iid = investigation()
    assert post(client, iid, repository_id="other-repo").status_code == 404
    patch_id = post(client, iid).json()["patch_id"]
    assert client.get(f"/debug/patches/{patch_id}?repository_id=other-repo").status_code == 404
    assert client.get(f"/debug/patches/{patch_id}/verification?repository_id=other-repo").status_code == 404
    assert client.get(f"/debug/investigations/{iid}/patches?repository_id=other-repo").status_code == 404
    assert client.get(f"/debug/patches/{patch_id}?repository_id={REPOSITORY}").status_code == 200


def test_untrusted_repository_is_forbidden(client, investigation, holder, tmp_path):
    holder["trusted"] = [tmp_path / "nowhere"]
    response = post(client, investigation())
    assert response.status_code == 403 and holder["llm"].calls == []


def test_unindexed_repository_is_not_found(client, investigation):
    assert post(client, investigation(repository_id="never-indexed")).status_code == 404


def test_request_validation(client, investigation):
    iid = investigation()
    assert post(client, iid, max_attempts=0).status_code == 422
    assert post(client, iid, max_attempts=11).status_code == 422
    assert post(client, iid, max_attempts=4).status_code == 422  # above the configured limit
    assert post(client, iid, diagnosis={"root_cause": "trust me"}).status_code == 422
    large = client.post(f"/debug/investigations/{iid}/patches", content=b"{" + b" " * 70_000 + b"}",
                        headers={"content-type": "application/json"})
    assert large.status_code == 413


def test_llm_timeout_maps_to_504_without_internals(client, investigation, holder):
    holder["llm"] = FakeLLM([LLMTimeoutError("secret internal detail")])
    response = post(client, investigation())
    assert response.status_code == 504
    body = response.json()
    assert body["failure_reason"] == "llm_timeout" and body["status"] == "failed"
    assert "secret internal detail" not in response.text and "Traceback" not in response.text
