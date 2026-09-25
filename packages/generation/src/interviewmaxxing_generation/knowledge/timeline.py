"""Years of experience derived from the resume experience timeline.

Screeners ask "How many years of paid media experience do you have?"; the resume states
dated roles, not totals. This module derives them deterministically: the total years
across all dated roles (overlapping periods merged) and, per area, the years of the roles
whose resume bullets, title or linked story name that area. Every duration is rounded
down to whole years and never exceeds the timeline; areas under a full year yield no
fact, so the question holds rather than answering "0". Facts carry the provenance
``derived:experience_timeline``, deterministic ids and the key convention the factual
resolver already reads (``years_experience`` and ``years_experience.<area>``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from interviewmaxxing_core import (
    CandidateFact,
    CandidateProfile,
    FactVerification,
    VerificationMethod,
    VerificationStatus,
)

from .stories import StoryRoleLink, find_skills, find_tools, resume_roles

DERIVED_SOURCE = "derived:experience_timeline"
TOTAL_KEY = "years_experience"
AREA_PREFIX = "years_experience."
_DATE = re.compile(r"^(\d{4})(?:-(\d{2}))?$")
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


@dataclass(frozen=True)
class RoleSpan:
    role_id: str
    company: str
    start: int
    """Months since year 0 of the first month."""
    end: int
    """Months since year 0 of the last month (inclusive)."""
    areas: frozenset[str]

    @property
    def months(self) -> int:
        return self.end - self.start + 1


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


def role_spans(profile: CandidateProfile, *, today: date,
               story_links: Mapping[str, StoryRoleLink] | None = None,
               story_areas: Mapping[str, Sequence[str]] | None = None) -> list[RoleSpan]:
    """Dated resume roles with the areas each names: its title and verified bullets,
    plus the tools and skills of the stories linked to it."""
    linked: dict[str, set[str]] = {}
    for story_id, link in (story_links or {}).items():
        linked.setdefault(link.resume_role_id, set()).update((story_areas or {}).get(story_id, ()))
    # Story facts already in the profile name their resume role in their evidence, so
    # every indexed stories source counts, whichever run derived the years.
    for fact in profile.verified_facts():
        if not fact.source.startswith("story:") or not isinstance(fact.value, str):
            continue
        role_id = next((line[len("resume_role_id: "):] for line in fact.evidence
                        if line.startswith("resume_role_id: ")), None)
        if role_id:
            linked.setdefault(role_id, set()).update(find_skills(fact.value), find_tools(fact.value))
    spans = []
    now = today.year * 12 + (today.month - 1)
    for role in resume_roles(profile):
        start = _month_index(role.start)
        end = now if role.current and not role.end else _month_index(role.end)
        if start is None or end is None or end < start:
            continue
        text = " ".join([role.title, *role.bullets])
        areas = set(find_skills(text)) | set(find_tools(text)) | linked.get(role.id, set())
        spans.append(RoleSpan(role.id, role.company, start, min(end, now),
                              frozenset(with_families(areas))))
    return spans


def _describe(spans: Sequence[RoleSpan]) -> str:
    def month(index: int) -> str:
        return f"{index // 12:04d}-{index % 12 + 1:02d}"
    return "; ".join(f"{span.company} ({month(span.start)} to {month(span.end)})"
                     for span in sorted(spans, key=lambda s: s.start))


def derive_experience_years(profile: CandidateProfile, *, today: date, verified_at: datetime,
                            story_links: Mapping[str, StoryRoleLink] | None = None,
                            story_areas: Mapping[str, Sequence[str]] | None = None,
                            ) -> list[CandidateFact]:
    """Whole years of experience in total and per area, rounded down; nothing under a
    full year, nothing above the timeline."""
    spans = role_spans(profile, today=today, story_links=story_links, story_areas=story_areas)
    if not spans:
        return []
    verification = FactVerification(status=VerificationStatus.VERIFIED,
                                    method=VerificationMethod.USER_CONFIRMED, verified_at=verified_at)
    facts: list[CandidateFact] = []
    total_months = merged_months([(span.start, span.end) for span in spans])
    total_years = total_months // 12
    if total_years >= 1:
        facts.append(CandidateFact(
            id="derived_" + TOTAL_KEY, key=TOTAL_KEY, value=total_years, source=DERIVED_SOURCE,
            verification=verification, evidence=[
                f"Derived from the resume experience timeline: {len(spans)} dated roles, "
                f"{total_months} months with overlaps merged, rounded down to whole years",
                "roles: " + _describe(spans)]))
    areas = sorted({area for span in spans for area in span.areas}, key=str.casefold)
    for area in areas:
        named = [span for span in spans if area in span.areas]
        months = merged_months([(span.start, span.end) for span in named])
        years = min(months // 12, total_years)
        if years < 1:
            continue
        slug = area_slug(area)
        if not slug:
            continue
        facts.append(CandidateFact(
            id="derived_" + TOTAL_KEY + "_" + slug, key=AREA_PREFIX + slug, value=years,
            source=DERIVED_SOURCE, verification=verification, evidence=[
                f"Years of {area} experience derived from the resume roles that name it: "
                f"{months} months with overlaps merged, rounded down to whole years",
                "roles: " + _describe(named)]))
    return facts


__all__ = ["AREA_FAMILIES", "AREA_PREFIX", "DERIVED_SOURCE", "TOTAL_KEY", "RoleSpan", "area_slug",
           "derive_experience_years", "merged_months", "role_spans", "with_families"]
