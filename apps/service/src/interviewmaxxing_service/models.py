"""Presentation models, field for field the frontend's ``apps/web/lib/service/types.ts``.

These are views over canonical state, not a second backend model. Every model
serializes with camelCase keys (``dump``) and every nullable field is always present,
matching the TypeScript ``T | null`` members. Request bodies are parsed strictly:
unknown keys are rejected so a typo cannot silently drop data.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator
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
ReviewControl = Literal["text", "long_text", "single_select", "multi_select", "boolean", "file"]
ReviewSource = Literal["identity", "saved_answer", "fact", "user", "generated", "resume"]

PRESENTATION_VERSION = "2"
"""Version of this presentation contract, reported by ``/healthz`` as
``presentationVersion``. An absent value is version 1 (before prepared reviews: no
``preparation``/``review``, no ``lookup`` flag, no ``GET /applications``)."""


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
    lookup: bool = False
    """A site lookup (``TYPEAHEAD``): ``options`` are the site's suggestions for what was
    typed (value equals label), and any other text is accepted too and typed into the
    site's search box verbatim."""


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


class PreparationView(View):
    """A prepared application: the form was filled and the run stopped at the final
    review step without submitting (a ``preparation.ready`` stop)."""

    ready: Literal[True] = True
    form_step: int | None
    """0-based index of the final form step, as recorded (page ``form_step + 1``)."""
    form_url: str | None
    captcha_pending: bool
    prepared_at: str
    submitted: Literal[False] = False
    evidence: list[EvidenceView]
    """Evidence recorded by the preparing run (the filled review page)."""


class ReviewAnswerView(View):
    """One answer the service filled in, for review. Never carries internal ids."""

    question: str
    wording_recorded: bool
    """``question`` is recorded wording for this question (the site's own, or the
    question the saved answer was saved for); otherwise it is a plain name for the kind
    of question, because the form's wording was not recorded."""
    page: int
    control: ReviewControl
    value: str | list[str]
    source: ReviewSource
    confidence: float


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
    preparation: PreparationView | None
    review: list[ReviewAnswerView]


class ApplicationSummaryView(View):
    """One application in ``GET /applications``."""

    id: str
    state: ApplicationStateName
    application_url: str
    job: JobIdentityView
    requested_at: str
    updated_at: str
    preparation: PreparationView | None
    pipeline_entry_ids: list[str]
    """Pipeline cards linked to this application, plus unlinked cards whose application
    URL the store resolves to it. For finding prepared cards only; never a link."""


class ApplicationListView(View):
    applications: list[ApplicationSummaryView]


# --- the review lane: prepared queue, review page, approve and submit -------------------

ReuseChoice = Literal["application", "job", "global"]
ReviewStage = Literal["prepared", "browser_action", "other"]
"""Where an application stands for the review lane: ``prepared`` (stopped at the final
review step, nothing submitted), ``browser_action`` (held only by a sign-in, a CAPTCHA
or another step the person does in the browser) or ``other`` (anything else)."""
HoldKind = Literal[
    "ready", "approved", "edited", "questions", "sign_in", "captcha", "browser_action"
]
ProvenanceKind = Literal[
    "identity",
    "resume",
    "saved_answer",
    "saved_policy",
    "derived",
    "fact_screener",
    "narrative",
    "user",
    "blank",
]


class ProviderCostView(View):
    """AI provider usage recorded for an application (every ``provider.budget`` event)."""

    known_usd: float
    calls: int
    unknown_cost_calls: int
    """Calls whose cost the provider did not report (not in ``known_usd``)."""


class HoldView(View):
    kind: HoldKind
    summary: str
    """One plain line: what the application waits for."""


class ReviewQueueItemView(View):
    """One application in the Prepared queue (``GET /review``)."""

    id: str
    state: ApplicationStateName
    stage: ReviewStage
    application_url: str
    job: JobIdentityView
    prepared_at: str | None
    """When the preparation behind a ``prepared`` stop was recorded."""
    stopped_at: str
    """When the current stop was recorded (the queue's order, newest first)."""
    captcha_pending: bool
    provider_cost: ProviderCostView | None
    hold: HoldView
    approved: bool
    """A valid approval of this preparation exists (``submission_approval``)."""


