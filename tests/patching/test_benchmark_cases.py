"""The patching benchmark's reference patches, run through the full pipeline.

Correct patches must be verified, incorrect ones rejected, regression-breaking
ones rejected and test-tampering or out-of-scope ones fail validation. The
benchmark repositories themselves must stay byte-for-byte unchanged.
"""

import json
from pathlib import Path

import pytest

from app.patching.evaluation import (
    PatchingBenchmarkCase,
    benchmark_investigation,
    reference_llm,
    run_case,
    score_execution,
    summarize,
)
from app.patching.models import PatchLimits
from app.patching.workspace import WorkspaceManager, snapshot_tree

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "benchmarks" / "patching"
CASES = [PatchingBenchmarkCase.model_validate(c) for c in json.loads((BENCH / "cases.json").read_text())]
DIAGNOSES = {d["investigation_case"]: d for d in json.loads((BENCH / "diagnoses.json").read_text())}
VARIANTS = [(case, ref) for case in CASES for ref in case.reference_patches]


def test_benchmark_has_at_least_five_cases_and_every_kind_of_reference():
    assert len(CASES) >= 5
    expected = {ref.expected_status for _, ref in VARIANTS}
    assert expected == {"verified", "rejected", "validation_failed"}
    assert all(any(ref.expected_status == "verified" for ref in case.reference_patches) for case in CASES)
    names = {ref.name for _, ref in VARIANTS}
    assert {"correct", "no_fix", "regression", "test_tamper"} <= names


def test_expected_answers_are_separate_from_model_input():
    for case in CASES:
        model_input = json.dumps(DIAGNOSES[case.investigation_case])
        for ref in case.reference_patches:
            assert (BENCH / ref.file).read_text() not in model_input
        assert "required_tests" not in model_input and case.expected_behaviour not in model_input


@pytest.mark.parametrize(("case", "reference"), VARIANTS, ids=[f"{c.case_id}:{r.name}" for c, r in VARIANTS])
def test_reference_patch_gets_its_expected_verdict(case, reference, tmp_path):
    root = (ROOT / case.repository_path).resolve()
    before = snapshot_tree(root).root_hash
    execution = run_case(
        case, root, reference_llm((BENCH / reference.file).read_text()),
        benchmark_investigation(case, DIAGNOSES[case.investigation_case]),
        workspaces=WorkspaceManager(tmp_path / "ws"), limits=PatchLimits(baseline_runs=1),
        trusted_roots=[ROOT / "benchmarks"], max_attempts=1, full_suite=reference.name == "regression",
    )
    score = score_execution(case, execution, variant=reference.name, expected_status=reference.expected_status)
    assert execution.status == reference.expected_status, execution.summary
    assert snapshot_tree(root).root_hash == before and not execution.original_repository_modified
    if reference.expected_status == "verified":
        assert score.success and score.bug_fixed and score.regression_free and score.only_expected_files
    else:
        assert not score.success and not score.verified


def test_scoring_does_not_reward_a_green_suite_alone(tmp_path):
    case = CASES[0]
    root = (ROOT / case.repository_path).resolve()
    def run(ref):
        return run_case(
            case, root, reference_llm((BENCH / ref.file).read_text()),
            benchmark_investigation(case, DIAGNOSES[case.investigation_case]),
            workspaces=WorkspaceManager(tmp_path / ref.name), limits=PatchLimits(baseline_runs=1),
            trusted_roots=[ROOT / "benchmarks"], max_attempts=1, full_suite=False,
        )

    references = {r.name: r for r in case.reference_patches}
    regression = score_execution(case, run(references["regression"]), variant="regression")
    assert regression.bug_fixed and not regression.regression_free and not regression.success
    correct = score_execution(case, run(references["correct"]), variant="correct")
    summary = summarize([correct, regression])
    assert summary.bug_fix_rate == 1.0 and summary.verified_patch_rate == 0.5 and summary.success_rate == 0.5
    assert summary.mean_attempts == 1.0 and summary.total_cost_usd is None  # the fake model has no price
