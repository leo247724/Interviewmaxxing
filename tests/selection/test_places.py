"""Location text parsing: common city/region/country shapes read without guessing."""

from __future__ import annotations

import pytest

from interviewmaxxing_selection import (
    Place,
    RemoteRegionStatus,
    explicitly_elsewhere,
    parse_place,
    parse_places,
    remote_eligibility_status,
    same_place,
)

AUSTIN = Place("austin", "TX", "US")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Austin, TX", [AUSTIN]),
        ("Austin, Texas, United States", [AUSTIN]),
        ("Austin, TX, USA", [AUSTIN]),
        ("Austin, TX 78701", [AUSTIN]),
        ("Austin TX", [AUSTIN]),
        ("Austin Tx", [AUSTIN]),
        ("Austin Texas", [AUSTIN]),
        ("Hybrid - Austin, TX", [AUSTIN]),
        ("Austin, TX (Hybrid)", [AUSTIN]),
        ("Austin, TX and Remote", [AUSTIN]),
        ("Austin, Texas Metropolitan Area", [AUSTIN]),
        ("Greater Austin Area", [Place("austin")]),
        ("Austin", [Place("austin")]),
        ("Austin, MN", [Place("austin", "MN", "US")]),
        ("Austin, Minnesota", [Place("austin", "MN", "US")]),
        ("New York, NY", [Place("new york", "NY", "US")]),
        ("Washington, DC", [Place("washington", "DC", "US")]),
        ("New York, NY; Austin, TX", [Place("new york", "NY", "US"), AUSTIN]),
        ("Austin or Dallas, TX", [Place("austin"), Place("dallas", "TX", "US")]),
        ("Austin/Dallas, TX", [Place("austin"), Place("dallas", "TX", "US")]),
        ("Austin-Round Rock, TX", [Place("austin-round rock", "TX", "US")]),
        ("Texas, United States", [Place(None, "TX", "US")]),
        ("Texas", [Place(None, "TX", "US")]),
        ("Based in TX", [Place(None, "TX", "US")]),
        ("California, United States", [Place(None, "CA", "US")]),
        ("United States", [Place(None, None, "US")]),
        ("US", [Place(None, None, "US")]),
        ("U.S. residents only", [Place(None, None, "US")]),
        ("US-based", [Place(None, None, "US")]),
        ("Anywhere in the US", [Place(None, None, "US")]),
        ("Remote (US)", [Place(None, None, "US")]),
        ("Remote - United States", [Place(None, None, "US")]),
        ("United States, Canada", [Place(None, None, "US"), Place(None, None, "CA")]),
        ("Canada only", [Place(None, None, "CA")]),
        ("Ontario, Canada", [Place("ontario", None, "CA")]),
        ("EMEA", [Place("emea")]),
        ("Worldwide", [Place(worldwide=True)]),
        ("Multiple Locations", []),
        ("Remote", []),
        ("", []),
        (None, []),
    ],
)
def test_parse_places(text: str | None, expected: list[Place]) -> None:
    assert parse_places(text) == expected


def test_parse_place_takes_the_first_stated_place() -> None:
    assert parse_place("Austin, TX; Dallas, TX") == AUSTIN
    assert parse_place("Various") == Place()
    assert not Place().stated
    assert Place(None, None, "US").whole_country
    assert not AUSTIN.whole_country


@pytest.mark.parametrize(
    ("place", "target", "same", "elsewhere"),
    [
        (AUSTIN, AUSTIN, True, False),
        (Place("austin"), AUSTIN, True, False),  # region unstated: compared only when stated
        (AUSTIN, Place("austin"), True, False),
        (Place("austin", "MN", "US"), AUSTIN, False, True),  # Austin, MN is not Austin, TX
        (Place("austin-round rock", "TX", "US"), AUSTIN, True, False),
        (Place("round rock", "TX", "US"), AUSTIN, False, True),
        (Place("dallas", "TX", "US"), AUSTIN, False, True),
        (Place(None, "TX", "US"), AUSTIN, False, False),  # incomplete: neither
        (Place(None, None, "US"), AUSTIN, False, False),
        (Place(None, "CA", "US"), AUSTIN, False, True),  # explicit other region
        (Place("austin", None, "CA"), AUSTIN, False, True),  # explicit other country
        (Place(), AUSTIN, False, False),
    ],
)
def test_same_place_and_explicitly_elsewhere(
    place: Place, target: Place, same: bool, elsewhere: bool
) -> None:
    assert same_place(place, target) is same
    assert explicitly_elsewhere(place, target) is elsewhere


@pytest.mark.parametrize(
    ("stated", "preferred", "expected"),
    [
        ("United States", "United States", RemoteRegionStatus.MATCH),
        ("US", "United States", RemoteRegionStatus.MATCH),
        ("USA", "United States", RemoteRegionStatus.MATCH),
        ("U.S. only", "United States", RemoteRegionStatus.MATCH),
        ("United States, Canada", "United States", RemoteRegionStatus.MATCH),
        ("Worldwide", "United States", RemoteRegionStatus.MATCH),
        ("Anywhere", "United States", RemoteRegionStatus.MATCH),
        ("Canada", "United States", RemoteRegionStatus.OUTSIDE),
        ("Canada only", "United States", RemoteRegionStatus.OUTSIDE),
        ("Ontario, Canada", "United States", RemoteRegionStatus.OUTSIDE),
        ("United Kingdom", "United States", RemoteRegionStatus.OUTSIDE),
        ("Texas", "United States", RemoteRegionStatus.AMBIGUOUS),  # narrower
        ("Texas, United States", "United States", RemoteRegionStatus.AMBIGUOUS),
        ("EMEA", "United States", RemoteRegionStatus.AMBIGUOUS),  # unrecognized
        ("Nationwide", "United States", RemoteRegionStatus.AMBIGUOUS),  # which nation?
        (None, "United States", RemoteRegionStatus.AMBIGUOUS),
        ("", "United States", RemoteRegionStatus.AMBIGUOUS),
        ("United States", "Texas", RemoteRegionStatus.MATCH),  # broader covers it
        ("Texas", "Texas", RemoteRegionStatus.MATCH),
        ("California", "Texas", RemoteRegionStatus.OUTSIDE),
        ("Austin, TX", "Texas", RemoteRegionStatus.AMBIGUOUS),
    ],
)
def test_remote_eligibility_status(
    stated: str | None, preferred: str, expected: RemoteRegionStatus
) -> None:
    assert remote_eligibility_status(stated, preferred) is expected


@pytest.mark.parametrize("stated", [
    "United States (except California)", "United States, excluding TX",
    "United States; Texas only", "United States; Canada only",
    "United States, subject to state approval", "United States (selected locations)",
    "United States, California", "Worldwide except the United States",
])
def test_compound_restrictions_never_disappear_behind_country(stated: str) -> None:
    assert remote_eligibility_status(stated, "United States") is RemoteRegionStatus.AMBIGUOUS


@pytest.mark.parametrize("stated", ["US", "United States", "US only", "US and Canada", "United States, Canada"])
def test_explicit_whole_country_eligibility_remains_positive(stated: str) -> None:
    assert remote_eligibility_status(stated, "United States") is RemoteRegionStatus.MATCH


@pytest.mark.parametrize("stated", ["Austin, ON, Canada", "Austin, Manitoba, Canada", "Austin, CA, Canada"])
def test_foreign_country_is_not_detached_from_locality(stated: str) -> None:
    places = parse_places(stated)
    assert places and all(p.country == "CA" for p in places)
    assert not any(same_place(p, AUSTIN) for p in places)
