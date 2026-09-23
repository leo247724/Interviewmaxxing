"""Pipeline routes over the real P1 ``PipelineStore`` (fictional rows only)."""

from __future__ import annotations

import csv
import io
from typing import Any

from interviewmaxxing_core import ApplicationState, ApplicationStore
from interviewmaxxing_pipeline import FIELD_HEADERS, FIELD_KEYS, PipelineStore, PipelineUpdate

from .conftest import Harness

EMPTY = dict.fromkeys(FIELD_KEYS)


def _fields(**values: Any) -> dict[str, Any]:
    return {**EMPTY, **values}


def _csv(rows: list[dict[str, str]]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=FIELD_HEADERS)
    writer.writeheader()
    for row in rows:
        writer.writerow({h: row.get(h, "") for h in FIELD_HEADERS})
    return out.getvalue()


def _create(h: Harness, **values: Any) -> dict[str, Any]:
    r = h.client.post("/pipeline/entries", {
        "lane": "saved",
        "fields": _fields(company="Fictional Co", role="Marketing Manager", **values),
        "applicationUrl": None,
    })
    assert r.status == 201, r.json
    return dict(r.json)


def test_board_starts_with_lanes_and_no_entries(harness: Harness) -> None:
    board = harness.client.get("/pipeline").json
    assert [lane["id"] for lane in board["lanes"]][:2] == ["saved", "applied"]
    assert board["entries"] == []


def test_create_keeps_all_23_fields_and_blank_is_null(harness: Harness) -> None:
    entry = _create(harness, fitScore=0, compensationLow=120000, compensationHigh=140000,
                    nextInterviewDate="2026-10-01", interviewTimeCT="14:30",
                    decisionDueText="End of next week", stage="Recruiter screen booked")
    assert set(entry["fields"]) == set(FIELD_KEYS)
    assert entry["fields"]["fitScore"] == 0  # zero is not blank
    assert entry["fields"]["priority"] is None
    assert entry["fields"]["decisionDueText"] == "End of next week"
    assert entry["origin"] == "manual" and entry["revision"] == 1
    assert entry["application"] is None and entry["applicationUrl"] is None
    assert [h["kind"] for h in entry["history"]] == ["created"]
    board = harness.client.get("/pipeline").json
    assert [e["id"] for e in board["entries"]] == [entry["id"]]


def test_update_move_history_and_stale_revisions(harness: Harness) -> None:
    entry = _create(harness)
    eid = entry["id"]
    upd = harness.client.post(f"/pipeline/entries/{eid}", {
        "revision": 1, "fields": {"status": "Applied via careers page", "nextAction": "Wait"},
        "applicationUrl": "https://jobs.example.test/fictional/1/apply",
    })
    assert upd.status == 200, upd.json
    assert upd.json["revision"] == 2
    assert upd.json["fields"]["role"] == "Marketing Manager"  # partial update keeps others
    assert upd.json["applicationUrl"] == "https://jobs.example.test/fictional/1/apply"
    stale = harness.client.post(f"/pipeline/entries/{eid}", {"revision": 1, "fields": {}})
    assert stale.status == 409 and stale.json["error"]["code"] == "conflict"

    moved = harness.client.post(f"/pipeline/entries/{eid}/move", {"revision": 2, "lane": "offer"})
    assert moved.status == 200
    assert moved.json["lane"] == "offer"
    kinds = [h["kind"] for h in moved.json["history"]]
    assert kinds[0] == "created" and kinds[-1] == "moved"
    assert moved.json["history"][-1]["toLane"] == "offer"
    # A manual "Offer" lane is the user's record: no application or receipt appears.
    assert moved.json["application"] is None
    with harness.store() as store:
        assert store.list_applications() == []

    bad_lane = harness.client.post(f"/pipeline/entries/{eid}/move", {"revision": 3, "lane": "nope"})
    assert bad_lane.status == 422 and "lane" in bad_lane.json["error"]["fieldErrors"]


def test_invalid_fields_and_absent_entries(harness: Harness) -> None:
    r = harness.client.post("/pipeline/entries", {
        "lane": "saved", "fields": _fields(company="X", fitScore=11), "applicationUrl": None,
    })
    assert r.status == 422 and "fitScore" in r.json["error"]["fieldErrors"]
    r = harness.client.post("/pipeline/entries", {
        "lane": "saved", "fields": {"company": "X", "bogus": 1}, "applicationUrl": None,
    })
    assert r.json["error"]["fieldErrors"] == {"bogus": "This field isn't part of the pipeline."}
    r = harness.client.post("/pipeline/entries", {
        "lane": "saved", "fields": _fields(company="X"), "applicationUrl": "ftp://nope",
    })
    assert r.status == 422 and "applicationUrl" in r.json["error"]["fieldErrors"]
    assert harness.client.post(
        "/pipeline/entries/pipe_missing", {"revision": 1, "fields": {}}
    ).status == 404
    assert harness.client.post("/pipeline/entries/..%2Fx/move", {"revision": 1, "lane": "saved"}
                               ).status == 400
    assert harness.client.get("/pipeline", origin="http://evil.example").status == 403


