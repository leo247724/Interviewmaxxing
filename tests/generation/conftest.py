"""Fixtures for the packet resolver tests (fictional data only).

Helpers are fixtures because the suite runs with ``--import-mode=importlib``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_core import (
    Application,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    BooleanValue,
    CandidateProfile,
    ChoiceValue,
    FileValue,
    JobRecord,
    MultiChoiceValue,
    PacketContext,
    TextValue,
    UserInput,
)
from interviewmaxxing_generation import FactualPacketResolver

GENERATION_FIXTURES = Path(__file__).parents[1] / "fixtures" / "generation"
NOW = datetime(2026, 9, 22, 20, 30, tzinfo=UTC)


def load_generation_fixture(name: str) -> Any:
    return json.loads((GENERATION_FIXTURES / name).read_text())


@pytest.fixture
def generation_fixture() -> Callable[[str], Any]:
    return load_generation_fixture


@pytest.fixture
def extended_candidate(fictional_candidate: CandidateProfile) -> CandidateProfile:
    """Avery Example plus the fictional facts and saved answers of
    ``candidate_additions.json``."""
    extra = load_generation_fixture("candidate_additions.json")
    data = fictional_candidate.model_dump(mode="json")
    data["facts"] += extra["facts"]
    data["saved_answers"] += extra["saved_answers"]
    return CandidateProfile.model_validate(data)


@pytest.fixture
def screening_form() -> ApplicationForm:
    return ApplicationForm.model_validate(load_generation_fixture("screening_form.json"))


MakeContext = Callable[..., PacketContext]


@pytest.fixture
def make_context(mock_job: JobRecord) -> MakeContext:
    def make(
        form: ApplicationForm,
        candidate: CandidateProfile,
        *,
        job: JobRecord | None = None,
        user_inputs: Sequence[UserInput] = (),
        application_id: str = "app_generation",
    ) -> PacketContext:
        job = job or mock_job
        application = Application(
            id=application_id, request_id="req_generation", job_id=job.id,
            candidate_id=candidate.id, state=ApplicationState.INSPECTING, version=2,
            created_at=NOW, updated_at=NOW,
        )
        return PacketContext(application=application, job=job, form=form, candidate=candidate,
                             user_inputs=user_inputs)

    return make


@pytest.fixture
def resolve() -> Callable[[PacketContext], ApplicationPacket]:
    resolver = FactualPacketResolver(clock=lambda: NOW)

    def run(context: PacketContext) -> ApplicationPacket:
        return asyncio.run(resolver.resolve(context))

    return run


def _value(value: Any) -> Any:
    if isinstance(value, TextValue):
        return value.text
    if isinstance(value, ChoiceValue):
        return [value.value, value.label]
    if isinstance(value, MultiChoiceValue):
        return [[c.value, c.label] for c in value.choices]
    if isinstance(value, BooleanValue):
        return value.checked
    if isinstance(value, FileValue):
        return {"filename": value.artifact.filename, "sha256": value.artifact.sha256}
    raise TypeError(value)


def project(packet: ApplicationPacket) -> dict[str, Any]:
    """The packet's decisions in a stable, comparable form (for golden files)."""
    return {
        "answers": {
            a.field_id: {
                "source": a.provenance.source.value,
                "refs": a.provenance.reference_ids,
                "value": _value(a.value),
            }
            for a in packet.answers
        },
        "missing": {
            m.field_id: {
                "reason": m.reason.value,
                "candidates": [_value(c) for c in m.candidates],
            }
            for m in packet.missing_inputs
        },
    }


@pytest.fixture
def projection() -> Callable[[ApplicationPacket], dict[str, Any]]:
    return project
