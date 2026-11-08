"""15.3: configuration rules for all four environments; no secret in errors, summaries or examples."""

import re
from pathlib import Path

import pytest

from app.core.config import (
    ConfigurationError,
    Settings,
    environment_problems,
    load_settings,
)

STRONG = "x7Kq9vLm2RtZp4NwB8cYd3FhJs6Ua5Ge"
GOOD_DB = "postgresql+psycopg://atlas:Zq8rT5vN2mKp4LxW@db.internal:5432/atlas"


def prod(**overrides):
    values = {
        "environment": "production", "database_url": GOOD_DB, "auth_secret_key": STRONG,
        "neo4j_uri": "bolt://neo4j.internal:7687", "neo4j_password": "Hk3vP9mQ7rLs2TxN",
        "patch_test_backend": "container", "patch_container_allow_untrusted": True,
        "patch_workspace_dir": Path("/var/lib/atlas/workspaces"), "cors_allowed_origins": ["https://atlas.example.com"],
        "llm_allow_external": False,
        "smtp_host": "smtp.example.com", "smtp_from": "atlas@example.com", "public_base_url": "https://atlas.example.com",
    }
    values.update(overrides)
    return values


def make(**values):
    return Settings(_env_file=None, **values)


def test_development_and_test_are_permissive():
    for env in ("development", "test"):
        s = make(environment=env, database_url="postgresql://atlas:atlas_dev_password@localhost:5433/atlas")
        assert s.api_docs_enabled and not s.is_deployed and s.patch_test_backend == "local"


@pytest.mark.parametrize("env", ["staging", "production"])
def test_a_complete_deployed_configuration_is_valid(env):
    s = make(**prod(environment=env))
    assert s.is_deployed and not s.api_docs_enabled and s.patch_test_backend == "container"


@pytest.mark.parametrize("overrides,setting", [
    ({"auth_secret_key": None}, "AUTH_SECRET_KEY"),
    ({"auth_secret_key": "short"}, "AUTH_SECRET_KEY"),
    ({"auth_secret_key": "changeme"}, "AUTH_SECRET_KEY"),
    ({"database_url": "postgresql://atlas:atlas_dev_password@db/atlas"}, "DATABASE_URL"),
    ({"database_url": "postgresql://atlas@db/atlas"}, "DATABASE_URL"),
    ({"neo4j_password": None}, "NEO4J_PASSWORD"),
    ({"neo4j_password": "neo4j"}, "NEO4J_PASSWORD"),
    ({"patch_test_backend": "local"}, "PATCH_TEST_BACKEND"),
    ({"patch_container_allow_untrusted": False}, "PATCH_CONTAINER_ALLOW_UNTRUSTED"),
    ({"patch_workspace_dir": None}, "PATCH_WORKSPACE_DIR"),
    ({"agent_trusted_test_roots": [Path("/srv/repos")]}, "AGENT_TRUSTED_TEST_ROOTS"),
    ({"auth_cookie_secure": False}, "AUTH_COOKIE_SECURE"),
    ({"benchmarks_endpoint_enabled": True}, "BENCHMARKS_ENDPOINT_ENABLED"),
    ({"cors_allowed_origins": ["*"]}, "CORS_ALLOWED_ORIGINS"),
    ({"cors_allowed_origins": ["http://atlas.example.com"]}, "CORS_ALLOWED_ORIGINS"),
    ({"llm_allow_external": True, "anthropic_api_key": None}, "ANTHROPIC_API_KEY"),
])
@pytest.mark.parametrize("env", ["staging", "production"])
def test_insecure_or_missing_values_prevent_startup(env, overrides, setting):
    with pytest.raises(ValueError, match=setting):
        make(**prod(environment=env, **overrides))


def test_docs_cannot_be_forced_on_in_production():
    assert any("DOCS_ENABLED" in p for p in environment_problems(make(**prod(), docs_enabled=False)) + [
        "DOCS_ENABLED" if make(**prod(environment="staging", docs_enabled=True)).api_docs_enabled else ""])
    with pytest.raises(ValueError, match="DOCS_ENABLED"):
        make(**prod(docs_enabled=True))


