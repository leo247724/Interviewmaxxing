"""LinkedIn Jobs through the visible search UI and job pages.

The installed ``opencli linkedin search`` adapter replays LinkedIn's internal Voyager
API; this adapter deliberately does not. It navigates the normal search URL (the
same parameters the filter UI writes: ``keywords``, ``location``, ``f_WT`` work type,
``distance``, ``f_TPR``, ``start``) and reads the rendered page.

Observed limitation (2026-09-22): in a background window the tab is
``document.hidden``, so LinkedIn renders only the first few result cards and does
not render the job description at all. Unrendered cards are read from their job
page's top card when the detail budget allows; descriptions stay NONE unless the
page rendered one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from interviewmaxxing_core import ListingStatus, SourceSearchState, WorkArrangement

from ..text import (
    clean,
    decode_linkedin_redirect,
    employer_key_from_url,
    is_host,
    known_source_ref,
    linkedin_job_url,
    parse_arrangement,
    parse_compensation,
    pay_segment,
    split_linkedin_caption,
    stated_remote_region,
)
from .base import (
    AccessGuard,
    AccessProblem,
    BudgetPlan,
    Observation,
    PhraseSyntax,
    SearchContext,
    SearchLeg,
    SourceOutcome,
    build_legs,
    check_access,
    interrupted,
    make_listing,
    plan_note,
)

NAME = "linkedin"
HOME = "https://www.linkedin.com/jobs/"
PAGE_SIZE = 25
LOGIN_PATHS = ("/authwall", "/login", "/checkpoint", "/uas/", "/signup")
SYNTAX = PhraseSyntax(batch_size=4, style="grouped_or")
"""LinkedIn honours ``(a) OR (b)`` in the keyword box and stays semantic."""
_WORK_TYPE = {WorkArrangement.ONSITE: "1", WorkArrangement.REMOTE: "2", WorkArrangement.HYBRID: "3"}
_TOP_STOP = {
    "Apply", "Easy Apply", "Save", "Saved", "Unsave", "Take the next step in your job search",
    "Looking for talent?", "Use AI to assess how you fit", "Application status",
    "Show match details", "About the job", "Tailor my resume",
}
_BADGES = {"Applied", "Application submitted", "Viewed", "Saved", "Promoted",
           "Actively reviewing applicants", "Be an early applicant"}


def search_url(leg: SearchLeg, *, start: int = 0, posted_within_days: int | None = None) -> str:
    params: dict[str, str] = {"keywords": leg.text, "location": leg.location}
    params["f_WT"] = ",".join(_WORK_TYPE[a] for a in leg.arrangements)
    if leg.radius_miles:
        params["distance"] = str(leg.radius_miles)
    if posted_within_days:
        params["f_TPR"] = f"r{posted_within_days * 86400}"
    if start:
        params["start"] = str(start)
    return "https://www.linkedin.com/jobs/search/?" + urlencode(params)


@dataclass
class Card:
    id: str
    rendered: bool
    title: str | None = None
    company: str | None = None
    location: str | None = None
    arrangement: WorkArrangement = WorkArrangement.UNKNOWN
    pay_text: str | None = None
    badges: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    seen_on: str = HOME
    """The results page the card was read from (provenance, not identity)."""


def parse_cards(payload: dict[str, Any]) -> list[Card]:
    seen_on = str((payload.get("page") or {}).get("url") or HOME)
    cards: list[Card] = []
    for item in payload.get("cards") or []:
        job_id = clean(item.get("id"))
        if not job_id or not job_id.isdigit():
            continue
        title = clean(item.get("title"))
        if not title:
            label = clean(item.get("title_label"))
            title = label.removesuffix(" with verification") if label else None
        location, arrangement = split_linkedin_caption(item.get("caption"))
        rendered = bool(item.get("rendered")) and bool(title)
        cards.append(Card(
            id=job_id, rendered=rendered, title=title, company=clean(item.get("company")),
            location=location, arrangement=arrangement,
            pay_text=pay_segment(item.get("metadata")),
            badges=[b for b in (clean(x) for x in item.get("footer") or []) if b],
            raw={k: item.get(k) for k in ("caption", "metadata", "footer", "title_label")},
            seen_on=seen_on,
        ))
    return cards


def card_observation(card: Card, *, observed_at: datetime, query_id: str | None, leg: str) -> Observation:
    if not card.title:
        raise ValueError(f"LinkedIn card {card.id} has no rendered title")
    listing = make_listing(
        source=NAME, source_listing_id=card.id, posting_url=linkedin_job_url(card.id),
        source_url=card.seen_on, title=card.title, company=card.company, location=card.location,
        work_arrangement=card.arrangement,
        remote_eligibility=stated_remote_region(card.location)
        if card.arrangement is WorkArrangement.REMOTE else None,
        compensation=parse_compensation(card.pay_text, dollar_currency="USD"),
        observed_at=observed_at, query_id=query_id,
        evidence="LinkedIn search result card; job id from the result's data attribute",
    )
    return Observation(listing, {"kind": "linkedin.card", "leg": leg, "badges": card.badges, **card.raw})


def detail_observation(payload: dict[str, Any], job_id: str, *, observed_at: datetime,
                       query_id: str | None, leg: str, card: Card | None = None) -> Observation:
    """A listing from a /jobs/view/<id>/ page, falling back to card values."""
    lines = [clean(x) or "" for x in payload.get("top_lines") or []]
    top: list[str] = []
    for line in lines:
        if line in _TOP_STOP:
            break
        top.append(line)
    badges = [line for line in lines if line in _BADGES]
    company = top[0] if len(top) > 0 else None
    title = top[1] if len(top) > 1 else None
    doc = clean(payload.get("document_title")) or ""
    if doc.endswith(" | LinkedIn"):
        body = doc.removesuffix(" | LinkedIn")
        if company and body.endswith(f" | {company}"):
            title = body.removesuffix(f" | {company}")
        elif not title and " | " in body:
            title, company = body.rsplit(" | ", 1)
    location = posted = None
    if len(top) > 2:
        parts = [p.strip() for p in top[2].split("·")]
        location = parts[0] or None
        posted = next((p for p in parts[1:] if "ago" in p or p.lower().startswith("reposted")), None)
    rest = top[3:]
    pay = next((seg for seg in (pay_segment(line) for line in rest) if seg), None)
    arrangement = next((parse_arrangement(line) for line in rest
                        if line in ("Remote", "Hybrid", "On-site")), WorkArrangement.UNKNOWN)

    if card is not None:
        title = title or card.title
        company = company or card.company
        location = location or card.location
        pay = pay or card.pay_text
        if arrangement is WorkArrangement.UNKNOWN:
            arrangement = card.arrangement
    if not title:
        raise ValueError(f"LinkedIn job {job_id} page showed no title")

    application_url = None
    apply_links = payload.get("apply_links") or []
    for link in apply_links:
        target = decode_linkedin_redirect(link.get("href"))
        if target and not is_host(target, "linkedin.com"):
            application_url = target
            break
    if payload.get("closed"):
        status = ListingStatus.CLOSED
    elif apply_links or payload.get("easy_apply"):
        status = ListingStatus.OPEN
    else:
        status = ListingStatus.UNKNOWN

    description = payload.get("description")
    if isinstance(description, str):
        description = description.strip().removesuffix("… more").removesuffix("…more").strip()
    evidence = "LinkedIn job page top card"
    if description:
        evidence += " and description"
    else:
        evidence += "; description not rendered (background window)"
    if application_url:
        evidence += "; external apply link decoded from LinkedIn's redirect"
    listing = make_listing(
        source=NAME, source_listing_id=job_id, posting_url=linkedin_job_url(job_id),
        source_url=linkedin_job_url(job_id), title=title, company=company, location=location, work_arrangement=arrangement,
        remote_eligibility=stated_remote_region(location)
        if arrangement is WorkArrangement.REMOTE else None,
        compensation=parse_compensation(pay, dollar_currency="USD"),
        description=description if isinstance(description, str) else None,
        full_description=True, status=status, posted_text=posted,
        application_url=application_url, employer_job_key=employer_key_from_url(application_url),
        employer_key_url=application_url, observed_at=observed_at, query_id=query_id,
        evidence=evidence,
    )
    raw = {"kind": "linkedin.detail", "leg": leg, "badges": badges, "top_lines": top,
           "easy_apply": bool(payload.get("easy_apply")),
           "apply_links": [decode_linkedin_redirect(x.get("href")) for x in apply_links]}
    return Observation(listing, raw)


class LinkedInAdapter:
    name = NAME

    def search(self, ctx: SearchContext) -> SourceOutcome:
        legs = build_legs(ctx.query, SYNTAX)
        planned: list[tuple[Card, SearchLeg]] = []
        seen: set[str] = set()
        reserved = 0
        pages = 0
        more_available = False
        skipped_unrendered = 0
        plan = BudgetPlan(ctx.limit, legs, ctx.query.location_priority)
        guard = AccessGuard()
        with guard:
            for index, leg in enumerate(legs):
                budget = plan.budget(index)
                taken = 0
                for page in range(ctx.max_pages_per_leg):
                    if taken >= budget:
                        more_available = True
                        break
                    payload = self._open(ctx, search_url(
                        leg, start=page * PAGE_SIZE, posted_within_days=ctx.query.posted_within_days))
                    pages += 1
                    cards = parse_cards(payload)
                    if not cards:
                        if payload.get("no_results") or page > 0:
                            break
                        raise ValueError(f"no LinkedIn result list for {leg.label}")
                    for card in cards:
                        if card.id in seen:
                            continue
                        if taken >= budget:
                            more_available = True
                            break
                        if not card.rendered:
                            if reserved >= ctx.detail_limit:
                                skipped_unrendered += 1
                                more_available = True
                                continue
                            reserved += 1
                        seen.add(card.id)
                        planned.append((card, leg))
                        taken += 1
                    if not payload.get("has_next"):
                        break
                else:
                    more_available = True
                plan.spent(index, taken)

        observations: list[Observation] = []
        problems: list[str] = []
        detail_slots = ctx.detail_limit
        ordered = [p for p in planned if not p[0].rendered] + [p for p in planned if p[0].rendered]
        details: dict[str, Observation] = {}
        for card, leg in ordered:
            if detail_slots <= 0 or guard.problem is not None:
                break
            detail_slots -= 1
            try:
                details[card.id] = self._detail(ctx, card, leg)
                pages += 1
            except AccessProblem as problem:
                guard.problem = problem  # job pages now need the user; keep the cards
                break
            except Exception as exc:  # one broken page should not lose the search
                problems.append(f"job {card.id}: {exc}")
        for card, leg in planned:
            if card.id in details:
                observations.append(details[card.id])
            elif card.rendered:
                observations.append(card_observation(card, observed_at=ctx.clock(),
                                                     query_id=ctx.query.id, leg=leg.label))
        notes = [n for n in (plan_note(ctx.query, SYNTAX),) if n]
        if skipped_unrendered:
            notes.append(f"{skipped_unrendered} result cards were not rendered in the background "
                         "window and exceeded the detail budget")
        if problems:
            notes.append("detail pages failed: " + "; ".join(problems[:5]))
        if more_available:
            notes.append(f"stopped at the limit of {ctx.limit} listings")
        state = SourceSearchState.PARTIAL if (more_available or problems or skipped_unrendered) \
            else SourceSearchState.OK
        outcome = SourceOutcome(state, observations, pages, "; ".join(notes) or None)
        return interrupted(outcome, guard.problem) if guard.problem else outcome

    def _open(self, ctx: SearchContext, url: str) -> dict[str, Any]:
        ctx.transport.open(ctx.session, url)
        ctx.transport.wait_for(
            ctx.session, "li[data-occludable-job-id], .jobs-search-no-results-banner", 15000)
        payload = ctx.evaluate("linkedin_search")
        check_access("LinkedIn", payload, ctx, login_paths=LOGIN_PATHS, home_url=HOME)
        return payload

    def _detail(self, ctx: SearchContext, card: Card, leg: SearchLeg) -> Observation:
        ctx.transport.open(ctx.session, linkedin_job_url(card.id))
        payload: dict[str, Any] = {}
        for _ in range(2):
            ctx.transport.pause(ctx.session, ctx.page_pause_s * 2)
            payload = ctx.evaluate("linkedin_detail")
            check_access("LinkedIn", payload, ctx, login_paths=LOGIN_PATHS, home_url=HOME)
            if len(payload.get("top_lines") or []) >= 2:
                break
        # The page must be this job: a redirect or a stale tab would otherwise enrich
        # the card with another posting's company, description and apply link.
        ref = known_source_ref(str((payload.get("page") or {}).get("url") or ""))
        if ref is None or ref[0] != NAME or ref[1] != card.id:
            shown = ref[1] if ref else "no job id"
            raise ValueError(f"job page showed {shown} instead; kept the search card only")
        return detail_observation(payload, card.id, observed_at=ctx.clock(),
                                  query_id=ctx.query.id, leg=leg.label, card=card)
