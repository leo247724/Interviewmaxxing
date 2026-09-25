"""What an inspected page needs from the user, independent of candidate data.

* Sign-in and CAPTCHA pages need the user to act in the browser: ``USER_ACTION``.
* Required ``UNSUPPORTED`` controls (custom widgets) need the user to operate them:
  ``UNSUPPORTED_CONTROL``. Once operated, the inspector reports them as no longer
  required, and ``wait_for_user`` returns.
* Consent and attestation fields are listed by :func:`attestation_fields`; they are
  ``EXPLICIT_ANSWER_REQUIRED`` types, so a packet may only answer them from a saved
  answer or user input (an authorized attestation) and otherwise must report
  ``UNCOVERED_ATTESTATION``.
"""

from __future__ import annotations

from interviewmaxxing_core import (
    USER_ACTION_PAGES,
    ApplicationField,
    ApplicationForm,
    ControlType,
    MissingInput,
    MissingReason,
    PageInspection,
    PageKind,
    SemanticType,
)

from .signals import CONSENT_GATE_ACTION

ATTESTATION_TYPES = frozenset({SemanticType.ATTESTATION, SemanticType.CONSENT})


def consent_gate(inspection: PageInspection) -> bool:
    """Whether the page is a data-processing consent page in front of the application form
    (Jobvite's "Data Consent"), reported as ``SIGN_IN_REQUIRED``. Round 14: the runner
    accepts it only when the person's own statement covers it (``accept_data_consent``)."""
    return (inspection.kind is PageKind.SIGN_IN_REQUIRED
            and CONSENT_GATE_ACTION in (inspection.message or ""))


def user_action_needs(inspection: PageInspection) -> list[MissingInput]:
    """A ``USER_ACTION`` item for a sign-in or CAPTCHA page, else nothing. A data-processing
    consent page in front of the form is reported as ``SIGN_IN_REQUIRED`` (the user passes
    it the same way); its item says to accept the consent."""
    if inspection.kind not in USER_ACTION_PAGES:
        return []
    if consent_gate(inspection):
        what = "Accept the data-processing consent"
    else:
        what = "Sign in" if inspection.kind is PageKind.SIGN_IN_REQUIRED else "Solve the CAPTCHA"
    return [
        MissingInput(
            field_id=None,
            label=what,
            reason=MissingReason.USER_ACTION,
            prompt=inspection.message or f"{what} in the browser window, then continue.",
        )
    ]


def unsupported_control_needs(form: ApplicationForm) -> list[MissingInput]:
    """``UNSUPPORTED_CONTROL`` items for required controls the runtime cannot operate."""
    return [
        MissingInput.for_field(
            form,
            f,
            reason=MissingReason.UNSUPPORTED_CONTROL,
            prompt=f"Please set {f.label!r} yourself in the browser window.",
        )
        for f in form.fields
        if f.control_type is ControlType.UNSUPPORTED and f.required
    ]


def attestation_fields(form: ApplicationForm) -> list[ApplicationField]:
    """Consent and personal-attestation questions on this step."""
    return [f for f in form.fields if f.semantic_type in ATTESTATION_TYPES]
