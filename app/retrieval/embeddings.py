"""Embedding providers behind one protocol.

- ``HashingEmbeddingProvider``: local and deterministic. It hashes identifier
  sub-terms into a fixed-size vector. No network, no model download. It is a
  lexical-semantic baseline: it relates ``get_user`` to "user", but it does not
  know that "fetch" and "load" are similar. Use a real model for that.
- ``OpenAICompatibleEmbeddingProvider``: any HTTP ``/embeddings`` endpoint with
  the OpenAI request shape (OpenAI, Ollama, vLLM, LM Studio, LiteLLM ...).
  Sending source code off this machine needs ``EMBEDDING_ALLOW_EXTERNAL=true``.
- ``FakeEmbeddingProvider``: for tests. Deterministic, records calls, and can
  fail on a chosen call.

``model_id`` names the provider, model and dimension. It is stored with every
vector, and searches only compare vectors with the same ``model_id``, so
embeddings from incompatible models are never mixed.
"""

import hashlib
import math
import time
from collections import Counter
from collections.abc import Sequence
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from app.config import Settings
from app.models import EMBEDDING_DIMENSIONS
from app.retrieval.text import fold_suffix, identifier_terms_of

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})


class EmbeddingError(Exception):
    """Base class. Messages never contain API keys or response bodies."""


class EmbeddingConfigurationError(EmbeddingError):
    """The provider settings are invalid or unsafe."""


class EmbeddingProviderError(EmbeddingError):
    """The provider failed or returned something unusable."""


class EmbeddingDimensionError(EmbeddingProviderError):
    """A vector does not have the configured number of dimensions."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str:
        """Stable identity of the vector space: ``provider:model:dimensions``."""
        ...

    @property
    def dimensions(self) -> int: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def embed_in_batches(
    provider: EmbeddingProvider, texts: Sequence[str], batch_size: int
) -> list[list[float]]:
    """Embed ``texts`` in order, ``batch_size`` at a time, validating every vector.

    All or nothing: if any batch fails the error propagates and no partial
    result is returned, so a caller cannot store half an index by accident.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = list(texts[start : start + batch_size])
        vectors.extend(validate_vectors(provider.embed_documents(batch), len(batch), provider.dimensions))
    return vectors


def validate_vectors(
    vectors: Sequence[Sequence[float]], expected_count: int, dimensions: int
) -> list[list[float]]:
    if len(vectors) != expected_count:
        raise EmbeddingProviderError(
            f"Provider returned {len(vectors)} vectors for {expected_count} inputs"
        )
    checked = []
    for vector in vectors:
        if len(vector) != dimensions:
            raise EmbeddingDimensionError(
                f"Expected {dimensions} dimensions, provider returned {len(vector)}"
            )
        values = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in values):
            raise EmbeddingProviderError("Provider returned a non-finite value")
        if not any(values):
            raise EmbeddingProviderError("Provider returned a zero vector")
        checked.append(values)
    return checked


def _require_text(text: str) -> str:
    if not text or not text.strip():
        raise EmbeddingError("Cannot embed empty text")
    return text


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def _hash64(value: str, salt: bytes = b"") -> int:
    return int.from_bytes(hashlib.blake2b(value.encode(), digest_size=8, salt=salt).digest(), "big")


# --- local hashing provider --------------------------------------------------


class HashingEmbeddingProvider:
    """Feature hashing over identifier terms with sublinear term frequency."""

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS, model: str = "atlas-hashing-v1") -> None:
        if dimensions < 8:
            raise EmbeddingConfigurationError("dimensions must be at least 8")
        self._dimensions = dimensions
        self._model = model

    @property
    def model_id(self) -> str:
        return f"hashing:{self._model}:{self._dimensions}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(_require_text(text)) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(_require_text(text))

    def _embed(self, text: str) -> list[float]:
        counts = Counter(fold_suffix(term) for term in identifier_terms_of(text))
        if not counts:
            counts = Counter({"<no-terms>": 1})
        vector = [0.0] * self._dimensions
        for term, count in counts.items():
            hashed = _hash64(term)
            sign = 1.0 if hashed & 1 else -1.0
            vector[(hashed >> 1) % self._dimensions] += sign * (1.0 + math.log(count))
        if not any(vector):  # every feature cancelled out; keep the vector usable
            vector[_hash64(text) % self._dimensions] = 1.0
        return _normalize(vector)


# --- fake provider for tests ----------------------------------------------------


