"""Results returned by the browser runtime: inspection, fill, navigation, submission.

The browser reports what it observed; it never decides that an application was
submitted. Only a ``SubmissionObservation`` with outcome ``ACCEPTED`` and at least
one acceptance signal lets the store mark an application ``SUBMITTED``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from ._base import Contract, NonEmptyStr, UtcDatetime, utc_now
from .artifacts import EvidenceRef
from .forms import ApplicationForm
from .jobs import JobIdentityObservation


class PageKind(StrEnum):
    APPLICATION_FORM = "APPLICATION_FORM"
    JOB_DESCRIPTION = "JOB_DESCRIPTION"
    """A posting page whose apply button must be followed to reach the form."""
    SIGN_IN_REQUIRED = "SIGN_IN_REQUIRED"
    CAPTCHA = "CAPTCHA"
    CONFIRMATION = "CONFIRMATION"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    """The site itself says the candidate already applied."""
    JOB_CLOSED = "JOB_CLOSED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


USER_ACTION_PAGES: frozenset[PageKind] = frozenset({PageKind.SIGN_IN_REQUIRED, PageKind.CAPTCHA})


class PageInspection(Contract):
    """What the browser found on the current page."""

    kind: PageKind
    observed_url: NonEmptyStr
    form: ApplicationForm | None = None
    job_identity: JobIdentityObservation | None = None
    """Set only when the page itself shows a stable ATS identity."""
    message: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)
    inspected_at: UtcDatetime = Field(default_factory=utc_now)
    captcha_pending: bool = False
    """An ``APPLICATION_FORM`` carries an embedded CAPTCHA widget (badge, checkbox or
    token field) that is not solved yet. The form can be filled and prepared; the
    site's own submit needs the user to solve the CAPTCHA first."""

    @model_validator(mode="after")
    def _form_iff_form_page(self) -> Self:
        if (self.kind is PageKind.APPLICATION_FORM) != (self.form is not None):
            raise ValueError("form must be present exactly when kind is APPLICATION_FORM")
        return self


class FieldFillStatus(StrEnum):
    FILLED = "FILLED"
    SKIPPED = "SKIPPED"
    """No answer was supplied (optional field)."""
    FAILED = "FAILED"
    VERIFICATION_MISMATCH = "VERIFICATION_MISMATCH"
    """The control was operated but reads back a different value."""


class FieldFillResult(Contract):
    field_id: NonEmptyStr
    status: FieldFillStatus
    detail: str | None = None


class FillResult(Contract):
    form_step: int = Field(ge=0)
    fields: list[FieldFillResult]
    page_errors: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        bad = {FieldFillStatus.FAILED, FieldFillStatus.VERIFICATION_MISMATCH}
        return not self.page_errors and not any(f.status in bad for f in self.fields)

    def failed_field_ids(self) -> list[str]:
        return [f.field_id for f in self.fields if f.status is not FieldFillStatus.FILLED
                and f.status is not FieldFillStatus.SKIPPED]


class NavigationResult(Contract):
    """Result of moving to the next (non-final) step. Never used for submission."""

    advanced: bool
    inspection: PageInspection
    validation_errors: list[str] = Field(default_factory=list)


class SubmitActionResult(Contract):
    """Whether the final submit action was dispatched. Says nothing about acceptance."""

    dispatched: bool
    """False only when the browser is certain the site received nothing (e.g. the
    submit control was absent or disabled and nothing was clicked)."""
    dispatched_at: UtcDatetime | None = None
    detail: str | None = None


class SubmissionOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    """The site acknowledged the application (confirmation page, message, reference)."""
    NOT_SUBMITTED = "NOT_SUBMITTED"
    """Definitely not received: e.g. the form is still shown with validation errors,
    or nothing was dispatched."""
    UNKNOWN = "UNKNOWN"
    """May have been received; confirmation not observed (timeout, crash, odd page)."""


class NotSubmittedNext(StrEnum):
    """Where a definitely-not-submitted application goes next."""

    FILLING = "FILLING"
    NEEDS_INPUT = "NEEDS_INPUT"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"


class SubmissionObservation(Contract):
    outcome: SubmissionOutcome
    signals: list[str] = Field(default_factory=list)
    """Concrete observations, e.g. ``heading 'Application submitted'``, ``url /thanks``."""
    confirmation_reference: str | None = None
    observed_url: str | None = None
    validation_errors: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    next_state: NotSubmittedNext | None = None
    """Required for NOT_SUBMITTED; forbidden otherwise."""
    detail: str | None = None
    observed_at: UtcDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _evidence_for_definite_outcomes(self) -> Self:
        if self.outcome is SubmissionOutcome.ACCEPTED and not self.signals:
            raise ValueError("ACCEPTED requires at least one observed acceptance signal")
        if self.outcome is SubmissionOutcome.NOT_SUBMITTED:
            if not (self.signals or self.validation_errors):
                raise ValueError("NOT_SUBMITTED requires the observation proving it")
            if self.next_state is None:
                raise ValueError("NOT_SUBMITTED requires next_state")
        elif self.next_state is not None:
            raise ValueError("next_state is only meaningful for NOT_SUBMITTED")
        return self


class ReconciliationMethod(StrEnum):
    SITE_CONFIRMATION = "SITE_CONFIRMATION"
    ATS_CANDIDATE_PORTAL = "ATS_CANDIDATE_PORTAL"
    CONFIRMATION_EMAIL = "CONFIRMATION_EMAIL"
    USER_CONFIRMED = "USER_CONFIRMED"


class SubmissionReconciliation(Contract):
    """Resolves a ``SUBMISSION_UNKNOWN`` application after the fact."""

    outcome: SubmissionOutcome
    method: ReconciliationMethod
    detail: NonEmptyStr
    """What established the outcome, e.g. ``confirmation email received 14:02``."""
    confirmation_reference: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)
    reconciled_at: UtcDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _definite(self) -> Self:
        if self.outcome is SubmissionOutcome.UNKNOWN:
            raise ValueError("reconciliation must establish ACCEPTED or NOT_SUBMITTED")
        return self
