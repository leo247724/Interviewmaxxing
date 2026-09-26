"""Experience questions read deterministically (round 11).

The person's years facts (``years_experience`` for the total, ``years_experience.<area>``
per area) settle several screener questions without a model call, and name the platforms
a select-all question lists:

- ``prefer_stated``: a fact the person stated (``user:`` source) replaces a derived one
  (``derived:``) for the same key; the two are never averaged or compared. Since round 14
  it lives in ``interviewmaxxing_generation.resolver``, below this package, so the factual
  pass reads the facts the same way; the names are re-exported here unchanged;
- ``years_requirement``: "at least 8 years", "5+ years", "3 or more years" and the area
  the wording names, if any ("… of total experience in direct response marketing");
- ``area_facts``: the ``years_experience.<area>`` facts whose area the question names, by the
  area's own words or a synonym (Meta, Instagram and Facebook are ``meta_ads``);
- ``platforms_named``: the ad platforms an option names, and ``platform_facts``: the years
  and story facts that state the person's own work on them;
- ``experience_wording``: a question about experience, skills, platforms, years or having
  done something, which always reaches the fact screeners.

Nothing here produces a value: every answer cites facts that state it."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from interviewmaxxing_core import CandidateFact
from interviewmaxxing_generation.questions import wording_key, years_fact_area
from interviewmaxxing_generation.resolver import DERIVED_SOURCE as DERIVED_SOURCE
from interviewmaxxing_generation.resolver import USER_SOURCE as USER_SOURCE
from interviewmaxxing_generation.resolver import derived as derived
from interviewmaxxing_generation.resolver import prefer_stated as prefer_stated
from interviewmaxxing_generation.resolver import stated_by_person as stated_by_person

STORY_SOURCES = ("story:", "user:story")


def years_value(fact: CandidateFact) -> float | None:
    """The number of years a years fact states (7, 7.5, "7 years"), else None."""
    value = fact.value
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:\+|years?|yrs?)?\s*", value, re.IGNORECASE)
        return float(match.group(1)) if match else None
    return None


def total_facts(facts: Iterable[CandidateFact]) -> list[CandidateFact]:
    return [f for f in facts if years_fact_area(f.key) == "" and years_value(f) is not None]


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20}
_N = r"(\d+|" + "|".join(_NUMBER_WORDS) + r")"
_REQUIREMENT = re.compile(
    rf"\b(?P<cmp>at least|a minimum of|minimum of|min\.? of|no less than|more than|over)\s+{_N}"
    rf"\s*\+?\s*(?:years?|yrs?)\b"
    rf"|\b(?P<plus>\d+)\s*\+\s*(?:years?|yrs?)\b"
    rf"|\b(?P<more>\d+)\s+or\s+more\s+(?:years?|yrs?)\b",
    re.IGNORECASE)
_AREA_AFTER = re.compile(
    r"\b(?:years?|yrs?)\s+(?:of\s+)?(?P<pre>[^?.;:]*?)\s*\b(?:experience|exp)\b"
    r"(?:\s+(?:in|with|as|doing|managing|leading|running|across|on|of)\s+(?P<post>[^?.;:]+))?"
    r"|\b(?:years?|yrs?)\s+(?P<doing>(?:managing|leading|running|working|doing|building|owning|"
    r"in|with)\b[^?.;:]+)",
    re.IGNORECASE)
_GENERIC_AREA_WORDS = frozenset({
    "total", "professional", "relevant", "work", "working", "overall", "combined", "full",
    "time", "full-time", "hands-on", "hands", "on", "practical", "related", "industry", "of",
    "the", "a", "an", "your", "you", "have", "do", "prior", "previous"})
"""Qualifiers that do not name an area ("total experience", "relevant work experience")."""


@dataclass(frozen=True, slots=True)
class YearsRequirement:
    """A years threshold a yes/no question sets: at least ``years`` (more than, when
    ``strict``), in ``area`` when the wording names one (None: total experience)."""

    years: float
    strict: bool
    area: str | None

    def met_by(self, value: float) -> bool:
        return value > self.years if self.strict else value >= self.years


def _number(token: str) -> float:
    return float(_NUMBER_WORDS.get(token.casefold(), token))


_NOT_EXPERIENCE_YEARS = re.compile(
    r"\byears?\s+(?:old|of\s+age)\b|\bage\b|\blived\b|\bliv(?:e|ing)\b|\bresid\w*|\baddress\b|"
    r"\bcitizen\w*|\bmarried\b|\bwith\s+(?:us|this\s+company|the\s+company)\b|\bemployed\s+(?:by|at)\b",
    re.IGNORECASE)
"""A years threshold about age, residence, citizenship or tenure, never experience."""
_SECOND_NUMBER = re.compile(
    r"(?<![\w.,$\u20ac\u00a3])\d+(?:[.,]\d+)*(?![\w%])(?!\s*\+?\s*(?:years?|yrs?)\b)")
"""A number of its own beside the threshold, with no "years" after it ("…, including 2 in paid
social", "teams of 10 or more"): a second minimum. Not a number inside a word (B2B, GA4), an
amount ($1M, 50%) or another years mention ("in the past 10 years") (round 12b)."""
_EXPERIENCE_NEAR = re.compile(r"\bexperience\b|\bexp\b|\b(?:managing|leading|running|working|doing|"
                              r"building|owning|in|with)\b", re.IGNORECASE)


def years_requirement(question: str) -> YearsRequirement | None:
    """The years threshold and area of "Do you have at least 8 years of total experience in
    direct response marketing?", "5+ years of paid media experience?"; None otherwise, for a
    threshold about age, residence or tenure ("at least 18 years old", "lived at your
    address for at least 3 years"), and for a question setting two thresholds (Jev reads
    "8 years in total, including 3 in direct response")."""
    matches = list(_REQUIREMENT.finditer(question))
    if len(matches) != 1 or _NOT_EXPERIENCE_YEARS.search(question):
        return None
    match = matches[0]
    if _EXPERIENCE_NEAR.search(question[match.end():]) is None:
        return None
    if any(not match.start() <= number.start() < match.end()
           for number in _SECOND_NUMBER.finditer(question)):
        return None  # "5+ years, including 2 in paid social" sets two minimums: Jev reads it
    if match.group("cmp"):
        years = _number(match.group(2))
        strict = match.group("cmp").casefold() in ("more than", "over")
    else:
        years, strict = _number(match.group("plus") or match.group("more")), False
    area: str | None = None
    found = _AREA_AFTER.search(question[match.start():])
    if found is not None:
        words = " ".join(part for part in (found.group("pre"), found.group("post"), found.group("doing"))
                         if part)
        kept = [w for w in wording_key(words).split() if w not in _GENERIC_AREA_WORDS]
        area = " ".join(kept) or None
    return YearsRequirement(years=years, strict=strict, area=area)


AREA_SYNONYMS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"\b(?:meta|facebook|instagram)\b", re.IGNORECASE), ("meta_ads",)),
    (re.compile(r"\bpaid social\b", re.IGNORECASE), ("meta_ads", "linkedin_ads", "paid_social")),
    (re.compile(r"\bgoogle ads\b|\badwords\b|\bgoogle\b(?!\s+(?:analytics|tag|sheets|docs|drive|"
                r"workspace|cloud|data studio|looker))|\bpaid search\b|\bsearch (?:ads|campaigns?|"
                r"engine marketing)\b|\bppc\b|\bsem\b", re.IGNORECASE), ("google_ads", "paid_search")),
    (re.compile(r"\bseo\b|\bsearch engine optimi[sz]ation\b", re.IGNORECASE), ("seo",)),
    (re.compile(r"\blinked\s?in\b", re.IGNORECASE), ("linkedin_ads",)),
    (re.compile(r"\bpaid media\b|\bperformance marketing\b|\bperformance (?:media|advertising|"
                r"channels)\b", re.IGNORECASE), ("paid_media", "performance_marketing")),
    (re.compile(r"\bdirect reports?\b|\bmanag\w*\s+(?:a\s+)?(?:team|people)\b|\bteam (?:lead\w*|"
                r"management)\b|\bled\s+(?:a\s+)?team\b|\bpeople manage\w*\b", re.IGNORECASE),
     ("team_leadership",)),
)
"""Question words and the ``years_experience.<area>`` keys that state them."""


def area_facts(question: str, facts: Iterable[CandidateFact]) -> list[CandidateFact]:
    """The per-area years facts whose area the question names, by the area's own words
    ("direct response marketing" for ``years_experience.direct_response_marketing``) or a
    synonym (``AREA_SYNONYMS``)."""
    text = wording_key(question)
    synonyms = {area for pattern, areas in AREA_SYNONYMS if pattern.search(question) for area in areas}
    found = []
    for fact in facts:
        area = years_fact_area(fact.key)
        if not area or years_value(fact) is None:
            continue
        slug = fact.key.split(".", 1)[1].casefold()
        if slug in synonyms or re.search(rf"\b{re.escape(area)}\b", text):
            found.append(fact)
    return found


PLATFORMS: tuple[tuple[str, re.Pattern[str], tuple[str, ...]], ...] = (
    ("google_ads", re.compile(r"\bgoogle\b(?!\s+(?:analytics|tag|sheets|docs|drive|workspace|cloud|"
                              r"data\s+studio|looker|my\s+business|business\s+profile|maps|slides|"
                              r"forms|meet|chrome|search\s+console))|\badwords\b", re.IGNORECASE),
     ("google_ads", "paid_search")),
    ("meta_ads", re.compile(r"\bmeta\b|\bfacebook\b|\binstagram\b", re.IGNORECASE),
     ("meta_ads", "facebook_ads")),
    ("linkedin_ads", re.compile(r"\blinked\s?in\b", re.IGNORECASE), ("linkedin_ads",)),
    ("tiktok_ads", re.compile(r"\btik\s?tok\b", re.IGNORECASE), ("tiktok_ads",)),
    ("microsoft_ads", re.compile(r"\bmicrosoft\s+(?:ads|advertising)\b|\bbing\b", re.IGNORECASE),
     ("microsoft_ads", "bing_ads")),
    ("amazon_ads", re.compile(r"\bamazon\b(?!\s+(?:web\s+services|aws|s3|ec2))", re.IGNORECASE),
     ("amazon_ads",)),
    ("programmatic", re.compile(r"\bprogrammatic\b|\bdsps?\b|\btrade desk\b|\bdv360\b",
                                re.IGNORECASE), ("programmatic", "dsp")),
    ("x_ads", re.compile(r"\btwitter\b|\bx\s+ads\b", re.IGNORECASE), ("x_ads", "twitter_ads")),
    ("snapchat_ads", re.compile(r"\bsnap(?:chat)?\b", re.IGNORECASE), ("snapchat_ads",)),
    ("pinterest_ads", re.compile(r"\bpinterest\b", re.IGNORECASE), ("pinterest_ads",)),
    ("reddit_ads", re.compile(r"\breddit\b", re.IGNORECASE), ("reddit_ads",)),
    ("youtube_ads", re.compile(r"\byoutube\b", re.IGNORECASE), ("youtube_ads",)),
)
"""Ad platforms: the name, how an option or a fact names it, and its years-fact areas."""


def platforms_named(text: str) -> list[str]:
    return [name for name, pattern, _ in PLATFORMS if pattern.search(text)]


_DENIAL = re.compile(r"\b(?:not|never|no|none|without)\b|n't\b", re.IGNORECASE)
"""A story that denies any work ("… run by a partner team; I did not manage them") is never
deterministic support: Jev reads it."""


def platform_facts(platform: str, facts: Sequence[CandidateFact]) -> list[CandidateFact]:
    """The facts that state the person's own work on ``platform``: a years fact for one of
    its areas with one year or more, or a story fact naming it."""
    _, pattern, areas = next(entry for entry in PLATFORMS if entry[0] == platform)
    found = []
    for fact in facts:
        area = years_fact_area(fact.key)
        value = years_value(fact)
        if ((area and value is not None and value >= 1 and fact.key.split(".", 1)[1] in areas)
                or (fact.source.startswith(STORY_SOURCES) and isinstance(fact.value, str)
                    and pattern.search(fact.value) and not _DENIAL.search(fact.value))):
            found.append(fact)
    return found


_EXPERIENCE_WORDING = re.compile(
    r"\bexperienced?\b|\bexperiences\b|\byears?\s+of\b(?!\s+age)|"
    r"\b\d+\s*\+?\s*(?:years?|yrs?)\b(?!\s+(?:old|of\s+age))|"
    r"\bskills?\b|\bproficien\w*|\bfamiliar(?:ity)?\b|\bplatforms?\b|\bhands-on\b|\bexpertise\b|"
    r"\bworked\s+(?:in|at|with|on|for|as)\b|\b(?:managed|managing|led|leading|owned|owning|"
    r"built|building|ran|running|launched|executed|executing|implemented|optimi[sz]ed)\b",
    re.IGNORECASE)


def experience_wording(question: str) -> bool:
    """The question asks about the applicant's experience, skills, platforms, years or
    something they have done ("Have you led client-facing conversations …", "Do you have
    SEO AND GEO optimization experience?", "Which paid media platforms have you managed?")."""
    return _EXPERIENCE_WORDING.search(question) is not None
