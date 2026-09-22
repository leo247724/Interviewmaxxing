"""Job discovery, Jev selection and pipeline tracking contracts (D0).

These are additive and separate from the application flow:

* A ``JobListing`` is an *observed* posting from a job source. It is not a
  ``JobRecord`` (the submission identity the store binds when the user applies).
* A ``JobSelection`` is an auditable APPLY/SKIP/REVIEW decision. It is not a
  submission, not a receipt, and not an interview probability. An effective APPLY
  only makes a listing eligible to enter the existing application flow.
* A ``PipelineEntry`` is the user's own tracker row with a free-form stage. It is
  not an ``ApplicationState`` and never changes one.

Only observed data is recorded. Anything a source did not show stays ``None`` or
``UNKNOWN``; nothing is inferred from a title.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_core import to_jsonable_python

from ._base import Confidence, Contract, NonEmptyStr, UtcDatetime, new_id, utc_now
from .urls import InvalidApplicationUrl, normalize_application_url


def _lower(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


def _upper(value: Any) -> Any:
    return value.strip().upper() if isinstance(value, str) else value


SourceName = Annotated[
    str, BeforeValidator(_lower), StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]*$")
]
"""Lower-case source slug, e.g. ``linkedin``, ``builtin``, ``indeed``, ``google``."""

CurrencyCode = Annotated[str, BeforeValidator(_upper), StringConstraints(pattern=r"^[A-Z]{3}$")]
"""ISO 4217 code, e.g. ``USD``."""

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

KNOWN_SOURCES: tuple[str, ...] = ("linkedin", "builtin", "indeed", "google")
DEFAULT_TITLE_PHRASES: tuple[str, ...] = ("marketing manager", "marketing director")
DEFAULT_ONSITE_LOCATION = "Austin, TX"
DEFAULT_REMOTE_REGION = "United States"
DEFAULT_JEV_MODEL = "typesafe/jev-1.13"


def snapshot_hash(data: BaseModel | Mapping[str, Any] | list[Any]) -> str:
    """SHA-256 of canonical JSON (sorted keys, no whitespace). Use it for the job and
    candidate evidence actually sent to a decision, so a cached decision can be
    matched to identical inputs and invalidated when they change."""
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json")
    raw = json.dumps(to_jsonable_python(data), sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


# --- shared search/preference parts ---------------------------------------------------


class WorkArrangement(StrEnum):
    ONSITE = "ONSITE"
    HYBRID = "HYBRID"
    REMOTE = "REMOTE"
    UNKNOWN = "UNKNOWN"
    """The source did not say. Never guessed from the title or location."""


class LocationPriority(StrEnum):
    STRONGLY_PREFER_ONSITE_HYBRID = "STRONGLY_PREFER_ONSITE_HYBRID"
    """Matching onsite/hybrid targets rank well above eligible remote roles."""
    BALANCED = "BALANCED"
    PREFER_REMOTE = "PREFER_REMOTE"


class CompensationPeriod(StrEnum):
    YEAR = "YEAR"
    MONTH = "MONTH"
    WEEK = "WEEK"
    DAY = "DAY"
    HOUR = "HOUR"


class OnsiteTarget(Contract):
    """A place the candidate will commute to, for onsite and/or hybrid roles."""

    location: NonEmptyStr
    """E.g. ``"Austin, TX"``."""
    arrangements: list[WorkArrangement] = Field(
        default_factory=lambda: [WorkArrangement.ONSITE, WorkArrangement.HYBRID]
    )
    radius_miles: int | None = Field(default=None, ge=1)

    @field_validator("arrangements")
    @classmethod
    def _onsite_or_hybrid(cls, value: list[WorkArrangement]) -> list[WorkArrangement]:
        allowed = {WorkArrangement.ONSITE, WorkArrangement.HYBRID}
        if not value or not set(value) <= allowed or len(set(value)) != len(value):
            raise ValueError("onsite targets take a non-empty, unique subset of ONSITE, HYBRID")
        return value


class RemoteTarget(Contract):
    """Remote roles the candidate is eligible for. ``eligible_region`` is where the
    worker may live (e.g. ``"United States"``), unrelated to any onsite location:
    US-wide remote is not "remote in Texas"."""

    eligible_region: NonEmptyStr


class CompensationFloor(Contract):
    """The minimum acceptable pay, e.g. 100000 USD per YEAR."""

    amount: float = Field(gt=0)
    currency: CurrencyCode
    period: CompensationPeriod

    @field_validator("amount")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("amount must be finite")
        return value


def _default_onsite() -> list[OnsiteTarget]:
    return [OnsiteTarget(location=DEFAULT_ONSITE_LOCATION)]


def _default_remote() -> RemoteTarget:
    return RemoteTarget(eligible_region=DEFAULT_REMOTE_REGION)


def _default_floor() -> CompensationFloor:
    return CompensationFloor(amount=100_000, currency="USD", period=CompensationPeriod.YEAR)


def _clean_phrases(values: list[str]) -> list[str]:
    cleaned = [" ".join(v.split()) for v in values]
    return [v for v in dict.fromkeys(cleaned) if v]


# --- search query and per-source results -----------------------------------------------


class JobSearchQuery(Contract):
    """What to search for. Every field is user-editable; defaults are the user's
    stated targets (both titles, Austin onsite/hybrid, US-wide remote, USD 100k/yr)."""

    id: NonEmptyStr = Field(default_factory=lambda: new_id("qry"))
    title_phrases: list[str] = Field(default_factory=lambda: list(DEFAULT_TITLE_PHRASES))
    keywords: list[str] = Field(default_factory=list)
    """Extra terms to include (custom keywords)."""
    excluded_keywords: list[str] = Field(default_factory=list)
    onsite: list[OnsiteTarget] = Field(default_factory=_default_onsite)
    remote: RemoteTarget | None = Field(default_factory=_default_remote)
    location_priority: LocationPriority = LocationPriority.STRONGLY_PREFER_ONSITE_HYBRID
    minimum_compensation: CompensationFloor | None = Field(default_factory=_default_floor)
    """Used to rank/filter where a source supports it. Listings without comparable
    pay are kept (compensation unknown), never dropped for lacking a salary."""
    sources: list[SourceName] = Field(default_factory=lambda: list(KNOWN_SOURCES))
    max_results_per_source: int = Field(default=50, ge=1, le=500)
    posted_within_days: int | None = Field(default=None, ge=1)
    created_at: UtcDatetime = Field(default_factory=utc_now)

    @field_validator("title_phrases", "keywords", "excluded_keywords")
    @classmethod
    def _phrases(cls, value: list[str]) -> list[str]:
        return _clean_phrases(value)

    @model_validator(mode="after")
    def _searchable(self) -> Self:
        if not self.title_phrases:
            raise ValueError("at least one title phrase is required")
        if not self.onsite and self.remote is None:
            raise ValueError("search needs an onsite target, a remote target, or both")
        if not self.sources or len(set(self.sources)) != len(self.sources):
            raise ValueError("sources must be a non-empty list without repeats")
        return self

    @classmethod
    def from_preferences(cls, preferences: SelectionPreferences, **overrides: Any) -> JobSearchQuery:
        base = {
            "title_phrases": preferences.target_titles,
            "excluded_keywords": preferences.excluded_keywords,
            "onsite": preferences.onsite,
            "remote": preferences.remote,
            "location_priority": preferences.location_priority,
            "minimum_compensation": preferences.minimum_compensation,
        }
        return cls(**{**base, **overrides})


class SourceSearchState(StrEnum):
    OK = "OK"
    """Searched successfully; ``result_count`` may legitimately be 0."""
    PARTIAL = "PARTIAL"
    """Some results were collected, then the search stopped (limit, pagination, error)."""
    NEEDS_USER = "NEEDS_USER"
    """Sign-in, a challenge or consent is required; the user must act in ``session_name``."""
    BLOCKED = "BLOCKED"
    """The source denied access. Not an empty result."""
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"
    """Not attempted (disabled or unsupported for this query)."""


class SourceSearchResult(Contract):
    """The outcome of one source for one query. A blocked source is never a silent
    empty success: only OK means "searched, and this is everything found"."""

    query_id: NonEmptyStr
    source: SourceName
    state: SourceSearchState
    listing_ids: list[str] = Field(default_factory=list)
    pages_visited: int = Field(default=0, ge=0)
    message: str | None = None
    """What happened, in plain language (required for every state except OK)."""
    user_action: str | None = None
    """For NEEDS_USER: what the user must do, e.g. "Sign in to LinkedIn"."""
    session_name: str | None = None
    """Browser session the user should use to resolve it, e.g. ``imx-jobs-linkedin``."""
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None

    @property
    def result_count(self) -> int:
        return len(self.listing_ids)

    @model_validator(mode="after")
    def _explained(self) -> Self:
        if self.state is not SourceSearchState.OK and not (self.message or "").strip():
            raise ValueError(f"{self.state} results need a message")
        if self.state is SourceSearchState.NEEDS_USER and not (self.user_action or "").strip():
            raise ValueError("NEEDS_USER results need user_action")
        if self.state in (SourceSearchState.BLOCKED, SourceSearchState.NEEDS_USER,
                          SourceSearchState.SKIPPED) and self.listing_ids:
            raise ValueError(f"{self.state} results cannot carry listings; use PARTIAL")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        return self


class JobSearchRun(Contract):
    id: NonEmptyStr = Field(default_factory=lambda: new_id("run"))
    query: JobSearchQuery
    results: list[SourceSearchResult] = Field(default_factory=list)
    started_at: UtcDatetime = Field(default_factory=utc_now)
    finished_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _results_match_query(self) -> Self:
        for result in self.results:
            if result.query_id != self.query.id:
                raise ValueError("source result belongs to another query")
            if result.source not in self.query.sources:
                raise ValueError(f"source {result.source!r} was not in the query")
        return self


# --- observed listings -----------------------------------------------------------------


class ListingStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    """The source says it no longer accepts applications. Never recommended as open."""
    UNKNOWN = "UNKNOWN"


class DescriptionCompleteness(StrEnum):
    FULL = "FULL"
    """The full description from the detail page."""
    PARTIAL = "PARTIAL"
    """A snippet or truncated text (e.g. a search card)."""
    NONE = "NONE"


class Compensation(Contract):
    """Pay as stated. ``raw_text`` is verbatim. Bounds are set only when the source
    states them explicitly with currency and period; otherwise pay is unknown or not
    comparable. Never estimated."""

    raw_text: str | None = None
    minimum: float | None = Field(default=None, ge=0)
    maximum: float | None = Field(default=None, ge=0)
    currency: CurrencyCode | None = None
    period: CompensationPeriod | None = None

    @model_validator(mode="after")
    def _explicit_bounds(self) -> Self:
        bounds = [b for b in (self.minimum, self.maximum) if b is not None]
        if any(not math.isfinite(b) for b in bounds):
            raise ValueError("compensation bounds must be finite")
        if bounds and (self.currency is None or self.period is None):
            raise ValueError("compensation bounds need an explicit currency and period")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("compensation minimum exceeds maximum")
        if not bounds and not (self.raw_text or "").strip():
            raise ValueError("compensation needs raw_text or explicit bounds")
        return self

    @property
    def is_comparable(self) -> bool:
        return (self.minimum is not None or self.maximum is not None) and bool(
            self.currency and self.period
        )


def meets_floor(pay: Compensation | None, floor: CompensationFloor) -> bool | None:
    """Deterministic comparison of stated pay with a floor.

    True if the stated range reaches the floor, False if its top is below it, and
    None (unknown) when pay is missing, only partly stated below the floor, in
    another currency, or in a period not exactly convertible (only MONTH -> YEAR x12
    and YEAR -> MONTH /12 are converted; hourly/daily/weekly need assumptions)."""
    if pay is None or not pay.is_comparable or pay.currency != floor.currency:
        return None
    factor: float
    if pay.period == floor.period:
        factor = 1.0
    elif pay.period is CompensationPeriod.MONTH and floor.period is CompensationPeriod.YEAR:
        factor = 12.0
    elif pay.period is CompensationPeriod.YEAR and floor.period is CompensationPeriod.MONTH:
        factor = 1 / 12
    else:
        return None
    if pay.maximum is not None:
        return pay.maximum * factor >= floor.amount
    assert pay.minimum is not None
    return True if pay.minimum * factor >= floor.amount else None


def _posting_url_key(url: str) -> str:
    try:
        return normalize_application_url(url)
    except InvalidApplicationUrl:
        return url.strip()


def _slug(value: str) -> str:
    return value.strip().lower()


def employer_job_key(ats_type: str, tenant: str, external_job_id: str) -> str:
    """Cross-source identity of one employer job, in the same format as
    ``JobIdentityObservation.identity_key``: ``ats:<ats type>:<tenant>:<job id>``.

    Set it only from job-specific evidence, such as an ATS posting URL or page that
    contains this job's own id (``boards.greenhouse.io/<tenant>/jobs/<id>``,
    ``jobs.lever.co/<tenant>/<id>``). Never derive it from a generic apply endpoint,
    a careers home page, a search page, or a title/company match."""
    parts = [_slug(ats_type), _slug(tenant), _slug(external_job_id)]
    if not all(parts) or any(":" in part for part in parts[:2]):
        raise ValueError("employer_job_key needs a non-empty ats type, tenant and job id")
    return "ats:" + ":".join(parts)


class ListingSource(Contract):
    """One source's observation of one posting. Kept for every source that showed it.

    Where it was seen and what it is are separate:

    * ``source_url`` is where the observation was made. It may be a search or
      results page shared by many postings, so it is never identity.
    * ``source_listing_id`` (the source's own job id) or ``posting_url`` (a URL for
      this posting alone) identifies the posting within the source. At least one is
      required; an observation without either is rejected rather than keyed on a
      search URL.
    * ``employer_job_key`` (optional) is proven cross-source identity, from
      ``employer_job_key(...)``.
    * ``application_url`` is only the link shown to apply. It may be a generic
      endpoint shared by many jobs, so it is never identity.
    """

    source: SourceName
    source_listing_id: str | None = None
    """The source's own id for the posting (e.g. a LinkedIn job id), if shown."""
    posting_url: str | None = None
    """A URL that shows this posting alone (a detail page or job-specific ATS
    posting), never a search, results or generic apply page."""
    source_url: NonEmptyStr
    """Where the observation was made (may be a shared search page)."""
    employer_job_key: str | None = Field(default=None, pattern=r"^ats:[^:\s]+:[^:\s]+:\S+$")
    """Proven employer/ATS job identity; see ``employer_job_key(...)``."""
    application_url: str | None = None
    """The application link this source pointed to, if visible. Not identity."""
    observed_at: UtcDatetime
    evidence: NonEmptyStr
    """What was observed, e.g. ``"search card + detail page, job id in URL"``. When
    ``employer_job_key`` is set, say where its job id was read."""
    query_id: str | None = None

    @field_validator("source_listing_id", "posting_url")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def _identified(self) -> Self:
        if self.source_listing_id is None and self.posting_url is None:
            raise ValueError(
                "a listing observation needs source_listing_id or a job-specific "
                "posting_url; a search page URL is not a posting identity"
            )
        return self

    @property
    def posting_key(self) -> str:
        """Identity of the posting within its source: ``src:<source>:id:<id>`` when
        the source showed its own id, else ``src:<source>:url:<normalized posting URL>``."""
        if self.source_listing_id is not None:
            return f"src:{self.source}:id:{self.source_listing_id}"
        assert self.posting_url is not None
        return f"src:{self.source}:url:{_posting_url_key(self.posting_url)}"


