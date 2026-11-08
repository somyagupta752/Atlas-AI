"""Worker host validation (design: docs/security/rootless-worker-design.md, plan: worker-validation-plan.md).

Opt-in. These tests start real, hardened, throwaway containers on whichever daemon ``DOCKER_HOST`` selects, so they run
only when asked:

    ATLAS_WORKER_VALIDATION=1 DOCKER_HOST=unix:///run/user/<W>/docker.sock \\
    ATLAS_EXPECT_ROOTLESS=1 ATLAS_WORKSPACE_ROOT=/var/lib/atlas-workers/workspaces \\
    uv run pytest tests/security/test_worker_host.py -m worker_host -v

* ``ATLAS_EXPECT_ROOTLESS=1`` turns on the assertions that only hold for a rootless daemon (V1, V9). Run WITHOUT it
  against a development daemon and those are skipped: a passing run on Docker Desktop proves nothing about rootless.
* ``ATLAS_WORKSPACE_ROOT`` is where workspaces are created (the ACL-prepared directory on the VM). Default: a temp dir.
* ``ATLAS_PROXY_URL`` (for example ``http://127.0.0.1:2375``) enables the proxy-verb checks (V10).
* Run as an OPERATIONS account that is not ``atlas-worker``, or V9's canary will be readable by design.

Every test bounds its own damage: allocation, fork and write loops stop at fixed limits even if a limit is not enforced,
and a failure to enforce a limit fails the test instead of exhausting the VM.
"""

import json
import os
import shutil
import stat
import subprocess
import textwrap
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.patching.container import (
    NAME_PREFIX,
    ContainerLimits,
    ContainerTestRunner,
    leftover_containers,
)
from app.patching.models import PatchLimits
from app.patching.workspace import WorkspaceManager

pytestmark = [
    pytest.mark.worker_host,
    pytest.mark.skipif(not os.environ.get("ATLAS_WORKER_VALIDATION"),
                       reason="set ATLAS_WORKER_VALIDATION=1 to run worker host validation"),
]
ROOTLESS = bool(os.environ.get("ATLAS_EXPECT_ROOTLESS"))
needs_rootless = pytest.mark.skipif(not ROOTLESS, reason="only meaningful against a rootless daemon "
                                                         "(set ATLAS_EXPECT_ROOTLESS=1)")
IMAGE = Settings(_env_file=None, database_url="postgresql://x/y").patch_container_image
KB, MB = 1024, 1024 * 1024


def docker(*args: str, timeout: int = 120, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=check)


@pytest.fixture(scope="module", autouse=True)
def worker_image():
    info = docker("info", "--format", "{{json .}}")
    if info.returncode != 0:
        pytest.fail(f"cannot reach the Docker daemon: {info.stderr.strip()[:200]}")
    if docker("image", "inspect", IMAGE).returncode != 0:
        pytest.fail(f"the worker image {IMAGE} is not in THIS daemon's image store (images are per daemon)")


@pytest.fixture
def workspaces(tmp_path):
    root = os.environ.get("ATLAS_WORKSPACE_ROOT")
    base = Path(root) / f"validation-{uuid.uuid4().hex[:8]}" if root else tmp_path / "ws"
    base.mkdir(parents=True, exist_ok=True)
    manager = WorkspaceManager(base)
    yield manager
    shutil.rmtree(base, ignore_errors=True)


def runner(**limits) -> ContainerTestRunner:
    defaults = {"image": IMAGE, "memory_bytes": 128 * MB, "pids_limit": 64, "cpus": 0.5}
    defaults.update(limits)
    return ContainerTestRunner(PatchLimits(check_timeout_seconds=60), ContainerLimits(**defaults))


def run_hostile(manager, test_source: str, *, timeout: float = 40, **limits):
    """Run ``test_source`` as a pytest file through the real production command."""
    source = manager.base_dir.parent / f"src-{uuid.uuid4().hex[:6]}"
    source.mkdir()
    (source / "test_hostile.py").write_text(textwrap.dedent(test_source))
    workspace = manager.create(source, execution_id=uuid.uuid4().hex, label="validation")
    try:
        result = runner(**limits).run(workspace, ["test_hostile.py"], label="v", kind="targeted", timeout=timeout)
        return result, workspace
    finally:
        shutil.rmtree(source, ignore_errors=True)


def outcomes(result) -> dict[str, str]:
    return {k.split("::")[-1]: v.outcome.value for k, v in result.tests.items()}


@pytest.fixture(autouse=True)
def no_leftovers():
    yield
    assert leftover_containers() == [], "a worker container survived its run"


