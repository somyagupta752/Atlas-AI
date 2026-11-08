"""Container backend for patch verification: each pytest run in its own disposable container.

Implements the ``TestRunner`` interface, so the rest of the pipeline does not
know whether tests run locally or here. Only pytest runs in the container: it
is the step that executes repository code. Diff parsing, ``git apply``, ``ast``
syntax checks and ruff only read code, and stay on the host.

Every run is ``docker run`` with an argument list built here and nothing else:

    --init --name atlas-test-<random> --label atlas.patching=1   (removed by Atlas after inspection)
    --network none                         no network at all, not even loopback to the host
    --read-only                            the image filesystem cannot be written
    --tmpfs /tmp:rw,nosuid,nodev,size=...  scratch space, in memory, size-limited
    --mount type=bind,src=<workspace repo>,dst=/workspace   the only writable host path
    --mount type=bind,src=<meta out dir>,dst=/atlas-out     where the JUnit report is written
    --user <uid>:<gid>                     never root (the host user, or 65534 if Atlas runs as root)
    --cap-drop ALL --security-opt no-new-privileges
    --pids-limit N --memory M --memory-swap M --cpus C --ulimit nofile=... --ulimit fsize=...
    --env KEY=VALUE ...                    an explicit, fixed list: never the host environment
    <image> timeout -k 5 <seconds> python -m pytest ... --junitxml=/atlas-out/junit.xml

Timeouts are enforced twice. Inside, ``timeout`` stops pytest; the container
exits when its only child exits, and every process in it dies with it. Outside,
a host deadline (the run timeout plus a start-up allowance) stops the docker
CLI, and ``docker rm -f`` kills the container. Before removal Atlas reads the
container's ``State.OOMKilled``, so "killed by the memory limit" and "killed by
the timeout" are told apart from Docker's own record, not guessed from exit
code 137. ``docker rm -f`` always runs afterwards.

A container is an isolation boundary, not a guarantee: it shares the host
kernel. Treat running untrusted repositories as security-sensitive even here.
"""

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.patching.models import PatchLimits, TestSuiteRun
from app.patching.test_execution import PytestRunner
from app.patching.workspace import Workspace
from app.test_runner import ProcessResult, run_process

DEFAULT_IMAGE = "atlas-ai-test-runner:py3.12.13-pytest9.2.0"
CONTAINER_ROOT = "/workspace"
CONTAINER_OUT = "/atlas-out"
INNER_TIMEOUT_EXIT = 124  # coreutils timeout: the command was stopped
KILLED_EXIT = 137  # SIGKILL; State.OOMKilled tells whether the memory limit did it
DOCKER_RUN_ERROR = 125
# What the docker CLI itself needs to reach the daemon. None of it reaches the container.
CLI_ENV_ALLOWLIST = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH",
                     "DOCKER_TLS_VERIFY", "LANG")
CONTAINER_ENV = {
    "HOME": "/tmp",
    "TMPDIR": "/tmp",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONNOUSERSITE": "1",
    "PYTHONUTF8": "1",
    "NO_COLOR": "1",
}


class ContainerUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContainerLimits:
    image: str = DEFAULT_IMAGE
    cpus: float = 1.0
    memory_bytes: int = 1024**3
    pids_limit: int = 256
    tmpfs_bytes: int = 128 * 1024**2
    open_files: int = 1024
    file_bytes: int = 200 * 1024**2
    startup_allowance_seconds: float = 15.0


def _cli_env() -> dict[str, str]:
    return {key: os.environ[key] for key in CLI_ENV_ALLOWLIST if key in os.environ}


def container_user() -> str:
    uid, gid = (os.getuid(), os.getgid()) if hasattr(os, "getuid") else (65534, 65534)
    return "65534:65534" if uid == 0 else f"{uid}:{gid}"


