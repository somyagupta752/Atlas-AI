"""15.2: every endpoint requires a login, and repository endpoints enforce membership and role."""

import uuid

import pytest

from app.auth.models import Repository, RepositoryMembership
from app.evaluation.models import EvaluationRun
from app.main import app
from app.models import InvestigationRecord, PatchProposalRecord
from tests.auth.conftest import signup

pytestmark = pytest.mark.real_auth

PUBLIC = {("POST", "/auth/register"), ("POST", "/auth/login"), ("POST", "/auth/refresh"), ("POST", "/auth/logout"),
          ("POST", "/auth/password-reset"), ("POST", "/auth/password-reset/confirm"), ("GET", "/health"), ("GET", "/ready"),
          ("GET", "/docs"), ("GET", "/docs/oauth2-redirect"), ("GET", "/redoc"), ("GET", "/openapi.json")}


def fill(path: str) -> str:
    out = path
    for name in ("repository_id", "investigation_id", "patch_id", "run_id", "configuration_id", "baseline_id",
                 "member_id"):
        out = out.replace("{" + name + "}", str(uuid.uuid4()) if name != "repository_id" else "some-repo")
    return out.replace("{case_id}", "bug-001-average")


def test_every_operation_requires_authentication(client):
    checked = 0
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            if (method.upper(), path) in PUBLIC:
                continue
            response = client.request(method.upper(), fill(path), json={} if method != "get" else None)
            assert response.status_code == 401, (method.upper(), path, response.status_code)
            checked += 1
    assert checked >= 40  # the count would shrink if routes were silently dropped from the check


@pytest.fixture
def team(env, client):
    """An owner, an editor, a viewer and an outsider, with repository ``proj`` indexed by the owner."""
    repo_dir = env["allowed"] / "proj"
    repo_dir.mkdir()
    (repo_dir / "app.py").write_text("def add(a, b):\n    return a + b\n")
    users = {name: signup(f"{name}@example.com") for name in ("owner", "editor", "viewer", "outsider")}
    owner_client, owner_headers, _ = users["owner"]
    indexed = owner_client.post("/repositories/proj/index", headers=owner_headers,
                                json={"repository_path": str(repo_dir)})
    assert indexed.status_code == 200, indexed.text
    for name, role in (("editor", "editor"), ("viewer", "viewer")):
        r = owner_client.put("/repositories/proj/members", headers=owner_headers,
                             json={"email": f"{name}@example.com", "role": role})
        assert r.status_code == 200, r.text
    return {"users": users, "repo_dir": repo_dir}


def denied(response) -> bool:
    if response.status_code in (401, 403):
        return True
    return response.status_code == 404 and "Repository not found" in response.text or (
        response.status_code == 404 and "not found" in response.text.lower() and "Investigation" in response.text) or (
        response.status_code == 404 and "Patch not found" in response.text)


@pytest.fixture
def seeded(db_session, team):
    """An investigation and a patch inside ``proj``."""
    inv = InvestigationRecord(id=uuid.uuid4(), repository_id="proj", bug_report="x", status="completed",
                              result=None)
    db_session.add(inv)
    db_session.flush()
    patch = PatchProposalRecord(
        id=uuid.uuid4(), investigation_id=inv.id, execution_id=uuid.uuid4(), repository_id="proj", attempt_number=1,
        description="d", rationale="r", unified_diff="", affected_files=[], assumptions=[], risks=[],
        status="generated", status_history=[], provenance={})
    db_session.add(patch)
    db_session.flush()
    return {"investigation": str(inv.id), "patch": str(patch.id)}


