"""The saved earliest start date on a start-date choice, deterministically (round 10).

The saved ``earliest_start_date`` is a date or a notice period in the person's words
("2026-10-15", "October 15, 2026", "Two weeks after an offer is accepted", "immediately",
"1 month"). A site's start-date select buckets availability ("Immediately", "Within 2
weeks", "2-4 weeks", "1-3 months", "More than 3 months", or Lever's "One week after offer
acceptance" …). The saved value is read as a number of days from today and the option
whose stated range contains it is chosen; when none contains it, the next later one;
"Other" never."""

from __future__ import annotations

import math
import re
from datetime import date, datetime

_WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_UNIT_DAYS = {"day": 1, "week": 7, "wk": 7, "month": 30, "mo": 30, "year": 365, "yr": 365}
_NUMBER = r"(\d+(?:\.\d+)?|" + "|".join(_WORD_NUMBERS) + r")"
_DURATION = re.compile(
    _NUMBER + r"\+?\s*(?:(?:-|\u2013|\u2014|to)\s*" + _NUMBER + r")?\s*"
    r"(days?|weeks?|wks?|months?|mos?|years?|yrs?)\b", re.IGNORECASE)
_IMMEDIATE = re.compile(
    r"\b(?:immediately|immediate|asap|as soon as possible|right away|straight away|now|today|"
    r"available now|upon (?:an )?offer|on offer|no notice)\b", re.IGNORECASE)
_WITHIN = re.compile(r"\b(?:within|less than|under|up to|in|inside|no more than|at most)\b|\bor less\b|"
                     r"\bor sooner\b|\bor fewer\b", re.IGNORECASE)
_MORE_THAN = re.compile(r"\+|\b(?:more than|over|beyond|longer than|at least|greater than)\b|"
                        r"\bor more\b|\bor longer\b|\band up\b|\bor later\b", re.IGNORECASE)
_NON_ITEM = re.compile(r"^(?:other|others|not sure|unsure|n/?a|prefer not to (?:say|answer)|"
                       r"depends|flexible|negotiable|select|choose|please select)\b", re.IGNORECASE)
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%B %d, %Y", "%B %d %Y", "%b %d, %Y",
                 "%b %d %Y", "%d %B %Y", "%d %b %Y", "%B %Y", "%b %Y", "%Y-%m")


def _number(token: str) -> float:
    return _WORD_NUMBERS.get(token.casefold(), 0.0) or float(token)


def _days(token: str, count: float) -> float:
    unit = token.casefold().rstrip("s")
    return count * _UNIT_DAYS.get(unit, _UNIT_DAYS.get(unit[:2], 1))


def parse_date(value: str) -> date | None:
    """A date the value states in a common form; a month alone is its first day."""
    text = " ".join(value.strip().split())
    for pattern in _DATE_FORMATS:
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def days_from_today(value: str, today: date) -> tuple[float, float] | None:
    """The days from ``today`` the saved value states, as an inclusive range: a date, an
    immediate start (0), a notice period ("2 weeks" → 14, "4-6 weeks" → 28-42, "one
    month" → 30); None when the value states none."""
    parsed = parse_date(value)
    if parsed is not None:
        days = max(0, (parsed - today).days)
        return float(days), float(days)
    match = _DURATION.search(value)
    if match is not None:
        low, high, unit = match.group(1), match.group(2), match.group(3)
        first = _days(unit, _number(low))
        second = _days(unit, _number(high)) if high else first
        return min(first, second), max(first, second)
    if _IMMEDIATE.search(value):
        return 0.0, 0.0
    return None


def option_bucket(label: str) -> tuple[float, float] | None:
    """The inclusive range of days an availability option states: "Immediately" (0),
    "Within 2 weeks" (0-14), "2-4 weeks" (14-28), "1-3 months" (30-90), "More than 3
    months" (90+), "Two weeks after offer acceptance" (14); None for "Other" and for any
    option that states no availability."""
    if _NON_ITEM.match(label.strip()):
        return None
    match = _DURATION.search(label)
    if match is None:
        return (0.0, 0.0) if _IMMEDIATE.search(label) else None
    low, high, unit = match.group(1), match.group(2), match.group(3)
    first = _days(unit, _number(low))
    if high:
        second = _days(unit, _number(high))
        return min(first, second), max(first, second)
    if _MORE_THAN.search(label):
        return first, math.inf
    if _WITHIN.search(label) or _IMMEDIATE.search(label):
        return 0.0, first
    return first, first


def bucket_choice(saved: tuple[float, float],
                  buckets: list[tuple[str, tuple[float, float]]]) -> tuple[str | None, str]:
    """The key of the option whose range contains the saved availability (its later
    bound), else the next later option, else None; with how it was chosen."""
    _, days = saved
    containing = [(key, bounds) for key, bounds in buckets if bounds[0] <= days <= bounds[1]]
    if containing:
        return min(containing, key=lambda item: item[1][1])[0], "contains"
    later = [(key, bounds) for key, bounds in buckets if bounds[0] > days]
    if later:
        return min(later, key=lambda item: item[1][0])[0], "next_later"
    return None, "NO_LATER_OPTION"


def in_words(value: str) -> str:
    """The saved start date for a free-text answer: a date written out ("October 15,
    2026"), otherwise the value as saved."""
    parsed = parse_date(value)
    if parsed is None:
        return value.strip()
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
