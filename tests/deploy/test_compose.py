"""15.7: static review of the production deployment files. Nothing is started; `docker compose config` only renders."""

import fnmatch
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.prod.yml"
SECRET_NAMES = ["database_url", "auth_secret_key", "neo4j_password", "postgres_password", "backup_encryption_key",
                "anthropic_api_key", "metrics_token", "smtp_password"]


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    if not shutil.which("docker") or subprocess.run(["docker", "compose", "version"], capture_output=True,
                                                    check=False).returncode:
        pytest.skip("docker compose is not available")
    secrets = tmp_path_factory.mktemp("secrets")
    for name in SECRET_NAMES:
        (secrets / name).write_text("placeholder")
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(Path.home()),
           "SECRETS_DIR": str(secrets), "REGISTRY": "registry.example", "ATLAS_VERSION": "1.0.0",
           "WORKSPACE_HOST_DIR": "/var/lib/atlas/workspaces", "REPOSITORIES_DIR": "/srv/repositories"}
    done = subprocess.run(["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"], env=env,
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr[-400:]
    return json.loads(done.stdout)


def services(rendered):
    return rendered["services"]


def test_expected_services_exist(rendered):
    assert set(services(rendered)) == {"api", "frontend", "postgres", "neo4j", "migrate", "backup", "docker-proxy"}


def test_missing_required_variables_stop_the_deployment():
    if not shutil.which("docker"):
        pytest.skip("docker is not available")
    done = subprocess.run(["docker", "compose", "-f", str(COMPOSE), "config", "-q"],
                          env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}, capture_output=True,
                          text=True, check=False)
    assert done.returncode != 0 and "required variable" in done.stderr


def test_only_the_dashboard_publishes_a_port_and_only_on_loopback_by_default(rendered):
    published = {name: s.get("ports") for name, s in services(rendered).items() if s.get("ports")}
    assert set(published) == {"frontend"}
    port = published["frontend"][0]
    assert port["host_ip"] == "127.0.0.1" and port["target"] == 8080


def test_databases_and_workers_are_not_reachable_from_outside(rendered):
    nets = rendered["networks"]
    assert nets["backend"]["internal"] and nets["workers"]["internal"] and nets["app"]["internal"]
    assert not nets["edge"].get("internal") and not nets["egress"].get("internal")
    s = services(rendered)
    assert set(s["postgres"]["networks"]) == {"backend"} and set(s["neo4j"]["networks"]) == {"backend"}
    assert set(s["docker-proxy"]["networks"]) == {"workers"}
    assert set(s["frontend"]["networks"]) == {"edge", "app"}
    assert set(s["api"]["networks"]) == {"app", "backend", "workers", "egress"}
    assert set(s["backup"]["networks"]) == {"backend"} and set(s["migrate"]["networks"]) == {"backend"}
    # nothing but the api can reach the Docker proxy, and only the api has a route to the internet
    on = lambda net: {n for n, svc in s.items() if net in svc["networks"]}
    assert on("workers") == {"api", "docker-proxy"} and on("egress") == {"api"} and on("edge") == {"frontend"}


def test_no_service_except_the_proxy_touches_the_docker_socket(rendered):
    for name, svc in services(rendered).items():
        for v in svc.get("volumes", []):
            mounted = f"{v.get('source')}:{v.get('target')}"
            if "docker.sock" in mounted:
                assert name == "docker-proxy", name
        assert "docker.sock" not in json.dumps(svc.get("environment", {})), name
    api = services(rendered)["api"]
    assert api["environment"]["DOCKER_HOST"] == "tcp://docker-proxy:2375"


def test_the_docker_proxy_denies_everything_it_does_not_need(rendered):
    env = services(rendered)["docker-proxy"]["environment"]
    for dangerous in ("EXEC", "VOLUMES", "NETWORKS", "BUILD", "COMMIT", "SERVICES", "SWARM", "SECRETS", "CONFIGS",
                      "PLUGINS", "AUTH", "SYSTEM", "INFO", "EVENTS", "NODES", "TASKS", "ALLOW_RESTARTS"):
        assert str(env[dangerous]) == "0", dangerous
    assert str(env["CONTAINERS"]) == "1" and str(env["POST"]) == "1"


