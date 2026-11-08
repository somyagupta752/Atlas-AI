"""The global LLM budget: requests, model time and cost across every attempt and repair call."""

import threading
import uuid

import pytest

from app.agents.llm import FakeLLM, LLMTimeoutError, LLMUsage, final_answer
from app.patching.budget import (
    LLMBudget,
    LLMBudgetExhaustedError,
    LLMPricingUnknownError,
    estimate_input_tokens,
    model_prices,
    usage_cost,
    worst_case_cost,
)
from app.patching.generator import (
    PatchGenerationError,
    PatchGenerator,
    RepositoryContext,
)
from app.patching.models import PatchLimits
from tests.patching.conftest import (
    AVERAGE_FIX,
    AVERAGE_NO_FIX,
    AVERAGE_REGRESSION,
    make_analysis,
    submit,
)

OPUS = "claude-opus-5-5"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def budget(**kwargs):
    values = {"max_requests": 10, "max_seconds": 100.0, "max_cost_usd": 1.0, **kwargs}
    return LLMBudget(**values)


def priced(script, max_tokens=1000):
    """A FakeLLM priced as Opus, so costs are real numbers."""
    return FakeLLM(script, model=OPUS, pricing=None, max_tokens=max_tokens)


def costing(response, input_tokens=10_000, output_tokens=500):
    response.usage = LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens)
    return response


# --- accounting ----------------------------------------------------------------------------


def test_reserve_and_settle_charge_the_actual_cost():
    b = budget()
    r = b.reserve(estimated_cost=0.30, timeout=60, label="a")
    assert b.remaining_cost() == pytest.approx(0.70) and b.requests == 1
    record = b.settle(r, actual_cost=0.05, outcome="ok", model=OPUS)
    assert b.remaining_cost() == pytest.approx(0.95)
    assert record["estimated_cost_usd"] == 0.3 and record["actual_cost_usd"] == 0.05
    assert not record["cost_estimate_exceeded"]
    with pytest.raises(ValueError):
        b.settle(r, actual_cost=0.05, outcome="ok")


def test_request_limit():
    b = budget(max_requests=2)
    for _ in range(2):
        b.settle(b.reserve(estimated_cost=0.01, timeout=10), actual_cost=0.01, outcome="ok")
    with pytest.raises(LLMBudgetExhaustedError) as info:
        b.reserve(estimated_cost=0.01, timeout=10)
    assert info.value.limit == "requests" and b.exhausted_by == "requests"


def test_cost_limit_refuses_a_call_that_could_exceed_the_remainder():
    b = budget(max_cost_usd=0.50)
    b.settle(b.reserve(estimated_cost=0.30, timeout=10), actual_cost=0.25, outcome="ok")
    with pytest.raises(LLMBudgetExhaustedError) as info:
        b.reserve(estimated_cost=0.30, timeout=10)
    assert info.value.limit == "cost" and "$0.2500" in str(info.value)
    assert b.requests == 1  # the refused call was never counted as made


def test_time_limit_clips_the_timeout_then_refuses():
    clock = Clock()
    b = LLMBudget(max_requests=10, max_seconds=100, max_cost_usd=None, clock=clock)
    r = b.reserve(estimated_cost=None, timeout=60)
    clock.now = 70
    b.settle(r, actual_cost=None, outcome="ok")
    second = b.reserve(estimated_cost=None, timeout=60)
    assert second.seconds == pytest.approx(30) and second.clipped
    clock.now = 100
    b.settle(second, actual_cost=None, outcome="timeout")
    with pytest.raises(LLMBudgetExhaustedError) as info:
        b.reserve(estimated_cost=None, timeout=60)
    assert info.value.limit == "seconds"


def test_unknown_price_with_a_cost_cap_refuses_and_without_one_runs():
    with pytest.raises(LLMPricingUnknownError):
        budget().reserve(estimated_cost=None, timeout=10)
    uncapped = budget(max_cost_usd=None)
    uncapped.settle(uncapped.reserve(estimated_cost=None, timeout=10), actual_cost=None, outcome="ok")
    assert model_prices(FakeLLM([], model="mystery")) is not None  # a fake declares a zero price
    assert model_prices(FakeLLM([], model="mystery", pricing=None)) is None


def test_actual_cost_above_the_estimate_is_flagged_and_charged():
    b = budget(max_cost_usd=0.10)
    record = b.settle(b.reserve(estimated_cost=0.02, timeout=10), actual_cost=0.08, outcome="ok")
    assert record["cost_estimate_exceeded"] and b.report()["cost_estimate_exceeded"]
    with pytest.raises(LLMBudgetExhaustedError):
        b.reserve(estimated_cost=0.03, timeout=10)


