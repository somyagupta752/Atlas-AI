"""Facts about where an evaluation ran. Fixed commands only: nothing here comes from a request."""

import hashlib
import json
import platform
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def git_info() -> dict[str, Any]:
    """Commit and whether the working tree had uncommitted changes. Not cached: it can change."""
    status = _git("status", "--porcelain", "--", ".", ":(exclude)benchmarks/patching/results")
    return {"commit": _git("rev-parse", "HEAD"), "dirty": None if status is None else bool(status)}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


@lru_cache(maxsize=1)
def runtime_versions() -> dict[str, str]:
    from importlib import metadata

    versions = {"python": sys.version.split()[0], "platform": platform.platform()}
    for package in ("anthropic", "sqlalchemy", "pydantic", "pytest"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            pass
    return versions
