"""Board lanes: the user's editable columns, and a conservative initial placement.

A lane is manual tracking only. Moving a card to "Applied" or "Offer" records the
user's own view; it never submits anything, never creates an application record
and never counts as a site-confirmed submission.

``suggest_lane`` places a newly imported row using only its own Stage and Status
wording (Status first, as the more current). Notes, recruiter names and dates are
never read, so no interview date or employer outcome is inferred from them. When
no rule matches, the row goes to the first lane and the suggestion says so.

A keyword only counts when its own clause states it. Clauses are split at ``; , .
! ? ( ) / |``, dashes between spaces and "but". A keyword is ignored when:

* a negation is next to it ("No offer yet", "Application not submitted", "Not
  rejected; awaiting decision" is therefore Decision, not Closed);
* for the outcome lanes (Applied, Offer, Closed), the clause is uncertain: a question
  ("Rejected?") or a hedge such as "pending", "possible", "maybe", "expected",
  "hoping", "if" or "unless".
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
    ("saved-not-applied", re.compile(
        r"\b(?:not (?:yet )?applied|haven't (?:yet )?applied|to apply|not (?:yet )?submitted)\b")),
    ("applied", re.compile(r"\b(?:applied|application (?:submitted|sent)|submitted)\b")),
    ("saved", re.compile(r"\b(?:saved|interested|bookmarked)\b")),
)
"""Checked in order; the first match wins (so "offer declined" is closed and "not yet
applied" is saved)."""


_CLAUSE_SPLIT: Final = re.compile(r"[;,.!?()/|\n]|\s[-\u2013\u2014]+\s|\bbut\b")
_NEGATION: Final = re.compile(
    r"\b(?:no|not|never|none|nothing|without|non|neither|nor|cannot|can't|won't|didn't|"
    r"hasn't|haven't|isn't|wasn't|aren't|weren't|don't|doesn't|yet to|n't)\b")
_HEDGE: Final = re.compile(
    r"\b(?:pending|possible|possibly|potential|potentially|maybe|perhaps|might|may|"
    r"expected|expecting|expect|hoping|hope|hopefully|if|unless|whether|tbd|unclear|"
    r"unknown|likely|unlikely|probably)\b")
_OUTCOME_LANES: Final = frozenset({"applied", "offer", "closed"})
_NEGATION_IS_THE_RULE: Final = frozenset({"saved-not-applied"})
_WINDOW_BEFORE: Final = 4
_WINDOW_AFTER: Final = 3


def _clauses(text: str) -> list[tuple[str, bool]]:
    """Clauses of ``text`` with whether each is a question."""
    result: list[tuple[str, bool]] = []
    position = 0
    for match in _CLAUSE_SPLIT.finditer(text):
        result.append((text[position:match.start()], match.group(0) == "?"))
        position = match.end()
    result.append((text[position:], False))
    return [(c.strip(), q) for c, q in result if c.strip()]


def _stated(clause: str, match: re.Match[str], *, outcome: bool, question: bool) -> bool:
    """True when the matched keyword is asserted, not negated or hedged."""
    before = " ".join(clause[:match.start()].split()[-_WINDOW_BEFORE:])
    after = " ".join(clause[match.end():].split()[:_WINDOW_AFTER])
    if _NEGATION.search(before) or _NEGATION.search(after):
        return False
    return not (outcome and (question or _HEDGE.search(clause)))


def suggest_lane(
    stage: str | None, status: str | None, lanes: BoardLanes = DEFAULT_BOARD_LANES
) -> LaneSuggestion:
    """Initial lane for a row from its Stage and Status wording only."""
    available = set(lanes.ids())
    for column, text in (("status", status), ("stage", stage)):
        if not text:
            continue
        clauses = _clauses(text.casefold())
        for rule, pattern in _RULES:
            lane = "saved" if rule == "saved-not-applied" else rule
            if lane not in available:
                continue
            for clause, question in clauses:
                match = pattern.search(clause)
                if match is None:
                    continue
                if rule not in _NEGATION_IS_THE_RULE and not _stated(
                        clause, match, outcome=lane in _OUTCOME_LANES, question=question):
                    continue
                return LaneSuggestion(lane=lane, rule=f"{column}:{lane}",
                                      reason=f"{column.capitalize()} wording suggests {lane}")
    first = lanes.lanes[0].id
    return LaneSuggestion(lane=first, rule=None,
                          reason="No stage or status wording matched; review the lane")