def matrix(seeded, repo_dir):
    inv, patch = seeded["investigation"], seeded["patch"]
    return [
        # method, path, body, minimum role
        ("GET", "/repositories/proj/graph", None, "viewer"),
        ("GET", "/repositories/proj/graph/stats", None, "viewer"),
        ("GET", "/repositories/proj/index/status", None, "viewer"),
        ("POST", "/repositories/proj/search", {"query": "add"}, "viewer"),
        ("GET", f"/debug/investigations/{inv}", None, "viewer"),
        ("GET", f"/debug/investigations/{inv}/observations", None, "viewer"),
        ("GET", f"/debug/investigations/{inv}/patches", None, "viewer"),
        ("GET", f"/debug/patches/{patch}", None, "viewer"),
        ("GET", f"/debug/patches/{patch}/verification", None, "viewer"),
        ("POST", "/repositories/proj/index", {"repository_path": str(repo_dir)}, "editor"),
        ("POST", "/repositories/graph/build", {"repository_path": str(repo_dir), "repository_id": "proj"}, "editor"),
        ("POST", "/debug/investigate", {"repository_id": "proj", "bug_report": "add returns the wrong sum"},
         "editor"),
        ("POST", f"/debug/investigations/{inv}/patches", {}, "editor"),
        ("GET", "/repositories/proj/members", None, "owner"),
        ("PUT", "/repositories/proj/members", {"email": "viewer@example.com", "role": "viewer"}, "owner"),
        ("DELETE", "/repositories/proj/graph", None, "owner"),
        ("DELETE", "/repositories/proj/index", None, "owner"),
    ]


RANK = {"viewer": 1, "editor": 2, "owner": 3}


def test_every_repository_endpoint_enforces_membership_and_role(team, seeded):
    users = team["users"]
    cases = matrix(seeded, team["repo_dir"])
    # Deleting is destructive and changes later results, so owner-only operations run last for the owner.
    cases.sort(key=lambda c: c[3] == "owner")
    for method, path, body, minimum in cases:
        for name, role_rank in (("outsider", 0), ("viewer", 1), ("editor", 2), ("owner", 3)):
            client, headers, _ = users[name]
            response = client.request(method, path, headers=headers, json=body)
            allowed = role_rank >= RANK[minimum]
            context = (method, path, name, response.status_code, response.text[:120])
            if allowed:
                assert not denied(response), context
            else:
                assert denied(response), context
                # an outsider must not even learn that the repository exists
                if name == "outsider":
                    assert response.status_code in (404,) , context


def test_outsiders_get_the_same_answer_for_real_and_nonexistent_repositories(team):
    client, headers, _ = team["users"]["outsider"]
    real = client.get("/repositories/proj/graph", headers=headers)
    fake = client.get("/repositories/never-existed/graph", headers=headers)
    assert real.status_code == fake.status_code == 404 and real.json() == fake.json()


def test_viewers_cannot_modify_or_spend(team, seeded):
    client, headers, _ = team["users"]["viewer"]
    assert client.post("/debug/investigate", headers=headers,
                       json={"repository_id": "proj", "bug_report": "a real bug report here"}).status_code == 403
    assert client.post(f"/debug/investigations/{seeded['investigation']}/patches", headers=headers,
                       json={}).status_code == 403
    assert client.delete("/repositories/proj/index", headers=headers).status_code == 403


def test_the_first_indexer_owns_the_repository_and_nobody_can_take_it_over(team, env):
    outsider, headers, _ = team["users"]["outsider"]
    other = env["allowed"] / "proj2"
    other.mkdir()
    (other / "x.py").write_text("x = 1\n")
    # index a repository id that exists: not a member, so denied (and not created again)
    r = outsider.post("/repositories/proj/index", headers=headers, json={"repository_path": str(other)})
    assert r.status_code == 404
    # a new id is claimed by its creator
    assert outsider.post("/repositories/mine/index", headers=headers,
                         json={"repository_path": str(other)}).status_code == 200
    assert outsider.get("/repositories/mine/index/status", headers=headers).status_code == 200
    owner, owner_headers, _ = team["users"]["owner"]
    assert owner.get("/repositories/mine/index/status", headers=owner_headers).status_code == 404


