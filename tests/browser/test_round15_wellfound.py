"""Round 15: Wellfound's apply button and note dialog.

Live (read-only, 2026-09-25, observed by the lead): OpenCLI's click on Wellfound's "Apply" and
"Apply now" changes nothing, while ``element.click()`` opens the application: a dialog with one
note textarea and "Send application". Here:

1. an apply control whose click left the page as it was is clicked once more by script
   (``dom_click``, OpenCLI's one writing script), and the message says so;
2. the note dialog is the application form (one question, a submit control that sends an
   application, application wording in its text);
3. the note ("Write a note to <recruiter> at <Company>.") is a cover-letter textarea.

The pages are fictional and local (``file://``); the bridge's click is a synthetic mouse click
that the page ignores, as Wellfound's press handling does; nothing is submitted.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import async_playwright

from interviewmaxxing_browser import (
    CommandResult,
    OpenCliConfig,
    OpenCliSessionFactory,
    PlaywrightSessionFactory,
)
from interviewmaxxing_core import BrowserOptions, ControlType, PageKind, SemanticType

NOTE = "userNote"
SETTLE_S = 4.0
"""Short settle: an unchanged click is known only once the settle timeout has passed."""

PRESS_JS = """
  // A press handler like react-aria's usePress: a pointer press (pointerdown then pointerup)
  // or a click() (detail 0) opens; a bare synthetic mouse click (detail 1, no pointer events)
  // is ignored. ?press=any opens on any click; ?press=none never opens.
  const mode = new URLSearchParams(location.search).get('press') || 'aria';
  window.__opened = 0; window.__sent = false;
  function open() {
    if (document.getElementById('modal')) return;
    window.__opened++;
    const modal = document.createElement('div');
    modal.id = 'modal'; modal.setAttribute('role', 'dialog'); modal.tabIndex = -1;
    modal.setAttribute('data-test', 'JobApplicationModal');
    modal.innerHTML = '<p>Indicate how you can stand out as a candidate in the note below to improve your odds.</p>' +
      '<form id="note-form"><textarea id="form-input--userNote" name="userNote" rows="6" ' +
      'placeholder="Write a note to Jordan at Brambleway Analytics."></textarea>' +
      '<button type="submit" data-test="JobApplicationModal--SubmitButton">Send application</button></form>';
    modal.querySelector('form').addEventListener('submit', (e) => { e.preventDefault(); window.__sent = true; });
    document.body.appendChild(modal);
  }
  for (const button of document.querySelectorAll('button.apply')) {
    let pressed = false;
    button.addEventListener('pointerdown', () => { pressed = true; });
    button.addEventListener('pointerup', () => { if (pressed && mode !== 'none') open(); pressed = false; });
    button.addEventListener('click', (e) => {
      if (mode === 'any' || (mode === 'aria' && e.detail === 0)) open();
    });
  }
