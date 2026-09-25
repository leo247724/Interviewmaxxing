"""WP10 round 5, the live prepare-only retry: eight classifier misses, from the projected
``routing.trace`` fields (types and confidences, never values).

(a) "Are you authorized to be employed in the United States?" (Ashby radio) got no type, so
the status derivation never ran: WORK_AUTHORIZATION. (b) "Do you have experience working at
a digital marketing agency?" (Greenhouse select) was CONSENT and held: CUSTOM_BOOLEAN, and
no yes/no experience question ever lands in CONSENT. (c) "What is the largest overall annual
ad spend you have personally overseen …" (text) read WEBSITE: CUSTOM_TEXT. (d) A time-zone
select-all and (g) a familiarity select-all came out UNKNOWN although their saved keys exist:
CUSTOM_MULTISELECT, so routing reaches the saved answer. (e) "County" (text) was UNKNOWN:
CUSTOM_TEXT, so the saved county applies. (f) "Company name" in a work-history block was
untyped: CURRENT_COMPANY. (h) Two years-of-experience radios kept YEARS_EXPERIENCE with a
confidence of None or 0.99: the heuristics' reading is explicit at 1.0.
"""
from __future__ import annotations

import asyncio
import json
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
    WORK_AUTHORIZATION_STATUS_QUESTION,
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
    PacketContext,
    SavedAnswer,
    SemanticType,
)
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient

Spec = str | tuple[dict[str, float], float]
DEFAULTS = {"r": "COPY_KNOWN", "n": "literal", "u": "APPLICANT_CURRENT", "s": "CUSTOM_TEXT",
            "d": "APPLICATION_ATTACHMENT"}


class Jev:
    """Scripted Jev. ``scripts[field_id][kind]`` answers a field's route (r), prose (n),
    source (u), semantic (s) or file purpose (d) question; ``picks`` answers other choice
    questions by name. A spec is a choice (all mass, confidence 1.0) or (probabilities,
    confidence). Unscripted choices pick NONE/UNKNOWN; nouls answer ``nouls`` or 0.0."""

    def __init__(self, scripts: dict[str, dict[str, Spec]] | None = None,
                 picks: dict[str, Spec] | None = None, nouls: dict[str, float] | None = None) -> None:
        self.scripts, self.picks, self.nouls = scripts or {}, picks or {}, nouls or {}
        self.requests: list[dict[str, Any]] = []

    def asked(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if name in r["questions"]]

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        self.requests.append(request)
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": self.nouls.get(name, 0.0)}
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
                probabilities = {c: spec[0].get(c, 0.0) for c in criteria}
                confidence = spec[1]
            answers[name] = {"type": "choice", "confidence": confidence, "probabilities": probabilities,
                             "choice": max(criteria, key=lambda c: probabilities[c])}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())


def router(provider: Jev) -> AIFormRouter:
    return AIFormRouter(BoundedDecisions(JevClient(ApiKey("synthetic-test", source="fixture"),
        transport=provider, max_attempts=1), CallBudget()))


def observed(label: str, control: ControlType, *options: str, field_id: str = "answer",
             name: str = "", input_type: str | None = None,
             section: tuple[str, ...] = ()) -> ApplicationField:
    """A field as the inspector reports it: typed by the browser heuristics."""
    return ApplicationField(id=field_id, selector=f"#{field_id}", label=label,
        semantic_type=classify_semantics(label=label, name=name, control_type=control,
                                         input_type=input_type),
        control_type=control, required=True, input_type=input_type,
        section_context=list(section),
        options=[FieldOption(value=f"v{i}", label=o) for i, o in enumerate(options)] or None)


def classified(provider: Jev, *fields: ApplicationField) -> tuple[ApplicationForm, AIFormRouter]:
    r = router(provider)
    form = r.annotate(ApplicationForm(url="https://synthetic.test/apply", fields=list(fields)),
                      document_id="wp10-round5-retry")
    return form, r


def saved(answer_id: str, question: str, value: Any, *phrases: str,
          semantic: SemanticType | None = None) -> SavedAnswer:
    return SavedAnswer(id=answer_id, scope=AnswerScope.GLOBAL, semantic_type=semantic,
                       question=question, match_phrases=list(phrases), value=value,
                       confirmed_at="2026-09-02T12:00:00Z")


