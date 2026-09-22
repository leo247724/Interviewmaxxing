"""Board lanes: the user's editable columns, and a conservative initial placement.

A lane is manual tracking only. Moving a card to "Applied" or "Offer" records the
user's own view; it never submits anything, never creates an application record
and never counts as a site-confirmed submission.

``suggest_lane`` places a newly imported row using only its own Stage and Status
wording (Status first, as the more current). Notes, recruiter names and dates are
never read, so no interview date or employer outcome is inferred from them. When
no rule matches, the row goes to the first lane and the suggestion says so.
"""

from __future__ import annotations

import re
from typing import Final, Self

from pydantic import Field, model_validator

from interviewmaxxing_core import Contract

from ._types import NonEmptyStr

LANE_ID_PATTERN: Final = r"^[a-z][a-z0-9_-]{0,39}$"


class BoardLane(Contract):
    id: str = Field(pattern=LANE_ID_PATTERN)
    label: NonEmptyStr
    description: str | None = None


class BoardLanes(Contract):
    """The ordered, user-configurable columns of one candidate's board."""

    lanes: list[BoardLane]

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if not self.lanes:
            raise ValueError("a board needs at least one lane")
        ids = [lane.id for lane in self.lanes]
        labels = [lane.label.casefold() for lane in self.lanes]
        if len(set(ids)) != len(ids) or len(set(labels)) != len(labels):
            raise ValueError("lane ids and labels must be distinct")
        return self

    def ids(self) -> list[str]:
        return [lane.id for lane in self.lanes]

    def get(self, lane_id: str) -> BoardLane | None:
        return next((lane for lane in self.lanes if lane.id == lane_id), None)


DEFAULT_BOARD_LANES: Final = BoardLanes(lanes=[
    BoardLane(id="saved", label="Saved", description="Tracked, not yet applied"),
    BoardLane(id="applied", label="Applied", description="You applied (your own record)"),
    BoardLane(id="scheduling", label="Scheduling", description="Arranging a conversation"),
    BoardLane(id="interviewing", label="Interviewing"),
    BoardLane(id="assessment", label="Assessment", description="Take-home or case work"),
    BoardLane(id="follow-up", label="Follow-up", description="Waiting on or owing a reply"),
    BoardLane(id="decision", label="Decision", description="A decision is pending"),
    BoardLane(id="offer", label="Offer"),
    BoardLane(id="closed", label="Closed", description="Ended, withdrawn or declined"),
])


class LaneSuggestion(Contract):
    lane: str
    rule: str | None
    """The rule that matched (e.g. ``status:interviewing``); None when nothing did."""
    reason: str


_RULES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("closed", re.compile(
        r"\b(?:rejected|rejection|declined|withdrew|withdrawn|closed|not (?:moving|proceeding)"
        r" forward|no longer (?:considered|in consideration|moving forward)|position filled)\b")),
    ("offer", re.compile(r"\boffers?\b")),
    ("decision", re.compile(r"\b(?:decision|deciding)\b")),
    ("assessment", re.compile(
        r"\b(?:assessments?|take[- ]home|case (?:study|exercise)|exercise|assignment"
        r"|work sample)\b")),
    ("scheduling", re.compile(r"\b(?:scheduling|to schedule|to be scheduled|availability)\b")),
    ("interviewing", re.compile(
        r"\b(?:interview\w*|onsite|on-site|panel|screen|screening|phone screen|final round"
        r"|round \d+|\d+(?:st|nd|rd|th) round)\b")),
    ("follow-up", re.compile(
        r"\b(?:follow[- ]?up|awaiting (?:response|feedback|reply)|waiting (?:on|for))\b")),
    ("saved", re.compile(r"\b(?:not (?:yet )?applied|to apply|haven't applied)\b")),
    ("applied", re.compile(r"\b(?:applied|application (?:submitted|sent)|submitted)\b")),
    ("saved", re.compile(r"\b(?:saved|interested|bookmarked)\b")),
)
"""Checked in order; the first match wins (so "offer declined" is closed and "not yet
applied" is saved)."""


def suggest_lane(
    stage: str | None, status: str | None, lanes: BoardLanes = DEFAULT_BOARD_LANES
) -> LaneSuggestion:
    """Initial lane for a row from its Stage and Status wording only."""
    available = set(lanes.ids())
    for column, text in (("status", status), ("stage", stage)):
        if not text:
            continue
        words = text.casefold()
        for lane, pattern in _RULES:
            if lane in available and pattern.search(words):
                return LaneSuggestion(lane=lane, rule=f"{column}:{lane}",
                                      reason=f"{column.capitalize()} wording suggests {lane}")
    first = lanes.lanes[0].id
    return LaneSuggestion(lane=first, rule=None,
                          reason="No stage or status wording matched; review the lane")