# --- V1, V2: the daemon and its limits -------------------------------------------------------------------------------


@needs_rootless
def test_v1_the_daemon_is_rootless_and_its_account_is_unprivileged():
    info = json.loads(docker("info", "--format", "{{json .}}").stdout)
    assert any("rootless" in option for option in info["SecurityOptions"]), info["SecurityOptions"]
    assert os.geteuid() != 0, "run the validation as an operations account, not root"
    process = subprocess.run(["ps", "-o", "user=,args=", "-C", "dockerd"], capture_output=True, text=True, check=False)
    assert process.stdout.strip() and not process.stdout.startswith("root "), process.stdout


def test_v2_cgroup_v2_and_every_limit_is_enforced_from_inside_the_container():
    assert docker("info", "--format", "{{.CgroupVersion}}").stdout.strip() == "2"
    probe = docker("run", "--rm", "--network", "none", "--memory", "128m", "--memory-swap", "128m", "--cpus", "0.5",
                   "--pids-limit", "64", "--entrypoint", "sh", IMAGE, "-c",
                   "cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory.swap.max /sys/fs/cgroup/pids.max "
                   "/sys/fs/cgroup/cpu.max")
    assert probe.returncode == 0, probe.stderr[-300:]
    memory, swap, pids, cpu = probe.stdout.split("\n")[:4]
    assert int(memory) == 128 * MB, "memory limit not enforced (controller not delegated?)"
    assert swap in ("0", "max") == False or int(swap) == 0, "swap must be disabled"
    assert pids == "64", "pids limit not enforced (controller not delegated?)"
    assert cpu.split()[0] == "50000", "cpu limit not enforced (controller not delegated?)"


# --- V3, V4, V5: what the worker can see, write and reach --------------------------------------------------------------


