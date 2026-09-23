"""Deterministic selection policy: pay comparison, explicit hard constraints and holds.

Jev never does arithmetic, date math or duplicate detection. Code compares stated pay
with the floor (core ``meets_floor``), enforces the user's explicit hard constraints,
recognizes closed and already-applied listings, and caps the effective decision
whenever evidence is missing, ambiguous, contradictory or the provider failed. None of
these checks can turn a non-APPLY into APPLY.

Holds are tracked with a fine-grained :class:`HoldReason` and recorded on the
canonical ``JobSelection`` as core ``PolicyHold(code, detail)``; the detail starts with
the reason name so the original reason stays auditable.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from interviewmaxxing_core import (
    DescriptionCompleteness,
    HoldCode,
    JobListing,
    ListingStatus,
    LocationPriority,
    PolicyHold,
    SelectionChoice,
    SelectionPreferences,
    UnknownCompensationPolicy,
    WorkArrangement,
    meets_floor,
)

from .evidence import MAX_DESCRIPTION_CHARS


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CompensationStatus(StrEnum):
    MEETS_FLOOR = "MEETS_FLOOR"
    """The stated range reaches the floor."""
    BELOW_FLOOR = "BELOW_FLOOR"
    """The stated top of the range is below the floor (explicit hard constraint)."""
    UNKNOWN = "UNKNOWN"
    """No pay stated, or only raw text without explicit bounds."""
    NONCOMPARABLE = "NONCOMPARABLE"
    """Bounds are stated but not comparable without assumptions (another currency,
    hourly/daily/weekly pay, or only a lower bound under the floor)."""
    NO_FLOOR = "NO_FLOOR"


def check_compensation(
    listing: JobListing, preferences: SelectionPreferences
) -> CompensationStatus:
    floor = preferences.minimum_compensation
    if floor is None:
        return CompensationStatus.NO_FLOOR
    pay = listing.compensation
    if pay is None or not pay.is_comparable:
        return CompensationStatus.UNKNOWN
    result = meets_floor(pay, floor)
    if result is None:
        return CompensationStatus.NONCOMPARABLE
    return CompensationStatus.MEETS_FLOOR if result else CompensationStatus.BELOW_FLOOR


class LocationStatus(StrEnum):
    ONSITE_ACCEPTED = "ONSITE_ACCEPTED"
    ONSITE_MISMATCH = "ONSITE_MISMATCH"
    """Explicit onsite/hybrid role outside every accepted location/arrangement."""
    REMOTE_REGION_MATCH = "REMOTE_REGION_MATCH"
    """Remote, and the stated eligibility equals the user's remote region."""
    REMOTE_NEEDS_ELIGIBILITY = "REMOTE_NEEDS_ELIGIBILITY"
    """Remote with unstated or different eligibility text; judged from the listing."""
    REMOTE_NOT_WANTED = "REMOTE_NOT_WANTED"
    UNKNOWN = "UNKNOWN"


_US_ALIASES = frozenset(
    {"us", "u.s.", "usa", "u.s.a.", "united states", "united states of america"}
)


def _region_key(text: str) -> str:
    key = " ".join(text.casefold().split())
    return "united states" if key in _US_ALIASES else key


def _has_term(text: str | None, term: str) -> bool:
    if not text or not term.strip():
        return False
    return re.search(rf"\b{re.escape(term.strip().casefold())}\b", text.casefold()) is not None


def check_location(listing: JobListing, preferences: SelectionPreferences) -> LocationStatus:
    """Work arrangement and location only; commute is not considered. An onsite
    target matches when the listing's location names its locality (the part of
    ``"Austin, TX"`` before the comma) and the arrangement is accepted there."""
    arrangement = listing.work_arrangement
    if arrangement is WorkArrangement.REMOTE:
        if preferences.remote is None:
            return LocationStatus.REMOTE_NOT_WANTED
        stated = listing.remote_eligibility
        if stated and _region_key(stated) == _region_key(preferences.remote.eligible_region):
            return LocationStatus.REMOTE_REGION_MATCH
        return LocationStatus.REMOTE_NEEDS_ELIGIBILITY
    if arrangement is WorkArrangement.UNKNOWN or not listing.location:
        return LocationStatus.UNKNOWN
    for target in preferences.onsite:
        locality = target.location.split(",", 1)[0]
        if arrangement in target.arrangements and _has_term(listing.location, locality):
            return LocationStatus.ONSITE_ACCEPTED
    return LocationStatus.ONSITE_MISMATCH


