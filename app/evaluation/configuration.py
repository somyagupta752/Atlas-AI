"""Versioned experiment configurations (Task 14.2).

A configuration says exactly what produced a result. It is validated, normalised
(defaults are written out, so a snapshot never depends on a default that changes
later) and hashed. Once stored it is never edited: a change is a new version.

Secrets never enter a configuration. API keys come from runtime settings. Unknown
fields are rejected, so ``api_key`` cannot be smuggled in, and every stored string
is also scanned for key-shaped values and for the live values of configured secrets.
"""

import re
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.evaluation import PRICING
from app.agents.prompts import SYSTEM_PROMPT as DIAGNOSIS_SYSTEM_PROMPT
from app.agents.tool_registry import DEFAULT_TOOL_NAMES
from app.config import Settings
from app.evaluation.environment import git_info, sha256_json, sha256_text
from app.patching.generator import PROMPT_VERSION as PATCH_PROMPT_VERSION
from app.patching.generator import SYSTEM_PROMPT as PATCH_SYSTEM_PROMPT

CONFIG_SCHEMA_VERSION = 1
SUPPORTED_PROVIDERS = ("anthropic",)
RETRIEVAL_STRATEGIES = ("hybrid", "keyword", "vector", "graph", "none")
# Bounds for values that spend money or time.
MAX_BUDGET_USD = 100.0
MAX_PATCH_ATTEMPTS = 10
SECRET_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|"
    r"(?i:bearer)\s+[A-Za-z0-9._-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ModelSpec(_Strict):
    provider: Literal["anthropic"]
    model_id: str = Field(min_length=1, max_length=200)
    # Written out explicitly; "high" is the application default.
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_tokens: int = Field(default=16000, ge=1024, le=64000)

    @field_validator("model_id")
    @classmethod
    def _priced(cls, value: str) -> str:
        # A cost cap cannot bound a model whose price is unknown, so refuse it up front.
        if value not in PRICING:
            raise ValueError(f"unsupported model {value!r}; supported: {', '.join(sorted(PRICING))}")
        return value


class RetrievalSpec(_Strict):
    strategy: Literal["hybrid", "keyword", "vector", "graph", "none"] = "hybrid"
    graph_enabled: bool = True
    # None: use the runtime EMBEDDING_MODEL, written into the stored document.
    embedding_model: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.strategy == "graph" and not self.graph_enabled:
            raise ValueError("strategy 'graph' needs graph_enabled=true")
        return self

    @property
    def sources(self) -> list[str]:
        """Retrieval sources the agent's retrieve_evidence tool may use."""
        if self.strategy == "none":
            return []
        chosen = ["keyword", "vector", "graph"] if self.strategy == "hybrid" else [self.strategy]
        return [s for s in chosen if s != "graph" or self.graph_enabled]


class AgentSpec(_Strict):
    max_iterations: int = Field(default=12, ge=1, le=100)
    max_tool_calls: int = Field(default=20, ge=1, le=200)
    max_duration_seconds: float = Field(default=120.0, gt=0, le=3600)
    tools: list[str] = Field(default_factory=lambda: list(DEFAULT_TOOL_NAMES), min_length=1)

    @field_validator("tools")
    @classmethod
    def _known_tools(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(DEFAULT_TOOL_NAMES))
        if unknown:
            raise ValueError(f"unknown tools {unknown}; supported: {list(DEFAULT_TOOL_NAMES)}")
        return sorted(set(value))


class PatchingSpec(_Strict):
    max_attempts: int = Field(default=3, ge=1, le=MAX_PATCH_ATTEMPTS)
    run_full_test_suite: bool = True


class TestExecutionSpec(_Strict):
    __test__ = False

    # No default: where code runs is a decision, never an accident.
    backend: Literal["local", "container"]
    # blind: the model never sees test files (tests still run). visible: up to 5 test files are shown.
    test_visibility: Literal["blind", "visible"] = "blind"


class BudgetSpec(_Strict):
    max_cost_usd: float = Field(default=1.0, gt=0, le=MAX_BUDGET_USD)
    max_requests: int = Field(default=6, ge=1, le=1000)
    max_seconds: float = Field(default=300.0, gt=0, le=86_400)


