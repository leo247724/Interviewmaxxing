"""Edges of the consent/attestation post-check (WP10 item 3).

Which option sets make a yes/no question, where consent wording is looked for (label, help,
placeholder, section headings, options), which marketing experience questions come out a
custom boolean, the boundaries of the consent-to-custom-boolean fold, the WRITER order and a
provider failure. Fictional wording and scripted providers only.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from interviewmaxxing_browser.ai import AIFormRouter, BoundedDecisions, CallBudget, FormRouteReport
from interviewmaxxing_browser.ai.classification import FieldRoute
from interviewmaxxing_core import (
    ApplicationField,
    ApplicationForm,
    ControlType,
    FieldOption,
    SemanticType,
)
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient

Spec = str | tuple[dict[str, float], float]
DEFAULTS = {"r": "COPY_KNOWN", "n": "literal", "u": "HISTORICAL_OR_CONTEXTUAL", "s": "CUSTOM_BOOLEAN",
            "d": "APPLICATION_ATTACHMENT"}


class Jev:
    """Scripted Jev: ``scripts[kind]`` answers every field's r/n/u/s/d question with a choice
    (all mass, confidence 1.0) or (probabilities, confidence); ``status`` fails every call."""

    def __init__(self, scripts: dict[str, Spec] | None = None, *, status: int = 200) -> None:
        self.scripts, self.status = scripts or {}, status
        self.requests: list[dict[str, Any]] = []

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        self.requests.append(request)
        if self.status != 200:
            return HttpResponse(self.status, {}, b"{}")
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            criteria = list(question["criteria"])
            spec = self.scripts.get(name[0], DEFAULTS[name[0]])
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


def question(label: str, semantic: SemanticType, *options: str | FieldOption,
             control: ControlType = ControlType.SELECT, required: bool = True,
             help_text: str | None = None, placeholder: str | None = None,
             section: tuple[str, ...] = ()) -> ApplicationField:
    return ApplicationField(id="answer", selector="#answer", label=label, semantic_type=semantic,
        control_type=control, required=required, help_text=help_text, placeholder=placeholder,
        section_context=list(section),
        options=[o if isinstance(o, FieldOption) else FieldOption(value=f"v{i}", label=o)
                 for i, o in enumerate(options)] or None)


def decide(field: ApplicationField, provider: Jev | None = None) -> tuple[ApplicationForm, Any]:
    r = router(provider or Jev())
    annotated = r.annotate(ApplicationForm(url="https://synthetic.test/apply", fields=[field]),
                           document_id="edges")
    return annotated, r.report_for(annotated).fields[0]


EXPERIENCE = "Have you worked in a performance marketing agency environment?"


# --- what counts as a yes/no question ---------------------------------------------------

@pytest.mark.parametrize("options", [
    ("Yes - I have", "No, never"),
    ("YES", "no"),
    ("Yes.", "No."),
    ("Yes (I have)", "No - not yet"),
    ("Yes", "No", "Prefer not to say"),
    (FieldOption(value="", label="Select..."), "Yes", "No"),  # a placeholder is not an option
])
@pytest.mark.parametrize("control", [ControlType.SELECT, ControlType.RADIO])
def test_yes_no_option_sets_on_single_choice_controls_are_demoted(
    options: tuple[str | FieldOption, ...], control: ControlType,
) -> None:
    annotated, decision = decide(question(EXPERIENCE, SemanticType.CONSENT, *options, control=control))
    assert annotated.fields[0].semantic_type is SemanticType.CUSTOM_BOOLEAN
    assert decision.demoted_from is SemanticType.CONSENT


@pytest.mark.parametrize("options", [
    ("Yes/No", "Prefer not to say"),  # no separate yes and no options
    ("Sí", "No"),  # localized: not recognised as yes, so kept
    ("Y", "N"),
    ("True", "False"),
    ("Yes, full time", "Yes, part time", "No"),  # two yes options: not one yes/no pair
    ("Yes", FieldOption(value="v1", label="No", disabled=True)),  # a disabled no
])
def test_option_sets_that_are_not_one_yes_no_pair_keep_the_consent_type(
    options: tuple[str | FieldOption, ...],
) -> None:
    annotated, decision = decide(question(EXPERIENCE, SemanticType.CONSENT, *options))
    assert annotated.fields[0].semantic_type is SemanticType.CONSENT
    assert decision.demoted_from is None and decision.route is FieldRoute.HUMAN_INPUT


def test_an_optional_yes_no_question_is_typed_the_same_way() -> None:
    _, decision = decide(question(EXPERIENCE, SemanticType.ATTESTATION, "Yes", "No", required=False))
    assert (decision.semantic_type, decision.demoted_from) == (
        SemanticType.CUSTOM_BOOLEAN, SemanticType.ATTESTATION)


@pytest.mark.parametrize("field", [
    question("Have you worked in an agency?", SemanticType.CONSENT,
             control=ControlType.TEXT),  # a yes/no text box
    question("Have you worked in an agency?", SemanticType.CONSENT, control=ControlType.TYPEAHEAD),
    question("Have you worked in an agency?", SemanticType.CONSENT, "Yes", "No",
             control=ControlType.MULTISELECT),
    question("Have you worked in an agency?", SemanticType.ATTESTATION, "Yes", "No",
             control=ControlType.CHECKBOX_GROUP),
])
def test_other_controls_are_never_demoted(field: ApplicationField) -> None:
    annotated, decision = decide(field, Jev({"s": "CONSENT"}))
    assert annotated.fields[0].semantic_type is field.semantic_type
    assert decision.demoted_from is None and decision.route is FieldRoute.HUMAN_INPUT


# --- where consent wording is found -------------------------------------------------------

@pytest.mark.parametrize("field", [
    question("Do you authorise us to carry out a background check?", SemanticType.CONSENT, "Yes", "No"),
    question("Background check", SemanticType.CONSENT, "Yes", "No",
             section=("Background check authorization",)),
    question("I CONSENT TO BRAMBLEWAY CONTACTING MY REFERENCES", SemanticType.CONSENT, "Yes", "No"),
    question("I understand that any false statement may lead to withdrawal of an offer.",
             SemanticType.ATTESTATION, "Yes", "No"),
    question("I've read the applicant privacy notice", SemanticType.CONSENT, "Yes", "No"),
    question("The answers I gave are true and accurate.", SemanticType.ATTESTATION, "Yes", "No"),
    question("My answers are complete to the best of my knowledge.", SemanticType.ATTESTATION,
             "Yes", "No"),
    question("E-signature", SemanticType.ATTESTATION, "Yes", "No"),
    question("Please sign below to confirm your answers", SemanticType.ATTESTATION, "Yes", "No"),
    question("SMS opt-in", SemanticType.CONSENT, "Yes", "No"),
    question("Would you like to receive SMS reminders about interviews?", SemanticType.CONSENT,
             "Yes", "No"),
    question("May we keep your application on file for 12 months?", SemanticType.CONSENT, "Yes", "No"),
    question("Can recruiters contact you about other roles?", SemanticType.CONSENT, "Yes", "No"),
    question("Pre-employment screening", SemanticType.CONSENT, "Yes", "No",
             help_text="Select Yes if you agree to a drug screen after an offer."),
    question("Talent pool", SemanticType.CONSENT, "Yes", "No", placeholder="Select Yes to opt in"),
    question("Talent pool", SemanticType.CONSENT, "Yes, I consent", "No, I do not consent"),
])
def test_consent_wording_in_any_visible_part_keeps_the_explicit_type(field: ApplicationField) -> None:
    annotated, decision = decide(field)
    assert annotated.fields[0].semantic_type is field.semantic_type
    assert decision.demoted_from is None and decision.route is FieldRoute.HUMAN_INPUT


@pytest.mark.parametrize("label", [
    "Do you have experience with SMS marketing?",
    "Do you have privacy engineering experience?",
    "Do you have experience with GDPR compliance for marketing data?",
    "Have you managed data retention policies for a CRM?",
    "Have you run subscription growth campaigns?",
    "Are you a Google Ads certified professional?",
    "Do you hold a Meta Blueprint certification?",
    "Have you received a HubSpot certification?",
    "Have you read 'Influence' by Robert Cialdini?",
    "Do you understand SQL well enough to write your own queries?",
    "Have you written terms of service copy for a consumer app?",
    "Have you worked in a newsletter-driven media business?",
])
def test_marketing_experience_questions_without_consent_acts_come_out_custom_boolean(label: str) -> None:
    # The pilot's heuristics type such questions CONSENT or ATTESTATION on marketing, privacy
    # or data words; topics are not consent wording.
    _, decision = decide(question(label, SemanticType.CONSENT, "Yes", "No"))
    assert (decision.semantic_type, decision.demoted_from) == (
        SemanticType.CUSTOM_BOOLEAN, SemanticType.CONSENT)
    assert decision.route is FieldRoute.COPY_KNOWN


# --- the consent-to-custom-boolean fold ---------------------------------------------------

@pytest.mark.parametrize("split,demoted", [
    ({"CONSENT": 0.57, "CUSTOM_BOOLEAN": 0.40, "UNKNOWN": 0.03}, SemanticType.CONSENT),  # outside 0.03
    ({"CONSENT": 0.50, "CUSTOM_BOOLEAN": 0.45, "UNKNOWN": 0.03, "CUSTOM_SELECT": 0.02},
     SemanticType.CONSENT),  # pool exactly 0.95
    ({"ATTESTATION": 0.52, "CONSENT": 0.46, "UNKNOWN": 0.02}, SemanticType.ATTESTATION),
    ({"CONSENT": 0.97, "CUSTOM_BOOLEAN": 0.03}, SemanticType.CONSENT),  # unsplit, low confidence
])
def test_the_fold_accepts_pooled_consent_mass_at_its_boundaries(
    split: dict[str, float], demoted: SemanticType,
) -> None:
    _, decision = decide(question(EXPERIENCE, SemanticType.CUSTOM_SELECT, "Yes", "No"),
                         Jev({"s": (split, 0.40)}))
    assert (decision.semantic_type, decision.demoted_from) == (SemanticType.CUSTOM_BOOLEAN, demoted)
    assert decision.semantic_confidence == 0.40


@pytest.mark.parametrize("split,demoted", [
    # Round 6: on an experience question a reading that names nothing and the Yes/No select
    # shape are custom yes/no readings too.
    ({"CONSENT": 0.56, "CUSTOM_BOOLEAN": 0.40, "UNKNOWN": 0.04}, SemanticType.CONSENT),
    ({"CUSTOM_SELECT": 0.50, "CUSTOM_BOOLEAN": 0.30, "CONSENT": 0.20}, None),
])
def test_on_an_experience_question_unknown_and_select_readings_join_the_fold(
    split: dict[str, float], demoted: SemanticType | None,
) -> None:
    annotated, decision = decide(question(EXPERIENCE, SemanticType.CUSTOM_SELECT, "Yes", "No"),
                                 Jev({"s": (split, 0.40)}))
    assert annotated.fields[0].semantic_type is SemanticType.CUSTOM_BOOLEAN
    assert decision.demoted_from is demoted


@pytest.mark.parametrize("label,split", [
    (EXPERIENCE, {"CONSENT": 0.56, "CUSTOM_BOOLEAN": 0.40, "RELOCATION": 0.04}),  # outside 0.04
    (EXPERIENCE, {"CONSENT": 0.60, "RELOCATION": 0.40}),
    # Consent wording: the split is not folded, it stays unknown and holds.
    ("Do you consent to a background check?", {"CONSENT": 0.60, "CUSTOM_BOOLEAN": 0.40}),
])
def test_the_fold_rejects_outside_mass_and_consent_wording(label: str, split: dict[str, float]) -> None:
    annotated, decision = decide(question(label, SemanticType.CUSTOM_SELECT, "Yes", "No"),
                                 Jev({"s": (split, 0.40)}))
    assert annotated.fields[0].semantic_type is SemanticType.UNKNOWN
    assert decision.demoted_from is None


def test_a_confident_custom_boolean_and_a_confident_real_consent_are_left_alone() -> None:
    _, plain = decide(question(EXPERIENCE, SemanticType.CUSTOM_SELECT, "Yes", "No"),
                      Jev({"s": "CUSTOM_BOOLEAN"}))
    assert (plain.semantic_type, plain.demoted_from) == (SemanticType.CUSTOM_BOOLEAN, None)
    _, consent = decide(question("Do you agree to our candidate privacy terms?",
                                 SemanticType.CUSTOM_SELECT, "Yes", "No"), Jev({"s": "CONSENT"}))
    assert (consent.semantic_type, consent.demoted_from) == (SemanticType.CONSENT, None)
    assert consent.route is FieldRoute.HUMAN_INPUT


# --- order with the writer, failures and the recorded field --------------------------------

def test_a_real_consent_routed_to_the_writer_still_needs_an_explicit_answer() -> None:
    _, decision = decide(question("Do you consent to a background check?", SemanticType.CONSENT,
                                  "Yes", "No"), Jev({"r": "WRITER", "n": "prose"}))
    assert decision.semantic_type is SemanticType.CONSENT
    assert decision.route is FieldRoute.HUMAN_INPUT and decision.demoted_from is None


def test_a_provider_failure_never_demotes_the_inspectors_type() -> None:
    annotated, decision = decide(question(EXPERIENCE, SemanticType.CONSENT, "Yes", "No"),
                                 Jev(status=500))
    assert annotated.fields[0].semantic_type is SemanticType.CONSENT
    assert decision.route is FieldRoute.AMBIGUOUS and decision.demoted_from is None


def test_demoted_from_survives_the_report_round_trip_and_the_annotated_lookup() -> None:
    provider = Jev()
    r = router(provider)
    form = ApplicationForm(url="https://synthetic.test/apply",
                           fields=[question(EXPERIENCE, SemanticType.ATTESTATION, "Yes", "No")])
    annotated = r.annotate(form, document_id="round-trip")
    report = r.report_for(annotated)
    assert report is not None and report is r.report_for(form)
    dumped = report.model_dump(mode="json")
    assert dumped["fields"][0]["demoted_from"] == "ATTESTATION"
    assert FormRouteReport.model_validate(dumped) == report
    assert len(provider.requests) == 1  # the annotated form is not classified again
