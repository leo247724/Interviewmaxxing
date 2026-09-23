"""Conservative parsing of location and remote-eligibility text.

Job sources state locations in a handful of common shapes: ``"Austin, TX"``,
``"Austin, Texas, United States"``, ``"Austin TX"``, ``"Greater Austin Area"``,
``"Texas, United States"``, ``"United States"``, ``"Remote (US)"``, ``"Hybrid - Austin,
TX"`` or several places separated by ``;`` or ``|``. This module reads those shapes into
:class:`Place` values with a locality, a region (US state code) and a country code,
each only when the text states it. Nothing is inferred: a region never implies a
locality, and text that does not name a place yields no place at all.

Aliases are limited to US states and a fixed list of country names; anything else is
kept verbatim as a locality so it can be compared but never mistaken for a region or
country.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

US_STATES: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "puerto rico": "PR",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}
_STATE_CODES = frozenset(US_STATES.values())

COUNTRIES: dict[str, str] = {
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "u.s.": "US",
    "u.s": "US",
    "u.s.a.": "US",
    "u.s.a": "US",
    "us": "US",
    "canada": "CA",
    "united kingdom": "GB",
    "uk": "GB",
    "u.k.": "GB",
    "great britain": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "ireland": "IE",
    "australia": "AU",
    "new zealand": "NZ",
    "germany": "DE",
    "france": "FR",
    "spain": "ES",
    "italy": "IT",
    "netherlands": "NL",
    "the netherlands": "NL",
    "belgium": "BE",
    "switzerland": "CH",
    "austria": "AT",
    "sweden": "SE",
    "norway": "NO",
    "denmark": "DK",
    "finland": "FI",
    "poland": "PL",
    "portugal": "PT",
    "czech republic": "CZ",
    "czechia": "CZ",
    "hungary": "HU",
    "romania": "RO",
    "greece": "GR",
    "ukraine": "UA",
    "turkey": "TR",
    "israel": "IL",
    "united arab emirates": "AE",
    "uae": "AE",
    "south africa": "ZA",
    "nigeria": "NG",
    "kenya": "KE",
    "egypt": "EG",
    "india": "IN",
    "pakistan": "PK",
    "singapore": "SG",
    "malaysia": "MY",
    "indonesia": "ID",
    "philippines": "PH",
    "vietnam": "VN",
    "thailand": "TH",
    "japan": "JP",
    "south korea": "KR",
    "china": "CN",
    "mexico": "MX",
    "brazil": "BR",
    "argentina": "AR",
    "colombia": "CO",
    "chile": "CL",
}

_GLOBAL = frozenset(
    {"worldwide", "world", "global", "globally", "anywhere", "any location", "international"}
)
_NOISE = frozenset(
    {
        "remote",
        "hybrid",
        "onsite",
        "on-site",
        "on site",
        "in-office",
        "in office",
        "office",
        "flexible",
        "multiple locations",
        "various locations",
        "several locations",
        "multiple cities",
        "various",
        "multiple",
        "nationwide",
        "n/a",
        "na",
        "tbd",
        "tba",
        "not specified",
        "unspecified",
        "other",
        "any",
        "none",
        "unknown",
    }
)

_SEGMENT_SPLIT = re.compile(r"\s*(?:;|\||\n|/|•|·)\s*|\s+or\s+", re.IGNORECASE)
_DASHES = "-" + chr(0x2013) + chr(0x2014)  # hyphen, en dash, em dash
_PART_SPLIT = re.compile(rf"\s*(?:,|:|\s[{_DASHES}]\s)\s*")
_BRACKETS = re.compile(r"[()\[\]]")
_ZIP = re.compile(r"\s+\d{5}(?:-\d{4})?$")
_PREFIX = re.compile(
    r"^(?:based in|located in|office in|offices in|headquartered in|hq in|hq|in|greater|"
    r"downtown|metro|the|anywhere in|anywhere within|within|throughout|across)\s+"
)
_SUFFIX = re.compile(
    r"(?:[\s-]+(?:only|residents?|based|citizens|area|metro|metropolitan|region|"
    r"and surrounding areas?|(?:and|or|&|\+)\s+(?:remote|hybrid|onsite|on-site)))+$"
)
_LOCALITY_SPLIT = re.compile(rf"\s*[{_DASHES}/]\s*")


class RemoteRegionStatus(StrEnum):
    MATCH = "MATCH"
    """The stated eligibility covers the preferred region (or is worldwide)."""
    OUTSIDE = "OUTSIDE"
    """Every stated place is explicitly another country (or region) than preferred."""
    AMBIGUOUS = "AMBIGUOUS"
    """Narrower than the preferred region, unrecognized, or unparseable."""


@dataclass(frozen=True, slots=True)
class Place:
    locality: str | None = None
    """City or other locality text, casefolded, exactly as stated."""
    region: str | None = None
    """US state code when the text states a state, else ``None``."""
    country: str | None = None
    """Country code when stated; a US state implies ``"US"``."""
    worldwide: bool = False
    """The text said worldwide/anywhere (an eligibility statement, not a place)."""

    @property
    def stated(self) -> bool:
        return bool(self.locality or self.region or self.country or self.worldwide)

    @property
    def whole_country(self) -> bool:
        return bool(self.country) and not self.region and not self.locality


def _normalize(part: str) -> str:
    key = " ".join(part.casefold().split())
    key = _ZIP.sub("", key)
    while True:
        stripped = _SUFFIX.sub("", _PREFIX.sub("", key)).strip(" .,-")
        if stripped == key:
            return key
        key = stripped


_Kind = str  # "country" | "region" | "locality" | "global" | "noise"


def _is_state_code(token: str) -> bool:
    return len(token) == 2 and token.upper() in _STATE_CODES and token != token.lower()


def _classify(part: str) -> list[tuple[_Kind, str, bool]]:
    """``[(kind, value, is_state_name)]``: usually one item; ``"Austin TX"`` gives two."""
    key = _normalize(part)
    if not key:
        return [("noise", "", False)]
    if key in _GLOBAL:
        return [("global", "", False)]
    if key in _NOISE:
        return [("noise", "", False)]
    if key in COUNTRIES:
        return [("country", COUNTRIES[key], False)]
    if key in US_STATES:
        return [("region", US_STATES[key], True)]
    if len(key) == 2 and key.upper() in _STATE_CODES:
        return [("region", key.upper(), False)]
    words = key.split()
    original_tail = part.split()[-1].strip(" .,") if part.split() else ""
    for size in (2, 1):
        if len(words) > size:
            tail = " ".join(words[-size:])
            head = " ".join(words[:-size])
            if tail in US_STATES:
                return [("locality", head, False), ("region", US_STATES[tail], True)]
            if size == 1 and _is_state_code(original_tail):
                return [("locality", head, False), ("region", tail.upper(), False)]
    return [("locality", key, False)]


def _assemble(parts: list[tuple[_Kind, str, bool]]) -> list[Place]:
    # A state *name* directly followed by a region code is the city of that name
    # ("New York, NY", "Washington, DC").
    kinds = list(parts)
    for i in range(len(kinds) - 1):
        kind, value, is_name = kinds[i]
        if kind == "region" and is_name and kinds[i + 1][0] == "region":
            name = next(n for n, code in US_STATES.items() if code == value)
            kinds[i] = ("locality", name, False)
    places: list[Place] = []
    locality: str | None = None
    region: str | None = None
    country: str | None = None
    country_stated = False  # False while the country is only implied by a US state

    def flush() -> None:
        nonlocal locality, region, country, country_stated
        if locality or region or country:
            places.append(Place(locality=locality, region=region, country=country))
        locality = region = country = None
        country_stated = False

    for kind, value, _ in kinds:
        if kind == "noise":
            continue
        if kind == "global":
            flush()
            places.append(Place(worldwide=True))
        elif kind == "locality":
            flush()
            locality = value
        elif kind == "region":
            if region:
                flush()
            region, country = value, "US"
        elif kind == "country":
            if country_stated or (country and country != value):
                flush()
            country, country_stated = value, True
    flush()
    return places


def parse_places(text: str | None) -> list[Place]:
    """Every stated place in ``text``; an empty list when it names none."""
    if not text or not text.strip():
        return []
    cleaned = _BRACKETS.sub(", ", text)
    places: list[Place] = []
    for segment in _SEGMENT_SPLIT.split(cleaned):
        parts: list[tuple[_Kind, str, bool]] = []
        for raw in _PART_SPLIT.split(segment):
            if raw.strip():
                parts.extend(_classify(raw))
        places.extend(p for p in _assemble(parts) if p.stated)
    return places


def parse_place(text: str | None) -> Place:
    """The first stated place in ``text``, or an empty :class:`Place`."""
    places = parse_places(text)
    return places[0] if places else Place()


def _locality_components(name: str) -> set[str]:
    return {name, *(c for c in _LOCALITY_SPLIT.split(name) if c)}


def same_locality(a: str, b: str) -> bool:
    """``"austin"`` matches ``"austin"`` and a hyphenated metro such as
    ``"austin-round rock"``, never a different city."""
    return a == b or b in _locality_components(a) or a in _locality_components(b)


def same_place(place: Place, target: Place) -> bool:
    """The listing place names the target locality, with no stated region or
    country contradicting it. Regions and countries are compared only when both
    sides state them."""
    if not (place.locality and target.locality):
        return False
    if not same_locality(place.locality, target.locality):
        return False
    if place.region and target.region and place.region != target.region:
        return False
    return not (place.country and target.country and place.country != target.country)


def explicitly_elsewhere(place: Place, target: Place) -> bool:
    """The listing place states a different locality, region or country than the
    target. An incomplete place (only a matching region or country) is not elsewhere."""
    if place.locality and target.locality and not same_locality(place.locality, target.locality):
        return True
    if place.region and target.region and place.region != target.region:
        return True
    return bool(place.country and target.country and place.country != target.country)


def remote_eligibility_status(stated: str | None, preferred: str) -> RemoteRegionStatus:
    """Compare stated remote eligibility text with the preferred eligible region.

    The preferred region is usually a country ("United States"). Text naming that
    country, or worldwide eligibility, is a MATCH. Text naming only other countries is
    OUTSIDE. Text that is narrower (a state), unrecognized (an unknown region name) or
    unparseable is AMBIGUOUS: the candidate's residence is not known here.
    """
    target = parse_place(preferred)
    places = parse_places(re.sub(r"\s+(?:and|&)\s+", ";", stated, flags=re.IGNORECASE) if stated else stated)
    if not places or not target.stated:
        return RemoteRegionStatus.AMBIGUOUS
    verdicts: list[RemoteRegionStatus] = []
    for place in places:
        if place.worldwide:
            verdicts.append(RemoteRegionStatus.MATCH)
        elif target.whole_country:
            if place.country == target.country:
                verdicts.append(
                    RemoteRegionStatus.MATCH
                    if place.whole_country
                    else RemoteRegionStatus.AMBIGUOUS
                )
            elif place.country:
                verdicts.append(RemoteRegionStatus.OUTSIDE)
            else:
                verdicts.append(RemoteRegionStatus.AMBIGUOUS)
        elif target.region and not target.locality:
            if (place.region == target.region and not place.locality) or (
                place.whole_country and place.country == target.country
            ):
                verdicts.append(RemoteRegionStatus.MATCH)
            elif (place.country and target.country and place.country != target.country) or (
                place.region and place.region != target.region
            ):
                verdicts.append(RemoteRegionStatus.OUTSIDE)
            else:
                verdicts.append(RemoteRegionStatus.AMBIGUOUS)
        elif same_place(place, target) or (place.whole_country and place.country == target.country):
            verdicts.append(RemoteRegionStatus.MATCH)
        elif explicitly_elsewhere(place, target):
            verdicts.append(RemoteRegionStatus.OUTSIDE)
        else:
            verdicts.append(RemoteRegionStatus.AMBIGUOUS)
    if RemoteRegionStatus.MATCH in verdicts:
        return RemoteRegionStatus.MATCH
    if all(v is RemoteRegionStatus.OUTSIDE for v in verdicts):
        return RemoteRegionStatus.OUTSIDE
    return RemoteRegionStatus.AMBIGUOUS
