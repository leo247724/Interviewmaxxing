"""Round 7 item 1: a field's label is the question the page shows, never a placeholder
that only says what to do, an option's text or a machine identifier.

The question can come from a label, a legend, a question title given by a <label> that
labels nothing (Ashby), the heading its block opens with (Breezy) or the text before it
(Lever). Options that do not share a name are still one question: Ashby's name="Yes" /
name="No" checkboxes and yes/no toggle buttons. Mock pages ``/forms/breezy-like`` and
``/forms/ashby-like`` plus reduced live shapes (fictional data, headless Chromium,
nothing submitted).
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_core import (
    ApplicationForm,
    BrowserOptions,
    ControlType,
    FieldFillStatus,
    PageKind,
    SemanticType,
)

ID_LIKE = re.compile(r"^(?:section_\d+_question_\d+|field-\d+|[0-9a-f]{8}-[0-9a-f-]{20,}|[a-z]+[A-Z]\w*)$")
PLACEHOLDER_ONLY = {"type here...", "pick date...", "start typing...", "type your response"}
BREEZY = "section_1787064635874_question"
ASHBY_SALARY = "bad815aa-0000-4000-8000-00000000a005"
ASHBY_DATE = "52938440-0000-4000-8000-00000000a006"
ASHBY_REFERRAL = "7c1e2a90-5b3d-4f6e-8a1b-2c3d4e5f6a7b_25b7ff0a-0000-4000-8000-00000000a001"
ASHBY_WORK = "0f9e8d7c-6b5a-4c3d-9e2f-1a0b9c8d7e6f"
ASHBY_VISA = "cc031c31-0000-4000-8000-00000000a003"
ASHBY_RELOCATE = "c60ace77-0000-4000-8000-00000000a004"


async def _open(options: BrowserOptions, url: str | None = None, html: str = "") -> tuple[Any, Any]:
    browser = await PlaywrightSessionFactory().start(options)
    if html:
        await browser.page.route("https://example.test/apply", lambda route: route.fulfill(
            content_type="text/html; charset=utf-8", body=f"<title>Apply</title><h1>Fictional role</h1>{html}"))
    return browser, await browser.open(url or "https://example.test/apply")


def _inspect(options: BrowserOptions, url: str | None = None, html: str = "") -> ApplicationForm:
    async def scenario() -> Any:
        browser, page = await _open(options, url, html)
        try:
            return page
        finally:
            await browser.close()

    page = asyncio.run(scenario())
    assert page.kind is PageKind.APPLICATION_FORM, page.message
    form: ApplicationForm = page.form
    return form


def _no_machine_labels(form: ApplicationForm) -> None:
    for f in form.fields:
        assert not ID_LIKE.match(f.label), f
        assert f.label.lower() not in PLACEHOLDER_ONLY, f
        assert f.label.lower() not in {o.label.lower() for o in f.options or []}, f


def _labels(field: Any) -> list[str]:
    return [o.label for o in field.options or []]


# --- Breezy: questions are <h3> headings, custom inputs are named section_<n>_question_<i> ---


def test_breezy_questions_are_their_headings(options: BrowserOptions, server: Any) -> None:
    form = _inspect(options, server.url("/forms/breezy-like"))
    _no_machine_labels(form)
    assert form.field(f"{BREEZY}_0").label == "How many years have you spent leading a team of media buyers?"
    assert form.field(f"{BREEZY}_1").label == "What is your target salary for this role?"
    assert form.field(f"{BREEZY}_1").semantic_type is SemanticType.SALARY_EXPECTATION
    assert form.field(f"{BREEZY}_2").control_type is ControlType.TEXTAREA
    radio = form.field(f"{BREEZY}_3")
    assert radio.label == "Have you managed paid media for more than 100 client accounts at one time?"
    assert (radio.control_type, _labels(radio), radio.required) == (ControlType.RADIO, ["Yes", "No"], True)
    # Checkboxes with neither labels nor values: each option is named by its own text,
    # which is not help text of the question.
    boxes = form.field(f"{BREEZY}_4")
    assert boxes.label == "Which ad platforms have you managed budgets on?"
    assert boxes.control_type is ControlType.CHECKBOX_GROUP
    assert _labels(boxes) == ["Google Ads", "Meta", "LinkedIn", "TikTok"] and boxes.help_text is None
    race = form.field("race_ethnicity")
    assert race.label == "Race or Ethnicity" and _labels(race)[0] == "White (not Hispanic or Latino)"
    # The consent checkbox after the phone input states its own text; the phone heading
    # opens the phone input's block, not the checkbox's.
    sms = form.field("smsConsent")
    assert sms.label.startswith("By providing your phone number you agree") and sms.semantic_type is SemanticType.CONSENT
    # The salary block: one heading over a currency select, the amount and an unnamed period.
    salary = form.field("cSalary")
    assert (salary.label, salary.placeholder) == ("Desired Salary", "Desired Salary")
    assert form.field("salaryCurrency").label == "Desired Salary"
    period = next(f for f in form.fields if _labels(f) == ["Hourly", "Weekly", "Monthly", "Yearly"])
    assert period.label == "Desired Salary" and period.control_type is ControlType.SELECT


# --- Ashby: question titles are <label for="<field path>">, placeholders say what to do ------


def test_ashby_questions_come_from_their_titles(options: BrowserOptions, server: Any) -> None:
    form = _inspect(options, server.url("/forms/ashby-like"))
    _no_machine_labels(form)
    salary = form.field(ASHBY_SALARY)
    assert (salary.label, salary.placeholder) == ("What is your expected salary?", "Type here...")
    # The date input has no id or name: its title labels nothing, and names the field.
    date = form.field(ASHBY_DATE)
    assert date.label == "If you were to receive an offer, what is the earliest you could start?"
    assert (date.placeholder, date.control_type) == ("Pick date...", ControlType.TEXT)
    referral = form.field(ASHBY_REFERRAL)
    assert referral.label == "How did you first hear about us?" and referral.control_type is ControlType.RADIO
    # Options named after their own text are one question each, named by the field path.
    work = form.field(ASHBY_WORK)
    assert (work.label, work.control_type, _labels(work)) == (
        "How would you like to work?", ControlType.RADIO, ["On-site", "Hybrid", "Remote"])
    visa = form.field(ASHBY_VISA)
    assert (visa.label, visa.control_type, _labels(visa)) == (
        "Will you now or in the future require sponsorship?", ControlType.CHECKBOX_GROUP, ["Yes", "No"])
    assert visa.semantic_type is SemanticType.SPONSORSHIP
    # Yes/no toggle buttons over a display:none checkbox: a question whose options are
    # the buttons.
    relocate = form.field(ASHBY_RELOCATE)
    assert (relocate.label, relocate.control_type, _labels(relocate)) == (
        "Are you willing to relocate to Denver?", ControlType.RADIO, ["Yes", "No"])
    assert not any(f.label in ("Yes", "No") for f in form.fields)
    # Ashby draws the required "*" with CSS (a class's ::after): it counts as shown.
    assert relocate.required and not visa.required and not date.required


def test_ashby_like_date_choices_and_yes_no_are_filled(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, Any, dict[str, Any]]:
        browser, page = await _open(options, server.url("/forms/ashby-like"))
        try:
            form = page.form
            answers = {
                "_systemfield_name": "Avery Quill", "_systemfield_email": "avery.quill@example.test",
                ASHBY_SALARY: "$120,000", ASHBY_DATE: "10/15/2026", ASHBY_REFERRAL: "LinkedIn",
                ASHBY_WORK: "Hybrid", ASHBY_VISA: ["No"], ASHBY_RELOCATE: "Yes",
            }
            packet = kit.build(form, answers).packet
            first = await browser.fill(form, packet)
            again = await browser.fill(form, packet)  # pressed already: not released
            # The second fill ends in the date input (nothing else needed a click), so its
            # calendar is open: it is not a question and changes no question.
            await browser.page.focus(".ashby-application-form-input-date")
            reread = await browser.inspect()
            state = await browser.page.evaluate("""() => ({
              date: document.querySelector('.ashby-application-form-input-date').value,
              popup: !!document.querySelector('.react-datepicker-popper'),
              radios: Array.from(document.querySelectorAll('fieldset input[type=radio]'))
                .filter((r) => r.checked).map((r) => r.labels[0].textContent),
              visa: Array.from(document.querySelectorAll('fieldset input[type=checkbox]'))
                .filter((c) => c.checked).map((c) => c.name),
              pressed: Array.from(document.querySelectorAll('.ashby-application-form-input-yesno button'))
                .map((b) => b.getAttribute('aria-pressed')),
              mirror: document.querySelector('.ashby-application-form-input-yesno input').checked,
            })""")
            return form, first, again, reread, state
        finally:
            await browser.close()

    form, first, again, reread, state = asyncio.run(scenario())
    for result in (first, again):
        statuses = {f.field_id: f.status for f in result.fields}
        # The location lookup is not answered here (see test_custom_widgets_round7.py).
        assert statuses.pop("_systemfield_location") is FieldFillStatus.SKIPPED
        assert statuses == {f.id: FieldFillStatus.FILLED for f in form.fields if f.id != "_systemfield_location"}
    assert state == {"date": "10/15/2026", "popup": True, "radios": ["LinkedIn", "Hybrid"], "visa": ["No"],
                     "pressed": ["true", "false"], "mirror": True}
    assert reread.form is not None and reread.form.fingerprint == form.fingerprint


# --- reduced live shapes ---------------------------------------------------------------------

COMPYL_CHECKBOX_GROUP = """<div id="form">
<div class="ashby-application-form-field-entry"><label for="_systemfield_name">Name</label>
<input id="_systemfield_name" name="_systemfield_name" placeholder="Type here..." required></div>
<fieldset class="ashby-application-form-input-checkbox-group"><label class="ashby-application-form-question-title"
 for="cc031c31-de77-46e1-a7d2-e56bb6505ba5">Will you now or in the future require sponsorship?</label>
