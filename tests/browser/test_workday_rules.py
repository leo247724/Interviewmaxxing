"""Workday rules: sign-in wording, the apply-route chooser, segmented dates, dial codes,
honeypots, announcements, step-scoped menu probing, the inspector's Workday shapes, the
mock wizard's server contract and the session's blocked hosts (no LinkedIn traffic).

Mostly pure tests on synthetic ``DomSnapshot``s; a few HTTP checks against the
localhost mock (``scripts/mock_workday.py``), two ``set_content`` pages on about:blank
and two sessions on the mock whose blocked hosts are a never-resolving ``.test`` name or
loopback. No real host is contacted; LinkedIn URLs appear only as strings. All people and
employers are fictional.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import Message
from http.cookies import SimpleCookie
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import async_playwright

from interviewmaxxing_browser.aria import MenuObservation, MenuProbe, _classify, _meaningful
from interviewmaxxing_browser.needs import user_action_needs
from interviewmaxxing_browser.normalize import build_page, sign_in_message
from interviewmaxxing_browser.semantics import classify
from interviewmaxxing_browser.session import (
    BLOCKED_HOSTS,
    PlaywrightSessionFactory,
    blocked_request_pattern,
)
from interviewmaxxing_browser.signals import (
    APPLY_LINK,
    MANUAL_APPLY,
    date_segment_values,
    lookup_matches,
    national_number,
)
from interviewmaxxing_browser.snapshot import DomSnapshot, inspector_script
from interviewmaxxing_core import (
    BrowserOptions,
    ControlType,
    MissingReason,
    PageKind,
    SemanticType,
)

WORKDAY_APPLY = (
    "https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site/job/X/apply/applyManually"
)
WIZARD_URL = "http://127.0.0.1:8123/jobs/workday-wizard/apply/applyManually"
"""A mock-like wizard address for synthetic snapshots (never opened)."""


# --- synthetic snapshots ------------------------------------------------------------------


def _control(**overrides: Any) -> dict[str, Any]:
    """A raw ``DomControl``: a visible, labelled native text input outside any form
    (Workday renders no <form>), unless overridden."""
    raw: dict[str, Any] = {
        "kind": "native", "tag": "input", "type": "text", "name": "", "id": "", "selector": "",
        "role": "", "autocomplete_list": False, "label": "", "label_source": "label",
        "described": [], "error_message": "", "legend": None, "legend_selector": None,
        "legend_described": [], "group_label": None, "group_described": [], "adjacent": [],
        "adjacent_errors": [], "label_selector": None, "required": False, "disabled": False,
        "visible": True, "label_visible": True, "readonly": False, "value": "", "checked": False,
        "files": [], "placeholder": "", "autocomplete": "", "accept": "", "max_length": None,
        "multiple": False, "options": [], "invalid": False, "image_alts": [], "form_index": -1,
        "has_value": False,
    }
    raw.update(overrides)
    raw["selector"] = raw["selector"] or f"#{raw['id'] or raw['name']}"
    return raw


def _text(name: str, label: str, **overrides: Any) -> dict[str, Any]:
    return _control(**{"name": name, "id": name, "label": label, **overrides})


def _select(name: str, label: str, labels: list[str], **overrides: Any) -> dict[str, Any]:
    options = [{"value": f"wd_{name}_{i}", "label": text, "disabled": False, "selected": False}
               for i, text in enumerate(labels, start=1)]
    return _control(tag="select", type="select", name=name, id=name, label=label,
                    options=options, **overrides)


def _button(text: str, **overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "text": text, "type": "button", "selector": f"#button-{uuid.uuid4().hex[:8]}",
        "disabled": False, "form_index": -1, "submits_form": False, "form_no_validate": False,
        "effective_method": "", "effective_action": "",
    }
    raw.update(overrides)
    return raw


def _link(text: str, href: str) -> dict[str, Any]:
    return {"text": text, "href": href, "selector": f"a[href='{href}']"}


def _snapshot(url: str = WIZARD_URL, **overrides: Any) -> DomSnapshot:
    raw: dict[str, Any] = {
        "url": url, "title": "Brambleway Analytics Careers", "headings": [], "regions": [],
        "body_text": "", "record_members": [], "ld_json": [],
        "meta": {"og_site_name": "", "og_title": ""}, "forms": [], "controls": [],
        "buttons": [], "links": [], "step": None, "password_visible": False,
        "captcha_frames": [], "captcha_tokens": [], "captcha_widget": False,
    }
    raw.update(overrides)
    return DomSnapshot.model_validate(raw)


NAMES = [_text("firstName", "First Name", required=True),
         _text("lastName", "Last Name", required=True)]
SAVE = _button("Save and Continue")


# --- 1. sign-in pages ---------------------------------------------------------------------


def test_workday_sign_in_names_the_tenant_and_says_an_account_is_needed() -> None:
    snapshot = _snapshot(
        WORKDAY_APPLY,
        password_visible=True,
        controls=[
            _text("email", "Email Address", required=True, autocomplete="email"),
            _control(type="password", name="password", id="input-5", label="Password"),
            _control(type="password", name="verifyPassword", id="input-6",
                     label="Verify New Password"),
            _control(type="checkbox", name="createAccountCheckbox", id="input-9",
                     label="I acknowledge and agree to the Candidate Privacy Notice"),
        ],
        buttons=[_button("Sign In"), _button("Create Account", type="role-button")],
    )
    model = build_page(snapshot)

    assert model.inspection.kind is PageKind.SIGN_IN_REQUIRED
    assert model.form is None
    message = model.inspection.message
    assert message == sign_in_message(snapshot)
    for part in ("Workday account", "salesforce", "salesforce.wd12.myworkdayjobs.com", "needed"):
        assert part in message
    needs = user_action_needs(model.inspection)
    assert [(n.reason, n.label, n.field_id, n.prompt) for n in needs] == [
        (MissingReason.USER_ACTION, "Sign in", None, message)]


@pytest.mark.parametrize(("offers", "expected"), [
    ({"buttons": [_button("Create Account")]}, "An account on careers.example.test is needed"),
    ({"links": [_link("Sign up", "https://careers.example.test/signup")]},
     "An account on careers.example.test is needed"),
    ({"headings": [{"level": 2, "text": "Register"}]},
     "An account on careers.example.test is needed"),
    ({"buttons": [_button("Sign In")],
      "links": [_link("Forgot your password?", "https://careers.example.test/forgot")]},
     "Sign in to careers.example.test"),
])
def test_other_sign_in_pages_name_their_host(offers: dict[str, Any], expected: str) -> None:
    snapshot = _snapshot("https://careers.example.test/login", password_visible=True, **offers)
    inspection = build_page(snapshot).inspection

    assert inspection.kind is PageKind.SIGN_IN_REQUIRED
    assert inspection.message is not None and inspection.message.startswith(expected)
    assert "Workday" not in inspection.message
    assert [n.prompt for n in user_action_needs(inspection)] == [inspection.message]


@pytest.mark.parametrize("url", [
    "https://wd1.myworkdaysite.com/en-US/recruiting/brambleway/External/job/X/apply/applyManually",
    "https://wd1.myworkdaysite.com/recruiting/brambleway/External/job/X/apply/applyManually",
])
def test_myworkdaysite_sign_in_names_the_tenant_from_the_path(url: str) -> None:
    message = sign_in_message(_snapshot(url, password_visible=True))

    assert message.startswith(
        "A Workday account for brambleway (wd1.myworkdaysite.com/recruiting/brambleway) is needed")
    assert "create one for brambleway" in message
    assert "for wd1" not in message


def test_myworkdayjobs_tenant_is_the_host_even_with_a_recruiting_site() -> None:
    url = ("https://brambleway.wd5.myworkdayjobs.com/recruiting/job/Denver/"
           "Growth-Marketing-Manager_JR-BWA-201/apply/applyManually")
    message = sign_in_message(_snapshot(url, password_visible=True))

    assert message.startswith(
        "A Workday account for brambleway (brambleway.wd5.myworkdayjobs.com) is needed")


# --- 2. apply-route wording ---------------------------------------------------------------


RAQUO = "\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK}"
RSAQUO = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}"
ARROW = "\N{RIGHTWARDS ARROW}"


@pytest.mark.parametrize("text", [
    "Apply", "Apply now", f"Apply Now {RAQUO}", "Apply here", "Apply online", "Apply today",
    "Apply Manually", f"Apply manually {RAQUO}", "Apply for this job", "Apply to the role",
    "Apply for this position", "Start your application", "Begin Application",
    "Continue to application", "Continue to your application", "I'm interested",
    f"Apply {RSAQUO}", f"Apply {ARROW}", "Apply >",
])
def test_apply_link_wording(text: str) -> None:
    assert APPLY_LINK.search(text)


@pytest.mark.parametrize("text", [
    "Autofill with Resume", "Use My Last Application", "Apply with LinkedIn",
    "Apply using Indeed", "Apply via Seek", "Submit application", "Submit",
])
def test_apply_link_excludes_third_party_and_submit_routes(text: str) -> None:
    assert not APPLY_LINK.search(text)


@pytest.mark.parametrize(("text", "manual"), [
    ("Apply Manually", True),
    (f"Apply manually {RAQUO}", True),
    ("apply manually", True),
    (f"Apply Manually {RSAQUO}", True),
    ("Manually apply", True),
    ("Manual application", True),
    ("Apply", False),
    ("Apply now", False),
    ("Autofill with Resume", False),
    ("Use My Last Application", False),
    ("Apply with LinkedIn", False),
    ("Start your application", False),
])
def test_manual_apply_is_only_the_manual_route(text: str, manual: bool) -> None:
    assert bool(MANUAL_APPLY.search(text)) is manual


# --- 3. dates and national numbers --------------------------------------------------------

MDY = ("month", "day", "year")
DMY = ("day", "month", "year")
MY = ("month", "year")


@pytest.mark.parametrize(("text", "kinds", "expected"), [
    # ISO, and year first with other separators
    ("2026-09-24", MDY, ["09", "24", "2026"]),
    ("2026-9-4", MDY, ["09", "04", "2026"]),
    ("2026/09/24", MDY, ["09", "24", "2026"]),
    ("2026.09.24", MDY, ["09", "24", "2026"]),
    # the widget's own order
    ("09/24/2026", MDY, ["09", "24", "2026"]),
    ("9/24/2026", MDY, ["09", "24", "2026"]),
    # month names
    ("September 24, 2026", MDY, ["09", "24", "2026"]),
    ("Sept 24 2026", MDY, ["09", "24", "2026"]),
    ("24 September 2026", MDY, ["09", "24", "2026"]),
    ("May 5, 2026", MDY, ["05", "05", "2026"]),
    ("Feb 29, 2028", MDY, ["02", "29", "2028"]),
    # not a calendar date
    ("2026-02-30", MDY, None),
    ("02/30/2026", MDY, None),
    ("Feb 29, 2027", MDY, None),
    ("2026-13-01", MDY, None),
    ("13/01/2026", MDY, None),
    # two-digit years are never guessed
    ("09/24/26", MDY, None),
    ("September 24, 26", MDY, None),
    ("26-09-24", MDY, None),
    # incomplete or not a date
    ("2026-09", MDY, None),
    ("", MDY, None),
    ("next Monday", MDY, None),
    # month/year widgets
    ("2026-09", MY, ["09", "2026"]),
    ("09/2026", MY, ["09", "2026"]),
    ("September 2026", MY, ["09", "2026"]),
    ("2026-09-24", MY, None),
    ("09/26", MY, None),
    # day-first widgets
    ("24/09/2026", DMY, ["24", "09", "2026"]),
    ("24.09.2026", DMY, ["24", "09", "2026"]),
    ("2026-09-24", DMY, ["24", "09", "2026"]),
    ("24 September 2026", DMY, ["24", "09", "2026"]),
    ("09/24/2026", DMY, None),
    # year-first widgets
    ("2026-09-24", ("year", "month", "day"), ["2026", "09", "24"]),
])
def test_date_segment_values(text: str, kinds: tuple[str, ...], expected: list[str] | None) -> None:
    assert date_segment_values(text, kinds) == expected


@pytest.mark.parametrize(("phone", "code", "expected"), [
    ("+1 (303) 555-0142", "1", "(303) 555-0142"),
    ("+13035550142", "1", "3035550142"),
    ("+1-303-555-0142", "1", "303-555-0142"),
    ("+44 20 7946 0958", "44", "20 7946 0958"),
    ("+44 20 7946 0958", "1", None),
    ("(303) 555-0142", "1", None),
    ("303-555-0142", "1", None),
    ("+1 (303) 555-0142", None, None),
    ("+1 (303) 555-0142", "", None),
    ("+1", "1", None),
])
def test_national_number(phone: str, code: str | None, expected: str | None) -> None:
    assert national_number(phone, code) == expected


# --- 4. semantic types of Workday's labels ------------------------------------------------

@pytest.mark.parametrize(("label", "control", "input_type", "name", "element_id", "expected"), [
    # Workday's My Information page, labels and ids as they appear
    ("Country Phone Code", ControlType.SELECT, None, "countryPhoneCode",
     "phoneNumber--countryPhoneCode", SemanticType.COUNTRY),
    ("Phone Device Type", ControlType.SELECT, None, "phoneType", "phoneNumber--phoneType",
     SemanticType.CUSTOM_SELECT),
    ("Phone Extension", ControlType.TEXT, "text", "extension", "phoneNumber--extension",
     SemanticType.CUSTOM_TEXT),
    ("Phone Number", ControlType.TEXT, "text", "phoneNumber", "phoneNumber--phoneNumber",
     SemanticType.PHONE),
    ("How Did You Hear About Us?", ControlType.TYPEAHEAD, None, "", "source--source",
     SemanticType.REFERRAL_SOURCE),
    ("Country", ControlType.SELECT, None, "country", "country--country", SemanticType.COUNTRY),
    # the same wording without Workday's ids
    ("Country Phone Code", ControlType.SELECT, None, "", "", SemanticType.COUNTRY),
    ("Phone Device Type", ControlType.SELECT, None, "", "", SemanticType.CUSTOM_SELECT),
    ("Phone Extension", ControlType.TEXT, "text", "", "", SemanticType.CUSTOM_TEXT),
    # existing phone labels
    ("Phone", ControlType.TEXT, "tel", "phone", "phone", SemanticType.PHONE),
    ("Phone", ControlType.TEXT, "text", "", "", SemanticType.PHONE),
    ("Mobile phone", ControlType.TEXT, "text", "", "", SemanticType.PHONE),
    ("Phone number", ControlType.TEXT, "tel", "", "", SemanticType.PHONE),
    ("Telephone", ControlType.TEXT, "text", "", "", SemanticType.PHONE),
    ("Cell", ControlType.TEXT, "text", "", "", SemanticType.PHONE),
    # a number that may include an extension is still the number
    ("Phone number (include extension)", ControlType.TEXT, "text", "", "", SemanticType.PHONE),
    # an extension box is never the number, whatever its input type
    ("Phone Extension", ControlType.TEXT, "tel", "", "", SemanticType.CUSTOM_TEXT),
    ("Ext.", ControlType.TEXT, "tel", "", "", SemanticType.CUSTOM_TEXT),
    ("Work Phone Extension", ControlType.TEXT, "text", "", "", SemanticType.CUSTOM_TEXT),
    ("Home Phone Ext.", ControlType.TEXT, "tel", "", "", SemanticType.CUSTOM_TEXT),
    ("Business Phone Extension", ControlType.TEXT, "text", "", "", SemanticType.CUSTOM_TEXT),
    ("Phone Number Extension", ControlType.TEXT, "text", "", "", SemanticType.CUSTOM_TEXT),
])
def test_classify_workday_phone_and_source_labels(
    label: str, control: ControlType, input_type: str | None, name: str, element_id: str,
    expected: SemanticType,
) -> None:
    assert classify(label=label, name=name, element_id=element_id, input_type=input_type,
                    control_type=control) is expected


# --- 5. honeypots and zero-box text inputs ------------------------------------------------

HONEYPOT_WORDING = "Enter website. This input is for robots only, do not enter if you're human."


@pytest.mark.parametrize("honeypot", [
    _text("website", HONEYPOT_WORDING, id="wd-beecatcher", selector="#wd-beecatcher"),
    _text("website", "Website", adjacent=["This input is for robots only."]),
    _text("website", "Website", described=[
        {"text": "If you are human, leave this field blank.", "error": False}]),
], ids=["label", "adjacent", "described"])
def test_visible_honeypot_and_zero_box_text_inputs_are_not_fields(honeypot: dict[str, Any]) -> None:
    assert honeypot["visible"]  # a honeypot someone rendered with a box of its own
    model = build_page(_snapshot(controls=[
        *NAMES,
        honeypot,
        _text("preferredName", "Preferred Name", visible=False, label_visible=True),
        _control(tag="textarea", type="textarea", name="notes", id="notes", label="Notes",
                 visible=False, label_visible=True),
        _control(type="checkbox", name="acceptTerms", id="acceptTerms", required=True,
                 label="Yes, I have read and consent to the terms and conditions",
                 visible=False, label_visible=True),
        _control(type="file", name="resume", id="resume-input", label="Resume/CV",
                 accept=".pdf,.doc,.docx", visible=False, label_visible=False),
    ], buttons=[SAVE]))

    assert model.inspection.kind is PageKind.APPLICATION_FORM
    assert model.form is not None
    fields = {f.id: f.control_type for f in model.form.fields}
    assert fields == {
        "firstName": ControlType.TEXT,
        "lastName": ControlType.TEXT,
        "acceptTerms": ControlType.CHECKBOX,  # styled choices hide their inputs
        "resume": ControlType.FILE,  # so do styled uploaders
    }


def test_a_visible_website_question_is_still_a_field() -> None:
    model = build_page(_snapshot(controls=[*NAMES, _text("website", "Website")], buttons=[SAVE]))

    assert model.form is not None
    assert model.form.field("website").semantic_type is SemanticType.WEBSITE


# --- 6. announcements ---------------------------------------------------------------------

ERRORS_FOUND = ("Errors Found (1) Error - First Name: The field First Name is required and "
                "must have a value.")


def test_page_loaded_announcements_are_not_page_errors() -> None:
    model = build_page(_snapshot(controls=NAMES, buttons=[SAVE], regions=[
        {"role": "alert", "text": "My Information page is loaded"},
        {"role": "alert", "text": "Growth Marketing Manager page is loaded."},
        {"role": "alert", "text": ERRORS_FOUND},
    ]))

    assert model.form is not None
    assert model.form.page_errors == [ERRORS_FOUND]


# --- 7. country phone code pickers --------------------------------------------------------

PHONE_TYPE = _select("phoneType", "Phone Device Type", ["Mobile", "Home", "Work"])
PHONE_CODE = _select("countryPhoneCode", "Country Phone Code",
                     ["United States of America (+1)", "Canada (+1)", "United Kingdom (+44)"])
PHONE_NUMBER = _text("phoneNumber", "Phone Number", required=True)
EXTENSION = _text("extension", "Phone Extension")


def _phone_model(*controls: dict[str, Any]) -> Any:
    model = build_page(_snapshot(controls=[*NAMES, *controls], buttons=[SAVE]))
    assert model.form is not None
    return model


def test_phone_number_is_bound_to_the_country_phone_code_just_before_it() -> None:
    model = _phone_model(PHONE_TYPE, PHONE_CODE, PHONE_NUMBER, EXTENSION)  # Workday's order

    assert model.form.field("countryPhoneCode").semantic_type is SemanticType.COUNTRY
    assert model.form.field("phoneNumber").semantic_type is SemanticType.PHONE
    assert model.bindings["phoneNumber"].dial_code_field == "countryPhoneCode"
    assert {k: b.dial_code_field for k, b in model.bindings.items() if k != "phoneNumber"} == {
        "firstName": None, "lastName": None, "phoneType": None, "countryPhoneCode": None,
        "extension": None}


def test_a_picker_two_questions_back_still_binds() -> None:
    model = _phone_model(PHONE_CODE, PHONE_TYPE, PHONE_NUMBER)

    assert model.bindings["phoneNumber"].dial_code_field == "countryPhoneCode"


@pytest.mark.parametrize("controls", [
    [PHONE_CODE, PHONE_TYPE, EXTENSION, PHONE_NUMBER],  # three questions back
    [_select("country", "Country", ["United States of America", "Canada"]), PHONE_NUMBER],
    [PHONE_CODE, _text("phoneNumber", "Phone Number", type="tel", phone_picker="iti")],
], ids=["three-back", "not-a-dial-code", "international-phone"])
def test_phone_number_without_its_own_dial_code_picker(controls: list[dict[str, Any]]) -> None:
    model = _phone_model(*controls)

    assert model.bindings["phoneNumber"].dial_code_field is None


def test_an_international_phone_keeps_its_country_in_the_number() -> None:
    model = _phone_model(PHONE_CODE, _text("phoneNumber", "Phone Number", type="tel",
                                           phone_picker="iti"))

    assert model.form.field("phoneNumber").expects_international_phone


PHONE_CODE_ITEMS = ["United States of America (+1)", "United States Minor Outlying Islands (+1)",
                    "Canada (+1)"]


@pytest.mark.parametrize(("typed", "expected"), [
    ("United States", [0]),
    ("USA", [0]),
    ("Canada", [2]),
    ("Canada +1", [2]),
    ("United States of America (+1)", [0]),  # the item itself
])
def test_lookup_matches_ignore_a_trailing_dial_code(typed: str, expected: list[int]) -> None:
    assert lookup_matches(typed, PHONE_CODE_ITEMS) == expected


# --- 8. menu probing is scoped to the wizard's step ---------------------------------------

DOCUMENT = f"1790000000000.5 {WIZARD_URL}"
STATES = [("wd_state_1", "California"), ("wd_state_2", "Colorado"), ("wd_state_3", "New York")]
MENU_FACTS = {"combo": 1, "role": "", "haspopup": "listbox", "autocomplete": "",
              "editable": False, "multiselectable": False, "dialog": False, "value": "",
              "expanded": False}


def _step_snapshot(current: int | None) -> DomSnapshot:
    menu = _control(kind="custom", tag="button", type="button", name="state",
                    id="address--countryRegion", label="State", label_visible=False,
                    required=True, aria=dict(MENU_FACTS))
    step = None if current is None else {"current": current, "total": 7, "source": "text"}
    return _snapshot(controls=[*NAMES, menu], buttons=[SAVE], step=step, document=DOCUMENT)


def _state_field(snapshot: DomSnapshot) -> Any:
    form = build_page(snapshot).form
    assert form is not None
    return form.field("state")


def test_menu_probe_page_is_the_document_and_its_step() -> None:
    step2, step3 = _step_snapshot(2), _step_snapshot(3)

    assert MenuProbe.page(step2) == f"{DOCUMENT} step 2/7"
    assert MenuProbe.page(step3) == f"{DOCUMENT} step 3/7"
    assert MenuProbe.page(_step_snapshot(None)) == DOCUMENT
    other = step2.model_copy(update={"document": f"1790000009999.5 {WIZARD_URL}"})
    assert MenuProbe.page(other) != MenuProbe.page(step2)


def test_menu_observations_are_dropped_on_a_step_change() -> None:
    step2, step3 = _step_snapshot(2), _step_snapshot(3)
    menu = next(c for c in step2.controls if c.id == "address--countryRegion")
    probe = MenuProbe()

    assert probe.targets(step2, -1) == []  # not this probe's page yet
    assert probe.merge(step2) is step2
    assert probe.document == MenuProbe.page(step2)
    assert [c.id for c in probe.targets(step2, -1)] == ["address--countryRegion"]

    probe.observations[MenuProbe.key(menu)] = MenuObservation(
        "select", selector="#address--countryRegion", origin="1790000000000.5", url=WIZARD_URL,
        control={"tag": "BUTTON", "id": "address--countryRegion"},
        options=tuple({"label": label, "value": value, "disabled": False, "index": i,
                       "selected": False} for i, (value, label) in enumerate(STATES)),
        close_method="escape")
    probe.probes, probe.seconds, probe.closer = 1, 0.4, "escape"
    probe.confirm("#address--countryRegion", "wd_state_2")

    merged = probe.merge(step2)  # same step: the observation is reused
    aria = next(c for c in merged.controls if c.id == "address--countryRegion").aria
    assert aria is not None and aria["probed"] and aria["kind"] == "select"
    assert [o["value"] for o in aria["options"]] == [v for v, _ in STATES]
    assert _state_field(merged).control_type is ControlType.SELECT
    assert probe.targets(step2, -1) == []
    assert probe.targets(step3, -1) == []  # never probed against another step's cache

    unmerged = probe.merge(step3)  # a new step of the same document
    assert probe.document == MenuProbe.page(step3) != MenuProbe.page(step2)
    assert (probe.observations, probe.confirmed, probe.probes, probe.seconds, probe.stopped,
            probe.closer) == ({}, {}, 0, 0.0, "", "")
    assert next(c for c in unmerged.controls if c.id == "address--countryRegion").aria == MENU_FACTS
    assert _state_field(unmerged).control_type is ControlType.UNSUPPORTED
    assert [c.id for c in probe.targets(step3, -1)] == ["address--countryRegion"]


def test_short_generated_listbox_ids_constrain_nothing() -> None:
    assert MenuObservation(kind="select", listbox_id="cq4q3").listbox_id_pattern == ""
    assert MenuObservation(kind="select", listbox_id="").listbox_id_pattern == ""
    pattern = MenuObservation(kind="select", listbox_id="react-select-3-listbox").listbox_id_pattern
    assert re.fullmatch(pattern, "react-select-17-listbox")
    assert not re.fullmatch(pattern, "react-select-x-listbox")


def test_select_one_is_no_option() -> None:
    select_one = {"label": "Select One", "value": "", "disabled": True}
    blank = {"label": "None", "value": "", "disabled": False}
    retired = {"label": "Retired", "value": "wd_retired", "disabled": True}
    yes = {"label": "Yes", "value": "wd_yn_1", "disabled": False}

    assert _meaningful([select_one, blank, yes, retired]) == [blank, yes, retired]


def _menu_option(index: int, label: str, value: str, disabled: bool = False) -> dict[str, Any]:
    return {"index": index, "id": value or "select-one", "label": label, "value": value,
            "disabled": disabled, "selected": False, "nested": False}


def test_a_workday_dropdown_probes_as_a_select_without_select_one() -> None:
    options = [_menu_option(0, "Select One", "", disabled=True),
               _menu_option(1, "Yes", "wd_yn_1"), _menu_option(2, "No", "wd_yn_2")]
    opened = {"expanded": True, "menu": {"id": "cq4q3", "visible": True, "count": 3,
                                         "options": options}}
    before = {"origin": "1790000000000.5", "url": WIZARD_URL, "control": {"tag": "BUTTON"},
              "display": "", "editable": False}

    observation = _classify(opened, before, "#primaryQuestionnaire--q1", "click")
    assert observation.kind == "select"
    assert [(o["label"], o["value"]) for o in observation.options] == [
        ("Yes", "wd_yn_1"), ("No", "wd_yn_2")]
    assert (observation.listbox_id, observation.listbox_id_pattern) == ("cq4q3", "")

    only = {"expanded": True, "menu": {**opened["menu"], "count": 1, "options": options[:1]}}
    assert _classify(only, before, "#primaryQuestionnaire--q1", "click").kind == "unobservable"

    picker = _classify({"expanded": False, "menu": None},
                       {**before, "control": {"tag": "INPUT"}, "editable": True, "picker": True},
                       "#source--source", "click")
    assert (picker.kind, picker.multi) == ("lookup", True)


# --- 9. the inspector on Workday's shapes (about:blank, no network) ------------------------

SR_ONLY = ("position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);"
           "white-space:nowrap")
PROGRESS_HTML = f"""<!doctype html><title>Growth Marketing Manager - Application</title>
<style>.sr{{{SR_ONLY}}}</style>
<div aria-label="Application Progress"><ol>
<li><label class="sr" aria-live="polite">completed step 1 of 7</label><label>Create Account/Sign In</label></li>
<li><label class="sr" aria-live="polite">current step 2 of 7</label><label>My Information</label></li>
<li><label class="sr" aria-live="polite">step 3 of 7</label><label>My Experience</label></li>
<li><label class="sr" aria-live="polite">step 4 of 7</label><label>Application Questions</label></li>
<li><label class="sr" aria-live="polite">step 5 of 7</label><label>Voluntary Disclosures</label></li>
<li><label class="sr" aria-live="polite">step 6 of 7</label><label>Self Identify</label></li>
<li><label class="sr" aria-live="polite">step 7 of 7</label><label>Review</label></li>
</ol></div>
<h2>My Information</h2>
<div><label for="firstName">First Name</label><input id="firstName" name="firstName" type="text"></div>
<div><label for="lastName">Last Name</label><input id="lastName" name="lastName" type="text"></div>
<button type="button">Save and Continue</button>"""

INLINE_PROGRESS_HTML = """<!doctype html><title>Growth Marketing Manager - Application</title>
<ol>
<li><span>completed step 1 of 7</span><span>Create Account/Sign In</span></li>
<li><span>current step 2 of 7</span><span>My Information</span></li>
<li><span>step 3 of 7</span><span>My Experience</span></li>
</ol>
<div><label for="firstName">First Name</label><input id="firstName" name="firstName" type="text"></div>
<div><label for="lastName">Last Name</label><input id="lastName" name="lastName" type="text"></div>
<button type="button">Save and Continue</button>"""
"""Inline step labels: the page text runs "current step 2 of 7My Information"."""

WIDGETS_HTML = """<!doctype html><title>Self Identify</title>
<div class="field">
 <label for="month">Date</label>
 <div class="date">
  <input type="text" role="spinbutton" id="month" aria-label="Month" placeholder="MM" aria-required="true">
  <span aria-hidden="true">/</span>
  <input type="text" role="spinbutton" id="day" aria-label="Day" placeholder="DD" aria-required="true">
  <span aria-hidden="true">/</span>
  <input type="text" role="spinbutton" id="year" aria-label="Year" placeholder="YYYY" aria-required="true">
  <button type="button" aria-label="Calendar">&#128197;</button>
 </div>
