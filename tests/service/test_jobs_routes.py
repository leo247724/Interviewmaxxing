"""Jobs, preferences and selection routes over the service seams.

The fakes produce canonical D0 records; real J1/J2 packages are exercised in
``test_package_integration.py``."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_core import (
    Compensation,
    CompensationPeriod,
    JobListing,
    JobSearchQuery,
    JobSelection,
    ListingSource,
    ListingStatus,
    LocalPaths,
    LocationPriority,
    ModelDecision,
    SelectionChoice,
    SelectionPreferences,
    SourceSearchResult,
    SourceSearchState,
    WorkArrangement,
    listing_id_for,
    snapshot_hash,
)
from interviewmaxxing_service.jobs_api import DecisionRecord, Rank

from .conftest import FakeCandidates, FictionalSite, Harness, serve

NOW = datetime(2026, 9, 22, 20, 0, tzinfo=UTC)


def listing(
    job_id: str,
    title: str,
    *,
    location: str | None,
    arrangement: WorkArrangement,
    source: str = "linkedin",
    pay: tuple[float, float] | None = None,
    status: ListingStatus = ListingStatus.OPEN,
    minutes_ago: int = 0,
) -> JobListing:
    observed = NOW - timedelta(minutes=minutes_ago)
    url = f"https://jobs.example.test/{source}/{job_id}"
    record = ListingSource(source=source, source_listing_id=job_id, source_url=url,
                           application_url=f"{url}/apply",
                           observed_at=observed, evidence="fictional fixture")
    return JobListing(
        id=listing_id_for(source, job_id), source=source, source_listing_id=job_id,
        source_url=url, application_url=f"{url}/apply", title=title, company="Fictional Co",
        location=location, work_arrangement=arrangement,
        remote_eligibility="United States" if arrangement is WorkArrangement.REMOTE else None,
        compensation=Compensation(raw_text=f"${pay[0]:,.0f}-${pay[1]:,.0f}", minimum=pay[0],
                                  maximum=pay[1], currency="USD",
                                  period=CompensationPeriod.YEAR) if pay else None,
        status=status, observed_at=observed, evidence="fictional fixture", provenance=[record],
    )


class FakeListings:
    def __init__(self) -> None:
        self.items: dict[str, JobListing] = {}

    def get_listing(self, listing_id: str) -> JobListing | None:
        return self.items.get(listing_id)

    def list_listings(self, *, limit: int | None = None) -> list[JobListing]:
        return list(self.items.values())[:limit]


class FakeSearch:
    """Per source: listings to store, or an access problem."""

    def __init__(self, repo: FakeListings) -> None:
        self.repo = repo
        self.plan: dict[str, list[JobListing] | SourceSearchState] = {}
        self.calls: list[str] = []
        self.gate = threading.Event()
        self.gate.set()

    def search_source(self, query: JobSearchQuery, source: str) -> SourceSearchResult:
        self.calls.append(source)
        self.gate.wait(10)
        started = datetime.now(UTC)
        planned = self.plan.get(source, [])
        if isinstance(planned, SourceSearchState):
            return SourceSearchResult(
                query_id=query.id, source=source, state=planned,
                message="Sign in to continue" if planned is SourceSearchState.NEEDS_USER
                else "Access denied by the source",
                user_action="Sign in to LinkedIn in the imx-jobs-linkedin window"
                if planned is SourceSearchState.NEEDS_USER else None,
                session_name="imx-jobs-linkedin"
                if planned is SourceSearchState.NEEDS_USER else None,
                started_at=started, finished_at=datetime.now(UTC),
            )
        for x in planned:
            self.repo.items[x.id] = x
        return SourceSearchResult(
            query_id=query.id, source=source, state=SourceSearchState.OK,
            listing_ids=[x.id for x in planned], started_at=started,
            finished_at=datetime.now(UTC),
        )


class FakeDecisions:
    """Deterministic stand-in with J2's rank semantics (onsite/hybrid in Austin first
    under the strong preference, eligible remote second, unknown last)."""

    def __init__(self) -> None:
        self.records: dict[str, list[DecisionRecord]] = {}
        self.calls = 0
        self.gate = threading.Event()
        self.gate.set()
        self.seen_priority: list[LocationPriority] = []

    def decide(self, listing: JobListing, preferences: SelectionPreferences,
               profile: Any, *, candidate_id: str, application_lookup: Any) -> DecisionRecord:
        self.calls += 1
        self.seen_priority.append(preferences.location_priority)
        self.gate.wait(10)
        selection = JobSelection(
            listing_id=listing.id, candidate_id=candidate_id, returned_model="typesafe/jev-test",
            rubric_version="test-1", preferences_fingerprint=preferences.fingerprint,
            job_evidence_hash=snapshot_hash({"id": listing.id}),
            candidate_evidence_hash=snapshot_hash({"c": 1}),
            model_decision=ModelDecision(
                choice=SelectionChoice.REVIEW,
                probabilities={SelectionChoice.APPLY: 0.3, SelectionChoice.SKIP: 0.1,
                               SelectionChoice.REVIEW: 0.6},
                confidence=0.6,
            ),
            effective_choice=SelectionChoice.REVIEW, reasons=["fictional reason"],
        )
        record = DecisionRecord(selection=selection, unresolved=["Pay isn't stated."])
        self.records.setdefault(listing.id, []).append(record)
        return record

    def latest(self, listing_id: str) -> DecisionRecord | None:
        found = self.records.get(listing_id)
        return found[-1] if found else None

    def get(self, selection_id: str) -> DecisionRecord | None:
        return next((r for rs in self.records.values() for r in rs
                     if r.selection.id == selection_id), None)

    def is_current(self, selection: JobSelection, listing: JobListing,
                   preferences: SelectionPreferences, profile: Any) -> bool:
        return selection.preferences_fingerprint == preferences.fingerprint

    def rank(self, listing: JobListing, preferences: SelectionPreferences) -> Rank:
        onsite = listing.work_arrangement in (WorkArrangement.ONSITE, WorkArrangement.HYBRID) \
            and "austin" in (listing.location or "").lower()
        remote = listing.work_arrangement is WorkArrangement.REMOTE
        if preferences.location_priority is LocationPriority.BALANCED and (onsite or remote):
            return Rank("EQUAL", "balanced")
        first, second = (remote, onsite) if preferences.location_priority is \
            LocationPriority.PREFER_REMOTE else (onsite, remote)
        if first:
            return Rank("PREFERRED", "preferred")
        if second:
            return Rank("SECONDARY", "secondary, not excluded")
        return Rank("UNRANKED", "unknown")


@pytest.fixture
def jobs(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, tmp_path: Path
) -> Iterator[tuple[Harness, FakeListings, FakeSearch, FakeDecisions]]:
    repo = FakeListings()
    search = FakeSearch(repo)
    decisions = FakeDecisions()
    with serve(isolated_imx_home, fictional_site, FakeCandidates(tmp_path / "p"),
               listings=repo, search=search, decisions=decisions) as h:
        yield h, repo, search, decisions


def _prefs(h: Harness) -> dict[str, Any]:
    body = dict(h.client.get("/selection/preferences").json)
    body.pop("fingerprint")
    return body


def _wait_run(h: Harness, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while True:
        run = h.client.get(f"/jobs/search/{run_id}").json
        if run["finishedAt"]:
            return dict(run)
        assert time.monotonic() < deadline
        time.sleep(0.05)


def test_default_preferences_are_the_users_targets(jobs: Any) -> None:
    h, *_ = jobs
    prefs = h.client.get("/selection/preferences").json
    assert prefs["titlePhrases"] == ["marketing manager", "marketing director"]
    assert prefs["onsite"] == [{"location": "Austin, TX", "arrangements": ["ONSITE", "HYBRID"]}]
    assert prefs["remote"] == {"eligibleRegion": "United States"}
    assert prefs["locationPriority"] == "STRONGLY_PREFER_ONSITE_HYBRID"
    assert prefs["minimumCompensation"] == {"amount": 100000.0, "currency": "USD",
                                            "period": "YEAR"}
    assert prefs["unknownCompensation"] == "KEEP"
    assert prefs["sources"] == ["linkedin", "builtin", "indeed", "google"]
    assert prefs["fingerprint"] == SelectionPreferences().fingerprint


def test_preferences_persist_and_priority_changes_fingerprint(jobs: Any) -> None:
    h, *_ = jobs
    body = _prefs(h)
    before = h.client.get("/selection/preferences").json["fingerprint"]
    body["keywords"] = ["B2B"]
    body["locationPriority"] = "BALANCED"
    saved = h.client.post("/selection/preferences", body)
    assert saved.status == 200
    assert saved.json["locationPriority"] == "BALANCED" and saved.json["keywords"] == ["B2B"]
    assert saved.json["fingerprint"] != before
    # Omitting locationPriority keeps the saved value (additive field).
    body.pop("locationPriority")
    again = h.client.post("/selection/preferences", body).json
    assert again["locationPriority"] == "BALANCED"
    assert h.client.get("/selection/preferences").json == again
    bad = dict(body, titlePhrases=[], onsite=[], remote=None)
    r = h.client.post("/selection/preferences", bad)
    assert r.status == 422 and r.json["error"]["fieldErrors"]
    assert h.client.post("/selection/preferences", dict(body, extra=1)).status == 400


def test_search_is_async_per_source_and_deduplicated(jobs: Any) -> None:
    h, _repo, search, _ = jobs
    search.plan = {
        "linkedin": SourceSearchState.NEEDS_USER,
        "builtin": [listing("b1", "Marketing Manager", location="Austin, TX",
                            arrangement=WorkArrangement.HYBRID)],
        "indeed": SourceSearchState.BLOCKED,
        "google": [],
    }
    search.gate.clear()
    body = _prefs(h)
    first = h.client.post("/jobs/search", body)
    assert first.status == 202
    run_id = first.json["id"]
    assert run_id.startswith("srch_")
    assert {r["state"] for r in first.json["results"]} <= {"QUEUED", "RUNNING"}
    # The same query while running joins the same run; a different one is refused.
    assert h.client.post("/jobs/search", body).json["id"] == run_id
    other = dict(body, keywords=["different"])
    assert h.client.post("/jobs/search", other).status == 409
    search.gate.set()
    run = _wait_run(h, run_id)
    states = {r["source"]: r for r in run["results"]}
    assert states["linkedin"]["state"] == "NEEDS_USER"
    assert states["linkedin"]["userAction"] and states["linkedin"]["sessionName"]
    assert states["indeed"]["state"] == "BLOCKED" and states["indeed"]["message"]
    assert states["builtin"]["state"] == "OK" and states["builtin"]["resultCount"] == 1
    assert states["google"] == {**states["google"], "state": "OK", "resultCount": 0}
    assert search.calls == ["linkedin", "builtin", "indeed", "google"]
    listed = h.client.get("/jobs").json
    assert listed["lastRun"]["id"] == run_id
    assert [x["title"] for x in listed["listings"]] == ["Marketing Manager"]
    assert h.client.get("/jobs/search/srch_missing").status == 404


def test_listings_rank_austin_above_remote_without_excluding_remote(jobs: Any) -> None:
    h, repo, _, _ = jobs
    items = [
        listing("r1", "Remote Director", location=None, arrangement=WorkArrangement.REMOTE,
                pay=(150000, 180000), minutes_ago=0),
        listing("a1", "Austin Manager", location="Austin, TX",
                arrangement=WorkArrangement.HYBRID, minutes_ago=30),
        listing("u1", "Unknown Arrangement", location="Somewhere",
                arrangement=WorkArrangement.UNKNOWN, minutes_ago=1),
        listing("c1", "Closed Austin", location="Austin, TX",
                arrangement=WorkArrangement.ONSITE, status=ListingStatus.CLOSED),
        listing("a2", "Austin Low Pay", location="Austin, TX",
                arrangement=WorkArrangement.ONSITE, pay=(60000, 80000)),
    ]
    for x in items:
        repo.items[x.id] = x
    views = h.client.get("/jobs").json["listings"]
    assert [v["title"] for v in views] == [
        "Austin Manager", "Austin Low Pay", "Remote Director", "Unknown Arrangement",
        "Closed Austin",
    ]
    tiers = {v["title"]: v["locationTier"] for v in views}
    assert tiers["Remote Director"] == "SECONDARY"  # eligible, ranked lower, not excluded
    assert views[0]["provenance"][0]["applicationUrl"].endswith("/apply")
    body = _prefs(h)
    body["locationPriority"] = "PREFER_REMOTE"
    h.client.post("/selection/preferences", body)
    assert h.client.get("/jobs").json["listings"][0]["title"] == "Remote Director"
    one = h.client.get(f"/jobs/{items[1].id}")
    assert one.status == 200 and one.json["title"] == "Austin Manager"
    assert h.client.get("/jobs/lst_missing").status == 404


def test_decision_is_explicit_async_and_deduplicated(jobs: Any) -> None:
    h, repo, _, decisions = jobs
    x = listing("d1", "Marketing Director", location="Austin, TX",
                arrangement=WorkArrangement.ONSITE)
    repo.items[x.id] = x
    assert h.client.get("/jobs").json["listings"][0]["selection"] is None
    assert decisions.calls == 0  # listing views never decide on their own
    decisions.gate.clear()
    results: list[Any] = []
    threads = [threading.Thread(
        target=lambda: results.append(h.client.post(f"/selection/jobs/{x.id}", {})))
        for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    decisions.gate.set()
    for t in threads:
        t.join(30)
    assert decisions.calls == 1
    assert {r.status for r in results} == {200}
    view = results[0].json
    sel = view["selection"]
    assert sel["effectiveChoice"] == "REVIEW" and sel["modelChoice"] == "REVIEW"
    assert sel["unresolved"] == ["Pay isn't stated."] and sel["stale"] is False
    assert decisions.seen_priority == [LocationPriority.STRONGLY_PREFER_ONSITE_HYBRID]
    # A preference change makes the decision stale; it is not silently recomputed.
    body = _prefs(h)
    body["locationPriority"] = "BALANCED"
    h.client.post("/selection/preferences", body)
    assert h.client.get(f"/jobs/{x.id}").json["selection"]["stale"] is True
    assert decisions.calls == 1
    with h.store() as store:
        assert store.list_applications() == []  # deciding never applies


def test_track_links_listing_to_one_pipeline_card(jobs: Any) -> None:
    h, repo, _, _ = jobs
    x = listing("t1", "Marketing Manager", location="Austin, TX",
                arrangement=WorkArrangement.HYBRID, pay=(110000, 130000))
    repo.items[x.id] = x
    first = h.client.post(f"/jobs/{x.id}/track", {})
    assert first.status == 201
    entry_id = first.json["pipelineEntryId"]
    assert entry_id
    again = h.client.post(f"/jobs/{x.id}/track", {})
    assert again.status == 200 and again.json["pipelineEntryId"] == entry_id
    board = h.client.get("/pipeline").json
    (entry,) = board["entries"]
    assert entry["origin"] == "jobs" and entry["listingId"] == x.id
    assert entry["lane"] == "saved"
    assert entry["fields"]["company"] == "Fictional Co"
    assert entry["fields"]["compensationLow"] == 110000
    assert entry["fields"]["workArrangement"] == "Hybrid"
    assert entry["applicationUrl"] == x.application_url
    assert entry["application"] is None
    with h.store() as store:
        assert store.list_applications() == []  # tracking never applies


def test_unavailable_packages_fail_truthfully(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, tmp_path: Path
) -> None:
    with serve(isolated_imx_home, fictional_site, FakeCandidates(tmp_path / "p"),
               runner_problem=lambda: "The application runner isn't installed.") as h:
        assert h.client.get("/jobs").status == 503
        assert h.client.post("/selection/jobs/lst_x", {}).status == 503
        assert h.client.get("/selection/preferences").status == 200  # stored locally
        health = h.client.get("/healthz").json
        assert health["runner"] == "unavailable" and health["jobs"] == "unavailable"
        r = h.start()
        assert r.status == 503 and r.json["error"]["code"] == "unavailable"
        with h.store() as store:
            assert store.list_applications() == []  # nothing recorded
