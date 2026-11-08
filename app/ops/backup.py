"""PostgreSQL backup, verification and restore (Task 15.6).

    python -m app.ops.backup keygen                      print a new backup key (store it in your secret manager)
    python -m app.ops.backup backup                      dump, encrypt, write a manifest, apply retention
    python -m app.ops.backup verify [FILE]               decrypt, check the hash, restore into a scratch database
    python -m app.ops.backup restore FILE --target-db DB --confirm
    python -m app.ops.backup prune                       apply retention only

Settings (environment): ``DATABASE_URL`` (never printed), ``BACKUP_DIR``, ``BACKUP_ENCRYPTION_KEY``, and optionally
``BACKUP_PG_DOCKER_CONTAINER`` to run pg_dump/pg_restore inside a running PostgreSQL container (matching the server
version) instead of using client binaries on the host.

Passwords reach the tools through ``PGPASSWORD`` in the environment, never on a command line. Backup files are created
0600 in a 0700 directory, and the directory is refused if anyone else can read it.
"""

import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.ops.crypto import (
    BackupCryptoError,
    decrypt_file,
    encrypt_file,
    generate_key,
    parse_key,
)
from app.ops.retention import RetentionPolicy, plan_backup_retention

TOOL_VERSION = "1"
TIMEOUT = 3600


class BackupError(Exception):
    """A backup operation failed. The message never contains a password or key."""


@dataclass(frozen=True)
class Database:
    user: str
    password: str
    host: str
    port: int
    name: str

    @classmethod
    def from_url(cls, url: str) -> "Database":
        parts = urlsplit(url.replace("postgresql+psycopg://", "postgresql://"))
        if not parts.username or not parts.path.strip("/"):
            raise BackupError("DATABASE_URL needs a user and a database name")
        return cls(unquote(parts.username), unquote(parts.password or ""), parts.hostname or "localhost",
                   parts.port or 5432, parts.path.strip("/"))


@dataclass
class BackupConfig:
    directory: Path
    key: bytes
    database: Database
    docker_container: str | None = None
    policy: RetentionPolicy = field(default_factory=RetentionPolicy)

    @classmethod
    def from_environment(cls) -> "BackupConfig":
        from app.core.config import get_settings

        raw_key = os.environ.get("BACKUP_ENCRYPTION_KEY")
        if not raw_key:
            # A container gets the key as a mounted secret file, not as an environment variable.
            from app.core.config import SECRETS_DIR

            secret_file = SECRETS_DIR / "backup_encryption_key"
            raw_key = secret_file.read_text().strip() if secret_file.is_file() else None
        if not raw_key:
            raise BackupError("BACKUP_ENCRYPTION_KEY (or the backup_encryption_key secret file) is required")
        directory = os.environ.get("BACKUP_DIR")
        if not directory:
            raise BackupError("BACKUP_DIR is required")
        return cls(Path(directory), parse_key(raw_key), Database.from_url(get_settings().database_url.get_secret_value()),
                   os.environ.get("BACKUP_PG_DOCKER_CONTAINER") or None)


