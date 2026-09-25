"""The saved desired salary, read deterministically (round 10).

A saved salary states one amount, a pay period and perhaps a currency ("USD 95,000 per
year", "$45/hr", "95k annually"). Three readings of it need no model call:

- a range select (``salary_range``, ``range_options``, ``containing_option``): the one
  option whose numeric bounds contain the amount, in the same period;
- a pay-period select: the period the value states (``periods_named``);
- a salary-typed field of any wording (``salary_wording``, ``convert_amount``): the plain
  desired salary is a base figure, converted to the period the wording names; a range or
  minimum wording takes it as the minimum; a total-compensation wording states it as the
  base salary.

Nothing here produces a value: every answer is the person's own saved text or one of the
site's own options."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from interviewmaxxing_core import ApplicationField, FieldOption

PERIOD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("year", re.compile(r"per\s+year|/\s*(?:yr|year)\b|\byearly\b|\bannual(?:ly|ized)?\b|"
                        r"per\s+annum|\ba\s+year\b|\bp\.?a\.?(?=\s|$)", re.IGNORECASE)),
    ("hour", re.compile(r"per\s+hour|/\s*(?:hr|hour)\b|\bhourly\b|\ban\s+hour\b", re.IGNORECASE)),
    ("month", re.compile(r"per\s+month|/\s*(?:mo|month)\b|\bmonthly\b|\ba\s+month\b", re.IGNORECASE)),
    ("week", re.compile(r"per\s+week|/\s*(?:wk|week)\b|\bweekly\b|\ba\s+week\b", re.IGNORECASE)),
    ("day", re.compile(r"per\s+day|/\s*day\b|\bdaily\b|\ba\s+day\b", re.IGNORECASE)),
)
"""The pay period a text names ("USD 95,000 per year", "$45/hr", "Expected annual salary")."""
_AMOUNT = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*([kKmM](?![a-zA-Z]))?")
_CURRENCY_MARKS: dict[str, str] = {
    "$": "USD", "usd": "USD", "us$": "USD", "dollar": "USD", "dollars": "USD",
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "£": "GBP", "gbp": "GBP", "pound": "GBP", "pounds": "GBP",
    "cad": "CAD", "c$": "CAD", "aud": "AUD", "a$": "AUD", "inr": "INR", "₹": "INR",
}
_CURRENCY = re.compile(r"[$€£₹]|\b(?:usd|us\$|c\$|a\$|eur|gbp|cad|aud|inr|dollars?|euros?|pounds?)\b",
                       re.IGNORECASE)
_APPROXIMATE = re.compile(
    r"\b(?:about|around|approximately|roughly|circa|at least|minimum|min|more than|less than|"
    r"up to|or more|and above|and up|or above|over|under|below|negotiable)\b|[~+]|"
    r"\d\s*[kKmM]?\s*(?:-|\u2013|\u2014|to)\s*[$€£]?\d", re.IGNORECASE)
"""A saved value that states no one exact amount: approximate, a floor, or a range."""
_NOT_BASE = re.compile(
    r"\b(?:ote|on[- ]target|total|bonus|bonuses|equity|commission|commissions|package|"
    r"benefits|tc|all[- ]in|including|incl)\b", re.IGNORECASE)
"""A saved value that names more than a base figure."""
_BARE_YEAR = re.compile(r"^(?:19|20)\d\d$")
_RANGE_WORDS = frozenset({
    "less", "than", "under", "below", "fewer", "up", "to", "more", "over", "above", "or",
    "and", "between", "plus", "greater", "higher", "lower", "at", "least", "most", "minimum",
    "maximum", "min", "max", "k", "m", "per", "a", "an", "year", "yr", "annually", "annual",
    "annualized", "yearly", "annum", "month", "mo", "monthly", "week", "wk", "weekly", "day",
    "daily", "hour", "hr", "hourly",
    "usd", "us", "eur", "gbp", "cad", "aud", "inr", "dollars", "dollar", "euros", "euro",
    "pounds", "pound", "salary", "range", "thousand", "million", "p", "pa"})
"""Words a salary-range option may contain besides its amounts, currency and period
("$50,000 - $59,999", "Under $30,000", "$200K and above", "$20 - $25 per hour")."""
_RANGE_SEPARATOR = re.compile(r"\d\s*[kKmM]?\s*(?:-|\u2013|\u2014|to|and)\s*[$€£₹]?\d",
                              re.IGNORECASE)
_UPPER_BOUND = re.compile(r"\b(?:less than|under|below|fewer than|up to|or less|or under|"
                          r"or below|and under|and below|at most|maximum|max)\b", re.IGNORECASE)
_LOWER_BOUND = re.compile(r"\+|\b(?:more than|over|above|or more|and above|or above|and over|"
                          r"or over|and up|or higher|and higher|greater than|at least|"
                          r"minimum|min)\b", re.IGNORECASE)
_NON_ITEM = re.compile(
    r"^(?:other|none|n/?a|not applicable|prefer not to (?:say|answer|disclose)|decline to "
    r"(?:say|answer|state)|negotiable|open|flexible|not sure|unsure|no preference|"
    r"depends|varies|select|choose|please select|please choose)\b", re.IGNORECASE)
"""Options of a range select that are not ranges and never contain an amount."""
_HOURLY_CEILING = 1_000.0
_ANNUAL_FLOOR = 10_000.0
"""Ranges that state no period are read by magnitude: bounds under 1,000 are hourly,
bounds of 10,000 and above are yearly; anything between (monthly) is not read."""


def amounts_named(text: str) -> list[float]:
    """Every amount in ``text`` with its thousands separators and k/m suffix applied."""
    values = []
    for whole, fraction, suffix in _AMOUNT.findall(text):
        number = float(whole.replace(",", "") + ("." + fraction if fraction else ""))
        values.append(number * {"k": 1e3, "m": 1e6}.get(suffix.lower(), 1.0))
    return values


def periods_named(text: str) -> list[str]:
    """The pay periods ``text`` names, in ``PERIOD_PATTERNS`` order ("year", "hour" …)."""
    return [period for period, pattern in PERIOD_PATTERNS if pattern.search(text)]


def currencies_named(text: str) -> set[str]:
    """The ISO currency codes ``text`` names by symbol, code or word."""
    return {_CURRENCY_MARKS[mark.casefold()] for mark in _CURRENCY.findall(text)
            if mark.casefold() in _CURRENCY_MARKS}


@dataclass(frozen=True, slots=True)
class SavedSalary:
    """One exact amount the saved salary states, with its period and currency when
    stated, and whether it is a plain (base) figure."""

    amount: float
    period: str | None
    currency: str | None
    base: bool


def parse_salary(value: str) -> SavedSalary | None:
    """The one exact amount a saved salary states ("USD 95,000 per year", "$45/hr",
    "95k annually", "95,000"); None for a range, several amounts, an approximate amount,
    a bare year or contradictory periods or currencies."""
    if _APPROXIMATE.search(value):
        return None
    amounts = amounts_named(value)
    matches = _AMOUNT.findall(value)
    if len(amounts) != 1 or (_BARE_YEAR.match(matches[0][0]) and not matches[0][2]
                             and not currencies_named(value) and not periods_named(value)):
        return None
    periods, currencies = periods_named(value), currencies_named(value)
    if len(periods) > 1 or len(currencies) > 1:
        return None
    return SavedSalary(amount=amounts[0], period=periods[0] if periods else None,
                       currency=next(iter(currencies), None), base=_NOT_BASE.search(value) is None)


@dataclass(frozen=True, slots=True)
class SalaryRange:
    """Inclusive numeric bounds of one range option, with the period and currency it
    states (None when it states none)."""

    low: float
    high: float
    period: str | None
    currency: str | None

    def contains(self, amount: float) -> bool:
        return self.low <= amount <= self.high


def salary_range(label: str) -> SalaryRange | None:
    """The bounds a range option states: "$50,000 - $59,999", "Between $50k and $60k",
    "Under $30,000", "$200,000+", "$200K and above", "$20 - $25 per hour", or one exact
    amount ("$95,000"); None for anything that names something else ("Google Analytics
    4", "5-10 years")."""
    text = label.casefold()
    words = re.findall(r"[a-z]+", _CURRENCY.sub(" ", _AMOUNT.sub(" ", text)))
    if any(word not in _RANGE_WORDS for word in words):
        return None
    periods, currencies = periods_named(label), currencies_named(label)
    if len(periods) > 1 or len(currencies) > 1:
        return None
    period, currency = (periods[0] if periods else None), next(iter(currencies), None)
    amounts = amounts_named(text)
    if len(amounts) == 2 and _RANGE_SEPARATOR.search(text):
        return SalaryRange(min(amounts), max(amounts), period, currency)
    if len(amounts) != 1:
        return None
    if _UPPER_BOUND.search(text):
        return SalaryRange(-math.inf, amounts[0], period, currency)
    if _LOWER_BOUND.search(text):
        return SalaryRange(amounts[0], math.inf, period, currency)
    return SalaryRange(amounts[0], amounts[0], period, currency)


def range_options(options: Sequence[FieldOption]) -> list[tuple[FieldOption, SalaryRange]]:
    """The range options of a salary select (at least two), or nothing when any option
    is neither a range nor a non-answer ("Prefer not to say", "Negotiable")."""
    ranges: list[tuple[FieldOption, SalaryRange]] = []
    for option in options:
        bounds = salary_range(option.label)
        if bounds is not None:
            ranges.append((option, bounds))
        elif not _NON_ITEM.match(option.label.strip()):
            return []
    return ranges if len(ranges) >= 2 else []


class RangeStatus(StrEnum):
    MAPPED = "MAPPED"
    NO_UNIT = "NO_UNIT"
    """The saved salary states no pay period."""
    UNIT_UNSTATED = "UNIT_UNSTATED"
    """Neither the options, the field's wording nor the bounds' magnitude state the
    period the ranges are in."""
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    NOT_CONTAINED = "NOT_CONTAINED"
    AMBIGUOUS = "AMBIGUOUS"
    """The amount sits on a boundary two ranges share."""


def options_period(ranges: Sequence[tuple[FieldOption, SalaryRange]],
                   wording: str) -> tuple[str | None, str | None]:
    """The period the ranges are in and where it was read: stated by the options
    (all agreeing), by the field's own wording, or by the bounds' magnitude."""
    stated = {bounds.period for _, bounds in ranges if bounds.period is not None}
    if len(stated) == 1:
        return next(iter(stated)), "options"
    if stated:
        return None, None
    named = periods_named(wording)
    if len(named) == 1:
        return named[0], "wording"
    if named:
        return None, None
    finite = [bound for _, bounds in ranges for bound in (bounds.low, bounds.high)
              if math.isfinite(bound)]
    if finite and all(bound < _HOURLY_CEILING for bound in finite):
        return "hour", "magnitude"
    if finite and all(bound >= _ANNUAL_FLOOR for bound in finite):
        return "year", "magnitude"
    return None, None


@dataclass(frozen=True, slots=True)
class RangeChoice:
    """The outcome of placing a saved salary on a range select."""

    option: FieldOption | None
    status: RangeStatus
    unit_source: str | None
    """Where the options' period was read: ``options``, ``wording`` or ``magnitude``."""
    period: str | None
    """The period the ranges are in (the saved amount is converted to it)."""


def containing_option(salary: SavedSalary, ranges: Sequence[tuple[FieldOption, SalaryRange]],
                      wording: str) -> RangeChoice:
    """The one range option containing the saved amount, converted to the period the
    ranges are in (``convert_amount``), or why there is none."""
    if salary.period is None:
        return RangeChoice(None, RangeStatus.NO_UNIT, None, None)
    period, source = options_period(ranges, wording)
    if period is None:
        return RangeChoice(None, RangeStatus.UNIT_UNSTATED, None, None)
    stated = {bounds.currency for _, bounds in ranges if bounds.currency is not None}
    stated |= currencies_named(wording) if not stated else set()
    if salary.currency is not None and stated and salary.currency not in stated:
        return RangeChoice(None, RangeStatus.CURRENCY_MISMATCH, source, period)
    amount = convert_amount(salary.amount, salary.period, period)
    containing = [option for option, bounds in ranges if bounds.contains(amount)]
    if not containing:
        return RangeChoice(None, RangeStatus.NOT_CONTAINED, source, period)
    if len(containing) > 1:
        return RangeChoice(None, RangeStatus.AMBIGUOUS, source, period)
    return RangeChoice(containing[0], RangeStatus.MAPPED, source, period)


HOURS_PER_YEAR = 2080.0
_PER_YEAR = {"year": 1.0, "month": 12.0, "week": 52.0, "day": 260.0, "hour": HOURS_PER_YEAR}
"""How many of each pay period make a year; hourly figures assume 2,080 hours."""
_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}