def listing_id_for(
    source: str, source_listing_id: str | None = None, posting_url: str | None = None
) -> str:
    """Stable listing id from the same key ``ListingSource.posting_key`` uses: the
    source plus its own id, else the source plus a job-specific posting URL. Raises
    ``ValueError`` when neither is given (never falls back to a search URL)."""
    record = ListingSource(
        source=source, source_listing_id=source_listing_id, posting_url=posting_url,
        source_url=posting_url or "about:blank", observed_at=utc_now(), evidence="id",
    )
    digest = hashlib.sha256(record.posting_key.encode()).hexdigest()
    return f"lst_{digest[:32]}"


def _source_ids(listing: JobListing) -> dict[str, str]:
    return {p.source: p.source_listing_id for p in listing.provenance if p.source_listing_id}


class JobListing(Contract):
    """One observed posting. Fields hold only what a source showed."""

    id: NonEmptyStr
    """Must equal ``listing_id_for(source, source_listing_id, posting_url)``."""
    source: SourceName
    """The source this record was first observed on."""
    source_listing_id: str | None = None
    posting_url: str | None = None
    """This posting's own URL on ``source`` (see ``ListingSource.posting_url``)."""
    source_url: NonEmptyStr
    application_url: str | None = None
    """The application link when visible. The application flow is given this (or
    ``posting_url``) if the listing is selected. Not identity: it may be generic."""
    title: NonEmptyStr
    company: str | None = None
    location: str | None = None
    """Location text as stated."""
    work_arrangement: WorkArrangement = WorkArrangement.UNKNOWN
    remote_eligibility: str | None = None
    """Where a remote worker may live, as stated (e.g. ``"United States"``, ``"Texas"``)."""
    compensation: Compensation | None = None
    description: str | None = None
    description_completeness: DescriptionCompleteness = DescriptionCompleteness.NONE
    status: ListingStatus = ListingStatus.UNKNOWN
    posted_text: str | None = None
    """Posting age/date as shown, e.g. ``"3 days ago"``."""
    observed_at: UtcDatetime
    evidence: NonEmptyStr
    provenance: list[ListingSource] = Field(min_length=1)
    """Every source that showed this posting; the first is ``source``."""

    @field_validator("source_listing_id", "posting_url")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        first = self.provenance[0]
        own = (self.source, self.source_listing_id, self.posting_url, self.source_url)
        if (first.source, first.source_listing_id, first.posting_url, first.source_url) != own:
            raise ValueError(
                "provenance[0] must be the listing's own source, id, posting_url and source_url"
            )
        if self.id != listing_id_for(self.source, self.source_listing_id, self.posting_url):
            raise ValueError("id must equal listing_id_for(source, source_listing_id, posting_url)")
        has_text = bool((self.description or "").strip())
        if has_text == (self.description_completeness is DescriptionCompleteness.NONE):
            raise ValueError("description_completeness must be NONE exactly when there is no text")
        keys = [(p.posting_key, p.source_url) for p in self.provenance]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate provenance records")
        ids: dict[str, str] = {}
        for record in self.provenance:
            if record.source_listing_id is None:
                continue
            seen = ids.setdefault(record.source, record.source_listing_id)
            if seen != record.source_listing_id:
                raise ValueError(
                    f"contradictory {record.source} ids {seen!r} and "
                    f"{record.source_listing_id!r} for one posting"
                )
        employer = {p.employer_job_key for p in self.provenance if p.employer_job_key}
        if len(employer) > 1:
            raise ValueError(f"contradictory employer job keys {sorted(employer)}")
        return self

    @property
    def identity_keys(self) -> frozenset[str]:
        """Keys that prove two listings are the same posting: each observation's
        ``posting_key`` and any proven ``employer_job_key`` (as ``job:<key>``).
        Search URLs, application URLs, titles, companies and locations are never keys."""
        keys = {p.posting_key for p in self.provenance}
        keys |= {f"job:{p.employer_job_key}" for p in self.provenance if p.employer_job_key}
        return frozenset(keys)

    def contradicts(self, other: JobListing) -> bool:
        """True when one source gives the two listings different ids of its own, or
        they carry different employer job keys: they are different postings."""
        mine, theirs = _source_ids(self), _source_ids(other)
        if any(mine[s] != theirs[s] for s in mine.keys() & theirs.keys()):
            return True
        a = {p.employer_job_key for p in self.provenance if p.employer_job_key}
        b = {p.employer_job_key for p in other.provenance if p.employer_job_key}
        return bool(a and b and a != b)

    def is_same_posting(self, other: JobListing) -> bool:
        """True only on a shared identity key and no contradiction. Equal listing ids
        imply this, because ids are derived from the same posting key."""
        return not self.contradicts(other) and bool(self.identity_keys & other.identity_keys)

    def merged_with(self, other: JobListing) -> JobListing:
        """Keep this listing's fields and add ``other``'s provenance (for example a
        Google result proven to be the same employer job). Missing fields are filled
        from ``other`` only where this listing has none. Raises unless the two are
        provably the same posting."""
        if not self.is_same_posting(other):
            raise ValueError("listings are not provably the same posting")
        records = {(p.posting_key, p.source_url): p for p in self.provenance}
        for incoming in other.provenance:
            key = (incoming.posting_key, incoming.source_url)
            previous = records.get(key)
            if previous is None:
                records[key] = incoming
                continue
            # A later detail-page observation may establish the employer identity
            # absent from the initial search card. Keep that proof and its evidence.
            evidence = previous.evidence
            if incoming.evidence != evidence and incoming.evidence not in evidence.split("\n"):
                evidence += "\n" + incoming.evidence
            records[key] = ListingSource.model_validate({
                **previous.model_dump(),
                "employer_job_key": previous.employer_job_key or incoming.employer_job_key,
                "application_url": previous.application_url or incoming.application_url,
                "observed_at": max(previous.observed_at, incoming.observed_at),
                "evidence": evidence,
            })
        fill: dict[str, Any] = {}
        for name in ("application_url", "company", "location", "remote_eligibility",
                     "compensation", "posted_text"):
            if getattr(self, name) is None and getattr(other, name) is not None:
                fill[name] = getattr(other, name)
        if (self.work_arrangement is WorkArrangement.UNKNOWN
                and other.work_arrangement is not WorkArrangement.UNKNOWN):
            fill["work_arrangement"] = other.work_arrangement
        rank = {DescriptionCompleteness.NONE: 0, DescriptionCompleteness.PARTIAL: 1,
                DescriptionCompleteness.FULL: 2}
        if rank[other.description_completeness] > rank[self.description_completeness]:
            fill["description"] = other.description
            fill["description_completeness"] = other.description_completeness
        if ListingStatus.CLOSED in (self.status, other.status):
            fill["status"] = ListingStatus.CLOSED
        elif self.status is ListingStatus.UNKNOWN:
            fill["status"] = other.status
        return type(self).model_validate(
            {**self.model_dump(), **fill, "provenance": list(records.values())}
        )


