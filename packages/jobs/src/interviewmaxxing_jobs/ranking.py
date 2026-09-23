"""Deterministic location ordering for stored listings (never a filter).

With ``LocationPriority.STRONGLY_PREFER_ONSITE_HYBRID`` (the user's default) a listing
that states onsite/hybrid work in a preferred onsite location (e.g. Austin, TX) ranks
well above an eligible remote role, which still ranks above everything else. Nothing
is excluded, and nothing is inferred: the tier uses only the stated work arrangement
and location text.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol

from interviewmaxxing_core import (
    JobListing,
    ListingStatus,
    LocationPriority,
    OnsiteTarget,
    RemoteTarget,
    WorkArrangement,
)


class LocationPreferences(Protocol):
    """Satisfied by both ``JobSearchQuery`` and ``SelectionPreferences``."""

    @property
    def onsite(self) -> list[OnsiteTarget]: ...

    @property
    def remote(self) -> RemoteTarget | None: ...

    @property
    def location_priority(self) -> LocationPriority: ...


def _place(location: str) -> str:
    """The city part of a target such as ``"Austin, TX"``."""
    return location.split(",")[0].strip().casefold()


def in_onsite_target(listing: JobListing, targets: Sequence[OnsiteTarget]) -> OnsiteTarget | None:
    text = (listing.location or "").casefold()
    for target in targets:
        city = _place(target.location)
        if city and city in text:
            return target
    return None


def location_tier(listing: JobListing, prefs: LocationPreferences) -> int:
    """0 is best. Tiers per priority (onsite = stated onsite/hybrid in a target place;
    near = arrangement not stated but located in a target place; remote = stated
    remote while a remote target is set):

    * STRONGLY_PREFER_ONSITE_HYBRID: onsite 0, near 1, remote 2, other 3
    * BALANCED: onsite and remote 0, near 1, other 2
    * PREFER_REMOTE: remote 0, onsite 1, near 2, other 3
    """
    target = in_onsite_target(listing, prefs.onsite)
    onsite = target is not None and listing.work_arrangement in target.arrangements
    near = target is not None and listing.work_arrangement is WorkArrangement.UNKNOWN
    remote = prefs.remote is not None and listing.work_arrangement is WorkArrangement.REMOTE
    priority = prefs.location_priority
    if priority is LocationPriority.BALANCED:
        return 0 if onsite or remote else 1 if near else 2
    if priority is LocationPriority.PREFER_REMOTE:
        return 0 if remote else 1 if onsite else 2 if near else 3
    return 0 if onsite else 1 if near else 2 if remote else 3


def rank_listings(listings: Iterable[JobListing], prefs: LocationPreferences) -> list[JobListing]:
    """Stable order: open/unknown before closed, then location tier. Ties keep the
    incoming order (e.g. newest first from the store)."""
    return sorted(listings, key=lambda x: (x.status is ListingStatus.CLOSED,
                                           location_tier(x, prefs)))