def test_v3_host_files_secrets_and_the_docker_socket_are_invisible(workspaces, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-hostsecret-hostsecret")
    canary = tmp_path / "host-canary.txt"
    canary.write_text("HOST-SECRET-CANARY")
    result, _ = run_hostile(workspaces, f"""
        import os, pathlib

        def test_canary_path_does_not_exist():
            assert not pathlib.Path({str(canary)!r}).exists()

        def test_no_docker_socket_or_secret_mounts():
            for p in ("/var/run/docker.sock", "/run/docker.sock", "/run/secrets", "/host", "/Users"):
                assert not pathlib.Path(p).exists(), p

        def test_environment_is_clean():
            assert "ANTHROPIC_API_KEY" not in os.environ and "DATABASE_URL" not in os.environ
            assert "hostsecret" not in repr(dict(os.environ))

        def test_not_root_and_no_capabilities():
            assert os.getuid() != 0
            status = pathlib.Path("/proc/self/status").read_text()
            assert "CapEff:\\t0000000000000000" in status and "NoNewPrivs:\\t1" in status
    """)
    assert outcomes(result) and all(v == "passed" for v in outcomes(result).values()), (outcomes(result), result.error)


def test_v4_only_the_workspace_and_tmp_are_writable_and_symlinks_lead_nowhere(workspaces, tmp_path):
    canary = tmp_path / "outside.txt"
    canary.write_text("OUTSIDE")
    result, workspace = run_hostile(workspaces, f"""
        import os, pathlib, pytest

        def test_workspace_is_writable():
            pathlib.Path("ok.txt").write_text("x")

        @pytest.mark.parametrize("target", ["/etc/atlas_probe", "/usr/atlas_probe", "/atlas_probe",
                                            "/proc/sys/kernel/atlas_probe"])
        def test_nowhere_else_is_writable(target):
            with pytest.raises(OSError):
                open(target, "w")

        def test_tmp_is_size_limited():
            written = 0
            try:
                with open("/tmp/fill", "wb") as handle:
                    for _ in range(256):
                        handle.write(b"x" * 1048576)
                        written += 1
            except OSError:
                pass
            assert written < 256, "tmpfs was not size limited"

        def test_symlink_to_a_host_path_does_not_expose_it():
            os.symlink({str(canary)!r}, "link")
            assert not pathlib.Path("link").exists()
            with pytest.raises(OSError):
                open("link").read()
    """, memory_bytes=512 * MB)  # /tmp is memory-backed: the tmpfs size limit (128 MiB) must trip before the memory limit
    assert outcomes(result) and all(v == "passed" for v in outcomes(result).values()), (outcomes(result), result.error)
    assert (workspace.repo / "ok.txt").exists() and canary.read_text() == "OUTSIDE"


def test_v5_the_worker_has_no_network(workspaces):
    result, _ = run_hostile(workspaces, """
        import socket, pathlib, pytest

        @pytest.mark.parametrize("host,port", [("1.1.1.1", 53), ("8.8.8.8", 443), ("169.254.169.254", 80),
                                               ("10.0.0.1", 22), ("172.17.0.1", 80), ("127.0.0.1", 2375)])
        def test_connections_fail(host, port):
            s = socket.socket()
            s.settimeout(3)
            with pytest.raises(OSError):
                s.connect((host, port))

        def test_dns_fails():
            with pytest.raises(OSError):
                socket.getaddrinfo("example.com", 80)

        def test_no_interface_is_up_except_loopback_and_there_is_no_route():
            for entry in pathlib.Path("/sys/class/net").iterdir():
                if entry.name != "lo" and entry.is_dir():  # down, unconfigured devices (tunl0, gre0) may be listed
                    assert not int((entry / "flags").read_text(), 16) & 1, f"{entry.name} is up"
            routes = pathlib.Path("/proc/net/route").read_text().strip().splitlines()
            assert len(routes) <= 1, routes   # header only: no default route, no host route
    """)
    assert outcomes(result) and all(v == "passed" for v in outcomes(result).values()), (outcomes(result), result.error)


# --- V6: exhaustion --------------------------------------------------------------------------------------------------


def test_v6_memory_exhaustion_is_killed_by_the_limit(workspaces):
    result, _ = run_hostile(workspaces, """
        def test_allocate():
            blocks = []
            for _ in range(24):          # bounded at 1.5 GiB even if the limit were missing
                blocks.append(bytearray(64 * 1024 * 1024))
                for i in range(0, len(blocks[-1]), 4096):
                    blocks[-1][i] = 1
    """)
    assert result.status == "error" and "memory limit" in (result.error or ""), (result.status, result.error)


def test_v6_process_exhaustion_is_capped_by_the_pids_limit(workspaces):
    result, _ = run_hostile(workspaces, """
        import os, time

        def test_fork_cap():
            children, failed = [], False
            for _ in range(300):         # bounded: at most 300 sleeping children even if unlimited
                try:
                    pid = os.fork()
                except OSError:
                    failed = True
                    break
                if pid == 0:
                    time.sleep(8)
                    os._exit(0)
                children.append(pid)
            for pid in children:
                try:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
                except OSError:
                    pass
            assert failed and len(children) <= 64, f"forked {len(children)} processes"
    """)
    assert outcomes(result) and all(v == "passed" for v in outcomes(result).values()), (outcomes(result), result.error)


def test_v6_file_size_and_descriptor_limits_apply(workspaces):
    result, _ = run_hostile(workspaces, """
        import resource, signal, pytest

        def test_file_size_limit_is_set_and_stops_a_large_write():
            soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
            assert hard != resource.RLIM_INFINITY
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            with pytest.raises(OSError):
                with open("big.bin", "wb") as handle:
                    for _ in range(int(hard / 1048576) + 8):
                        handle.write(b"x" * 1048576)

        def test_descriptor_limit_is_set():
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            assert hard <= 1024
    """, file_bytes=8 * MB)
    assert outcomes(result) and all(v == "passed" for v in outcomes(result).values()), (outcomes(result), result.error)


def test_v6_a_hung_or_cpu_bound_test_is_killed_and_the_container_removed(workspaces):
    result, _ = run_hostile(workspaces, """
        def test_spin():
            while True:
                pass
    """, timeout=6)
    assert result.status == "timed_out" or "timed out" in (result.error or "") or result.tests == {}, result.error


# --- V8: UID mapping and the shared workspace --------------------------------------------------------------------------


def test_v8_files_the_worker_writes_can_be_read_and_deleted_by_the_api_account(workspaces):
    result, workspace = run_hostile(workspaces, """
        import os, pathlib

        def test_write_files_and_directories():
            pathlib.Path("marker.txt").write_text("from the worker")
            os.makedirs("sub/dir")
            pathlib.Path("sub/dir/data.txt").write_text("nested")
            os.chmod("sub", 0o700)       # the worker may lock its own directory against everyone else
    """)
    assert outcomes(result) and all(v == "passed" for v in outcomes(result).values()), (outcomes(result), result.error)
    marker = workspace.repo / "marker.txt"
    assert marker.read_text() == "from the worker"
    assert (workspace.repo / "sub" / "dir" / "data.txt").read_text() == "nested"
    if ROOTLESS:
        assert marker.stat().st_uid != os.getuid(), "files should belong to a mapped sub-uid, proving the mapping works"
    workspaces.cleanup(workspace)  # the API's own cleanup path must be able to delete everything
    assert not workspace.path.exists(), "the API account could not delete files the worker created (fix the ACLs)"


def test_v8_the_workspace_directory_is_private_to_the_two_accounts():
    root = os.environ.get("ATLAS_WORKSPACE_ROOT")
    if not root:
        pytest.skip("ATLAS_WORKSPACE_ROOT not set")
    mode = stat.S_IMODE(Path(root).stat().st_mode)
    assert mode & 0o007 == 0, f"{root} is accessible to other users (mode {oct(mode)})"


# --- V9: a hostile create request is harmless --------------------------------------------------------------------------


@needs_rootless
def test_v9_a_privileged_container_with_the_host_root_mounted_cannot_read_or_write_what_the_worker_account_cannot(
        tmp_path):
    """What a compromised API could ask for through the daemon. It must not reach root-owned or other accounts' data."""
    private = tmp_path / "ops-private"
    private.mkdir(mode=0o700)
    canary = private / "canary.txt"
    canary.write_text("OPERATIONS-ACCOUNT-SECRET")
    canary.chmod(0o600)
    host_path = str(canary)
    attempts = {
        "read root-only file": "cat /host/etc/shadow",
        "read another account's canary": f"cat /host{host_path}",
        "list root's home": "ls /host/root",
        "write into /etc": "touch /host/etc/atlas-escape-probe",
        "write into /usr/bin": "touch /host/usr/bin/atlas-escape-probe",
        "read the machine's private keys": "cat /host/etc/ssh/ssh_host_ed25519_key",
        "read the rootful daemon's data": "ls /host/var/lib/docker",
    }
    for name, command in attempts.items():
        done = docker("run", "--rm", "--privileged", "--network", "none", "-v", "/:/host", "--entrypoint", "sh", IMAGE,
                      "-c", command)
        assert done.returncode != 0, f"ESCAPE: '{name}' succeeded: {done.stdout[:80]!r}"
        assert "OPERATIONS-ACCOUNT-SECRET" not in done.stdout
    still_there = Path("/etc/atlas-escape-probe")
    assert not still_there.exists() and not Path("/usr/bin/atlas-escape-probe").exists()


@needs_rootless
def test_v9_cgroup_and_device_access_does_not_extend_to_the_host():
    done = docker("run", "--rm", "--privileged", "--network", "none", "--entrypoint", "sh", IMAGE, "-c",
                  "ls /dev/mem /dev/kmem /dev/sda 2>&1; echo write > /sys/kernel/atlas_probe")
    assert "No such file" in done.stdout + done.stderr or done.returncode != 0
    assert "write error" in done.stderr or done.returncode != 0


# --- V10: the filtering proxy -------------------------------------------------------------------------------------------

PROXY = os.environ.get("ATLAS_PROXY_URL")


def proxy(method: str, path: str, body: bytes | None = None) -> int:
    request = urllib.request.Request(PROXY.rstrip("/") + path, data=body, method=method,
                                     headers={"Content-Type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.mark.skipif(not PROXY, reason="set ATLAS_PROXY_URL to test the proxy")
@pytest.mark.parametrize("method,path", [
    ("GET", "/volumes"), ("GET", "/networks"), ("GET", "/info"), ("GET", "/events"), ("GET", "/system/df"),
    ("GET", "/swarm"), ("GET", "/secrets"), ("GET", "/plugins"), ("POST", "/build"), ("POST", "/commit"),
    ("POST", "/containers/anything/exec"), ("POST", "/networks/create"), ("POST", "/volumes/create"),
    ("POST", "/containers/anything/restart"),
])
def test_v10_the_proxy_refuses_every_call_atlas_does_not_use(method, path):
    assert proxy(method, path, b"{}" if method == "POST" else None) == 403, (method, path)


@pytest.mark.skipif(not PROXY, reason="set ATLAS_PROXY_URL to test the proxy")
def test_v10_the_proxy_allows_what_a_worker_run_needs():
    assert proxy("GET", "/version") == 200
    assert proxy("GET", "/containers/json?all=1") == 200
    assert proxy("GET", f"/images/{IMAGE}/json") == 200
    assert proxy("POST", "/containers/create?name=" + NAME_PREFIX + "proxycheck",
                 json.dumps({"Image": "atlas-image-that-does-not-exist"}).encode()) in (404, 400)
