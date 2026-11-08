"""14.2: validation, secrets, versioning, cloning (unit) and the configuration API (real PostgreSQL)."""

import copy

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.database import get_db
from app.evaluation.configuration import (
    ConfigurationSpec,
    SecretFoundError,
    assert_no_secrets,
    build_configuration,
    configuration_hash,
    merge_overrides,
    prompt_fingerprints,
    prompts_changed,
)
from app.evaluation.models import EvaluationRun
from app.main import app

EXAMPLE = {
    "name": "Opus Graph Hybrid",
    "model": {"provider": "anthropic", "model_id": "claude-opus-5-5", "effort": "high"},
    "retrieval": {"strategy": "hybrid", "graph_enabled": True},
    "agent": {"max_iterations": 10},
    "patching": {"max_attempts": 3},
    "test_execution": {"backend": "container", "test_visibility": "blind"},
    "budget": {"max_cost_usd": 4},
}


def settings(**kw):
    return Settings(database_url="postgresql+psycopg://x/y", _env_file=None, **kw)


def mutate(**path_values):
    body = copy.deepcopy(EXAMPLE)
    for path, value in path_values.items():
        node = body
        *parents, leaf = path.split("__")
        for key in parents:
            node = node[key]
        if value is Ellipsis:
            del node[leaf]
        else:
            node[leaf] = value
    return body


# --- validation ----------------------------------------------------------------------


def test_example_configuration_is_valid_and_defaults_are_written_out():
    document = build_configuration(ConfigurationSpec.model_validate(EXAMPLE), settings())
    assert document["model"]["effort"] == "high"
    assert document["agent"]["max_tool_calls"] == 20  # a default made explicit
    assert document["retrieval"]["embedding_model"] == "atlas-hashing-v1"  # from runtime settings
    assert document["prompts"]["patch_prompt_version"]
    assert len(document["prompts"]["diagnosis_system_sha256"]) == 64


@pytest.mark.parametrize("changes,fragment", [
    ({"model__model_id": ""}, "model_id"),
    ({"model__model_id": "gpt-imaginary"}, "unsupported model"),
    ({"model__provider": "openai"}, "provider"),
    ({"model__effort": "extreme"}, "effort"),
    ({"patching__max_attempts": 0}, "max_attempts"),
    ({"patching__max_attempts": 11}, "max_attempts"),
    ({"agent__max_iterations": 101}, "max_iterations"),
    ({"budget__max_cost_usd": 0}, "max_cost_usd"),
    ({"budget__max_cost_usd": -1}, "max_cost_usd"),
    ({"budget__max_cost_usd": 100.01}, "max_cost_usd"),
    ({"retrieval__strategy": "magic"}, "strategy"),
    ({"test_execution__backend": "ssh"}, "backend"),
    ({"test_execution__backend": Ellipsis}, "backend"),
    ({"test_execution__test_visibility": "peek"}, "test_visibility"),
    ({"agent__tools": ["shell"]}, "unknown tools"),
    ({"agent__tools": []}, "tools"),
    ({"name": ""}, "name"),
    ({"name": "x" * 201}, "name"),
])
def test_invalid_configurations_are_rejected_with_a_clear_message(changes, fragment):
    with pytest.raises(ValidationError) as caught:
        ConfigurationSpec.model_validate(mutate(**changes))
    assert fragment in str(caught.value)


def test_graph_strategy_needs_the_graph_enabled():
    with pytest.raises(ValidationError, match="graph_enabled"):
        ConfigurationSpec.model_validate(mutate(retrieval={"strategy": "graph", "graph_enabled": False}))


