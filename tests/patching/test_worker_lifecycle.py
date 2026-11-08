"""15.4: worker lifecycle - orphan cleanup that only touches Atlas's own containers, cancellation, concurrency."""

import shutil
import subprocess
import threading
import time
import uuid

import pytest

from app.patching.container import (
    NAME_PREFIX,
    ContainerLimits,
    ContainerTestRunner,
    cleanup_orphans,
    leftover_containers,
)
from app.patching.models import PatchLimits
from app.patching.workspace import WorkspaceManager

pytestmark = pytest.mark.container
IMAGE = ContainerLimits().image
DOCKER = shutil.which("docker")


@pytest.fixture(scope="module")
def runner():
    r = ContainerTestRunner(PatchLimits(check_timeout_seconds=60), ContainerLimits())
    if not r.available():
        pytest.skip("container image not built")
    return r


def start_sleeper(name: str, labels: dict[str, str]) -> None:
    args = [DOCKER, "run", "-d", "--init", "--name", name, "--network", "none"]
    for k, v in labels.items():
        args += ["--label", f"{k}={v}"]
    args += ["--entrypoint", "sleep", IMAGE, "300"]
    subprocess.run(args, check=True, capture_output=True)


def exists(name: str) -> bool:
    out = subprocess.run([DOCKER, "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
                         check=False, capture_output=True, text=True).stdout
    return name in out.split()


def remove(*names):
    for n in names:
        subprocess.run([DOCKER, "rm", "-f", n], check=False, capture_output=True)


def test_cleanup_removes_only_this_deployments_atlas_workers(runner):
    tag = uuid.uuid4().hex[:8]
    mine = f"{NAME_PREFIX}orphan-{tag}"
    other_deploy = f"{NAME_PREFIX}other-{tag}"
    unrelated = f"unrelated-{tag}"
    wrong_name = f"notatlas-{tag}"
    try:
        start_sleeper(mine, {"atlas.patching": "1", "atlas.instance": f"t{tag}"})
        start_sleeper(other_deploy, {"atlas.patching": "1", "atlas.instance": f"other{tag}"})
        start_sleeper(unrelated, {})
        start_sleeper(wrong_name, {"atlas.patching": "1", "atlas.instance": f"t{tag}"})  # labelled, wrong name
        report = cleanup_orphans(f"t{tag}")
        assert report["removed"] == [mine] and report["failed"] == []
        assert not exists(mine)
        assert exists(other_deploy) and exists(unrelated) and exists(wrong_name)
    finally:
        remove(mine, other_deploy, unrelated, wrong_name)


def test_cleanup_keeps_containers_it_is_told_to_keep(runner):
    name = f"{NAME_PREFIX}keep-{uuid.uuid4().hex[:8]}"
    try:
        start_sleeper(name, {"atlas.patching": "1", "atlas.instance": "keeptest"})
        assert cleanup_orphans("keeptest", keep=frozenset({name}))["removed"] == []
        assert exists(name)
    finally:
        remove(name)


def test_cleanup_survives_docker_being_unavailable():
    report = cleanup_orphans("x", docker="/nonexistent/docker")
    assert report == {"removed": [], "failed": ["docker is unavailable"]}


def test_cancellation_kills_a_running_worker_and_leaves_nothing(runner, tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "test_slow.py").write_text("import time\n\ndef test_slow():\n    time.sleep(120)\n")
    manager = WorkspaceManager(tmp_path / "ws")
    workspace = manager.create(source, execution_id="cancel", label="cancel")
    result = {}

    def work():
        result["run"] = runner.run(workspace, ["test_slow.py"], label="slow", kind="targeted", timeout=100)

    thread = threading.Thread(target=work)
    started = time.monotonic()
    thread.start()
    names: list[str] = []
    for _ in range(100):
        time.sleep(0.2)
        names = runner.kill_active()
        if names:
            break
    thread.join(30)
    manager.cleanup(workspace)
    assert names and all(n.startswith(NAME_PREFIX) for n in names)
    assert not thread.is_alive() and time.monotonic() - started < 60  # not the 100 s of the test
    assert not any(n in leftover_containers() for n in names)


def test_concurrent_workers_have_separate_names_and_files(runner, tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "test_w.py").write_text(
        "import pathlib\n\ndef test_write():\n    p = pathlib.Path('marker.txt')\n"
        "    assert not p.exists()\n    p.write_text('x')\n")
    manager = WorkspaceManager(tmp_path / "ws")
    workspaces = [manager.create(source, execution_id=f"c{i}", label="c") for i in range(3)]
    results: dict[int, object] = {}

    def work(i):
        results[i] = runner.run(workspaces[i], ["test_w.py"], label=f"c{i}", kind="targeted", timeout=60)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(3)]
    [t.start() for t in threads]
    [t.join(120) for t in threads]
    try:
        for i in range(3):
            tests = results[i].tests
            assert tests and all(t.outcome.value == "passed" for t in tests.values()), (i, results[i].error)
            assert (workspaces[i].repo / "marker.txt").exists()
        assert not (source / "marker.txt").exists()  # the source was never written
        assert len({w.repo for w in workspaces}) == 3
    finally:
        for w in workspaces:
            manager.cleanup(w)
    assert leftover_containers() == []


def test_a_worker_that_cannot_start_is_cleaned_up_and_reported(tmp_path):
    bad = ContainerTestRunner(PatchLimits(), ContainerLimits(image="atlas-image-that-does-not-exist:0"))
    source = tmp_path / "src"
    source.mkdir()
    (source / "test_a.py").write_text("def test_a():\n    pass\n")
    manager = WorkspaceManager(tmp_path / "ws")
    workspace = manager.create(source, execution_id="bad", label="bad")
    try:
        run = bad.run(workspace, ["test_a.py"], label="bad", kind="targeted", timeout=20)
    finally:
        manager.cleanup(workspace)
    assert run.status == "error" and run.error
    assert leftover_containers() == []


def test_startup_purges_abandoned_workspaces(tmp_path):
    base = tmp_path / "ws"
    manager = WorkspaceManager(base)
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.py").write_text("x = 1\n")
    abandoned = manager.create(source, execution_id="old", label="old")
    fresh_process = WorkspaceManager(base)  # a restarted process has no active workspaces
    assert fresh_process.purge_all_inactive() == 1
    assert not abandoned.path.exists()
    assert fresh_process.purge_all_inactive() == 0
