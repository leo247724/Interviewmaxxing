"""The browser runtime on the four replicated application flows of the localhost mock ATS.

A LinkedIn-style Easy Apply dialog wizard (link and button triggers, saved resume cards,
uploads, validation, an open shadow root, one accepted submission), a careers page that
embeds a Greenhouse-style application iframe, a JazzHR-style form whose only actions are
anchors, and a Dayforce-style posting wrapped in a job-alert form with client-side routes.

Each test drives ``GenericApplicationBrowser`` through ``PlaywrightSessionFactory`` in
headless Chromium. Sessions are prepare-only except in the one submission test. Everything
is fictional (Brambleway Analytics, candidate Avery Quill) and nothing leaves localhost.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import pytest

from interviewmaxxing_browser import (
    PlaywrightApplicationBrowser,
    PlaywrightDriver,
    PlaywrightSessionFactory,
    SubmissionRefused,
)
from interviewmaxxing_browser.aria import MenuProbe
from interviewmaxxing_browser.normalize import DIALOG_FORM_INDEX
from interviewmaxxing_browser.runtime import _holds_application
from interviewmaxxing_browser.signals import ButtonIntent, autofill_decline
from interviewmaxxing_browser.snapshot import DomControl
from interviewmaxxing_core import (
    ApplicationField,
    ApplicationForm,
    BrowserOptions,
    ControlType,
    FieldFillStatus,
    FillResult,
    PageKind,
    SemanticType,
    SubmissionOutcome,
)

SETTLE_S = 10.0
"""Settle timeout of every session: the Dayforce-style routes land 2.5 s after each click."""
WIZARD = "/jobs/modal-wizard"
APPLY_URL = WIZARD + "/apply?openSDUIApplyFlow=true"
EMAIL = "avery.quill@example.test"
PINNED = "resume_avery_quill.pdf"
SAVED = "Avery_Quill_Resume_2025.pdf"
SQL = "How many years of work experience do you have with SQL?"
AUTHORIZED = "Are you legally authorized to work in the United States?"
SPONSORSHIP = "Will you now or in the future require sponsorship for employment visa status?"
FOLLOW = "Follow Brambleway Analytics to stay up to date with their page."
WHOLE_NUMBER = "Enter a whole number between 0 and 99"
CONTACT = {"Email address": EMAIL, "Phone country code": "United States (+1)",
           "Mobile phone number": "+1 (303) 555-0142", "City": "Denver"}
QUESTIONS = {SQL: "7", AUTHORIZED: "Yes", SPONSORSHIP: "No"}
ANSWERED = {"email": EMAIL, "phone_country": "United States (+1)", "phone": "3035550142",
            "city": "Denver", "resume": PINNED, "resume_uploaded": False, "sql_years": "7",
            "work_authorization": "Yes", "sponsorship": "No", "follow": True}
"""``window.__easyApply.answers`` once the walk reaches the review step."""
CHOSE_PINNED = f"chose the resume {PINNED!r} already on the site is the pinned file"
UPLOAD_INPUT = "#jobs-document-upload-file-input-upload-resume"
FILLED, SKIPPED = FieldFillStatus.FILLED, FieldFillStatus.SKIPPED
EMBED_SRC = "/embed/job_app?for=brambleway&token=4007131"
STANDARD_IDS = ["first_name", "last_name", "email", "phone", "linkedin_url", "resume",
                "work_authorization", "years_experience", "sponsorship", "skills",
                "work_arrangements", "open_to_relocation", "why_brambleway"]
CORE_IDS = ["first_name", "last_name", "email", "phone", "linkedin_url", "resume",
            "work_authorization", "sponsorship"]

EASY = "() => JSON.parse(JSON.stringify(window.__easyApply))"
BEHIND = """() => ({
  dialogs: document.querySelectorAll('[role=dialog]').length,
  pills: Array.from(document.querySelectorAll('button.filter-pill')).map((b) =>
    [b.textContent.trim(), b.getAttribute('aria-pressed')]),
  decoys: ['input[type=search][aria-label="Search jobs"]',
           'textarea[aria-label="Write a message to the hiring team"]',
           'input[type=text][aria-label="Add a note about this job"]']
    .map((s) => document.querySelector(s) !== null),
})"""
"""The job view behind the dialog: open dialogs, the search-filter pills and whether the
three form-less decoys (search box, message box, note field) are still in the document."""
DECOY_LABELS = {"Search jobs", "Write a message to the hiring team", "Add a note about this job"}
SHADOW = """() => {
  const root = document.getElementById('interop-outlet').shadowRoot;
  return [document.querySelectorAll('[role=dialog]').length, root.querySelectorAll('[role=dialog]').length];
}"""
INLINE_ERRORS = """() => Array.from(document.querySelectorAll('.artdeco-inline-feedback--error'))
  .map((e) => e.textContent.trim())"""
TABS = """() => [
  getComputedStyle(document.getElementById('job-detail-panel')).display,
  getComputedStyle(document.getElementById('job-application-panel')).display,
  document.getElementById('tab-overview').getAttribute('aria-selected'),
  document.getElementById('tab-application').getAttribute('aria-selected')]"""
CLICKS = """document.addEventListener('click', (event) => {
  const target = event.target instanceof Element ? event.target.closest('button, a, [role=tab]') : null;
  if (target) sessionStorage.setItem('clicks', (sessionStorage.getItem('clicks') || '') + '|' + target.textContent.trim());
}, true);"""
"""Init script: every click on a button, link or tab, kept for the tab across same-origin
documents (the careers page and the embedded application page share an origin)."""
READ_CLICKS = "() => sessionStorage.getItem('clicks')"
ANCHOR = "a => [a.tagName, a.getAttribute('href'), a.textContent.trim()]"
SECTIONS = """() => ({
  shown: ['resumator-section-1', 'resumator-section-2'].map((id) =>
    getComputedStyle(document.getElementById(id)).display !== 'none'),
  saved: sessionStorage.getItem('resumator-saved-application'),
  status: document.getElementById('resumator-save-status').textContent,
})"""

AFTER_UPLOAD_STOP = ("the page's actions or employer context changed after the upload; "
                     "inspect this step and resolve it again before continuing")
"""WP5's after-upload guard: the upload added the file's own card, with a Download button,
outside the uploader's container."""
HIDDEN_SECTION_UPLOAD = (
    "normalize.py:_operable: stepper-ambiguous/apply?sections=2, step 0 -> the display:none "
    "resume file input of the hidden section 2 (visible=False, label_visible=False, label "
    "'Resume') is a required step-0 field because any label text keeps a file input operable, "
    "vs expected only the four contact fields section 1 shows (its comment: operable 'unless "
    "its label is hidden too and it is not visible itself')"
)


