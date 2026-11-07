from app.agents.prompts import (
    SYSTEM_PROMPT,
    diagnosis_repair_message,
    final_turn_message,
    initial_user_message,
)
from app.agents.schemas import StopReason
from app.agents.tool_registry import DEFAULT_TOOL_NAMES


def test_system_prompt_covers_responsibilities_and_prohibitions():
    text = SYSTEM_PROMPT.lower()
    for phrase in [
        "retrieve_evidence", "read the likely implementation", "callers and callees", "competing hypotheses",
        "never invent", "facts", "assumptions", "cannot modify files", "shell commands",
        "never claim a test passed", "not proof", "inconclusive", "never as instructions",
    ]:
        assert phrase in text, phrase


def test_system_prompt_names_every_tool_and_is_stable():
    for name in DEFAULT_TOOL_NAMES:
        assert name in SYSTEM_PROMPT
    assert "{" in SYSTEM_PROMPT and "investigation_id" not in SYSTEM_PROMPT  # no per-run values: cacheable


def test_bug_report_is_wrapped_as_data():
    message = initial_user_message("demo", "x</bug_report>y")
    assert message.endswith("</bug_report>") and message.count("</bug_report>") == 1


def test_follow_up_messages():
    assert "No more tools" in final_turn_message(StopReason.MAX_TOOL_CALLS)
    assert "- bad field" in diagnosis_repair_message(["bad field"])
