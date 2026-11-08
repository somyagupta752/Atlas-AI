"""15.1: security boundaries asserted at the edge of the system (HTTP, container command, secrets in Git)."""

import os
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app, get_allowed_repository_roots, get_embedding_provider_optional
from app.patching.container import ContainerLimits, ContainerTestRunner
from app.patching.models import PatchLimits
from app.patching.workspace import Workspace
from app.retrieval.embeddings import HashingEmbeddingProvider
from app.tools.base import redact_secrets

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(db_session, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_allowed_repository_roots] = lambda: [allowed]
    app.dependency_overrides[get_embedding_provider_optional] = lambda: HashingEmbeddingProvider()
    yield TestClient(app), allowed, tmp_path
    app.dependency_overrides.clear()


# --- path traversal and symlink escape at the API ---------------------------------------


@pytest.mark.parametrize("endpoint", ["/repositories/analyze", "/repositories/api-x/index"])
def test_paths_outside_the_allowed_roots_are_refused(client, endpoint):
    http, allowed, tmp = client
    (tmp / "outside").mkdir()
    body = {"repository_path": str(tmp / "outside")}
    assert http.post(endpoint, json=body).status_code == 403
    for sneaky in (f"{allowed}/../outside", "/etc", "/", "~", "../../.."):
        body["repository_path"] = sneaky
        assert http.post(endpoint, json=body).status_code in (403, 422), sneaky


def test_a_symlink_inside_the_root_cannot_point_outside_it(client):
    http, allowed, tmp = client
    secret = tmp / "secret_dir"
    secret.mkdir()
    (secret / "passwords.py").write_text("PASSWORD = 'hunter2hunter2'\n")
    (allowed / "link").symlink_to(secret, target_is_directory=True)
    refused = http.post("/repositories/analyze", json={"repository_path": str(allowed / "link")})
    assert refused.status_code == 403
    repo = allowed / "repo"
    repo.mkdir()
    (repo / "ok.py").write_text("x = 1\n")
    (repo / "escape.py").symlink_to(secret / "passwords.py")
    body = http.post("/repositories/analyze", json={"repository_path": str(repo)}).json()
    assert "hunter2" not in str(body)
    assert all("escape" not in f["file_path"] or f.get("errors") for f in body.get("files", []))


def test_repository_ids_cannot_carry_traversal_or_injection(client):
    http, _, _ = client
    for bad in ("..%2f..%2fetc", "a'; DROP TABLE code_chunks;--", "a b", "x" * 300, "%00"):
        assert http.get(f"/repositories/{bad}/index/status").status_code in (404, 422), bad
        assert http.post(f"/repositories/{bad}/search", json={"query": "x"}).status_code in (404, 422), bad


def test_sql_metacharacters_in_filters_are_data_not_code(client):
    http, _, _ = client
    for value in ("' OR '1'='1", "';DROP TABLE x;--"):
        assert http.get("/evaluations", params={"status": value}).json()["items"] == []
        assert http.get("/evaluations/configurations", params={"name": value}).json()["items"] == []
    assert http.get("/evaluations").status_code == 200  # tables still exist


# --- resource exhaustion ----------------------------------------------------------------


@pytest.mark.parametrize("path", ["/evaluations/configurations", "/repositories/analyze", "/debug/investigate",
                                  "/evaluations/run"])
def test_oversized_bodies_are_rejected_before_parsing(client, path):
    http, _, _ = client
    assert http.post(path, content=b"{" + b" " * 70_000 + b"}",
                     headers={"content-type": "application/json"}).status_code == 413


def test_security_headers_are_set(client):
    http, _, _ = client
    r = http.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert http.get("/evaluations").headers["cache-control"] == "no-store"


def fake_workspace(tmp_path, repo):
    return Workspace(workspace_id="w", execution_id="e", label="l", path=tmp_path, snapshot=None)  # type: ignore[arg-type]


# --- the worker container ---------------------------------------------------------------


