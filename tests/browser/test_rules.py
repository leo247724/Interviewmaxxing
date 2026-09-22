"""Pure rules: semantic classification, button intent, references and job ids."""

from __future__ import annotations

import pytest

from interviewmaxxing_browser import classify
from interviewmaxxing_browser.signals import (
    ACCEPTANCE,
    ButtonIntent,
    button_intent,
    confirmation_references,
    job_ids,
)
from interviewmaxxing_core import EXPLICIT_ANSWER_REQUIRED, ControlType, SemanticType

C = ControlType
S = SemanticType


@pytest.mark.parametrize(
    ("label", "control", "kwargs", "expected"),
    [
        ("First name", C.TEXT, {}, S.FIRST_NAME),
        ("Family name", C.TEXT, {}, S.LAST_NAME),
        ("Your email", C.TEXT, {"input_type": "email"}, S.EMAIL),
        ("Contact", C.TEXT, {"autocomplete": "section-a tel"}, S.PHONE),
        ("Resume/CV", C.FILE, {}, S.RESUME),
        ("Cover letter", C.FILE, {}, S.COVER_LETTER),
        ("Portfolio", C.FILE, {}, S.UNKNOWN),
        ("LinkedIn profile URL (optional)", C.TEXT, {"input_type": "url"}, S.LINKEDIN),
        ("Personal site", C.TEXT, {"input_type": "url"}, S.WEBSITE),
        ("Are you legally authorized to work in the United States?", C.SELECT, {}, S.WORK_AUTHORIZATION),
        ("Will you now or in the future require visa sponsorship?", C.RADIO, {}, S.SPONSORSHIP),
        ("Desired annual base salary (USD)", C.TEXT, {}, S.SALARY_EXPECTATION),
        ("Gender (voluntary)", C.SELECT, {}, S.EEO_GENDER),
        ("Are you a protected veteran?", C.RADIO, {}, S.EEO_VETERAN_STATUS),
        ("Years of professional experience", C.SELECT, {}, S.YEARS_EXPERIENCE),
        ("How did you hear about us?", C.CHECKBOX_GROUP, {}, S.REFERRAL_SOURCE),
        ("What is your notice period?", C.SELECT, {}, S.CUSTOM_SELECT),
        ("Why do you want to work here?", C.TEXTAREA, {}, S.CUSTOM_LONG_TEXT),
        ("Primary skills", C.MULTISELECT, {}, S.CUSTOM_MULTISELECT),
        ("Preferred office", C.UNSUPPORTED, {}, S.UNKNOWN),
        # Identity from name attributes when the label is terse.
        ("Given", C.TEXT, {"name": "first_name"}, S.FIRST_NAME),
    ],
)
def test_classify(label: str, control: ControlType, kwargs: dict[str, str], expected: SemanticType) -> None:
    assert classify(label=label, control_type=control, **kwargs) is expected


@pytest.mark.parametrize(
    ("label", "help_text", "expected"),
    [
        ("I agree", "I confirm that I have never been dismissed from employment.", S.ATTESTATION),
        ("I agree", "Brambleway may keep my application on file for 12 months.", S.CONSENT),
        ("I certify that the information in this application is true and complete.", "", S.ATTESTATION),
        ("I have read and acknowledge the Applicant Privacy Notice.", "", S.CONSENT),
        ("I am open to relocating to Denver, CO", "", S.RELOCATION),
        ("I am a US citizen", "", S.ATTESTATION),
        ("Subscribe to the newsletter", "", S.CONSENT),
        ("Remember this device", "", S.CUSTOM_BOOLEAN),
        ("I identify as disabled", "", S.EEO_DISABILITY_STATUS),
    ],
)
def test_checkbox_statements_need_explicit_answers(label: str, help_text: str, expected: SemanticType) -> None:
    assert classify(label=label, help_text=help_text, control_type=C.CHECKBOX) is expected


def test_help_text_alone_never_changes_non_checkbox_types() -> None:
    # Instructions in the help text must not turn a free-text question into a
    # different (answerable-from-profile) type.
    got = classify(label="Tell us about yourself", help_text="Enter your email address",
                   control_type=C.TEXTAREA)
    assert got is S.CUSTOM_LONG_TEXT
    assert S.ATTESTATION in EXPLICIT_ANSWER_REQUIRED and S.CONSENT in EXPLICIT_ANSWER_REQUIRED


@pytest.mark.parametrize(
    ("text", "submits", "expected"),
    [
        ("Submit application", True, ButtonIntent.SUBMIT),
        ("Apply", True, ButtonIntent.SUBMIT),
        ("Continue", True, ButtonIntent.NEXT),
        ("Save and continue", True, ButtonIntent.NEXT),
        ("Next step", False, ButtonIntent.NEXT),
        ("Review and submit", True, ButtonIntent.AMBIGUOUS),
        ("Go", True, ButtonIntent.AMBIGUOUS),
        ("", True, ButtonIntent.AMBIGUOUS),
        ("Back", True, ButtonIntent.OTHER),
        ("Check status", True, ButtonIntent.OTHER),
        ("Upload", False, ButtonIntent.OTHER),
        ("Sign in", True, ButtonIntent.OTHER),
        ("Toggle menu", False, ButtonIntent.OTHER),
    ],
)
def test_button_intent(text: str, submits: bool, expected: ButtonIntent) -> None:
    assert button_intent(text, submits_form=submits) is expected


def test_references_and_job_ids_need_digits() -> None:
    text = "Application: Senior Engineer. Confirmation reference: BWA-000123. Reference: none"
    assert confirmation_references(text) == ["BWA-000123"]
    assert job_ids("Brambleway · Job ID BWA-ENG-101 · Req #4012") == ["BWA-ENG-101", "4012"]
    assert job_ids("Job ID pending") == []


@pytest.mark.parametrize(
    ("text", "accepted"),
    [
        ("Application submitted", True),
        ("Thank you for applying!", True),
        ("We have received your application", True),
        ("Application received on 2026-09-22", True),
        ("Thank you!", False),
        ("Something went wrong", False),
        ("We are still processing a recent application from this email address", False),
    ],
)
def test_acceptance_wording(text: str, accepted: bool) -> None:
    assert bool(ACCEPTANCE.search(text)) is accepted
