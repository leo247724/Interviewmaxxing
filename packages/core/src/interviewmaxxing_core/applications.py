"""Application requests, records, the state machine, events and receipts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from ._base import Contract, NonEmptyStr, UtcDatetime
from .artifacts import EvidenceRef
from .execution import ReconciliationMethod, SubmissionOutcome


class ApplicationState(StrEnum):
    REQUESTED = "REQUESTED"
    INSPECTING = "INSPECTING"
    PACKET_READY = "PACKET_READY"
    FILLING = "FILLING"
    NEEDS_INPUT = "NEEDS_INPUT"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    DUPLICATE = "DUPLICATE"
    WITHDRAWN = "WITHDRAWN"


S = ApplicationState

PRE_SUBMISSION_STATES: frozenset[ApplicationState] = frozenset(
    {S.REQUESTED, S.INSPECTING, S.PACKET_READY, S.FILLING, S.NEEDS_INPUT, S.FAILED_RETRYABLE}
)
"""States in which no submit action has been dispatched for the current attempt."""

SUBMISSION_BLOCKING_STATES: frozenset[ApplicationState] = frozenset(
    {S.SUBMITTING, S.SUBMITTED, S.SUBMISSION_UNKNOWN}
)
"""A submit may have reached the site. These can never be retried; SUBMISSION_UNKNOWN
must first be reconciled."""

TERMINAL_STATES: frozenset[ApplicationState] = frozenset(
    {S.SUBMITTED, S.FAILED_PERMANENT, S.DUPLICATE, S.WITHDRAWN}
)

_FAIL = {S.FAILED_RETRYABLE, S.FAILED_PERMANENT}
# Any pre-submission application may be found to duplicate another, or be withdrawn.
_EXIT = {S.DUPLICATE, S.WITHDRAWN}

TRANSITIONS: dict[ApplicationState, frozenset[ApplicationState]] = {
    S.REQUESTED: frozenset({S.INSPECTING, S.NEEDS_INPUT} | _FAIL | _EXIT),
    S.INSPECTING: frozenset({S.PACKET_READY, S.NEEDS_INPUT} | _FAIL | _EXIT),
    S.PACKET_READY: frozenset({S.FILLING, S.INSPECTING, S.NEEDS_INPUT} | _FAIL | _EXIT),
    # FILLING -> INSPECTING is the next page of a multi-step form.
    S.FILLING: frozenset({S.INSPECTING, S.NEEDS_INPUT, S.SUBMITTING} | _FAIL | _EXIT),
    # Resume after the user answers: re-inspect the current page.
    S.NEEDS_INPUT: frozenset({S.INSPECTING, S.FAILED_PERMANENT} | _EXIT),
    # Leaving SUBMITTING needs a SubmissionObservation (or crash recovery).
    S.SUBMITTING: frozenset({S.SUBMITTED, S.SUBMISSION_UNKNOWN, S.FILLING, S.NEEDS_INPUT} | _FAIL),
    # Leaving SUBMISSION_UNKNOWN needs a SubmissionReconciliation.
    S.SUBMISSION_UNKNOWN: frozenset({S.SUBMITTED, S.FAILED_RETRYABLE}),
    S.FAILED_RETRYABLE: frozenset({S.INSPECTING, S.FAILED_PERMANENT} | _EXIT),
    S.SUBMITTED: frozenset(),
    S.FAILED_PERMANENT: frozenset(),
    S.DUPLICATE: frozenset(),
    S.WITHDRAWN: frozenset(),
}

GUARDED_TRANSITIONS: frozenset[tuple[ApplicationState, ApplicationState]] = frozenset(
    (src, dst)
    for src, dsts in TRANSITIONS.items()
    for dst in dsts
    if src in SUBMISSION_BLOCKING_STATES or dst in SUBMISSION_BLOCKING_STATES
)
"""Transitions into or out of a submission state. Only the store's dedicated
submission operations perform these; generic ``transition`` refuses them."""


def can_transition(src: ApplicationState, dst: ApplicationState) -> bool:
    return dst in TRANSITIONS[src]


def event_name_for(state: ApplicationState) -> str:
    """Event type emitted on entering ``state``, e.g. ``application.submitted``."""
    return f"application.{state.value.lower()}"


class RequestDisposition(StrEnum):
    """What recording a request found. Only NEW and RESUMABLE may proceed to work."""

    NEW = "NEW"
    RESUMABLE = "RESUMABLE"
    """An existing application for this candidate/job can continue or retry."""
    ALREADY_SUBMITTED = "ALREADY_SUBMITTED"
    SUBMISSION_IN_PROGRESS = "SUBMISSION_IN_PROGRESS"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    """A previous submit may have reached the site; reconcile before any retry."""
    CLOSED = "CLOSED"
    """Existing application is FAILED_PERMANENT or WITHDRAWN."""


def disposition_for(state: ApplicationState, *, created: bool) -> RequestDisposition:
    if created:
        return RequestDisposition.NEW
    return {
        S.SUBMITTED: RequestDisposition.ALREADY_SUBMITTED,
        S.SUBMITTING: RequestDisposition.SUBMISSION_IN_PROGRESS,
        S.SUBMISSION_UNKNOWN: RequestDisposition.SUBMISSION_UNKNOWN,
        S.FAILED_PERMANENT: RequestDisposition.CLOSED,
        S.WITHDRAWN: RequestDisposition.CLOSED,
        S.DUPLICATE: RequestDisposition.CLOSED,
    }.get(state, RequestDisposition.RESUMABLE)


class ApplicationRequest(Contract):
    id: NonEmptyStr
    candidate_id: NonEmptyStr
    application_url: NonEmptyStr
    """Exactly as the user supplied it; used for navigation."""
    normalized_url: NonEmptyStr
    """Duplicate-detection alias (``urls.normalize_application_url``)."""
    requested_at: UtcDatetime
    selection_source: Literal["USER_PROVIDED"] = "USER_PROVIDED"
    job_id: NonEmptyStr
    application_id: NonEmptyStr


class Application(Contract):
    id: NonEmptyStr
    request_id: NonEmptyStr
    """The request that created the application (later requests are linked to it)."""
    job_id: NonEmptyStr
    candidate_id: NonEmptyStr
    state: ApplicationState
    version: int = Field(ge=1)
    """Incremented on every change; useful for optimistic checks and display."""
    packet_id: str | None = None
    submitted_at: UtcDatetime | None = None
    failure_reason: str | None = None
    duplicate_of: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    claim_owner: str | None = None
    claim_expires_at: UtcDatetime | None = None


class ApplicationEvent(Contract):
    """Durable history entry. Every state transition emits exactly one of these, in
    the same transaction as the state change."""

    id: NonEmptyStr
    sequence: int = Field(ge=1)
    """Global, strictly increasing order of events in the store."""
    application_id: NonEmptyStr
    event: NonEmptyStr
    """e.g. ``application.submitted``, ``job.identity_bound``, ``form.discovered``."""
    timestamp: UtcDatetime
    from_state: ApplicationState | None = None
    to_state: ApplicationState | None = None
    actor: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Claim(Contract):
    """An exclusive, expiring lease to work on one application. Every mutating store
    operation on an application requires the current claim."""

    application_id: NonEmptyStr
    owner: NonEmptyStr
    token: NonEmptyStr
    expires_at: UtcDatetime


class SubmissionAttempt(Contract):
    id: NonEmptyStr
    application_id: NonEmptyStr
    attempt_number: int = Field(ge=1)
    owner: NonEmptyStr
    packet_id: str | None = None
    started_at: UtcDatetime
    """Persisted before the submit action is dispatched."""
    finished_at: UtcDatetime | None = None
    outcome: SubmissionOutcome | Literal["INTERRUPTED"] | None = None
    detail: str | None = None


class Receipt(Contract):
    """Proof of a confirmed submission (ARCHITECTURE.md section 17)."""

    application_id: NonEmptyStr
    candidate_id: NonEmptyStr
    job_id: NonEmptyStr
    application_url: NonEmptyStr
    company: str | None = None
    title: str | None = None
    ats_type: str | None = None
    external_job_id: str | None = None
    submitted_at: UtcDatetime
    """When the accepted submit action was dispatched (attempt start)."""
    confirmed_at: UtcDatetime
    """When acceptance was observed or established."""
    confirmation_reference: str | None = None
    signals: list[str] = Field(default_factory=list)
    reconciliation_method: ReconciliationMethod | None = None
    """Set when acceptance was established after a SUBMISSION_UNKNOWN."""
    evidence: list[EvidenceRef] = Field(default_factory=list)
    attempt_id: NonEmptyStr
