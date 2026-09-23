from __future__ import annotations

import stat
from datetime import timedelta
from pathlib import Path

import pytest

from interviewmaxxing_core import (
    Compensation,
    CompensationPeriod,
    DescriptionCompleteness,
    JobListing,
    JobSearchQuery,
    ListingStatus,
    LocationPriority,
    WorkArrangement,
)
from interviewmaxxing_jobs.ranking import location_tier, rank_listings
from interviewmaxxing_jobs.sources.base import make_listing
from interviewmaxxing_jobs.store import JobStore, ListingConflict, default_db_path

from .conftest import NOW

KEY = "ats:greenhouse:fictionalwidgets:7001"


def listing(source: str = "linkedin", sid: str = "4000000001", *, title: str = "Marketing Manager",
            company: str | None = "Fictional Widgets Co", location: str | None = "Austin, TX",
            arrangement: WorkArrangement = WorkArrangement.HYBRID, seen_on: str | None = None,
            key: str | None = None, status: ListingStatus = ListingStatus.UNKNOWN,
            description: str | None = None, pay: Compensation | None = None,
            application_url: str | None = None, minutes: int = 0,
            remote_eligibility: str | None = None) -> JobListing:
    posting = f"https://jobs.{source}.test/view/{sid}"
    return make_listing(
        source=source, source_listing_id=sid, posting_url=posting,
        source_url=seen_on or posting, title=title, company=company, location=location,
        work_arrangement=arrangement, status=status, description=description,
        remote_eligibility=remote_eligibility,
        full_description=True, compensation=pay, application_url=application_url,
        employer_job_key=key, employer_key_url=f"https://boards.greenhouse.io/x/jobs/{sid}" if key else None,
        observed_at=NOW + timedelta(minutes=minutes), query_id="qry_test",
        evidence=f"fictional {source} observation",
    )


def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs" / "jobs.sqlite3")


