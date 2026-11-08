"""15.6: Neo4j recovery - the graph rebuilds from source, and an offline dump restores into a fresh database."""

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from app.knowledge_graph.repository import InMemoryGraphRepository
from app.knowledge_graph.service import GraphService
from app.models import RepositoryIndexRecord
from app.ops.rebuild_graphs import rebuild_graphs

ROOT = Path(__file__).resolve().parents[2]


def test_the_graph_is_rebuilt_from_source_for_every_indexed_repository(db_session, write_repo):
    ok = write_repo({"a.py": "def f():\n    return 1\n\n\ndef g():\n    return f()\n"}, "ok")
    gone = ok.parent / "vanished"
    outside = write_repo({"b.py": "x = 1\n"}, "outside")
    suffix = uuid.uuid4().hex[:6]
    for slug, path in ((f"rb-ok-{suffix}", ok), (f"rb-gone-{suffix}", gone), (f"rb-out-{suffix}", outside)):
        db_session.add(RepositoryIndexRecord(repository_id=slug, root_path=str(path), status="ready", index_version=1))
    db_session.flush()
    repository = InMemoryGraphRepository()
    service = GraphService(repository)
    report = rebuild_graphs(db_session, service, [ok.parent / "ok", ok.parent / "vanished"],
                            only=[f"rb-ok-{suffix}", f"rb-gone-{suffix}", f"rb-out-{suffix}"])
    assert report.rebuilt == [f"rb-ok-{suffix}"]
    assert "no longer exists" in report.skipped[f"rb-gone-{suffix}"]
    assert "outside the allowed roots" in report.skipped[f"rb-out-{suffix}"]
    assert service.get_stats(f"rb-ok-{suffix}").total_nodes > 0  # the graph exists again, from source alone
    assert report.skipped.keys() == {f"rb-gone-{suffix}", f"rb-out-{suffix}"}


def test_script_refuses_missing_arguments():
    done = subprocess.run([str(ROOT / "scripts" / "neo4j_backup.sh")], capture_output=True, text=True, check=False)
    assert done.returncode == 2 and "dump" in done.stdout


@pytest.mark.container
@pytest.mark.slow
def test_offline_dump_and_restore_round_trip(tmp_path):
    """Starts two throwaway Neo4j containers; never touches the development database."""
    docker = shutil.which("docker")
    if not docker or subprocess.run([docker, "image", "inspect", "neo4j:5.15-community"], capture_output=True,
                                    check=False).returncode:
        pytest.skip("neo4j:5.15-community is not available locally")
    tag = uuid.uuid4().hex[:6]
    names = [f"atlas-neo4j-src-{tag}", f"atlas-neo4j-dst-{tag}"]
    volumes = [f"atlas_neo4j_src_{tag}", f"atlas_neo4j_dst_{tag}"]

    def run(*args, check=True, timeout=180):
        return subprocess.run(list(args), capture_output=True, text=True, check=check, timeout=timeout)

    def start(name, volume):
        run(docker, "run", "-d", "--name", name, "-v", f"{volume}:/data", "-e", "NEO4J_AUTH=neo4j/throwaway-pw-123456",
            "neo4j:5.15-community")

    def cypher(name, query):
        for _ in range(60):
            done = run(docker, "exec", name, "cypher-shell", "-u", "neo4j", "-p", "throwaway-pw-123456", "--format",
                       "plain", query, check=False)
            if done.returncode == 0:
                return done.stdout.strip()
            time.sleep(2)
        raise AssertionError(done.stderr)

    try:
        start(names[0], volumes[0])
        cypher(names[0], "CREATE (:CodeEntity {id: 'recovery-marker', name: 'f'})")
        out = tmp_path / "dumps"
        done = run(str(ROOT / "scripts" / "neo4j_backup.sh"), "dump", names[0], str(out), timeout=300)
        assert (out / "neo4j.dump").is_file() and oct((out / "neo4j.dump").stat().st_mode & 0o777) == "0o600"
        assert oct(out.stat().st_mode & 0o777) == "0o700", done.stderr
        # a new database in a new volume gets the data back
        start(names[1], volumes[1])
        cypher(names[1], "RETURN 1")
        assert "recovery-marker" not in cypher(names[1], "MATCH (n:CodeEntity) RETURN n.id")
        run(str(ROOT / "scripts" / "neo4j_backup.sh"), "restore", names[1], str(out / "neo4j.dump"), timeout=300)
        assert "recovery-marker" in cypher(names[1], "MATCH (n:CodeEntity) RETURN n.id")
    finally:
        for n in names:
            run(docker, "rm", "-f", n, check=False)
        for v in volumes:
            run(docker, "volume", "rm", "-f", v, check=False)
