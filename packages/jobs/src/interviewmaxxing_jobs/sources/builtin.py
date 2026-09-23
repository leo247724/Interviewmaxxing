"""Built In (builtin.com) through its public job board.

Search URLs are the ones the board's own filters produce (observed 2026-09-22):
``/jobs/<remote|hybrid|office...>?search=<text>&city=<City>&state=<State>&country=USA
&allLocations=true&page=<n>``. Job pages publish a schema.org JobPosting, which is
read for the description, explicit salary and remote eligibility. The APPLY button
is a Built In redirect handler and is never followed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

from interviewmaxxing_core import ListingStatus, SourceSearchState, WorkArrangement

from ..text import (
    clean,
    compensation_from_schema,
    html_to_text,
    parse_arrangement,
    parse_compensation,
)
from .base import (
    AccessProblem,
    BudgetPlan,
    Observation,
    SearchContext,
    SearchLeg,
    SourceOutcome,
    build_legs,
    check_access,
    expired,
    is_telecommute,
    make_listing,
    posting_address,
    posting_org,
    posting_remote_region,
)

NAME = "builtin"
HOME = "https://builtin.com/jobs"
PAGE_SIZE = 25
LOGIN_PATHS = ("/auth/login", "/user/login")
_PATH_SEGMENT = {WorkArrangement.REMOTE: "remote", WorkArrangement.HYBRID: "hybrid",
                 WorkArrangement.ONSITE: "office"}
_US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_US_NAMES = {"united states", "united states of america", "usa", "us"}


def search_url(leg: SearchLeg, *, page: int = 1) -> str | None:
    """The board URL for a leg, or None when the location is not a US city/state or
    the United States (Built In's location filter is US-centric)."""
    segments = "/".join(_PATH_SEGMENT[a] for a in sorted(
        leg.arrangements, key=lambda a: ("remote", "hybrid", "office").index(_PATH_SEGMENT[a])))
    params: dict[str, str] = {"search": leg.text}
    if leg.target == "remote":
        if leg.location.strip().lower() not in _US_NAMES:
            return None
        params.update(city="", state="", country="USA")
    else:
        m = re.fullmatch(r"\s*(?P<city>[^,]+),\s*(?P<state>[A-Za-z .]+?)\s*(?:,\s*(USA?|United States))?\s*",
                         leg.location)
        if not m:
            return None
        state = m.group("state").strip()
        state = _US_STATES.get(state.upper(), state.title())
        if state not in _US_STATES.values():
            return None
        params.update(city=m.group("city").strip(), state=state, country="USA")
    params["allLocations"] = "true"
    if page > 1:
        params["page"] = str(page)
    return f"https://builtin.com/jobs/{segments}?" + urlencode(params)


@dataclass
class Card:
    id: str
    title: str
    url: str
    company: str | None = None
    posted: str | None = None
    arrangement_label: str | None = None
    location: str | None = None
    salary: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    seen_on: str = HOME
    """The results page the card was read from (provenance, not identity)."""


def parse_cards(payload: dict[str, Any]) -> list[Card]:
    seen_on = str((payload.get("page") or {}).get("url") or HOME)
    cards = []
    for item in payload.get("cards") or []:
        job_id, title, href = clean(item.get("id")), clean(item.get("title")), clean(item.get("href"))
        if not (job_id and job_id.isdigit() and title and href):
            continue
        path = urlsplit(href).path
        cards.append(Card(
            id=job_id, title=title, url=f"https://builtin.com{path}",
            company=clean(item.get("company")), posted=clean(item.get("posted")),
            arrangement_label=clean(item.get("arrangement")), location=clean(item.get("location")),
            salary=clean(item.get("salary")), raw={"level": clean(item.get("level"))},
            seen_on=seen_on,
        ))
    return cards


def has_page(payload: dict[str, Any], page: int) -> bool:
    return any(re.search(rf"[?&]page={page}(?:&|$)", str(h)) for h in payload.get("page_links") or [])


def card_observation(card: Card, *, observed_at: datetime, query_id: str | None, leg: str) -> Observation:
    listing = make_listing(
        source=NAME, source_listing_id=card.id, posting_url=card.url, source_url=card.seen_on,
        title=card.title, company=card.company, location=card.location,
        work_arrangement=parse_arrangement(card.arrangement_label),
        compensation=parse_compensation(card.salary),
        posted_text=card.posted, observed_at=observed_at, query_id=query_id,
        evidence="Built In search result card; job id from the card element",
    )
    return Observation(listing, {"kind": "builtin.card", "leg": leg,
                                 "arrangement_label": card.arrangement_label, **card.raw})


def detail_observation(payload: dict[str, Any], card: Card, *, observed_at: datetime,
                       query_id: str | None, leg: str) -> Observation:
    posting = payload.get("posting") if isinstance(payload.get("posting"), dict) else None
    title = clean((posting or {}).get("title")) or clean(payload.get("h1")) or card.title
    description = html_to_text((posting or {}).get("description"))
    compensation = compensation_from_schema((posting or {}).get("baseSalary"), card.salary) \
        or parse_compensation(card.salary)
    arrangement = parse_arrangement(card.arrangement_label)
    if arrangement is WorkArrangement.UNKNOWN and not card.arrangement_label and is_telecommute(posting):
        arrangement = WorkArrangement.REMOTE
    if payload.get("closed") or expired((posting or {}).get("validThrough"), observed_at):
        status = ListingStatus.CLOSED
    elif payload.get("apply_href"):
        status = ListingStatus.OPEN
    else:
        status = ListingStatus.UNKNOWN
    evidence = "Built In job page"
    if posting:
        evidence += " with schema.org JobPosting (description, salary, location requirements)"
    listing = make_listing(
        source=NAME, source_listing_id=card.id, posting_url=card.url, source_url=card.url,
        title=title, company=posting_org(posting) or card.company,
        location=card.location or posting_address(posting), work_arrangement=arrangement,
        remote_eligibility=posting_remote_region(posting), compensation=compensation,
        description=description, full_description=True, status=status,
        posted_text=card.posted or clean((posting or {}).get("datePosted")),
        observed_at=observed_at, query_id=query_id, evidence=evidence,
    )
    raw = {
        "kind": "builtin.detail", "leg": leg, "arrangement_label": card.arrangement_label,
        "apply_redirect": payload.get("apply_href"), **card.raw,
        **{k: (posting or {}).get(k) for k in ("datePosted", "validThrough", "employmentType",
                                               "jobLocationType", "directApply")},
    }
    return Observation(listing, raw)


class BuiltInAdapter:
    name = NAME

    def search(self, ctx: SearchContext) -> SourceOutcome:
        legs = build_legs(ctx.query)
        planned: list[tuple[Card, SearchLeg]] = []
        seen: set[str] = set()
        pages = 0
        more_available = False
        notes: list[str] = []
        plan = BudgetPlan(ctx.limit, legs, ctx.query.location_priority)
        for index, leg in enumerate(legs):
            budget = plan.budget(index)
            taken = 0
            for page in range(1, ctx.max_pages_per_leg + 1):
                url = search_url(leg, page=page)
                if url is None:
                    notes.append(f"skipped {leg.label}: Built In's filter takes a US city/state "
                                 "or the United States")
                    break
                if taken >= budget:
                    more_available = True
                    break
                ctx.transport.open(ctx.session, url)
                ctx.transport.wait_for(ctx.session, '[data-id="job-card"]', 15000)
                payload = ctx.evaluate("builtin_search")
                pages += 1
                check_access("Built In", payload, ctx, login_paths=LOGIN_PATHS, home_url=HOME)
                cards = parse_cards(payload)
                for card in cards:
                    if card.id in seen:
                        continue
                    if taken >= budget:
                        more_available = True
                        break
                    seen.add(card.id)
                    planned.append((card, leg))
                    taken += 1
                if not cards or not has_page(payload, page + 1):
                    break
            else:
                more_available = True
            plan.spent(index, taken)
        if ctx.query.posted_within_days:
            notes.append("posted_within_days is not applied on Built In")

        observations: list[Observation] = []
        problems: list[str] = []
        for index, (card, leg) in enumerate(planned):
            if index < ctx.detail_limit:
                try:
                    observations.append(self._detail(ctx, card, leg))
                    pages += 1
                    continue
                except AccessProblem:
                    raise
                except Exception as exc:
                    problems.append(f"job {card.id}: {exc}")
            observations.append(card_observation(card, observed_at=ctx.clock(),
                                                 query_id=ctx.query.id, leg=leg.label))
        if problems:
            notes.append("detail pages failed: " + "; ".join(problems[:5]))
        if more_available:
            notes.append(f"stopped at the limit of {ctx.limit} listings")
        state = SourceSearchState.PARTIAL if (more_available or problems) else SourceSearchState.OK
        return SourceOutcome(state, observations, pages, "; ".join(notes) or None)

    def _detail(self, ctx: SearchContext, card: Card, leg: SearchLeg) -> Observation:
        ctx.transport.open(ctx.session, card.url)
        ctx.transport.wait_for(ctx.session, 'script[type="application/ld+json"], h1', 15000)
        payload = ctx.evaluate("builtin_detail")
        check_access("Built In", payload, ctx, login_paths=LOGIN_PATHS, home_url=HOME)
        return detail_observation(payload, card, observed_at=ctx.clock(), query_id=ctx.query.id,
                                  leg=leg.label)
