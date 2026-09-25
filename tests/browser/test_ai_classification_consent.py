"""Consent and attestation only with their wording (WP10 item 3).

Pilot 7 held "Have you worked in a performance marketing agency environment?" and "Do you
have at least 8 years of total experience in direct response marketing …?" as explicit
answers: the browser heuristics typed them CONSENT ("marketing" matches their consent
rule) and the classifier kept every consent type. A yes/no question whose wording has no
consent or attestation language now comes out CUSTOM_BOOLEAN (``demoted_from`` records
the type it had), however the heuristics or Jev typed it.
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
    AnswerScope,
    Application,
    ApplicationField,
    ApplicationForm,
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
DEFAULTS = {"r": "COPY_KNOWN", "n": "literal", "u": "HISTORICAL_OR_CONTEXTUAL", "s": "CUSTOM_BOOLEAN",
            "d": "APPLICATION_ATTACHMENT"}


class Jev:
    """Scripted Jev. ``scripts[kind]`` answers every field's r/n/u/s/d question; ``picks``
    answers other choice questions by name (a spec is a choice, or (probabilities,
    confidence)). Unscripted choices pick NONE/UNKNOWN; nouls answer 0.0."""

    def __init__(self, scripts: dict[str, Spec] | None = None, picks: dict[str, Spec] | None = None) -> None:
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
                spec = self.scripts.get(name[0], DEFAULTS[name[0]])
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


def question(label: str, semantic: SemanticType, *options: str, control: ControlType = ControlType.SELECT,
             help_text: str | None = None, section: tuple[str, ...] = ()) -> ApplicationField:
    return ApplicationField(id="answer", selector="#answer", label=label, semantic_type=semantic,
        control_type=control, required=True, help_text=help_text, section_context=list(section),
        options=[FieldOption(value=f"v{i}", label=o) for i, o in enumerate(options)] or None)


def classify(provider: Jev, field: ApplicationField) -> tuple[ApplicationForm, Any]:
    r = router(provider)
    annotated = r.annotate(ApplicationForm(url="https://synthetic.test/apply", fields=[field]),
                           document_id="consent")
    return annotated, r.report_for(annotated).fields[0]


AGENCY = "Have you worked in a performance marketing agency environment?"
DIRECT_RESPONSE = ("Do you have at least 8 years of total experience in direct response marketing "
                   "(paid social, paid search or programmatic)?")


@pytest.mark.parametrize("label", [AGENCY, DIRECT_RESPONSE])
@pytest.mark.parametrize("typed", [SemanticType.CONSENT, SemanticType.ATTESTATION])
def test_a_consent_typed_yes_no_experience_question_is_a_custom_boolean(
    label: str, typed: SemanticType,
) -> None:
    # Typed as the pilot's heuristics typed it, so Jev is never asked its meaning.
    provider = Jev()
    annotated, decision = classify(provider, question(label, typed, "Yes", "No"))
    assert "s0" not in provider.requests[0]["questions"]
    assert annotated.fields[0].semantic_type is SemanticType.CUSTOM_BOOLEAN
    assert (decision.semantic_type, decision.demoted_from) == (SemanticType.CUSTOM_BOOLEAN, typed)
    assert decision.route is FieldRoute.COPY_KNOWN
    assert decision.source_requirement == "exact verified identity/fact or explicit scoped answer"


@pytest.mark.parametrize("label", [AGENCY, DIRECT_RESPONSE])
def test_whatever_the_heuristics_say_the_question_comes_out_a_custom_boolean(label: str) -> None:
    # The live heuristics type these CONSENT; a fixed heuristic types them CUSTOM_SELECT (and,
    # since round 6, a minimum-years question CUSTOM_BOOLEAN) and Jev reads a custom boolean.
    # Either way the classifier's type is CUSTOM_BOOLEAN.
    heuristic = classify_semantics(label=label, control_type=ControlType.SELECT)
    assert heuristic in (SemanticType.CONSENT, SemanticType.CUSTOM_SELECT, SemanticType.CUSTOM_BOOLEAN)
    _, decision = classify(Jev(), question(label, heuristic, "Yes", "No"))
    assert decision.semantic_type is SemanticType.CUSTOM_BOOLEAN


@pytest.mark.parametrize("split,demoted", [
    ({"CONSENT": 1.0}, SemanticType.CONSENT),  # a confident consent reading
    ({"ATTESTATION": 0.97, "CUSTOM_BOOLEAN": 0.03}, SemanticType.ATTESTATION),
    # Split between consent and a custom boolean: one reading without consent wording.
    ({"CONSENT": 0.58, "CUSTOM_BOOLEAN": 0.40, "UNKNOWN": 0.02}, SemanticType.CONSENT),
    ({"CUSTOM_BOOLEAN": 0.55, "ATTESTATION": 0.25, "CONSENT": 0.18, "UNKNOWN": 0.02}, None),
])
def test_jev_consent_mass_on_a_plain_yes_no_question_is_custom_boolean_mass(
    split: dict[str, float], demoted: SemanticType | None,
) -> None:
    provider = Jev({"s": (split, 0.62)})
    annotated, decision = classify(provider, question(AGENCY, SemanticType.CUSTOM_SELECT, "Yes", "No"))
    assert annotated.fields[0].semantic_type is SemanticType.CUSTOM_BOOLEAN
    assert decision.demoted_from is demoted
    assert decision.semantic_probabilities == {k: split.get(k, 0.0) for k in decision.semantic_probabilities}
    assert decision.route is FieldRoute.COPY_KNOWN


@pytest.mark.parametrize("split", [
    {"CONSENT": 0.60, "WORK_AUTHORIZATION": 0.40},  # another reading holds real mass
    {"CONSENT": 0.50, "CUSTOM_BOOLEAN": 0.44, "RELOCATION": 0.06},
])
def test_a_consent_split_with_another_reading_stays_unknown(split: dict[str, float]) -> None:
    _, decision = classify(Jev({"s": (split, 0.62)}),
                           question(AGENCY, SemanticType.CUSTOM_SELECT, "Yes", "No"))
    assert decision.semantic_type is SemanticType.UNKNOWN and decision.demoted_from is None


@pytest.mark.parametrize("field", [
    question("Do you consent to a background check?", SemanticType.CONSENT, "Yes", "No"),
    question("I certify that the information I have provided is true and complete.",
             SemanticType.ATTESTATION, "Yes", "No"),
    question("Would you like to receive text message updates about your application?",
             SemanticType.CONSENT, "Yes", "No"),
    question("Data processing", SemanticType.CONSENT, "Yes", "No",
             help_text="By selecting Yes you agree to the processing of your personal data."),
    question("Do you accept?", SemanticType.CONSENT, "Yes", "No", section=("Acknowledgements",)),
    question("Privacy notice", SemanticType.CONSENT, "Yes, I have read it", "No",
             help_text="Have you read our applicant privacy notice?"),
    question("May we contact your current employer?", SemanticType.CONSENT, "Yes", "No"),
    question("Marketing emails", SemanticType.CONSENT, "Yes, I agree", "No"),
    question("Signature", SemanticType.ATTESTATION, "Yes", "No"),
])
def test_consent_and_attestation_wording_keeps_the_explicit_answer(field: ApplicationField) -> None:
    annotated, decision = classify(Jev(), field)
    assert annotated.fields[0].semantic_type is field.semantic_type
    assert decision.demoted_from is None
    assert decision.route is FieldRoute.HUMAN_INPUT
    assert decision.source_requirement == "only explicit scoped user/saved answer; never inferred"


@pytest.mark.parametrize("field", [
    # Not yes/no questions: sensitive types other than a plain yes/no are never downgraded.
    question("Describe a campaign result", SemanticType.ATTESTATION, control=ControlType.TEXTAREA),
    question("I have five or more years of paid media experience", SemanticType.ATTESTATION,
             control=ControlType.CHECKBOX),
    question("How many years of marketing experience do you have?", SemanticType.CONSENT,
             "0-2", "3-5", "6+"),
    question("Which marketing channels have you managed?", SemanticType.CONSENT,
             "Paid social", "Paid search", control=ControlType.CHECKBOX_GROUP),
])
def test_only_a_plain_yes_no_question_is_demoted(field: ApplicationField) -> None:
    annotated, decision = classify(Jev(), field)
    assert annotated.fields[0].semantic_type is field.semantic_type
    assert decision.demoted_from is None and decision.route is FieldRoute.HUMAN_INPUT


def test_consent_criteria_name_their_wording_and_the_prompt_version_is_bumped() -> None:
    provider = Jev()
    classify(provider, question(AGENCY, SemanticType.CUSTOM_SELECT, "Yes", "No"))
    [request] = provider.requests
    assert request["state"]["version"] == "full-form-routing-v13"
    semantic = request["questions"]["s0"]
    for word in ("consent", "agree", "acknowledge", "authorize", "permission", "certify",
                 "declare", "sign"):
        assert word in semantic["instructions"]
    assert "consent, agree, acknowledge, authorize or permit" in semantic["criteria"]["CONSENT"]
    assert "certify, declare, attest, affirm or sign" in semantic["criteria"]["ATTESTATION"]
    for criterion in ("CONSENT", "ATTESTATION", "CUSTOM_BOOLEAN"):
        assert "experience" in semantic["criteria"][criterion]


def test_the_demoted_question_reaches_the_experience_screener_instead_of_an_explicit_hold(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    field = question(AGENCY, SemanticType.CONSENT, "Yes", "No")
    provider = Jev(picks={"experience": "UNKNOWN"})
    r = router(provider)
    annotated = r.annotate(ApplicationForm(url="https://synthetic.test/apply", fields=[field]),
                           document_id="screener")
    ctx = PacketContext(form=annotated, candidate=fictional_candidate, job=mock_job,
        application=Application(id="app-consent", request_id="request-consent", job_id=mock_job.id,
            candidate_id=fictional_candidate.id, state=ApplicationState.INSPECTING, version=1,
            created_at="2026-09-24T00:00:00Z", updated_at="2026-09-24T00:00:00Z"))
    resolver = DynamicPacketResolver(r.decisions, router=r)
    packet = asyncio.run(resolver.resolve(ctx))
    assert ctx.problems(packet) == []
    # The verified facts do not say, so it still holds, but as a yes/no experience
    # question with the fact that would settle it, not as an explicit consent answer.
    assert provider.asked("experience")
    assert [t["stage"] for t in resolver.narrative_traces] == ["experience_screener"]
    [missing] = packet.missing_inputs
    assert missing.reason is MissingReason.NO_ANSWER
    assert missing.prompt.startswith("Confirm whether you have the experience this question asks about")


# --- Jev's own reading must agree: a real consent reads as an explicit answer -------------

def resolve(provider: Jev, field: ApplicationField, candidate: CandidateProfile,
            job: JobRecord) -> tuple[Any, ApplicationForm, DynamicPacketResolver]:
    r = router(provider)
    annotated = r.annotate(ApplicationForm(url="https://synthetic.test/apply", fields=[field]),
                           document_id="consent-e2e")
    ctx = PacketContext(form=annotated, candidate=candidate, job=job, application=Application(
        id="app-consent-e2e", request_id="request-consent-e2e", job_id=job.id,
        candidate_id=candidate.id, state=ApplicationState.INSPECTING, version=1,
        created_at="2026-09-24T00:00:00Z", updated_at="2026-09-24T00:00:00Z"))
    resolver = DynamicPacketResolver(r.decisions, router=r)
    packet = asyncio.run(resolver.resolve(ctx))
    assert ctx.problems(packet) == []
    return packet, annotated, resolver


def test_a_real_consent_without_consent_words_never_takes_a_reworded_saved_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    # "Marketing communications" names no consent act, but Jev reads it as the applicant's
    # own decision (an explicit answer, human input). Demoted, it would become a custom yes/no
    # question that a differently worded GLOBAL saved answer could answer.
    saved = SavedAnswer(id="sa.emails", scope=AnswerScope.GLOBAL, semantic_type=None,
                        question="Would you like to receive marketing emails from us?", value="Yes",
                        confirmed_at="2026-09-02T12:00:00Z")
    candidate = fictional_candidate.model_copy(update={"saved_answers": [
        *fictional_candidate.saved_answers, saved]})
    provider = Jev({"r": "HUMAN_INPUT", "u": "EXPLICIT_ANSWER"}, picks={"wording": "q0"})
    field = question("Marketing communications", SemanticType.CONSENT, "Yes", "No")
    packet, annotated, _ = resolve(provider, field, candidate, mock_job)
    assert annotated.fields[0].semantic_type is SemanticType.CONSENT
    assert packet.answers == [] and not provider.asked("wording")
    assert [m.field_id for m in packet.missing_inputs] == ["answer"]


@pytest.mark.parametrize("scripts", [
    {"u": "EXPLICIT_ANSWER"},  # a decision or consent, not the applicant's own facts
    {"u": ({"HISTORICAL_OR_CONTEXTUAL": 0.90, "EXPLICIT_ANSWER": 0.10}, 0.90)},
    {"u": ({"HISTORICAL_OR_CONTEXTUAL": 0.95, "UNCLEAR": 0.05}, 0.95)},
    {"r": "HUMAN_INPUT"},  # Jev routes it to the applicant
    {"r": "WRITER", "n": "prose"},  # a select never takes prose
])
def test_a_consent_type_stays_unless_jev_reads_one_literal_fact_about_the_applicant(
    scripts: dict[str, Spec],
) -> None:
    # A plain yes/no question without experience wording (an experience question is never
    # a consent, whatever Jev reads: round 5 retry).
    field = question("Would you be open to a contract-to-hire arrangement?", SemanticType.CONSENT,
                     "Yes", "No")
    annotated, decision = classify(Jev(scripts), field)
    assert annotated.fields[0].semantic_type is SemanticType.CONSENT
    assert decision.demoted_from is None and decision.route is FieldRoute.HUMAN_INPUT


def test_current_and_historical_source_readings_pool_for_the_demotion() -> None:
    split = ({"APPLICANT_CURRENT": 0.50, "HISTORICAL_OR_CONTEXTUAL": 0.48, "UNCLEAR": 0.02}, 0.50)
    _, decision = classify(Jev({"u": split}), question(AGENCY, SemanticType.CONSENT, "Yes", "No"))
    assert (decision.semantic_type, decision.demoted_from) == (
        SemanticType.CUSTOM_BOOLEAN, SemanticType.CONSENT)


@pytest.mark.parametrize("label", [
    "Have you managed a newsletter with more than 50,000 subscribers?",
    "Have you reduced unsubscribe rates in email marketing?",
    "Have you built opt-in email campaigns?",
    "Have you received a promotion for your email marketing work?",
    "Have you negotiated an agency agreement with a media partner?",
    "Are you an authorized reseller of Google Ads?",
    "Can you confirm that you have 5+ years of paid search experience?",
    "Do you have GDPR and data privacy experience in marketing?",
])
def test_marketing_topics_are_not_consent_wording(label: str) -> None:
    _, decision = classify(Jev(), question(label, SemanticType.CONSENT, "Yes", "No"))
    assert (decision.semantic_type, decision.demoted_from) == (
        SemanticType.CUSTOM_BOOLEAN, SemanticType.CONSENT)


@pytest.mark.parametrize("label,typed", [
    ("GDPR: May we retain your personal data for future vacancies?", SemanticType.CONSENT),
    ("Have you reviewed the candidate privacy notice?", SemanticType.CONSENT),
    ("Do you allow Brambleway to process your personal data?", SemanticType.CONSENT),
    ("SMS opt-in", SemanticType.CONSENT),
    ("All information provided is accurate and complete.", SemanticType.ATTESTATION),
    ("I have never been convicted of a felony.", SemanticType.ATTESTATION),
])
def test_consent_acts_keep_the_type_even_when_jev_reads_a_literal_fact(
    label: str, typed: SemanticType,
) -> None:
    # The wording check alone keeps these, whatever Jev's source reading.
    _, decision = classify(Jev(), question(label, typed, "Yes", "No"))
    assert (decision.semantic_type, decision.demoted_from) == (typed, None)
