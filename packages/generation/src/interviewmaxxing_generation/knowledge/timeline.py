"""Years of experience derived from the resume experience timeline.

Screeners ask "How many years of paid media experience do you have?"; the resume states
dated roles, not totals. This module derives them deterministically: the total years
across all dated roles (overlapping periods merged) and, per area, the years of the roles
whose title names the area (the whole role) plus the bullets that state their own
duration ("ran paid social for 18 months": that duration, never the whole role). A bullet
that merely mentions an area ("piloted TikTok Ads in Q4") dates nothing. Every duration is
rounded down to whole years and never exceeds the timeline; an area under a full year
yields no fact, so the question holds rather than answering "0".

The total only restates the person's confirmed role dates, so it is VERIFIED
(``USER_CONFIRMED``); each per-area fact is an extraction and is written UNVERIFIED until
the person confirms it through the facts import (CONTRACTS 3: a verification is the
person's, never a run clock). Facts carry the provenance ``derived:experience_timeline``,
deterministic ids and the key convention the factual resolver already reads
(``years_experience`` and ``years_experience.<area>``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from interviewmaxxing_core import (
    CandidateFact,
    CandidateProfile,
    FactVerification,
    VerificationMethod,
    VerificationStatus,
)

from .stories import _WORD_NUMBERS, find_skills, find_tools, resume_roles

DERIVED_SOURCE = "derived:experience_timeline"
TOTAL_KEY = "years_experience"
AREA_PREFIX = "years_experience."
CONFIRMATION_NOTE = "Unverified until confirmed through the facts import (scripts/rag_answers.py import-facts)"
_DATE = re.compile(r"^(\d{4})(?:-(\d{2}))?$")
_STATED_DURATION = re.compile(
    r"\b(?:(?P<qual>over|more than|almost|nearly|about|around|approximately|roughly|under|less than)\s+)?"
    r"(?:(?P<n>\d+(?:\.\d+)?|" + "|".join(_WORD_NUMBERS) + r")\+?|(?P<article>an?))\s*"
    r"(?P<unit>years?|yrs?|months?)\b", re.IGNORECASE)
_STATED_RANGE = re.compile(r"\b((?:19|20)\d{2})\s*(?:-|\u2013|\u2014|to|through|until)\s*((?:19|20)\d{2}|present|now|today)\b",
                           re.IGNORECASE)
AREA_FAMILIES: dict[str, frozenset[str]] = {
    # A role that names a member area is a role in the family; questions ask for the
    # family ("paid media") more often than for the product a bullet names.
    "paid search": frozenset({"Google Ads", "Microsoft Ads", "PPC", "Performance Max",
                              "negative keywords", "Quality Score optimization",
                              "responsive search ads"}),
    "paid social": frozenset({"Meta Ads", "Facebook Ads", "Instagram Ads", "LinkedIn Ads",
                              "TikTok Ads", "Reddit Ads"}),
    "paid media": frozenset({"paid search", "paid social", "Google Ads", "Microsoft Ads", "PPC",
                             "Performance Max", "Demand Gen", "YouTube", "Meta Ads", "Facebook Ads",
                             "Instagram Ads", "LinkedIn Ads", "TikTok Ads", "Reddit Ads",
                             "media buying", "performance marketing", "programmatic advertising",
                             "retargeting", "CPA optimization", "ROAS analysis"}),
    "digital marketing": frozenset({"paid media", "paid search", "paid social", "SEO",
                                    "email marketing", "content marketing", "social media marketing",
                                    "conversion tracking", "landing pages", "lead generation",
                                    "marketing automation", "demand generation", "growth marketing",
                                    "performance marketing"}),
}
"""Family areas implied by their members; evaluated in order, so a family may build on
another ("paid media" includes "paid search")."""


def with_families(areas: set[str]) -> set[str]:
    """The areas plus every family one of them belongs to."""
    expanded = set(areas)
    for family, members in AREA_FAMILIES.items():
        if expanded & members:
            expanded.add(family)
    return expanded


def stated_duration_months(text: str, *, today: date | None = None) -> int | None:
    """The duration a resume bullet states for its own work, in whole months: "18
    months" is 18, "3 years" 36, "over 2 years" 24, "about a year" 12, "2021 to 2023"
    24 (a year range counts the years between its ends); "under a year" states no
    usable duration. None when the bullet states none: a mention dates nothing."""
    months: list[int] = []
    for match in _STATED_DURATION.finditer(text):
        qual = (match.group("qual") or "").casefold()
        if qual in ("under", "less than"):
            continue
        raw = (match.group("n") or "1").casefold()
        number = _WORD_NUMBERS.get(raw) or float(raw)
        unit = match.group("unit").casefold()
        months.append(int(number * 12) if unit.startswith(("year", "yr")) else int(number))
    for match in _STATED_RANGE.finditer(text):
        start = int(match.group(1))
        end_text = match.group(2).casefold()
        end = (today or date.today()).year if end_text in ("present", "now", "today") else int(end_text)
        if end >= start:
            months.append((end - start) * 12)
    return max(months) if months else None


@dataclass(frozen=True)
class RoleSpan:
    role_id: str
    company: str
    start: int
    """Months since year 0 of the first month."""
    end: int
    """Months since year 0 of the last month (inclusive)."""
    title_areas: frozenset[str] = frozenset()
    """The areas the role's title names (with their families): the whole role."""
    bullet_areas: Mapping[str, int] = field(default_factory=dict)
    """Area -> months, from the bullets that state their own duration (with families),
    capped at the role's length."""

    @property
    def months(self) -> int:
        return self.end - self.start + 1

    @property
    def areas(self) -> frozenset[str]:
        return self.title_areas | frozenset(self.bullet_areas)


