"""Explicit application handoffs validate, link before execution, and recover safely."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_core import (
    ApplicationState,
    IdentityEvidenceKind,
    JobIdentityObservation,
    LocalPaths,
    SubmissionObservation,
    SubmissionOutcome,
    WorkArrangement,
)
from interviewmaxxing_pipeline import NewPipelineItem, PipelineStore, PipelineUpdate, TrackingFields

from .conftest import SITE_URL, FakeCandidates, FictionalSite, Harness, serve
from .test_jobs_routes import FakeListings, listing


@pytest.fixture
def links(isolated_imx_home: LocalPaths, fictional_site: FictionalSite,
          tmp_path: Path) -> Iterator[tuple[Harness, FakeListings, str]]:
    repo = FakeListings()
    for i in range(2):
        item = listing(f"link-{i}", "Fictional Analyst", location="Austin, TX",
                       arrangement=WorkArrangement.HYBRID).model_copy(
                           update={"application_url": SITE_URL if i == 0 else SITE_URL + "-other"})
        repo.items[item.id] = item
    with serve(isolated_imx_home, fictional_site, FakeCandidates(tmp_path / "profile"),
               listings=repo) as h:
        yield h, repo, h.setup_candidate()


def body(h: Harness, resume_id: str, **links: Any) -> dict[str, Any]:
    return {"applicationUrl": SITE_URL, "profile": h.profile(), "resumeId": resume_id, **links}


def card(h: Harness, **extra: Any) -> str:
    result = h.client.post("/pipeline/entries", {
        "lane": "saved", "fields": {"company": "Fictional Co", "role": "Fictional Analyst"},
        "applicationUrl": SITE_URL, **extra,
    })
    assert result.status == 201, result.json
    return str(result.json["id"])


def test_listing_only_handoff_tracks_once_and_links_before_dispatch(links: Any) -> None:
    h, repo, resume_id = links
    listing_id = next(iter(repo.items))
    observed: list[str] = []
    submit = h.service.dispatcher.submit

    def assert_linked(application_id: str, *args: Any, **kwargs: Any) -> Any:
        with h.app.pipeline.store() as store:
            [item] = store.list_items("default")
            assert item.application_id == application_id and item.listing_id == listing_id
            observed.append(item.id)
        return submit(application_id, *args, **kwargs)

    h.service.dispatcher.submit = assert_linked
    first = h.client.post("/applications", body(h, resume_id, listingId=listing_id))
    assert first.status == 201, first.json
    assert len(observed) == 1
    again = h.client.post("/applications", body(h, resume_id, listingId=listing_id))
    assert again.status == 200 and again.json["id"] == first.json["id"]
    [entry] = h.client.get("/pipeline").json["entries"]
    assert entry["id"] == observed[0] and entry["application"]["applicationId"] == first.json["id"]


@pytest.mark.parametrize("wrong", ["missing", "foreign", "listing", "url", "different", "empty"])
def test_bad_handoff_is_rejected_before_recording_or_execution(links: Any, wrong: str) -> None:
    h, repo, resume_id = links
    first, second = repo.items
    entry_id = card(h, listingId=first)
    data = body(h, resume_id, pipelineEntryId=entry_id, listingId=first)
    field = "pipelineEntryId"
    if wrong == "missing":
        data["pipelineEntryId"] = "entry_missing"
    elif wrong == "empty":
        data["pipelineEntryId"] = ""
    elif wrong == "foreign":
        with h.app.pipeline.store() as store:
            data["pipelineEntryId"] = store.create_item("other-candidate", NewPipelineItem(
                tracking=TrackingFields(company="Other Co"), application_url=SITE_URL,
            )).id
    elif wrong == "listing":
        data["listingId"] = "listing_missing"
        field = "listingId"
    elif wrong == "different":
        data["listingId"] = second
        field = "listingId"
    else:
        data["applicationUrl"] = SITE_URL + "-wrong"
        field = "applicationUrl"
    result = h.client.post("/applications", data)
    assert result.status == 422, result.json
    assert field in result.json["error"]["fieldErrors"]
    assert h.runs == []
    with h.store() as store:
        assert store.find_application("default", data["applicationUrl"]) is None


def test_link_write_failure_never_dispatches_and_same_request_recovers(
    links: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, _repo, resume_id = links
    entry_id = card(h)
    data = body(h, resume_id, pipelineEntryId=entry_id)
    real_update = PipelineStore.update_item

    def fail_link(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("fictional link write failure")

    monkeypatch.setattr(PipelineStore, "update_item", fail_link)
    failed = h.client.post("/applications", data)
    assert failed.status == 500
    assert h.runs == []
    with h.store() as store:
        recorded = store.find_application("default", SITE_URL)
        assert recorded is not None and recorded.state is ApplicationState.REQUESTED
        assert store.pinned_resume(recorded.id) is not None
    monkeypatch.setattr(PipelineStore, "update_item", real_update)
    recovered = h.client.post("/applications", data)
    assert recovered.status == 200 and recovered.json["id"] == recorded.id
    [entry] = h.client.get("/pipeline").json["entries"]
    assert entry["application"]["applicationId"] == recorded.id


def test_existing_other_application_link_is_never_overwritten(links: Any) -> None:
    h, _repo, resume_id = links
    entry_id = card(h)
    with h.store() as store:
        other_id = store.record_request("default", SITE_URL + "-other").application.id
    with h.app.pipeline.store() as store:
        item = store.get_item("default", entry_id)
        store.update_item("default", entry_id, PipelineUpdate(application_id=other_id),
                          expected_revision=item.revision)
    result = h.client.post("/applications", body(h, resume_id, pipelineEntryId=entry_id))
    assert result.status == 409 and "pipelineEntryId" in result.json["error"]["fieldErrors"]
    assert h.runs == []
    [entry] = h.client.get("/pipeline").json["entries"]
    assert entry["application"]["applicationId"] == other_id


@pytest.mark.parametrize("via_card", [False, True])
def test_shared_apply_url_cannot_link_another_known_jobs_receipt(links: Any, via_card: bool) -> None:
    h, repo, resume_id = links
    first = next(iter(repo.items.values()))
    first = first.model_copy(update={"provenance": [
        source.model_copy(update={"employer_job_key": "ats:mock:fictional:job-b"})
        for source in first.provenance
    ]})
    repo.items[first.id] = first
    with h.store() as store:
        app = store.record_request("default", SITE_URL).application
        claim = store.claim(app.id, "fictional-test")
        store.transition(claim, ApplicationState.INSPECTING)
        store.bind_job_identity(claim, JobIdentityObservation(
            ats_type="mock", ats_tenant="fictional", external_job_id="job-a",
            title="Different observed job", evidence_kind=IdentityEvidenceKind.ATS_JOB_ID_ON_PAGE,
            evidence="Job A ID on fictional form",
        ))
        store.transition(claim, ApplicationState.PACKET_READY)
        store.transition(claim, ApplicationState.FILLING)
        attempt = store.begin_submission(claim)
        store.record_submission_outcome(claim, attempt.id, SubmissionObservation(
            outcome=SubmissionOutcome.ACCEPTED, signals=["Job A received"],
            confirmation_reference="A-RECEIPT",
        ))
        store.release(claim)
    handoff = {"pipelineEntryId": card(h, listingId=first.id)} if via_card else {"listingId": first.id}
    rejected = h.client.post("/applications", body(h, resume_id, **handoff))
    assert rejected.status == 409, rejected.json
    assert next(iter(handoff)) in rejected.json["error"]["fieldErrors"]
    assert all(entry["application"] is None for entry in h.client.get("/pipeline").json["entries"])
    assert h.runs == []
    with h.store() as store:
        assert store.get_application(app.id).state is ApplicationState.SUBMITTED
        assert store.get_receipt(app.id).confirmation_reference == "A-RECEIPT"
    # Matching explicit identity can reuse the canonical submitted application.
    repo.items[first.id] = first.model_copy(update={"provenance": [
        source.model_copy(update={"employer_job_key": "ats:mock:fictional:job-a"})
        for source in first.provenance
    ]})
    accepted = h.client.post("/applications", body(h, resume_id, **handoff))
    assert accepted.status == 200 and accepted.json["id"] == app.id
    [entry] = h.client.get("/pipeline").json["entries"]
    assert entry["application"]["confirmationReference"] == "A-RECEIPT"
    assert h.runs == []
