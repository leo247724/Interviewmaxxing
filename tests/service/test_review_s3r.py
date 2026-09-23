"""S3R review corrections: candidate isolation, decision task lifecycle, rank before
limit, linked receipt authority, posting URLs and batched lookups."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import interviewmaxxing_selection as sel_pkg
from interviewmaxxing_core import (
    ApplicationState,
    ApplicationStore,
    EvidenceKind,
    EvidenceRef,
    JobListing,
    ListingSource,
    LocalPaths,
    ReconciliationMethod,
    SelectionPreferences,
    SubmissionObservation,
    SubmissionOutcome,
    SubmissionReconciliation,
    WorkArrangement,
    listing_id_for,
)
from interviewmaxxing_pipeline import PipelineStore, PipelineUpdate
from interviewmaxxing_service import jobs_api
from interviewmaxxing_service.integration import LocalJobsBackend, LocalSelectionBackend
from interviewmaxxing_service.state import ServiceState

from .conftest import FakeCandidates, FictionalSite, Harness, serve
from .test_jobs_routes import FakeDecisions, FakeListings, FakeSearch, listing
from .test_package_integration import NoBrowser, jev_transport

S = ApplicationState
SEARCH_PAGE = "https://jobs.example.test/search?q=paid+media"


def posting(job_id: str, *, location: str | None, arrangement: WorkArrangement,
            observed: datetime, application_url: str | None = None,
            posting_url: str | None = None) -> JobListing:
    """A listing seen on a shared search page (``source_url``)."""
    record = ListingSource(
        source="builtin", source_listing_id=job_id, posting_url=posting_url,
        source_url=SEARCH_PAGE, application_url=application_url, observed_at=observed,
        evidence="fictional search card",
    )
    return JobListing(
        id=listing_id_for("builtin", job_id), source="builtin", source_listing_id=job_id,
        posting_url=posting_url, source_url=SEARCH_PAGE, application_url=application_url,
        title=f"Paid Media Manager {job_id}", company="Fictional Co", location=location,
        work_arrangement=arrangement,
        remote_eligibility="United States" if arrangement is WorkArrangement.REMOTE else None,
        observed_at=observed, evidence="fictional search card", provenance=[record],
    )


# --- 1. candidate isolation (real J2) ---------------------------------------------------


@pytest.fixture
def shared_home(isolated_imx_home: LocalPaths, tmp_path: Path) -> tuple[LocalPaths, Any, Any]:
    repo = LocalJobsBackend(db_path=tmp_path / "jobs.sqlite3", transport=NoBrowser(), adapters={})
    key = sel_pkg.ApiKey("sk-fictional", source="test")
    decisions = LocalSelectionBackend(
        isolated_imx_home,
        client_factory=lambda: sel_pkg.JevClient(key, transport=jev_transport()),
    )
    return isolated_imx_home, repo, decisions


def test_candidates_never_see_or_link_each_others_decisions(
    shared_home: Any, fictional_site: FictionalSite, tmp_path: Path
) -> None:
    paths, repo, decisions = shared_home
    x = posting("iso-1", location="Austin, TX", arrangement=WorkArrangement.HYBRID,
                observed=datetime.now(UTC), posting_url="https://jobs.example.test/builtin/iso-1")
    repo.store.upsert(x)
    a_paths = dataclasses.replace(paths, candidate_id="cand-a")
    b_paths = dataclasses.replace(paths, candidate_id="cand-b")

    with serve(a_paths, fictional_site, FakeCandidates(tmp_path / "a"),
               listings=repo, search=repo, decisions=decisions) as a:
        sel_a = a.client.post(f"/selection/jobs/{x.id}", {}).json["selection"]
        assert sel_a is not None

    with serve(b_paths, fictional_site, FakeCandidates(tmp_path / "b"),
               listings=repo, search=repo, decisions=decisions) as b:
        # A's decision is invisible to B, even though listing and inputs are identical.
        assert b.client.get(f"/jobs/{x.id}").json["selection"] is None
        # B's own decision is recomputed, not A's cached result.
        view = b.client.post(f"/selection/jobs/{x.id}", {}).json
        assert view["decisionTask"]["state"] == "DONE"
        sel_b = view["selection"]
        assert sel_b["id"] != sel_a["id"]
        assert view["decisionTask"]["resultId"] == sel_b["id"]
        # A card of B's that names A's selection id shows no selection.
        tracked = b.client.post(f"/jobs/{x.id}/track", {}).json
        with PipelineStore.from_paths(b_paths) as store:
            item = store.get_item("cand-b", tracked["pipelineEntryId"])
            store.update_item("cand-b", item.id, PipelineUpdate(selection_id=sel_a["id"]),
                              expected_revision=item.revision)
        entry = next(e for e in b.client.get("/pipeline").json["entries"]
                     if e["id"] == tracked["pipelineEntryId"])
        assert entry["selection"] is None
    assert decisions.get_many([sel_a["id"]], candidate_id="cand-b") == {}
    assert set(decisions.get_many([sel_a["id"]], candidate_id="cand-a")) == {sel_a["id"]}
    latest_b = decisions.latest_for([x.id], candidate_id="cand-b")[x.id].selection
    assert latest_b.id == sel_b["id"] and latest_b.candidate_id == "cand-b"


def test_missing_profile_currentness_keeps_the_explicit_candidate(shared_home: Any) -> None:
    _paths, _repo, decisions = shared_home
    x = posting("no-profile", location="Austin, TX", arrangement=WorkArrangement.HYBRID,
                observed=datetime.now(UTC))
    prefs = SelectionPreferences()
    record = decisions.decide(x, prefs, None, candidate_id="cand-without-profile",
                              application_lookup=lambda _: None)
    assert record.selection.candidate_id == "cand-without-profile"
    assert decisions.is_current(record.selection, x, prefs, None)
    changed = prefs.model_copy(update={"role_focus": "A different responsibility focus"})
    assert not decisions.is_current(record.selection, x, changed, None)


def test_real_selection_batch_avoids_per_listing_history(
    shared_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _paths, _repo, decisions = shared_home
    x = posting("batched-real", location="Austin, TX", arrangement=WorkArrangement.HYBRID,
                observed=datetime.now(UTC))
    record = decisions.decide(x, SelectionPreferences(), None, candidate_id="batch-candidate",
                              application_lookup=lambda _: None)
    calls: list[tuple[list[str], str]] = []
    real_batch = sel_pkg.SelectionStore.latest_many

    def batch(store: Any, ids: Any, *, candidate_id: str) -> Any:
        values = list(ids)
        calls.append((values, candidate_id))
        return real_batch(store, values, candidate_id=candidate_id)

    def no_single_read(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("per-listing selection read")

    monkeypatch.setattr(sel_pkg.SelectionStore, "latest_many", batch)
    monkeypatch.setattr(sel_pkg.SelectionStore, "latest", no_single_read)
    monkeypatch.setattr(sel_pkg.SelectionStore, "history", no_single_read)
    found = decisions.latest_for([x.id, x.id, "missing"], candidate_id="batch-candidate")
    assert {key: value.selection.id for key, value in found.items()} == {x.id: record.selection.id}
    assert calls == [([x.id, "missing"], "batch-candidate")]
    assert decisions.latest_for([x.id], candidate_id="other-candidate") == {}


# --- 2. decision task lifecycle -----------------------------------------------------------


class FlakyDecisions(FakeDecisions):
    """Fails on demand; answers repeats from a cache (same result id), like J2."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = False

    def decide(self, listing: JobListing, preferences: Any, profile: Any, *,
               candidate_id: str, application_lookup: Any) -> Any:
        if self.fail:
            self.calls += 1
            raise RuntimeError("provider exploded")
        cached = self.latest_for([listing.id], candidate_id=candidate_id).get(listing.id)
        if cached is not None and cached.selection.preferences_fingerprint == preferences.fingerprint:
            self.calls += 1
            return cached
        return super().decide(listing, preferences, profile, candidate_id=candidate_id,
                              application_lookup=application_lookup)


