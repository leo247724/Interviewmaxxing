"""Custom widget shapes found on live forms, replicated on the localhost mock ATS.

Greenhouse renders React-select menus inside the application form (no body portal),
next to a real "Toggle flyout" button, with a hidden required proxy input while a
required select is empty: an open menu or a choice must not look like a changed page.
Its page-level "Autofill my application" button is disabled for a moment on every
keystroke, its phone Country filters on the country's name only, and its résumé
uploader replaces the hidden input with the file's name. Rippling's popover menus close
only on a choice or a press outside them, and its SPA may render the questions without a
<form>; its location input never exposes aria-expanded and answers the first query
slowly. Workable shows the phone's dial code apart from the number and its uploader
empties its input. Fictional data, real headless Chromium, nothing submitted.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_browser.annotations import observation_signature
from interviewmaxxing_browser.aria import _filter_queries
from interviewmaxxing_browser.normalize import build_page
from interviewmaxxing_browser.signals import JOB_CLOSED
from interviewmaxxing_browser.snapshot import DomSnapshot, inspector_script
from interviewmaxxing_core import (
    AnswerSource,
    ApplicationForm,
    ApplicationPacket,
    BrowserOptions,
    ControlType,
    FieldFillStatus,
    PacketAnswer,
    PageKind,
    Provenance,
    TextValue,
    UserInput,
)

INLINE = "/jobs/react-select-inline/apply"
INLINE_ASYNC = "/jobs/react-select-inline-async/apply"
ORPHAN = "/jobs/div-combobox-orphan/apply"
WORKABLE = "/jobs/workable-like/apply"
COUNT_OPENS = """() => {
  window.__opens = 0;
  new MutationObserver((records) => {
    for (const r of records) if (r.target.getAttribute('aria-expanded') === 'true') window.__opens++;
  }).observe(document.body, {subtree: true, attributes: true, attributeFilter: ['aria-expanded']});
}"""
INLINE_ANSWERS: dict[str, Any] = {
    "question_9004": "United States +1", "phone": "+1 (303) 555-0142", "question_9001": "Yes",
    "years_experience": "6 to 9 years", "question_9002": "No", "question_9003": "LinkedIn",
    "why_brambleway": "Fictional answer for tests.",
}
INLINE_MENUS = ["question_9004", "question_9001", "question_9002", "question_9003"]
STATE = "() => JSON.parse(JSON.stringify(window.__widgetState))"
PROXIES = "() => document.querySelectorAll('input.select__required').length"
POPOVERS = """() => ({
  poppers: document.querySelectorAll('[data-testid=popper]').length,
  expanded: Array.from(document.querySelectorAll('[aria-expanded=true]')).map((e) => e.id),
})"""
CONTACT = {"first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test"}


async def _signature(browser: Any) -> str:
    """The fill guard's observation of the page as it is right now."""
    raw = await browser.page.evaluate(inspector_script())
    return observation_signature(build_page(browser.menus.merge(DomSnapshot.model_validate(raw))))


def _packet(kit: SimpleNamespace, form: ApplicationForm, answers: dict[str, Any]) -> ApplicationPacket:
    """``kit.build``, with a lookup's answer as the text to type."""
    lookups = {k: v for k, v in answers.items() if form.field(k).control_type is ControlType.TYPEAHEAD}
    built = kit.build(form, {k: v for k, v in answers.items() if k not in lookups}).packet
    typed = []
    for field_id, text in lookups.items():
        value = TextValue(text=text)
        user = UserInput.for_field(form, field_id, value)
        typed.append(PacketAnswer(field_id=field_id, semantic_type=form.field(field_id).semantic_type,
                                  value=value, provenance=Provenance(source=AnswerSource.USER_INPUT,
                                                                     reference_ids=[user.id])))
    return ApplicationPacket.model_validate({
        **built.model_dump(),
        "answers": [*(a.model_dump() for a in built.answers), *(a.model_dump() for a in typed)],
        "missing_inputs": [m.model_dump() for m in built.missing_inputs if m.field_id not in lookups],
    })


