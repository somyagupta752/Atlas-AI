import json
import math

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.models import EMBEDDING_DIMENSIONS
from app.retrieval.embeddings import (
    EmbeddingConfigurationError,
    EmbeddingDimensionError,
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingProviderError,
    FakeEmbeddingProvider,
    HashingEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    create_embedding_provider,
    embed_in_batches,
    validate_vectors,
)

API_KEY = "sk-test-secret-key-123"


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


# --- fake and hashing providers ---------------------------------------------


def test_fake_provider_is_deterministic_normalized_and_has_the_right_size():
    provider = FakeEmbeddingProvider(dimensions=16)
    first = provider.embed_documents(["a", "b"])
    second = provider.embed_documents(["a", "b"])

    assert first == second
    assert first[0] != first[1]
    assert all(len(v) == 16 for v in first)
    assert all(math.isclose(math.sqrt(sum(x * x for x in v)), 1.0) for v in first)
    assert provider.embed_query("a") == first[0]
    assert isinstance(provider, EmbeddingProvider)


def test_hashing_provider_relates_identifiers_to_words():
    provider = HashingEmbeddingProvider(dimensions=EMBEDDING_DIMENSIONS)
    code = provider.embed_documents(["def get_user(user_id): return repository.find_by_id(user_id)"])[0]
    related = provider.embed_query("get user by id")
    unrelated = provider.embed_query("render chart colors")

    assert cosine(code, related) > cosine(code, unrelated)
    assert provider.model_id == f"hashing:atlas-hashing-v1:{EMBEDDING_DIMENSIONS}"


def test_batches_preserve_order_and_respect_the_batch_size():
    provider = FakeEmbeddingProvider(dimensions=8)
    texts = [f"text {i}" for i in range(7)]

    vectors = embed_in_batches(provider, texts, batch_size=3)

    assert [len(call) for call in provider.document_calls] == [3, 3, 1]
    assert vectors == [provider.embed_query(t) for t in texts]


def test_empty_input_makes_no_provider_call():
    provider = FakeEmbeddingProvider(dimensions=8)

    assert embed_in_batches(provider, [], batch_size=10) == []
    assert provider.document_calls == []


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_query_or_document_is_rejected(text):
    with pytest.raises(EmbeddingError):
        HashingEmbeddingProvider().embed_query(text)
    with pytest.raises(EmbeddingError):
        FakeEmbeddingProvider(dimensions=8).embed_documents([text])


def test_provider_failure_halfway_through_returns_nothing():
    provider = FakeEmbeddingProvider(dimensions=8, fail_on_call=2)

    with pytest.raises(EmbeddingProviderError):
        embed_in_batches(provider, ["a", "b", "c", "d"], batch_size=2)
    assert len(provider.document_calls) == 2


def test_wrong_dimensions_are_rejected():
    provider = FakeEmbeddingProvider(dimensions=8, returned_dimensions=4)

    with pytest.raises(EmbeddingDimensionError):
        embed_in_batches(provider, ["a"], batch_size=1)


@pytest.mark.parametrize(
    ("vectors", "count"),
    [([[1.0, 0.0]], 2), ([[math.nan, 1.0]], 1), ([[0.0, 0.0]], 1), ([[math.inf, 0.0]], 1)],
)
def test_vector_validation(vectors, count):
    with pytest.raises(EmbeddingProviderError):
        validate_vectors(vectors, count, 2)


def test_invalid_batch_size():
    with pytest.raises(ValueError):
        embed_in_batches(FakeEmbeddingProvider(dimensions=8), ["a"], batch_size=0)


# --- HTTP provider (never touches the network: MockTransport) ----------------


def http_provider(handler, **kwargs):
    defaults = {
        "base_url": "http://localhost:11434/v1",
        "model": "test-model",
        "dimensions": 3,
        "retry_backoff_seconds": 0,
        "transport": httpx.MockTransport(handler),
    }
    return OpenAICompatibleEmbeddingProvider(**{**defaults, **kwargs})


