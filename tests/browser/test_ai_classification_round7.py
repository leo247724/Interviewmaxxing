"""WP10 round 7: the applicant's own narratives keep the applicant's own source scope.

Live (mass batch ``mass-20260925-a``), Jev under ``full-form-routing-v13`` routed six
narrative textareas WRITER at 1.0 and read their source as an explicit answer ("Why
Dovetail & this role?" 0.41, "Why did you decide to apply to this role at ClickUp?" 0.78,
"Tell us a bit about why you're applying to work at Yondr. …" 0.34, "How do you use AI to 10x
your output? …" 0.24) or as the applicant's past below the gate ("How are you currently using
AI in your workflows?" 0.47, "Tell us about yourself & your interest in Smalls." 0.49), and
the resolver held all six before the writer. By the owner's rule these are the applicant's
own narrative (motivation or practice): the classifier gives them HISTORICAL_OR_CONTEXTUAL
with confidence, in the v14 criteria and, whatever Jev's split among the applicant's readings,
in the pooled source gate.

The brief gives one number per reading, the confidence (and, where the arithmetic allows, the
leading probability); the fixtures give the rest of the mass to the applicant's readings and at
most 0.02 to another person or entity. A reading with more than the outside bound (0.03) on
another person keeps Jev's scope (``test_another_person_above_the_outside_bound_keeps_jev_reading``).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser.ai import (
    AIFormRouter,
    BoundedDecisions,
    CallBudget,
    DynamicPacketResolver,
)
from interviewmaxxing_browser.ai.classification import (
    PROMPT_VERSION,
    FieldRoute,
    FieldRouteDecision,
    SourceScope,
)
from interviewmaxxing_browser.ai.providers import NarrativeDraft
from interviewmaxxing_browser.ai.routing import MIN_CONFIDENCE, _scope_passes
from interviewmaxxing_browser.semantics import classify as classify_semantics
from interviewmaxxing_core import (
    Application,
    ApplicationField,
    ApplicationForm,
    ApplicationState,
    CandidateFact,
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
WRITER_READING: dict[str, Spec] = {"r": "WRITER", "n": "prose", "s": "CUSTOM_LONG_TEXT"}
DEFAULTS = {"r": "COPY_KNOWN", "n": "literal", "u": "APPLICANT_CURRENT", "s": "CUSTOM_TEXT"}
AC, HIST, EXPLICIT, UNCLEAR, OTHER = (s.value for s in (
    SourceScope.APPLICANT_CURRENT, SourceScope.HISTORICAL_OR_CONTEXTUAL, SourceScope.EXPLICIT_ANSWER,
    SourceScope.UNCLEAR, SourceScope.OTHER_PERSON_OR_ENTITY))


class Jev:
    """Scripted Jev. ``scripts[field_id][kind]`` answers a field's route (r), prose (n),
    source (u) or semantic (s) question: a choice (all mass, confidence 1.0) or
    (probabilities, confidence). Other choices pick NONE/hold/UNKNOWN or their first option;
    every noul answers 1.0 except ``candidate_narrative`` (the resolver's scope fallback),
    which answers 0.0, so a field reaches the writer only on the classifier's own scope."""

    def __init__(self, scripts: dict[str, dict[str, Spec]]) -> None:
        self.scripts = scripts
        self.requests: list[dict[str, Any]] = []

    def asked(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if name in r["questions"]]

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        self.requests.append(request)
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.0 if name == "candidate_narrative" else 1.0}
                continue
            criteria = list(question["criteria"])
            if name[0] in DEFAULTS and name[1:].isdigit():
                field_id = request["state"]["fields"][f"f{name[1:]}"]["field_id"]
                spec = self.scripts.get(field_id, {}).get(name[0], DEFAULTS[name[0]])
            else:
                spec = next((c for c in ("NONE", "hold", "UNKNOWN") if c in criteria), criteria[0])
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