def test_every_container_drops_capabilities_and_blocks_privilege_escalation(rendered):
    for name, svc in services(rendered).items():
        assert "no-new-privileges:true" in svc.get("security_opt", []), name
        assert svc.get("privileged") is not True and not svc.get("pid") and not svc.get("network_mode"), name
        if name != "docker-proxy":
            assert svc.get("cap_drop") == ["ALL"], name
    for name in ("postgres", "neo4j"):  # their entrypoints start as root and drop to an unprivileged user
        assert set(services(rendered)[name]["cap_add"]) <= {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"}
    for name in ("api", "frontend", "migrate", "backup"):
        assert services(rendered)[name]["read_only"] is True, name
        assert not services(rendered)[name].get("cap_add"), name


def test_secrets_are_mounted_files_never_environment_values(rendered):
    for name, svc in services(rendered).items():
        for key, value in (svc.get("environment") or {}).items():
            assert not re.search(r"(PASSWORD|SECRET|API_KEY|TOKEN|ENCRYPTION_KEY|DATABASE_URL)$", key) or \
                str(value).startswith("/run/secrets/") or key == "POSTGRES_PASSWORD_FILE", (name, key)
            assert "placeholder" not in str(value)
    s = services(rendered)
    assert {x["source"] for x in s["api"]["secrets"]} == {
        "database_url", "auth_secret_key", "neo4j_password", "anthropic_api_key", "metrics_token", "smtp_password"}
    # least privilege: the backup key reaches only the backup job; the postgres password only postgres
    holders = lambda secret: {n for n, svc in s.items() if secret in [x["source"] for x in svc.get("secrets", [])]}
    assert holders("backup_encryption_key") == {"backup"} and holders("postgres_password") == {"postgres"}
    assert holders("anthropic_api_key") == {"api"} and holders("neo4j_password") == {"api", "neo4j"}
    # the SMTP password reaches only the services that load the full settings; the frontend never sees it
    assert holders("smtp_password") == {"api", "migrate", "backup"}
    assert not services(rendered)["frontend"].get("secrets")


def test_backups_are_visible_only_to_the_backup_job(rendered):
    for name, svc in services(rendered).items():
        targets = [v["target"] for v in svc.get("volumes", [])]
        assert ("/var/lib/atlas/backups" in targets) == (name == "backup"), name


def test_the_workspace_path_is_identical_inside_and_outside_the_api(rendered):
    mounts = {v["source"]: v["target"] for v in services(rendered)["api"]["volumes"] if v["type"] == "bind"}
    assert mounts["/var/lib/atlas/workspaces"] == "/var/lib/atlas/workspaces"
    assert services(rendered)["api"]["environment"]["PATCH_WORKSPACE_DIR"] == "/var/lib/atlas/workspaces"
    repo_mount = next(v for v in services(rendered)["api"]["volumes"] if v["target"] == "/srv/repositories")
    assert repo_mount["read_only"] is True


def test_startup_order_runs_migrations_before_the_api_and_waits_for_health(rendered):
    s = services(rendered)
    assert s["api"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert s["api"]["depends_on"]["neo4j"]["condition"] == "service_healthy"
    assert s["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert s["frontend"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert s["backup"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert s["migrate"]["restart"] == "no" and s["api"]["restart"] == "unless-stopped"
    for name in ("postgres", "neo4j"):
        assert s[name]["healthcheck"]["test"]


def test_every_environment_variable_is_a_real_setting_or_a_known_tool_variable(rendered):
    fields = {name.upper() for name in Settings.model_fields}
    tool_vars = {"DOCKER_HOST", "DOCKER_CONFIG", "FORWARDED_ALLOW_IPS", "BACKUP_DIR", "POSTGRES_USER", "POSTGRES_DB",
                 "POSTGRES_PASSWORD_FILE", "NEO4J_AUTH", "BACKUP_PG_DOCKER_CONTAINER"}
    proxy = set(services(rendered)["docker-proxy"]["environment"])
    for name, svc in services(rendered).items():
        if name == "docker-proxy":
            continue
        unknown = set(svc.get("environment", {})) - fields - tool_vars
        assert not unknown, (name, unknown)
    assert proxy  # the proxy's own variables are documented by its image


def test_production_settings_in_the_compose_file_pass_atlas_startup_validation(rendered, tmp_path):
    """The values the stack sets, plus placeholder-strength secrets, must satisfy the deployed-environment rules."""
    for name in ("api", "migrate", "backup"):
        env = {k: v for k, v in services(rendered)[name]["environment"].items() if k.upper() in
               {f.upper() for f in Settings.model_fields}}
        values = {k.lower(): v for k, v in env.items()}
        values.update(database_url="postgresql://atlas:Zq8rT5vN2mKp4LxW@postgres:5432/atlas",
                      auth_secret_key="x7Kq9vLm2RtZp4NwB8cYd3FhJs6Ua5Ge")
        if "neo4j_uri" in values:
            values["neo4j_password"] = "Hk3vP9mQ7rLs2TxN"
        values["allowed_repository_roots"] = [Path("/srv/repositories")]
        values.pop("cors_allowed_origins", None)
        values.pop("allowed_repository_roots", None)
        # Left unset (deploy.env supplies them), the stack must NOT start: a deployment cannot silently lack reset email.
        with pytest.raises(ValueError, match="SMTP_HOST is required"):
            Settings(_env_file=None, **values)
        values.update(smtp_host="smtp.example.com", smtp_from="atlas@example.com",
                      public_base_url="https://atlas.example.com")
        settings = Settings(_env_file=None, **values)
        assert settings.environment == "production" and settings.patch_test_backend == "container", name
        assert settings.patch_container_allow_untrusted and not settings.agent_trusted_test_roots, name


def test_resource_limits_are_set(rendered):
    for name, svc in services(rendered).items():
        assert svc.get("mem_limit") or svc.get("deploy", {}).get("resources"), name
    assert services(rendered)["api"]["pids_limit"] and services(rendered)["api"]["stop_grace_period"] == "45s"


# --- the Dockerfiles and ignore files ------------------------------------------------------------------------------


def dockerignore_patterns(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]


def ignored(relative: str, patterns: list[str]) -> bool:
    excluded = False
    for pattern in patterns:
        negate = pattern.startswith("!")
        bare = pattern.lstrip("!").rstrip("/")
        hit = relative == bare or relative.startswith(bare + "/") or fnmatch.fnmatch(relative, bare) or \
            fnmatch.fnmatch(Path(relative).name, bare)
        if hit:
            excluded = not negate
    return excluded


@pytest.mark.parametrize("dockerfile,context", [("Dockerfile", "."), ("frontend/Dockerfile", "frontend")])
def test_every_copied_path_exists_and_is_not_excluded_by_dockerignore(dockerfile, context):
    base = ROOT / context
    patterns = dockerignore_patterns(base / ".dockerignore")
    copies = re.findall(r"^COPY\s+(?:--\S+\s+)*(.+?)\s+\S+\s*$", (ROOT / dockerfile).read_text(), re.MULTILINE)
    assert copies
    for sources in copies:
        for source in sources.split():
            relative = source.rstrip("/")
            if relative.startswith("/"):  # from another build stage
                continue
            assert (base / relative).exists(), f"{dockerfile}: COPY source {relative!r} does not exist"
            assert not ignored(relative, patterns), f"{dockerfile}: {relative!r} is excluded by .dockerignore"


def test_dockerignore_keeps_secrets_and_history_out_of_every_build():
    for ignore in (ROOT / ".dockerignore", ROOT / "frontend" / ".dockerignore"):
        patterns = dockerignore_patterns(ignore)
        for sensitive in (".env", "secrets/x", "id.pem", "id.key"):
            assert ignored(sensitive, patterns) or "frontend" in str(ignore) and sensitive == "secrets/x", \
                (ignore, sensitive)
    root = dockerignore_patterns(ROOT / ".dockerignore")
    assert all(ignored(p, root) for p in (".git/config", "secrets/database_url", "backups/x", ".env.production",
                                          "deploy.env", "tests/test_api.py"))
    assert not ignored(".env.example", root) and not ignored("deploy.env.example", root)


def test_the_backend_image_is_non_root_pinned_and_copies_no_secrets():
    text = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^USER atlas$", text, re.MULTILINE) and "USER root" in text  # root only inside the backup build stage
    assert text.rstrip().splitlines()[-1].startswith("CMD") and "FROM base AS runtime" in text
    assert text.index("FROM base AS backup") < text.index("FROM base AS runtime")  # the default target is the API
    assert "uv sync --frozen --no-dev" in text and "HEALTHCHECK" in text
    assert not re.search(r"(?im)^(ENV|ARG)\s+\S*(PASSWORD|SECRET|API_KEY|TOKEN)", text)
    assert "WORKDIR /app" in text.split("AS builder")[1].split("AS base")[0]  # venv path equals the runtime path
    assert re.search(r"chmod 700 /var/lib/atlas/backups", text)


def test_the_frontend_image_serves_static_files_and_proxies_only_api_paths():
    nginx = (ROOT / "frontend" / "nginx.conf").read_text()
    assert "proxy_set_header X-Forwarded-For $remote_addr" in nginx and "proxy_add_x_forwarded_for" not in nginx
    assert "(auth|evaluations|repositories|debug)" in nginx
    active = "\n".join(line for line in nginx.splitlines() if not line.strip().startswith("#"))
    for private in ("/docs", "/openapi.json", "/metrics", "/redoc"):
        assert private not in active
    text = (ROOT / "frontend" / "Dockerfile").read_text()
    assert "nginx-unprivileged" in text and "npm ci" in text and "vite" not in text.split("AS runtime")[1]


def test_workflows_and_images_never_embed_secret_looking_values():
    for path in [COMPOSE, ROOT / "Dockerfile", ROOT / "deploy.env.example", ROOT / "frontend" / "Dockerfile"]:
        assert not re.search(r"sk-ant-|AKIA[0-9A-Z]{16}|-----BEGIN", path.read_text()), path.name


# --- application fixes the deployment depends on ---------------------------------------------------------------


def test_evaluation_scratch_space_defaults_to_the_shared_workspace_directory(tmp_path):
    from app.evaluation.benchmarks import load_suite
    from app.evaluation.runner import EvaluationRunner

    shared = tmp_path / "shared-workspaces"
    settings = Settings(_env_file=None, database_url="postgresql://x/y", patch_workspace_dir=shared)
    runner = EvaluationRunner(session_factory=lambda: None, settings=settings, suite=load_suite())
    assert runner.workspace_dir == shared and shared.is_dir()
    unset = EvaluationRunner(session_factory=lambda: None, settings=Settings(_env_file=None, database_url="postgresql://x/y",
                                                                             patch_workspace_dir=None),
                             suite=load_suite())
    assert unset.workspace_dir is None  # development: the system temp directory


def test_an_image_without_git_reports_the_commit_it_was_built_from(monkeypatch):
    from app.evaluation.environment import git_info

    monkeypatch.setenv("ATLAS_REVISION", "abc123def456")
    assert git_info() == {"commit": "abc123def456", "dirty": False, "source": "image"}
    monkeypatch.setenv("ATLAS_REVISION", "unknown")
    assert git_info().get("source") != "image"  # "unknown" is not a commit


def test_the_backup_key_can_come_from_a_mounted_secret_file(tmp_path, monkeypatch):
    from app.ops import backup as bk
    from app.ops.crypto import generate_key, parse_key

    key = generate_key()
    (tmp_path / "backup_encryption_key").write_text(key + "\n")
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "b"))
    monkeypatch.setattr("app.core.config.SECRETS_DIR", tmp_path)
    assert bk.BackupConfig.from_environment().key == parse_key(key)
    (tmp_path / "backup_encryption_key").unlink()
    with pytest.raises(bk.BackupError, match="required"):
        bk.BackupConfig.from_environment()


def test_direct_mode_pg_commands_pass_the_password_only_through_the_environment(tmp_path):
    from app.ops import backup as bk
    from app.ops.crypto import generate_key, parse_key

    db = bk.Database("atlas", "p@ssw0rd-value", "postgres", 5432, "atlas")
    config = bk.BackupConfig(tmp_path, parse_key(generate_key()), db, docker_container=None)
    argv = bk._argv(config, ["pg_dump", "-U", db.user, "-d", db.name, "-Fc"])
    assert argv[:1] == ["pg_dump"] and argv[-4:] == ["-h", "postgres", "-p", "5432"]
    assert "p@ssw0rd-value" not in " ".join(argv) and bk._env(config)["PGPASSWORD"] == "p@ssw0rd-value"
    via_docker = bk._argv(bk.BackupConfig(tmp_path, config.key, db, docker_container="pg"), ["pg_dump"])
    assert "p@ssw0rd-value" not in " ".join(via_docker) and "PGPASSWORD" in via_docker


def test_the_backup_stage_has_postgresql_16_client_tools():
    text = (ROOT / "Dockerfile").read_text()
    stage = text.split("AS backup")[1].split("AS runtime")[0]
    assert "postgresql-client-16" in stage and "apt.postgresql.org.sh" in stage


def _proxied_by_nginx() -> set[str]:
    nginx = (ROOT / "frontend" / "nginx.conf").read_text()
    paths = set(re.search(r"location ~ \^/\(([^)]*)\)", nginx).group(1).split("|"))
    return paths | {m for m in re.findall(r"location = /(\w+) \{", nginx) if m in ("ready", "health")}


def _proxied_by_vite() -> set[str]:
    vite = (ROOT / "frontend" / "vite.config.js").read_text()
    listed = re.search(r"Object\.fromEntries\(\[([^\]]*)\]", vite).group(1)
    return {p.strip('"/ ') for p in listed.split(",") if p.strip()}


def test_the_dev_proxy_and_the_production_proxy_forward_the_same_api_paths():
    """A path that works in `npm run dev` but is missing in nginx would only fail after deployment."""
    production, development = _proxied_by_nginx(), _proxied_by_vite()
    assert development - {"ready"} <= production, (development, production)
    assert {"auth", "repositories", "evaluations", "debug"} <= production
    for private in ("docs", "openapi", "metrics", "redoc"):
        assert private not in production and private not in development
    vite = (ROOT / "frontend" / "vite.config.js").read_text()
    # the browser tests run the production bundle through `vite preview`: it must forward exactly what `vite dev` does
    assert "server: { proxy }" in vite and "preview: { proxy }" in vite


def test_no_page_of_the_dashboard_shares_a_path_with_an_api_endpoint():
    """A page at /repositories would show the API's JSON after a reload, because the proxy forwards that prefix."""
    app = (ROOT / "frontend" / "src" / "App.jsx").read_text()
    pages = re.findall(r'<Route path="(/[^"*]*)"', app) + re.findall(r'<NavLink to="(/[^"]*)"', app)
    assert pages
    api_prefixes = _proxied_by_nginx() | _proxied_by_vite()
    for page in pages:
        first = page.strip("/").split("/")[0]
        assert first not in api_prefixes, f"page route {page!r} collides with the API prefix /{first}"
