"""Presentation models for the pipeline, jobs and selection routes.

Field for field the frontend's ``apps/web/lib/pipeline/types.ts`` and
``apps/web/lib/jobs/types.ts`` (dashboard checkpoint 816afa1). They are views over
the canonical D0 contracts and the P1/J1/J2 packages, never a second domain model.
Request bodies are strict: unknown keys are rejected.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr

from .models import Body, ConfirmationAuthority, ConfirmationMethod, View

Choice = Literal["APPLY", "SKIP", "REVIEW"]
Period = Literal["YEAR", "MONTH", "WEEK", "DAY", "HOUR"]
Arrangement = Literal["ONSITE", "HYBRID", "REMOTE", "UNKNOWN"]
Priority = Literal["STRONGLY_PREFER_ONSITE_HYBRID", "BALANCED", "PREFER_REMOTE"]
Tier = Literal["PREFERRED", "EQUAL", "SECONDARY", "UNRANKED"]
LocationTier = Literal[
    "ONSITE_HYBRID_TARGET", "REMOTE_ELIGIBLE", "REMOTE_UNCONFIRMED", "OUTSIDE_TARGET", "UNRESOLVED"
]
SourceState = Literal[
    "QUEUED", "RUNNING", "OK", "PARTIAL", "NEEDS_USER", "BLOCKED", "ERROR", "SKIPPED"
]

# --- pipeline -------------------------------------------------------------------------


class PipelineLaneView(View):
    id: str
    label: str


class PipelineHistoryItem(View):
    at: str
    kind: Literal["created", "imported", "moved", "edited"]
    summary: str
    from_lane: str | None
    to_lane: str | None


class PipelineProvenanceView(View):
    import_id: str
    file_name: str | None
    source_digest: str
    source_row: int
    imported_at: str
    imported_values: dict[str, str]
    """The row as first imported (P1 ``initial``), verbatim, by workbook header."""
    source_id: str
    """Additive: P1's logical source id for this card."""
    latest_imported_values: dict[str, str]
    """Additive: the row as most recently imported (P1 ``latest``)."""
    first_imported_at: str
    """Additive."""
    version_count: int
    """Additive: how many imported versions P1 keeps (``source_versions``)."""


class LinkedApplicationView(View):
    application_id: str
    state: str
    submitted_at: str | None
    confirmation_reference: str | None
    confirmation_method: ConfirmationMethod | None
    """Additive: same strings as ``SubmissionReceiptView.confirmationMethod``; null
    without a receipt."""
    confirmation_authority: ConfirmationAuthority | None
    """Additive: ``user`` exactly when the method is ``USER_CONFIRMED``; null without a
    receipt."""


class LinkedSelectionView(View):
    selection_id: str
    effective_choice: Choice
    decided_at: str


class PipelineEntryView(View):
    id: str
    lane: str
    revision: int
    fields: dict[str, Any]
    """The 23 reference fields under their reference keys; blank is null."""
    application_url: str | None
    listing_id: str | None
    origin: Literal["manual", "import", "jobs"]
    application: LinkedApplicationView | None
    selection: LinkedSelectionView | None
    provenance: PipelineProvenanceView | None
    history: list[PipelineHistoryItem]
    created_at: str
    updated_at: str


class PipelineBoardView(View):
    lanes: list[PipelineLaneView]
    entries: list[PipelineEntryView]


class PipelineEntryInput(Body):
    lane: StrictStr
    fields: dict[str, Any]
    application_url: StrictStr | None
    listing_id: StrictStr | None = None


class PipelineUpdateInput(Body):
    revision: StrictInt
    fields: dict[str, Any] | None = None
    application_url: StrictStr | None = None


class PipelineMoveInput(Body):
    revision: StrictInt
    lane: StrictStr


class ImportRowError(View):
    field: str
    message: str


class ImportPreviewRow(View):
    row_number: int
    action: Literal["create", "update", "unchanged", "error"]
    company: str | None
    role: str | None
    errors: list[ImportRowError]


class ImportCounts(View):
    create: int
    update: int
    unchanged: int
    error: int


class ImportPreviewView(View):
    preview_id: str
    file_name: str
    source_digest: str
    rows: list[ImportPreviewRow]
    counts: ImportCounts


class ImportReceiptView(View):
    import_id: str
    file_name: str
    source_digest: str
    imported_at: str
    created: int
    updated: int
    unchanged: int


class ImportInput(Body):
    format: Literal["csv", "json"]
    file_name: StrictStr
    content: StrictStr
    source_id: StrictStr | None = None
    """Additive: the stable logical source of this upload, chosen by the user (for
    example ``"numbers-pipeline"``). Re-imports of the same tracker must reuse it.
    Required for CSV and for JSON exports that do not declare ``source.sourceId`` or a
    workbook path; never derived from the content digest."""


# --- jobs and selection ------------------------------------------------------------------


class OnsiteTargetView(View):
    location: str
    arrangements: list[Literal["ONSITE", "HYBRID"]]


class RemoteTargetView(View):
    eligible_region: str