def test_http_provider_sends_the_openai_request_shape_and_orders_by_index():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": [{"index": 1, "embedding": [0, 1, 0]}, {"index": 0, "embedding": [1, 0, 0]}]},
        )

    provider = http_provider(handler, api_key=SecretStr(API_KEY), send_dimensions=True)
    vectors = provider.embed_documents(["first", "second"])

    assert vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert seen["url"] == "http://localhost:11434/v1/embeddings"
    assert seen["auth"] == f"Bearer {API_KEY}"
    assert seen["body"] == {"model": "test-model", "input": ["first", "second"], "dimensions": 3}
    assert provider.model_id == "openai_compatible:test-model:3"
    assert API_KEY not in repr(provider)


def test_http_provider_errors_do_not_leak_the_key_or_body():
    def handler(request):
        return httpx.Response(401, json={"error": f"Incorrect API key provided: {API_KEY}"})

    provider = http_provider(handler, api_key=SecretStr(API_KEY))
    with pytest.raises(EmbeddingProviderError) as info:
        provider.embed_query("x")

    assert "401" in str(info.value)
    assert API_KEY not in str(info.value)


def test_http_provider_retries_server_errors_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0, 0]}]})

    assert http_provider(handler).embed_query("x") == [1.0, 0.0, 0.0]
    assert len(calls) == 3


def test_http_provider_network_failure_becomes_a_provider_error():
    def handler(request):
        raise httpx.ConnectError("connection refused to 10.0.0.1")

    with pytest.raises(EmbeddingProviderError) as info:
        http_provider(handler, max_retries=0).embed_query("x")
    assert "10.0.0.1" not in str(info.value)


def test_http_provider_validates_dimensions_and_response_shape():
    with pytest.raises(EmbeddingDimensionError):
        http_provider(lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1]}]})).embed_query("x")
    with pytest.raises(EmbeddingProviderError):
        http_provider(lambda r: httpx.Response(200, json={"unexpected": True})).embed_query("x")


def test_http_provider_empty_batch_makes_no_request():
    def handler(request):
        raise AssertionError("no request expected")

    assert http_provider(handler).embed_documents([]) == []


def test_external_endpoints_require_explicit_opt_in():
    with pytest.raises(EmbeddingConfigurationError, match="EMBEDDING_ALLOW_EXTERNAL"):
        OpenAICompatibleEmbeddingProvider("https://api.example.com/v1", "m", 3)

    provider = OpenAICompatibleEmbeddingProvider(
        "https://api.example.com/v1", "m", 3, allow_external=True
    )
    assert provider.dimensions == 3


def test_api_key_is_never_sent_over_plain_http_to_an_external_host():
    with pytest.raises(EmbeddingConfigurationError, match="plain HTTP"):
        OpenAICompatibleEmbeddingProvider(
            "http://api.example.com/v1", "m", 3, api_key=SecretStr(API_KEY), allow_external=True
        )


def test_invalid_base_url_is_rejected():
    with pytest.raises(EmbeddingConfigurationError):
        OpenAICompatibleEmbeddingProvider("ftp://localhost", "m", 3)


# --- configuration --------------------------------------------------------------


def settings(**overrides):
    return Settings(database_url="postgresql://unused", _env_file=None, **overrides)


def test_default_configuration_is_the_local_hashing_provider():
    provider = create_embedding_provider(settings())

    assert isinstance(provider, HashingEmbeddingProvider)
    assert provider.dimensions == EMBEDDING_DIMENSIONS


def test_configured_dimensions_must_match_the_schema():
    with pytest.raises(EmbeddingConfigurationError, match="migration"):
        create_embedding_provider(settings(embedding_dimensions=1536))


def test_http_provider_requires_a_base_url():
    with pytest.raises(EmbeddingConfigurationError, match="EMBEDDING_BASE_URL"):
        create_embedding_provider(settings(embedding_provider="openai_compatible"))


def test_http_provider_from_settings_refuses_external_hosts_by_default():
    with pytest.raises(EmbeddingConfigurationError):
        create_embedding_provider(
            settings(
                embedding_provider="openai_compatible",
                embedding_base_url="https://api.example.com/v1",
                embedding_model="m",
            )
        )
