"""WP10 round 6: four live misses on the merged head (projected ``routing.trace`` types).

1. "Do you have 5+ years of hands-on paid media experience, including paid social, …" (Lever
   radio, Yes/No) read UNKNOWN 0.62, so no experience screener ran. A yes/no question about a
   minimum number of years of an area's experience ("N+ years", "at least N years", "N or more
   years", "over N years") is a CUSTOM_BOOLEAN experience screener; routing answers it from
   ``years_experience.<area>``.
2. "Do you have experience working at a digital marketing agency?" (Greenhouse select) still
   reached routing as CONSENT: the round-5 experience guard gave way to consent wording in the
   section headings above the question, and a CONSENT / CUSTOM_SELECT split never pooled.
3. "Which of the following U.S. time zones are you available to work in? (Select all that
   apply)" held NO_ANSWER; the saved ``available_time_zones`` answer is free text.
4. "Position", "Company Name" and "End Date MM/YYYY" in Paylocity's work-history block reached
   routing untyped.

Fields are typed by the browser heuristics first, then annotated, as the runtime does.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from interviewmaxxing_browser.ai import (
    AIFormRouter,
    BoundedDecisions,
    CallBudget,
    DynamicPacketResolver,
)
from interviewmaxxing_browser.ai.classification import FieldRoute
from interviewmaxxing_browser.semantics import classify as classify_semantics
from interviewmaxxing_core import (
    AnswerScope,
    AnswerSource,
    Application,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    CandidateProfile,
    ControlType,
    FieldOption,
    JobRecord,
    MissingReason,
    PacketContext,
    SavedAnswer,
    SemanticType,
)
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient

Spec = str | tuple[dict[str, float], float]
DEFAULTS = {"r": "COPY_KNOWN", "n": "literal", "u": "APPLICANT_CURRENT", "s": "CUSTOM_TEXT",
            "d": "APPLICATION_ATTACHMENT"}
Noul = Callable[[dict[str, Any], str], float]


class Jev:
    """Scripted Jev. ``scripts[field_id][kind]`` answers a field's route (r), prose (n),
    source (u), semantic (s) or file purpose (d) question; ``picks`` answers other choice
    questions by name. A spec is a choice (all mass, confidence 1.0) or (probabilities,
    confidence). Unscripted choices pick NONE/UNKNOWN; a noul answers ``nouls`` (a value
    per name, or a function of the request and the name) or 0.0."""

    def __init__(self, scripts: dict[str, dict[str, Spec]] | None = None,
                 picks: dict[str, Spec] | None = None,
                 nouls: dict[str, float] | Noul | None = None) -> None:
        self.scripts, self.picks, self.nouls = scripts or {}, picks or {}, nouls or {}
        self.requests: list[dict[str, Any]] = []

    def asked(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if name in r["questions"]]

    def _noul(self, request: dict[str, Any], name: str) -> float:
        if callable(self.nouls):
            return self.nouls(request, name)
        return self.nouls.get(name, 0.0)

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        self.requests.append(request)
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": self._noul(request, name)}
                continue
            criteria = list(question["criteria"])
            if name[0] in DEFAULTS and name[1:].isdigit():
                field_id = request["state"]["fields"][f"f{name[1:]}"]["field_id"]
                spec = self.scripts.get(field_id, {}).get(name[0], DEFAULTS[name[0]])
            else:
                spec = self.picks.get(name, next(
                    (c for c in ("NONE", "hold", "UNKNOWN") if c in criteria), criteria[0]))
            if isinstance(spec, str):
                probabilities, confidence = {c: float(c == spec) for c in criteria}, 1.0
            else:
                probabilities, confidence = {c: spec[0].get(c, 0.0) for c in criteria}, spec[1]
            answers[name] = {"type": "choice", "confidence": confidence, "probabilities": probabilities,
                             "choice": max(criteria, key=lambda c: probabilities[c])}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())


def router(provider: Jev) -> AIFormRouter:
    return AIFormRouter(BoundedDecisions(JevClient(ApiKey("synthetic-test", source="fixture"),
        transport=provider, max_attempts=1), CallBudget()))


def observed(label: str, control: ControlType, *options: str, field_id: str = "answer",
             name: str = "", section: tuple[str, ...] = ()) -> ApplicationField:
    """A field as the inspector reports it: typed by the browser heuristics."""
    return ApplicationField(id=field_id, selector=f"#{field_id}", label=label,
        semantic_type=classify_semantics(label=label, name=name, control_type=control),
        control_type=control, required=True, section_context=list(section),
        options=[FieldOption(value=f"v{i}", label=o) for i, o in enumerate(options)] or None)


def classified(provider: Jev, *fields: ApplicationField) -> tuple[ApplicationForm, AIFormRouter]:
    r = router(provider)
    form = r.annotate(ApplicationForm(url="https://synthetic.test/apply", fields=list(fields)),
                      document_id="wp10-round6")
    return form, r


def decision_for(provider: Jev, field: ApplicationField) -> Any:
    form, r = classified(provider, field)
    report = r.report_for(form)
    assert report is not None
    return report.fields[0]


def saved(answer_id: str, question: str, value: Any, *phrases: str) -> SavedAnswer:
    return SavedAnswer(id=answer_id, scope=AnswerScope.GLOBAL, semantic_type=None,
                       question=question, match_phrases=list(phrases), value=value,
                       confirmed_at="2026-09-02T12:00:00Z")


def with_saved(candidate: CandidateProfile, *answers: SavedAnswer) -> CandidateProfile:
    return candidate.model_copy(update={"saved_answers": [*candidate.saved_answers, *answers]})


def resolve(provider: Jev, candidate: CandidateProfile, job: JobRecord,
            *fields: ApplicationField) -> tuple[ApplicationPacket, PacketContext, AIFormRouter,
                                                 DynamicPacketResolver]:
    form, r = classified(provider, *fields)
    ctx = PacketContext(form=form, candidate=candidate, job=job, application=Application(
        id="app-wp10-r6", request_id="request-wp10-r6", job_id=job.id, candidate_id=candidate.id,
        state=ApplicationState.INSPECTING, version=1,
        created_at="2026-09-25T00:00:00Z", updated_at="2026-09-25T00:00:00Z"))
    resolver = DynamicPacketResolver(r.decisions, router=r)
    packet = asyncio.run(resolver.resolve(ctx))
    assert ctx.problems(packet) == []
    return packet, ctx, r, resolver


def stage_traces(resolver: DynamicPacketResolver, stage: str) -> list[dict[str, Any]]:
    return [trace for trace in resolver.narrative_traces if trace["stage"] == stage]


YES_NO = ("Yes", "No")
HISTORICAL = "HISTORICAL_OR_CONTEXTUAL"


# --- 1. a yes/no question about a minimum number of years of experience ---------------------

LEVER_YEARS = ("Do you have 5+ years of hands-on paid media experience, including paid social, "
               "paid search and programmatic?")
LIVE_SPLIT = ({"YEARS_EXPERIENCE": 0.62, "CUSTOM_BOOLEAN": 0.30, "CUSTOM_SELECT": 0.06,
               "UNKNOWN": 0.02}, 0.62)
"""Jev's reading of the Lever question: nothing reaches the 0.95 gate."""


