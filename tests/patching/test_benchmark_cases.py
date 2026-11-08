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
    run_holdout,
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
    assert all(any(ref.expected_status == "verified" and ref.expected_holdout is not False
                   for ref in case.reference_patches) for case in CASES)
    names = {ref.name for _, ref in VARIANTS}
    assert {"correct", "no_fix", "regression", "test_tamper"} <= names


def test_expected_answers_are_separate_from_model_input():
    for case in CASES:
        model_input = json.dumps(DIAGNOSES[case.investigation_case])
        for ref in case.reference_patches:
            for diff in ref.diff_files:
                assert (BENCH / diff).read_text() not in model_input
        for extra in [*case.holdout_tests, *case.exploratory_tests]:
            assert (BENCH / extra).read_text() not in model_input
        assert "required_tests" not in model_input and case.expected_behaviour not in model_input


@pytest.mark.parametrize(("case", "reference"), VARIANTS, ids=[f"{c.case_id}:{r.name}" for c, r in VARIANTS])
def test_reference_patch_gets_its_expected_verdict(case, reference, tmp_path):
    root = (ROOT / case.repository_path).resolve()
    before = snapshot_tree(root).root_hash
    workspaces = WorkspaceManager(tmp_path / "ws")
    limits = PatchLimits(baseline_runs=1)
    execution = run_case(
        case, root, reference_llm([(BENCH / f).read_text() for f in reference.diff_files]),
        benchmark_investigation(case, DIAGNOSES[case.investigation_case]),
        workspaces=workspaces, limits=limits, trusted_roots=[ROOT / "benchmarks"],
        max_attempts=len(reference.diff_files), full_suite=reference.name == "regression",
    )
    holdout = run_holdout(case, root, execution, BENCH, workspaces=workspaces, limits=limits)
    score = score_execution(case, execution, variant=reference.name, expected_status=reference.expected_status,
                            expected_attempts=reference.expected_attempts,
                            expected_holdout=reference.expected_holdout, holdout=holdout)
    assert execution.status == reference.expected_status, execution.summary
    assert score.matches_expectation, (score.attempt_statuses, score.holdout_passed)
    assert snapshot_tree(root).root_hash == before and not execution.original_repository_modified
    if reference.expected_status == "verified" and reference.expected_holdout is not False:
        assert score.success and score.bug_fixed and score.regression_free and score.only_expected_files
    else:
        assert not score.success


def test_overfit_patches_are_verified_by_atlas_but_fail_the_holdout():
    """Atlas can only check the tests the repository has; held-out tests measure generalisation."""
    overfit = [(c, r) for c, r in VARIANTS if r.expected_holdout is False]
    assert {r.name for _, r in overfit} == {"special_case_2000", "overfit"}
    assert all(r.expected_status == "verified" for _, r in overfit)


def test_every_harder_category_is_covered():
    categories = {c.category for c in CASES}
    assert {"multi_file", "ambiguous_root_cause", "interacting_bugs", "plausible_wrong_fix"} <= categories
    assert any(len(r.diff_files) > 1 and r.expected_attempts == 2 for _, r in VARIANTS)  # a real retry
    assert any(len((BENCH / r.diff_files[-1]).read_text().split("--- a/")) > 2 for _, r in VARIANTS)  # multi-file


def test_agent_cases_are_consistent():
    from app.agents.evaluation import DebuggingBenchmarkCase, root_cause_matches
    from app.code_intelligence import analyze_repository
    from app.tools.get_symbol import symbol_matches

    agent = [DebuggingBenchmarkCase.model_validate(c)
             for c in json.loads((ROOT / "benchmarks" / "agent" / "cases.json").read_text())]
    by_id = {c.case_id: c for c in agent}
    for case in CASES:
        expected = by_id[case.investigation_case]
        root = ROOT / expected.repository_path
        assert all((root / f).is_file() for f in expected.expected_files)
        names = [(e.qualified_name, e.name, p.file_path) for p in analyze_repository(root).files
                 for e in [*p.functions, *p.classes]]
        assert all(any(symbol_matches(s, *n) for n in names) for s in expected.expected_symbols)
        assert root_cause_matches(expected.expected_root_cause, expected.root_cause_keywords)
        assert not root_cause_matches(expected.bug_report, expected.root_cause_keywords), case.case_id


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


