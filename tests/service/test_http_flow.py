"""HTTP contract and application flow over the real store with a scripted runner."""

from __future__ import annotations

import json
import time
from typing import Any

from interviewmaxxing_core import ApplicationState, SubmissionOutcome

from .conftest import SITE_URL, Harness

S = ApplicationState

VIEW_KEYS = {
    "id", "state", "applicationUrl", "job", "requestedAt", "updatedAt", "progress",
    "resumeFileName", "needs", "receipt", "prior", "failure", "uncertain", "events",
}


def poll(h: Harness, app_id: str, until: set[str], timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        view = h.client.get(f"/applications/{app_id}").json
        if view["state"] in until:
            return view
        assert time.monotonic() < deadline, f"stuck in {view['state']}"
        time.sleep(0.05)


def answer_all(h: Harness, app_id: str, view: dict[str, Any]) -> dict[str, Any]:
    need = view["needs"]
    answers: dict[str, Any] = {}
    for q in need["questions"]:
        if q["control"] == "single_select":
            answers[q["id"]] = q["options"][-1]["value"]
        elif q["control"] == "multi_select":
            answers[q["id"]] = [q["options"][0]["value"]]
        else:
            answers[q["id"]] = "Because the fictional role fits my verified experience."
    attestations = {a["id"]: True for a in need["attestations"]}
    return h.client.post(
        f"/applications/{app_id}/answers", {"answers": answers, "attestations": attestations}
    ).json


def test_health_has_no_private_data(harness: Harness) -> None:
    r = harness.client.get("/healthz")
    assert r.status == 200
    assert r.json == {
        "status": "ok", "service": "interviewmaxxing-service", "contractVersion": "2",
        "executor": "idle",
    }
    assert "access-control-allow-origin" not in r.headers
    assert r.headers["cache-control"] == "no-store"


def test_empty_candidate_then_upload_and_select(harness: Harness) -> None:
    empty = harness.client.get("/candidate").json
    assert empty == {
        "profile": {"firstName": "", "lastName": "", "email": "", "phone": "", "location": "",
                    "linkedinUrl": "", "websiteUrl": ""},
        "resumes": [],
        "defaultResumeId": None,
    }
    up = harness.client.upload("R\u00e9sum\u00e9 \u2013 Avery.pdf", b"%PDF-1.4 fictional")
    assert up.status == 201
    assert set(up.json) == {"id", "fileName", "sizeBytes", "uploadedAt"}
    assert up.json["fileName"] == "R\u00e9sum\u00e9 \u2013 Avery.pdf"
    assert up.json["sizeBytes"] == len(b"%PDF-1.4 fictional")
    listed = harness.client.get("/candidate").json
    assert [r["id"] for r in listed["resumes"]] == [up.json["id"]]
    assert listed["defaultResumeId"] is None  # uploaded, not yet chosen for a profile


def test_start_records_before_dispatch_and_asks_exact_questions(harness: Harness) -> None:
    harness.site.hold.clear()  # keep the runner waiting so we see the recorded state
    r = harness.start()
    assert r.status == 201
    view = r.json
    assert set(view) == VIEW_KEYS
    assert view["state"] == "REQUESTED"
    assert view["applicationUrl"] == SITE_URL
    app_id = view["id"]
    with harness.store() as store:  # durable before the runner did anything
        assert store.get_application(app_id).state is S.REQUESTED
    harness.site.hold.set()

    view = poll(harness, app_id, {"NEEDS_INPUT"})
    need = view["needs"]
    assert need["kind"] == "questions"
    by_label = {q["label"]: q for q in need["questions"]}
    gender = by_label["Gender (voluntary)"]
    assert gender["control"] == "single_select"
    assert gender["options"] == [
        {"value": "f", "label": "Female"}, {"value": "m", "label": "Male"},
        {"value": "x", "label": "Non-binary"},
        {"value": "decline", "label": "I decline to self-identify"},
    ]
    assert gender["value"] is None
    assert gender["reason"].startswith("Only you can answer this")
    assert by_label["How did you hear about us?"]["control"] == "multi_select"
    assert [a["accepted"] for a in need["attestations"]] == [False]
    assert need["errors"] == {}
    # The profile the user confirmed was saved through the candidate API before running.
    assert harness.candidates.upserts == 1
    assert harness.runs[0].allow_browser_action is False


def test_full_flow_answers_resume_receipt_and_repeat(harness: Harness) -> None:
    app_id = harness.start().json["id"]
    view = poll(harness, app_id, {"NEEDS_INPUT"})
    saved = answer_all(harness, app_id, view)
    assert saved["state"] == "NEEDS_INPUT"
    assert saved["needs"]["savedAt"] is not None
    assert all(q["value"] is not None for q in saved["needs"]["questions"])
    assert all(a["accepted"] for a in saved["needs"]["attestations"])

    resumed = harness.client.post(f"/applications/{app_id}/resume", {})
    assert resumed.status == 200
    assert resumed.json["state"] != "NEEDS_INPUT"
    done = poll(harness, app_id, {"SUBMITTED"})
    receipt = done["receipt"]
    assert receipt["confirmationReference"] == "FIC-000001"
    assert receipt["confirmationMethod"] == "SUBMISSION_OBSERVED"
    assert receipt["confirmationAuthority"] == "site"
    assert {e["source"] for e in receipt["evidence"]} == {"site"}
    assert any(e["kind"] == "screenshot" and e["href"] for e in receipt["evidence"])
    assert harness.site.accepted_posts == 1

    # Repeating the request returns the same application; nothing is sent again.
    again = harness.start(resume_id=harness.setup_candidate())
    assert again.status == 200
    assert again.json["id"] == app_id
    assert again.json["state"] == "SUBMITTED"
    harness.wait_idle()
    assert harness.site.accepted_posts == 1
    assert "The earlier submission stands" in again.json["events"][-1]["message"]
    with harness.store() as store:
        # The service's request, the runner's own (folded in the view) and the repeat.
        assert len(store.list_requests(app_id)) == 3
    types = [e["type"] for e in again.json["events"]]
    assert types.count("application.requested") == 1
    assert types.count("application.request_repeated") == 1


def test_answers_are_validated_against_the_sites_options(harness: Harness) -> None:
    app_id = harness.start().json["id"]
    view = poll(harness, app_id, {"NEEDS_INPUT"})
    gender = next(q for q in view["needs"]["questions"] if q["label"] == "Gender (voluntary)")

    # A placeholder / invented option is rejected, nothing is saved.
    bad = harness.client.post(
        f"/applications/{app_id}/answers",
        {"answers": {gender["id"]: "Female"}, "attestations": {}},
    )
    assert bad.status == 200
    assert bad.json["needs"]["errors"] == {gender["id"]: "Choose one of the listed options."}
    with harness.store() as store:
        assert store.list_user_inputs(app_id) == []

    # An unknown / stale question id is a conflict.
    stale = harness.client.post(
        f"/applications/{app_id}/answers", {"answers": {"q_nope": "x"}, "attestations": {}}
    )
    assert stale.status == 409
    assert stale.json["error"]["code"] == "conflict"
    assert "q_nope" in stale.json["error"]["fieldErrors"]

    # Declining a required statement is not an answer.
    privacy = view["needs"]["attestations"][0]
    declined = harness.client.post(
        f"/applications/{app_id}/answers",
        {"answers": {}, "attestations": {privacy["id"]: False}},
    )
    assert privacy["id"] in declined.json["needs"]["errors"]

    # Resume refuses while required questions are unanswered.
    blocked = harness.client.post(f"/applications/{app_id}/resume", {})
    assert blocked.status == 422
    assert blocked.json["error"]["code"] == "invalid"
    assert set(blocked.json["error"]["fieldErrors"]) == {
        q["id"] for q in view["needs"]["questions"]
    } | {privacy["id"]}
    assert harness.site.accepted_posts == 0


def test_answer_preserves_exact_question_and_default_scope(harness: Harness) -> None:
    app_id = harness.start().json["id"]
    view = poll(harness, app_id, {"NEEDS_INPUT"})
    gender = next(q for q in view["needs"]["questions"] if q["label"] == "Gender (voluntary)")
    why = next(q for q in view["needs"]["questions"] if q["control"] == "long_text")
    r = harness.client.post(
        f"/applications/{app_id}/answers",
        {"answers": {gender["id"]: "decline", why["id"]: "A fictional reason."},
         "attestations": {}, "reuse": {why["id"]: "global"}},
    )
    assert r.status == 200
    with harness.store() as store:
        inputs = {u.field_id: u for u in store.list_user_inputs(app_id)}
        packet = store.latest_packet(app_id)
    assert packet is not None
    asked = {m.field_id: m for m in packet.missing_inputs}
    form = harness.site.form
    assert inputs["gender"].value.model_dump() == {
        "kind": "choice", "value": "decline", "label": "I decline to self-identify"
    }
    assert inputs["gender"].field_fingerprint == form.field("gender").fingerprint
    assert inputs["gender"].question == asked["gender"].label
    assert inputs["gender"].reuse.value == "APPLICATION"
    assert inputs["why_us"].reuse.value == "GLOBAL"
    # Only the answer the user chose to reuse reaches the candidate package.
    assert [a.scope.value for a in harness.candidates.saved_answers] == ["GLOBAL"]
    assert harness.candidates.saved_answers[0].value == "A fictional reason."


def test_sign_in_interaction_then_continue(harness: Harness) -> None:
    harness.site.sign_in_first = True
    app_id = harness.start().json["id"]
    view = poll(harness, app_id, {"NEEDS_INPUT"})
    assert view["needs"] == {
        "kind": "interaction", "interaction": "SIGN_IN",
        "instructions": view["needs"]["instructions"],
        "pageUrl": "https://jobs.example.test/sign-in",
    }
    harness.client.post(f"/applications/{app_id}/resume", {})
    view = poll(harness, app_id, {"NEEDS_INPUT"})
    assert harness.runs[-1].allow_browser_action is True
    assert view["needs"]["kind"] == "questions"


def test_uncertain_submission_locks_and_reconciles_through_the_site(harness: Harness) -> None:
    harness.site.outcome = SubmissionOutcome.UNKNOWN
    app_id = harness.start().json["id"]
    answer_all(harness, app_id, poll(harness, app_id, {"NEEDS_INPUT"}))
    harness.client.post(f"/applications/{app_id}/resume", {})
    view = poll(harness, app_id, {"SUBMISSION_UNKNOWN"})
    assert view["uncertain"]["reason"]
    assert view["receipt"] is None
    assert harness.site.accepted_posts == 1

    # No retry, from resume or from a repeated request.
    assert harness.client.post(f"/applications/{app_id}/resume", {}).status == 409
    again = harness.start(resume_id=harness.setup_candidate())
    assert again.json["state"] == "SUBMISSION_UNKNOWN"
    harness.wait_idle()
    assert harness.site.accepted_posts == 1

    # A recheck that finds nothing keeps the lock and says so.
    first = harness.client.post(f"/applications/{app_id}/reconcile", {"kind": "recheck"})
    assert first.status == 200
    assert first.json["state"] == "SUBMISSION_UNKNOWN"
    assert first.json["uncertain"]["lastCheckedAt"] is not None
    assert "stays locked" in first.json["uncertain"]["lastCheckResult"]

    # The user's report of non-receipt is recorded as theirs and does not unlock.
    report = harness.client.post(
        f"/applications/{app_id}/reconcile", {"kind": "user_confirmed_not_received"}
    )
    assert report.json["state"] == "SUBMISSION_UNKNOWN"
    assert any(e["source"] == "user" for e in report.json["uncertain"]["evidence"])
    assert harness.client.post(f"/applications/{app_id}/resume", {}).status == 409

    # When the site shows the confirmation, the recheck records it.
    harness.site.revealed = True
    settled = harness.client.post(f"/applications/{app_id}/reconcile", {"kind": "recheck"})
    assert settled.json["state"] == "SUBMITTED"
    assert settled.json["receipt"]["confirmationReference"] == "FIC-000001"
    assert settled.json["receipt"]["confirmationMethod"] == "SITE_CONFIRMATION"
    assert settled.json["receipt"]["confirmationAuthority"] == "site"
    assert harness.site.accepted_posts == 1


def test_user_found_confirmation_is_attributed_to_the_user(harness: Harness) -> None:
    harness.site.outcome = SubmissionOutcome.UNKNOWN
    app_id = harness.start().json["id"]
    answer_all(harness, app_id, poll(harness, app_id, {"NEEDS_INPUT"}))
    harness.client.post(f"/applications/{app_id}/resume", {})
    poll(harness, app_id, {"SUBMISSION_UNKNOWN"})
    r = harness.client.post(
        f"/applications/{app_id}/reconcile",
        {"kind": "user_found_confirmation", "foundIn": "email", "reference": "EM-42",
         "note": "Thanks for applying email"},
    )
    view = r.json
    assert view["state"] == "SUBMITTED"
    assert view["receipt"]["confirmationReference"] == "EM-42"
    sources = {e["source"] for e in view["receipt"]["evidence"] if e["kind"] == "user_report"}
    assert sources == {"user"}
    # The uncertain page's screenshots are still listed as site artifacts, but they
    # did not confirm anything: the receipt's authority is the user's report.
    assert {e["source"] for e in view["receipt"]["evidence"]} == {"site", "user"}
    assert view["receipt"]["confirmationMethod"] == "USER_CONFIRMED"
    assert view["receipt"]["confirmationAuthority"] == "user"
    assert "on your report" in view["events"][-1]["message"]
    with harness.store() as store:
        receipt = store.get_receipt(app_id)
    assert receipt is not None and receipt.reconciliation_method.value == "USER_CONFIRMED"


def test_reconcile_requires_an_unknown_submission(harness: Harness) -> None:
    app_id = harness.start().json["id"]
    poll(harness, app_id, {"NEEDS_INPUT"})
    r = harness.client.post(f"/applications/{app_id}/reconcile", {"kind": "recheck"})
    assert r.status == 409
    bad = harness.client.post(f"/applications/{app_id}/reconcile", {"kind": "retry"})
    assert bad.status == 400


def test_crash_before_submit_is_retryable_and_crash_state_is_durable(harness: Harness) -> None:
    harness.site.crash_before_submit = True
    app_id = harness.start().json["id"]
    answer_all(harness, app_id, poll(harness, app_id, {"NEEDS_INPUT"}))
    harness.client.post(f"/applications/{app_id}/resume", {})
    view = poll(harness, app_id, {"FAILED_RETRYABLE"})
    assert view["failure"]["retryable"] is True
    assert harness.site.accepted_posts == 0
    harness.site.crash_before_submit = False
    harness.client.post(f"/applications/{app_id}/resume", {})
    assert poll(harness, app_id, {"SUBMITTED"})["receipt"] is not None
    assert harness.site.accepted_posts == 1


def test_one_run_at_a_time(harness: Harness) -> None:
    harness.site.hold.clear()
    first = harness.start()
    assert harness.site.holding.wait(5)
    second = harness.start(url="https://jobs.example.test/fictional-co/5555/apply")
    assert second.status == 409
    assert second.json["error"]["code"] == "conflict"
    with harness.store() as store:  # the refused request was not recorded
        assert len(store.list_applications()) == 1
    # Asking again for the running application just reports it.
    same = harness.start()
    assert same.status == 200 and same.json["id"] == first.json["id"]
    harness.site.hold.set()
    poll(harness, first.json["id"], {"NEEDS_INPUT"})


def test_client_disconnect_does_not_lose_the_application(harness: Harness) -> None:
    import socket

    rid = harness.setup_candidate()
    body = json.dumps(
        {"applicationUrl": SITE_URL, "profile": harness.profile(), "resumeId": rid}
    ).encode()
    port = harness.client.port
    sock = socket.create_connection(("127.0.0.1", port))
    sock.sendall(
        b"POST /applications HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nOrigin: http://127.0.0.1:4317\r\n"
        b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" % (port, len(body)) + body
    )
    sock.close()  # leave before reading the response
    deadline = time.monotonic() + 5
    while True:
        with harness.store() as store:
            apps = store.list_applications()
        if apps:
            break
        assert time.monotonic() < deadline
        time.sleep(0.05)
    # Asking again recovers the same application id.
    again = harness.start(resume_id=rid)
    assert again.json["id"] == apps[0].id
    harness.wait_idle()


def test_concurrent_identical_starts_make_one_application(harness: Harness) -> None:
    from concurrent.futures import ThreadPoolExecutor

    rid = harness.setup_candidate()
    harness.site.hold.clear()
    payload = {"applicationUrl": SITE_URL, "profile": harness.profile(), "resumeId": rid}
    with ThreadPoolExecutor(8) as pool:
        responses = list(pool.map(
            lambda _: harness.client.post("/applications", payload), range(8)
        ))
    harness.site.hold.set()
    assert {r.status for r in responses} <= {200, 201}
    assert len({r.json["id"] for r in responses}) == 1
    assert [r.status for r in responses].count(201) == 1
    poll(harness, responses[0].json["id"], {"NEEDS_INPUT"})
    assert len(harness.runs) == 1  # one run, not eight
