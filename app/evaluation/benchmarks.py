"""The benchmark suite as the evaluation laboratory sees it.

Reuses the existing definitions (``benchmarks/patching/cases.json``, ``diagnoses.json``,
``benchmarks/agent/cases.json``). Clients choose case IDs from this list; they never send
paths or commands. The version is a hash of every file that can change a result.
"""

import hashlib
import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from app.agents.evaluation import DebuggingBenchmarkCase
from app.patching.evaluation import PatchingBenchmarkCase

BENCHMARKS_DIR = Path(__file__).resolve().parents[2] / "benchmarks"
PATCHING_DIR = BENCHMARKS_DIR / "patching"
# Inputs that define what is being measured. Results, caches and compiled files are not part of it.
VERSIONED_ROOTS = ("patching/cases.json", "patching/diagnoses.json", "patching/holdout", "patching/exploratory",
                   "patching/reference", "agent/cases.json")


class UnknownBenchmarkError(LookupError):
    def __init__(self, case_ids: list[str]) -> None:
        super().__init__(f"Unknown benchmark case(s): {', '.join(case_ids)}")
        self.case_ids = case_ids


@dataclass(frozen=True)
class BenchmarkSuite:
    cases: dict[str, PatchingBenchmarkCase]
    diagnoses: dict[str, dict]
    agent_cases: dict[str, DebuggingBenchmarkCase]
    version: str

    def select(self, case_ids: list[str] | None) -> list[PatchingBenchmarkCase]:
        """The named cases in suite order (not request order), or all when ``case_ids`` is None."""
        if case_ids is None:
            return list(self.cases.values())
        unknown = sorted(set(case_ids) - set(self.cases))
        if unknown:
            raise UnknownBenchmarkError(unknown)
        wanted = set(case_ids)
        return [c for c in self.cases.values() if c.case_id in wanted]

    def categories(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for case in self.cases.values():
            out.setdefault(case.category, []).append(case.case_id)
        return out

    def case_root(self, case: PatchingBenchmarkCase) -> Path:
        root = (BENCHMARKS_DIR.parent / case.repository_path).resolve()
        if BENCHMARKS_DIR.resolve() not in root.parents:
            raise ValueError(f"case {case.case_id} points outside the benchmarks directory")
        return root


def _digest(root: Path = BENCHMARKS_DIR) -> str:
    h = hashlib.sha256()
    files: list[Path] = []
    for relative in VERSIONED_ROOTS:
        target = root / relative
        files += [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
    for case_file in sorted(root.glob("bug_*/**/*")):
        if case_file.is_file() and "__pycache__" not in case_file.parts and case_file.suffix != ".pyc":
            files.append(case_file)
    for path in sorted(set(files)):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        h.update(str(path.relative_to(root)).encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


@cache
def load_suite() -> BenchmarkSuite:
    cases = [PatchingBenchmarkCase.model_validate(c)
             for c in json.loads((PATCHING_DIR / "cases.json").read_text())]
    diagnoses = {d["investigation_case"]: d for d in json.loads((PATCHING_DIR / "diagnoses.json").read_text())}
    agent = {c["case_id"]: DebuggingBenchmarkCase.model_validate(c)
             for c in json.loads((BENCHMARKS_DIR / "agent" / "cases.json").read_text())}
    return BenchmarkSuite(
        cases={c.case_id: c for c in cases}, diagnoses=diagnoses, agent_cases=agent,
        version="patching-" + _digest()[:16],
    )