@pytest.mark.parametrize("label,control,expected", [
    (LEVER_YEARS, ControlType.RADIO, SemanticType.CUSTOM_BOOLEAN),
    ("Do you have at least 8 years of total experience in direct response marketing?",
     ControlType.RADIO, SemanticType.CUSTOM_BOOLEAN),
    ("Do you have 5 or more years of B2B SaaS marketing experience?", ControlType.SELECT,
     SemanticType.CUSTOM_BOOLEAN),
    ("Have you had over 5 years of experience managing paid social budgets?", ControlType.SELECT,
     SemanticType.CUSTOM_BOOLEAN),
    ("Do you have at least eight years of experience in lifecycle marketing?", ControlType.RADIO,
     SemanticType.CUSTOM_BOOLEAN),
    ("Do you have more than 3 years of SEO experience?", ControlType.RADIO, SemanticType.CUSTOM_BOOLEAN),
    ("Do you have a minimum of 4 years of agency experience?", ControlType.RADIO,
     SemanticType.CUSTOM_BOOLEAN),
    ("Do you have 3 years of Google Ads experience?", ControlType.RADIO, SemanticType.CUSTOM_BOOLEAN),
    # Not the family: an age, a count of years, a select-all and a text box keep their types.
    ("Are you at least 18 years old?", ControlType.RADIO, SemanticType.CUSTOM_SELECT),
    ("How many years of paid media experience do you have?", ControlType.RADIO,
     SemanticType.YEARS_EXPERIENCE),
    ("Which of these do you have 5+ years of experience in?", ControlType.CHECKBOX_GROUP,
     SemanticType.CUSTOM_MULTISELECT),
    ("Do you have 5+ years of paid media experience?", ControlType.TEXT, SemanticType.CUSTOM_TEXT),
])
def test_the_heuristics_type_a_minimum_years_question_as_a_yes_no_screener(
    label: str, control: ControlType, expected: SemanticType,
) -> None:
    assert classify_semantics(label=label, control_type=control) is expected


