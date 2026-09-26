"""A site that refuses a dispatched submit, as Ashby does with its "flagged as possible
spam" banner, is a definite non-submission (NOT_SUBMITTED, FAILED_RETRYABLE next), never
an uncertain outcome. Real headless Chromium against a fictional Ashby-like form served
on a loopback port by this test; nothing leaves the machine."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_browser.runtime import REFUSAL_SIGNAL
from interviewmaxxing_core import BrowserOptions, NotSubmittedNext, SubmissionOutcome

FORM = """<!doctype html><html lang="en"><head><title>Decision Scientist @ Fictional Co</title></head>
<body><main><h1>Decision Scientist</h1>{banner}
<form method="post" action="/apply">
<label for="first_name">First name</label><input id="first_name" name="first_name" required>
<label for="email">Email</label><input id="email" name="email" type="email" required>
<button type="submit">Submit Application</button></form></main></body></html>"""
BANNER = ("<div role=\"alert\"><p>We couldn\u2019t submit your application.</p><p>Your application "
          "submission was flagged as possible spam. If you believe this was a mistake, please "
          "submit your application again.</p><p>Try these steps:</p><ul><li>Turn off your VPN</li>"
          "<li>Turn off browser extensions</li><li>Try another browser</li>"
          "<li>Try another network</li></ul></div>")


class _Site(BaseHTTPRequestHandler):
    refuse_on_new_page = False

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._send(FORM.format(banner=""))

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if type(self).refuse_on_new_page:
            self._send(f"<!doctype html><html lang=\"en\"><head><title>Application</title></head>"
                       f"<body><main><h1>Application</h1>{BANNER}</main></body></html>")
        else:
            self._send(FORM.format(banner=BANNER))


@pytest.fixture
def site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/apply"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("new_page", [False, True])
def test_a_refused_submit_is_not_submitted_and_retryable(
    kit: SimpleNamespace, site: str, options: BrowserOptions, new_page: bool
) -> None:
    _Site.refuse_on_new_page = new_page

    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory().start(replace(options, allow_submission=True))
        try:
            page = await browser.open(site)
            form = page.form
            assert form is not None and form.is_final_step is True, page
            filled = await browser.fill(form, kit.build(form, {
                "first_name": "Avery", "email": "avery.quill@example.test"}).packet)
            assert filled.ok, filled
            assert (await browser.submit()).dispatched
            return await browser.confirm()
        finally:
            await browser.close()

    observation = kit.run(scenario())
    assert observation.outcome is SubmissionOutcome.NOT_SUBMITTED, observation
    assert observation.next_state is NotSubmittedNext.FAILED_RETRYABLE
    [signal] = observation.signals
    assert signal.startswith(REFUSAL_SIGNAL) and "flagged as possible spam" in signal
    assert "couldn\u2019t submit your application" in (observation.detail or "")