def with_saved(candidate: CandidateProfile, *answers: SavedAnswer) -> CandidateProfile:
    return candidate.model_copy(update={"saved_answers": [*candidate.saved_answers, *answers]})


def resolve(provider: Jev, candidate: CandidateProfile, job: JobRecord,
            *fields: ApplicationField) -> tuple[ApplicationPacket, PacketContext, AIFormRouter,
                                                 DynamicPacketResolver]:
    form, r = classified(provider, *fields)
    ctx = PacketContext(form=form, candidate=candidate, job=job, application=Application(
        id="app-wp10", request_id="request-wp10", job_id=job.id, candidate_id=candidate.id,
        state=ApplicationState.INSPECTING, version=1,
        created_at="2026-09-24T00:00:00Z", updated_at="2026-09-24T00:00:00Z"))
    resolver = DynamicPacketResolver(r.decisions, router=r)
    packet = asyncio.run(resolver.resolve(ctx))
    assert ctx.problems(packet) == []
    return packet, ctx, r, resolver


def stage_traces(resolver: DynamicPacketResolver, stage: str) -> list[dict[str, Any]]:
    return [trace for trace in resolver.narrative_traces if trace["stage"] == stage]


# --- the live wordings ------------------------------------------------------------------

EMPLOYED_US = "Are you authorized to be employed in the United States?"
AGENCY_EXPERIENCE = "Do you have experience working at a digital marketing agency?"
AD_SPEND = ("What is the largest overall annual ad spend you have personally overseen across "
            "paid social, paid search and programmatic?")
TIME_ZONES = ("Which of the following U.S. time zones are you available to work in? (Select all "
              "that apply)")
TIME_ZONE_OPTIONS = ("Eastern", "Central", "Mountain", "Pacific")
COUNTY = "County"
COMPANY_NAME = "Company name"
FAMILIARITY = ("How well do you know us? Please select all of the following sources/tools that "
               "you have used or are familiar with")
FAMILIARITY_OPTIONS = ("I have used your product", "I have read your blog or newsletter",
                       "I had not heard of you before this posting")
PAID_MEDIA_YEARS = "How many years of experience do you have working in Paid Media?"
PROGRAMMATIC_YEARS = "How many years of programmatic media experience do you have?"
YEAR_RANGES = ("0-2 years", "3-5 years", "6-9 years", "10+ years")
YES_NO = ("Yes", "No")

CUSTOM_ONLY = {"UNKNOWN": 0.55, "CUSTOM_MULTISELECT": 0.40, "CUSTOM_SELECT": 0.04, "LOCATION": 0.01}
"""Jev names no category for the time-zone select-all: UNKNOWN leads at 0.55."""
COUNTY_SPLIT = {"UNKNOWN": 0.43, "CUSTOM_TEXT": 0.54, "CITY": 0.03}
FAMILIARITY_SPLIT = {"UNKNOWN": 0.54, "CUSTOM_MULTISELECT": 0.44, "REFERRAL_SOURCE": 0.02}
AD_SPEND_SPLIT = {"WEBSITE": 0.90, "CUSTOM_TEXT": 0.08, "UNKNOWN": 0.02}


@pytest.mark.parametrize("label,control,expected", [
    (EMPLOYED_US, ControlType.RADIO, SemanticType.WORK_AUTHORIZATION),
    ("Employment authorization", ControlType.SELECT, SemanticType.WORK_AUTHORIZATION),
    ("Are you eligible for employment in the U.S.?", ControlType.RADIO, SemanticType.WORK_AUTHORIZATION),
    (AGENCY_EXPERIENCE, ControlType.SELECT, SemanticType.CUSTOM_SELECT),
    (AD_SPEND, ControlType.TEXT, SemanticType.CUSTOM_TEXT),
    (TIME_ZONES, ControlType.CHECKBOX_GROUP, SemanticType.CUSTOM_MULTISELECT),
    (COUNTY, ControlType.TEXT, SemanticType.CUSTOM_TEXT),
    (COMPANY_NAME, ControlType.TEXT, SemanticType.CURRENT_COMPANY),
    ("Employer name", ControlType.TEXT, SemanticType.CURRENT_COMPANY),
    ("Employer", ControlType.TEXT, SemanticType.CURRENT_COMPANY),
    (FAMILIARITY, ControlType.CHECKBOX_GROUP, SemanticType.CUSTOM_MULTISELECT),
    (PAID_MEDIA_YEARS, ControlType.RADIO, SemanticType.YEARS_EXPERIENCE),
    (PROGRAMMATIC_YEARS, ControlType.RADIO, SemanticType.YEARS_EXPERIENCE),
    ("Total years of experience", ControlType.SELECT, SemanticType.YEARS_EXPERIENCE),
    # A yes/no about a minimum is not a count of years; nor is a count of something else.
    ("Do you have at least 8 years of total experience in direct response marketing?",
     ControlType.RADIO, SemanticType.CUSTOM_SELECT),
    ("How many years have you lived at your current address?", ControlType.TEXT,
     SemanticType.CUSTOM_TEXT),
])
def test_the_heuristics_type_the_retry_wordings(
    label: str, control: ControlType, expected: SemanticType,
) -> None:
    assert classify_semantics(label=label, control_type=control) is expected