def convert_amount(amount: float, source: str, target: str) -> float:
    """``amount`` per ``source`` period as an amount per ``target`` period: monthly is
    annual / 12, hourly is annual / 2080, and the other way round from an hourly or
    monthly figure; rounded to the nearest 1 for an hourly figure and to the nearest
    100 otherwise."""
    if source == target:
        return amount
    annual = amount * _PER_YEAR[source]
    converted = annual / _PER_YEAR[target]
    return float(round(converted)) if target == "hour" else float(round(converted / 100) * 100)


def render_salary(amount: float, period: str | None, currency: str | None) -> str:
    """A figure in the person's terms: "$95,000 per year", "€7,900 per month", "45 per hour"."""
    number = f"{amount:,.0f}" if amount.is_integer() else f"{amount:,.2f}".rstrip("0").rstrip(".")
    symbol = _SYMBOLS.get(currency or "")
    figure = f"{symbol}{number}" if symbol else f"{currency} {number}" if currency else number
    return f"{figure} per {period}" if period else figure


def field_wording(field: ApplicationField) -> str:
    """The field's own wording and its section headings, for periods and currencies."""
    return " ".join([field.label, field.help_text or "", field.placeholder or "",
                     *field.section_context])


# --- what a salary wording asks for --------------------------------------------------------