@pytest.fixture
def flaky(isolated_imx_home: LocalPaths, fictional_site: FictionalSite, tmp_path: Path,
          monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Harness, FakeListings, FlakyDecisions]]:
    monkeypatch.setattr(jobs_api, "DECISION_WAIT_S", 0.3)
    repo = FakeListings()
    decisions = FlakyDecisions()
    with serve(isolated_imx_home, fictional_site, FakeCandidates(tmp_path / "p"),
               listings=repo, search=FakeSearch(repo), decisions=decisions) as h:
        yield h, repo, decisions


def _one(repo: FakeListings) -> JobListing:
    x = listing("t-1", "Paid Media Manager", location="Austin, TX",
                arrangement=WorkArrangement.ONSITE)
    repo.items[x.id] = x
    return x


def test_decision_task_is_pollable_while_running_and_coalesced(flaky: Any) -> None:
    h, repo, decisions = flaky
    x = _one(repo)
    assert h.client.get(f"/jobs/{x.id}").json["decisionTask"] is None
    decisions.gate.clear()
    first = h.client.post(f"/selection/jobs/{x.id}", {})
    assert first.status == 202
    task = first.json["decisionTask"]
    assert task["state"] in ("QUEUED", "RUNNING") and task["resultId"] is None
    again = h.client.post(f"/selection/jobs/{x.id}", {})
    assert again.json["decisionTask"]["id"] == task["id"]  # joined, not repeated
    polled = h.client.get(f"/jobs/{x.id}").json["decisionTask"]
    assert polled["id"] == task["id"] and polled["state"] == "RUNNING"
    decisions.gate.set()
    h.app.jobs._decision_pool.submit(lambda: None).result(10)  # let the worker finish
    done = h.client.get(f"/jobs/{x.id}").json
    assert done["decisionTask"]["state"] == "DONE"
    assert done["decisionTask"]["resultId"] == done["selection"]["id"]
    assert done["decisionTask"]["error"] is None
    assert decisions.calls == 1