# --- selection preferences and decisions -------------------------------------------------


class UnknownCompensationPolicy(StrEnum):
    KEEP = "KEEP"
    """Evaluate on the other evidence; unknown pay does not disqualify."""
    REVIEW = "REVIEW"
    """Hold listings without comparable pay for the user's review."""


class SelectionPreferences(Contract):
    """The user's editable job-selection preferences. ``fingerprint`` changes with any
    edit, which invalidates decisions made under the old preferences."""

    target_titles: list[str] = Field(default_factory=lambda: list(DEFAULT_TITLE_PHRASES))
    onsite: list[OnsiteTarget] = Field(default_factory=_default_onsite)
    remote: RemoteTarget | None = Field(default_factory=_default_remote)
    location_priority: LocationPriority = LocationPriority.STRONGLY_PREFER_ONSITE_HYBRID
    minimum_compensation: CompensationFloor | None = Field(default_factory=_default_floor)
    unknown_compensation: UnknownCompensationPolicy = UnknownCompensationPolicy.KEEP
    excluded_keywords: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)
    notes: str | None = None

    @field_validator("target_titles", "excluded_keywords", "excluded_companies")
    @classmethod
    def _phrases(cls, value: list[str]) -> list[str]:
        return _clean_phrases(value)

    @model_validator(mode="after")
    def _usable(self) -> Self:
        if not self.target_titles:
            raise ValueError("at least one target title is required")
        if not self.onsite and self.remote is None:
            raise ValueError("preferences need an onsite target, a remote target, or both")
        return self

    @property
    def fingerprint(self) -> str:
        return snapshot_hash(self)


