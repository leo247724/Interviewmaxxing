"""The typed Decisions API client: request shape, response validation, errors, retries."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from interviewmaxxing_selection import (
    DECISIONS_URL,
    DEFAULT_MODEL,
    ApiKey,
    ChoiceQuestion,
    DecisionRequest,
    HttpResponse,
    JevClient,
    JevProviderError,
    NoulQuestion,
    ProviderFailureKind,
)

REQUEST = DecisionRequest(
    state={"listing": "Fictional listing text"},
    questions={
        "role": ChoiceQuestion(
            instructions="Which role family?",
            criteria={"marketing": "marketing", "engineering": "engineering", "other": "other"},
        ),
        "remote": NoulQuestion(instructions="Is it remote?"),
    },
)

# The exact shape observed from the live API on 2026-09-22 (snake_case usage, id).
LIVE_SHAPE: dict[str, Any] = {
    "model": "typesafe/jev-1.13-20260917",
    "answers": {
        "remote": {"type": "noul", "noul": 0.89},
        "role": {
            "type": "choice",
            "choice": "marketing",
            "probabilities": {"marketing": 1, "engineering": 0, "other": 0},
            "confidence": 1,
        },
    },
    "usage": {"input_tokens": 389, "output_tokens": 55, "cost": 1.6338e-05},
    "id": "gen-dec-fictional",
    "provider": "TypeSafe",
}


class Scripted:
    def __init__(self, *responses: HttpResponse | BaseException) -> None:
        self.responses = list(responses)
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[Mapping[str, str]] = []
        self.urls: list[str] = []
        self.timeouts: list[float] = []

    def __call__(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> HttpResponse:
        self.urls.append(url)
        self.headers.append(headers)
        self.bodies.append(json.loads(body))
        self.timeouts.append(timeout)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def resp(status: int, payload: Any, headers: dict[str, str] | None = None) -> HttpResponse:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return HttpResponse(status, headers or {}, body)


def client(api_key: ApiKey, transport: Scripted, **kw: Any) -> tuple[JevClient, list[float]]:
    sleeps: list[float] = []
    return JevClient(api_key, transport=transport, sleep=sleeps.append, **kw), sleeps


def test_sends_pinned_model_to_decisions_endpoint(api_key: ApiKey) -> None:
    transport = Scripted(resp(200, LIVE_SHAPE))
    jev, _ = client(api_key, transport, timeout_seconds=7)
    result = jev.decide(REQUEST)
    assert transport.urls == [DECISIONS_URL]
    assert "chat/completions" not in transport.urls[0]
    assert transport.bodies[0]["model"] == DEFAULT_MODEL == "typesafe/jev-1.13"
    assert transport.bodies[0]["questions"]["role"]["criteria"]["marketing"] == "marketing"
    assert transport.headers[0]["Authorization"] == f"Bearer {api_key.reveal()}"
    assert transport.timeouts == [7]
    assert result.response.model == "typesafe/jev-1.13-20260917"
    assert result.response.provider == "TypeSafe"
    assert result.response.usage is not None
    assert result.response.usage.input_tokens == 389
    assert result.response.usage.cost == pytest.approx(1.6338e-05)
    assert result.response.choice("role").choice == "marketing"
    assert result.raw == LIVE_SHAPE


def test_camel_case_usage_also_parses(api_key: ApiKey) -> None:
    payload = {**LIVE_SHAPE, "usage": {"inputTokens": 381, "outputTokens": 62, "cost": 1.6e-05}}
    jev, _ = client(api_key, Scripted(resp(200, payload)))
    usage = jev.decide(REQUEST).response.usage
    assert usage is not None and (usage.input_tokens, usage.output_tokens) == (381, 62)


def test_rejects_chat_endpoint(api_key: ApiKey) -> None:
    with pytest.raises(ValueError, match="Decisions API"):
        JevClient(api_key, url="https://openrouter.ai/api/v1/chat/completions")


def _role(**changes: Any) -> dict[str, Any]:
    role = dict(LIVE_SHAPE["answers"]["role"])
    role.update(changes)
    return {**LIVE_SHAPE, "answers": {**LIVE_SHAPE["answers"], "role": role}}


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        (_role(choice="sales"), "not an offered option"),
        (_role(probabilities={"marketing": 1, "engineering": 0}), "probabilities cover"),
        (
            _role(probabilities={"marketing": 0.5, "engineering": 0.1, "other": 0.1}),
            "sum to",
        ),
        (_role(confidence=1.5), "less than or equal"),
        (_role(confidence=-0.1), "greater than or equal"),
        (_role(probabilities={"marketing": 0.2, "engineering": 0.8, "other": 0}), "most probable"),
        ({**LIVE_SHAPE, "answers": {"role": LIVE_SHAPE["answers"]["role"]}}, "missing"),
        (
            {
                **LIVE_SHAPE,
                "answers": {
                    **LIVE_SHAPE["answers"],
                    "remote": {
                        "type": "choice",
                        "choice": "x",
                        "probabilities": {"x": 1},
                        "confidence": 1,
                    },
                },
            },
            "expected noul",
        ),
        ({"answers": LIVE_SHAPE["answers"]}, "Field required"),
        ({**LIVE_SHAPE, "usage": {"input_tokens": -1}}, "greater than or equal"),
        ([1, 2], "not a JSON object"),
    ],
)
def test_malformed_answers_are_actionable(api_key: ApiKey, payload: Any, fragment: str) -> None:
    jev, sleeps = client(api_key, Scripted(resp(200, payload)))
    with pytest.raises(JevProviderError) as excinfo:
        jev.decide(REQUEST)
    error = excinfo.value.error
    assert error.kind is ProviderFailureKind.MALFORMED_RESPONSE
    assert fragment in error.message
    assert sleeps == []  # not retried


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_numbers_rejected(api_key: ApiKey, bad: str) -> None:
    text = json.dumps(LIVE_SHAPE).replace('"confidence": 1', f'"confidence": {bad}')
    assert bad in text
    jev, _ = client(api_key, Scripted(resp(200, text.encode())))
    with pytest.raises(JevProviderError) as excinfo:
        jev.decide(REQUEST)
    assert excinfo.value.error.kind is ProviderFailureKind.MALFORMED_RESPONSE
    cost_text = json.dumps(LIVE_SHAPE).replace("1.6338e-05", bad)
    jev, _ = client(api_key, Scripted(resp(200, cost_text.encode())))
    with pytest.raises(JevProviderError):
        jev.decide(REQUEST)


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, ProviderFailureKind.UNAUTHORIZED),
        (402, ProviderFailureKind.PAYMENT_REQUIRED),
        (403, ProviderFailureKind.FORBIDDEN),
        (400, ProviderFailureKind.BAD_REQUEST),
        (404, ProviderFailureKind.BAD_REQUEST),
    ],
)
def test_non_retryable_statuses(api_key: ApiKey, status: int, kind: ProviderFailureKind) -> None:
    transport = Scripted(resp(status, {"error": {"code": status, "message": "nope"}}))
    jev, sleeps = client(api_key, transport)
    with pytest.raises(JevProviderError) as excinfo:
        jev.decide(REQUEST)
    error = excinfo.value.error
    assert (error.kind, error.status, error.attempts, error.retryable) == (kind, status, 1, False)
    assert error.message == "nope"
    assert error.action
    assert sleeps == []


def test_error_messages_are_redacted(api_key: ApiKey) -> None:
    echo = {"error": {"code": 401, "message": f"bad key {api_key.reveal()}"}}
    jev, _ = client(api_key, Scripted(resp(401, echo)))
    with pytest.raises(JevProviderError) as excinfo:
        jev.decide(REQUEST)
    assert api_key.reveal() not in str(excinfo.value)
    assert api_key.reveal() not in excinfo.value.error.model_dump_json()


def test_error_in_200_body_is_mapped(api_key: ApiKey) -> None:
    jev, _ = client(api_key, Scripted(resp(200, {"error": {"code": 402, "message": "credits"}})))
    with pytest.raises(JevProviderError) as excinfo:
        jev.decide(REQUEST)
    assert excinfo.value.error.kind is ProviderFailureKind.PAYMENT_REQUIRED


def test_transient_failures_retry_then_succeed(api_key: ApiKey) -> None:
    transport = Scripted(
        TimeoutError(), resp(503, {"error": {"message": "busy"}}), resp(200, LIVE_SHAPE)
    )
    jev, sleeps = client(api_key, transport, backoff_seconds=(0.5, 2.0))
    result = jev.decide(REQUEST)
    assert result.attempts == 3
    assert sleeps == [0.5, 2.0]


@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (TimeoutError(), ProviderFailureKind.TIMEOUT),
        (OSError("connection reset"), ProviderFailureKind.NETWORK),
        (resp(502, b"<html>bad gateway</html>"), ProviderFailureKind.UNAVAILABLE),
    ],
)
def test_retries_are_bounded(
    api_key: ApiKey, failure: HttpResponse | BaseException, kind: ProviderFailureKind
) -> None:
    transport = Scripted(failure, failure, failure, resp(200, LIVE_SHAPE))
    jev, sleeps = client(api_key, transport, max_attempts=3)
    with pytest.raises(JevProviderError) as excinfo:
        jev.decide(REQUEST)
    assert excinfo.value.error.kind is kind
    assert excinfo.value.error.attempts == 3
    assert len(transport.bodies) == 3 and len(sleeps) == 2


def test_rate_limit_honors_short_retry_after(api_key: ApiKey) -> None:
    transport = Scripted(resp(429, {}, {"Retry-After": "1.5"}), resp(200, LIVE_SHAPE))
    jev, sleeps = client(api_key, transport)
    assert jev.decide(REQUEST).attempts == 2
    assert sleeps == [1.5]


def test_rate_limit_with_long_retry_after_is_returned(api_key: ApiKey) -> None:
    transport = Scripted(resp(429, {"error": {"message": "slow down"}}, {"retry-after": "120"}))
    jev, sleeps = client(api_key, transport)
    with pytest.raises(JevProviderError) as excinfo:
        jev.decide(REQUEST)
    error = excinfo.value.error
    assert error.kind is ProviderFailureKind.RATE_LIMITED
    assert error.retry_after_seconds == 120 and not error.retryable
    assert sleeps == []


def test_request_rejects_bad_option_names_and_nan_state() -> None:
    with pytest.raises(ValueError):
        ChoiceQuestion(instructions="x", criteria={"has space": "a", "b": "b"})
    with pytest.raises(ValueError):
        ChoiceQuestion(instructions="x", criteria={"only": "one"})
    request = DecisionRequest(state={"n": math.nan}, questions=REQUEST.questions)
    with pytest.raises(ValueError):
        request.body()


def test_client_bounds(api_key: ApiKey) -> None:
    make: Callable[..., JevClient] = lambda **kw: JevClient(api_key, **kw)  # noqa: E731
    with pytest.raises(ValueError):
        make(max_attempts=0)
    with pytest.raises(ValueError):
        make(max_attempts=10)
    with pytest.raises(ValueError):
        make(timeout_seconds=0)
    with pytest.raises(ValueError):
        make(url="http://openrouter.ai/api/alpha/decisions")