def test_failed_decision_is_visible_and_cache_hits_complete(flaky: Any) -> None:
    h, repo, decisions = flaky
    x = _one(repo)
    decisions.fail = True
    failed = h.client.post(f"/selection/jobs/{x.id}", {})
    assert failed.status == 200
    task = failed.json["decisionTask"]
    assert task["state"] == "FAILED" and "RuntimeError" in task["error"]
    assert failed.json["selection"] is None
    assert h.client.get(f"/jobs/{x.id}").json["decisionTask"]["state"] == "FAILED"

    decisions.fail = False
    first = h.client.post(f"/selection/jobs/{x.id}", {}).json
    result = first["decisionTask"]["resultId"]
    assert first["decisionTask"]["state"] == "DONE" and result == first["selection"]["id"]
    # Asked again: the (cached) answer has the same id, and the task still completes.
    second = h.client.post(f"/selection/jobs/{x.id}", {}).json
    assert second["decisionTask"]["id"] != first["decisionTask"]["id"]
    assert second["decisionTask"]["state"] == "DONE"
    assert second["decisionTask"]["resultId"] == result
    listed = next(v for v in h.client.get("/jobs").json["listings"] if v["id"] == x.id)
    assert listed["decisionTask"]["state"] == "DONE"


def test_restart_marks_unfinished_decisions_interrupted(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, tmp_path: Path
) -> None:
    repo = FakeListings()
    x = _one(repo)
    isolated_imx_home.ensure()
    state = ServiceState(isolated_imx_home.state_db.parent / "service.sqlite3")
    task, _ = state.create_or_join(candidate_id="default", kind="decision", dedupe_key="k",
                                   subject=x.id, request={}, prefix="dec")
    state.update(task.id, state="RUNNING")
    state.close()
    with serve(isolated_imx_home, fictional_site, FakeCandidates(tmp_path / "p"),
               listings=repo, search=FakeSearch(repo), decisions=FakeDecisions()) as h:
        view = h.client.get(f"/jobs/{x.id}").json["decisionTask"]
        assert view["id"] == task.id and view["state"] == "INTERRUPTED"
        assert view["error"] == "The service stopped before this decision finished. Ask again."
        assert view["resultId"] is None


