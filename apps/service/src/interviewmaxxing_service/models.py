"""Presentation models, field for field the frontend's ``apps/web/lib/service/types.ts``.

These are views over canonical state, not a second backend model. Every model
serializes with camelCase keys (``dump``) and every nullable field is always present,
matching the TypeScript ``T | null`` members. Request bodies are parsed strictly:
unknown keys are rejected so a typo cannot silently drop data.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr
from pydantic.alias_generators import to_camel

ApplicationStateName = Literal[
    "REQUESTED",
    "INSPECTING",
    "PACKET_READY",
    "FILLING",
    "NEEDS_INPUT",
    "SUBMITTING",
    "SUBMITTED",
    "SUBMISSION_UNKNOWN",
    "FAILED_RETRYABLE",
    "FAILED_PERMANENT",
    "DUPLICATE",
    "WITHDRAWN",
]
EventTone = Literal["info", "progress", "attention", "success", "warning", "error"]
QuestionControl = Literal[
    "text", "long_text", "email", "tel", "url", "number", "single_select", "multi_select", "boolean"
]
InteractionKind = Literal["SIGN_IN", "CAPTCHA", "VERIFICATION"]
EvidenceKindName = Literal["screenshot", "page_text", "page_url", "email", "portal", "user_report"]
ViewAnswerValue = StrictStr | list[StrictStr] | StrictBool | None


class View(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class Body(BaseModel):
    """Strict request body."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=False, extra="forbid")


# --- candidate ------------------------------------------------------------------------


class CandidateProfileInput(Body):
    first_name: StrictStr
    last_name: StrictStr
    email: StrictStr
    phone: StrictStr
    location: StrictStr
    linkedin_url: StrictStr
    website_url: StrictStr


class CandidateProfileView(View):
    first_name: str
    last_name: str
    email: str
    phone: str
    location: str
    linkedin_url: str
    website_url: str


class ResumeDocumentView(View):
    id: str
    file_name: str
    size_bytes: int
    uploaded_at: str


class CandidateView(View):
    profile: CandidateProfileView
    resumes: list[ResumeDocumentView]
    default_resume_id: str | None


class StartApplicationInput(Body):
    application_url: StrictStr
    profile: CandidateProfileInput
    resume_id: StrictStr
    pipeline_entry_id: StrictStr | None = None
    listing_id: StrictStr | None = None


# --- application ----------------------------------------------------------------------


class JobIdentityView(View):
    title: str | None
    company: str | None
    ats: str | None


class ApplicationEventView(View):
    id: str
    type: str
    at: str
    message: str
    tone: EventTone


class QuestionOption(View):
    value: str
    label: str


class RequiredQuestionView(View):
    id: str
    label: str
    help: str | None
    control: QuestionControl
    required: bool
    options: list[QuestionOption] | None
    value: str | list[str] | bool | None
    max_length: int | None
    reason: str | None


class AttestationView(View):
    id: str
    statement: str
    required: bool
    accepted: bool


class QuestionsNeed(View):
    kind: Literal["questions"] = "questions"
    questions: list[RequiredQuestionView]
    attestations: list[AttestationView]
    saved_at: str | None
    errors: dict[str, str]


class InteractionNeed(View):
    kind: Literal["interaction"] = "interaction"
    interaction: InteractionKind
    instructions: str
    page_url: str | None


class EvidenceView(View):
    kind: EvidenceKindName
    label: str
    value: str | None
    href: str | None
    observed_at: str
    source: Literal["site", "user"]


ConfirmationAuthority = Literal["site", "user"]
ConfirmationMethod = Literal[
    "SUBMISSION_OBSERVED",
    "SITE_CONFIRMATION",
    "ATS_CANDIDATE_PORTAL",
    "CONFIRMATION_EMAIL",
    "USER_CONFIRMED",
]


class SubmissionReceiptView(View):
    receipt_id: str
    submitted_at: str
    confirmation_reference: str | None
    evidence: list[EvidenceView]
    confirmation_method: ConfirmationMethod
    """How acceptance was established: observed right after submit, or the
    ``ReconciliationMethod`` that settled an uncertain submission."""
    confirmation_authority: ConfirmationAuthority
    """Who established it. ``user`` exactly when the method is ``USER_CONFIRMED``, even
    if site artifacts (screenshots of the uncertain page) are listed as evidence."""


class PriorSubmissionView(View):
    application_id: str
    application_url: str
    submitted_at: str
    confirmation_reference: str | None


class FailureView(View):
    reason: str
    detail: str | None
    retryable: bool
    evidence: list[EvidenceView]


class UncertainSubmissionView(View):
    attempted_at: str
    reason: str
    evidence: list[EvidenceView]
    last_checked_at: str | None
    last_check_result: str | None


class ProgressView(View):
    page: int
    page_count: int | None


class ApplicationView(View):
    id: str
    state: ApplicationStateName
    application_url: str
    job: JobIdentityView
    requested_at: str
    updated_at: str
    progress: ProgressView | None
    resume_file_name: str | None
    needs: QuestionsNeed | InteractionNeed | None
    receipt: SubmissionReceiptView | None
    prior: PriorSubmissionView | None
    failure: FailureView | None
    uncertain: UncertainSubmissionView | None
    events: list[ApplicationEventView]


# --- request bodies for application actions --------------------------------------------

ReuseChoice = Literal["application", "job", "global"]


class AnswerInput(Body):
    answers: dict[str, ViewAnswerValue]
    attestations: dict[str, StrictBool]
    reuse: dict[str, ReuseChoice] = Field(default_factory=dict)
    """Optional service extension: per question id, how far the user allowed reuse.
    Omitted questions stay local to this application."""


class EmptyBody(Body):
    pass


class RecheckInput(Body):
    kind: Literal["recheck"]


class UserFoundConfirmationInput(Body):
    kind: Literal["user_found_confirmation"]
    found_in: Literal["email", "portal", "other"]
    reference: StrictStr | None
    note: StrictStr | None


class UserConfirmedNotReceivedInput(Body):
    kind: Literal["user_confirmed_not_received"]


ReconcileInput = Annotated[
    RecheckInput | UserFoundConfirmationInput | UserConfirmedNotReceivedInput,
    Field(discriminator="kind"),
]


class ReconcileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: ReconcileInput