class FakeEmbeddingProvider:
    """Deterministic pseudo-random unit vectors keyed by the text. Never calls out.

    Args:
        fail_on_call: 1-based ``embed_documents`` call number that raises.
        vectors: fixed vectors for specific texts, to control similarity in tests.
        returned_dimensions: return vectors of this size, to test validation.
    """

    def __init__(
        self,
        dimensions: int = EMBEDDING_DIMENSIONS,
        model: str = "fake-v1",
        fail_on_call: int | None = None,
        vectors: dict[str, list[float]] | None = None,
        returned_dimensions: int | None = None,
    ) -> None:
        self._dimensions = dimensions
        self._model = model
        self.fail_on_call = fail_on_call
        self.vectors = vectors or {}
        self.returned_dimensions = returned_dimensions or dimensions
        self.document_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    @property
    def model_id(self) -> str:
        return f"fake:{self._model}:{self._dimensions}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def embedded_texts(self) -> list[str]:
        return [text for call in self.document_calls for text in call]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls.append(list(texts))
        if self.fail_on_call is not None and len(self.document_calls) == self.fail_on_call:
            raise EmbeddingProviderError("Fake provider failure")
        return [self._vector(_require_text(text)) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        return self._vector(_require_text(text))

    def _vector(self, text: str) -> list[float]:
        if text in self.vectors:
            return _normalize(list(self.vectors[text]))
        values = [
            ((_hash64(text, salt=i.to_bytes(4, "big")) % 2001) - 1000) / 1000.0
            for i in range(self.returned_dimensions)
        ]
        return _normalize(values)


# --- HTTP provider ------------------------------------------------------------


def is_local_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in LOCAL_HOSTS or host.endswith(".localhost")


class OpenAICompatibleEmbeddingProvider:
    """POST ``{base_url}/embeddings`` with ``{"model", "input"}``; read ``data[].embedding``."""

    def __init__(
        self,
        base_url: str,
        model: str,
        dimensions: int,
        api_key: SecretStr | None = None,
        allow_external: bool = False,
        timeout_seconds: float = 30.0,
        send_dimensions: bool = False,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise EmbeddingConfigurationError("EMBEDDING_BASE_URL must be an http(s) URL")
        local = is_local_url(base_url)
        if not local and not allow_external:
            raise EmbeddingConfigurationError(
                "EMBEDDING_BASE_URL is not on localhost. Sending repository source code to an "
                "external service requires EMBEDDING_ALLOW_EXTERNAL=true."
            )
        if not local and parts.scheme != "https" and api_key is not None:
            raise EmbeddingConfigurationError("Refusing to send an API key over plain HTTP")
        if not model:
            raise EmbeddingConfigurationError("EMBEDDING_MODEL is required")
        self._url = base_url.rstrip("/") + "/embeddings"
        self._model = model
        self._dimensions = dimensions
        self._send_dimensions = send_dimensions
        self._max_retries = max_retries
        self._backoff = retry_backoff_seconds
        headers = {"Content-Type": "application/json"}
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key.get_secret_value()}"
        self._client = httpx.Client(timeout=timeout_seconds, headers=headers, transport=transport)

    @property
    def model_id(self) -> str:
        return f"openai_compatible:{self._model}:{self._dimensions}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def __repr__(self) -> str:  # never show headers, which hold the key
        return f"OpenAICompatibleEmbeddingProvider(model={self._model!r})"

    def close(self) -> None:
        self._client.close()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        for text in texts:
            _require_text(text)
        return self._request(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._request([_require_text(text)])[0]

    def _request(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, object] = {"model": self._model, "input": texts}
        if self._send_dimensions:
            payload["dimensions"] = self._dimensions
        response = self._post_with_retries(payload)
        try:
            data = response.json()["data"]
            ordered = sorted(data, key=lambda item: item.get("index", 0))
            vectors = [item["embedding"] for item in ordered]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingProviderError("Embedding provider returned an unexpected response") from exc
        return validate_vectors(vectors, len(texts), self._dimensions)

    def _post_with_retries(self, payload: dict[str, object]) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.post(self._url, json=payload)
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    time.sleep(self._backoff * (2**attempt))
                    continue
                # Type only: messages can contain the endpoint address.
                raise EmbeddingProviderError(f"Embedding request failed ({type(exc).__name__})") from exc
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < self._max_retries:
                time.sleep(self._backoff * (2**attempt))
                continue
            if response.status_code >= 400:
                # The body is not included: provider errors can echo part of the key.
                raise EmbeddingProviderError(f"Embedding provider returned HTTP {response.status_code}")
            return response
        raise EmbeddingProviderError("Embedding request failed")  # unreachable


def create_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Build the configured provider. Raise ``EmbeddingConfigurationError`` if unusable."""
    if settings.embedding_dimensions != EMBEDDING_DIMENSIONS:
        raise EmbeddingConfigurationError(
            f"EMBEDDING_DIMENSIONS={settings.embedding_dimensions} does not match the "
            f"code_chunks.embedding column ({EMBEDDING_DIMENSIONS}). Add a migration first."
        )
    if settings.embedding_provider == "hashing":
        return HashingEmbeddingProvider(settings.embedding_dimensions, settings.embedding_model)
    if not settings.embedding_base_url:
        raise EmbeddingConfigurationError("EMBEDDING_BASE_URL is required for openai_compatible")
    return OpenAICompatibleEmbeddingProvider(
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        api_key=settings.embedding_api_key,
        allow_external=settings.embedding_allow_external,
        timeout_seconds=settings.embedding_timeout_seconds,
        send_dimensions=settings.embedding_send_dimensions,
    )
