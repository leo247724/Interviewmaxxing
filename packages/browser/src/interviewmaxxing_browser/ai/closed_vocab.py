"""Countries and US states matched to a site's options without a model call (round 11).

A phone-country select lists 244 options ("🇺🇸 United States (+1)"); Jev timed out
mapping "United States" onto them. A country or a state is a closed vocabulary: its option
is found by name, ISO code or a common alias after the dial code, flag and bracketed codes
are stripped. Only when nothing matches does Jev decide, and on a list longer than
``LONG_OPTION_LIST`` it sees only the options sharing a word with the value plus a bounded
sample."""

from __future__ import annotations

import re
from collections.abc import Sequence

from interviewmaxxing_core import FieldOption, normalize_text
from interviewmaxxing_generation.values import us_state_code

LONG_OPTION_LIST = 100
"""An option list longer than this is never sent to Jev whole."""
OPTION_SAMPLE = 20
"""Options beyond the word-sharing candidates that a bounded Jev decision still sees."""

_FLAGS = re.compile("[\U0001F1E6-\U0001F1FF]")
_DIAL = re.compile(r"\(?\s*\+\s?\d[\d\s-]*\)?")
_BRACKETED = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_TRAILING_CODE = re.compile(r"\s*[-\u2013\u2014|/,]\s*[A-Za-z]{2,3}\s*$")
COUNTRY_ALIASES: dict[str, frozenset[str]] = {
    "united states": frozenset({"united states", "united states of america", "usa", "us",
                                "u.s.", "u.s", "u.s.a.", "u.s.a", "america"}),
    "united kingdom": frozenset({"united kingdom", "uk", "u.k.", "u.k", "great britain", "gb",
                                 "united kingdom of great britain and northern ireland"}),
    "canada": frozenset({"canada", "ca"}),
    "mexico": frozenset({"mexico", "mx", "méxico"}),
    "germany": frozenset({"germany", "de", "deutschland"}),
    "france": frozenset({"france", "fr"}),
    "india": frozenset({"india", "ind"}),
    "australia": frozenset({"australia", "au"}),
    "ireland": frozenset({"ireland", "ie", "republic of ireland"}),
    "netherlands": frozenset({"netherlands", "nl", "the netherlands", "holland"}),
    "spain": frozenset({"spain", "es", "españa"}),
    "brazil": frozenset({"brazil", "br", "brasil"}),
}
"""Common spellings and ISO codes of the countries applicants name most."""
_ALIAS_OF = {alias: name for name, aliases in COUNTRY_ALIASES.items() for alias in aliases}


def _clean(label: str) -> str:
    """The option's name without its flag, dial code, bracketed codes or a trailing code."""
    text = _FLAGS.sub(" ", label)
    text = _DIAL.sub(" ", text)
    text = _BRACKETED.sub(" ", text)
    cleaned = _TRAILING_CODE.sub("", text)
    return normalize_text(cleaned if cleaned.strip() else text).strip(" .,-")


def country_key(text: str) -> str:
    """The country a label or a value names, by name, ISO code or alias ("USA" and
    "United States (+1)" are both "united states")."""
    cleaned = _clean(text)
    return _ALIAS_OF.get(cleaned, cleaned)


def country_options(options: Sequence[FieldOption], value: str) -> list[FieldOption]:
    """The enabled options naming the country ``value`` names."""
    key = country_key(value)
    return [o for o in options if not o.disabled and o.value.strip() and key
            and country_key(o.label) == key]


def state_options(options: Sequence[FieldOption], value: str) -> list[FieldOption]:
    """The enabled options naming the US state ``value`` names ("TX", "Texas", "TX - Texas")."""
    code = us_state_code(value)
    if code is None:
        return []
    found = []
    for option in options:
        if option.disabled or not option.value.strip():
            continue
        parts = [_clean(option.label), *re.split(r"\s*[-\u2013\u2014|,/]\s*", normalize_text(option.label))]
        if any(us_state_code(part) == code for part in parts if part):
            found.append(option)
    return found


def bounded_candidates(options: Sequence[FieldOption], value: str) -> list[FieldOption]:
    """For a list longer than ``LONG_OPTION_LIST``: the options sharing a word with the value
    (after aliases) plus the first ``OPTION_SAMPLE`` others, in page order."""
    words = set(re.findall(r"[a-z]{2,}", country_key(value))) | set(re.findall(r"[a-z]{2,}",
                                                                            normalize_text(value)))
    sharing = [o for o in options if words & set(re.findall(r"[a-z]{2,}", _clean(o.label)))]
    others = [o for o in options if o not in sharing][:OPTION_SAMPLE]
    chosen = {id(o) for o in [*sharing, *others]}
    return [o for o in options if id(o) in chosen]
