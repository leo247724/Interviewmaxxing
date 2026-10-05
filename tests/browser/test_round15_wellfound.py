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
        if command in ("check", "uncheck"):
            target = self.page.locator("css:light=" + positionals[0])
            wanted = command == "check"
            before = await target.is_checked()
            if before != wanted:
                await target.dispatch_event("click")
            after = await target.is_checked()
            return self.ok({"checked": after, "changed": before != after, "matches_n": 1, "match_level": "exact",
                            "target": positionals[0]})
        if command == "fill":
            target = self.page.locator("css:light=" + positionals[0])
            await target.fill(positionals[1])
            actual = await target.input_value()
            return self.ok({"filled": True, "verified": actual == positionals[1], "text": positionals[1],
                            "actual": actual, "matches_n": 1, "match_level": "exact", "target": positionals[0]})
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


# --- the mock ATS: wellfound-modal, and the approved submit (interim 2) ---------------------------

WELLFOUND = "/jobs/wellfound-modal"
MOCK_STATE = "() => JSON.parse(JSON.stringify(window.__mock))"
LETTER = ("I rebuilt the tracking for a regional bakery chain's paid search so only paid orders "
          "counted, and online orders grew 35% that year.")
BRIDGE_CONFIG = OpenCliConfig(profile="fixture-profile", navigation_grace_s=0.05, poll_interval_s=0.01)


async def _over_bridge(options: BrowserOptions, url: str, work: Any) -> Any:
    """``work(browser, bridge)`` on an OpenCLI browser whose bridge click the page ignores."""
    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(headless=True)
        try:
            bridge = SyntheticClickBridge(await chromium.new_page())
            browser = await OpenCliSessionFactory(BRIDGE_CONFIG, runner=bridge,
                                                  settle_timeout_s=SETTLE_S).start(options)
            try:
                page = await browser.open(url)
                return await work(browser, bridge, page)
            finally:
                await browser.close()
        finally:
            await chromium.close()


def _script_clicks(bridge: SyntheticClickBridge) -> tuple[int, int]:
    """(apply script clicks, submit script clicks) the bridge ran: ``_DOM_CLICK`` refuses
    anything "inside a dialog", ``_DOM_SUBMIT`` anything that "is not a submit button"."""
    evals = bridge.evals()
    return (sum(1 for e in evals if "inside a dialog" in e), sum(1 for e in evals if "is not a submit button" in e))


def test_the_mock_opens_its_dialog_over_the_bridge_by_one_script_click(
    kit: Any, server: Any, options: BrowserOptions
) -> None:
    async def work(browser: Any, bridge: SyntheticClickBridge, page: Any) -> Any:
        return page, await bridge.page.evaluate(MOCK_STATE), _script_clicks(bridge)

    page, state, (apply_clicks, submit_clicks) = kit.run(_over_bridge(options, server.url(WELLFOUND), work))
    assert page.kind is PageKind.APPLICATION_FORM and "clicked 'Apply' (DOM click)" in (page.message or "")
    assert (state["opened"], state["ignored"], state["scriptClicks"]) == (1, 1, 1)
    assert (apply_clicks, submit_clicks) == (1, 0) and state["sent"] == 0
    assert "openedOther" not in state  # the "Similar Jobs" rail was never touched
    assert server.submissions("wellfound-modal")["accepted_count"] == 0


def test_the_similar_jobs_rail_is_never_clicked_even_when_this_job_has_no_apply(
    kit: Any, server: Any, options: BrowserOptions
) -> None:
    """This job was applied to before: its own Apply controls read "Applied" (disabled), and
    the rail's "Apply" buttons belong to other listings. None of them is ever clicked."""
    async def work(browser: Any, bridge: SyntheticClickBridge, page: Any) -> Any:
        return page, await bridge.page.evaluate(MOCK_STATE), _script_clicks(bridge), bridge.calls

    page, state, (apply_clicks, _), calls = kit.run(_over_bridge(options, server.url(WELLFOUND + "?applied=1"), work))
    assert page.kind is not PageKind.APPLICATION_FORM, page.message
    assert "openedOther" not in state and state["opened"] == 0 and apply_clicks == 0
    assert not [argv for argv in calls if argv[argv.index("browser") + 2] == "click"]


