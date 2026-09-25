"""The Workday-style application wizard on the localhost mock ATS (``scripts/mock_workday.py``;
fictional Brambleway Analytics, fictional candidate Avery Quill).

The posting's Apply opens a "Start Your Application" chooser whose manual route is
followed; the account step (Create Account / Sign In) is the person's to complete, in
this browser or in an earlier session of the same persistent profile. Then one
document runs My Information, My Experience, Application Questions, Voluntary
Disclosures, Self Identify and Review: button dropdowns that are probed into selects,
search-on-Enter pickers answered as lookups ("How Did You Hear About Us?", and the
Country Phone Code beside the number), a Month / Day / Year spinbutton date, an error
banner with inline errors, a "page is loaded" announcement and a honeypot for robots.
Real headless Chromium; nothing is ever submitted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest

from interviewmaxxing_browser import (
    PlaywrightSessionFactory,
    SubmissionRefused,
    user_action_needs,
)
from interviewmaxxing_core import (
    AnswerSource,
    AnswerValue,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ArtifactRef,
    BooleanValue,
    BrowserOptions,
    ChoiceValue,
    ControlType,
    FieldFillStatus,
    FileValue,
    MissingInput,
    MissingReason,
    MultiChoiceValue,
    PacketAnswer,
    PageInspection,
    PageKind,
    Provenance,
    SemanticType,
    TextValue,
    UserInput,
)

POSTING = "/jobs/workday-wizard"
CHOOSER = f"{POSTING}/apply"
MANUAL = f"{POSTING}/apply/applyManually"
JOB_CODE = "JR-BWA-201"
EMAIL = "avery.quill@example.test"
PASSWORD = "fixture-password-123"
PROMPT = "source--source"
PHONE_CODE = "phoneNumber--countryPhoneCode"
DATE = "selfIdentifiedDisabilityData--dateSignedOn-dateSectionMonth-input"
STEP_FIELDS: dict[int, list[str]] = {
    1: [PROMPT, "candidateIsPreviousWorker", "country", "firstName", "lastName", "addressLine1",
        "city", "state", "postalCode", "email", "phoneType", PHONE_CODE, "phoneNumber", "extension"],
    2: ["resumeAttachments--attachments-input", "linkedin"],
    3: ["workAuthorization", "sponsorship", "growthYears"],
    4: ["gender", "ethnicity", "veteranStatus", "acceptTerms"],
    5: ["selfIdName", DATE, "disabilityStatus"],
}
"""The questions of each wizard page by ``form.step`` ("current step N of 7" is step N - 1;
step 6 is Review)."""
SAVED_PAGES = ["myInformation", "myExperience", "primaryQuestionnaire", "voluntaryDisclosures",
               "selfIdentify"]

RESUME = object()
"""Marker value: answer the file question with the supplied resume."""
ANSWERS: dict[str, Any] = {
    PROMPT: "LinkedIn", "candidateIsPreviousWorker": "No", "country": "United States of America",
    "firstName": "Avery", "lastName": "Quill", "addressLine1": "1200 Larimer St", "city": "Denver",
    "state": "Colorado", "postalCode": "80202", "email": EMAIL, "phoneType": "Mobile",
    PHONE_CODE: "United States of America (+1)", "phoneNumber": "+1 (303) 555-0142",
    "resumeAttachments--attachments-input": RESUME,
    "linkedin": "https://www.linkedin.example.test/in/avery-quill",
    "workAuthorization": "Yes", "sponsorship": "No", "growthYears": "6-9 years",
    "gender": "I do not wish to self-identify", "ethnicity": "I do not wish to self-identify",
    "veteranStatus": "I am not a protected veteran", "acceptTerms": True,
    "selfIdName": "Avery Quill", DATE: "2026-09-24", "disabilityStatus": ["I do not want to answer"],
}

WD_VALUES = "() => JSON.parse(JSON.stringify(window.__wdState.values))"
POPUP_STATE = """() => {
  const frame = document.querySelector('[data-automation-id=wd-popup-frame]');
  return {open: !!frame, hidden: document.getElementById('root').getAttribute('aria-hidden'),
    apply: document.querySelectorAll('#root [data-automation-id=adventureButton]').length,
    routes: frame ? Array.from(frame.querySelectorAll('a[role=button]'))
      .map((a) => [a.textContent.trim(), new URL(a.href).pathname]) : []};
}"""
"""The in-page "Start Your Application" popup as the page shows it."""
PROMPT_STATE = """() => {
  const input = document.getElementById('source--source');
  const field = input.closest('[data-automation-id=formField-source]');
  return {typed: input.value, chosen: [...window.__wdState.values.source],
    items: Array.from(field.querySelectorAll('[data-automation-id=selectedItem] [data-automation-id=promptOption]'))
      .map((p) => p.textContent),
    popups: document.querySelectorAll('.wd-popup-host').length};
}"""
"""The "How Did You Hear About Us?" picker as the person sees it: the search box, the
chosen items beside it (and in page state), and whether any list is open."""
COUNT_KEYS = """() => {
  window.__keys = 0;
  document.getElementById('source--source').addEventListener('keydown', () => { window.__keys += 1; });
}"""
SEGMENTS = "() => Array.from(document.querySelectorAll('input[role=spinbutton]')).map((e) => e.value)"
NO_AUTO_ADVANCE = """() => {
  // Fixture variant: a date widget that does not move on by itself. Digits stay in the
  // focused segment (its last 2 or 4 kept) and separators are ignored.
  window.__typedInto = [];
  document.addEventListener('keydown', (event) => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement) || input.getAttribute('role') !== 'spinbutton') return;
    if (event.key.length !== 1 || event.ctrlKey || event.metaKey) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (!/^[0-9]$/.test(event.key)) return;
    const size = input.getAttribute('aria-label') === 'Year' ? 4 : 2;
    input.value = (input.value + event.key).slice(-size);
    input.dispatchEvent(new Event('input', {bubbles: true}));
    window.__typedInto.push(input.getAttribute('aria-label'));
  }, true);
}"""


# --- packets and the person's part --------------------------------------------------------


def _value(field: ApplicationField, spec: Any, resume: ArtifactRef) -> AnswerValue:
    if spec is RESUME:
        return FileValue(artifact=resume)
    if field.control_type in (ControlType.TEXT, ControlType.TEXTAREA, ControlType.TYPEAHEAD):
        return TextValue(text=spec)  # a lookup's answer, and a date, are the text to type
    if field.control_type is ControlType.CHECKBOX:
        return BooleanValue(checked=bool(spec))
    options = {o.label: o for o in field.options or []}
    if field.control_type in (ControlType.SELECT, ControlType.RADIO):
        return ChoiceValue(value=options[spec].value, label=spec)
    return MultiChoiceValue(choices=[options[label] for label in spec])


def _packet(form: ApplicationForm, answers: Mapping[str, Any], resume: ArtifactRef) -> ApplicationPacket:
    """Answer ``form`` by field id from the (fictional) person's own input, the file
    question with the supplied resume. Unanswered required questions are listed missing."""
    packet_answers = []
    for field_id, spec in answers.items():
        field = form.field(field_id)
        value = _value(field, spec, resume)
        if spec is RESUME:
            provenance = Provenance(source=AnswerSource.RESUME, reference_ids=[resume.id])
        else:
            user = UserInput.for_field(form, field_id, value)
            provenance = Provenance(source=AnswerSource.USER_INPUT, reference_ids=[user.id])
        packet_answers.append(PacketAnswer(field_id=field_id, semantic_type=field.semantic_type,
                                           value=value, provenance=provenance))
    missing = [MissingInput.for_field(form, f, reason=MissingReason.NO_ANSWER, prompt=f"Answer {f.label}")
               for f in form.fields if f.required and f.id not in answers]
    return ApplicationPacket(
        application_id="app_workday", job_id="job_workday", candidate_id="cand_fixture_avery_quill",
        form_url=form.url, form_step=form.step, form_fingerprint=form.fingerprint,
        answers=packet_answers, missing_inputs=missing)


def _pick(form: ApplicationForm, answers: Mapping[str, Any]) -> dict[str, Any]:
    ids = {f.id for f in form.fields}
    return {k: v for k, v in answers.items() if k in ids}


async def _session(options: BrowserOptions) -> Any:
    return await PlaywrightSessionFactory().start(options)


async def _sign_in(page: Any) -> None:
    """The person's part of the account step: "Sign In", then the fictional account."""
    async with page.expect_navigation():
        await page.click("[data-automation-id=signInLink]")
    await page.fill("[data-automation-id=email]", EMAIL)
    await page.fill("[data-automation-id=password]", PASSWORD)
    async with page.expect_navigation():
        await page.click("[data-automation-id=click_filter]")
    await page.wait_for_function("() => !!window.__wdState")