def observed(label: str, field_id: str = "answer", *, control: ControlType = ControlType.TEXTAREA,
             options: tuple[str, ...] = (), input_type: str | None = None) -> ApplicationField:
    """A required field as the inspector reports it: typed by the browser heuristics."""
    return ApplicationField(id=field_id, selector=f"#{field_id}", label=label, required=True,
        semantic_type=classify_semantics(label=label, control_type=control), control_type=control,
        input_type=input_type,
        options=[FieldOption(value=f"v{i}", label=o) for i, o in enumerate(options)] or None)


def decide(fld: ApplicationField, source: Spec, **reading: Spec) -> FieldRouteDecision:
    provider = Jev({fld.id: {**WRITER_READING, "u": source, **reading}})
    report = router(provider).classify_form(
        ApplicationForm(url="https://synthetic.test/apply", fields=[fld]), document_id="wp10-round7")
    return report.fields[0]


# The six live readings (field id, wording, Jev's source probabilities, its confidence).
LIVE: list[tuple[str, str, dict[str, float], float]] = [
    ("dovetail", "Why Dovetail & this role?",
     {EXPLICIT: 0.41, HIST: 0.33, AC: 0.18, UNCLEAR: 0.06, OTHER: 0.02}, 0.41),
    ("clickup", "Why did you decide to apply to this role at ClickUp?",
     {EXPLICIT: 0.78, HIST: 0.12, AC: 0.07, UNCLEAR: 0.02, OTHER: 0.01}, 0.78),
    ("yondr", "Tell us a bit about why you\u2019re applying to work at Yondr. What excited you about this role?",
     {EXPLICIT: 0.34, HIST: 0.31, AC: 0.27, UNCLEAR: 0.07, OTHER: 0.01}, 0.34),
    ("ai_output", "How do you use AI to 10x your output? Please mention any tools\u2026",
     {EXPLICIT: 0.40, AC: 0.30, HIST: 0.22, UNCLEAR: 0.06, OTHER: 0.02}, 0.24),
    ("ai_workflows", "How are you currently using AI in your workflows?",
     {HIST: 0.47, AC: 0.45, EXPLICIT: 0.05, UNCLEAR: 0.03, OTHER: 0.0}, 0.47),
    ("smalls", "Tell us about yourself & your interest in Smalls.",
     {HIST: 0.49, AC: 0.38, UNCLEAR: 0.09, EXPLICIT: 0.03, OTHER: 0.01}, 0.49),
]
LIVE_IDS = [case[0] for case in LIVE]


@pytest.mark.parametrize(("field_id", "wording", "probabilities", "confidence"), LIVE, ids=LIVE_IDS)
def test_the_live_narratives_are_the_applicants_own_with_confidence(
    field_id: str, wording: str, probabilities: dict[str, float], confidence: float,
) -> None:
    decision = decide(observed(wording, field_id), (probabilities, confidence))
    assert (decision.route, decision.semantic_type) == (FieldRoute.WRITER, SemanticType.CUSTOM_LONG_TEXT)
    assert decision.source_scope is SourceScope.HISTORICAL_OR_CONTEXTUAL
    share = 1.0 - probabilities[OTHER]
    assert decision.source_scope_confidence == pytest.approx(share) and share >= 0.95
    assert decision.source_scope_probabilities == pytest.approx(
        {AC: 0.0, HIST: share, EXPLICIT: 0.0, UNCLEAR: 0.0, OTHER: probabilities[OTHER]})
    assert _scope_passes(decision, SourceScope.HISTORICAL_OR_CONTEXTUAL)  # the resolver's own gate
    # Jev's own reading is kept beside it.
    assert decision.scoped_from is SourceScope(max(probabilities, key=probabilities.__getitem__))
    assert (decision.scoped_from_confidence, decision.scoped_from_probabilities) == (confidence, probabilities)
    assert decision.profile_copy_allowed is False