def test_an_ignored_click_that_asked_the_site_is_never_repeated_by_script(
    kit: Any, server: Any, options: BrowserOptions
) -> None:
    async def work(browser: Any, bridge: SyntheticClickBridge, page: Any) -> Any:
        return page, await bridge.page.evaluate(MOCK_STATE)

    page, state = kit.run(_over_bridge(options, server.url(WELLFOUND + "?click_request=1"), work))
    # Both apply clicks asked the site (a request the page shows nothing for): no script click.
    assert page.kind is PageKind.JOB_DESCRIPTION and "(DOM click)" not in (page.message or "")
    assert (state["opened"], state["scriptClicks"], state["ignored"]) == (0, 0, 2)


async def _fill_note(kit: Any, browser: Any, page: Any) -> None:
    assert page.form is not None
    result = await browser.fill(page.form, kit.build(page.form, {"userNote": LETTER}).packet)
    assert result.ok, result.fields


def test_the_approved_submit_clicks_send_application_once_by_script_and_reads_applied(
    kit: Any, server: Any, options: BrowserOptions
) -> None:
    async def work(browser: Any, bridge: SyntheticClickBridge, page: Any) -> Any:
        await _fill_note(kit, browser, page)
        sent = await browser.submit_approved()
        observed = await browser.confirm()
        return sent, observed, await bridge.page.evaluate(MOCK_STATE), _script_clicks(bridge)

    sent, observed, state, (_, submit_clicks) = kit.run(_over_bridge(options, server.url(WELLFOUND), work))
    assert sent.dispatched and "once by script (DOM click)" in sent.detail
    assert submit_clicks == 1 and (state["submitIgnored"], state["submitScriptClicks"], state["sent"]) == (0, 1, 1)
    assert observed.outcome.value == "ACCEPTED", observed
    # Live: the dialog stays open and says so; the job page's button reads "✓ Applied".
    assert any("YOUR APPLICATION HAS BEEN SENT" in signal for signal in observed.signals), observed.signals
    assert any("reads '\u2713 Applied'" in signal for signal in observed.signals), observed.signals
    summary = server.submissions("wellfound-modal")
    assert summary["accepted_count"] == 1 and summary["submissions"][0]["fields"]["userNote"] == LETTER


def test_a_submit_outside_an_approved_run_never_clicks_by_script(
    kit: Any, server: Any, options: BrowserOptions
) -> None:
    async def work(browser: Any, bridge: SyntheticClickBridge, page: Any) -> Any:
        await _fill_note(kit, browser, page)
        sent = await browser.submit()
        observed = await browser.confirm()
        return sent, observed, await bridge.page.evaluate(MOCK_STATE), _script_clicks(bridge)

    sent, observed, state, (_, submit_clicks) = kit.run(_over_bridge(options, server.url(WELLFOUND), work))
    assert sent.dispatched and "(DOM click)" not in sent.detail and submit_clicks == 0
    assert (state["submitIgnored"], state["sent"]) == (1, 0)
    assert observed.outcome.value == "UNKNOWN"  # the page did not answer; nothing is repeated
    assert server.submissions("wellfound-modal")["accepted_count"] == 0


def test_an_approved_submit_the_page_answers_has_only_one_dispatch(
    kit: Any, server: Any, options: BrowserOptions
) -> None:
    async def work(browser: Any, bridge: SyntheticClickBridge, page: Any) -> Any:
        await _fill_note(kit, browser, page)
        sent = await browser.submit_approved()
        observed = await browser.confirm()
        return sent, observed, await bridge.page.evaluate(MOCK_STATE), _script_clicks(bridge)

    sent, observed, state, (_, submit_clicks) = kit.run(
        _over_bridge(options, server.url(WELLFOUND + "?submit=any"), work))
    assert sent.dispatched and "once by script (DOM click)" in sent.detail and submit_clicks == 1
    assert state["submitIgnored"] == 0 and state["sent"] == 1 and observed.outcome.value == "ACCEPTED"
    assert server.submissions("wellfound-modal")["accepted_count"] == 1  # once, never twice





def test_an_approved_spa_submit_uses_one_structured_click(
    kit: Any, server: Any, options: BrowserOptions
) -> None:
    async def work(browser: Any, bridge: SyntheticClickBridge, page: Any) -> Any:
        await bridge.page.evaluate("() => document.querySelector("
                                   "'[data-test=JobApplicationModal--SubmitButton]').type = 'button'")
        page = await browser.inspect()
        await _fill_note(kit, browser, page)
        sent = await browser.submit_approved()
        observed = await browser.confirm()
        return sent, observed, await bridge.page.evaluate(MOCK_STATE), _script_clicks(bridge)

    sent, observed, state, (_, submit_clicks) = kit.run(
        _over_bridge(options, server.url(WELLFOUND + "?submit=any"), work))
    assert sent.dispatched and "DOM click" not in sent.detail and submit_clicks == 0
    assert observed.outcome.value == "ACCEPTED", observed
    assert state["sent"] == 1 and state["submitIgnored"] == 0
    assert server.submissions("wellfound-modal")["accepted_count"] == 1


