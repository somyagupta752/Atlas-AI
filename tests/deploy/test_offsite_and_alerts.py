"""15.9: the off-host backup copy and the alert rules."""

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "offsite_backup.sh"
RULES = ROOT / "deploy" / "alerts" / "atlas.rules.yml"

needs_rsync = pytest.mark.skipif(not shutil.which("rsync") or not shutil.which("bash"), reason="rsync or bash missing")


def run(*args, check=False):
    return subprocess.run([str(SCRIPT), *map(str, args)], capture_output=True, text=True, check=check)


def make_backup(directory: Path, stamp: str = "20260101T030000Z", age_minutes: float = 0, body: bytes = b"encrypted") -> Path:
    dump = directory / f"atlas-pg-{stamp}.dump.enc"
    dump.write_bytes(body)
    manifest = directory / f"atlas-pg-{stamp}.manifest.json"
    manifest.write_text("{}")
    for written in (dump, manifest):
        written.chmod(0o600)  # as app/ops/backup.py writes them
    when = time.time() - age_minutes * 60
    os.utime(dump, (when, when))
    return dump


# --- freshness check ------------------------------------------------------------------------------------------------------


@needs_rsync
def test_check_passes_for_a_recent_backup_and_fails_when_stale_or_missing(tmp_path):
    src = tmp_path / "b"
    src.mkdir()
    assert run("check", src).returncode == 1  # empty directory
    make_backup(src, age_minutes=27 * 60)
    stale = run("check", src)
    assert stale.returncode == 1 and "STALE" in stale.stderr  # older than 26 hours
    make_backup(src, "20260102T030000Z", age_minutes=60)
    assert run("check", src).returncode == 0
    assert run("check", src, 30).returncode == 1  # a stricter limit in minutes
    assert run("check", tmp_path / "missing").returncode == 1


@needs_rsync
def test_work_files_and_foreign_files_do_not_count_as_a_backup(tmp_path):
    src = tmp_path / "b"
    src.mkdir()
    (src / "dump").write_bytes(b"plain")
    (src / "notes.dump.enc").write_bytes(b"x")
    assert run("check", src).returncode == 1


# --- push ----------------------------------------------------------------------------------------------------------------


@needs_rsync
def test_push_copies_backups_and_manifests_and_nothing_else(tmp_path):
    src, dest = tmp_path / "b", tmp_path / "off"
    src.mkdir()
    dest.mkdir()
    make_backup(src, "20260101T030000Z", age_minutes=120)
    make_backup(src, "20260102T030000Z", age_minutes=60)
    (src / "dump").write_bytes(b"PLAINTEXT WORK FILE")
    (src / "work-abc").mkdir()
    (src / "work-abc" / "dump").write_bytes(b"PLAINTEXT")
    done = run("push", src, dest)
    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in dest.iterdir()) == [
        "atlas-pg-20260101T030000Z.dump.enc", "atlas-pg-20260101T030000Z.manifest.json",
        "atlas-pg-20260102T030000Z.dump.enc", "atlas-pg-20260102T030000Z.manifest.json"]
    assert all(oct(p.stat().st_mode)[-3:] == "600" for p in dest.iterdir())  # private on the destination
    assert run("push", src, dest).returncode == 0  # repeating is harmless


@needs_rsync
def test_push_never_deletes_at_the_destination_or_overwrites_a_copied_backup(tmp_path):
    src, dest = tmp_path / "b", tmp_path / "off"
    src.mkdir()
    dest.mkdir()
    (dest / "atlas-pg-20250101T030000Z.dump.enc").write_bytes(b"older, already pruned at the source")
    make_backup(src, age_minutes=10, body=b"new")
    run("push", src, dest, check=True)
    assert (dest / "atlas-pg-20250101T030000Z.dump.enc").read_bytes() == b"older, already pruned at the source"
    copied = dest / "atlas-pg-20260101T030000Z.dump.enc"
    copied.write_bytes(b"tampered")  # a corrupted copy is not silently replaced by an in-place overwrite ...
    again = run("push", src, dest)
    assert again.returncode == 1 and "differ" in again.stderr  # ... and the verification step notices it


@needs_rsync
def test_push_refuses_to_run_when_the_backup_job_has_stopped(tmp_path):
    src, dest = tmp_path / "b", tmp_path / "off"
    src.mkdir()
    dest.mkdir()
    make_backup(src, age_minutes=48 * 60)
    refused = run("push", src, dest)
    assert refused.returncode == 1 and "no backup from the last 26 hours" in refused.stderr


def test_usage_errors_are_reported():
    assert run().returncode == 2
    assert run("push").returncode == 2
    assert run("explode", ROOT).returncode == 2


def test_the_script_is_shell_clean():
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck is not installed")
    assert subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, check=False).returncode == 0


# --- alert rules -----------------------------------------------------------------------------------------------------------


def rules():
    return [r for g in yaml.safe_load(RULES.read_text())["groups"] for r in g["rules"]]


def test_every_rule_is_complete_and_points_at_a_real_runbook():
    names = [r["alert"] for r in rules()]
    assert len(names) == len(set(names)) >= 8
    for rule in rules():
        assert rule["labels"]["severity"] in {"page", "ticket"}, rule["alert"]
        assert rule["annotations"]["summary"] and rule["expr"].strip(), rule["alert"]
        target, _, anchor = rule["annotations"]["runbook"].partition("#")
        page = ROOT / target
        assert page.is_file(), (rule["alert"], target)
        if anchor:  # a heading in that page must produce this anchor
            slugs = {re.sub(r"[^a-z0-9 -]", "", h.lower()).strip().replace(" ", "-")
                     for h in re.findall(r"^#+ (.+)$", page.read_text(), re.MULTILINE)}
            assert anchor in slugs, (rule["alert"], anchor)


def test_every_metric_named_in_a_rule_exists_in_the_application():
    source = (ROOT / "app" / "core" / "metrics.py").read_text()
    defined = set(re.findall(r'(?:Counter|Histogram|Gauge)\(\s*"(atlas_[a-z_]+)"', source))
    used = {m for r in rules() for m in re.findall(r"\b(atlas_[a-z_]+?)(?:_bucket|_sum|_count)?\b(?=[{\[ )]|$)", r["expr"])}
    assert used and used <= defined, used - defined


def test_label_values_used_in_rules_are_ones_the_application_produces():
    exprs = " ".join(r["expr"] for r in rules())
    assert 'status_class="5xx"' in exprs  # metrics.status_class() returns "5xx"
    assert 'status="failed"' in exprs  # RunStatus.FAILED
    assert 'outcome!="ok"' in exprs  # observability.py records "ok" for a successful model call
    assert 'outcome!="sent"' in exprs  # app/auth/mail.py records "sent" for a delivered email