class LocationTier(StrEnum):
    """Where an eligible listing ranks under ``SelectionPreferences.location_priority``.
    An ordinal preference, not a weight and never an exclusion."""

    PREFERRED = "PREFERRED"
    """The arrangement the user ranks first (default: matching Austin onsite/hybrid)."""
    EQUAL = "EQUAL"
    """BALANCED priority: matching onsite/hybrid and eligible remote rank the same."""
    SECONDARY = "SECONDARY"
    """Eligible, and a valid option, but ranked below the preferred tier."""
    UNRANKED = "UNRANKED"
    """Location unknown or not accepted; never assumed to be the preferred tier."""


_ONSITE_STATUSES = frozenset({LocationStatus.ONSITE_ACCEPTED})
_REMOTE_STATUSES = frozenset(
    {LocationStatus.REMOTE_REGION_MATCH, LocationStatus.REMOTE_NEEDS_ELIGIBILITY}
)


def location_tier(location: LocationStatus, priority: LocationPriority) -> LocationTier:
    if priority is LocationPriority.BALANCED and location in _ONSITE_STATUSES | _REMOTE_STATUSES:
        return LocationTier.EQUAL
    first, second = (
        (_REMOTE_STATUSES, _ONSITE_STATUSES)
        if priority is LocationPriority.PREFER_REMOTE
        else (_ONSITE_STATUSES, _REMOTE_STATUSES)
    )
    if location in first:
        return LocationTier.PREFERRED
    if location in second:
        return LocationTier.SECONDARY
    return LocationTier.UNRANKED


def location_priority_reason(
    tier: LocationTier, priority: LocationPriority, location: LocationStatus
) -> str:
    """A plain ranking reason for display and for Jev's state."""
    kind = "onsite/hybrid in an accepted location" if location in _ONSITE_STATUSES else "remote"
    if tier is LocationTier.PREFERRED:
        return f"location priority: {kind} is the preferred tier ({priority.value})"
    if tier is LocationTier.SECONDARY:
        return (
            f"location priority: {kind} is an eligible secondary tier, ranked below the "
            f"preferred tier ({priority.value}); not excluded"
        )
    if tier is LocationTier.EQUAL:
        return f"location priority: {kind} ranks equally with other eligible roles (BALANCED)"
    return "location priority: unranked (location unknown or not accepted)"


class HoldReason(StrEnum):
    # Explicit hard constraints: effective SKIP without calling Jev.
    LISTING_CLOSED = "LISTING_CLOSED"
    DUPLICATE_APPLICATION = "DUPLICATE_APPLICATION"
    PAY_BELOW_FLOOR = "PAY_BELOW_FLOOR"
    LOCATION_MISMATCH = "LOCATION_MISMATCH"
    REMOTE_NOT_WANTED = "REMOTE_NOT_WANTED"
    EXCLUDED_KEYWORD = "EXCLUDED_KEYWORD"
    EXCLUDED_COMPANY = "EXCLUDED_COMPANY"
    # Holds: APPLY becomes REVIEW (those in SKIP_BLOCKING also turn SKIP into REVIEW).
    MISSING_PROFILE = "MISSING_PROFILE"
    MISSING_LISTING_DETAILS = "MISSING_LISTING_DETAILS"
    INCOMPLETE_DESCRIPTION = "INCOMPLETE_DESCRIPTION"
    LOCATION_UNKNOWN = "LOCATION_UNKNOWN"
    ELIGIBILITY_AMBIGUOUS = "ELIGIBILITY_AMBIGUOUS"
    UNKNOWN_COMPENSATION = "UNKNOWN_COMPENSATION"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CONTRADICTORY_EVIDENCE = "CONTRADICTORY_EVIDENCE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    SUSPECTED_INSTRUCTION_INJECTION = "SUSPECTED_INSTRUCTION_INJECTION"
    PROVIDER_ERROR = "PROVIDER_ERROR"