class SelectionChoice(StrEnum):
    APPLY = "APPLY"
    SKIP = "SKIP"
    REVIEW = "REVIEW"


class HoldCode(StrEnum):
    MISSING_PROFILE = "MISSING_PROFILE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    HARD_CONSTRAINT = "HARD_CONSTRAINT"
    """An explicit preference is violated (e.g. pay below the floor, excluded company)."""
    UNKNOWN_COMPENSATION = "UNKNOWN_COMPENSATION"
    LISTING_CLOSED = "LISTING_CLOSED"
    DUPLICATE_APPLICATION = "DUPLICATE_APPLICATION"
    """An application record already exists for this job."""


class PolicyHold(Contract):
    """A deterministic rule that stopped an APPLY. Recorded separately from Jev's answer."""

    code: HoldCode
    detail: NonEmptyStr


class ModelDecision(Contract):
    """Jev's original answer, unmodified."""

    choice: SelectionChoice
    probabilities: dict[SelectionChoice, float]
    confidence: Confidence
    provider: str | None = None
    decision_id: str | None = None
    """The provider's id for this decision, if returned."""

    @model_validator(mode="after")
    def _well_formed(self) -> Self:
        if not math.isfinite(self.confidence):
            raise ValueError("confidence must be finite")
        if set(self.probabilities) != set(SelectionChoice):
            raise ValueError("probabilities must cover exactly APPLY, SKIP and REVIEW")
        values = list(self.probabilities.values())
        if any(not math.isfinite(v) or not 0.0 <= v <= 1.0 for v in values):
            raise ValueError("probabilities must be finite and within [0, 1]")
        if abs(sum(values) - 1.0) > 1e-3:
            raise ValueError("probabilities must sum to 1")
        return self


