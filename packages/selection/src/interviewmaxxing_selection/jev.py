"""Typed client for Jev on the OpenRouter Decisions API.

``POST https://openrouter.ai/api/alpha/decisions`` takes ``{model, state, questions}``
and returns ``{answers, model, provider, usage}``. Jev is *not* a chat model: never send
it to ``/chat/completions`` and do not use ``/api/v1/models`` membership to reject it
(the ordinary catalogue omits decision models). The requested model ID is pinned; the
returned ID (e.g. ``typesafe/jev-1.13-20260917``) is recorded separately.

References: https://openrouter.ai/typesafe/jev-1.13/api,
https://docs.typesafe.ai/primitives/choice, https://docs.typesafe.ai/confidence.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    JsonValue,
    StringConstraints,
    ValidationError,
)

from interviewmaxxing_core import DEFAULT_JEV_MODEL

from .credentials import ApiKey

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = DEFAULT_JEV_MODEL
"""``typesafe/jev-1.13``: pinned by default; the returned dated ID is recorded."""
PROBABILITY_SUM_TOLERANCE = 0.01
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_ERROR_MESSAGE = 500

OptionKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]
Instructions = Annotated[str, StringConstraints(min_length=1, max_length=4000)]
Probability = Annotated[FiniteFloat, Field(ge=0.0, le=1.0)]


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- request


class ChoiceQuestion(_Wire):
    """Pick one option. ``criteria`` maps option name -> what it means."""

    type: Literal["choice"] = "choice"
    instructions: Instructions
    criteria: dict[OptionKey, Instructions] = Field(min_length=2, max_length=255)


class NoulQuestion(_Wire):
    """A yes/no question answered with a probability."""

    type: Literal["noul"] = "noul"
    instructions: Instructions


Question = Annotated[ChoiceQuestion | NoulQuestion, Field(discriminator="type")]


class DecisionRequest(_Wire):
    model: Annotated[str, StringConstraints(min_length=1)] = DEFAULT_MODEL
    state: dict[OptionKey, JsonValue]
    """Data the questions are about. Untrusted text belongs here, never in instructions."""
    questions: dict[OptionKey, Question] = Field(min_length=1)

    def body(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, allow_nan=False
        ).encode("utf-8")


# -------------------------------------------------------------------------- response


class ChoiceAnswer(_Wire):
    type: Literal["choice"]
    choice: str
    confidence: Probability
    probabilities: dict[str, Probability]


class NoulAnswer(_Wire):
    type: Literal["noul"]
    noul: Probability


Answer = Annotated[ChoiceAnswer | NoulAnswer, Field(discriminator="type")]


class DecisionUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    cost: Annotated[FiniteFloat, Field(ge=0.0)] | None = None
    """USD, as reported by the provider. ``None`` when not reported."""
    input_tokens: Annotated[
        int, Field(ge=0, validation_alias=AliasChoices("input_tokens", "inputTokens"))
    ] = 0
    output_tokens: Annotated[
        int, Field(ge=0, validation_alias=AliasChoices("output_tokens", "outputTokens"))
    ] = 0
    """The live API returns snake_case; published examples show camelCase. Both parse."""


class DecisionResponse(BaseModel):
    """The provider's decision. Unknown top-level fields are tolerated (kept in raw)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    answers: dict[str, Answer]
    model: Annotated[str, StringConstraints(min_length=1)]
    """The returned model version, e.g. ``typesafe/jev-1.13-20260917``."""
    provider: str | None = None
    usage: DecisionUsage | None = None
    id: str | None = None
    """Provider generation ID, e.g. ``gen-dec-...``."""

    def check_against(self, request: DecisionRequest) -> None:
        """Raise ``ValueError`` unless every requested question has a well-formed,
        correctly typed answer covering exactly the requested options."""
        missing = sorted(set(request.questions) - set(self.answers))
        extra = sorted(set(self.answers) - set(request.questions))
        if missing or extra:
            raise ValueError(f"answers do not match questions (missing={missing}, extra={extra})")
        for name, question in request.questions.items():
            answer = self.answers[name]
            if answer.type != question.type:
                raise ValueError(f"{name}: expected {question.type} answer, got {answer.type}")
            if isinstance(question, ChoiceQuestion) and isinstance(answer, ChoiceAnswer):
                _check_choice(name, question, answer)

    def choice(self, name: str) -> ChoiceAnswer:
        answer = self.answers[name]
        if not isinstance(answer, ChoiceAnswer):
            raise TypeError(f"{name} is not a choice answer")
        return answer