class ConfigurationSpec(_Strict):
    """What a client may send. Prompt hashes and commit are added by the server, never accepted."""

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    model: ModelSpec
    retrieval: RetrievalSpec = Field(default_factory=RetrievalSpec)
    agent: AgentSpec = Field(default_factory=AgentSpec)
    patching: PatchingSpec = Field(default_factory=PatchingSpec)
    test_execution: TestExecutionSpec
    budget: BudgetSpec = Field(default_factory=BudgetSpec)

    @field_validator("name", "description")
    @classmethod
    def _no_secret_values(cls, value: str) -> str:
        if SECRET_PATTERN.search(value):
            raise ValueError("looks like a secret; configurations must not contain credentials")
        return value


class SecretFoundError(ValueError):
    pass


def prompt_fingerprints() -> dict[str, Any]:
    """Hashes of the prompts and tool contracts that shape model behaviour right now."""
    from app.agents.tool_registry import create_default_registry
    from app.patching.generator import SUBMIT_PATCH_TOOL

    registry = create_default_registry()
    return {
        "diagnosis_system_sha256": sha256_text(DIAGNOSIS_SYSTEM_PROMPT),
        "patch_system_sha256": sha256_text(PATCH_SYSTEM_PROMPT),
        "patch_prompt_version": PATCH_PROMPT_VERSION,
        "patch_tool_sha256": sha256_json(SUBMIT_PATCH_TOOL),
        "tool_names": list(DEFAULT_TOOL_NAMES),
        "tools_sha256": sha256_json(registry.get_schemas()),
    }


def runtime_secret_values(settings: Settings) -> list[str]:
    values = []
    for secret in (settings.anthropic_api_key, settings.neo4j_password, settings.embedding_api_key):
        if secret is not None and len(secret.get_secret_value()) >= 8:
            values.append(secret.get_secret_value())
    return values


def assert_no_secrets(value: Any, secrets: list[str] | None = None, path: str = "$") -> None:
    """Raise ``SecretFoundError`` when any string looks like, or equals, a credential."""
    if isinstance(value, str):
        if SECRET_PATTERN.search(value) or any(s in value for s in secrets or ()):
            raise SecretFoundError(f"a credential-like value was found at {path}")
    elif isinstance(value, dict):
        for key, item in value.items():
            assert_no_secrets(str(key), secrets, f"{path}.<key>")
            assert_no_secrets(item, secrets, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_secrets(item, secrets, f"{path}[{index}]")


def build_configuration(spec: ConfigurationSpec, settings: Settings) -> dict[str, Any]:
    """The normalised, secret-free document stored with a configuration (everything but ``name``)."""
    document = spec.model_dump(mode="json", exclude={"name"})
    document["retrieval"]["embedding_model"] = spec.retrieval.embedding_model or settings.embedding_model
    document["schema_version"] = CONFIG_SCHEMA_VERSION
    document["prompts"] = prompt_fingerprints()
    document["created_at_application"] = git_info()
    assert_no_secrets(document, runtime_secret_values(settings))
    return document


def configuration_hash(document: dict[str, Any]) -> str:
    """Identity of the experimental conditions: independent of name, description and creation commit."""
    identity = {k: v for k, v in document.items() if k not in ("description", "created_at_application")}
    return sha256_json(identity)


def merge_overrides(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``overrides`` into ``base``. A non-dict value replaces; ``None`` is not a delete."""
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_overrides(merged[key], value)
        else:
            merged[key] = value
    return merged


def spec_from_document(name: str, document: dict[str, Any]) -> ConfigurationSpec:
    """Rebuild a spec from a stored document (dropping the server-added blocks)."""
    editable = {k: v for k, v in document.items()
                if k in ConfigurationSpec.model_fields and k != "name"}
    return ConfigurationSpec.model_validate({"name": name, **editable})


def denormalised_fields(spec: ConfigurationSpec) -> dict[str, str]:
    return {
        "model_provider": spec.model.provider,
        "model_id": spec.model.model_id,
        "retrieval_strategy": spec.retrieval.strategy,
        "test_visibility": spec.test_execution.test_visibility,
        "test_backend": spec.test_execution.backend,
    }


def prompts_changed(document: dict[str, Any]) -> list[str]:
    """Names of prompt/tool fingerprints that differ between a stored configuration and the code now."""
    pinned, current = document.get("prompts", {}), prompt_fingerprints()
    return sorted(key for key in current if pinned.get(key) != current[key])
