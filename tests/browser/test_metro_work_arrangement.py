"""Round 13: the work arrangement follows the job's metro (``metro_area``).

The owner's rule: for a job in his metro (his city and the towns he lists) in-person or hybrid
work is fine, whatever the job requires; for any job elsewhere the answer is remote. The
candidate is the fictional Avery Example, moved to Austin, TX for these tests, with the
towns and preferences imported through the simple-answers map. Fields are typed by the browser
heuristics and Jev's annotation; the wordings are the live ones the brief quotes (employer
names are fictional). Nothing is submitted.
"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from interviewmaxxing_browser.ai import (
    AIFormRouter,
    BoundedDecisions,
    CallBudget,
    DynamicPacketResolver,
)
from interviewmaxxing_browser.ai.metro import (
    MetroVerdict,
    job_place,
    metro_places,
    person_metro,
    posting_mode,
    read_place,
)
from interviewmaxxing_browser.semantics import classify as classify_semantics
from interviewmaxxing_candidate.simple_answers import SimpleAnswers
from interviewmaxxing_core import (
    AnswerScope,
    AnswerSource,
    Application,
    ApplicationField,
    ApplicationForm,
    ApplicationState,
    CandidateProfile,
    ChoiceValue,
    ControlType,
    FieldOption,
    JobRecord,
    MultiChoiceValue,
    PacketContext,
    PostalAddress,
    SavedAnswer,
)
from interviewmaxxing_core.preferences import METRO_AREA_QUESTION
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
METRO = ("Round Rock, Cedar Park, Leander, Pflugerville, Georgetown, Hutto, Kyle, Buda, San Marcos, "
         "Lakeway, Bee Cave, Dripping Springs")
YES_NO = ("Yes", "No")
R, S, CB = ControlType.RADIO, ControlType.SELECT, ControlType.CHECKBOX_GROUP

ASHBY_ONSITE = ("Base is a fully in-person work environment. This role requires working on-site "
                "five days per week at the Austin office. Are you able to meet this requirement?")
OFFICES = ("Burlingame, CA", "Columbus, OH", "Austin, TX", "New York City, NY", "Remote")
NO_AUSTIN = ("Burlingame, CA", "Columbus, OH", "New York City, NY", "Remote")
RELOCATE_Q = "If you are not currently based in Austin, would you be willing to relocate?"
RELOCATE_OPTIONS = ("I'm based in Austin", "Yes", "No")
SF_ONSITE = ("This role requires working on-site three days a week in our San Francisco office. "
             "Are you able to meet this requirement?")
MODES = ("Remote", "Hybrid", "On-site")
MODE_Q = "Which work arrangement do you prefer?"


# --- a scripted Jev --------------------------------------------------------------------------------


@dataclass
class Script:
    route: str = "HUMAN_INPUT"
    scope: str = "EXPLICIT_ANSWER"
    semantic: str | None = None
    picks: dict[str, str] = field(default_factory=dict)
    """Decision name -> a fragment of the option label Jev picks (at 0.99)."""


class Jev:
    """Classifies each field from its script and answers every other decision with its
    no-answer choice unless the script names a pick."""

    def __init__(self, scripts: dict[str, Script] | None = None) -> None:
        self.scripts = scripts or {}
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def script(self, text: str) -> Script:
        found = [s for fragment, s in self.scripts.items() if fragment in text]
        return found[0] if found else Script()

    def asked(self, name: str) -> list[dict[str, Any]]:
        with self.lock:
            return [r for r in self.requests if name in r["questions"]]

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        with self.lock:
            self.requests.append(request)
        state = request["state"]
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.0}
                continue
            criteria = list(question["criteria"])
            choice, probability = self._choice(name, state, criteria)
            rest = (1 - probability) / (len(criteria) - 1)
            answers[name] = {"type": "choice", "choice": choice, "confidence": 0.97 if probability < 1 else 1.0,
                             "probabilities": {k: probability if k == choice else rest for k in criteria}}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())

    def _choice(self, name: str, state: dict[str, Any], criteria: list[str]) -> tuple[str, float]:
        if name[0] in "rnusd" and name[1:].isdigit():
            data = state["fields"][f"f{name[1:]}"]
            script = self.script(data["question"])
            if name[0] == "s":
                semantic = script.semantic or {"TEXTAREA": "CUSTOM_LONG_TEXT", "TEXT": "CUSTOM_TEXT"}.get(
                    data["control"], "CUSTOM_BOOLEAN")
                return semantic, 1.0
            return {"r": script.route, "n": "literal", "u": script.scope,
                    "d": "APPLICATION_ATTACHMENT"}[name[0]], 1.0
        text = state.get("question") if isinstance(state.get("question"), str) else ""
        script = self.script(str(text))
        if name in script.picks:
            options: dict[str, str] = state.get("options", {})
            key = next(k for k, label in options.items() if script.picks[name] in label)
            return key, 0.99
        return next(c for c in ("NONE", "UNKNOWN", "hold", criteria[0]) if c in criteria), 1.0


def decisions(provider: Jev) -> BoundedDecisions:
    return BoundedDecisions(JevClient(ApiKey("synthetic-key", source="test"),
        transport=provider, max_attempts=1), CallBudget())


# --- the fictional Austin profile and jobs ----------------------------------------------------------


def austin_candidate(base: CandidateProfile, *, metro: str | None = METRO,
                     relocate: str | None = None, preference: str | None = "remote") -> CandidateProfile:
    """Avery Example in Austin, TX, with the metro towns, preference and relocation answer
    imported through the simple-answers map."""
    identity = base.identity.model_copy(update={"address": PostalAddress(
        city="Austin", region="TX", postal_code="78701", country="United States")})
    data = SimpleAnswers.from_identity(identity).model_dump()
    data.update(metro_area=metro, willing_to_relocate=relocate, work_arrangement_preference=preference)
    imported = SimpleAnswers.model_validate(data).saved_answer_updates(confirmed_at=NOW)
    return base.model_copy(update={"identity": identity,
                                   "saved_answers": [*base.saved_answers, *imported]})


def job_at(base: JobRecord, location: str | None, title: str = "Senior Paid Media Manager") -> JobRecord:
    return base.model_copy(update={"location": location, "title": title})


def typed_field(label: str, control: ControlType, options: tuple[str, ...], *,
                field_id: str = "q") -> ApplicationField:
    semantic = classify_semantics(label=label, control_type=control)
    return ApplicationField(id=field_id, selector=f"#{field_id}", label=label, semantic_type=semantic,
        control_type=control, required=True,
        options=[FieldOption(value=f"v{i}", label=o) for i, o in enumerate(options)])


def resolve(provider: Jev, candidate: CandidateProfile, job: JobRecord,
            *fields: ApplicationField) -> tuple[Any, PacketContext, DynamicPacketResolver]:
    router = AIFormRouter(decisions(provider))
    annotated = router.annotate(ApplicationForm(url="https://example.test/apply", fields=list(fields)),
                                document_id="metro")
    app = Application(id="app-metro", request_id="request-metro", job_id=job.id,
        candidate_id=candidate.id, state=ApplicationState.INSPECTING, version=2,
        created_at="2026-09-25T00:00:00Z", updated_at="2026-09-25T00:00:00Z")
    ctx = PacketContext(application=app, job=job, form=annotated, candidate=candidate)
    resolver = DynamicPacketResolver(router.decisions, router=router)
    packet = asyncio.run(resolver.resolve(ctx))
    assert ctx.problems(packet) == []
    return packet, ctx, resolver


def arrangement_traces(resolver: DynamicPacketResolver) -> list[dict[str, Any]]:
    return [t for t in resolver.narrative_traces if t["stage"] == "work_arrangement"]


def metro_id(candidate: CandidateProfile) -> str:
    return next(a.id for a in candidate.saved_answers if a.question == METRO_AREA_QUESTION)


def label_of(answer: Any) -> str | list[str]:
    if isinstance(answer.value, MultiChoiceValue):
        return [c.label for c in answer.value.choices]
    assert isinstance(answer.value, ChoiceValue)
    return answer.value.label


# --- the live wordings ----------------------------------------------------------------------------


@pytest.mark.parametrize("location", ["Austin, TX", "Austin, Texas, United States", None])
@pytest.mark.parametrize("semantic", [None, "LOCATION"], ids=["heuristics", "jev-location"])
def test_the_austin_in_person_question_is_yes_for_a_person_in_the_metro(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, location: str | None,
    semantic: str | None,
) -> None:
    """"This role requires working on-site five days per week at the Austin office": in the
    metro in-person work is fine, although the saved preference is remote."""
    candidate = austin_candidate(fictional_candidate)
    provider = Jev({ASHBY_ONSITE[:40]: Script(semantic=semantic)})
    packet, ctx, resolver = resolve(provider, candidate, job_at(mock_job, location),
                                    typed_field(ASHBY_ONSITE, R, YES_NO))
    assert ctx.form.field("q").semantic_type.value == (semantic or "CUSTOM_BOOLEAN")
    [answer] = packet.answers
    assert label_of(answer) == "Yes"
    assert answer.provenance.source is AnswerSource.SAVED_ANSWER
    assert answer.provenance.reference_ids == [metro_id(candidate)]
    [trace] = arrangement_traces(resolver)
    assert (trace["question"], trace["metro"], trace["place_source"], trace["status"]) == (
        "onsite_city", "IN_METRO", "question", "ANSWERED")
    assert trace["place"] == "Austin"


@pytest.mark.parametrize(("relocate", "expected"), [(None, "No"), ("No", "No"), ("Yes", "Yes")])
def test_an_on_site_question_outside_the_metro_is_no_unless_the_person_relocates(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, relocate: str | None, expected: str,
) -> None:
    candidate = austin_candidate(fictional_candidate, relocate=relocate)
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job, "San Francisco, CA"),
                                  typed_field(SF_ONSITE, R, YES_NO))
    [answer] = packet.answers
    assert label_of(answer) == expected
    [trace] = arrangement_traces(resolver)
    assert (trace["metro"], trace["place"]) == ("OUTSIDE", "San Francisco")
    assert trace["relocation"] == ({"Yes": "yes", "No": "no"}.get(relocate or "", "unstated"))


def test_an_on_site_question_naming_a_metro_town_is_yes(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    label = "This position requires working in person at our Round Rock, TX facility. Can you work on-site?"
    candidate = austin_candidate(fictional_candidate)
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job, None), typed_field(label, R, YES_NO))
    [answer] = packet.answers
    assert label_of(answer) == "Yes"
    assert arrangement_traces(resolver)[0]["place"] == "Round Rock"


def test_austin_minnesota_is_not_the_metro(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    label = "This role requires working on-site in Austin, MN. Are you able to work on-site?"
    candidate = austin_candidate(fictional_candidate)
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job, "Austin, MN"),
                                  typed_field(label, R, YES_NO))
    [answer] = packet.answers
    assert label_of(answer) == "No"
    assert arrangement_traces(resolver)[0]["metro"] == "OUTSIDE"


@pytest.mark.parametrize("control", [S, CB])
def test_the_location_preference_list_takes_the_austin_office(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, control: ControlType,
) -> None:
    candidate = austin_candidate(fictional_candidate)
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job, "New York, NY"),
                                  typed_field("Location Preference", control, OFFICES))
    [answer] = packet.answers
    assert label_of(answer) == (["Austin, TX"] if control is CB else "Austin, TX")
    assert answer.provenance.reference_ids == [metro_id(candidate)]
    [trace] = arrangement_traces(resolver)
    assert (trace["question"], trace["metro"], trace["place"]) == ("office_location", "IN_METRO", "Austin")


@pytest.mark.parametrize("control", [S, CB])
def test_the_location_preference_list_without_an_austin_office_takes_remote(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, control: ControlType,
) -> None:
    candidate = austin_candidate(fictional_candidate)
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job, "Austin, TX"),
                                  typed_field("Location Preference", control, NO_AUSTIN))
    [answer] = packet.answers
    assert label_of(answer) == (["Remote"] if control is CB else "Remote")
    assert arrangement_traces(resolver)[0]["metro"] == "OUTSIDE"


def test_an_office_list_without_the_metro_or_remote_holds(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    candidate = austin_candidate(fictional_candidate)
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job, "Austin, TX"),
        typed_field("Location Preference", S, ("Burlingame, CA", "Columbus, OH", "New York City, NY")))
    assert packet.answers == [] and len(packet.missing_inputs) == 1
    assert arrangement_traces(resolver)[0]["status"] == "NO_OPTION"


def test_a_relocation_question_naming_the_persons_city_takes_based_there(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    """"If you are not currently based in Austin, would you be willing to relocate?": the
    relocation screener picks "I'm based in Austin", true for the verified address."""
    candidate = austin_candidate(fictional_candidate)
    provider = Jev({RELOCATE_Q[:40]: Script(route="COPY_KNOWN", scope="APPLICANT_CURRENT",
                                            picks={"relocation": "based in Austin"})})
    field_ = typed_field(RELOCATE_Q, R, RELOCATE_OPTIONS)
    packet, ctx, resolver = resolve(provider, candidate, job_at(mock_job, "Austin, TX"), field_)
    assert ctx.form.field("q").semantic_type.value == "RELOCATION"
    [answer] = packet.answers
    assert label_of(answer) == "I'm based in Austin"
    assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY
    [trace] = [t for t in resolver.narrative_traces if t["stage"] == "relocation_screener"]
    assert trace["status"] == "ANSWERED"


def test_a_relocation_question_naming_another_city_is_not_answered_from_the_address(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    label = "If you are not currently based in Denver, would you be willing to relocate?"
    candidate = austin_candidate(fictional_candidate)
    provider = Jev({label[:40]: Script(route="COPY_KNOWN", scope="APPLICANT_CURRENT",
                                       picks={"relocation": "based in Denver"})})
    packet, _, resolver = resolve(provider, candidate, job_at(mock_job, "Denver, CO"),
                                  typed_field(label, R, ("I'm based in Denver", "Yes", "No")))
    assert not [t for t in resolver.narrative_traces if t["stage"] == "relocation_screener"]
    assert all(label_of(a) != "I'm based in Denver" for a in packet.answers)


# --- remote / hybrid / on-site selects ---------------------------------------------------------------


@pytest.mark.parametrize(("location", "title", "expected", "verdict"), [
    ("Austin, TX (Hybrid)", "Senior Paid Media Manager", "Hybrid", "IN_METRO"),
    ("Austin, TX", "Senior Paid Media Manager - Onsite", "On-site", "IN_METRO"),
    ("Austin, TX", "Senior Paid Media Manager", "Hybrid", "IN_METRO"),
    ("Remote (Austin, TX)", "Senior Paid Media Manager", "Remote", "IN_METRO"),
    ("New York, NY", "Senior Paid Media Manager", "Remote", "OUTSIDE"),
    ("Remote - US", "Senior Paid Media Manager", "Remote", "REMOTE"),
    (None, "Senior Paid Media Manager", "Remote", "REMOTE"),
])
def test_a_work_mode_select_follows_the_jobs_metro(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, location: str | None, title: str,
    expected: str, verdict: str,
) -> None:
    candidate = austin_candidate(fictional_candidate)
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job, location, title),
                                  typed_field(MODE_Q, S, MODES))
    [answer] = packet.answers
    assert label_of(answer) == expected
    assert answer.provenance.reference_ids == [metro_id(candidate)]
    [trace] = arrangement_traces(resolver)
    assert (trace["question"], trace["metro"], trace["place_source"]) == ("work_mode", verdict, "job")


def test_a_select_all_work_mode_question_for_a_metro_job_takes_hybrid_and_on_site(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    candidate = austin_candidate(fictional_candidate)
    packet, _, _ = resolve(Jev(), candidate, job_at(mock_job, "Pflugerville, TX"),
                           typed_field("Which work arrangements are you open to?", CB, MODES))
    [answer] = packet.answers
    assert label_of(answer) == ["Hybrid", "On-site"]


def test_an_unreadable_job_location_leaves_the_select_to_the_preference(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    candidate = austin_candidate(fictional_candidate, preference="hybrid")
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job, "Multiple Locations"),
                                  typed_field(MODE_Q, S, MODES))
    [answer] = packet.answers
    assert label_of(answer) == "Hybrid"  # the saved preference
    assert arrangement_traces(resolver)[0]["status"] == "NOT_DECIDED"
    assert [t["stage"] for t in resolver.narrative_traces][-1] == "work_arrangement_preference"


def test_without_a_metro_area_the_preference_decides_as_before(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    """Without ``metro_area`` the round-10 derivation stands: the remote preference answers
    an on-site Austin question No, and "Location Preference" (one of the preference's match
    phrases) takes its Remote option, as before."""
    candidate = austin_candidate(fictional_candidate, metro=None)
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job, "Austin, TX"),
        typed_field(ASHBY_ONSITE, R, YES_NO, field_id="onsite"),
        typed_field("Location Preference", S, OFFICES, field_id="offices"))
    assert label_of(packet.answer_for("onsite")) == "No"
    offices = packet.answer_for("offices")
    assert label_of(offices) == "Remote"
    preference = next(a.id for a in candidate.saved_answers if a.value == "remote")
    assert offices.provenance.reference_ids == [preference]
    assert all("metro" not in t for t in arrangement_traces(resolver))


def test_a_copied_preference_stays_when_the_metro_cannot_decide(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    """With a metro area, "Location Preference" over work modes is decided by the metro; when
    the job's place cannot be read, the preference the factual pass copied stays."""
    candidate = austin_candidate(fictional_candidate, preference="hybrid")
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job, "Multiple Locations"),
                                  typed_field("Location Preference", S, MODES))
    [answer] = packet.answers
    assert label_of(answer) == "Hybrid"
    assert arrangement_traces(resolver)[0]["status"] == "NOT_DECIDED"


