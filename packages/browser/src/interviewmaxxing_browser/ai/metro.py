"""The person's metro area and where a job is (round 13).

The owner's rule: for a job in his metro (his own city and the towns around it that he lists
in the simple answers' ``metro_area``) in-person or hybrid work is fine, whatever the job
requires; for a job anywhere else the answer is remote. This module reads places only: which
metro the person states, and whether a text (a job's location, a question, an option) names a
place in it, names another place, says remote, or names nothing it can read. The routing
decides what each verdict answers (``DynamicPacketResolver._onsite_city``, ``_work_location``
and ``_office_choice``)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from interviewmaxxing_core import JobRecord, PostalAddress, modes_named
from interviewmaxxing_generation.questions import question_key
from interviewmaxxing_generation.values import us_state_code, us_states_named


class MetroVerdict(StrEnum):
    IN_METRO = "IN_METRO"
    """The text names a place in the person's metro (with no other state after it)."""
    OUTSIDE = "OUTSIDE"
    """The text names another place: a "City, ST", a US state, a country or another city."""
    REMOTE = "REMOTE"
    """A job location that says remote (or anywhere), or states no location at all."""
    UNKNOWN = "UNKNOWN"
    """Nothing readable ("Multiple Locations", "Hybrid"); the saved preference decides."""


@dataclass(frozen=True, slots=True)
class Metro:
    """The person's own city and state (verified identity) and the metro places they list."""

    city: str
    state: str | None
    """The two-letter code of the person's state, when the identity states one."""
    places: tuple[str, ...]
    """The person's city first, then every ``metro_area`` place."""


@dataclass(frozen=True, slots=True)
class PlaceReading:
    verdict: MetroVerdict
    place: str | None = None
    """The metro place named (IN_METRO) or the first other place named (OUTSIDE)."""


_SPLIT = re.compile(r"[,;\n/]|\band\b", re.IGNORECASE)
_STATE_CODE_AFTER = re.compile(r"\s*,?\s*([A-Z]{2})\b")
_WORDS_AFTER = re.compile(r"\s*,\s*([A-Za-z]+(?:\s+[A-Za-z]+){0,2})")
_CITY_STATE = re.compile(r"\b([A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*)*)\s*,\s*([A-Z]{2})\b")
_CAPITALIZED = re.compile(r"[A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*)*")
_REMOTE = re.compile(r"\b(?:remote(?:ly)?|anywhere|work from home|wfh|distributed|virtual)\b",
                     re.IGNORECASE)
_COUNTRIES = frozenset({
    "united states", "united states of america", "usa", "us", "u.s", "u.s.a", "canada", "mexico",
    "united kingdom", "uk", "england", "ireland", "germany", "france", "spain", "portugal",
    "netherlands", "india", "australia", "brazil", "argentina", "colombia", "philippines",
    "singapore", "japan", "israel", "poland", "sweden"})
_GENERIC = frozenset({
    "multiple", "multiple locations", "locations", "location", "various", "various locations",
    "hybrid", "remote", "onsite", "on-site", "in-office", "office", "offices", "hq",
    "headquarters", "global", "worldwide", "anywhere", "flexible", "tbd", "n/a", "na", "other",
    "north america", "americas", "emea", "apac", "latam", "none", "any", "no preference"})
"""Capitalized words in a job location that name no place."""


def metro_places(value: str | None) -> tuple[str, ...]:
    """The places a ``metro_area`` value lists ("Round Rock, Cedar Park; Leander and Kyle"), in
    order, each once; a state code or state name among them ("Round Rock, TX") is dropped, so a
    state never makes a whole state the metro."""
    if not value:
        return ()
    places: list[str] = []
    seen: set[str] = set()
    for part in _SPLIT.split(value):
        name = " ".join(part.split()).strip(" .")
        key = question_key(name)
        if not key or us_state_code(name) is not None or key in seen:
            continue
        seen.add(key)
        places.append(name)
    return tuple(places)


def person_metro(address: PostalAddress, metro_area: str | None) -> Metro | None:
    """The person's metro: their verified city and the ``metro_area`` places; None without a
    city."""
    city = " ".join((address.city or "").split())
    if not city:
        return None
    others = [place for place in metro_places(metro_area) if question_key(place) != question_key(city)]
    return Metro(city=city, state=us_state_code(address.region), places=(city, *others))


