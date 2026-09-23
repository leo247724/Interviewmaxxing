"""Loopback, origin, content-type, bounds, isolation and evidence-serving rules."""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any

import pytest

from interviewmaxxing_core import ApplicationStore, EvidenceKind, EvidenceRef

from .conftest import ORIGIN, SITE_URL, Harness


def _needs_input(h: Harness) -> str:
    app_id: str = h.start().json["id"]
    deadline = time.monotonic() + 10
    while h.client.get(f"/applications/{app_id}").json["state"] != "NEEDS_INPUT":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return app_id


def _submitted(h: Harness) -> str:
    app_id = _needs_input(h)
    need = h.client.get(f"/applications/{app_id}").json["needs"]
    answers: dict[str, Any] = {}
    for q in need["questions"]:
        opts = q["options"]
        answers[q["id"]] = (
            [opts[0]["value"]] if q["control"] == "multi_select"
            else opts[0]["value"] if opts else "Fictional answer."
        )
    h.client.post(
        f"/applications/{app_id}/answers",
        {"answers": answers, "attestations": {a["id"]: True for a in need["attestations"]}},
    )
    h.client.post(f"/applications/{app_id}/resume", {})
    h.wait_idle()
    assert h.client.get(f"/applications/{app_id}").json["state"] == "SUBMITTED"
    return app_id


@pytest.mark.parametrize("host", ["evil.example:80", "127.0.0.1", "127.0.0.2:{port}", "localhost.evil.example:{port}"])
def test_host_must_be_this_loopback_address(harness: Harness, host: str) -> None:
    r = harness.client.request(
        "GET", "/healthz", host=host.format(port=harness.client.port), origin=None
    )
    assert r.status == 403
    assert r.json["error"]["code"] == "invalid"


def test_localhost_host_names_are_accepted(harness: Harness) -> None:
    for name in ("localhost", "127.0.0.1"):
        r = harness.client.request(
            "GET", "/healthz", host=f"{name}:{harness.client.port}", origin=None
        )
        assert r.status == 200


@pytest.mark.parametrize(
    "origin", [None, "http://evil.example", "http://127.0.0.1:4318", "null", ORIGIN + "/"]
)
def test_mutations_need_the_exact_origin(harness: Harness, origin: str | None) -> None:
    r = harness.client.post(
        "/applications",
        {"applicationUrl": SITE_URL, "profile": harness.profile(), "resumeId": "x"},
        origin=origin,
    )
    assert r.status == 403
    with harness.store() as store:
        assert store.list_applications() == []