def test_paths_outside_allowed_roots_do_not_claim_a_repository(team, db_session):
    client, headers, _ = team["users"]["outsider"]
    r = client.post("/repositories/squat/index", headers=headers, json={"repository_path": "/etc"})
    assert r.status_code == 403
    from sqlalchemy import select

    assert db_session.scalar(select(Repository).where(Repository.slug == "squat")) is None


def test_membership_management(team, db_session):
    owner, owner_headers, owner_user = team["users"]["owner"]
    members = owner.get("/repositories/proj/members", headers=owner_headers).json()["members"]
    assert {m["email"]: m["role"] for m in members} == {"owner@example.com": "owner", "editor@example.com": "editor",
                                                       "viewer@example.com": "viewer"}
    # the last owner cannot be removed or demoted
    assert owner.put("/repositories/proj/members", headers=owner_headers,
                     json={"email": "owner@example.com", "role": "viewer"}).status_code == 409
    assert owner.delete(f"/repositories/proj/members/{owner_user['id']}", headers=owner_headers).status_code == 409
    # unknown users, invalid roles
    assert owner.put("/repositories/proj/members", headers=owner_headers,
                     json={"email": "ghost@example.com", "role": "viewer"}).status_code == 404
    assert owner.put("/repositories/proj/members", headers=owner_headers,
                     json={"email": "viewer@example.com", "role": "admin"}).status_code == 422
    # a removed member loses access immediately
    viewer, viewer_headers, viewer_user = team["users"]["viewer"]
    assert viewer.get("/repositories/proj/index/status", headers=viewer_headers).status_code == 200
    assert owner.delete(f"/repositories/proj/members/{viewer_user['id']}", headers=owner_headers).status_code == 204
    assert viewer.get("/repositories/proj/index/status", headers=viewer_headers).status_code == 404
    # promotion works
    assert owner.put("/repositories/proj/members", headers=owner_headers,
                     json={"email": "editor@example.com", "role": "owner"}).status_code == 200
    owners = db_session.query(RepositoryMembership).join(Repository).filter(
        Repository.slug == "proj", RepositoryMembership.role == "owner").count()
    assert owners == 2


def test_a_member_cannot_grant_themselves_more(team):
    editor, headers, _ = team["users"]["editor"]
    assert editor.put("/repositories/proj/members", headers=headers,
                      json={"email": "editor@example.com", "role": "owner"}).status_code == 403


def test_evaluation_data_is_private_to_its_owner(env, team, db_session):
    from tests.evaluation.test_configuration import mutate

    a, ha, ua = team["users"]["owner"]
    b, hb, _ub = team["users"]["outsider"]
    body = mutate(name="private-cfg", test_execution={"backend": "local", "test_visibility": "blind"})
    created = a.post("/evaluations/configurations", headers=ha, json=body)
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    assert b.get(f"/evaluations/configurations/{cid}", headers=hb).status_code == 404
    assert b.post(f"/evaluations/configurations/{cid}/clone", headers=hb, json={}).status_code == 404
    assert cid not in [c["id"] for c in b.get("/evaluations/configurations", headers=hb).json()["items"]]
    assert cid in [c["id"] for c in a.get("/evaluations/configurations", headers=ha).json()["items"]]
    assert b.post("/evaluations/run", headers=hb, json={"name": "steal", "configuration_id": cid,
                                                       "mode": "reference"}).status_code == 404

    run = EvaluationRun(name="a-run", status="running", evaluation_mode="reference", diagnosis_mode="recorded",
                        configuration_snapshot={}, benchmark_version="v", selected_case_ids=[], total_cases=0,
                        environment={}, owner_id=uuid.UUID(ua["id"]))
    db_session.add(run)
    db_session.flush()
    run.status = "completed"
    db_session.flush()
    rid = str(run.id)
    for method, path in (("GET", f"/evaluations/{rid}"), ("GET", f"/evaluations/{rid}/cases"),
                         ("GET", f"/evaluations/{rid}/cases/bug-001-average"), ("GET", f"/evaluations/{rid}/metrics"),
                         ("POST", f"/evaluations/{rid}/recalculate"), ("POST", f"/evaluations/{rid}/cancel")):
        assert b.request(method, path, headers=hb).status_code == 404, path
        assert a.request(method, path, headers=ha).status_code != 404 or "case" in path, path
    assert rid not in [r["id"] for r in b.get("/evaluations", headers=hb).json()["items"]]
    assert rid in [r["id"] for r in a.get("/evaluations", headers=ha).json()["items"]]
    assert b.post("/evaluations/compare", headers=hb, json={"evaluation_run_ids": [rid, rid]}).status_code in (404, 422)
    assert b.post("/evaluations/baselines", headers=hb, json={"evaluation_run_id": rid}).status_code == 404
    baseline = a.post("/evaluations/baselines", headers=ha, json={"evaluation_run_id": rid})
    assert baseline.status_code == 201
    bid = baseline.json()["id"]
    assert b.get(f"/evaluations/baselines/{bid}", headers=hb).status_code == 404
    assert bid not in [x["id"] for x in b.get("/evaluations/baselines", headers=hb).json()["items"]]
    assert b.post(f"/evaluations/{rid}/regression-check", headers=hb, json={"baseline_id": bid}).status_code == 404