def _check_choice(name: str, question: ChoiceQuestion, answer: ChoiceAnswer) -> None:
    options = set(question.criteria)
    if answer.choice not in options:
        raise ValueError(f"{name}: choice {answer.choice!r} is not an offered option")
    keys = set(answer.probabilities)
    if keys != options:
        raise ValueError(f"{name}: probabilities cover {sorted(keys)}, expected {sorted(options)}")
    total = math.fsum(answer.probabilities.values())
    if abs(total - 1.0) > PROBABILITY_SUM_TOLERANCE:
        raise ValueError(f"{name}: probabilities sum to {total:.4f}, not 1")
    if answer.probabilities[answer.choice] + 1e-6 < max(answer.probabilities.values()):
        raise ValueError(f"{name}: choice is not the most probable option")


# ---------------------------------------------------------------------------- errors


class ProviderFailureKind(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    UNAVAILABLE = "UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    BAD_REQUEST = "BAD_REQUEST"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"


_ACTIONS: dict[ProviderFailureKind, str] = {
    ProviderFailureKind.NOT_CONFIGURED: "Configure OPENROUTER_API_KEY via IMX_OPENROUTER_ENV_FILE.",
    ProviderFailureKind.UNAUTHORIZED: "Check the OpenRouter API key in the configured env file.",
    ProviderFailureKind.FORBIDDEN: "The key may not use this model or endpoint; check OpenRouter settings.",
    ProviderFailureKind.PAYMENT_REQUIRED: "Add OpenRouter credits, then retry selection.",
    ProviderFailureKind.RATE_LIMITED: "Rate limited; retry selection later.",
    ProviderFailureKind.UNAVAILABLE: "Jev is temporarily unavailable; retry selection later.",
    ProviderFailureKind.TIMEOUT: "Jev did not answer in time; retry selection later.",
    ProviderFailureKind.NETWORK: "Network error reaching OpenRouter; check connectivity and retry.",
    ProviderFailureKind.BAD_REQUEST: "The decision request was rejected; report this as a bug.",
    ProviderFailureKind.MALFORMED_RESPONSE: "Jev returned an invalid answer; retry or review manually.",
}


class ProviderFailure(_Wire):
    """Why a Jev call produced no usable decision. Never contains the API key."""

    kind: ProviderFailureKind
    status: int | None = None
    message: str = ""
    retryable: bool = False
    attempts: int = 0
    retry_after_seconds: FiniteFloat | None = None

    @property
    def action(self) -> str:
        return _ACTIONS[self.kind]


class JevProviderError(Exception):
    def __init__(self, error: ProviderFailure) -> None:
        super().__init__(f"{error.kind}: {error.message}")
        self.error = error


# ------------------------------------------------------------------------- transport


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[str, Mapping[str, str], bytes, float], HttpResponse]
"""``(url, headers, body, timeout_seconds) -> HttpResponse``. Raises ``TimeoutError``
for timeouts and ``OSError`` for other network failures."""


def urllib_transport(
    url: str, headers: Mapping[str, str], body: bytes, timeout: float
) -> HttpResponse:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(
                response.status, dict(response.headers), response.read(_MAX_RESPONSE_BYTES)
            )
    except urllib.error.HTTPError as exc:
        with exc:
            return HttpResponse(exc.code, dict(exc.headers or {}), exc.read(_MAX_RESPONSE_BYTES))
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TimeoutError("request timed out") from None
        raise OSError(f"network error: {type(exc.reason).__name__}") from None


# ---------------------------------------------------------------------------- client


@dataclass(frozen=True, slots=True)
class DecisionResult:
    request: DecisionRequest
    response: DecisionResponse
    raw: dict[str, Any]
    """The provider JSON as received (answers, model, provider, usage, ...)."""
    attempts: int
    latency_seconds: float


_RETRY_STATUSES = frozenset({408, 500, 502, 503, 504, 520, 522, 524, 529})


