"""Work-arrangement preference (remote, hybrid, on-site) selects.

One untyped GLOBAL saved answer (``work_arrangement_preference`` in the simple-answers
map, one of ``WORK_ARRANGEMENTS``) answers a choice whose options are all work modes,
whatever the field's own semantic type: live forms type "Location Preference" as a
location, which the verified address can never answer."""

from __future__ import annotations

import re

from .forms import ApplicationField, ControlType, normalize_text

WORK_ARRANGEMENT_PREFERENCE_QUESTION = "Which work arrangement do you prefer: remote, hybrid or on-site?"
"""The saved question of the work-arrangement preference (an untyped GLOBAL saved answer)."""

WORK_ARRANGEMENTS: tuple[str, ...] = ("remote", "hybrid", "on-site")
"""The closed vocabulary of ``work_arrangement_preference``."""

_MODE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("remote", re.compile(r"\b(?:remote(?:ly)?|wfh|work(?:ing)? from home|telecommut\w*|"
                          r"telework\w*|virtual(?:ly)?|distributed)\b", re.IGNORECASE)),
    ("hybrid", re.compile(r"\bhybrid\b", re.IGNORECASE)),
    ("on-site", re.compile(r"\b(?:on-?\s?site|in-?\s?office|in-?\s?person|in the office|office|"
                           r"onsite)\b", re.IGNORECASE)),
)
"""The work modes an option or a saved value names."""
_MODE_WORDS = frozenset({
    "remote", "remotely", "fully", "hybrid", "on", "site", "onsite", "in", "office", "person",
    "work", "working", "from", "home", "wfh", "only", "or", "and", "either", "flexible", "flex",
    "no", "preference", "any", "open", "to", "all", "partially", "partly", "mostly",
    "telecommute", "telecommuting", "telework", "teleworking", "virtual", "virtually",
    "distributed", "days", "day", "week", "per", "a", "the", "at", "location", "based", "of",
    "prefer", "not", "say", "other", "both", "n", "applicable", "first", "friendly",
    "arrangement", "setting", "workplace", "us", "anywhere"})
"""Words a work-mode option may use besides the mode itself ("Hybrid - 3 days in office",
"Remote (US only)", "No preference", "Prefer not to say"); anything else names another
kind of answer."""
_PARENTHETICAL = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_WORK_MODE_CONTROLS = frozenset({ControlType.SELECT, ControlType.RADIO, ControlType.MULTISELECT,
                                 ControlType.CHECKBOX_GROUP})


def modes_named(text: str) -> list[str]:
    """The work modes ``text`` names, in ``WORK_ARRANGEMENTS`` order."""
    return [mode for mode, pattern in _MODE_PATTERNS if pattern.search(text)]


def names_work_mode(label: str) -> bool:
    """The option names a work mode ("Remote", "Hybrid (2 days on-site)", "In-office")."""
    return bool(modes_named(label))


def work_mode_of(label: str) -> str | None:
    """The one work mode an option names ("Fully remote" → "remote", "In-office" →
    "on-site"), or None when it names none or several ("Remote or hybrid")."""
    modes = modes_named(label)
    return modes[0] if len(modes) == 1 else None


def normalize_work_arrangement(value: str) -> str | None:
    """The ``WORK_ARRANGEMENTS`` code a person's wording states ("Onsite", "In office",
    "Fully remote", "WFH"), or None when it states none or several."""
    return work_mode_of(value)


def _mode_option(label: str) -> bool:
    words = re.findall(r"[a-z]+", normalize_text(_PARENTHETICAL.sub(" ", label)))
    return all(word in _MODE_WORDS for word in words)


def is_work_mode_choice(field: ApplicationField) -> bool:
    """A choice (single or select-all) whose enabled options are all work modes or
    neutral preferences ("No preference", "Either"), at least two of them naming a mode."""
    options = [o for o in field.options or [] if not o.disabled and o.value.strip()]
    return (field.control_type in _WORK_MODE_CONTROLS
            and len(options) >= 2 and all(_mode_option(o.label) for o in options)
            and sum(1 for o in options if names_work_mode(o.label)) >= 2)


def stated_work_arrangement_preference(question: str) -> bool:
    """Whether a saved answer's question is the work-arrangement preference question."""
    return normalize_text(question) == normalize_text(WORK_ARRANGEMENT_PREFERENCE_QUESTION)
