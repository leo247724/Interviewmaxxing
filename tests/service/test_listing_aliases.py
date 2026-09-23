"""J1's accepted aliases preserve service tasks, decisions and pipeline handoffs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_core import JobListing, LocalPaths, WorkArrangement
from interviewmaxxing_jobs.sources.base import make_listing
from interviewmaxxing_service import jobs_api
from interviewmaxxing_service.integration import LocalJobsBackend

from .conftest import SITE_URL, FakeCandidates, FictionalSite, serve
from .test_jobs_routes import FakeDecisions
from .test_package_integration import NoBrowser


def observed(source: str, sid: str, key: str = "ats:mock:fictional:job1") -> JobListing:
    return make_listing(
        source=source, source_listing_id=sid,
        posting_url=f"https://jobs.{source}.example.test/view/{sid}",
        source_url=f"https://jobs.{source}.example.test/search",
        title="Fictional Analyst", company="Fictional Co", location="Austin, TX",
        work_arrangement=WorkArrangement.HYBRID, application_url=SITE_URL,
        employer_job_key=key, employer_key_url="https://fictional.example.test/jobs/job1",
        observed_at=datetime.now(UTC), query_id="query-fictional", evidence="fictional card",
    )


def test_canonical_merge_preserves_card_decision_task_and_application_handoff(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, tmp_path: Path,
) -> None:
    repo = LocalJobsBackend(db_path=tmp_path / "jobs.sqlite3", transport=NoBrowser(), adapters={})
    old = repo.store.upsert(observed("google", "google-job1"))
    with serve(isolated_imx_home, fictional_site, FakeCandidates(tmp_path / "profile"),
               listings=repo, decisions=FakeDecisions()) as h:
        decided = h.client.post(f"/selection/jobs/{old.id}", {}).json
        tracked = h.client.post(f"/jobs/{old.id}/track", {}).json
        canonical = repo.store.upsert(observed("linkedin", "40000001"))
        assert canonical.id != old.id and repo.get_listing(old.id).id == canonical.id
        for listing_id in (old.id, canonical.id):
            view = h.client.get(f"/jobs/{listing_id}").json
            assert view["id"] == canonical.id
            assert view["pipelineEntryId"] == tracked["pipelineEntryId"]
            assert view["decisionTask"]["id"] == decided["decisionTask"]["id"]
            assert view["selection"]["id"] == decided["selection"]["id"]
        again = h.client.post(f"/jobs/{canonical.id}/track", {})
        assert again.status == 200 and again.json["pipelineEntryId"] == tracked["pipelineEntryId"]
        [view] = h.client.get("/jobs").json["listings"]
        assert view["decisionTask"]["id"] == decided["decisionTask"]["id"]
        assert view["pipelineEntryId"] == tracked["pipelineEntryId"]
        started = h.client.post("/applications", {
            "applicationUrl": SITE_URL, "profile": h.profile(), "resumeId": h.setup_candidate(),
            "pipelineEntryId": tracked["pipelineEntryId"], "listingId": canonical.id,
        })
        assert started.status == 201, started.json
        [entry] = h.client.get("/pipeline").json["entries"]
        assert entry["id"] == tracked["pipelineEntryId"]
        assert entry["listingId"] == old.id  # candidate-owned provenance is not rewritten
        assert entry["application"]["applicationId"] == started.json["id"]


def test_queued_alias_decision_coalesces_and_remains_pollable_after_merge(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jobs_api, "DECISION_WAIT_S", 0.03)
    repo = LocalJobsBackend(db_path=tmp_path / "jobs.sqlite3", transport=NoBrowser(), adapters={})
    blocker = repo.store.upsert(observed("linkedin", "40000002", "ats:mock:fictional:blocker"))
    old = repo.store.upsert(observed("google", "google-job1"))
    decisions = FakeDecisions()
    decisions.gate.clear()
    with serve(isolated_imx_home, fictional_site, FakeCandidates(tmp_path / "profile"),
               listings=repo, decisions=decisions) as h:
        try:
            h.client.post(f"/selection/jobs/{blocker.id}", {})
            queued = h.client.post(f"/selection/jobs/{old.id}", {}).json["decisionTask"]
            assert queued["state"] == "QUEUED"
            canonical = repo.store.upsert(observed("linkedin", "40000001"))
            joined = h.client.post(f"/selection/jobs/{canonical.id}", {}).json["decisionTask"]
            assert joined["id"] == queued["id"] and joined["state"] == "QUEUED"
        finally:
            decisions.gate.set()
            h.app.jobs._decision_pool.submit(lambda: None).result(10)
        assert decisions.calls == 2
        for listing_id in (old.id, canonical.id):
            done: dict[str, Any] = h.client.get(f"/jobs/{listing_id}").json
            assert done["decisionTask"]["id"] == queued["id"]
            assert done["decisionTask"]["state"] == "DONE"
            assert done["decisionTask"]["resultId"] == done["selection"]["id"]