def test_unknown_and_server_owned_fields_are_rejected():
    for extra in ({"api_key": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"}, {"prompts": {}}, {"shell": "ls"}):
        with pytest.raises(ValidationError, match="Extra inputs"):
            ConfigurationSpec.model_validate({**EXAMPLE, **extra})
    with pytest.raises(ValidationError, match="Extra inputs"):
        ConfigurationSpec.model_validate(mutate(model={"provider": "anthropic", "model_id": "claude-opus-5-5",
                                                       "api_key": "x"}))


def test_retrieval_sources_follow_strategy_and_graph_switch():
    def sources(**retrieval):
        return ConfigurationSpec.model_validate(mutate(retrieval=retrieval)).retrieval.sources

    assert sources(strategy="hybrid") == ["keyword", "vector", "graph"]
    assert sources(strategy="hybrid", graph_enabled=False) == ["keyword", "vector"]
    assert sources(strategy="keyword") == ["keyword"]
    assert sources(strategy="none") == []


# --- secrets -------------------------------------------------------------------------


def test_credential_shaped_values_are_refused_anywhere():
    with pytest.raises(ValidationError, match="secret"):
        ConfigurationSpec.model_validate(mutate(name="sk-ant-api03-abcdefghijklmnopqrstuvwxyz"))
    with pytest.raises(ValidationError, match="secret"):
        ConfigurationSpec.model_validate(mutate(description="use Bearer abcdefghijklmnopqrstuvwxyz123456"))


def test_live_secret_values_never_reach_a_stored_document():
    live = settings(anthropic_api_key=SecretStr("super-secret-key-value"))
    spec = ConfigurationSpec.model_validate(mutate(description="note: super-secret-key-value"))
    with pytest.raises(SecretFoundError):
        build_configuration(spec, live)
    with pytest.raises(SecretFoundError):
        assert_no_secrets({"a": [{"b": "xx super-secret-key-value"}]}, ["super-secret-key-value"])


# --- identity ------------------------------------------------------------------------


def test_hash_ignores_name_and_description_but_not_conditions():
    base = build_configuration(ConfigurationSpec.model_validate(EXAMPLE), settings())
    renamed = build_configuration(ConfigurationSpec.model_validate({**EXAMPLE, "description": "other"}), settings())
    changed = build_configuration(ConfigurationSpec.model_validate(mutate(patching__max_attempts=2)), settings())
    assert configuration_hash(base) == configuration_hash(renamed)
    assert configuration_hash(base) != configuration_hash(changed)


def test_merge_overrides_is_deep_and_does_not_mutate_the_base():
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    merged = merge_overrides(base, {"a": {"b": 9}, "e": 4})
    assert merged == {"a": {"b": 9, "c": 2}, "d": 3, "e": 4}
    assert base == {"a": {"b": 1, "c": 2}, "d": 3}


def test_prompt_changes_are_detected():
    document = build_configuration(ConfigurationSpec.model_validate(EXAMPLE), settings())
    assert prompts_changed(document) == []
    document["prompts"] = {**document["prompts"], "patch_system_sha256": "0" * 64}
    assert prompts_changed(document) == ["patch_system_sha256"]
    assert set(prompt_fingerprints()) == set(document["prompts"])


# --- API -----------------------------------------------------------------------------


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def create(client, **changes):
    return client.post("/evaluations/configurations", json=mutate(**changes) if changes else EXAMPLE)


def test_create_get_and_list(client):
    created = create(client, name="api-cfg-1")
    assert created.status_code == 201
    body = created.json()
    assert body["version"] == 1 and body["model_id"] == "claude-opus-5-5" and body["test_backend"] == "container"
    assert client.get(f"/evaluations/configurations/{body['id']}").json()["config_hash"] == body["config_hash"]
    listing = client.get("/evaluations/configurations", params={"name": "api-cfg-1"}).json()
    assert [i["id"] for i in listing["items"]] == [body["id"]]
    assert listing["page"] == {"limit": 25, "offset": 0, "total": 1}


def test_duplicate_name_is_a_conflict(client):
    assert create(client, name="dup-cfg").status_code == 201
    again = create(client, name="dup-cfg")
    assert again.status_code == 409 and "Clone" in again.json()["detail"]


def test_invalid_requests_are_422_and_never_echo_the_input(client):
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
    response = client.post("/evaluations/configurations", json={**EXAMPLE, "api_key": secret})
    assert response.status_code == 422
    assert secret not in response.text
    assert any(e["loc"][-1] == "api_key" for e in response.json()["detail"])
    leaked_in_name = create(client, name=secret)
    assert leaked_in_name.status_code == 422 and secret not in leaked_in_name.text


def test_responses_and_stored_rows_never_contain_secrets(client, db_session):
    body = create(client, name="no-secrets").json()
    text = str(body)
    assert "sk-" not in text and "api_key" not in text.lower()


def test_clone_makes_a_new_version_and_leaves_history_unchanged(client, db_session):
    source = create(client, name="clone-me").json()
    run = EvaluationRun(
        name="used", status="completed", evaluation_mode="reference", diagnosis_mode="recorded",
        configuration_id=source["id"], configuration_snapshot=source["config"], benchmark_version="v1",
        selected_case_ids=[], total_cases=0, environment={},
    )
    db_session.add(run)
    db_session.flush()
    snapshot_before = copy.deepcopy(run.configuration_snapshot)

    clone = client.post(f"/evaluations/configurations/{source['id']}/clone",
                        json={"overrides": {"patching": {"max_attempts": 2}}})
    assert clone.status_code == 201
    new = clone.json()
    assert new["id"] != source["id"] and new["name"] == "clone-me" and new["version"] == 2
    assert new["parent_configuration_id"] == source["id"]
    assert new["config"]["patching"]["max_attempts"] == 2
    assert new["config_hash"] != source["config_hash"]

    original = client.get(f"/evaluations/configurations/{source['id']}").json()
    assert original["config"] == source["config"] and original["runs_using"] == 1
    db_session.refresh(run)
    assert run.configuration_snapshot == snapshot_before

    third = client.post(f"/evaluations/configurations/{source['id']}/clone", json={}).json()
    assert third["version"] == 3


def test_clone_with_a_new_name_starts_at_version_one(client):
    source = create(client, name="orig-name").json()
    other = client.post(f"/evaluations/configurations/{source['id']}/clone", json={"name": "renamed"}).json()
    assert other["name"] == "renamed" and other["version"] == 1
    taken = client.post(f"/evaluations/configurations/{source['id']}/clone", json={"name": "renamed"})
    assert taken.status_code == 409


def test_clone_validates_the_merged_result(client):
    source = create(client, name="bad-clone").json()
    bad = client.post(f"/evaluations/configurations/{source['id']}/clone",
                      json={"overrides": {"budget": {"max_cost_usd": -5}}})
    assert bad.status_code == 422 and "max_cost_usd" in str(bad.json())
    secret = client.post(f"/evaluations/configurations/{source['id']}/clone",
                         json={"overrides": {"model": {"api_key": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"}}})
    assert secret.status_code == 422 and "sk-ant" not in secret.text
    assert client.get("/evaluations/configurations", params={"name": "bad-clone"}).json()["page"]["total"] == 1


def test_clone_picks_up_changed_prompts(client, monkeypatch):
    source = create(client, name="prompt-drift").json()
    import app.evaluation.configuration as module

    monkeypatch.setattr(module, "PATCH_SYSTEM_PROMPT", module.PATCH_SYSTEM_PROMPT + " changed")
    clone = client.post(f"/evaluations/configurations/{source['id']}/clone", json={}).json()
    assert clone["config"]["prompts"]["patch_system_sha256"] != source["config"]["prompts"]["patch_system_sha256"]
    assert clone["config_hash"] != source["config_hash"]


def test_missing_and_malformed_ids(client):
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/evaluations/configurations/{missing}").status_code == 404
    assert client.post(f"/evaluations/configurations/{missing}/clone", json={}).status_code == 404
    assert client.get("/evaluations/configurations/not-a-uuid").status_code == 422
    assert client.post("/evaluations/configurations", json={}).status_code == 422
    assert client.post("/evaluations/configurations", content="{", headers={"content-type": "application/json"}
                       ).status_code == 422


def test_pagination_bounds_and_stable_order(client):
    for i in range(3):
        create(client, name=f"page-cfg-{i}")
    first = client.get("/evaluations/configurations", params={"limit": 2}).json()
    second = client.get("/evaluations/configurations", params={"limit": 2, "offset": 2}).json()
    again = client.get("/evaluations/configurations", params={"limit": 2}).json()
    assert [i["id"] for i in first["items"]] == [i["id"] for i in again["items"]]
    assert len(first["items"]) == 2 and first["page"]["total"] >= 3 and second["page"]["offset"] == 2
    ids = [i["id"] for i in first["items"]] + [i["id"] for i in second["items"]]
    assert len(set(ids)) == len(ids)
    for bad in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": 1_000_001}):
        assert client.get("/evaluations/configurations", params=bad).status_code == 422
    assert client.get("/evaluations/configurations", params={"offset": 999_999}).json()["items"] == []


def test_database_failure_is_a_generic_500(db_session):
    class Broken:
        def scalar(self, *a, **k):
            raise OperationalError("SELECT secret_table", {}, Exception("password=hunter2 host=10.0.0.5"))

        def __getattr__(self, name):
            raise OperationalError("SELECT secret_table", {}, Exception("password=hunter2"))

    app.dependency_overrides[get_db] = lambda: Broken()
    try:
        response = TestClient(app, raise_server_exceptions=False).get("/evaluations/configurations")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 500
    assert "hunter2" not in response.text and "secret_table" not in response.text and "Traceback" not in response.text