# --- the v13 gate: every wording with the type it must get ------------------------------

@pytest.mark.parametrize("field,scripts,expected", [
    # (a) The heuristics type it; Jev is not asked its meaning.
    (observed(EMPLOYED_US, ControlType.RADIO, *YES_NO), {}, SemanticType.WORK_AUTHORIZATION),
    # (b) Whatever Jev reads, a yes/no experience question is a custom boolean.
    (observed(AGENCY_EXPERIENCE, ControlType.SELECT, *YES_NO),
     {"s": "CONSENT", "r": "HUMAN_INPUT", "u": "EXPLICIT_ANSWER"}, SemanticType.CUSTOM_BOOLEAN),
    (observed(AGENCY_EXPERIENCE, ControlType.SELECT, *YES_NO),
     {"s": ({"CONSENT": 0.60, "CUSTOM_BOOLEAN": 0.38, "UNKNOWN": 0.02}, 0.55), "r": "HUMAN_INPUT"},
     SemanticType.CUSTOM_BOOLEAN),
    # (c) WEBSITE at 0.9 on a quantity text box is the custom-text reading; a sure WEBSITE too.
    (observed(AD_SPEND, ControlType.TEXT), {"s": (AD_SPEND_SPLIT, 0.85)}, SemanticType.CUSTOM_TEXT),
    (observed(AD_SPEND, ControlType.TEXT), {"s": ({"WEBSITE": 0.97, "CUSTOM_TEXT": 0.03}, 0.96)},
     SemanticType.CUSTOM_TEXT),
    # (d) (e) (g) Jev names no category: the field is what its control is.
    (observed(TIME_ZONES, ControlType.CHECKBOX_GROUP, *TIME_ZONE_OPTIONS),
     {"s": (CUSTOM_ONLY, 0.50)}, SemanticType.CUSTOM_MULTISELECT),
    (observed(COUNTY, ControlType.TEXT), {"s": (COUNTY_SPLIT, 0.43)}, SemanticType.CUSTOM_TEXT),
    (observed(FAMILIARITY, ControlType.CHECKBOX_GROUP, *FAMILIARITY_OPTIONS),
     {"s": (FAMILIARITY_SPLIT, 0.54)}, SemanticType.CUSTOM_MULTISELECT),
    # (f) The heuristics type the work-history block's company.
    (observed(COMPANY_NAME, ControlType.TEXT, name="company-name-0"), {}, SemanticType.CURRENT_COMPANY),
    # (h) Both years questions are the heuristics' YEARS_EXPERIENCE.
    (observed(PAID_MEDIA_YEARS, ControlType.RADIO, *YEAR_RANGES), {}, SemanticType.YEARS_EXPERIENCE),
    (observed(PROGRAMMATIC_YEARS, ControlType.RADIO, *YEAR_RANGES), {}, SemanticType.YEARS_EXPERIENCE),
])
def test_the_v13_gate_types_every_retry_wording(
    field: ApplicationField, scripts: dict[str, Spec], expected: SemanticType,
) -> None:
    provider = Jev({"answer": scripts})
    form, r = classified(provider, field)
    decision = r.report_for(form).fields[0]
    assert form.fields[0].semantic_type is decision.semantic_type is expected
    if not scripts:
        # The heuristics' reading is explicit, never a missing confidence (h).
        assert not provider.asked("s0")
        assert decision.semantic_confidence == 1.0
        assert decision.semantic_probabilities == {expected.value: 1.0}
        assert decision.semantic_pool_share is None