_COMPENSATION_CLAUSE = re.compile(
    r"\b(?:ote|on[- ]target|total|bonus|bonuses|equity|commission|commissions|benefits|"
    r"package|tc|all[- ]in|stock|options|rsus?)\b", re.IGNORECASE)
"""A wording that asks for more than a base figure: answered as the desired base salary,
stated as such."""
_MINIMUM = re.compile(r"\b(?:minimum|min|floor|lowest|range|at least)\b", re.IGNORECASE)
"""A range or minimum wording: the saved figure is the minimum; no maximum is invented."""
_NOT_DERIVED = re.compile(
    r"\b(?:current|currently|present|previous|prior|last|most recent|history|maximum|max|"
    r"highest|ceiling|currency|reason|why|explain|justify)\b", re.IGNORECASE)
"""A salary question the desired salary does not answer (a current, previous or maximum
salary, a currency, an explanation): left to the wording decision."""
_SPECIFIC = re.compile(r"\b(?:as specific as possible|be specific|in detail|details)\b", re.IGNORECASE)


class WordingKind(StrEnum):
    PLAIN = "PLAIN"
    """A desired, expected, target, base or annual salary: the saved figure itself."""
    MINIMUM = "MINIMUM"
    """A range or minimum wording: the saved figure as the minimum."""
    COMPENSATION_CLAUSE = "COMPENSATION_CLAUSE"
    """OTE, total compensation, bonus or equity: the figure stated as the base salary."""
    NOT_DERIVED = "NOT_DERIVED"
    """Not settled here: the wording decision or the person decides."""


def salary_wording(question: str) -> WordingKind:
    """What a salary-typed question asks for, from its label, help text and field id."""
    if _NOT_DERIVED.search(question):
        return WordingKind.NOT_DERIVED
    if _COMPENSATION_CLAUSE.search(question):
        return WordingKind.COMPENSATION_CLAUSE
    if _MINIMUM.search(question):
        return WordingKind.MINIMUM
    return WordingKind.PLAIN


def asks_for_detail(question: str) -> bool:
    """A wording that asks the person to be specific ("as specific as possible")."""
    return _SPECIFIC.search(question) is not None


def render_amount(amount: float) -> str:
    """The bare number a numeric input takes ("95000", "45.5")."""
    return str(int(amount)) if amount.is_integer() else str(amount)
