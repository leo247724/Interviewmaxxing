"""Translate a known value (identity, fact or saved answer) into a field's answer.

Choices are matched against the field's actual, enabled, non-placeholder options by
visible label. Nothing is fabricated: a value that maps to no option, or to more
than one, is reported as unmapped (with the plausible candidates) instead of
guessed. Option machine values are never matched against user text, because a
value such as ``"0"`` may label "No".
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Final

from interviewmaxxing_core import (
    AnswerValue,
    ApplicationField,
    BooleanValue,
    ChoiceValue,
    ControlType,
    FieldOption,
    MultiChoiceValue,
    SemanticType,
    TextValue,
    answer_problems,
)

from .questions import question_key

RawValue = str | int | float | bool | list[str]
"""A value in the user's terms: ``SavedAnswer.value`` or ``CandidateFact.value``."""


@dataclass(frozen=True, slots=True)
class Mapped:
    value: AnswerValue


@dataclass(frozen=True, slots=True)
class Unmapped:
    reason: str
    candidates: tuple[AnswerValue, ...] = field(default=())


Translation = Mapped | Unmapped

_TRUE_WORDS: Final = frozenset({"yes", "true"})
_FALSE_WORDS: Final = frozenset({"no", "false"})
_NUMBER: Final = re.compile(r"-?\d+(?:\.\d+)?")


def render_scalar(raw: str | int | float | bool) -> str:
    """Text form of a scalar. Booleans become Yes/No; 0 stays ``"0"``."""
    if isinstance(raw, bool):
        return "Yes" if raw else "No"
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    return str(raw)


def as_number(raw: RawValue) -> float | None:
    """A numeric fact value (``7``, ``7.5``, ``"7"``); booleans are not numbers."""
    if isinstance(raw, bool | list):
        return None
    if isinstance(raw, int | float):
        return float(raw)
    text = raw.strip()
    return float(text) if _NUMBER.fullmatch(text) else None


def usable_options(fld: ApplicationField) -> list[FieldOption]:
    """Enabled options with a machine value (placeholders like "Select..." excluded)."""
    return [o for o in fld.options or [] if not o.disabled and o.value.strip()]


def _choice(option: FieldOption) -> ChoiceValue:
    return ChoiceValue(value=option.value, label=option.label)


# --- equivalent spellings (country and US state only) --------------------------------

_US_STATES: Final = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah", "vt": "vermont",
    "va": "virginia", "wa": "washington", "wv": "west virginia", "wi": "wisconsin",
    "wy": "wyoming", "dc": "district of columbia",
}

_EQUIVALENTS: Final[dict[SemanticType, list[frozenset[str]]]] = {
    SemanticType.COUNTRY: [
        frozenset({"united states", "united states of america", "usa", "us", "u.s", "u.s.a"}),
        frozenset({"united kingdom", "uk", "u.k", "great britain"}),
    ],
    SemanticType.STATE: [frozenset({abbr, name}) for abbr, name in _US_STATES.items()],
}


def _spellings(text: str, semantic_type: SemanticType) -> frozenset[str]:
    key = question_key(text)
    spellings = {key}
    for group in _EQUIVALENTS.get(semantic_type, []):
        if key in group:
            spellings |= group
    return frozenset(spellings)


def is_united_states(country: str | None) -> bool:
    """True for the spellings of the United States that country options accept
    ("United States", "USA", "U.S.", ...)."""
    return bool(country) and question_key(country) in _EQUIVALENTS[SemanticType.COUNTRY][0]


def us_state_code(region: str | None) -> str | None:
    """The two-letter code of a US state given by code or by name ("Oregon" -> "OR")."""
    key = question_key(region)
    if key in _US_STATES:
        return key.upper()
    return next((code.upper() for code, name in _US_STATES.items() if name == key), None)


def us_states_named(text: str) -> set[str]:
    """Codes of the US states a text names, by uppercase code ("AL, AZ, CA") or by full
    name ("New York"); longer names win ("West Virginia" is not also Virginia)."""
    codes = {token.lower() for token in re.findall(r"\b[A-Z]{2}\b", text) if token.lower() in _US_STATES}
    remaining = " ".join(text.casefold().split())
    for code, name in sorted(_US_STATES.items(), key=lambda item: -len(item[1])):
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        if pattern.search(remaining):
            codes.add(code)
            remaining = pattern.sub(" ", remaining)
    return {code.upper() for code in codes}


def us_state_name(region: str) -> str | None:
    """The state name for a two-letter US state abbreviation ("TX" -> "Texas",
    "DC" -> "District of Columbia"); None for anything else."""
    name = _US_STATES.get(question_key(region))
    if name is None:
        return None
    return " ".join(word if word == "of" else word.capitalize() for word in name.split())


# --- numeric ranges ------------------------------------------------------------------

_UNIT = re.compile(r"\s*\b(?:years?|yrs?)\b\.?")
_RANGE_PATTERNS: Final = (
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|\u2013|\u2014|to)\s*(\d+(?:\.\d+)?)"), "between"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*\+"), "at_least"),
    (re.compile(r"(\d+(?:\.\d+)?) or more"), "at_least"),
    (re.compile(r"(?:more than|over) (\d+(?:\.\d+)?)"), "above"),
    (re.compile(r"(?:less than|under|fewer than) (\d+(?:\.\d+)?)"), "below"),
    (re.compile(r"(?:up to )(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?) or (?:less|fewer)"), "at_most"),
    (re.compile(r"(\d+(?:\.\d+)?)"), "exactly"),
)


