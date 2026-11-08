"""Start the REAL Atlas API against a throwaway database for the browser tests, and remove the database afterwards.

Playwright runs this as a web server. It:

1. creates a database named ``atlas_e2e_<random>`` on the PostgreSQL server the project's DATABASE_URL points to (the
   same credentials, never the same database), after dropping stale ``atlas_e2e_*`` databases that nobody is connected to;
2. migrates it with Alembic;
3. starts uvicorn on E2E_API_PORT with a configuration made for the tests: test environment, a known signing key, plain-http
   cookies, a small rate limit, no model provider, no graph store;
4. on SIGTERM / SIGINT stops the API and DROPS the database.

Safety: every statement that creates or drops a database goes through ``checked_name``, which refuses any name that does
not start with ``atlas_e2e_``. The development database cannot be touched by this script.
"""

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psycopg
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[3]
STATE_DIR = ROOT / "frontend" / "e2e" / ".state"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "api.log"
PORT = int(os.environ.get("E2E_API_PORT", "8765"))
PREFIX = "atlas_e2e_"


def checked_name(name: str) -> str:
    if not name.startswith(PREFIX) or not name.replace("_", "").isalnum():
        raise SystemExit(f"refusing to touch database {name!r}: not an E2E database")
    return name


def base_url() -> str:
    sys.path.insert(0, str(ROOT))
    from app.core.config import get_settings

    return get_settings().database_url.get_secret_value()


def psycopg_dsn(sqlalchemy_url: str, database: str) -> str:
    return make_url(sqlalchemy_url).set(drivername="postgresql", database=database).render_as_string(hide_password=False)


def sweep_stale(admin_dsn: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT d.datname FROM pg_database d WHERE d.datname LIKE 'atlas\\_e2e\\_%' AND NOT EXISTS "
            "(SELECT 1 FROM pg_stat_activity a WHERE a.datname = d.datname)").fetchall()
        for (stale,) in rows:
            conn.execute(f'DROP DATABASE IF EXISTS "{checked_name(stale)}"')


def main() -> int:
    source = base_url()
    admin_dsn = psycopg_dsn(source, "postgres")
    sweep_stale(admin_dsn)
    name = checked_name(PREFIX + secrets.token_hex(4))
    # A fresh signing key for every run, shared with the tests through the state file. No key is written in the repository.
    auth_secret = secrets.token_urlsafe(48)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    sqlalchemy_url = make_url(source).set(database=name).render_as_string(hide_password=False)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    fixture = Path(tempfile.mkdtemp(prefix="atlas-e2e-repos-"))
    repo = fixture / "demo"
    repo.mkdir()
    (repo / "calculator.py").write_text(
        "def average(numbers):\n    return sum(numbers) / len(numbers)\n\n\ndef total(numbers):\n    return sum(numbers)\n")
    (repo / "test_calculator.py").write_text("from calculator import average\n\n\ndef test_average():\n    assert average([2, 4]) == 3\n")

    env = {
        **os.environ,
        "ENVIRONMENT": "test",
        "DATABASE_URL": sqlalchemy_url,
        "AUTH_SECRET_KEY": auth_secret,
        "AUTH_COOKIE_SECURE": "false",           # the tests use plain http on localhost
        "AUTH_RATE_LIMIT_ATTEMPTS": "5",
        "AUTH_RATE_LIMIT_WINDOW_SECONDS": "300",
        "ALLOWED_REPOSITORY_ROOTS": json.dumps([str(fixture)]),
        "NEO4J_URI": "",                         # no graph store: these tests never touch the development Neo4j
        "SMTP_HOST": "127.0.0.1",                # reset email goes to the local capture service, never to a real server
        "SMTP_PORT": os.environ.get("E2E_SMTP_PORT", "2525"),
        "SMTP_SECURITY": "none",
        "SMTP_FROM": "atlas@e2e.example.test",
        "PUBLIC_BASE_URL": f"http://127.0.0.1:{os.environ.get('E2E_WEB_PORT', '4173')}",
        "LLM_PROVIDER": "disabled",              # and no model provider
        "LLM_ALLOW_EXTERNAL": "false",
        "EMBEDDING_PROVIDER": "hashing",
        "CORS_ALLOWED_ORIGINS": "[]",
        "BENCHMARKS_ENDPOINT_ENABLED": "false",
    }
    env.pop("ANTHROPIC_API_KEY", None)
    state = {"database": name, "database_url": sqlalchemy_url, "pg_dsn": psycopg_dsn(sqlalchemy_url, name),
             "fixture_root": str(fixture), "fixture_repo": str(repo), "auth_secret": auth_secret, "log": str(LOG_FILE),
             "api_port": PORT}
    LOG_FILE.write_text("")
    os.chmod(LOG_FILE, 0o600)
    fd = os.open(STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(state, handle)

    child: subprocess.Popen | None = None

    def note(message: str) -> None:
        with (STATE_DIR / "teardown.log").open("a") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')} {message}\n")

    def teardown() -> None:
        note("teardown started")
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
        try:
            with psycopg.connect(admin_dsn, autocommit=True) as conn:
                conn.execute(f'DROP DATABASE IF EXISTS "{checked_name(name)}" WITH (FORCE)')
            note(f"dropped {name}")
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
            STATE_FILE.unlink(missing_ok=True)

    def on_signal(number, _frame) -> None:
        note(f"received signal {signal.Signals(number).name}")
        raise SystemExit(0)  # unwinds into the `finally` below, which tears everything down exactly once

    for caught in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(caught, on_signal)
    note(f"started pid={os.getpid()} ppid={os.getppid()} pgid={os.getpgrp()}")
    try:
        migrate = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env,
                                 capture_output=True, text=True, check=False)
        if migrate.returncode != 0:
            print(migrate.stderr[-2000:], file=sys.stderr)
            return 1
        with LOG_FILE.open("ab") as log:
            child = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT),
                 "--no-server-header"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            parent = os.getppid()
            while child.poll() is None:
                # If whatever started us is gone (killed without a catchable signal), nobody will ever send us SIGTERM:
                # stop on our own, so the API, its port and its database do not outlive the test run.
                if os.getppid() != parent:
                    note("parent process vanished; shutting down")
                    break
                time.sleep(0.5)
    finally:
        teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
