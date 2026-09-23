"""Opt-in live smoke: the OpenCLI driver in the user's connected Chrome profile.

Runs only with ``IMX_OPENCLI_LIVE=1``. It uses one owned background tab in session
``imx-application-<random suffix>`` (prefix: ``IMX_OPENCLI_SESSION``) on profile ``IMX_OPENCLI_PROFILE``
(default ``jgd7jms9``), and only localhost servers this test starts itself: the
fictional mock ATS and a tiny fictional form server. It never binds or touches other
tabs; ``IMX_OPENCLI_PROTECTED_TABS`` (comma-separated) adds tab ids it must refuse.

    IMX_OPENCLI_LIVE=1 .venv-task/bin/python -m pytest -s tests/browser/test_opencli_live.py
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import pytest

from interviewmaxxing_browser import (
    OpenCliConfig,
    OpenCliSessionFactory,
    PlaywrightSessionFactory,
    SubmissionRefused,
)
from interviewmaxxing_core import BrowserOptions, FieldFillStatus, SubmissionOutcome

pytestmark = pytest.mark.skipif(
    os.environ.get("IMX_OPENCLI_LIVE") != "1", reason="live OpenCLI smoke is opt-in (IMX_OPENCLI_LIVE=1)"
)

CONFIG = OpenCliConfig(
    session=os.environ.get("IMX_OPENCLI_SESSION", "imx-application"),
    profile=os.environ.get("IMX_OPENCLI_PROFILE", "jgd7jms9"),
    window="background",
    protected_tabs=frozenset(t for t in os.environ.get("IMX_OPENCLI_PROTECTED_TABS", "").split(",") if t),
)


# --- a tiny fictional form server (no file field, so a full OpenCLI submit is possible) --

FORM = """<!doctype html><html><head><title>Apply: Opencli Tester</title></head><body>
<h1>Opencli Tester</h1><p>Fictional Co · Job ID OC-101</p>{alert}
<form method="post" action="{action}">
  <label for="email">Email</label><input id="email" name="email" type="email" required>
  <label for="auth">Are you legally authorized to work in the United States?</label>
  <select id="auth" name="auth" required><option value="">Select</option>
    <option value="ys">Yes</option><option value="nn">No</option></select>
  <fieldset><legend>Will you require visa sponsorship?</legend>
    <input type="radio" id="s1" name="sponsor" value="s_yes" required><label for="s1">Yes</label>
    <input type="radio" id="s2" name="sponsor" value="s_no" required><label for="s2">No</label></fieldset>
  <input type="checkbox" id="reloc" name="relocate" value="yes"><label for="reloc">I am open to relocating</label>
  <label for="why">Why do you want to work here?</label><textarea id="why" name="why" required></textarea>
  <button type="submit">Submit application</button>