@pytest.mark.parametrize("field,split,expected,demoted", [
    # The live reading: YEARS_EXPERIENCE leads at 0.62, all of it the custom yes/no reading.
    (observed(LEVER_YEARS, ControlType.RADIO, *YES_NO), LIVE_SPLIT, SemanticType.CUSTOM_BOOLEAN,
     SemanticType.YEARS_EXPERIENCE),
    # A sure count reading on Yes/No options is still the screener.
    (observed(LEVER_YEARS, ControlType.RADIO, *YES_NO), ({"YEARS_EXPERIENCE": 0.97,
     "CUSTOM_BOOLEAN": 0.03}, 0.96), SemanticType.CUSTOM_BOOLEAN, SemanticType.YEARS_EXPERIENCE),
    (observed(LEVER_YEARS, ControlType.RADIO, *YES_NO), ({"CUSTOM_BOOLEAN": 0.70,
     "UNKNOWN": 0.20, "YEARS_EXPERIENCE": 0.10}, 0.7), SemanticType.CUSTOM_BOOLEAN, None),
    # Another named reading above the outside bound: not one reading.
    (observed(LEVER_YEARS, ControlType.RADIO, *YES_NO), ({"YEARS_EXPERIENCE": 0.60,
     "CUSTOM_BOOLEAN": 0.30, "RELOCATION": 0.10}, 0.6), SemanticType.UNKNOWN, None),
    # Year ranges are not Yes/No: a count question keeps its count reading.
    (observed("How many years of paid media experience do you have?", ControlType.RADIO,
              "0-2 years", "3-5 years", "6+ years"), ({"YEARS_EXPERIENCE": 0.97,
     "CUSTOM_SELECT": 0.03}, 0.97), SemanticType.YEARS_EXPERIENCE, None),
])
def test_the_v13_gate_types_a_minimum_years_question_as_a_yes_no_screener(
    field: ApplicationField, split: Spec, expected: SemanticType, demoted: SemanticType | None,
) -> None:
    decision = decision_for(Jev({"answer": {"s": split, "u": HISTORICAL}}), field)
    assert decision.semantic_type is expected
    assert decision.demoted_from is demoted


def years_fact(request: dict[str, Any], name: str) -> float:
    """``has_fN`` is true for the years_experience.paid_media fact only."""
    if not name.startswith("has_"):
        return 0.0
    fact = request["state"]["facts"][name.removeprefix("has_")]
    return 1.0 if fact["key"] == "years_experience.paid_media" else 0.0