@pytest.mark.parametrize("wording", [
    "Tell us a bit about why you are applying to work at Yondr. What excited you about this role?",
    "Tell us a bit about why you're applying to work at Yondr. What excited you about this role?",
    "Why Dovetail and this role?", "Why Dovetail + this position?", "What drew you to Smalls?",
    "Why do you want to work at Smalls?", "Tell me about yourself.",
    "Describe how you use AI in your current role.", "How have you used generative AI tools in your work?",
    "At Lorikeet, AI is part of how we work every day. Tell us about a specific time you've used AI\u2026",
])
def test_other_wordings_of_the_same_narratives(wording: str) -> None:
    decision = decide(observed(wording), LIVE[0][2:4])  # the Dovetail reading
    assert decision.source_scope is SourceScope.HISTORICAL_OR_CONTEXTUAL
    assert _scope_passes(decision, SourceScope.HISTORICAL_OR_CONTEXTUAL)


@pytest.mark.parametrize("source", [
    ({AC: 0.97, HIST: 0.03}, 0.95), ({HIST: 0.99, AC: 0.01}, 0.97),
], ids=["current", "historical"])
def test_a_sure_applicant_reading_is_jevs_own(source: tuple[dict[str, float], float]) -> None:
    """Lorikeet's question read HISTORICAL_OR_CONTEXTUAL at 1.0 live: Jev's own reading stands."""
    decision = decide(observed("How are you currently using AI in your workflows?"), source)
    assert decision.scoped_from is None and decision.scoped_from_probabilities == {}
    assert decision.source_scope is SourceScope(max(source[0], key=source[0].__getitem__))
    assert (decision.source_scope_confidence, decision.source_scope_probabilities) == (
        source[1], {AC: 0.0, HIST: 0.0, EXPLICIT: 0.0, UNCLEAR: 0.0, OTHER: 0.0} | source[0])


# --- The live form through the resolver ------------------------------------------------------