@dataclass
class JevClient:
    """Calls the Decisions API with an explicit timeout and bounded retries.

    Transient failures (timeouts, network errors, 408/5xx, 429 with a short
    ``Retry-After``) are retried up to ``max_attempts``. 401/402/403 and other 4xx are
    returned immediately as actionable :class:`ProviderFailure` states.
    """

    api_key: ApiKey
    url: str = DECISIONS_URL
    timeout_seconds: float = 20.0
    max_attempts: int = 3
    backoff_seconds: tuple[float, ...] = (0.5, 2.0)
    max_retry_after_seconds: float = 10.0
    app_title: str = "Interviewmaxxing"
    transport: Transport = field(default=urllib_transport)
    sleep: Callable[[float], None] = field(default=time.sleep)
    monotonic: Callable[[], float] = field(default=time.monotonic)

    def __post_init__(self) -> None:
        if not self.url.startswith("https://") or "/chat/completions" in self.url:
            raise ValueError("Jev must be called through the HTTPS Decisions API")
        if not (1 <= self.max_attempts <= 5):
            raise ValueError("max_attempts must be between 1 and 5")
        if not (0 < self.timeout_seconds <= 120):
            raise ValueError("timeout_seconds must be in (0, 120]")

    def decide(self, request: DecisionRequest) -> DecisionResult:
        """Return a validated decision or raise :class:`JevProviderError`."""
        headers = {
            "Authorization": f"Bearer {self.api_key.reveal()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Title": self.app_title,
        }
        body = request.body()
        started = self.monotonic()
        last: ProviderFailure | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.transport(self.url, headers, body, self.timeout_seconds)
            except TimeoutError:
                last = ProviderFailure(
                    kind=ProviderFailureKind.TIMEOUT,
                    message=f"no response within {self.timeout_seconds:g}s",
                    retryable=True,
                    attempts=attempt,
                )
            except OSError as exc:
                last = ProviderFailure(
                    kind=ProviderFailureKind.NETWORK,
                    message=self._clean(str(exc) or type(exc).__name__),
                    retryable=True,
                    attempts=attempt,
                )
            else:
                outcome = self._interpret(request, response, attempt)
                if isinstance(outcome, DecisionResponse):
                    raw = json.loads(response.body)
                    return DecisionResult(
                        request=request,
                        response=outcome,
                        raw=raw,
                        attempts=attempt,
                        latency_seconds=self.monotonic() - started,
                    )
                last = outcome
            if not last.retryable or attempt == self.max_attempts:
                break
            self.sleep(self._delay(attempt, last))
        assert last is not None
        raise JevProviderError(last)

    # -- helpers ---------------------------------------------------------------

    def _delay(self, attempt: int, error: ProviderFailure) -> float:
        if error.retry_after_seconds is not None:
            return error.retry_after_seconds
        index = min(attempt - 1, len(self.backoff_seconds) - 1)
        return self.backoff_seconds[index] if self.backoff_seconds else 0.0

    def _clean(self, text: str) -> str:
        return self.api_key.redact(text)[:_MAX_ERROR_MESSAGE]

    def _interpret(
        self, request: DecisionRequest, response: HttpResponse, attempt: int
    ) -> DecisionResponse | ProviderFailure:
        payload: Any = None
        try:
            payload = json.loads(response.body)
        except ValueError:
            payload = None
        status = response.status
        if status == 200 and isinstance(payload, dict) and "error" in payload:
            status = _error_code(payload) or 502
        if status == 200:
            return self._parse(request, payload, attempt)
        message = self._clean(_error_message(payload) or f"HTTP {status}")
        if status == 401:
            return ProviderFailure(
                kind=ProviderFailureKind.UNAUTHORIZED,
                status=status,
                message=message,
                attempts=attempt,
            )
        if status == 402:
            return ProviderFailure(
                kind=ProviderFailureKind.PAYMENT_REQUIRED,
                status=status,
                message=message,
                attempts=attempt,
            )
        if status == 403:
            return ProviderFailure(
                kind=ProviderFailureKind.FORBIDDEN, status=status, message=message, attempts=attempt
            )
        if status == 429:
            retry_after = _retry_after(response.headers)
            short = retry_after is None or retry_after <= self.max_retry_after_seconds
            return ProviderFailure(
                kind=ProviderFailureKind.RATE_LIMITED,
                status=status,
                message=message,
                retryable=short,
                attempts=attempt,
                retry_after_seconds=retry_after,
            )
        if status in _RETRY_STATUSES or status >= 500:
            return ProviderFailure(
                kind=ProviderFailureKind.UNAVAILABLE,
                status=status,
                message=message,
                retryable=True,
                attempts=attempt,
            )
        return ProviderFailure(
            kind=ProviderFailureKind.BAD_REQUEST, status=status, message=message, attempts=attempt
        )

    def _parse(
        self, request: DecisionRequest, payload: Any, attempt: int
    ) -> DecisionResponse | ProviderFailure:
        try:
            if not isinstance(payload, dict):
                raise ValueError("response body is not a JSON object")
            decision = DecisionResponse.model_validate(payload)
            decision.check_against(request)
        except (ValidationError, ValueError) as exc:
            detail = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
            return ProviderFailure(
                kind=ProviderFailureKind.MALFORMED_RESPONSE,
                status=200,
                message=self._clean(detail),
                attempts=attempt,
            )
        return decision


def _error_code(payload: Mapping[str, Any]) -> int | None:
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, int) and 400 <= code <= 599 else None


def _error_message(payload: Any) -> str | None:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return str(error["message"])
        if isinstance(error, str):
            return error
    return None


def _retry_after(headers: Mapping[str, str]) -> float | None:
    for name, value in headers.items():
        if name.lower() == "retry-after":
            try:
                seconds = float(value)
            except ValueError:
                return None
            return seconds if math.isfinite(seconds) and seconds >= 0 else None
    return None
