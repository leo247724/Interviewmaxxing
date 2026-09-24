"""Bounded OpenRouter embeddings; no implicit fallback or fabricated vectors.

Contract: https://openrouter.ai/docs/api/api-reference/embeddings/create-embeddings
Only metadata and hashes enter receipts. HTTP error bodies are deliberately omitted.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

EMBEDDING_MODEL = "openai/text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
MAX_EMBEDDING_INPUTS = 512
MAX_INPUT_BYTES = 8000
MAX_BATCH_BYTES = 64000


class KnowledgeError(RuntimeError):
    """Retrieval/indexing could not safely complete; contains no source text."""


class EmbeddingError(KnowledgeError):
    """The provider did not supply valid embeddings under the fixed contract."""


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    receipt: dict[str, Any]


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> EmbeddingResult: ...


def validate_vector(value: object) -> list[float]:
    if not isinstance(value, list) or len(value) != EMBEDDING_DIMENSIONS:
        raise EmbeddingError("Embedding dimension mismatch")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value):
        raise EmbeddingError("Embedding contains a nonnumeric component")
    try:
        vector = [float(v) for v in value]
    except (OverflowError, ValueError):
        raise EmbeddingError("Embedding contains an invalid component") from None
    if not all(math.isfinite(v) and abs(v) <= 3.402823466e38 for v in vector):
        raise EmbeddingError("Embedding contains a nonfinite or out-of-range component")
    if not any(vector):
        raise EmbeddingError("Zero embeddings cannot be used for cosine retrieval")
    return vector


class OpenRouterEmbedder:
    """Synchronous, bounded batches with a per-request network timeout.

The two accepted response model IDs are the router ID and its upstream spelling;
both identify the same fixed model. Different models or dimensions fail closed.
"""

    def __init__(self, api_key: str, *, timeout: float = 30, batch_size: int = 32,
                 before_request: Callable[[bytes], object] | None = None,
                 after_request: Callable[[object, dict[str, Any]], None] | None = None) -> None:
        if not api_key.strip():
            raise ValueError("An OpenRouter API key is required")
        if not 0 < timeout <= 60 or not 1 <= batch_size <= 32:
            raise ValueError("Embedding timeout or batch size is outside its bounds")
        self._api_key = api_key
        self.timeout = timeout
        self.batch_size = batch_size
        self._before_request = before_request
        self._after_request = after_request

    def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        started = time.monotonic()
        if isinstance(texts, (str, bytes)) or len(texts) > MAX_EMBEDDING_INPUTS:
            raise EmbeddingError("Embedding input count exceeds its bounds")
        batches: list[list[str]] = []
        pending: list[str] = []
        pending_bytes = 0
        for value in texts:
            if not isinstance(value, str) or not value.strip():
                raise EmbeddingError("Embedding inputs must be nonempty text")
            size = len(value.encode("utf-8"))
            if size > MAX_INPUT_BYTES:
                raise EmbeddingError("Embedding input exceeds its byte limit")
            if pending and (len(pending) >= self.batch_size
                            or pending_bytes + size > MAX_BATCH_BYTES):
                batches.append(pending)
                pending, pending_bytes = [], 0
            pending.append(value)
            pending_bytes += size
        if pending:
            batches.append(pending)

        vectors: list[list[float]] = []
        usage: dict[str, int | float] = {}
        usage_batches: dict[str, int] = {}
        for batch in batches:
            batch_vectors, batch_usage = self._request(batch)
            vectors.extend(batch_vectors)
            for key, amount in batch_usage.items():
                usage[key] = usage.get(key, 0) + amount
                usage_batches[key] = usage_batches.get(key, 0) + 1
        return EmbeddingResult(vectors, {
            "model": EMBEDDING_MODEL, "dimensions": EMBEDDING_DIMENSIONS,
            "input_count": len(texts), "batch_count": len(batches),
            "input_sha256": [hashlib.sha256(t.encode()).hexdigest() for t in texts],
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "usage": usage,
            "usage_reported_batches": usage_batches,
        })

    def _request(self, batch: list[str]) -> tuple[list[list[float]], dict[str, int | float]]:
        body = json.dumps({"model": EMBEDDING_MODEL, "input": batch,
                           "dimensions": EMBEDDING_DIMENSIONS,
                           "encoding_format": "float"}).encode()
        request = urllib.request.Request(
            "https://openrouter.ai/api/v1/embeddings",
            data=body,
            headers={"Authorization": f"Bearer {self._api_key}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        # Runtime supplies cycle-free shared-budget hooks. Reservation sees the
        # exact serialized bytes, including JSON escapes, before any HTTP call.
        reservation = self._before_request(body) if self._before_request else None
        started = time.monotonic()
        metadata: dict[str, Any] = {"model": EMBEDDING_MODEL, "resolved_model": None,
                                    "cost_usd": None, "status": "ERROR"}
        try:
            vectors, usage = self._perform_request(request, len(batch), metadata)
            metadata["status"] = "OK"
            return vectors, usage
        finally:
            metadata["latency_seconds"] = time.monotonic() - started
            if self._after_request:
                self._after_request(reservation, metadata)

    def _perform_request(self, request: urllib.request.Request, count: int,
                         metadata: dict[str, Any]) -> tuple[list[list[float]], dict[str, int | float]]:
        max_response = count * EMBEDDING_DIMENSIONS * 32 + 65536
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(max_response + 1)
        except urllib.error.HTTPError as exc:
            raise EmbeddingError(f"Embedding provider returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise EmbeddingError("Embedding provider transport failed") from None
        if len(raw) > max_response:
            raise EmbeddingError("Embedding response exceeds its size limit")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            raise EmbeddingError("Embedding provider returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise EmbeddingError("Embedding provider returned an error payload")
        usage: dict[str, int | float] = {}
        raw_usage = payload.get("usage")
        if isinstance(raw_usage, dict):
            for key in ("prompt_tokens", "total_tokens", "input_tokens", "cost"):
                number = raw_usage.get(key)
                if not isinstance(number, (int, float)) or isinstance(number, bool):
                    continue
                try:
                    valid = math.isfinite(number) and number >= 0
                except OverflowError:
                    valid = False
                if valid:
                    usage[key] = number
        metadata["cost_usd"] = usage.get("cost")
        if payload.get("error"):
            raise EmbeddingError("Embedding provider returned an error payload")
        if payload.get("model") not in (EMBEDDING_MODEL, "text-embedding-3-small"):
            raise EmbeddingError("Embedding model mismatch")
        metadata["resolved_model"] = payload["model"]
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != count:
            raise EmbeddingError("Embedding response count mismatch")
        by_index: dict[int, list[float]] = {}
        for item in data:
            if not isinstance(item, dict):
                raise EmbeddingError("Malformed embedding response item")
            index = item.get("index")
            if type(index) is not int or not 0 <= index < count or index in by_index:
                raise EmbeddingError("Embedding response index mismatch")
            by_index[index] = validate_vector(item.get("embedding"))
        return [by_index[i] for i in range(count)], usage
