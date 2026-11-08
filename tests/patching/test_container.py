"""Container backend: isolation is tested from the inside.

Each case writes a small repository whose tests *attempt* something the
container must prevent (network, host files, secrets, writes outside the
workspace, privilege escalation, process and memory exhaustion, outliving a
timeout), runs it through ContainerTestRunner, and checks the structured
per-test results. Needs Docker and the image from docker/test-runner.Dockerfile;
skips without them (REQUIRE_CONTAINER=1 makes that a failure).
"""

import os
import textwrap
import time
import uuid

import pytest

from app.patching.container import (
    CONTAINER_ENV,
    ContainerLimits,
    ContainerTestRunner,
    container_user,
    leftover_containers,
)
from app.patching.models import PatchLimits
from app.patching.service import TestBackendUnavailableError
from app.patching.test_execution import PytestRunner
from app.patching.verification import VerificationEngine
from app.patching.workspace import WorkspaceManager
from tests.patching.conftest import AVERAGE_FIX, submit

CANARY = "sk-ant-api03-container-canary-" + uuid.uuid4().hex


def _available() -> bool:
    return ContainerTestRunner().available()


requires_container = pytest.mark.skipif(
    not _available() and os.environ.get("REQUIRE_CONTAINER") != "1",
    reason="Docker or the atlas-ai-test-runner image is not available",
)


@pytest.fixture
def manager(tmp_path):
    return WorkspaceManager(tmp_path / "ws")


@pytest.fixture
def run_inside(write_repo, manager):
    """Write ``{path: source}``, copy it to a workspace, run its tests in a container."""

    def run(files, *, limits=None, timeout=60.0, runner=None, name="inner"):
        root = write_repo({path: textwrap.dedent(source) for path, source in files.items()}, name=name)
        workspace = manager.create(root, execution_id="container-test", label="patched-1")
        runner = runner or ContainerTestRunner(PatchLimits(), limits or ContainerLimits())
        return runner.run(workspace, sorted(p for p in files if p.startswith("test_")), label="patched",
                          kind="targeted", timeout=timeout), workspace

    return run


def outcomes(run):
    return {test_id.split("::")[-1]: result.outcome.value for test_id, result in run.tests.items()}


# --- command construction (no Docker needed) ---------------------------------------------


def test_docker_command_applies_every_control(tmp_path, manager, average_repo):
    workspace = manager.create(average_repo, execution_id="e", label="patched-1")
    out = workspace.meta / "out"
    out.mkdir()
    command = ContainerTestRunner().docker_command("atlas-test-x", workspace, out, ["test_solution.py"], 30)
    joined = " ".join(command)
    for flag in ("--init", "--network none", "--read-only", "--cap-drop ALL",
                 "--security-opt no-new-privileges", "--pids-limit 256", "--memory-swap", "--cpus 1.0",
                 f"--user {container_user()}", "--entrypoint timeout", "-k 5 30s"):
        assert flag in joined, flag
    assert "--rm" not in command  # removed after its OOM state is read
    assert "--privileged" not in command and "--env-file" not in command and "host" not in command
    assert not container_user().startswith("0:")
    env_values = [command[i + 1] for i, part in enumerate(command) if part == "--env"]
    assert env_values == [f"{k}={v}" for k, v in CONTAINER_ENV.items()]  # always KEY=VALUE: nothing inherited
    mounts = [command[i + 1] for i, part in enumerate(command) if part == "--mount"]
    assert mounts == [f"type=bind,src={workspace.repo},dst=/workspace", f"type=bind,src={out},dst=/atlas-out"]
    assert command[command.index("--entrypoint") + 2:].count("--junitxml=/atlas-out/junit.xml") == 1


def test_unvalidated_targets_are_refused_before_docker(manager, average_repo):
    workspace = manager.create(average_repo, execution_id="e", label="patched-1")
    for target in ("--rootdir=/", "../x.py", "/etc/passwd"):
        with pytest.raises(ValueError):
            ContainerTestRunner().run(workspace, [target], label="x", kind="targeted", timeout=5)


def test_missing_image_is_unavailable_and_fails_closed(build_service):
    runner = ContainerTestRunner(container=ContainerLimits(image="atlas-ai-test-runner:does-not-exist"))
    assert runner.available() is False
    service, llm, iid = build_service([submit(AVERAGE_FIX)], engine=VerificationEngine(runner=runner))
    with pytest.raises(TestBackendUnavailableError):
        service.generate_and_verify_patch(iid)
    assert llm.calls == []