# --- 3. rank before the listing limit (real J1) --------------------------------------------


def test_austin_is_ranked_before_the_listing_limit(
    shared_home: Any, fictional_site: FictionalSite, tmp_path: Path
) -> None:
    paths, repo, decisions = shared_home
    old = datetime.now(UTC) - timedelta(days=3)
    austin = posting("aus-old", location="Austin, TX", arrangement=WorkArrangement.ONSITE,
                     observed=old)
    repo.store.upsert(austin)  # stored first, so it is the oldest
    for i in range(4):
        repo.store.upsert(posting(f"rem-{i}", location=None, arrangement=WorkArrangement.REMOTE,
                                  observed=datetime.now(UTC)))
    with serve(paths, fictional_site, FakeCandidates(tmp_path / "p"),
               listings=repo, search=repo, decisions=decisions) as h:
        h.app.jobs.listing_limit = 2
        views = h.client.get("/jobs").json["listings"]
        assert len(views) == 2
        assert views[0]["id"] == austin.id and views[0]["locationTier"] == "ONSITE_HYBRID_TARGET"
        assert views[1]["locationTier"] == "REMOTE_ELIGIBLE"  # remote kept, ranked second


# --- 4. linked application receipt authority --------------------------------------------------


def _uncertain(store: ApplicationStore, url: str) -> str:
    app = store.record_request("default", url).application
    claim = store.claim(app.id, "test")
    for state in (S.INSPECTING, S.PACKET_READY, S.FILLING):
        store.transition(claim, state)
    attempt = store.begin_submission(claim)
    store.record_submission_outcome(claim, attempt.id, SubmissionObservation(
        outcome=SubmissionOutcome.UNKNOWN, detail="no confirmation shown",
        evidence=[EvidenceRef(kind=EvidenceKind.PAGE_TEXT, description="site page text")],
    ))
    store.release(claim)
    return app.id


def _card(h: Harness, app_id: str, company: str) -> dict[str, Any]:
    r = h.client.post("/pipeline/entries", {"lane": "applied", "fields": {"company": company},
                                            "applicationUrl": None})
    with PipelineStore.from_paths(h.paths) as store:
        store.update_item("default", r.json["id"], PipelineUpdate(application_id=app_id),
                          expected_revision=1)
    entry = next(e for e in h.client.get("/pipeline").json["entries"] if e["id"] == r.json["id"])
    return dict(entry["application"])


def test_linked_application_carries_exact_confirmation_authority(harness: Harness) -> None:
    with harness.store() as store:
        user_app = _uncertain(store, "http://127.0.0.1:9/fictional/user")
        site_app = _uncertain(store, "http://127.0.0.1:9/fictional/site")
        open_app = _uncertain(store, "http://127.0.0.1:9/fictional/open")
        for app_id, method in ((user_app, ReconciliationMethod.USER_CONFIRMED),
                               (site_app, ReconciliationMethod.SITE_CONFIRMATION)):
            claim = store.claim(app_id, "test")
            store.reconcile_submission(claim, SubmissionReconciliation(
                outcome=SubmissionOutcome.ACCEPTED, method=method, detail="fictional",
                confirmation_reference="REF-1"))
            store.release(claim)
    user = _card(harness, user_app, "User Co")
    assert (user["confirmationMethod"], user["confirmationAuthority"]) == ("USER_CONFIRMED", "user")
    site = _card(harness, site_app, "Site Co")
    assert (site["confirmationMethod"], site["confirmationAuthority"]) == ("SITE_CONFIRMATION", "site")
    # A user's report that nothing arrived does not unlock an uncertain application.
    r = harness.client.post(f"/applications/{open_app}/reconcile",
                            {"kind": "user_confirmed_not_received"})
    assert r.json["state"] == "SUBMISSION_UNKNOWN"
    still = _card(harness, open_app, "Open Co")
    assert still["state"] == "SUBMISSION_UNKNOWN"
    assert still["confirmationMethod"] is None and still["confirmationAuthority"] is None


