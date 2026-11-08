"""15.6: encryption, retention policy, backup privacy and a real backup -> restore -> verify cycle."""

import json
import os
import shutil
import stat
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from app.ops import backup as bk
from app.ops.crypto import (
    MAGIC,
    BackupCryptoError,
    decrypt_file,
    encrypt_file,
    generate_key,
    parse_key,
)
from app.ops.retention import RetentionPolicy, backup_time, plan_backup_retention

KEY = parse_key(generate_key())


# --- encryption ---------------------------------------------------------------------------------


@pytest.mark.parametrize("size", [0, 1, 1024, 1024 * 1024, 1024 * 1024 + 1, 3 * 1024 * 1024 + 17])
def test_encrypt_decrypt_round_trip(tmp_path, size):
    source = tmp_path / "plain"
    source.write_bytes(os.urandom(size))
    sealed, back = tmp_path / "sealed", tmp_path / "back"
    encrypt_file(source, sealed, KEY)
    decrypt_file(sealed, back, KEY)
    assert back.read_bytes() == source.read_bytes()
    assert sealed.read_bytes().startswith(MAGIC) and stat.S_IMODE(sealed.stat().st_mode) == 0o600
    if size >= 64:  # a 1-byte plaintext can match ciphertext by chance; a 64-byte prefix cannot
        assert source.read_bytes()[:64] not in sealed.read_bytes()


def test_wrong_key_tampering_and_truncation_are_detected(tmp_path):
    source = tmp_path / "plain"
    source.write_bytes(os.urandom(3 * 1024 * 1024))
    sealed = tmp_path / "sealed"
    encrypt_file(source, sealed, KEY)
    data = sealed.read_bytes()

    def attempt(blob, key=KEY):
        target = tmp_path / "blob"
        target.write_bytes(blob)
        with pytest.raises(BackupCryptoError):
            decrypt_file(target, tmp_path / "out", key)

    attempt(data, parse_key(generate_key()))  # wrong key
    flipped = bytearray(data)
    flipped[len(data) // 2] ^= 1
    attempt(bytes(flipped))  # one flipped bit
    attempt(data[: len(data) - 100])  # truncated tail
    first_chunk_end = 16 + 4 + int.from_bytes(data[16:20], "big")
    attempt(data[:first_chunk_end])  # whole chunks dropped: the last-chunk marker no longer matches
    attempt(b"not an atlas backup file at all")


def test_keys_must_be_32_bytes():
    with pytest.raises(BackupCryptoError):
        parse_key("c2hvcnQ")
    assert len(parse_key(generate_key())) == 32


# --- retention -----------------------------------------------------------------------------------


def name(day: datetime) -> str:
    return f"atlas-pg-{day.strftime('%Y%m%dT%H%M%SZ')}.dump.enc"


def test_seven_daily_and_four_weekly_backups_are_kept():
    start = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)  # a Tuesday
    names = [name(start + timedelta(days=i)) for i in range(70)]
    now = start + timedelta(days=69)
    keep, delete = plan_backup_retention(names, now)
    kept_days = {backup_time(n).date() for n in keep}
    newest = [start.date() + timedelta(days=i) for i in range(69, 62, -1)]
    assert set(newest) <= kept_days  # the last 7 days
    sundays = sorted((d for d in (start.date() + timedelta(days=i) for i in range(70)) if d.weekday() == 6),
                     reverse=True)[:4]
    assert set(sundays) <= kept_days  # the last 4 Sundays
    assert len(keep) <= 7 + 4 and set(keep).isdisjoint(delete) and set(keep) | set(delete) == set(names)


def test_one_backup_per_day_the_latest():
    day = datetime(2026, 9, 10, tzinfo=UTC)
    early, late = name(day.replace(hour=1)), name(day.replace(hour=23))
    older = name(day.replace(day=9, hour=5))
    keep, delete = plan_backup_retention([early, late, older], day, RetentionPolicy(daily=2, weekly=0))
    assert late in keep and older in keep and delete == [early]


def test_foreign_files_and_the_newest_backup_are_never_deleted():
    names = ["notes.txt", "atlas-pg-garbage.dump.enc", "../../etc/passwd", name(datetime(2020, 1, 1, tzinfo=UTC))]
    keep, delete = plan_backup_retention(names, datetime(2026, 1, 1, tzinfo=UTC), RetentionPolicy(daily=0, weekly=0))
    assert delete == [] and keep == [name(datetime(2020, 1, 1, tzinfo=UTC))]
    assert plan_backup_retention([], datetime.now(UTC)) == ([], [])


def test_a_stopped_backup_job_does_not_lose_its_last_backups():
    old = [name(datetime(2025, 1, 1 + i, tzinfo=UTC)) for i in range(5)]
    keep, delete = plan_backup_retention(old, datetime(2026, 11, 1, tzinfo=UTC))
    assert set(keep) == set(old) and delete == []  # retention counts backups, not the age of the clock


# --- privacy ------------------------------------------------------------------------------------