def _state_after(text: str) -> str | None:
    """The US state written right after a place ("…, TX", "…, Texas", "… TX"), if any."""
    code = _STATE_CODE_AFTER.match(text)
    if code is not None and us_state_code(code.group(1)) is not None:
        return us_state_code(code.group(1))
    words = _WORDS_AFTER.match(text)
    if words is None:
        return None
    parts = words.group(1).split()
    for count in (3, 2, 1):  # "District of Columbia", "New Mexico", "Texas"
        if len(parts) >= count and (state := us_state_code(" ".join(parts[:count]))) is not None:
            return state
    return None


def metro_place_named(text: str, metro: Metro) -> str | None:
    """The first metro place ``text`` names as a whole word, unless the state written right
    after it is another state ("Austin, MN" is not the Austin in Texas)."""
    for place in metro.places:
        for match in re.finditer(rf"(?<![\w-]){re.escape(place)}(?![\w-])", text, re.IGNORECASE):
            state = _state_after(text[match.end():match.end() + 32])
            if state is None or metro.state is None or state == metro.state:
                return place
    return None


def names_place(label: str) -> bool:
    """An option that names a place by itself: a "City, ST" ("Austin, TX", "New York City,
    NY") or a US state ("Texas")."""
    return (any(us_state_code(m.group(2)) is not None for m in _CITY_STATE.finditer(label))
            or bool(us_states_named(label)))


def _other_place(text: str, *, places: Sequence[str], capitalized: bool) -> str | None:
    """A place ``text`` names that is not in the metro: a "City, ST", a US state, a country,
    one of ``places`` (candidates the caller found) or, with ``capitalized``, a capitalized
    run that is no generic word."""
    city_state = next((m.group(0) for m in _CITY_STATE.finditer(text)
                       if us_state_code(m.group(2)) is not None), None)
    if city_state is not None:
        return city_state
    states = us_states_named(text)
    if states:
        return sorted(states)[0]
    for candidate in places:
        if question_key(candidate) not in _GENERIC and question_key(candidate) not in _COUNTRIES:
            return candidate
    runs: list[str] = _CAPITALIZED.findall(text) if capitalized else []
    for run in runs:
        key = question_key(run)
        if key in _COUNTRIES:
            return run
        if key and key not in _GENERIC and not any(word in _GENERIC for word in key.split()):
            return run
    lowered = " ".join(text.casefold().split())
    return next((country for country in _COUNTRIES
                 if re.search(rf"(?<![\w.]){re.escape(country)}(?![\w.])", lowered)), None)


def read_place(text: str | None, metro: Metro, *, places: Sequence[str] = (),
               remote: bool = False, capitalized: bool = False) -> PlaceReading:
    """Where ``text`` is, for the person's ``metro``: IN_METRO when it names a metro place;
    with ``remote``, REMOTE when it says remote or anywhere; OUTSIDE when it names another
    place; UNKNOWN otherwise. A question's wording is read without ``remote`` (a question
    that says "this is not a remote role" is not a remote job) and without ``capitalized``
    (its capitalized words are mostly not places): the caller passes the places it found."""
    if not text or not text.strip():
        return PlaceReading(MetroVerdict.UNKNOWN)
    named = metro_place_named(text, metro)
    if named is not None:
        return PlaceReading(MetroVerdict.IN_METRO, named)
    if remote and _REMOTE.search(text):
        return PlaceReading(MetroVerdict.REMOTE)
    other = _other_place(text, places=places, capitalized=capitalized)
    if other is not None:
        return PlaceReading(MetroVerdict.OUTSIDE, other)
    return PlaceReading(MetroVerdict.UNKNOWN)


def job_place(job: JobRecord, metro: Metro) -> PlaceReading:
    """Where the job is: its location text read with remote wording and capitalized places; a
    job that states no location is remote (the owner's rule)."""
    if not job.location or not job.location.strip():
        return PlaceReading(MetroVerdict.REMOTE)
    return read_place(job.location, metro, remote=True, capitalized=True)


def posting_mode(job: JobRecord) -> str | None:
    """The one work mode the posting states in its location or title ("Austin, TX (Hybrid)",
    "Senior Manager - Onsite"), else None."""
    modes = modes_named(" ".join(part for part in (job.location, job.title) if part))
    return modes[0] if len(modes) == 1 else None