def test_container_command_has_every_isolation_flag_and_no_host_access(tmp_path):
    repo, out = tmp_path / "repo", tmp_path / "out"
    repo.mkdir()
    out.mkdir()
    runner = ContainerTestRunner(PatchLimits(), ContainerLimits())
    workspace = fake_workspace(tmp_path, repo)
    cmd = runner.docker_command("atlas-test-x", workspace, out, ["test_a.py"], 30)
    joined = " ".join(cmd)
    for required in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges", "--pids-limit",
                     "--memory ", "--memory-swap ", "--cpus", "nofile=", "fsize=", "--label atlas.patching=1"):
        assert required in joined, required
    assert "--privileged" not in joined and "docker.sock" not in joined and "--user" in cmd
    assert cmd[cmd.index("--user") + 1].split(":")[0] != "0"
    mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--mount"]
    assert len(mounts) == 2 and all(m.startswith("type=bind,src=" + str(tmp_path)) for m in mounts)
    assert not any(a in ("-v", "--volume", "--env-file", "--pid", "--ipc", "--net", "--network=host")
                   for a in cmd)
    envs = [cmd[i + 1].split("=")[0] for i, a in enumerate(cmd) if a == "--env"]
    assert set(envs) <= {"HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED", "PYTHONNOUSERSITE",
                         "PYTHONUTF8", "NO_COLOR"}