def test_backup_directory_is_private_and_a_readable_one_is_refused(tmp_path):
    private = bk.prepare_directory(tmp_path / "backups")
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    with pytest.raises(bk.BackupError, match="accessible to other users"):
        bk.prepare_directory(shared)


def test_database_url_parsing_never_exposes_the_password_in_errors():
    db = bk.Database.from_url("postgresql+psycopg://atlas:p%40ss@db.internal:5433/atlas")
    assert (db.user, db.password, db.host, db.port, db.name) == ("atlas", "p@ss", "db.internal", 5433, "atlas")
    with pytest.raises(bk.BackupError) as caught:
        bk.Database.from_url("postgresql://:secretpw@host/")
    assert "secretpw" not in str(caught.value)


# --- a real cycle against PostgreSQL -------------------------------------------------------------

CONTAINER = "atlas-postgres"


def docker_postgres_up() -> bool:
    if not shutil.which("docker"):
        return False
    done = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER], capture_output=True,
                          text=True, check=False)
    return done.stdout.strip() == "true"


@pytest.fixture
def real_config(tmp_path):
    if not docker_postgres_up():
        pytest.skip("the atlas-postgres container is not running")
    from app.core.config import get_settings

    return bk.BackupConfig(tmp_path / "backups", KEY,
                           bk.Database.from_url(get_settings().database_url.get_secret_value()),
                           docker_container=CONTAINER)


def databases(config):
    return bk.sql(config, "SELECT datname FROM pg_database", "postgres").split()


def test_backup_restore_and_verify_a_real_database(real_config):
    config = real_config
    path = bk.backup(config)
    manifest = json.loads(path.with_suffix(".manifest.json").read_text())
    assert path.name.endswith(".dump.enc") and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert manifest["alembic_revision"] == bk.sql(config, "SELECT version_num FROM alembic_version")
    assert {"users", "evaluation_runs", "code_chunks", "alembic_version"} <= set(manifest["tables"])
    assert manifest["counts_consistent"] is True and manifest["tables"]["evaluation_runs"] >= 1
    # the encrypted file holds none of the data in readable form
    raw = path.read_bytes()
    assert b"PGDMP" not in raw and b"evaluation_runs" not in raw and config.database.password.encode() not in raw

    report = bk.verify(config, path)
    assert report.ok and report.counts_checked and report.alembic_revision == manifest["alembic_revision"]
    assert not [d for d in databases(config) if d.startswith("atlas_verify_")]  # the scratch database is gone
    assert not list(config.directory.glob("work-*")) and not list(config.directory.glob("restore-*"))


def test_a_restored_database_matches_the_original_row_for_row(real_config):
    config = real_config
    path = bk.backup(config)
    target = "atlas_restore_check"
    try:
        bk.restore(config, path, target)
        original, restored = bk.table_counts(config), bk.table_counts(config, target)
        manifest = json.loads(path.with_suffix(".manifest.json").read_text())
        assert restored == manifest["tables"] and set(original) == set(restored)
        assert bk.sql(config, "SELECT count(*) FROM evaluation_case_results", target) == \
            bk.sql(config, "SELECT count(*) FROM evaluation_case_results")
        # the restored schema is usable: the immutability triggers came back with it
        triggers = bk.sql(config, "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'evaluation_%' AND NOT tgisinternal",
                          target)
        assert int(triggers) >= 7
    finally:
        bk.sql(config, f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)', "postgres")


def test_a_corrupted_backup_fails_verification_and_leaves_nothing_behind(real_config):
    config = real_config
    path = bk.backup(config)
    blob = bytearray(path.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    path.write_bytes(bytes(blob))
    with pytest.raises(bk.BackupError, match="corrupt"):
        bk.verify(config, path)
    assert not [d for d in databases(config) if d.startswith("atlas_verify_")]


def test_the_wrong_key_cannot_restore(real_config):
    path = bk.backup(real_config)
    other = bk.BackupConfig(real_config.directory, parse_key(generate_key()), real_config.database,
                            real_config.docker_container)
    with pytest.raises(bk.BackupError):
        bk.restore(other, path, "atlas_wrong_key")


def test_prune_removes_old_backups_and_their_manifests(real_config):
    config = real_config
    bk.prepare_directory(config.directory)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(12):
        stamp = name(base + timedelta(days=i))
        (config.directory / stamp).write_bytes(b"x")
        (config.directory / stamp).with_suffix(".manifest.json").write_text("{}")
    (config.directory / "keep-me.txt").write_text("not a backup")
    deleted = bk.prune(config, now=base + timedelta(days=12))
    # 12 daily backups: the newest 7 stay, plus Sunday 4 January as the weekly one; 4 are deleted
    assert len(deleted) == 4 and (config.directory / "atlas-pg-20260104T000000Z.dump.enc").exists()
    assert (config.directory / "keep-me.txt").exists()
    assert all(not (config.directory / d).exists() for d in deleted)
    assert all(not (config.directory / d).with_suffix(".manifest.json").exists() for d in deleted)