class ContainerTestRunner(PytestRunner):
    """Same command identity and result parsing as the local runner; a different execution boundary."""

    __test__ = False
    isolated = True

    def __init__(self, limits: PatchLimits | None = None, container: ContainerLimits | None = None,
                 *, docker: str | None = None) -> None:
        super().__init__(limits)
        self.container = container or ContainerLimits()
        self.docker = docker or shutil.which("docker") or "docker"
        self._image_id: str | None = None

    # --- availability -----------------------------------------------------------------

    def available(self) -> bool:
        try:
            completed = subprocess.run(
                [self.docker, "image", "inspect", "--format", "{{.Id}}", self.container.image],
                capture_output=True, text=True, timeout=15, env=_cli_env(), stdin=subprocess.DEVNULL, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if completed.returncode != 0:
            return False
        self._image_id = completed.stdout.strip() or None
        return True

    def identity(self) -> dict[str, Any]:
        if self._image_id is None:
            self.available()
        c = self.container
        return {
            "backend": "container",
            "isolated": True,
            "image": c.image,
            "image_id": self._image_id,
            "network": "none",
            "read_only_root": True,
            "user": container_user(),
            "capabilities": "none",
            "cpus": c.cpus,
            "memory_bytes": c.memory_bytes,
            "pids_limit": c.pids_limit,
            "tmpfs_bytes": c.tmpfs_bytes,
        }

    # --- execution --------------------------------------------------------------------

    def docker_command(self, name: str, workspace: Workspace, out_dir: Path, targets: list[str],
                       timeout: float) -> list[str]:
        c = self.container
        for path in (workspace.repo, out_dir):
            if "," in str(path) or "=" in str(path):
                raise ValueError("Workspace paths must not contain ',' or '=' (docker --mount syntax)")
        env = [arg for key, value in CONTAINER_ENV.items() for arg in ("--env", f"{key}={value}")]
        inner = max(1, int(timeout))
        return [
            self.docker, "run", "--init", "--name", name, "--label", "atlas.patching=1",
            "--network", "none",
            "--read-only",
            "--tmpfs", f"/tmp:rw,nosuid,nodev,size={c.tmpfs_bytes}",
            "--mount", f"type=bind,src={workspace.repo},dst={CONTAINER_ROOT}",
            "--mount", f"type=bind,src={out_dir},dst={CONTAINER_OUT}",
            "--workdir", CONTAINER_ROOT,
            "--user", container_user(),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(c.pids_limit),
            "--memory", str(c.memory_bytes),
            "--memory-swap", str(c.memory_bytes),
            "--cpus", str(c.cpus),
            "--ulimit", f"nofile={c.open_files}:{c.open_files}",
            "--ulimit", f"fsize={c.file_bytes}:{c.file_bytes}",
            *env,
            "--entrypoint", "timeout",
            c.image,
            "-k", "5", f"{inner}s",
            "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header", "-o", "junit_family=xunit1",
            f"--junitxml={CONTAINER_OUT}/junit.xml", f"--rootdir={CONTAINER_ROOT}", *targets,
        ]

    def run(self, workspace: Workspace, targets: list[str], *, label: str, kind: str,
            timeout: float) -> TestSuiteRun:
        self.validate_targets(targets)
        self._counter += 1
        out_dir = workspace.meta / f"container-{label}-{self._counter}"
        out_dir.mkdir(mode=0o700)
        name = f"atlas-test-{uuid.uuid4().hex[:20]}"
        started = time.monotonic()
        try:
            command = self.docker_command(name, workspace, out_dir, targets, timeout)
            process = run_process(
                command,
                timeout=timeout + self.container.startup_allowance_seconds,
                cwd=workspace.meta,
                env=_cli_env(),
                max_output_bytes=self.limits.max_check_output_bytes,
            )
            oom_killed = self._oom_killed(name)
        except (OSError, ValueError) as exc:
            return self._failed(label, kind, targets, f"the container could not be started: {type(exc).__name__}",
                                time.monotonic() - started)
        finally:
            self._remove(name)

        if process.timed_out:  # the host backstop fired: the container was killed above
            return self._to_run(process, workspace, out_dir / "junit.xml", targets, label=label, kind=kind)
        if oom_killed:
            run = self._to_run(process, workspace, out_dir / "junit.xml", targets, label=label, kind=kind)
            return run.model_copy(update={"status": "error", "error": "the container hit its memory limit and was "
                                          f"killed ({self.container.memory_bytes} bytes)"})
        if process.exit_code in (INNER_TIMEOUT_EXIT, KILLED_EXIT):
            # 124: timeout stopped pytest. 137 without OOM: timeout had to SIGKILL it (SIGTERM was ignored).
            process = process.model_copy(update={"timed_out": True, "exit_code": None})
        elif process.exit_code == DOCKER_RUN_ERROR:
            return self._failed(label, kind, targets, "the container failed to start: "
                                + process.stderr.strip()[-300:], process.duration_seconds)
        process = _container_paths(process)
        return self._to_run(process, workspace, out_dir / "junit.xml", targets, label=label, kind=kind)

    def _failed(self, label, kind, targets, error, duration) -> TestSuiteRun:
        return TestSuiteRun(label=label, kind=kind, command=self.command_identity(targets), targets=targets,
                            status="error", exit_code=None, error=error[:500], duration_seconds=round(duration, 3))

    def _oom_killed(self, name: str) -> bool:
        try:
            completed = subprocess.run(
                [self.docker, "inspect", "--format", "{{.State.OOMKilled}}", name], capture_output=True, text=True,
                timeout=15, env=_cli_env(), stdin=subprocess.DEVNULL, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0 and completed.stdout.strip() == "true"

    def _remove(self, name: str) -> None:
        """Kill and remove the container if it still exists. Idempotent; never raises."""
        try:
            subprocess.run([self.docker, "rm", "-f", name], capture_output=True, timeout=30, env=_cli_env(),
                           stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError):
            pass


def _container_paths(process: ProcessResult) -> ProcessResult:
    return process.model_copy(update={
        "stdout": process.stdout.replace(CONTAINER_ROOT + "/", "./").replace(CONTAINER_ROOT, "."),
        "stderr": process.stderr.replace(CONTAINER_ROOT + "/", "./").replace(CONTAINER_ROOT, "."),
    })


def leftover_containers(docker: str | None = None) -> list[str]:
    """Names of Atlas test containers still present. Should always be empty."""
    docker = docker or shutil.which("docker") or "docker"
    completed = subprocess.run(
        [docker, "ps", "-a", "--filter", "label=atlas.patching=1", "--format", "{{json .Names}}"],
        capture_output=True, text=True, timeout=15, env=_cli_env(), stdin=subprocess.DEVNULL, check=False,
    )
    return [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
