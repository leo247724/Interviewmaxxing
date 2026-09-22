"""Fixtures for browser-runtime tests: a localhost mock ATS, real headless Chromium
sessions, and a packet builder with honest provenance.

Everything is local and fictional (Brambleway Analytics, candidate Avery Quill).
The runtime under test never calls the mock's ``/__test__/`` API; only these
fixtures and assertions do.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import urllib.request
from collections.abc import Callable, Coroutine, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_core import (
    PROFILE_IDENTITY_TYPES,
    AnswerSource,
    AnswerValue,
    ApplicationForm,
    ApplicationPacket,
    ArtifactRef,
    BooleanValue,
    BrowserOptions,
    ChoiceValue,
    ControlType,
    FileValue,
    MissingInput,
    MissingReason,
    MultiChoiceValue,
    PacketAnswer,
    Provenance,
    SemanticType,
    TextValue,
    UserInput,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "browser"
CANDIDATE: dict[str, Any] = json.loads((FIXTURES / "candidate.json").read_text("utf-8"))
RESUME_PATH = FIXTURES / CANDIDATE["resume"]["path"]
RESUME_ID = "resume.avery-quill"

def _load_mock_ats() -> ModuleType:
    if "mock_ats" in sys.modules:
        return sys.modules["mock_ats"]
    spec = importlib.util.spec_from_file_location("mock_ats", REPO / "scripts" / "mock_ats.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["mock_ats"] = module
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


mock_ats = _load_mock_ats()


def run[R](coro: Coroutine[Any, Any, R]) -> R:
    return asyncio.run(coro)


class MockServer:
    """Handle on the in-process mock ATS; ``api`` is for test assertions only."""

    def __init__(self, ats: Any) -> None:
        self.ats = ats
        self.origin: str = ats.origin

    def url(self, path: str) -> str:
        return f"{self.origin}{path}"

    def api(self, method: str, path: str) -> Any:
        request = urllib.request.Request(self.url(path), method=method, data=b"" if method == "POST" else None)
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def submissions(self, job_id: str | None = None) -> dict[str, Any]:
        query = f"?job_id={job_id}" if job_id else ""
        result: dict[str, Any] = self.api("GET", f"/__test__/submissions{query}")
        return result


@pytest.fixture(scope="module")
def mock_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[MockServer]:
    ats = mock_ats.MockATS(port=0, state_dir=tmp_path_factory.mktemp("mock-ats") / "state").start()
    try:
        yield MockServer(ats)
    finally:
        ats.stop()


@pytest.fixture
def server(mock_server: MockServer) -> MockServer:
    mock_server.api("POST", "/__test__/reset")
    return mock_server


@pytest.fixture
def options(tmp_path: Path) -> BrowserOptions:
    root = tmp_path / "artifacts"
    return BrowserOptions(artifacts_dir=root / "app_test", artifacts_root=root, headless=True)


@pytest.fixture
def resume() -> ArtifactRef:
    return ArtifactRef.from_file(RESUME_PATH, id=RESUME_ID, media_type="application/pdf")


# --- packets --------------------------------------------------------------------------


RESUME = object()
"""Marker value: answer a FILE field with the supplied resume."""


@dataclass
class BuiltPacket:
    packet: ApplicationPacket
    user_inputs: list[UserInput]


def _value_for(form: ApplicationForm, field_id: str, spec: Any, resume: ArtifactRef) -> AnswerValue:
    field = form.field(field_id)
    ctype = field.control_type
    if spec is RESUME:
        return FileValue(artifact=resume)
    if ctype in (ControlType.TEXT, ControlType.TEXTAREA):
        return TextValue(text=spec)
    if ctype is ControlType.CHECKBOX:
        return BooleanValue(checked=bool(spec))
    options = {o.label: o for o in field.options or []}
    if ctype in (ControlType.SELECT, ControlType.RADIO):
        option = options[spec]
        return ChoiceValue(value=option.value, label=option.label)
    return MultiChoiceValue(choices=[options[label] for label in spec])


def build_packet(
    form: ApplicationForm,
    answers: Mapping[str, Any],
    resume: ArtifactRef,
    *,
    application_id: str = "app_test",
    job_id: str = "job_test",
    candidate_id: str = "cand_fixture_avery_quill",
    source_overrides: Mapping[str, Provenance] | None = None,
    semantic_overrides: Mapping[str, SemanticType] | None = None,
) -> BuiltPacket:
    """Answer ``form`` by field id. Identity fields come from the profile, the file
    from the supplied resume, everything else from the (fictional) user's input.
    Unanswered required fields are reported missing, never guessed."""
    packet_answers: list[PacketAnswer] = []
    inputs: list[UserInput] = []
    for field_id, spec in answers.items():
        field = form.field(field_id)
        value = _value_for(form, field_id, spec, resume)
        if source_overrides and field_id in source_overrides:
            provenance = source_overrides[field_id]
        elif spec is RESUME:
            provenance = Provenance(source=AnswerSource.RESUME, reference_ids=[resume.id])
        elif field.semantic_type in PROFILE_IDENTITY_TYPES:
            provenance = Provenance(source=AnswerSource.PROFILE_IDENTITY)
        else:
            user = UserInput.for_field(form, field_id, value)
            inputs.append(user)
            provenance = Provenance(source=AnswerSource.USER_INPUT, reference_ids=[user.id])
        packet_answers.append(
            PacketAnswer(
                field_id=field_id,
                semantic_type=(semantic_overrides or {}).get(field_id, field.semantic_type),
                value=value,
                provenance=provenance,
            )
        )
    missing = [
        MissingInput.for_field(form, f, reason=MissingReason.NO_ANSWER, prompt=f"Answer {f.label}")
        for f in form.fields
        if f.required and f.id not in answers
    ]
    packet = ApplicationPacket(
        application_id=application_id,
        job_id=job_id,
        candidate_id=candidate_id,
        form_url=form.url,
        form_step=form.step,
        form_fingerprint=form.fingerprint,
        answers=packet_answers,
        missing_inputs=missing,
    )
    return BuiltPacket(packet, inputs)


IDENTITY = CANDIDATE["identity"]
CORE_ANSWERS: dict[str, Any] = {
    "first_name": IDENTITY["first_name"],
    "last_name": IDENTITY["last_name"],
    "email": IDENTITY["email"],
    "phone": IDENTITY["phone"],
    "linkedin_url": IDENTITY["linkedin_url"],
    "resume": RESUME,
    "work_authorization": "Yes, I am authorized to work in the US",
    "sponsorship": "No, I will not require sponsorship",
}
STANDARD_EXTRA: dict[str, Any] = {
    "years_experience": "6 to 9 years",
    "skills": ["Python", "SQL", "Apache Spark", "dbt"],
    "work_arrangements": ["Remote", "Hybrid"],
    "why_brambleway": CANDIDATE["saved_answers"]["why_brambleway"],
}


def pick(form: ApplicationForm, answers: Mapping[str, Any]) -> dict[str, Any]:
    """The subset of ``answers`` that are questions on this form step."""
    ids = {f.id for f in form.fields}
    return {k: v for k, v in answers.items() if k in ids}


@pytest.fixture
def packet_for(resume: ArtifactRef) -> Callable[..., BuiltPacket]:
    def make(form: ApplicationForm, answers: Mapping[str, Any], **kwargs: Any) -> BuiltPacket:
        return build_packet(form, answers, resume, **kwargs)

    return make


def field_ids(form: ApplicationForm) -> Sequence[str]:
    return [f.id for f in form.fields]


@pytest.fixture
def kit(resume: ArtifactRef) -> SimpleNamespace:
    """Helpers for test modules (``--import-mode=importlib`` forbids importing conftest)."""

    def build(form: ApplicationForm, answers: Mapping[str, Any], **kwargs: Any) -> BuiltPacket:
        return build_packet(form, answers, resume, **kwargs)

    return SimpleNamespace(
        RESUME=RESUME,
        CORE=CORE_ANSWERS,
        STANDARD=STANDARD_EXTRA,
        CANDIDATE=CANDIDATE,
        RESUME_PATH=RESUME_PATH,
        pick=pick,
        run=run,
        build=build,
        mock_ats=mock_ats,
    )