# --- Greenhouse: menus inside the form ------------------------------------------------------


def test_in_form_menus_are_probed_and_an_open_one_changes_nothing(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url(INLINE))
            buttons = [(b.text, b.disabled) for b in browser.last_page.snapshot.buttons]
            flyouts = await browser.page.evaluate(
                "() => document.querySelectorAll('button[aria-label=\"Toggle flyout\"]').length")
            proxies = await browser.page.evaluate(PROXIES)
            closed = await _signature(browser)
            await browser.page.click("#question_9004")
            menu = await browser.page.evaluate(
                "() => { const m = document.querySelector('.select__menu'); return m && "
                "{inForm: !!m.closest('form'), options: m.querySelectorAll('[role=option]').length}; }")
            opened = await _signature(browser)
            await browser.page.keyboard.press("Escape")
            return page, buttons, flyouts, proxies, closed, menu, opened, list(browser.menus.log)
        finally:
            await browser.close()

    page, buttons, flyouts, proxies, closed, menu, opened, log = kit.run(scenario())
    assert page.kind is PageKind.APPLICATION_FORM, page.message
    form = page.form
    # The hidden required proxies are part of their selects, never questions of their own.
    assert [f.id for f in form.fields] == [
        "first_name", "last_name", "email", "question_9004", "phone", "resume", "question_9001",
        "years_experience", "question_9002", "question_9003", "why_brambleway"]
    assert proxies == 3  # the three required selects, still empty
    # The uploader's hidden input is labelled "Attach"; the question is its group's name.
    assert form.field("resume").control_type is ControlType.FILE and form.field("resume").label == "Resume/CV"
    for field_id in INLINE_MENUS:
        assert form.field(field_id).control_type is ControlType.SELECT, field_id
    assert [o.label for o in form.field("question_9001").options or []] == ["Yes", "No"]
    assert len(form.field("question_9004").options or []) == 31
    assert [selector for selector, _, _ in log] == [f"#{m}" for m in INLINE_MENUS]
    # The fixture reproduces Greenhouse: the open menu is in the form, beside real
    # "Toggle flyout" buttons. Those belong to their menus and are not page buttons.
    assert flyouts == 4 and "Toggle flyout" not in [text for text, _ in buttons]
    assert {"Autofill my application", "Attach", "Dropbox"} <= {text for text, _ in buttons}
    # The submit button stays disabled until every required question is answered.
    assert ("Submit application", True) in buttons
    assert menu == {"inForm": True, "options": 31}
    assert opened == closed


def test_in_form_menus_an_uploader_and_a_keystroke_flicker_fill_through(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """Typing disables the page's autofill button for a moment; attaching replaces the
    uploader's input and buttons with the file's name; choosing removes a proxy input and
    adds a "Clear selection" button; the last answer enables the submit button. None of it
    is a changed page, and a second fill of the same form is clean."""
    async def scenario() -> tuple[Any, ...]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url(INLINE))
            packet = kit.build(page.form, {**CONTACT, **INLINE_ANSWERS, "resume": kit.RESUME}).packet
            fill = await browser.fill(page.form, packet)
            state = await browser.page.evaluate(STATE)
            # The page enables its submit button on its own schedule (it polls validity).
            await browser.page.wait_for_function(
                "() => !document.querySelector('button[type=submit]').disabled", timeout=3000)
            shown = await browser.page.evaluate(
                "() => ({chip: (document.querySelector('.file-upload__filename') || {}).innerText || null, "
                "input: !!document.getElementById('resume'), proxies: document.querySelectorAll("
                "'input.select__required').length, clears: document.querySelectorAll("
                "'button[aria-label=\"Clear selection\"]').length, submit: document.querySelector("
                "'button[type=submit]').disabled, invalid: document.getElementById('f-first_name')"
                ".getAttribute('aria-invalid')})")
            page_buttons = [b.text for b in browser.last_page.snapshot.buttons]
            review = await browser.prepare_review()
            again = await browser.fill(page.form, packet)
            return fill, state, shown, page_buttons, review, again
        finally:
            await browser.close()

    fill, state, shown, page_buttons, review, again = kit.run(scenario())
    assert fill.ok, [f for f in fill.fields if f.status is not FieldFillStatus.FILLED]
    assert {f.status for f in fill.fields} == {FieldFillStatus.FILLED}
    resume = next(f for f in fill.fields if f.field_id == "resume")
    assert resume.detail == "the uploader shows the file; its input is empty or replaced"
    assert shown["chip"].startswith(kit.RESUME_PATH.name) and not shown["input"] and shown["proxies"] == 0
    # Answering enabled the submit button, gave the answered clearable menus a "Clear
    # selection" button (theirs, not the page's) and left aria-invalid="false" behind.
    assert shown["submit"] is False and shown["clears"] == 2 and shown["invalid"] == "false"
    assert "Clear selection" not in page_buttons
    assert {k: v for k, v in state.items() if k != "resume"} == {
        "question_9004": {"value": "us"}, "question_9001": {"value": "in_wa_yes"},
        "question_9002": {"value": "in_sp_no"}, "question_9003": {"value": "src_linkedin"}}
    # The question is still on the page, answered: the review sees the form as approved.
    assert review.form is not None and review.form.page_errors == []
    assert review.form.field("resume").label == "Resume/CV"
    assert again.ok and {f.status for f in again.fields} == {FieldFillStatus.FILLED}
    assert next(f for f in again.fields if f.field_id == "resume").detail == "the uploader already shows this file"