"""

POSTING = f"""<!doctype html><html lang="en"><head><title>Growth Marketing Manager at Brambleway Analytics</title></head>
<body><main>
<h1>Growth Marketing Manager</h1>
<section class="card"><p>Brambleway Analytics · Remote (United States)</p>
<button type="button" class="apply" data-test="Button">Apply</button></section>
<section><h2>About the role</h2><p>Own paid acquisition and lifecycle programs for a fictional analytics company.</p>
<p>You will plan budgets, run experiments and report results.</p></section>
<button type="button" class="apply" data-test="Button">Apply now</button>
</main><script>{PRESS_JS}</script></body></html>"""


@pytest.fixture
def posting(tmp_path: Path) -> str:
    page = tmp_path / "wellfound-posting.html"
    page.write_text(POSTING, encoding="utf-8")
    return page.as_uri()


async def _bridge_like(browser: Any) -> list[str]:
    """Give the Playwright driver OpenCLI's two click paths: ``click`` as a synthetic mouse
    click (what the page ignores) and ``dom_click`` as ``element.click()``. Returns the log of
    DOM clicks."""
    page = browser.page
    dom_clicks: list[str] = []

    async def click(selector: str, *, trial: bool = False) -> None:
        if not trial:
            await page.evaluate("(s) => document.querySelector(s).dispatchEvent("
                                "new MouseEvent('click', {bubbles: true, cancelable: true, detail: 1}))", selector)

    async def dom_click(selector: str) -> None:
        dom_clicks.append(selector)
        await page.evaluate("(s) => document.querySelector(s).click()", selector)

    browser.driver.click = click
    browser.driver.dom_click = dom_click
    return dom_clicks


def test_an_apply_click_that_changed_nothing_is_clicked_once_by_script(
    kit: Any, posting: str, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, list[str], dict[str, Any]]:
        browser = await PlaywrightSessionFactory(settle_timeout_s=SETTLE_S).start(options)
        try:
            dom_clicks = await _bridge_like(browser)
            page = await browser.open(posting)
            state = await browser.page.evaluate("() => ({opened: window.__opened, sent: window.__sent})")
            return page, dom_clicks, state
        finally:
            await browser.close()

    page, dom_clicks, state = kit.run(scenario())
    assert page.kind is PageKind.APPLICATION_FORM and page.form is not None, page.message
    assert "clicked 'Apply' (DOM click)" in (page.message or ""), page.message
    assert len(dom_clicks) == 1 and state == {"opened": 1, "sent": False}
    # 2. and 3.: the note dialog is the form; its one question is a cover-letter textarea.
    (note,) = page.form.fields
    assert (note.id, note.control_type, note.semantic_type) == (NOTE, ControlType.TEXTAREA, SemanticType.COVER_LETTER)
    assert page.form.is_final_step is True and page.form.submit_selector


def test_a_click_the_page_answers_is_never_repeated_by_script(
    kit: Any, posting: str, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, list[str]]:
        browser = await PlaywrightSessionFactory(settle_timeout_s=SETTLE_S).start(options)
        try:
            dom_clicks = await _bridge_like(browser)
            return await browser.open(posting + "?press=any"), dom_clicks
        finally:
            await browser.close()

    page, dom_clicks = kit.run(scenario())
    assert page.kind is PageKind.APPLICATION_FORM, page.message
    assert dom_clicks == [] and "(DOM click)" not in (page.message or "")


def test_playwright_clicks_as_before_and_has_no_dom_click(kit: Any, posting: str, options: BrowserOptions) -> None:
    async def scenario() -> tuple[Any, bool]:
        browser = await PlaywrightSessionFactory(settle_timeout_s=SETTLE_S).start(options)
        try:
            return await browser.open(posting), hasattr(browser.driver, "dom_click")
        finally:
            await browser.close()

    page, has_dom_click = kit.run(scenario())
    assert not has_dom_click
    assert page.kind is PageKind.APPLICATION_FORM and "clicked 'Apply'" in (page.message or "")
    assert "(DOM click)" not in (page.message or "")


def test_each_apply_control_gets_at_most_one_dom_click(kit: Any, posting: str, options: BrowserOptions) -> None:
    async def scenario() -> tuple[Any, list[str], int]:
        browser = await PlaywrightSessionFactory(settle_timeout_s=SETTLE_S).start(options)
        try:
            dom_clicks = await _bridge_like(browser)
            page = await browser.open(posting + "?press=none")
            return page, dom_clicks, await browser.page.evaluate("() => window.__opened")
        finally:
            await browser.close()

    page, dom_clicks, opened = kit.run(scenario())
    # Nothing opens the application: each control was clicked once and once by script.
    assert page.kind is PageKind.JOB_DESCRIPTION and opened == 0
    assert len(dom_clicks) == 2 and len(set(dom_clicks)) == 2
    assert "clicked 'Apply' (DOM click); then clicked 'Apply now' (DOM click)" in (page.message or "")


DIALOG_PAGE = """<!doctype html><html lang="en"><head><title>Fictional page</title></head><body>
<h1>Fictional page</h1><div role="dialog" tabindex="-1">{text}<form>
<textarea name="note" rows="4"></textarea><button type="submit">{button}</button></form></div></body></html>"""


@pytest.mark.parametrize(("text", "button", "application"), [
    ("<p>Tell us about yourself.</p>", "Send application", True),  # a submit that sends an application
    ("<p>Stand out as a candidate with a short note.</p>", "Submit", True),  # application wording in its text
    ("<p>Tell us about yourself.</p>", "Submit", False),  # one question, a plain submit: not an application
], ids=["sends-application", "candidate-wording", "plain"])
def test_a_one_question_dialog_is_the_application_by_its_submit_or_its_text(
    text: str, button: str, application: bool, kit: Any, tmp_path: Path, options: BrowserOptions
) -> None:
    page_file = tmp_path / "dialog.html"
    page_file.write_text(DIALOG_PAGE.format(text=text, button=button), encoding="utf-8")

    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory(settle_timeout_s=SETTLE_S).start(options)
        try:
            return await browser.open(page_file.as_uri())
        finally:
            await browser.close()

    page = kit.run(scenario())
    assert (page.kind is PageKind.APPLICATION_FORM) is application, (page.kind, page.message)


# --- the same through the OpenCLI driver, over a bridge whose click the page ignores -------------

class SyntheticClickBridge:
    """Answers the ``opencli browser`` commands ``open`` needs on one real Chromium page:
    ``eval`` runs the script, and ``click`` is a synthetic mouse click on the one element the
    selector matches (the page ignores it, as Wellfound ignores Browser Bridge's click)."""

    TAB = "BRIDGE00TAB1"

    def __init__(self, page: Any) -> None:
        self.page = page
        self.calls: list[list[str]] = []

    @staticmethod
    def ok(payload: Any) -> CommandResult:
        return CommandResult(0, json.dumps(payload) + "\n", "")

    def evals(self) -> list[str]:
        return [argv[argv.index("--") + 1] for argv in self.calls
                if argv[argv.index("browser") + 2] == "eval"]

    async def __call__(self, argv: Sequence[str], timeout_s: float) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        i = argv.index("browser")
        command = argv[i + 2]
        positionals = argv[argv.index("--") + 1:] if "--" in argv else []
        if command == "tab":
            return self.ok({"page": self.TAB} if argv[i + 3] == "new" else [{"page": self.TAB}])
        if command == "close":
            return self.ok({"closed": True})
        if command == "open":
            await self.page.goto(positionals[0], wait_until="load")
            return self.ok({"url": self.page.url, "page": self.TAB})
        if command == "eval":
            value = await self.page.evaluate(positionals[0])
            return CommandResult(0, (value if isinstance(value, str) else json.dumps(value)) + "\n", "")
        if command == "screenshot":
            await self.page.screenshot(path=positionals[0])
            return self.ok({"path": positionals[0]})
        if command == "click":
            await self.page.evaluate("(s) => document.querySelector(s).dispatchEvent("
                                     "new MouseEvent('click', {bubbles: true, cancelable: true, detail: 1}))",
                                     positionals[0])
            return self.ok({"clicked": True, "matches_n": 1, "match_level": "exact", "target": positionals[0]})
        return CommandResult(1, json.dumps({"error": {"code": "unknown_command", "message": command}}), "")


def test_opencli_clicks_an_unanswered_apply_control_once_by_script(
    kit: Any, posting: str, options: BrowserOptions
) -> None:
    config = OpenCliConfig(profile="fixture-profile", navigation_grace_s=0.05, poll_interval_s=0.01)

    async def scenario() -> tuple[Any, list[str], dict[str, Any]]:
        async with async_playwright() as playwright:
            chromium = await playwright.chromium.launch(headless=True)
            try:
                bridge = SyntheticClickBridge(await chromium.new_page())
                browser = await OpenCliSessionFactory(config, runner=bridge, settle_timeout_s=SETTLE_S).start(options)
                page = await browser.open(posting)
                state = await bridge.page.evaluate("() => ({opened: window.__opened, sent: window.__sent})")
                await browser.close()
                return page, bridge.evals(), state
            finally:
                await chromium.close()

    page, evals, state = kit.run(scenario())
    assert page.kind is PageKind.APPLICATION_FORM and page.form is not None, page.message
    assert "clicked 'Apply' (DOM click)" in (page.message or "")
    assert state == {"opened": 1, "sent": False}
    # One writing script ran once: the fixed click() on the apply control.
    writes = [e for e in evals if "el.click()" in e]
    assert len(writes) == 1 and "Apply" not in writes[0]  # the element by its selector, never by text
    (note,) = page.form.fields
    assert (note.id, note.semantic_type) == (NOTE, SemanticType.COVER_LETTER)