def prepare_directory(directory: Path) -> Path:
    """Create the backup directory private, and refuse one that other users can read or write."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = stat.S_IMODE(directory.stat().st_mode)
    if mode & 0o077:
        raise BackupError(f"{directory} is accessible to other users (mode {oct(mode)}); run chmod 700")
    if directory.is_symlink():
        raise BackupError("the backup directory must not be a symlink")
    return directory


# --- running PostgreSQL tools ------------------------------------------------------------------


def _argv(config: BackupConfig, tool: list[str], *, stdin: bool = False) -> list[str]:
    db = config.database
    if config.docker_container:
        # `-e PGPASSWORD` with no value copies it from this process's environment, so it is not in the arguments.
        return ["docker", "exec", *(["-i"] if stdin else []), "-e", "PGPASSWORD", config.docker_container, *tool]
    return [*tool, "-h", db.host, "-p", str(db.port)]


def _env(config: BackupConfig) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "DOCKER_HOST", "DOCKER_CONFIG")}
    env["PGPASSWORD"] = config.database.password
    return env


def _run(argv: list[str], config: BackupConfig, *, stdout=None, stdin=None, capture: bool = True
         ) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, env=_env(config), stdout=stdout if stdout else (subprocess.PIPE if capture else None),
                              stderr=subprocess.PIPE, stdin=stdin, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupError(f"could not run {argv[0]}: {type(exc).__name__}") from None


def _fail(step: str, done: subprocess.CompletedProcess) -> BackupError:
    detail = (done.stderr or b"").decode(errors="replace").strip().splitlines()[-1:] or ["no error output"]
    return BackupError(f"{step} failed (exit {done.returncode}): {detail[0][:200]}")


def sql(config: BackupConfig, statement: str, database: str | None = None) -> str:
    db = config.database
    argv = _argv(config, ["psql", "-U", db.user, "-d", database or db.name, "-tA", "-v", "ON_ERROR_STOP=1",
                          "-c", statement])
    if config.docker_container:
        argv = [a for a in argv if a not in ("-h", db.host, "-p", str(db.port))]
    done = _run(argv, config)
    if done.returncode != 0:
        raise _fail("psql", done)
    return done.stdout.decode().strip()


def table_counts(config: BackupConfig, database: str | None = None) -> dict[str, int]:
    names = sql(config, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1", database).split()
    counts = {}
    for name in names:
        quoted = name.replace('"', '""')  # the name comes from pg_tables; quote it like quote_ident anyway
        counts[name] = int(sql(config, f'SELECT count(*) FROM "{quoted}"', database))  # noqa: S608
    return counts


# --- operations --------------------------------------------------------------------------------


def backup(config: BackupConfig, *, now: datetime | None = None) -> Path:
    now = now or datetime.now(UTC)
    prepare_directory(config.directory)
    work = Path(tempfile.mkdtemp(prefix="work-", dir=config.directory))
    os.chmod(work, 0o700)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    final = config.directory / f"atlas-pg-{stamp}.dump.enc"
    try:
        before = table_counts(config)
        plain = work / "dump"
        db = config.database
        argv = _argv(config, ["pg_dump", "-U", db.user, "-d", db.name, "-Fc", "--no-owner"])
        if config.docker_container:
            argv = [a for a in argv if a not in ("-h", db.host, "-p", str(db.port))]
        with plain.open("wb") as handle:
            os.chmod(plain, 0o600)
            done = _run(argv, config, stdout=handle)
        if done.returncode != 0 or plain.stat().st_size == 0:
            raise _fail("pg_dump", done)
        after = table_counts(config)
        digest = hashlib.sha256(plain.read_bytes()).hexdigest()
        encrypt_file(plain, final, config.key)
        manifest = {
            "file": final.name, "created_at": now.isoformat(), "plaintext_sha256": digest,
            "plaintext_bytes": plain.stat().st_size, "encrypted_bytes": final.stat().st_size,
            "alembic_revision": sql(config, "SELECT version_num FROM alembic_version"),
            "postgres_version": sql(config, "SHOW server_version"), "database": db.name,
            "tables": after, "counts_consistent": before == after, "tool_version": TOOL_VERSION,
        }
        manifest_path = final.with_suffix(".manifest.json")
        fd = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(manifest, out, indent=2)
    except BaseException:
        final.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    prune(config, now=now)
    return final


def prune(config: BackupConfig, *, now: datetime | None = None) -> list[str]:
    names = [p.name for p in config.directory.iterdir() if p.is_file()]
    _keep, delete = plan_backup_retention(names, now or datetime.now(UTC), config.policy)
    for name in delete:
        (config.directory / name).unlink(missing_ok=True)
        (config.directory / name).with_suffix(".manifest.json").unlink(missing_ok=True)
    return delete


def latest_backup(directory: Path) -> Path:
    files = sorted(p for p in directory.iterdir() if p.name.endswith(".dump.enc"))
    if not files:
        raise BackupError("no backup found")
    return files[-1]


def _decrypt(config: BackupConfig, source: Path, work: Path) -> Path:
    plain = work / "dump"
    try:
        decrypt_file(source, plain, config.key)
    except BackupCryptoError as exc:
        raise BackupError(str(exc)) from None
    return plain


def _manifest(source: Path) -> dict:
    path = source.with_suffix(".manifest.json")
    if not path.is_file():
        raise BackupError("the manifest next to the backup is missing")
    return json.loads(path.read_text())


def restore(config: BackupConfig, source: Path, target_db: str, *, create: bool = True) -> None:
    """Restore ``source`` into ``target_db``. Never into the live database unless it is named explicitly."""
    prepare_directory(config.directory)
    work = Path(tempfile.mkdtemp(prefix="restore-", dir=config.directory))
    os.chmod(work, 0o700)
    try:
        plain = _decrypt(config, source, work)
        manifest = _manifest(source)
        if hashlib.sha256(plain.read_bytes()).hexdigest() != manifest["plaintext_sha256"]:
            raise BackupError("the decrypted backup does not match its recorded hash")
        db = config.database
        if create:
            sql(config, f'CREATE DATABASE "{target_db}"', "postgres")
        argv = _argv(config, ["pg_restore", "-U", db.user, "-d", target_db, "--no-owner", "--exit-on-error"],
                     stdin=True)
        if config.docker_container:
            argv = [a for a in argv if a not in ("-h", db.host, "-p", str(db.port))]
        with plain.open("rb") as handle:
            done = _run(argv, config, stdin=handle)
        if done.returncode != 0:
            raise _fail("pg_restore", done)
    finally:
        shutil.rmtree(work, ignore_errors=True)


@dataclass
class VerifyReport:
    file: str
    hash_ok: bool
    alembic_revision: str
    tables_missing: list[str]
    count_mismatches: dict[str, tuple[int, int]]
    counts_checked: bool

    @property
    def ok(self) -> bool:
        return self.hash_ok and not self.tables_missing and not self.count_mismatches


def verify(config: BackupConfig, source: Path | None = None) -> VerifyReport:
    """Restore into a throwaway database and compare it with the manifest. The scratch database is always dropped."""
    source = source or latest_backup(config.directory)
    manifest = _manifest(source)
    scratch = f"atlas_verify_{secrets.token_hex(4)}"
    try:
        restore(config, source, scratch)
        restored = table_counts(config, scratch)
        revision = sql(config, "SELECT version_num FROM alembic_version", scratch)
    finally:
        try:
            sql(config, f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)', "postgres")
        except BackupError:
            pass
    expected = manifest["tables"]
    missing = sorted(set(expected) - set(restored))
    mismatches = {t: (expected[t], restored[t]) for t in expected if t in restored and expected[t] != restored[t]}
    checked = bool(manifest.get("counts_consistent"))
    return VerifyReport(source.name, True, revision, missing, mismatches if checked else {}, checked)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    command, rest = argv[0], argv[1:]
    try:
        if command == "keygen":
            print(generate_key())
            return 0
        config = BackupConfig.from_environment()
        if command == "backup":
            print(backup(config))
        elif command == "prune":
            print("\n".join(prune(config)) or "nothing to prune")
        elif command == "verify":
            report = verify(config, Path(rest[0]) if rest else None)
            print(json.dumps({"file": report.file, "ok": report.ok, "alembic_revision": report.alembic_revision,
                              "tables_missing": report.tables_missing,
                              "count_mismatches": report.count_mismatches, "counts_checked": report.counts_checked}))
            return 0 if report.ok else 1
        elif command == "restore":
            if len(rest) < 3 or "--target-db" not in rest or "--confirm" not in rest:
                print("restore FILE --target-db NAME --confirm")
                return 2
            target = rest[rest.index("--target-db") + 1]
            restore(config, Path(rest[0]), target, create="--existing" not in rest)
            print(f"restored into {target}")
        else:
            print(__doc__)
            return 2
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
