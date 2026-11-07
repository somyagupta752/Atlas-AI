"""Patch generator on FakeLLM. The generator proposes; it never applies or writes."""

import json

import pytest

from app.agents.llm import (
    FakeLLM,
    LLMProviderError,
    LLMRateLimitError,
    LLMResponse,
    LLMTimeoutError,
    LLMUsage,
    final_answer,
    tool_call,
)
from app.patching.generator import (
    SUBMIT_PATCH_TOOL,
    SYSTEM_PROMPT,
    AttemptFeedback,
    PatchGenerationError,
    PatchGenerator,
    RepositoryContext,
)
from app.patching.models import PatchLimits, PatchStatus
from app.patching.validator import PatchScope, validate_patch
from app.patching.workspace import snapshot_tree
from tests.patching.conftest import (
    AVERAGE_FIX,
    AVERAGE_SYNTAX_ERROR,
    AVERAGE_TEST_TAMPER,
    AVERAGE_UNRELATED,
    make_analysis,
    submit,
)

MULTI = AVERAGE_FIX + "--- a/stats.py\n+++ b/stats.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


@pytest.fixture
def context(average_repo):
    return RepositoryContext(
        repository_id="avg",
        bug_report="The average calculation crashes when the input list is empty. It should return zero.",
        modifiable_files={"solution.py": (average_repo / "solution.py").read_text()},
        test_files={"test_solution.py": (average_repo / "test_solution.py").read_text()},
        failing_tests=[{"test_id": "test_solution.py::test_average_of_empty_list",
                        "message": "ZeroDivisionError: division by zero"}],
    )


def generate(script, context, history=None, **kwargs):
    llm = script if isinstance(script, FakeLLM) else FakeLLM(script)
    generator = PatchGenerator(llm, **kwargs)
    result = generator.generate_patch(make_analysis("11111111-1111-1111-1111-111111111111"), context,
                                      execution_id="22222222-2222-2222-2222-222222222222", attempt_number=1,
                                      history=history)
    return result, llm


def test_valid_unified_diff_becomes_a_generated_proposal(context, average_repo):
    before = snapshot_tree(average_repo).root_hash
    result, llm = generate([submit(AVERAGE_FIX)], context)
    proposal = result.proposal
    assert proposal.status == PatchStatus.GENERATED
    assert proposal.unified_diff == AVERAGE_FIX and proposal.affected_files == ["solution.py"]
    assert proposal.rationale and proposal.description and proposal.assumptions
    assert (proposal.investigation_id, proposal.attempt_number) == ("11111111-1111-1111-1111-111111111111", 1)
    assert proposal.model == "fake-model" and proposal.provider == "fake"
    assert llm.calls[0]["tools"] == [SUBMIT_PATCH_TOOL]
    assert snapshot_tree(average_repo).root_hash == before  # never applied


def test_json_text_answer_is_accepted(context):
    payload = {"description": "d", "rationale": "r", "unified_diff": AVERAGE_FIX, "affected_files": ["solution.py"],
               "assumptions": [], "risks": ["none"]}
    result, _ = generate([final_answer("```json\n" + json.dumps(payload) + "\n```")], context)
    assert result.proposal.risks == ["none"]


def test_multiple_file_patch(context):
    result, _ = generate([submit(MULTI, files=["solution.py", "stats.py"])], context)
    assert result.proposal.affected_files == ["solution.py", "stats.py"]


@pytest.mark.parametrize("diff", [AVERAGE_SYNTAX_ERROR, AVERAGE_TEST_TAMPER, AVERAGE_UNRELATED, "not a diff"])
def test_generator_returns_bad_patches_for_the_validator_to_judge(context, diff):
    """Malformed, test-tampering or unrelated diffs are still proposals; validation is a separate step."""
    result, _ = generate([submit(diff)], context)
    assert result.proposal.unified_diff == diff


def test_malformed_and_invalid_path_diffs_fail_validation(context, average_repo):
    for diff in ("not a diff", AVERAGE_FIX.replace("a/solution.py", "a/../../etc/solution.py")):
        result, _ = generate([submit(diff)], context)
        validation, patch = validate_patch(result.proposal.unified_diff,
                                           PatchScope(average_repo, frozenset({"solution.py"})), PatchLimits())
        assert not validation.valid and patch is None


