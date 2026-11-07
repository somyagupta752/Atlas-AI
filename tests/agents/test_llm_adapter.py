"""The Anthropic adapter against a fake SDK client. No network, no API key."""

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app.agents.llm import (
    AnthropicProvider,
    FakeLLM,
    LLMConfigurationError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
    create_llm_provider,
    final_answer,
    from_anthropic_response,
    to_anthropic_request,
    tool_call,
)
from app.config import Settings

TOOLS = [{"name": "read_file", "description": "Reads a file.", "parameters": {"type": "object", "properties": {}}}]


class Block(SimpleNamespace):
    def to_dict(self):
        return dict(self.__dict__)


def message(blocks, stop_reason="end_turn"):
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        model="claude-opus-5-5",
        _request_id="req_1",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=3, cache_creation_input_tokens=0),
    )


class FakeClient:
    def __init__(self, result):
        self.result = result
        self.requests = []
        self.options = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **request):
        self.requests.append(request)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def with_options(self, **options):
        self.options.append(options)
        return self


def provider(result, **kwargs):
    client = FakeClient(result)
    return AnthropicProvider(model="claude-opus-5-5", client=client, **kwargs), client


def status_error(cls, status, headers=None):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, headers=headers or {"request-id": "req_9"}, request=request)
    return cls("error body with secrets", response=response, body={"error": {"message": "hidden"}})


def test_tool_call_response_is_translated():
    thinking = Block(type="thinking", thinking="", signature="sig")
    use = Block(type="tool_use", id="toolu_1", name="read_file", input={"file_path": "a.py"})
    llm, client = provider(message([thinking, Block(type="text", text="Reading."), use], "tool_use"))

    response = llm.generate([{"role": "user", "content": "go"}], TOOLS)

    assert response.finish_reason == "tool_calls"
    assert response.text == "Reading."
    assert response.tool_calls[0].model_dump() == {"id": "toolu_1", "name": "read_file", "arguments": {"file_path": "a.py"}}
    assert response.usage.input_tokens == 10 and response.request_id == "req_1"
    assert response.provider_blocks[0]["type"] == "thinking"  # kept for the next turn
    request = client.requests[0]
    assert request["model"] == "claude-opus-5-5" and request["tools"][0]["input_schema"] == TOOLS[0]["parameters"]
    assert request["fallbacks"] == "default" and request["betas"] == ["server-side-fallback-2026-07-01"]
    assert request["output_config"] == {"effort": "high"}
    assert "tool_choice" not in request


def test_final_text_and_refusal():
    llm, _ = provider(message([Block(type="text", text="{}")]))
    assert llm.generate([{"role": "user", "content": "go"}], []).finish_reason == "stop"
    llm, _ = provider(message([], "refusal"))
    assert llm.generate([{"role": "user", "content": "go"}], []).finish_reason == "refusal"


def test_tool_choice_none_and_timeout_are_passed():
    llm, client = provider(message([Block(type="text", text="x")]))
    llm.generate([{"role": "user", "content": "go"}], TOOLS, timeout=7, tool_choice="none")
    assert client.requests[0]["tool_choice"] == {"type": "none"}
    assert client.options[0]["timeout"].read == 7


def test_conversation_translation():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "provider_blocks": None,
         "tool_calls": [{"id": "a", "name": "read_file", "arguments": {}}, {"id": "b", "name": "x", "arguments": "bad"}]},
        {"role": "tool", "tool_call_id": "a", "name": "read_file", "content": "{}", "is_error": False},
        {"role": "tool", "tool_call_id": "b", "name": "x", "content": "err", "is_error": True},
        {"role": "user", "content": "answer now"},
    ]
    system, out, _ = to_anthropic_request(messages, [])

    assert system == "sys"
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[1]["content"][1] == {"type": "tool_use", "id": "b", "name": "x", "input": {}}
    results = out[2]["content"]
    assert [b["type"] for b in results] == ["tool_result", "tool_result", "text"]
    assert results[1]["is_error"] is True and "is_error" not in results[0]


def test_provider_blocks_are_sent_back_unchanged():
    blocks = [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "tool_use", "id": "a", "name": "t", "input": {}}]
    _, out, _ = to_anthropic_request(
        [{"role": "user", "content": "go"}, {"role": "assistant", "content": None, "tool_calls": [], "provider_blocks": blocks}], []
    )
    assert out[1]["content"] == blocks


@pytest.mark.parametrize(
    "bad",
    [
        SimpleNamespace(content=None, stop_reason="end_turn"),
        message([Block(type="text", text="x")], "tool_use"),
        message([Block(type="tool_use", id="", name="read_file", input={})], "tool_use"),
    ],
)
def test_invalid_responses(bad):
    with pytest.raises(LLMInvalidResponseError):
        from_anthropic_response(bad)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (anthropic.APITimeoutError(request=httpx2.Request("POST", "https://x")), LLMTimeoutError),
        (status_error(anthropic.RateLimitError, 429, {"retry-after": "12", "request-id": "r"}), LLMRateLimitError),
        (status_error(anthropic.AuthenticationError, 401), LLMConfigurationError),
        (status_error(anthropic.InternalServerError, 500), LLMProviderError),
        (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")), LLMProviderError),
    ],
)
def test_provider_errors_are_mapped_without_leaking(error, expected):
    llm, _ = provider(error)
    with pytest.raises(expected) as info:
        llm.generate([{"role": "user", "content": "go"}], [])
    assert "secrets" not in str(info.value) and "hidden" not in str(info.value)
    if expected is LLMRateLimitError:
        assert info.value.retry_after == 12 and info.value.retryable


def test_server_error_is_retryable():
    llm, _ = provider(status_error(anthropic.InternalServerError, 503))
    with pytest.raises(LLMProviderError) as info:
        llm.generate([{"role": "user", "content": "go"}], [])
    assert info.value.retryable and info.value.status_code == 503


def test_factory_requires_consent_and_never_needs_a_hardcoded_key():
    base = {"database_url": "postgresql://x"}
    with pytest.raises(LLMConfigurationError, match="LLM_ALLOW_EXTERNAL"):
        create_llm_provider(Settings(**base, llm_allow_external=False, _env_file=None))
    with pytest.raises(LLMConfigurationError, match="disabled"):
        create_llm_provider(Settings(**base, llm_provider="disabled", _env_file=None))
    llm = create_llm_provider(Settings(**base, llm_allow_external=True, anthropic_api_key="sk-ant-test", _env_file=None))
    assert llm.model == "claude-opus-5-5"
    assert "sk-ant-test" not in repr(Settings(**base, anthropic_api_key="sk-ant-test", _env_file=None))


def test_fake_llm_script_and_errors():
    fake = FakeLLM([tool_call("a", {"x": 1}), lambda m, t: final_answer(str(len(m))), LLMTimeoutError("t")])
    assert fake.generate([], []).tool_calls[0].name == "a"
    assert fake.generate([{"role": "user", "content": "q"}], []).text == "1"
    with pytest.raises(LLMTimeoutError):
        fake.generate([], [])
    with pytest.raises(LLMInvalidResponseError, match="exhausted"):
        fake.generate([], [])
    assert len(fake.calls) == 4
