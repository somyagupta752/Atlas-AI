"""Application-controlled test and lint execution inside a workspace.

Commands are built here and nowhere else. Neither the model nor the client
chooses an executable, an option or a path that is not a validated test file:

    <python> -m pytest -q -p no:cacheprovider --no-header -o junit_family=xunit1
        --junitxml=<meta>/junit-<n>.xml --rootdir=. [<validated test files>...]
    <python> -m ruff check --no-cache --output-format=json --exit-zero <patched files>

Individual results come from pytest's JUnit XML (xunit1 records each test's
file), not from scraping output. Baseline and patched runs use the same
command identity and the same environment builder; only the workspace differs.

The child environment contains no credentials: an allowlist of locale and PATH
variables, with HOME and TMPDIR pointing into the workspace's meta directory.
"""

import importlib.util
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, Field

from app.patching.models import PatchLimits, TestCaseResult, TestOutcome, TestSuiteRun
from app.patching.workspace import Workspace
from app.test_runner import ProcessResult, ResourceLimits, run_process
from app.tools.base import redact_secrets

ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "SYSTEMROOT")
MAX_JUNIT_BYTES = 20 * 1024**2
TEST_TARGET = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*\.py$")
SEVERITY = {TestOutcome.PASSED: 0, TestOutcome.SKIPPED: 1, TestOutcome.FAILED: 2, TestOutcome.ERROR: 3}


def child_environment(workspace: Workspace) -> dict[str, str]:
    env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    env.update(
        {
            "HOME": str(workspace.meta / "home"),
            "TMPDIR": str(workspace.meta / "tmp"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
            "NO_COLOR": "1",
        }
    )
    return env


def environment_identity() -> dict[str, Any]:
    """What a reproduction needs: interpreter and tool versions, and the variables set."""
    def version(module: str) -> str | None:
        try:
            from importlib.metadata import version as dist_version

            return dist_version(module)
        except Exception:  # noqa: BLE001 - absent tools are recorded as None
            return None

    return {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "pytest": version("pytest"),
        "ruff": version("ruff"),
        "environment_variables": sorted([*ENV_ALLOWLIST, "HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED",
                                         "PYTHONNOUSERSITE", "PYTHONUTF8", "NO_COLOR"]),
    }


def _clean(text: str, workspace: Workspace) -> str:
    return redact_secrets(text.replace(str(workspace.repo), ".").replace(str(workspace.meta), "<meta>"))


def module_prefix(file_path: str) -> str:
    return PurePosixPath(file_path).with_suffix("").as_posix().replace("/", ".")


def test_id(classname: str, name: str, file: str | None) -> str:
    """Stable ID: ``path/test_x.py::Class::test_name``. Independent of the workspace path."""
    if file:
        file = PurePosixPath(file.replace("\\", "/")).as_posix().removeprefix("./")
        prefix = module_prefix(file)
        rest = classname[len(prefix) :].lstrip(".") if classname.startswith(prefix) else ""
        return "::".join([file, *(rest.split(".") if rest else []), name])
    if not classname:
        return f"<collection>::{name}"
    return f"{classname}::{name}"


def parse_junit(path: Path, workspace: Workspace) -> dict[str, TestCaseResult]:
    """Per-test outcomes from a JUnit XML report. Raises ``ValueError`` if unreadable."""
    if not path.is_file():
        raise ValueError("pytest did not write a JUnit report")
    if path.stat().st_size > MAX_JUNIT_BYTES:
        raise ValueError("The JUnit report is too large")
    root = ET.fromstring(path.read_bytes())
    results: dict[str, TestCaseResult] = {}
    for case in root.iter("testcase"):
        name = case.get("name", "")
        identifier = test_id(case.get("classname", ""), name, case.get("file"))
        outcome = TestOutcome.PASSED
        message = None
        for child in case:
            tag = child.tag
            if tag == "failure":
                outcome, message = TestOutcome.FAILED, child.get("message")
            elif tag == "error":
                outcome, message = TestOutcome.ERROR, child.get("message")
            elif tag == "skipped" and outcome == TestOutcome.PASSED:
                outcome, message = TestOutcome.SKIPPED, child.get("message")
        if message:
            message = _clean(message, workspace)[:500]
        try:
            duration = float(case.get("time", "0") or 0)
        except ValueError:
            duration = 0.0
        new = TestCaseResult(test_id=identifier, outcome=outcome, message=message, duration_seconds=round(duration, 4))
        existing = results.get(identifier)
        # Setup/teardown errors can add a second entry for the same test: keep the worse outcome.
        if existing is None or SEVERITY[new.outcome] > SEVERITY[existing.outcome]:
            results[identifier] = new
    return results


class PytestRunner:
    def __init__(self, limits: PatchLimits | None = None, *, python: str | None = None) -> None:
        self.limits = limits or PatchLimits()
        self.python = python or sys.executable
        self._counter = 0

    @property
    def resource_limits(self) -> ResourceLimits:
        return ResourceLimits(
            cpu_seconds=self.limits.process_cpu_seconds,
            memory_bytes=self.limits.process_memory_bytes,
            file_bytes=self.limits.process_file_bytes,
        )

    @staticmethod
    def command_identity(targets: list[str]) -> list[str]:
        return ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header", "-o", "junit_family=xunit1",
                "--junitxml=<meta>/junit.xml", "--rootdir=.", *targets]

    def run(
        self,
        workspace: Workspace,
        targets: list[str],
        *,
        label: str,
        kind: str,
        timeout: float,
    ) -> TestSuiteRun:
        for target in targets:
            if not TEST_TARGET.match(target) or ".." in target.split("/"):
                raise ValueError(f"Refusing to run an unvalidated test target: {target!r}")
        self._counter += 1
        junit = workspace.meta / f"junit-{label}-{self._counter}.xml"
        command = [self.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header", "-o",
                   "junit_family=xunit1", f"--junitxml={junit}", "--rootdir=.", *targets]
        process = run_process(
            command,
            timeout=timeout,
            cwd=workspace.repo,
            env=child_environment(workspace),
            max_output_bytes=self.limits.max_check_output_bytes,
            limits=self.resource_limits,
        )
        return self._to_run(process, workspace, junit, targets, label=label, kind=kind)

    def _to_run(self, process: ProcessResult, workspace, junit, targets, *, label, kind) -> TestSuiteRun:
        base = {
            "label": label,
            "kind": kind,
            "command": self.command_identity(targets),
            "targets": targets,
            "exit_code": process.exit_code,
            "stdout": _clean(process.stdout, workspace),
            "stderr": _clean(process.stderr, workspace),
            "output_truncated": process.stdout_truncated or process.stderr_truncated,
            "duration_seconds": round(process.duration_seconds, 3),
        }
        if process.timed_out:
            return TestSuiteRun(status="timed_out", error="The test run timed out", **base)
        try:
            tests = parse_junit(junit, workspace)
        except (ValueError, ET.ParseError) as exc:
            return TestSuiteRun(status="error", error=str(exc)[:200], **base)
        if process.exit_code == 5 or (process.exit_code == 0 and not tests):
            return TestSuiteRun(status="no_tests", tests=tests, error="pytest collected no tests", **base)
        if process.exit_code in (0, 1) or (process.exit_code == 2 and tests):
            # 2: interrupted, typically by collection errors; those appear as error entries.
            return TestSuiteRun(status="completed", tests=tests, **base)
        return TestSuiteRun(status="error", tests=tests, error=f"pytest exited with {process.exit_code}", **base)


