"""Graph-based test discovery: the Task 10 code graph suggests tests for Task 13.

The graph improves discovery. It is never an authority:

- its output is a list of *suggestions* for ``select_tests(suggested=...)``,
  which validates every path exactly like any other source;
- a graph suggestion never outranks a test the investigation ran or cited
  (priority 3 at best, after priorities 0-2);
- graph data that is missing, unreachable or stale never blocks verification.
  The discovery falls back to an in-memory graph built from the current source
  with the same Task 10 builder, or, when that is disabled, adds nothing.

How it works:

1. Seeds: every function and method in the modifiable files, from the current
   source. A patch can only change those files, so this covers any symbol a
   patch could modify. Symbols the diagnosis named weigh 1.0, the rest 0.6.
2. Traversal: breadth-first over incoming ``CALLS`` edges (who calls this?),
   one hop per query, up to ``max_depth`` hops, through helpers such as
   ``make_service()`` in a test file. Constructors resolve to ``__init__``.
3. Tests: callers that are test functions (``test*`` in a ``test_*.py`` /
   ``*_test.py`` file) end their path.
4. Ranking: seed weight x 0.7 per inferred edge x 0.6 per hop after the first.
   A file scores as its best test. Files are ranked by (score desc, path) and cut
   to ``max_suggestions``. Every step is sorted, so the result is deterministic.

Staleness: Neo4j holds whatever was last built. Before Neo4j results are used,
every seed and every discovered test is checked against the current source
(same qualified name, same line range). Any mismatch discards the Neo4j result
as a whole and uses the in-memory graph instead; mixing the two would not be
deterministic.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

TEST_FILE = re.compile(r"^(test_.*|.*_test)\.py$")
FOCUS_WEIGHT = 1.0
OTHER_WEIGHT = 0.6
INFERRED_FACTOR = 0.7
HOP_DECAY = 0.6
MAX_SEED_LOOKUP = 500
LIMIT_PER_NODE = 50


@dataclass(frozen=True)
class Suggestion:
    """A test file proposed by a source other than the investigation. Validated by ``select_tests``."""

    target: str
    reason: str = "suggested"
    priority: int = 5
    score: float = 0.0


class GraphDiscovery(BaseModel):
    source: Literal["neo4j", "in_memory", "unavailable", "disabled"]
    seeds: int = 0
    suggestions: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    stale_entities: list[str] = Field(default_factory=list)


class _Stale(Exception):
    pass


def _is_test_function(props: dict[str, Any]) -> bool:
    path = str(props.get("file_path") or "")
    return bool(TEST_FILE.match(PurePosixPath(path).name)) and str(props.get("name", "")).startswith("test")


def _current_entities(analysis: Any) -> dict[str, dict[str, tuple[int, int]]]:
    """file -> qualified name -> (start, end), for functions and classes in the current source."""
    out: dict[str, dict[str, tuple[int, int]]] = {}
    for parsed in analysis.files:
        entities = out.setdefault(parsed.file_path, {})
        for item in [*parsed.functions, *parsed.classes]:
            entities[item.qualified_name] = (item.start_line, item.end_line)
    return out


def _matches_current(props: dict[str, Any], current: dict[str, dict[str, tuple[int, int]]]) -> bool:
    lines = current.get(str(props.get("file_path")), {}).get(str(props.get("qualified_name")))
    return lines is not None and lines == (props.get("line_start"), props.get("line_end"))


def _seed_weight(qualified_name: str, focus: set[str]) -> float:
    name = qualified_name.rsplit(".", 1)[-1]
    return FOCUS_WEIGHT if qualified_name in focus or name in focus else OTHER_WEIGHT


def _traverse(source: Any, repository_id: str, seeds: dict[str, tuple[float, str]], max_depth: int,
              frontier_limit: int, check: Any = None) -> dict[str, dict[str, Any]]:
    """Best path to every test function reachable through callers. ``seeds``: id -> (weight, name)."""
    best: dict[str, dict[str, Any]] = {}
    visited = {seed: (weight, [name]) for seed, (weight, name) in seeds.items()}
    frontier = dict(visited)
    for depth in range(1, max_depth + 1):
        if not frontier:
            break
        records = source.expand_neighborhood(repository_id, sorted(frontier), 1, ["CALLS"], LIMIT_PER_NODE)
        found: dict[str, tuple[float, list[str]]] = {}
        for record in sorted(records, key=lambda r: (r["seed_id"], r["entity_id"])):
            step = record["steps"][0]
            if step["target_id"] != record["seed_id"] or step["source_id"] != record["entity_id"]:
                continue  # only "who calls this", never "what this calls"
            props = record.get("properties") or {}
            if record.get("label") != "Function":
                continue
            if check is not None:
                check(props)
            weight, chain = frontier[record["seed_id"]]
            score = weight * (INFERRED_FACTOR if step.get("resolution_status") == "inferred" else 1.0)
            score *= HOP_DECAY if depth > 1 else 1.0
            name = str(props.get("qualified_name") or record["entity_id"])
            caller = record["entity_id"]
            if _is_test_function(props):
                current = best.get(caller)
                if current is None or score > current["score"]:
                    best[caller] = {"file": props["file_path"], "test": name, "score": score, "depth": depth,
                                    "chain": [name, *chain]}
                continue
            previous = visited.get(caller)
            if previous is not None and previous[0] >= score:
                continue
            candidate = found.get(caller)
            if candidate is None or score > candidate[0]:
                found[caller] = (score, [name, *chain])
        visited.update(found)
        ranked = sorted(found.items(), key=lambda item: (-item[1][0], item[0]))[:frontier_limit]
        frontier = dict(ranked)
    return best


def _suggestions(best: dict[str, dict[str, Any]], label: str, max_suggestions: int) -> list[Suggestion]:
    by_file: dict[str, list[dict[str, Any]]] = {}
    for item in best.values():
        by_file.setdefault(item["file"], []).append(item)
    ranked = []
    for path, items in by_file.items():
        items.sort(key=lambda i: (-i["score"], i["test"]))
        ranked.append((path, items))
    ranked.sort(key=lambda entry: (-entry[1][0]["score"], entry[0]))
    out = []
    for path, items in ranked[:max_suggestions]:
        top = items[0]
        names = ", ".join(i["test"] for i in items[:3]) + (f" (+{len(items) - 3} more)" if len(items) > 3 else "")
        reason = (f"graph ({label}): {names} reach the patched code; best path {' -> '.join(top['chain'])} "
                  f"({top['depth']} hop{'s' if top['depth'] > 1 else ''}, score {top['score']:.2f})")
        out.append(Suggestion(target=path, reason=reason, priority=3 if top["depth"] == 1 else 4,
                              score=round(top["score"], 4)))
    return out


def discover_tests(
    root: Path,
    repository_id: str,
    affected_files: list[str],
    focus_symbols: list[str] | None = None,
    *,
    graph: Any = None,
    max_depth: int = 3,
    max_suggestions: int = 10,
    frontier_limit: int = 200,
    allow_in_memory: bool = True,
    analysis: Any = None,
) -> tuple[list[Suggestion], GraphDiscovery]:
    """Suggest test files that call into ``affected_files``. Never raises for graph problems."""
    from app.code_intelligence import analyze_repository
    from app.knowledge_graph.repository import GraphStoreError

    analysis = analysis or analyze_repository(root)
    current = _current_entities(analysis)
    affected = set(affected_files)
    focus = {s for s in focus_symbols or [] if s}
    seed_names = sorted({q for f in affected for q in current.get(f, {})})
    report = GraphDiscovery(source="unavailable")
    if not seed_names:
        report.warnings.append("No functions in the modifiable files: nothing to search the graph from")
        return [], report

    if graph is not None:
        try:
            records = graph.find_entities_by_name(repository_id, seed_names, MAX_SEED_LOOKUP)
            seeds = {}
            for record in sorted(records, key=lambda r: r["id"]):
                props = record.get("properties") or {}
                if record.get("label") != "Function" or props.get("file_path") not in affected:
                    continue
                if not _matches_current(props, current):
                    raise _Stale(f"{props.get('file_path')}::{props.get('qualified_name')}")
                seeds[record["id"]] = (_seed_weight(props["qualified_name"], focus), props["qualified_name"])
            expected = {(f, q) for f in affected for q in current.get(f, {})}
            if not seeds:
                report.warnings.append("The Neo4j graph has no entities for the modifiable files; "
                                       "used an in-memory graph of the current source")
            else:
                if len(seeds) < sum(1 for f, q in expected if _is_function(analysis, f, q)):
                    raise _Stale("functions missing from the stored graph")

                def check(props: dict[str, Any]) -> None:
                    if _is_test_function(props) and not _matches_current(props, current):
                        raise _Stale(f"{props.get('file_path')}::{props.get('qualified_name')}")

                best = _traverse(graph, repository_id, seeds, max_depth, frontier_limit, check)
                suggestions = _suggestions(best, "neo4j", max_suggestions)
                report.source, report.seeds = "neo4j", len(seeds)
                report.suggestions = [s.__dict__ for s in suggestions]
                return suggestions, report
        except _Stale as exc:
            report.stale_entities.append(str(exc))
            report.warnings.append(f"The Neo4j graph is stale ({exc}); used an in-memory graph of the current source")
        except GraphStoreError:
            report.warnings.append("The Neo4j graph is unavailable; used an in-memory graph of the current source")
        except Exception as exc:  # noqa: BLE001 - graph trouble must never block verification
            logger.warning("Graph test discovery failed on Neo4j: %s", type(exc).__name__)
            report.warnings.append(f"Neo4j graph lookup failed ({type(exc).__name__}); used an in-memory graph")

    if not allow_in_memory:
        report.warnings.append("In-memory graph fallback is disabled; no graph suggestions")
        return [], report
    from app.knowledge_graph.builder import build_code_graph
    from app.retrieval.graph_search import InMemoryGraphContextSource

    memory = InMemoryGraphContextSource(build_code_graph(analysis, repository_id))
    records = memory.find_entities_by_name(repository_id, seed_names, MAX_SEED_LOOKUP)
    seeds = {
        r["id"]: (_seed_weight(r["properties"]["qualified_name"], focus), r["properties"]["qualified_name"])
        for r in sorted(records, key=lambda r: r["id"])
        if r.get("label") == "Function" and r["properties"].get("file_path") in affected
    }
    best = _traverse(memory, repository_id, seeds, max_depth, frontier_limit)
    suggestions = _suggestions(best, "in-memory", max_suggestions)
    report.source, report.seeds = "in_memory", len(seeds)
    report.suggestions = [s.__dict__ for s in suggestions]
    return suggestions, report


def _is_function(analysis: Any, file_path: str, qualified_name: str) -> bool:
    for parsed in analysis.files:
        if parsed.file_path == file_path:
            return any(f.qualified_name == qualified_name for f in parsed.functions)
    return False
