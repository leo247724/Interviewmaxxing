"""Shared pieces of the per-source adapters."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from interviewmaxxing_core import (
    Compensation,
    DescriptionCompleteness,
    JobListing,
    JobSearchQuery,
    ListingSource,
    ListingStatus,
    LocationPriority,
    SourceSearchState,
    WorkArrangement,
    listing_id_for,
)

from ..opencli import BrowserTransport, extraction_script


@dataclass(frozen=True)
class SearchLeg:
    """One search the source runs: a title phrase in one place."""

    text: str
    """Text typed into the source's keyword box (title phrase plus custom keywords)."""
    title_phrase: str
    target: Literal["onsite", "remote"]
    location: str
    """Onsite city, or the remote eligibility region."""
    arrangements: tuple[WorkArrangement, ...]
    radius_miles: int | None = None

    @property
    def label(self) -> str:
        kinds = "/".join(a.value.lower() for a in self.arrangements)
        return f"{self.text!r} {kinds} in {self.location}"


def build_legs(query: JobSearchQuery) -> list[SearchLeg]:
    """Search legs in priority order. With the default
    ``STRONGLY_PREFER_ONSITE_HYBRID`` every onsite/hybrid leg (e.g. Austin) runs
    before any remote leg; ``PREFER_REMOTE`` reverses that; ``BALANCED`` alternates
    per title phrase. Remote legs are always kept when a remote target is set."""
    extra = " ".join(query.keywords)
    onsite: list[SearchLeg] = []
    remote: list[SearchLeg] = []
    per_phrase: list[SearchLeg] = []
    for phrase in query.title_phrases:
        text = f"{phrase} {extra}".strip()
        for target in query.onsite:
            leg = SearchLeg(text, phrase, "onsite", target.location, tuple(target.arrangements),
                            target.radius_miles)
            onsite.append(leg)
            per_phrase.append(leg)
        if query.remote is not None:
            leg = SearchLeg(text, phrase, "remote", query.remote.eligible_region,
                            (WorkArrangement.REMOTE,))
            remote.append(leg)
            per_phrase.append(leg)
    if query.location_priority is LocationPriority.BALANCED:
        return per_phrase
    if query.location_priority is LocationPriority.PREFER_REMOTE:
        return remote + onsite
    return onsite + remote


_WEIGHTS = {
    LocationPriority.STRONGLY_PREFER_ONSITE_HYBRID: (4, 1),
    LocationPriority.BALANCED: (1, 1),
    LocationPriority.PREFER_REMOTE: (1, 4),
}


def leg_budgets(total: int, legs: int, weights: Sequence[int] | None = None) -> list[int]:
    """Split a per-source result limit across legs in proportion to ``weights``.
    Leftover slots go to the most-preferred (highest weight) legs first, then by
    largest remainder, then to earlier legs."""
    if legs <= 0:
        return []
    w = list(weights) if weights is not None else [1] * legs
    whole = sum(w)
    shares = [total * x / whole for x in w]
    out = [math.floor(x) for x in shares]
    order = sorted(range(legs), key=lambda i: (-w[i], -(shares[i] - out[i]), i))
    for i in order[: total - sum(out)]:
        out[i] += 1
    return out


class BudgetPlan:
    """Per-leg result budgets for one source. Preferred (onsite/hybrid by default)
    legs get the larger share and run first; whatever a leg does not use carries
    forward, so a thin Austin market still leaves room for eligible remote roles
    and remote roles can never crowd Austin out of a bounded result limit."""

    def __init__(self, limit: int, legs: Sequence[SearchLeg], priority: LocationPriority):
        on, rem = _WEIGHTS[priority]
        self.budgets = leg_budgets(limit, len(legs),
                                   [on if leg.target == "onsite" else rem for leg in legs])
        self.carry = 0

    def budget(self, index: int) -> int:
        return self.budgets[index] + self.carry

    def spent(self, index: int, taken: int) -> None:
        self.carry = max(0, self.budget(index) - taken)


@dataclass
class Observation:
    """A listing as one source showed it, plus the raw evidence kept privately."""

    listing: JobListing
    raw: dict[str, Any]


class AccessProblem(Exception):
    """The source needs the user (sign-in, verification) or denied access."""

    def __init__(self, state: SourceSearchState, message: str, user_action: str | None = None):
        super().__init__(message)
        self.state = state
        self.message = message
        self.user_action = user_action


@dataclass
class SourceOutcome:
    state: SourceSearchState
    observations: list[Observation] = field(default_factory=list)
    pages_visited: int = 0
    message: str | None = None
    user_action: str | None = None


@dataclass
class SearchContext:
    transport: BrowserTransport
    session: str
    query: JobSearchQuery
    limit: int
    """Maximum listings for this source."""
    detail_limit: int
    """Maximum detail pages to read for this source."""
    clock: Callable[[], datetime]
    profile: str
    max_pages_per_leg: int = 3
    page_pause_s: float = 1.5

    def evaluate(self, script: str) -> dict[str, Any]:
        result = self.transport.evaluate(self.session, extraction_script(script))
        if not isinstance(result, dict):
            raise ValueError(f"{script} returned {type(result).__name__}, expected an object")
        return result


