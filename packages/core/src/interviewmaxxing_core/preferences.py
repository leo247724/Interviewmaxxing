"""Work-arrangement preference selects (remote, hybrid, on-site).

One untyped GLOBAL saved answer (``work_location_preference`` in the simple-answers map)
answers a single choice whose options are all work modes, whatever the field's own
semantic type: live forms type "Location Preference" as a location, which the verified
address can never answer."""

from __future__ import annotations

import re

from .forms import ApplicationField, ControlType, normalize_text

WORK_LOCATION_PREFERENCE_QUESTION = "Which work arrangement do you prefer: remote, hybrid or on-site?"
"""The saved question of the work-location preference (an untyped GLOBAL saved answer)."""

_MODE = re.compile(
    r"\b(?:remote(?:ly)?|hybrid|on-?\s?site|in-?\s?office|in-?\s?person|wfh|work from home|"
    r"telecommut\w*|telework\w*|virtual(?:ly)?|distributed)\b", re.IGNORECASE)
"""A work mode an option names."""
_MODE_WORDS = frozenset({
    "remote", "remotely", "fully", "hybrid", "on", "site", "onsite", "in", "office", "person",
    "work", "from", "home", "wfh", "only", "or", "and", "either", "flexible", "flex", "no",
    "preference", "any", "open", "to", "all", "partially", "partly", "mostly", "telecommute",
    "telecommuting", "telework", "teleworking", "virtual", "virtually", "distributed", "days",
    "day", "week", "per", "a", "the", "at", "location", "based", "of", "prefer",
    "not", "say", "other", "both", "n", "applicable", "first", "friendly", "arrangement",
    "setting", "workplace", "us", "anywhere"})
"""Words a work-mode option may use besides the mode itself ("Hybrid - 3 days in office",
"Remote (US only)", "No preference", "Prefer not to say"); anything else names another
kind of answer."""
_PARENTHETICAL = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def names_work_mode(label: str) -> bool:
    """The option names a work mode ("Remote", "Hybrid (2 days on-site)", "In-office")."""
    return _MODE.search(label) is not None


def _mode_option(label: str) -> bool:
    words = re.findall(r"[a-z]+", normalize_text(_PARENTHETICAL.sub(" ", label)))
    return all(word in _MODE_WORDS for word in words)


def is_work_mode_choice(field: ApplicationField) -> bool:
    """A single choice whose enabled options are all work modes or neutral preferences
    ("No preference", "Either"), at least two of them naming a mode."""
    options = [o for o in field.options or [] if not o.disabled and o.value.strip()]
    return (field.control_type in (ControlType.SELECT, ControlType.RADIO)
            and len(options) >= 2 and all(_mode_option(o.label) for o in options)
            and sum(1 for o in options if names_work_mode(o.label)) >= 2)


def stated_work_location_preference(question: str) -> bool:
    """Whether a saved answer's question is the work-location preference question."""
    return normalize_text(question) == normalize_text(WORK_LOCATION_PREFERENCE_QUESTION)
