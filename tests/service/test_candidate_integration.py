"""The service over the real candidate package (C2P ``LocalCandidateStore``)."""

from __future__ import annotations

import json
import stat
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_core import CandidateProfile, LocalPaths

from .conftest import ORIGIN, SITE_URL, FictionalSite, Harness, serve

candidate = pytest.importorskip("interviewmaxxing_candidate")

from interviewmaxxing_service import ServiceConfig  # noqa: E402
from interviewmaxxing_service.integration import LocalCandidateGateway  # noqa: E402

PDF = b"%PDF-1.4\n% fictional resume for Avery Example\n%%EOF\n"


@pytest.fixture
def real(isolated_imx_home: LocalPaths, fictional_site: FictionalSite) -> Iterator[Harness]:
    config = ServiceConfig(paths=isolated_imx_home, allowed_origin=ORIGIN, port=0)
    with serve(isolated_imx_home, fictional_site, LocalCandidateGateway(config)) as h:
        yield h


def _wait_state(h: Harness, app_id: str, state: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while True:
        view = h.client.get(f"/applications/{app_id}").json
        if view["state"] == state:
            return view
        assert time.monotonic() < deadline, view["state"]
        time.sleep(0.05)


def _start(h: Harness, rid: str, **profile: str) -> Any:
    return h.client.post(
        "/applications",
        {"applicationUrl": SITE_URL, "profile": h.profile(**profile), "resumeId": rid},
    )


def test_upload_before_profile_then_setup_and_reload(real: Harness) -> None:
    before = real.client.get("/candidate").json
    assert before["resumes"] == [] and before["defaultResumeId"] is None
    up = real.client.upload("Résumé Avery.pdf", PDF)
    assert up.status == 201
    rid = up.json["id"]
    assert rid.startswith("resume_")
    assert up.json["fileName"].endswith(".pdf") and up.json["sizeBytes"] == len(PDF)

    stored = next((real.paths.profile_dir / "default" / "resumes" / rid).glob("*.pdf"))
    assert stored.read_bytes() == PDF
    assert stat.S_IMODE(stored.stat().st_mode) == 0o400
    assert stat.S_IMODE(stored.parent.stat().st_mode) == 0o700

    listed = real.client.get("/candidate").json
    assert [r["id"] for r in listed["resumes"]] == [rid]
    assert listed["profile"]["email"] == ""  # nothing invented before setup

    app = _start(real, rid)
    assert app.status == 201
    _wait_state(real, app.json["id"], "NEEDS_INPUT")
    after = real.client.get("/candidate").json
    assert after["defaultResumeId"] == rid
    assert after["profile"]["firstName"] == "Avery"
    assert after["profile"]["location"] == "Springfield, Oregon, USA"

    # A fresh service over the same files shows the same candidate and selection.
    fresh = LocalCandidateGateway(ServiceConfig(paths=real.paths, allowed_origin=ORIGIN))
    state = fresh.setup("default")
    assert state.selected_resume_id == rid and state.complete


def test_upload_rejections_come_from_the_candidate_store(real: Harness) -> None:
    not_pdf = real.client.upload("cv.pdf", b"this is not a pdf")
    assert not_pdf.status == 422
    assert not_pdf.json["error"]["fieldErrors"]["resumeFile"]
    wrong_type = real.client.upload("cv.exe", b"MZ")
    assert wrong_type.status == 422
    assert not (real.paths.profile_dir / "default" / "resumes").exists() or not any(
        (real.paths.profile_dir / "default" / "resumes").iterdir()
    )


def test_profile_update_keeps_facts_answers_and_history(
    real: Harness, core_fixture: Callable[[str], Any]
) -> None:
    # An imported profile with facts and embedded saved answers.
    data = core_fixture("candidate_profile.json")
    data["id"] = "default"
    directory = real.paths.profile_dir / "default"
    directory.mkdir(parents=True, mode=0o700)
    (directory / "profile.json").write_text(json.dumps(data))
    imported = real.client.get("/candidate").json
    original_resume = data["resume"]["id"]
    assert imported["defaultResumeId"] == original_resume
    assert imported["resumes"][-1]["id"] == original_resume

    app = _start(real, original_resume, phone="+1 555 0199")
    app_id = app.json["id"]
    _wait_state(real, app_id, "NEEDS_INPUT")
    raw = json.loads((directory / "profile.json").read_text())
    assert raw["facts"] == data["facts"]
    assert raw["saved_answers"] == data["saved_answers"]
    assert raw["experience"] == data["experience"]
    assert raw["resume"] == data["resume"]  # current resume kept exactly
    assert raw["identity"]["phone"] == "+1 555 0199"

    # Selecting a newly uploaded resume changes only the resume reference.
    rid = real.client.upload("new.pdf", PDF).json["id"]
    # Settle the running application first; one run at a time.
    real.wait_idle()
    other = real.client.post(
        "/applications",
        {"applicationUrl": "https://jobs.example.test/fictional-co/7777/apply",
         "profile": real.profile(phone="+1 555 0199"), "resumeId": rid},
    )
    assert other.status == 201
    real.wait_idle()
    raw2 = json.loads((directory / "profile.json").read_text())
    assert raw2["facts"] == data["facts"] and raw2["saved_answers"] == data["saved_answers"]
    assert raw2["resume"]["id"] == rid
    loaded = CandidateProfile.model_validate(
        candidate.LocalCandidateStore.from_paths(real.paths).load("default").model_dump()
    )
    assert loaded.id == "default"
    # The first application and its history are untouched.
    with real.store() as store:
        assert len(store.list_applications(candidate_id="default")) == 2
        assert store.list_events(app_id)


def test_reused_answer_is_saved_by_the_candidate_store(real: Harness) -> None:
    rid = real.client.upload("cv.pdf", PDF).json["id"]
    app_id = _start(real, rid).json["id"]
    view = _wait_state(real, app_id, "NEEDS_INPUT")
    why = next(q for q in view["needs"]["questions"] if q["control"] == "long_text")
    r = real.client.post(
        f"/applications/{app_id}/answers",
        {"answers": {why["id"]: "Fictional reason."}, "attestations": {},
         "reuse": {why["id"]: "job"}},
    )
    assert r.status == 200
    answers = json.loads((real.paths.profile_dir / "default" / "answers.json").read_text())
    assert len(answers) == 1
    assert answers[0]["scope"] == "JOB" and answers[0]["value"] == "Fictional reason."
    assert answers[0]["job_url"]


def test_invalid_existing_profile_is_not_overwritten(real: Harness) -> None:
    directory = real.paths.profile_dir / "default"
    directory.mkdir(parents=True, mode=0o700)
    # A fact without its required verification: the update itself cannot validate.
    broken = '{"id": "default", "identity": null, "resume": null, "facts": [{"id": "f1"}]}'
    (directory / "profile.json").write_text(broken)
    rid = real.client.upload("cv.pdf", PDF).json["id"]
    r = _start(real, rid)
    assert r.status == 409
    assert r.json["error"]["code"] == "conflict"
    assert (directory / "profile.json").read_text() == broken


def test_upload_bound_is_shared_with_the_candidate_store(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, tmp_path: Path
) -> None:
    from interviewmaxxing_service import ServiceConfig

    config = ServiceConfig(
        paths=isolated_imx_home, allowed_origin=ORIGIN, port=0, max_upload_bytes=64
    )
    with serve(
        isolated_imx_home, fictional_site, LocalCandidateGateway(config), max_upload_bytes=64
    ) as h:
        assert h.client.upload("cv.pdf", PDF + b"x" * 64).status == 413
        assert h.client.upload("cv.pdf", PDF).status == 201
