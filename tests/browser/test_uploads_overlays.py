"""Third-party autofill surfaces on the localhost mock ATS (fictional employer, fictional
data), on the Lever-like ``linkedin-autofill`` form: an "Apply with LinkedIn" button
that reads "Loading…" (``aria-busy``) for a while under a transparent overlay, and an
"Autofill your application?" dialog that makes the page inert, shown on load or right
after the resume is attached.

The runtime waits the loading button out, never clicks the button, its overlay or the
dialog's accept button, dismisses the dialog exactly once with "No thanks", keeps
``submit_selector`` on the real "Submit application" button and fills the candidate's
own values, resume first. Real headless Chromium; nothing is ever submitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_core import (
    BrowserOptions,
    FieldFillStatus,
    FillResult,
    PageInspection,
    PageKind,
)

LINKEDIN = "/jobs/linkedin-autofill/apply"
IDS = ["resume", "name", "email", "phone", "location", "urls[LinkedIn]"]
OURS = {
    "name": "Avery Quill", "email": "avery.quill@example.test", "phone": "+1 (303) 555-0142",
    "urls[LinkedIn]": "https://www.linkedin.example.test/in/avery-quill",
}
"""The candidate's own values by field name; the LinkedIn button and the dialog's accept
button would write "LinkedIn Member" values instead."""
RESUME_FILE = {"name": "resume_avery_quill.pdf", "size": 802}
NEVER_CLICKED = {"overlayClicks": 0, "linkedinClicks": 0, "promptAccepted": 0}
PROMPT = ("promptShown", "promptDismissed", "promptAccepted")
STATE = """(names) => {
  const main = document.querySelector('main');
  const button = document.querySelector('#linkedin-apply');
  return {
    mock: JSON.parse(JSON.stringify({...window.__mock, files: undefined})),
    button: [button.textContent.trim(), button.getAttribute('aria-busy')],
    prompt: document.querySelector('#autofill-prompt') !== null,
    blocked: main.hasAttribute('inert') || main.getAttribute('aria-hidden') === 'true',
    values: Object.fromEntries(names.map((name) => {
      const control = document.querySelector(`form [name="${name}"]`);
      return [name, control ? control.value : null];
    })),
    files: Array.from(document.querySelector('#f-resume').files)
      .map((f) => ({name: f.name, size: f.size})),
  };
}"""
TARGETS = """(selector) => Array.from(document.querySelectorAll(selector))
  .map((el) => [el.textContent.trim(), el.getAttribute('type')])"""


@dataclass
class Run:
    page: PageInspection
    opened: dict[str, Any]
    """Page state right after ``open()`` returned."""
    targets: list[Any]
    """What ``form.submit_selector`` matches: ``[text, type]`` per element."""
    fill: FillResult
    after: dict[str, Any]
    """Page state right after ``fill()`` returned."""

    @property
    def events(self) -> list[str]:
        return [entry["event"] for entry in self.after["mock"]["log"]]


def _at(events: list[str], event: str) -> int:
    """Position of the first ``event`` in the page's log (which must contain it)."""
    assert event in events, (event, events)
    return events.index(event)


def _first_input(events: list[str]) -> int:
    """Position of the first trusted input event on any form control."""
    inputs = [i for i, event in enumerate(events) if event.startswith("input:")]
    assert inputs, events
    return inputs[0]


async def _open_and_fill(kit: SimpleNamespace, options: BrowserOptions, url: str) -> Run:
    identity = kit.CANDIDATE["identity"]
    answers = {
        "resume": kit.RESUME, "name": f"{identity['first_name']} {identity['last_name']}",
        "email": identity["email"], "phone": identity["phone"],
        "urls[LinkedIn]": identity["linkedin_url"],
    }
    browser = await PlaywrightSessionFactory().start(options)
    try:
        page = await browser.open(url)
        opened = await browser.page.evaluate(STATE, list(OURS))
        assert page.form is not None, page.message
        targets = await browser.page.evaluate(TARGETS, page.form.submit_selector)
        fill = await browser.fill(page.form, kit.build(page.form, answers).packet)
        return Run(page, opened, targets, fill, await browser.page.evaluate(STATE, list(OURS)))
    finally:
        await browser.close()


def _assert_filled_with_ours(run: Run) -> None:
    assert run.page.kind is PageKind.APPLICATION_FORM, run.page.message
    assert [f.id for f in run.page.form.fields] == IDS
    assert run.fill.ok, [f for f in run.fill.fields if f.status is not FieldFillStatus.FILLED]
    statuses = {f.field_id: f.status for f in run.fill.fields}
    answered = ["resume", *OURS]
    assert {k: statuses.get(k) for k in answered} == dict.fromkeys(answered, FieldFillStatus.FILLED)
    assert run.after["values"] == OURS
    assert run.after["files"] == [RESUME_FILE]
    assert run.after["mock"]["uploads"] == 1
    assert {k: run.after["mock"][k] for k in NEVER_CLICKED} == NEVER_CLICKED


def test_the_loading_linkedin_button_is_waited_out_and_never_clicked(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    # The default: the button reads "Loading…" (aria-busy) for 1.5 s after load. No dialog.
    run = kit.run(_open_and_fill(kit, options, server.url(LINKEDIN + "?prompt=none")))
    _assert_filled_with_ours(run)
    # open() returned only once the button was ready.
    assert "linkedin-ready" in [entry["event"] for entry in run.opened["mock"]["log"]]
    assert run.opened["button"] == ["Apply with LinkedIn", None]
    # Neither the LinkedIn button nor its overlay is the form's action.
    form = run.page.form
    assert form.is_final_step is True and form.next_selector is None
    assert run.targets == [["Submit application", "submit"]]
    events = run.events
    assert _at(events, "linkedin-ready") < _first_input(events), events
    assert run.after["mock"]["promptShown"] == 0
    assert server.submissions("linkedin-autofill")["accepted_count"] == 0


def test_an_autofill_dialog_shown_on_load_is_dismissed_once_before_anything_is_filled(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    # The dialog appears 400 ms after load, while the button still reads "Loading…" (700 ms).
    run = kit.run(_open_and_fill(kit, options, server.url(LINKEDIN + "?loading_ms=700&prompt=load")))
    _assert_filled_with_ours(run)
    # open() dismissed it ("No thanks") and inspected the page the dialog had made inert.
    assert [run.opened["mock"][k] for k in PROMPT] == [1, 1, 0]
    assert not run.opened["prompt"] and not run.opened["blocked"]
    assert [run.after["mock"][k] for k in PROMPT] == [1, 1, 0]  # and it never came back
    events = run.events
    assert _at(events, "prompt-shown") < _at(events, "prompt-dismissed") < _first_input(events), events
    assert server.submissions("linkedin-autofill")["accepted_count"] == 0


def test_an_autofill_dialog_shown_after_the_upload_is_dismissed_once(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    # The dialog appears 300 ms after the resume input's change.
    run = kit.run(_open_and_fill(kit, options, server.url(LINKEDIN + "?loading_ms=300&prompt=upload")))
    _assert_filled_with_ours(run)
    assert run.opened["mock"]["promptShown"] == 0  # nothing was attached yet
    assert [run.after["mock"][k] for k in PROMPT] == [1, 1, 0]
    assert not run.after["prompt"] and not run.after["blocked"]
    events = run.events
    assert _at(events, "upload") < _at(events, "prompt-shown") < _at(events, "prompt-dismissed"), events
    assert server.submissions("linkedin-autofill")["accepted_count"] == 0