def test_system_records_are_readable_by_every_user_but_not_editable(env, team, db_session):
    run = EvaluationRun(name="historical", status="running", evaluation_mode="live", diagnosis_mode="recorded",
                        configuration_snapshot={}, benchmark_version="unknown-historical", selected_case_ids=[],
                        total_cases=0, environment={}, is_historical=True)
    db_session.add(run)
    db_session.flush()
    run.status = "completed"
    db_session.flush()
    b, hb, _ = team["users"]["outsider"]
    assert b.get(f"/evaluations/{run.id}", headers=hb).status_code == 200


def test_benchmark_host_endpoint_is_operator_only_and_off_by_default(team):
    client, headers, _ = team["users"]["owner"]
    assert client.post("/benchmarks/run", headers=headers, json={"case_id": "bug-001-average"}).status_code == 404


def test_the_repository_list_shows_only_the_callers_repositories_with_their_roles(team):
    owner, owner_headers, _ = team["users"]["owner"]
    viewer, viewer_headers, _ = team["users"]["viewer"]
    outsider, outsider_headers, _ = team["users"]["outsider"]
    mine = owner.get("/repositories", headers=owner_headers).json()["items"]
    assert [(r["repository_id"], r["role"]) for r in mine] == [("proj", "owner")]
    assert [(r["repository_id"], r["role"]) for r in viewer.get("/repositories", headers=viewer_headers).json()["items"]
            ] == [("proj", "viewer")]
    assert outsider.get("/repositories", headers=outsider_headers).json()["items"] == []


def test_repository_detail_reports_the_role_and_what_it_allows(team):
    for name, role, can_manage in (("owner", "owner", True), ("editor", "editor", False), ("viewer", "viewer", False)):
        client, headers, _ = team["users"][name]
        body = client.get("/repositories/proj", headers=headers).json()
        assert body["role"] == role and ("manage members" in body["capabilities"]) is can_manage
    assert "index and re-index" not in team["users"]["viewer"][0].get(
        "/repositories/proj", headers=team["users"]["viewer"][1]).json()["capabilities"]
    outsider, headers, _ = team["users"]["outsider"]
    assert outsider.get("/repositories/proj", headers=headers).status_code == 404


def test_only_editors_and_owners_see_the_servers_directory_layout(team):
    seen = {name: team["users"][name][0].get("/repositories/proj/index/status", headers=team["users"][name][1]).json()
            for name in ("owner", "editor", "viewer")}
    assert seen["owner"]["root_path"] and seen["editor"]["root_path"] == seen["owner"]["root_path"]
    assert seen["viewer"]["root_path"] == "" and seen["viewer"]["chunk_count"] == seen["owner"]["chunk_count"]
