from __future__ import annotations

from datetime import UTC, datetime

import pytest

from interviewmaxxing_core import JobListing, JobSearchQuery, LocationPriority, WorkArrangement
from interviewmaxxing_jobs.ranking import location_tier, place_match, remote_eligibility
from interviewmaxxing_jobs.sources.base import make_listing

NOW = datetime(2026, 9, 22, 18, 0, tzinfo=UTC)


def fictional(location: str | None, arrangement: WorkArrangement,
              eligibility: str | None = None) -> JobListing:
    return make_listing(source="linkedin", source_listing_id="1", posting_url="https://jobs.test/1",
                        source_url="https://jobs.test/1", title="Paid Media Manager",
                        company="Fictional Widgets Co", location=location,
                        work_arrangement=arrangement, remote_eligibility=eligibility,
                        observed_at=NOW, evidence="fictional", query_id=None)


QUERY = JobSearchQuery()  # Austin, TX onsite/hybrid; US-wide remote; strongly prefer Austin


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Austin, TX", "confirmed"),
        ("Austin, TX 78701", "confirmed"),
        ("Austin, Texas Metropolitan Area", "confirmed"),
        ("Austin, TX, USA", "confirmed"),
        ("Austin", "unverified"),
        ("Greater Austin Area", "unverified"),
        ("Austin, MN", "outside"),
        ("Austin, Minnesota", "outside"),
        ("Austin, MN; Dallas, TX", "outside"),
        ("Austin, Canada", "outside"),
        ("Austin, mn", "outside"),
        ("Austin, tx", "confirmed"),
        ("Denver, CO", "none"),
        ("Round Rock, TX", "none"),  # same state, not the target city
        (None, "none"),
    ],
)
def test_place_match_needs_city_and_region(location: str | None, expected: str) -> None:
    match, _ = place_match(fictional(location, WorkArrangement.ONSITE), QUERY.onsite)
    assert match == expected


@pytest.mark.parametrize(
    ("eligibility", "expected"),
    [
        ("United States", "eligible"),
        ("USA", "eligible"),
        ("Remote in United States", "eligible"),
        ("Nationwide", "unknown"),
        ("United States, Canada", "eligible"),
        ("Texas", "ineligible"),  # narrower than the independent US-wide target
        ("TX", "ineligible"),
        ("Canada", "ineligible"),
        ("California", "ineligible"),
        ("United Kingdom", "ineligible"),
        ("Canada-only", "ineligible"),
        ("Canada only (United States employer)", "ineligible"),
        ("United States (Texas only)", "ineligible"),
        ("United States except Texas", "unknown"),
        ("Not United States", "unknown"),
        ("Remote", "unknown"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_remote_eligibility_comes_only_from_the_stated_region(eligibility: str | None,
                                                               expected: str) -> None:
    assert remote_eligibility(fictional("Anywhere", WorkArrangement.REMOTE, eligibility), QUERY) == expected


def test_onsite_role_is_never_remote_eligible() -> None:
    assert remote_eligibility(fictional("Austin, TX", WorkArrangement.ONSITE, "United States"), QUERY) == "ineligible"
    no_remote = JobSearchQuery(remote=None)
    assert remote_eligibility(fictional("Anywhere", WorkArrangement.REMOTE, "United States"), no_remote) == "ineligible"


def test_strong_austin_preference_tiers() -> None:
    tiers = {
        "austin hybrid": location_tier(fictional("Austin, TX", WorkArrangement.HYBRID), QUERY),
        "austin onsite": location_tier(fictional("Austin, TX", WorkArrangement.ONSITE), QUERY),
        "austin unstated": location_tier(fictional("Austin, TX 78701", WorkArrangement.UNKNOWN), QUERY),
        "city only": location_tier(fictional("Austin", WorkArrangement.ONSITE), QUERY),
        "remote us": location_tier(fictional("Anywhere", WorkArrangement.REMOTE, "United States"), QUERY),
        "remote texas": location_tier(fictional("Remote", WorkArrangement.REMOTE, "Texas"), QUERY),
        "remote unknown": location_tier(fictional("Anywhere", WorkArrangement.REMOTE), QUERY),
        "austin mn": location_tier(fictional("Austin, MN", WorkArrangement.ONSITE), QUERY),
        "remote canada": location_tier(fictional("Anywhere", WorkArrangement.REMOTE, "Canada"), QUERY),
        "denver": location_tier(fictional("Denver, CO", WorkArrangement.HYBRID), QUERY),
        "austin remote only": location_tier(fictional("Austin, TX", WorkArrangement.REMOTE, "Canada"), QUERY),
    }
    assert tiers["austin hybrid"] == tiers["austin onsite"] == 0
    assert tiers["austin unstated"] == 1
    assert tiers["city only"] == 2
    assert tiers["remote us"] == 3
    assert tiers["remote texas"] == 5
    assert tiers["remote unknown"] == 4
    assert tiers["austin mn"] == tiers["remote canada"] == tiers["denver"] == 5
    assert tiers["austin remote only"] == 5  # a remote role's eligibility, not its office city


def test_other_priorities_keep_every_class_ranked() -> None:
    austin = fictional("Austin, TX", WorkArrangement.HYBRID)
    remote = fictional("Anywhere", WorkArrangement.REMOTE, "United States")
    canada = fictional("Anywhere", WorkArrangement.REMOTE, "Canada")
    balanced = JobSearchQuery(location_priority=LocationPriority.BALANCED)
    assert location_tier(austin, balanced) == location_tier(remote, balanced) == 0
    assert location_tier(canada, balanced) == 3
    prefer_remote = JobSearchQuery(location_priority=LocationPriority.PREFER_REMOTE)
    assert location_tier(remote, prefer_remote) == 0 and location_tier(austin, prefer_remote) == 1
    assert location_tier(canada, prefer_remote) == 4
