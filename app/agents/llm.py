"""Provider-independent LLM interface, one real adapter (Anthropic) and a scripted fake.

The agent loop only ever sees the types defined here. Provider SDK objects are
translated at this boundary, in both directions, and never leak out.

Internal message format (plain JSON-compatible dicts, so state can be persisted):

    {"role": "system", "content": "..."}                      # first message only
    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "..." | None,
     "tool_calls": [{"id": "...", "name": "...", "arguments": {...}}],
     "provider_blocks": [...] | None}                         # opaque, see below
    {"role": "tool", "tool_call_id": "...", "name": "...",
     "content": "...", "is_error": bool}

``provider_blocks`` is the adapter's own serialization of the assistant turn.
Some providers require parts of a turn (for Anthropic, thinking blocks) to be
sent back unchanged. The loop stores and returns it without reading it.

Tool definitions are ``{"name", "description", "parameters"}`` with a JSON
Schema in ``parameters``.
"""

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

FinishReason = Literal["stop", "tool_calls", "length", "refusal", "other"]
# "none": tools stay defined (earlier turns reference them) but none may be called.
ToolChoice = Literal["auto", "none"]


class ToolCall(BaseModel):
    """One tool call requested by the model. ``arguments`` is untrusted input."""

    id: str
    name: str
    # Normally a dict. Anything else (a string of malformed JSON, a list) is kept
    # as-is so the executor can reject it explicitly instead of the adapter guessing.
    arguments: Any = Field(default_factory=dict)


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: "LLMUsage") -> "LLMUsage":
        return LLMUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
        )


class LLMResponse(BaseModel):
    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: FinishReason = "stop"
    provider: str = "unknown"
    model: str | None = None
    request_id: str | None = None
    usage: LLMUsage = Field(default_factory=LLMUsage)
    # Opaque provider serialization of the assistant turn; see the module docstring.
    provider_blocks: list[dict[str, Any]] | None = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    @property
    def is_empty(self) -> bool:
        return not self.tool_calls and not (self.text and self.text.strip())

    def to_message(self) -> dict[str, Any]:
        """The assistant message to append to the conversation."""
        return {
            "role": "assistant",
            "content": self.text,
            "tool_calls": [call.model_dump() for call in self.tool_calls],
            "provider_blocks": self.provider_blocks,
        }


# --- errors -------------------------------------------------------------------
# Messages never contain credentials, request bodies or repository content.


class LLMError(Exception):
    """Base class. ``retryable`` tells the caller whether a later attempt may succeed."""

    retryable = False
    kind = "provider_error"


class LLMConfigurationError(LLMError):
    """The provider is not configured, or its credentials were rejected."""

    kind = "configuration_error"


class LLMTimeoutError(LLMError):
    retryable = True
    kind = "timeout"


class LLMRateLimitError(LLMError):
    retryable = True
    kind = "rate_limited"

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMProviderError(LLMError):
    """The provider failed: 5xx, overload, connection error, or a rejected request."""

    def __init__(self, message: str, status_code: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class LLMInvalidResponseError(LLMError):
    """The provider answered, but the response cannot be interpreted."""

    kind = "invalid_response"


class LLMProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        timeout: float | None = None,
        tool_choice: ToolChoice = "auto",
    ) -> LLMResponse:
        """One model turn. Raises ``LLMError`` subclasses only.

        ``timeout`` caps this call (seconds). ``tool_choice="none"`` forbids tool calls.
        """
        ...


# --- fake ---------------------------------------------------------------------


@dataclass
class FakeLLM:
    """Deterministic, scripted provider for tests. Spends nothing, calls nothing.

    Each script step is one of:
    - an ``LLMResponse``: returned as-is;
    - an ``Exception`` instance: raised (provider errors, timeouts);
    - a callable ``(messages, tools) -> LLMResponse | Exception``: for steps that
      depend on earlier tool results, such as citing an observation ID.

    Every request is recorded in ``calls`` so tests can assert what the model saw.
    """

    script: list[Any]
    name: str = "fake"
    model: str = "fake-model"
    repeat_last: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)
    # A fake costs nothing. Set to None to price ``model`` from the real pricing table.
    pricing: dict[str, float] | None = field(
        default_factory=lambda: {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    )
    max_tokens: int = 1024

    def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        timeout: float | None = None,
        tool_choice: ToolChoice = "auto",
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": json.loads(json.dumps(messages, default=str)),
                "tools": tools,
                "timeout": timeout,
                "tool_choice": tool_choice,
            }
        )
        index = len(self.calls) - 1
        if index >= len(self.script):
            if not self.repeat_last or not self.script:
                raise LLMInvalidResponseError("FakeLLM script exhausted")
            index = len(self.script) - 1
        step = self.script[index]
        if callable(step) and not isinstance(step, (LLMResponse, BaseException)):
            step = step(messages, tools)
        if isinstance(step, BaseException):
            raise step
        if not isinstance(step, LLMResponse):
            raise LLMInvalidResponseError(f"FakeLLM step {index} is not an LLMResponse")
        return step.model_copy(update={"provider": self.name, "model": self.model}, deep=True)