def test_a_copied_preference_is_replaced_by_the_metro(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    """The factual pass copies the remote preference onto "Location Preference" by its match
    phrase; for an Austin hybrid job the metro decides Hybrid instead."""
    candidate = austin_candidate(fictional_candidate)
    packet, _, _ = resolve(Jev(), candidate, job_at(mock_job, "Austin, TX (Hybrid)"),
                           typed_field("Location Preference", S, MODES))
    [answer] = packet.answers
    assert label_of(answer) == "Hybrid"
    assert answer.provenance.reference_ids == [metro_id(candidate)]


def test_the_metro_area_is_never_a_reworded_question(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    label = "Which cities are you willing to commute to?"
    candidate = austin_candidate(fictional_candidate)
    provider = Jev({label[:30]: Script(semantic="CUSTOM_TEXT")})
    field_ = ApplicationField(id="q", selector="#q", label=label, semantic_type=classify_semantics(
        label=label, control_type=ControlType.TEXT), control_type=ControlType.TEXT, required=True)
    resolve(provider, candidate, job_at(mock_job, "Austin, TX"), field_)
    for request in provider.asked("wording"):
        questions = json.dumps(request["state"]["saved_questions"])
        assert METRO_AREA_QUESTION not in questions


# --- the simple-answers key -------------------------------------------------------------------------


def test_metro_area_imports_as_an_untyped_global_answer_and_exports_back(
    fictional_candidate: CandidateProfile,
) -> None:
    candidate = austin_candidate(fictional_candidate)
    [saved] = [a for a in candidate.saved_answers if a.question == METRO_AREA_QUESTION]
    assert (saved.scope, saved.semantic_type, saved.value) == (AnswerScope.GLOBAL, None, METRO)
    assert SimpleAnswers.from_profile(candidate).metro_area == METRO


def test_a_null_metro_area_adds_nothing(fictional_candidate: CandidateProfile) -> None:
    candidate = austin_candidate(fictional_candidate, metro=None)
    assert not [a for a in candidate.saved_answers if a.question == METRO_AREA_QUESTION]


# --- reading places ---------------------------------------------------------------------------------


@pytest.fixture
def metro() -> Any:
    found = person_metro(PostalAddress(city="Austin", region="Texas", country="United States"), METRO)
    assert found is not None
    return found


def test_the_metro_is_the_city_and_the_listed_towns(metro: Any) -> None:
    assert metro.places[:3] == ("Austin", "Round Rock", "Cedar Park") and metro.state == "TX"
    assert metro_places("Round Rock, TX; Cedar Park and Kyle / Texas, round rock") == (
        "Round Rock", "Cedar Park", "Kyle")
    assert person_metro(PostalAddress(city=None, region="TX"), METRO) is None


@pytest.mark.parametrize(("location", "verdict"), [
    ("Austin, TX", MetroVerdict.IN_METRO), ("Austin, Texas, United States", MetroVerdict.IN_METRO),
    ("Round Rock, TX (Hybrid)", MetroVerdict.IN_METRO), ("Georgetown, TX", MetroVerdict.IN_METRO),
    ("New York, NY; Austin, TX", MetroVerdict.IN_METRO), ("Austin, MN", MetroVerdict.OUTSIDE),
    ("Georgetown, DC", MetroVerdict.OUTSIDE), ("San Marcos, CA", MetroVerdict.OUTSIDE),
    ("San Francisco, CA", MetroVerdict.OUTSIDE), ("Denver", MetroVerdict.OUTSIDE),
    ("United States", MetroVerdict.OUTSIDE), ("Remote - US", MetroVerdict.REMOTE),
    ("Anywhere", MetroVerdict.REMOTE), (None, MetroVerdict.REMOTE), ("", MetroVerdict.REMOTE),
    ("Multiple Locations", MetroVerdict.UNKNOWN), ("Hybrid", MetroVerdict.UNKNOWN),
])
def test_a_job_location_reads_as_in_the_metro_outside_remote_or_unknown(
    metro: Any, mock_job: JobRecord, location: str | None, verdict: MetroVerdict,
) -> None:
    assert job_place(job_at(mock_job, location), metro).verdict is verdict


def test_a_question_saying_not_remote_is_not_a_remote_job(metro: Any) -> None:
    reading = read_place("This is not a remote role. Can you work in our Chicago office?", metro,
                         places=["Chicago"])
    assert reading.verdict is MetroVerdict.OUTSIDE and reading.place == "Chicago"


@pytest.mark.parametrize(("location", "title", "mode"), [
    ("Austin, TX (Hybrid)", "Manager", "hybrid"), ("Austin, TX", "Manager - Onsite", "on-site"),
    ("Remote - US", "Manager", "remote"), ("Austin, TX", "Manager", None),
    ("Hybrid or Remote", "Manager", None),
])
def test_the_posting_states_one_mode_or_none(
    mock_job: JobRecord, location: str, title: str, mode: str | None,
) -> None:
    assert posting_mode(job_at(mock_job, location, title)) == mode


def test_saved_answer_fixture_shape() -> None:
    """The metro answer is an ordinary GLOBAL saved answer (a guard for the fixtures above)."""
    answer = SavedAnswer(id="sa.metro", scope=AnswerScope.GLOBAL, question=METRO_AREA_QUESTION,
                         value=METRO, confirmed_at=NOW)
    assert answer.applies_to(JobRecord(id="j", application_url="https://example.test/a",
        normalized_url="https://example.test/a", created_at=NOW, updated_at=NOW))


# --- round 14: on-site questions that name no place ---------------------------------------------

ONSITE_DAYS = "Are you able to work on-site three days a week?"
HYBRID_OFFICE = "This role is hybrid (3 days in the office). Are you comfortable with that?"


def onsite_traces(resolver: DynamicPacketResolver) -> list[dict[str, Any]]:
    return [t for t in arrangement_traces(resolver) if t["question"] == "onsite"]


def outcome(packet: Any) -> tuple[list[tuple[str, Any]], list[tuple[str, str]]]:
    """The packet's answers and holds, comparable across two runs."""
    return ([(a.field_id, label_of(a)) for a in packet.answers],
            [(m.field_id, m.reason.value) for m in packet.missing_inputs])


@pytest.mark.parametrize("label", [ONSITE_DAYS, HYBRID_OFFICE])
@pytest.mark.parametrize(("location", "verdict"), [
    ("Austin, TX", "IN_METRO"), ("Round Rock, TX (Hybrid)", "IN_METRO"),
    ("Austin, Texas Metropolitan Area", "IN_METRO"), ("Remote (Austin, TX)", "IN_METRO"),
])
def test_an_on_site_question_naming_no_place_is_yes_for_a_metro_job(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str, location: str, verdict: str,
) -> None:
    candidate = austin_candidate(fictional_candidate)
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job, location), typed_field(label, R, YES_NO))
    [answer] = packet.answers
    assert label_of(answer) == "Yes"
    assert answer.provenance.source is AnswerSource.SAVED_ANSWER
    assert answer.provenance.reference_ids == [metro_id(candidate)]
    [trace] = onsite_traces(resolver)
    assert (trace["metro"], trace["place_source"], trace["status"], trace["choice"]) == (
        verdict, "job", "ANSWERED", "yes")
    assert trace["reference_ids"] == [metro_id(candidate)]


