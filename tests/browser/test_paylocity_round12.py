"""Round 12: Paylocity's custom controls (``paylocity-address`` on the local mock ATS).

react-widgets DropdownLists named by a ``<label for>`` that points at a div, input-selects
(react-selects without ARIA roles) whose value element covers the input, a street address
input that keeps what is typed while it lists suggestions, and a OneTrust cookie banner
over the bottom half of the page. Real headless Chromium, fictional data, temp IMX_HOME;
nothing is submitted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_browser.runtime import _NATIVE_VALIDITY
from interviewmaxxing_cli.runner import LocalApplicationRunner, NoninteractiveInteraction
from interviewmaxxing_core import (
    AnswerSource,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    BrowserOptions,
    ChoiceValue,
    ControlType,
    FieldFillStatus,
    LocalPaths,
    PacketAnswer,
    Provenance,
    SemanticType,
    TextValue,
    UserInput,
    render_question,
)

REPO = Path(__file__).resolve().parents[2]
RESUME_PATH = REPO / "tests" / "fixtures" / "browser" / "resume_avery_quill.pdf"
VERIFIED_AT = "2026-09-01T12:00:00Z"
APPLY = "/jobs/paylocity-address/apply"
SMS = "info.smsOptedIn"
WORKED = "info.haveYouWorkedWithUsBefore"
COUNTRY = "public-site-address-country"
STATE = "public-site-address-us-state"
ADDRESS = "address.address1"
SMS_QUESTION = "We may use SMS during the hiring process. Do you give us permission to text you?"
WORKED_QUESTION = "Have you worked with us before?"
PAGE_STATE = """() => ({
  sms: window.__widgetState['info.smsOptedIn'].value,
  worked: window.__widgetState['info.haveYouWorkedWithUsBefore'].value,
  country: window.__widgetState['address.country'].value,
  state: window.__widgetState['address.state'].value,
  address: document.getElementById('public-site-address-address-1').value,
  picked: window.__addressPicked,
  listOpen: !document.getElementById('public-site-address-address-1-autocomplete-list').hidden,
  cookie: window.__cookieChoice,
})"""
REVIEW_STATE = """() => ({
  title: document.title,
  cookie: window.__cookieChoice || null,
  saved: Object.fromEntries(Array.from(document.querySelectorAll('dl.review dt'),
    (dt) => [dt.textContent, dt.nextElementSibling.textContent])),
})"""
ANSWERS = {"first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
           "phone": "+1 303 555 0142", SMS: "Yes", WORKED: "No", COUNTRY: "United States",
           ADDRESS: "1234 Fictional Avenue", "address.city": "Denver", STATE: "Colorado", "address.zip": "80202"}


def _packet(kit: SimpleNamespace, form: ApplicationForm, answers: dict[str, str]) -> ApplicationPacket:
    """``kit.build`` plus typed answers for the lookups (their answer is text)."""
    lookups = {k: v for k, v in answers.items() if form.field(k).control_type is ControlType.TYPEAHEAD}
    built = kit.build(form, {k: v for k, v in answers.items() if k not in lookups}).packet
    typed = []
    for field_id, text in lookups.items():
        value = TextValue(text=text)
        user = UserInput.for_field(form, field_id, value)
        typed.append(PacketAnswer(field_id=field_id, semantic_type=form.field(field_id).semantic_type, value=value,
                                  provenance=Provenance(source=AnswerSource.USER_INPUT, reference_ids=[user.id])))
    return ApplicationPacket.model_validate({
        **built.model_dump(),
        "answers": [*(a.model_dump() for a in built.answers), *(a.model_dump() for a in typed)],
        "missing_inputs": [m.model_dump() for m in built.missing_inputs if m.field_id not in lookups],
    })


def _fill(kit: SimpleNamespace, options: BrowserOptions, url: str, answers: dict[str, str]) -> tuple[Any, Any, dict[str, Any]]:
    async def scenario() -> tuple[Any, Any, dict[str, Any]]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(url)
            assert page.form is not None, page.message
            result = await browser.fill(page.form, _packet(kit, page.form, answers))
            return page.form, result, await browser.page.evaluate(PAGE_STATE)
        finally:
            await browser.close()

    form, result, state = kit.run(scenario())
    return form, result, state


def test_the_paylocity_controls_are_questions_named_as_shown(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            return await browser.open(server.url(APPLY))
        finally:
            await browser.close()

    page = kit.run(scenario())
    form = page.form
    assert form is not None and form.is_final_step is False and form.next_selector == "#btn-submit"
    shape = {f.id: (f.control_type, f.semantic_type, f.label) for f in form.fields}
    # react-widgets DropdownLists: SELECTs with the options read after opening, named by
    # the <label for> that points at the div; the SMS one is the consent question.
    assert shape[SMS] == (ControlType.SELECT, SemanticType.CONSENT, SMS_QUESTION)
    assert shape[WORKED][0] is ControlType.SELECT and shape[WORKED][2] == WORKED_QUESTION
    assert [o.label for o in form.field(SMS).options or []] == ["Yes", "No"]
    assert "text messages" in (form.field(SMS).help_text or "")
    # Input-selects: lookups labelled by the question alone, never with what they show.
    assert shape[COUNTRY] == (ControlType.TYPEAHEAD, SemanticType.COUNTRY, "Country")
    assert shape[STATE] == (ControlType.TYPEAHEAD, SemanticType.STATE, "State")
    # The street address keeps what is typed: a text question, not a lookup.
    assert shape[ADDRESS] == (ControlType.TEXT, SemanticType.ADDRESS, "Address Line 1")


@pytest.mark.parametrize("cookie_ms", [600, 3000], ids=["banner-before-fill", "banner-mid-fill"])
def test_the_paylocity_form_is_filled_and_read_back(
    cookie_ms: int, kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    form, result, state = _fill(kit, options, server.url(f"{APPLY}?cookie_ms={cookie_ms}"), ANSWERS)
    statuses = {f.field_id: f.status for f in result.fields}
    assert statuses == {f.id: FieldFillStatus.FILLED if f.id in ANSWERS else FieldFillStatus.SKIPPED
                        for f in form.fields}, result
    assert result.page_errors == []
    country = next(f for f in result.fields if f.field_id == COUNTRY)
    assert country.detail == "already shows this value"  # "United States" was shown already
    assert state == {"sms": "true", "worked": "false", "country": "US", "state": "CO",
                     "address": "1234 Fictional Avenue", "picked": None, "listOpen": False,
                     # Declined, whether it slid in before the fill or while it ran.
                     "cookie": "reject"}


def test_a_chosen_input_select_is_not_an_empty_required_input_to_the_browser(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """The input-select's required input stays empty after a choice (the value element
    shows it). On a form without ``novalidate`` whose next control is the site's own script,
    the browser-validity read before ``advance`` must not report it: only one showing its
    placeholder is empty."""
    async def scenario() -> tuple[list[str], list[str]]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.open(server.url(APPLY))
            await browser.page.evaluate("() => document.querySelector('form').removeAttribute('novalidate')")
            before: list[str] = await browser.page.evaluate(_NATIVE_VALIDITY, {"form": "form", "button": None})
            form = (await browser.inspect()).form
            assert form is not None
            result = await browser.fill(form, _packet(kit, form, ANSWERS))
            assert result.ok, result.fields
            after: list[str] = await browser.page.evaluate(_NATIVE_VALIDITY, {"form": "form", "button": None})
            return before, after
        finally:
            await browser.close()

    before, after = kit.run(scenario())
    assert [line.split(":")[0] for line in before if line.startswith(("State", "Country"))] == ["State"], before
    assert not any(line.startswith(("State", "Country")) for line in after), after


@pytest.mark.parametrize(("state_answer", "status"), [
    ("Colorad", FieldFillStatus.NEEDS_CHOICE),
    ("Atlantis", FieldFillStatus.NEEDS_CHOICE),
], ids=["no-exact-option", "no-option"])
def test_an_input_select_chooses_only_an_option_equal_to_the_answer(
    state_answer: str, status: FieldFillStatus, kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    _, result, state = _fill(kit, options, server.url(APPLY), {**ANSWERS, STATE: state_answer})
    chosen = next(f for f in result.fields if f.field_id == STATE)
    assert chosen.status is status, chosen
    if state_answer == "Colorad":
        assert chosen.suggestions == ["Colorado"]  # listed, not taken
    assert state["state"] is None


# --- the consent banner never accepts marketing cookies ---------------------------------------

ACCEPT_ONLY = """<!doctype html><title>Apply</title><h1>Fictional role</h1>
<form method="post"><label for="n">Full name</label><input id="n" name="full_name" required>
<button type="submit">Submit application</button></form>
<div id="banner" role="region" aria-label="Cookie banner"><p>We use cookies to improve your experience.</p>
<button type="button" id="settings">Cookies Settings</button>{extra}
<button type="button" id="accept">Accept All Cookies</button></div>
<script>window.__clicked = [];
document.getElementById("banner").addEventListener("click", (e) => { if (e.target.id) window.__clicked.push(e.target.id); });</script>"""


@pytest.mark.parametrize(("extra", "clicked"), [
    ("", []),
    # A notice's "Got it" is often OneTrust's accept button relabelled: never clicked.
    ('<button type="button" id="got">Got it</button>', []),
    ('<button type="button" id="necessary">Accept only necessary cookies</button>', ["necessary"]),
    ('<button type="button" id="reject">Reject All</button>', ["reject"]),
], ids=["accept-only", "notice", "necessary-only", "reject-all"])
def test_a_cookie_banner_is_declined_never_accepted(extra: str, clicked: list[str], options: BrowserOptions) -> None:
    async def scenario() -> list[str]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.page.route("https://example.test/apply", lambda route: route.fulfill(
                content_type="text/html; charset=utf-8", body=ACCEPT_ONLY.replace("{extra}", extra)))
            await browser.open("https://example.test/apply")
            result: list[str] = await browser.page.evaluate("() => window.__clicked")
            return result
        finally:
            await browser.close()

    assert asyncio.run(scenario()) == clicked


CLOSE_BUTTONS = """<!doctype html><title>Apply</title><h1>Fictional role</h1>
<form method="post"><label for="n">Full name</label><input id="n" name="full_name" required>
<button type="submit">Submit application</button></form>
<div role="dialog" aria-label="Job alerts"><p>Get new roles by email.</p>
<button type="button" id="dismiss">Dismiss</button><button type="button" id="close">Close</button></div>
<div id="chat"><button type="button" id="ok">OK</button></div>
<footer><a href="#cookies">Cookie policy</a></footer>
<script>window.__clicked = [];
document.addEventListener("click", (e) => { if (e.target.id) window.__clicked.push(e.target.id); });
setTimeout(() => { const late = document.createElement("div");
  late.innerHTML = '<p>We use cookies.</p><button type="button" id="late-ok">OK</button>';
  document.body.append(late); }, 400);</script>"""


def test_buttons_that_close_something_else_are_never_taken_for_a_cookie_banner(
    kit: SimpleNamespace, options: BrowserOptions
) -> None:
    """Only a decline is ever clicked: a dialog's "Dismiss"/"Close" and a generic "OK", shown
    when the page opens or later, are left alone, even on a page that mentions cookies."""

    async def scenario() -> tuple[list[str], Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.page.route("https://example.test/apply", lambda route: route.fulfill(
                content_type="text/html; charset=utf-8", body=CLOSE_BUTTONS))
            page = await browser.open("https://example.test/apply")
            await asyncio.sleep(0.6)
            page = await browser.inspect()
            result = await browser.fill(page.form, kit.build(page.form, {"full_name": "Avery Quill"}).packet)
            clicked: list[str] = await browser.page.evaluate("() => window.__clicked")
            return clicked, result
        finally:
            await browser.close()

    clicked, result = kit.run(scenario())
    assert clicked == []
    assert [f.status for f in result.fields] == [FieldFillStatus.FILLED]


# --- the runner prepares the form to its final review step -----------------------------------


def _write_profile(paths: LocalPaths, sms_policy: str) -> None:
    """The fictional candidate, with the street address and saved answers worded as the
    form asks them."""
    directory = paths.profile_dir / "default"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RESUME_PATH, directory / "resume.pdf")
    profile = {
        "id": "default",
        "identity": {
            "first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
            "phone": "+1 (303) 555-0142",
            "address": {"street": "1234 Fictional Avenue", "city": "Denver", "region": "CO",
                        "postal_code": "80202", "country": "United States"},
            "verified_at": VERIFIED_AT,
        },
        "resume": {"id": "resume_supplied", "path": "resume.pdf"},
        "facts": [],
        "saved_answers": [
            # The consent's question is its label and the SMS policy shown under it.
            {"id": "sa.sms", "scope": "GLOBAL", "semantic_type": "CONSENT",
             "question": render_question(SMS_QUESTION, sms_policy),
             "value": "Yes", "confirmed_at": VERIFIED_AT},
            {"id": "sa.worked", "scope": "GLOBAL", "semantic_type": None, "question": WORKED_QUESTION,
             "value": "No", "confirmed_at": VERIFIED_AT},
        ],
    }
    (directory / "profile.json").write_text(json.dumps(profile, indent=2))


class RecordingFactory:
    """The Playwright factory, recording the page as each browser closes."""

    def __init__(self) -> None:
        self.states: list[dict[str, Any]] = []

    async def start(self, options: BrowserOptions) -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        close = browser.close

        async def close_and_record() -> None:
            with contextlib.suppress(Exception):
                self.states.append(await browser.page.evaluate(REVIEW_STATE))
            await close()

        browser.close = close_and_record
        return browser


def test_the_runner_prepares_the_paylocity_form_to_its_final_review(
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    _write_profile(isolated_imx_home, kit.mock_ats.PC_SMS.hint)
    factory = RecordingFactory()
    runner = LocalApplicationRunner(paths=isolated_imx_home, interaction=NoninteractiveInteraction(),
                                    headless=True, browser_factory=factory, prepare_only=True)

    result = kit.run(runner.apply(server.url(APPLY), candidate_id="default"))

    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    assert "Prepared to the final review step" in result.message, result.message
    assert result.missing_inputs == []
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        events = store.list_events(result.application_id)
        # The application step's packet (the review step that follows asks nothing).
        [step, review] = [e.metadata for e in events if e.event == "packet.saved"]
        packet = store.get_packet(step["packet_id"])
        assert store.list_attempts(result.application_id) == []
    assert (step["form_step"], step["missing_field_ids"]) == (0, [])
    assert (review["form_step"], review["answered"]) == (1, 0)
    [ready] = [e for e in events if e.event == "preparation.ready"]
    assert ready.metadata["submitted"] is False
    failures = [e for e in events if e.event.startswith("application.") and "FAILED" in json.dumps(e.metadata)]
    assert failures == [], failures
    answers = {a.field_id: a.value for a in packet.answers}
    # A probed menu's option is known by its shown text (the page keeps "true"/"false").
    assert answers[SMS] == ChoiceValue(value="Yes", label="Yes")
    assert answers[WORKED] == ChoiceValue(value="No", label="No")
    assert answers[STATE] == TextValue(text="Colorado") and answers[COUNTRY] == TextValue(text="United States")
    assert answers[ADDRESS] == TextValue(text="1234 Fictional Avenue")
    # Stopped on the review page with the step's answers saved by the site; the banner was
    # declined on the form step, so the review page shows none; nothing submitted.
    assert factory.states and factory.states[-1]["title"].startswith("Review:"), factory.states
    assert factory.states[-1]["cookie"] == "reject"
    saved = factory.states[-1]["saved"]
    assert {k: saved[k] for k in (SMS_QUESTION, WORKED_QUESTION, "Country", "Address Line 1", "City", "State",
                                  "Zip")} == {SMS_QUESTION: "Yes", WORKED_QUESTION: "No", "Country": "United States",
                                              "Address Line 1": "1234 Fictional Avenue", "City": "Denver",
                                              "State": "Colorado", "Zip": "80202"}
    assert server.submissions("paylocity-address")["accepted_count"] == 0