async def _signed_in(browser: Any, server: Any) -> PageInspection:
    """Open the posting, sign in as the person would, and inspect My Information."""
    page = await browser.open(server.url(POSTING))
    assert page.kind is PageKind.SIGN_IN_REQUIRED, page.kind
    await _sign_in(browser.page)
    inspection: PageInspection = await browser.inspect()
    assert inspection.form is not None and inspection.form.step == 1, inspection.kind
    return inspection


def _form(inspection: PageInspection) -> ApplicationForm:
    assert inspection.form is not None, inspection.kind
    return inspection.form


async def _through(browser: Any, inspection: PageInspection, resume: ArtifactRef, *,
                   until: int) -> PageInspection:
    """Answer and save each page, from the inspected one on, until step ``until`` shows."""
    while inspection.form is not None and inspection.form.step < until:
        form = inspection.form
        fill = await browser.fill(form, _packet(form, _pick(form, ANSWERS), resume))
        assert fill.ok, [f for f in fill.fields if f.status is not FieldFillStatus.FILLED]
        nav = await browser.advance()
        assert nav.advanced, nav.validation_errors
        inspection = nav.inspection
    assert inspection.form is not None and inspection.form.step == until, inspection.kind
    return inspection


def _workday(server: Any) -> dict[str, Any]:
    state: dict[str, Any] = server.api("GET", "/__test__/workday")
    return state


