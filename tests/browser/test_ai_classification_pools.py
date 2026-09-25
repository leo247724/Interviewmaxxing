"""Pooled gates read pooled mass (WP10 item 1).

Pilot 7 held Greenhouse's "Do you currently reside in the US?" (COUNTRY 0.24 / LOCATION
0.71) and Vercel's country question (COUNTRY 0.49 / LOCATION 0.50) as UNKNOWN, and five
Ashby required resumes as AMBIGUOUS, because each pooled gate still required one choice's
confidence of 0.90, which a split between two pooled choices never gives. The fields here
are typed by the browser heuristics first, then annotated, as the runtime does.
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


def router(provider: Jev, budget: CallBudget | None = None) -> AIFormRouter:
    return AIFormRouter(BoundedDecisions(JevClient(ApiKey("synthetic-test", source="fixture"),
        transport=provider, max_attempts=1), budget or CallBudget()))


def observed(label: str, control: ControlType, *options: str, field_id: str = "answer",
             required: bool = True) -> ApplicationField:
    """A field as the inspector reports it: typed by the browser heuristics."""
    return ApplicationField(id=field_id, selector=f"#{field_id}", label=label,
        semantic_type=classify_semantics(label=label, control_type=control),
        control_type=control, required=required,
        options=[FieldOption(value=f"v{i}", label=o) for i, o in enumerate(options)] or None)


def resolve(provider: Jev, candidate: CandidateProfile, job: JobRecord,
            *fields: ApplicationField) -> tuple[ApplicationPacket, PacketContext, AIFormRouter,
                                                 DynamicPacketResolver]:
    r = router(provider)
    form = r.annotate(ApplicationForm(url="https://synthetic.test/apply", fields=list(fields)),
                      document_id="wp10-pools")
    ctx = PacketContext(form=form, candidate=candidate, job=job, application=Application(
        id="app-wp10", request_id="request-wp10", job_id=job.id, candidate_id=candidate.id,
        state=ApplicationState.INSPECTING, version=1,
        created_at="2026-09-24T00:00:00Z", updated_at="2026-09-24T00:00:00Z"))
    resolver = DynamicPacketResolver(r.decisions, router=r)
    packet = asyncio.run(resolver.resolve(ctx))
    assert ctx.problems(packet) == []
    return packet, ctx, r, resolver


# --- residence types --------------------------------------------------------------------

GREENHOUSE_US = "Do you currently reside in the US?"
VERCEL_COUNTRIES = ("Are you currently based in any of these countries? United States, Canada, "
                    "United Kingdom, Germany")


@pytest.mark.parametrize("label,split,confidence,expected", [
    # Greenhouse, pilot 7: the pooled mass is exactly 0.95 and no other type exceeds 0.03.
    (GREENHOUSE_US, {"COUNTRY": 0.24, "LOCATION": 0.71, "CUSTOM_BOOLEAN": 0.03, "UNKNOWN": 0.02},
     0.61, SemanticType.LOCATION),
    # Vercel, pilot 7: an even split; the leading residence type wins.
    (VERCEL_COUNTRIES, {"COUNTRY": 0.49, "LOCATION": 0.50, "CUSTOM_BOOLEAN": 0.01},
     0.50, SemanticType.LOCATION),
    (VERCEL_COUNTRIES, {"COUNTRY": 0.50, "LOCATION": 0.49, "CUSTOM_BOOLEAN": 0.01},
     0.50, SemanticType.COUNTRY),
])
def test_a_split_residence_reading_is_one_reading_whatever_its_confidence(
    label: str, split: dict[str, float], confidence: float, expected: SemanticType,
) -> None:
    field = observed(label, ControlType.SELECT, "Yes", "No")
    assert field.semantic_type is SemanticType.CUSTOM_SELECT  # the heuristics leave it custom
    provider = Jev({"answer": {"s": (split, confidence)}})
    r = router(provider)
    annotated = r.annotate(ApplicationForm(url="https://synthetic.test/apply", fields=[field]),
                           document_id="residence")
    assert annotated.fields[0].semantic_type is expected
    decision = r.report_for(annotated).fields[0]
    assert decision.semantic_type is expected
    assert decision.semantic_confidence == confidence  # the raw reading is kept for the trace
    # On Yes/No options the CUSTOM_BOOLEAN shape joins the residence pool (round 2).
    assert decision.semantic_pool_share == pytest.approx(
        sum(split.get(t, 0.0) for t in ("LOCATION", "COUNTRY", "STATE", "CITY", "CUSTOM_BOOLEAN")))
    assert decision.route is FieldRoute.COPY_KNOWN


def test_the_greenhouse_residence_question_is_answered_from_the_verified_address(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    split = {"COUNTRY": 0.24, "LOCATION": 0.71, "CUSTOM_BOOLEAN": 0.03, "UNKNOWN": 0.02}
    provider = Jev({"answer": {"s": (split, 0.61)}}, picks={"residence": "o0"})
    packet, ctx, _, resolver = resolve(provider, fictional_candidate, mock_job,
                                       observed(GREENHOUSE_US, ControlType.SELECT, "Yes", "No"))
    assert ctx.form.field("answer").semantic_type is SemanticType.LOCATION
    assert packet.is_complete
    [answer] = packet.answers
    assert (answer.value.value, answer.value.label) == ("v0", "Yes")
    assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY
    [request] = provider.asked("residence")
    assert request["state"]["applicant_address"]["country"] == "United States"
    assert [t["status"] for t in resolver.narrative_traces
            if t["stage"] == "residence_screener"] == ["ANSWERED"]


@pytest.mark.parametrize("split", [
    # The pool reaches 0.96 but another reading holds 0.04, over the 0.03 outside bound.
    {"COUNTRY": 0.50, "LOCATION": 0.46, "CUSTOM_TEXT": 0.04},
    # Every outside reading is small, but the pool is only 0.94.
    {"COUNTRY": 0.40, "LOCATION": 0.54, "CUSTOM_TEXT": 0.02, "UNKNOWN": 0.02,
     "RELOCATION": 0.02},
    # Work authorization is not residence, however it is split.
    {"COUNTRY": 0.50, "WORK_AUTHORIZATION": 0.50},
])
def test_a_pool_short_of_the_gate_stays_unknown_and_holds(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, split: dict[str, float],
) -> None:
    provider = Jev({"answer": {"s": (split, 0.97)}}, picks={"residence": "o0"})
    packet, ctx, r, _ = resolve(provider, fictional_candidate, mock_job,
                                observed(GREENHOUSE_US, ControlType.SELECT, "Yes", "No"))
    assert ctx.form.field("answer").semantic_type is SemanticType.UNKNOWN
    decision = r.report_for(ctx.form).fields[0]
    assert decision.semantic_pool_share == pytest.approx(
        sum(split.get(t, 0.0) for t in ("LOCATION", "COUNTRY", "STATE", "CITY")))
    assert packet.answers == [] and not provider.asked("residence")
    assert [m.field_id for m in packet.missing_inputs] == ["answer"]


def test_text_controls_are_never_pooled() -> None:
    field = observed("Where do you currently live?", ControlType.TEXT)
    provider = Jev({"answer": {"s": ({"COUNTRY": 0.49, "LOCATION": 0.50, "CITY": 0.01}, 0.5)}})
    r = router(provider)
    annotated = r.annotate(ApplicationForm(url="https://synthetic.test/apply", fields=[field]),
                           document_id="text")
    decision = r.report_for(annotated).fields[0]
    assert decision.semantic_type is SemanticType.UNKNOWN
    assert decision.semantic_pool_share is None


def test_a_confident_residence_type_needs_no_pool_and_other_types_are_not_pooled() -> None:
    fields = [observed(GREENHOUSE_US, ControlType.RADIO, "Yes", "No", field_id="us"),
              observed("How did you hear about this job?", ControlType.SELECT, "LinkedIn",
                       "Company website", field_id="source")]
    provider = Jev({"us": {"s": ({"COUNTRY": 0.97, "LOCATION": 0.03}, 0.95)},
                    "source": {"s": ({"REFERRAL_SOURCE": 0.98, "LOCATION": 0.02}, 0.96)}})
    r = router(provider)
    report = r.classify_form(ApplicationForm(url="https://synthetic.test/apply", fields=fields),
                             document_id="confident")
    assert report.field("us").semantic_type is SemanticType.COUNTRY
    assert report.field("us").semantic_pool_share == pytest.approx(1.0)
    # The heuristics typed it, so Jev is not asked its meaning and nothing is pooled.
    assert fields[1].semantic_type is SemanticType.REFERRAL_SOURCE
    assert report.field("source").semantic_type is SemanticType.REFERRAL_SOURCE
    assert report.field("source").semantic_pool_share is None


# --- the required resume's attachment and parser purposes ------------------------------

ASHBY_RESUME = "Resume / or drag and drop here"
ROUTE_099 = ({"APPROVED_DOCUMENT": 0.99, "AMBIGUOUS": 0.01}, 0.97)
ROUTE_090 = ({"APPROVED_DOCUMENT": 0.90, "AMBIGUOUS": 0.10}, 0.90)
"""A route not sure the upload is the approved document: the purpose gates decide."""


def resume_traces(resolver: DynamicPacketResolver) -> list[dict[str, Any]]:
    return [t for t in resolver.narrative_traces if t["stage"] == "resume_upload"]


@pytest.mark.parametrize("purpose,autofill", [
    ({"APPLICATION_ATTACHMENT": 0.52, "AUTOFILL_PARSER": 0.46, "OTHER_OR_UNCLEAR": 0.02}, True),
    ({"APPLICATION_ATTACHMENT": 0.46, "AUTOFILL_PARSER": 0.52, "OTHER_OR_UNCLEAR": 0.02}, True),
    # Unsplit but not confident: the pooled gate does not read the per-choice confidence,
    # and a purpose that is not confidently an attachment may also be parsed (round 2).
    ({"APPLICATION_ATTACHMENT": 0.97, "AUTOFILL_PARSER": 0.03}, True),
])
def test_the_ashby_required_resume_is_approved_on_its_pooled_purpose(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    purpose: dict[str, float], autofill: bool,
) -> None:
    field = observed(ASHBY_RESUME, ControlType.FILE)
    assert field.semantic_type is SemanticType.RESUME
    provider = Jev({"answer": {"r": ROUTE_090, "d": (purpose, 0.55)}})
    packet, ctx, r, resolver = resolve(provider, fictional_candidate, mock_job, field)
    decision = r.report_for(ctx.form).fields[0]
    assert decision.route is FieldRoute.APPROVED_DOCUMENT
    assert decision.autofill is autofill
    assert decision.document_pool_share == pytest.approx(
        purpose["APPLICATION_ATTACHMENT"] + purpose["AUTOFILL_PARSER"])
    assert decision.document_purpose_confidence == 0.55
    [answer] = packet.answers
    assert answer.semantic_type is SemanticType.RESUME
    assert answer.provenance.source is AnswerSource.RESUME
    assert answer.provenance.reference_ids == [fictional_candidate.resume.id]
    assert len(resume_traces(resolver)) == int(autofill)


@pytest.mark.parametrize("purpose,route", [
    # 0.04 on another purpose: over the outside bound, so the per-choice gate decides.
    ({"APPLICATION_ATTACHMENT": 0.50, "AUTOFILL_PARSER": 0.46, "OTHER_OR_UNCLEAR": 0.04},
     FieldRoute.AMBIGUOUS),
    ({"APPLICATION_ATTACHMENT": 0.30, "AUTOFILL_PARSER": 0.66, "OTHER_OR_UNCLEAR": 0.04},
     FieldRoute.UNSUPPORTED),
    ({"APPLICATION_ATTACHMENT": 0.40, "AUTOFILL_PARSER": 0.50, "OTHER_OR_UNCLEAR": 0.10},
     FieldRoute.UNSUPPORTED),
])
def test_a_resume_purpose_with_outside_mass_is_not_pooled(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    purpose: dict[str, float], route: FieldRoute,
) -> None:
    # With a route that is not sure (round 2 approves a sure one whatever the purpose).
    provider = Jev({"answer": {"r": ROUTE_090, "d": (purpose, 0.55)}})
    packet, ctx, r, resolver = resolve(provider, fictional_candidate, mock_job,
                                       observed(ASHBY_RESUME, ControlType.FILE))
    decision = r.report_for(ctx.form).fields[0]
    assert decision.route is route and decision.autofill is False
    assert decision.document_pool_share == pytest.approx(1 - purpose["OTHER_OR_UNCLEAR"])
    assert packet.answers == [] and not packet.is_complete
    assert not resume_traces(resolver)


@pytest.mark.parametrize("label,required,semantic", [
    (ASHBY_RESUME, False, SemanticType.RESUME),  # optional: never pooled
    ("Cover letter", True, SemanticType.COVER_LETTER),  # not the resume
    ("Portfolio or work samples", True, SemanticType.UNKNOWN),  # not the resume
])
def test_only_a_required_resume_upload_is_pooled(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    label: str, required: bool, semantic: SemanticType,
) -> None:
    split = {"APPLICATION_ATTACHMENT": 0.52, "AUTOFILL_PARSER": 0.46, "OTHER_OR_UNCLEAR": 0.02}
    field = observed(label, ControlType.FILE, required=required)
    assert field.semantic_type is semantic
    provider = Jev({"answer": {"r": ROUTE_099, "d": (split, 0.55), "s": "UNKNOWN"}})
    packet, ctx, r, resolver = resolve(provider, fictional_candidate, mock_job, field)
    decision = r.report_for(ctx.form).fields[0]
    # The per-choice attachment gate still decides these: 0.52 at confidence 0.55 fails it.
    assert decision.route is FieldRoute.AMBIGUOUS
    assert decision.reason == "File control purpose is not a verified application attachment"
    assert decision.document_pool_share == pytest.approx(0.98)
    assert packet.answers == []
    assert not resume_traces(resolver)


def test_a_confident_ordinary_attachment_still_passes_the_per_choice_gate(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    # 0.04 elsewhere keeps the resume out of the pool; the attachment reading alone is
    # confident, so the ordinary attachment gate approves it as before.
    purpose = {"APPLICATION_ATTACHMENT": 0.96, "OTHER_OR_UNCLEAR": 0.04}
    provider = Jev({"answer": {"r": ROUTE_099, "d": (purpose, 0.95)}})
    packet, ctx, r, _ = resolve(provider, fictional_candidate, mock_job,
                                observed(ASHBY_RESUME, ControlType.FILE))
    decision = r.report_for(ctx.form).fields[0]
    assert decision.route is FieldRoute.APPROVED_DOCUMENT and decision.autofill is False
    assert [a.provenance.source for a in packet.answers] == [AnswerSource.RESUME]


@pytest.mark.parametrize("route", ["WRITER", ({"APPROVED_DOCUMENT": 0.6, "WRITER": 0.4}, 0.6)])
def test_a_pooled_resume_stays_the_resume_after_a_writer_reading(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, route: Spec,
) -> None:
    # A prose reading made the meaning long text before the pooled resume gate approved the
    # upload; the annotated type must still be the resume, or the resume is never attached.
    split = {"APPLICATION_ATTACHMENT": 0.52, "AUTOFILL_PARSER": 0.46, "OTHER_OR_UNCLEAR": 0.02}
    provider = Jev({"answer": {"r": route, "n": "prose", "d": (split, 0.55)}})
    packet, ctx, r, _ = resolve(provider, fictional_candidate, mock_job,
                                observed(ASHBY_RESUME, ControlType.FILE))
    decision = r.report_for(ctx.form).fields[0]
    assert decision.route is FieldRoute.APPROVED_DOCUMENT and decision.autofill is True
    assert decision.semantic_type is ctx.form.field("answer").semantic_type is SemanticType.RESUME
    assert "uploaded first" in decision.reason and "re-inspected" in decision.reason
    assert [a.provenance.source for a in packet.answers] == [AnswerSource.RESUME]
