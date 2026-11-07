"""The full lifecycle through PatchingService: FakeLLM, real workspaces, real pytest, real PostgreSQL rows."""

import itertools
import json
import uuid

import pytest

from app.agents.agent import RepositoryAccessDeniedError
from app.agents.llm import FakeLLM, LLMTimeoutError, final_answer
from app.agents.repository import InvestigationNotFoundError
from app.models import InvestigationRecord, PatchProposalRecord, PatchVerificationRecord
from app.patching.models import (
    PATCH_TRANSITIONS,
    TERMINAL_PATCH_STATUSES,
    InvalidPatchTransitionError,
    PatchLimits,
    PatchStatus,
    check_patch_transition,
)
from app.patching.repository import PatchPersistenceError, PatchRepository
from app.patching.service import InvestigationNotEligibleError, PatchRequestError
from app.patching.workspace import WorkspaceError, WorkspaceManager, snapshot_tree
from tests.patching.conftest import (
    AVERAGE_FIX,
    AVERAGE_NO_FIX,
    AVERAGE_NOOP,
    AVERAGE_REGRESSION,
    AVERAGE_SYNTAX_ERROR,
    AVERAGE_TEST_TAMPER,
    AVERAGE_UNRELATED,
    make_analysis,
    submit,
)


class RecordingManager(WorkspaceManager):
    """Records every workspace and what it contained when it was created."""

    def __init__(self, *args, fail_on=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.created = []
        self.fail_on = fail_on

    def create(self, source_root, *, execution_id, label):
        if self.fail_on and label.startswith(self.fail_on):
            raise WorkspaceError("disk full")
        workspace = super().create(source_root, execution_id=execution_id, label=label)
        self.created.append((label, (workspace.repo / "solution.py").read_text()))
        return workspace


@pytest.fixture
def manager(tmp_path):
    return RecordingManager(tmp_path / "ws")


@pytest.fixture
def stored(db_session):
    """A persisted completed investigation (patch rows reference it) and a PatchRepository."""

    def make():
        iid = str(uuid.uuid4())
        db_session.add(InvestigationRecord(id=uuid.UUID(iid), repository_id="avg", bug_report="crash",
                                           status="completed"))
        db_session.commit()
        return iid, PatchRepository(db_session)

    return make


def rows(db_session, iid):
    return list(db_session.query(PatchProposalRecord).filter_by(investigation_id=uuid.UUID(iid))
                .order_by(PatchProposalRecord.attempt_number))


# --- outcomes --------------------------------------------------------------------------


def test_correct_patch_is_verified_and_persisted(build_service, average_repo, db_session, stored):
    iid, patches = stored()
    before = snapshot_tree(average_repo).root_hash
    service, llm, _ = build_service([submit(AVERAGE_FIX)], patches=patches, investigation_id=iid)
    execution = service.generate_and_verify_patch(iid)

    assert execution.status == "verified" and execution.stop_reason == "verified"
    assert len(execution.attempts) == 1 and execution.final_patch_id == execution.attempts[0].proposal.patch_id
    assert not execution.original_repository_modified and snapshot_tree(average_repo).root_hash == before
    assert execution.baseline["bug_tests"] == ["test_solution.py::test_average_of_empty_list"]
    assert execution.model == "fake-model" and len(llm.calls) == 1

    (row,) = rows(db_session, iid)
    assert row.status == "verified" and row.unified_diff == AVERAGE_FIX
    assert [h["status"] for h in row.status_history] == ["generated", "validated", "applied", "verifying", "verified"]
    assert row.validation_result["valid"] and row.diff_sha256
    assert row.provenance["model"] == "fake-model" and row.provenance["prompt_version"] == "patch-v1"
    assert row.provenance["source_snapshot_hash"] == before
    verification = db_session.query(PatchVerificationRecord).filter_by(patch_id=row.id).one()
    assert verification.status == "verified" and {c["name"] for c in verification.checks} >= {
        "patch_application", "syntax_check", "original_bug_test", "regression_tests", "ruff", "full_test_suite"}
    assert verification.before_results["bug_tests"] and verification.after_results["targeted"]
    assert verification.environment["snapshot_hash"] == before


def test_retry_first_fails_second_passes_with_real_failure_feedback(build_service, manager, db_session, stored):
    iid, patches = stored()
    service, llm, _ = build_service([submit(AVERAGE_NO_FIX), submit(AVERAGE_FIX)], patches=patches,
                                    investigation_id=iid, manager=manager)
    execution = service.generate_and_verify_patch(iid)

    assert [a.status for a in execution.attempts] == ["rejected", "verified"]
    assert execution.status == "verified" and execution.final_patch_id == execution.attempts[1].proposal.patch_id
    retry_prompt = llm.calls[1]["messages"][1]["content"]
    assert '<previous_attempt number="1">' in retry_prompt
    assert "ZeroDivisionError" in retry_prompt and "max(len(numbers), 0)" in retry_prompt
    # Every attempt got a fresh copy of the original, never the previous attempt's output.
    labels = [label for label, _ in manager.created]
    assert labels == ["baseline", "patched-1", "patched-2"]
    original = manager.created[0][1]
    assert all(content == original for _, content in manager.created)

    first, second = rows(db_session, iid)
    assert (first.status, second.status) == ("rejected", "verified")
    assert first.unified_diff == AVERAGE_NO_FIX  # the failed attempt is preserved unchanged
    assert first.execution_id == second.execution_id and (first.attempt_number, second.attempt_number) == (1, 2)


def test_all_attempts_fail_and_the_limit_stops_the_loop(build_service, db_session, stored):
    iid, patches = stored()
    script = [submit(AVERAGE_NO_FIX), submit(AVERAGE_REGRESSION), submit(AVERAGE_SYNTAX_ERROR), submit(AVERAGE_FIX)]
    service, llm, _ = build_service(script, patches=patches, investigation_id=iid)
    execution = service.generate_and_verify_patch(iid, max_attempts=3)

    assert execution.status == "rejected" and execution.stop_reason == "max_attempts"
    assert len(llm.calls) == 3 and len(execution.attempts) == 3
    assert execution.summary.startswith("Stopped after the maximum of 3 attempt(s)")
    assert execution.final_patch_id == execution.attempts[-1].proposal.patch_id
    assert "syntax_check" in execution.attempts[-1].summary
    stored_rows = rows(db_session, iid)
    assert [r.status for r in stored_rows] == ["rejected", "rejected", "rejected"]
    assert len({r.id for r in stored_rows}) == 3
    assert db_session.query(PatchVerificationRecord).filter(
        PatchVerificationRecord.patch_id.in_([r.id for r in stored_rows])).count() == 3


def test_regression_breaking_patch_is_rejected(build_service):
    service, _, iid = build_service([submit(AVERAGE_REGRESSION)])
    execution = service.generate_and_verify_patch(iid, max_attempts=1)
    assert execution.status == "rejected"
    assert execution.attempts[0].verification.check("regression_tests").status == "failed"


@pytest.mark.parametrize(
    ("diff", "fragment"),
    [
        (AVERAGE_TEST_TAMPER, "modifies a test"),
        (AVERAGE_UNRELATED, "README.md"),
        (AVERAGE_NOOP, "does not change"),
        ("this is not a diff", "outside a hunk"),
        (AVERAGE_FIX.replace("a/solution.py", "a/../solution.py").replace("b/solution.py", "b/../solution.py"),
         "traversal"),
    ],
)
def test_invalid_patches_end_as_validation_failed_without_any_workspace(build_service, manager, diff, fragment):
    service, _, iid = build_service([submit(diff)], manager=manager)
    execution = service.generate_and_verify_patch(iid, max_attempts=1)
    assert execution.status == "validation_failed"
    attempt = execution.attempts[0]
    assert attempt.application is None and attempt.verification is None
    assert any(fragment in e for e in attempt.validation.errors), attempt.validation.errors
    assert [label for label, _ in manager.created] == ["baseline"]  # never a patched workspace


def test_validation_failure_is_fed_back_and_retried(build_service, db_session, stored):
    iid, patches = stored()
    service, llm, _ = build_service([submit(AVERAGE_TEST_TAMPER), submit(AVERAGE_FIX)], patches=patches,
                                    investigation_id=iid)
    execution = service.generate_and_verify_patch(iid)
    assert [a.status for a in execution.attempts] == ["validation_failed", "verified"]
    assert "Tests must not be changed" in llm.calls[1]["messages"][1]["content"]
    first = rows(db_session, iid)[0]
    assert [h["status"] for h in first.status_history] == ["generated", "validation_failed"]
    assert db_session.query(PatchVerificationRecord).filter_by(patch_id=first.id).count() == 0


def test_identical_patch_is_rejected_without_rerunning_tests(build_service, manager):
    service, _, iid = build_service([submit(AVERAGE_NO_FIX), submit(AVERAGE_NO_FIX)], manager=manager)
    execution = service.generate_and_verify_patch(iid, max_attempts=2)
    second = execution.attempts[1]
    assert second.status == "rejected" and second.verification.check("duplicate_patch").status == "failed"
    assert "identical to attempt 1" in second.summary
    assert [label for label, _ in manager.created] == ["baseline", "patched-1"]


def test_syntax_error_is_rejected(build_service):
    service, _, iid = build_service([submit(AVERAGE_SYNTAX_ERROR)])
    execution = service.generate_and_verify_patch(iid, max_attempts=1)
    assert execution.status == "rejected"
    assert execution.attempts[0].verification.check("syntax_check").status == "failed"


def test_baseline_not_reproducible_stops_before_spending_on_the_model(build_service, average_repo):
    (average_repo / "solution.py").write_text(
        "def calculate_average(numbers):\n    return sum(numbers) / len(numbers) if numbers else 0.0\n"
    )
    service, llm, iid = build_service([submit(AVERAGE_FIX)])
    execution = service.generate_and_verify_patch(iid)
    assert execution.status == "inconclusive" and execution.failure_reason == "baseline_not_reproducible"
    assert llm.calls == [] and execution.attempts == [] and execution.final_patch_id is None
    assert "could not be reproduced" in execution.summary


def test_baseline_not_reproducible_can_still_generate_when_configured(build_service, average_repo):
    (average_repo / "solution.py").write_text(
        "def calculate_average(numbers):\n    return sum(numbers) / len(numbers) if numbers else 0.0\n"
    )
    limits = PatchLimits(baseline_runs=1, generate_when_baseline_unreproducible=True)
    diff = ("--- a/solution.py\n+++ b/solution.py\n@@ -1,2 +1,2 @@\n def calculate_average(numbers):\n"
            "-    return sum(numbers) / len(numbers) if numbers else 0.0\n"
            "+    return sum(numbers) / len(numbers) if len(numbers) else 0.0\n")
    service, _, iid = build_service([submit(diff)], limits=limits)
    execution = service.generate_and_verify_patch(iid, max_attempts=1)
    assert execution.status == "inconclusive"  # never verified without a reproduced bug


# --- failures ----------------------------------------------------------------------------


def test_llm_timeout_fails_and_keeps_earlier_attempts(build_service, db_session, stored):
    iid, patches = stored()
    service, _, _ = build_service([submit(AVERAGE_NO_FIX), LLMTimeoutError("slow")], patches=patches,
                                  investigation_id=iid)
    execution = service.generate_and_verify_patch(iid)
    assert execution.status == "failed" and execution.failure_reason == "llm_timeout"
    assert len(execution.attempts) == 1 and execution.attempts[0].status == "rejected"
    assert [r.status for r in rows(db_session, iid)] == ["rejected"]


def test_invalid_structured_response_fails_cleanly(build_service):
    service, _, iid = build_service([final_answer("just add a check"), final_answer("really")])
    execution = service.generate_and_verify_patch(iid)
    assert execution.status == "failed" and execution.failure_reason == "llm_invalid_response"


def test_workspace_creation_failure(build_service, tmp_path):
    service, _, iid = build_service([submit(AVERAGE_FIX)], manager=RecordingManager(tmp_path / "w", fail_on="patched"))
    execution = service.generate_and_verify_patch(iid)
    assert execution.status == "failed" and execution.failure_reason == "workspace_error"
    assert execution.attempts[0].proposal.status == PatchStatus.FAILED
    service, _, iid = build_service([submit(AVERAGE_FIX)], manager=RecordingManager(tmp_path / "x", fail_on="base"))
    assert service.generate_and_verify_patch(iid).failure_reason == "workspace_error"


def test_database_error_keeps_earlier_records(build_service, db_session, stored, monkeypatch):
    iid, patches = stored()
    real = patches.record_verification
    calls = []

    def flaky(result, status):
        calls.append(status)
        if len(calls) == 2:
            raise PatchPersistenceError("Could not save patch data: OperationalError")
        return real(result, status)

    monkeypatch.setattr(patches, "record_verification", flaky)
    service, _, _ = build_service([submit(AVERAGE_NO_FIX), submit(AVERAGE_FIX)], patches=patches, investigation_id=iid)
    execution = service.generate_and_verify_patch(iid)
    assert execution.status == "failed" and execution.failure_reason == "persistence_error"
    first, second = rows(db_session, iid)
    assert first.status == "rejected"  # committed before the failure, still there
    assert second.status in ("verifying", "failed")


def test_internal_error_marks_the_patch_failed(build_service, db_session, stored, monkeypatch):
    iid, patches = stored()
    service, _, _ = build_service([submit(AVERAGE_FIX)], patches=patches, investigation_id=iid)
    monkeypatch.setattr(service._engine, "verify", lambda **_: (_ for _ in ()).throw(RuntimeError("boom")))
    execution = service.generate_and_verify_patch(iid)
    assert execution.status == "failed" and execution.failure_reason == "internal_error"
    assert "boom" not in execution.summary
    assert rows(db_session, iid)[0].status == "failed"


def test_verification_budget_exhausted_before_any_attempt(build_service):
    clock = iter(range(0, 10_000, 100))
    service, llm, iid = build_service([submit(AVERAGE_FIX)], limits=PatchLimits(baseline_runs=1, max_total_seconds=50))
    service._clock = lambda: float(next(clock))
    execution = service.generate_and_verify_patch(iid)
    assert execution.status == "inconclusive" and llm.calls == []


# --- eligibility and access ----------------------------------------------------------------


def test_only_completed_investigations_are_eligible(build_service):
    service, _, iid = build_service([submit(AVERAGE_FIX)], status="inconclusive")
    with pytest.raises(InvestigationNotEligibleError):
        service.generate_and_verify_patch(iid)


def test_investigation_without_modifiable_files_is_not_eligible(build_service):
    iid = str(uuid.uuid4())
    analysis = make_analysis(iid, affected_files=["test_solution.py", ".env", "../x.py"], evidence=[])
    service, _, _ = build_service([submit(AVERAGE_FIX)], analysis=analysis, investigation_id=iid)
    with pytest.raises(InvestigationNotEligibleError, match="no modifiable source file"):
        service.generate_and_verify_patch(iid)


def test_cross_repository_access_is_not_found(build_service):
    service, _, iid = build_service([submit(AVERAGE_FIX)])
    with pytest.raises(InvestigationNotFoundError):
        service.generate_and_verify_patch(iid, repository_id="someone-else")
    with pytest.raises(InvestigationNotFoundError):
        service.generate_and_verify_patch(str(uuid.uuid4()))


def test_untrusted_repository_is_refused(build_service, tmp_path):
    service, llm, iid = build_service([submit(AVERAGE_FIX)], trusted=[tmp_path / "elsewhere"])
    with pytest.raises(RepositoryAccessDeniedError):
        service.generate_and_verify_patch(iid)
    assert llm.calls == []


def test_attempt_limit_is_validated(build_service):
    service, _, iid = build_service([submit(AVERAGE_FIX)])
    for value in (0, 4):
        with pytest.raises(PatchRequestError):
            service.generate_and_verify_patch(iid, max_attempts=value)


def test_original_repository_is_never_modified_by_any_scenario(build_service, average_repo):
    before = snapshot_tree(average_repo).root_hash
    script = [submit(AVERAGE_TEST_TAMPER), submit(AVERAGE_REGRESSION), submit(AVERAGE_FIX)]
    service, _, iid = build_service(FakeLLM(script))
    execution = service.generate_and_verify_patch(iid)
    assert execution.status == "verified"
    assert snapshot_tree(average_repo).root_hash == before
    assert "if not numbers" not in (average_repo / "solution.py").read_text()


def test_execution_report_is_json_serialisable(build_service):
    service, _, iid = build_service([submit(AVERAGE_FIX)])
    execution = service.generate_and_verify_patch(iid)
    assert json.loads(execution.model_dump_json())["status"] == "verified"


# --- lifecycle transitions -------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ["generated", "validated", "applied", "verifying", "verified"],
        ["generated", "validated", "applied", "verifying", "rejected"],
        ["generated", "validated", "rejected"],
        ["generated", "validation_failed"],
        ["generated", "validated", "applied", "verifying", "inconclusive"],
    ],
)
def test_allowed_lifecycles(path):
    for current, new in itertools.pairwise(path):
        check_patch_transition(current, new)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        ("validation_failed", "applied"),
        ("generated", "applied"),
        ("generated", "verified"),
        ("validated", "verified"),
        ("applied", "verified"),
        ("rejected", "verified"),
        ("verified", "rejected"),
        ("verifying", "validated"),
    ],
)
def test_invalid_transitions_are_rejected(current, new):
    with pytest.raises(InvalidPatchTransitionError):
        check_patch_transition(current, new)


def test_terminal_statuses_are_final():
    for status in TERMINAL_PATCH_STATUSES:
        assert PATCH_TRANSITIONS[status] == frozenset()


def test_repository_refuses_invalid_transitions(stored):
    from app.patching.models import PatchProposal

    iid, patches = stored()
    proposal = PatchProposal(investigation_id=iid, repository_id="avg", execution_id=str(uuid.uuid4()),
                             attempt_number=1, description="d", unified_diff=AVERAGE_FIX, rationale="r")
    patches.create_proposal(proposal, {})
    patches.transition(proposal.patch_id, PatchStatus.VALIDATION_FAILED)
    with pytest.raises(InvalidPatchTransitionError):
        patches.transition(proposal.patch_id, PatchStatus.APPLIED)
    assert patches.get_proposal(proposal.patch_id).status == "validation_failed"