def test_cross_site_fetch_metadata_is_refused(harness: Harness) -> None:
    r = harness.client.upload("cv.pdf", b"%PDF", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status == 403
    assert harness.candidates.list_resumes("default") == []


def test_reads_with_a_foreign_origin_are_refused(harness: Harness) -> None:
    r = harness.client.get("/candidate", origin="http://evil.example")
    assert r.status == 403
    assert harness.client.get("/candidate", origin=ORIGIN).status == 200


def test_json_routes_need_json(harness: Harness) -> None:
    body = b"applicationUrl=https%3A%2F%2Fjobs.example.test"
    for ctype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data"):
        r = harness.client.request("POST", "/applications", body, {"Content-Type": ctype})
        assert r.status == 415


def test_malformed_json_and_fields(harness: Harness) -> None:
    raw = harness.client.request(
        "POST", "/applications", b"{not json", {"Content-Type": "application/json"}
    )
    assert raw.status == 400
    dup = harness.client.request(
        "POST", "/applications", b'{"a":1,"a":2}', {"Content-Type": "application/json"}
    )
    assert dup.status == 400
    missing = harness.client.post("/applications", {"applicationUrl": SITE_URL})
    assert missing.status == 400
    assert set(missing.json["error"]["fieldErrors"]) == {"profile", "resumeId"}
    extra = harness.client.post(
        "/applications",
        {"applicationUrl": SITE_URL, "profile": harness.profile(), "resumeId": "r", "x": 1},
    )
    assert extra.json["error"]["fieldErrors"] == {"x": "This field isn't accepted."}


def test_start_field_errors(harness: Harness) -> None:
    rid = harness.setup_candidate()
    cases = {
        "ftp://jobs.example.test/1": "applicationUrl",
        "not a url": "applicationUrl",
        "https://user:pw@jobs.example.test/1": "applicationUrl",
    }
    for url, field in cases.items():
        r = harness.client.post(
            "/applications", {"applicationUrl": url, "profile": harness.profile(), "resumeId": rid}
        )
        assert r.status == 422 and field in r.json["error"]["fieldErrors"], url
    wrong_resume = harness.client.post(
        "/applications",
        {"applicationUrl": SITE_URL, "profile": harness.profile(), "resumeId": "res_other"},
    )
    assert wrong_resume.json["error"]["fieldErrors"] == {
        "resumeId": "Choose one of your saved resumes or upload one."
    }
    bad_email = harness.client.post(
        "/applications",
        {"applicationUrl": SITE_URL, "profile": harness.profile(email="nope"), "resumeId": rid},
    )
    assert bad_email.json["error"]["fieldErrors"] == {"email": "Enter a valid email address."}
    bad_location = harness.client.post(
        "/applications",
        {"applicationUrl": SITE_URL, "profile": harness.profile(location="a, b, c, d"),
         "resumeId": rid},
    )
    assert "location" in bad_location.json["error"]["fieldErrors"]
    with harness.store() as store:
        assert store.list_applications() == []
    assert harness.candidates.upserts == 0


def test_query_strings_are_refused(harness: Harness) -> None:
    r = harness.client.get("/candidate?email=avery@example.test")
    assert r.status == 400


def test_unknown_routes_methods_and_ids(harness: Harness) -> None:
    assert harness.client.get("/nope").status == 404
    assert harness.client.request("DELETE", "/candidate").status == 405
    assert harness.client.request("OPTIONS", "/applications").status == 405
    assert harness.client.get("/candidate", origin=None).status == 200
    assert harness.client.get("/applications/app_doesnotexist").status == 404
    assert harness.client.get("/applications/..%2F..%2Fetc").status == 400
    assert harness.client.get("/applications/%00").status == 400


def test_upload_rules(harness: Harness) -> None:
    c = harness.client
    assert c.upload("cv.exe", b"MZ").json["error"]["fieldErrors"]["resumeFile"]
    assert c.upload("cv.pdf", b"").json["error"]["fieldErrors"]["resumeFile"] == "The file is empty."
    for name in ("../../etc/passwd.pdf", "a/b.pdf", "a\\b.pdf", ".hidden.pdf", "bad\x00.pdf"):
        r = c.upload(name, b"%PDF")
        assert r.status == 422, name
    raw = c.request(
        "POST", "/resumes", b"%PDF",
        {"Content-Type": "application/octet-stream", "X-Imx-Filename": "%ZZ"},
    )
    assert raw.status in (400, 422)
    no_name = c.request("POST", "/resumes", b"%PDF", {"Content-Type": "application/octet-stream"})
    assert no_name.status == 422
    multipart = c.request("POST", "/resumes", b"--x", {"Content-Type": "multipart/form-data"})
    assert multipart.status == 415
    rejected = c.upload("cv.pdf", b"REJECT")
    assert rejected.json["error"]["fieldErrors"] == {
        "resumeFile": "The candidate store rejected this file."
    }
    assert harness.candidates.list_resumes("default") == []


def test_upload_size_bound(harness: Harness) -> None:
    limit = harness.service.config.max_upload_bytes
    r = harness.client.request(
        "POST", "/resumes", None,
        {"Content-Type": "application/octet-stream", "X-Imx-Filename": "cv.pdf",
         "Content-Length": str(limit + 1)},
    )
    assert r.status == 413
    big_json = b'{"answers":{"q":"' + b"x" * (70 * 1024) + b'"},"attestations":{}}'
    r = harness.client.request(
        "POST", "/applications/app_x/answers", big_json, {"Content-Type": "application/json"}
    )
    assert r.status == 413


def test_profile_updates_keep_address_and_confirmation_time(harness: Harness) -> None:
    rid = harness.setup_candidate()
    first = harness.client.post(
        "/applications",
        {"applicationUrl": SITE_URL, "profile": harness.profile(), "resumeId": rid},
    )
    harness.wait_idle()
    profile = harness.candidates.profiles["default"]
    assert profile.identity.address.city == "Springfield"
    assert profile.identity.address.region == "Oregon"
    assert profile.identity.address.country == "USA"
    assert first.status == 201
    view = harness.client.get("/candidate").json
    assert view["profile"]["location"] == "Springfield, Oregon, USA"
    assert view["defaultResumeId"] == rid
    confirmed = profile.identity.verified_at

    # Same details again: nothing is rewritten.
    harness.client.post(
        "/applications",
        {"applicationUrl": SITE_URL, "profile": harness.profile(), "resumeId": rid},
    )
    harness.wait_idle()
    assert harness.candidates.profiles["default"].identity.verified_at == confirmed


def test_other_candidates_applications_are_invisible(harness: Harness) -> None:
    with ApplicationStore.open(harness.paths.state_db) as store:
        other = store.record_request("someone-else", "https://jobs.example.test/x/1").application
    assert harness.client.get(f"/applications/{other.id}").status == 404
    assert harness.client.post(f"/applications/{other.id}/resume", {}).status == 404
    assert harness.client.post(
        f"/applications/{other.id}/answers", {"answers": {}, "attestations": {}}
    ).status == 404


def test_evidence_is_served_by_opaque_id_only(harness: Harness) -> None:
    app_id = _submitted(harness)
    view = harness.client.get(f"/applications/{app_id}").json
    shot = next(e for e in view["receipt"]["evidence"] if e["kind"] == "screenshot")
    assert shot["href"].startswith(f"/api/imx/applications/{app_id}/evidence/ev_")
    path = shot["href"].removeprefix("/api/imx")
    png = harness.client.get(path)
    assert png.status == 200
    assert png.headers["content-type"] == "image/png"
    assert png.headers["content-disposition"].startswith("inline")
    assert png.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in png.headers["content-security-policy"]
    assert png.body == b"\x89PNG fictional"

    page = next(e for e in view["receipt"]["evidence"] if e["label"] == "Saved copy of the page")
    html = harness.client.get(page["href"].removeprefix("/api/imx"))
    assert html.headers["content-disposition"].startswith("attachment;")
    assert html.headers["content-type"].startswith("text/html")

    ev_id = path.rsplit("/", 1)[1]
    assert harness.client.get(f"/applications/{app_id}/evidence/ev_missing").status == 404
    assert harness.client.get(f"/applications/{app_id}/evidence/..%2Fx").status == 400
    with ApplicationStore.open(harness.paths.state_db) as store:
        other = store.record_request("default", "https://jobs.example.test/x/2").application
    assert harness.client.get(f"/applications/{other.id}/evidence/{ev_id}").status == 404


def test_evidence_symlink_escape_and_tampering(harness: Harness, tmp_path: Any) -> None:
    app_id = _submitted(harness)
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    app_dir = harness.paths.application_artifacts(app_id)
    os.symlink(secret, app_dir / "link.txt")
    (app_dir / "note.txt").write_text("original")
    # Rows written behind the store's back: a symlink escaping the artifacts dir and a
    # file whose digest no longer matches.
    conn = sqlite3.connect(harness.paths.state_db)
    try:
        for ev in (
            EvidenceRef(id="ev_link", kind=EvidenceKind.PAGE_TEXT, path=f"{app_id}/link.txt"),
            EvidenceRef(id="ev_note", kind=EvidenceKind.PAGE_TEXT, path=f"{app_id}/note.txt",
                        sha256="0" * 64),
        ):
            conn.execute(
                "INSERT INTO evidence (id, application_id, attempt_id, captured_at, body)"
                " VALUES (?, ?, NULL, ?, ?)",
                (ev.id, app_id, "2026-09-22T20:00:00.000000Z", ev.model_dump_json()),
            )
        conn.commit()
    finally:
        conn.close()
    assert harness.client.get(f"/applications/{app_id}/evidence/ev_link").status == 404
    assert harness.client.get(f"/applications/{app_id}/evidence/ev_note").status == 409


def test_test_only_mode_refuses_non_loopback_applications(harness: Harness) -> None:
    assert harness.client.get("/healthz").json["applicationMode"] == "TEST_ONLY"
    rid = harness.setup_candidate()
    r = harness.client.post("/applications", {
        "applicationUrl": "https://careers.example.com/jobs/1/apply",
        "profile": harness.profile(), "resumeId": rid,
    })
    assert r.status == 422
    assert "TEST_ONLY" in r.json["error"]["fieldErrors"]["applicationUrl"]
    with harness.store() as store:
        assert store.list_applications() == []  # nothing recorded
    # An application recorded elsewhere for a real site cannot be driven from here.
    with ApplicationStore.open(harness.paths.state_db) as store:
        app = store.record_request("default", "https://careers.example.com/jobs/2/apply").application
    assert harness.client.post(f"/applications/{app.id}/resume", {}).status == 403
    assert harness.runs == []


def test_selected_resume_is_pinned_per_application(harness: Harness) -> None:
    first = harness.setup_candidate()
    a = harness.start(resume_id=first).json["id"]
    harness.wait_idle()
    second = harness.client.upload("Second.pdf", b"%PDF-1.4 second fictional resume").json["id"]
    b = harness.client.post("/applications", {
        "applicationUrl": "http://127.0.0.1:9/fictional-co/8888/apply",
        "profile": harness.profile(), "resumeId": second,
    }).json["id"]
    harness.wait_idle()
    # Asking again for A with the other resume keeps A's original pin.
    again = harness.start(resume_id=second)
    assert again.json["id"] == a
    harness.wait_idle()
    with harness.store() as store:
        pin_a, pin_b = store.pinned_resume(a), store.pinned_resume(b)
    assert pin_a is not None and pin_a.id == first
    assert pin_b is not None and pin_b.id == second
    assert harness.client.get(f"/applications/{a}").json["resumeFileName"] == pin_a.filename