def test_an_approved_dom_submit_refusal_is_not_dispatched(
    kit: Any, server: Any, options: BrowserOptions
) -> None:
    from interviewmaxxing_browser import NotActionable

    async def work(browser: Any, bridge: SyntheticClickBridge, page: Any) -> Any:
        await _fill_note(kit, browser, page)

        async def refuse(selector: str) -> None:
            raise NotActionable("disabled before DOM dispatch")

        browser.driver.dom_click_submit = refuse
        sent = await browser.submit_approved()
        return sent, await browser.confirm(), await bridge.page.evaluate(MOCK_STATE)

    sent, observed, state = kit.run(_over_bridge(options, server.url(WELLFOUND), work))
    assert not sent.dispatched and "refused" in sent.detail
    assert observed.outcome.value == "NOT_SUBMITTED"
    assert state["sent"] == 0 and state["submitIgnored"] == 0
    assert server.submissions("wellfound-modal")["accepted_count"] == 0


def test_an_inflight_approved_submit_is_never_dispatched_again(
    kit: Any, server: Any, options: BrowserOptions
) -> None:
    """An enabled, unchanged form and empty resource timing can hide a pending POST."""
    import asyncio

    from interviewmaxxing_browser import SubmissionRefused

    async def work(browser: Any, bridge: SyntheticClickBridge, page: Any) -> Any:
        await _fill_note(kit, browser, page)
        release = asyncio.Event()
        started = asyncio.Event()
        requests = []

        async def hold_submit(route: Any) -> None:
            if route.request.post_data_json.get("operationName") == "CreateJobApplication":
                requests.append(route.request)
                started.set()
                await release.wait()
            await route.continue_()

        await bridge.page.route("**/*graphql*", hold_submit)
        browser.settle_timeout_s = 0.1
        try:
            mark = await bridge.page.evaluate("() => performance.now()")
            sent = await browser.submit_approved()
            await asyncio.wait_for(started.wait(), timeout=2)
            # The request stays in flight longer than the runtime's settle window.
            await asyncio.sleep(0.2)
            assert await bridge.page.evaluate("(since) => performance.getEntriesByType('resource').filter("
                                             "e => e.initiatorType === 'fetch' && e.startTime >= since).length", mark) == 0
            observed = await browser.confirm()
            assert observed.outcome.value == "UNKNOWN", observed
            with pytest.raises(SubmissionRefused, match="already dispatched"):
                await browser.submit_approved()
            state = await bridge.page.evaluate(MOCK_STATE)
            assert sent.dispatched and len(requests) == 1
            assert state["sent"] == 1 and state["submitIgnored"] == 0
            assert _script_clicks(bridge)[1] == 1
        finally:
            release.set()
        await bridge.page.wait_for_function("window.__mock.sent === 1 && "
                                            "document.body.innerText.includes('YOUR APPLICATION HAS BEEN SENT')")
        return await browser.confirm()

    observed = kit.run(_over_bridge(options, server.url(WELLFOUND + "?submit=any"), work))
    assert observed.outcome.value == "ACCEPTED", observed
    assert server.submissions("wellfound-modal")["accepted_count"] == 1


def _write_profile(paths: Any) -> None:
    """The fictional candidate (identity and resume only): the note stays unanswered."""
    import shutil

    directory = paths.profile_dir / "default"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "fixtures" / "browser" / "resume_avery_quill.pdf",
                    directory / "resume.pdf")
    (directory / "profile.json").write_text(json.dumps({
        "id": "default",
        "identity": {"first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
                     "phone": "+1 (303) 555-0142", "verified_at": "2026-09-01T12:00:00Z"},
        "resume": {"id": "resume_supplied", "path": "resume.pdf"},
        "facts": [], "saved_answers": [],
    }, indent=2))


