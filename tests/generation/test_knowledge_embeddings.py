"""Fictional urllib responses exercise the exact embedding provider contract."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from interviewmaxxing_generation.knowledge import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EmbeddingError,
    OpenRouterEmbedder,
)


def response(count=1):
    return {"model": EMBEDDING_MODEL,
            "data": [{"index": i, "embedding": [float(i + 1), *([0.0] * (EMBEDDING_DIMENSIONS - 1))]}
                     for i in range(count)],
            "usage": {"prompt_tokens": count * 5, "total_tokens": count * 5, "cost": 0.00001}}


def wire(monkeypatch, mutate=None):
    calls = []

    def urlopen(request, *, timeout):
        body = json.loads(request.data)
        calls.append((request, timeout, body))
        payload = response(len(body["input"]))
        if mutate:
            mutate(payload)
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return calls


def test_fixed_contract_bounded_batches_sorted_indices_and_usage(monkeypatch):
    calls = wire(monkeypatch, lambda p: p["data"].reverse())
    embedder = OpenRouterEmbedder("fictional-not-a-secret", batch_size=2, timeout=4)
    result = embedder.embed(["First fictional input", "Second", "Third"])
    assert [vector[0] for vector in result.vectors] == [1.0, 2.0, 1.0]
    assert [len(body["input"]) for _, _, body in calls] == [2, 1]
    for request, timeout, body in calls:
        assert request.full_url == "https://openrouter.ai/api/v1/embeddings"
        assert request.method == "POST" and timeout == 4
        assert body["model"] == EMBEDDING_MODEL and body["dimensions"] == 1536
        assert body["encoding_format"] == "float"
    assert result.receipt["usage"] == {"prompt_tokens": 15, "total_tokens": 15, "cost": 0.00002}
    assert result.receipt["usage_reported_batches"] == {"prompt_tokens": 2, "total_tokens": 2, "cost": 2}
    assert result.receipt["batch_count"] == 2
    serialized = json.dumps(result.receipt)
    assert "fictional" not in serialized and "First" not in serialized


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(model="different/model"),
    lambda p: p.update(model=["invalid model value"]),
    lambda p: p.update(data=[]),
    lambda p: p["data"][0].update(embedding=[1.0]),
    lambda p: p["data"][0].update(embedding=[0.0] * 1536),
    lambda p: p["data"][0]["embedding"].__setitem__(0, float("nan")),
    lambda p: p["data"][0]["embedding"].__setitem__(0, float("inf")),
    lambda p: p["data"][0]["embedding"].__setitem__(0, 1e300),
    lambda p: p["data"][0]["embedding"].__setitem__(0, True),
    lambda p: p["data"][0]["embedding"].__setitem__(0, "1.0"),
    lambda p: p["data"][0].update(index=True),
    lambda p: p["data"][0].update(index=7),
    lambda p: p.update(error={"message": "Fictional provider error containing source text"}),
])
def test_malformed_provider_response_fails_explicitly(monkeypatch, mutation):
    wire(monkeypatch, mutation)
    with pytest.raises(EmbeddingError) as error:
        OpenRouterEmbedder("fictional-key").embed(["Fictional input"])
    assert "Fictional" not in str(error.value)


def test_duplicate_provider_indices_rejected(monkeypatch):
    wire(monkeypatch, lambda p: p["data"][1].update(index=0))
    with pytest.raises(EmbeddingError, match="index mismatch"):
        OpenRouterEmbedder("fictional-key").embed(["one", "two"])


@pytest.mark.parametrize("error", [
    urllib.error.HTTPError("https://example.invalid", 429, "Private message", {}, None),
    urllib.error.URLError("Private transport message"), TimeoutError("Private timeout message"),
])
def test_transport_error_is_explicit_and_scrubbed(monkeypatch, error):
    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(EmbeddingError) as caught:
        OpenRouterEmbedder("fictional-key").embed(["Fictional source"])
    assert "Private" not in str(caught.value) and caught.value.__suppress_context__


def test_invalid_json_fails_explicitly(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: io.BytesIO(b"<html>"))
    with pytest.raises(EmbeddingError, match="invalid JSON"):
        OpenRouterEmbedder("fictional-key").embed(["one"])


@pytest.mark.parametrize("texts", [[""], ["x" * 8001], ["one"] * 513, "not-an-input-array"])
def test_input_bounds_precede_network(monkeypatch, texts):
    calls = wire(monkeypatch)
    with pytest.raises(EmbeddingError):
        OpenRouterEmbedder("fictional-key").embed(texts)
    assert calls == []


def test_batch_bytes_are_bounded_and_empty_projection_needs_no_provider(monkeypatch):
    calls = wire(monkeypatch)
    embedder = OpenRouterEmbedder("fictional-key")
    assert embedder.embed([]).vectors == [] and calls == []
    embedder.embed(["x" * 8000] * 9)
    assert [len(body["input"]) for _, _, body in calls] == [8, 1]


def test_budget_hooks_see_exact_encoded_bytes_and_usage_for_every_batch(monkeypatch):
    calls = wire(monkeypatch)
    reservations, receipts = [], []

    def before(body):
        token = object()
        reservations.append((body, token))
        return token

    embedder = OpenRouterEmbedder("fictional-key", batch_size=1, before_request=before,
                                 after_request=lambda token, meta: receipts.append((token, dict(meta))))
    embedder.embed(['"quoted" 中文', "two"])
    assert len(reservations) == len(receipts) == len(calls) == 2
    for (body, token), (seen_token, metadata), (request, _, _) in zip(reservations, receipts, calls, strict=True):
        assert body == request.data and token is seen_token
        assert metadata["model"] == metadata["resolved_model"] == EMBEDDING_MODEL
        assert metadata["cost_usd"] == 0.00001 and metadata["status"] == "OK"
        assert metadata["latency_seconds"] >= 0
        assert "quoted" not in json.dumps(metadata)
    assert b"\\u4e2d" in reservations[0][0]


def test_denied_budget_calls_neither_http_nor_after_hook(monkeypatch):
    calls = wire(monkeypatch)
    receipts = []

    def deny(_body):
        raise RuntimeError("Synthetic budget exhausted")

    embedder = OpenRouterEmbedder("fictional-key", before_request=deny,
                                 after_request=lambda *args: receipts.append(args))
    with pytest.raises(RuntimeError, match="budget exhausted"):
        embedder.embed(["one"])
    assert calls == receipts == []


@pytest.mark.parametrize("transport_failure", [True, False])
def test_reserved_failure_always_records_safe_error(monkeypatch, transport_failure):
    if transport_failure:
        def fail(*_args, **_kwargs):
            raise urllib.error.URLError("secret diagnostic")

        monkeypatch.setattr("urllib.request.urlopen", fail)
    else:
        wire(monkeypatch, lambda payload: payload["data"][0].update(embedding=[1.0]))
    token, receipts = object(), []
    embedder = OpenRouterEmbedder("fictional-key", before_request=lambda _body: token,
                                 after_request=lambda reservation, meta: receipts.append((reservation, meta)))
    with pytest.raises(EmbeddingError):
        embedder.embed(["one"])
    assert len(receipts) == 1 and receipts[0][0] is token
    meta = receipts[0][1]
    assert meta["status"] == "ERROR" and "secret" not in json.dumps(meta)
    assert meta["cost_usd"] == (None if transport_failure else 0.00001)
    assert meta["resolved_model"] == (None if transport_failure else EMBEDDING_MODEL)
