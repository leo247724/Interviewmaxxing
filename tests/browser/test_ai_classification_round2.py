"""WP10 round 2: the live misses of the v13 pooled gates, with the traced numbers.

- Ashby's required ``_systemfield_resume`` ("Resume / or drag and drop here"): route
  APPROVED_DOCUMENT 0.98, but purpose ATTACHMENT 0.52 / PARSER 0.37 / OTHER 0.11 (pool 0.89).
- Greenhouse "Do you currently reside in the US?" (Yes/No): LOCATION 0.75 / CUSTOM_BOOLEAN
  0.13 / COUNTRY 0.11. On Yes/No options CUSTOM_BOOLEAN is the control's shape.
- "What is your current home address?" (text): ADDRESS 0.93 / LOCATION 0.05.

Fields are typed by the browser heuristics first, then annotated, as the runtime does.
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
    confidence). Unscripted choices pick NONE/UNKNOWN; nouls answer 0.0."""

    def __init__(self, scripts: dict[str, dict[str, Spec]] | None = None,
                 picks: dict[str, Spec] | None = None) -> None:
        self.scripts, self.picks = scripts or {}, picks or {}
        self.requests: list[dict[str, Any]] = []

    def asked(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if name in r["questions"]]

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        self.requests.append(request)
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.0}
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
             required: bool = True) -> ApplicationField:
    """A field as the inspector reports it: typed by the browser heuristics."""
    return ApplicationField(id=field_id, selector=f"#{field_id}", label=label,
        semantic_type=classify_semantics(label=label, control_type=control),
        control_type=control, required=required,
        options=[FieldOption(value=f"v{i}", label=o) for i, o in enumerate(options)] or None)


def classify(provider: Jev, *fields: ApplicationField) -> tuple[ApplicationForm, AIFormRouter]:
    r = router(provider)
    return r.annotate(ApplicationForm(url="https://synthetic.test/apply", fields=list(fields)),
                      document_id="wp10-round2"), r


def resolve(provider: Jev, candidate: CandidateProfile, job: JobRecord, *fields: ApplicationField,
            ) -> tuple[ApplicationPacket, PacketContext, AIFormRouter, DynamicPacketResolver]:
    form, r = classify(provider, *fields)
    ctx = PacketContext(form=form, candidate=candidate, job=job, application=Application(
        id="app-wp10-r2", request_id="request-wp10-r2", job_id=job.id, candidate_id=candidate.id,
        state=ApplicationState.INSPECTING, version=1,
        created_at="2026-09-24T00:00:00Z", updated_at="2026-09-24T00:00:00Z"))
    resolver = DynamicPacketResolver(r.decisions, router=r)
    packet = asyncio.run(resolver.resolve(ctx))
    assert ctx.problems(packet) == []
    return packet, ctx, r, resolver


def resume_traces(resolver: DynamicPacketResolver) -> list[dict[str, Any]]:
    return [t for t in resolver.narrative_traces if t["stage"] == "resume_upload"]


# --- 1. a required resume whose route is sure -------------------------------------------

ASHBY_RESUME = "Resume / or drag and drop here"
ROUTE_098 = ({"APPROVED_DOCUMENT": 0.98, "AMBIGUOUS": 0.02}, 0.98)
ASHBY_PURPOSE = ({"APPLICATION_ATTACHMENT": 0.52, "AUTOFILL_PARSER": 0.37, "OTHER_OR_UNCLEAR": 0.11},
                 0.52)
PARSER_099 = ({"AUTOFILL_PARSER": 0.99, "OTHER_OR_UNCLEAR": 0.01}, 0.99)


