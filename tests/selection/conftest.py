"""Offline helpers for selection tests. Nothing here reaches the network."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_core import JobListing, SelectionPreferences
from interviewmaxxing_selection import (
    ApiKey,
    CandidateEvidence,
    HttpResponse,
    JevClient,
    SelectionService,
    SelectionStore,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "selection"
FAKE_KEY = "sk-or-v1-fictional-test-key-0000000000"


def load_listing(name: str) -> JobListing:
    data = json.loads((FIXTURES / "listings.json").read_text())
    return JobListing.model_validate(data[name])


@pytest.fixture
def listing() -> Callable[[str], JobListing]:
    return load_listing


@pytest.fixture
def prefs() -> SelectionPreferences:
    return SelectionPreferences()


@pytest.fixture
def candidate() -> CandidateEvidence:
    return CandidateEvidence(
        candidate_id="cand_fictional",
        verified_facts={
            "current_title": "Senior Marketing Manager",
            "years_experience.marketing": 9,
        },
        experience=[
            {"title": "Senior Marketing Manager", "start": "2019", "end": None, "current": True}
        ],
    )


@pytest.fixture
def api_key() -> ApiKey:
    return ApiKey(FAKE_KEY, source="test")


def ok(payload: dict[str, Any]) -> HttpResponse:
    return HttpResponse(200, {}, json.dumps(payload).encode())


def answer_for(
    question: Mapping[str, Any], choice: str | None, confidence: float = 0.95
) -> dict[str, Any]:
    if question["type"] == "noul":
        return {"type": "noul", "noul": 0.5}
    options = list(question["criteria"])
    chosen = choice or options[0]
    rest = [o for o in options if o != chosen]
    probs = {o: (1 - confidence) / len(rest) for o in rest}
    probs[chosen] = confidence
    return {"type": "choice", "choice": chosen, "probabilities": probs, "confidence": confidence}


@dataclass
class JevBot:
    """A fake Decisions API: answers each asked question with a scripted option."""

    choices: dict[str, str] = field(default_factory=dict)
    confidence: dict[str, float] = field(default_factory=dict)
    model: str = "typesafe/jev-1.13-20260917"
    calls: list[dict[str, Any]] = field(default_factory=list)
    headers: list[Mapping[str, str]] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)

    def __call__(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> HttpResponse:
        request = json.loads(body)
        self.calls.append(request)
        self.headers.append(headers)
        self.urls.append(url)
        answers = {
            name: answer_for(q, self.choices.get(name), self.confidence.get(name, 0.95))
            for name, q in request["questions"].items()
        }
        return ok(
            {
                "model": self.model,
                "answers": answers,
                "usage": {"input_tokens": 300, "output_tokens": 40, "cost": 0.0000145},
                "id": f"gen-dec-test-{len(self.calls)}",
                "provider": "TypeSafe",
            }
        )


GOOD = {
    "role_match": "match",
    "seniority_match": "at_target_level",
    "qualification_match": "meets",
    "location_eligibility": "eligible",
    "preference_match": "aligned",
    "listing_consistency": "consistent",
    "selection": "APPLY",
}


@pytest.fixture
def bot() -> JevBot:
    return JevBot(choices=dict(GOOD))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SelectionStore]:
    s = SelectionStore(tmp_path / "sel" / "selection.sqlite3")
    yield s
    s.close()


@pytest.fixture
def make_service(api_key: ApiKey, store: SelectionStore) -> Callable[..., SelectionService]:
    def make(transport: Any, **kwargs: Any) -> SelectionService:
        client = JevClient(api_key, transport=transport, sleep=lambda _s: None)
        return SelectionService(client=client, store=store, **kwargs)

    return make


@pytest.fixture
def new_bot() -> Callable[..., JevBot]:
    """``new_bot(**overrides)``: a JevBot answering GOOD except the overridden questions."""

    def make(confidence: dict[str, float] | None = None, **overrides: str) -> JevBot:
        return JevBot(choices={**GOOD, **overrides}, confidence=confidence or {})

    return make


@pytest.fixture
def http_ok() -> Callable[[dict[str, Any]], HttpResponse]:
    return ok


@pytest.fixture
def valid_answer() -> Callable[..., dict[str, Any]]:
    return answer_for