@dataclass
class Retriever:
    facts: list[CandidateFact]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def retrieve(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(facts=self.facts, job_evidence=[], voice_samples=[], receipt={"status": "OK"})


@dataclass
class Writer:
    sentences: list[dict[str, Any]]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def write(self, **kwargs: Any) -> NarrativeDraft:
        self.calls.append(kwargs)
        return NarrativeDraft.model_validate({"status": "READY", "sentences": self.sentences,
                                              "missing_information": []})


def test_the_live_narratives_reach_the_writer_without_a_scope_hold(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    evidence = fictional_candidate.verified_facts()[0].model_copy(update={
        "id": "fact.ai", "key": "experience", "value": "Built documented AI workflows for paid media.",
        "evidence": ["Built documented AI workflows for paid media."]})
    candidate = fictional_candidate.model_copy(update={"facts": [evidence], "experience": [], "education": []})
    control = observed("Describe a campaign you ran", "campaign")
    fields = [observed(wording, field_id) for field_id, wording, _, _ in LIVE] + [control]
    provider = Jev({field_id: {**WRITER_READING, "u": (probabilities, confidence)}
                    for field_id, _, probabilities, confidence in LIVE}
                   | {"campaign": {**WRITER_READING, "u": LIVE[0][2:4]}})
    r = router(provider)
    form = r.annotate(ApplicationForm(url="https://synthetic.test/apply", fields=fields),
                      document_id="mass-20260925-a")
    context = PacketContext(form=form, candidate=candidate, job=mock_job, application=Application(
        id="app-wp10-r7", request_id="request-wp10-r7", job_id=mock_job.id, candidate_id=candidate.id,
        state=ApplicationState.INSPECTING, version=1,
        created_at="2026-09-25T00:00:00Z", updated_at="2026-09-25T00:00:00Z"))
    writer = Writer([{"text": evidence.value, "fact_ids": [evidence.id]}])
    packet = asyncio.run(DynamicPacketResolver(r.decisions, writer, router=r,
                                               retriever=Retriever([evidence])).resolve(context))
    assert context.problems(packet) == []
    answered = {a.field_id: a for a in packet.answers}
    assert sorted(answered) == sorted(LIVE_IDS)
    assert len(writer.calls) == len(LIVE)
    assert all(answered[i].confidence >= MIN_CONFIDENCE for i in LIVE_IDS)
    assert all(answered[i].provenance.reference_ids == [evidence.id] for i in LIVE_IDS)
    # No field needed the resolver's narrative-scope fallback (it would have held them: 0.0).
    assert not provider.asked("candidate_narrative")
    # An ordinary narrative with the same explicit-answer reading still holds: the rule is the
    # owner's for these questions only, never a blanket scope for every writer field.
    assert [m.field_id for m in packet.missing_inputs] == ["campaign"]


# --- What keeps Jev's reading -----------------------------------------------------------------

def test_another_person_above_the_outside_bound_keeps_jev_reading() -> None:
    source = ({EXPLICIT: 0.41, HIST: 0.30, AC: 0.18, UNCLEAR: 0.07, OTHER: 0.04}, 0.41)
    decision = decide(observed("Why Dovetail & this role?"), source)
    assert decision.scoped_from is None
    assert (decision.source_scope, decision.source_scope_confidence) == (SourceScope.EXPLICIT_ANSWER, 0.41)
    assert decision.source_scope_probabilities == source[0]
    assert not _scope_passes(decision, SourceScope.HISTORICAL_OR_CONTEXTUAL)


@pytest.mark.parametrize("wording", [
    "Why do you want to work remotely?",
    "Why are you interested in this role, and what are your salary expectations?",
    "Why did you decide to apply? Are you willing to relocate?",
    "Why did you leave the company?",
    "Tell us why there is a gap in your employment.",
    "Tell us about yourself (optional: gender, pronouns).",
    "Please describe any disability accommodations and how you use assistive tools.",
    "How does your manager use AI in your team's workflows?",
    "Tell us about yourself. By submitting you agree to our privacy policy.",
    "Describe a campaign you ran.",
    "Explain how you evaluate paid-media lead quality.",
])
def test_wording_that_is_not_the_applicants_own_narrative_keeps_jev_reading(wording: str) -> None:
    decision = decide(observed(wording), LIVE[0][2:4])
    assert decision.scoped_from is None and decision.source_scope is SourceScope.EXPLICIT_ANSWER


def test_only_a_writer_routed_text_narrative_is_rescoped() -> None:
    wording = "Why did you decide to apply to this role at ClickUp?"
    source = LIVE[1][2:4]
    for decision in (
        decide(observed(wording), source, r="HUMAN_INPUT", n="literal"),
        decide(observed(wording), source, r="AMBIGUOUS", n="literal"),
        decide(observed(wording, control=ControlType.SELECT, options=("I love the product", "Other")),
               source, s="CUSTOM_SELECT"),
        decide(observed(wording, control=ControlType.TEXT, input_type="url"), source),
    ):
        assert decision.scoped_from is None and decision.source_scope is SourceScope.EXPLICIT_ANSWER


def test_a_protected_reading_is_never_rescoped() -> None:
    """A salary reading of the same wording makes the field an explicit human answer."""
    decision = decide(observed("Why did you decide to apply to this role at ClickUp?"), LIVE[1][2:4],
                      s="SALARY_EXPECTATION")
    assert decision.route is FieldRoute.HUMAN_INPUT and decision.semantic_type is SemanticType.SALARY_EXPECTATION
    assert decision.scoped_from is None and decision.source_scope is SourceScope.EXPLICIT_ANSWER


# --- The v14 criteria -------------------------------------------------------------------------

def test_the_source_criteria_name_the_applicants_own_narratives() -> None:
    provider = Jev({"answer": WRITER_READING})
    router(provider).classify_form(ApplicationForm(url="https://synthetic.test/apply",
        fields=[observed("Why Dovetail & this role?")]), document_id="criteria")
    [request] = provider.requests
    assert request["state"]["version"] == PROMPT_VERSION == "full-form-routing-v14"
    criteria = request["questions"]["u0"]["criteria"]
    assert ("So is the applicant's own narrative: why they apply or chose this role/company, their "
            "interest, 'tell us about yourself', how they use AI or other tools (now or before).") in criteria[HIST]
    assert criteria[EXPLICIT].endswith(
        "their own narrative (why they applied, their interest, how they use AI), or where the "
        "applicant currently lives.")
    assert ("Composing cover letter or other personal narrative text, even about the present, is "
            "HISTORICAL_OR_CONTEXTUAL.") in criteria[AC]
    assert "A company or tool named in the applicant's own narrative is not." in criteria[OTHER]