class IsolatedFake(PytestRunner):
    """Claims to be isolated; for trust-policy tests only (it runs locally)."""

    isolated = True


def test_untrusted_repositories_need_both_an_isolated_backend_and_explicit_permission(build_service, tmp_path):
    from app.agents.agent import RepositoryAccessDeniedError

    untrusted = [tmp_path / "elsewhere"]
    service, _, iid = build_service([submit(AVERAGE_FIX)], trusted=untrusted,
                                    engine=VerificationEngine(runner=IsolatedFake()))
    with pytest.raises(RepositoryAccessDeniedError):  # isolated, but not permitted
        service.generate_and_verify_patch(iid)
    service, _, iid = build_service([submit(AVERAGE_FIX)], trusted=untrusted)
    service._allow_untrusted = True
    with pytest.raises(RepositoryAccessDeniedError):  # permitted, but the local backend is not isolated
        service.generate_and_verify_patch(iid)
    service, _, iid = build_service([submit(AVERAGE_FIX)], trusted=untrusted,
                                    engine=VerificationEngine(runner=IsolatedFake()))
    service._allow_untrusted = True
    assert service.generate_and_verify_patch(iid, max_attempts=1).status == "verified"


# --- isolation, from inside the container ----------------------------------------------------

ISOLATION = '''
    import os
    import pathlib
    import socket
    import threading

    import pytest

    HOST_SECRET = {secret!r}
    HOST_SOURCE = {source!r}
    CANARY = {canary!r}


    def test_runs_as_a_non_root_user():
        assert os.getuid() != 0 and os.geteuid() != 0


    def test_no_network_by_ip():
        for target in [("1.1.1.1", 53), ("8.8.8.8", 443), ("172.17.0.1", 80)]:
            with pytest.raises(OSError):
                socket.create_connection(target, timeout=2)


    def test_no_dns():
        with pytest.raises(OSError):
            socket.getaddrinfo("example.com", 443)


    def test_no_host_gateway():
        with pytest.raises(OSError):
            socket.getaddrinfo("host.docker.internal", 80)


    def test_no_routes_and_only_loopback_is_up():
        routes = pathlib.Path("/proc/net/route").read_text().splitlines()[1:]
        assert routes == []
        up = [i.name for i in pathlib.Path("/sys/class/net").iterdir()
              if i.name != "lo" and (i / "operstate").is_file()
              and (i / "operstate").read_text().strip() not in ("down", "unknown")]
        assert up == [] and pathlib.Path("/sys/class/net/lo").exists()


    def test_host_files_are_not_visible():
        assert not os.path.exists(HOST_SECRET)
        assert not os.path.exists(HOST_SOURCE)
        assert not os.path.exists("/Users") and not os.path.exists("/var/run/docker.sock")


    def test_no_secrets_in_the_environment():
        for key in ("ANTHROPIC_API_KEY", "DATABASE_URL", "NEO4J_PASSWORD", "EMBEDDING_API_KEY",
                    "ATLAS_CONTAINER_CANARY"):
            assert key not in os.environ
        assert CANARY not in repr(dict(os.environ))
        assert CANARY not in pathlib.Path("/proc/self/environ").read_text(errors="ignore")


    def test_root_filesystem_is_read_only():
        for path in ("/usr/local/lib/evil.py", "/etc/evil", "/evil", "/home/evil"):
            with pytest.raises(OSError):
                open(path, "w")


    def test_only_the_workspace_and_tmp_are_writable():
        pathlib.Path("written.txt").write_text("ok")
        pathlib.Path("/tmp/scratch").write_text("ok")


    def test_no_capabilities_and_no_new_privileges():
        status = pathlib.Path("/proc/self/status").read_text()
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        assert int(fields["CapEff"].strip(), 16) == 0
        assert int(fields["CapBnd"].strip(), 16) == 0
        assert fields["NoNewPrivs"].strip() == "1"


    def test_process_limit():
        threads, stop = [], threading.Event()
        try:
            for _ in range(500):
                thread = threading.Thread(target=stop.wait)
                thread.start()
                threads.append(thread)
        except RuntimeError:
            pass
        finally:
            stop.set()
        assert len(threads) < 64
'''