def tool_call(name: str, arguments: Any = None, call_id: str | None = None, text: str | None = None) -> LLMResponse:
    """A one-call ``LLMResponse``, for scripts."""
    return tool_calls([(name, arguments if arguments is not None else {}, call_id)], text=text)


def tool_calls(calls: Iterable[tuple[str, Any, str | None]], text: str | None = None) -> LLMResponse:
    built = [
        ToolCall(id=call_id or f"call_{index}_{name}", name=name, arguments=arguments)
        for index, (name, arguments, call_id) in enumerate(calls)
    ]
    return LLMResponse(text=text, tool_calls=built, finish_reason="tool_calls")


def final_answer(text: str) -> LLMResponse:
    return LLMResponse(text=text, finish_reason="stop")


# --- Anthropic ----------------------------------------------------------------

ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"

_STOP_REASONS: dict[str | None, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "refusal": "refusal",
}


def to_anthropic_request(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """Translate internal messages and tools into ``(system, messages, tools)``.

    Consecutive ``tool`` messages become one ``user`` message of ``tool_result``
    blocks, as the API requires: every ``tool_use`` gets its result in the next
    user turn. A ``user`` message that directly follows tool results is merged
    into the same turn as a trailing text block.
    """
    system: str | None = None
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            if out or system is not None:
                raise ValueError("a system message is only allowed first")
            system = message["content"]
        elif role == "user":
            block = {"type": "text", "text": message["content"]}
            if out and out[-1]["role"] == "user":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif role == "assistant":
            out.append({"role": "assistant", "content": _assistant_blocks(message)})
        elif role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message["tool_call_id"],
                "content": message["content"],
            }
            if message.get("is_error"):
                block["is_error"] = True
            if out and out[-1]["role"] == "user" and _only_tool_results(out[-1]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        else:
            raise ValueError(f"unknown message role: {role!r}")
    anthropic_tools = [
        {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
        for t in tools
    ]
    return system, out, anthropic_tools


def _assistant_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    if message.get("provider_blocks"):
        # Sent back unchanged: thinking blocks must not be edited or dropped.
        return message["provider_blocks"]
    blocks: list[dict[str, Any]] = []
    if message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        arguments = call["arguments"] if isinstance(call["arguments"], dict) else {}
        blocks.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": arguments})
    return blocks


def _only_tool_results(message: dict[str, Any]) -> bool:
    return all(block["type"] == "tool_result" for block in message["content"])


def from_anthropic_response(message: Any) -> LLMResponse:
    """Translate an SDK ``Message`` into an ``LLMResponse``. Raises ``LLMInvalidResponseError``."""
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        raise LLMInvalidResponseError("Response has no content list")
    texts: list[str] = []
    calls: list[ToolCall] = []
    blocks: list[dict[str, Any]] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            texts.append(block.text)
        elif block_type == "tool_use":
            if not getattr(block, "id", None) or not getattr(block, "name", None):
                raise LLMInvalidResponseError("tool_use block without id or name")
            calls.append(ToolCall(id=block.id, name=block.name, arguments=block.input))
        blocks.append(_block_to_dict(block))

    stop_reason = getattr(message, "stop_reason", None)
    finish = _STOP_REASONS.get(stop_reason, "other")
    if finish == "tool_calls" and not calls:
        raise LLMInvalidResponseError("stop_reason is tool_use but no tool_use block was returned")
    if calls and finish == "stop":
        finish = "tool_calls"

    usage = getattr(message, "usage", None)
    return LLMResponse(
        text="\n".join(texts) if texts else None,
        tool_calls=calls,
        finish_reason=finish,
        provider="anthropic",
        model=getattr(message, "model", None),
        request_id=getattr(message, "_request_id", None),
        usage=LLMUsage(
            input_tokens=_int(usage, "input_tokens"),
            output_tokens=_int(usage, "output_tokens"),
            cache_read_input_tokens=_int(usage, "cache_read_input_tokens"),
            cache_creation_input_tokens=_int(usage, "cache_creation_input_tokens"),
        ),
        provider_blocks=blocks,
    )


def _block_to_dict(block: Any) -> dict[str, Any]:
    to_dict = getattr(block, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if isinstance(block, dict):
        return block
    raise LLMInvalidResponseError("Unrecognised content block")


def _int(obj: Any, name: str) -> int:
    value = getattr(obj, name, None)
    return value if isinstance(value, int) else 0


class AnthropicProvider:
    """Claude through the official ``anthropic`` SDK (Messages API, client tools).

    - Credentials come from the environment (``ANTHROPIC_API_KEY``), never from code.
    - Connect and read timeouts are explicit. The SDK retries 408/409/429/5xx and
      connection errors with exponential backoff, honouring ``retry-after``.
    - After retries, SDK exceptions become ``LLMError`` subclasses. Their
      messages carry the status code and request ID only, never bodies or keys.
    - The system prompt and tool list are stable across turns, so automatic
      prompt caching makes every turn after the first much cheaper.
    - ``fallbacks="default"`` lets the API re-run a request that a safety
      classifier declined on a recommended fallback model. A remaining refusal
      is reported as ``finish_reason="refusal"``.
    """

    name = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        max_tokens: int = 16000,
        effort: str | None = "high",
        timeout_seconds: float = 60.0,
        connect_timeout_seconds: float = 10.0,
        max_retries: int = 2,
        server_fallbacks: bool = True,
        client: Any = None,
    ) -> None:
        import anthropic

        self._anthropic = anthropic
        self.model = model
        self._max_tokens = max_tokens
        self._effort = effort
        self._server_fallbacks = server_fallbacks
        self._timeout_seconds = timeout_seconds
        self._connect_timeout = connect_timeout_seconds
        if client is None:
            kwargs: dict[str, Any] = {
                "timeout": anthropic.Timeout(timeout_seconds, connect=connect_timeout_seconds),
                "max_retries": max_retries,
            }
            if api_key:
                kwargs["api_key"] = api_key
            try:
                client = anthropic.Anthropic(**kwargs)
            except anthropic.AnthropicError as exc:
                raise LLMConfigurationError(f"Anthropic client could not be created: {type(exc).__name__}") from None
        self._client = client

    @property
    def max_tokens(self) -> int:
        """The output token limit of every request: a budget's worst case."""
        return self._max_tokens

    def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        timeout: float | None = None,
        tool_choice: ToolChoice = "auto",
    ) -> LLMResponse:
        try:
            system, api_messages, api_tools = to_anthropic_request(messages, tools)
        except (KeyError, ValueError) as exc:
            raise LLMInvalidResponseError(f"Conversation cannot be sent: {exc}") from None
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "messages": api_messages,
            "cache_control": {"type": "ephemeral"},
        }
        if system:
            request["system"] = system
        if api_tools:
            request["tools"] = api_tools
            if tool_choice == "none":
                request["tool_choice"] = {"type": "none"}
        if self._effort:
            request["output_config"] = {"effort": self._effort}
        if self._server_fallbacks:
            request["betas"] = [ANTHROPIC_FALLBACK_BETA]
            request["fallbacks"] = "default"

        client = self._client
        if timeout is not None:
            read = max(1.0, min(timeout, self._timeout_seconds))
            client = client.with_options(
                timeout=self._anthropic.Timeout(read, connect=min(self._connect_timeout, read))
            )
        message = self._call(client, request)
        return from_anthropic_response(message)

    def _call(self, client: Any, request: dict[str, Any]) -> Any:
        anthropic = self._anthropic
        try:
            return client.beta.messages.create(**request)
        except anthropic.APITimeoutError:
            raise LLMTimeoutError("LLM request timed out") from None
        except anthropic.RateLimitError as exc:
            raise LLMRateLimitError(
                f"LLM rate limit reached{_request_suffix(exc)}", retry_after=_retry_after(exc)
            ) from None
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMConfigurationError(
                f"LLM credentials were rejected ({exc.status_code}){_request_suffix(exc)}"
            ) from None
        except anthropic.APIStatusError as exc:
            retryable = exc.status_code >= 500 or exc.status_code in (408, 409, 529)
            raise LLMProviderError(
                f"LLM provider returned HTTP {exc.status_code}{_request_suffix(exc)}",
                status_code=exc.status_code,
                retryable=retryable,
            ) from None
        except anthropic.APIConnectionError:
            raise LLMProviderError("Could not connect to the LLM provider", retryable=True) from None
        except anthropic.APIResponseValidationError:
            raise LLMInvalidResponseError("LLM provider returned a malformed response") from None
        except anthropic.AnthropicError as exc:
            raise LLMProviderError(f"LLM provider error: {type(exc).__name__}") from None


