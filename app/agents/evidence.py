"""Ground each evidence item in the tool observations it cites.

An observation is what a tool returned. Evidence is a claim drawn from it. A
claim is accepted only when:

- every cited observation exists in this investigation and succeeded;
- its source type matches the tools that produced those observations;
- its file and line range lie inside a range those observations showed;
- its qualified name appears in what those observations returned.

This does not prove the claim is *true*. It proves the claim points at real,
observed code, so no source location can be invented.
"""

from app.agents.schemas import Evidence
from app.agents.state import InvestigationState, Observation

# Tools whose observations can back each evidence type.
ALLOWED_TOOLS: dict[str, frozenset[str]] = {
    "source_code": frozenset({"read_file", "get_symbol", "search_code", "retrieve_evidence"}),
    "test": frozenset({"read_file", "get_symbol", "search_code", "retrieve_evidence", "run_tests"}),
    "graph": frozenset({"get_callers", "get_callees"}),
    "retrieval": frozenset({"retrieve_evidence"}),
    "execution": frozenset({"run_tests"}),
}


def validate_evidence(item: Evidence, state: InvestigationState) -> list[str]:
    errors: list[str] = []
    cited: list[Observation] = []
    for observation_id in item.observation_ids:
        observation = state.get_observation(observation_id)
        if observation is None:
            errors.append(f"Evidence {item.id} cites unknown observation {observation_id!r}")
        elif not observation.ok:
            errors.append(f"Evidence {item.id} cites {observation_id}, which did not succeed ({observation.status})")
        else:
            cited.append(observation)
    if errors:
        return errors

    allowed = ALLOWED_TOOLS[item.source_type]
    if not any(o.tool_name in allowed for o in cited):
        errors.append(
            f"Evidence {item.id} is '{item.source_type}' but cites none of: {', '.join(sorted(allowed))}"
        )

    if item.file_path:
        locations = [loc for o in cited for loc in o.locations]
        if not any(loc.file_path == item.file_path for loc in locations):
            errors.append(f"Evidence {item.id}: {item.file_path} does not appear in the cited observations")
        elif (item.start_line or item.end_line) and not any(
            loc.covers(item.file_path, item.start_line, item.end_line) for loc in locations
        ):
            errors.append(
                f"Evidence {item.id}: lines {item.start_line}-{item.end_line} of {item.file_path} "
                "were not shown by the cited observations"
            )

    if item.qualified_name:
        short = item.qualified_name.replace(".<locals>", "").rsplit(".", 1)[-1]
        if not any(short in o.content for o in cited):
            errors.append(f"Evidence {item.id}: {item.qualified_name} does not appear in the cited observations")
    return errors


def validate_all(evidence: list[Evidence], state: InvestigationState) -> list[str]:
    errors: list[str] = []
    for item in evidence:
        errors.extend(validate_evidence(item, state))
    return errors


def evidence_files(evidence: list[Evidence]) -> set[str]:
    return {item.file_path for item in evidence if item.file_path}
