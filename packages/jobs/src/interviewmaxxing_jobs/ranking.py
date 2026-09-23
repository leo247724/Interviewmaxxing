"""Deterministic location ordering for stored listings (never a filter).

With ``LocationPriority.STRONGLY_PREFER_ONSITE_HYBRID`` (the user's default) a listing
that states onsite/hybrid work in a preferred onsite place (e.g. Austin, TX) ranks
well above an eligible remote role, which still ranks above everything else. Nothing
is excluded, and nothing is inferred: the tier uses only the stated work arrangement,
location text and remote eligibility.

A place counts as *confirmed* only when both the target city and its region (state
abbreviation or name) are stated; a city alone ("Austin") is *unverified* and a
matching city in another region ("Austin, MN") is *outside*. Remote eligibility is
*eligible* only when the listing states a region that covers the remote target (e.g.
"United States"). A narrower region such as Texas-only does not establish US-wide
eligibility, and the onsite target never supplies remote eligibility. A stated
region elsewhere ("Canada") is *ineligible*; nothing stated is *unknown*.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Literal, Protocol

from interviewmaxxing_core import (
    JobListing,
    ListingStatus,
    LocationPriority,
    OnsiteTarget,
    RemoteTarget,
    WorkArrangement,
)

from .text import US_STATES

PlaceMatch = Literal["confirmed", "unverified", "outside", "none"]
Eligibility = Literal["eligible", "unknown", "ineligible"]

_US_NAMES = ("united states", "united states of america", "usa", "u.s.a.", "u.s.", "us",
             "anywhere in the us", "anywhere in the united states")
_STATE_NAMES = {name.casefold(): abbr for abbr, name in US_STATES.items()}


class LocationPreferences(Protocol):
    """Satisfied by both ``JobSearchQuery`` and ``SelectionPreferences``."""

    @property
    def onsite(self) -> list[OnsiteTarget]: ...

    @property
    def remote(self) -> RemoteTarget | None: ...

    @property
    def location_priority(self) -> LocationPriority: ...


def _word(text: str, word: str) -> bool:
    return re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", text, re.I) is not None


def _split_target(location: str) -> tuple[str, str | None]:
    """``"Austin, TX"`` -> ``("austin", "TX")``; region is a US state abbreviation
    when it can be recognised, else the raw region text, else None."""
    parts = [p.strip() for p in location.split(",") if p.strip()]
    city = parts[0].casefold() if parts else location.strip().casefold()
    if len(parts) < 2:
        return city, None
    region = parts[1]
    if region.upper() in US_STATES:
        return city, region.upper()
    return city, _STATE_NAMES.get(region.casefold(), region)


def _states_in(text: str) -> set[str]:
    """US states named in a location text, by abbreviation or full name."""
    found = {abbr for abbr in US_STATES if re.search(rf"(?<![A-Za-z]){abbr}(?![A-Za-z])", text)}
    short = re.fullmatch(r"([A-Za-z]{2})(?:\s+\d{5}(?:-\d{4})?)?", text.strip())
    if short and short[1].upper() in US_STATES:
        found.add(short[1].upper())
    found |= {abbr for abbr, name in US_STATES.items() if _word(text, name)}
    return found


def place_match(listing: JobListing, targets: Sequence[OnsiteTarget]) -> tuple[PlaceMatch, OnsiteTarget | None]:
    """How the listing's stated location relates to the onsite targets."""
    text = listing.location or ""
    if not text.strip():
        return "none", None
    best: tuple[PlaceMatch, OnsiteTarget | None] = ("none", None)
    for target in targets:
        city, region = _split_target(target.location)
        if not city or not _word(text, city):
            continue
        # Keep the region adjacent to this city: Austin, MN; Dallas, TX must
        # not borrow Dallas's state. Only the first region after a comma belongs
        # to the matched city; remaining place/country labels are separate.
        city_match = re.search(rf"(?<![A-Za-z]){re.escape(city)}(?![A-Za-z])", text, re.I)
        assert city_match is not None
        local = re.split(r"[;|/\n]", text[city_match.end():], maxsplit=1)[0].strip()
        parts = [part.strip() for part in local.split(",") if part.strip()]
        local_region = parts[0] if parts else local
        states = _states_in(local_region)
        if region is not None:
            if region in states or _word(local_region, region):
                return "confirmed", target
            if states - {region} or (local.startswith(",") and local_region):
                best = ("outside", target) if best[0] == "none" else best
                continue
            if best[0] != "confirmed":
                best = ("unverified", target)
        else:
            best = ("confirmed", target)
    return best


