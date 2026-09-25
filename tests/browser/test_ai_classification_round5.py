"""WP10 round 5: the legal types pool with the choice shapes on select and radio controls.

Live (Rippling): "Is your authorization to work in the United States" with the options
Permanent / Temporary and subject to expiration read WORK_AUTHORIZATION 0.86 / CUSTOM_BOOLEAN
0.08 / CUSTOM_SELECT 0.05. Under the 0.95 per-choice gate that is UNKNOWN, so the round-9
derivation from the stated status never saw the field. On a single-choice control the custom
boolean and custom select readings restate the control's shape, not another meaning: they
join the legal type's pool (WORK_AUTHORIZATION, and SPONSORSHIP likewise) the way the
residence types pool, and the field takes the legal type when the pooled share reaches 0.95
and the legal type outweighs the shapes together. A split led by a shape stays UNKNOWN, the
two legal types never pool with each other, and text controls are never pooled.
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
from interviewmaxxing_browser.ai.classification import LEGAL_TYPES, FieldRoute
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
                probabilities = {c: spec[0].get(c, 0.0) for c in criteria}
                confidence = spec[1]
            answers[name] = {"type": "choice", "confidence": confidence, "probabilities": probabilities,
                             "choice": max(criteria, key=lambda c: probabilities[c])}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())


def router(provider: Jev) -> AIFormRouter:
    return AIFormRouter(BoundedDecisions(JevClient(ApiKey("synthetic-test", source="fixture"),
        transport=provider, max_attempts=1), CallBudget()))


def observed(label: str, control: ControlType, *options: str, field_id: str = "answer") -> ApplicationField:
    """A field as the inspector reports it: typed by the browser heuristics."""
    return ApplicationField(id=field_id, selector=f"#{field_id}", label=label,
        semantic_type=classify_semantics(label=label, control_type=control),
        control_type=control, required=True,
        options=[FieldOption(value=f"v{i}", label=o) for i, o in enumerate(options)] or None)


def classified(provider: Jev, *fields: ApplicationField) -> tuple[ApplicationForm, AIFormRouter]:
    r = router(provider)
    form = r.annotate(ApplicationForm(url="https://synthetic.test/apply", fields=list(fields)),
                      document_id="wp10-round5")
    return form, r


def with_status(candidate: CandidateProfile, code: str) -> CandidateProfile:
    """The stated status as the simple-answers import writes it: one untyped GLOBAL answer."""
    status = SavedAnswer(id="sa.status", scope=AnswerScope.GLOBAL, semantic_type=None,
                         question=WORK_AUTHORIZATION_STATUS_QUESTION, value=code,
                         confirmed_at="2026-09-02T12:00:00Z")
    return candidate.model_copy(update={"saved_answers": [*candidate.saved_answers, status]})


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


RIPPLING_AUTHORIZATION = "Is your authorization to work in the United States"
RIPPLING_OPTIONS = ("Permanent", "Temporary and subject to expiration")
VISA_SUPPORT = "Do you need employer support for a visa transfer?"
RIPPLING_SPLIT = {"WORK_AUTHORIZATION": 0.86, "CUSTOM_BOOLEAN": 0.08, "CUSTOM_SELECT": 0.05,
                  "UNKNOWN": 0.01}
"""Live: 0.99 pooled, 0.01 outside, and the legal type outweighs the shapes (0.13)."""
SPONSORSHIP_SPLIT = {"SPONSORSHIP": 0.62, "CUSTOM_BOOLEAN": 0.30, "CUSTOM_SELECT": 0.06,
                     "UNKNOWN": 0.02}
SHAPES = ("CUSTOM_BOOLEAN", "CUSTOM_SELECT")


def pooled(split: dict[str, float], legal: str) -> float:
    return sum(split.get(t, 0.0) for t in (legal, *SHAPES))


def test_the_legal_types_are_the_two_the_status_derives() -> None:
    assert LEGAL_TYPES == (SemanticType.WORK_AUTHORIZATION, SemanticType.SPONSORSHIP)


@pytest.mark.parametrize("control", [ControlType.SELECT, ControlType.RADIO])
@pytest.mark.parametrize("label,options,split,expected", [
    (RIPPLING_AUTHORIZATION, RIPPLING_OPTIONS, RIPPLING_SPLIT, SemanticType.WORK_AUTHORIZATION),
    # Exactly the gate: 0.95 pooled, 0.03 outside, on non-yes/no options.
    (RIPPLING_AUTHORIZATION, RIPPLING_OPTIONS,
     {"WORK_AUTHORIZATION": 0.60, "CUSTOM_SELECT": 0.20, "CUSTOM_BOOLEAN": 0.15, "CUSTOM_TEXT": 0.03,
      "UNKNOWN": 0.02}, SemanticType.WORK_AUTHORIZATION),
    (VISA_SUPPORT, ("Yes", "No"), SPONSORSHIP_SPLIT, SemanticType.SPONSORSHIP),
])
def test_a_legal_type_that_outweighs_the_choice_shapes_takes_the_field_on_the_pooled_share(
    control: ControlType, label: str, options: tuple[str, ...], split: dict[str, float],
    expected: SemanticType,
) -> None:
    field = observed(label, control, *options)
    assert field.semantic_type is SemanticType.CUSTOM_SELECT  # the heuristics leave it custom
    form, r = classified(Jev({"answer": {"s": (split, 0.58)}}), field)
    assert form.fields[0].semantic_type is expected
    decision = r.report_for(form).fields[0]
    assert decision.semantic_type is expected
    assert decision.semantic_confidence == 0.58  # the raw reading is kept for the trace
    assert decision.semantic_probabilities[expected.value] == split[expected.value]
    assert decision.semantic_pool_share == pytest.approx(pooled(split, expected.value))
    assert decision.route is FieldRoute.COPY_KNOWN


@pytest.mark.parametrize("code,expected", [("us_permanent_resident", "Permanent"),
                                           ("us_citizen", "Permanent")])
def test_the_rippling_authorization_question_is_derived_from_the_stated_status(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, code: str, expected: str,
) -> None:
    provider = Jev({"answer": {"s": (RIPPLING_SPLIT, 0.58)}})
    packet, ctx, _, resolver = resolve(
        provider, with_status(fictional_candidate, code), mock_job,
        observed(RIPPLING_AUTHORIZATION, ControlType.SELECT, *RIPPLING_OPTIONS))
    assert ctx.form.field("answer").semantic_type is SemanticType.WORK_AUTHORIZATION
    assert packet.is_complete
    [answer] = packet.answers
    assert answer.value.label == expected
    assert answer.semantic_type is SemanticType.WORK_AUTHORIZATION
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, ["sa.status"])
    assert not provider.asked("status") and not provider.asked("wording")
    [trace] = stage_traces(resolver, "status_derivation")
    assert (trace["status"], trace["via"]) == ("ANSWERED", "table")
    assert code not in json.dumps(resolver.narrative_traces)  # the stated value never reaches a trace


def test_a_pooled_sponsorship_question_takes_one_truthful_option_decision(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = Jev({"answer": {"s": (SPONSORSHIP_SPLIT, 0.58)}}, picks={"status": "o0"})
    packet, ctx, _, resolver = resolve(provider, with_status(fictional_candidate, "h1b"), mock_job,
                                       observed(VISA_SUPPORT, ControlType.RADIO, "Yes", "No"))
    assert ctx.form.field("answer").semantic_type is SemanticType.SPONSORSHIP
    [request] = provider.asked("status")
    assert request["state"]["status"]["code"] == "h1b"
    [answer] = packet.answers
    assert (answer.value.label, answer.provenance.reference_ids) == ("Yes", ["sa.status"])
    [trace] = stage_traces(resolver, "status_derivation")
    assert (trace["status"], trace["via"], trace["choice"]) == ("ANSWERED", "jev", "o0")


@pytest.mark.parametrize("split,recorded", [
    # Led by CUSTOM_SELECT: the shapes are the reading, and there is no legal pool to record.
    ({"CUSTOM_SELECT": 0.50, "WORK_AUTHORIZATION": 0.40, "CUSTOM_BOOLEAN": 0.09, "UNKNOWN": 0.01},
     0.0),
    # The legal type leads each shape but not the shapes together.
    ({"WORK_AUTHORIZATION": 0.47, "CUSTOM_SELECT": 0.30, "CUSTOM_BOOLEAN": 0.22, "UNKNOWN": 0.01},
     0.0),
    # The pool reaches 0.96 but a competing custom reading holds 0.04, over the 0.03 outside
    # bound: CUSTOM_TEXT is another meaning, not the control's shape.
    ({"WORK_AUTHORIZATION": 0.90, "CUSTOM_SELECT": 0.06, "CUSTOM_TEXT": 0.04}, 0.96),
    # Every outside reading is small, but the pool is only 0.93.
    ({"WORK_AUTHORIZATION": 0.80, "CUSTOM_SELECT": 0.10, "CUSTOM_BOOLEAN": 0.03, "UNKNOWN": 0.03,
      "RELOCATION": 0.02, "CUSTOM_TEXT": 0.02}, 0.93),
    # The two legal types never pool with each other: a different legal answer is outside.
    ({"WORK_AUTHORIZATION": 0.60, "SPONSORSHIP": 0.35, "CUSTOM_SELECT": 0.05}, 0.65),
    ({"SPONSORSHIP": 0.50, "WORK_AUTHORIZATION": 0.45, "CUSTOM_BOOLEAN": 0.05}, 0.55),
    # Residence is not a shape: a residence/legal split is neither reading.
    ({"WORK_AUTHORIZATION": 0.50, "COUNTRY": 0.45, "CUSTOM_SELECT": 0.05}, 0.55),
])
def test_a_split_led_by_a_shape_or_short_of_the_gate_stays_unknown_and_holds(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, split: dict[str, float],
    recorded: float,
) -> None:
    provider = Jev({"answer": {"s": (split, 0.97)}}, picks={"status": "o0"})
    packet, ctx, r, resolver = resolve(
        provider, with_status(fictional_candidate, "us_citizen"), mock_job,
        observed(RIPPLING_AUTHORIZATION, ControlType.SELECT, *RIPPLING_OPTIONS))
    assert ctx.form.field("answer").semantic_type is SemanticType.UNKNOWN
    decision = r.report_for(ctx.form).fields[0]
    assert decision.semantic_pool_share == pytest.approx(recorded)
    assert packet.answers == [] and not provider.asked("status")
    assert not stage_traces(resolver, "status_derivation")
    assert [m.field_id for m in packet.missing_inputs] == ["answer"]


@pytest.mark.parametrize("label,control,options,split", [
    ("Is your authorization to work in the United States permanent or temporary?",
     ControlType.TEXT, (), RIPPLING_SPLIT),
    (RIPPLING_AUTHORIZATION, ControlType.TEXT, (), {"WORK_AUTHORIZATION": 0.90, "CUSTOM_TEXT": 0.10}),
    (VISA_SUPPORT, ControlType.TEXT, (), SPONSORSHIP_SPLIT),
    # A select-all control is not a single choice either.
    (RIPPLING_AUTHORIZATION, ControlType.MULTISELECT, RIPPLING_OPTIONS, RIPPLING_SPLIT),
])
def test_text_controls_are_never_pooled(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
    control: ControlType, options: tuple[str, ...], split: dict[str, float],
) -> None:
    field = observed(label, control, *options)
    assert field.semantic_type in (SemanticType.CUSTOM_TEXT, SemanticType.CUSTOM_MULTISELECT)
    provider = Jev({"answer": {"s": (split, 0.58)}})
    packet, ctx, r, resolver = resolve(provider, with_status(fictional_candidate, "us_citizen"),
                                       mock_job, field)
    decision = r.report_for(ctx.form).fields[0]
    assert ctx.form.field("answer").semantic_type is decision.semantic_type is SemanticType.UNKNOWN
    assert decision.semantic_pool_share is None
    assert packet.answers == [] and not stage_traces(resolver, "status_derivation")


def test_a_confident_legal_type_needs_no_pool_and_a_heuristic_type_is_not_asked() -> None:
    fields = [observed(RIPPLING_AUTHORIZATION, ControlType.RADIO, *RIPPLING_OPTIONS, field_id="kind"),
              observed("Work authorization status", ControlType.SELECT, "Citizen", "Visa holder",
                       field_id="status")]
    assert fields[1].semantic_type is SemanticType.WORK_AUTHORIZATION
    provider = Jev({"kind": {"s": ({"WORK_AUTHORIZATION": 0.97, "CUSTOM_SELECT": 0.03}, 0.95)},
                    "status": {"s": ({"CUSTOM_SELECT": 0.60, "WORK_AUTHORIZATION": 0.40}, 0.5)}})
    form, r = classified(provider, *fields)
    report = r.report_for(form)
    assert report.field("kind").semantic_type is SemanticType.WORK_AUTHORIZATION
    assert report.field("kind").semantic_pool_share == pytest.approx(1.0)
    # The heuristics typed it, so Jev is not asked its meaning, nothing is pooled and an
    # explicit-answer type is never downgraded.
    assert report.field("status").semantic_type is SemanticType.WORK_AUTHORIZATION
    assert report.field("status").semantic_pool_share is None


def test_a_residence_split_is_read_before_the_legal_pool() -> None:
    # "Do you currently reside in the US?" at LOCATION 0.75 / CUSTOM_BOOLEAN 0.13 / COUNTRY
    # 0.11 pools into location (round 2) and the legal pool never reads the shape again.
    field = observed("Do you currently reside in the US?", ControlType.SELECT, "Yes", "No")
    split = {"LOCATION": 0.75, "CUSTOM_BOOLEAN": 0.13, "COUNTRY": 0.11, "WORK_AUTHORIZATION": 0.01}
    form, r = classified(Jev({"answer": {"s": (split, 0.6)}}), field)
    decision = r.report_for(form).fields[0]
    assert decision.semantic_type is SemanticType.LOCATION
    assert decision.semantic_pool_share == pytest.approx(0.99)