@pytest.mark.parametrize("hook", ["uploadRenderOn: 'focusin'", None], ids=["lands-mid-fill", "lands-after-fill"])
def test_an_uploader_that_rerenders_seconds_later_does_not_stop_the_fill(
    hook: str | None, kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """Greenhouse finishes the upload seconds after the attach, then re-renders the résumé
    block (the input and its buttons give way to the file's name and "Remove file") and
    the page's action area (the submit button's path shifts). Arriving while later fields
    are written, or after the fill, that is our own answer, not a changed page."""
    async def scenario() -> tuple[Any, ...]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            if hook is not None:  # re-render as the next question takes focus
                await browser.page.add_init_script(f"window.__widgetHooks = {{selectNext: {{}}, {hook}}};")
            page = await browser.open(server.url(INLINE_ASYNC))
            submit_before = page.form.submit_selector
            fill = await browser.fill(page.form, kit.build(
                page.form, {**CONTACT, **INLINE_ANSWERS, "resume": kit.RESUME}).packet)
            await browser.page.wait_for_selector(".file-upload__filename", timeout=5000)
            review = await browser.prepare_review()
            return fill, submit_before, review, await browser.page.evaluate(STATE)
        finally:
            await browser.close()

    fill, submit_before, review, state = kit.run(scenario())
    assert fill.ok, [f for f in fill.fields if f.status is not FieldFillStatus.FILLED]
    assert {f.status for f in fill.fields} == {FieldFillStatus.FILLED}
    assert review.form is not None and review.form.page_errors == []
    # The submit action is the same button in a new place; the résumé is still answered.
    assert review.form.submit_selector != submit_before
    assert review.form.field("resume").label == "Resume/CV"
    assert state["question_9003"] == {"value": "src_linkedin"}


@pytest.mark.parametrize(("chosen", "committed", "status"), [
    ("United States +1", None, FieldFillStatus.FILLED),
    ("United States +1", "ca", FieldFillStatus.VERIFICATION_MISMATCH),
])
def test_a_dial_code_country_is_clicked_and_confirmed_by_reopening(
    chosen: str, committed: str | None, status: FieldFillStatus,
    kit: SimpleNamespace, server: Any, options: BrowserOptions,
) -> None:
    """The flagged Country filters on the country's name only, so typing its label would
    leave no option: the rendered option is clicked. It then shows only "+1", which
    Canada shares, so the menu is reopened and aria-selected must name the choice."""
    async def scenario() -> tuple[Any, ...]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url(INLINE))
            await browser.page.evaluate(COUNT_OPENS)
            if committed:
                await browser.page.evaluate(
                    f"() => {{ window.__widgetHooks.selectValue = {{question_9004: '{committed}'}}; }}")
            fill = await browser.fill_fields(page.form, kit.build(page.form, {"question_9004": chosen}).packet,
                                             ["question_9004"])
            typed = await browser.page.evaluate("() => document.getElementById('question_9004').value")
            return fill, await browser.page.evaluate("() => window.__opens"), typed, await browser.page.evaluate(STATE)
        finally:
            await browser.close()

    fill, opens, typed, state = kit.run(scenario())
    [result] = fill.fields
    assert result.status is status, result
    assert opens == 2 and typed == ""  # chosen, then reopened once; nothing was typed
    if committed:
        assert "aria-selected on 'Canada +1'" in (result.detail or "")
        assert state["question_9004"] == {"value": "ca"}
    else:
        assert state["question_9004"] == {"value": "us"}