def test_preparation_never_touches_the_submit(
    kit: Any, server: Any, options: BrowserOptions, isolated_imx_home: Any
) -> None:
    from interviewmaxxing_cli.runner import LocalApplicationRunner, NoninteractiveInteraction
    from interviewmaxxing_core import ApplicationState

    _write_profile(isolated_imx_home)

    async def scenario() -> tuple[Any, list[str], dict[str, Any]]:
        async with async_playwright() as playwright:
            chromium = await playwright.chromium.launch(headless=True)
            try:
                bridge = SyntheticClickBridge(await chromium.new_page())
                factory = OpenCliSessionFactory(BRIDGE_CONFIG, runner=bridge, settle_timeout_s=SETTLE_S)
                runner = LocalApplicationRunner(paths=isolated_imx_home, interaction=NoninteractiveInteraction(),
                                                headless=True, browser_factory=factory, prepare_only=True)
                result = await runner.apply(server.url(WELLFOUND), candidate_id="default")
                return result, bridge.evals(), await bridge.page.evaluate(MOCK_STATE)
            finally:
                await chromium.close()

    result, evals, state = kit.run(scenario())
    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    assert "Prepared to the final review step" in result.message, result.message
    assert sum(1 for e in evals if "inside a dialog" in e) == 1  # the apply control, once
    assert not any("is not a submit button" in e for e in evals)  # never the submit
    assert (state["submitIgnored"], state["submitScriptClicks"], state["sent"]) == (0, 0, 0)
    assert server.submissions("wellfound-modal")["accepted_count"] == 0


# --- item 4: the runner prepares the note dialog with the note from a fixture writer -----------

POSTING_EVIDENCE = {"id": "job:" + "d" * 64, "source_url": "https://synthetic.test/jobs/wellfound",
                    "source_version": "e" * 64,
                    "text": ("Own paid search strategy for enterprise brands and report results to the sales "
                             "team. Requirements: hands-on Google Ads management and conversion tracking.")}
LINKEDIN = "https://www.linkedin.example/in/avery-example"


def _rubric_note() -> list[dict[str, Any]]:
    """Shorten the rubric letter fixture to the note shape this dialog requests."""
    fixture = json.loads((Path(__file__).parents[1] / "fixtures" / "browser" / "rubric_letter.json")
                         .read_text(encoding="utf-8"))["sentences"]
    ids = {"$FACT": "fact.bakery", "$JOB": POSTING_EVIDENCE["id"], "$STORY": None, "$CONTACT": "contact:links"}
    selected = [(fixture[i], paragraph) for i, paragraph in [(1, 0), (2, 0), (3, 1), (4, 1), (5, 1), (12, 2)]]
    return [{**sentence, "paragraph": paragraph, "fact_ids": [ids[i] for i in sentence["fact_ids"] if ids[i]],
             "job_evidence_ids": [ids[i] for i in sentence["job_evidence_ids"] if ids[i]]}
            for sentence, paragraph in selected]


class NoteWriter:
    """The fixture writer: the rubric letter for any draft; every review supports it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []

    def write(self, **kwargs: Any) -> Any:
        from interviewmaxxing_browser.ai.providers import NarrativeDraft

        self.calls.append(kwargs)
        return NarrativeDraft.model_validate({"status": "READY", "sentences": _rubric_note(),
                                              "missing_information": []})

    def review(self, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        self.reviews.append(kwargs)
        return SimpleNamespace(verdict="SUPPORTED", issues=[], reference_ids=[], rubric="PASS",
                               rubric_issues=[], owner_question="")


class LetterRetriever:
    """Knowledge retrieval double: the candidate's verified facts and the posting's text."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        self.calls.append(kwargs)
        facts = [f for f in kwargs["candidate"].facts] if "candidate" in kwargs else []
        return SimpleNamespace(facts=facts, job_evidence=[POSTING_EVIDENCE],
                               voice_samples=["I write short, plain sentences."], story_chunks=[],
                               receipt={"status": "OK", "story_ids": []})