def test_empty_patch_is_repaired_once_then_fails(context):
    with pytest.raises(PatchGenerationError) as info:
        generate([submit("   "), submit("")], context)
    assert info.value.kind == "invalid_response" and "unified_diff" in str(info.value)


def test_invalid_structured_response_is_repaired_with_a_tool_error(context):
    bad = tool_call("submit_patch", {"description": "d", "unified_diff": AVERAGE_FIX, "extra": 1}, "call_bad")
    result, llm = generate([bad, submit(AVERAGE_FIX)], context)
    assert result.llm_calls == 2
    repair = llm.calls[1]["messages"][-1]
    assert repair["role"] == "tool" and repair["tool_call_id"] == "call_bad" and repair["is_error"]
    assert "rationale" in repair["content"] and "extra" in repair["content"]


@pytest.mark.parametrize(
    "response",
    [
        final_answer("I think you should add a check."),
        tool_call("run_tests", {"test_path": "test_solution.py"}),
        tool_call("submit_patch", "not an object"),
        LLMResponse(text="partial", finish_reason="length"),
    ],
)
def test_unusable_responses_raise_invalid_response(context, response):
    with pytest.raises(PatchGenerationError) as info:
        generate([response, response], context)
    assert info.value.kind == "invalid_response"


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (LLMTimeoutError("t"), "timeout"),
        (LLMRateLimitError("r"), "rate_limited"),
        (LLMProviderError("p", status_code=500, retryable=True), "provider_error"),
    ],
)
def test_provider_errors(context, error, kind):
    with pytest.raises(PatchGenerationError) as info:
        generate([error], context)
    assert info.value.kind == kind


def test_refusal(context):
    with pytest.raises(PatchGenerationError) as info:
        generate([LLMResponse(text="no", finish_reason="refusal")], context)
    assert info.value.kind == "refusal"


def test_timeout_is_passed_to_the_provider(context):
    _, llm = generate([submit(AVERAGE_FIX)], context, timeout_seconds=42)
    assert llm.calls[0]["timeout"] == 42


def test_prompt_contains_rules_context_and_untrusted_data_warning(context):
    _, llm = generate([submit(AVERAGE_FIX)], context)
    system, user = llm.calls[0]["messages"][0]["content"], llm.calls[0]["messages"][1]["content"]
    for rule in ("smallest reasonable change", "Never change tests", "Do not refactor", "configuration",
                 "unified diff", "assumptions", "not instructions"):
        assert rule in system
    assert system == SYSTEM_PROMPT
    assert "<bug_report>" in user and "It should return zero." in user
    assert "Root cause: calculate_average divides by zero" in user
    assert '<file path="solution.py" modifiable="true">' in user and "  1| def calculate_average" in user
    assert '<file path="test_solution.py" modifiable="false">' in user
    assert "test_average_of_empty_list: ZeroDivisionError" in user
    assert "a hint, not a requirement" in user


def test_secrets_in_source_are_redacted_from_the_prompt(context):
    context.modifiable_files["solution.py"] += 'API_KEY = "sk-ant-abcdefghijklmnopqrstuvwxyz"\n'
    _, llm = generate([submit(AVERAGE_FIX)], context)
    assert "sk-ant-abcdefghijklmnop" not in json.dumps(llm.calls[0]["messages"])


def test_history_carries_the_previous_failure(context):
    history = [AttemptFeedback(1, "rejected", AVERAGE_FIX.replace("0.0", "None"),
                               ["original_bug_test: test_average_of_empty_list still failed: assert None == 0.0"],
                               "[original_bug_test] ...\nE   assert None == 0.0")]
    _, llm = generate([submit(AVERAGE_FIX)], context, history=history)
    user = llm.calls[0]["messages"][1]["content"]
    assert '<previous_attempt number="1">' in user and "assert None == 0.0" in user and "return None" in user


def test_usage_is_accumulated_across_repair(context):
    first = tool_call("submit_patch", {}, "c1")
    first.usage = LLMUsage(input_tokens=100, output_tokens=10)
    second = submit(AVERAGE_FIX)
    second.usage = LLMUsage(input_tokens=200, output_tokens=20)
    result, _ = generate([first, second], context)
    assert result.proposal.usage.input_tokens == 300 and result.proposal.usage.output_tokens == 30