@pytest.mark.parametrize("field,split,share", [
    (observed(TIME_ZONES, ControlType.CHECKBOX_GROUP, *TIME_ZONE_OPTIONS), CUSTOM_ONLY, 0.99),
    (observed(COUNTY, ControlType.TEXT), COUNTY_SPLIT, 0.97),
    (observed(FAMILIARITY, ControlType.CHECKBOX_GROUP, *FAMILIARITY_OPTIONS), FAMILIARITY_SPLIT, 0.98),
    (observed(AD_SPEND, ControlType.TEXT), AD_SPEND_SPLIT, 0.98),
])
def test_the_custom_and_quantity_pools_record_their_share(
    field: ApplicationField, split: dict[str, float], share: float,
) -> None:
    form, r = classified(Jev({"answer": {"s": (split, 0.5)}}), field)
    decision = r.report_for(form).fields[0]
    assert decision.semantic_type is not SemanticType.UNKNOWN
    assert decision.semantic_pool_share == pytest.approx(share)
    assert decision.semantic_confidence == 0.5  # the raw reading is kept for the trace


@pytest.mark.parametrize("field,split", [
    # A named type at 0.04 is outside the custom pool: County may be the city.
    (observed(COUNTY, ControlType.TEXT), {"UNKNOWN": 0.43, "CUSTOM_TEXT": 0.53, "CITY": 0.04}),
    # The pool is only 0.94.
    (observed(TIME_ZONES, ControlType.CHECKBOX_GROUP, *TIME_ZONE_OPTIONS),
     {"UNKNOWN": 0.50, "CUSTOM_MULTISELECT": 0.44, "LOCATION": 0.03, "STATE": 0.03}),
    # A profile-URL reading on a text box that is not a quantity question is not pooled.
    (observed("Where can we see your work?", ControlType.TEXT), AD_SPEND_SPLIT),
    # A quantity question that names a link keeps its URL reading out of the pool.
    (observed("How many campaigns are in the link you share?", ControlType.TEXT), AD_SPEND_SPLIT),
])
def test_a_split_outside_the_pools_stays_unknown(field: ApplicationField, split: dict[str, float]) -> None:
    form, r = classified(Jev({"answer": {"s": (split, 0.9)}}), field)
    assert r.report_for(form).fields[0].semantic_type is SemanticType.UNKNOWN


def test_a_quantity_url_input_keeps_its_website_reading() -> None:
    field = observed("How many followers does your website have? Enter its URL", ControlType.TEXT,
                     input_type="url")
    form, r = classified(Jev({"answer": {"s": ({"WEBSITE": 0.97, "CUSTOM_TEXT": 0.03}, 0.96)}}), field)
    assert r.report_for(form).fields[0].semantic_type is SemanticType.WEBSITE


def test_a_yes_no_custom_split_is_the_custom_boolean() -> None:
    field = observed("Are you open to a hybrid schedule?", ControlType.SELECT, *YES_NO)
    split = {"CUSTOM_SELECT": 0.50, "UNKNOWN": 0.30, "CUSTOM_BOOLEAN": 0.20}
    form, r = classified(Jev({"answer": {"s": (split, 0.5)}}), field)
    assert r.report_for(form).fields[0].semantic_type is SemanticType.CUSTOM_BOOLEAN


# --- (b) a Yes/No experience question never lands in CONSENT ------------------------------

@pytest.mark.parametrize("label", [AGENCY_EXPERIENCE,
                                   "Have you worked in a performance marketing agency environment?"])