class CompensationFloorView(View):
    amount: float
    currency: str
    period: Period


class SearchPreferencesView(View):
    title_phrases: list[str]
    keywords: list[str]
    excluded_keywords: list[str]
    excluded_companies: list[str]
    onsite: list[OnsiteTargetView]
    remote: RemoteTargetView | None
    location_priority: Priority
    """Additive (canonical ``LocationPriority``): how onsite/hybrid targets rank
    against eligible remote roles. Default ``STRONGLY_PREFER_ONSITE_HYBRID``."""
    role_focus: str
    """Additive (canonical ``role_focus``): the semantic role description Jev judges
    responsibilities against. Title phrases are search seeds, not an allowlist."""
    minimum_compensation: CompensationFloorView | None
    unknown_compensation: Literal["KEEP", "REVIEW"]
    sources: list[str]
    max_results_per_source: int
    fingerprint: str | None


class OnsiteTargetInput(Body):
    location: StrictStr
    arrangements: list[Literal["ONSITE", "HYBRID"]]


class RemoteTargetInput(Body):
    eligible_region: StrictStr


class CompensationFloorInput(Body):
    amount: StrictFloat | StrictInt
    currency: StrictStr
    period: Period


class SearchPreferencesInput(Body):
    title_phrases: list[StrictStr]
    keywords: list[StrictStr]
    excluded_keywords: list[StrictStr]
    excluded_companies: list[StrictStr]
    onsite: list[OnsiteTargetInput]
    remote: RemoteTargetInput | None
    location_priority: Priority | None = None
    """Optional; omitted keeps the saved value (default strongly prefer onsite/hybrid)."""
    role_focus: StrictStr | None = None
    """Optional; omitted keeps the saved value (default: D0 ``DEFAULT_ROLE_FOCUS``)."""
    minimum_compensation: CompensationFloorInput | None
    unknown_compensation: Literal["KEEP", "REVIEW"]
    sources: list[StrictStr]
    max_results_per_source: StrictInt = Field(ge=1, le=500)


class SourceResultView(View):
    source: str
    state: SourceState
    result_count: int
    message: str | None
    user_action: str | None
    session_name: str | None
    started_at: str | None
    finished_at: str | None


class SearchRunView(View):
    id: str
    started_at: str
    finished_at: str | None
    results: list[SourceResultView]


class CompensationView(View):
    raw_text: str | None
    minimum: float | None
    maximum: float | None
    currency: str | None
    period: Period | None


class ListingSourceView(View):
    source: str
    source_url: str
    """Where the observation was made; may be a shared search page. Never a link to
    apply or navigate to the job."""
    posting_url: str | None
    """Additive: this posting's own page on the source (canonical ``posting_url``)."""
    application_url: str | None
    observed_at: str


DecisionTaskState = Literal["QUEUED", "RUNNING", "DONE", "FAILED", "INTERRUPTED"]


class DecisionTaskView(View):
    """The latest explicit decision request for this listing (service task record)."""

    id: str
    state: DecisionTaskState
    error: str | None
    """Plain-language reason for FAILED or INTERRUPTED; null otherwise."""
    result_id: str | None
    """For DONE: the ``SelectionView.id`` it produced (may equal an earlier id when the
    decision cache answered). Null otherwise."""
    requested_at: str
    updated_at: str


class PolicyHoldView(View):
    code: str
    detail: str


class ProviderErrorView(View):
    code: str
    message: str
    retryable: StrictBool


class SelectionView(View):
    id: str
    effective_choice: Choice
    model_choice: Choice | None
    probabilities: dict[str, float] | None
    confidence: float | None
    requested_model: str
    returned_model: str | None
    rubric_version: str
    holds: list[PolicyHoldView]
    reasons: list[str]
    unresolved: list[str]
    provider_error: ProviderErrorView | None
    decided_at: str
    stale: bool


class ListingView(View):
    id: str
    title: str
    company: str | None
    location: str | None
    work_arrangement: Arrangement
    remote_eligibility: str | None
    compensation: CompensationView | None
    description: str | None
    description_completeness: Literal["FULL", "PARTIAL", "NONE"]
    status: Literal["OPEN", "CLOSED", "UNKNOWN"]
    posted_text: str | None
    observed_at: str
    provenance: list[ListingSourceView]
    selection: SelectionView | None
    pipeline_entry_id: str | None
    application_id: str | None
    posting_url: str | None
    """Additive: the listing's own posting page (canonical ``posting_url``). Navigate or
    apply with ``applicationUrl`` first, then this; never with a shared ``sourceUrl``."""
    decision_task: DecisionTaskView | None
    """Additive: the latest decision request's lifecycle; null if none was asked."""
    location_tier: LocationTier | None
    """How the stated location relates to the targets (frontend ``LocationTier``, from J2
    ``check_location``); null when the selection package is not installed."""
    priority_tier: Tier | None
    """Additive: rank under ``locationPriority`` (J2 ``LocationTier``). Ordering only,
    never a filter."""
    rank_reason: str | None


class ListingsView(View):
    listings: list[ListingView]
    last_run: SearchRunView | None
