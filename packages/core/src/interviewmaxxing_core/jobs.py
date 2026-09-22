"""Minimal job identity for the supplied-URL flow.

A job starts life keyed only by its normalized application URL. When the browser
observes a stable ATS identity on the page (e.g. Greenhouse board + job id), the
store binds that identity to the job; URLs that turn out to show the same ATS job
are merged into one canonical job. An HTTP redirect is not identity evidence.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from ._base import Contract, NonEmptyStr, UtcDatetime, utc_now


class IdentityEvidenceKind(StrEnum):
    """How an ATS job identity was established. There is deliberately no REDIRECT
    member: landing on a URL after a redirect does not prove which job it is."""

    ATS_JOB_ID_ON_PAGE = "ATS_JOB_ID_ON_PAGE"
    """An ATS job id read from the page DOM, embedded data or the form's own action."""
    STRUCTURED_DATA = "STRUCTURED_DATA"
    """schema.org JobPosting or equivalent structured data on the page."""
    USER_CONFIRMED = "USER_CONFIRMED"
    """The user confirmed the identity."""


def _slug(value: str) -> str:
    return value.strip().lower()


class JobIdentityObservation(Contract):
    """An ATS-scoped job identity observed on the live page."""

    ats_type: NonEmptyStr
    """e.g. ``greenhouse``, ``lever``, ``ashby``, ``generic``."""
    ats_tenant: NonEmptyStr
    """The ATS account the job belongs to, e.g. a Greenhouse board token or a host."""
    external_job_id: NonEmptyStr
    evidence_kind: IdentityEvidenceKind
    evidence: NonEmptyStr
    """What was observed, e.g. ``form action /boards/acme/jobs/4012345``."""
    observed_url: str | None = None
    """The page URL at observation time. Recorded, never bound as an alias."""
    company: str | None = None
    title: str | None = None
    location: str | None = None
    observed_at: UtcDatetime = Field(default_factory=utc_now)

    @property
    def identity_key(self) -> str:
        return f"ats:{_slug(self.ats_type)}:{_slug(self.ats_tenant)}:{_slug(self.external_job_id)}"


class JobRecord(Contract):
    """The store's view of a job."""

    id: NonEmptyStr
    application_url: NonEmptyStr
    """The URL of the request that created this job, as supplied."""
    normalized_url: NonEmptyStr
    identity_key: str | None = None
    """Bound ATS identity (``JobIdentityObservation.identity_key``), if any."""
    ats_type: str | None = None
    external_job_id: str | None = None
    company: str | None = None
    title: str | None = None
    location: str | None = None
    merged_into: str | None = None
    """Set when this job was found to be the same as another canonical job."""
    created_at: UtcDatetime
    updated_at: UtcDatetime