def test_other_candidates_cards_are_invisible(harness: Harness) -> None:
    from interviewmaxxing_pipeline import NewPipelineItem, TrackingFields

    with PipelineStore.from_paths(harness.paths) as store:
        other = store.create_item(
            "someone-else", NewPipelineItem(tracking=TrackingFields(company="Other Co"))
        )
    assert harness.client.get("/pipeline").json["entries"] == []
    assert harness.client.post(
        f"/pipeline/entries/{other.id}/move", {"revision": 1, "lane": "applied"}
    ).status == 404


def test_linked_application_is_the_only_submission_state(harness: Harness) -> None:
    entry = _create(harness)
    with ApplicationStore.open(harness.paths.state_db) as apps:
        app = apps.record_request("default", "https://jobs.example.test/fictional/9/apply").application
    with PipelineStore.from_paths(harness.paths) as store:
        store.update_item("default", entry["id"], PipelineUpdate(application_id=app.id),
                          expected_revision=1)
    view = harness.client.get("/pipeline").json["entries"][0]
    assert view["application"] == {
        "applicationId": app.id, "state": ApplicationState.REQUESTED.value,
        "submittedAt": None, "confirmationReference": None,
        "confirmationMethod": None, "confirmationAuthority": None,
    }


def test_import_preview_commit_and_idempotent_reimport(harness: Harness) -> None:
    rows = [
        {"Company": "Fictional Co", "Role": "Marketing Director", "Stage": "Applied",
         "Status": "Waiting to hear back", "Fit / 10": "8", "Comp low (USD/year)": "$120,000",
         "Comp high (USD/year)": "$150,000"},
        {"Company": "Example Labs", "Role": "Marketing Manager", "Stage": "Interviewing",
         "Status": "Panel scheduled", "Fit / 10": "12"},
    ]
    bad = harness.client.post("/pipeline/import/preview", {
        "format": "csv", "fileName": "tracker.csv", "content": _csv(rows),
        "sourceId": "fictional-tracker",
    })
    assert bad.status == 200, bad.json
    assert bad.json["counts"]["error"] == 1
    error_row = next(r for r in bad.json["rows"] if r["action"] == "error")
    assert error_row["errors"][0]["field"] == "fitScore"
    refused = harness.client.post(f"/pipeline/import/{bad.json['previewId']}/commit", {})
    assert refused.status == 409
    assert harness.client.get("/pipeline").json["entries"] == []  # nothing partial

    rows[1]["Fit / 10"] = "7"
    good = harness.client.post("/pipeline/import/preview", {
        "format": "csv", "fileName": "tracker.csv", "content": _csv(rows),
        "sourceId": "fictional-tracker",
    }).json
    assert good["counts"] == {"create": 2, "update": 0, "unchanged": 0, "error": 0}
    receipt = harness.client.post(f"/pipeline/import/{good['previewId']}/commit", {})
    assert receipt.status == 200
    assert receipt.json["created"] == 2 and receipt.json["fileName"] == "tracker.csv"
    entries = harness.client.get("/pipeline").json["entries"]
    assert {e["origin"] for e in entries} == {"import"}
    prov = next(e for e in entries if e["fields"]["company"] == "Fictional Co")["provenance"]
    assert prov["importId"] == receipt.json["importId"]
    assert prov["importedValues"]["Status"] == "Waiting to hear back"
    assert prov["sourceRow"] == 2
    assert prov["sourceId"] == "fictional-tracker"
    assert prov["latestImportedValues"] == prov["importedValues"]
    assert prov["versionCount"] == 1

    # The same preview cannot be committed twice; a reimport changes nothing.
    assert harness.client.post(f"/pipeline/import/{good['previewId']}/commit", {}).status == 404
    again = harness.client.post("/pipeline/import/preview", {
        "format": "csv", "fileName": "tracker.csv", "content": _csv(rows),
        "sourceId": "fictional-tracker",
    }).json
    assert again["counts"]["unchanged"] == 2
    assert len(harness.client.get("/pipeline").json["entries"]) == 2


def test_import_bounds_and_unreadable_files(harness: Harness) -> None:
    unreadable = harness.client.post("/pipeline/import/preview", {
        "format": "json", "fileName": "x.json", "content": "{not json",
    })
    assert unreadable.status in (200, 422)
    if unreadable.status == 200:
        assert unreadable.json["counts"]["error"] >= 1
    # Declared size over the 4 MiB import bound is refused before the body is read.
    r = harness.client.request("POST", "/pipeline/import/preview", None, {
        "Content-Type": "application/json", "Content-Length": str(5 * 1024 * 1024),
    })
    assert r.status == 413
    # Other routes keep the 64 KiB JSON bound.
    r = harness.client.request("POST", "/pipeline/entries", None, {
        "Content-Type": "application/json", "Content-Length": str(100 * 1024),
    })
    assert r.status == 413


def test_uploaded_bytes_need_an_explicit_logical_source(harness: Harness) -> None:
    rows = [{"Company": "Fictional Co", "Role": "Marketing Director", "Stage": "Applied"}]
    preview = harness.client.post("/pipeline/import/preview", {
        "format": "csv", "fileName": "tracker.csv", "content": _csv(rows),
    }).json
    assert preview["counts"]["error"] >= 1  # no source id: nothing can be committed
    assert harness.client.post(f"/pipeline/import/{preview['previewId']}/commit", {}).status == 409
    bad = harness.client.post("/pipeline/import/preview", {
        "format": "csv", "fileName": "tracker.csv", "content": _csv(rows), "sourceId": "../x",
    })
    assert bad.status == 422 and "sourceId" in bad.json["error"]["fieldErrors"]