HOLD_CODES: dict[HoldReason, HoldCode] = {
    HoldReason.LISTING_CLOSED: HoldCode.LISTING_CLOSED,
    HoldReason.DUPLICATE_APPLICATION: HoldCode.DUPLICATE_APPLICATION,
    HoldReason.PAY_BELOW_FLOOR: HoldCode.HARD_CONSTRAINT,
    HoldReason.LOCATION_MISMATCH: HoldCode.HARD_CONSTRAINT,
    HoldReason.REMOTE_NOT_WANTED: HoldCode.HARD_CONSTRAINT,
    HoldReason.EXCLUDED_KEYWORD: HoldCode.HARD_CONSTRAINT,
    HoldReason.EXCLUDED_COMPANY: HoldCode.HARD_CONSTRAINT,
    HoldReason.MISSING_PROFILE: HoldCode.MISSING_PROFILE,
    HoldReason.MISSING_LISTING_DETAILS: HoldCode.INSUFFICIENT_EVIDENCE,
    HoldReason.INCOMPLETE_DESCRIPTION: HoldCode.INSUFFICIENT_EVIDENCE,
    HoldReason.LOCATION_UNKNOWN: HoldCode.INSUFFICIENT_EVIDENCE,
    HoldReason.ELIGIBILITY_AMBIGUOUS: HoldCode.INSUFFICIENT_EVIDENCE,
    HoldReason.UNKNOWN_COMPENSATION: HoldCode.UNKNOWN_COMPENSATION,
    HoldReason.INSUFFICIENT_EVIDENCE: HoldCode.INSUFFICIENT_EVIDENCE,
    HoldReason.CONTRADICTORY_EVIDENCE: HoldCode.INSUFFICIENT_EVIDENCE,
    HoldReason.LOW_CONFIDENCE: HoldCode.LOW_CONFIDENCE,
    HoldReason.SUSPECTED_INSTRUCTION_INJECTION: HoldCode.INSUFFICIENT_EVIDENCE,
    HoldReason.PROVIDER_ERROR: HoldCode.PROVIDER_ERROR,
}
"""Core ``HoldCode`` for each reason. Contradiction, ambiguity and suspected injection
have no dedicated core code yet and are recorded as INSUFFICIENT_EVIDENCE."""

HARD_CONSTRAINTS = frozenset(
    {
        HoldReason.LISTING_CLOSED,
        HoldReason.DUPLICATE_APPLICATION,
        HoldReason.PAY_BELOW_FLOOR,
        HoldReason.LOCATION_MISMATCH,
        HoldReason.REMOTE_NOT_WANTED,
        HoldReason.EXCLUDED_KEYWORD,
        HoldReason.EXCLUDED_COMPANY,
    }
)

SKIP_BLOCKING = frozenset(
    {
        HoldReason.CONTRADICTORY_EVIDENCE,
        HoldReason.LOW_CONFIDENCE,
        HoldReason.SUSPECTED_INSTRUCTION_INJECTION,
        HoldReason.PROVIDER_ERROR,
    }
)
"""Holds under which a model SKIP is not trusted either (becomes REVIEW), so promising
roles are never silently discarded."""


class Hold(_Model):
    reason: HoldReason
    detail: str

    def to_contract(self) -> PolicyHold:
        return PolicyHold(
            code=HOLD_CODES[self.reason], detail=f"{self.reason.value}: {self.detail}"
        )


def hard_constraint_holds(
    listing: JobListing,
    preferences: SelectionPreferences,
    *,
    compensation: CompensationStatus,
    location: LocationStatus,
    existing_application_id: str | None,
) -> list[Hold]:
    holds: list[Hold] = []
    if listing.status is ListingStatus.CLOSED:
        holds.append(Hold(reason=HoldReason.LISTING_CLOSED, detail="source marks it closed"))
    if existing_application_id:
        holds.append(
            Hold(
                reason=HoldReason.DUPLICATE_APPLICATION,
                detail=f"existing application {existing_application_id}",
            )
        )
    if compensation is CompensationStatus.BELOW_FLOOR:
        pay = listing.compensation.raw_text if listing.compensation else None
        holds.append(
            Hold(reason=HoldReason.PAY_BELOW_FLOOR, detail=f"stated pay {pay!r} below floor")
        )
    if location is LocationStatus.ONSITE_MISMATCH:
        holds.append(
            Hold(
                reason=HoldReason.LOCATION_MISMATCH,
                detail=f"{listing.work_arrangement.value} in {listing.location!r}",
            )
        )
    if location is LocationStatus.REMOTE_NOT_WANTED:
        holds.append(Hold(reason=HoldReason.REMOTE_NOT_WANTED, detail="no remote target set"))
    for term in preferences.excluded_keywords:
        if _has_term(listing.title, term):
            holds.append(Hold(reason=HoldReason.EXCLUDED_KEYWORD, detail=f"title has {term!r}"))
    company = (listing.company or "").casefold().strip()
    for excluded in preferences.excluded_companies:
        if company and company == excluded.casefold().strip():
            holds.append(Hold(reason=HoldReason.EXCLUDED_COMPANY, detail=excluded))
    return holds


