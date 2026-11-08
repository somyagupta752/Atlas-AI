"""Deterministic test selection for patch verification.

Sources, in priority order (lower number first when the count limit applies):

0. tests the investigation ran that failed: they reproduce the bug;
1. other tests the investigation ran;
2. test files named in the diagnosis (affected files, cited evidence);
3. test files that import an affected module, or are named after it
   (``test_<module>.py``);
4. test files that import a module which imports an affected module (one hop
   through the import graph);
5. suggestions (the code graph, or a model), accepted only after the same
   validation. A suggestion's priority is never better than 3, so it cannot
   outrank what the investigation itself ran or cited.

Every target is validated: a relative path inside the repository, a pytest file
name, an existing regular file, not reached through a symlink. Anything else is
dropped with a warning. The full suite is a separate, explicit regression run.
"""

import ast
import os
import re
from pathlib import Path, PurePosixPath

from app.agents.schemas import RootCauseAnalysis
from app.patching.graph_tests import Suggestion
from app.patching.models import TestSelection, TestSelectionItem
from app.patching.workspace import EXCLUDED_DIRECTORIES
from app.tools.base import ToolInputError, ToolPermissionError, resolve_in_repository

TEST_FILE = re.compile(r"^(test_.*|.*_test)\.py$")
TEST_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(::[A-Za-z_][A-Za-z0-9_]*)*(\[[^\]]{0,200}\])?$")
MAX_SCANNED_FILES = 5_000
MIN_SUGGESTION_PRIORITY = 3  # suggestions rank after investigation-sourced tests (0-2)


def validate_test_target(root: Path, raw: str) -> tuple[str, str | None]:
    """``(relative test file, optional test name)``. Raises ``ValueError`` with a reason."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("empty test path")
    file_part, _, name = raw.strip().partition("::")
    try:
        resolved, relative = resolve_in_repository(root, file_part)
    except (ToolInputError, ToolPermissionError) as exc:
        raise ValueError(str(exc)) from None
    if not TEST_FILE.match(PurePosixPath(relative).name):
        raise ValueError(f"{relative} is not a pytest file (test_*.py or *_test.py)")
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{relative} goes through a symlink")
    if not resolved.is_file():
        raise ValueError(f"{relative} is not a file")
    if name and not TEST_NAME.match(name):
        raise ValueError(f"invalid test name {name!r}")
    return relative, (name or None)


def _python_files(root: Path):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRECTORIES and not d.endswith(".egg-info"))
        for name in sorted(filenames):
            if name.endswith(".py"):
                path = Path(dirpath) / name
                if path.is_symlink():
                    continue
                count += 1
                if count > MAX_SCANNED_FILES:
                    return
                yield path.relative_to(root).as_posix(), path


def _imports(path: Path) -> set[str]:
    """Dotted names a file imports: ``a.b`` for ``import a.b``, ``a.b`` and ``a.b.c`` for ``from a.b import c``."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def module_names(relative: str) -> set[str]:
    """Names a module may be imported by: every dotted suffix, without ``__init__``."""
    parts = list(PurePosixPath(relative).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return {".".join(parts[i:]) for i in range(len(parts))} if parts else set()


class _Collector:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.items: dict[str, TestSelectionItem] = {}
        self.warnings: list[str] = []

    def add(self, raw: str, reason: str, priority: int, *, reproduces: bool = False, score: float = 0.0) -> None:
        try:
            relative, name = validate_test_target(self.root, raw)
        except ValueError as exc:
            self.warnings.append(f"Ignored test target {raw[:120]!r}: {exc}")
            return
        item = self.items.get(relative)
        if item is None:
            item = self.items[relative] = TestSelectionItem(target=relative, priority=priority)
        if priority < item.priority:
            item.priority, item.score = priority, score
        elif priority == item.priority:
            item.score = max(item.score, score)
        if reason not in item.reasons:
            item.reasons.append(reason)
        if reproduces:
            item.reproduces_bug = True
            if name and name not in item.bug_test_names:
                item.bug_test_names.append(name)


def select_tests(
    root: Path,
    investigation: RootCauseAnalysis,
    *,
    affected_files: list[str],
    affected_symbols: list[str] | None = None,
    max_test_files: int = 20,
    include_full_suite: bool = True,
    suggested: list["str | Suggestion"] | None = None,
) -> TestSelection:
    """Select targeted tests for ``affected_files`` in the workspace at ``root``."""
    root = root.resolve()
    collect = _Collector(root)

    for run in investigation.tests_run:
        if run.outcome in ("failed", "error"):
            collect.add(run.test_path, f"failed during the investigation ({run.outcome})", 0, reproduces=True)
        else:
            collect.add(run.test_path, f"run during the investigation ({run.outcome})", 1)
    for path in investigation.affected_files:
        if TEST_FILE.match(PurePosixPath(path).name):
            collect.add(path, "named in the diagnosis", 2)
    for evidence in investigation.evidence:
        if evidence.file_path and TEST_FILE.match(PurePosixPath(evidence.file_path).name):
            collect.add(evidence.file_path, f"cited as evidence {evidence.id}", 2)

    affected_modules: dict[str, str] = {}
    for path in affected_files:
        for name in module_names(path):
            affected_modules.setdefault(name, path)
    stems = {PurePosixPath(p).stem: p for p in affected_files}
    symbol_names = {s.rsplit(".", 1)[-1] for s in affected_symbols or [] if s}

    files = list(_python_files(root))
    imports = {relative: _imports(path) for relative, path in files}
    importers: dict[str, set[str]] = {}  # source module file -> affected files it imports
    for relative, names in imports.items():
        if TEST_FILE.match(PurePosixPath(relative).name):
            continue
        hit = {affected_modules[n] for n in names if n in affected_modules and affected_modules[n] != relative}
        if hit:
            importers[relative] = hit

    for relative, names in imports.items():
        file_name = PurePosixPath(relative).name
        if not TEST_FILE.match(file_name):
            continue
        direct = sorted({affected_modules[n] for n in names if n in affected_modules})
        if direct:
            collect.add(relative, f"imports affected module {', '.join(direct)}", 3)
        stem = file_name.removesuffix(".py").removeprefix("test_").removesuffix("_test")
        if stem in stems:
            collect.add(relative, f"named after affected file {stems[stem]}", 3)
        if symbol_names and names & {f"{m}.{s}" for m in affected_modules for s in symbol_names}:
            collect.add(relative, "imports an affected symbol", 3)
        indirect = sorted(
            {f"{via} -> {target}" for via, targets in importers.items() for target in targets
             if module_names(via) & names}
        )
        if indirect and not direct:
            collect.add(relative, f"imports {indirect[0]} (one hop)", 4)

    for raw in suggested or []:
        if isinstance(raw, Suggestion):
            collect.add(raw.target, raw.reason, max(MIN_SUGGESTION_PRIORITY, raw.priority), score=raw.score)
        else:
            collect.add(raw, "suggested", 5)

    ordered = sorted(collect.items.values(), key=lambda item: (item.priority, -item.score, item.target))
    truncated = len(ordered) > max_test_files
    kept = ordered[:max_test_files]
    if truncated and any(item.reproduces_bug for item in ordered[max_test_files:]):
        collect.warnings.append("A bug-reproducing test was dropped by the test count limit")
    return TestSelection(items=kept, full_suite=include_full_suite, truncated=truncated, warnings=collect.warnings)