@pytest.mark.parametrize("scripts,demoted", [
    ({"s": "CONSENT"}, SemanticType.CONSENT),
    ({"s": "ATTESTATION", "r": "HUMAN_INPUT"}, SemanticType.ATTESTATION),
    ({"s": "CONSENT", "u": "EXPLICIT_ANSWER"}, SemanticType.CONSENT),
    ({"s": "CONSENT", "r": "HUMAN_INPUT", "u": "EXPLICIT_ANSWER"}, SemanticType.CONSENT),
    ({"s": ({"CONSENT": 0.50, "CUSTOM_BOOLEAN": 0.48, "UNKNOWN": 0.02}, 0.5), "r": "HUMAN_INPUT",
      "u": ({"EXPLICIT_ANSWER": 0.60, "HISTORICAL_OR_CONTEXTUAL": 0.40}, 0.6)}, SemanticType.CONSENT),
])
def test_a_yes_no_experience_question_never_lands_in_consent(
    label: str, scripts: dict[str, Spec], demoted: SemanticType,
) -> None:
    field = observed(label, ControlType.SELECT, *YES_NO)
    assert field.semantic_type is SemanticType.CUSTOM_SELECT
    form, r = classified(Jev({"answer": scripts}), field)
    decision = r.report_for(form).fields[0]
    assert form.fields[0].semantic_type is decision.semantic_type is SemanticType.CUSTOM_BOOLEAN
    assert decision.demoted_from is demoted
    assert decision.route is not FieldRoute.HUMAN_INPUT or scripts.get("r") == "HUMAN_INPUT"


@pytest.mark.parametrize("typed", [SemanticType.CONSENT, SemanticType.ATTESTATION])
def test_a_heuristics_consent_type_on_an_experience_question_is_demoted_whatever_jev_routes(
    typed: SemanticType,
) -> None:
    field = ApplicationField(id="answer", selector="#answer", label=AGENCY_EXPERIENCE,
        semantic_type=typed, control_type=ControlType.RADIO, required=True,
        options=[FieldOption(value="v0", label="Yes"), FieldOption(value="v1", label="No")])
    form, r = classified(Jev({"answer": {"r": "HUMAN_INPUT", "u": "EXPLICIT_ANSWER"}}), field)
    decision = r.report_for(form).fields[0]
    assert (decision.semantic_type, decision.demoted_from) == (SemanticType.CUSTOM_BOOLEAN, typed)


@pytest.mark.parametrize("label", [
    "Do you consent to a background check on your work experience?",
    "I certify that my experience working at an agency is stated truthfully.",
])
def test_experience_wording_with_a_consent_act_keeps_the_explicit_answer(label: str) -> None:
    field = observed(label, ControlType.SELECT, *YES_NO)
    form, r = classified(Jev({"answer": {"s": "CONSENT", "r": "HUMAN_INPUT"}}), field)
    decision = r.report_for(form).fields[0]
    assert decision.semantic_type in (SemanticType.CONSENT, SemanticType.ATTESTATION)
    assert decision.demoted_from is None and decision.route is FieldRoute.HUMAN_INPUT


# --- end to end: the typed field reaches its answer ----------------------------------------

def test_the_ashby_employment_question_is_derived_from_the_stated_status(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    candidate = with_saved(fictional_candidate,
                           saved("sa.status", WORK_AUTHORIZATION_STATUS_QUESTION, "us_citizen"))
    provider = Jev()
    packet, ctx, _, resolver = resolve(provider, candidate, mock_job,
                                       observed(EMPLOYED_US, ControlType.RADIO, *YES_NO))
    assert ctx.form.field("answer").semantic_type is SemanticType.WORK_AUTHORIZATION
    [answer] = packet.answers
    assert answer.value.label == "Yes"
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, ["sa.status"])
    [trace] = stage_traces(resolver, "status_derivation")
    assert (trace["status"], trace["via"]) == ("ANSWERED", "table")
    assert "us_citizen" not in json.dumps(resolver.narrative_traces)