class ProviderUsage(Contract):
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)

    @field_validator("cost_usd")
    @classmethod
    def _finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("cost must be finite")
        return value


class ProviderError(Contract):
    code: NonEmptyStr
    """E.g. ``"401"``, ``"402"``, ``"429"``, ``"UNAVAILABLE"``, ``"TIMEOUT"``, ``"MALFORMED"``."""
    message: NonEmptyStr
    retryable: bool = False


class SelectionOverride(Contract):
    """An explicit user decision, recorded apart from Jev's answer."""

    choice: SelectionChoice
    reason: NonEmptyStr
    decided_at: UtcDatetime = Field(default_factory=utc_now)


class JobSelection(Contract):
    """A persisted, auditable decision about one listing for one candidate.

    ``model_decision`` is Jev's original answer. ``holds`` and ``override`` are
    recorded separately. ``effective_choice`` is the result after them. Effective
    APPLY is impossible with any hold, a provider error, no model decision, or no
    candidate evidence. This is not a submission, not a receipt, and not an
    interview probability.
    """

    id: NonEmptyStr = Field(default_factory=lambda: new_id("sel"))
    listing_id: NonEmptyStr
    candidate_id: NonEmptyStr
    requested_model: NonEmptyStr = DEFAULT_JEV_MODEL
    returned_model: str | None = None
    """The model the provider actually reported (e.g. ``typesafe/jev-1.13-20260917``)."""
    rubric_version: NonEmptyStr
    preferences_fingerprint: Sha256Hex
    job_evidence_hash: Sha256Hex
    candidate_evidence_hash: Sha256Hex | None = None
    """None when no usable profile was available; requires a MISSING_PROFILE hold."""
    model_decision: ModelDecision | None = None
    provider_error: ProviderError | None = None
    usage: ProviderUsage | None = None
    holds: list[PolicyHold] = Field(default_factory=list)
    override: SelectionOverride | None = None
    effective_choice: SelectionChoice
    reasons: list[str] = Field(default_factory=list)
    """Short, evidence-based reasons for display."""
    decided_at: UtcDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _auditable(self) -> Self:
        codes = {h.code for h in self.holds}
        if self.model_decision is not None and self.provider_error is not None:
            raise ValueError("a selection has a model decision or a provider error, not both")
        if self.provider_error is not None and HoldCode.PROVIDER_ERROR not in codes:
            raise ValueError("a provider error must be recorded as a PROVIDER_ERROR hold")
        if self.candidate_evidence_hash is None and HoldCode.MISSING_PROFILE not in codes:
            raise ValueError("missing candidate evidence must be recorded as a MISSING_PROFILE hold")
        if self.model_decision is not None and self.returned_model is None:
            raise ValueError("a model decision must record the returned model")
        if self.override is not None and self.override.choice is not self.effective_choice:
            raise ValueError("effective_choice must equal the user's override")
        if self.effective_choice is SelectionChoice.APPLY:
            if self.holds or self.provider_error is not None:
                raise ValueError("effective APPLY is not allowed with holds or a provider error")
            if self.model_decision is None or self.candidate_evidence_hash is None:
                raise ValueError("effective APPLY needs a model decision and candidate evidence")
            if self.override is None and self.model_decision.choice is not SelectionChoice.APPLY:
                raise ValueError("effective APPLY needs Jev's APPLY or an explicit user override")
        return self

    def is_current_for(
        self, *, preferences: SelectionPreferences, job_evidence_hash: str,
        candidate_evidence_hash: str | None, rubric_version: str,
    ) -> bool:
        """True if this decision was made on exactly these inputs (cache check)."""
        return (
            self.preferences_fingerprint == preferences.fingerprint
            and self.job_evidence_hash == job_evidence_hash
            and self.candidate_evidence_hash == candidate_evidence_hash
            and self.rubric_version == rubric_version
        )