</div>
<div class="field">
 <label for="x">State</label>
 <div><button type="button" aria-haspopup="listbox" id="x">Select One</button></div>
</div>
<button type="button">Save and Continue</button>"""


PICKERS_HTML = """<!doctype html><title>My Information</title>
<div class="field">
 <label for="source--source">How Did You Hear About Us?</label>
 <div>
  <input type="text" id="source--source" placeholder="Search" aria-describedby="source-count">
  <span id="source-count" aria-hidden="true" hidden>0 items selected</span>
 </div>
</div>
<div class="field">
 <label for="referral--source">Referral Source</label>
 <div>
  <ul role="listbox" aria-label="items selected">
   <div role="option" aria-label="LinkedIn, press delete to clear value." aria-selected="false">
    <span>LinkedIn</span><span aria-hidden="true">&#215;</span>
   </div>
  </ul>
  <input type="text" id="referral--source" placeholder="Search" aria-describedby="referral-count">
  <span id="referral-count" aria-hidden="true" hidden>1 item selected, LinkedIn</span>
 </div>
</div>
<div class="field"><label for="country--country">Country</label>
 <button type="button" aria-haspopup="listbox" id="country--country"
  aria-label="Country United States Required">United States</button></div>
<div class="field"><label for="state">State<abbr aria-hidden="true">*</abbr></label>
 <button type="button" aria-haspopup="listbox" id="state" aria-label="State Select One">Select One</button></div>