@pytest.mark.parametrize("label", [ONSITE_DAYS, HYBRID_OFFICE])
@pytest.mark.parametrize(("location", "verdict"), [
    ("San Francisco, CA", "OUTSIDE"), ("Remote", "REMOTE"), ("Remote - US", "REMOTE"), (None, "REMOTE"),
])
@pytest.mark.parametrize(("relocate", "expected"), [(None, "No"), ("No", "No"), ("Yes", "Yes")])
def test_an_on_site_question_naming_no_place_is_no_elsewhere_unless_the_person_relocates(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str, location: str | None,
    verdict: str, relocate: str | None, expected: str,
) -> None:
    candidate = austin_candidate(fictional_candidate, relocate=relocate)
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job, location), typed_field(label, R, YES_NO))
    [answer] = packet.answers
    assert label_of(answer) == expected
    assert answer.provenance.reference_ids == [metro_id(candidate)]  # relocation is traced, not cited
    [trace] = onsite_traces(resolver)
    assert (trace["metro"], trace["place_source"]) == (verdict, "job")
    assert trace["relocation"] == ({"Yes": "yes", "No": "no"}.get(relocate or "", "unstated"))


def test_an_unreadable_job_location_leaves_the_on_site_question_to_its_earlier_route(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    job = job_at(mock_job, "Multiple Locations")
    field = typed_field(ONSITE_DAYS, R, YES_NO)
    packet, _, resolver = resolve(Jev(), austin_candidate(fictional_candidate), job, field)
    [trace] = onsite_traces(resolver)
    assert (trace["metro"], trace["status"]) == ("UNKNOWN", "NOT_DECIDED")
    before, _, earlier = resolve(Jev(), austin_candidate(fictional_candidate, metro=None), job, field)
    assert outcome(packet) == outcome(before) and not arrangement_traces(earlier)


@pytest.mark.parametrize("location", ["Austin, TX", "San Francisco, CA", None])
def test_without_a_metro_area_an_on_site_question_naming_no_place_keeps_its_route(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, location: str | None,
) -> None:
    packet, _, resolver = resolve(Jev(), austin_candidate(fictional_candidate, metro=None),
                                  job_at(mock_job, location), typed_field(ONSITE_DAYS, R, YES_NO))
    assert not arrangement_traces(resolver)
    assert not [a for a in packet.answers if a.provenance.note and "metro" in a.provenance.note]


@pytest.mark.parametrize("label", [
    "Are you able to attend an in-person interview?",
    "Are you willing to travel on-site to client locations up to 25% of the time?",
    "Are you comfortable with in-person onboarding during your first week?",
    "Do you have experience working in a hybrid team?",
    "Have you worked on-site before?",
    "Do you currently work on-site?",
    "Is your current role remote or in-office? Are you able to share details?",
])
def test_on_site_wording_about_something_else_is_not_the_metros(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
) -> None:
    _, _, resolver = resolve(Jev(), austin_candidate(fictional_candidate), job_at(mock_job, "Austin, TX"),
                             typed_field(label, R, YES_NO))
    assert not arrangement_traces(resolver)


@pytest.mark.parametrize(("label", "location", "expected", "verdict"), [
    # The employer's name after "at" is no place: the job's location decides.
    ("Are you able to work on-site at Mock Co three days a week?", "Austin, TX", "Yes", "IN_METRO"),
    ("Are you comfortable working in-office at Mock Co's headquarters?", "Denver, CO", "No", "OUTSIDE"),
    # "with us" is the pronoun, not the country.
    ("Are you able to work on-site with us three days a week?", "Austin, TX", "Yes", "IN_METRO"),
    # The person's own state or country is too broad to place the work.
    ("Are you able to work on-site in Texas three days a week?", "Austin, TX", "Yes", "IN_METRO"),
    ("Are you able to work on-site in Texas three days a week?", "Houston, TX", "No", "OUTSIDE"),
    ("Are you willing to work in-office in the US?", "Round Rock, TX", "Yes", "IN_METRO"),
])
def test_what_an_on_site_question_names_that_is_no_place(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str, location: str,
    expected: str, verdict: str,
) -> None:
    candidate = austin_candidate(fictional_candidate)
    packet, _, resolver = resolve(Jev(), candidate, job_at(mock_job, location), typed_field(label, R, YES_NO))
    [answer] = packet.answers
    assert label_of(answer) == expected
    [trace] = arrangement_traces(resolver)
    assert (trace["question"], trace["metro"], trace["place_source"]) == ("onsite", verdict, "job")


def test_another_state_in_the_question_places_the_work_outside(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    label = "Are you able to work on-site in Colorado three days a week?"
    packet, _, resolver = resolve(Jev(), austin_candidate(fictional_candidate), job_at(mock_job, "Austin, TX"),
                                  typed_field(label, R, YES_NO))
    [answer] = packet.answers
    assert label_of(answer) == "No"
    [trace] = arrangement_traces(resolver)
    assert (trace["metro"], trace["place"], trace["place_source"]) == ("OUTSIDE", "CO", "question")


def test_a_city_question_naming_only_the_employer_is_read_by_the_job(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    # Before round 14 "at Mock Co" made this a city question placed OUTSIDE (the company).
    label = "This role requires working on-site at Mock Co five days a week. Are you able to?"
    packet, _, resolver = resolve(Jev(), austin_candidate(fictional_candidate), job_at(mock_job, "Austin, TX"),
                                  typed_field(label, R, YES_NO))
    [answer] = packet.answers
    assert label_of(answer) == "Yes"
    assert [t["question"] for t in arrangement_traces(resolver)] == ["onsite"]


@pytest.mark.parametrize(("text", "verdict", "place"), [
    ("Are you able to work on-site with us?", MetroVerdict.UNKNOWN, None),
    ("Are you able to work on-site in the US?", MetroVerdict.UNKNOWN, None),
    ("Are you able to work on-site in the U.S.?", MetroVerdict.UNKNOWN, None),
    ("Are you able to work on-site in Texas?", MetroVerdict.UNKNOWN, None),
    ("Are you able to work on-site in Colorado?", MetroVerdict.OUTSIDE, "CO"),
    ("Are you able to work on-site in the UK?", MetroVerdict.OUTSIDE, "uk"),
    ("Are you able to work on-site in Canada?", MetroVerdict.OUTSIDE, "canada"),
    ("Are you able to work on-site in Houston, TX?", MetroVerdict.OUTSIDE, "Houston, TX"),
    ("Are you able to work on-site in Round Rock, Texas?", MetroVerdict.IN_METRO, "Round Rock"),
])
def test_a_question_reading_skips_the_persons_own_state_and_country(
    metro: Any, text: str, verdict: MetroVerdict, place: str | None,
) -> None:
    reading = read_place(text, metro, question=True)
    assert (reading.verdict, reading.place) == (verdict, place)


@pytest.mark.parametrize(("location", "verdict"), [
    ("United States", MetroVerdict.OUTSIDE), ("US", MetroVerdict.OUTSIDE), ("Texas", MetroVerdict.OUTSIDE),
    ("Toronto, Canada", MetroVerdict.OUTSIDE),
])
def test_a_job_location_still_reads_the_country_and_the_state_as_elsewhere(
    metro: Any, mock_job: JobRecord, location: str, verdict: MetroVerdict,
) -> None:
    # Unchanged from round 13: only a question's reading treats them as too broad.
    assert job_place(job_at(mock_job, location), metro).verdict is verdict