class SourceAdapter(Protocol):
    name: str

    def search(self, ctx: SearchContext) -> SourceOutcome: ...


def resolve_command(profile: str, session: str, url: str) -> str:
    return f"opencli --profile {profile} browser {session} open {url} --window foreground"


def check_access(source: str, payload: Mapping[str, Any], ctx: SearchContext, *,
                 login_paths: Sequence[str] = (), home_url: str) -> None:
    """Raise AccessProblem when the page is a sign-in wall, a challenge or a denial."""
    page = payload.get("page") or {}
    signals = page.get("signals") or {}
    path = urlsplit(str(page.get("url") or "")).path.lower()
    resolve = resolve_command(ctx.profile, ctx.session, home_url)
    if signals.get("denied"):
        raise AccessProblem(SourceSearchState.BLOCKED,
                            f"{source} denied access ({page.get('title') or 'access denied'}).")
    if signals.get("challenge") or path.startswith("/sorry"):
        raise AccessProblem(
            SourceSearchState.NEEDS_USER,
            f"{source} is showing a verification check; it is not solved automatically.",
            f"Open the {ctx.session} browser session ({resolve}), complete the check "
            "yourself, then search again.",
        )
    if any(path.startswith(p) for p in login_paths) or signals.get("password_field"):
        raise AccessProblem(
            SourceSearchState.NEEDS_USER,
            f"{source} requires sign-in before showing jobs.",
            f"Sign in to {source} in the {ctx.session} browser session ({resolve}), "
            "then search again.",
        )


def completeness(text: str | None, full: bool) -> DescriptionCompleteness:
    if not (text or "").strip():
        return DescriptionCompleteness.NONE
    return DescriptionCompleteness.FULL if full else DescriptionCompleteness.PARTIAL


def make_listing(
    *,
    source: str,
    source_listing_id: str | None,
    posting_url: str | None,
    source_url: str,
    title: str,
    observed_at: datetime,
    evidence: str,
    query_id: str | None,
    company: str | None = None,
    location: str | None = None,
    work_arrangement: WorkArrangement = WorkArrangement.UNKNOWN,
    remote_eligibility: str | None = None,
    compensation: Compensation | None = None,
    description: str | None = None,
    full_description: bool = False,
    status: ListingStatus = ListingStatus.UNKNOWN,
    posted_text: str | None = None,
    application_url: str | None = None,
    employer_job_key: str | None = None,
    employer_key_url: str | None = None,
) -> JobListing:
    """One source observation as a D0 listing.

    ``source_listing_id``/``posting_url`` identify the posting within its source;
    ``source_url`` is where it was seen (for a search card, the results page).
    ``employer_job_key`` is set only from a job-specific ATS URL (``employer_key_url``),
    which the evidence names."""
    description = (description or "").strip() or None
    if employer_job_key:
        evidence += f"; employer job key {employer_job_key} read from {employer_key_url}"
    first = ListingSource(
        source=source, source_listing_id=source_listing_id, posting_url=posting_url,
        source_url=source_url, employer_job_key=employer_job_key,
        application_url=application_url, observed_at=observed_at, evidence=evidence,
        query_id=query_id,
    )
    return JobListing(
        id=listing_id_for(source, source_listing_id, posting_url),
        source=source, source_listing_id=source_listing_id, posting_url=posting_url,
        source_url=source_url, application_url=application_url, title=title,
        company=company or None, location=location or None, work_arrangement=work_arrangement,
        remote_eligibility=remote_eligibility, compensation=compensation,
        description=description, description_completeness=completeness(description, full_description),
        status=status, posted_text=posted_text or None, observed_at=observed_at,
        evidence=evidence, provenance=[first],
    )


def expired(valid_through: object, now: datetime) -> bool:
    """True when a JobPosting ``validThrough`` date has passed."""
    if not isinstance(valid_through, str) or not valid_through.strip():
        return False
    try:
        when = datetime.fromisoformat(valid_through.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        return False
    return when < now


def posting_org(posting: Mapping[str, Any] | None) -> str | None:
    org = (posting or {}).get("hiringOrganization")
    if isinstance(org, dict) and isinstance(org.get("name"), str):
        return org["name"].strip() or None
    return None


def posting_address(posting: Mapping[str, Any] | None) -> str | None:
    place = (posting or {}).get("jobLocation")
    if isinstance(place, list):
        place = place[0] if len(place) == 1 else None
    address = place.get("address") if isinstance(place, dict) else None
    if not isinstance(address, dict):
        return None
    parts = [address.get(k) for k in ("addressLocality", "addressRegion", "addressCountry")]
    text = ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    return text or None


def posting_remote_region(posting: Mapping[str, Any] | None) -> str | None:
    req = (posting or {}).get("applicantLocationRequirements")
    if isinstance(req, list):
        names = [str(r["name"]).strip() for r in req
                 if isinstance(r, dict) and isinstance(r.get("name"), str)]
        return ", ".join(n for n in names if n) or None
    if isinstance(req, dict) and isinstance(req.get("name"), str):
        return req["name"].strip() or None
    return None


def is_telecommute(posting: Mapping[str, Any] | None) -> bool:
    kind = (posting or {}).get("jobLocationType")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(k, str) and k.upper() == "TELECOMMUTE" for k in kinds)