<div class="field"><label for="phoneType">Phone Device Type</label>
 <button type="button" aria-haspopup="listbox" id="phoneType"
  aria-label="Phone Device Type Select One">Select One</button></div>
<div class="field"><label for="narrow">Nickname</label>
 <input type="text" id="narrow" style="width:1px;padding:0;border:0"></div>
<div class="field"><label for="flat">Preferred Name</label>
 <input type="text" id="flat" style="height:1px;padding:0;border:0"></div>
<div class="field"><label for="city">City</label><input type="text" id="city"></div>
<button type="button">Save and Continue</button>"""
"""Workday's prompt pickers (a search box described by a selection count, its chosen items
in a listbox beside it), menu buttons that say "Required" only in their name or label, and
text boxes squeezed below 2 px (the honeypot's shape)."""


async def _inspect_all(*documents: str) -> list[DomSnapshot]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            snapshots = []
            for html in documents:
                await page.set_content(html)
                assert page.url == "about:blank"
                snapshots.append(DomSnapshot.model_validate(await page.evaluate(inspector_script())))
            return snapshots
        finally:
            await browser.close()


async def _inspect(html: str) -> DomSnapshot:
    return (await _inspect_all(html))[0]


def test_inspector_reads_the_current_step_from_named_progress_steps() -> None:
    separate, inline = asyncio.run(_inspect_all(PROGRESS_HTML, INLINE_PROGRESS_HTML))

    assert "current step 2 of 7My Information" in inline.body_text
    for snapshot in (separate, inline):
        assert snapshot.step is not None
        assert (snapshot.step.current, snapshot.step.total, snapshot.step.source) == (2, 7, "text")
        assert MenuProbe.page(snapshot).endswith(" step 2/7")
        model = build_page(snapshot)
        assert model.form is not None and model.form.step == 1  # zero-based


def test_inspector_workday_pickers_required_menus_and_tiny_boxes() -> None:
    snapshot = asyncio.run(_inspect(PICKERS_HTML))
    controls = {c.id: c for c in snapshot.controls}

    # the chosen-items listbox and its option are part of the picker, not questions
    assert sorted(controls) == ["city", "country--country", "flat", "narrow", "phoneType",
                                "referral--source", "source--source", "state"]
    empty, chosen = controls["source--source"], controls["referral--source"]
    for picker in (empty, chosen):
        assert picker.aria is not None
        assert (picker.aria["combo"], picker.aria["picker"], picker.aria["expanded"]) == (1, True, False)
        assert MenuProbe.candidate(picker, picker.form_index)
    assert (empty.aria or {})["value"] == ""
    assert (chosen.aria or {})["value"] == "LinkedIn"  # no "press delete" state, no charm

    assert controls["country--country"].label == "Country"
    assert [controls[k].required for k in ("country--country", "state", "phoneType")] == [
        True, True, False]  # "... Required" name, a starred label, neither

    assert [controls[k].visible for k in ("narrow", "flat", "city")] == [False, False, True]
    model = build_page(snapshot)
    assert model.form is not None
    ids = {f.id for f in model.form.fields}
    assert "city" in ids and not {"narrow", "flat"} & ids


def test_inspector_segmented_date_and_listbox_menu_button() -> None:
    snapshot = asyncio.run(_inspect(WIDGETS_HTML))
    controls = {c.id: c for c in snapshot.controls}

    assert sorted(controls) == ["month", "x"]  # day and year belong to the date
    date = controls["month"]
    assert (date.kind, date.label, date.label_source) == ("native", "Date", "label")
    assert [s["kind"] for s in date.date_segments] == ["month", "day", "year"]
    assert [s["selector"] for s in date.date_segments] == ["#month", "#day", "#year"]
    assert (date.placeholder, date.value, date.required) == ("MM/DD/YYYY", "", True)

    menu = controls["x"]
    assert (menu.kind, menu.tag, menu.type, menu.label) == ("custom", "button", "button", "State")
    assert menu.aria is not None
    assert (menu.aria["combo"], menu.aria["haspopup"], menu.aria["value"]) == (1, "listbox", "")
    assert MenuProbe.candidate(menu, menu.form_index)
    texts = [b.text for b in snapshot.buttons]
    assert "Select One" not in texts and "Save and Continue" in texts

    model = build_page(snapshot)
    assert model.form is not None
    assert model.form.field("month").input_type == "date"
    assert model.bindings["month"].date_segments == (
        ("month", "#month"), ("day", "#day"), ("year", "#year"))
    assert model.form.field("x").control_type is ControlType.UNSUPPORTED
    assert model.form.field("x").label == "State"


# --- 10. the mock wizard's server contract ------------------------------------------------

SLUG = "workday-wizard"
EMAIL = "avery.wizard@example.test"
PASSWORD = "Brambleway-Fixture-1!"
PHONE = "(303) 555-0142"
DATE = "09/24/2026"
TEXT_ANSWERS = {
    "firstName": "Avery", "lastName": "Quill", "addressLine1": "12 Fictional Way",
    "city": "Denver", "postalCode": "80202", "email": EMAIL, "phoneNumber": PHONE,
    "extension": "", "linkedin": "", "selfIdName": "Avery Quill",
}


def _valid_pages(kit: SimpleNamespace) -> dict[str, dict[str, Any]]:
    """A valid answer to every wizard question but the résumé, page by page, from the
    mock's own catalog (``PAGES``): fictional text, and the first listed option of every
    choice (its opaque ``wd_*`` value, or a prompt's item; prompts and checkgroups take a
    list). A new required text question fails here, loudly."""
    wd = kit.mock_ats.WD
    pages: dict[str, dict[str, Any]] = {page_id: {} for page_id in wd.PAGE_IDS}
    for question in wd.field_catalog():
        kind, name = question["kind"], question["name"]
        first = question["options"][0]["value"] if question["options"] else None
        if kind == "file":
            continue
        if kind == "text":
            value: Any = TEXT_ANSWERS[name]
        elif kind == "date":
            value = DATE
        elif kind == "checkbox":
            value = True
        elif kind in ("prompt", "checkgroup"):
            value = [first]
        else:
            value = first
        pages[question["page"]][name] = value
    return pages


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # the test reads the 303 itself


_OPENER = urllib.request.build_opener(_NoRedirect())


def _http(server: Any, method: str, path: str, *, body: bytes | None = None,
          content_type: str = "", cookie: str = "") -> tuple[int, Message, bytes]:
    headers = {k: v for k, v in (("Content-Type", content_type), ("Cookie", cookie)) if v}
    request = urllib.request.Request(server.url(path), data=body, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        try:
            return error.code, error.headers, error.read()
        finally:
            error.close()


def _account(server: Any, **form: str) -> tuple[int, Message, str]:
    status, headers, body = _http(server, "POST", f"/jobs/{SLUG}/account",
                                  body=urllib.parse.urlencode(form).encode(),
                                  content_type="application/x-www-form-urlencoded")
    return status, headers, body.decode()


def _session(headers: Message) -> str:
    cookies = SimpleCookie()
    for header in headers.get_all("Set-Cookie") or []:
        cookies.load(header)
    return f"bwa_wd_session={cookies['bwa_wd_session'].value}"


def _create_account(server: Any) -> str:
    status, headers, _ = _account(server, mode="create", email=EMAIL, password=PASSWORD,
                                  verifyPassword=PASSWORD, createAccountCheckbox="on")
    assert status == 303
    return _session(headers)


def _wizard(server: Any, action: str, payload: dict[str, Any], cookie: str) -> tuple[int, Any]:
    status, _, body = _http(server, "POST", f"/jobs/{SLUG}/wizard/{action}",
                            body=json.dumps(payload).encode(), content_type="application/json",
                            cookie=cookie)
    return status, json.loads(body)


def _upload(server: Any, cookie: str, filename: str, data: bytes) -> tuple[int, Any]:
    boundary = f"imx-{uuid.uuid4().hex}"
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n')
    body = head.encode() + data + f"\r\n--{boundary}--\r\n".encode()
    status, _, raw = _http(server, "POST", f"/jobs/{SLUG}/wizard/upload", body=body,
                           content_type=f"multipart/form-data; boundary={boundary}", cookie=cookie)
    return status, json.loads(raw)


def _counts(server: Any) -> tuple[int, int]:
    summary = server.submissions(SLUG)
    return summary["accepted_count"], summary["rejected_count"]


def test_catalog_lists_the_formless_signed_in_wizard(server: Any) -> None:
    job = next(j for j in server.api("GET", "/__test__/jobs")["jobs"] if j["job_id"] == SLUG)

    assert (job["formless"], job["requires_signin"], job["multistep"]) == (True, True, True)
    assert [s["title"] for s in job["steps"]] == [
        "My Information", "My Experience", "Application Questions", "Voluntary Disclosures",
        "Self Identify"]
    fields = {f["name"]: f for s in job["steps"] for f in s["fields"]}
    assert {"countryPhoneCode", "phoneNumber", "selfIdDate", "resume"} <= set(fields)
    selects = [f for f in fields.values() if f["kind"] == "select"]
    assert selects and all(o["value"].startswith("wd_") for f in selects for o in f["options"])


def test_apply_routes_are_recorded_and_signed_out_routes_show_the_account_step(
    server: Any,
) -> None:
    status, _, body = _http(server, "GET", f"/jobs/{SLUG}/apply/autofillWithResume")

    assert status == 200
    assert "Create Account" in body.decode() and 'name="website"' in body.decode()
    summary = server.api("GET", "/__test__/workday")
    assert summary["route_visits"] == {"autofillWithResume": 1}
    assert (summary["accepted_count"], summary["submit_call_count"]) == (0, 0)


@pytest.mark.parametrize("action", ["save", "upload", "submit"])
def test_wizard_requests_without_a_session_are_refused(server: Any, action: str) -> None:
    status, payload = _wizard(server, action, {"page": "myInformation", "values": {}}, cookie="")

    assert (status, payload) == (401, {"error": "Sign in to continue."})
    assert _counts(server) == (0, 0)


def test_creating_an_account_signs_in_and_opens_the_wizard(server: Any) -> None:
    status, headers, _ = _account(server, mode="create", email=EMAIL, password=PASSWORD,
                                  verifyPassword=PASSWORD, createAccountCheckbox="on")

    assert status == 303
    assert headers["Location"] == f"/jobs/{SLUG}/apply/applyManually"
    set_cookie = headers["Set-Cookie"]
    assert set_cookie.startswith("bwa_wd_session=") and "HttpOnly" in set_cookie
    assert server.api("GET", "/__test__/workday")["accounts"] == [EMAIL]
    status, _, page = _http(server, "GET", f"/jobs/{SLUG}/apply/applyManually",
                            cookie=_session(headers))
    assert status == 200 and "window.__WD_BOOT" in page.decode()
    assert "createAccountSubmitButton" not in page.decode()


def test_account_errors_are_422_pages_without_a_session(server: Any) -> None:
    status, headers, page = _account(server, mode="create", email=EMAIL, password=PASSWORD,
                                     verifyPassword=PASSWORD + "x", createAccountCheckbox="on")
    assert status == 422 and "The passwords do not match." in page
    assert headers.get("Set-Cookie") is None
    assert server.api("GET", "/__test__/workday")["accounts"] == []

    _create_account(server)
    status, headers, page = _account(server, mode="signin", email=EMAIL, password="wrong-password")
    assert status == 422 and "Wrong email address or password." in page
    assert headers.get("Set-Cookie") is None

    status, headers, _ = _account(server, mode="signin", email=EMAIL, password=PASSWORD)
    assert status == 303 and headers["Set-Cookie"].startswith("bwa_wd_session=")


def test_a_missing_required_answer_is_rejected_and_recorded(
    server: Any, kit: SimpleNamespace,
) -> None:
    cookie = _create_account(server)
    values = {**_valid_pages(kit)["myInformation"], "lastName": ""}

    status, payload = _wizard(server, "save", {"page": "myInformation", "values": values}, cookie)

    assert status == 422
    assert payload == {"errors": {
        "lastName": "The field Last Name is required and must have a value."}}
    summary = server.submissions(SLUG)
    assert (summary["accepted_count"], summary["rejected_count"]) == (0, 1)
    assert summary["rejections"][0]["errors"] == payload["errors"]
    assert summary["rejections"][0]["step"] == 2  # the account step is step 1


@pytest.mark.parametrize(("phone", "error"), [
    ("+1 (303) 555-0142", "Phone Number: enter the number without the country phone code; "
                          "choose it in Country Phone Code."),
    ("+13035550142", "Phone Number: enter the number without the country phone code; "
                     "choose it in Country Phone Code."),
    ("303-555-014", "Phone Number: enter a 10-digit phone number."),
    ("(303) 555-0142", None),
    ("303.555.0142", None),
])
def test_phone_number_is_saved_without_its_country_code(
    server: Any, kit: SimpleNamespace, phone: str, error: str | None,
) -> None:
    cookie = _create_account(server)
    values = {**_valid_pages(kit)["myInformation"], "phoneNumber": phone}

    status, payload = _wizard(server, "save", {"page": "myInformation", "values": values}, cookie)

    if error is None:
        assert (status, payload) == (200, {"ok": True})
        assert _counts(server) == (0, 0)
    else:
        assert (status, payload) == (422, {"errors": {"phoneNumber": error}})
        assert _counts(server) == (0, 1)


@pytest.mark.parametrize(("code", "error"), [
    (["United States of America (+1)"], None),
    (["United States of America (+1)", "Canada (+1)"],  # one item only
     "Country Phone Code: select an item from the list."),
    (["wd_phonecode_1"],  # a prompt keeps its items' labels, not option values
     "Country Phone Code: select an item from the list."),
    ([], "The field Country Phone Code is required and must have a value."),
])
def test_country_phone_code_is_a_single_item_prompt(
    server: Any, kit: SimpleNamespace, code: list[str], error: str | None,
) -> None:
    cookie = _create_account(server)
    values = {**_valid_pages(kit)["myInformation"], "countryPhoneCode": code}

    status, payload = _wizard(server, "save", {"page": "myInformation", "values": values}, cookie)

    if error is None:
        assert (status, payload) == (200, {"ok": True})
    else:
        assert (status, payload) == (422, {"errors": {"countryPhoneCode": error}})


def test_only_submit_after_every_page_records_an_application(
    server: Any, kit: SimpleNamespace,
) -> None:
    cookie = _create_account(server)

    status, payload = _wizard(server, "submit", {}, cookie)
    assert status == 422 and "Complete every page" in payload["error"]
    assert _counts(server) == (0, 0)

    status, upload = _upload(server, cookie, "avery-quill-resume.pdf", kit.RESUME_PATH.read_bytes())
    assert status == 200 and upload["filename"] == "avery-quill-resume.pdf"

    pages = _valid_pages(kit)
    *first, last = pages
    for page_id in first:
        assert _wizard(server, "save", {"page": page_id, "values": pages[page_id]},
                       cookie) == (200, {"ok": True})
    status, payload = _wizard(server, "submit", {}, cookie)
    assert (status, payload) == (
        422, {"error": f"Complete every page before submitting: {last}"})
    assert _counts(server) == (0, 0)

    assert _wizard(server, "save", {"page": last, "values": pages[last]},
                   cookie) == (200, {"ok": True})
    assert _counts(server) == (0, 0)  # saving every page submits nothing

    status, payload = _wizard(server, "submit", {}, cookie)
    assert status == 200
    reference = payload["reference"]
    assert re.fullmatch(r"BWA-\d{6}", reference)
    summary = server.submissions(SLUG)
    assert (summary["accepted_count"], summary["rejected_count"]) == (1, 0)
    record = summary["submissions"][0]
    assert record["confirmation_reference"] == reference
    information = pages["myInformation"]
    assert record["fields"]["phoneNumber"] == PHONE
    assert record["fields"]["countryPhoneCode"] == information["countryPhoneCode"]
    assert record["fields"]["selfIdDate"] == DATE
    assert record["files"]["resume"]["filename"] == "avery-quill-resume.pdf"
    assert server.api("GET", "/__test__/workday")["submit_call_count"] == 3

    status, _, page = _http(server, "GET", f"/jobs/{SLUG}/wizard/submitted?ref={reference}")
    assert status == 200 and reference in page.decode()


# --- 11. blocked hosts: no LinkedIn traffic (URLs below are strings, never opened) ---------

@pytest.mark.parametrize(("url", "blocked"), [
    ("https://www.linkedin.com/li/track", True),
    ("https://static.licdn.com/x", True),
    ("https://linkedin.com", True),
    ("https://applywithlinkedin.myworkdaygadgets.com/awli/", True),
    ("https://user@www.linkedin.com:443/x", True),
    ("HTTPS://WWW.LINKEDIN.COM/", True),
    ("https://evil.example.test@www.linkedin.com/", True),  # the host is LinkedIn's
    ("https://evil.com/?u=linkedin.com", False),
    ("https://notlinkedin.com/", False),
    ("https://linkedin.com.evil.net/", False),
    ("https://www.linkedin.example.test/in/a", False),
    ("https://www.linkedin.com@evil.example.test/", False),  # the host is not LinkedIn's
    ("https://other.myworkdaygadgets.com/x", False),
    ("http://127.0.0.1:5000/jobs", False),
    ("https://www.linkedin.com./li/track", True),  # a fully qualified name is the same host
    ("https://www.linkedin.com.:443/x", True),
    ("https://linkedin.com.evil.net./", False),
])
def test_blocked_request_pattern(url: str, blocked: bool) -> None:
    assert bool(blocked_request_pattern(BLOCKED_HOSTS).match(url)) is blocked


def test_sessions_block_linkedin_by_default() -> None:
    hosts = PlaywrightSessionFactory().blocked_hosts

    assert hosts == BLOCKED_HOSTS
    assert {"linkedin.com", "licdn.com"} <= set(hosts)


BLOCKED_BY_CLIENT = ("net::ERR_BLOCKED_BY_CLIENT", "net::ERR_BLOCKED_BY_CLIENT.Inspector")
"""Chromium's text for a request the client aborted: a document gets the bare code, a
subresource (fetch, image, beacon) the ``.Inspector`` variant. An unblocked request to a
``.test`` host fails with ``net::ERR_NAME_NOT_RESOLVED`` instead."""


def test_a_session_aborts_requests_to_its_blocked_hosts(
    kit: SimpleNamespace, server: Any, options: BrowserOptions,
) -> None:
    async def scenario() -> tuple[Any, str, str | None, list[tuple[str, str | None]]]:
        browser = await PlaywrightSessionFactory(blocked_hosts=("blocked.example.test",)).start(options)
        try:
            page = browser.page
            failures: list[tuple[str, str | None]] = []
            page.on("requestfailed", lambda request: failures.append((request.url, request.failure)))
            inspection = await browser.open(server.url("/jobs/standard"))
            async with page.expect_event(
                "requestfailed", predicate=lambda request: "blocked.example.test" in request.url,
            ) as event:
                fetched = await page.evaluate(
                    "() => fetch('http://blocked.example.test/x')"
                    ".then(() => 'fetched', (error) => String(error))")
            request = await event.value
            return inspection, fetched, request.failure, failures
        finally:
            await browser.close()

    inspection, fetched, failure, failures = kit.run(scenario())

    assert inspection.kind is PageKind.APPLICATION_FORM  # the local mock itself is not blocked
    assert fetched.startswith("TypeError")
    assert failure in BLOCKED_BY_CLIENT
    assert [f for f in failures if "blocked.example.test" in f[0]] == [
        ("http://blocked.example.test/x", failure)]
    assert not [f for f in failures if f[0].startswith(server.origin)]


def test_a_session_opens_no_websocket_to_its_blocked_hosts(
    kit: SimpleNamespace, server: Any, options: BrowserOptions,
) -> None:
    """Loopback only: the page is on 127.0.0.1, the blocked host is ``localhost`` and a
    local listener records every request line that reaches it."""

    async def scenario() -> tuple[str, list[str]]:
        arrived: list[str] = []

        async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            arrived.append((await reader.readline()).decode().strip())
            writer.close()

        listener = await asyncio.start_server(accept, "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        browser = await PlaywrightSessionFactory(blocked_hosts=("localhost",)).start(options)
        try:
            await browser.page.goto(server.url("/jobs/standard"))
            # However the socket ends on the page (a block may mock it open or close it), an
            # unblocked handshake reaches the listener before the page sees an error.
            outcome = await browser.page.evaluate(f"""() => new Promise((resolve) => {{
                const socket = new WebSocket('ws://localhost:{port}/socket');
                socket.onopen = () => resolve('open');
                socket.onerror = () => resolve('error');
                socket.onclose = () => resolve('closed');
                setTimeout(() => resolve('timeout'), 3000);
            }})""")
            return outcome, arrived
        finally:
            await browser.close()
            listener.close()
            await listener.wait_closed()

    outcome, arrived = kit.run(scenario())

    assert arrived == [], f"the page saw {outcome!r}"