def _month_index(value: str | None) -> int | None:
    match = _DATE.match(value or "")
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2) or 1)
    if not 1 <= month <= 12:
        return None
    return year * 12 + (month - 1)


def merged_months(spans: Sequence[tuple[int, int]]) -> int:
    """Total months covered by inclusive [start, end] spans, overlaps counted once."""
    total = 0
    current: tuple[int, int] | None = None
    for start, end in sorted(spans):
        if current is None or start > current[1] + 1:
            if current is not None:
                total += current[1] - current[0] + 1
            current = (start, end)
        else:
            current = (current[0], max(current[1], end))
    if current is not None:
        total += current[1] - current[0] + 1
    return total


def area_slug(area: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", area.casefold()).strip("_")


def role_spans(profile: CandidateProfile, *, today: date) -> list[RoleSpan]:
    """Dated resume roles with the areas each dates: the title's areas for the whole
    role, and a bullet's areas for the duration the bullet itself states. Stories never
    date an area: a linked story's tools are that story's evidence, not a timeline."""
    spans = []
    now = today.year * 12 + (today.month - 1)
    for role in resume_roles(profile):
        start = _month_index(role.start)
        end = now if role.current and not role.end else _month_index(role.end)
        if start is None or end is None or end < start:
            continue
        end = min(end, now)
        length = end - start + 1
        title_areas = with_families(set(find_skills(role.title)) | set(find_tools(role.title)))
        bullet_areas: dict[str, int] = {}
        for bullet in role.bullets:
            months = stated_duration_months(bullet, today=today)
            if months is None or months < 1:
                continue
            for area in with_families(set(find_skills(bullet)) | set(find_tools(bullet))) - title_areas:
                bullet_areas[area] = max(bullet_areas.get(area, 0), min(months, length))
        spans.append(RoleSpan(role.id, role.company, start, end, frozenset(title_areas), bullet_areas))
    return spans


def _describe(spans: Sequence[RoleSpan]) -> str:
    def month(index: int) -> str:
        return f"{index // 12:04d}-{index % 12 + 1:02d}"
    return "; ".join(f"{span.company} ({month(span.start)} to {month(span.end)})"
                     for span in sorted(spans, key=lambda s: s.start))


def derive_experience_years(profile: CandidateProfile, *, today: date, verified_at: datetime) -> list[CandidateFact]:
    """Whole years of experience in total (VERIFIED: it restates the confirmed role dates)
    and per area (UNVERIFIED until the person confirms it), rounded down; nothing under a
    full year and nothing above the timeline."""
    spans = role_spans(profile, today=today)
    if not spans:
        return []
    facts: list[CandidateFact] = []
    total_months = merged_months([(span.start, span.end) for span in spans])
    total_years = total_months // 12
    if total_years >= 1:
        facts.append(CandidateFact(
            id="derived_" + TOTAL_KEY, key=TOTAL_KEY, value=total_years, source=DERIVED_SOURCE,
            verification=FactVerification(status=VerificationStatus.VERIFIED,
                                          method=VerificationMethod.USER_CONFIRMED, verified_at=verified_at),
            evidence=[
                f"Derived from the resume experience timeline: {len(spans)} dated roles, "
                f"{total_months} months with overlaps merged, rounded down to whole years",
                "roles: " + _describe(spans)]))
    areas = sorted({area for span in spans for area in span.areas}, key=str.casefold)
    for area in areas:
        titled = [span for span in spans if area in span.title_areas]
        dated = [(span, span.bullet_areas[area]) for span in spans if area in span.bullet_areas]
        months = merged_months([(span.start, span.end) for span in titled]) + sum(m for _, m in dated)
        years = min(min(months, total_months) // 12, total_years)
        if years < 1:
            continue
        slug = area_slug(area)
        if not slug:
            continue
        basis = [f"{span.company}: title, {span.months} months" for span in sorted(titled, key=lambda s: s.start)]
        basis += [f"{span.company}: a bullet stating {m} months" for span, m in sorted(dated, key=lambda item: item[0].start)]
        facts.append(CandidateFact(
            id="derived_" + TOTAL_KEY + "_" + slug, key=AREA_PREFIX + slug, value=years,
            source=DERIVED_SOURCE, verification=FactVerification(status=VerificationStatus.UNVERIFIED),
            evidence=[
                f"Years of {area} experience from the resume roles whose title names it and the "
                f"bullets that state their own duration: {months} months, rounded down to whole years",
                "basis: " + "; ".join(basis), CONFIRMATION_NOTE]))
    return facts


__all__ = ["AREA_FAMILIES", "AREA_PREFIX", "CONFIRMATION_NOTE", "DERIVED_SOURCE", "TOTAL_KEY",
           "RoleSpan", "area_slug", "derive_experience_years", "merged_months", "role_spans",
           "stated_duration_months", "with_families"]