def test_the_agency_experience_question_reaches_the_experience_screener(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = Jev({"answer": {"s": "CONSENT", "u": "HISTORICAL_OR_CONTEXTUAL"}},
                   picks={"experience": "UNKNOWN"})
    packet, ctx, _, resolver = resolve(provider, fictional_candidate, mock_job,
                                       observed(AGENCY_EXPERIENCE, ControlType.SELECT, *YES_NO))
    assert ctx.form.field("answer").semantic_type is SemanticType.CUSTOM_BOOLEAN
    assert provider.asked("experience")
    assert [t["stage"] for t in resolver.narrative_traces] == ["experience_screener"]
    [missing] = packet.missing_inputs
    assert missing.prompt.startswith("Confirm whether you have the experience this question asks about")


def test_the_time_zone_select_all_takes_the_saved_time_zones(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    candidate = with_saved(fictional_candidate, saved(
        "sa.time_zones", "Which time zones are you available to work in?",
        ["US Central", "US Eastern"], "What time zones can you work in?", "Time zone availability"))
    provider = Jev({"answer": {"s": (CUSTOM_ONLY, 0.50)}},
                   picks={"wording": "q0", "equivalent_0": "o1", "equivalent_1": "o0"})
    packet, ctx, _, resolver = resolve(
        provider, candidate, mock_job,
        observed(TIME_ZONES, ControlType.CHECKBOX_GROUP, *TIME_ZONE_OPTIONS))
    assert ctx.form.field("answer").semantic_type is SemanticType.CUSTOM_MULTISELECT
    [request] = provider.asked("wording")
    assert [q["question"] for q in request["state"]["saved_questions"].values()] == [
        "Which time zones are you available to work in?"]
    [answer] = packet.answers
    assert sorted(c.label for c in answer.value.choices) == ["Central", "Eastern"]
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, ["sa.time_zones"])
    [trace] = stage_traces(resolver, "question_equivalence")
    assert trace["status"] == "MAPPED"


def test_the_county_box_takes_the_saved_county(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    candidate = with_saved(fictional_candidate, saved(
        "sa.county", "County", "Lane", "County of residence", "What county do you live in?"))
    provider = Jev({"answer": {"s": (COUNTY_SPLIT, 0.43)}})
    packet, ctx, _, _ = resolve(provider, candidate, mock_job, observed(COUNTY, ControlType.TEXT))
    assert ctx.form.field("answer").semantic_type is SemanticType.CUSTOM_TEXT
    [answer] = packet.answers
    assert answer.value.text == "Lane"
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, ["sa.county"])


@pytest.mark.parametrize("scope,answered", [("APPLICANT_CURRENT", True),
                                            ("HISTORICAL_OR_CONTEXTUAL", False)])
def test_the_work_history_company_is_the_verified_current_company_only_for_the_current_entry(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, scope: str, answered: bool,
) -> None:
    field = observed(COMPANY_NAME, ControlType.TEXT, name="company-name-0",
                     section=("Work history",))
    provider = Jev({"answer": {"u": scope}})
    packet, ctx, _, _ = resolve(provider, fictional_candidate, mock_job, field)
    assert ctx.form.field("answer").semantic_type is SemanticType.CURRENT_COMPANY
    if answered:
        [answer] = packet.answers
        assert answer.value.text == "Fictional Widgets Co"
        assert answer.provenance.source is AnswerSource.CANDIDATE_FACT
    else:
        assert packet.answers == []
        assert [m.field_id for m in packet.missing_inputs] == ["answer"]


@pytest.mark.parametrize("includes,answered", [
    ({"includes_o0": 0.99}, True),  # the options allow: one option says what the answer says
    ({}, False),  # no option includes the answer: held for the person
])
def test_the_familiarity_select_all_takes_the_saved_familiarity_when_the_options_allow(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, includes: dict[str, float],
    answered: bool,
) -> None:
    candidate = with_saved(fictional_candidate, saved(
        "sa.familiarity", "Before applying, how familiar were you with this company?",
        "I had used your product before applying", "How familiar are you with this company?"))
    provider = Jev({"answer": {"s": (FAMILIARITY_SPLIT, 0.54)}}, picks={"wording": "q0"},
                   nouls=includes)
    packet, ctx, _, resolver = resolve(
        provider, candidate, mock_job,
        observed(FAMILIARITY, ControlType.CHECKBOX_GROUP, *FAMILIARITY_OPTIONS))
    assert ctx.form.field("answer").semantic_type is SemanticType.CUSTOM_MULTISELECT
    assert provider.asked("wording") and provider.asked("includes_o0")
    [wording] = stage_traces(resolver, "question_equivalence")
    [selection] = stage_traces(resolver, "multi_select")
    if answered:
        [answer] = packet.answers
        assert [c.label for c in answer.value.choices] == ["I have used your product"]
        assert answer.provenance.reference_ids == ["sa.familiarity"]
        assert (wording["status"], selection["status"]) == ("MAPPED", "MAPPED")
    else:
        assert packet.answers == []
        assert (wording["status"], selection["status"]) == ("VALUE_DOES_NOT_FIT", "NONE")
        [missing] = packet.missing_inputs
        assert "cannot be used here" in missing.prompt
