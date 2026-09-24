"""Identity values in the form lookup and phone-picker controls expect.

A lookup (``ControlType.TYPEAHEAD``) asks the site to search for a place and commits
one of its own suggestions. It is answered from the verified identity address only
for the candidate's own location, city, state or country; the browser types the
value and the runner chooses among the site's suggestions if it has to.

A ``tel`` control with a country picker (``ApplicationField.expects_international_phone``)
selects the country from the number itself, so a national number is converted to
``+<dialing code><national number>`` using the identity country. A number that cannot
be converted reliably (unknown country, wrong number of digits, several possible
readings) is never guessed: the question goes to the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from interviewmaxxing_core import CandidateIdentity, SemanticType

from .questions import question_key
from .values import is_united_states, us_state_name

LOOKUP_TYPES: Final = frozenset(
    {SemanticType.LOCATION, SemanticType.CITY, SemanticType.STATE, SemanticType.COUNTRY}
)
"""Semantic types a lookup control is answered for from the verified identity."""


def _clean(value: str | None) -> str | None:
    text = " ".join((value or "").split())
    return text or None


def lookup_text(identity: CandidateIdentity, semantic_type: SemanticType) -> str | None:
    """What to type into a lookup asking for the candidate's own location.

    LOCATION is "City, Region", plus ", Country" outside the United States (a city is
    required); CITY is the city; STATE is the region, a two-letter US abbreviation
    spelled out ("TX" -> "Texas"); COUNTRY is the country as stored. None for any
    other type or a missing value."""
    address = identity.address
    country = _clean(address.country)
    if semantic_type is SemanticType.CITY:
        return _clean(address.city)
    if semantic_type is SemanticType.COUNTRY:
        return country
    if semantic_type is SemanticType.STATE:
        region = _clean(address.region)
        if region is not None and (country is None or is_united_states(country)):
            return us_state_name(region) or region
        return region
    if semantic_type is SemanticType.LOCATION:
        city = _clean(address.city)
        if city is None:
            return None
        parts = [city, _clean(address.region)]
        if country is not None and not is_united_states(country):
            parts.append(country)
        return ", ".join(p for p in parts if p)
    return None


# --- international phone numbers ----------------------------------------------------------


class PhoneFormatError(ValueError):
    """The verified phone number cannot be put in international form reliably."""


@dataclass(frozen=True, slots=True)
class DialingPlan:
    code: str
    """International dialing code without "+"."""
    trunk: str
    """National prefix dropped in international form ("0"; "1" in North America;
    empty where the national number keeps it, as in Italy)."""
    lengths: frozenset[int]
    """Valid lengths of the national (significant) number."""


_NORTH_AMERICA = DialingPlan("1", "1", frozenset({10}))
_UK = DialingPlan("44", "0", frozenset({9, 10}))

_PLANS: Final[dict[str, DialingPlan]] = {
    "canada": _NORTH_AMERICA,
    "united kingdom": _UK, "uk": _UK, "u.k": _UK, "great britain": _UK, "britain": _UK,
    "england": _UK, "scotland": _UK, "wales": _UK, "northern ireland": _UK,
    "ireland": DialingPlan("353", "0", frozenset({7, 8, 9})),
    "australia": DialingPlan("61", "0", frozenset({9})),
    "new zealand": DialingPlan("64", "0", frozenset({8, 9, 10})),
    "india": DialingPlan("91", "0", frozenset({10})),
    "germany": DialingPlan("49", "0", frozenset(range(6, 12))),
    "france": DialingPlan("33", "0", frozenset({9})),
    "spain": DialingPlan("34", "", frozenset({9})),
    "italy": DialingPlan("39", "", frozenset(range(6, 12))),
    "netherlands": DialingPlan("31", "0", frozenset({9})),
    "the netherlands": DialingPlan("31", "0", frozenset({9})),
    "mexico": DialingPlan("52", "", frozenset({10})),
    "brazil": DialingPlan("55", "0", frozenset({10, 11})),
    "japan": DialingPlan("81", "0", frozenset({9, 10})),
    "singapore": DialingPlan("65", "", frozenset({8})),
    "israel": DialingPlan("972", "0", frozenset({8, 9})),
    "philippines": DialingPlan("63", "0", frozenset({8, 9, 10})),
    "south africa": DialingPlan("27", "0", frozenset({9})),
}


def dialing_plan(country: str | None) -> DialingPlan | None:
    """The dialing plan of a country name as a profile stores it, if known."""
    if not country or not country.strip():
        return None
    if is_united_states(country):
        return _NORTH_AMERICA
    return _PLANS.get(question_key(country))


_NOT_DIGIT = re.compile(r"[^0-9]")
_EXAMPLE = "for example +44 20 7946 0018"


def international_phone(phone: str, country: str | None) -> str:
    """``+<dialing code><national number>`` for a verified national phone number.

    A number that already starts with "+" is returned unchanged. The national number
    is read with or without the national trunk prefix ("0", or "1" in North America),
    with the country code but no "+", or after "00"; it must have a valid length for
    the country and only one reading may be valid. Raises ``PhoneFormatError`` when
    the country's dialing code is unknown or the digits do not qualify."""
    text = phone.strip()
    if text.startswith("+"):
        return text
    lead = ("This phone field selects the country from the number, so it needs your "
            "number in international form (+ country code), but your verified phone "
            "number has no country code")
    name = (country or "").strip()
    plan = dialing_plan(country)
    if plan is None:
        where = (f"the dialing code for {name!r} is not known here" if name
                 else "your profile has no country")
        raise PhoneFormatError(f"{lead} and {where}. Enter it with its country code, {_EXAMPLE}")
    digits = _NOT_DIGIT.sub("", text)
    readings: set[str] = set()
    if digits.startswith("00"):
        if digits[2:].startswith(plan.code):
            readings.add(digits[2 + len(plan.code):])
    else:
        if plan.trunk and digits.startswith(plan.trunk):
            readings.add(digits[len(plan.trunk):])
        else:
            readings.add(digits)
        if digits.startswith(plan.code):
            readings.add(digits[len(plan.code):])
    valid = {number for number in readings if len(number) in plan.lengths}
    if len(valid) != 1:
        problem = ("does not have a valid number of digits" if not valid
                   else "can be read more than one way")
        raise PhoneFormatError(
            f"{lead}, and it {problem} for {name} (+{plan.code}). "
            f"Enter it with its country code, {_EXAMPLE}")
    return f"+{plan.code}{valid.pop()}"