# --- helpers ------------------------------------------------------------------------------


class KnownBug(AssertionError):
    """Raised by the one check an xfail reason is about. Each xfail is marked
    ``raises=KnownBug``, so it fails loudly when anything else goes wrong first."""


def expect(condition: bool, detail: object) -> None:
    """``assert`` for the behaviour an xfail reason describes (see ``KnownBug``)."""
    if not condition:
        raise KnownBug(detail)


@asynccontextmanager
async def session(options: BrowserOptions, *,
                  may_submit: bool = False) -> AsyncIterator[PlaywrightApplicationBrowser]:
    """A headless Playwright session; prepare-only unless ``may_submit``."""
    browser = await PlaywrightSessionFactory(settle_timeout_s=SETTLE_S).start(
        options if may_submit else replace(options, allow_submission=False))
    try:
        yield browser
    finally:
        await browser.close()


def driver_of(browser: PlaywrightApplicationBrowser) -> PlaywrightDriver:
    driver = browser.driver
    assert isinstance(driver, PlaywrightDriver)
    return driver


async def easy_state(browser: PlaywrightApplicationBrowser) -> dict[str, Any]:
    state: dict[str, Any] = await browser.page.evaluate(EASY)
    return state


def labelled(form: ApplicationForm, answers: Mapping[str, Any]) -> dict[str, Any]:
    """Answers keyed by question label, re-keyed by field id (labels are unique per step)."""
    ids = {f.label: f.id for f in form.fields}
    return {ids[label]: value for label, value in answers.items()}


def field_labelled(form: ApplicationForm, label: str) -> ApplicationField:
    return next(f for f in form.fields if f.label == label)


def outcome(result: FillResult) -> list[tuple[str, FieldFillStatus, str | None]]:
    return [(r.field_id, r.status, r.detail) for r in result.fields]


async def fill(browser: PlaywrightApplicationBrowser, kit: SimpleNamespace, form: ApplicationForm,
               answers: Mapping[str, Any]) -> FillResult:
    """Fill ``form`` with a packet built from label -> value answers (the conftest builder)."""
    result: FillResult = await browser.fill(form, kit.build(form, labelled(form, answers)).packet)
    return result


async def open_form(browser: PlaywrightApplicationBrowser, url: str) -> ApplicationForm:
    page = await browser.open(url)
    assert page.kind is PageKind.APPLICATION_FORM and page.form is not None, page.message
    return page.form


async def step_on(browser: PlaywrightApplicationBrowser, kit: SimpleNamespace, form: ApplicationForm,
                  answers: Mapping[str, Any]) -> ApplicationForm:
    """Fill one step and advance to the next; both must succeed."""
    result = await fill(browser, kit, form, answers)
    assert result.ok, outcome(result)
    nav = await browser.advance()
    assert nav.advanced and nav.inspection.form is not None, nav.validation_errors
    return nav.inspection.form


async def resume_step(browser: PlaywrightApplicationBrowser, kit: SimpleNamespace,
                      url: str) -> ApplicationForm:
    return await step_on(browser, kit, await open_form(browser, url), CONTACT)


async def questions_step(browser: PlaywrightApplicationBrowser, kit: SimpleNamespace,
                         url: str) -> ApplicationForm:
    form = await resume_step(browser, kit, url)
    return await step_on(browser, kit, form, {"Upload resume": kit.RESUME})


def nothing_received(server: Any, job_id: str) -> None:
    summary = server.submissions(job_id)
    assert (summary["accepted_count"], summary["rejected_count"]) == (0, 0)


# --- modal-wizard: reaching the dialog ------------------------------------------------------


@pytest.mark.parametrize(("query", "entry"), [
    ("", "'Easy Apply' (link to {origin}" + APPLY_URL + ")"),
    ("?trigger=button", "'Easy Apply' (button)"),
], ids=["link", "button"])
def test_observe_names_the_easy_apply_control_and_opens_nothing(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, query: str, entry: str
) -> None:
    url = server.url(WIZARD + query)

    async def scenario() -> tuple[Any, ...]:
        async with session(options) as browser:
            page = await browser.observe(url)
            return page, await browser.page.evaluate(BEHIND), await easy_state(browser), browser.page.url

    page, behind, state, now = kit.run(scenario())
    assert page.kind is PageKind.JOB_DESCRIPTION and page.form is None
    assert page.message == "Job posting; apply control: " + entry.format(origin=server.origin) + "."
    assert now == url  # nothing followed
    assert behind == {"dialogs": 0, "pills": [["Easy Apply", "false"], ["Remote", "false"]],
                      "decoys": [True, True, True]}
    assert (state["opens"], state["open"]) == (0, False)
    nothing_received(server, "modal-wizard")