def test_the_lever_question_is_answered_by_the_screener_from_the_years_fact(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = Jev({"answer": {"s": LIVE_SPLIT, "u": HISTORICAL}}, picks={"experience": "YES"},
                   nouls=years_fact)
    packet, ctx, _, resolver = resolve(provider, fictional_candidate, mock_job,
                                       observed(LEVER_YEARS, ControlType.RADIO, *YES_NO))
    assert ctx.form.field("answer").semantic_type is SemanticType.CUSTOM_BOOLEAN
    [trace] = stage_traces(resolver, "experience_screener")
    assert trace["status"] == "ANSWERED"
    [answer] = packet.answers
    assert answer.value.label == "Yes"
    assert answer.provenance.source is AnswerSource.GENERATED_FROM_FACTS
    assert answer.provenance.reference_ids == ["fact.years_paid_media"]


# --- 2. the agency question under a consent heading ------------------------------------------

AGENCY = "Do you have experience working at a digital marketing agency?"
PRIVACY_HEADING = ("Applicant privacy notice", "By submitting this application you agree to the "
                   "processing of your personal data")


@pytest.mark.parametrize("section", [(), PRIVACY_HEADING])
@pytest.mark.parametrize("split", [
    "CONSENT",
    ({"CONSENT": 0.60, "CUSTOM_SELECT": 0.38, "UNKNOWN": 0.02}, 0.6),  # the select's own shape
    ({"CONSENT": 0.55, "CUSTOM_BOOLEAN": 0.30, "UNKNOWN": 0.15}, 0.55),
])
def test_the_agency_question_is_never_a_consent_whatever_the_heading_above_it(
    section: tuple[str, ...], split: Spec,
) -> None:
    field = observed(AGENCY, ControlType.SELECT, *YES_NO, section=section)
    assert field.semantic_type is SemanticType.CUSTOM_SELECT
    decision = decision_for(Jev({"answer": {"s": split, "r": "HUMAN_INPUT",
                                            "u": "EXPLICIT_ANSWER"}}), field)
    assert decision.semantic_type is SemanticType.CUSTOM_BOOLEAN
    assert decision.demoted_from is SemanticType.CONSENT
    assert decision.source_requirement != "only explicit scoped user/saved answer; never inferred"


@pytest.mark.parametrize("label", [
    "Do you agree to the processing of your personal data?",  # a consent act in the question
    "Would you like to join our talent community?",  # no experience wording: the heading counts
])
def test_under_a_consent_heading_a_question_without_experience_wording_keeps_its_consent(
    label: str,
) -> None:
    field = observed(label, ControlType.SELECT, *YES_NO, section=PRIVACY_HEADING)
    decision = decision_for(Jev({"answer": {"s": "CONSENT"}}), field)
    assert decision.semantic_type is SemanticType.CONSENT
    assert decision.route is FieldRoute.HUMAN_INPUT and decision.demoted_from is None


def test_the_agency_question_under_a_consent_heading_reaches_the_experience_screener(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = Jev({"answer": {"s": "CONSENT", "u": HISTORICAL}}, picks={"experience": "UNKNOWN"})
    packet, ctx, _, resolver = resolve(
        provider, fictional_candidate, mock_job,
        observed(AGENCY, ControlType.SELECT, *YES_NO, section=PRIVACY_HEADING))
    assert ctx.form.field("answer").semantic_type is SemanticType.CUSTOM_BOOLEAN
    assert [t["stage"] for t in resolver.narrative_traces] == ["experience_screener"]
    [missing] = packet.missing_inputs  # the facts do not say: held as a yes/no screener
    assert missing.reason is MissingReason.NO_ANSWER
    assert missing.prompt.startswith("Confirm whether you have the experience this question asks about")


# --- 3. the time-zone select-all reaches its free-text saved answer -------------------------

TIME_ZONES = ("Which of the following U.S. time zones are you available to work in? (Select all "
              "that apply)")
TIME_ZONE_OPTIONS = ("Eastern", "Central", "Mountain", "Pacific")
TIME_ZONES_SAVED = saved("sa.available_time_zones", "Which time zones are you available to work in?",
                         "US Eastern and US Central", "What time zones can you work in?",
                         "Time zone availability", "Which time zones can you work?")
"""The simple-answers ``available_time_zones`` key: untyped, free text."""


@pytest.mark.parametrize("split,typed", [
    # Jev names no category (the retry's reading): the select-all keeps its custom type.
    ({"UNKNOWN": 0.55, "CUSTOM_MULTISELECT": 0.40, "CUSTOM_SELECT": 0.04, "LOCATION": 0.01},
     SemanticType.CUSTOM_MULTISELECT),
    # A location reading above the outside bound leaves it UNKNOWN; the saved answer still applies.
    ({"UNKNOWN": 0.50, "CUSTOM_MULTISELECT": 0.35, "LOCATION": 0.15}, SemanticType.UNKNOWN),
])
def test_the_time_zone_select_all_takes_the_free_text_saved_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    split: dict[str, float], typed: SemanticType,
) -> None:
    candidate = with_saved(fictional_candidate, TIME_ZONES_SAVED)
    provider = Jev({"answer": {"s": (split, 0.5)}}, picks={"wording": "q0"},
                   nouls={"includes_o0": 0.99, "includes_o1": 0.99})
    packet, ctx, _, resolver = resolve(
        provider, candidate, mock_job, observed(TIME_ZONES, ControlType.CHECKBOX_GROUP, *TIME_ZONE_OPTIONS))
    assert ctx.form.field("answer").semantic_type is typed
    [answer] = packet.answers
    assert [c.label for c in answer.value.choices] == ["Eastern", "Central"]
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, ["sa.available_time_zones"])
    [wording] = stage_traces(resolver, "question_equivalence")
    [selection] = stage_traces(resolver, "multi_select")
    assert (wording["gate"], wording["status"], selection["status"]) == ("untyped", "MAPPED", "MAPPED")