# --- 5. posting URLs ------------------------------------------------------------------------------


def test_posting_url_is_exposed_and_the_search_page_is_never_a_link(jobs: Any) -> None:
    h, repo, _search, _dec = jobs
    now = datetime.now(UTC)
    with_posting = posting("p-1", location="Austin, TX", arrangement=WorkArrangement.ONSITE,
                           observed=now, posting_url="https://jobs.example.test/builtin/p-1")
    bare = posting("p-2", location="Austin, TX", arrangement=WorkArrangement.ONSITE, observed=now)
    repo.items.update({with_posting.id: with_posting, bare.id: bare})
    view = h.client.get(f"/jobs/{with_posting.id}").json
    assert view["postingUrl"] == "https://jobs.example.test/builtin/p-1"
    [prov] = view["provenance"]
    assert prov["postingUrl"] == "https://jobs.example.test/builtin/p-1"
    assert prov["sourceUrl"] == SEARCH_PAGE and prov["applicationUrl"] is None
    # Tracking falls back to the posting page, never the shared search page.
    h.client.post(f"/jobs/{with_posting.id}/track", {})
    h.client.post(f"/jobs/{bare.id}/track", {})
    urls = {e["listingId"]: e["applicationUrl"] for e in h.client.get("/pipeline").json["entries"]}
    assert urls == {with_posting.id: "https://jobs.example.test/builtin/p-1", bare.id: None}


# --- 6. batched lookups ------------------------------------------------------------------------------


def test_listing_page_loads_pipeline_and_decisions_once(jobs: Any) -> None:
    h, repo, _search, decisions = jobs
    for i in range(5):
        x = listing(f"b-{i}", "Paid Media Manager", location="Austin, TX",
                    arrangement=WorkArrangement.ONSITE)
        repo.items[x.id] = x
    pipeline = h.app.jobs.pipeline
    calls = {"items": 0, "latest_for": 0}
    real_items, real_latest = pipeline.items_by_listing, decisions.latest_for

    def items_once() -> Any:
        calls["items"] += 1
        return real_items()

    def latest_once(ids: Any, *, candidate_id: str) -> Any:
        calls["latest_for"] += 1
        return real_latest(ids, candidate_id=candidate_id)

    def no_scan(listing_id: str) -> Any:
        raise AssertionError("per-listing pipeline scan")

    pipeline.items_by_listing = items_once
    pipeline.item_for_listing = no_scan
    decisions.latest_for = latest_once
    try:
        assert len(h.client.get("/jobs").json["listings"]) == 5
    finally:
        del pipeline.items_by_listing, pipeline.item_for_listing
        del decisions.latest_for
    assert calls == {"items": 1, "latest_for": 1}
    assert repo.rank_for is not None  # ranked by the saved preferences before the limit


@pytest.fixture
def jobs(isolated_imx_home: LocalPaths, fictional_site: FictionalSite,
         tmp_path: Path) -> Iterator[tuple[Harness, FakeListings, FakeSearch, FakeDecisions]]:
    repo = FakeListings()
    search = FakeSearch(repo)
    decisions = FakeDecisions()
    with serve(isolated_imx_home, fictional_site, FakeCandidates(tmp_path / "p"),
               listings=repo, search=search, decisions=decisions) as h:
        yield h, repo, search, decisions