@pytest.mark.parametrize(("query", "reached", "how"), [
    ("", APPLY_URL, "followed the apply link 'Easy Apply'"),
    ("?trigger=button", WIZARD + "?trigger=button", "clicked 'Easy Apply'"),
], ids=["link", "button"])
def test_open_takes_easy_apply_to_the_contact_step_of_the_dialog(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, query: str, reached: str, how: str
) -> None:
    async def scenario() -> tuple[Any, ...]:
        async with session(options) as browser:
            page = await browser.open(server.url(WIZARD + query))
            last = browser.last_page
            assert page.form is not None and page.form.next_selector and last is not None, page.message
            assert last.form_selector is not None
            shown = [await browser.page.locator(last.form_selector).get_attribute("role"),
                     await browser.page.locator(page.form.next_selector).get_attribute("aria-label")]
            return page, last, shown, await browser.page.evaluate(BEHIND), await easy_state(browser)

    page, last, shown, behind, state = kit.run(scenario())
    assert page.kind is PageKind.APPLICATION_FORM
    assert page.message == f"Reached the form: {how}."
    assert page.observed_url == server.url(reached)
    form = page.form
    # Only the dialog's own questions, with what the site pre-filled from the profile.
    assert [(f.label, f.control_type, f.required) for f in form.fields] == [
        ("Email address", ControlType.SELECT, True), ("Phone country code", ControlType.SELECT, True),
        ("Mobile phone number", ControlType.TEXT, True), ("City", ControlType.TEXT, True)]
    assert [last.bindings[f.id].value for f in form.fields] == [
        EMAIL, "United States (+1)", "3035550142", "Boulder"]
    assert not DECOY_LABELS & {f.label for f in form.fields}
    assert behind["decoys"] == [True, True, True]  # still behind the dialog, never questions
    assert (form.step, form.is_final_step, form.submit_selector) == (0, False, None)
    assert last.dialog_index is not None and last.step_source == "progress"
    assert shown == ["dialog", "Continue to next step"]
    assert behind["dialogs"] == 1
    assert behind["pills"] == [["Easy Apply", "false"], ["Remote", "false"]]  # the toggle stays off
    assert (state["opens"], state["open"], state["step"]) == (1, True, 1)
    assert set(state["writes"].values()) == {0}
    nothing_received(server, "modal-wizard")


# --- modal-wizard: the prepare-only walk ------------------------------------------------------