def _request_suffix(exc: Any) -> str:
    request_id = getattr(exc, "request_id", None)
    return f" (request {request_id})" if request_id else ""


def _retry_after(exc: Any) -> float | None:
    response = getattr(exc, "response", None)
    value = getattr(response, "headers", {}).get("retry-after") if response is not None else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def create_llm_provider(settings: Any) -> LLMProvider:
    """Build the configured provider. Raises ``LLMConfigurationError`` when it cannot run."""
    if settings.llm_provider == "disabled":
        raise LLMConfigurationError("LLM_PROVIDER is disabled")
    if settings.llm_provider == "anthropic":
        if not settings.llm_allow_external:
            raise LLMConfigurationError(
                "The debugging agent sends repository source code to the Anthropic API. "
                "Set LLM_ALLOW_EXTERNAL=true to allow it."
            )
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        return AnthropicProvider(
            model=settings.llm_model,
            api_key=key,
            max_tokens=settings.llm_max_tokens,
            effort=settings.llm_effort,
            timeout_seconds=settings.llm_timeout_seconds,
            connect_timeout_seconds=settings.llm_connect_timeout_seconds,
            max_retries=settings.llm_max_retries,
            server_fallbacks=settings.llm_server_fallbacks,
        )
    raise LLMConfigurationError(f"Unknown LLM provider: {settings.llm_provider!r}")


LLMFactory = Callable[[], LLMProvider]