def test_exploratory_tests_are_reported_but_never_scored(tmp_path):
    """bug-008: a patch that keeps raising on 'a:1,' fails the exploratory test and still succeeds."""
    from app.patching.evaluation import run_exploratory

    case = next(c for c in CASES if c.case_id == "bug-008-score-report")
    assert case.exploratory_tests and not set(case.exploratory_tests) & set(case.holdout_tests)
    holdout_source = (BENCH / case.holdout_tests[0]).read_text()
    assert "def test_whitespace_only" in holdout_source and "trailing" not in holdout_source
    assert "def test_trailing_comma" in (BENCH / case.exploratory_tests[0]).read_text()

    # The shape of the live patch: return early on blank text, keep raising on stray commas.
    early_return = (
        "--- a/parsing.py\n+++ b/parsing.py\n@@ -1,4 +1,6 @@\n def parse_scores(text: str) -> dict[str, int]:\n"
        '     """\'alice:3, bob:5\' -> {\'alice\': 3, \'bob\': 5}. Blank input means no scores."""\n'
        "     scores = {}\n+    if not text.strip():\n+        return scores\n     for item in text.split(\",\"):\n"
        + "--- a/report.py\n" + (BENCH / "reference/bug-008-score-report/correct.diff").read_text().split("--- a/report.py\n")[1]
    )
    root = (ROOT / case.repository_path).resolve()
    workspaces = WorkspaceManager(tmp_path / "ws")
    limits = PatchLimits(baseline_runs=1)
    execution = run_case(case, root, reference_llm(early_return),
                         benchmark_investigation(case, DIAGNOSES[case.investigation_case]),
                         workspaces=workspaces, limits=limits, trusted_roots=[ROOT / "benchmarks"],
                         max_attempts=1, full_suite=False)
    holdout = run_holdout(case, root, execution, BENCH, workspaces=workspaces, limits=limits)
    exploratory = run_exploratory(case, root, execution, BENCH, workspaces=workspaces, limits=limits)
    score = score_execution(case, execution, holdout=holdout, exploratory=exploratory)
    assert execution.status == "verified" and holdout["passed"] is True
    assert exploratory["passed"] is False and score.exploratory_passed is False
    assert score.success
    summary = summarize([score])
    assert summary.holdout_pass_rate == 1.0 and summary.holdout_cases == 1
    assert summary.exploratory_pass_rate == 0.0 and summary.success_rate == 1.0


RETRY_CASES = [c for c in CASES if c.category == "repair_retry"]


def _run_reference(case, diffs, tmp_path, limits=None):
    limits = limits or PatchLimits(baseline_runs=1)
    llm = reference_llm(diffs)
    workspaces = WorkspaceManager(tmp_path / "ws")
    execution = run_case(case, (ROOT / case.repository_path).resolve(), llm,
                         benchmark_investigation(case, DIAGNOSES[case.investigation_case]),
                         workspaces=workspaces, limits=limits, trusted_roots=[ROOT / "benchmarks"],
                         max_attempts=len(diffs), full_suite=False)
    holdout = run_holdout(case, (ROOT / case.repository_path).resolve(), execution, BENCH, workspaces=workspaces,
                          limits=limits)
    return execution, holdout, llm


@pytest.mark.parametrize("case", RETRY_CASES, ids=[c.case_id for c in RETRY_CASES])
def test_retry_record_proves_the_second_attempt_fixed_the_bug(case, tmp_path):
    refs = {r.name: r for r in case.reference_patches}
    diffs = [(BENCH / f).read_text() for f in refs["retry"].diff_files]
    execution, holdout, llm = _run_reference(case, diffs, tmp_path)
    score = score_execution(case, execution, holdout=holdout)
    evidence = score.repair_evidence
    assert score.repaired_by_retry and score.first_attempt_failed and score.success
    assert evidence["first_attempt_status"] == "rejected" and evidence["first_attempt_failed_checks"]
    assert evidence["feedback_delivered"] and evidence["diff_changed"] and evidence["final_received_feedback_from"] == [1]
    assert all(v == "passed" for v in evidence["final_required_tests"].values())
    # The second prompt carries the first attempt's real failure output, not a summary invented by Atlas.
    second_prompt = llm.calls[1]["messages"][1]["content"]
    first_check = next(c for c in execution.attempts[0].verification.checks if c.status == "failed")
    assert first_check.reason.splitlines()[0][:60] in second_prompt
    assert '<previous_attempt number="1">' in second_prompt


@pytest.mark.parametrize("case", RETRY_CASES, ids=[c.case_id for c in RETRY_CASES])
def test_first_attempt_success_is_never_a_repair(case, tmp_path):
    correct = (BENCH / next(r for r in case.reference_patches if r.name == "correct").file).read_text()
    execution, holdout, _ = _run_reference(case, [correct], tmp_path)
    score = score_execution(case, execution, holdout=holdout)
    assert score.success and not score.first_attempt_failed and not score.repaired_by_retry
    assert score.repair_evidence == {}


def test_plausible_first_attempts_fail_a_relevant_test_with_a_specific_message(tmp_path):
    expected = {
        "bug-011-split-bill": "[3334, 3333, 3333]",
        "bug-012-versions": "invalid literal for int()",
        "bug-013-unique-emails": "Ada.Lovelace@Example.com",
    }
    for case in RETRY_CASES:
        first = (BENCH / next(r for r in case.reference_patches if r.name == "plausible_first").file).read_text()
        execution, _, _ = _run_reference(case, [first], tmp_path / case.case_id)
        reasons = " ".join(execution.attempts[0].verification.failure_reasons)
        assert expected[case.case_id] in reasons, (case.case_id, reasons)


def test_test_blind_context_hides_tests_from_the_prompt_but_still_runs_them(tmp_path):
    case = next(c for c in RETRY_CASES if c.case_id == "bug-013-unique-emails")
    first = (BENCH / next(r for r in case.reference_patches if r.name == "plausible_first").file).read_text()
    execution, _, llm = _run_reference(case, [first], tmp_path,
                                       limits=PatchLimits(baseline_runs=1, max_context_test_files=0))
    prompt = llm.calls[0]["messages"][1]["content"]
    assert "test_original_spelling_is_kept" not in prompt and "modifiable=\"false\"" not in prompt
    assert "1 related test file(s) are not shown" in prompt
    assert execution.status == "rejected"  # the hidden test still ran and caught the regression
    assert "test_original_spelling_is_kept" in " ".join(execution.attempts[0].verification.failure_reasons)
