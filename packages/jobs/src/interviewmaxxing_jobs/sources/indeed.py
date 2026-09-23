"""Indeed (US site) through its normal search and job pages.

Search URLs are the site's own (``/jobs?q=<text>&l=<place>&radius=&fromage=&start=``;
``l=Remote`` is Indeed's US-wide remote location). Job pages publish a schema.org
JobPosting with the full description. Apply controls are recorded, never clicked:
"Apply now" is Indeed's hosted apply flow, so it is not an employer application URL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from interviewmaxxing_core import ListingStatus, SourceSearchState, WorkArrangement

from ..text import (
    clean,
    compensation_from_schema,
    country_level_region,
    employer_key_from_url,
    html_to_text,
    indeed_job_url,
    is_host,
    parse_arrangement,
    parse_compensation,
    pay_segment,
    split_indeed_location,
)
from .base import (
    PLAIN,
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
    plan_note,
    posting_address,
    posting_org,
    posting_remote_region,
)

NAME = "indeed"
HOME = "https://www.indeed.com/"
PAGE_SIZE = 10
LOGIN_PATHS = ("/account/login", "/auth")
_FROMAGE = (1, 3, 7, 14)
_US_NAMES = {"united states", "united states of america", "usa", "us"}


def search_url(leg: SearchLeg, *, start: int = 0, posted_within_days: int | None = None) -> str | None:
    if leg.target == "remote":
        if leg.location.strip().lower() not in _US_NAMES:
            return None
        where = "Remote"
    else:
        where = leg.location
    params = {"q": leg.text, "l": where}
    if leg.radius_miles and leg.target == "onsite":
        params["radius"] = str(leg.radius_miles)
    if posted_within_days:
        allowed = [d for d in _FROMAGE if d >= posted_within_days]
        if allowed:
            params["fromage"] = str(allowed[0])
    if start:
        params["start"] = str(start)
    return "https://www.indeed.com/jobs?" + urlencode(params)


@dataclass
class Card:
    jk: str
    title: str
    company: str | None = None
    location: str | None = None
    arrangement: WorkArrangement = WorkArrangement.UNKNOWN
    salary: str | None = None
    attributes: list[str] = field(default_factory=list)
    seen_on: str = HOME
    """The results page the card was read from (provenance, not identity)."""


def parse_cards(payload: dict[str, Any]) -> list[Card]:
    seen_on = str((payload.get("page") or {}).get("url") or HOME)
    cards = []
    for item in payload.get("cards") or []:
        jk, title = clean(item.get("jk")), clean(item.get("title"))
        if not (jk and title):
            continue
        location, arrangement = split_indeed_location(item.get("location"))
        attributes = [a for a in (clean(x) for x in item.get("attributes") or []) if a]
        if arrangement is WorkArrangement.UNKNOWN:
            arrangement = next((parse_arrangement(a) for a in attributes
                                if a in ("Remote", "Hybrid work", "Hybrid", "On-site", "In-person")),
                               WorkArrangement.UNKNOWN)
        cards.append(Card(jk=jk, title=title, company=clean(item.get("company")), location=location,
                          arrangement=arrangement, salary=clean(item.get("salary")),
                          attributes=attributes, seen_on=seen_on))
    return cards


def card_observation(card: Card, *, observed_at: datetime, query_id: str | None, leg: str) -> Observation:
    listing = make_listing(
        source=NAME, source_listing_id=card.jk, posting_url=indeed_job_url(card.jk),
        source_url=card.seen_on, title=card.title, company=card.company, location=card.location,
        work_arrangement=card.arrangement,
        compensation=parse_compensation(card.salary, dollar_currency="USD"),
        observed_at=observed_at, query_id=query_id,
        evidence="Indeed search result card; job key from the result link",
    )
    return Observation(listing, {"kind": "indeed.card", "leg": leg, "attributes": card.attributes})


def detail_observation(payload: dict[str, Any], jk: str, *, observed_at: datetime,
                       query_id: str | None, leg: str, card: Card | None = None) -> Observation:
    posting = payload.get("posting") if isinstance(payload.get("posting"), dict) else None
    header = [clean(x) or "" for x in payload.get("header_lines") or []]
    detail_lines = [clean(x) or "" for x in payload.get("detail_lines") or []]
    title = clean((posting or {}).get("title")) or (header[0] if header else None) \
        or (card.title if card else None)
    if not title:
        raise ValueError(f"Indeed job {jk} page showed no title")
    pay_text = next((seg for seg in (pay_segment(x) for x in header + detail_lines) if seg), None) \
        or (card.salary if card else None)
    compensation = compensation_from_schema((posting or {}).get("baseSalary"), pay_text) \
        or parse_compensation(pay_text, dollar_currency="USD")
    arrangement = card.arrangement if card else WorkArrangement.UNKNOWN
    if arrangement is WorkArrangement.UNKNOWN:
        labels = [x for x in header + detail_lines if x in ("Remote", "Hybrid work", "Hybrid", "On-site", "In-person")]
        arrangement = parse_arrangement(labels[0]) if len(set(labels)) == 1 else WorkArrangement.UNKNOWN
    if arrangement is WorkArrangement.UNKNOWN and is_telecommute(posting):
        arrangement = WorkArrangement.REMOTE
    location = (card.location if card else None) or posting_address(posting)
    description = html_to_text((posting or {}).get("description"))
    if description is None and not payload.get("expired"):
        # An expired page shows a notice where the description was; that is not one.
        description = payload.get("description_text") or None
    apply_links = payload.get("apply_links") or []
    application_url = next((x["href"] for x in apply_links
                            if x.get("href") and not is_host(x["href"], "indeed.com")), None)
    if payload.get("expired") or expired((posting or {}).get("validThrough"), observed_at):
        status = ListingStatus.CLOSED
    elif apply_links:
        status = ListingStatus.OPEN
    else:
        status = ListingStatus.UNKNOWN
    remote_region = posting_remote_region(posting)
    if remote_region is None and arrangement is WorkArrangement.REMOTE:
        remote_region = country_level_region(location)
    listing = make_listing(
        source=NAME, source_listing_id=jk, posting_url=indeed_job_url(jk),
        source_url=indeed_job_url(jk), title=title,
        company=posting_org(posting) or (card.company if card else None), location=location,
        work_arrangement=arrangement, remote_eligibility=remote_region,
        compensation=compensation, description=description, full_description=True,
        status=status, posted_text=clean((posting or {}).get("datePosted")),
        application_url=application_url, employer_job_key=employer_key_from_url(application_url),
        employer_key_url=application_url, observed_at=observed_at, query_id=query_id,
        evidence="Indeed job page" + (" with schema.org JobPosting" if posting else ""),
    )
    raw = {"kind": "indeed.detail", "leg": leg,
           "apply_controls": [{"text": x.get("text"), "href": x.get("href")} for x in apply_links],
           **{k: (posting or {}).get(k) for k in ("datePosted", "validThrough", "employmentType",
                                                  "jobLocationType", "directApply")}}
    return Observation(listing, raw)


class IndeedAdapter:
    name = NAME

    def search(self, ctx: SearchContext) -> SourceOutcome:
        legs = build_legs(ctx.query)
        planned: list[tuple[Card, SearchLeg]] = []
        seen: set[str] = set()
        pages = 0
        more_available = False
        notes: list[str] = [n for n in (plan_note(ctx.query, PLAIN),) if n]
        plan = BudgetPlan(ctx.limit, legs, ctx.query.location_priority)
        for index, leg in enumerate(legs):
            budget = plan.budget(index)
            taken = 0
            for page in range(ctx.max_pages_per_leg):
                url = search_url(leg, start=page * PAGE_SIZE,
                                 posted_within_days=ctx.query.posted_within_days)
                if url is None:
                    notes.append(f"skipped {leg.label}: Indeed US remote search covers the United States only")
                    break
                if taken >= budget:
                    more_available = True
                    break
                ctx.transport.open(ctx.session, url)
                ctx.transport.wait_for(
                    ctx.session, '.job_seen_beacon, [data-testid="noResultsMessage"]', 15000)
                payload = ctx.evaluate("indeed_search")
                pages += 1
                check_access("Indeed", payload, ctx, login_paths=LOGIN_PATHS, home_url=HOME)
                cards = parse_cards(payload)
                if not cards and not payload.get("no_results") and page == 0:
                    raise ValueError(f"no Indeed result list for {leg.label}")
                for card in cards:
                    if card.jk in seen:
                        continue
                    if taken >= budget:
                        more_available = True
                        break
                    seen.add(card.jk)
                    planned.append((card, leg))
                    taken += 1
                if not cards or not payload.get("next_href"):
                    break
            else:
                more_available = True
            plan.spent(index, taken)

        observations: list[Observation] = []
        problems: list[str] = []
        for index, (card, leg) in enumerate(planned):
            label = f"{leg.target}:{leg.label}"
            if index < ctx.detail_limit:
                try:
                    observations.append(self._detail(ctx, card, label))
                    pages += 1
                    continue
                except AccessProblem:
                    raise
                except Exception as exc:
                    problems.append(f"job {card.jk}: {exc}")
            observations.append(card_observation(card, observed_at=ctx.clock(),
                                                 query_id=ctx.query.id, leg=label))
        if problems:
            notes.append("detail pages failed: " + "; ".join(problems[:5]))
        if more_available:
            notes.append(f"stopped at the limit of {ctx.limit} listings")
        state = SourceSearchState.PARTIAL if (more_available or problems) else SourceSearchState.OK
        return SourceOutcome(state, observations, pages, "; ".join(notes) or None)

    def _detail(self, ctx: SearchContext, card: Card, leg: str) -> Observation:
        ctx.transport.open(ctx.session, indeed_job_url(card.jk))
        ctx.transport.wait_for(ctx.session, '[data-testid="viewjob-main-content"], #jobDescriptionText, h1', 15000)
        payload = ctx.evaluate("indeed_detail")
        check_access("Indeed", payload, ctx, login_paths=LOGIN_PATHS, home_url=HOME)
        return detail_observation(payload, card.jk, observed_at=ctx.clock(), query_id=ctx.query.id,
                                  leg=leg, card=card)