def test_host_secrets_in_the_environment_never_reach_the_container_command(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-hostsecret-hostsecret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:hostpassword@db/x")
    runner = ContainerTestRunner(PatchLimits(), ContainerLimits())
    repo, out = tmp_path / "r", tmp_path / "o"
    repo.mkdir()
    out.mkdir()
    workspace = fake_workspace(tmp_path, repo)
    text = " ".join(runner.docker_command("n", workspace, out, [], 5))
    assert "hostsecret" not in text and "hostpassword" not in text
    from app.patching.container import _cli_env

    assert "ANTHROPIC_API_KEY" not in _cli_env() and "DATABASE_URL" not in _cli_env()


@pytest.mark.container
def test_a_real_worker_cannot_see_host_secrets_or_the_network(tmp_path, monkeypatch):
    """Runs a hostile test in the real image: no env secrets, no network, no docker socket, no root."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-hostsecret-hostsecret")
    from app.patching.workspace import WorkspaceManager

    runner = ContainerTestRunner(PatchLimits(check_timeout_seconds=30), ContainerLimits())
    if not runner.available():
        pytest.skip("container image not built")
    source = tmp_path / "src"
    source.mkdir()
    (source / "test_hostile.py").write_text(
        "import os, socket, pathlib\n"
        "def test_env():\n    assert 'ANTHROPIC_API_KEY' not in os.environ and 'DATABASE_URL' not in os.environ\n"
        "    assert 'hostsecret' not in repr(dict(os.environ))\n"
        "def test_no_network():\n"
        "    s = socket.socket(); s.settimeout(3)\n"
        "    try:\n        s.connect(('1.1.1.1', 53))\n    except OSError:\n        return\n    raise AssertionError('network')\n"
        "def test_no_docker_socket():\n    assert not pathlib.Path('/var/run/docker.sock').exists()\n"
        "def test_not_root():\n    assert os.getuid() != 0\n"
        "def test_root_fs_read_only():\n"
        "    try:\n        open('/etc/atlas_probe', 'w')\n    except OSError:\n        return\n    raise AssertionError('writable')\n"
        "def test_cannot_read_host_home():\n    assert not pathlib.Path('/Users').exists() and not pathlib.Path('/host').exists()\n"
    )
    manager = WorkspaceManager(tmp_path / "ws")
    workspace = manager.create(source, execution_id="sec", label="sec")
    try:
        run = runner.run(workspace, ["test_hostile.py"], label="sec", kind="targeted", timeout=40)
    finally:
        manager.cleanup(workspace)
    outcomes = {k.split("::")[-1]: v.outcome.value for k, v in run.tests.items()}
    assert outcomes and all(v == "passed" for v in outcomes.values()), (outcomes, run.error, run.stderr[-400:])


# --- secrets ---------------------------------------------------------------------------


def test_redaction_covers_common_credential_shapes():
    text = ("key sk-ant-api03-abcdefghijklmnopqrstuvwx and password = 'hunter2hunter2' and "
            "postgresql://user:s3cretpass@host/db and ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    cleaned = redact_secrets(text)
    for leaked in ("abcdefghijklmnopqrstuvwx", "hunter2hunter2", "s3cretpass", "ghp_abcdefghijklmnopqrstuvwxyz"):
        assert leaked not in cleaned, leaked


SECRET_SHAPES = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|"
                           r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def test_no_credential_shaped_strings_are_committed():
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.split(b"\0")
    hits = []
    for name in filter(None, tracked):
        path = ROOT / name.decode()
        if name.startswith(b"tests/"):
            continue  # fixtures use deliberately fake keys; production code and docs must have none
        if not path.is_file() or path.stat().st_size > 2_000_000 or path.suffix in (".png", ".lock"):
            continue
        try:
            if SECRET_SHAPES.search(path.read_text(errors="ignore")):
                hits.append(str(path.relative_to(ROOT)))
        except OSError:
            continue
    assert hits == [], hits


def test_env_files_are_ignored_by_git_and_the_example_has_only_placeholders():
    ignored = subprocess.run(["git", "check-ignore", ".env", ".env.production"], check=False, cwd=ROOT, capture_output=True,
                             text=True).stdout.split()
    assert ".env" in ignored and ".env.production" in ignored
    example = (ROOT / ".env.example").read_text()
    for line in example.splitlines():
        if re.match(r"^(ANTHROPIC_API_KEY|EMBEDDING_API_KEY)=\S+", line):
            raise AssertionError(f"a real-looking key in .env.example: {line.split('=')[0]}")


def test_the_api_process_environment_is_not_exposed_by_any_endpoint(client):
    http, _, _ = client
    os.environ["ATLAS_PROBE_SECRET"] = "probe-secret-value-123456"
    try:
        for path in ("/health", "/evaluations", "/evaluations/benchmarks", "/openapi.json"):
            assert "probe-secret-value" not in http.get(path).text
    finally:
        del os.environ["ATLAS_PROBE_SECRET"]


# --- hostile XML from a worker ---------------------------------------------------------------------------------------

BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<testsuite><testcase classname="t" name="n"><failure message="&lol4;"/></testcase></testsuite>"""
EXTERNAL_ENTITY = """<?xml version="1.0"?>
<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]>
<testsuite><testcase classname="t" name="n"><failure message="&secret;"/></testcase></testsuite>"""
PLAIN = ('<testsuite><testcase classname="t" name="ok"/>'
         '<testcase classname="t" name="bad"><failure message="boom"/></testcase></testsuite>')


@pytest.mark.parametrize("document", [BILLION_LAUGHS, EXTERNAL_ENTITY, "<!DOCTYPE x><testsuite/>"])
def test_hostile_junit_xml_is_refused_not_expanded(tmp_path, document):
    from app.patching.test_execution import parse_junit

    report = tmp_path / "junit.xml"
    report.write_text(document)
    workspace = Workspace(workspace_id="w", execution_id="e", label="l", path=tmp_path, snapshot=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="forbidden XML"):
        parse_junit(report, workspace)


def test_a_normal_junit_report_still_parses(tmp_path):
    from app.patching.test_execution import parse_junit

    report = tmp_path / "junit.xml"
    report.write_text(PLAIN)
    workspace = Workspace(workspace_id="w", execution_id="e", label="l", path=tmp_path, snapshot=None)  # type: ignore[arg-type]
    outcomes = {k: v.outcome.value for k, v in parse_junit(report, workspace).items()}
    assert sorted(outcomes.values()) == ["failed", "passed"]
