"""Google Jobs (the jobs view of an ordinary Google search, ``udm=8``).

The query is written the way a person searches ("marketing manager jobs in Austin,
TX"). Results are Google's aggregation of postings hosted elsewhere. Each listing is
identified by Google's own job document id; the results page it was read from is
provenance only. Apply options that point at LinkedIn/Indeed/Built In postings are
kept as raw evidence (``linked_postings``) but never merge listings: under D0R2 a
cross-source merge needs an ``employer_job_key``, which is set only when an apply
option is a job-specific employer ATS posting (e.g. Greenhouse ``/<board>/jobs/<id>``).

Selecting a result is a structured click in the results list; apply options are
recorded, never opened.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from interviewmaxxing_core import SourceSearchState, WorkArrangement

from ..text import (
    clean,
    employer_key_from_url,
    known_source_ref,
    parse_arrangement,
    parse_compensation,
    pay_segment,
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
    make_listing,
)

NAME = "google"
HOME = "https://www.google.com/"
_POSTED = re.compile(r"\b(?:ago|yesterday|today|just posted)\b", re.I)
_DESCRIPTION_END = ("Show full description", "Report this listing", "Show less")


def search_text(leg: SearchLeg) -> str:
    if leg.target == "remote":
        return f"remote {leg.text} jobs {leg.location}"
    return f"{leg.text} jobs in {leg.location}"


def search_url(leg: SearchLeg) -> str:
    return "https://www.google.com/search?" + urlencode({"q": search_text(leg), "udm": "8"})


def result_url(doc_id: str, query_text: str) -> str:
    return (f"https://www.google.com/search?ibp=htl;jobs&q={quote(query_text)}"
            f"&htidocid={quote(doc_id, safe='')}")


@dataclass
class Item:
    index: int
    doc_id: str
    title: str
    company: str | None = None
    location: str | None = None
    via: str | None = None
    posted: str | None = None
    pay_text: str | None = None
    arrangement: WorkArrangement = WorkArrangement.UNKNOWN
    chips: list[str] = field(default_factory=list)
    seen_on: str = HOME
    """The results page the item was read from (provenance, not identity)."""


def parse_items(payload: dict[str, Any]) -> list[Item]:
    seen_on = str((payload.get("page") or {}).get("url") or HOME)
    items: list[Item] = []
    for raw in payload.get("items") or []:
        lines = [x for x in (clean(v) for v in raw.get("lines") or []) if x]
        doc_id = clean(raw.get("doc_id"))
        if not doc_id or len(lines) < 2:
            continue
        item = Item(index=int(raw.get("index", len(items))), doc_id=doc_id, title=lines[0],
                    company=lines[1], seen_on=seen_on)
        rest = lines[2:]
        if rest and (" • " in rest[0] or rest[0].startswith("via ")):
            where, _, via = rest.pop(0).rpartition(" • ")
            item.location = clean(where) or None
            item.via = clean(via.removeprefix("via "))
        for chip in rest:
            if item.posted is None and _POSTED.search(chip):
                item.posted = chip
            elif item.pay_text is None and pay_segment(chip):
                item.pay_text = pay_segment(chip)
            elif chip.lower() == "work from home":
                item.arrangement = WorkArrangement.REMOTE
            else:
                item.chips.append(chip)
        items.append(item)
    return items


def item_observation(item: Item, query_text: str, *, observed_at: datetime, query_id: str | None,
                     leg: str, detail: dict[str, Any] | None = None) -> Observation:
    options: list[dict[str, str | None]] = []
    linked: list[dict[str, str]] = []
    ats_url: str | None = None
    ats_key: str | None = None
    description = None
    partial = True
    arrangement = item.arrangement
    if detail:
        text = str(detail.get("text") or "")
        lines = [ln.strip() for ln in text.splitlines()]
        if "Job description" in lines:
            start = lines.index("Job description") + 1
            body: list[str] = []
            for line in lines[start:]:
                if line in _DESCRIPTION_END:
                    break
                body.append(line)
            description = "\n".join(body).strip() or None
            partial = "Show full description" in lines[start:]
        if arrangement is WorkArrangement.UNKNOWN and "Work from home" in lines:
            arrangement = parse_arrangement("Work from home")
        for link in detail.get("apply_links") or []:
            href, label = link.get("href"), clean(link.get("text"))
            options.append({"text": label, "host": urlsplit(href or "").hostname})
            ref = known_source_ref(href)
            if ref:
                linked.append({"source": ref[0], "source_listing_id": ref[1], "posting_url": ref[2],
                               "label": label or ""})
            key = employer_key_from_url(href)
            if key and ats_key is None:
                ats_key, ats_url = key, href
    listing = make_listing(
        source=NAME, source_listing_id=item.doc_id, posting_url=result_url(item.doc_id, query_text),
        source_url=item.seen_on, application_url=ats_url, employer_job_key=ats_key,
        employer_key_url=ats_url,
        title=item.title, company=item.company, location=item.location,
        work_arrangement=arrangement, compensation=parse_compensation(item.pay_text),
        description=description, full_description=not partial, posted_text=item.posted,
        observed_at=observed_at, query_id=query_id,
        evidence="Google Jobs result" + (" and detail pane" if detail else "")
                 + (f" (via {item.via})" if item.via else ""),
    )
    return Observation(listing, {"kind": "google.result", "leg": leg, "via": item.via,
                                 "chips": item.chips, "apply_options": options,
                                 "linked_postings": linked})


class GoogleAdapter:
    name = NAME

    def search(self, ctx: SearchContext) -> SourceOutcome:
        legs = build_legs(ctx.query)
        observations: list[Observation] = []
        seen: set[str] = set()
        pages = 0
        details_left = ctx.detail_limit
        more_available = False
        problems: list[str] = []
        plan = BudgetPlan(ctx.limit, legs, ctx.query.location_priority)
        for index, leg in enumerate(legs):
            budget = plan.budget(index)
            if budget <= 0:
                more_available = True
                continue
            text = search_text(leg)
            ctx.transport.open(ctx.session, search_url(leg))
            ctx.transport.wait_for(ctx.session, "[data-share-url]", 15000)
            payload = ctx.evaluate("google_search")
            pages += 1
            check_access("Google", payload, ctx, home_url=HOME)
            items = parse_items(payload)
            taken = 0
            for item in items:
                if item.doc_id in seen:
                    continue
                if taken >= budget:
                    more_available = True
                    break
                seen.add(item.doc_id)
                taken += 1
                detail = None
                if details_left > 0:
                    details_left -= 1
                    try:
                        detail = self._detail(ctx, item)
                    except AccessProblem:
                        raise
                    except Exception as exc:
                        problems.append(f"result {item.doc_id}: {exc}")
                observations.append(item_observation(
                    item, text, observed_at=ctx.clock(), query_id=ctx.query.id,
                    leg=leg.label, detail=detail))
            plan.spent(index, taken)
        notes = ["Google shows only its first results page here"]
        if ctx.query.posted_within_days:
            notes.append("posted_within_days is not applied on Google")
        if problems:
            notes.append("detail panes failed: " + "; ".join(problems[:5]))
        if more_available:
            notes.append(f"stopped at the limit of {ctx.limit} listings")
        state = SourceSearchState.PARTIAL if (more_available or problems) else SourceSearchState.OK
        return SourceOutcome(state, observations, pages, "; ".join(notes))

    def _detail(self, ctx: SearchContext, item: Item) -> dict[str, Any]:
        ctx.transport.click(ctx.session, "[data-share-url]", nth=item.index)
        for _ in range(2):
            ctx.transport.pause(ctx.session, ctx.page_pause_s)
            payload = ctx.evaluate("google_detail")
            check_access("Google", payload, ctx, home_url=HOME)
            active = payload.get("active")
            if isinstance(active, dict) and clean(active.get("heading")) == item.title:
                return active
        raise ValueError("the detail pane did not show the selected result")