</form></body></html>"""


class FormSite:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, list[str]]]] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, body: str, location: str | None = None) -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path == "/o1/form":
                    self._send(200, FORM.format(alert="", action="/o1/apply"))
                elif self.path == "/o1/form-uncertain":
                    self._send(200, FORM.format(alert="", action="/o1/apply-uncertain"))
                elif self.path.startswith("/o1/done/"):
                    n = self.path.rsplit("/", 1)[-1]
                    self._send(200, "<!doctype html><html><body><h1>Application submitted</h1>"
                               f"<p>Opencli Tester · Job ID OC-101</p><p>Reference: OC-REF-{n}</p></body></html>")
                else:
                    self._send(404, "<h1>Not found</h1>")

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
                site.posts.append((self.path, body))
                if self.path == "/o1/apply":
                    self._send(303, "", location=f"/o1/done/{len(site.posts)}")
                else:
                    alert = ('<div role="alert">The connection timed out. We cannot confirm whether '
                             "your application was submitted.</div>")
                    self._send(502, FORM.format(alert=alert, action="/o1/apply-uncertain"))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def form_site() -> Iterator[FormSite]:
    site = FormSite()
    try:
        yield site
    finally:
        site.stop()


ANSWERS = {
    "email": "avery.quill@example.test",
    "auth": "Yes",
    "sponsor": "No",
    "relocate": True,
    "why": "I build data platforms.",
}


def _opencli(kit: SimpleNamespace, options: BrowserOptions, steps: Any) -> Any:
    async def scenario() -> Any:
        browser = await OpenCliSessionFactory(CONFIG).start(options)
        try:
            return await steps(browser)
        finally:
            await browser.close()

    return kit.run(scenario())


def test_live_inspection_is_identical_to_playwright(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def playwright() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            return (await browser.open(server.url("/jobs/standard"))).form
        finally:
            await browser.close()

    async def steps(browser: Any) -> tuple[Any, str, str]:
        inspection = await browser.open(server.url("/jobs/standard"))  # follows the apply link
        return inspection, browser.driver.tab, browser.location

    reference = kit.run(playwright())
    inspection, tab, location = _opencli(kit, options, steps)
    print(f"\n[live] OpenCLI {location}")
    assert tab and tab not in CONFIG.protected_tabs
    assert inspection.observed_url == server.url("/jobs/standard/apply")
    assert inspection.job_identity is not None and inspection.job_identity.external_job_id == "BWA-ENG-101"
    assert [f.id for f in inspection.form.fields] == [f.id for f in reference.fields]
    assert inspection.form.fingerprint == reference.fingerprint


def test_live_accepted_submission_with_structured_commands(
    kit: SimpleNamespace, form_site: FormSite, options: BrowserOptions
) -> None:
    async def steps(browser: Any) -> tuple[Any, Any, Any]:
        form = (await browser.open(f"{form_site.origin}/o1/form")).form
        fill = await browser.fill(form, kit.build(form, ANSWERS).packet)
        action = await browser.submit()
        return fill, action, await browser.confirm()

    fill, action, observation = _opencli(kit, options, steps)
    assert fill.ok, [(r.field_id, r.status, r.detail) for r in fill.fields]
    assert action.dispatched
    assert observation.outcome is SubmissionOutcome.ACCEPTED, observation.signals
    assert observation.confirmation_reference == "OC-REF-1"
    ((path, body),) = form_site.posts
    assert path == "/o1/apply"
    assert body == {"email": ["avery.quill@example.test"], "auth": ["ys"], "sponsor": ["s_no"],
                    "relocate": ["yes"], "why": ["I build data platforms."]}


def test_live_uncertain_outcome_is_never_submitted_twice(
    kit: SimpleNamespace, form_site: FormSite, options: BrowserOptions
) -> None:
    async def steps(browser: Any) -> Any:
        form = (await browser.open(f"{form_site.origin}/o1/form-uncertain")).form
        await browser.fill(form, kit.build(form, ANSWERS).packet)
        assert (await browser.submit()).dispatched
        observation = await browser.confirm()
        with pytest.raises(SubmissionRefused):
            await browser.submit()
        return observation

    observation = _opencli(kit, options, steps)
    assert observation.outcome is SubmissionOutcome.UNKNOWN, observation.signals
    assert len(form_site.posts) == 1


def test_live_resume_upload_is_real_or_reported(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """Multipart resume upload through OpenCLI on the mock's uncertain job. Where Browser
    Bridge may not attach files, the field must fail with an actionable message and
    nothing may be submitted; where it can, the server must receive the exact file once
    and the uncertain outcome must never be retried."""

    async def steps(browser: Any) -> tuple[Any, Any, Any, bool]:
        form = (await browser.open(server.url("/jobs/uncertain/apply"))).form
        fill = await browser.fill(form, kit.build(form, kit.CORE).packet)
        action = await browser.submit()
        observation = await browser.confirm()
        refused = False
        if action.dispatched:
            try:
                await browser.submit()
            except SubmissionRefused:
                refused = True
        return fill, action, observation, refused

    fill, action, observation, refused = _opencli(kit, options, steps)
    resume = next(r for r in fill.fields if r.field_id == "resume")
    summary = server.submissions("uncertain")
    print(f"\n[live] resume upload: {resume.status.value} {resume.detail or ''}".rstrip())
    if resume.status is FieldFillStatus.FILLED:
        assert action.dispatched and observation.outcome is SubmissionOutcome.UNKNOWN and refused
        assert summary["accepted_count"] == 1
        uploaded = summary["submissions"][0]["files"]["resume"]
        assert uploaded["sha256"] == hashlib.sha256(kit.RESUME_PATH.read_bytes()).hexdigest()
    else:
        assert resume.status is FieldFillStatus.FAILED and "Attach" in (resume.detail or "")
        assert not action.dispatched and observation.outcome is SubmissionOutcome.NOT_SUBMITTED
        assert summary["accepted_count"] == 0 and summary["rejected_count"] == 0


def test_live_multiselect_limit_is_reported(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def steps(browser: Any) -> Any:
        form = (await browser.open(server.url("/jobs/standard/apply"))).form
        answers = {k: v for k, v in {**kit.CORE, **kit.STANDARD}.items() if k != "resume"}
        return await browser.fill(form, kit.build(form, answers).packet)

    fill = _opencli(kit, options, steps)
    results = {r.field_id: r for r in fill.fields}
    assert results["skills"].status is FieldFillStatus.FAILED
    assert "multi-select" in (results["skills"].detail or "")
    assert results["work_arrangements"].status is FieldFillStatus.FILLED  # checkbox groups work
    assert results["years_experience"].status is FieldFillStatus.FILLED
    assert not fill.ok
    assert server.submissions()["accepted_count"] == 0
