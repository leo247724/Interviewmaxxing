"""S1/S3 acceptance: HTTP -> service -> real I1 runner -> headless Chromium -> the
separately running localhost mock ATS (``scripts/mock_ats.py``).

Everything is fictional and local: a temporary ``IMX_HOME`` with the mock's fictional
candidate "Avery Quill", the real C2P candidate store, the real I1 runner and browser
package, and a mock ATS process on a loopback port. The user's real profile is never
read, and nothing leaves the machine. Assertions cover the HTTP views, the canonical
store and the mock's server-side submission records.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_core import ApplicationStore, LocalPaths
from interviewmaxxing_service import ServiceConfig
from interviewmaxxing_service.integration import (
    LocalCandidateGateway,
    runner_factory,
    runner_problem,
)

from .conftest import FictionalSite, Harness, serve

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[2]
MOCK_ATS = ROOT / "scripts" / "mock_ats.py"
RESUME = ROOT / "tests" / "fixtures" / "browser" / "resume_avery_quill.pdf"
SECOND_RESUME = b"%PDF-1.4\n% second fictional resume for Avery Quill\n%%EOF\n"
VERIFIED_AT = "2026-09-01T12:00:00Z"
STATES_DONE = {"SUBMITTED", "NEEDS_INPUT", "SUBMISSION_UNKNOWN", "FAILED_RETRYABLE",
               "FAILED_PERMANENT", "DUPLICATE"}


class MockAts:
    def __init__(self, state_dir: Path) -> None:
        ready = state_dir.parent / "mock-ready.json"
        self.proc = subprocess.Popen(
            [sys.executable, str(MOCK_ATS), "--state-dir", str(state_dir),
             "--ready-file", str(ready)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert self.proc.poll() is None and time.monotonic() < deadline, "mock ATS failed"
            time.sleep(0.05)
        self.origin: str = json.loads(ready.read_text())["origin"]

    def url(self, job: str) -> str:
        return f"{self.origin}/jobs/{job}/apply"

    def posting(self, job: str) -> str:
        return f"{self.origin}/jobs/{job}"

    def _call(self, path: str, method: str = "GET") -> Any:
        request = urllib.request.Request(self.origin + path, method=method,
                                         data=b"" if method == "POST" else None)
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def submissions(self, job: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call(f"/__test__/submissions?job_id={job}")
        return result

    def reveal(self, submission_id: str) -> None:
        self._call(f"/__test__/submissions/{submission_id}/reveal", "POST")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def _saved(answer_id: str, question: str, value: Any, semantic_type: str | None = None) -> dict[str, Any]:
    return {"id": answer_id, "scope": "GLOBAL", "semantic_type": semantic_type,
            "question": question, "value": value, "confirmed_at": VERIFIED_AT}


def write_fictional_profile(profile_dir: Path) -> None:
    """The mock ATS's fictional candidate with saved answers for the standard job's
    explicit questions (the user saved them earlier)."""
    directory = profile_dir / "default"
    directory.mkdir(parents=True)
    shutil.copyfile(RESUME, directory / "resume.pdf")
    profile = {
        "id": "default",
        "identity": {
            "first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
            "phone": "+1 (303) 555-0142",
            "linkedin_url": "https://www.linkedin.example.test/in/avery-quill",
            "address": {"city": "Denver", "region": "CO", "country": "United States"},
            "verified_at": VERIFIED_AT,
        },
        "resume": {"id": "resume_supplied", "path": "resume.pdf"},
        "facts": [{"id": "fact.years", "key": "years_professional_experience", "value": 7,
                   "source": "user",
                   "verification": {"status": "VERIFIED", "method": "USER_STATED",
                                    "verified_at": VERIFIED_AT}}],
        "saved_answers": [
            _saved("sa.work_auth", "Are you legally authorized to work in the United States?",
                   "Yes, I am authorized to work in the US", "WORK_AUTHORIZATION"),
            _saved("sa.sponsorship", "Will you now or in the future require visa sponsorship?",
                   "No, I will not require sponsorship", "SPONSORSHIP"),
            _saved("sa.years", "Years of professional experience", "6 to 9 years"),
            _saved("sa.skills", "Primary skills\nHold Ctrl or Command to select more than one.",
                   ["Python", "SQL", "Apache Spark", "dbt"]),
            _saved("sa.why", "Why do you want to work at Brambleway Analytics?",
                   "I have built data platforms for seven years and want to work on "
                   "logistics forecasting."),
        ],
    }
    (directory / "profile.json").write_text(json.dumps(profile, indent=2))


PROFILE = {
    "firstName": "Avery", "lastName": "Quill", "email": "avery.quill@example.test",
    "phone": "+1 (303) 555-0142", "location": "Denver, CO, United States",
    "linkedinUrl": "https://www.linkedin.example.test/in/avery-quill", "websiteUrl": "",
}


@pytest.fixture
def ats(tmp_path: Path) -> Iterator[MockAts]:
    server = MockAts(tmp_path / "mock-state")
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def home(isolated_imx_home: LocalPaths) -> LocalPaths:
    write_fictional_profile(isolated_imx_home.profile_dir)
    return isolated_imx_home


def real_service(paths: LocalPaths, site: FictionalSite) -> Any:
    config = ServiceConfig(paths=paths, allowed_origin="http://127.0.0.1:4317", port=0)
    return serve(
        paths, site, LocalCandidateGateway(config),
        executor_factory=lambda c: runner_factory(
            ServiceConfig(paths=c.paths, allowed_origin=c.allowed_origin, headless=True)
        ),
        runner_problem=runner_problem, reconcile_wait_s=25.0,
    )


def wait_for(h: Harness, app_id: str, states: set[str], timeout: float = 120.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        view = h.client.get(f"/applications/{app_id}").json
        if view["state"] in states and h.service.dispatcher.status(app_id) is None:
            return dict(view)
        assert time.monotonic() < deadline, f"stuck in {view['state']}: {view['events'][-3:]}"
        time.sleep(0.2)


def start(h: Harness, url: str, resume_id: str = "resume_supplied") -> Any:
    return h.client.post(
        "/applications", {"applicationUrl": url, "profile": PROFILE, "resumeId": resume_id}
    )


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_standard_application_submits_once_with_receipt_and_upload(
    home: LocalPaths, ats: MockAts, fictional_site: FictionalSite
) -> None:
    with real_service(home, fictional_site) as h:
        assert h.client.get("/healthz").json["runner"] == "available"
        r = start(h, ats.url("standard"))
        assert r.status == 201, r.json
        app_id = r.json["id"]
        view = wait_for(h, app_id, STATES_DONE)
        assert view["state"] == "SUBMITTED", view["events"][-5:]
        server = ats.submissions("standard")
        assert server["accepted_count"] == 1
        [record] = server["submissions"]
        receipt = view["receipt"]
        assert receipt["confirmationReference"] == record["confirmation_reference"]
        assert receipt["confirmationMethod"] == "SUBMISSION_OBSERVED"
        assert receipt["confirmationAuthority"] == "site"
        assert record["files"]["resume"]["sha256"] == sha(RESUME.read_bytes())
        assert (record["fields"]["first_name"], record["fields"]["last_name"]) == ("Avery", "Quill")
        assert view["job"]["title"] == "Senior Data Platform Engineer"

        # Asking again is the same application; nothing is sent twice.
        again = start(h, ats.url("standard"))
        assert again.status == 200 and again.json["id"] == app_id
        assert again.json["state"] == "SUBMITTED"
        assert ats.submissions("standard")["accepted_count"] == 1


def test_resume_pins_survive_profile_change_and_restart_with_missing_answers(
    home: LocalPaths, ats: MockAts, fictional_site: FictionalSite
) -> None:
    with real_service(home, fictional_site) as h:
        a = start(h, ats.url("standard")).json["id"]
        assert wait_for(h, a, STATES_DONE)["state"] == "SUBMITTED"
        # The user uploads another resume and applies to B with it.
        second = h.client.upload("Avery Quill second.pdf", SECOND_RESUME)
        assert second.status == 201, second.json
        b_resp = start(h, ats.url("missing-required"), resume_id=second.json["id"])
        assert b_resp.status == 201, b_resp.json
        b = b_resp.json["id"]
        view = wait_for(h, b, STATES_DONE)
        assert view["state"] == "NEEDS_INPUT"
        questions = {q["label"]: q for q in view["needs"]["questions"]}
        assert ats.submissions("missing-required")["accepted_count"] == 0

    # Process restart: a new service over the same fictional home.
    with real_service(home, fictional_site) as h:
        view = h.client.get(f"/applications/{b}").json
        assert {q["label"]: q["id"] for q in view["needs"]["questions"]} == {
            label: q["id"] for label, q in questions.items()
        }
        answers: dict[str, Any] = {}
        for q in view["needs"]["questions"]:
            if q["control"] == "single_select":
                options = {o["label"]: o["value"] for o in q["options"]}
                answers[q["id"]] = options.get("2 weeks") or options.get("No") \
                    or next(iter(options.values()))
                assert "3 months or more (no longer offered)" not in options
            else:
                answers[q["id"]] = "150000"
        saved = h.client.post(f"/applications/{b}/answers",
                              {"answers": answers, "attestations": {}})
        assert saved.status == 200 and saved.json["needs"]["errors"] == {}
        resumed = h.client.post(f"/applications/{b}/resume", {})
        assert resumed.status == 200, resumed.json
        view = wait_for(h, b, STATES_DONE)
        assert view["state"] == "SUBMITTED", view["events"][-5:]
        [record] = ats.submissions("missing-required")["submissions"]
        assert record["files"]["resume"]["sha256"] == sha(SECOND_RESUME)
        assert record["fields"]["salary_expectation"] == "150000"
        [record_a] = ats.submissions("standard")["submissions"]
        assert record_a["files"]["resume"]["sha256"] == sha(RESUME.read_bytes())
        with ApplicationStore.open(home.state_db) as store:
            pin_a, pin_b = store.pinned_resume(a), store.pinned_resume(b)
        assert pin_a is not None and pin_a.sha256 == sha(RESUME.read_bytes())
        assert pin_b is not None and pin_b.sha256 == sha(SECOND_RESUME)
        assert ats.submissions("standard")["accepted_count"] == 1


def test_uncertain_submission_is_locked_then_reconciled_from_the_site(
    home: LocalPaths, ats: MockAts, fictional_site: FictionalSite
) -> None:
    with real_service(home, fictional_site) as h:
        app_id = start(h, ats.posting("uncertain")).json["id"]
        view = wait_for(h, app_id, STATES_DONE)
        assert view["state"] == "SUBMISSION_UNKNOWN", view["events"][-5:]
        assert view["receipt"] is None and view["uncertain"]["reason"]
        assert ats.submissions("uncertain")["accepted_count"] == 1

        assert h.client.post(f"/applications/{app_id}/resume", {}).status == 409
        again = start(h, ats.posting("uncertain"))
        assert again.json["id"] == app_id and again.json["state"] == "SUBMISSION_UNKNOWN"
        assert ats.submissions("uncertain")["accepted_count"] == 1

        pending = h.client.post(f"/applications/{app_id}/reconcile", {"kind": "recheck"})
        assert pending.status == 200
        view = wait_for(h, app_id, STATES_DONE)
        assert view["state"] == "SUBMISSION_UNKNOWN"
        assert view["uncertain"]["lastCheckedAt"] is not None

        [record] = ats.submissions("uncertain")["submissions"]
        ats.reveal(record["submission_id"])
        h.client.post(f"/applications/{app_id}/reconcile", {"kind": "recheck"})
        view = wait_for(h, app_id, STATES_DONE)
        assert view["state"] == "SUBMITTED", view["events"][-5:]
        assert view["receipt"]["confirmationMethod"] == "SITE_CONFIRMATION"
        assert view["receipt"]["confirmationAuthority"] == "site"
        assert view["receipt"]["confirmationReference"] == record["confirmation_reference"]
        assert ats.submissions("uncertain")["accepted_count"] == 1