def test_a_failed_call_without_usage_keeps_its_reservation_as_spent():
    b = budget()
    b.settle(b.reserve(estimated_cost=0.40, timeout=10), actual_cost=None, outcome="error")
    assert b.remaining_cost() == pytest.approx(0.60)
    assert b.report()["calls"][0]["charged_cost_usd"] == 0.4


def test_reservations_stop_two_calls_from_sharing_the_remainder():
    b = budget(max_cost_usd=0.50)
    b.reserve(estimated_cost=0.30, timeout=10)  # in flight, not settled
    with pytest.raises(LLMBudgetExhaustedError):
        b.reserve(estimated_cost=0.30, timeout=10)


def test_concurrent_reservations_never_overspend():
    b = budget(max_cost_usd=1.0, max_requests=1000)
    granted, refused = [], []
    start = threading.Barrier(20)

    def claim():
        start.wait()
        try:
            granted.append(b.reserve(estimated_cost=0.15, timeout=10))
        except LLMBudgetExhaustedError:
            refused.append(1)

    threads = [threading.Thread(target=claim) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(granted) == 6 and len(refused) == 14  # 6 x 0.15 = 0.90 <= 1.00 < 7 x 0.15


def test_parent_budget_is_shared_and_a_refusal_releases_the_child():
    parent = budget(max_cost_usd=0.50)
    first, second = budget(parent=parent), budget(parent=parent)
    first.settle(first.reserve(estimated_cost=0.30, timeout=10), actual_cost=0.30, outcome="ok")
    with pytest.raises(LLMBudgetExhaustedError):
        second.reserve(estimated_cost=0.30, timeout=10)
    assert second.requests == 0 and second.remaining_cost() == pytest.approx(1.0)
    assert parent.cost_spent == pytest.approx(0.30)
    parent.charge(0.15, "investigation")
    assert parent.remaining_cost() == pytest.approx(0.05)


def test_cost_helpers():
    prices = {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0}
    assert worst_case_cost(prices, 1_000_000, 0) == pytest.approx(5.0)  # the highest input rate
    assert worst_case_cost(prices, 0, 16_000) == pytest.approx(0.32)
    assert usage_cost(prices, LLMUsage(input_tokens=1_000_000, output_tokens=1_000_000)) == pytest.approx(24.0)
    assert estimate_input_tokens([{"role": "user", "content": "x" * 2500}], []) >= 1000


# --- generator: three different stops ---------------------------------------------------------


@pytest.fixture
def context(average_repo):
    return RepositoryContext(repository_id="avg", bug_report="crash",
                             modifiable_files={"solution.py": (average_repo / "solution.py").read_text()})


def generate(llm, context, b):
    return PatchGenerator(llm).generate_patch(make_analysis(str(uuid.uuid4())), context,
                                              execution_id=str(uuid.uuid4()), attempt_number=1, budget=b)


def test_budget_exhausted_before_a_call_never_calls_the_model(context):
    llm = priced([submit(AVERAGE_FIX)], max_tokens=16_000)
    with pytest.raises(PatchGenerationError) as info:
        generate(llm, context, budget(max_cost_usd=0.01))
    assert info.value.kind == "budget_exhausted" and info.value.budget_limit == "cost"
    assert llm.calls == []


def test_model_timeout_is_reported_as_a_timeout(context):
    b = budget(max_seconds=1000)  # room for the full 120 s per-call timeout
    with pytest.raises(PatchGenerationError) as info:
        generate(priced([LLMTimeoutError("slow")]), context, b)
    assert info.value.kind == "timeout"
    assert b.report()["calls"][0]["outcome"] == "timeout"


def test_timeout_caused_by_the_time_budget_is_reported_as_budget(context):
    clock = Clock()
    b = LLMBudget(max_requests=10, max_seconds=30, max_cost_usd=1.0, clock=clock)
    llm = priced([LLMTimeoutError("cut short")])
    with pytest.raises(PatchGenerationError) as info:
        generate(llm, context, b)
    assert info.value.kind == "budget_exhausted" and info.value.budget_limit == "seconds"
    assert llm.calls[0]["timeout"] == 30  # the per-call 120 s timeout was clipped to the budget


def test_repair_calls_count_and_actual_costs_are_recorded(context):
    b = budget()
    llm = priced([costing(final_answer("not json")), costing(submit(AVERAGE_FIX))])
    generate(llm, context, b)
    report = b.report()
    assert report["requests"] == 2 and [c["outcome"] for c in report["calls"]] == ["ok", "ok"]
    expected = usage_cost(model_prices(llm), LLMUsage(input_tokens=10_000, output_tokens=500))
    assert report["calls"][0]["actual_cost_usd"] == pytest.approx(expected, abs=1e-6)
    # The scripted usage (10k input tokens) is larger than this small prompt could produce: detected.
    assert report["calls"][0]["cost_estimate_exceeded"] and report["cost_estimate_exceeded"]
    assert report["cost_spent_usd"] == pytest.approx(2 * expected, abs=1e-6)


# --- service ---------------------------------------------------------------------------------


def test_request_limit_stops_after_a_rejected_attempt_without_reporting_failure(build_service):
    llm = priced([costing(submit(AVERAGE_NO_FIX)), costing(submit(AVERAGE_FIX))])
    service, _, iid = build_service(llm, limits=PatchLimits(baseline_runs=1, max_llm_requests=1))
    execution = service.generate_and_verify_patch(iid)
    assert execution.status == "rejected" and execution.stop_reason == "llm_budget_exhausted"
    assert execution.failure_reason != "llm_timeout" and "LLM budget (requests)" in execution.summary
    assert len(execution.attempts) == 1 and len(llm.calls) == 1
    assert execution.llm_budget["requests"] == 1 and execution.llm_budget["exhausted_by"] == "requests"
    assert execution.usage.input_tokens == 10_000  # the earlier attempt's usage is kept


def test_cost_cap_too_small_for_any_call_is_inconclusive(build_service):
    llm = priced([submit(AVERAGE_FIX)], max_tokens=16_000)
    service, _, iid = build_service(llm, limits=PatchLimits(baseline_runs=1, max_llm_cost_usd=0.01))
    execution = service.generate_and_verify_patch(iid)
    assert execution.status == "inconclusive" and execution.failure_reason == "llm_budget_exhausted"
    assert execution.attempts == [] and llm.calls == []


def test_unknown_price_refuses_before_any_work(build_service, manager_spy):
    llm = FakeLLM([submit(AVERAGE_FIX)], model="unpriced-model", pricing=None)
    service, _, iid = build_service(llm, manager=manager_spy)
    with pytest.raises(LLMPricingUnknownError):
        service.generate_and_verify_patch(iid)
    assert llm.calls == [] and manager_spy.created == []
    uncapped, _, iid = build_service(llm, limits=PatchLimits(baseline_runs=1, max_llm_cost_usd=None))
    assert uncapped.generate_and_verify_patch(iid, max_attempts=1).status == "verified"


def test_shared_budget_across_executions(build_service, average_repo):
    parent = LLMBudget(max_requests=100, max_seconds=1000, max_cost_usd=0.03)
    script = [costing(submit(AVERAGE_FIX), input_tokens=5_000, output_tokens=0)]
    first, _, iid1 = build_service(priced(script, max_tokens=500))
    first._shared_budget = parent
    assert first.generate_and_verify_patch(iid1).status == "verified"
    second, llm, iid2 = build_service(priced(list(script), max_tokens=500))
    second._shared_budget = parent
    execution = second.generate_and_verify_patch(iid2)
    assert execution.stop_reason == "llm_budget_exhausted" and llm.calls == []


def test_budget_report_is_stored_with_each_proposal(build_service, stored_investigation):
    iid, patches, rows = stored_investigation()
    llm = priced([costing(submit(AVERAGE_REGRESSION)), costing(submit(AVERAGE_FIX))])
    service, _, _ = build_service(llm, patches=patches, investigation_id=iid)
    assert service.generate_and_verify_patch(iid).status == "verified"
    first, second = rows()
    assert first.provenance["llm_budget"]["requests"] == 1 and second.provenance["llm_budget"]["requests"] == 2


@pytest.fixture
def manager_spy(tmp_path):
    from tests.patching.test_patching_service import RecordingManager

    return RecordingManager(tmp_path / "spy")


@pytest.fixture
def stored_investigation(db_session):
    from app.models import InvestigationRecord, PatchProposalRecord
    from app.patching.repository import PatchRepository

    def make():
        iid = str(uuid.uuid4())
        db_session.add(InvestigationRecord(id=uuid.UUID(iid), repository_id="avg", bug_report="crash",
                                           status="completed"))
        db_session.commit()

        def rows():
            return list(db_session.query(PatchProposalRecord).filter_by(investigation_id=uuid.UUID(iid))
                        .order_by(PatchProposalRecord.attempt_number))

        return iid, PatchRepository(db_session), rows

    return make