def test_development_pointed_at_a_remote_database_is_refused():
    """A deployment that forgot ENVIRONMENT must not silently run with development rules."""
    with pytest.raises(ValueError, match="remote host"):
        make(environment="development", database_url="postgresql://u:pw@prod-db.example.com/atlas")


def test_load_settings_error_names_settings_but_never_values(monkeypatch):
    secret_db = "postgresql://atlas:Sup3rS3cretPassw0rd@db.internal/atlas"
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", secret_db)
    monkeypatch.setenv("NEO4J_URI", "bolt://neo4j.internal:7687")
    monkeypatch.setenv("NEO4J_PASSWORD", "neo4j")
    monkeypatch.setenv("AUTH_SECRET_KEY", "tooshort")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-visibleinerror-visibleinerror")
    monkeypatch.chdir("/")  # no .env
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    message = str(caught.value)
    assert "AUTH_SECRET_KEY" in message and "NEO4J_PASSWORD" in message
    for value in ("Sup3rS3cretPassw0rd", "tooshort", "visibleinerror", "neo4j.internal:7687"):
        assert value not in message, value


def test_field_level_errors_do_not_echo_the_bad_value(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pw@localhost/atlas")
    monkeypatch.setenv("PATCH_MAX_LLM_COST_USD", "not-a-number-secret-looking")
    monkeypatch.chdir("/")
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    assert "PATCH_MAX_LLM_COST_USD" in str(caught.value) and "secret-looking" not in str(caught.value)


def test_secrets_are_masked_everywhere_they_could_be_printed():
    s = make(**prod(anthropic_api_key="sk-ant-api03-maskme-maskme-maskme"))
    dumped = repr(s) + str(s) + str(s.model_dump()) + s.model_dump_json() + str(s.public_summary())
    for value in (STRONG, "Zq8rT5vN2mKp4LxW", "Hk3vP9mQ7rLs2TxN", "maskme-maskme"):
        assert value not in dumped, value
    assert "environment" in s.public_summary()


def test_public_summary_has_no_secret_fields():
    summary = make(**prod()).public_summary()
    assert not [k for k in summary if re.search(r"key|password|secret|token|url", k, re.IGNORECASE)]


def test_secrets_dir_values_are_read(tmp_path, monkeypatch):
    (tmp_path / "auth_secret_key").write_text(STRONG)
    s = Settings(_env_file=None, _secrets_dir=str(tmp_path), **{k: v for k, v in prod().items()
                                                                if k != "auth_secret_key"})
    assert s.auth_secret_key.get_secret_value() == STRONG


def test_the_example_file_holds_placeholders_only():
    text = (Path(__file__).resolve().parents[2] / ".env.example").read_text()
    assigned = {m.group(1): m.group(2) for m in re.finditer(r"^([A-Z_]+)=(.*)$", text, re.MULTILINE)}
    for name, value in assigned.items():
        if re.search(r"KEY|SECRET|TOKEN", name):
            assert value == "" or value.startswith("<"), name
    assert "AUTH_SECRET_KEY" in text and "ENVIRONMENT" in text


def test_empty_secret_files_mean_not_set(tmp_path, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "METRICS_TOKEN", "AUTH_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)  # the environment outranks the secrets directory
    for name in ("anthropic_api_key", "metrics_token"):
        (tmp_path / name).write_text("\n")
    (tmp_path / "auth_secret_key").write_text(STRONG + "\n")
    values = {k: v for k, v in prod().items() if k != "auth_secret_key"}
    s = Settings(_env_file=None, _secrets_dir=str(tmp_path), **values)
    assert s.anthropic_api_key is None and s.metrics_token is None
    assert s.auth_secret_key.get_secret_value() == STRONG  # the trailing newline is stripped
