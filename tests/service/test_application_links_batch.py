"""prepare-batch pipeline bookkeeping seen through the running service.

``sync_pipeline_card`` (``interviewmaxxing_cli.batch``) links a prepared application to
its Saved card and moves the card of a closed job to Closed. These tests check that
what it writes is the service's own link contract: the dashboard's ``GET /pipeline``
shows it, ``POST /applications`` with ``pipelineEntryId`` accepts it without starting
a run, an existing service link is never replaced, and only the lane of a Saved card
changes. Real application and pipeline stores, the scripted runner, the fictional
loopback site; nothing is opened or submitted.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest

from interviewmaxxing_cli.batch import BatchOutcome, LedgerEntry, sync_pipeline_card
from interviewmaxxing_core import (
    ApplicationState,
    ApplicationStore,
    Claim,
    IdentityEvidenceKind,
    JobIdentityObservation,
    normalize_application_url,
)
from interviewmaxxing_pipeline import PipelineUpdate

from .conftest import SITE_URL, Harness

S = ApplicationState
BATCH_ID = "b-service"
OWNER = "fictional-prepare-batch"
PREPARED_MESSAGE = (
    "Prepared to the final review step. Nothing was submitted. "
    "Submission remains disabled when this application is resumed."
)
CLOSED_MESSAGE = "The job is no longer accepting applications."
# 20:00 on Jan 31 at UTC-8 is Feb 1 in UTC: the note must carry the UTC date.
FINISHED_NON_UTC = datetime(2026, 1, 31, 20, 0, tzinfo=timezone(timedelta(hours=-8)))
# A second fictional loopback link to the same fictional job (never opened).
ALIAS_URL = "http://127.0.0.1:9/fictional-co/4012-b/apply"


# --- helpers ----------------------------------------------------------------------------


def _card(h: Harness, url: str = SITE_URL, **fields: Any) -> str:
    """A Saved card created through the dashboard route, as the user would."""
    created = h.client.post("/pipeline/entries", {
        "lane": "saved",
        "fields": {"company": "Fictional Co", "role": "Fictional Analyst", **fields},
        "applicationUrl": url,
    })
    assert created.status == 201, created.json
    return str(created.json["id"])


def _board(h: Harness) -> list[dict[str, Any]]:
    board = h.client.get("/pipeline")
    assert board.status == 200, board.json
    return list(board.json["entries"])


def _moves_to_applied(entries: list[dict[str, Any]]) -> list[str]:
    return [item["summary"] for entry in entries for item in entry["history"]
            if item["toLane"] == "applied"]


def _handoff(h: Harness, resume_id: str, url: str, entry_id: str) -> dict[str, Any]:
    return {"applicationUrl": url, "profile": h.profile(), "resumeId": resume_id,
            "pipelineEntryId": entry_id}


def _inspecting(store: ApplicationStore, candidate_id: str, url: str) -> tuple[str, Claim]:
    app = store.record_request(candidate_id, url).application
    claim = store.claim(app.id, OWNER)
    store.transition(claim, S.INSPECTING)
    return app.id, claim


def _prepared(h: Harness, url: str = SITE_URL,
              identity: JobIdentityObservation | None = None) -> str:
    """What a prepare-only run leaves: NEEDS_INPUT after a ``preparation.ready`` stop."""
    with h.store() as store:
        app_id, claim = _inspecting(store, h.paths.candidate_id, url)
        if identity is not None:
            store.bind_job_identity(claim, identity)
        store.append_event(claim, "preparation.ready", {"submitted": False})
        store.transition(claim, S.NEEDS_INPUT, metadata={
            "missing_inputs": [], "reason": "prepared for final review; submission disabled",
        })
        store.release(claim)
    return app_id


def _closed(h: Harness, url: str = SITE_URL) -> str:
    """What a run leaves for a posting that no longer accepts applications."""
    with h.store() as store:
        app_id, claim = _inspecting(store, h.paths.candidate_id, url)
        store.transition(claim, S.FAILED_PERMANENT, failure_reason=CLOSED_MESSAGE)
        store.release(claim)  # a no-op: the terminal state already released it
    return app_id


def _ledger(url: str, application_id: str, pipeline_id: str, outcome: BatchOutcome,
            state: ApplicationState, message: str, *, finished_at: datetime | None = None,
            listing_id: str | None = None) -> LedgerEntry:
    finished = finished_at or datetime.now(UTC)
    return LedgerEntry(
        batch_id=BATCH_ID, listing_id=listing_id or f"url:{normalize_application_url(url)}",
        pipeline_id=pipeline_id, company="Fictional Co", title="Fictional Analyst",
        application_url=url, backend="fictional", status="resolved", attempt=1,
        worker_slot=None if outcome == "already_recorded" else 0,
        application_id=application_id, state=state, outcome=outcome, message=message,
        started_at=finished - timedelta(seconds=0.5), finished_at=finished, duration_s=0.5,
    )


# --- tests ------------------------------------------------------------------------------


def test_batch_link_is_accepted_by_the_service_handoff_without_dispatch(harness: Harness) -> None:
    h = harness
    cid = h.paths.candidate_id
    assert cid == h.app.pipeline.candidate_id  # the batch and the service see one candidate
    entry_id = _card(h)
    app_id = _prepared(h)
    entry = _ledger(SITE_URL, app_id, entry_id, "prepared", S.NEEDS_INPUT, PREPARED_MESSAGE)

    synced = sync_pipeline_card(h.paths, cid, entry)
    assert (synced.linked, synced.link_reason, synced.linked_application_id) == (True, None, app_id)
    assert (synced.closed_synced, synced.closed_sync_reason) == (None, None)
    [card] = _board(h)
    assert card["id"] == entry_id and card["lane"] == "saved"
    assert card["application"]["applicationId"] == app_id
    assert card["application"]["state"] == "NEEDS_INPUT"
    assert card["revision"] == 2  # one revision-checked link write
    assert [item["kind"] for item in card["history"]] == ["created"]  # a link is not a move

    # Idempotent: a second sync reports the same link and writes nothing.
    assert sync_pipeline_card(h.paths, cid, entry) == synced
    assert _board(h) == [card]

    # The service's own handoff accepts the batch-written link as its own: same
    # application, nothing dispatched (NEEDS_INPUT is not runnable), one link.
    handoff = h.client.post("/applications", _handoff(h, h.setup_candidate(), SITE_URL, entry_id))
    assert handoff.status == 200, handoff.json
    assert handoff.json["id"] == app_id and handoff.json["state"] == "NEEDS_INPUT"
    h.wait_idle()
    assert h.runs == []
    entries = _board(h)
    assert [e["application"]["applicationId"] for e in entries if e["application"]] == [app_id]
    assert len(entries) == 1 and entries[0]["revision"] == card["revision"]
    assert _moves_to_applied(entries) == []
    with h.store() as store:
        assert store.get_application(app_id).state is S.NEEDS_INPUT


def test_service_link_first_then_batch_sync_keeps_it_and_never_relinks(harness: Harness) -> None:
    h = harness
    cid = h.paths.candidate_id
    entry_id = _card(h)
    started = h.client.post("/applications", _handoff(h, h.setup_candidate(), SITE_URL, entry_id))
    assert started.status == 201, started.json
    app_id = started.json["id"]
    h.wait_idle()
    assert len(h.runs) == 1  # the service linked the card, then ran the scripted runner
    [linked] = _board(h)
    assert linked["application"]["applicationId"] == app_id
    with h.store() as store:
        state = store.get_application(app_id).state
    assert state is S.NEEDS_INPUT

    # A later batch finds the application already recorded: same link, no write.
    again = _ledger(SITE_URL, app_id, entry_id, "already_recorded", state,
                    f"an application already exists ({state.value})",
                    listing_id="lst_fictional_4012")
    synced = sync_pipeline_card(h.paths, cid, again)
    assert (synced.linked, synced.link_reason, synced.linked_application_id) == (True, None, app_id)
    assert synced.closed_synced is None
    assert _board(h) == [linked]

    # Rows for other applications never replace the service's link, nor move the card.
    other_url = SITE_URL + "-other"
    other = sync_pipeline_card(h.paths, cid, _ledger(
        other_url, _prepared(h, other_url), entry_id, "prepared", S.NEEDS_INPUT,
        PREPARED_MESSAGE))
    assert (other.linked, other.link_reason, other.linked_application_id) == (
        False, "card links another application", None)
    assert other.closed_synced is None
    closed_url = SITE_URL + "-closed"
    closed = sync_pipeline_card(h.paths, cid, _ledger(
        closed_url, _closed(h, closed_url), entry_id, "closed", S.FAILED_PERMANENT,
        CLOSED_MESSAGE))
    assert (closed.linked, closed.link_reason) == (False, "card links another application")
    assert (closed.closed_synced, closed.closed_sync_reason) == (False, "card not linked")
    assert _board(h) == [linked]  # still the original link, lane, revision and history


@pytest.mark.parametrize("outcome", ["closed", "already_recorded"])
def test_closed_sync_moves_only_the_lane_and_shows_the_note_on_the_board(
    harness: Harness, outcome: BatchOutcome,
) -> None:
    h = harness
    cid = h.paths.candidate_id
    entry_id = _card(h, status="Saved from a fictional search", fitScore=7,
                     nextAction="Review the prepared form",
                     processSourceNotes="Fictional referral from a fictional colleague")
    with h.app.pipeline.store() as pipeline:
        item = pipeline.get_item(cid, entry_id)
        pipeline.update_item(cid, entry_id, PipelineUpdate(
            notes="Fictional private note", next_action_due=date(2026, 10, 1),
        ), expected_revision=item.revision)
        item_before = pipeline.get_item(cid, entry_id)
    app_id = _closed(h)
    before = _board(h)
    [card_before] = before
    with h.store() as store:
        app_before = store.get_application(app_id)
        events_before = store.list_events(app_id)
    if outcome == "closed":
        entry = _ledger(SITE_URL, app_id, entry_id, "closed", S.FAILED_PERMANENT, CLOSED_MESSAGE,
                        finished_at=FINISHED_NON_UTC)
        observed = "2026-02-01"  # finished_at in UTC (Jan 31 at UTC-8)
    else:  # an earlier run saw it close; the note uses the stored application
        entry = _ledger(SITE_URL, app_id, entry_id, "already_recorded", S.FAILED_PERMANENT,
                        "an application already exists (FAILED_PERMANENT)",
                        finished_at=FINISHED_NON_UTC)
        assert app_before.updated_at is not None
        observed = app_before.updated_at.astimezone(UTC).date().isoformat()

    synced = sync_pipeline_card(h.paths, cid, entry)
    assert (synced.linked, synced.link_reason, synced.linked_application_id) == (True, None, app_id)
    assert (synced.closed_synced, synced.closed_sync_reason) == (True, None)

    after = _board(h)
    assert len(after) == len(before) == 1  # no card created
    [card] = after
    assert card["id"] == entry_id and card["lane"] == "closed"
    assert card["fields"] == card_before["fields"]
    assert card["applicationUrl"] == card_before["applicationUrl"] == SITE_URL
    assert card["listingId"] == card_before["listingId"]
    assert card["application"] == {
        "applicationId": app_id, "state": "FAILED_PERMANENT", "submittedAt": None,
        "confirmationReference": None, "confirmationMethod": None,
        "confirmationAuthority": None,
    }
    assert [(i["kind"], i["fromLane"], i["toLane"]) for i in card["history"]] == [
        ("created", None, "saved"), ("moved", "saved", "closed"),
    ]
    summary = card["history"][-1]["summary"]
    assert summary.startswith("Moved from Saved to Closed. ")
    assert f"{observed} (UTC)" in summary and f"prepare-batch {BATCH_ID}" in summary
    assert CLOSED_MESSAGE in summary and f"(application {app_id})" in summary
    assert "already exists" not in summary
    assert _moves_to_applied(after) == []
    unchanged = {"lane", "revision", "updated_at", "application_id"}
    with h.app.pipeline.store() as pipeline:
        item_after = pipeline.get_item(cid, entry_id)
    assert item_after.model_dump(exclude=unchanged) == item_before.model_dump(exclude=unchanged)
    with h.store() as store:  # the application itself is untouched
        assert store.get_application(app_id) == app_before
        assert store.get_receipt(app_id) is None
        assert store.list_events(app_id) == events_before

    # Re-running the batch moves nothing a second time: a card already in Closed is a
    # no-op (no move, no note, no problem recorded).
    rerun = sync_pipeline_card(h.paths, cid, entry)
    assert (rerun.linked, rerun.linked_application_id) == (True, app_id)
    assert (rerun.closed_synced, rerun.closed_sync_reason) == (None, None)
    assert _board(h) == after


def test_closed_sync_leaves_a_card_the_user_moved_to_applied(harness: Harness) -> None:
    h = harness
    cid = h.paths.candidate_id
    entry_id = _card(h)
    moved = h.client.post(f"/pipeline/entries/{entry_id}/move", {"revision": 1, "lane": "applied"})
    assert moved.status == 200 and moved.json["lane"] == "applied", moved.json
    app_id = _closed(h)

    synced = sync_pipeline_card(h.paths, cid, _ledger(
        SITE_URL, app_id, entry_id, "closed", S.FAILED_PERMANENT, CLOSED_MESSAGE))
    assert (synced.linked, synced.linked_application_id) == (True, app_id)
    assert synced.closed_synced is False
    assert (synced.closed_sync_reason or "").startswith("card not in Saved")
    assert synced.closed_sync_reason == "card not in Saved (in applied)"
    [card] = _board(h)
    assert card["id"] == entry_id and card["lane"] == "applied"
    assert card["application"]["applicationId"] == app_id  # linked, not moved
    assert [(i["kind"], i["fromLane"], i["toLane"]) for i in card["history"]] == [
        ("created", None, "saved"), ("moved", "saved", "applied"),
    ]
    # The only move to Applied is the user's own, without any batch note.
    assert _moves_to_applied([card]) == ["Moved from Saved to Applied."]


def test_closed_sync_disabled_links_but_leaves_the_card_in_saved(harness: Harness) -> None:
    h = harness
    cid = h.paths.candidate_id
    entry_id = _card(h)
    app_id = _closed(h)

    synced = sync_pipeline_card(h.paths, cid, _ledger(
        SITE_URL, app_id, entry_id, "closed", S.FAILED_PERMANENT, CLOSED_MESSAGE),
        sync_closed=False)
    assert (synced.linked, synced.linked_application_id) == (True, app_id)
    assert (synced.closed_synced, synced.closed_sync_reason) == (None, None)
    [card] = _board(h)
    assert card["lane"] == "saved" and [i["kind"] for i in card["history"]] == ["created"]
    assert card["application"]["applicationId"] == app_id
    assert card["application"]["state"] == "FAILED_PERMANENT"


def test_duplicate_row_links_the_survivor_the_service_handoff_resolves(harness: Harness) -> None:
    h = harness
    cid = h.paths.candidate_id
    entry_id = _card(h, url=ALIAS_URL)
    identity = JobIdentityObservation(
        ats_type="mock", ats_tenant="fictional", external_job_id="job-4012",
        title="Fictional Analyst", evidence_kind=IdentityEvidenceKind.ATS_JOB_ID_ON_PAGE,
        evidence="Job 4012 ID on the fictional form",
    )
    survivor = _prepared(h, SITE_URL, identity)
    with h.store() as store:  # the alias link turns out to be the same job
        duplicate, claim = _inspecting(store, cid, ALIAS_URL)
        assert store.bind_job_identity(claim, identity).duplicate_of == survivor
        store.release(claim)
        assert store.get_application(duplicate).state is S.DUPLICATE
        found = store.find_application(cid, ALIAS_URL)
        assert found is not None and found.id == survivor

    synced = sync_pipeline_card(h.paths, cid, _ledger(
        ALIAS_URL, duplicate, entry_id, "duplicate", S.DUPLICATE,
        "This job already has an application."))
    assert (synced.linked, synced.link_reason, synced.linked_application_id) == (
        True, None, survivor)
    assert synced.closed_synced is None
    [card] = _board(h)
    assert card["application"]["applicationId"] == survivor
    assert card["application"]["state"] == "NEEDS_INPUT"

    # The service resolves the same URL to the same survivor and keeps the link.
    handoff = h.client.post("/applications", _handoff(h, h.setup_candidate(), ALIAS_URL, entry_id))
    assert handoff.status == 200, handoff.json
    assert handoff.json["id"] == survivor
    h.wait_idle()
    assert h.runs == []
    assert _board(h) == [card]
