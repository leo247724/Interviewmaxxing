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

DESIRED_SALARY_QUESTION = "What is your desired salary?"
"""The saved question of the desired salary (simple answers ``desired_salary``), which the
salary derivation reads first among the person's salary answers."""

WORK_ARRANGEMENTS: tuple[str, ...] = ("remote", "hybrid", "on-site")
"""The closed vocabulary of ``work_arrangement_preference``."""

METRO_AREA_QUESTION = ("Which towns and cities around where you live are your metro area, where "
                       "on-site or hybrid work is fine?")
"""The saved question of the person's metro area (simple answers ``metro_area``, round 13): the
places around their own city they commute to, comma-separated, as an untyped GLOBAL saved
answer. A job in the metro may be on-site or hybrid; a job anywhere else is remote. Only the
work-arrangement derivation reads it, never a wording match."""


def stated_metro_area(question: str) -> bool:
    """Whether a saved answer's question is the metro-area question."""
    return normalize_text(question) == normalize_text(METRO_AREA_QUESTION)

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


# --- middle name and time zone (round 15) ------------------------------------------------

MIDDLE_NAME_QUESTION = "What is your middle name?"
"""The saved question of the simple answers' ``middle_name`` (round 15): the person's middle
name, or "N/A" when they have none (the importer stores "none" and the like as "N/A")."""

NO_MIDDLE_NAME = "N/A"
_NONE_WORDS = re.compile(r"^(?:none|no|n/?a|na|no middle name|-+|—)$", re.IGNORECASE)


def normalize_middle_name(value: str) -> str:
    """The person's middle name as stated, or "N/A" for none ("none", "no", "n/a", "-")."""
    cleaned = " ".join(value.split())
    return NO_MIDDLE_NAME if _NONE_WORDS.match(cleaned) else cleaned


TIME_ZONE_QUESTION = "What is your time zone?"
"""The saved question of the simple answers' ``time_zone`` (round 15): the person's own time
zone ("Central"), not the time zones they can work in (``available_time_zones``)."""

US_TIME_ZONES: tuple[str, ...] = ("eastern", "central", "mountain", "pacific", "alaska", "hawaii")
_ZONE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("eastern", re.compile(r"\beastern\b|\be[sd]?t\b|\bnew[_ ]york\b", re.IGNORECASE)),
    ("central", re.compile(r"\bcentral\b|\bc[sd]?t\b|\bchicago\b", re.IGNORECASE)),
    ("mountain", re.compile(r"\bmountain\b|\bm[sd]?t\b|\bdenver\b|\bphoenix\b|\barizona\b", re.IGNORECASE)),
    ("pacific", re.compile(r"\bpacific\b|\bp[sd]?t\b|\blos[_ ]angeles\b", re.IGNORECASE)),
    ("alaska", re.compile(r"\balaska\w*\b|\bak[sd]?t\b|\banchorage\b", re.IGNORECASE)),
    ("hawaii", re.compile(r"\bhawaii\w*\b|\bh[sd]?t\b|\bhonolulu\b", re.IGNORECASE)),
)


def time_zone_of(text: str) -> str | None:
    """The one US time zone ``text`` names ("Central", "CST", "Central Time (US & Canada)",
    "America/Chicago" → "central"), else None (none, several, or only a UTC offset)."""
    zones = [zone for zone, pattern in _ZONE_PATTERNS if pattern.search(text)]
    return zones[0] if len(zones) == 1 else None


_STATE_ZONES: dict[str, str] = {
    **dict.fromkeys(("CT", "DE", "DC", "GA", "ME", "MD", "MA", "NH", "NJ", "NY", "NC", "OH", "PA",
                     "RI", "SC", "VT", "VA", "WV", "FL", "IN", "KY", "MI"), "eastern"),
    **dict.fromkeys(("AL", "AR", "IL", "IA", "LA", "MN", "MS", "MO", "OK", "WI", "TN", "KS", "NE",
                     "ND", "SD", "TX"), "central"),
    **dict.fromkeys(("AZ", "CO", "MT", "NM", "UT", "WY", "ID"), "mountain"),
    **dict.fromkeys(("CA", "WA", "OR", "NV"), "pacific"),
    "AK": "alaska", "HI": "hawaii",
}
"""Each state's zone, the majority one where a state spans two (``_MINORITY_CITIES``)."""
_MINORITY_CITIES: dict[str, dict[str, str]] = {
    "FL": dict.fromkeys(("pensacola", "panama city", "fort walton beach", "destin", "navarre",
                         "crestview", "niceville", "milton"), "central"),
    "IN": dict.fromkeys(("gary", "hammond", "evansville", "merrillville", "valparaiso",
                         "michigan city", "crown point", "portage", "east chicago"), "central"),
    "KY": dict.fromkeys(("bowling green", "owensboro", "paducah", "hopkinsville", "madisonville",
                         "murray"), "central"),
    "MI": dict.fromkeys(("iron mountain", "menominee", "ironwood"), "central"),
    "TN": dict.fromkeys(("knoxville", "chattanooga", "johnson city", "kingsport", "bristol",
                         "cleveland", "oak ridge", "maryville", "morristown"), "eastern"),
    "KS": dict.fromkeys(("goodland", "sharon springs", "tribune", "syracuse"), "mountain"),
    "NE": dict.fromkeys(("scottsbluff", "sidney", "alliance", "ogallala", "chadron"), "mountain"),
    "ND": dict.fromkeys(("dickinson", "bowman", "beach"), "mountain"),
    "SD": dict.fromkeys(("rapid city", "spearfish", "sturgis", "belle fourche", "hot springs"), "mountain"),
    "TX": dict.fromkeys(("el paso", "horizon city", "socorro", "canutillo", "anthony", "fabens"), "mountain"),
    "ID": dict.fromkeys(("coeur d'alene", "lewiston", "moscow", "sandpoint", "post falls"), "pacific"),
    "OR": dict.fromkeys(("ontario", "nyssa", "vale"), "mountain"),
    "NV": dict.fromkeys(("west wendover",), "mountain"),
    "AK": dict.fromkeys(("adak", "atka"), "hawaii"),
}
"""Cities of a state that spans two zones in its minority zone (the larger cities only)."""


def address_time_zone(city: str | None, state_code: str | None) -> str | None:
    """The US time zone of a verified address: the state's zone, or the minority zone for a
    listed city of a state that spans two ("El Paso, TX" is Mountain, "Austin, TX" Central)."""
    if not state_code:
        return None
    code = state_code.upper()
    minority = _MINORITY_CITIES.get(code, {}).get(" ".join((city or "").casefold().split()))
    return minority or _STATE_ZONES.get(code)