def remote_eligibility(listing: JobListing, prefs: LocationPreferences) -> Eligibility:
    """Whether a stated remote role is open to the candidate, from the listing's own
    ``remote_eligibility`` text (never from its title or search origin)."""
    if prefs.remote is None or listing.work_arrangement is not WorkArrangement.REMOTE:
        return "ineligible"
    stated = (listing.remote_eligibility or "").strip()
    if not stated:
        return "unknown"
    text = stated.casefold()
    region = prefs.remote.eligible_region.strip().casefold()
    if text in {"remote", "anywhere", "nationwide", "unspecified", "location flexible"}:
        return "unknown"
    # Restrictive prose cannot establish unrestricted eligibility. Keep it
    # visible for review instead of inferring from the search or office city.
    if re.search(r"\b(?:except|excluding|excluded|not|outside)\b", text):
        return "unknown"
    if region in _US_NAMES:
        states = _states_in(stated)
        if states or re.search(r"\b(?:canada|united kingdom)\W+only\b", text):
            return "ineligible"
        return "eligible" if any(_word(text, name) for name in _US_NAMES) else "ineligible"
    target_state = _STATE_NAMES.get(region) or (region.upper() if region.upper() in US_STATES else None)
    if target_state:
        return "eligible" if target_state in _states_in(stated) or any(
            _word(text, name) for name in _US_NAMES
        ) else "ineligible"
    return "eligible" if _word(text, region) else "ineligible"


def location_tier(listing: JobListing, prefs: LocationPreferences) -> int:
    """0 is best. Classes: *onsite* = stated onsite/hybrid at a confirmed target place;
    *near* = arrangement unstated at a confirmed target place; *unverified* = the
    target city without its region (any non-remote arrangement); *remote* = stated
    remote and eligible; *remote?* = stated remote, eligibility unknown; *other* =
    outside the target region, ineligible remote, or no usable location.

    * STRONGLY_PREFER_ONSITE_HYBRID: onsite 0, near 1, unverified 2, remote 3, remote? 4, other 5
    * BALANCED: onsite and remote 0, near 1, unverified and remote? 2, other 3
    * PREFER_REMOTE: remote 0, onsite 1, remote? and near 2, unverified 3, other 4
    """
    match, target = place_match(listing, prefs.onsite)
    arrangement = listing.work_arrangement
    onsite = match == "confirmed" and target is not None and arrangement in target.arrangements
    near = match == "confirmed" and arrangement is WorkArrangement.UNKNOWN
    unverified = match == "unverified" and arrangement is not WorkArrangement.REMOTE
    eligibility = remote_eligibility(listing, prefs)
    remote = eligibility == "eligible"
    remote_unknown = eligibility == "unknown"
    priority = prefs.location_priority
    if priority is LocationPriority.BALANCED:
        return 0 if onsite or remote else 1 if near else 2 if unverified or remote_unknown else 3
    if priority is LocationPriority.PREFER_REMOTE:
        return (0 if remote else 1 if onsite else 2 if remote_unknown or near
                else 3 if unverified else 4)
    return (0 if onsite else 1 if near else 2 if unverified else 3 if remote
            else 4 if remote_unknown else 5)


def rank_listings(listings: Iterable[JobListing], prefs: LocationPreferences) -> list[JobListing]:
    """Stable order: open/unknown before closed, then location tier. Ties keep the
    incoming order (e.g. newest first from the store)."""
    return sorted(listings, key=lambda x: (x.status is ListingStatus.CLOSED,
                                           location_tier(x, prefs)))