@pytest.mark.parametrize("candidate_answers,status", [
    ((TIME_ZONES_SAVED,), "NONE"),  # Jev does not find the saved question the same question
    ((), None),  # no saved time zones at all: no wording decision is made
])
def test_a_no_answer_hold_on_the_time_zones_comes_from_the_wording_step(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    candidate_answers: tuple[SavedAnswer, ...], status: str | None,
) -> None:
    candidate = with_saved(fictional_candidate, *candidate_answers)
    provider = Jev({"answer": {"s": ({"UNKNOWN": 0.55, "CUSTOM_MULTISELECT": 0.45}, 0.5)}})
    packet, ctx, _, resolver = resolve(
        provider, candidate, mock_job, observed(TIME_ZONES, ControlType.CHECKBOX_GROUP, *TIME_ZONE_OPTIONS))
    assert ctx.form.field("answer").semantic_type is SemanticType.CUSTOM_MULTISELECT
    assert packet.answers == []
    [missing] = packet.missing_inputs
    assert missing.reason is MissingReason.NO_ANSWER
    assert [t["status"] for t in stage_traces(resolver, "question_equivalence")] == (
        [status] if status else [])


# --- 4. the work-history block ----------------------------------------------------------------

WORK_HISTORY = ("Work History",)


@pytest.mark.parametrize("label,name,expected", [
    ("Position", "position-0", SemanticType.CURRENT_TITLE),
    ("Job Title", "", SemanticType.CURRENT_TITLE),
    ("Company Name", "company-name-0", SemanticType.CURRENT_COMPANY),
    # No core type names an employment date; it is never the availability START_DATE.
    ("End Date MM/YYYY", "end-date-0", SemanticType.CUSTOM_TEXT),
    ("Start Date MM/YYYY", "start-date-0", SemanticType.CUSTOM_TEXT),
    ("Start Date", "start-date-0", SemanticType.CUSTOM_TEXT),
    # Outside a repeated block the availability questions keep START_DATE.
    ("What is your earliest start date?", "", SemanticType.START_DATE),
    ("Start date", "start_date", SemanticType.START_DATE),
    # A question id that merely ends in digits (Greenhouse) is not a repeated block's entry.
    ("When can you start?", "question_32145678", SemanticType.START_DATE),
    ("Position applied for", "", SemanticType.CUSTOM_TEXT),
])
def test_the_heuristics_type_the_work_history_block(label: str, name: str, expected: SemanticType) -> None:
    assert classify_semantics(label=label, name=name, control_type=ControlType.TEXT) is expected


@pytest.mark.parametrize("scope,answered", [("APPLICANT_CURRENT", True), (HISTORICAL, False)])
def test_the_current_role_fills_the_work_history_title_and_company(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, scope: str, answered: bool,
) -> None:
    fields = [observed("Position", ControlType.TEXT, field_id="position-0", name="position-0",
                       section=WORK_HISTORY),
              observed("Company Name", ControlType.TEXT, field_id="company-name-0",
                       name="company-name-0", section=WORK_HISTORY),
              observed("End Date MM/YYYY", ControlType.TEXT, field_id="end-date-0",
                       name="end-date-0", section=WORK_HISTORY)]
    provider = Jev({field.id: {"u": scope} for field in fields})
    packet, ctx, _, _ = resolve(provider, fictional_candidate, mock_job, *fields)
    assert [ctx.form.field(f.id).semantic_type for f in fields] == [
        SemanticType.CURRENT_TITLE, SemanticType.CURRENT_COMPANY, SemanticType.CUSTOM_TEXT]
    copied = {a.field_id: a.value.text for a in packet.answers}
    if answered:
        assert copied == {"position-0": "Paid Media Lead", "company-name-0": "Fictional Widgets Co"}
    else:  # a past role's entry: its source is historical, so nothing is copied
        assert copied == {}
    # No source states an employment end date yet: it waits for the person.
    assert "end-date-0" in {m.field_id for m in packet.missing_inputs}