def option_range(label: str) -> tuple[float, bool, float, bool] | None:
    """``(low, low_inclusive, high, high_inclusive)`` for labels like ``"3-5 years"``,
    ``"10+"``, ``"Less than 1 year"``; None when the label is not a numeric range."""
    text = _UNIT.sub("", question_key(label)).strip()
    for pattern, kind in _RANGE_PATTERNS:
        match = pattern.fullmatch(text)
        if match is None:
            continue
        numbers = [float(g) for g in match.groups() if g is not None]
        if kind == "between":
            return numbers[0], True, numbers[1], True
        if kind == "at_least":
            return numbers[0], True, math.inf, False
        if kind == "above":
            return numbers[0], False, math.inf, False
        if kind == "below":
            return -math.inf, False, numbers[0], False
        if kind == "at_most":
            return -math.inf, False, numbers[0], True
        return numbers[0], True, numbers[0], True
    return None


def _in_range(number: float, bounds: tuple[float, bool, float, bool]) -> bool:
    low, low_inc, high, high_inc = bounds
    above = number > low or (low_inc and number == low)
    below = number < high or (high_inc and number == high)
    return above and below


# --- option matching -----------------------------------------------------------------


def match_options(
    fld: ApplicationField, raw: str | int | float | bool, *, numeric_ranges: bool = False
) -> list[FieldOption]:
    """Usable options whose visible label says ``raw``.

    Booleans match Yes/True or No/False labels. Text matches a label exactly (after
    ``question_key``) or, for country/state fields, an equivalent spelling. With
    ``numeric_ranges``, a number matches options whose label is a range containing it
    (only when no label matches exactly)."""
    options = usable_options(fld)
    if isinstance(raw, bool):
        words = _TRUE_WORDS if raw else _FALSE_WORDS
        return [o for o in options if question_key(o.label) in words]
    spellings = _spellings(render_scalar(raw), fld.semantic_type)
    exact = [o for o in options if question_key(o.label) in spellings]
    if exact or not numeric_ranges:
        return exact
    number = as_number(raw)
    if number is None:
        return []
    ranged = []
    for option in options:
        bounds = option_range(option.label)
        if bounds is not None and _in_range(number, bounds):
            ranged.append(option)
    return ranged


def translate(fld: ApplicationField, raw: RawValue, *, numeric_ranges: bool = False) -> Translation:
    """Map ``raw`` onto ``fld``'s control, or explain why it cannot be mapped."""
    control = fld.control_type
    result: Translation
    if control in (ControlType.TEXT, ControlType.TEXTAREA, ControlType.TYPEAHEAD):
        # A lookup is typed as text; the site's matching suggestion is chosen later.
        result = _to_text(fld, raw)
    elif control in (ControlType.SELECT, ControlType.RADIO):
        result = _to_choice(fld, raw, numeric_ranges=numeric_ranges)
    elif control in (ControlType.MULTISELECT, ControlType.CHECKBOX_GROUP):
        result = _to_multi_choice(fld, raw)
    elif control is ControlType.CHECKBOX:
        result = _to_boolean(raw)
    else:
        return Unmapped(f"a {control.value.lower()} control cannot take this value")
    if isinstance(result, Mapped):
        problems = answer_problems(fld, result.value)
        if problems:
            return Unmapped("; ".join(problems))
    return result


def _to_text(fld: ApplicationField, raw: RawValue) -> Translation:
    if isinstance(raw, list):
        return Unmapped("a list of choices cannot fill a text field")
    text = render_scalar(raw)
    if not text.strip():
        return Unmapped("the known value is empty")
    if fld.input_type == "number" and as_number(raw) is None:
        return Unmapped(f"{text!r} is not a number")
    if fld.max_length is not None and len(text) > fld.max_length:
        return Unmapped(f"the known answer is longer than the {fld.max_length}-character limit")
    return Mapped(TextValue(text=text))


def _to_choice(fld: ApplicationField, raw: RawValue, *, numeric_ranges: bool) -> Translation:
    if isinstance(raw, list):
        if len(raw) != 1:
            return Unmapped("several saved choices cannot answer a single-choice question")
        raw = raw[0]
    matches = match_options(fld, raw, numeric_ranges=numeric_ranges)
    if len(matches) == 1:
        return Mapped(_choice(matches[0]))
    shown = render_scalar(raw)
    if not matches:
        return Unmapped(f"{shown!r} is not one of the options")
    return Unmapped(
        f"{shown!r} matches more than one option", tuple(_choice(o) for o in matches)
    )


def _to_multi_choice(fld: ApplicationField, raw: RawValue) -> Translation:
    if isinstance(raw, bool):
        return Unmapped("a yes/no value cannot select options")
    items: list[str | int | float] = list(raw) if isinstance(raw, list) else [raw]
    chosen: list[FieldOption] = []
    for item in items:
        matches = match_options(fld, item)
        if len(matches) != 1:
            state = "is not one of" if not matches else "matches more than one of"
            return Unmapped(f"{render_scalar(item)!r} {state} the options")
        option = FieldOption(value=matches[0].value, label=matches[0].label)
        if option not in chosen:
            chosen.append(option)
    return Mapped(MultiChoiceValue(choices=chosen))


def _to_boolean(raw: RawValue) -> Translation:
    if isinstance(raw, bool):
        return Mapped(BooleanValue(checked=raw))
    if isinstance(raw, str):
        key = question_key(raw)
        if key in _TRUE_WORDS:
            return Mapped(BooleanValue(checked=True))
        if key in _FALSE_WORDS:
            return Mapped(BooleanValue(checked=False))
    return Unmapped(f"{raw!r} is not a yes/no answer")