class ReviewQueueView(View):
    applications: list[ReviewQueueItemView]


class ReviewProvenanceView(View):
    kind: ProvenanceKind
    label: str
    detail: str | None
    """The resolver's own note (what it derived the answer from), when it recorded one."""


class ReviewCitationsView(View):
    facts: list[str]
    """Candidate fact ids the answer cites."""
    passages: list[str]
    """Story passage (chunk) ids a narrative cites."""
    job_evidence: list[str]
    """Job-description evidence ids a narrative cites (context, not candidate facts)."""


class ReviewEditView(View):
    """How the answer can be changed through ``POST /applications/{id}/answers``: the
    answer goes under ``question_id`` (in ``attestations`` when ``attestation``), with
    one of ``reuse`` as its ``reuse`` scope; then ``resume`` prepares it again."""

    control: QuestionControl
    options: list[QuestionOption] | None
    lookup: bool
    attestation: bool
    required: bool
    value: str | list[str] | bool | None
    """The current answer in the form's own terms (option values, a boolean, text)."""
    reuse: list[ReuseChoice]
    note: str | None


class ReviewRowView(View):
    """One question of the prepared form, in form order: its answer, or a blank."""

    question_id: str | None
    """The question's id for an edit; None when it can't be edited here."""
    question: str
    wording_recorded: bool
    page: int
    control: ReviewControl
    value: str | list[str] | None
    """What the form holds: text, option label(s), "Yes"/"No" or a file name; None when
    the field was left blank."""
    required: bool | None
    provenance: ReviewProvenanceView
    citations: ReviewCitationsView | None
    confidence: float | None
    edit: ReviewEditView | None
    no_edit_reason: str | None
    """Why the answer can't be changed here, when ``edit`` is None."""


class ApprovalView(View):
    packet_id: str
    approved_at: str
    approver: str
    pages: int
    """Form pages the approval pins (one packet each)."""


class SubmitReadinessView(View):
    allowed: bool
    """Everything but the person's confirmation is in place."""
    problems: list[str]
    """What is missing, in plain words; empty when ``allowed``."""
    enabled: bool
    """The service was started with ``IMX_ALLOW_SUBMISSION=1``."""
    opens_browser: bool
    """The submission run opens a visible browser window (the person can act in it)."""
    command: str
    """The command-line equivalent."""


class BrowserActionView(View):
    available: bool
    """``POST /applications/{id}/resume`` opens a visible browser window for this
    application now (the ``resume --act`` equivalent)."""
    reason: str | None
    command: str
    """``interviewmaxxing resume APP --act``, to run in a terminal instead."""


class ApplicationReviewView(View):
    """``GET /applications/{id}/review``: the desk's view plus what the review lane needs."""

    application: ApplicationView
    stage: ReviewStage
    prepared_packet_id: str | None
    """The packet an approval pins now; None unless stopped at a completed preparation."""
    approval: ApprovalView | None
    changed_since_preparation: bool
    """Answers were saved after this preparation: prepare it again before approving."""
    provider_cost: ProviderCostView | None
    answers: list[ReviewRowView]
    edit_note: str | None
    """Why answers can't be edited at all (for example an older preparation), or None."""
    submit: SubmitReadinessView
    browser: BrowserActionView


class ApproveInput(Body):
    packet_id: StrictStr
    """The packet the person reviewed (``preparedPacketId``); anything else is refused."""


class SubmitInput(Body):
    packet_id: StrictStr
    """The approved packet the person confirmed (``approval.packetId``)."""
    confirm: StrictBool
    """The person's explicit confirmation in the dashboard: exactly JSON ``true``."""

    @field_validator("confirm")
    @classmethod
    def _confirmed(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("the person must confirm the submission")
        return value


# --- request bodies for application actions --------------------------------------------


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