def test_prepare_only_walk_fills_each_dialog_step_and_stops_at_the_review(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> None:
        async with session(options) as browser:
            contact = await open_form(browser, server.url(WIZARD))
            email, country, phone, city = (f.id for f in contact.fields)
            result = await fill(browser, kit, contact, CONTACT)
            assert outcome(result) == [
                (email, FILLED, "already selected; left as it is"),
                (country, FILLED, "already selected; left as it is"),
                (phone, FILLED, "already shows this value; left as it is"),  # same national number
                (city, FILLED, None)]  # "Boulder" differed, so it was overwritten
            assert result.page_errors == []
            state = await easy_state(browser)
            assert [state["writes"][k] for k in ("email", "phone_country", "phone")] == [0, 0, 0]
            assert state["writes"]["city"] > 0 and state["answers"]["city"] == "Denver"

            nav = await browser.advance()
            assert nav.advanced and nav.inspection.form is not None, nav.validation_errors
            resume = nav.inspection.form
            last = browser.last_page
            assert last is not None and (resume.step, last.step_source) == (33, "progress")
            assert [(f.label, f.control_type, f.semantic_type) for f in resume.fields] == [
                ("Upload resume", ControlType.FILE, SemanticType.RESUME)]  # the cards are its state
            result = await fill(browser, kit, resume, {"Upload resume": kit.RESUME})
            assert outcome(result) == [(resume.fields[0].id, FILLED, CHOSE_PINNED)]
            state = await easy_state(browser)
            assert (state["answers"]["resume"], state["answers"]["resume_uploaded"]) == (PINNED, False)
            assert state["writes"]["resume"] > 0

            nav = await browser.advance()
            assert nav.advanced and nav.inspection.form is not None, nav.validation_errors
            questions = nav.inspection.form
            assert questions.step == 67
            assert [(f.label, f.control_type, f.required) for f in questions.fields] == [
                (SQL, ControlType.TEXT, True), (AUTHORIZED, ControlType.RADIO, True),
                (SPONSORSHIP, ControlType.SELECT, True)]  # the legend's hidden "Required" is not wording
            assert [o.label for o in field_labelled(questions, AUTHORIZED).options or []] == ["Yes", "No"]
            result = await fill(browser, kit, questions, QUESTIONS)
            assert [r.status for r in result.fields] == [FILLED] * 3 and result.page_errors == []

            nav = await browser.advance()
            assert nav.advanced and nav.inspection.form is not None, nav.validation_errors
            review = nav.inspection.form
            assert (review.step, review.is_final_step, review.next_selector) == (100, True, None)
            assert [(f.label, f.control_type, f.required) for f in review.fields] == [
                (FOLLOW, ControlType.CHECKBOX, False)]
            assert review.submit_selector is not None
            submit = browser.page.locator(review.submit_selector)
            assert await submit.get_attribute("aria-label") == "Submit application"
            result = await browser.fill(review, kit.build(review, {}).packet)
            assert outcome(result) == [(review.fields[0].id, SKIPPED, None)]  # left as the site set it

            prepared = await browser.prepare_review()
            assert prepared.kind is PageKind.APPLICATION_FORM and prepared.form is not None
            assert (prepared.form.step, prepared.form.page_errors) == (100, [])
            sent = await browser.submit()
            assert (sent.dispatched, sent.detail) == (False, "submission belongs to the user in this session")
            assert (await browser.confirm()).outcome is SubmissionOutcome.NOT_SUBMITTED
            state = await easy_state(browser)
            assert (state["step"], state["open"], state["submitted"]) == (4, True, False)
            assert state["answers"] == ANSWERED
            assert [state["writes"][k] for k in ("email", "phone_country", "phone", "follow")] == [0] * 4

    kit.run(scenario())
    nothing_received(server, "modal-wizard")


# --- modal-wizard: resumes --------------------------------------------------------------------


@pytest.mark.parametrize(("variant", "detail", "chosen", "uploaded"), [
    ("one", f"chose the only resume on the site, {SAVED!r}", SAVED, False),
    ("nomatch", "already attached; not attached again", PINNED, True),
    ("none", "already attached; not attached again", PINNED, True),
], ids=["only-card", "no-card-named-like-it", "no-cards"])
def test_resume_step_uses_the_only_card_or_uploads_the_pinned_file(
    kit: SimpleNamespace, server: Any, options: BrowserOptions,
    variant: str, detail: str | None, chosen: str, uploaded: bool,
) -> None:
    async def scenario() -> None:
        async with session(options) as browser:
            form = await resume_step(browser, kit, server.url(f"{WIZARD}?resumes={variant}"))
            (field,) = form.fields
            assert (field.label, field.control_type) == ("Upload resume", ControlType.FILE)
            result = await fill(browser, kit, form, {"Upload resume": kit.RESUME})
            state = await easy_state(browser)
            assert (state["answers"]["resume"], state["answers"]["resume_uploaded"]) == (chosen, uploaded)
            assert (state["writes"]["resume"] > 0) is uploaded  # the only card was selected already
            if uploaded:
                # The upload adds the file's own card, whose Download button lies outside the
                # uploader's container: WP5's guard stops the fill for a fresh inspection. The
                # re-inspected field keeps its approved binding (this runtime's upload), and the
                # second fill finds the pinned file still attached (digest-checked).
                assert outcome(result) == [(field.id, FILLED, None)]
                assert result.page_errors == [AFTER_UPLOAD_STOP]
                form = (await browser.inspect()).form
                assert form is not None and [f.id for f in form.fields] == [field.id]
                result = await fill(browser, kit, form, {"Upload resume": kit.RESUME})
                state = await easy_state(browser)
                assert (state["answers"]["resume"], state["answers"]["resume_uploaded"]) == (PINNED, True)
            assert outcome(result) == [(field.id, FILLED, detail)]
            assert result.page_errors == []
            nav = await browser.advance()
            assert nav.advanced and nav.inspection.form is not None, nav.validation_errors
            assert nav.inspection.form.step == 67

    kit.run(scenario())
    nothing_received(server, "modal-wizard")


def test_unusable_saved_resumes_without_file_attachment_are_handed_to_the_person(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> None:
        async with session(options) as browser:
            driver_of(browser).attaches_files = False  # like OpenCLI's Browser Bridge
            form = await resume_step(browser, kit, server.url(f"{WIZARD}?resumes=nomatch"))
            (field,) = form.fields
            assert field.control_type is ControlType.FILE  # the pinned file is not known yet
            packet = kit.build(form, {field.id: kit.RESUME}).packet
            with pytest.raises(ValueError, match="the resume must be attached in the browser window") as held:
                await browser.fill(form, packet)
            assert f"is the pinned {PINNED!r}" in str(held.value)
            state = await easy_state(browser)
            assert (state["answers"]["resume"], state["writes"]["resume"]) == (SAVED, 0)  # untouched

            page = await browser.inspect()
            assert page.form is not None
            (handed,) = page.form.fields
            assert (handed.id, handed.control_type, handed.required) == (field.id, ControlType.UNSUPPORTED, True)
            assert handed.help_text is not None
            assert handed.help_text.endswith(f"Attach your resume in the browser window ({PINNED}).")
            last = browser.last_page
            assert last is not None and last.unsupported_pending == [handed.id]

            # The person attaches the pinned file in the browser window; the site shows it as
            # a selected card named like the pinned file, and the step is the runtime's again.
            await browser.page.set_input_files(UPLOAD_INPUT, str(kit.RESUME_PATH))
            done = await browser.wait_for_user("attach the resume", timeout_s=5.0)
            last = browser.last_page
            assert last is not None and last.unsupported_pending == []
            assert done.form is not None
            (field,) = done.form.fields
            assert field.control_type is ControlType.FILE
            result = await browser.fill(done.form, kit.build(done.form, {field.id: kit.RESUME}).packet)
            assert outcome(result) == [(field.id, FILLED, CHOSE_PINNED)]
            nav = await browser.advance()
            assert nav.advanced and nav.inspection.form is not None and nav.inspection.form.step == 67
            state = await easy_state(browser)
            assert (state["answers"]["resume"], state["answers"]["resume_uploaded"]) == (PINNED, True)

    kit.run(scenario())
    nothing_received(server, "modal-wizard")


def test_wizard_without_saved_resumes_or_file_attachment_asks_the_person_at_once(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[ApplicationForm, list[str]]:
        async with session(options) as browser:
            driver_of(browser).attaches_files = False
            form = await resume_step(browser, kit, server.url(f"{WIZARD}?resumes=none"))
            last = browser.last_page
            assert last is not None
            return form, list(last.unsupported_pending)

    form, pending = kit.run(scenario())
    (field,) = form.fields
    assert (field.label, field.control_type, field.required) == (
        "Upload resume", ControlType.UNSUPPORTED, True)
    assert field.help_text is not None
    assert field.help_text.endswith("Attach your resume in the browser window.")  # no pinned file yet
    assert pending == [field.id]


# --- modal-wizard: validation ---------------------------------------------------------------


def test_unanswered_or_rejected_answer_keeps_the_wizard_on_its_step(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> None:
        async with session(options) as browser:
            form = await questions_step(browser, kit, server.url(WIZARD))
            sql = field_labelled(form, SQL)
            partial = kit.build(form, labelled(form, {AUTHORIZED: "Yes", SPONSORSHIP: "No"})).packet
            assert [m.field_id for m in partial.missing_inputs] == [sql.id]
            result = await browser.fill(form, partial)
            assert [(r.field_id, r.status) for r in result.fields] == [
                (sql.id, SKIPPED), *((f.id, FILLED) for f in form.fields[1:])]
            blocked = await browser.advance()
            # The browser's own validation blocks the step: Review is never clicked.
            assert not blocked.advanced
            assert len(blocked.validation_errors) == 1
            assert blocked.validation_errors[0].startswith(f"{SQL}: ")
            assert blocked.inspection.form is not None and blocked.inspection.form.step == 67
            assert await browser.page.evaluate(INLINE_ERRORS) == []
            assert (await easy_state(browser))["step"] == 3

            # A non-numeric answer passes that check; the site rejects it inline.
            current = (await browser.inspect()).form
            assert current is not None
            result = await fill(browser, kit, current, {**QUESTIONS, SQL: "seven"})
            assert result.ok, outcome(result)
            rejected = await browser.advance()
            assert not rejected.advanced
            assert f"{SQL}: {WHOLE_NUMBER}" in rejected.validation_errors
            assert rejected.inspection.form is not None and rejected.inspection.form.step == 67
            assert field_labelled(rejected.inspection.form, SQL).validation_error == WHOLE_NUMBER
            last = browser.last_page
            assert last is not None and last.step_source == "progress"
            assert await browser.page.evaluate(INLINE_ERRORS) == [WHOLE_NUMBER]
            state = await easy_state(browser)
            assert (state["step"], state["answers"]["sql_years"]) == (3, "seven")

    kit.run(scenario())
    nothing_received(server, "modal-wizard")


def test_corrected_answer_after_the_sites_inline_error_fills_and_advances(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> None:
        async with session(options) as browser:
            form = await questions_step(browser, kit, server.url(WIZARD))
            result = await fill(browser, kit, form, {**QUESTIONS, SQL: "seven"})
            assert result.ok, outcome(result)
            rejected = await browser.advance()
            assert not rejected.advanced and f"{SQL}: {WHOLE_NUMBER}" in rejected.validation_errors
            current = (await browser.inspect()).form
            assert current is not None
            assert field_labelled(current, SQL).validation_error == WHOLE_NUMBER
            # Typing the corrected answer empties the site's error container; the other
            # answers already hold, and the step moves on.
            fixed = await fill(browser, kit, current, QUESTIONS)
            assert [r.status for r in fixed.fields] == [FILLED] * 3, outcome(fixed)
            assert fixed.page_errors == []
            assert await browser.page.evaluate(INLINE_ERRORS) == []
            nav = await browser.advance()
            assert nav.advanced and nav.inspection.form is not None, nav.validation_errors
            assert nav.inspection.form.step == 100
            assert (await easy_state(browser))["answers"]["sql_years"] == "7"

    kit.run(scenario())
    nothing_received(server, "modal-wizard")


# --- modal-wizard: open shadow root -----------------------------------------------------------


def test_step_worded_like_an_autofill_offer_is_never_declined_and_moves_on_only_by_advance(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """A wizard step reading "Import from LinkedIn or fill out this form", with a
    default-submit Skip, is the application itself. Filling declines real autofill offers
    first, but clicks nothing in this dialog: not Dismiss, Skip or Continue. Only
    advance() moves the step on, through Continue."""

    async def scenario() -> tuple[Any, ...]:
        async with session(options) as browser:
            await browser.page.add_init_script(CLICKS)
            contact = await open_form(browser, server.url(WIZARD + "?import=1"))
            last = browser.last_page
            assert last is not None
            opened = await browser.page.evaluate(READ_CLICKS)
            result = await fill(browser, kit, contact, CONTACT)
            filled = (await easy_state(browser), await browser.page.evaluate(READ_CLICKS))
            nav = await browser.advance()
            return (contact, last, opened, result, filled, nav, await easy_state(browser),
                    await browser.page.evaluate(READ_CLICKS))

    contact, last, opened, result, filled, nav, state, clicks = kit.run(scenario())
    # The dialog reads as an offer, and on wording alone its Dismiss would decline it.
    [prompt] = last.snapshot.prompts
    assert "Import from LinkedIn or fill out this form." in prompt.text
    assert [(b.text, b.submits, b.navigates) for b in prompt.buttons] == [
        ("Dismiss", False, False), ("Import from LinkedIn", False, False),
        ("Skip", True, False), ("Continue", True, False)]
    assert autofill_decline(prompt.text, [(b.text, b.selector) for b in prompt.buttons]) == (
        prompt.buttons[0].selector)
    # But it is the application's own dialog, whose Continue is the step's next action.
    assert prompt.dialog_index == last.dialog_index and _holds_application(last, prompt)
    assert contact.next_selector == prompt.buttons[3].selector
    assert [f.label for f in contact.fields] == [
        "Email address", "Phone country code", "Mobile phone number", "City"]
    assert result.ok, outcome(result)
    before, after_fill = filled
    assert (before["open"], before["step"], before["skipped"], before["imported"]) == (True, 1, 0, 0)
    assert after_fill == opened  # nothing clicked while filling
    assert nav.advanced and nav.inspection.form is not None, nav.validation_errors
    assert nav.inspection.form.step == 33
    assert (state["open"], state["step"], state["skipped"], state["imported"]) == (True, 2, 0, 0)
    assert clicks == (opened or "") + "|Continue"
    nothing_received(server, "modal-wizard")


def test_dialog_in_an_open_shadow_root_is_read_and_walked_to_the_review(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> None:
        async with session(options) as browser:
            contact = await open_form(browser, server.url(f"{WIZARD}?shadow=1"))
            last = browser.last_page
            assert last is not None and last.dialog_index is not None and last.step_source == "progress"
            assert last.form_selector is not None and last.form_selector.startswith("#interop-outlet >> ")
            assert contact.next_selector is not None
            assert contact.next_selector.startswith("#interop-outlet >> ")
            assert await browser.page.evaluate(SHADOW) == [0, 1]  # only inside the shadow root
            assert [f.label for f in contact.fields] == list(CONTACT)
            assert await browser.page.locator(contact.next_selector).get_attribute("aria-label") == (
                "Continue to next step")
            resume = await step_on(browser, kit, contact, CONTACT)
            questions = await step_on(browser, kit, resume, {"Upload resume": kit.RESUME})
            review = await step_on(browser, kit, questions, QUESTIONS)
            assert [f.step for f in (contact, resume, questions, review)] == [0, 33, 67, 100]
            assert review.is_final_step is True and review.submit_selector is not None
            assert review.submit_selector.startswith("#interop-outlet >> ")
            state = await easy_state(browser)
            assert state["answers"] == ANSWERED
            assert [state["writes"][k] for k in ("email", "phone_country", "phone")] == [0, 0, 0]

    kit.run(scenario())
    nothing_received(server, "modal-wizard")


# --- iframe-embed ---------------------------------------------------------------------------


@pytest.mark.parametrize(("query", "hidden"), [("", True), ("?panel=visible", False)],
                         ids=["hidden-panel", "visible-panel"])
def test_embedded_application_page_is_reported_then_opened_without_a_click(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, query: str, hidden: bool
) -> None:
    careers, src = server.url("/jobs/iframe-embed" + query), server.url(EMBED_SRC)

    async def scenario() -> tuple[Any, ...]:
        async with session(options) as browser:
            await browser.page.add_init_script(CLICKS)
            seen = await browser.observe(careers)
            tabs = await browser.page.evaluate(TABS)
            page = await browser.open(careers)
            return (seen, tabs, page, await browser.page.evaluate(READ_CLICKS), browser.page.url,
                    driver_of(browser)._frame)

    seen, tabs, page, clicks, top_url, frame = kit.run(scenario())
    assert seen.kind is PageKind.JOB_DESCRIPTION
    where = " (in a hidden panel); apply control: 'Apply Now' (button)" if hidden else ""
    assert seen.message == f"Job posting; the application form is embedded from {src}{where}."
    # observe switched nothing: the tabs are as the page rendered them.
    assert tabs == (["block", "none", "true", "false"] if hidden else ["none", "block", "false", "true"])
    assert page.kind is PageKind.APPLICATION_FORM and page.form is not None
    assert page.message == f"Reached the form: opened the application page embedded in {careers} ({src})."
    assert page.observed_url == top_url == src  # opened like a link, as the top document
    assert frame is None
    assert clicks is None  # neither "Apply Now" nor the Application tab was clicked
    assert [f.id for f in page.form.fields] == STANDARD_IDS
    assert page.form.is_final_step is True
    nothing_received(server, "iframe-embed")


def test_embedded_only_application_page_is_operated_inside_its_frame(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    careers = server.url("/jobs/iframe-embed?embedded_only=1")
    src = server.url(EMBED_SRC + "&embedded_only=1")

    async def scenario() -> tuple[Any, ...]:
        async with session(options) as browser:
            await browser.page.add_init_script(CLICKS)
            page = await browser.open(careers)
            driver = driver_of(browser)
            entered = (driver._frame is not None, driver.url, browser.page.url)
            assert page.form is not None, page.message
            result = await fill(browser, kit, page.form, {"First name": "Avery"})
            frame = next(f for f in browser.page.frames if f.url == src)
            typed = await frame.locator("#f-first_name").input_value()
            top = await browser.page.main_frame.locator("#f-first_name").count()
            return (page, entered, result, typed, top, await browser.page.evaluate(TABS),
                    await browser.page.evaluate(READ_CLICKS))

    page, entered, result, typed, top, tabs, clicks = kit.run(scenario())
    assert page.kind is PageKind.APPLICATION_FORM and page.form is not None
    assert page.message == (f"Reached the form: opened the application page embedded in {careers} ({src}); "
                            "then it does not load on its own, so it is operated inside the frame.")
    assert page.observed_url == src
    assert entered == (True, src, careers)  # reads and actions run in the frame of the careers page
    assert [f.id for f in page.form.fields] == STANDARD_IDS
    assert [(r.field_id, r.status) for r in result.fields if r.status is not SKIPPED] == [
        ("first_name", FILLED)]
    assert (typed, top) == ("Avery", 0)  # written inside the frame, not in the top document
    # To show the frame the runtime switched to its hidden panel once, with "Apply Now".
    assert tabs == ["none", "block", "false", "true"] and clicks == "|Apply Now"
    nothing_received(server, "iframe-embed")


# --- stepper-ambiguous ------------------------------------------------------------------------


def test_jazzhr_anchor_form_is_one_final_step_submitted_by_its_anchor(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, ...]:
        async with session(options) as browser:
            page = await browser.open(server.url("/jobs/stepper-ambiguous"))
            last = browser.last_page
            assert page.form is not None and page.form.submit_selector and last is not None, page.message
            anchor = await browser.page.locator(page.form.submit_selector).evaluate(ANCHOR)
            return page, [(b.button.text, b.intent) for b in last.buttons], anchor

    page, buttons, anchor = kit.run(scenario())
    assert page.kind is PageKind.APPLICATION_FORM and page.form is not None
    assert page.message == "Reached the form: followed the apply link 'Apply for this job'."
    assert page.observed_url == server.url("/jobs/stepper-ambiguous/apply")
    form = page.form
    assert [f.id for f in form.fields] == [
        "first_name", "last_name", "email", "phone", "desired_salary", "heard_about", "resume"]
    assert (form.is_final_step, form.submit_selector, form.next_selector) == (
        True, "#resumator-submit-resume", None)
    assert anchor == ["A", "#", "Submit Application"]
    assert buttons == [("Attach resume", ButtonIntent.OTHER), ("Paste resume", ButtonIntent.OTHER),
                       ("Submit Application", ButtonIntent.SUBMIT)]
    nothing_received(server, "stepper-ambiguous")


def test_jazzhr_sections_advance_by_the_next_anchor_and_never_save(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> None:
        async with session(options) as browser:
            first = await open_form(browser, server.url("/jobs/stepper-ambiguous/apply?sections=2"))
            last = browser.last_page
            assert last is not None and first.next_selector is not None
            assert (first.step, first.is_final_step, first.submit_selector) == (0, False, None)
            assert await browser.page.locator(first.next_selector).evaluate(ANCHOR) == ["A", "#", "Next"]
            assert [(b.button.text, b.intent) for b in last.buttons] == [
                ("Next", ButtonIntent.NEXT), ("Save", ButtonIntent.OTHER)]
            contact = {"First Name": "Avery", "Last Name": "Quill", "Email": EMAIL, "Phone": "3035550142"}
            result = await fill(browser, kit, first, contact)
            assert [r.status for r in result.fields if r.field_id in labelled(first, contact)] == [FILLED] * 4
            nav = await browser.advance()
            assert nav.advanced and nav.inspection.form is not None, nav.validation_errors
            second = nav.inspection.form
            assert (second.step, second.is_final_step, second.submit_selector, second.next_selector) == (
                1, True, "#resumator-submit-resume", None)
            assert {"desired_salary", "heard_about", "resume"} <= {f.id for f in second.fields}
            assert not {"first_name", "last_name", "email", "phone"} & {f.id for f in second.fields}
            sections = await browser.page.evaluate(SECTIONS)
            assert sections == {"shown": [False, True], "saved": None, "status": ""}  # Save never clicked

    kit.run(scenario())
    nothing_received(server, "stepper-ambiguous")


@pytest.mark.xfail(strict=True, raises=KnownBug, reason=HIDDEN_SECTION_UPLOAD)
def test_jazzhr_first_section_asks_only_the_questions_it_shows(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> ApplicationForm:
        async with session(options) as browser:
            return await open_form(browser, server.url("/jobs/stepper-ambiguous/apply?sections=2"))

    first = kit.run(scenario())
    assert first.step == 0 and first.is_final_step is False
    ids = [f.id for f in first.fields]
    expect(ids == ["first_name", "last_name", "email", "phone"], ids)


# --- apply-in-alert-form ----------------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "?nav=spa"], ids=["legacy-form", "spa-routes"])
def test_apply_inside_a_job_alert_form_reaches_the_manual_form_without_subscribing(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, query: str
) -> None:
    async def scenario() -> tuple[Any, list[tuple[str, str]]]:
        async with session(options) as browser:
            posts: list[tuple[str, str]] = []
            browser.page.on("request", lambda r: posts.append((r.url, r.post_data or ""))
                            if r.method == "POST" else None)
            return await browser.open(server.url("/jobs/apply-in-alert-form" + query)), posts

    page, posts = kit.run(scenario())
    assert page.kind is PageKind.APPLICATION_FORM and page.form is not None, page.message
    assert page.observed_url == server.url("/jobs/apply-in-alert-form/apply/manual")
    assert page.message == "Reached the form: clicked 'Apply'; then clicked 'Apply without an Account'."
    assert [f.id for f in page.form.fields] == CORE_IDS
    assert page.form.is_final_step is True
    if query:
        assert posts == []  # client-side routes only
    else:
        # The legacy portal's Apply submits its whole-page form: the alert email stays empty.
        [(url, body)] = posts
        sent = parse_qs(body, keep_blank_values=True)
        assert url == server.url("/jobs/apply-in-alert-form/start")
        assert (sent["apply"], sent["alert_email"], "subscribe" in sent) == (["1"], [""], False)
    summary = server.submissions()
    assert (summary["alert_count"], summary["accepted_count"], summary["rejected_count"]) == (0, 0, 0)


# --- menu probing inside the application dialog ------------------------------------------------


def combobox(*, dialog: int, in_dialog: bool = True, form: int = DIALOG_FORM_INDEX,
             expanded: bool = False) -> DomControl:
    """A closed, visible single-choice menu control as ``inspect.js`` reports it."""
    return DomControl.model_validate({
        "kind": "custom", "tag": "div", "type": "combobox", "name": "", "id": "visa-menu",
        "selector": "#visa-menu", "role": "combobox", "autocomplete_list": False,
        "label": SPONSORSHIP, "label_source": "aria-labelledby", "described": [],
        "error_message": "", "legend": None, "legend_selector": None, "legend_described": [],
        "group_label": None, "group_described": [], "adjacent": [], "adjacent_errors": [],
        "label_selector": None, "required": True, "disabled": False, "visible": True,
        "label_visible": True, "readonly": False, "value": "", "checked": False, "files": [],
        "placeholder": "", "autocomplete": "", "accept": "", "max_length": None,
        "multiple": False, "options": [], "invalid": False, "image_alts": [],
        "form_index": form, "has_value": False, "dialog_index": dialog,
        "aria": {"combo": 1, "role": "combobox", "haspopup": "listbox", "autocomplete": "",
                 "editable": False, "multiselectable": False, "dialog": in_dialog, "value": "",
                 "expanded": expanded},
    })


@pytest.mark.parametrize(("control", "form_index", "dialog", "expected"), [
    pytest.param(combobox(dialog=0), DIALOG_FORM_INDEX, 0, True, id="in-the-application-dialog"),
    pytest.param(combobox(dialog=1), DIALOG_FORM_INDEX, 0, False, id="in-a-dialog-nested-in-it"),
    pytest.param(combobox(dialog=0), DIALOG_FORM_INDEX, None, False, id="in-a-dialog-that-is-not-the-form"),
    pytest.param(combobox(dialog=0, expanded=True), DIALOG_FORM_INDEX, 0, False, id="already-open"),
    pytest.param(combobox(dialog=-1, in_dialog=False, form=0), 0, None, True, id="in-a-page-form"),
    pytest.param(combobox(dialog=-1, in_dialog=False, form=1), 0, None, False, id="in-another-form"),
])
def test_menu_probe_candidates_are_the_forms_own_closed_menus(
    control: DomControl, form_index: int, dialog: int | None, expected: bool
) -> None:
    assert MenuProbe.candidate(control, form_index, dialog) is expected


# --- modal-wizard: submission -----------------------------------------------------------------


def test_submitting_the_easy_apply_dialog_is_accepted_once(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, ...]:
        async with session(options, may_submit=True) as browser:
            contact = await open_form(browser, server.url(WIZARD))
            resume = await step_on(browser, kit, contact, CONTACT)
            questions = await step_on(browser, kit, resume, {"Upload resume": kit.RESUME})
            review = await step_on(browser, kit, questions, QUESTIONS)
            assert review.is_final_step is True
            left = await browser.fill(review, kit.build(review, {}).packet)
            assert [r.status for r in left.fields] == [SKIPPED]
            prepared = await browser.prepare_review()
            assert prepared.form is not None and prepared.form.page_errors == []
            before = server.submissions("modal-wizard")["accepted_count"]
            sent = await browser.submit()
            observed = await browser.confirm()
            with pytest.raises(SubmissionRefused, match="already observed an accepted submission"):
                await browser.submit()
            return before, sent, observed, await easy_state(browser)

    before, sent, observed, state = kit.run(scenario())
    assert before == 0
    assert (sent.dispatched, sent.detail) == (True, "clicked 'Submit application' once")
    assert observed.outcome is SubmissionOutcome.ACCEPTED, observed.signals
    assert observed.signals == ["acceptance text 'Application submitted'",
                                "job id 'BWA-LI-130' shown with it",
                                "job title 'Growth Marketing Lead' shown with it"]
    summary = server.submissions("modal-wizard")
    assert (summary["accepted_count"], summary["rejected_count"]) == (1, 0)
    (record,) = summary["submissions"]
    assert record["fields"] == {
        "email": EMAIL, "phone_country": "United States (+1)", "phone": "3035550142",
        "city": "Denver", "sql_years": "7", "work_authorization": "Yes", "sponsorship": "No",
        "follow_company": "yes"}
    assert record["files"] == {"resume": {"source": "saved_resume", "filename": PINNED,
                                          "details": "1 KB · Last used on 3/2/2026"}}
    assert record["extra_fields"] == {}  # the card was posted as resume_choice, never a field
    assert state["submitted"] and state["result"]["confirmation_reference"] == record["confirmation_reference"]