# --- ruff ----------------------------------------------------------------------------


class RuffReport(BaseModel):
    available: bool
    violations: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    exit_code: int | None = None
    duration_seconds: float = 0.0
    timed_out: bool = False

    def keys(self) -> Counter:
        """Line-independent identity of each violation, so a shifted line is not 'new'."""
        return Counter((v["file"], v["code"], v["message"]) for v in self.violations)


class RuffRunner:
    def __init__(self, limits: PatchLimits | None = None, *, python: str | None = None) -> None:
        self.limits = limits or PatchLimits()
        self.python = python or sys.executable

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("ruff") is not None

    def run(self, workspace: Workspace, files: list[str], *, timeout: float) -> RuffReport:
        files = [f for f in files if f.endswith((".py", ".pyi")) and (workspace.repo / f).is_file()]
        if not self.available():
            return RuffReport(available=False, error="ruff is not installed")
        if not files:
            return RuffReport(available=True)
        process = run_process(
            [self.python, "-m", "ruff", "check", "--no-cache", "--output-format=json", "--exit-zero", "--", *files],
            timeout=timeout,
            cwd=workspace.repo,
            env=child_environment(workspace),
            max_output_bytes=self.limits.max_check_output_bytes * 4,
        )
        report = {"exit_code": process.exit_code, "duration_seconds": round(process.duration_seconds, 3)}
        if process.timed_out:
            return RuffReport(available=True, timed_out=True, error="ruff timed out", **report)
        if process.exit_code != 0 or process.stdout_truncated:
            return RuffReport(available=True, error=_clean(process.stderr, workspace)[:500] or "ruff failed", **report)
        try:
            raw = json.loads(process.stdout or "[]")
        except ValueError:
            return RuffReport(available=True, error="ruff returned unreadable output", **report)
        repo = str(workspace.repo.resolve())
        violations = []
        for item in raw:
            filename = str(item.get("filename", ""))
            for prefix in (repo, str(workspace.repo)):
                filename = filename.removeprefix(prefix + os.sep)
            violations.append(
                {
                    "file": filename.replace(os.sep, "/"),
                    "code": item.get("code") or "syntax",
                    "message": str(item.get("message", ""))[:300],
                    "line": (item.get("location") or {}).get("row"),
                }
            )
        return RuffReport(available=True, violations=violations, **report)