def test_the_ashby_required_resume_is_attached_and_its_parser_helper_is_not(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    resume = observed(ASHBY_RESUME, ControlType.FILE, field_id="_systemfield_resume")
    helper = observed("Autofill from resume", ControlType.FILE, field_id="field-field", required=False)
    assert resume.semantic_type is helper.semantic_type is SemanticType.RESUME
    provider = Jev({"_systemfield_resume": {"r": ROUTE_098, "d": ASHBY_PURPOSE},
                    "field-field": {"r": ROUTE_098, "d": PARSER_099}})
    packet, ctx, r, resolver = resolve(provider, fictional_candidate, mock_job, resume, helper)
    report = r.report_for(ctx.form)
    decision = report.field("_systemfield_resume")
    assert decision.route is FieldRoute.APPROVED_DOCUMENT
    assert decision.document_pool_share == pytest.approx(0.89)  # under the purpose pool gate
    assert decision.autofill is True  # the purpose is not confidently an attachment
    assert "uploaded first" in decision.reason and "re-inspected" in decision.reason
    # The optional parser helper stays unsupported: never attach to a parser-only helper.
    assert report.field("field-field").route is FieldRoute.UNSUPPORTED
    assert [(a.field_id, a.provenance.source) for a in packet.answers] == [
        ("_systemfield_resume", AnswerSource.RESUME)]
    assert [t["field_id"] for t in resume_traces(resolver)] == ["_systemfield_resume"]


def test_a_resume_typed_by_jev_is_approved_on_a_sure_route(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    field = observed("Upload here", ControlType.FILE)
    assert field.semantic_type is SemanticType.UNKNOWN  # nothing in the label says resume
    provider = Jev({"answer": {"r": ROUTE_098, "d": ASHBY_PURPOSE, "s": "RESUME"}})
    packet, ctx, r, _ = resolve(provider, fictional_candidate, mock_job, field)
    decision = r.report_for(ctx.form).fields[0]
    assert (decision.route, decision.semantic_type, decision.autofill) == (
        FieldRoute.APPROVED_DOCUMENT, SemanticType.RESUME, True)
    assert [a.provenance.source for a in packet.answers] == [AnswerSource.RESUME]


@pytest.mark.parametrize("purpose,autofill", [
    # Confidently an attachment: no parser to wait out.
    (({"APPLICATION_ATTACHMENT": 0.97, "OTHER_OR_UNCLEAR": 0.03}, 0.95), False),
    # The purpose reading does not block a sure route, whatever it says.
    (({"OTHER_OR_UNCLEAR": 0.99, "APPLICATION_ATTACHMENT": 0.01}, 0.99), True),
    (PARSER_099, True),
])
def test_a_sure_route_approves_the_required_resume_whatever_the_purpose(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, purpose: Spec, autofill: bool,
) -> None:
    provider = Jev({"answer": {"r": ROUTE_098, "d": purpose}})
    packet, ctx, r, _ = resolve(provider, fictional_candidate, mock_job,
                                observed(ASHBY_RESUME, ControlType.FILE))
    decision = r.report_for(ctx.form).fields[0]
    assert decision.route is FieldRoute.APPROVED_DOCUMENT and decision.autofill is autofill
    assert [a.provenance.source for a in packet.answers] == [AnswerSource.RESUME]


@pytest.mark.parametrize("route,required,expected", [
    # A route that is not sure leaves the purpose gate in charge.
    (({"APPROVED_DOCUMENT": 0.94, "AMBIGUOUS": 0.06}, 0.98), True, FieldRoute.AMBIGUOUS),
    # An optional upload keeps the purpose gate whatever the route.
    (ROUTE_098, False, FieldRoute.AMBIGUOUS),
])
def test_the_purpose_gate_still_decides_without_a_sure_route_or_a_required_resume(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    route: Spec, required: bool, expected: FieldRoute,
) -> None:
    provider = Jev({"answer": {"r": route, "d": ASHBY_PURPOSE}})
    packet, ctx, r, resolver = resolve(provider, fictional_candidate, mock_job,
                                       observed(ASHBY_RESUME, ControlType.FILE, required=required))
    decision = r.report_for(ctx.form).fields[0]
    assert decision.route is expected and decision.autofill is False
    assert packet.answers == [] and not resume_traces(resolver)


# --- 2. a Yes/No residence question: CUSTOM_BOOLEAN is the control's shape -----------------

GREENHOUSE_US = "Do you currently reside in the US?"
LIVE_SPLIT = {"LOCATION": 0.75, "CUSTOM_BOOLEAN": 0.13, "COUNTRY": 0.11, "UNKNOWN": 0.01}


def test_the_yes_no_residence_split_pools_with_its_shape_and_is_answered(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    field = observed(GREENHOUSE_US, ControlType.SELECT, "Yes", "No")
    assert field.semantic_type is SemanticType.CUSTOM_SELECT
    provider = Jev({"answer": {"s": (LIVE_SPLIT, 0.74)}}, picks={"residence": "o0"})
    packet, ctx, r, _ = resolve(provider, fictional_candidate, mock_job, field)
    decision = r.report_for(ctx.form).fields[0]
    assert decision.semantic_type is ctx.form.field("answer").semantic_type is SemanticType.LOCATION
    assert decision.semantic_pool_share == pytest.approx(0.99)
    [answer] = packet.answers
    assert answer.value.label == "Yes" and answer.provenance.source is AnswerSource.PROFILE_IDENTITY


@pytest.mark.parametrize("field,split,share", [
    # A text box copies one exact datum: never pooled.
    (observed(GREENHOUSE_US, ControlType.TEXT), LIVE_SPLIT, None),
    # CUSTOM_TEXT is a competing reading, not the Yes/No shape.
    (observed(GREENHOUSE_US, ControlType.SELECT, "Yes", "No"),
     {"LOCATION": 0.75, "CUSTOM_TEXT": 0.13, "COUNTRY": 0.11, "UNKNOWN": 0.01}, 0.86),
    # Without Yes/No options, CUSTOM_BOOLEAN is not the control's shape.
    (observed("Where are you currently based?", ControlType.SELECT, "United States", "Canada", "Other"),
     LIVE_SPLIT, 0.86),
    # The shape joins only while the residence types together outweigh it.
    (observed(GREENHOUSE_US, ControlType.SELECT, "Yes", "No"),
     {"CUSTOM_BOOLEAN": 0.60, "STATE": 0.40}, 0.40),
])
def test_other_splits_are_not_one_residence_reading(
    field: ApplicationField, split: dict[str, float], share: float | None,
) -> None:
    form, r = classify(Jev({"answer": {"s": (split, 0.74)}}), field)
    decision = r.report_for(form).fields[0]
    assert decision.semantic_type is SemanticType.UNKNOWN
    assert decision.semantic_pool_share == (pytest.approx(share) if share is not None else None)


def test_a_state_split_with_a_large_yes_no_shape_is_a_residence_reading() -> None:
    # {"STATE": 0.60, "CUSTOM_BOOLEAN": 0.40} on Yes/No options: all of Jev's meaning is the
    # state, so the question pools as STATE (the residence screener remains the gate).
    rippling = ("Do you reside in any of the following states: AL, AZ, CA, CO, CT, DC, FL, GA, "
                "OR, TX?")
    form, r = classify(Jev({"answer": {"s": ({"STATE": 0.60, "CUSTOM_BOOLEAN": 0.40}, 0.97)}}),
                       observed(rippling, ControlType.SELECT, "Yes", "No"))
    decision = r.report_for(form).fields[0]
    assert decision.semantic_type is SemanticType.STATE
    assert decision.semantic_pool_share == pytest.approx(1.0)


# --- 3. a text box asking for the home address --------------------------------------------

HOME_ADDRESS = "What is your current home address?"


def test_a_home_address_split_with_location_is_the_address_and_copies_the_street(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    field = observed(HOME_ADDRESS, ControlType.TEXT)
    assert field.semantic_type is SemanticType.CUSTOM_TEXT  # "home address" is not a heuristic rule
    identity = fictional_candidate.identity
    candidate = fictional_candidate.model_copy(update={"identity": identity.model_copy(update={
        "address": identity.address.model_copy(update={"street": "12 Example Lane"})})})
    split = ({"ADDRESS": 0.93, "LOCATION": 0.05, "CUSTOM_TEXT": 0.02}, 0.93)
    packet, ctx, r, _ = resolve(Jev({"answer": {"s": split}}), candidate, mock_job, field)
    decision = r.report_for(ctx.form).fields[0]
    assert decision.semantic_type is ctx.form.field("answer").semantic_type is SemanticType.ADDRESS
    assert decision.semantic_pool_share == pytest.approx(0.98)
    [answer] = packet.answers
    assert answer.value.text == "12 Example Lane"
    assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY


@pytest.mark.parametrize("label,split,expected,share", [
    # A location-led split still asks for the address the label names.
    ("Home address", {"ADDRESS": 0.40, "LOCATION": 0.57, "CUSTOM_TEXT": 0.03},
     SemanticType.ADDRESS, 1.0 - 0.03),
    # Another reading over the outside bound: not one address reading.
    ("Home address", {"ADDRESS": 0.93, "CUSTOM_TEXT": 0.05, "LOCATION": 0.02},
     SemanticType.UNKNOWN, 0.95),
    # Only a label that asks for an address pools.
    ("Where do you live?", {"ADDRESS": 0.93, "LOCATION": 0.05, "CUSTOM_TEXT": 0.02},
     SemanticType.UNKNOWN, None),
])
def test_only_an_address_label_pools_address_with_location(
    label: str, split: dict[str, float], expected: SemanticType, share: float | None,
) -> None:
    form, r = classify(Jev({"answer": {"s": (split, 0.93)}}), observed(label, ControlType.TEXT))
    decision = r.report_for(form).fields[0]
    assert decision.semantic_type is expected
    assert decision.semantic_pool_share == (pytest.approx(share) if share is not None else None)