# --- Workable: a separately shown dial code, a dropzone that empties its input ----------


def test_a_separate_dial_code_phone_and_a_dropzone_read_back(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario(hook: str | None) -> tuple[Any, ...]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            if hook:
                await browser.page.add_init_script(
                    f"window.__widgetHooks = {{selectNext: {{}}, {hook}: {{resume: true}}}};")
            page = await browser.open(server.url(WORKABLE))
            fill = await browser.fill(page.form, kit.build(page.form, {
                **CONTACT, "phone": "+15615550100", "input_files_input_resume": kit.RESUME}).packet)
            shown = await browser.page.evaluate(
                "() => ({phone: document.getElementById('phone').value, "
                "code: document.querySelector('.iti__selected-dial-code').textContent, "
                "files: document.getElementById('input_files_input_resume').files.length, "
                "name: (document.querySelector('[data-id=filename]') || {}).textContent || null})")
            review = await browser.prepare_review() if fill.ok else None
            return page, fill, shown, review
        finally:
            await browser.close()

    page, fill, shown, _ = kit.run(scenario(None))
    assert page.form.field("phone").expects_international_phone
    statuses = {f.field_id: f for f in fill.fields}
    assert statuses["phone"].status is FieldFillStatus.FILLED, statuses["phone"]
    # The widget took "+1" out of the input and shows it in its own element.
    assert shown["phone"] == "5615550100" and shown["code"] == "+1"
    resume = statuses["input_files_input_resume"]
    assert resume.status is FieldFillStatus.FILLED, resume
    assert shown["files"] == 0 and shown["name"] == kit.RESUME_PATH.name.replace(" ", "\u00a0", 1)
    assert fill.ok

    # Workable's own uploader keeps the file in its input and shows the name beside it:
    # verified by the bytes, and the shown name is not new wording of the question.
    _, kept, shown, review = kit.run(scenario("keepFile"))
    assert kept.ok and shown["files"] == 1 and shown["name"], kept
    assert review is not None and review.form is not None and review.form.page_errors == []

    _, failed, shown, _ = kit.run(scenario("uploadError"))
    resume = next(f for f in failed.fields if f.field_id == "input_files_input_resume")
    # The uploader shows an error and no file: the attach is not taken as done.
    assert resume.status is not FieldFillStatus.FILLED and shown["name"] is None


# --- closed postings and typed menu filters -------------------------------------------------


@pytest.mark.parametrize(("text", "closed"), [
    ("Job not found", True),
    ("The job you requested was not found.", True),
    ("This job is no longer available", True),
    ("Posting not found", True),
    ("The job does not exist", True),
    ("The job you are looking for is no longer open.", True),
    ("Page not found", False),
    ("No jobs found matching your search", False),
    ("If your job title is not found, choose Other.", False),
])
def test_not_found_and_no_longer_open_wording_is_job_closed(text: str, closed: bool) -> None:
    assert bool(JOB_CLOSED.search(text)) is closed


def test_a_job_not_found_page_is_job_closed(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            return await browser.open(server.url("/closed/not-found"))
        finally:
            await browser.close()

    page = kit.run(scenario())
    assert page.kind is PageKind.JOB_CLOSED and page.form is None


def test_typed_menu_filters_try_the_name_without_its_code() -> None:
    assert _filter_queries("United States +1") == ["United States +1", "United States", "United"]
    assert _filter_queries("Other (please specify)") == ["Other (please specify)", "Other"]
    assert _filter_queries("Yes") == ["Yes"]


# --- Rippling: popover menus, no <form> ---------------------------------------------------


def test_formless_popover_menus_are_probed_and_closed_by_an_outside_press(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url(ORPHAN))
            left = await browser.page.evaluate(POPOVERS)
            closed = await _signature(browser)
            await browser.page.click("#field-63")
            opened = await _signature(browser)
            shown = await browser.page.evaluate(POPOVERS)
            forms = await browser.page.evaluate("() => document.forms.length")
            return (page, browser.last_page.form_index, dict(browser.menus.observations), left,
                    closed == opened, shown, forms)
        finally:
            await browser.close()

    page, form_index, observations, left, stable, shown, forms = kit.run(scenario())
    assert page.kind is PageKind.APPLICATION_FORM, page.message
    assert forms == 0 and form_index == -1
    form = page.form
    gender, custom = form.field("field-55"), form.field("field-63")
    assert gender.control_type is custom.control_type is ControlType.SELECT
    assert gender.label == "Gender"  # aria-labelledby
    # Its aria-label is the placeholder ("Select"); the question is the paragraph before it.
    assert custom.label == "Are you legally authorized to work in the United States?"
    assert [o.label for o in gender.options or []] == ["Male", "Female", "Non-binary", "Choose not to disclose"]
    assert [o.label for o in custom.options or []] == ["No", "Yes"]
    location = form.field("field-42")
    assert location.control_type is ControlType.TYPEAHEAD and location.label == "Location"
    closers = {json.loads(key)[0]: o.close_method for key, o in observations.items()}
    assert closers["field-55"] == closers["field-63"] == "outside"
    assert left == {"poppers": 0, "expanded": []}
    # An open popover inside the questions is still the same page.
    assert shown == {"poppers": 1, "expanded": ["field-63"]} and stable


def test_formless_popover_menus_and_a_slow_lookup_are_filled_and_read_back(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url(ORPHAN))
            fill = await browser.fill(page.form, _packet(kit, page.form, {
                **CONTACT, "field-55": "Female", "field-63": "Yes", "field-42": "Austin, TX"}))
            location = await browser.page.evaluate("() => document.getElementById('field-42').value")
            return fill, await browser.page.evaluate(STATE), location, await browser.page.evaluate(POPOVERS)
        finally:
            await browser.close()

    fill, state, location, left = kit.run(scenario())
    assert fill.ok, [f for f in fill.fields if f.status is not FieldFillStatus.FILLED]
    assert {f.status for f in fill.fields} == {FieldFillStatus.FILLED}
    # The first query only answers after the site loads its place search (about 2 s).
    assert state == {"gender": {"value": "Female"}, "custom_work_authorization": {"value": "Yes"},
                     "location": {"value": "Austin, TX, USA"}}
    assert location == "Austin, TX, USA"
    assert left == {"poppers": 0, "expanded": []}


def test_a_menu_its_probe_cannot_close_stays_with_the_user(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.page.add_init_script(
                "window.__widgetHooks = {selectNext: {}, stuck: {'field-55': true}};")
            page = await browser.open(server.url(ORPHAN))
            return page, browser.menus.stopped, await browser.page.evaluate(POPOVERS)
        finally:
            await browser.close()

    page, stopped, left = kit.run(scenario())
    form = page.form
    # Its list is readable right now, but a menu that cannot be closed is never operated.
    assert left == {"poppers": 1, "expanded": ["field-55"]}
    assert form.field("field-55").control_type is ControlType.UNSUPPORTED
    assert stopped == "the menu did not close again"
    assert form.field("field-63").control_type is ControlType.UNSUPPORTED