def evidence_holds(
    listing: JobListing,
    preferences: SelectionPreferences,
    *,
    has_candidate_evidence: bool,
    compensation: CompensationStatus,
    location: LocationStatus,
) -> list[Hold]:
    """Holds known before Jev answers."""
    holds: list[Hold] = []
    if not has_candidate_evidence:
        holds.append(
            Hold(reason=HoldReason.MISSING_PROFILE, detail="no verified candidate qualifications")
        )
    text = (listing.description or "").strip()
    if not text:
        holds.append(Hold(reason=HoldReason.MISSING_LISTING_DETAILS, detail="no description"))
    elif (
        listing.description_completeness is not DescriptionCompleteness.FULL
        or len(listing.description or "") > MAX_DESCRIPTION_CHARS
    ):
        holds.append(
            Hold(reason=HoldReason.INCOMPLETE_DESCRIPTION, detail="only partial description")
        )
    if location is LocationStatus.UNKNOWN:
        holds.append(
            Hold(reason=HoldReason.LOCATION_UNKNOWN, detail="work arrangement or location unstated")
        )
    if preferences.unknown_compensation is UnknownCompensationPolicy.REVIEW and compensation in (
        CompensationStatus.UNKNOWN,
        CompensationStatus.NONCOMPARABLE,
    ):
        holds.append(
            Hold(reason=HoldReason.UNKNOWN_COMPENSATION, detail=f"pay {compensation.value}")
        )
    injection = detect_instruction_injection(listing)
    if injection:
        holds.append(Hold(reason=HoldReason.SUSPECTED_INSTRUCTION_INJECTION, detail=injection))
    return holds


_INJECTION = re.compile(
    r"(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|"
    r"preceding|your|system)\s+(instructions|prompts?|rules|rubric)"
    r"|system\s+prompt|you\s+are\s+(now\s+)?(an?\s+)?(ai|assistant|language\s+model|jev)"
    r"|(respond\s+with|answer\s+with|answer|output|decide|classify\s+(this|it)\s+as|"
    r"mark\s+(this|it)\s+as)\s*[:\"'`]?\s*(APPLY|SKIP|REVIEW)\b",
    re.IGNORECASE,
)


def detect_instruction_injection(listing: JobListing) -> str | None:
    """A short description of instruction-like text aimed at the model, if any.

    Detection only adds a hold; the text is still sent as untrusted data, and the rubric
    (instructions and options) never contains listing text."""
    fields = {
        "title": listing.title,
        "company": listing.company,
        "location": listing.location,
        "remote_eligibility": listing.remote_eligibility,
        "compensation": listing.compensation.raw_text if listing.compensation else None,
        "description": listing.description,
    }
    for name, value in fields.items():
        if value and (match := _INJECTION.search(value)):
            return f"{name}: {match.group(0)[:80]!r}"
    return None


def effective_decision(model_choice: SelectionChoice | None, holds: list[Hold]) -> SelectionChoice:
    """Combine Jev's choice with policy. Hard constraints SKIP; holds cap APPLY at
    REVIEW; SKIP-blocking holds cap SKIP at REVIEW; no choice means REVIEW."""
    reasons = {h.reason for h in holds}
    if reasons & HARD_CONSTRAINTS:
        return SelectionChoice.SKIP
    if model_choice is None:
        return SelectionChoice.REVIEW
    if model_choice is SelectionChoice.APPLY and reasons:
        return SelectionChoice.REVIEW
    if model_choice is SelectionChoice.SKIP and reasons & SKIP_BLOCKING:
        return SelectionChoice.REVIEW
    return model_choice