# --- pipeline tracker ----------------------------------------------------------------------

DEFAULT_PIPELINE_STAGES: tuple[str, ...] = (
    "Interested", "Applied", "Screening", "Interviewing", "Offer", "Closed",
)
"""A starting set only. Stages are user-configurable strings; imported wording is kept."""


class PipelineStages(Contract):
    """The user's ordered board columns. Entries may still carry stages not listed
    here (e.g. imported wording); boards show those as additional columns."""

    stages: list[NonEmptyStr] = Field(default_factory=lambda: list(DEFAULT_PIPELINE_STAGES))

    @field_validator("stages")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        if not value or len({s.casefold() for s in value}) != len(value):
            raise ValueError("stages must be a non-empty list of distinct names")
        return value


class PipelineEntry(Contract):
    """A user-managed tracker row. ``stage`` is the user's own wording, kept verbatim.
    It is not an ``ApplicationState``: moving a card never submits, and a submission
    does not rewrite the user's stage. ``application_id`` optionally links the
    canonical application record."""

    id: NonEmptyStr = Field(default_factory=lambda: new_id("pipe"))
    candidate_id: NonEmptyStr
    listing_id: str | None = None
    title: str | None = None
    """Snapshot for display and for imported rows without a stored listing."""
    company: str | None = None
    stage: NonEmptyStr
    notes: str | None = None
    next_action: str | None = None
    next_action_due: date | UtcDatetime | None = None
    """Preserve a calendar due date as a date; never invent a midnight timestamp."""
    application_id: str | None = None
    selection_id: str | None = None
    import_source: str | None = None
    """E.g. the workbook name the row came from."""
    imported_values: dict[str, str] = Field(default_factory=dict)
    """All original nonblank imported cells, including raw Stage and Status, verbatim."""
    created_at: UtcDatetime = Field(default_factory=utc_now)
    updated_at: UtcDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _identifiable(self) -> Self:
        if self.listing_id is None and not (self.title or "").strip():
            raise ValueError("a pipeline entry needs a listing_id or a title")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at is before created_at")
        return self