# --- the posting, the chooser and the account step --------------------------------------------


def test_the_posting_and_its_chooser_lead_to_the_account_step(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await _session(options)
        try:
            posting = await browser.observe(server.url(POSTING))
            # Clicked, Apply opens the chooser in the page, as on the live site: the URL
            # stays and the posting goes aria-hidden behind the popup.
            await browser.page.click("[data-automation-id=adventureButton]")
            await browser.page.wait_for_selector("[data-automation-id=wd-popup-frame]")
            popup = await browser.page.evaluate(POPUP_STATE)
            # The popup offers "Autofill with Resume": inspecting declines that offer with the
            # popup's own Close, as any autofill offer, and follows none of its routes.
            in_page = await browser.inspect()
            in_page_url = browser.page.url
            closed = await browser.page.evaluate(POPUP_STATE)
            visits_after_inspect = dict(_workday(server)["route_visits"])
            # Opened as a URL, <posting>/apply is a page of its own (applyAdventurePage).
            chooser = await browser.observe(server.url(CHOOSER))
            chooser_links = [(lk.text, urlsplit(lk.href).path) for lk in browser.last_page.snapshot.links]
            adventure = await browser.page.evaluate(
                "() => !!document.querySelector('[data-automation-id=applyAdventurePage]')")
            page = await browser.open(server.url(POSTING))
            return (posting, popup, in_page, in_page_url, closed, visits_after_inspect, chooser,
                    chooser_links, adventure, page)
        finally:
            await browser.close()

    (posting, popup, in_page, in_page_url, closed, visits_after_inspect, chooser, chooser_links,
     adventure, page) = kit.run(scenario())
    routes = [("Autofill with Resume", f"{POSTING}/apply/autofillWithResume"),
              ("Apply Manually", MANUAL),
              ("Use My Last Application", f"{POSTING}/apply/useMyLastApplication")]
    assert posting.kind is PageKind.JOB_DESCRIPTION and posting.form is None
    assert posting.job_identity is not None and posting.job_identity.external_job_id == JOB_CODE
    # The popup: its three routes, with the posting (and its own Apply link) still in the
    # document behind the aria-hidden root.
    assert popup == {"open": True, "hidden": "true", "apply": 1,
                     "routes": [[text, path] for text, path in routes]}
    assert in_page.kind is PageKind.JOB_DESCRIPTION and in_page.form is None
    assert urlsplit(in_page_url).path == POSTING
    assert closed == {"open": False, "hidden": None, "apply": 1, "routes": []}
    assert visits_after_inspect == {}
    assert chooser.kind is PageKind.JOB_DESCRIPTION and chooser.form is None
    assert chooser.job_identity is not None and chooser.job_identity.external_job_id == JOB_CODE
    assert adventure is True
    assert [link for link in chooser_links if link in routes] == routes
    assert not any(text == "Apply" for text, _ in chooser_links)
    # open() follows Apply, then the chooser's manual route, to the account step.
    assert page.kind is PageKind.SIGN_IN_REQUIRED and page.form is None
    assert urlsplit(page.observed_url).path == MANUAL
    assert page.job_identity is not None and page.job_identity.external_job_id == JOB_CODE
    host = urlsplit(server.origin).netloc
    assert page.message is not None and f"An account on {host} is needed" in page.message
    [need] = user_action_needs(page)
    assert need.reason is MissingReason.USER_ACTION and need.field_id is None
    assert need.prompt == page.message
    # Only the manual route was visited (autofill would upload and parse the resume first).
    assert _workday(server)["route_visits"] == {"applyManually": 1}


def test_the_sign_in_view_of_the_account_step_still_needs_the_person(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, bool]:
        browser = await _session(options)
        try:
            first = await browser.open(server.url(POSTING))
            async with browser.page.expect_navigation():
                await browser.page.click("[data-automation-id=signInLink]")
            password = await browser.page.is_visible("[data-automation-id=password]")
            return first, await browser.inspect(), password
        finally:
            await browser.close()

    first, signin, password = kit.run(scenario())
    host = urlsplit(server.origin).netloc
    assert first.kind is PageKind.SIGN_IN_REQUIRED
    assert password and signin.kind is PageKind.SIGN_IN_REQUIRED and signin.form is None
    assert signin.observed_url.endswith(f"{MANUAL}?view=signin")
    # "Don't have an account yet? Create Account" is still offered: an account is needed.
    assert signin.message is not None and f"An account on {host} is needed" in signin.message
    assert [n.prompt for n in user_action_needs(signin)] == [signin.message]


# --- My Information -------------------------------------------------------------------------


def test_my_information_is_inspected_with_probed_dropdowns_and_no_honeypot(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await _session(options)
        try:
            inspection = await _signed_in(browser, server)
            buttons = {b.selector: b.text for b in browser.last_page.snapshot.buttons}
            shown_code = browser.last_page.bindings[PHONE_CODE].value
            page = await browser.page.evaluate(
                "() => ({announce: [document.getElementById('wd-announce').getAttribute('role'), "
                "document.getElementById('wd-announce').textContent], "
                "honeypot: !!document.querySelector('input[name=website]#wd-beecatcher'), "
                "code: window.__wdState.values.countryPhoneCode})")
            return inspection, buttons, shown_code, page, list(browser.menus.log)
        finally:
            await browser.close()

    inspection, buttons, shown_code, page, log = kit.run(scenario())
    assert inspection.kind is PageKind.APPLICATION_FORM, inspection.message
    form = inspection.form
    assert form is not None and form.step == 1
    assert [f.id for f in form.fields] == STEP_FIELDS[1]
    source = form.field(PROMPT)
    assert source.control_type is ControlType.TYPEAHEAD and source.required
    assert source.semantic_type is SemanticType.REFERRAL_SOURCE
    assert source.label == "How Did You Hear About Us?"
    menus = {
        "country": ["United States of America", "Canada", "United Kingdom", "Germany"],
        "state": ["California", "Colorado", "New York", "Texas", "Washington"],
        "phoneType": ["Mobile", "Home", "Work"],
    }
    for field_id, labels in menus.items():
        field = form.field(field_id)
        assert field.control_type is ControlType.SELECT and field.required, field_id
        assert [o.label for o in field.options or []] == labels, field_id
    # Country Phone Code is a single-choice picker (a lookup) that already holds the
    # tenant's country; it asks for the phone's country. The device type is its own choice.
    code = form.field(PHONE_CODE)
    assert code.control_type is ControlType.TYPEAHEAD and code.required
    assert code.semantic_type is SemanticType.COUNTRY and code.label == "Country Phone Code"
    assert page["code"] == ["United States of America (+1)"]
    assert shown_code == "United States of America (+1)"
    assert form.field("phoneType").semantic_type is SemanticType.CUSTOM_SELECT
    assert form.field("phoneNumber").semantic_type is SemanticType.PHONE
    extension = form.field("extension")
    assert extension.semantic_type is SemanticType.CUSTOM_TEXT and not extension.required
    # The "for robots only" honeypot is in the document but never a question.
    assert page["honeypot"]
    assert not {"website", "wd-beecatcher", "beecatcher"} & {f.id for f in form.fields}
    assert not any("robots" in f.label for f in form.fields)
    # The "page is loaded" announcement is an alert, not an error.
    assert page["announce"] == ["alert", "My Information page is loaded"]
    assert form.page_errors == []
    # Only "Save and Continue" moves on; the header's buttons are neither next nor submit.
    assert form.is_final_step is False and form.submit_selector is None
    assert form.next_selector is not None and buttons[form.next_selector] == "Save and Continue"
    header = [s for s, text in buttons.items() if text in ("English", "Sign In", "Search for Jobs")]
    assert len(header) == 3 and form.next_selector not in header
    # Probed once on this step: the pickers are lookups, the dropdowns are selects.
    assert [(selector, kind) for selector, kind, _ in log] == [
        ("#source--source", "lookup"), ("#country--country", "select"),
        ("#address--countryRegion", "select"), ("#phoneNumber--phoneType", "select"),
        ("#phoneNumber--countryPhoneCode", "lookup")]


def test_my_information_fills_with_a_national_phone_and_is_saved(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, resume: ArtifactRef
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await _session(options)
        try:
            form = _form(await _signed_in(browser, server))
            fill = await browser.fill(form, _packet(form, _pick(form, ANSWERS), resume))
            phone = await browser.page.evaluate(
                "() => document.getElementById('phoneNumber--phoneNumber').value")
            prompt = await browser.page.evaluate(PROMPT_STATE)
            values = await browser.page.evaluate(WD_VALUES)
            nav = await browser.advance()
            return fill, phone, prompt, values, nav
        finally:
            await browser.close()

    fill, phone, prompt, values, nav = kit.run(scenario())
    statuses = {f.field_id: f.status for f in fill.fields}
    assert statuses == {**dict.fromkeys(STEP_FIELDS[1], FieldFillStatus.FILLED),
                        "extension": FieldFillStatus.SKIPPED}, fill.fields
    assert fill.ok and fill.page_errors == []
    # Country Phone Code holds +1, so the number is typed without it.
    assert phone == "(303) 555-0142"
    # The prompt holds LinkedIn as its one item; its search box is empty and closed.
    assert prompt == {"typed": "", "chosen": ["LinkedIn"], "items": ["LinkedIn"], "popups": 0}
    assert values["countryPhoneCode"] == ["United States of America (+1)"]
    assert values["country"] == "wd_country_1" and values["phoneType"] == "wd_phonetype_1"
    assert values["state"] == "wd_state_2" and values["candidateIsPreviousWorker"] == "false"
    assert values["extension"] == ""
    assert nav.advanced, nav.validation_errors
    assert nav.inspection.form is not None and nav.inspection.form.step == 2
    assert [f.id for f in nav.inspection.form.fields] == STEP_FIELDS[2]
    wd = _workday(server)
    [save] = wd["saves"]
    assert save["page"] == "myInformation" and save["ok"] and save["errors"] == {}
    assert save["values"]["phoneNumber"] == "(303) 555-0142" and save["values"]["source"] == ["LinkedIn"]
    assert wd["submit_call_count"] == 0 and wd["accepted_count"] == 0


def test_a_required_question_left_missing_is_rejected_by_the_site(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, resume: ArtifactRef
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await _session(options)
        try:
            form = _form(await _signed_in(browser, server))
            answers = {k: v for k, v in _pick(form, ANSWERS).items() if k != "city"}
            packet = _packet(form, answers, resume)
            fill = await browser.fill(form, packet)
            nav = await browser.advance()
            return packet, fill, nav, await browser.inspect()
        finally:
            await browser.close()

    packet, fill, nav, again = kit.run(scenario())
    assert [m.field_id for m in packet.missing_inputs] == ["city"]  # the packet fits the form
    statuses = {f.field_id: f.status for f in fill.fields}
    assert statuses["city"] is FieldFillStatus.SKIPPED and fill.ok
    assert nav.advanced is False
    assert any("The field City is required and must have a value" in e for e in nav.validation_errors), \
        nav.validation_errors
    for inspection in (nav.inspection, again):
        form = inspection.form
        assert form is not None and form.step == 1 and [f.id for f in form.fields] == STEP_FIELDS[1]
        assert "required" in (form.field("city").validation_error or "")
        assert [f.id for f in form.fields if f.validation_error] == ["city"]
        assert any(e.startswith("Errors Found (1)") for e in form.page_errors), form.page_errors
    wd = _workday(server)
    [save] = wd["saves"]
    assert save["page"] == "myInformation" and not save["ok"] and list(save["errors"]) == ["city"]
    assert wd["accepted_count"] == 0 and wd["submit_call_count"] == 0


# --- the "How Did You Hear About Us?" prompt ---------------------------------------------------


@pytest.mark.parametrize(("typed", "suggestions", "detail"), [
    ("Job Fair", [], "no suggestions"),
    # Search lists the leaves in which every typed word begins a word: "or", "Other".
    ("O", ["Friend or Family", "Other"], "no suggestion matches"),
], ids=["no-results", "several-results"])
def test_a_prompt_search_without_one_match_needs_a_choice(
    typed: str, suggestions: list[str], detail: str,
    kit: SimpleNamespace, server: Any, options: BrowserOptions, resume: ArtifactRef,
) -> None:
    async def scenario() -> tuple[Any, Any]:
        browser = await _session(options)
        try:
            form = _form(await _signed_in(browser, server))
            fill = await browser.fill_fields(form, _packet(form, {PROMPT: typed}, resume), [PROMPT])
            return fill, await browser.page.evaluate(PROMPT_STATE)
        finally:
            await browser.close()

    fill, prompt = kit.run(scenario())
    [result] = fill.fields
    assert result.status is FieldFillStatus.NEEDS_CHOICE, result
    assert result.suggestions == suggestions
    assert detail in (result.detail or ""), result.detail
    # Nothing was chosen, the search box is empty again and its list is closed.
    assert prompt == {"typed": "", "chosen": [], "items": [], "popups": 0}


def test_a_prompt_already_holding_the_answer_is_left_alone(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, resume: ArtifactRef
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await _session(options)
        try:
            form = _form(await _signed_in(browser, server))
            packet = _packet(form, {PROMPT: "LinkedIn"}, resume)
            first = await browser.fill_fields(form, packet, [PROMPT])
            chosen = await browser.page.evaluate(PROMPT_STATE)
            await browser.page.evaluate(COUNT_KEYS)
            again = await browser.fill_fields(form, packet, [PROMPT])
            return first, chosen, again, await browser.page.evaluate(PROMPT_STATE), \
                await browser.page.evaluate("() => window.__keys")
        finally:
            await browser.close()

    first, chosen, again, kept, keys = kit.run(scenario())
    holding = {"typed": "", "chosen": ["LinkedIn"], "items": ["LinkedIn"], "popups": 0}
    assert first.fields[0].status is FieldFillStatus.FILLED, first.fields
    assert chosen == holding
    [result] = again.fields
    assert result.status is FieldFillStatus.FILLED, result
    # Not typed into or searched: no second item, and the chosen one is not taken out.
    assert kept == holding and keys == 0


async def _search_again(browser: Any, server: Any, resume: ArtifactRef,
                        before: list[str]) -> tuple[Any, ...]:
    """The picker holds ``before`` (chosen by earlier fills); with a single item the
    person also left a search in the box. Then "LinkedIn" is filled once more: its
    result is found already chosen."""
    form = _form(await _signed_in(browser, server))
    setup = [(await browser.fill_fields(form, _packet(form, {PROMPT: item}, resume), [PROMPT])).fields[0]
             for item in before]
    if len(before) == 1:
        await browser.page.fill(f"#{PROMPT}", "Linked")
    held = await browser.page.evaluate(PROMPT_STATE)
    fill = await browser.fill_fields(form, _packet(form, {PROMPT: "LinkedIn"}, resume), [PROMPT])
    return setup, held, fill.fields[0], await browser.page.evaluate(PROMPT_STATE)


@pytest.mark.parametrize(("before", "status"), [
    # The search in the box is not the answer: the result, already chosen, is not
    # clicked (that would un-choose it) and the picker holds exactly the answer.
    (["LinkedIn"], FieldFillStatus.FILLED),
    # The picker also holds another item: the answer alone is not what it holds, as the
    # fill that clicked LinkedIn beside Indeed reported for the very same picker.
    (["Indeed", "LinkedIn"], FieldFillStatus.VERIFICATION_MISMATCH),
], ids=["leftover-search", "another-item"])
def test_a_prompt_result_already_chosen_is_not_clicked_and_the_picker_is_read_back(
    before: list[str], status: FieldFillStatus,
    kit: SimpleNamespace, server: Any, options: BrowserOptions, resume: ArtifactRef,
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await _session(options)
        try:
            return await _search_again(browser, server, resume, before)
        finally:
            await browser.close()

    setup, held, result, after = kit.run(scenario())
    assert held["chosen"] == before
    if len(before) == 2:
        assert [r.status for r in setup] == [FieldFillStatus.FILLED, FieldFillStatus.VERIFICATION_MISMATCH]
        assert "Indeed" in (setup[1].detail or "") and "LinkedIn" in (setup[1].detail or "")
    assert result.status is status, (result, after)
    # Nothing chosen twice or taken out, and the list is closed again.
    assert after["chosen"] == before and after["items"] == before and after["popups"] == 0


def test_a_prompt_answer_found_already_chosen_leaves_no_search_text_behind(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, resume: ArtifactRef
) -> None:
    """The site empties the search box when a result is chosen, and the runtime empties
    it when nothing matches; when the result is found already chosen (and rightly not
    clicked), the text typed to find it is not left in the box either."""
    async def scenario() -> tuple[Any, ...]:
        browser = await _session(options)
        try:
            return await _search_again(browser, server, resume, ["LinkedIn"])
        finally:
            await browser.close()

    _, _, result, after = kit.run(scenario())
    assert result.status is FieldFillStatus.FILLED, result
    assert after == {"typed": "", "chosen": ["LinkedIn"], "items": ["LinkedIn"], "popups": 0}


# --- Self Identify and Review ---------------------------------------------------------------


def test_self_identify_dates_are_typed_into_month_day_year_segments(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, resume: ArtifactRef
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await _session(options)
        try:
            form = _form(await _through(browser, await _signed_in(browser, server), resume, until=5))
            filled = []
            for text in ("2026-09-24", "9/24/2026", "September 24, 2026"):
                for segment in await browser.page.query_selector_all("input[role=spinbutton]"):
                    await segment.fill("")
                fill = await browser.fill_fields(form, _packet(form, {DATE: text}, resume), [DATE])
                filled.append((text, fill.fields[0], await browser.page.evaluate(SEGMENTS),
                               (await browser.page.evaluate(WD_VALUES))["selfIdDate"]))
            wrong = await browser.fill_fields(form, _packet(form, {DATE: "13/40/2026"}, resume), [DATE])
            left = await browser.page.evaluate(SEGMENTS)
            # A widget that does not move on by itself: typed segment by segment instead.
            await browser.page.evaluate(NO_AUTO_ADVANCE)
            for segment in await browser.page.query_selector_all("input[role=spinbutton]"):
                await segment.fill("")
            fallback = await browser.fill_fields(form, _packet(form, {DATE: "2026-09-24"}, resume), [DATE])
            return (form, filled, wrong.fields[0], left, fallback.fields[0],
                    await browser.page.evaluate(SEGMENTS), await browser.page.evaluate("() => window.__typedInto"))
        finally:
            await browser.close()

    form, filled, wrong, left, fallback, segments, typed_into = kit.run(scenario())
    assert form.step == 5 and [f.id for f in form.fields] == STEP_FIELDS[5]
    date = form.field(DATE)
    assert date.control_type is ControlType.TEXT and date.input_type == "date"
    assert date.placeholder == "MM/DD/YYYY" and date.label == "Date" and date.required
    for text, result, shown, value in filled:
        assert result.status is FieldFillStatus.FILLED, (text, result)
        assert shown == ["09", "24", "2026"] and value == "09/24/2026", text
    assert wrong.status is FieldFillStatus.FAILED and "is not a date" in (wrong.detail or ""), wrong
    assert left == ["09", "24", "2026"]  # nothing was typed for the impossible date
    assert fallback.status is FieldFillStatus.FILLED, fallback
    assert segments == ["09", "24", "2026"]
    # The whole date went into Month first; then each segment was typed on its own.
    assert typed_into == ["Month"] * 8 + ["Month"] * 2 + ["Day"] * 2 + ["Year"] * 4


def test_review_is_final_and_is_neither_advanced_nor_submitted(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, resume: ArtifactRef
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await _session(options)
        try:
            review = await _through(browser, await _signed_in(browser, server), resume, until=6)
            with pytest.raises(SubmissionRefused, match="submits the application"):
                await browser.advance()
            return review, await browser.prepare_review()
        finally:
            await browser.close()

    review, prepared = kit.run(scenario())
    assert review.kind is PageKind.APPLICATION_FORM
    form = review.form
    assert form is not None and form.step == 6 and form.fields == []
    assert form.is_final_step is True and form.submit_selector and form.next_selector is None
    assert prepared.kind is PageKind.APPLICATION_FORM
    assert prepared.form is not None and prepared.form.page_errors == []
    wd = _workday(server)
    assert wd["submit_call_count"] == 0 and wd["accepted_count"] == 0
    # The manual route only: the open, the sign-in view and the return after signing in.
    assert wd["route_visits"] == {"applyManually": 3}
    assert [(s["page"], s["ok"]) for s in wd["saves"]] == [(p, True) for p in SAVED_PAGES]
    pages = wd["drafts"][EMAIL]["pages"]
    assert pages["myInformation"]["phoneNumber"] == "(303) 555-0142"
    assert pages["myInformation"]["source"] == ["LinkedIn"]
    assert pages["selfIdentify"]["selfIdDate"] == "09/24/2026"
    assert wd["drafts"][EMAIL]["resume"]["filename"] == kit.RESUME_PATH.name


# --- the person signs in --------------------------------------------------------------------


def test_a_persistent_profile_keeps_the_sign_in_for_the_next_session(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, tmp_path: Path
) -> None:
    profile = replace(options, profile_dir=tmp_path / "profile", headless=True)

    async def first_session() -> Any:
        browser = await _session(profile)
        try:
            return await _signed_in(browser, server)
        finally:
            await browser.close()

    async def next_session() -> tuple[Any, list[str]]:
        browser = await _session(profile)
        try:
            page = await browser.open(server.url(POSTING))
            return page, [c["name"] for c in await browser.page.context.cookies()]
        finally:
            await browser.close()

    signed_in = kit.run(first_session())
    assert signed_in.kind is PageKind.APPLICATION_FORM
    page, cookies = kit.run(next_session())
    assert "bwa_wd_session" in cookies
    # Straight to My Information: no account step in between.
    assert page.kind is PageKind.APPLICATION_FORM, page.message
    assert page.form is not None and page.form.step == 1
    assert [f.id for f in page.form.fields] == STEP_FIELDS[1]
    assert urlsplit(page.observed_url).path == MANUAL and not urlsplit(page.observed_url).query
    assert page.job_identity is not None and page.job_identity.external_job_id == JOB_CODE
    # Session one: the open, the sign-in view and the return; session two: the open only.
    assert _workday(server)["route_visits"] == {"applyManually": 4}


@pytest.mark.parametrize("signed_in_first", [
    pytest.param(False, id="signs-in-while-waiting"),
    pytest.param(True, id="signed-in-before"),
])
def test_waiting_for_the_person_returns_once_they_have_signed_in(
    signed_in_first: bool, kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await _session(options)
        loop = asyncio.get_running_loop()
        waiter: asyncio.Task[Any] | None = None
        try:
            page = await browser.open(server.url(POSTING))
            if signed_in_first:
                await _sign_in(browser.page)
                started = loop.time()
                return page, True, await browser.wait_for_user("sign in", timeout_s=30), loop.time() - started
            waiter = asyncio.create_task(browser.wait_for_user("sign in", timeout_s=30))
            await asyncio.sleep(1.0)
            still_waiting = not waiter.done()  # the account step is still shown
            await _sign_in(browser.page)
            started = loop.time()
            return page, still_waiting, await waiter, loop.time() - started
        finally:
            if waiter is not None and not waiter.done():
                waiter.cancel()
            await browser.close()

    page, still_waiting, after, seconds = kit.run(scenario())
    assert page.kind is PageKind.SIGN_IN_REQUIRED
    assert still_waiting  # it does not return while the account step is shown
    assert after.kind is PageKind.APPLICATION_FORM, after.message
    assert after.form is not None and after.form.step == 1
    # My Information's dropdowns were not probed while waiting; that is the runtime's own
    # work, done once the person is through, and never what the wait waited for.
    assert seconds < 10, seconds
    assert after.form.field("country").control_type is ControlType.SELECT
    assert after.form.field(PROMPT).control_type is ControlType.TYPEAHEAD