<div class="ashby-application-form-input-checkbox-group-option"><span><input type="checkbox"
 id="f0_cc031c31-labeled-checkbox-0" name="Yes"></span><label for="f0_cc031c31-labeled-checkbox-0">Yes</label></div>
<div class="ashby-application-form-input-checkbox-group-option"><span><input type="checkbox"
 id="f0_cc031c31-labeled-checkbox-1" name="No"></span><label for="f0_cc031c31-labeled-checkbox-1">No</label></div>
</fieldset><button type="button">Submit Application</button></div>"""

ASHBY_YES_NO = """<div class="ashby-application-form-field-entry"><label class="ashby-application-form-question-title"
 for="c60ace77-742d-4beb-ab2c-891fffcb05b7">Will you now or in the future require visa sponsorship?</label>
<div class="ashby-application-form-input-yesno"><button type="submit" aria-pressed="false" data-option="yes">Yes</button>
<button type="submit" aria-pressed="false" data-option="no">No</button>
<input type="checkbox" tabindex="-1" name="c60ace77-742d-4beb-ab2c-891fffcb05b7" style="display:none"></div></div>"""

NAME = ('<div><label for="_systemfield_name">Name</label>'
        '<input id="_systemfield_name" name="_systemfield_name" required></div>')

LEVER_CUSTOM = """<form method="post"><ul>
<li class="application-question"><label><div class="application-label">Full name</div>
<div class="application-field"><input type="text" name="name" required></div></label></li>
<li class="application-question"><div class="application-label multiple-select">Pronouns</div>
<div class="application-field"><ul id="pronouns"><div class="column-wrapper"><div class="table-row">
<li class="column"><label><input type="checkbox" name="pronouns" value="He/him"><span>He/him</span></label></li>
<li class="column"><label><input type="checkbox" name="pronouns" value="She/her"><span>She/her</span></label></li>
<li class="column"><label><input type="checkbox" name="pronouns" value="They/them"><span>They/them</span></label></li>
</div><div class="table-row"><li class="column"><label><input type="checkbox" name="customPronounsOption">
<span>Use custom pronouns</span></label></li></div></div></ul>
<input type="text" name="customPronouns" placeholder="Custom pronouns"></div></li>
<li class="application-question custom-question"><div class="application-label">Desired Salary<span class="required">✱</span></div>
<div class="application-field"><input type="text" name="cards[2a269d5e-6f40-4ed1-ae97-47dc53f45611][field0]"
 placeholder="Type your response"></div></li></ul><button type="submit">Submit application</button></form>"""

SEPARATE_CONSENTS = """<form method="post"><label>Full name <input name="full_name" required></label>
<fieldset><legend>Agreements</legend>
<input type="checkbox" id="a" name="terms_consent"><label for="a">I agree to the terms of use</label>
<input type="checkbox" id="b" name="privacy_consent"><label for="b">I agree to the privacy policy</label>
</fieldset><button type="submit">Submit application</button></form>"""

MACHINE_NAMES = """<form method="post"><label>Full name <input name="full_name" required></label>
<div class="row"><input type="text" name="field-123"></div>
<div class="row"><input type="text" name="startDate" placeholder="Pick date..."></div>
<button type="submit">Submit application</button></form>"""


def test_a_checkbox_group_named_by_its_options_is_one_question(options: BrowserOptions) -> None:
    """Compyl (Ashby): the options are name="Yes"/name="No" checkboxes of one fieldset
    whose title labels nothing; they were two questions labelled "Yes" and "No"."""
    form = _inspect(options, html=COMPYL_CHECKBOX_GROUP)
    visa = form.field("cc031c31-de77-46e1-a7d2-e56bb6505ba5")
    assert (visa.label, visa.control_type, _labels(visa)) == (
        "Will you now or in the future require sponsorship?", ControlType.CHECKBOX_GROUP, ["Yes", "No"])
    assert [f.id for f in form.fields] == ["_systemfield_name", visa.id]


@pytest.mark.parametrize("in_form", [False, True])
def test_yes_no_buttons_are_options_only_when_they_cannot_submit(options: BrowserOptions, in_form: bool) -> None:
    """Ashby draws a yes/no question as two type="submit" buttons over a display:none
    checkbox, outside any <form>. Inside a form those buttons would submit it: then they
    are never options (the question is left to the user as before)."""
    body = f"{NAME}{ASHBY_YES_NO}<button type='button'>Submit Application</button>"
    form = _inspect(options, html=f"<form method='post'>{body}</form>" if in_form else f"<div id='form'>{body}</div>")
    sponsorship = [f for f in form.fields if f.semantic_type is SemanticType.SPONSORSHIP]
    if in_form:
        assert sponsorship == []
    else:
        assert [(f.id, f.label, f.control_type, _labels(f)) for f in sponsorship] == [(
            "c60ace77-742d-4beb-ab2c-891fffcb05b7", "Will you now or in the future require visa sponsorship?",
            ControlType.RADIO, ["Yes", "No"])]


def test_lever_questions_before_their_fields(options: BrowserOptions) -> None:
    form = _inspect(options, html=LEVER_CUSTOM)
    _no_machine_labels(form)
    pronouns = form.field("pronouns")
    assert pronouns.label == "Pronouns" and _labels(pronouns) == ["He/him", "She/her", "They/them"]
    salary = form.field("cards[2a269d5e-6f40-4ed1-ae97-47dc53f45611][field0]")
    assert (salary.label, salary.placeholder) == ("Desired Salary", "Type your response")
    assert salary.required


def test_separately_named_consents_in_one_fieldset_stay_separate(options: BrowserOptions) -> None:
    form = _inspect(options, html=SEPARATE_CONSENTS)
    assert [(f.id, f.control_type) for f in form.fields if f.id != "full_name"] == [
        ("terms_consent", ControlType.CHECKBOX), ("privacy_consent", ControlType.CHECKBOX)]


def test_machine_names_and_instructions_are_never_labels(options: BrowserOptions) -> None:
    form = _inspect(options, html=MACHINE_NAMES)
    assert form.field("field-123").label == ""
    start = form.field("startDate")
    assert (start.label, start.placeholder) == ("", "Pick date...")


def _fabric_upload(question: str, required: bool) -> str:
    star, req = ("*", " required") if required else ("", "")
    return (f'<div data-fabric-component="Flex"><p data-fabric-component="BodyText">{question}{star}</p>'
            '<div data-fabric-component="FileUpload"><div data-fabric-component="FileUploadInput">'
            '<div data-fabric-component="FileUploadToggle"><div data-fabric-component="Flex">'
            f'<button type="button"><span>Choose File{star}</span></button><div><p>No file selected</p>'
            '</div></div></div><input accept=".pdf,.doc,.docx,.txt" aria-invalid="false" aria-label="file-input"'
            f' data-bi-id="-file-input" tabindex="-1" type="file"{req}></div>'
            f'<input name="{question[0].lower()}FileId" type="hidden" value=""></div></div>')


def test_an_uploader_named_by_a_developer_token_takes_its_question(options: BrowserOptions) -> None:
    """BambooHR: each file input's accessible name is aria-label="file-input"; its
    question ("Resume*") and state ("No file selected") are in its uploader box."""
    html = ('<form method="post"><label for="f">First Name</label><input id="f" name="firstName" required>'
            + _fabric_upload("Cover Letter", False) + _fabric_upload("Resume", True)
            + '<button type="submit">Submit Application</button></form>')
    form = _inspect(options, html=html)
    files = [(f.label, f.required, f.semantic_type) for f in form.fields if f.control_type is ControlType.FILE]
    assert files == [("Cover Letter", False, SemanticType.COVER_LETTER), ("Resume", True, SemanticType.RESUME)]
    assert all(f.help_text is None for f in form.fields)