def test_store_file_is_private_and_defaults_under_imx_home(tmp_path: Path) -> None:
    s = store(tmp_path)
    assert stat.S_IMODE(s.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(s.path.parent.stat().st_mode) == 0o700
    assert default_db_path({"IMX_HOME": str(tmp_path)}) == tmp_path / "jobs" / "jobs.sqlite3"
    assert default_db_path({"IMX_JOBS_DB": str(tmp_path / "x.db")}) == tmp_path / "x.db"


def test_same_title_and_company_never_merge(tmp_path: Path) -> None:
    s = store(tmp_path)
    a = s.upsert(listing(sid="1"))
    b = s.upsert(listing(sid="2"))
    c = s.upsert(listing(source="indeed", sid="1"))
    assert len({a.id, b.id, c.id}) == 3
    assert len(s.list_listings()) == 3


def test_shared_search_page_or_application_url_is_not_identity(tmp_path: Path) -> None:
    s = store(tmp_path)
    page = "https://www.linkedin.com/jobs/search/?keywords=marketing+manager"
    generic = "https://careers.fictional.test/apply"
    a = s.upsert(listing(sid="1", seen_on=page, application_url=generic))
    b = s.upsert(listing(sid="2", seen_on=page, application_url=generic))
    assert a.id != b.id
    assert len(s.list_listings()) == 2


def test_reobserving_a_posting_refreshes_it_and_keeps_enriched_identity(tmp_path: Path) -> None:
    s = store(tmp_path)
    pay = Compensation(raw_text="$110K/yr - $130K/yr", minimum=110_000, maximum=130_000,
                       currency="USD", period=CompensationPeriod.YEAR)
    detail = s.upsert(listing(key=KEY, status=ListingStatus.OPEN, description="Full text.",
                              pay=pay, application_url="https://boards.greenhouse.io/x/jobs/7001"))
    card = s.upsert(listing(seen_on=None, pay=Compensation(raw_text="110K-130K"), minutes=5))
    assert card.id == detail.id
    stored = s.get_listing(detail.id)
    assert stored is not None
    assert stored.status is ListingStatus.OPEN  # card said UNKNOWN; detail said OPEN
    assert stored.description == "Full text."
    assert stored.description_completeness is DescriptionCompleteness.FULL
    assert stored.compensation is not None and stored.compensation.minimum == 110_000
    assert f"job:{KEY}" in stored.identity_keys
    assert len(s.observations(detail.id)) == 2


def test_closed_observation_stays_closed(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.upsert(listing(status=ListingStatus.CLOSED))
    again = s.upsert(listing(status=ListingStatus.OPEN, minutes=1))
    assert again.status is ListingStatus.CLOSED
    assert s.list_listings(status=ListingStatus.CLOSED)[0].id == again.id


def test_cross_source_merge_needs_a_proven_employer_job_key(tmp_path: Path) -> None:
    s = store(tmp_path)
    google = s.upsert(listing(source="google", sid="doc1", key=KEY))
    unproven = s.upsert(listing(source="indeed", sid="a1b2c3d4e5f60718"))
    assert unproven.id != google.id
    direct = s.upsert(listing(source="linkedin", sid="4000000001", key=KEY,
                              description="From LinkedIn."))
    # The direct source stays primary; the Google id resolves to it.
    assert direct.source == "linkedin"
    assert {p.source for p in direct.provenance} == {"linkedin", "google"}
    assert s.get_listing(google.id) == s.get_listing(direct.id)
    assert {x.id for x in s.list_listings()} == {direct.id, unproven.id}
    assert [x.id for x in s.list_listings(source="google")] == [direct.id]
    assert {o["source"] for o in s.observations(google.id)} == {"google", "linkedin"}


def test_contradicting_employer_keys_are_kept_apart(tmp_path: Path) -> None:
    s = store(tmp_path)
    a = s.upsert(listing(source="linkedin", sid="1", key=KEY))
    b = s.upsert(listing(source="google", sid="doc9", key="ats:greenhouse:fictionalwidgets:9999"))
    assert a.id != b.id
    assert len(s.list_listings()) == 2


OTHER_KEY = "ats:greenhouse:fictionalwidgets:9999"


def test_same_posting_id_with_another_employer_key_is_rejected_unchanged(tmp_path: Path) -> None:
    s = store(tmp_path)
    closed = s.upsert(listing(key=KEY, status=ListingStatus.CLOSED))
    with pytest.raises(ListingConflict) as err:
        s.upsert(listing(key=OTHER_KEY, status=ListingStatus.OPEN, minutes=1), raw={"kind": "test"})
    assert err.value.listing_id == closed.id
    stored = s.get_listing(closed.id)
    assert stored is not None
    assert stored.status is ListingStatus.CLOSED
    assert {p.employer_job_key for p in stored.provenance} == {KEY}
    assert f"job:{OTHER_KEY}" not in stored.identity_keys
    assert len(s.list_listings()) == 1
    kept = s.conflicts(closed.id)
    assert len(kept) == 1 and kept[0]["raw"] == {"kind": "test"}
    assert "contradicts" in kept[0]["reason"]
    # The rejected key was never indexed: a posting proven under it stays separate.
    other = s.upsert(listing(source="google", sid="doc9", key=OTHER_KEY))
    assert other.id != closed.id and len(s.list_listings()) == 2


def test_contradiction_through_an_alias_is_rejected_and_the_alias_still_resolves(tmp_path: Path) -> None:
    s = store(tmp_path)
    google = s.upsert(listing(source="google", sid="doc1", key=KEY))
    direct = s.upsert(listing(source="linkedin", sid="4000000001", key=KEY))
    assert s.get_listing(google.id) == s.get_listing(direct.id)  # G is now an alias of L
    with pytest.raises(ListingConflict) as err:
        s.upsert(listing(source="google", sid="doc1", key=OTHER_KEY, minutes=2))
    assert err.value.listing_id == direct.id
    assert s.get_listing(google.id) == s.get_listing(direct.id)
    assert [x.id for x in s.list_listings()] == [direct.id]
    assert {p.employer_job_key for p in s.list_listings()[0].provenance} == {KEY}
    assert len(s.conflicts(google.id)) == 1
    # A consistent re-observation through the alias still merges normally.
    again = s.upsert(listing(source="google", sid="doc1", key=KEY, status=ListingStatus.CLOSED, minutes=3))
    assert again.id == direct.id and again.status is ListingStatus.CLOSED


def test_fresh_posting_contradicting_a_shared_key_stands_alone(tmp_path: Path) -> None:
    s = store(tmp_path)
    first = s.upsert(listing(source="linkedin", sid="1", key=KEY))
    second = s.upsert(listing(source="linkedin", sid="2", key=KEY))  # same key, other LinkedIn id
    assert second.id != first.id and len(s.list_listings()) == 2
    assert s.conflicts(first.id) == [] and s.conflicts(second.id) == []
    # The shared key stays with the record that held it first.
    proven = s.upsert(listing(source="google", sid="docK", key=KEY))
    assert proven.id == first.id
    assert s.get_listing(second.id) is not None and s.get_listing(second.id).source_listing_id == "2"


def test_runs_roundtrip(tmp_path: Path) -> None:
    from interviewmaxxing_core import JobSearchRun
    s = store(tmp_path)
    query = JobSearchQuery(location_priority=LocationPriority.STRONGLY_PREFER_ONSITE_HYBRID)
    run = JobSearchRun(query=query, started_at=NOW)
    s.save_run(run)
    loaded = s.get_run(run.id)
    assert loaded == run
    assert loaded is not None
    assert loaded.query.location_priority is LocationPriority.STRONGLY_PREFER_ONSITE_HYBRID
    assert s.latest_run() == run


def test_austin_onsite_hybrid_ranks_well_above_eligible_remote(tmp_path: Path) -> None:
    s = store(tmp_path)
    remote = s.upsert(listing(sid="r", location="United States", arrangement=WorkArrangement.REMOTE,
                              remote_eligibility="United States", minutes=3))
    elsewhere = s.upsert(listing(sid="e", location="Denver, CO", arrangement=WorkArrangement.ONSITE,
                                 minutes=2))
    unknown = s.upsert(listing(sid="u", location="Austin, TX 78701",
                               arrangement=WorkArrangement.UNKNOWN, minutes=1))
    austin = s.upsert(listing(sid="a", location="Austin, TX", arrangement=WorkArrangement.HYBRID))
    closed = s.upsert(listing(sid="c", location="Austin, TX", status=ListingStatus.CLOSED, minutes=4))
    query = JobSearchQuery()
    ranked = s.list_listings(rank_for=query)
    assert [x.id for x in ranked] == [austin.id, unknown.id, remote.id, elsewhere.id, closed.id]
    # Bounded results keep Austin even though remote is newer; remote is not excluded.
    assert [x.id for x in s.list_listings(rank_for=query, limit=3)] == [austin.id, unknown.id, remote.id]
    assert location_tier(remote, query) == 3

    remote_first = JobSearchQuery(location_priority=LocationPriority.PREFER_REMOTE)
    assert rank_listings([austin, remote], remote_first) == [remote, austin]
    no_remote = JobSearchQuery(remote=None)
    assert location_tier(remote, no_remote) == 5


def test_conflicting_identity_cannot_be_redirected_to_another_matching_record(tmp_path: Path) -> None:
    s = store(tmp_path)
    original = s.upsert(listing(source="google", sid="original", key=KEY))
    # A direct record holds the *other* key. The source identity of the incoming
    # observation must win over a compatible merge candidate under that other key.
    other = s.upsert(listing(source="linkedin", sid="9999", key=OTHER_KEY))
    with pytest.raises(ListingConflict):
        s.upsert(listing(source="google", sid="original", key=OTHER_KEY))
    assert s.get_listing(original.id) == original
    assert s.get_listing(other.id) == other
    assert len(s.list_listings()) == 2


def test_conflict_evidence_follows_a_later_canonical_merge(tmp_path: Path) -> None:
    s = store(tmp_path)
    google = s.upsert(listing(source="google", sid="doc1", key=KEY))
    with pytest.raises(ListingConflict):
        s.upsert(listing(source="google", sid="doc1", key=OTHER_KEY), raw={"rejected": True})
    direct = s.upsert(listing(key=KEY))
    assert s.get_listing(google.id) == direct
    assert len(s.conflicts(direct.id)) == 1
    assert s.conflicts(google.id)[0]["raw"] == {"rejected": True}


def test_listing_aliases_returns_only_accepted_historical_ids(tmp_path: Path) -> None:
    s = store(tmp_path)
    google = s.upsert(listing(source="google", sid="doc1", key=KEY))
    canonical = s.upsert(listing(key=KEY))
    indeed_input = listing(source="indeed", sid="aaaaaaaaaaaaaaaa", key=KEY)
    assert s.upsert(indeed_input).id == canonical.id
    # A different LinkedIn posting sharing a disputed employer key is separate.
    unrelated = s.upsert(listing(sid="different", key=KEY))
    with pytest.raises(ListingConflict):
        s.upsert(listing(source="google", sid="doc1", key=OTHER_KEY))
    expected = {canonical.id: [canonical.id, *sorted([google.id, indeed_input.id])]}
    assert s.listing_aliases([canonical.id]) == expected
    assert s.listing_aliases(iter([google.id, indeed_input.id, canonical.id, google.id, "missing"])) == expected
    assert s.listing_aliases([unrelated.id]) == {unrelated.id: [unrelated.id]}
    assert s.listing_aliases([]) == {}
    assert s.listing_aliases(["missing"]) == {}
    assert len(s.list_listings()) == 2


def test_listing_aliases_batches_large_requests(tmp_path: Path) -> None:
    s = store(tmp_path)
    google = s.upsert(listing(source="google", sid="doc1", key=KEY))
    canonical = s.upsert(listing(key=KEY))
    requested = [f"missing-{i}" for i in range(1201)] + [google.id, canonical.id]
    assert s.listing_aliases(requested) == {canonical.id: [canonical.id, google.id]}