@pytest.mark.container
@requires_container
def test_isolation_from_inside(run_inside, tmp_path, monkeypatch):
    secret = tmp_path / "host-secret.txt"
    secret.write_text(CANARY)
    monkeypatch.setenv("ATLAS_CONTAINER_CANARY", CANARY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY)
    source = str(__import__("pathlib").Path(__file__).resolve().parents[2] / "app" / "config.py")
    run, workspace = run_inside(
        {"test_isolation.py": ISOLATION.format(secret=str(secret), source=source, canary=CANARY)},
        limits=ContainerLimits(pids_limit=64),
    )
    assert run.status == "completed", run.error
    results = outcomes(run)
    assert len(results) == 11
    failed = {name: run.tests[t].message for t in run.tests for name in [t.split("::")[-1]]
              if run.tests[t].outcome.value != "passed"}
    assert not failed, failed
    assert (workspace.repo / "written.txt").read_text() == "ok"  # the workspace is the one writable host path
    assert CANARY not in run.stdout + run.stderr
    assert leftover_containers() == []


@pytest.mark.container
@requires_container
def test_results_match_the_local_runner(manager, average_repo):
    workspace = manager.create(average_repo, execution_id="e", label="baseline")
    container = ContainerTestRunner().run(workspace, ["test_solution.py"], label="b", kind="targeted", timeout=60)
    local = PytestRunner().run(workspace, ["test_solution.py"], label="b", kind="targeted", timeout=60)
    assert container.status == local.status == "completed"
    assert container.exit_code == local.exit_code == 1
    assert container.command == local.command  # same command identity
    assert {k: v.outcome for k, v in container.tests.items()} == {k: v.outcome for k, v in local.tests.items()}
    assert "/workspace" not in container.stdout and "ZeroDivisionError" in container.stdout


@pytest.mark.container
@requires_container
def test_timeout_kills_the_container_and_every_child(run_inside):
    hang = '''
        import signal
        import subprocess
        import time


        def test_hang():
            signal.signal(signal.SIGTERM, signal.SIG_IGN)  # ignore the polite stop
            subprocess.Popen(["sleep", "300"])
            subprocess.Popen(["python", "-c", "import time; time.sleep(300)"], start_new_session=True)
            time.sleep(300)
    '''
    started = time.monotonic()
    run, _ = run_inside({"test_hang.py": hang}, timeout=3)
    assert run.status == "timed_out" and run.exit_code is None
    assert time.monotonic() - started < 20
    assert leftover_containers() == []


@pytest.mark.container
@requires_container
def test_host_deadline_kills_a_container_whose_inner_timeout_is_gone(run_inside):
    class NoInnerTimeout(ContainerTestRunner):
        def docker_command(self, name, workspace, out_dir, targets, timeout):
            command = super().docker_command(name, workspace, out_dir, targets, timeout)
            at = command.index("--entrypoint")
            image = command[at + 2]
            rest = command[at + 3:]
            return command[:at] + [image] + rest[rest.index("python"):]

    sleeper = '''
        import time


        def test_sleep():
            time.sleep(300)
    '''
    runner = NoInnerTimeout(PatchLimits(), ContainerLimits(startup_allowance_seconds=2))
    started = time.monotonic()
    run, _ = run_inside({"test_sleep.py": sleeper}, timeout=2, runner=runner)
    assert run.status == "timed_out" and time.monotonic() - started < 30
    assert leftover_containers() == []


@pytest.mark.container
@requires_container
def test_memory_limit_kills_the_run(run_inside):
    hog = '''
        def test_allocate():
            blocks = [bytearray(64 * 1024 * 1024) for _ in range(16)]  # 1 GiB
            assert blocks
    '''
    run, _ = run_inside({"test_hog.py": hog}, limits=ContainerLimits(memory_bytes=256 * 1024**2))
    assert run.status == "error" and "memory" in (run.error or "")
    assert leftover_containers() == []


@pytest.mark.container
@requires_container
def test_end_to_end_patch_verification_in_containers(build_service):
    engine = VerificationEngine(PatchLimits(baseline_runs=1), runner=ContainerTestRunner(PatchLimits(baseline_runs=1)))
    service, _, iid = build_service([submit(AVERAGE_FIX)], engine=engine)
    execution = service.generate_and_verify_patch(iid)
    assert execution.status == "verified", execution.summary
    environment = execution.attempts[0].verification.environment
    assert environment["test_backend"]["backend"] == "container"
    assert environment["test_backend"]["network"] == "none" and environment["test_backend"]["image_id"]
    assert leftover_containers() == []