class LetterJev:
    """Routes the note to the writer (WRITER, prose, the applicant's own past) and scores every
    grounding and consistency question fully supported."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> Any:
        from interviewmaxxing_selection.jev import HttpResponse

        request = json.loads(body)
        self.requests.append(request)
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            if question["type"] != "choice":
                answers[name] = {"type": "noul", "noul": 1.0}
                continue
            criteria = list(question["criteria"])
            choice = {"r": "WRITER", "n": "prose", "u": "HISTORICAL_OR_CONTEXTUAL",
                      "s": "COVER_LETTER", "d": "APPLICATION_ATTACHMENT"}.get(name[0]) if name[1:].isdigit() else None
            choice = choice if choice in criteria else criteria[0]
            answers[name] = {"type": "choice", "choice": choice, "confidence": 1.0,
                             "probabilities": {key: float(key == choice) for key in criteria}}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917", "answers": answers,
                                                 "usage": {"cost": 0.0001}}).encode())


def _write_letter_profile(paths: Any) -> None:
    import shutil

    directory = paths.profile_dir / "default"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "fixtures" / "browser" / "resume_avery_quill.pdf",
                    directory / "resume.pdf")
    verified = {"status": "VERIFIED", "method": "USER_CONFIRMED", "verified_at": "2026-09-01T12:00:00Z"}
    (directory / "profile.json").write_text(json.dumps({
        "id": "default",
        "identity": {"first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
                     "phone": "+1 (303) 555-0142", "linkedin_url": LINKEDIN, "verified_at": "2026-09-01T12:00:00Z"},
        "resume": {"id": "resume_supplied", "path": "resume.pdf"},
        "facts": [{"id": "fact.bakery", "key": "experience", "source": "resume", "verification": verified,
                   "value": "Managed paid search for a regional bakery chain and grew online orders by 35% (2024)."},
                  {"id": "fact.reports", "key": "experience", "source": "resume", "verification": verified,
                   "value": "Wrote weekly reports for two store managers."}],
        "saved_answers": [],
    }, indent=2))


def test_the_runner_prepares_the_note_dialog_with_a_written_note(
    kit: Any, server: Any, options: BrowserOptions, isolated_imx_home: Any
) -> None:
    from interviewmaxxing_browser.ai import AIFormRouter, BoundedDecisions, CallBudget
    from interviewmaxxing_browser.ai.providers import NarrativeDraft
    from interviewmaxxing_browser.ai.routing import DynamicPacketResolver
    from interviewmaxxing_cli.runner import LocalApplicationRunner, NoninteractiveInteraction
    from interviewmaxxing_core import ApplicationState, ApplicationStore
    from interviewmaxxing_selection.credentials import ApiKey
    from interviewmaxxing_selection.jev import JevClient

    _write_letter_profile(isolated_imx_home)
    writer, retriever = NoteWriter(), LetterRetriever()
    decisions = BoundedDecisions(JevClient(ApiKey("synthetic-key", source="test"), transport=LetterJev(),
                                           max_attempts=1), CallBudget())
    resolver = DynamicPacketResolver(decisions, writer, router=AIFormRouter(decisions), retriever=retriever)

    class Recording:
        state: Any = None

        async def start(self, browser_options: BrowserOptions) -> Any:
            browser = await PlaywrightSessionFactory(settle_timeout_s=SETTLE_S).start(browser_options)
            close = browser.close

            async def close_and_record() -> None:
                Recording.state = await browser.page.evaluate(
                    "() => ({note: document.querySelector('#form-input--userNote')?.value ?? null,"
                    " mock: JSON.parse(JSON.stringify(window.__mock))})")
                await close()

            browser.close = close_and_record
            return browser

    runner = LocalApplicationRunner(paths=isolated_imx_home, interaction=NoninteractiveInteraction(), headless=True,
                                    browser_factory=Recording(), resolver=resolver, prepare_only=True)
    result = kit.run(runner.apply(server.url(WELLFOUND), candidate_id="default"))
    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    assert "Prepared to the final review step" in result.message, result.message
    # The note reached the writer as a cover letter and is on the page, "Send application" untouched.
    assert [call["purpose"] for call in writer.calls] == ["cover_letter"]
    assert writer.calls[0]["shape"] == "note"
    assert writer.calls[0]["job_evidence"] == [POSTING_EVIDENCE]
    assert {fact["id"] for fact in writer.calls[0]["facts"]} >= {"fact.bakery"}
    letter = NarrativeDraft.model_validate({"status": "READY", "sentences": _rubric_note(),
                                            "missing_information": []}).text
    assert Recording.state["note"] == letter
    assert (Recording.state["mock"]["sent"], Recording.state["mock"]["submitIgnored"]) == (0, 0)
    assert server.submissions("wellfound-modal")["accepted_count"] == 0
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert store.list_attempts(result.application_id) == []


# --- live dialogs ask more than a note (tonight's 30-dialog field map, wording fictional) -------

SCREEN = WELLFOUND + "?fields=screen"


def test_the_screening_dialog_reads_as_its_questions(kit: Any, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory(settle_timeout_s=SETTLE_S).start(options)
        try:
            return await browser.open(server.url(SCREEN))
        finally:
            await browser.close()

    page = kit.run(scenario())
    assert page.kind is PageKind.APPLICATION_FORM and page.form is not None, page.message
    by_label = {f.label: f for f in page.form.fields}
    # Required by the "*" of the question as shown, never by the attribute (Wellfound sets none).
    assert by_label["Phone Number"].required and not by_label["Portfolio Link"].required
    # A limit only the help text states is the answer's limit.
    salary = by_label["Please share your salary expectations"]
    assert (salary.max_length, salary.help_text) == (128, "Please limit your answer to 128 characters or less")
    # A radio group in one <label>: the question from its first line, the options from each input's parent.
    sponsorship = by_label["Will you now or in the future require sponsorship to work in the US?"]
    assert (sponsorship.control_type, sponsorship.semantic_type, sponsorship.required) == (
        ControlType.RADIO, SemanticType.SPONSORSHIP, True)
    assert [o.label for o in sponsorship.options or []] == ["Yes", "No"]
    # A checkbox group with an empty name, in one <label>: one question with its options.
    pronouns = by_label["What are your pronouns?"]
    assert (pronouns.control_type, pronouns.semantic_type, pronouns.required) == (
        ControlType.CHECKBOX_GROUP, SemanticType.PRONOUNS, True)
    assert [o.label for o in pronouns.options or []] == [
        "pronouns not listed", "they/them/theirs", "he/him/his", "she/her/hers"]
    # The name of the person who referred the applicant is not the referral source.
    referrer = by_label["If yes, please add the name of the employee who referred you"]
    assert (referrer.semantic_type, referrer.required) == (SemanticType.CUSTOM_LONG_TEXT, False)
    assert by_label["What interests you about working for this company?"].required


def test_the_screening_dialog_is_filled_and_its_answers_are_sent_in_an_approved_submit(
    kit: Any, server: Any, options: BrowserOptions
) -> None:
    answers_by_label = {
        "Phone Number": "+1 (303) 555-0142",
        "LinkedIn Profile": "https://www.linkedin.example/in/avery-example",
        "Please share your salary expectations": "USD 120,000 base",
        "Will you now or in the future require sponsorship to work in the US?": "No",
        "What are your pronouns?": ["they/them/theirs"],
        "What interests you about working for this company?": LETTER,
    }

    async def work(browser: Any, bridge: SyntheticClickBridge, page: Any) -> Any:
        assert page.form is not None, page.message
        answers = {f.id: answers_by_label[f.label] for f in page.form.fields if f.label in answers_by_label}
        filled = await browser.fill(page.form, kit.build(page.form, answers).packet)
        sent = await browser.submit_approved()
        observed = await browser.confirm()
        return filled, sent, observed

    filled, sent, observed = kit.run(_over_bridge(options, server.url(SCREEN), work))
    assert filled.ok, filled.fields
    assert "once by script (DOM click)" in sent.detail and observed.outcome.value == "ACCEPTED"
    [record] = server.submissions("wellfound-modal")["submissions"]
    fields = record["fields"]
    assert fields["customQuestionAnswers[900005][jobListingQuestionOptionId]"] == "7001"  # "No"
    assert fields["customQuestionAnswers[900003][answer]"] == "USD 120,000 base"
    assert fields["customQuestionAnswers[900007][answer]"] == LETTER
    assert "customQuestionAnswers[900008][answer]" in fields and not fields["customQuestionAnswers[900008][answer]"]


def test_opening_again_uses_a_fresh_tab(kit: Any, server: Any, options: BrowserOptions) -> None:
    """Live: Wellfound's Apply answers a script click only in a tab that loaded the job fresh.
    Each run opens its own tab; a second ``open`` in the same run (after a sign-in or consent
    wait) closes the driver's own tab and opens the page in a new one."""
    async def work(browser: Any, bridge: SyntheticClickBridge, page: Any) -> Any:
        before = len(bridge.calls)
        again = await browser.open(server.url(WELLFOUND))
        return page, again, [argv[argv.index("browser") + 2:argv.index("browser") + 4] for argv in bridge.calls[before:]]

    first, again, commands = kit.run(_over_bridge(options, server.url(WELLFOUND), work))
    assert first.kind is again.kind is PageKind.APPLICATION_FORM
    names = [" ".join(c) for c in commands]
    assert names.index("tab close") < names.index("tab new") < next(i for i, n in enumerate(names) if n.startswith("open"))
