#!/usr/bin/env python3
"""Deterministic localhost mock ATS for Interviewmaxxing browser tests.

Standard library only. Serves the careers site of a fictional employer,
"Brambleway Analytics", with ordinary accessible HTML application forms and a
server-side record of every accepted submission.

Routes under ``/__test__/`` exist strictly for test assertions and fixture
control. Product runtime code must never call them; it reconciles through the
public pages, for example ``/jobs/<job>/application-status``.

Run::

    uv run --no-project --python 3.12 scripts/mock_ats.py --state-dir /tmp/mock-ats

See ``tests/browser/MOCK_ATS.md`` for scenarios, routes and how to stop it.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import html
import ipaddress
import json
import os
import re
import signal
import socketserver
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.message import Message
from email.utils import collapse_rfc2231_value
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit


def _load_workday() -> Any:
    """``scripts/mock_workday.py`` (the ``workday-wizard`` scenario), loaded by path so
    this file keeps running as a plain script and as a module loaded by path."""
    import importlib.util

    name = "mock_workday"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("mock_workday.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


WD = _load_workday()

COMPANY = "Brambleway Analytics"
REFERENCE_PREFIX = "BWA"
MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 5000
RESUME_EXTENSIONS = (".pdf", ".doc", ".docx", ".txt")
SESSION_COOKIE = "bwa_session"
SIGNIN_EMAIL = "avery.quill@example.test"
SIGNIN_PASSWORD = "fixture-password-123"
CAPTCHA_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
HONEYPOT_FIELD = "website_hp"
CAPTCHA_WIDGET_FIELD = "g-recaptcha-response"
CONSENT_COOKIE = "bwa_consent"
INTERNAL_FIELDS = frozenset(
    {"resume_upload_id", "captcha_token", "captcha_answer", CAPTCHA_WIDGET_FIELD, HONEYPOT_FIELD,
     "phone_country"}
)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
US_PHONE_RE = re.compile(r"^\d{10}$")
US_PHONE_MESSAGE = "Enter a 10-digit US phone number using digits only, for example 3035550142."
FIXTURE_RESUME = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "browser" / "resume_avery_quill.pdf"
)
"""The fixture candidate's resume: ``fixture_identity`` jobs accept only these bytes."""
FIXTURE_IDENTITY = {
    "first_name": "Avery",
    "last_name": "Quill",
    "email": "avery.quill@example.test",
    "name": "Avery Quill",
}
"""Identity values ``fixture_identity`` jobs accept: exactly what the candidate enters."""
NOT_ENTERED_MESSAGE = "This value was not entered by the candidate."
WRONG_RESUME_MESSAGE = "The attached resume is not the file the candidate chose."


# --------------------------------------------------------------------------
# Form and job definitions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Option:
    value: str
    label: str
    disabled: bool = False


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    # text, email, tel, url, textarea, select, radio, checkbox,
    # checkbox_group, multiselect, file or custom_combobox (an ARIA widget
    # backed by a hidden input, deliberately not a native control). Script-driven
    # widgets whose value lives only in page state (see WIDGETS_JS): react_select,
    # react_multi, react_async (lookup), div_combobox, search_combobox,
    # remote_lookup (role-less lookup input), rippling_phone and intl_tel; fab_select
    # (BambooHR: a menu button over a hidden proxy <select> that holds only the chosen
    # option's id, which the form posts). Paylocity: rw_dropdown (a react-widgets
    # DropdownList: a div combobox owning a listbox it mounts on opening, named by a
    # <label for> and its data-for), pcty_select (a react-select without ARIA roles: an
    # input under a value div that covers it, a menu of plain divs) and
    # address_autocomplete (a native combobox input that keeps what is typed and lists
    # address suggestions; posted natively).
    # Custom uploaders mounted by page script (see SCENARIO_JS) and validated like
    # file: custom_file (a styled button and drop zone over a hidden, unlabeled
    # input) and label_file (a visually hidden input wrapped in its label).
    kind: str
    required: bool = False
    options: tuple[Option, ...] = ()
    hint: str | None = None
    """Shown under the label and referenced by aria-describedby."""
    autocomplete: str | None = None
    accept: str | None = None
    terms: str | None = None
    """Paragraph adjacent to a checkbox, not referenced by aria-describedby."""
    legend: str | None = None
    """Wraps a single checkbox in a fieldset with this legend."""
    disabled: bool = False
    dom_id: str | None = None
    """Element id of a script-driven widget (``question_6001``, ``field-3``) or of a
    control on a replicated vendor page (``resumator-firstname-value``)."""
    display: str | None = None
    """``dial``: a react_select shows only the dial code of the chosen label; ``dial-name``
    also shows a flag in each option and filters on the country's name only (Greenhouse).
    ``separate``: an intl_tel shows its dial code apart from the number (Workable).
    ``ids``: a fab_select's menu items take their option's id as element id (BambooHR's
    State menu); otherwise they are numbered ``menu-item-<n>``."""
    open_on: str | None = None
    """``click`` (default), ``keyboard`` (focus + ArrowDown only) or ``focus``."""
    remote: str | None = None
    """Suggestion URL prefix of a lookup (the query is appended)."""
    prefill: str | None = None
    """Initial value of a search combobox (a chosen value, like "+1 US"), the option id a
    fab_select shows already (BambooHR's Country), or the value of a pre-filled Easy
    Apply question ("3035550142")."""
    idle: str | None = None
    """Notice a search combobox shows when opened before anything is typed."""
    show_all: bool = False
    """A search combobox that lists every option when opened (a static menu)."""
    embedded: bool = False
    """Rendered inside another widget's block (a phone's country code)."""
    inline: bool = False
    """A react_select whose menu renders inside the form (no body portal), with a hidden
    required proxy input while it is empty and a "Toggle flyout" button (Greenhouse)."""
    popover: bool = False
    """A div_combobox or remote_lookup whose menu is a popover dialog that only an
    outside press or a choice closes (Rippling); lookups never expose aria-expanded."""
    labelled: bool = False
    """A popover div_combobox named by aria-labelledby (else only by the paragraph before it)."""
    clearable: bool = False
    """An inline react_select that shows a "Clear selection" button while it holds a value
    (Greenhouse's ClearIndicator); a fab_select's "Clear Selection" button likewise."""
    links_phone: bool = False
    """A dial-code react_select that sets the phone widget's country when chosen and then
    focuses the phone number (Greenhouse's phone fieldset)."""
    picker_label: str | None = None
    """The accessible name of a separate-dial-code phone widget's flag combobox."""
    uploader: str | None = None
    """A file field behind a script uploader whose file lives in page state: ``greenhouse``
    (a hidden input behind "Attach", replaced by the file's name once it takes a file),
    ``greenhouse-async`` (the same, re-rendered seconds after the attach), ``dropzone``
    (Workable: the input is emptied and the file's name shown) or ``teamtailor`` (Dropzone
    under a Stimulus controller: the script-made input takes its label's id; a file hides
    it, a fresh input replaces it and a preview whose hidden URL input reuses the id shows
    "Uploading…" until the upload ends)."""
    reveals: tuple[Field, ...] = ()
    """Follow-up questions (conditional fields) the page shows right after this radio
    group's block the moment one of its options is chosen (BambooHR): rendered into an
    inert ``<template>`` and mounted by REVEAL_JS on the change event (or on load, when the
    trigger is already checked and the server did not render them). The server validates
    and records them only when the trigger option was posted (``revealed_fields``)."""
    reveals_on: str | None = None
    """The option value that reveals them (choosing another option removes them again);
    None: any chosen option reveals them."""
    required_after: str | None = None
    """A radio group that is optional (no marker at all, as on BambooHR) until the named
    radio group is answered: the page then marks it required (an asterisk after its
    question, ``required`` and ``aria-required``) the moment an option there is chosen,
    and the server requires it only when that group was posted (``revealed_fields``)."""
    fabric_id: int | None = None
    """A BambooHR (Fabric UI) text field: its DOM id, and its label's ``for``, is the
    generated ``FabricTextField-<n>``, and FABRIC_JS mounts every such field again under the
    next free numbers, value kept, whenever a Yes/No of the form is answered (a React
    re-render with new keys)."""
    unnamed: bool = False
    """Rendered without a ``name`` (BambooHR's "Date Available"): nothing is posted, so the
    server neither requires nor records it, and a runtime knows the question by its
    generated DOM id."""
    reveal_hint: str | None = None
    """Help text this question's own block shows once its follow-up questions are revealed
    (Greenhouse's Hispanic/Latino question once the race question shows)."""
    lazy: bool = False
    """Rendered by page script only once its block scrolls into view (Teamtailor renders its
    custom questions late): until then the form holds an empty placeholder there."""
    disabled_by: str | None = None
    """A checkbox (by name) that takes this question away while it is checked (Paylocity's
    "I currently work here" and the entry's end date): DISABLE_JS disables and hides the
    question's block, and the server requires it only when that box was not posted."""
    aria: bool = False
    """A radio group, checkbox group or checkbox drawn as ARIA widgets (Greenhouse's job board,
    Radix-style): each option is a ``button[role=checkbox|radio][aria-checked]`` named by a
    ``<label for>``, beside an ``aria-hidden`` native "bubble" input (``tabindex=-1``,
    invisible) that carries the name and value for the form post. ARIA_CHOICE_JS keeps
    ``aria-checked`` and the bubble's ``checked`` together on a click."""
    placeholder: str | None = None
    """What a pcty_select shows while it holds no value ("Select a state")."""
    suggestions: tuple[str, ...] = ()
    """The address suggestions an address_autocomplete lists for what is typed."""

    @property
    def multi(self) -> bool:
        return self.kind in ("checkbox_group", "multiselect", "react_multi")

    @property
    def widget(self) -> bool:
        return self.kind in WIDGET_KINDS

    @property
    def scripted(self) -> bool:
        """Needs the widget script (a widget, or a file field behind a script uploader)."""
        return self.widget or self.uploader is not None

    def option_label(self, value: str) -> str:
        return next((o.label for o in self.options if o.value == value), value)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "options": [
                {"value": o.value, "label": o.label, "disabled": o.disabled}
                for o in self.options
            ],
            "disabled": self.disabled,
            "reveals": [r.describe() for r in self.reveals],
            "reveals_on": self.reveals_on,
            "required_after": self.required_after,
            "fabric_id": self.fabric_id,
            "unnamed": self.unnamed,
            "lazy": self.lazy,
        }


def revealed_fields(fields: tuple[Field, ...], form: dict[str, list[str]]) -> tuple[Field, ...]:
    """``fields`` with the follow-up questions whose trigger option ``form`` posts (or
    shows) inserted right after their trigger, and the questions ``required_after`` a
    group ``form`` answers made required, as the page shows them."""
    shown: list[Field] = []
    for f in fields:
        if f.required_after and not f.required and any(form.get(f.required_after, [])):
            f = replace(f, required=True)
        if f.disabled_by and f.required and any(form.get(f.disabled_by, [])):
            f = replace(f, required=False)
        shown.append(f)
        if not f.reveals:
            continue
        chosen = [v for v in form.get(f.name, []) if v]
        if chosen and (f.reveals_on is None or f.reveals_on in chosen):
            shown.extend(revealed_fields(f.reveals, form))
    return tuple(shown)


WIDGET_KINDS = frozenset({
    "react_select", "react_multi", "react_async", "div_combobox", "search_combobox",
    "remote_lookup", "rippling_phone", "intl_tel", "fab_select",
    "rw_dropdown", "pcty_select", "address_autocomplete",
})
UPLOADER_KINDS = frozenset({"custom_file", "label_file"})
FILE_KINDS = frozenset({"file"}) | UPLOADER_KINDS


def _options(*pairs: tuple[str, str]) -> tuple[Option, ...]:
    return tuple(Option(value, label) for value, label in pairs)


def _labels(*labels: str) -> tuple[Option, ...]:
    """Options whose submitted value is their visible label (lookups, Rippling menus)."""
    return tuple(Option(label, label) for label in labels)


FIRST_NAME = Field("first_name", "First name", "text", True, autocomplete="given-name")
LAST_NAME = Field("last_name", "Last name", "text", True, autocomplete="family-name")
EMAIL = Field("email", "Email", "email", True, autocomplete="email")
PHONE = Field("phone", "Phone", "tel", True, autocomplete="tel")
LINKEDIN = Field("linkedin_url", "LinkedIn profile URL", "url", autocomplete="url")
RESUME = Field(
    "resume",
    "Resume",
    "file",
    True,
    hint="PDF, DOC, DOCX or TXT, up to 5 MB.",
    accept=".pdf,.doc,.docx,.txt,application/pdf,text/plain",
)
WORK_AUTHORIZATION = Field(
    "work_authorization",
    "Are you legally authorized to work in the United States?",
    "select",
    True,
    _options(
        ("wa_authorized", "Yes, I am authorized to work in the US"),
        ("wa_not_authorized", "No, I am not authorized to work in the US"),
    ),
)
YEARS_EXPERIENCE = Field(
    "years_experience",
    "Years of professional experience",
    "select",
    True,
    _options(
        ("yrs_0_2", "0 to 2 years"),
        ("yrs_3_5", "3 to 5 years"),
        ("yrs_6_9", "6 to 9 years"),
        ("yrs_10_plus", "10 or more years"),
    ),
)
SPONSORSHIP = Field(
    "sponsorship",
    "Will you now or in the future require visa sponsorship?",
    "radio",
    True,
    _options(
        ("needs_sponsorship", "Yes, I will require sponsorship"),
        ("no_sponsorship", "No, I will not require sponsorship"),
    ),
)
SKILLS = Field(
    "skills",
    "Primary skills",
    "multiselect",
    True,
    _options(
        ("sk_python", "Python"),
        ("sk_sql", "SQL"),
        ("sk_spark", "Apache Spark"),
        ("sk_dbt", "dbt"),
        ("sk_k8s", "Kubernetes"),
        ("sk_go", "Go"),
    ),
    hint="Hold Ctrl or Command to select more than one.",
)
WORK_ARRANGEMENTS = Field(
    "work_arrangements",
    "Which work arrangements would you consider?",
    "checkbox_group",
    options=_options(
        ("arr_remote", "Remote"),
        ("arr_hybrid", "Hybrid"),
        ("arr_onsite", "On-site in Denver, CO"),
    ),
    hint="Select all that apply.",
)
OPEN_TO_RELOCATION = Field(
    "open_to_relocation", "I am open to relocating to Denver, CO", "checkbox"
)
WHY_BRAMBLEWAY = Field(
    "why_brambleway", "Why do you want to work at Brambleway Analytics?", "textarea", True
)
NOTICE_PERIOD = Field(
    "notice_period",
    "What is your notice period?",
    "select",
    True,
    (
        *_options(
            ("notice_immediate", "Immediately"),
            ("notice_2w", "2 weeks"),
            ("notice_1m", "1 month"),
            ("notice_2m_plus", "2 months or more"),
        ),
        Option("notice_3m_plus", "3 months or more (no longer offered)", disabled=True),
    ),
)
SALARY_EXPECTATION = Field(
    "salary_expectation", "Desired annual base salary (USD)", "text", True
)
FAA_CERTIFICATE = Field(
    "faa_part_107",
    "Do you hold an active FAA Part 107 remote pilot certificate?",
    "radio",
    True,
    _options(("faa_yes", "Yes"), ("faa_no", "No")),
)
TRAVEL_REQUIREMENT = Field(
    "travel_willingness",
    "Are you willing to travel to client sites up to 25% of the time?",
    "radio",
    True,
    _options(("travel_yes", "Yes"), ("travel_no", "No")),
)
"""The required question the ``changed-after-prepare`` form gains from its second load on."""
ATTEST_ACCURACY = Field(
    "attest_accuracy",
    "I certify that the information in this application is true and complete "
    "to the best of my knowledge.",
    "checkbox",
    True,
)
ATTEST_PRIVACY = Field(
    "attest_privacy_notice",
    "I have read and acknowledge the Brambleway Analytics Applicant Privacy Notice.",
    "checkbox",
    True,
)

AGREE_DECLARATION = Field(
    "agree_declaration",
    "I agree",
    "checkbox",
    True,
    legend="Candidate declaration",
    terms="I confirm that I have never been dismissed from employment for misconduct.",
)
AGREE_RETENTION = Field(
    "agree_retention",
    "I agree",
    "checkbox",
    True,
    hint="Brambleway Analytics may keep my application on file for 12 months and "
    "contact me about other roles.",
)
PREFERRED_OFFICE = Field(
    "preferred_office",
    "Preferred office",
    "custom_combobox",
    True,
    _options(("office_den", "Denver, CO"), ("office_bou", "Boulder, CO")),
)
REFERRAL_CODE = Field(
    "referral_code",
    "Employee referral code (referrals are closed)",
    "text",
    disabled=True,
)

# --- script-driven widgets (fictional replicas of hosted ATS custom controls) ---------

DIAL_CODES = _options(
    ("ar", "Argentina +54"), ("au", "Australia +61"), ("at", "Austria +43"),
    ("be", "Belgium +32"), ("br", "Brazil +55"), ("ca", "Canada +1"), ("cl", "Chile +56"),
    ("co", "Colombia +57"),
    ("dk", "Denmark +45"), ("fr", "France +33"), ("de", "Germany +49"), ("in", "India +91"),
    ("ie", "Ireland +353"), ("il", "Israel +972"), ("it", "Italy +39"), ("jp", "Japan +81"),
    ("mx", "Mexico +52"), ("nl", "Netherlands +31"), ("nz", "New Zealand +64"),
    ("no", "Norway +47"), ("ph", "Philippines +63"), ("pl", "Poland +48"),
    ("pt", "Portugal +351"), ("sg", "Singapore +65"), ("za", "South Africa +27"),
    ("kr", "South Korea +82"), ("es", "Spain +34"), ("se", "Sweden +46"),
    ("ch", "Switzerland +41"), ("gb", "United Kingdom +44"), ("us", "United States +1"),
)
"""A phone "Country" select: more than 20 options. "United States +1" and "Canada +1"
share the dial code the control displays after a choice ("+1")."""
HEARD_OPTIONS = _options(
    ("src_linkedin", "LinkedIn"), ("src_indeed", "Indeed"), ("src_site", "Company website"),
    ("src_referral", "Referral"), ("src_other", "Other"),
)
RS_PHONE_COUNTRY = Field("question_6004", "Country", "react_select", True, DIAL_CODES, display="dial")
RS_WORK_AUTHORIZATION = Field(
    "question_6001", "Are you legally authorized to work in the United States?", "react_select", True,
    _options(("rs_wa_yes", "Yes"), ("rs_wa_no", "No")),
)
RS_SPONSORSHIP = Field(
    "question_6002", "Will you now or in the future require visa sponsorship?", "react_select", True,
    _options(("rs_sp_yes", "Yes"), ("rs_sp_no", "No")),
)
RS_HEARD = Field("question_6003", "How did you hear about us?", "react_select", True, HEARD_OPTIONS,
                 open_on="focus")
"""Opens as soon as it has focus: a click on it while open would toggle it closed."""

RIPPLING_CODES = _labels("+1 US", "+1 CA", "+44 UK", "+49 DE", "+33 FR", "+61 AU", "+91 IN", "+52 MX")
RP_PHONE_CODE = Field("phone_country_code", "Country code", "search_combobox", options=RIPPLING_CODES,
                      dom_id="field-7-country", prefill="+1 US", embedded=True)
RP_PHONE = Field("phone", "Phone number", "rippling_phone", True, autocomplete="tel", dom_id="field-7")
RP_WORK_AUTHORIZATION = Field(
    "field-3", "Are you legally authorized to work in the United States?", "div_combobox", True,
    _labels("No", "Yes"), open_on="click",
)
RP_SPONSORSHIP = Field(
    "field-4", "Will you now or in the future require visa sponsorship?", "div_combobox", True,
    _labels("No", "Yes"), open_on="keyboard",
)
RP_PRONOUNS = Field("pronouns", "Pronouns", "search_combobox",
                    options=_labels("He/him", "She/her", "They/them", "Prefer not to say"),
                    dom_id="field-9", show_all=True)

CITIES_LONG = (
    "Austin, Texas, United States", "Austin, Minnesota, United States",
    "Austintown, Ohio, United States", "Austin, Indiana, United States",
    "Austin, Arkansas, United States", "Denver, Colorado, United States",
    "Boulder, Colorado, United States", "Round Rock, Texas, United States",
    "Aurora, Colorado, United States", "Dallas, Texas, United States",
)
CITIES_SHORT = (
    "Austin, TX, USA", "Austin, MN, USA", "Austintown, OH, USA", "Denver, CO, USA",
    "Boulder, CO, USA", "Round Rock, TX, USA",
)
US_STATE_NAMES = (
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois",
    "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts",
    "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota",
    "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
)
STATE_ABBREVIATIONS = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA",
    "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}

# Paylocity (Recruiting/Jobs/Apply): the SMS consent and "worked with us before" are
# react-widgets DropdownLists; Country and State are input-selects whose value div covers
# the input; the address line keeps what is typed while it lists suggestions.
PC_SMS = Field(
    "info.smsOptedIn", "We may use SMS during the hiring process. Do you give us permission to text you?",
    "rw_dropdown", True, _options(("true", "Yes"), ("false", "No")), dom_id="info.smsOptedIn",
    hint="By opting in, you agree to receive text messages about your application from Brambleway "
         "Analytics. Message and data rates may apply. Reply STOP to opt out at any time.",
)
PC_WORKED = Field(
    "info.haveYouWorkedWithUsBefore", "Have you worked with us before?", "rw_dropdown", True,
    _options(("true", "Yes"), ("false", "No")), dom_id="info.haveYouWorkedWithUsBefore",
)
PC_COUNTRIES = _options(("US", "United States"), ("CA", "Canada"), ("MX", "Mexico"),
                        ("GB", "United Kingdom"), ("IE", "Ireland"))
PC_COUNTRY = Field("address.country", "Country", "pcty_select", True, PC_COUNTRIES,
                   dom_id="public-site-address-country", prefill="US", placeholder="Select a country")
PC_ADDRESS1 = Field(
    "address.address1", "Address Line 1", "address_autocomplete", True,
    dom_id="public-site-address-address-1",
    suggestions=("1600 Larimer Street, Denver, CO, USA", "1600 Larkspur Lane, Boulder, CO, USA",
                 "1234 Fictional Avenue, Denver, CO, USA", "1234 Fictional Avenue Suite 200, Denver, CO, USA",
                 "4455 Brambleway Court, Austin, TX, USA"),
)
PC_ADDRESS2 = Field("address.address2", "Address Line 2", "text", dom_id="public-site-address-address-2")
PC_CITY = Field("address.city", "City", "text", True, dom_id="public-site-address-city")
PC_STATE = Field("address.state", "State", "pcty_select", True,
                 tuple(Option(STATE_ABBREVIATIONS[n], n) for n in US_STATE_NAMES),
                 dom_id="public-site-address-us-state", placeholder="Select a state")
PC_ZIP = Field("address.zip", "Zip", "text", True, dom_id="public-site-address-zip")
GH_LOCATION = Field("candidate_location", "Location (City)", "react_async", True, _labels(*CITIES_LONG),
                    dom_id="candidate-location", remote="/__fixture__/cities?style=long&q=")
RP_LOCATION = Field("location_short", "Location", "remote_lookup", options=_labels(*CITIES_SHORT),
                    dom_id="field-42", remote="/__fixture__/cities?style=short&q=")
RP_STATE = Field("state", "What state do you live in?", "search_combobox", True,
                 _labels(*US_STATE_NAMES), dom_id="field-43", idle="Start typing to search")
RP_POP_GENDER = Field("gender", "Gender", "div_combobox", True,
                      _labels("Male", "Female", "Non-binary", "Choose not to disclose"),
                      dom_id="field-55", popover=True, labelled=True)
RP_POP_AUTHORIZATION = Field(
    "custom_work_authorization", "Are you legally authorized to work in the United States?",
    "div_combobox", True, _labels("No", "Yes"), dom_id="field-63", popover=True,
)
RP_POP_LOCATION = Field("location", "Location", "remote_lookup", True, _labels(*CITIES_SHORT),
                        dom_id="field-42", remote="/__fixture__/cities?style=short&q=", popover=True)

ITI_COUNTRIES = (
    ("af", "Afghanistan", "93"), ("ar", "Argentina", "54"), ("au", "Australia", "61"),
    ("at", "Austria", "43"), ("be", "Belgium", "32"), ("br", "Brazil", "55"), ("ca", "Canada", "1"),
    ("cl", "Chile", "56"), ("co", "Colombia", "57"), ("dk", "Denmark", "45"), ("fr", "France", "33"),
    ("de", "Germany", "49"), ("in", "India", "91"), ("ie", "Ireland", "353"), ("il", "Israel", "972"),
    ("it", "Italy", "39"), ("jp", "Japan", "81"), ("mx", "Mexico", "52"), ("nl", "Netherlands", "31"),
    ("nz", "New Zealand", "64"), ("no", "Norway", "47"), ("ph", "Philippines", "63"),
    ("pl", "Poland", "48"), ("pt", "Portugal", "351"), ("sg", "Singapore", "65"),
    ("za", "South Africa", "27"), ("kr", "South Korea", "82"), ("es", "Spain", "34"),
    ("se", "Sweden", "46"), ("ch", "Switzerland", "41"), ("gb", "United Kingdom", "44"),
    ("us", "United States", "1"),
)
ITI_PHONE = Field("phone", "Phone", "intl_tel", True, autocomplete="tel")
PHONE_WIDGET_HEARD = Field("question_7003", "How did you hear about us?", "react_select",
                           options=HEARD_OPTIONS)
RS_CHANNELS = Field(
    "question_8001", "Which marketing channels have you managed?", "react_multi", True,
    _options(("ch_search", "Paid search"), ("ch_social", "Paid social"), ("ch_email", "Email"),
             ("ch_seo", "SEO"), ("ch_events", "Events")),
)
MULTI_PAGE_HEARD = Field("question_8002", "How did you hear about us?", "react_select", True,
                         HEARD_OPTIONS)
RS_INLINE_COUNTRY = Field("question_9004", "Country", "react_select", True, DIAL_CODES,
                          display="dial-name", inline=True)
"""Greenhouse's phone Country: options show a flag, and typing filters on the country's
name only (typing "United States +1" leaves no option)."""
GH_RESUME = Field("resume", "Resume/CV", "file", True, accept=".pdf,.doc,.docx,.txt", uploader="greenhouse")
GH_RESUME_ASYNC = Field("resume", "Resume/CV", "file", True, accept=".pdf,.doc,.docx,.txt",
                        uploader="greenhouse-async")
"""Greenhouse's uploader as it behaves live: the input keeps the file while the upload
runs; 2.5 s later the block re-renders with the file's name (the input and its buttons
gone) and the page's action area re-renders too (the submit button's path shifts)."""
WK_RESUME = Field("resume", "Resume", "file", True, accept=".pdf,.doc,.docx,.txt", uploader="dropzone")
WK_PHONE = Field("phone", "Phone", "intl_tel", True, autocomplete="tel", display="separate")
TT_RESUME = Field("resume", "Upload resume", "file", True, accept=".pdf,.doc,.docx,.txt",
                  uploader="teamtailor")
TT_FILES = Field("files", "Additional files", "file", uploader="teamtailor")
"""Teamtailor's uploaders: ``#candidate_resume_remote_url`` is Dropzone's hidden input
(the label's id handed to it by page script), not an import field."""
GH_COUNTRY = Field("country", "Country", "react_select", True, DIAL_CODES, display="dial-name",
                   inline=True, links_phone=True)
"""Greenhouse's phone-fieldset Country: its value shows a flag and "+" and the code as two
text nodes; choosing one sets the phone widget's country."""
GH_PHONE_COUNTRY_PICKER = Field("phone", "Phone", "intl_tel", True, autocomplete="tel",
                                display="separate", picker_label="Country")
"""A phone widget whose flag combobox is also named "Country" and shows its dial code as
"+" and the code in two text nodes."""
"""Workable's intl-tel-input (separateDialCode, nationalMode): the dial code is shown apart
from the number, and typing "+1…" leaves only the national digits in the input."""
RS_INLINE_AUTHORIZATION = Field(
    "question_9001", "Are you legally authorized to work in the United States?", "react_select", True,
    _options(("in_wa_yes", "Yes"), ("in_wa_no", "No")), inline=True, clearable=True,
)
RS_INLINE_SPONSORSHIP = Field(
    "question_9002", "Will you now or in the future require visa sponsorship?", "react_select", True,
    _options(("in_sp_yes", "Yes"), ("in_sp_no", "No")), inline=True, clearable=True,
)
RS_INLINE_HEARD = Field("question_9003", "How did you hear about us?", "react_select",
                        options=HEARD_OPTIONS, inline=True)
# BambooHR's Fabric selects post an id (the proxy <select>'s single option); the menu only
# shows labels. State menu items carry the state's id as their element id, as live.
FAB_PLACEHOLDER = "\N{EN DASH}Select\N{EN DASH}"
BH_COUNTRIES = _options(
    ("1", "United States"), ("2", "Canada"), ("3", "Australia"), ("4", "United Kingdom"),
    ("5", "Ireland"), ("6", "Germany"), ("7", "France"), ("8", "Mexico"), ("9", "India"),
    ("10", "New Zealand"),
)
BH_SPONSORSHIP = Field(
    "sponsorship",
    "Will you now or will you in the future require employment visa sponsorship?",
    "radio",
    True,
    _options(("yes", "Yes"), ("no", "No")),
    reveals=(
        Field(
            "authorization_basis",
            "What is the basis of your current authorization to work in the United States?",
            "select",
            True,
            _options(
                ("citizen", "U.S. citizen or national"),
                ("permanent_resident", "Lawful permanent resident"),
                ("visa", "A work visa or another status"),
            ),
        ),
        Field(
            "authorization_proof",
            "Can you provide documentation of your work authorization at hire?",
            "radio",
            True,
            _options(("yes", "Yes"), ("no", "No")),
        ),
    ),
)
"""BambooHR: a Yes/No whose answer reveals two follow-up questions (conditional fields)."""
BH_LOCATED = Field(
    "located_austin",
    "This position is located in Austin, Texas. Are you currently located in the Austin area?",
    "radio",
    True,
    _options(("yes", "Yes"), ("no", "No")),
)
BH_AUTHORIZED = Field(
    "customQuestionAnswers.yes_no_2101",
    "Are you legally authorized to work in the United States for any employer?",
    "radio",
    True,
    _options(("Yes", "Yes"), ("No", "No")),
)
BH_SPONSORSHIP_ONCE = Field(
    "customQuestionAnswers.yes_no_2102",
    "Will you now or will you in the future require employment visa sponsorship?",
    "radio",
    options=_options(("Yes", "Yes"), ("No", "No")),
    required_after="customQuestionAnswers.yes_no_2101",
)
"""BambooHR: optional (no marker) until work authorization is answered, then required."""
BH_IN_AUSTIN = Field(
    "customQuestionAnswers.yes_no_2103",
    "This position is in Austin, TX. Are you currently located in Austin?",
    "radio",
    options=_options(("Yes", "Yes"), ("No", "No")),
)
BH_ONSITE = Field(
    "customQuestionAnswers.yes_no_2104",
    "Are you able to work onsite in our Austin office?",
    "radio",
    options=_options(("Yes", "Yes"), ("No", "No")),
)
BH_STREET = Field("streetAddress.value", "Address", "text", True, fabric_id=48)
BH_CITY = Field("city.value", "City", "text", True, fabric_id=49)
BH_ZIP = Field("zip.value", "ZIP", "text", True, fabric_id=50)
BH_DATE_AVAILABLE = Field("date_available", "Date Available", "text", fabric_id=51, unnamed=True,
                          placeholder="mm/dd/yyyy")
"""BambooHR's "Date Available": a Fabric text field without a name (known by its generated id)."""
BH_SPONSORSHIP_ASKED = Field(
    "customQuestionAnswers.yes_no_2102",
    "Will you now or will you in the future require employment visa sponsorship?",
    "radio",
    True,
    _options(("Yes", "Yes"), ("No", "No")),
)
GH_EEO_AUTHORIZED = Field(
    "question_7001", "Are you legally authorized to work in the United States?", "select", True,
    _options(("1", "Yes"), ("0", "No")),
)
GH_EEO_GENDER = Field(
    "gender", "Gender", "select",
    options=_options(("1", "Male"), ("2", "Female"), ("3", "Decline To Self Identify")),
)
GH_EEO_RACE = Field(
    "race", "Please identify your race", "select",
    options=_options(
        ("1", "American Indian or Alaskan Native"), ("2", "Asian"), ("3", "Black or African American"),
        ("4", "White"), ("5", "Native Hawaiian or Other Pacific Islander"), ("6", "Two or More Races"),
        ("7", "Decline To Self Identify"),
    ),
)
GH_EEO_HISPANIC = Field(
    "hispanic_ethnicity", "Are you Hispanic/Latino?", "select",
    options=_options(("Yes", "Yes"), ("No", "No"), ("Decline To Self Identify", "Decline To Self Identify")),
    reveals=(GH_EEO_RACE,),
    reveals_on="No",
    reveal_hint="Race and ethnicity categories are those the U.S. Equal Employment Opportunity "
                "Commission defines.",
)
"""Greenhouse: "No" shows the race question right after it and a definitions hint in its block."""
GH_EEO_VETERAN = Field(
    "veteran_status", "Veteran Status", "select",
    options=_options(("1", "I am not a protected veteran"),
                     ("2", "I identify as one or more of the classifications of a protected veteran"),
                     ("3", "I don't wish to answer")),
)
PW_COMPANY = Field("workHistory.companyName.0", "Company Name", "text", True)
PW_POSITION = Field("workHistory.position.0", "Position", "text", True)
PW_START = Field("txt-workHistory-startDate-0", "Start Date", "text", True, hint="MM/YYYY")
PW_END = Field("txt-workHistory-endDate-0", "End Date", "text", True, hint="MM/YYYY",
               disabled_by="workHistory.currentlyWorkingHere.0")
PW_CURRENT = Field("workHistory.currentlyWorkingHere.0", "I currently work here", "checkbox")
GH_ARIA_CLIENTS = Field(
    "question_7201[]", "How many clients do you currently support?", "checkbox_group", True,
    _options(("71", "1-3"), ("72", "4-7"), ("73", "8 or more")), aria=True,
)
GH_ARIA_BUDGETS = Field(
    "question_7202", "What range of monthly budgets are you used to working with?", "radio", True,
    _options(("81", "Under $50k"), ("82", "$50k to $250k"), ("83", "Over $250k")), aria=True,
)
GH_ARIA_DOUBLE_CHECK = Field(
    "question_7203",
    "Please double-check all the information provided above. Ensuring accuracy is crucial, as any "
    "errors or omissions may impact the review of your application.",
    "checkbox", True, aria=True,
)
TT_LINKEDIN = Field("candidate[answers_attributes][0][text]", "Linkedin profile", "text", True, lazy=True)
"""Teamtailor: a custom question rendered only once it scrolls into view."""
BH_STATE = Field("state.value", "State", "fab_select", True,
                 _options(*((str(i + 1), name) for i, name in enumerate(US_STATE_NAMES))),
                 dom_id="fab-select341", clearable=True, display="ids")
BH_COUNTRY = Field("countryId.value", "Country", "fab_select", True, BH_COUNTRIES,
                   dom_id="fab-select343", prefill="1", clearable=True)
BH_EDUCATION = Field("educationLevelId", "Highest Education Obtained", "fab_select",
                     options=_options(("21", "High School"), ("22", "Associate's Degree"),
                                      ("23", "Bachelor's Degree"), ("24", "Master's Degree"),
                                      ("25", "Doctorate")),
                     dom_id="educationLevelId")
JV_RESUME = Field("resume", "Add Resume", "file", True, accept=".pdf,.doc,.docx,.txt", uploader="jobvite")
"""Jobvite's résumé: a "Select" button (aria-haspopup, named by the "Add Resume*" heading)
opens an "Attachment Options" popup that page script appends to <body>, so its visually
hidden file input is outside the form. A chosen file hides the button and lists its name."""
JV_REFERRED = Field(
    "referred", "Were you referred to this role by a current Brambleway employee?", "select", True,
    _options(("not_referred", "No, I was not referred"),
             ("referred", "Yes, I was referred by a Brambleway employee")),
)
JV_SPONSORSHIP = Field(
    "sponsorship",
    "Do you now or in the future will you require sponsorship for work in the United States?",
    "select", True,
    _options(("sp_current", "Yes, I CURRENTLY require work sponsorship"),
             ("sp_future", "Yes, I will require FUTURE work sponsorship"),
             ("sp_never", "No, I will NOT ever require any work sponsorship")),
)
JV_CONSENT_COOKIE = "bwa_jv_consent"
JV_POLICY_ID = "policy-7d1f"
JV_REGIONAL = (("policy-ca-en", "Canada - English"), ("policy-ca-fr", "Canada - Français"),
               ("policy-us-en", "United States - English"))
"""The regional policies of ``/jobs/jobvite-like/apply?policies=regional``."""

# --- upload and autofill scenarios (page behaviour in SCENARIO_JS) ----------------------

UPLOAD_ACCEPT = ".pdf,.doc,.docx,.txt"
CUSTOM_RESUME = Field("resume", "Resume/CV", "custom_file", True,
                      hint="PDF, DOC, DOCX or TXT, up to 5 MB.", accept=UPLOAD_ACCEPT)
COVER_LETTER = Field("cover_letter", "Cover letter", "label_file", accept=UPLOAD_ACCEPT)
LEVER_NAME = Field("name", "Full name", "text", True, autocomplete="name")
LEVER_LOCATION = Field("location", "Current location", "text")
LEVER_LINKEDIN = Field("urls[LinkedIn]", "LinkedIn URL", "url")

# --- replicas of real application flows (fictional; see the Handler's flow pages) ---------

MODAL_WIZARD = "modal-wizard"
"""LinkedIn-style Easy Apply: a modal dialog wizard built by page script."""
IFRAME_EMBED = "iframe-embed"
"""An employer careers page embedding a Greenhouse-style application iframe."""
STEPPER_AMBIGUOUS = "stepper-ambiguous"
"""JazzHR-style form whose only action controls are anchors."""
APPLY_IN_ALERT_FORM = "apply-in-alert-form"
"""Dayforce-style posting inside one form beside a job-alert signup."""

EASY_APPLY_POSTING = "4007130"
"""The fictional LinkedIn job number inside the Easy Apply element ids."""
EASY_APPLY_PLACEHOLDER = "Select an option"
"""LinkedIn's select placeholder: a real option whose value is its text."""
EASY_APPLY_MAX_UPLOAD = 2 * 1024 * 1024
EASY_APPLY_EXTENSIONS = (".pdf", ".doc", ".docx")
SQL_YEARS_RE = re.compile(r"^\d{1,2}$")
SQL_YEARS_MESSAGE = "Enter a whole number between 0 and 99"


def _ea_id(component: str, element: int, suffix: str) -> str:
    """A LinkedIn-style Easy Apply control id (``…-9001-multipleChoice``)."""
    return (f"{component}-formElement-urn-li-jobs-applyformcommon-easyApplyFormElement-"
            f"{EASY_APPLY_POSTING}-{element}-{suffix}")


EA_EMAIL = Field("email", "Email address", "select", True,
                 _labels("avery.quill@example.test", "a.quill@example.test"),
                 dom_id=_ea_id("text-entity-list-form-component", 9001, "multipleChoice"),
                 prefill="avery.quill@example.test")
EA_PHONE_COUNTRY = Field(
    "phone_country", "Phone country code", "select", True,
    _labels("United States (+1)", "Canada (+1)", "United Kingdom (+44)", "Afghanistan (+93)"),
    dom_id=_ea_id("text-entity-list-form-component", 9002, "phoneNumber-country"), prefill="United States (+1)",
)
EA_PHONE = Field("phone", "Mobile phone number", "text", True,
                 dom_id=_ea_id("single-line-text-form-component", 9002, "phoneNumber-nationalNumber"),
                 prefill="3035550142")
EA_CITY = Field("city", "City", "text", True, dom_id=_ea_id("single-line-text-form-component", 9003, "text"),
                prefill="Boulder")
EA_RESUME = Field(
    "resume", "Resume", "file", True, hint="DOC, DOCX, PDF (2 MB)",
    accept="application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
    "application/pdf",
    dom_id="jobs-document-upload-file-input-upload-resume",
)
EA_SQL_YEARS = Field("sql_years", "How many years of work experience do you have with SQL?", "text", True,
                     dom_id=_ea_id("single-line-text-form-component", 9004, "numeric"))
EA_WORK_AUTHORIZATION = Field(
    "work_authorization", "Are you legally authorized to work in the United States?", "radio", True,
    _labels("Yes", "No"),
    dom_id="urn:li:fsd_formElement:urn:li:jobs_applyformcommon_easyApplyFormElement:"
    f"({EASY_APPLY_POSTING},9005,multipleChoice)",
)
"""Its radios share this urn-like ``name`` and have the ids ``<name>-0`` and ``<name>-1``."""
EA_SPONSORSHIP = Field(
    "sponsorship", "Will you now or in the future require sponsorship for employment visa status?", "select",
    True, _labels("Yes", "No"), dom_id=_ea_id("text-entity-list-form-component", 9006, "multipleChoice"),
)
EA_FOLLOW = Field("follow_company", f"Follow {COMPANY} to stay up to date with their page.", "checkbox",
                  dom_id="follow-company-checkbox")
EASY_APPLY_QUESTIONS = {
    "email": EA_EMAIL, "phone_country": EA_PHONE_COUNTRY, "phone": EA_PHONE, "city": EA_CITY,
    "sql_years": EA_SQL_YEARS, "work_authorization": EA_WORK_AUTHORIZATION, "sponsorship": EA_SPONSORSHIP,
}
"""The dialog's questions by their ``window.__easyApply`` key (which is also the posted name)."""
EASY_APPLY_RESUMES: dict[str, tuple[tuple[str, str], ...]] = {
    "match": (("Avery_Quill_Resume_2025.pdf", "290 KB · Last used on 8/12/2026"),
              ("resume_avery_quill.pdf", "1 KB · Last used on 3/2/2026")),
    "one": (("Avery_Quill_Resume_2025.pdf", "290 KB · Last used on 8/12/2026"),),
    "nomatch": (("Avery_Quill_Resume_2025.pdf", "290 KB · Last used on 8/12/2026"),
                ("AQ_CV_marketing.docx", "48 KB · Last used on 1/15/2026")),
    "none": (),
}
"""Saved resume cards (file name, details) per ``?resumes=`` variant; the first is preselected."""
EASY_APPLY_PROFILE = {"name": "Avery Quill", "initials": "AQ", "headline": "Growth marketing and analytics",
                      "location": "Boulder, Colorado, United States"}

EMBED_BOARD = "brambleway"
EMBED_TOKEN = "4007131"
"""``/embed/job_app?for=<board>&token=<token>`` serves the embedded form of ``iframe-embed``."""

JZ_FIRST_NAME = Field("first_name", "First Name", "text", True, autocomplete="given-name",
                      dom_id="resumator-firstname-value")
JZ_LAST_NAME = Field("last_name", "Last Name", "text", True, autocomplete="family-name",
                     dom_id="resumator-lastname-value")
JZ_EMAIL = Field("email", "Email", "email", True, autocomplete="email", dom_id="resumator-email-value")
JZ_PHONE = Field("phone", "Phone", "tel", True, autocomplete="tel", dom_id="resumator-phone-value")
JZ_SALARY = Field("desired_salary", "Desired salary", "text", dom_id="resumator-salary-value")
JZ_HEARD = Field(
    "heard_about", "How did you hear about this job?", "select", True,
    _options(("hear_linkedin", "LinkedIn"), ("hear_indeed", "Indeed"), ("hear_site", "Company website"),
             ("hear_other", "Other")),
    dom_id="resumator-heard-value",
)
JZ_RESUME = Field("resume", "Resume", "file", True, accept=".pdf,.doc,.docx", dom_id="resumator-resume-file")
"""Required, but a pasted resume (``resume_text``) stands in for the file."""
JZ_RESUME_TEXT = Field("resume_text", "Paste resume", "textarea", dom_id="resumator-resume-value")
JAZZHR_HIDDEN = (
    ("resumator-job-id", "BWA-JZ-132"), ("resumator-board-code", "bramblewayanalytics"),
    ("resumator-source", "applytojob"), ("resumator-referrer", ""),
    ("resumator-applicant-token", "c0ffee0132"), ("resumator-form-version", "3"),
)
"""The hidden inputs of the JazzHR-style form, posted unchanged (recorded as extra fields)."""

CORE_FIELDS = (
    FIRST_NAME,
    LAST_NAME,
    EMAIL,
    PHONE,
    LINKEDIN,
    RESUME,
    WORK_AUTHORIZATION,
    SPONSORSHIP,
)


@dataclass(frozen=True)
class Step:
    title: str
    fields: tuple[Field, ...]


@dataclass(frozen=True)
class Job:
    slug: str
    code: str
    title: str
    department: str
    location: str
    scenario: str
    steps: tuple[Step, ...]
    requires_signin: bool = False
    captcha: bool = False
    visible_confirmation: bool = True
    strict_phone: bool = False
    honeypot: bool = False
    """Adds a visually hidden anti-spam input; any value is rejected."""
    generic_thanks: bool = False
    """Acceptance returns a bare "Thank you!" page with no job or reference."""
    captcha_widget: bool = False
    """Embeds an invisible reCAPTCHA-style badge; only its token is checked, on submit."""
    spa_loading: bool = False
    """Page script renders the form 1.5 s after load, behind a loading indicator."""
    flash_closed: bool = False
    """The page shows "Job not found" (HTTP 200) until its data arrives 800 ms later, then
    the posting and its form (an SPA's first render)."""
    cookie_banner: bool = False
    """A modal cookie-consent dialog covers the page (main is inert) until dismissed."""
    onetrust: bool = False
    """A OneTrust cookie banner (``#onetrust-consent-sdk``) slides in over the bottom half of
    the page shortly after it loads (``?cookie_ms``, 600 ms) and takes the clicks there;
    "Reject All", "Accept All Cookies" and "Cookies Settings" (ONETRUST_JS)."""
    review: bool = False
    """A one-step form that is still followed by the review page (Paylocity: the form's
    Next control, then the final review); every step uses the step flow."""
    next_id: str | None = None
    """Element id of the step form's Next control (Paylocity's ``btn-submit``)."""
    description_px: int = 0
    """Height of a job description shown above the application form on its page, so the
    form starts below the fold (Teamtailor's apply page)."""
    formless: bool = False
    """The questions are not in a <form>: page script posts them (a Rippling-style SPA)."""
    autofill: bool = False
    """A page-level "Autofill my application" button outside the form that the page
    disables for a moment on every keystroke (Greenhouse)."""
    validity: bool = False
    """The submit button stays disabled until every required question is answered, and a
    text input that loses focus gets aria-invalid="false"."""
    fixture_identity: bool = False
    """Accepts only the fixture candidate's own identity values and resume file, so values
    page script wrote (a resume parser, LinkedIn) are rejected (fixture_identity_errors)."""
    data_consent: bool = False
    """Jobvite: the apply URL shows a "Data Consent" page (choose a location of residence
    and language, then "I Accept") until the consent is accepted; accepting posts it back to
    the apply URL, which records it and returns the form."""
    added_on_reload: Field | None = None
    """A question the (single-step) form gains from the second load of its application
    page on: the server counts ``GET /jobs/<job>/apply`` per job (``/__test__/reset``
    clears the count), and renders and validates the changed form after the first."""

    @property
    def multistep(self) -> bool:
        return len(self.steps) > 1 or self.review

    @property
    def fields(self) -> tuple[Field, ...]:
        return tuple(f for step in self.steps for f in step.fields)

    def field(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)

    def describe(self) -> dict[str, Any]:
        return {
            "job_id": self.slug,
            "job_code": self.code,
            "title": self.title,
            "company": COMPANY,
            "scenario": self.scenario,
            "posting_path": f"/jobs/{self.slug}",
            "apply_path": f"/jobs/{self.slug}/apply",
            "status_path": f"/jobs/{self.slug}/application-status",
            "requires_signin": self.requires_signin,
            "captcha": self.captcha,
            "visible_confirmation": self.visible_confirmation,
            "honeypot": self.honeypot,
            "generic_thanks": self.generic_thanks,
            "captcha_widget": self.captcha_widget,
            "spa_loading": self.spa_loading,
            "flash_closed": self.flash_closed,
            "cookie_banner": self.cookie_banner,
            "onetrust": self.onetrust,
            "formless": self.formless,
            "autofill": self.autofill,
            "validity": self.validity,
            "fixture_identity": self.fixture_identity,
            "data_consent": self.data_consent,
            "added_on_reload": self.added_on_reload.describe() if self.added_on_reload else None,
            "multistep": self.multistep,
            "steps": [
                {"title": s.title, "fields": [f.describe() for f in s.fields]}
                for s in self.steps
            ],
        }


def _single(*fields: Field) -> tuple[Step, ...]:
    return (Step("Application", fields),)


_WD_KINDS = {"text": "text", "dropdown": "select", "radio": "radio", "checkbox": "checkbox",
             "checkgroup": "checkbox_group", "prompt": "multiselect", "date": "text", "file": "file"}


def _workday_steps() -> tuple[Step, ...]:
    """The wizard's questions for the catalog (``/__test__/jobs``), page by page."""
    steps = []
    for page_def in WD.PAGES:
        fields = []
        for f in page_def["fields"]:
            if f["kind"] == "add_section":
                continue
            options = ([[leaf, leaf] for leaf in WD.prompt_leaves(f)] if f["kind"] == "prompt"
                       else f.get("options") or [])
            fields.append(Field(f["name"], f["label"], _WD_KINDS[f["kind"]], f["required"],
                                _options(*((v, lbl) for v, lbl in options)), dom_id=f["id"]))
        steps.append(Step(page_def["title"], tuple(fields)))
    return tuple(steps)


STANDARD_FIELDS = _single(
    FIRST_NAME,
    LAST_NAME,
    EMAIL,
    PHONE,
    LINKEDIN,
    RESUME,
    WORK_AUTHORIZATION,
    YEARS_EXPERIENCE,
    SPONSORSHIP,
    SKILLS,
    WORK_ARRANGEMENTS,
    OPEN_TO_RELOCATION,
    WHY_BRAMBLEWAY,
)


JOBS: dict[str, Job] = {
    job.slug: job
    for job in (
        Job(
            "standard",
            "BWA-ENG-101",
            "Senior Data Platform Engineer",
            "Engineering",
            "Denver, CO (Hybrid)",
            "Single-page form with every native control type; accepted with a visible confirmation.",
            STANDARD_FIELDS,
        ),
        Job(
            "multistep",
            "BWA-ML-102",
            "Machine Learning Engineer",
            "Engineering",
            "Remote (US)",
            "Three form steps plus a review page; accepted with a visible confirmation.",
            (
                Step("Contact information", (FIRST_NAME, LAST_NAME, EMAIL, PHONE, LINKEDIN)),
                Step(
                    "Resume and experience",
                    (RESUME, YEARS_EXPERIENCE, WORK_AUTHORIZATION, SPONSORSHIP),
                ),
                Step(
                    "Additional questions",
                    (SKILLS, WORK_ARRANGEMENTS, OPEN_TO_RELOCATION, WHY_BRAMBLEWAY),
                ),
            ),
        ),
        Job(
            "missing-required",
            "BWA-AE-103",
            "Analytics Engineer",
            "Data",
            "Denver, CO (Hybrid)",
            "Required questions the fixture candidate cannot answer (notice period, "
            "salary, certification); must surface as missing input.",
            _single(*CORE_FIELDS, NOTICE_PERIOD, SALARY_EXPECTATION, FAA_CERTIFICATE),
        ),
        Job(
            "attestation",
            "BWA-SEC-104",
            "Staff Security Engineer",
            "Security",
            "Remote (US)",
            "Required personal attestations that only the candidate may give.",
            _single(*CORE_FIELDS, ATTEST_ACCURACY, ATTEST_PRIVACY),
        ),
        Job(
            "validation",
            "BWA-BE-105",
            "Backend Engineer",
            "Engineering",
            "Denver, CO (On-site)",
            "Server-side phone rule (10 digits only) rejects the fixture phone with a "
            "visible error.",
            _single(*CORE_FIELDS),
            strict_phone=True,
        ),
        Job(
            "signin",
            "BWA-PE-106",
            "Product Engineer",
            "Product",
            "Remote (US)",
            "The application form requires signing in to a candidate account first.",
            _single(*CORE_FIELDS),
            requires_signin=True,
        ),
        Job(
            "captcha",
            "BWA-FE-107",
            "Frontend Engineer",
            "Engineering",
            "Remote (US)",
            "The application form includes an image CAPTCHA a person must solve.",
            _single(*CORE_FIELDS),
            captcha=True,
        ),
        Job(
            "agreement",
            "BWA-DE-109",
            "Data Engineer",
            "Data",
            "Remote (US)",
            "Two checkboxes labelled only \"I agree\"; their terms are an adjacent "
            "paragraph inside a legend-titled fieldset, and aria-describedby text.",
            _single(*CORE_FIELDS, AGREE_DECLARATION, AGREE_RETENTION),
        ),
        Job(
            "custom-control",
            "BWA-OPS-110",
            "Operations Analyst",
            "Operations",
            "Denver, CO (Hybrid)",
            "A required custom ARIA combobox (not a native control), a disabled field and "
            "a visually hidden honeypot input.",
            _single(*CORE_FIELDS, PREFERRED_OFFICE, REFERRAL_CODE),
            honeypot=True,
        ),
        Job(
            "vague-confirmation",
            "BWA-QA-111",
            "QA Engineer",
            "Engineering",
            "Remote (US)",
            "Accepted and counted, but the response is a bare \"Thank you!\" page that "
            "names no job and no reference; the status page later shows the receipt.",
            _single(*CORE_FIELDS),
            generic_thanks=True,
        ),
        Job(
            "uncertain",
            "BWA-SRE-108",
            "Site Reliability Engineer",
            "Infrastructure",
            "Denver, CO (Hybrid)",
            "Submission is recorded, then the response is an error page with no "
            "confirmation. A test-only reveal later exposes the receipt on the status page.",
            _single(*CORE_FIELDS),
            visible_confirmation=False,
        ),
        Job(
            "captcha-widget",
            "BWA-FE-112",
            "Frontend Platform Engineer",
            "Engineering",
            "Remote (US)",
            "The standard form plus an invisible reCAPTCHA-style badge; the POST is accepted "
            "only with a non-empty g-recaptcha-response token.",
            STANDARD_FIELDS,
            captcha_widget=True,
        ),
        Job(
            "spa-loading",
            "BWA-DE-113",
            "Data Platform Engineer",
            "Engineering",
            "Denver, CO (Hybrid)",
            "The standard form is injected by page script 1.5 s after load, behind a "
            "\"Fetching application form\" indicator.",
            STANDARD_FIELDS,
            spa_loading=True,
        ),
        Job(
            "cookie-banner",
            "BWA-AE-114",
            "Analytics Platform Engineer",
            "Data",
            "Remote (US)",
            "The standard form under a modal cookie-consent dialog (Cookies settings, Accept "
            "all, Decline all) that must be dismissed before the form can be used.",
            STANDARD_FIELDS,
            cookie_banner=True,
        ),
        Job(
            "react-select",
            "BWA-GH-120",
            "Growth Marketing Manager",
            "Marketing",
            "Remote (US)",
            "The standard questions with work authorization, sponsorship, \"How did you hear "
            "about us?\" and a phone dial-code \"Country\" as React-select-style comboboxes "
            "whose values live only in page state.",
            _single(
                FIRST_NAME, LAST_NAME, EMAIL, RS_PHONE_COUNTRY, PHONE, LINKEDIN, RESUME,
                RS_WORK_AUTHORIZATION, YEARS_EXPERIENCE, RS_SPONSORSHIP, SKILLS,
                WORK_ARRANGEMENTS, OPEN_TO_RELOCATION, RS_HEARD, WHY_BRAMBLEWAY,
            ),
        ),
        Job(
            "div-combobox",
            "BWA-RP-121",
            "Lifecycle Marketing Specialist",
            "Marketing",
            "Remote (US)",
            "Rippling-style div comboboxes (one opens on click, one only from the keyboard) "
            "with the question in a preceding paragraph, a pre-filled phone country-code "
            "search and a static pronouns search.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, RP_PHONE_CODE, RP_PHONE, RP_WORK_AUTHORIZATION,
                    RP_SPONSORSHIP, RP_PRONOUNS),
        ),
        Job(
            "typeahead",
            "BWA-GH-122",
            "Field Marketing Manager",
            "Marketing",
            "Austin, TX (Hybrid)",
            "Location lookups: a React-select-style async city search, a role-less location "
            "input and a state search that list suggestions only after typing.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, GH_LOCATION, RP_LOCATION, RP_STATE),
        ),
        Job(
            "phone-widget",
            "BWA-GH-123",
            "Partner Marketing Manager",
            "Marketing",
            "Remote (US)",
            "An intl-tel-input-style phone field whose country picker follows a typed "
            "+<code>, beside a React-select-style question.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, ITI_PHONE, PHONE_WIDGET_HEARD),
        ),
        Job(
            "multiselect-react",
            "BWA-GH-124",
            "Content Marketing Manager",
            "Marketing",
            "Remote (US)",
            "A React-select-style multi-select with chips that the runtime must leave to the "
            "user, beside a single-choice one.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, RS_CHANNELS, MULTI_PAGE_HEARD),
        ),
        Job(
            "react-select-inline",
            "BWA-GH-125",
            "Performance Marketing Manager",
            "Marketing",
            "Remote (US)",
            "Greenhouse-style: React-select questions whose menus open inside the form (no body "
            "portal) next to a real \"Toggle flyout\" button, with a hidden required proxy input "
            "while a required one is empty; a dial-code \"Country\" with flags that filters on "
            "the country's name; a résumé uploader that replaces its hidden input with the "
            "file's name; a page-level \"Autofill my application\" button that is disabled "
            "for a moment on every keystroke; clearable questions that show a \"Clear "
            "selection\" button once answered; and a submit button that stays disabled until "
            "every required question is answered.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, RS_INLINE_COUNTRY, PHONE, GH_RESUME,
                    RS_INLINE_AUTHORIZATION, YEARS_EXPERIENCE, RS_INLINE_SPONSORSHIP, RS_INLINE_HEARD,
                    WHY_BRAMBLEWAY),
            autofill=True,
            validity=True,
        ),
        Job(
            "react-select-inline-async",
            "BWA-GH-128",
            "Lifecycle Marketing Manager",
            "Marketing",
            "Remote (US)",
            "react-select-inline with Greenhouse's uploader as it behaves live: the upload "
            "completes 2.5 s after the attach, then the résumé block re-renders with the file's "
            "name and the page's action area re-renders, shifting the submit button's path.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, RS_INLINE_COUNTRY, PHONE, GH_RESUME_ASYNC,
                    RS_INLINE_AUTHORIZATION, YEARS_EXPERIENCE, RS_INLINE_SPONSORSHIP, RS_INLINE_HEARD,
                    WHY_BRAMBLEWAY),
            autofill=True,
            validity=True,
        ),
        Job(
            "flash-closed",
            "BWA-OPS-130",
            "Marketing Operations Specialist",
            "Marketing",
            "Remote (US)",
            "The standard form behind an SPA whose first render says \"Job not found\" (HTTP "
            "200) for 800 ms before its data arrives.",
            STANDARD_FIELDS,
            flash_closed=True,
        ),
        Job(
            "phone-dialcode-collision",
            "BWA-GH-129",
            "Growth Operations Manager",
            "Marketing",
            "Remote (US)",
            "Greenhouse's phone fieldset: a dial-code \"Country\" React select (its value is a "
            "flag and \"+\" and the code as separate text nodes; choosing it sets the phone's "
            "country) beside a phone widget whose flag combobox is also named \"Country\" and "
            "shows the same dial code.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, GH_COUNTRY, GH_PHONE_COUNTRY_PICKER,
                    RS_INLINE_AUTHORIZATION),
        ),
        Job(
            "bamboohr-like",
            "BWA-BH-181",
            "Paid Media Manager",
            "Marketing",
            "Austin, TX (Hybrid)",
            "A BambooHR-style address block of Fabric selects: a role-less menu button "
            "(aria-haspopup, data-menu-id, an aria-label repeating what it shows) over a hidden "
            "proxy <select> that holds only the chosen option's id and that the <label> names. "
            "The menu is a body portal (a search box and a role=menu of menu items) that opens "
            "on click, Enter, Space or ArrowDown, closes on Escape or a toggle click (never on "
            "an outside press) and stays in the document hidden. Country already shows "
            "\"United States\"; State shows \"\N{EN DASH}Select\N{EN DASH}\".",
            _single(FIRST_NAME, LAST_NAME, EMAIL, BH_STATE, BH_COUNTRY, BH_EDUCATION),
        ),
        Job(
            "bamboohr-conditional",
            "BWA-BH-182",
            "Paid Media Manager",
            "Marketing",
            "Austin, TX (Hybrid)",
            "A BambooHR-style form with conditional fields: choosing Yes or No on the "
            "employment visa sponsorship radio mounts two follow-up questions (a select and "
            "a Yes/No radio) right after it, synchronously (?reveal_ms=<n> delays them), "
            "before the location question that follows. The server validates and records "
            "the follow-ups only when the sponsorship answer was posted.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, BH_SPONSORSHIP, BH_LOCATED),
        ),
        Job(
            "bamboohr-required",
            "BWA-BH-183",
            "Paid Media Manager",
            "Marketing",
            "Austin, TX (Hybrid)",
            "A BambooHR-style form of four Yes/No questions whose visa sponsorship question is "
            "optional (no marker) until the work authorization question above it is answered: "
            "the page then marks it required (an asterisk after the question, required, "
            "aria-required) at once, and the server requires it only when the authorization "
            "answer was posted. No question appears or disappears.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, BH_AUTHORIZED, BH_SPONSORSHIP_ONCE, BH_IN_AUSTIN,
                    BH_ONSITE),
        ),
        Job(
            "bamboohr-churn",
            "BWA-BH-184",
            "Paid Media Manager",
            "Marketing",
            "Austin, TX (Hybrid)",
            "A BambooHR-style form whose Fabric text fields (Address, City, ZIP and the unnamed "
            "\"Date Available\") carry generated ids FabricTextField-<n>: answering either Yes/No "
            "mounts them all again under new ids, values kept, as a React re-render with new keys "
            "does. \"Date Available\" comes after the two Yes/No questions and has no name, so a "
            "runtime knows it only by its generated id; nothing posts it.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, BH_STREET, BH_CITY, BH_ZIP, BH_AUTHORIZED,
                    BH_SPONSORSHIP_ASKED, BH_DATE_AVAILABLE),
        ),
        Job(
            "greenhouse-eeo",
            "BWA-GH-130",
            "Marketing Operations Manager",
            "Marketing",
            "Remote (United States)",
            "A Greenhouse-style form whose voluntary self-identification block follows the custom "
            "question: Gender, \"Are you Hispanic/Latino?\" and Veteran Status (native selects). "
            "Choosing \"No\" for Hispanic/Latino mounts \"Please identify your race\" right after it "
            "and adds a definitions hint to the Hispanic/Latino question's own block (its help text); "
            "the server records the race answer only when \"No\" was posted.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, GH_EEO_AUTHORIZED, GH_EEO_GENDER, GH_EEO_HISPANIC,
                    GH_EEO_VETERAN),
        ),
        Job(
            "paylocity-work-history",
            "BWA-PL-213",
            "Paid Media Manager",
            "Marketing",
            "Denver, CO",
            "A Paylocity-style work-history entry for the most recent role: Company Name "
            "(workHistory.companyName.0), Position (workHistory.position.0), Start Date and End "
            "Date with the format \"MM/YYYY\" shown under them (txt-workHistory-startDate-0, "
            "txt-workHistory-endDate-0, both required) and \"I currently work here\" "
            "(workHistory.currentlyWorkingHere.0). Checking the box disables and hides the end date "
            "(DISABLE_JS); the server then does not require it.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, PW_COMPANY, PW_POSITION, PW_START, PW_END, PW_CURRENT),
        ),
        Job(
            "greenhouse-aria",
            "BWA-GH-131",
            "Paid Social Manager",
            "Marketing",
            "Remote (United States)",
            "A Greenhouse-style form whose choices are ARIA widgets (Radix-style): a required checkbox "
            "group \"How many clients do you currently support?\" (question_7201[]), a required radio "
            "group \"What range of monthly budgets are you used to working with?\" (question_7202, a "
            "role=radiogroup) and a required single checkbox \"Please double-check all the information "
            "provided above. ...\" (question_7203). Every option is a button[role=checkbox|radio] with "
            "aria-checked and a <label for>, beside an aria-hidden, invisible native bubble input that "
            "carries the name and value; ARIA_CHOICE_JS keeps both in step on a click.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, GH_ARIA_CLIENTS, GH_ARIA_BUDGETS, GH_ARIA_DOUBLE_CHECK),
        ),
        Job(
            "teamtailor-late",
            "BWA-TT-172",
            "Demand Generation Director",
            "Marketing",
            "New York, NY (Hybrid)",
            "A Teamtailor-style apply page: a tall job description, then the form, whose first "
            "question, the required custom question \"Linkedin profile\" "
            "(candidate[answers_attributes][0][text]), is an empty placeholder until it scrolls into "
            "view; then LAZY_JS renders it. A runtime that only reads the page never sees it; filling "
            "the first fields brings it into view.",
            _single(TT_LINKEDIN, FIRST_NAME, LAST_NAME, EMAIL, PHONE),
            description_px=2400,
        ),
        Job(
            "paylocity-address",
            "BWA-PL-212",
            "Paid Media Manager",
            "Marketing",
            "Denver, CO (Hybrid)",
            "A Paylocity-style application step before the review page (its Next control is "
            "#btn-submit). The SMS consent and \"Have you worked with us before?\" are "
            "react-widgets DropdownLists: a div combobox owning the listbox it mounts on "
            "opening, named by a <label for> that points at it and by data-for. Country "
            "(already \"United States\") and State (\"Select a state\") are input-selects "
            "without ARIA roles: the <label> wraps the widget, a value div covers the input and "
            "takes the pointer, and typing lists the matches as plain divs. Address Line 1 is a "
            "combobox input that keeps what is typed while it lists longer address suggestions. "
            "A OneTrust banner slides over the bottom half of the page 600 ms after it loads.",
            (Step("Application", (FIRST_NAME, LAST_NAME, EMAIL, PHONE, PC_SMS, PC_WORKED, PC_COUNTRY,
                                  PC_ADDRESS1, PC_ADDRESS2, PC_CITY, PC_STATE, PC_ZIP)),),
            review=True,
            onetrust=True,
            next_id="btn-submit",
        ),
        Job(
            "workable-like",
            "BWA-WK-127",
            "Demand Generation Manager",
            "Marketing",
            "Remote (US)",
            "A Workable-style form: an intl-tel-input phone that shows its dial code apart from "
            "the number (typing +1… leaves only the national digits in the input) and a "
            "drag-and-drop résumé uploader that empties its input once it takes the file and "
            "shows the file's name.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, WK_PHONE, WK_RESUME),
        ),
        Job(
            "teamtailor-like",
            "BWA-TT-171",
            "Growth Marketing Lead",
            "Marketing",
            "Remote (US)",
            "A Teamtailor-style form: Dropzone résumé and additional-files uploaders whose "
            "script-made hidden input takes its label's id. A chosen file hides the input; a "
            "fresh input replaces it and gets the id back only when the upload ends, and the "
            "preview shows \"Uploading…\" for 1.2 s next to a hidden URL input with the same id.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, PHONE, TT_RESUME, TT_FILES),
        ),
        Job(
            "jobvite-like",
            "BWA-JV-190",
            "Paid Media Manager",
            "Marketing",
            "Remote (US)",
            "A Jobvite-style posting: Apply leads to a \"Data Consent\" page (choose a location of "
            "residence and language, then \"I Accept\", which posts back to the apply URL and "
            "returns the form), and the résumé's \"Select\" button opens an attachment popup that "
            "page script appends to <body>, its hidden file input outside the form.",
            _single(JV_RESUME, FIRST_NAME, LAST_NAME, EMAIL, PHONE, JV_REFERRED, JV_SPONSORSHIP),
            data_consent=True,
        ),
        Job(
            "div-combobox-orphan",
            "BWA-RP-126",
            "Brand Marketing Manager",
            "Marketing",
            "Remote (US)",
            "A Rippling-style application rendered without a <form> and posted by page "
            "script: popover div comboboxes that only a choice or an outside press closes "
            "(a labelled Gender question and a custom question named by its paragraph) and "
            "a role-less location lookup that never exposes aria-expanded and answers its "
            "first query slowly.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, RP_POP_GENDER, RP_POP_AUTHORIZATION, RP_POP_LOCATION),
            formless=True,
        ),
        Job(
            "autofill-upload",
            "BWA-AS-130",
            "Revenue Operations Analyst",
            "Operations",
            "Remote (US)",
            "Ashby-style resume parsing: 600 ms after a resume is chosen (the last field), page "
            "script overwrites first name, last name and email with parsed values the candidate "
            "never entered. Only the candidate's own values and resume are accepted.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, PHONE, LINKEDIN, RESUME),
            fixture_identity=True,
        ),
        Job(
            "custom-uploader",
            "BWA-GH-131",
            "Customer Marketing Manager",
            "Marketing",
            "Remote (US)",
            "A styled upload button and drop zone over a hidden, unlabeled file input that page "
            "script clears once the file moves into page state (file chip, asynchronous "
            "\"uploaded\" notice), and an optional label-wrapped cover-letter input.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, CUSTOM_RESUME, COVER_LETTER),
            fixture_identity=True,
        ),
        Job(
            "linkedin-autofill",
            "BWA-LV-132",
            "Marketing Operations Specialist",
            "Marketing",
            "Remote (US)",
            "Lever-style: an \"Apply with LinkedIn\" button that loads late under a transparent "
            "overlay, and a modal autofill prompt; both fill in a LinkedIn member's name and "
            "email, which the server rejects.",
            _single(RESUME, LEVER_NAME, EMAIL, PHONE, LEVER_LOCATION, LEVER_LINKEDIN),
            fixture_identity=True,
        ),
        Job(
            "react-controlled",
            "BWA-RC-133",
            "Retention Marketing Manager",
            "Marketing",
            "Denver, CO (Hybrid)",
            "React-like controlled inputs: a value set by script without a trusted input event "
            "is reverted, and the first typed change re-renders the fields once as new elements.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, PHONE, RESUME),
            fixture_identity=True,
        ),
        Job(
            "react-controlled-narrative",
            "BWA-RC-134",
            "Lifecycle Content Manager",
            "Marketing",
            "Denver, CO (Hybrid)",
            "react-controlled with a multi-line narrative textarea inside the React root: a "
            "script-set value is reverted and the first typed change re-renders the fields.",
            _single(FIRST_NAME, LAST_NAME, EMAIL, WHY_BRAMBLEWAY),
        ),
        Job(
            "changed-after-prepare",
            "BWA-DS-128",
            "Decision Scientist",
            "Data",
            "Denver, CO (Hybrid)",
            "The core questions on the first load of the application page; from the second "
            "load on the form also asks a new required question (willing to travel), so an "
            "application prepared on the first load no longer matches when it is opened "
            "again. The server renders and validates whichever form it served last.",
            _single(*CORE_FIELDS),
            added_on_reload=TRAVEL_REQUIREMENT,
        ),
        Job(
            MODAL_WIZARD,
            "BWA-LI-130",
            "Growth Marketing Lead",
            "Marketing",
            "Remote (US)",
            "A LinkedIn-style job view whose Easy Apply link or button opens a four-step modal dialog "
            "(contact info, saved resume cards, additional questions, review) built by page script over "
            "page-behind decoys; only its final \"Submit application\" contacts the server.",
            (
                Step("Contact info", (EA_EMAIL, EA_PHONE_COUNTRY, EA_PHONE, EA_CITY)),
                Step("Resume", (EA_RESUME,)),
                Step("Additional Questions", (EA_SQL_YEARS, EA_WORK_AUTHORIZATION, EA_SPONSORSHIP)),
                Step("Review your application", (EA_FOLLOW,)),
            ),
        ),
        Job(
            IFRAME_EMBED,
            "BWA-GH-141",
            "Partnerships Manager",
            "Partnerships",
            "Remote (US)",
            "An employer careers page with Role overview and Application tabs; the Application panel "
            "holds the standard form in a Greenhouse-style iframe that page script injects 300 ms after load.",
            STANDARD_FIELDS,
        ),
        Job(
            STEPPER_AMBIGUOUS,
            "BWA-JZ-132",
            "Head of Paid Media",
            "Marketing",
            "Remote (US)",
            "A JazzHR-style form whose only action controls are anchors: \"Submit Application\" validates "
            "and submits by script, beside cookie-consent buttons and a Share anchor outside the form.",
            _single(JZ_FIRST_NAME, JZ_LAST_NAME, JZ_EMAIL, JZ_PHONE, JZ_SALARY, JZ_HEARD, JZ_RESUME,
                    JZ_RESUME_TEXT),
        ),
        Job(
            APPLY_IN_ALERT_FORM,
            "BWA-DF-133",
            "Marketing Project Manager",
            "Marketing",
            "Denver, CO (Hybrid)",
            "A Dayforce-style posting wrapped in one form whose job-alert email and Subscribe button sit "
            "beside Apply; Apply leads to a flow-selection page, then a client-side route to the form.",
            _single(*CORE_FIELDS),
        ),
        Job(
            WD.SLUG,
            WD.CODE,
            WD.TITLE,
            "Marketing",
            WD.LOCATION,
            "A Workday-style application (scripts/mock_workday.py): the posting's Apply opens a "
            "\"Start Your Application\" popup (Autofill with Resume, Apply Manually, Use My Last "
            "Application); Apply Manually shows the Create Account / Sign In step until the user "
            "signs in; then one document runs five pages (My Information, My Experience, "
            "Application Questions, Voluntary Disclosures, Self Identify) and Review with Save "
            "and Continue, a progress list, button dropdowns, a search-on-Enter multi-select "
            "prompt, a Month/Day/Year date, a country phone code beside the number, a résumé "
            "uploader that sends the file on attach, and Submit.",
            _workday_steps(),
            requires_signin=True,
            formless=True,
        ),
    )
}
SCENARIO_JOBS = frozenset(
    {"autofill-upload", "custom-uploader", "linkedin-autofill", "react-controlled",
     "react-controlled-narrative"}
)
"""Jobs whose apply page runs SCENARIO_JS (keyed by the job id), which defines window.__mock."""
REACT_ROOT_FIELDS = frozenset({"first_name", "last_name", "email", "phone"})
"""The react-controlled fields rendered inside ``<div id="react-root">``."""


# --------------------------------------------------------------------------
# Persistent state
# --------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Upload:
    filename: str
    content_type: str
    data: bytes


class Store:
    """JSON-file state: submissions, rejections, uploads, drafts, captchas, sessions and
    job-alert subscriptions.

    Identifiers come from monotonically increasing counters, so a fresh state
    directory always produces the same ids and confirmation references.
    """

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.uploads_dir = self.state_dir / "uploads"
        self.path = self.state_dir / "state.json"
        self.lock = threading.RLock()
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.data = json.loads(self.path.read_text("utf-8"))
            self.data.setdefault("alerts", [])  # state written before alerts existed
        else:
            self.data = self._empty()
            self._save()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema": 1,
            "counters": {
                "submission": 0,
                "rejection": 0,
                "upload": 0,
                "draft": 0,
                "captcha": 0,
                "session": 0,
            },
            "submissions": [],
            "rejections": [],
            "uploads": {},
            "drafts": {},
            "captchas": {},
            "sessions": {},
            "loads": {},
            "alerts": [],
        }

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), "utf-8")
        os.replace(tmp, self.path)

    def _next(self, kind: str) -> int:
        self.data["counters"][kind] += 1
        return self.data["counters"][kind]

    def reset(self) -> None:
        with self.lock:
            for path in self.uploads_dir.iterdir():
                path.unlink()
            self.data = self._empty()
            self._save()

    # application page loads (a form that changes after its first load)
    def count_load(self, job_id: str) -> int:
        with self.lock:
            loads = self.data.setdefault("loads", {})
            loads[job_id] = loads.get(job_id, 0) + 1
            self._save()
            return int(loads[job_id])

    def loads(self, job_id: str) -> int:
        with self.lock:
            return int(self.data.get("loads", {}).get(job_id, 0))

    # uploads
    def store_upload(self, upload: Upload) -> dict[str, Any]:
        with self.lock:
            upload_id = f"upl_{self._next('upload'):06d}"
            path = self.uploads_dir / f"{upload_id}.bin"
            path.write_bytes(upload.data)
            meta = {
                "upload_id": upload_id,
                "filename": upload.filename,
                "content_type": upload.content_type,
                "size": len(upload.data),
                "sha256": hashlib.sha256(upload.data).hexdigest(),
                "stored_path": str(path.resolve()),
            }
            self.data["uploads"][upload_id] = meta
            self._save()
            return meta

    def get_upload(self, upload_id: str | None) -> dict[str, Any] | None:
        with self.lock:
            return self.data["uploads"].get(upload_id or "")

    # submissions
    def add_submission(
        self,
        job: Job,
        fields: dict[str, Any],
        extra_fields: dict[str, Any],
        files: dict[str, dict[str, Any]],
        draft_id: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            n = self._next("submission")
            record = {
                "submission_id": f"sub_{n:06d}",
                "sequence": n,
                "confirmation_reference": f"{REFERENCE_PREFIX}-{n:06d}",
                "job_id": job.slug,
                "job_code": job.code,
                "job_title": job.title,
                "company": COMPANY,
                "received_at": _now(),
                "confirmation_visible": job.visible_confirmation,
                "revealed_at": None,
                "draft_id": draft_id,
                "fields": fields,
                "extra_fields": extra_fields,
                "files": files,
            }
            self.data["submissions"].append(record)
            self._save()
            return record

    def add_rejection(self, job: Job, errors: dict[str, str], step: int | None = None) -> None:
        with self.lock:
            n = self._next("rejection")
            self.data["rejections"].append(
                {
                    "rejection_id": f"rej_{n:06d}",
                    "job_id": job.slug,
                    "step": step,
                    "at": _now(),
                    "errors": errors,
                }
            )
            self._save()

    def add_consent(self, job: Job, policy: str) -> None:
        """A data-processing consent accepted on a Jobvite-style consent page."""
        with self.lock:
            self.data.setdefault("consents", []).append(
                {"job_id": job.slug, "at": _now(), "policy": policy}
            )
            self._save()

    def get_submission(self, submission_id: str) -> dict[str, Any] | None:
        with self.lock:
            return next(
                (s for s in self.data["submissions"] if s["submission_id"] == submission_id),
                None,
            )

    def reveal(self, submission_id: str) -> dict[str, Any] | None:
        with self.lock:
            record = self.get_submission(submission_id)
            if record is not None and not record["confirmation_visible"]:
                record["confirmation_visible"] = True
                record["revealed_at"] = _now()
                self._save()
            return record

    def find_submissions(self, job_id: str, email: str) -> list[dict[str, Any]]:
        email = email.strip().lower()
        with self.lock:
            return [
                s
                for s in self.data["submissions"]
                if s["job_id"] == job_id and str(s["fields"].get("email", "")).lower() == email
            ]

    def summary(self, job_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            subs = [s for s in self.data["submissions"] if job_id in (None, s["job_id"])]
            rejs = [r for r in self.data["rejections"] if job_id in (None, r["job_id"])]
            alerts = [a for a in self.data["alerts"] if job_id in (None, a["job_id"])]
            return {
                "job_id": job_id,
                "accepted_count": len(subs),
                "rejected_count": len(rejs),
                "submissions": subs,
                "rejections": rejs,
                "consents": [c for c in self.data.get("consents", []) if job_id in (None, c["job_id"])],
                "alert_count": len(alerts),
                "alerts": alerts,
            }

    # job-alert subscriptions (never applications)
    def add_alert(self, job: Job, email: str) -> dict[str, Any]:
        with self.lock:
            record = {"job_id": job.slug, "email": email, "received_at": _now()}
            self.data["alerts"].append(record)
            self._save()
            return record

    # multistep drafts
    def save_step(
        self,
        job: Job,
        draft_id: str | None,
        step: int,
        values: dict[str, Any],
        files: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        with self.lock:
            if draft_id is None:
                draft_id = f"dft_{self._next('draft'):06d}"
                self.data["drafts"][draft_id] = {
                    "draft_id": draft_id,
                    "job_id": job.slug,
                    "created_at": _now(),
                    "steps": {},
                    "files": {},
                }
            draft = self.data["drafts"][draft_id]
            draft["steps"][str(step)] = values
            draft["files"].update(files)
            self._save()
            return draft

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self.lock:
            return self.data["drafts"].get(draft_id)

    # captcha
    def new_captcha(self) -> str:
        with self.lock:
            token = f"cap_{self._next('captcha'):06d}"
            digest = hashlib.sha256(f"brambleway-captcha:{token}".encode()).digest()
            answer = "".join(CAPTCHA_ALPHABET[b % len(CAPTCHA_ALPHABET)] for b in digest[:5])
            self.data["captchas"][token] = {"answer": answer, "used": False}
            self._save()
            return token

    def captcha(self, token: str) -> dict[str, Any] | None:
        with self.lock:
            return self.data["captchas"].get(token)

    def consume_captcha(self, token: str, answer: str) -> bool:
        """Single use: any attempt spends the challenge."""
        with self.lock:
            challenge = self.data["captchas"].get(token)
            if challenge is None or challenge["used"]:
                return False
            challenge["used"] = True
            self._save()
            return answer.strip().upper() == challenge["answer"]

    # sign-in sessions
    def new_session(self, email: str) -> str:
        with self.lock:
            n = self._next("session")
            token = hashlib.sha256(f"brambleway-session:{n}".encode()).hexdigest()[:32]
            self.data["sessions"][token] = {"email": email, "created_at": _now()}
            self._save()
            return token

    def has_session(self, token: str) -> bool:
        with self.lock:
            return token in self.data["sessions"]

    def session_email(self, token: str) -> str | None:
        with self.lock:
            session = self.data["sessions"].get(token)
            return str(session["email"]) if session else None

    # workday-wizard: accounts, drafts and what the fixture observed
    def _wd(self) -> dict[str, Any]:
        return self.data.setdefault("workday", {
            "accounts": {}, "drafts": {}, "route_visits": {}, "saves": [], "submit_calls": [],
        })

    def wd_note(self, kind: str, entry: Any) -> None:
        with self.lock:
            state = self._wd()
            if kind == "route_visits":
                state["route_visits"][entry] = state["route_visits"].get(entry, 0) + 1
            else:
                state[kind].append(entry)
            self._save()

    def wd_create_account(self, email: str, password: str) -> bool:
        with self.lock:
            accounts = self._wd()["accounts"]
            if email in accounts or email == SIGNIN_EMAIL:
                return False
            accounts[email] = hashlib.sha256(f"wd-account:{password}".encode()).hexdigest()
            self._save()
            return True

    def wd_account_ok(self, email: str, password: str) -> bool:
        with self.lock:
            digest = self._wd()["accounts"].get(email)
            return digest == hashlib.sha256(f"wd-account:{password}".encode()).hexdigest()

    def wd_draft(self, email: str) -> dict[str, Any]:
        with self.lock:
            drafts = self._wd()["drafts"]
            draft = drafts.setdefault(email, {"pages": {}, "resume": None})
            return json.loads(json.dumps(draft))

    def wd_save_page(self, email: str, page_id: str, values: dict[str, Any]) -> None:
        with self.lock:
            draft = self._wd()["drafts"].setdefault(email, {"pages": {}, "resume": None})
            draft["pages"][page_id] = values
            self._save()

    def wd_set_resume(self, email: str, meta: dict[str, Any]) -> None:
        with self.lock:
            draft = self._wd()["drafts"].setdefault(email, {"pages": {}, "resume": None})
            draft["resume"] = meta
            self._wd().setdefault("uploads", []).append(meta["upload_id"])
            self._save()

    def wd_summary(self) -> dict[str, Any]:
        with self.lock:
            state = json.loads(json.dumps(self._wd()))
            state["accounts"] = sorted(state["accounts"])
            state["submit_call_count"] = len(state["submit_calls"])
            state["accepted_count"] = len([s for s in self.data["submissions"]
                                           if s["job_id"] == WD.SLUG])
            return state


# --------------------------------------------------------------------------
# Request parsing and validation
# --------------------------------------------------------------------------


class HttpError(Exception):
    def __init__(self, status: HTTPStatus, message: str | None = None):
        super().__init__(message or status.phrase)
        self.status = status
        self.message = message or status.phrase


def _header_param(header_value: str, header: str, param: str) -> str | None:
    msg = Message()
    msg[header] = header_value
    value = msg.get_param(param, header=header)
    if value is None:
        return None
    return collapse_rfc2231_value(value)


def parse_multipart(
    body: bytes, content_type: str
) -> tuple[dict[str, list[str]], dict[str, list[Upload]]]:
    """Parse a multipart/form-data body into text fields and file parts."""
    boundary = _header_param(content_type, "content-type", "boundary")
    if not boundary:
        raise HttpError(HTTPStatus.BAD_REQUEST, "multipart boundary missing")
    delimiter = b"\r\n--" + boundary.encode("latin-1")
    fields: dict[str, list[str]] = {}
    files: dict[str, list[Upload]] = {}
    chunks = (b"\r\n" + body).split(delimiter)
    for chunk in chunks[1:]:
        if chunk.startswith(b"--"):
            break
        if not chunk.startswith(b"\r\n"):
            raise HttpError(HTTPStatus.BAD_REQUEST, "malformed multipart body")
        head, sep, content = chunk[2:].partition(b"\r\n\r\n")
        if not sep:
            raise HttpError(HTTPStatus.BAD_REQUEST, "malformed multipart part")
        headers: dict[str, str] = {}
        for line in head.decode("utf-8", "replace").split("\r\n"):
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        name = _header_param(disposition, "content-disposition", "name")
        if name is None:
            continue
        filename = _header_param(disposition, "content-disposition", "filename")
        if filename is None:
            fields.setdefault(name, []).append(content.decode("utf-8", "replace"))
        else:
            filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
            files.setdefault(name, []).append(
                Upload(
                    filename,
                    headers.get("content-type", "application/octet-stream"),
                    content,
                )
            )
    return fields, files


def _required_message(f: Field) -> str:
    if f.kind in ("select", "radio", "custom_combobox", "react_select", "div_combobox",
                  "search_combobox", "react_async", "remote_lookup", "fab_select"):
        return "Select an answer."
    if f.kind in ("rw_dropdown", "pcty_select"):
        return "Select an answer."
    if f.kind == "checkbox":
        return "Check this box to continue."
    if f.multi:
        return "Select at least one option."
    if f.kind in FILE_KINDS:
        return "Attach a file."
    return "This field is required."


def _upload_error(upload: Upload) -> str | None:
    if not upload.filename.lower().endswith(RESUME_EXTENSIONS):
        return "Upload a PDF, DOC, DOCX or TXT file."
    if not upload.data:
        return "The selected file is empty."
    if len(upload.data) > MAX_UPLOAD_BYTES:
        return "The selected file must be smaller than 5 MB."
    return None


def validate(
    fields: tuple[Field, ...],
    form: dict[str, list[str]],
    uploads: dict[str, list[Upload]],
    retained: dict[str, dict[str, Any]],
    strict_phone: bool = False,
) -> tuple[dict[str, Any], dict[str, Upload | dict[str, Any]], dict[str, str]]:
    """Return (clean values, files, errors).

    Files map to a new ``Upload`` or to retained upload metadata. Empty or
    absent optional values are omitted from the clean values.
    """
    values: dict[str, Any] = {}
    files: dict[str, Upload | dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for f in fields:
        if f.disabled or f.unnamed:
            continue  # browsers never submit disabled or unnamed controls
        if f.kind in FILE_KINDS:
            attached = [u for u in uploads.get(f.name, []) if u.filename]
            if attached:
                error = _upload_error(attached[0])
                if error:
                    errors[f.name] = error
                else:
                    files[f.name] = attached[0]
            elif f.name in retained:
                files[f.name] = retained[f.name]
            elif f.required:
                errors[f.name] = _required_message(f)
            continue

        raw = form.get(f.name, [])
        allowed = {o.value for o in f.options if not o.disabled}
        if f.multi:
            chosen = [v for v in raw if v != ""]
            if any(v not in allowed for v in chosen):
                errors[f.name] = "Select only the listed options."
            elif chosen:
                values[f.name] = chosen
            elif f.required:
                errors[f.name] = _required_message(f)
            continue

        distinct = {v.strip() for v in raw if v.strip()}
        if len(distinct) > 1:
            errors[f.name] = "Provide a single answer."
            continue
        value = distinct.pop() if distinct else ""
        if not value:
            if f.required:
                errors[f.name] = _required_message(f)
            continue
        if f.options and value not in allowed:
            # Lookups post only a chosen suggestion: typed text never matches.
            errors[f.name] = "Select one of the listed options."
        elif f.kind == "intl_tel" and (phone_error := _intl_phone_error(value, form)):
            errors[f.name] = phone_error
        elif f.kind == "checkbox" and value != "yes":
            errors[f.name] = "Unexpected value for this checkbox."
        elif f.kind == "email" and not EMAIL_RE.match(value):
            errors[f.name] = "Enter a valid email address, like name@example.com."
        elif f.kind == "url" and not value.startswith(("https://", "http://")):
            errors[f.name] = "Enter a full web address starting with https://."
        elif f.kind == "tel" and strict_phone and not US_PHONE_RE.match(value):
            errors[f.name] = US_PHONE_MESSAGE
        elif len(value) > MAX_TEXT_CHARS:
            errors[f.name] = "Keep this answer under 5,000 characters."
        else:
            values[f.name] = value
            if f.kind == "intl_tel":
                values["phone_country"] = (form.get("phone_country") or [""])[0]
    return values, files, errors


def _intl_phone_error(value: str, form: dict[str, list[str]]) -> str | None:
    """The phone widget's number must be a 10-digit US national number and agree with
    the country its picker shows (posted from page state as ``phone_country``)."""
    country = (form.get("phone_country") or [""])[0]
    digits = re.sub(r"\D", "", value)
    national = digits
    if value.strip().startswith("+"):
        code = next((c[2] for c in ITI_COUNTRIES if c[0] == country), None)
        if code is None or not digits.startswith(code):
            return "The number does not match the selected country."
        national = digits[len(code):]
    if country != "us":
        return "Enter a US phone number."
    if len(national) != 10:
        return "Enter a 10-digit US phone number."
    return None


@functools.cache
def fixture_resume_sha256() -> str | None:
    """sha256 of FIXTURE_RESUME, read once; None when the file is missing."""
    try:
        return hashlib.sha256(FIXTURE_RESUME.read_bytes()).hexdigest()
    except OSError:
        return None


def fixture_identity_errors(
    fields: tuple[Field, ...],
    values: dict[str, Any],
    files: dict[str, Upload | dict[str, Any]],
) -> dict[str, str]:
    """What a ``fixture_identity`` job rejects besides ``validate``: an identity field whose
    value is not exactly the fixture candidate's, and a resume whose bytes are not the
    fixture file (a retained upload is compared by its stored sha256)."""
    errors: dict[str, str] = {}
    names = {f.name for f in fields}
    for name, expected in FIXTURE_IDENTITY.items():
        if name in names and values.get(name) != expected:
            errors[name] = NOT_ENTERED_MESSAGE
    if "resume" in names:
        resume = files.get("resume")
        digest: str | None
        if isinstance(resume, Upload):
            digest = hashlib.sha256(resume.data).hexdigest()
        else:
            digest = (resume or {}).get("sha256")
        expected_digest = fixture_resume_sha256()
        if expected_digest is None or digest != expected_digest:
            errors["resume"] = WRONG_RESUME_MESSAGE
    return errors


def _city_words(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", text).lower().split()


def city_matches(query: str, city: str) -> bool:
    """Suggestion filter of ``/__fixture__/cities`` (at least two query characters)."""
    wanted = _city_words(query)
    if len("".join(wanted)) < 2:
        return False
    words = _city_words(city)
    names = {name.lower(): abbr.lower() for name, abbr in STATE_ABBREVIATIONS.items()}
    aliases = [names[w] for w in words if w in names]
    aliases += [n for n, a in names.items() for w in words if w == a and " " not in n]
    if "usa" in words or "united" in words:
        aliases += ["us", "usa", "united", "states"]
    vocabulary = words + aliases
    return all(any(v.startswith(w) for v in vocabulary) for w in wanted)


def _extra_fields(fields: tuple[Field, ...], form: dict[str, list[str]]) -> dict[str, Any]:
    declared = {f.name for f in fields} | INTERNAL_FIELDS
    return {
        name: vals if len(vals) > 1 else vals[0]
        for name, vals in form.items()
        if name not in declared and vals
    }


def _as_lists(values: dict[str, Any]) -> dict[str, list[str]]:
    return {k: (v if isinstance(v, list) else [v]) for k, v in values.items()}


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


STYLE = """
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;color:#1d2330;background:#f6f7f9;line-height:1.5}
header.site,footer.site{background:#20364f;color:#fff;padding:.8rem 1.5rem}
header.site a{color:#fff;font-weight:600;text-decoration:none}
footer.site{background:#e8ebf0;color:#4a5568;font-size:.85rem;margin-top:3rem}
main{max-width:46rem;margin:0 auto;padding:1.5rem;background:#fff}
.meta{color:#4a5568}
.field{margin:1.25rem 0;border:0;padding:0}
.field>label,legend{display:block;font-weight:600;margin-bottom:.3rem}
.choice{margin:.25rem 0}
.choice label{font-weight:400}
input[type=text],input[type=email],input[type=tel],input[type=url],input[type=password],select,textarea{width:100%;box-sizing:border-box;padding:.45rem;border:1px solid #8a94a6;border-radius:4px;font:inherit}
[aria-invalid=true]{border-color:#b42318;outline:2px solid #b42318}
.hint{color:#4a5568;font-size:.9rem;margin:.1rem 0 .3rem}
.error{color:#b42318;font-weight:600;margin:.2rem 0}
.error-summary{border:3px solid #b42318;padding:.5rem 1rem;margin-bottom:1.5rem}
.error-summary a{color:#b42318}
.optional{font-weight:400;color:#4a5568}
.visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
button,.button{background:#1f6f43;color:#fff;border:0;border-radius:4px;padding:.6rem 1.2rem;font:inherit;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
.progress{display:flex;gap:1rem;list-style:none;padding:0;font-size:.9rem;color:#4a5568}
.progress [aria-current=step]{font-weight:700;color:#1d2330}
dl.review dt{font-weight:600;margin-top:.6rem}
dl.review dd{margin:0}
.notice{border-left:4px solid #20364f;padding:.5rem 1rem;background:#eef2f7}
"""


WIDGET_STYLE = """
.pcty-label{display:block;font-weight:600;margin-bottom:.3rem}
.rw-dropdownlist{display:flex;align-items:center;border:1px solid #8a94a6;border-radius:4px;min-height:38px;
background:#fff;position:relative;cursor:pointer;padding:0 .5rem}
.rw-dropdownlist .rw-input{flex:1}
.rw-dropdownlist-picker{width:1.25rem;height:1.25rem;display:inline-block}
.rw-popup-container{position:absolute;top:100%;left:0;right:0;z-index:30;background:#fff;border:1px solid #8a94a6}
.rw-list{list-style:none;margin:0;padding:0}
.rw-list-option{padding:.4rem .6rem}
.pcty-input-select{position:relative;font-weight:400;margin-top:.3rem}
.pcty-input-select__control{display:flex;align-items:center;border:1px solid #8a94a6;border-radius:4px;min-height:38px;background:#fff}
.pcty-input-select__value-container{position:relative;flex:1;min-height:36px}
.pcty-input-select__input input{width:100%;border:0;min-height:36px;padding:0 .5rem;box-sizing:border-box}
.input-select-input-single-value{position:absolute;inset:0;z-index:2;background:#fff;padding:.5rem;cursor:text}
.input-select-input-placeholder{color:#6b7280}
.pcty-input-select__indicator{display:inline-block;width:1.5rem}
.pcty-input-select__menu{position:absolute;top:100%;left:0;right:0;z-index:30;background:#fff;border:1px solid #8a94a6}
.pcty-input-select__option{padding:.4rem .6rem;cursor:pointer}
.pcty-input-select__option--is-focused{background:#e2e8f0}
.pcty-address{position:relative}
.pcty-address-list{position:absolute;top:100%;left:0;right:0;z-index:30;background:#fff;border:1px solid #8a94a6;
list-style:none;margin:0;padding:0}
.pcty-address-list li{padding:.4rem .6rem}
.tt-upload label{display:block;font-weight:600;margin-bottom:.3rem}
.tt-trigger{position:relative;border:2px dashed #8a94a6;border-radius:6px;padding:1rem;overflow:hidden}
.tt-preview{border:1px solid #cbd5e0;border-radius:6px;padding:.6rem 1rem}
.tt-bar{height:6px;background:#e2e8f0}
.tt-hidden{display:none}
.select__control{display:flex;align-items:center;border:1px solid #8a94a6;border-radius:4px;min-height:38px;background:#fff}
.select__value-container{display:grid;flex:1;padding:2px 8px;align-items:center}
.select__value-container--is-multi{display:flex;flex-wrap:wrap;position:relative}
.select__placeholder,.select__single-value{grid-area:1/1/2/3;color:#4a5568}
.select__value-container--is-multi .select__placeholder{position:absolute;left:8px}
.select__single-value{color:#1d2330}
.select__input-container{grid-area:1/1/2/3;display:grid;flex:1}
input.select__input{border:0;padding:0;margin:0;background:transparent;width:100%;min-width:2px;outline:0}
.select__indicators{display:flex;align-items:center;padding:0 8px;color:#8a94a6}
.select__option{padding:8px 12px}
.select__option--is-focused{background:#deebff}
.select__menu-notice{padding:8px 12px;color:#4a5568}
.select__multi-value{display:inline-flex;align-items:center;background:#e2e8f0;border-radius:2px;margin:2px;padding:0 4px}
.select__multi-value__remove{padding:0 4px;cursor:pointer}
.select__multi-value__remove::after{content:"\\00d7"}
.rip-question{margin:1.25rem 0}
.rip-question-text p{font-weight:600;margin:0 0 .3rem}
.rip-input{position:relative}
.rip-select{border:1px solid #8a94a6;border-radius:4px;padding:.45rem;min-height:1.2rem;background:#fff}
.rip-select p{margin:0}
.rip-list{position:absolute;left:0;right:0;z-index:40;list-style:none;margin:2px 0 0;padding:4px 0;background:#fff;border:1px solid #8a94a6;border-radius:4px;max-height:240px;overflow-y:auto}
.rip-option,.rip-notice{padding:6px 12px}
.rip-question--popover{position:relative}
.rip-popper{position:absolute;left:0;right:0;z-index:45;background:#fff;border:1px solid #8a94a6;border-radius:4px}
.rip-popper .rip-list{position:static;border:0;margin:0}
.rip-popper p{margin:0}
.rip-option--active{background:#deebff}
.fab-field{margin:1.25rem 0}
.fab-field label{display:block;font-weight:600;margin-bottom:.3rem}
.fab-Select{position:relative}
.fab-SelectToggle__container{display:flex;align-items:center;gap:.3rem}
button.fab-SelectToggle{min-width:16rem;text-align:left;border:1px solid #8a94a6;border-radius:4px;background:#fff;padding:.45rem}
.fab-SelectToggle__placeholder{color:#4a5568}
.fab-SelectToggle__toggleButton svg{width:0;height:0}
select.fab-proxy{position:absolute;left:0;bottom:0;width:1px;height:1px;opacity:0;border:0;pointer-events:none}
.fab-portal{position:absolute;z-index:50;flex-direction:column;background:#fff;border:1px solid #8a94a6;border-radius:4px;width:18rem}
.fab-MenuVessel__search{padding:6px}
.fab-MenuList__scrollContainer{max-height:240px;overflow-y:auto}
.fab-MenuOption{padding:6px 12px}
.fab-MenuOption--active{background:#deebff}
.rip-phone{display:flex;gap:.5rem}
.rip-phone .rip-country{position:relative;width:8rem}
.iti{position:relative;display:flex;gap:.5rem;align-items:center}
.iti__selected-country{background:#e8ebf0;color:#1d2330;padding:.45rem .6rem}
.iti__flag{display:inline-block;width:20px;height:14px;background:#8a94a6}
.iti__dropdown-content{position:absolute;top:100%;left:0;z-index:40;background:#fff;border:1px solid #8a94a6;padding:4px;width:18rem}
.iti__hide{display:none}
.iti__country-list{list-style:none;margin:0;padding:0;max-height:200px;overflow-y:auto}
.iti__country{padding:4px 8px}
.iti__dial-code{color:#4a5568;margin-left:.3rem}
"""

WIDGETS_JS = r"""(function () {
  "use strict";
  // Fictional replicas of custom widgets seen on hosted ATS forms. Their state lives
  // in JavaScript only (no hidden inputs); the form's "formdata" event serializes it.
  var state = window.__widgetState = {};
  var hooks = window.__widgetHooks = window.__widgetHooks || {selectNext: {}};
  var norm = function (s) { return String(s || "").replace(/\s+/g, " ").trim().toLowerCase(); };
  var el = function (tag, attrs, text) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (text !== undefined) node.textContent = text;
    return node;
  };
  var config = function (node) { return JSON.parse(node.getAttribute("data-widget")); };
  var shift = function (id, i, n) { return hooks.selectNext && hooks.selectNext[id] ? Math.min(i + 1, n - 1) : i; };
  // Like react-select, which leaves out aria-selected and aria-activedescendant when the
  // user agent names an Apple platform; its select__option--is-selected class stays.
  var apple = /Mac|iPhone|iPad/.test(navigator.userAgent);
  // Fixture control: expose none of the three selection signals.
  var hidden = function (id) { return !!(hooks.hideSelection && hooks.hideSelection[id]); };

  // React-select-like single and multi select (Greenhouse style).
  function reactSelect(shell) {
    var cfg = config(shell);
    var id = cfg.id, multi = !!cfg.multi;
    var input = document.getElementById(id);
    var control = shell.querySelector(".select__control");
    var values = shell.querySelector(".select__value-container");
    var inputBox = shell.querySelector(".select__input-container");
    var st = state[cfg.name] = {value: multi ? (cfg.initial || []) : (cfg.initial || null)};
    var menu = null, focused = -1, shown = [], loading = false, timer = null, remote = [];
    var labelOf = function (value) {
      var hit = cfg.options.filter(function (o) { return o[0] === value; })[0];
      return hit ? hit[1] : value;
    };
    var displayOf = function (value) {
      var label = labelOf(value);
      if (cfg.display === "dial" || cfg.display === "dial-name") { var m = label.match(/\+\d+$/); return m ? m[0] : label; }
      return label;
    };
    var hasValue = function () { return multi ? st.value.length > 0 : st.value !== null; };
    // Like react-select's RequiredInput: a hidden required proxy rendered only while a
    // required select has no value (it is removed as soon as one is chosen).
    function renderRequired() {
      var proxy = shell.querySelector("input.select__required");
      if (!cfg.inline || !cfg.required || hasValue()) { if (proxy) proxy.remove(); return; }
      if (proxy) return;
      proxy = el("input", {"class": "select__required", name: cfg.name, tabindex: "-1", "aria-hidden": "true", value: ""});
      proxy.required = true;
      proxy.style.cssText = "opacity:0;pointer-events:none;position:absolute;bottom:0;left:0;right:0;width:100%;height:1px";
      shell.appendChild(proxy);
    }
    // Greenhouse's ClearIndicator: a real "Clear selection" button while there is a value.
    function renderClear() {
      if (!cfg.clearable) return;
      var indicators = shell.querySelector(".select__indicators");
      var existing = indicators.querySelector("button.select__clear");
      if (!hasValue()) { if (existing) existing.remove(); return; }
      if (existing) return;
      var clear = el("button", {type: "button", "class": "select__clear", "aria-label": "Clear selection",
        "data-testid": "clear-selection"});
      clear.appendChild(el("span", {"aria-hidden": "true"}, "\u00d7"));
      clear.addEventListener("mousedown", function (e) {
        e.preventDefault(); e.stopPropagation();
        st.value = multi ? [] : null;
        renderValue();
      });
      indicators.insertBefore(clear, indicators.firstChild);
    }
    function renderValue() {
      renderRequired();
      renderClear();
      Array.prototype.slice.call(values.children).forEach(function (child) {
        if (child !== inputBox) child.remove();
      });
      if (!hasValue()) {
        values.insertBefore(el("div", {"class": "select__placeholder", id: "react-select-" + id + "-placeholder"},
          "Select..."), inputBox);
        input.setAttribute("aria-describedby", "react-select-" + id + "-placeholder");
        return;
      }
      input.removeAttribute("aria-describedby");
      if (!multi) {
        var single = el("div", {"class": "select__single-value"});
        if (hooks.valueText && hooks.valueText[id] !== undefined) {
          // Fixture control: the value shows this text instead.
          single.textContent = hooks.valueText[id];
        } else if (cfg.display === "dial-name") {
          // Greenhouse: a flag, then <span>{"+"}{dialCode}</span>, i.e. two text nodes.
          single.appendChild(el("div", {"class": "iti__flag iti__" + st.value}));
          var code = el("span", {});
          code.appendChild(document.createTextNode("+"));
          code.appendChild(document.createTextNode(displayOf(st.value).replace(/^\+/, "")));
          single.appendChild(code);
        } else {
          single.textContent = displayOf(st.value);
        }
        values.insertBefore(single, inputBox);
        return;
      }
      st.value.forEach(function (value) {
        var chip = el("div", {"class": "select__multi-value"});
        chip.appendChild(el("div", {"class": "select__multi-value__label"}, labelOf(value)));
        var remove = el("div", {role: "button", "class": "select__multi-value__remove",
          "aria-label": "Remove " + labelOf(value)});
        remove.addEventListener("mousedown", function (e) {
          e.preventDefault(); e.stopPropagation();
          st.value = st.value.filter(function (v) { return v !== value; });
          renderValue(); if (menu) renderMenu();
        });
        chip.appendChild(remove);
        values.insertBefore(chip, inputBox);
      });
    }
    function candidates() {
      if (cfg.async) return remote;
      var text = norm(input.value);
      return cfg.options.filter(function (o) {
        if (multi && st.value.indexOf(o[0]) >= 0) return false;
        var name = cfg.display === "dial-name" ? o[1].replace(/\s*\+\d+$/, "") : o[1];
        return !text || norm(name).indexOf(text) >= 0;
      });
    }
    function notice() {
      if (!cfg.async) return "No options";
      if (input.value.trim().length < 3) return input.value ? "Type at least 3 characters" : "Type to search";
      return loading ? "Loading..." : "No options";
    }
    function paintFocus() {
      var options = menu ? menu.querySelectorAll("[role=option]") : [];
      Array.prototype.forEach.call(options, function (o, i) {
        var chosen = !!shown[i] && (multi ? st.value.indexOf(shown[i][0]) >= 0 : st.value === shown[i][0]);
        o.className = "select__option" + (i === focused ? " select__option--is-focused" : "") +
          (chosen && !hidden(id) ? " select__option--is-selected" : "");
      });
      var active = options[focused];
      input.setAttribute("aria-activedescendant", active && !apple && !hidden(id) ? active.id : "");
    }
    function renderMenu() {
      var list = menu.querySelector("[role=listbox]");
      list.textContent = "";
      shown = loading ? [] : candidates();
      if (!shown.length) {
        list.appendChild(el("div", {"class": "select__menu-notice"}, notice()));
        input.setAttribute("aria-activedescendant", "");
        return;
      }
      if (focused < 0 || focused >= shown.length) focused = 0;
      shown.forEach(function (o, i) {
        var selected = multi ? st.value.indexOf(o[0]) >= 0 : st.value === o[0];
        var option = el("div", {id: "react-select-" + id + "-option-" + i, role: "option",
          "class": "select__option", "aria-selected": String(selected), tabindex: "-1"}, o[1]);
        if (apple || hidden(id)) option.removeAttribute("aria-selected");
        if (cfg.display === "dial-name") option.insertBefore(el("div", {"class": "iti__flag iti__" + o[0]}), option.firstChild);
        option.addEventListener("mousemove", function () { if (focused !== i) { focused = i; paintFocus(); } });
        option.addEventListener("click", function () { choose(i); });
        list.appendChild(option);
      });
      paintFocus();
    }
    function open() {
      if (menu) return;
      var r = control.getBoundingClientRect();
      menu = el("div", {"class": "select__menu", id: "react-select-" + id + "-menu"});
      menu.style.cssText = "position:absolute;z-index:50;background:#fff;border:1px solid #8a94a6;" +
        "border-radius:4px;box-shadow:0 4px 12px rgba(0,0,0,.15);" + (cfg.inline ? "left:0;right:0;top:100%" :
        "left:" + (r.left + window.scrollX) + "px;top:" + (r.bottom + window.scrollY + 2) + "px;width:" + r.width + "px");
      var list = el("div", {"class": "select__menu-list", role: "listbox", id: "react-select-" + id + "-listbox",
        "aria-multiselectable": String(multi)});
      list.style.cssText = "max-height:300px;overflow-y:auto;padding:4px 0";
      menu.appendChild(list);
      // Keep the focus in the input while the pointer is on the menu, like react-select.
      menu.addEventListener("mousedown", function (e) { e.preventDefault(); });
      // Without a menuPortalTarget react-select renders the menu inside its container,
      // so an open menu is part of the form (Greenhouse); otherwise a body portal.
      if (cfg.inline) shell.appendChild(menu); else document.body.appendChild(menu);
      input.setAttribute("aria-expanded", "true");
      input.setAttribute("aria-controls", "react-select-" + id + "-listbox");
      var current = multi ? -1 : candidates().map(function (o) { return o[0]; }).indexOf(st.value);
      focused = current >= 0 ? current : 0;
      renderMenu();
    }
    function close() {
      if (menu) { menu.remove(); menu = null; }
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-controls");
      input.setAttribute("aria-activedescendant", "");
      input.value = "";
      remote = [];
      loading = false;
    }
    function choose(i) {
      var option = shown[shift(id, i, shown.length)];
      // Fixture control: commit another option (by value) than the one clicked.
      if (hooks.selectValue && hooks.selectValue[id]) {
        option = cfg.options.filter(function (o) { return o[0] === hooks.selectValue[id]; })[0] || option;
      }
      if (!option) return;
      if (multi) { st.value = st.value.concat([option[0]]); input.value = ""; renderValue(); renderMenu(); return; }
      st.value = option[0];
      if (cfg.async) cfg.options = [option];
      close();
      renderValue();
      if (cfg.linksPhone) {
        // Greenhouse: the chosen country becomes the phone widget's, and the number gets focus.
        document.dispatchEvent(new CustomEvent("mock-phone-country", {detail: option[0]}));
        var tel = document.querySelector("input[type=tel]");
        if (tel) tel.focus();
      }
    }
    function search() {
      clearTimeout(timer);
      var text = input.value.trim();
      remote = [];
      if (text.length < 3) { loading = false; if (menu) renderMenu(); return; }
      loading = true;
      if (menu) renderMenu();
      timer = setTimeout(function () {
        fetch(cfg.async + encodeURIComponent(text)).then(function (r) { return r.json(); }).then(function (labels) {
          if (input.value.trim() !== text) return;
          remote = labels.map(function (label) { return [label, label]; });
          loading = false;
          if (menu) renderMenu();
        });
      }, 150);
    }
    control.addEventListener("mousedown", function (e) {
      if (e.target !== input) e.preventDefault();
      // A click on the control toggles the menu (a focused, open control closes).
      if (!menu) { input.focus(); open(); } else { close(); }
    });
    input.addEventListener("focus", function () { if (cfg.openOnFocus) open(); });
    input.addEventListener("blur", function () { close(); });
    input.addEventListener("input", function () {
      if (!menu) open();
      focused = 0;
      if (cfg.async) search(); else renderMenu();
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!menu) { open(); return; }
        var n = shown.length;
        if (n) { focused = (focused + (e.key === "ArrowDown" ? 1 : n - 1)) % n; paintFocus(); }
      } else if (e.key === "Enter") {
        if (menu && shown.length) { e.preventDefault(); choose(focused); }
      } else if (e.key === "Tab") {
        if (menu && shown.length && !multi) choose(focused);  // tabSelectsValue, like react-select
      } else if (e.key === "Escape") {
        if (menu) { e.preventDefault(); close(); }
      }
    });
    renderValue();
  }

  // Rippling's popover menu (its Select component): focus or a click opens it, a click on
  // the control while it is open is ignored, Escape and blur do nothing; only a choice
  // or a press outside the control and the popover (a document mousedown) closes it.
  // The list sits in a role=dialog popper inside the question block, not a portal; the
  // aria-label stays the placeholder, which is a <p> while a choice is a bare text node.
  function popoverCombobox(box, cfg) {
    var id = box.id;
    var st = state[cfg.name] = {value: cfg.initial || null};
    var popper = null;
    function display() {
      box.textContent = "";
      if (st.value === null) box.appendChild(el("p", {}, cfg.placeholder));
      else box.appendChild(document.createTextNode(st.value));
    }
    function open() {
      if (popper) return;
      popper = el("div", {role: "dialog", "data-testid": "popper", tabindex: "-1", "class": "rip-popper"});
      popper.appendChild(el("span", {role: "status", "class": "visually-hidden"},
        cfg.options.length + " results available. Press up and down arrow keys to navigate."));
      var list = el("ul", {id: id + "-list", role: "listbox", "data-testid": "menuList",
        "aria-label": cfg.placeholder, "class": "rip-list"});
      cfg.options.forEach(function (label, i) {
        var li = el("li", {id: id + "-list-option-" + i, role: "option", "data-idx": String(i),
          "aria-posinset": String(i + 1), "aria-setsize": String(cfg.options.length),
          "aria-selected": String(label === st.value), "aria-disabled": "false", "class": "rip-option"});
        var outer = el("div", {}), inner = el("div", {"data-testid": "menuListLabel"});
        inner.appendChild(el("p", {}, label));
        outer.appendChild(inner);
        li.appendChild(outer);
        li.addEventListener("click", function () { choose(i); });
        list.appendChild(li);
      });
      popper.appendChild(list);
      box.closest(".rip-question").appendChild(popper);
      box.setAttribute("aria-expanded", "true");
      box.setAttribute("aria-controls", id + "-list");
    }
    function close() {
      if (!popper) return;
      popper.remove();
      popper = null;
      box.setAttribute("aria-expanded", "false");
      box.removeAttribute("aria-controls");
    }
    function choose(i) {
      st.value = cfg.options[shift(id, i, cfg.options.length)];
      close();
      display();
    }
    box.addEventListener("focus", open);
    box.addEventListener("click", open);
    box.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); open(); }
    });
    document.addEventListener("mousedown", function (e) {
      // Fixture control (hooks.stuck): a popover no press outside it closes.
      if (hooks.stuck && hooks.stuck[id]) return;
      if (popper && !box.contains(e.target) && !popper.contains(e.target)) close();
    }, true);
    display();
  }

  // Rippling-style div combobox.
  function divCombobox(box) {
    var cfg = config(box);
    if (cfg.popover) { popoverCombobox(box, cfg); return; }
    var id = box.id;
    var st = state[cfg.name] = {value: cfg.initial || null};
    var list = null, focused = 0, byKeyboard = false, isOpen = false;
    function display() {
      box.textContent = "";
      box.appendChild(el("p", {}, st.value === null ? "Select" : st.value));
    }
    function paint() {
      var options = list.querySelectorAll("[role=option]");
      Array.prototype.forEach.call(options, function (o, i) {
        o.className = i === focused ? "rip-option rip-option--active" : "rip-option";
      });
      box.setAttribute("aria-activedescendant", options[focused] ? options[focused].id : "");
    }
    function open(keyboard) {
      if (isOpen) return;
      byKeyboard = keyboard;
      list = document.getElementById(id + "-list");
      if (!list) {
        list = el("ul", {id: id + "-list", role: "listbox", "class": "rip-list"});
        cfg.options.forEach(function (label, i) {
          var li = el("li", {id: id + "-list-option-" + i, role: "option", "class": "rip-option"}, label);
          li.addEventListener("mousedown", function (e) { e.preventDefault(); });
          li.addEventListener("click", function () { choose(i); });
          list.appendChild(li);
        });
        box.parentNode.appendChild(list);
      }
      Array.prototype.forEach.call(list.querySelectorAll("[role=option]"), function (li) {
        li.setAttribute("aria-selected", String(li.textContent === st.value));
      });
      list.style.display = "";
      focused = Math.max(0, cfg.options.indexOf(st.value));
      isOpen = true;
      box.setAttribute("aria-expanded", "true");
      box.setAttribute("aria-controls", id + "-list");
      paint();
    }
    function close() {
      if (!isOpen) return;
      isOpen = false;
      box.setAttribute("aria-expanded", "false");
      box.removeAttribute("aria-controls");
      box.removeAttribute("aria-activedescendant");
      // A keyboard-opened list is only hidden and stays in the document.
      if (byKeyboard) { list.style.display = "none"; } else { list.remove(); }
      list = null;
    }
    function choose(i) {
      st.value = cfg.options[shift(id, i, cfg.options.length)];
      close();
      display();
    }
    box.addEventListener("click", function () {
      if (cfg.open !== "click") return;
      if (isOpen) close(); else open(false);
    });
    box.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!isOpen) { open(true); return; }
        var n = cfg.options.length;
        focused = (focused + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
        paint();
      } else if (e.key === "Enter" && isOpen) {
        e.preventDefault(); choose(focused);
      } else if (e.key === "Escape" && isOpen) {
        e.preventDefault(); close();
      }
    });
    box.addEventListener("blur", function () { close(); });
    display();
  }

  // Rippling-style search comboboxes and role-less lookup inputs.
  function searchCombobox(input) {
    var cfg = config(input);
    var id = input.id;
    var st = state[cfg.name] = {value: cfg.initial || null};
    var list = null, focused = 0, items = [], timer = null, seq = 0;
    var popper = null, loaded = false;
    function ensureList() {
      if (list) return list;
      list = el("ul", {id: id + "-list", role: "listbox", "class": "rip-list"});
      list.addEventListener("mousedown", function (e) { e.preventDefault(); });
      if (cfg.popover) {
        // Rippling's location input: the list is in a popper dialog, and the input never
        // gets aria-expanded (only aria-controls while the list shows).
        popper = el("div", {role: "dialog", "data-testid": "popper", tabindex: "-1", "class": "rip-popper"});
        popper.appendChild(el("span", {role: "status", "class": "visually-hidden"}, "Results available."));
        list.setAttribute("aria-label", "textbox");
        popper.appendChild(list);
        input.closest(".field").appendChild(popper);
      } else {
        input.parentNode.appendChild(list);
        input.setAttribute("aria-expanded", "true");
      }
      input.setAttribute("aria-controls", id + "-list");
      return list;
    }
    function hide() {
      if (list) { list.remove(); list = null; }
      if (popper) { popper.remove(); popper = null; }
      input.removeAttribute("aria-controls");
      if (!cfg.popover) input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }
    function render(labels, emptyNotice) {
      items = labels;
      if (!labels.length && !emptyNotice) { hide(); return; }
      ensureList().textContent = "";
      if (!labels.length) { list.appendChild(el("li", {"class": "rip-notice"}, emptyNotice)); return; }
      focused = 0;
      labels.forEach(function (label, i) {
        var li = el("li", {id: id + "-list-option-" + i, role: "option", "class": "rip-option",
          "aria-selected": String(label === st.value)}, label);
        li.addEventListener("click", function () { choose(i); });
        list.appendChild(li);
      });
      input.setAttribute("aria-activedescendant", id + "-list-option-0");
    }
    function choose(i) {
      var label = items[shift(id, i, items.length)];
      if (label === undefined) return;
      st.value = label;
      input.value = label;
      hide();
    }
    function matching(text) {
      var q = norm(text);
      return cfg.options.filter(function (o) {
        var n = norm(o);
        return n.indexOf(q) === 0 || n.indexOf(" " + q) >= 0 || norm((cfg.aliases || {})[o]) === q;
      }).slice(0, 20);
    }
    function openAll() {
      if (cfg.showAll) render(cfg.options, "No options");
      else if (cfg.idle && !input.value) render([], cfg.idle);
    }
    function lookup() {
      var text = input.value.trim();
      var mine = ++seq;
      st.value = null;  // typing again un-chooses
      clearTimeout(timer);
      if (!text) { if (cfg.showAll || cfg.idle) openAll(); else hide(); return; }
      if (cfg.remote) {
        if (text.length < (cfg.popover ? 3 : 2)) { hide(); return; }
        // A popover lookup loads its place-search library on the first query (slow).
        var delay = cfg.popover ? (loaded ? 300 : 300 + cfg.lazy) : 150;
        timer = setTimeout(function () {
          loaded = true;
          fetch(cfg.remote + encodeURIComponent(text)).then(function (r) { return r.json(); })
            .then(function (labels) { if (mine === seq) render(labels, ""); });
        }, delay);
        return;
      }
      render(matching(text), "No results");
    }
    input.addEventListener("input", lookup);
    if (cfg.popover) {
      document.addEventListener("mousedown", function (e) {
        if (popper && e.target !== input && !popper.contains(e.target)) hide();
      }, true);
    }
    input.addEventListener("click", function () { if (!list) openAll(); });
    input.addEventListener("keydown", function (e) {
      if (!list) { if (e.key === "ArrowDown" && (cfg.showAll || cfg.idle)) { e.preventDefault(); openAll(); } return; }
      var options = list.querySelectorAll("[role=option]");
      if (e.key === "ArrowDown" && options.length) {
        e.preventDefault(); focused = (focused + 1) % options.length;
        input.setAttribute("aria-activedescendant", options[focused].id);
      } else if (e.key === "Enter" && options.length) {
        e.preventDefault(); choose(focused);
      } else if (e.key === "Escape") {
        e.preventDefault(); hide();
      }
    });
    if (!cfg.popover) input.addEventListener("blur", function () { hide(); });
  }

  // intl-tel-input 18 with separateDialCode and nationalMode (Workable): the flag shows the
  // dial code in its own element; a typed "+<code>" chooses the country and is taken out
  // of the input, which keeps only the national digits.
  function separateDialCode(root, cfg) {
    var input = root.querySelector("input[type=tel]");
    var flagBox = root.querySelector(".iti__selected-flag");
    var list = root.querySelector("[role=listbox]");
    var st = state[cfg.name] = {country: cfg.initial || "us"};
    var byIso = {};
    cfg.countries.forEach(function (c) { byIso[c[0]] = c; });
    function select(iso) {
      var c = byIso[iso];
      if (!c) return;
      st.country = iso;
      flagBox.querySelector(".iti__flag").className = "iti__flag iti__" + iso;
      var dial = flagBox.querySelector(".iti__selected-dial-code");
      dial.textContent = "";
      dial.appendChild(document.createTextNode("+"));
      dial.appendChild(document.createTextNode(c[2]));
      flagBox.setAttribute("title", c[1]);
      Array.prototype.forEach.call(list.children, function (li) {
        li.setAttribute("aria-selected", String(li.getAttribute("data-country-code") === iso));
      });
    }
    function toggle(show) {
      list.className = "iti__country-list" + (show ? "" : " iti__hide");
      flagBox.setAttribute("aria-expanded", String(show));
    }
    input.addEventListener("input", function () {
      var raw = input.value.trim();
      if (raw.charAt(0) !== "+") return;
      var digits = raw.replace(/\D/g, ""), match = null;
      cfg.countries.forEach(function (c) {
        if (digits.indexOf(c[2]) !== 0) return;
        if (!match || c[2].length > match[2].length || (c[2].length === match[2].length && c[0] === st.country)) match = c;
      });
      if (!match) return;
      if (match[2] !== byIso[st.country][2]) select(match[0]);
      input.value = digits.slice(match[2].length);
    });
    flagBox.addEventListener("click", function () { toggle(list.className.indexOf("iti__hide") >= 0); });
    document.addEventListener("mock-phone-country", function (e) { select(e.detail); });
    flagBox.addEventListener("keydown", function (e) { if (e.key === "Escape") toggle(false); });
    Array.prototype.forEach.call(list.children, function (li) {
      li.addEventListener("click", function () { select(li.getAttribute("data-country-code")); toggle(false); });
    });
    select(st.country);
  }

  // intl-tel-input-like phone widget.
  function intlTel(root) {
    var cfg = config(root);
    if (cfg.separate) { separateDialCode(root, cfg); return; }
    var input = root.querySelector("input[type=tel]");
    var button = root.querySelector(".iti__selected-country");
    var dropdown = root.querySelector(".iti__dropdown-content");
    var search = root.querySelector(".iti__search-input");
    var list = root.querySelector("[role=listbox]");
    var flag = button.querySelector(".iti__flag");
    var st = state[cfg.name] = {country: cfg.initial || "us"};
    var byIso = {};
    cfg.countries.forEach(function (c) { byIso[c[0]] = c; });
    function select(iso) {
      var c = byIso[iso];
      st.country = iso;
      flag.className = "iti__flag iti__" + iso;
      button.setAttribute("aria-label", "Change country, selected " + c[1] + " (+" + c[2] + ")");
      button.setAttribute("title", c[1] + " (+" + c[2] + ")");
      Array.prototype.forEach.call(list.children, function (li) {
        li.setAttribute("aria-selected", String(li.getAttribute("data-country-code") === iso));
      });
    }
    function toggle(show) {
      dropdown.className = "iti__dropdown-content" + (show ? "" : " iti__hide");
      button.setAttribute("aria-expanded", String(show));
      if (show) search.focus();
    }
    function format() {
      var raw = input.value;
      if (raw.trim().charAt(0) !== "+") return;
      var digits = raw.replace(/\D/g, "");
      var match = null;
      cfg.countries.forEach(function (c) {
        if (digits.indexOf(c[2]) !== 0) return;
        if (!match || c[2].length > match[2].length || (c[2].length === match[2].length && c[0] === st.country)) match = c;
      });
      if (!match) return;
      if (match[0] !== st.country && match[2] !== byIso[st.country][2]) select(match[0]);
      var national = digits.slice(match[2].length);
      var pretty = national;
      if (match[2] === "1" && national.length > 3) {
        pretty = national.slice(0, 3) + "-" + national.slice(3, 6) + (national.length > 6 ? "-" + national.slice(6) : "");
      } else if (national.length > 4) {
        pretty = national.slice(0, national.length - 4) + " " + national.slice(-4);
      }
      input.value = "+" + match[2] + " " + pretty;
    }
    button.addEventListener("click", function () { toggle(dropdown.className.indexOf("iti__hide") >= 0); });
    search.addEventListener("input", function () {
      var q = norm(search.value);
      Array.prototype.forEach.call(list.children, function (li) {
        li.style.display = !q || norm(li.textContent).indexOf(q) >= 0 ? "" : "none";
      });
    });
    Array.prototype.forEach.call(list.children, function (li) {
      li.addEventListener("click", function () { select(li.getAttribute("data-country-code")); toggle(false); input.focus(); });
    });
    input.addEventListener("input", format);
    select(st.country);
  }

  // Greenhouse's uploader: a visually hidden input behind an Attach button. Once it takes a
  // file it replaces its button container, input included, with the file's name and a
  // Remove button; the file lives in page state.
  function ghUpload(root) {
    var cfg = config(root);
    var st = state[cfg.name] = {value: null};
    var chooser = root.querySelector(".file-upload__wrapper").innerHTML;
    function showChip(group) {
      var wrapper = group.querySelector(".file-upload__wrapper");
      wrapper.textContent = "";
      var chip = el("div", {"class": "file-upload__filename"});
      // Fixture control (hooks.chipText): the chip shows a shortened or other text.
      var shown = hooks.chipText && hooks.chipText[cfg.name] !== undefined ? hooks.chipText[cfg.name] : st.value.name;
      chip.appendChild(el("span", {}, shown));
      var remove = el("button", {type: "button", "class": "btn btn--icon", "aria-label": "Remove file"}, "\u00d7");
      remove.addEventListener("click", function () { st.value = null; wrapper.innerHTML = chooser; bind(group); });
      chip.appendChild(remove);
      wrapper.appendChild(chip);
    }
    function bind(group) {
      var input = group.querySelector("input[type=file]");
      group.querySelector("button.attach").addEventListener("click", function () { input.click(); });
      input.addEventListener("change", function () {
        if (!input.files.length) return;
        st.value = input.files[0];
        window.__filesTaken = (window.__filesTaken || 0) + 1;  // each is an upload
        if (!cfg.async) { showChip(group); return; }
        // As live: the input keeps the file while the upload runs; seconds later the
        // block re-renders (a new element) with the file's name, and so does the page's
        // action area (the submit button moves up a level). Fixture controls:
        // hooks.uploadDelayMs sets the delay; hooks.uploadRenderOn = "focusin" re-renders
        // as the next question takes focus (while a later answer is being written).
        var rerender = function () {
          var block = group.closest(".field-wrapper"), next = block.cloneNode(true);
          block.replaceWith(next);
          showChip(next.querySelector("[role=group]"));
          var row = document.querySelector("form .actions-row");
          if (row) row.replaceWith.apply(row, Array.prototype.slice.call(row.childNodes));
        };
        if (hooks.uploadRenderOn === "focusin") {
          document.addEventListener("focusin", function next(e) {
            if (group.contains(e.target)) return;
            document.removeEventListener("focusin", next, true);
            setTimeout(rerender, 0);
          }, true);
        } else {
          setTimeout(rerender, hooks.uploadDelayMs !== undefined ? hooks.uploadDelayMs : cfg.async);
        }
      });
    }
    bind(root);
  }

  // Workable-style drag-and-drop uploader: it takes the file into page state and empties
  // its input, then shows the name split in two spans (a middle-ellipsis layout, the first
  // space a no-break space) and a Delete button. Fixture controls: hooks.uploadError (the
  // upload fails and an alert is shown instead), hooks.keepFile (the input keeps it).
  function dropzone(root) {
    var cfg = config(root);
    var st = state[cfg.name] = {value: null};
    var input = root.querySelector("input[type=file]");
    var preview = root.querySelector("[data-role=preview]");
    input.addEventListener("change", function () {
      if (!input.files.length) return;
      var file = input.files[0];
      // Fixture control (hooks.keepFile): the input keeps the file, as Workable's does.
      if (!(hooks.keepFile && hooks.keepFile[cfg.name])) input.value = "";
      preview.textContent = "";
      if (hooks.uploadError && hooks.uploadError[cfg.name]) {
        // A string is the site's own message, "{file}" standing for the file's name.
        var message = hooks.uploadError[cfg.name];
        preview.appendChild(el("p", {role: "alert"}, typeof message === "string"
          ? message.replace("{file}", file.name) : "Something went wrong. Please try again."));
        return;
      }
      st.value = file;
      var name = file.name.replace(" ", "\u00a0"), cut = Math.max(0, name.length - 9);
      var box = el("div", {"data-id": "filename"});
      box.appendChild(el("span", {}, name.slice(0, cut)));
      box.appendChild(el("span", {}, name.slice(cut)));
      preview.appendChild(box);
      var remove = el("button", {type: "button", "aria-label": "delete " + file.name}, "Delete");
      remove.addEventListener("click", function () { st.value = null; preview.textContent = ""; });
      preview.appendChild(remove);
    });
  }

  // Teamtailor's uploader: Dropzone makes the hidden file input and the controller hands it
  // the label's id. A chosen file disables and hides it; Dropzone then replaces it with a
  // fresh input (no id), and the preview built from the template (the name hidden behind
  // "Uploading…", a hidden URL input reusing the id) is added. When the upload ends
  // (hooks.uploadDelayMs, default 1200 ms) the name shows, the URL input gets the stored
  // file's address and the fresh input gets the id back.
  function ttUpload(root) {
    var cfg = config(root);
    var st = state[cfg.name] = {value: null};
    var trigger = root.querySelector("[data-target=trigger]");
    var previews = root.querySelector("[data-target=previews]");
    var template = root.querySelector("template[data-target=preview]");
    var label = root.querySelector("label").firstChild.textContent;
    var input = null;
    function accessible() {  // the controller's makeHiddenInputAccessible
      input.id = cfg.id;
      input.style.cssText = "position:absolute;top:0;left:0;height:100%;width:100%;cursor:pointer;opacity:0";
      input.setAttribute("aria-label", "Drop your file or upload, " + label);
    }
    function required() {  // the controller's toggleRequired (on connect, add and remove)
      if (!cfg.required) return;
      input.required = true;
      input.disabled = st.value !== null;
    }
    function mount() {  // Dropzone's setupHiddenFileInput
      if (input) input.remove();
      input = el("input", {type: "file", "class": "dz-hidden-input"});
      if (cfg.accept) input.setAttribute("accept", cfg.accept);
      input.style.cssText = "visibility:hidden;position:absolute;top:0;left:0;height:0;width:0";
      trigger.appendChild(input);
      input.addEventListener("change", function () {
        Array.prototype.forEach.call(input.files, add);
        mount();
      });
    }
    function add(file) {
      st.value = file;
      window.__filesTaken = (window.__filesTaken || 0) + 1;  // each is an upload
      required();
      trigger.classList.add("tt-hidden");
      var preview = template.content.firstElementChild.cloneNode(true);
      preview.querySelector("[data-dz-name]").textContent = file.name;
      Array.prototype.forEach.call(preview.querySelectorAll("[data-dz-remove]"), function (x) {
        x.addEventListener("click", function () {
          st.value = null; preview.remove(); trigger.classList.remove("tt-hidden"); required(); accessible();
        });
      });
      previews.appendChild(preview);
      setTimeout(function () {
        if (!preview.isConnected) return;
        preview.querySelector("[data-target=progress]").remove();
        preview.querySelector("[data-target=name]").classList.remove("tt-hidden");
        var url = preview.querySelector("input[type=text]");
        url.disabled = false;
        url.value = "https://files.example.test/tmp/" + encodeURIComponent(file.name);
        accessible();
      }, hooks.uploadDelayMs !== undefined ? hooks.uploadDelayMs : 1200);
    }
    mount();
    accessible();
    required();
  }

  // Jobvite's attachment button: "Select" opens an "Attachment Options" popup that the
  // directive appends to <body>, outside the form. Its visually hidden file input takes the
  // file into page state (the site uploads it at once and keeps it in the input), the popup
  // closes, and the file's name and a Remove link replace the button.
  var jvInputs = 0;
  function jvUpload(root) {
    var cfg = config(root);
    var st = state[cfg.name] = {value: null};
    var button = root.querySelector("button");
    var list = root.querySelector(".jv-file-list");
    var id = "file-input-" + (jvInputs++);
    var popup = el("div", {"class": "jv-add-attachment", role: "dialog", "aria-label": "Attachment Options",
                           "aria-hidden": "true", tabindex: "-1"});
    popup.hidden = true;
    popup.appendChild(el("div", {"class": "jv-add-attachment-item"})).appendChild(
      el("span", {role: "button", tabindex: "0"}, "Dropbox"));
    var item = popup.appendChild(el("div", {"class": "jv-add-attachment-item"}));
    item.appendChild(el("label", {"for": id})).appendChild(el("span", {role: "button", tabindex: "0"}, "File"));
    var input = item.appendChild(el("input", {id: id, type: "file", accept: cfg.accept,
      style: "position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);border:0"}));
    popup.appendChild(el("div", {"class": "jv-add-attachment-item"})).appendChild(
      el("span", {role: "button", tabindex: "0"}, "Type or Paste " + cfg.document));
    var close = popup.appendChild(el("a", {"class": "jv-close", href: ""}, "Close"));
    document.body.appendChild(popup);
    var show = function (open) {
      popup.hidden = !open;
      popup.setAttribute("aria-hidden", String(!open));
      button.setAttribute("aria-expanded", String(open));
    };
    button.addEventListener("click", function () { show(popup.hidden); });
    close.addEventListener("click", function (e) { e.preventDefault(); show(false); });
    input.addEventListener("change", function () {
      if (!input.files.length) return;
      st.value = input.files[0];
      window.__filesTaken = (window.__filesTaken || 0) + 1;  // each is an upload
      show(false);
      button.parentNode.hidden = true;
      list.textContent = "";
      var li = list.appendChild(el("li"));
      li.appendChild(el("span", {"class": "jv-file-name"}, st.value.name));
      var remove = li.appendChild(el("a", {href: "", "aria-label": "Remove " + cfg.document}, "Remove"));
      remove.addEventListener("click", function (e) {
        e.preventDefault();
        st.value = null;
        list.textContent = "";
        button.parentNode.hidden = false;
      });
    });
  }

  // BambooHR's Fabric select: a role-less menu button over a hidden proxy <select> that
  // holds only the chosen option's id. The menu (a search box and a role=menu of menu
  // items) is a body portal rendered on the first opening and only hidden afterwards; it
  // opens on click, Enter, Space or ArrowDown and closes on Escape or a toggle click, never
  // on an outside press or a blur. Fixture controls: hooks.selectNext[name] takes the next
  // item, hooks.fabIgnore[name] ignores a click on an item (the menu still closes).
  var fabItems = 73;  // menu items are numbered from one page-wide counter as menus open
  function fabSelect(root) {
    var cfg = config(root);
    var toggle = root.querySelector("button.fab-SelectToggle");
    var proxy = root.querySelector("select");
    var value = cfg.initial;
    var portal = null, menu = null, search = null, isOpen = false, active = -1, base = 0;
    var PLACEHOLDER = String.fromCharCode(8211) + "Select" + String.fromCharCode(8211);  // en dashes
    var itemId = function (i) { return cfg.ids ? cfg.options[i][0] : "menu-item-" + (base + i); };
    var labelOf = function (v) {
      var hit = cfg.options.filter(function (o) { return o[0] === v; })[0];
      return hit ? hit[1] : "";
    };
    function render() {
      var shown = value === null ? "" : labelOf(value);
      var guts = toggle.querySelector(".fab-SelectToggle__guts");
      guts.replaceChild(el("div", {"class": shown ? "fab-SelectToggle__content" : "fab-SelectToggle__placeholder"},
        shown || PLACEHOLDER), guts.firstChild);
      toggle.setAttribute("aria-label", cfg.label + " " + (shown || PLACEHOLDER));
      proxy.textContent = "";
      proxy.appendChild(el("option", {value: shown ? value : ""}));
      var holder = toggle.parentNode.querySelector(".fab-SelectToggle__clearButtonContainer");
      if (cfg.clearable && shown && !holder) {
        holder = el("div", {"class": "fab-SelectToggle__clearButtonContainer"});
        var clear = el("button", {type: "button", tabindex: "0", "aria-label": "Clear Selection", "class": "fab-clear"});
        clear.appendChild(el("span", {"aria-hidden": "true"}, String.fromCharCode(215)));
        clear.addEventListener("click", function () { value = null; render(); });
        holder.appendChild(clear);
        toggle.parentNode.appendChild(holder);
      } else if (holder && !shown) {
        holder.remove();
      }
    }
    function filter() {
      var q = norm(search.value);
      Array.prototype.forEach.call(menu.querySelectorAll("[role=menuitem]"), function (item) {
        item.style.display = !q || norm(item.textContent).indexOf(q) >= 0 ? "" : "none";
      });
    }
    function mark() {
      Array.prototype.forEach.call(menu.querySelectorAll("[role=menuitem]"), function (item, i) {
        item.className = "fab-MenuOption" + (i === active ? " fab-MenuOption--active" : "");
      });
      [menu, search].forEach(function (node) {
        if (active >= 0) node.setAttribute("aria-activedescendant", itemId(active));
        else node.removeAttribute("aria-activedescendant");
      });
    }
    function keys(e) {
      var n = cfg.options.length;
      if (e.key === "Escape") {
        e.preventDefault(); close();
      } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        active = active < 0 ? 0 : (active + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
        mark();
      } else if (e.key === "Enter" && active >= 0) {
        e.preventDefault(); choose(active);
      }
    }
    function build() {
      base = fabItems;
      fabItems += cfg.options.length;
      portal = el("div", {"data-fabric-component": "Select Menu", "data-helium-id": cfg.menu, "class": "fab-portal"});
      var vessel = el("div", {"class": "fab-MenuVessel", "data-menu-id": cfg.menu});
      var list = el("div", {"class": "fab-MenuVessel__list"});
      var box = el("div", {"class": "fab-MenuVessel__search"});
      var wrap = el("label", {"class": "fab-MenuSearch"});
      search = el("input", {"aria-label": "Search", "class": "fab-MenuSearch__input", placeholder: "Search...", type: "text"});
      wrap.appendChild(search);
      box.appendChild(wrap);
      menu = el("div", {id: cfg.menu, role: "menu", tabindex: "-1", "class": "fab-MenuList",
        "aria-owns": cfg.options.map(function (o, i) { return itemId(i); }).join(" ")});
      var scroller = el("div", {"class": "fab-MenuList__scrollContainer"});
      cfg.options.forEach(function (o, i) {
        var item = el("div", {"class": "fab-MenuOption", id: itemId(i), role: "menuitem", tabindex: "-1"});
        var content = el("div", {"class": "fab-MenuOption__content"});
        var row = el("div", {"class": "fab-MenuOption__row"});
        row.appendChild(el("div", {}, o[1]));
        content.appendChild(row);
        item.appendChild(content);
        item.addEventListener("mousedown", function (e) { e.preventDefault(); });
        item.addEventListener("click", function () { choose(i); });
        scroller.appendChild(item);
      });
      menu.appendChild(scroller);
      list.appendChild(box);
      list.appendChild(menu);
      vessel.appendChild(list);
      portal.appendChild(vessel);
      search.addEventListener("keydown", keys);
      search.addEventListener("input", filter);
      menu.addEventListener("keydown", keys);
      document.body.appendChild(portal);
    }
    function open() {
      if (isOpen) return;
      if (!portal) build();
      var r = toggle.getBoundingClientRect();
      portal.style.top = (window.scrollY + r.bottom + 2) + "px";
      portal.style.left = (window.scrollX + r.left) + "px";
      portal.style.visibility = "visible";
      portal.style.display = "flex";
      isOpen = true;
      toggle.setAttribute("aria-expanded", "true");
      active = value === null ? -1 : cfg.options.map(function (o) { return o[0]; }).indexOf(value);
      search.value = "";
      filter();
      mark();
      search.focus();
    }
    function close() {
      if (!isOpen) return;
      isOpen = false;
      portal.style.visibility = "hidden";
      portal.style.display = "none";
      toggle.setAttribute("aria-expanded", "false");
      toggle.focus();
    }
    function choose(i) {
      if (!(hooks.fabIgnore && hooks.fabIgnore[cfg.name])) {
        value = cfg.options[shift(cfg.name, i, cfg.options.length)][0];
        render();
      }
      close();
    }
    // Enter and Space reach the button as a click.
    toggle.addEventListener("click", function () { if (isOpen) close(); else open(); });
    toggle.addEventListener("keydown", function (e) {
      if (isOpen && e.key === "Escape") { e.preventDefault(); close(); }
      else if (!isOpen && e.key === "ArrowDown") { e.preventDefault(); open(); }
    });
    render();
  }

  // Paylocity's react-widgets DropdownList: a div combobox (aria-haspopup="true") owning the
  // listbox it mounts inside itself on opening (a click, or Space/ArrowDown while focused);
  // a choice, Escape or a click elsewhere removes it. The choice's text replaces "--" in
  // .rw-input. Fixture control: hooks.selectNext[name] takes the next option.
  function rwDropdown(holder) {
    var cfg = config(holder);
    var box = holder.querySelector("[role=combobox]");
    var display = box.querySelector(".rw-input");
    var st = state[cfg.name] = {value: cfg.initial || null};
    var popup = null;
    var labelOf = function (v) { var hit = cfg.options.filter(function (o) { return o[0] === v; })[0]; return hit ? hit[1] : ""; };
    function render() { display.textContent = st.value === null ? "--" : labelOf(st.value); }
    function close() {
      if (popup) { popup.remove(); popup = null; }
      box.setAttribute("aria-expanded", "false");
      box.removeAttribute("aria-activedescendant");
    }
    function open() {
      if (popup) return;
      popup = el("div", {"class": "rw-popup-container"});
      var list = el("ul", {id: cfg.id + "__listbox", role: "listbox", tabindex: "-1", "class": "rw-list"});
      cfg.options.forEach(function (o, i) {
        var li = el("li", {id: cfg.id + "__option__" + i, role: "option", tabindex: "-1", "class": "rw-list-option",
                           "aria-selected": String(o[0] === st.value)}, o[1]);
        li.addEventListener("mousedown", function (e) { e.preventDefault(); });
        li.addEventListener("click", function (e) {
          e.stopPropagation();
          st.value = cfg.options[shift(cfg.name, i, cfg.options.length)][0];
          render(); close();
        });
        list.appendChild(li);
      });
      popup.appendChild(list);
      box.appendChild(popup);
      box.setAttribute("aria-expanded", "true");
    }
    box.addEventListener("click", function () { if (popup) close(); else open(); });
    box.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { e.preventDefault(); close(); }
      else if (!popup && (e.key === " " || e.key === "ArrowDown")) { e.preventDefault(); open(); }
    });
    document.addEventListener("mousedown", function (e) { if (popup && !box.contains(e.target)) close(); });
    render();
  }

  // Paylocity's input-select, a react-select without ARIA roles: a text input under a
  // "single value" div that covers it and takes the pointer (a press on it focuses the
  // input). Typing lists the matching options as plain divs in the same container; Enter
  // takes the first, a click the one clicked; the value div then shows the choice and the
  // input is emptied. Blur or Escape drops the typed text.
  function pctySelect(root) {
    var cfg = config(root);
    var input = root.querySelector("input");
    var single = root.querySelector(".input-select-input-single-value");
    var st = state[cfg.name] = {value: cfg.initial || null};
    var menu = null, shown = [];
    var labelOf = function (v) { var hit = cfg.options.filter(function (o) { return o[0] === v; })[0]; return hit ? hit[1] : ""; };
    function render() {
      var has = st.value !== null;
      single.textContent = has ? labelOf(st.value) : cfg.placeholder;
      single.className = "input-select-input-single-value" + (has ? "" : " input-select-input-placeholder");
      single.style.visibility = input.value ? "hidden" : "visible";
    }
    function close() { if (menu) { menu.remove(); menu = null; } }
    function list() {
      var q = norm(input.value);
      shown = cfg.options.filter(function (o) { return q && norm(o[1]).indexOf(q) >= 0; });
      close();
      if (!q) return;
      menu = el("div", {"class": "pcty-input-select__menu"});
      var inner = el("div", {"class": "pcty-input-select__menu-list"});
      if (!shown.length) inner.appendChild(el("div", {"class": "pcty-input-select__menu-notice"}, "No options"));
      shown.forEach(function (o, i) {
        var item = el("div", {"class": "pcty-input-select__option" + (i === 0 ? " pcty-input-select__option--is-focused" : "")}, o[1]);
        item.addEventListener("mousedown", function (e) { e.preventDefault(); });
        item.addEventListener("click", function () { choose(o); });
        inner.appendChild(item);
      });
      menu.appendChild(inner);
      root.appendChild(menu);
    }
    function choose(o) { st.value = o[0]; input.value = ""; close(); render(); }
    single.addEventListener("mousedown", function (e) { e.preventDefault(); input.focus(); });
    input.addEventListener("input", function () { render(); list(); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && menu && shown.length) { e.preventDefault(); choose(shown[0]); }
      else if (e.key === "Escape") { close(); input.value = ""; render(); }
    });
    input.addEventListener("blur", function () { close(); input.value = ""; render(); });
    render();
  }

  // Paylocity's address line: a native combobox input that keeps what is typed. Three or
  // more typed characters list the fictional addresses that contain them (longer than
  // what was typed); Escape or a click elsewhere closes the list; a click on a suggestion
  // writes it into the input.
  function addressAutocomplete(root) {
    var cfg = config(root);
    var input = root.querySelector("input");
    var list = document.getElementById(cfg.id + "-autocomplete-list");
    var status = root.querySelector("[role=status]");
    window.__addressPicked = null;
    // ?address_list=late: the list the input names in aria-controls does not exist until
    // there are suggestions to show (a live Paylocity form), so a menu probe finds nothing.
    var late = new URLSearchParams(location.search).get("address_list") === "late";
    var slot = list.parentNode;
    if (late) list.remove();
    function close() { list.hidden = true; list.textContent = ""; input.setAttribute("aria-expanded", "false"); }
    input.addEventListener("input", function () {
      var q = norm(input.value);
      if (q.length < 3) { close(); return; }
      var hits = cfg.suggestions.filter(function (s) { return norm(s).indexOf(q) >= 0; });
      if (late && !list.isConnected) slot.appendChild(list);
      list.textContent = "";
      hits.forEach(function (s, i) {
        var li = el("li", {role: "option", id: cfg.id + "-suggestion-" + i, "aria-selected": "false"}, s);
        li.addEventListener("mousedown", function (e) { e.preventDefault(); });
        li.addEventListener("click", function () { input.value = s; window.__addressPicked = s; close(); });
        list.appendChild(li);
      });
      list.hidden = !hits.length;
      input.setAttribute("aria-expanded", hits.length ? "true" : "false");
      status.textContent = hits.length ? hits.length + " suggestions available" : "";
    });
    input.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
    document.addEventListener("mousedown", function (e) { if (!root.contains(e.target)) close(); });
  }

  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-mount]"), function (holder) {
    holder.innerHTML = JSON.parse(holder.getAttribute("data-widget-mount")).html;
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-kind=rw-dropdown]"), rwDropdown);
  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-kind=pcty-select]"), pctySelect);
  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-kind=address-autocomplete]"), addressAutocomplete);
  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-kind=react-select]"), reactSelect);
  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-kind=div-combobox]"), divCombobox);
  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-kind=fab-select]"), fabSelect);
  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-kind=search-combobox]"), searchCombobox);
  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-kind=intl-tel]"), intlTel);
  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-kind=gh-upload]"), ghUpload);
  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-kind=dropzone]"), dropzone);
  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-kind=tt-upload]"), ttUpload);
  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-kind=jv-upload]"), jvUpload);
  Array.prototype.forEach.call(document.forms, function (form) {
    form.addEventListener("formdata", function (e) {
      Object.keys(state).forEach(function (name) {
        var s = state[name];
        var value = s.country !== undefined ? s.country : s.value;
        if (value === null || value === undefined) return;
        (Array.isArray(value) ? value : [value]).forEach(function (v) { e.formData.append(name, v); });
      });
    });
  });
})();"""


ONETRUST_JS = r"""(function () {
  "use strict";
  // OneTrust's banner: shown shortly after load unless a choice was made on an earlier
  // page; the choice is recorded for the tests and the banner hidden.
  var banner = document.getElementById("onetrust-banner-sdk");
  var params = new URLSearchParams(location.search);
  var delay = parseInt(params.get("cookie_ms") || "600", 10);
  var closed = document.cookie.split("; ").filter(function (c) { return c.indexOf("OptanonAlertBoxClosed=") === 0; })[0];
  window.__cookieChoice = closed ? closed.split("=")[1] : null;
  window.__cookieSettingsOpened = 0;
  function choose(choice) {
    window.__cookieChoice = choice;
    document.cookie = "OptanonAlertBoxClosed=" + choice + "; path=/; max-age=86400";
    banner.classList.remove("ot-show");
  }
  document.getElementById("onetrust-reject-all-handler").addEventListener("click", function () { choose("reject"); });
  document.getElementById("onetrust-accept-btn-handler").addEventListener("click", function () { choose("accept"); });
  document.getElementById("onetrust-pc-btn-handler").addEventListener("click", function () { window.__cookieSettingsOpened += 1; });
  if (!closed) setTimeout(function () { banner.classList.add("ot-show"); }, delay);
})();"""


VALIDITY_JS = r"""(function () {
  "use strict";
  // Like many hosted forms: the submit button stays disabled until every required
  // question is answered, and a text input that loses focus gets aria-invalid="false".
  var form = document.querySelector("form");
  var submit = form.querySelector("button[type=submit]");
  function answered() {
    var state = window.__widgetState || {};
    var natives = Array.prototype.every.call(form.querySelectorAll("[required]"), function (f) {
      if (f.type === "checkbox" || f.type === "radio") return !!form.querySelector('[name="' + f.name + '"]:checked');
      return String(f.value || "").trim() !== "";
    });
    var uploads = Array.prototype.every.call(
      form.querySelectorAll("[data-widget-kind=gh-upload][aria-required=true]"), function (group) {
        var name = JSON.parse(group.getAttribute("data-widget")).name;
        return !!(state[name] && state[name].value);
      });
    return natives && uploads;
  }
  function update() {
    var blocked = !answered();
    if (submit.disabled === blocked) return;
    submit.disabled = blocked;
    submit.setAttribute("aria-disabled", String(blocked));
  }
  form.addEventListener("focusout", function (e) {
    if (e.target.matches("input:not([type=file]):not([role=combobox]), textarea")) {
      e.target.setAttribute("aria-invalid", "false");
    }
  });
  update();
  setInterval(update, 100);
})();"""


JV_CONSENT_JS = r"""(function () {
  "use strict";
  // Jobvite's consent form: choosing a policy shows it with "I Accept" (it submits the form
  // with the policy ids, and the site remembers the consent) and "I Decline" (back to the
  // posting); choosing nothing shows "Back" again.
  var select = document.getElementById("jv-country-select");
  var policy = document.getElementById("jv-policy");
  var back = document.getElementById("jv-back");
  var actions = document.getElementById("jv-accept-reject");
  select.addEventListener("change", function () {
    actions.textContent = "";
    var chosen = !!select.value;
    policy.hidden = !chosen;
    back.hidden = chosen;
    if (!chosen) return;
    var accept = document.createElement("button");
    accept.type = "submit";
    accept.className = "jv-button jv-button-primary";
    accept.textContent = "I Accept";
    accept.addEventListener("click", function () { document.cookie = "bwa_jv_consent=accepted; path=/"; });
    var decline = document.createElement("a");
    decline.className = "jv-button";
    decline.href = back.querySelector("a").getAttribute("href");
    decline.textContent = "I Decline";
    var ids = document.createElement("input");
    ids.type = "hidden";
    ids.name = "policyIds";
    ids.value = JSON.stringify({consentPolicyId: select.value});
    actions.append(accept, " ", decline, ids);
  });
})();"""


ASHBY_LIKE_JS = r"""(function () {
  "use strict";
  // /forms/ashby-like. The date input opens a react-datepicker-like calendar inside its
  // field entry while focused (a month listbox of day options and two unnamed month
  // buttons); a click on a day writes MM/DD/YYYY, and a typed date is kept when it parses
  // as M/D/YYYY (else cleared) once the input loses focus or Enter/Tab/Escape is pressed.
  var input = document.querySelector(".ashby-application-form-input-date");
  var entry = input.closest(".ashby-application-form-field-entry");
  var popper = null;
  function pad(n) { return String(n).padStart(2, "0"); }
  function close() { if (popper) { popper.remove(); popper = null; } }
  function commit() {
    var m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(input.value.trim());
    input.value = m ? pad(m[1]) + "/" + pad(m[2]) + "/" + m[3] : "";
  }
  function open() {
    if (popper) return;
    popper = document.createElement("div");
    popper.className = "react-datepicker-popper ashby-application-form-input-date-popup";
    // Like react-datepicker: positioned above the input (top-start), out of the flow.
    entry.style.position = "relative";
    popper.setAttribute("data-placement", "top-start");
    popper.style.cssText = "position:absolute;bottom:100%;left:0;z-index:5;background:#fff;border:1px solid #ccc";
    var days = "";
    for (var d = 1; d <= 30; d++) {
      days += '<div class="react-datepicker__day" tabindex="-1" role="option" aria-selected="false" ' +
        'aria-label="Choose September ' + d + ', 2026" data-day="' + d + '">' + d + "</div>";
    }
    popper.innerHTML = '<div class="react-datepicker"><div class="react-datepicker__month-container">' +
      '<div class="react-datepicker__header"><h4><span>September 2026</span></h4>' +
      '<button data-direction="previous"></button><button data-direction="next"></button></div>' +
      '<div class="react-datepicker__month" role="listbox" aria-label="month  2026-09">' +
      '<div class="react-datepicker__week">' + days + "</div></div></div></div>";
    popper.addEventListener("mousedown", function (e) { e.preventDefault(); });
    popper.addEventListener("click", function (e) {
      var day = e.target.closest("[data-day]");
      if (day) { input.value = "09/" + pad(day.dataset.day) + "/2026"; close(); }
    });
    entry.appendChild(popper);
  }
  input.addEventListener("focus", open);
  input.addEventListener("click", open);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Escape" || e.key === "Tab" || e.key === "Enter") { commit(); close(); }
  });
  input.addEventListener("blur", function () { commit(); close(); });
  // The location lookup mounts its suggestion portal on focus, as Ashby renders its list
  // into a portal of its own: appended to <body> (?portal=inline: right after the field
  // entry, before the later questions), holding the listbox and a "Powered by Google" link.
  // aria-controls names the portal's wrapper (?owns=listbox: the listbox itself).
  // Suggestions come from /__fixture__/cities for two or more typed characters; a click
  // writes the suggestion into the input and removes the portal, as do Escape and blur.
  var params = new URLSearchParams(location.search);
  var lookup = document.querySelector(".ashby-application-form-input-autocomplete");
  // ?ui=floating is Ashby as it renders live (Sanity): a Floating UI portal
  // (data-floating-ui-portal) holding the listbox that aria-controls names and a focus guard;
  // while suggestions show, everything else is marked aria-hidden with data-aria-hidden
  // ("hide others"); a chosen suggestion is saved first, so the input stays empty until the
  // save returns (?save_ms, 400 ms).
  var floating = params.get("ui") === "floating";
  var saveMs = parseInt(params.get("save_ms") || "400", 10);
  var portal = null;
  var marked = [];
  function markOthers() {
    if (!floating || marked.length || !portal) return;
    var keep = [lookup, portal];
    var walk = function (parent) {
      Array.prototype.forEach.call(parent.children, function (child) {
        if (keep.some(function (k) { return child === k || child.contains(k); })) {
          if (keep.indexOf(child) < 0) walk(child);
        } else if (child.getAttribute("aria-hidden") !== "true") {
          child.setAttribute("aria-hidden", "true");
          child.setAttribute("data-aria-hidden", "true");
          marked.push(child);
        }
      });
    };
    walk(document.body);
  }
  function unmarkOthers() {
    marked.forEach(function (n) { n.removeAttribute("aria-hidden"); n.removeAttribute("data-aria-hidden"); });
    marked = [];
  }
  function unmount() {
    unmarkOthers();
    if (portal) { portal.remove(); portal = null; }
    lookup.setAttribute("aria-expanded", "false");
    lookup.removeAttribute("aria-controls");
  }
  function choose(text) {
    if (!floating) { lookup.value = text; return; }
    lookup.value = "";
    setTimeout(function () { lookup.value = text; }, saveMs);
  }
  function mount() {
    if (portal) return;
    portal = document.createElement("div");
    if (floating) {
      portal.id = ":r2:";
      portal.setAttribute("data-floating-ui-portal", "");
      portal.innerHTML = '<div role="listbox" id=":r0:" tabindex="-1" style="position:absolute;z-index:5;background:#fff"></div>' +
        '<button type="button" tabindex="-1" style="position:fixed;opacity:0;width:1px;height:1px"></button>';
    } else {
      portal.id = "ashby-location-portal";
      portal.className = "ashby-autocomplete-portal";
      portal.innerHTML = '<div class="ashby-autocomplete-menu"><div role="listbox" id="ashby-location-listbox"></div>' +
        '<div class="ashby-autocomplete-footer"><a href="https://maps.example.test/attribution" target="_blank">Powered by Google</a></div></div>';
    }
    portal.addEventListener("mousedown", function (e) { e.preventDefault(); });
    portal.addEventListener("click", function (e) {
      var option = e.target.closest("[role=option]");
      if (!option) return;
      e.preventDefault();
      var text = option.textContent;
      unmount();
      choose(text);
    });
    if (params.get("portal") === "inline") {
      lookup.closest(".ashby-application-form-field-entry").after(portal);
    } else {
      document.body.appendChild(portal);
    }
    var list = portal.querySelector("[role=listbox]");
    lookup.setAttribute("aria-controls", floating || params.get("owns") === "listbox" ? list.id : portal.id);
  }
  var pending = 0;
  lookup.addEventListener("focus", mount);
  lookup.addEventListener("input", function () {
    mount();
    var query = lookup.value.trim();
    var ticket = ++pending;
    var list = portal.querySelector("[role=listbox]");
    if (query.length < 2) { list.innerHTML = ""; lookup.setAttribute("aria-expanded", "false"); unmarkOthers(); return; }
    fetch("/__fixture__/cities?style=long&q=" + encodeURIComponent(query)).then(function (r) { return r.json(); }).then(function (cities) {
      if (ticket !== pending || !portal) return;
      list.innerHTML = cities.map(function (c, i) {
        return '<div role="option" id="ashby-location-option-' + i + '" aria-selected="false">' + c + "</div>";
      }).join("");
      lookup.setAttribute("aria-expanded", cities.length ? "true" : "false");
      if (cities.length) markOthers(); else unmarkOthers();
    });
  });
  lookup.addEventListener("keydown", function (e) { if (e.key === "Escape") unmount(); });
  lookup.addEventListener("blur", unmount);
  // Options named after their own text act as one group (React re-renders the others).
  document.querySelectorAll("fieldset").forEach(function (box) {
    var radios = Array.prototype.slice.call(box.querySelectorAll("input[type=radio]"));
    radios.forEach(function (r) {
      r.addEventListener("change", function () {
        radios.forEach(function (o) { if (o !== r) o.checked = false; });
      });
    });
  });
  // Yes/no: two type="submit" buttons with aria-pressed; the hidden checkbox mirrors "yes".
  document.querySelectorAll(".ashby-application-form-input-yesno").forEach(function (box) {
    var buttons = box.querySelectorAll("button[aria-pressed]");
    var mirror = box.querySelector("input[type=checkbox]");
    buttons.forEach(function (b) {
      b.addEventListener("click", function (e) {
        e.preventDefault();
        buttons.forEach(function (o) { o.setAttribute("aria-pressed", String(o === b)); });
        mirror.checked = b.dataset.option === "yes";
      });
    });
  });
})();"""


AUTOFILL_JS = r"""(function () {
  "use strict";
  // Like Greenhouse: the page-level autofill button is disabled while a keystroke is
  // handled, then enabled again.
  var button = document.getElementById("autofill-application");
  document.addEventListener("input", function () {
    button.disabled = true;
    button.setAttribute("aria-disabled", "true");
    clearTimeout(button.__timer);
    button.__timer = setTimeout(function () {
      button.disabled = false;
      button.setAttribute("aria-disabled", "false");
    }, 120);
  }, true);
})();"""


FORMLESS_JS = r"""(function () {
  "use strict";
  // Posts a form-less application the way the form's own submission would: the named
  // controls, then the widgets' page state (see WIDGETS_JS), as multipart form data.
  var root = document.getElementById("application");
  document.getElementById("submit-application").addEventListener("click", function () {
    var data = new FormData();
    Array.prototype.forEach.call(root.querySelectorAll("input[name],select[name],textarea[name]"), function (f) {
      if (f.disabled) return;
      if (f.type === "file") { Array.prototype.forEach.call(f.files, function (x) { data.append(f.name, x); }); return; }
      if ((f.type === "checkbox" || f.type === "radio") && !f.checked) return;
      if (f.tagName === "SELECT") {
        Array.prototype.forEach.call(f.selectedOptions, function (o) { data.append(f.name, o.value); });
        return;
      }
      data.append(f.name, f.value);
    });
    var state = window.__widgetState || {};
    Object.keys(state).forEach(function (name) {
      var s = state[name];
      var value = s.country !== undefined ? s.country : s.value;
      if (value === null || value === undefined) return;
      (Array.isArray(value) ? value : [value]).forEach(function (v) { data.append(name, v); });
    });
    fetch(root.getAttribute("data-action"), {method: "POST", body: data}).then(function (response) {
      return response.text().then(function (html) {
        history.replaceState(null, "", response.url);
        document.open(); document.write(html); document.close();
      });
    });
  });
})();"""


REVEAL_JS = r"""(function () {
  "use strict";
  // BambooHR-style conditional fields: the follow-up questions a radio group's
  // <template data-reveals-for> holds are mounted right after the group's block the
  // moment a (trigger) option is chosen, synchronously like a React state update, or
  // ?reveal_ms=<n> later. Choosing a non-trigger option removes them again. On load a
  // trigger already checked mounts them unless the server rendered them already.
  // window.__mock.reveals counts the mounts for tests.
  var mock = window.__mock = window.__mock || {log: []};
  mock.reveals = 0;
  var params = new URLSearchParams(location.search);
  var delay = parseInt(params.get("reveal_ms"), 10);
  if (isNaN(delay) || delay < 0) delay = 0;
  Array.prototype.forEach.call(document.querySelectorAll("template[data-reveals-for]"), function (template) {
    var name = template.getAttribute("data-reveals-for");
    var on = template.getAttribute("data-reveals-on") || "";
    var first = template.content.querySelector("[id]");
    var mounted = [];
    // data-reveal-hint: help text the trigger's own block (right before the template)
    // shows while the follow-ups are shown (Greenhouse's Hispanic/Latino question).
    var hintText = template.getAttribute("data-reveal-hint") || "";
    var block = template.previousElementSibling;
    var hint = null;
    function shown() { return !!(first && document.getElementById(first.id)); }
    function mount() {
      if (shown()) return;
      var fragment = template.content.cloneNode(true);
      // ?reveal_extra=1: the reveal also adds a button that submits nothing (a definitions
      // link drawn as a button), a control outside the revealed questions.
      if (params.get("reveal_extra") === "1") {
        var extra = document.createElement("button");
        extra.type = "button";
        extra.className = "reveal-extra";
        extra.textContent = "Show definitions";
        fragment.appendChild(extra);
      }
      mounted = Array.prototype.slice.call(fragment.childNodes);
      template.parentNode.insertBefore(fragment, template.nextSibling);
      if (hintText && block && !hint) {
        hint = document.createElement("p");
        hint.className = "hint";
        hint.setAttribute("data-revealed-hint", "");
        hint.textContent = hintText;
        block.appendChild(hint);
      }
      mock.reveals++;
      mock.log.push({t: Math.round(performance.now()), event: "reveal", detail: name});
    }
    function unmount() {
      mounted.forEach(function (node) { if (node.parentNode) node.parentNode.removeChild(node); });
      mounted = [];
      if (hint && hint.parentNode) hint.parentNode.removeChild(hint);
      hint = null;
    }
    function triggers(value) { return on === "" || value === on; }
    Array.prototype.forEach.call(document.querySelectorAll('input[name="' + CSS.escape(name) + '"]'), function (radio) {
      radio.addEventListener("change", function () {
        if (!radio.checked) return;
        if (triggers(radio.value)) { if (delay) setTimeout(mount, delay); else mount(); }
        else unmount();
      });
      if (radio.checked && triggers(radio.value)) mount();
    });
    // A select triggers them too (Greenhouse: "No" to Hispanic/Latino shows the race question).
    Array.prototype.forEach.call(document.querySelectorAll('select[name="' + CSS.escape(name) + '"]'), function (select) {
      select.addEventListener("change", function () {
        if (select.value && triggers(select.value)) { if (delay) setTimeout(mount, delay); else mount(); }
        else unmount();
      });
      if (select.value && triggers(select.value)) mount();
    });
  });
  // BambooHR-style conditional requiredness: a question [data-required-after=<group>]
  // becomes required (an asterisk after its question, required, aria-required) the
  // moment an option of that group is chosen. window.__mock.required counts the marks.
  mock.required = 0;
  Array.prototype.forEach.call(document.querySelectorAll("[data-required-after]"), function (box) {
    var name = box.getAttribute("data-required-after");
    function mark() {
      if (box.getAttribute("aria-required") === "true") return;
      box.setAttribute("aria-required", "true");
      Array.prototype.forEach.call(box.querySelectorAll("input"), function (input) { input.required = true; });
      var legend = box.querySelector("legend");
      if (legend) {
        var star = document.createElement("span");
        star.setAttribute("aria-hidden", "true");
        star.textContent = "*";
        legend.appendChild(document.createTextNode(" "));
        legend.appendChild(star);
      }
      mock.required++;
      mock.log.push({t: Math.round(performance.now()), event: "required", detail: box.id});
    }
    Array.prototype.forEach.call(document.querySelectorAll('input[name="' + CSS.escape(name) + '"]'), function (radio) {
      radio.addEventListener("change", function () { if (radio.checked) mark(); });
      if (radio.checked) mark();
    });
  });
})();"""


FABRIC_JS = r"""(function () {
  "use strict";
  // BambooHR (Fabric UI): text fields carry generated ids (FabricTextField-<n>, one
  // page-wide counter) that a React re-render with new keys hands out afresh. Answering a
  // Yes/No mounts every such field again under the next free numbers, value kept, its
  // label's for following it. window.__mock.rerenders counts the re-renders.
  var mock = window.__mock = window.__mock || {log: []};
  mock.rerenders = 0;
  var next = 0;
  Array.prototype.forEach.call(document.querySelectorAll("input[data-fabric]"), function (input) {
    next = Math.max(next, parseInt(input.id.split("-").pop(), 10) + 1);
  });
  function remount() {
    Array.prototype.forEach.call(document.querySelectorAll("input[data-fabric]"), function (old) {
      var label = document.querySelector('label[for="' + CSS.escape(old.id) + '"]');
      var fresh = old.cloneNode(false);
      fresh.id = "FabricTextField-" + (next++);
      fresh.value = old.value;
      if (label) label.htmlFor = fresh.id;
      old.replaceWith(fresh);
    });
    mock.rerenders++;
    mock.log.push({t: Math.round(performance.now()), event: "rerender"});
  }
  Array.prototype.forEach.call(document.querySelectorAll('input[type="radio"]'), function (radio) {
    radio.addEventListener("change", function () { if (radio.checked) remount(); });
  });
})();"""

DISABLE_JS = r"""(function () {
  "use strict";
  // Paylocity's work history: while "I currently work here" is checked, the entry's end date
  // is disabled and hidden (never posted); unchecking brings it back, required again.
  Array.prototype.forEach.call(document.querySelectorAll("[data-disabled-by]"), function (block) {
    var box = document.querySelector('input[name="' + CSS.escape(block.getAttribute("data-disabled-by")) + '"]');
    var input = block.querySelector("input, select, textarea");
    if (!box || !input) return;
    function sync() {
      input.disabled = box.checked;
      input.required = !box.checked && block.hasAttribute("data-required");
      block.hidden = box.checked;
    }
    box.addEventListener("change", sync);
    sync();
  });
})();"""

ARIA_CHOICE_JS = r"""(function () {
  "use strict";
  // Radix-style ARIA options: a click toggles a role="checkbox" (or checks a role="radio"
  // and unchecks the other radios of its radiogroup), keeping aria-checked, data-state and
  // the aria-hidden bubble input's checked (the form post) together.
  var mock = window.__mock = window.__mock || {log: []};
  mock.ariaClicks = 0;
  function set(button, on) {
    button.setAttribute("aria-checked", on ? "true" : "false");
    button.setAttribute("data-state", on ? "checked" : "unchecked");
    var bubble = button.nextElementSibling;
    if (bubble && bubble.tagName === "INPUT") bubble.checked = on;
  }
  Array.prototype.forEach.call(document.querySelectorAll('button[role="checkbox"], button[role="radio"]'), function (button) {
    button.addEventListener("click", function () {
      mock.ariaClicks++;
      if (button.getAttribute("role") === "checkbox") { set(button, button.getAttribute("aria-checked") !== "true"); return; }
      var group = button.closest('[role="radiogroup"]');
      Array.prototype.forEach.call(group ? group.querySelectorAll('button[role="radio"]') : [button], function (other) {
        set(other, other === button);
      });
    });
  });
})();"""

LAZY_JS = r"""(function () {
  "use strict";
  // Teamtailor renders its custom questions late: a question's block is an empty
  // placeholder until it scrolls into view, then its template is mounted in place.
  // window.__mock.lazyMounts counts the mounts.
  var mock = window.__mock = window.__mock || {log: []};
  mock.lazyMounts = 0;
  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      var block = entry.target;
      var template = block.querySelector("template[data-lazy-question]");
      observer.unobserve(block);
      if (!template) return;
      block.replaceChildren(template.content.cloneNode(true));
      block.removeAttribute("style");
      if (new URLSearchParams(location.search).get("lazy_extra") === "1") {
        // ?lazy_extra=1: the question comes with a button that submits nothing ("Clear").
        var clear = document.createElement("button");
        clear.type = "button";
        clear.textContent = "Clear";
        block.appendChild(clear);
      }
      mock.lazyMounts++;
      mock.log.push({t: Math.round(performance.now()), event: "lazy-mount"});
    });
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-lazy-block]"), function (block) {
    observer.observe(block);
  });
})();"""


SCENARIO_STYLE = """
.visually-hidden{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
.spinner{display:inline-block;width:.8rem;height:.8rem;margin-right:.4rem;border:2px solid #8a94a6;border-top-color:transparent;border-radius:50%;vertical-align:-1px}
#resume-parse-status{margin-top:.4rem;color:#4a5568}
.uploader>.label{font-weight:600;margin-bottom:.3rem}
.dropzone{border:2px dashed #8a94a6;border-radius:6px;padding:.8rem 1rem}
.dropzone__text{margin-right:1rem;color:#4a5568}
.file-chip{display:inline-block;margin-top:.4rem;padding:.2rem .6rem;background:#e2e8f0;border-radius:4px}
.file-chip[hidden]{display:none}
.file-chip__remove{background:transparent;color:#1d2330;padding:0 .3rem}
.upload-notice{color:#4a5568;font-size:.9rem;margin-top:.3rem}
.upload-label{display:inline-block;border:1px solid #8a94a6;border-radius:4px;padding:.45rem .9rem;cursor:pointer}
#awli{position:relative;display:inline-block;margin:.5rem 0}
#awli .awli-button{display:block;background:#0a66c2}
.awli-overlay{position:absolute;inset:0;background:transparent;pointer-events:auto;cursor:pointer}
.autofill-prompt{position:fixed;top:3rem;left:1rem;right:1rem;z-index:1000;max-width:28rem;margin:0 auto;padding:1rem 1.5rem;background:#fff;border:1px solid #8a94a6;border-radius:6px;box-shadow:0 8px 24px rgba(0,0,0,.25)}
"""

SCENARIO_JS = r"""(function () {
  "use strict";
  // Fictional replicas of hosted ATS pages whose scripts change or hide what a runtime
  // fills in. window.__mock records what happened for tests (counters and a timed log);
  // timings come from the page's query string (?autofill_ms=250), else the defaults.
  var scenario = document.currentScript.getAttribute("data-scenario");
  var params = new URLSearchParams(location.search);
  var mock = window.__mock = {log: []};
  var byId = function (id) { return document.getElementById(id); };
  var log = function (event, detail) {
    var entry = {t: Math.round(performance.now()), event: event};
    if (detail !== undefined) entry.detail = String(detail);
    mock.log.push(entry);
  };
  var ms = function (name, fallback) {
    var n = parseInt(params.get(name), 10);
    return isNaN(n) || n < 0 ? fallback : n;
  };
  var counters = function (names) { names.forEach(function (name) { mock[name] = 0; }); };
  var firstFile = function (input) { return input.files && input.files.length ? input.files[0] : null; };
  // A value written by page script: assignment plus bubbling (untrusted) input and change.
  var assign = function (id, value) {
    var input = byId(id);
    input.value = value;
    input.dispatchEvent(new Event("input", {bubbles: true}));
    input.dispatchEvent(new Event("change", {bubbles: true}));
  };
  var afterLoad = function (fn) {
    if (document.readyState === "complete") fn(); else window.addEventListener("load", fn);
  };
  // Every trusted input event on a form control (typing); script-made events are not logged.
  document.addEventListener("input", function (e) {
    var t = e.target;
    if (e.isTrusted && t && /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName)) log("input:" + (t.name || t.id));
  }, true);

  // Ashby-style resume parser: autofill_ms after each resume change it overwrites the
  // contact fields with parsed values the candidate never entered.
  function autofillUpload() {
    counters(["uploads", "autofills"]);
    var input = byId("f-resume");
    input.addEventListener("change", function () {
      var file = firstFile(input);
      if (!file) return;
      mock.uploads++;
      log("upload", file.name);
      var status = byId("resume-parse-status");
      if (!status) {
        status = document.createElement("div");
        status.id = "resume-parse-status";
        status.setAttribute("role", "status");
        status.setAttribute("aria-live", "polite");
        input.parentNode.insertBefore(status, input.nextSibling);
      }
      status.setAttribute("aria-busy", "true");
      status.innerHTML = '<span class="spinner" aria-hidden="true"></span>Parsing your resume\u2026';
      setTimeout(function () {
        assign("f-first_name", "A.");
        assign("f-last_name", "Quill (resume)");
        assign("f-email", "a.quill@resume-parser.example.test");
        mock.autofills++;
        log("autofill");
        status.textContent = "We filled in some fields from your resume.";
        status.setAttribute("aria-busy", "false");
      }, ms("autofill_ms", 600));
    });
  }

  // Greenhouse-style uploader: a styled button and drop zone over a hidden, unlabeled
  // input that is cleared once the file moves into page state (a chip, then an
  // asynchronous notice); the form's formdata event posts the file. Beside it a
  // label-wrapped cover-letter input that keeps its file.
  function customUploader() {
    counters(["uploads", "coverUploads"]);
    mock.files = {};
    // Mounted by script like the rest of a client-rendered form.
    Array.prototype.forEach.call(document.querySelectorAll("[data-mount-html]"), function (holder) {
      holder.outerHTML = holder.getAttribute("data-mount-html");
    });
    var input = byId("resume-input"), chip = byId("resume-chip"), notice = byId("resume-notice");
    var timer = null;
    byId("resume-button").addEventListener("click", function () { input.click(); });
    var zone = byId("resume-dropzone");
    zone.addEventListener("dragover", function (e) { e.preventDefault(); });
    zone.addEventListener("drop", function (e) {
      e.preventDefault();
      var dropped = e.dataTransfer ? e.dataTransfer.files : [];
      if (!dropped.length) return;
      var transfer = new DataTransfer();
      Array.prototype.forEach.call(dropped, function (file) { transfer.items.add(file); });
      input.files = transfer.files;
      input.dispatchEvent(new Event("change", {bubbles: true}));
    });
    input.addEventListener("change", function () {
      var file = firstFile(input);
      if (!file) return;
      mock.uploads++;
      log("upload", file.name);
      mock.files.resume = file;
      input.value = "";
      chip.innerHTML = '<span class="file-chip__name"></span> <span class="file-chip__size"></span> ' +
        '<button type="button" class="file-chip__remove" aria-label="Remove file">\u00d7</button>';
      chip.querySelector(".file-chip__name").textContent = file.name;
      chip.querySelector(".file-chip__size").textContent = "(" + file.size + " bytes)";
      chip.hidden = false;
      notice.textContent = "Uploading\u2026";
      notice.setAttribute("aria-busy", "true");
      clearTimeout(timer);
      timer = setTimeout(function () {
        notice.textContent = file.name + " uploaded";
        notice.setAttribute("aria-busy", "false");
        log("uploaded", file.name);
      }, ms("upload_ms", 800));
    });
    chip.addEventListener("click", function (e) {
      if (!e.target.closest(".file-chip__remove")) return;
      clearTimeout(timer);
      delete mock.files.resume;
      chip.textContent = "";
      chip.hidden = true;
      notice.textContent = "";
      notice.removeAttribute("aria-busy");
      log("removed");
    });
    var cover = byId("cover-letter-input"), coverChip = byId("cover-letter-chip");
    cover.addEventListener("change", function () {
      var file = firstFile(cover);
      coverChip.textContent = file ? file.name : "";
      coverChip.hidden = !file;
      if (!file) return;
      mock.coverUploads++;
      log("cover-upload", file.name);
    });
    input.form.addEventListener("formdata", function (e) {
      var file = mock.files.resume;
      if (file) e.formData.set("resume", file, file.name);
    });
  }

  // Lever-style "Apply with LinkedIn": a button that finishes loading late under a
  // transparent overlay, and a modal autofill prompt. Both fill in a LinkedIn member.
  function linkedinAutofill() {
    counters(["overlayClicks", "linkedinClicks", "promptShown", "promptDismissed", "promptAccepted",
      "uploads"]);
    var button = byId("linkedin-apply"), resume = byId("f-resume"), main = byId("main");
    var mode = params.get("prompt") || "load";
    var prompt = "idle";  // idle, pending, shown, closed: a closed prompt never returns
    var fromLinkedIn = function () {
      assign("f-name", "LinkedIn Member");
      assign("f-email", "member@linkedin.example.test");
    };
    byId("linkedin-overlay").addEventListener("click", function () {
      mock.overlayClicks++;
      log("overlay-click");
    });
    button.addEventListener("click", function () {
      mock.linkedinClicks++;
      log("linkedin-click");
      fromLinkedIn();
    });
    function closePrompt() {
      byId("autofill-prompt").remove();
      prompt = "closed";
      main.removeAttribute("inert");
      main.removeAttribute("aria-hidden");
    }
    function showPrompt() {
      if (prompt !== "pending") return;
      prompt = "shown";
      document.body.insertAdjacentHTML("beforeend", [
        '<div id="autofill-prompt" role="dialog" aria-modal="true" aria-labelledby="autofill-prompt-title" class="autofill-prompt">',
        '  <h2 id="autofill-prompt-title">Autofill your application?</h2>',
        "  <p>Import your details from LinkedIn to fill in this form faster.</p>",
        '  <button type="button" id="autofill-prompt-accept">Autofill with LinkedIn</button>',
        '  <button type="button" id="autofill-prompt-dismiss">No thanks</button>',
        "</div>"
      ].join("\n"));
      main.setAttribute("inert", "");
      main.setAttribute("aria-hidden", "true");
      mock.promptShown++;
      log("prompt-shown");
      byId("autofill-prompt-dismiss").addEventListener("click", function () {
        closePrompt();
        mock.promptDismissed++;
        log("prompt-dismissed");
      });
      byId("autofill-prompt-accept").addEventListener("click", function () {
        mock.promptAccepted++;
        log("prompt-accepted");
        closePrompt();
        fromLinkedIn();
      });
    }
    function schedulePrompt(delay) {
      if (prompt !== "idle") return;
      prompt = "pending";
      setTimeout(showPrompt, delay);
    }
    resume.addEventListener("change", function () {
      var file = firstFile(resume);
      if (!file) return;
      mock.uploads++;
      log("upload", file.name);
      if (mode === "upload") schedulePrompt(ms("prompt_ms", 300));
    });
    afterLoad(function () {
      setTimeout(function () {
        button.textContent = "Apply with LinkedIn";
        button.removeAttribute("aria-busy");
        log("linkedin-ready");
      }, ms("loading_ms", 1500));
      if (mode === "load") schedulePrompt(ms("prompt_ms", 400));
    });
  }

  // React-like controlled inputs: state changes only on trusted input events (typing), a
  // reconcile loop puts any other value back, and the first change re-renders the fields
  // once as new elements.
  function reactControlled() {
    var root = byId("react-root");
    var blocks = Array.prototype.map.call(root.children, function (block) { return block.outerHTML; });
    var inputs = function () { return root.querySelectorAll("input[name], textarea[name]"); };
    var state = mock.state = {};
    Array.prototype.forEach.call(inputs(), function (input) { state[input.name] = input.value; });
    mock.renders = 0;
    var loseFirst = params.get("lose_first") === "1";
    var typed = false, scheduled = false;
    var syncRevert = params.get("revert") === "sync";
    root.addEventListener("input", function (e) {
      var name = e.target.name;
      if (!e.isTrusted || !name || !(name in state)) return;
      if (typed || !loseFirst) state[name] = e.target.value;  // else typed before hydration: lost
      // ?revert=sync: like React, a controlled control shows its state again at once.
      else if (syncRevert) e.target.value = state[name];
      typed = true;
      if (!scheduled) {
        scheduled = true;
        setTimeout(unmount, ms("rerender_ms", 0));
      }
    });
    function reconcile() {
      Array.prototype.forEach.call(inputs(), function (input) {
        if (input.value === state[input.name]) return;
        input.value = state[input.name];
        log("revert:" + input.name);
      });
    }
    setInterval(reconcile, 150);
    root.addEventListener("focusout", reconcile);
    function unmount() {
      root.innerHTML = '<p id="react-saving" aria-busy="true">Saving draft\u2026</p>';
      setTimeout(function () {
        root.innerHTML = blocks.join("");
        Array.prototype.forEach.call(inputs(), function (input) { input.value = state[input.name]; });
        mock.renders++;
        log("rerender");
      }, ms("unmount_ms", 300));
    }
    root.closest("form").addEventListener("formdata", function (e) {
      Object.keys(state).forEach(function (name) { e.formData.set(name, state[name]); });
    });
  }

  var scenarios = {
    "autofill-upload": autofillUpload,
    "custom-uploader": customUploader,
    "linkedin-autofill": linkedinAutofill,
    "react-controlled": reactControlled,
    "react-controlled-narrative": reactControlled
  };
  scenarios[scenario]();
})();"""


# --- replicas of real application flows -------------------------------------------------

EASY_APPLY_PAGE_STYLE = """
body.artdeco-modal-is-open{overflow:hidden}
.jobs-search-bar{display:flex;flex-wrap:wrap;gap:.75rem;align-items:center;margin-bottom:1rem}
.jobs-search-bar form{display:flex;gap:.5rem;flex:1 1 18rem}
.jobs-search-bar input[type=search]{flex:1;padding:.45rem;border:1px solid #8a94a6;border-radius:4px;font:inherit}
.filter-pill{background:#fff;color:#1d2330;border:1px solid #8a94a6;border-radius:1rem;padding:.3rem .9rem}
.filter-pill[aria-pressed=true]{background:#0a66c2;color:#fff;border-color:#0a66c2}
.jobs-apply-control{margin:1rem 0}
.jobs-apply-control a,.jobs-apply-button{display:inline-flex;align-items:center;gap:.4rem;background:#0a66c2;color:#fff;border:0;border-radius:1.2rem;padding:.5rem 1.2rem;font-weight:600;text-decoration:none}
.jobs-apply-control svg{width:16px;height:16px}
"""

EASY_APPLY_DIALOG_STYLE = """
.artdeco-modal-overlay{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.6);font:16px/1.4 system-ui,-apple-system,Segoe UI,sans-serif;color:#1d2330}
.artdeco-modal{position:relative;display:flex;flex-direction:column;box-sizing:border-box;width:min(744px,calc(100vw - 32px));max-height:calc(100vh - 48px);background:#fff;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.3)}
.artdeco-modal:focus{outline:none}
.artdeco-modal__dismiss{position:absolute;top:10px;right:10px;width:36px;height:36px;padding:0;border:0;border-radius:50%;background:transparent;cursor:pointer}
.artdeco-modal__dismiss::before{content:"\\00d7";font-size:26px;line-height:36px;color:#444}
.artdeco-modal__header{padding:16px 56px 12px 24px;border-bottom:1px solid #e0e0e0}
.artdeco-modal__header h2{margin:0;font-size:20px}
.artdeco-modal__content{flex:1 1 auto;overflow-y:auto}
.artdeco-completeness-meter-linear{display:flex;align-items:center;padding:12px 24px 0}
.artdeco-completeness-meter-linear__progress-element{flex:1;height:8px}
.ph5{padding-left:24px;padding-right:24px}
.pl3{padding-left:12px}
.pt1{padding-top:4px}
.t-12{font-size:12px}
.t-14{font-size:14px}
.t-16{font-size:16px}
.t-bold{font-weight:600}
.t-black--light{color:#555}
.display-flex{display:flex;align-items:center}
.visually-hidden,.visually-hidden-radio,.visually-hidden-checkbox,.a11y-text{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:0!important;border:0!important;overflow:hidden!important;clip:rect(1px,1px,1px,1px)!important;white-space:nowrap!important}
.hidden{display:none!important}
.jobs-easy-apply-profile-card{display:flex;gap:12px;align-items:center;margin:12px 0}
.jobs-easy-apply-profile-card__photo{display:flex;align-items:center;justify-content:center;width:56px;height:56px;border-radius:50%;background:#dfe7ef;font-weight:600}
.fb-dash-form-element{margin:16px 0}
.fb-dash-form-element label,.fb-dash-form-element legend{display:block;font-size:14px;font-weight:400;margin-bottom:4px}
.fb-dash-form-element fieldset{border:0;margin:0;padding:0}
.fb-dash-form-element select,.fb-dash-form-element input[type=text]{display:block;box-sizing:border-box;width:100%;padding:8px;border:1px solid #666;border-radius:4px;font:inherit;background:#fff}
.fb-text-selectable__option{display:flex;align-items:center;gap:8px;margin:4px 0}
.fb-text-selectable__option label{display:inline;margin:0}
.fb-dash-form-element__error-field{border-color:#cc1016!important;outline:1px solid #cc1016}
.artdeco-inline-feedback--error{color:#cc1016;font-size:13px;margin-top:4px}
.artdeco-inline-feedback--error ul{margin:4px 0 0;padding-left:20px}
.jobs-document-upload__title--is-required{display:block;margin-bottom:8px}
.ui-attachment{position:relative;display:flex;justify-content:space-between;align-items:center;gap:12px;border:1px solid #c0c0c0;border-radius:8px;padding:12px;margin:8px 0}
.jobs-document-upload-redesign-card__container--selected{border:2px solid #0a66c2}
.jobs-document-upload-redesign-card__file-name,.jobs-document-upload-redesign-card__header p{margin:0}
.jobs-document-upload-redesign-card__download-button{width:32px;height:32px;padding:0;border:0;background:transparent;cursor:pointer}
.jobs-document-upload-redesign-card__container .display-flex{position:relative}
.jobs-document-upload-redesign-card__container .visually-hidden-radio{top:12px;right:12px}
.jobs-document-upload-redesign-card__toggle-label{display:inline-block;box-sizing:border-box;width:24px;height:24px;margin-left:8px;border:2px solid #666;border-radius:50%;cursor:pointer}
.jobs-document-upload-redesign-card__container--selected .jobs-document-upload-redesign-card__toggle-label{border:7px solid #0a66c2}
.jobs-document-upload__show-more-less-button{border:0;background:transparent;color:#0a66c2;font:inherit;font-weight:600;padding:4px 0;cursor:pointer}
.js-jobs-document-upload__container{margin-top:12px}
.artdeco-button{display:inline-flex;align-items:center;justify-content:center;box-sizing:border-box;min-height:32px;padding:6px 16px;border:1px solid #0a66c2;border-radius:16px;background:#fff;color:#0a66c2;font:inherit;font-weight:600;cursor:pointer}
.artdeco-button--primary{background:#0a66c2;color:#fff}
.artdeco-button--tertiary{border-color:transparent}
.artdeco-button:disabled{opacity:.6;cursor:default}
.artdeco-modal footer{display:flex;flex-wrap:wrap;align-items:center;justify-content:flex-end;gap:8px;padding:16px 24px;margin-top:16px;border-top:1px solid #e0e0e0}
.artdeco-modal footer p{flex:1 0 100%;margin:0}
.jobs-easy-apply-review__section{border-top:1px solid #e0e0e0;padding:8px 0}
.jobs-easy-apply-review__section-header{display:flex;align-items:center;justify-content:space-between}
.jobs-easy-apply-review__section-header h4{margin:8px 0}
.jobs-easy-apply-review__section dl{margin:0}
.jobs-easy-apply-review__section dt{font-size:14px;color:#555}
.jobs-easy-apply-review__section dd{margin:0 0 8px}
.jobs-easy-apply-follow{position:relative;margin:16px 0}
.jobs-easy-apply-follow .visually-hidden-checkbox{top:10px;left:10px}
.jobs-easy-apply-follow label{cursor:pointer}
.jobs-easy-apply-follow label::before{content:"";display:inline-block;box-sizing:border-box;width:20px;height:20px;margin-right:8px;border:2px solid #666;border-radius:4px;vertical-align:middle}
.visually-hidden-checkbox:checked+label::before{border:6px solid #0a66c2}
.jobs-easy-apply-post-apply{padding-top:16px}
"""
"""Rendered in the page head; the ``?shadow=1`` variant copies it into the shadow root."""

EASY_APPLY_JS = r"""(function () {
  "use strict";
  // A LinkedIn-style Easy Apply dialog (fictional replica). Every answer lives in page state
  // until "Submit application", the only request the dialog makes. window.__easyApply is test
  // instrumentation: the step, the answers and per-field counts of input/change events.
  var cfg = JSON.parse(document.getElementById("easy-apply-config").textContent);
  var q = cfg.questions;
  var PLACEHOLDER = cfg.placeholder;  // a real option whose value is its text
  var PROGRESS = [0, 33, 67, 100];
  var KEYS = ["email", "phone_country", "phone", "city", "sql_years", "work_authorization", "sponsorship",
    "resume", "follow"];
  var STEP_KEYS = [["email", "phone_country", "phone", "city"], ["resume"],
    ["sql_years", "work_authorization", "sponsorship"], []];
  var host = null, root = null, outlet = null, ui = {}, cards = [], seq = 0, pending = false, busy = false;
  var app = window.__easyApply = {open: false, opens: 0, step: 0, submitted: false, writes: zeros(), answers: {},
    imported: 0, skipped: 0};

  function zeros() {
    var writes = {};
    KEYS.forEach(function (key) { writes[key] = 0; });
    return writes;
  }
  function h(tag, attrs, kids) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (name) {
      var value = attrs[name];
      if (value !== false && value !== null && value !== undefined) {
        node.setAttribute(name, value === true ? "" : String(value));
      }
    });
    (kids || []).forEach(function (kid) {
      node.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
    });
    return node;
  }
  function icon() {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("aria-hidden", "true");
    return svg;
  }
  function button(text, attrs, onClick) {
    var node = h("button", Object.assign({type: "button"}, attrs), [h("span", {"class": "artdeco-button__text"}, [text])]);
    node.addEventListener("click", onClick);
    return node;
  }
  function keyed(node, key) { node.__easyApplyKey = key; return node; }
  function heading(text) { return h("h3", {"class": "t-16 t-bold"}, [text]); }

  // The page behind the dialog.
  Array.prototype.forEach.call(document.querySelectorAll(".filter-pill"), function (pill) {
    pill.addEventListener("click", function () {
      pill.setAttribute("aria-pressed", String(pill.getAttribute("aria-pressed") !== "true"));
    });
  });
  if (cfg.shadow) {
    // LinkedIn's 2026 layout renders its dialogs inside an open shadow root.
    host = h("div", {id: "interop-outlet", "data-testid": "interop-shadowdom"});
    document.body.appendChild(host);
    root = host.attachShadow({mode: "open"});
  }
  var trigger = document.getElementById("jobs-apply-button-id");
  if (trigger) {
    trigger.addEventListener("click", function () {
      if (pending || app.open) return;
      pending = true;
      setTimeout(function () { pending = false; open(); }, 400);  // the simulated apply request
    });
  }
  if (cfg.autoOpen) setTimeout(open, 300);

  function open() {
    if (app.open) return;
    var answers = {resume: null, resume_uploaded: false, follow: true};
    Object.keys(q).forEach(function (key) {
      var spec = q[key];
      answers[key] = spec.value !== null ? spec.value : spec.kind === "select" ? PLACEHOLDER : spec.kind === "radio" ? null : "";
    });
    app = window.__easyApply = {open: true, opens: app.opens + 1, step: 1, submitted: false, writes: zeros(),
      answers: answers, imported: 0, skipped: 0};
    seq = 0;
    cards = cfg.cards.map(function (c, i) { return card(c.name, c.meta, null, i === 0); });
    syncResume();
    build();
    if (root) {
      host.setAttribute("style", "position:fixed;inset:0;z-index:1000");
      root.appendChild(document.getElementById("easy-apply-dialog-style").cloneNode(true));
      root.appendChild(outlet);
    } else {
      document.body.appendChild(outlet);
    }
    document.body.classList.add("artdeco-modal-is-open");
    render();
    ui.dialog.focus();
  }
  function close() {
    if (!app.open) return;
    app.open = false;
    if (root) { root.replaceChildren(); host.removeAttribute("style"); } else { outlet.remove(); }
    outlet = null;
    document.body.classList.remove("artdeco-modal-is-open");
  }
  function build() {
    ui = {fields: {}};
    ui.progress = h("progress", {max: "100", value: "0", "class": "artdeco-completeness-meter-linear__progress-element",
      "aria-valuetext": "Current value: 0", "aria-valuemin": "0", "aria-valuenow": "0", "aria-valuemax": "100"});
    ui.note = h("span", {"class": "pl3 t-14", role: "note"});
    ui.body = h("div", {"class": "ph5"});
    ui.footer = h("footer", {role: "presentation"});
    var form = h("form", {}, [ui.body, ui.footer]);
    form.addEventListener("submit", function (e) { e.preventDefault(); });  // Enter never navigates
    ui.region = h("div", {role: "region", tabindex: "-1"}, [
      h("div", {"class": "artdeco-completeness-meter-linear"}, [ui.progress, ui.note]), h("div", {}, [form])]);
    ui.content = h("div", {"class": "artdeco-modal__content jobs-easy-apply-modal__content"}, [ui.region]);
    var dismiss = h("button", {"aria-label": "Dismiss", "data-test-modal-close-btn": true,
      "class": "artdeco-button artdeco-modal__dismiss"}, [icon()]);
    dismiss.addEventListener("click", close);
    ui.dialog = h("div", {"data-test-modal": true, role: "dialog", tabindex: "-1",
      "aria-labelledby": "jobs-apply-header", "class": "artdeco-modal jobs-easy-apply-modal"}, [dismiss,
      h("div", {"class": "artdeco-modal__header"}, [h("h2", {id: "jobs-apply-header"}, ["Apply to " + cfg.job.company])]),
      ui.content]);
    ui.dialog.addEventListener("input", written);
    ui.dialog.addEventListener("change", written);
    outlet = h("div", {id: "artdeco-modal-outlet"}, [h("div", {
      "class": "artdeco-modal-overlay artdeco-modal-overlay--is-top-layer", "data-test-modal-container": true,
      "aria-hidden": "false"}, [ui.dialog])]);
  }
  function render() {
    var pct = PROGRESS[app.step - 1];
    var said = "Your job application progress is at " + pct + " percent.";
    ui.region.setAttribute("aria-label", said);
    ui.progress.value = pct;
    ui.progress.setAttribute("aria-valuenow", String(pct));
    ui.progress.setAttribute("aria-valuetext", "Current value: " + pct);
    ui.progress.textContent = "Current value: " + pct;
    ui.note.setAttribute("aria-label", said);
    ui.note.textContent = pct + "%";
    ui.fields = {};
    ui.body.replaceChildren.apply(ui.body, [contactStep, resumeStep, questionsStep, reviewStep][app.step - 1]());
    ui.footer.replaceChildren.apply(ui.footer, footer());
  }
  function footer() {
    var primary = "artdeco-button artdeco-button--2 artdeco-button--primary";
    var kids = [h("p", {"class": "t-12 t-black--light"},
      ["Submitting this application won\u2019t change your LinkedIn profile."])];
    if (app.step > 1) {
      kids.push(button("Back", {"aria-label": "Back to previous step",
        "class": "artdeco-button artdeco-button--2 artdeco-button--secondary"}, back));
    }
    if (app.step === 1 && cfg.importOffer) {
      // ?import=1: the step offers an import, worded like an autofill offer. Skip and
      // Continue both submit the step's form by default; Skip moves on unvalidated.
      kids.push(button("Skip", {type: "submit", "class": "artdeco-button artdeco-button--2 artdeco-button--secondary"},
        function () { app.skipped += 1; app.step += 1; render(); }));
      kids.push(button("Continue", {type: "submit", "aria-label": "Continue to next step",
        "data-easy-apply-next-button": true, "class": primary}, next));
    } else if (app.step < 3) {
      kids.push(button("Next", {"aria-label": "Continue to next step", "data-easy-apply-next-button": true,
        "data-live-test-easy-apply-next-button": true, "class": primary}, next));
    } else if (app.step === 3) {
      kids.push(button("Review", {"aria-label": "Review your application",
        "data-live-test-easy-apply-review-button": true, "class": primary}, next));
    } else {
      ui.submit = button("Submit application", {"aria-label": "Submit application",
        "data-live-test-easy-apply-submit-button": true, "class": primary}, submit);
      kids.push(ui.submit);
    }
    return kids;
  }
  function next() { if (valid()) { app.step += 1; render(); } }
  function back() { app.step -= 1; render(); }

  // Steps.
  function element(key, kids, controls, anchor) {
    // Like LinkedIn, every question keeps an empty "<id>-error" container that an error
    // fills and input empties again, so the question's own markup never moves.
    var slot = h("div", {id: anchor + "-error"});
    var node = h("div", {"class": "fb-dash-form-element", "data-test-form-element": true}, kids.concat([slot]));
    ui.fields[key] = {node: node, controls: controls, id: anchor, slot: slot};
    return node;
  }
  function selectQuestion(key) {
    var spec = q[key], value = app.answers[key];
    var select = keyed(h("select", {id: spec.id, required: true, "aria-required": "true",
      "data-test-text-entity-list-form-select": true}, [PLACEHOLDER].concat(spec.options).map(function (o) {
        return h("option", {value: o, selected: o === value}, [o]);
      })), key);
    select.value = value;
    return element(key, [h("label", {"for": spec.id, "class": "fb-dash-form-element__label"}, [spec.label]), select],
      [select], spec.id);
  }
  function textQuestion(key, extra) {
    var spec = q[key];
    var input = keyed(h("input", Object.assign({"class": "artdeco-text-input--input", id: spec.id, type: "text",
      required: true, "aria-required": "true", value: app.answers[key]}, extra || {})), key);
    return element(key, [h("div", {"class": "artdeco-text-input--container"}, [
      h("label", {"for": spec.id, "class": "artdeco-text-input--label"}, [spec.label]), input])], [input], spec.id);
  }
  function radioQuestion(key) {
    var spec = q[key], radios = [];
    var options = spec.options.map(function (o, i) {
      var radio = keyed(h("input", {type: "radio", id: spec.id + "-" + i, name: spec.id, value: o, required: true,
        checked: app.answers[key] === o}), key);
      radios.push(radio);
      return h("div", {"data-test-text-selectable-option": String(i), "class": "fb-text-selectable__option"},
        [radio, h("label", {"for": spec.id + "-" + i, "class": "t-14"}, [o])]);
    });
    var legend = h("legend", {}, [h("span", {"class": "fb-dash-form-element__label"}, [h("span", {}, [spec.label])]),
      h("span", {"class": "visually-hidden"}, ["Required"])]);
    var fieldset = h("fieldset", {id: spec.group, "data-test-form-builder-radio-button-form-component": "true"},
      [legend].concat(options));
    return element(key, [fieldset], radios, spec.group);
  }
  function contactStep() {
    var p = cfg.profile;
    var offer = !cfg.importOffer ? [] : [h("div", {"class": "jobs-easy-apply-import"}, [
      h("p", {"class": "t-14"}, ["Import from LinkedIn or fill out this form."]),
      button("Import from LinkedIn", {"class": "artdeco-button artdeco-button--2 artdeco-button--secondary"},
        function () { app.imported += 1; })])];
    return offer.concat([heading("Contact info"),
      h("div", {"class": "jobs-easy-apply-profile-card"}, [
        h("div", {"class": "jobs-easy-apply-profile-card__photo", "aria-hidden": "true"}, [p.initials]),
        h("div", {}, [h("div", {"class": "artdeco-entity-lockup__title t-16 t-bold"}, [p.name]),
          h("div", {"class": "artdeco-entity-lockup__subtitle t-14"}, [p.headline]),
          h("div", {"class": "artdeco-entity-lockup__caption t-12 t-black--light"}, [p.location])])]),
      selectQuestion("email"), selectQuestion("phone_country"), textQuestion("phone", {inputmode: "text"}),
      textQuestion("city")]);
  }
  function questionsStep() {
    return [heading("Additional Questions"), textQuestion("sql_years"), radioQuestion("work_authorization"),
      selectQuestion("sponsorship")];
  }

  // Resume cards: selecting one deselects the others; the selected card's toggle deselects it.
  function card(name, meta, file, selected) {
    var c = {id: seq++, name: name, meta: meta, file: file, selected: selected,
      ext: (name.split(".").pop() || "").toLowerCase()};
    var toggle = "jobsDocumentCardToggle-" + c.id;
    c.radio = keyed(h("input", {id: toggle, type: "radio", "class": "visually-hidden-radio"}), "resume");
    c.radio.addEventListener("click", function () { choose(c); });
    c.text = h("span", {"class": "a11y-text"});
    c.node = h("div", {"class": "ui-attachment", tabindex: "0", "aria-label": ""}, [
      h("div", {"class": "jobs-document-upload-redesign-card__header"}, [
        h("h3", {"class": "t-12 t-bold jobs-document-upload-redesign-card__file-name"}, [name]),
        h("p", {"class": "pt1 t-12"}, [meta])]),
      h("div", {"class": "display-flex"}, [
        h("button", {type: "button", "aria-label": "Download resume " + name,
          "class": "jobs-document-upload-redesign-card__download-button"}, [icon()]),
        c.radio,
        h("label", {"for": toggle, "class": "jobs-document-upload-redesign-card__toggle-label"}, [c.text])])]);
    return c;
  }
  function selectedCard() { return cards.filter(function (c) { return c.selected; })[0] || null; }
  function syncResume() {
    var c = selectedCard();
    app.answers.resume = c ? c.name : null;
    app.answers.resume_uploaded = !!(c && c.file);
  }
  function paint() {
    cards.forEach(function (c) {
      c.node.className = "ui-attachment jobs-document-upload-redesign-card__container" +
        (c.selected ? " jobs-document-upload-redesign-card__container--selected" : "") + " ui-attachment--" + c.ext;
      c.node.setAttribute("aria-label", c.selected ? "Selected" : "Select this resume");
      c.radio.checked = c.selected;
      c.text.textContent = (c.selected ? "Deselect resume " : "Select resume ") + c.name;
    });
    if (cards.length > 1) {
      var more = "Show " + (cards.length - 1) + " more resumes";
      ui.more.setAttribute("aria-label", more);
      ui.more.textContent = more;
      ui.list.after(ui.more);
    } else {
      ui.more.remove();
    }
  }
  function choose(c) {
    var was = c.selected;
    cards.forEach(function (other) { other.selected = !was && other === c; });
    paint();
    syncResume();
    clearError("resume");
    if (was) {
      // Unchecking by script fires nothing; report the deselection like a user edit.
      c.radio.dispatchEvent(new Event("input", {bubbles: true}));
      c.radio.dispatchEvent(new Event("change", {bubbles: true}));
    }
  }
  function uploaded() {
    var file = ui.file.files && ui.file.files[0];
    clearError("resume");
    if (!file) return;
    var problem = !/\.(pdf|docx?)$/i.test(file.name) ? "Only DOC, DOCX and PDF files are supported" :
      !file.size ? "The selected file is empty" :
      file.size > 2 * 1024 * 1024 ? "The file must be 2 MB or smaller" : null;
    if (problem) { fail("resume", problem); return; }
    var c = card(file.name, Math.max(1, Math.round(file.size / 1024)) + " KB \u00b7 Uploaded just now", file, true);
    cards.forEach(function (other) { other.selected = false; });
    cards.unshift(c);
    ui.list.insertBefore(c.node, ui.list.firstChild);
    paint();
    syncResume();
  }
  function resumeStep() {
    ui.list = h("div", {"class": "jobs-document-upload-redesign-card__list"}, cards.map(function (c) { return c.node; }));
    ui.more = h("button", {type: "button", "class": "jobs-document-upload__show-more-less-button"});
    ui.file = keyed(h("input", {name: "file", "class": "hidden", id: cfg.upload.id, type: "file",
      accept: cfg.upload.accept}), "resume");
    ui.file.addEventListener("change", uploaded);
    var node = h("div", {"class": "jobs-document-upload"}, [heading("Resume"),
      h("span", {"class": "t-14 jobs-document-upload__title--is-required"}, ["Be sure to include an updated resume"]),
      ui.list,
      h("div", {"class": "js-jobs-document-upload__container"}, [
        h("label", {"class": "jobs-document-upload__upload-button artdeco-button artdeco-button--secondary",
          "for": cfg.upload.id}, [h("span", {role: "button", "aria-label":
            "Upload resume button. Only, DOC, DOCX, PDF formats are supported. Max file size is (2 MB)."},
            ["Upload resume"])]),
        ui.file,
        h("p", {"class": "t-12 jobs-document-upload__format-text"}, ["DOC, DOCX, PDF (2 MB)"])])]);
    var slot = h("div", {id: cfg.upload.id + "-error"});
    node.appendChild(slot);
    ui.fields.resume = {node: node, controls: [], id: cfg.upload.id, slot: slot};
    paint();
    return [node];
  }

  // Review.
  function labelOf(key) { return key === "resume" ? "Resume" : q[key] ? q[key].label : key; }
  function reviewStep() {
    var sections = [["Contact info", 1], ["Resume", 2], ["Additional Questions", 3]];
    ui.alert = h("div", {"class": "jobs-easy-apply-review__feedback"});
    var follow = keyed(h("input", {id: "follow-company-checkbox", "class": "visually-hidden-checkbox", type: "checkbox",
      checked: app.answers.follow}), "follow");
    return [heading("Review your application"),
      h("p", {"class": "t-14"}, ["The employer will also receive a copy of your profile."]), ui.alert]
      .concat(sections.map(function (s) {
        var rows = [];
        STEP_KEYS[s[1] - 1].forEach(function (key) {
          var value = app.answers[key];
          rows.push(h("dt", {}, [labelOf(key)]), h("dd", {}, [value === null || value === "" ? "Not provided" : String(value)]));
        });
        return h("section", {"class": "jobs-easy-apply-review__section"}, [
          h("div", {"class": "jobs-easy-apply-review__section-header"}, [h("h4", {"class": "t-16 t-bold"}, [s[0]]),
            button("Edit", {"aria-label": "Edit " + s[0], "class": "artdeco-button artdeco-button--2 artdeco-button--tertiary"},
              function () { app.step = s[1]; render(); })]),
          h("dl", {}, rows)]);
      }))
      .concat([h("div", {"class": "jobs-easy-apply-follow"}, [follow, h("label", {"for": "follow-company-checkbox"},
        ["Follow ", h("span", {}, [cfg.job.company]), " to stay up to date with their page."])])]);
  }

  // Answers, validation and the one request.
  function written(e) {
    var key = e.target.__easyApplyKey;
    if (!key || !app.open) return;
    app.writes[key] += 1;
    if (key === "follow") app.answers.follow = e.target.checked;
    else if (key === "work_authorization") { if (e.target.checked) app.answers[key] = e.target.value; }
    else if (key !== "resume") app.answers[key] = e.target.value;
    if (key !== "resume" && key !== "follow") clearError(key);
  }
  function problem(key) {
    if (key === "resume") return selectedCard() ? null : "Please select or upload a resume";
    var value = app.answers[key];
    if (value === null || String(value).trim() === "" || value === PLACEHOLDER) return "Please enter a valid answer";
    if (key === "sql_years" && !/^\d{1,2}$/.test(String(value).trim())) return "Enter a whole number between 0 and 99";
    return null;
  }
  function valid() {
    var ok = true;
    STEP_KEYS[app.step - 1].forEach(function (key) {
      clearError(key);
      var message = problem(key);
      if (message) { fail(key, message); ok = false; }
    });
    return ok;
  }
  function fail(key, message) {
    var f = ui.fields[key];
    if (!f) return;
    var id = f.id + "-error";
    f.slot.replaceChildren(h("div", {"class": "artdeco-inline-feedback artdeco-inline-feedback--error", role: "alert"},
      [h("span", {"class": "artdeco-inline-feedback__message"}, [message])]));
    f.controls.forEach(function (control) {
      control.setAttribute("aria-invalid", "true");
      control.setAttribute("aria-describedby", id);
      control.classList.add("fb-dash-form-element__error-field");
    });
  }
  function clearError(key) {
    var f = ui.fields[key];
    if (!f) return;
    f.slot.replaceChildren();
    f.controls.forEach(function (control) {
      control.removeAttribute("aria-invalid");
      control.removeAttribute("aria-describedby");
      control.classList.remove("fb-dash-form-element__error-field");
    });
  }
  function submit() {
    if (busy) return;
    busy = true;
    ui.submit.disabled = true;
    var body = new FormData();
    Object.keys(q).forEach(function (key) {
      if (app.answers[key] !== null) body.append(key, app.answers[key]);
    });
    if (app.answers.follow) body.append("follow_company", "yes");
    var c = selectedCard();
    if (c && c.file) body.append("resume", c.file, c.file.name);
    else if (c) body.append("resume_choice", c.name);
    fetch(cfg.submitUrl, {method: "POST", body: body, credentials: "same-origin"}).then(function (response) {
      return response.json().then(function (data) { return {ok: response.ok, data: data}; });
    }).then(function (result) {
      busy = false;
      if (result.ok && result.data.accepted) submitted(result.data); else refused(result.data.errors || {});
    }).catch(function () {
      busy = false;
      refused({application: "Something went wrong. Please try again."});
    });
  }
  function submitted(data) {
    app.submitted = true;
    app.result = data;
    ui.content.replaceChildren(
      h("div", {"class": "ph5 jobs-easy-apply-post-apply", role: "status"}, [h("h3", {}, ["Application submitted"]),
        h("p", {}, ["Your application for ", h("strong", {}, [cfg.job.title]),
          " (Job ID " + cfg.job.code + ") was sent to " + cfg.job.company + "."])]),
      h("footer", {role: "presentation"}, [button("Done",
        {"class": "artdeco-button artdeco-button--2 artdeco-button--primary"}, close)]));
  }
  function refused(errors) {
    ui.submit.disabled = false;
    var items = Object.keys(errors).map(function (key) {
      return h("li", {}, [(q[key] || key === "resume" ? labelOf(key) + ": " : "") + errors[key]]);
    });
    ui.alert.replaceChildren(h("div", {id: "jobs-easy-apply-submit-error", role: "alert",
      "class": "artdeco-inline-feedback artdeco-inline-feedback--error"}, [
      h("span", {"class": "artdeco-inline-feedback__message"}, ["Your application could not be submitted."]),
      h("ul", {}, items)]));
  }
})();"""

CAREERS_EMBED_JS = r"""(function () {
  "use strict";
  // An employer careers page with tabs. The timer stands in for the Greenhouse job board embed
  // script (boards.greenhouse.io/embed/job_board/js?for=<board>) injecting the application iframe.
  var cfg = JSON.parse(document.getElementById("careers-config").textContent);
  var tabs = [document.getElementById("tab-overview"), document.getElementById("tab-application")];
  function select(tab) {
    tabs.forEach(function (t) {
      var on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute("aria-controls")).style.display = on ? "" : "none";
    });
  }
  tabs.forEach(function (t) { t.addEventListener("click", function () { select(t); }); });
  document.querySelector(".apply-btn").addEventListener("click", function () { select(tabs[1]); });
  setTimeout(function () {
    var frame = document.createElement("iframe");
    frame.id = "grnhse_iframe";
    frame.title = "Greenhouse Job Board";
    frame.width = "100%";
    frame.height = "1200";
    frame.setAttribute("frameborder", "0");
    frame.setAttribute("scrolling", "no");
    frame.setAttribute("src", cfg.src);
    document.getElementById("grnhse_app").appendChild(frame);
  }, 300);
})();"""

JAZZHR_STYLE = """
.cookie-consent-bar{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;background:#20364f;color:#fff;padding:.75rem 1rem;margin-bottom:1rem}
.cookie-consent-bar p{flex:1 1 16rem;margin:0}
.job-header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:0 1rem}
.btn{display:inline-block;padding:.55rem 1.1rem;border:1px solid #1f6f43;border-radius:4px;color:#1f6f43;font-weight:600;text-decoration:none}
.btn-primary{background:#1f6f43;color:#fff}
.form-group{margin:1rem 0}
.control-label{display:block;font-weight:600;margin-bottom:.3rem}
.required{color:#b42318}
.help-block{display:block;margin:.25rem 0;color:#4a5568}
.has-error .form-control{border-color:#b42318;outline:2px solid #b42318}
.has-error .help-block[role=alert]{color:#b42318;font-weight:600}
"""

JAZZHR_JS = r"""(function () {
  "use strict";
  // A JazzHR-style application page (fictional replica): every action control is an anchor.
  // "Submit Application" validates required fields, then submits the form by script.
  var form = document.getElementById("form_submit_new_resume");
  var file = document.getElementById("resumator-resume-file");
  var text = document.getElementById("resumator-resume-value");
  var EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  var sent = false;
  function click(node, handler) {
    if (node) node.addEventListener("click", function (e) { e.preventDefault(); handler(); });
  }
  function anchor(scope, label) {
    return Array.prototype.filter.call(scope.querySelectorAll("a.btn"), function (a) {
      return a.textContent.trim() === label;
    })[0];
  }
  var bar = document.getElementById("resumator-cookie-consent");
  Array.prototype.forEach.call(bar.querySelectorAll("button"), function (b) {
    b.addEventListener("click", function () { bar.style.display = "none"; });
  });
  click(document.querySelector("a.share"), function () {});
  click(document.getElementById("resumator-choose-upload"), function () { file.click(); });
  click(document.getElementById("resumator-choose-paste"), function () {
    document.getElementById("resumator-resume-paste").style.display = "";
    text.focus();
  });
  file.addEventListener("change", function () {
    document.getElementById("resumator-resume-filename").textContent =
      file.files.length ? "Attached: " + file.files[0].name : "";
    clear(file);
  });
  function clear(control) {
    var old = document.getElementById(control.id + "-error");
    if (old) old.remove();
    control.closest(".form-group").classList.remove("has-error");
    control.removeAttribute("aria-invalid");
    control.removeAttribute("aria-describedby");
  }
  function fail(control, message) {
    clear(control);
    var group = control.closest(".form-group");
    var note = document.createElement("span");
    note.className = "help-block";
    note.id = control.id + "-error";
    note.setAttribute("role", "alert");
    note.textContent = message;
    group.appendChild(note);
    group.classList.add("has-error");
    control.setAttribute("aria-invalid", "true");
    control.setAttribute("aria-describedby", note.id);
  }
  function problem(control) {
    if (control === file) {
      var kept = form.querySelector("input[name=resume_upload_id]");
      return file.files.length || text.value.trim() || kept ? null : "Attach or paste your resume.";
    }
    var value = control.value.trim();
    if (!value) return "This field is required.";
    if (control.type === "email" && !EMAIL.test(value)) return "Enter a valid email address.";
    return null;
  }
  function check(scope) {
    var ok = true;
    Array.prototype.forEach.call(scope.querySelectorAll("[aria-required=true]"), function (control) {
      var message = problem(control);
      if (message) { fail(control, message); ok = false; } else { clear(control); }
    });
    return ok;
  }
  form.addEventListener("submit", function () { sent = true; });
  click(document.getElementById("resumator-submit-resume"), function () {
    if (sent || !check(form)) return;
    if (form.requestSubmit) form.requestSubmit(); else form.submit();
  });
  // ?sections=2: two client-side sections with Next / Save / Back anchors (never posted).
  var one = document.getElementById("resumator-section-1");
  var two = document.getElementById("resumator-section-2");
  if (one && two) {
    click(anchor(one, "Next"), function () {
      if (!check(one)) return;
      one.style.display = "none";
      two.style.display = "";
    });
    click(anchor(one, "Save"), function () {
      var saved = {};
      Array.prototype.forEach.call(one.querySelectorAll("input[name]"), function (i) { saved[i.name] = i.value; });
      try { sessionStorage.setItem("resumator-saved-application", JSON.stringify(saved)); } catch (e) { /* ignore */ }
      document.getElementById("resumator-save-status").textContent = "Saved";
    });
    click(anchor(two, "Back"), function () {
      two.style.display = "none";
      one.style.display = "";
    });
  }
})();"""

DAYFORCE_STYLE = """
.ant-btn{display:inline-block;border:1px solid #1f6f43;border-radius:6px;padding:.5rem 1.2rem;font:inherit;font-weight:600;cursor:pointer}
.ant-btn-primary{background:#1f6f43;color:#fff}
.ant-btn-default{background:#fff;color:#1f6f43}
.ant-btn-loading{opacity:.65}
.job-actions{display:flex;gap:.75rem;margin:1rem 0}
.job-alerts{border-top:1px solid #d5dae2;margin-top:2rem;padding-top:1rem}
.flow-selection button{margin:.5rem .75rem .5rem 0}
"""

DAYFORCE_JS = r"""(function () {
  "use strict";
  // A Dayforce-style portal (fictional replica): its buttons change routes on the client after
  // the portal's delay, with history.pushState and in-place rendering; no document is loaded.
  var cfg = JSON.parse(document.getElementById("dayforce-config").textContent);
  var main = document.getElementById("main");
  var waiting = false;
  function navigate(route) {
    history.pushState({url: route.url}, "", route.url);
    document.title = route.title;
    main.innerHTML = route.html;
    window.scrollTo(0, 0);
    wire();
  }
  function later(button, route) {
    button.addEventListener("click", function () {
      if (waiting) return;
      waiting = true;
      button.classList.add("ant-btn-loading");
      setTimeout(function () { waiting = false; navigate(route); }, cfg.delayMs);
    });
  }
  function wire() {
    Array.prototype.forEach.call(main.querySelectorAll("button"), function (button) {
      var text = button.textContent.trim();
      if (button.getAttribute("test-id") === "apply-button") later(button, cfg.routes.flow);
      else if (text === "Apply without an Account") later(button, cfg.routes.manual);
      else if (text === "Sign In") button.addEventListener("click", function () { location.assign(cfg.signIn); });
    });
  }
  window.addEventListener("popstate", function () { location.reload(); });
  wire();
})();"""


def page(
    title: str, body: str, head_extra: str = "", *, main_attrs: str = "", after_main: str = ""
) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} | {COMPANY} Careers</title>
<style>{STYLE}</style>
{head_extra}
</head>
<body>
<header class="site"><a href="/">{COMPANY} Careers</a></header>
<main id="main"{main_attrs}>
{body}
</main>
{after_main}
<footer class="site">Fictional employer for local software testing. Applications here are not sent to anyone.</footer>
</body>
</html>
"""


def _job_heading(job: Job) -> str:
    return (
        f"<h1>{esc(job.title)}</h1>\n"
        f'<p class="meta">{COMPANY} · {esc(job.department)} · {esc(job.location)} · '
        f"Job ID {esc(job.code)}</p>"
    )


def _job_description(job: Job) -> str:
    """A job description tall enough (``Job.description_px``) that the form below it
    starts below the fold."""
    return (
        f'<section class="job-description" style="min-height:{job.description_px}px">'
        "<h2>About the role</h2><p>Brambleway Analytics is looking for someone to own pipeline "
        "generation across paid, organic and partner channels. You will build the demand "
        "engine with sales and product marketing and report on what it brings in.</p>"
        "<h2>Hiring process</h2><ol><li>Conversation with the hiring manager</li>"
        "<li>Case study</li><li>Reference check and offer</li></ol></section>"
    )


def _json_island(element_id: str, payload: Any) -> str:
    """Page-script configuration in an inert ``application/json`` script element. Markup
    characters are escaped, so embedded route content never reads as tags in the raw HTML."""
    data = json.dumps(payload).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return f'<script type="application/json" id="{element_id}">{data}</script>'


def _flow_selection_html(job: Job) -> str:
    """The Dayforce-style "How would you like to apply?" route: two buttons and no form."""
    return (
        '<div class="flow-selection"><h1>How would you like to apply?</h1>'
        f'<p class="meta">{esc(job.title)} · {COMPANY} · {esc(job.location)} · Job ID {esc(job.code)}</p>'
        '<button type="button" class="ant-btn ant-btn-primary">Apply without an Account</button> '
        '<button type="button" class="ant-btn ant-btn-default">Sign In</button></div>'
    )


def _widget_attrs(kind: str, config: dict[str, Any]) -> str:
    return f' data-widget-kind="{kind}" data-widget="{esc(json.dumps(config))}"'


def render_widget(f: Field, values: dict[str, list[str]], error: str | None) -> str:
    """Script-driven widgets. Their values live only in page state (see WIDGETS_JS);
    a re-render after a rejected POST restores the posted state."""
    if f.embedded:
        return ""  # rendered inside the widget it belongs to
    dom = f.dom_id or f.name
    posted = values.get(f.name, [])
    current = posted[0] if posted else None
    marker = ' <span aria-hidden="true">*</span>' if f.required else ""
    required = ' aria-required="true"' if f.required else ""
    err = (f'<p class="error" id="{esc(dom)}-error"><span class="visually-hidden">Error: </span>'
           f"{esc(error)}</p>") if error else ""
    options = [[o.value, o.label] for o in f.options]

    if f.kind == "rw_dropdown":
        # Paylocity's react-widgets DropdownList: a div combobox (aria-haspopup="true")
        # that owns its listbox by aria-owns; the listbox exists only while it is open. The
        # <label for> points at the div (not a labelable element), and data-for repeats the
        # question. The chosen option's text replaces "--" in .rw-input.
        chosen = current if current is not None else f.prefill
        shown = next((o.label for o in f.options if o.value == chosen), "")
        settings = {"id": dom, "name": f.name, "options": options, "initial": chosen if shown else None}
        hint = (f'<p class="hint pcty-hint" id="{esc(dom)}-hint">{esc(f.hint)}</p>') if f.hint else ""
        return (
            f'<div class="field pcty-field"><label class="pcty-label" for="{esc(dom)}">{esc(f.label)}{marker}</label>'
            f'{hint}{err}<div class="rw-holder"{_widget_attrs("rw-dropdown", settings)}>'
            f'<div data-for="{esc(f.label)}" id="{esc(dom)}" role="combobox" aria-owns="{esc(dom)}__listbox" '
            'aria-expanded="false" aria-haspopup="true" aria-autocomplete="list" tabindex="0" '
            'class="rw-dropdownlist rw-widget"><span tabindex="-1" title="open dropdown" aria-label="open dropdown" '
            'class="rw-dropdownlist-picker rw-select rw-btn"><svg aria-hidden="true" viewBox="0 0 16 16"></svg></span>'
            f'<div class="rw-input">{esc(shown) if shown else "--"}</div></div></div></div>'
        )

    if f.kind == "pcty_select":
        # Paylocity's input-select (a react-select without ARIA roles): the <label> (for the
        # input) wraps the whole widget, so its text holds what the widget shows; a "single
        # value" div covers the input (and takes the pointer), and typing lists the matching
        # options as plain divs in the same container.
        chosen = current if current is not None else f.prefill
        shown = next((o.label for o in f.options if o.value == chosen), "")
        placeholder = f.placeholder or "Select..."
        settings = {"id": dom, "name": f.name, "options": options, "initial": chosen if shown else None,
                    "placeholder": placeholder}
        value_class = "input-select-input-single-value" + ("" if shown else " input-select-input-placeholder")
        return (
            f'<div class="field pcty-field"><label class="pcty-label pcty-select-label" for="{esc(dom)}">'
            f'<span class="pcty-label-text">{esc(f.label)}{marker}</span>{err}'
            f'<div class="pcty-input-select"{_widget_attrs("pcty-select", settings)}>'
            '<div class="pcty-input-select__control"><div class="pcty-input-select__value-container">'
            f'<div class="{value_class}">{esc(shown or placeholder)}</div>'
            '<div class="pcty-input-select__input"><input aria-autocomplete="list" autocapitalize="none" '
            f'autocorrect="off" id="{esc(dom)}" maxlength="250"{" required" if f.required else ""} type="text" value="">'
            '</div></div><div class="pcty-input-select__indicators"><span class="pcty-input-select__indicator">'
            '<svg aria-hidden="true" viewBox="0 0 16 16"></svg></span></div></div></div></label></div>'
        )

    if f.kind == "address_autocomplete":
        # Paylocity's address line: a native combobox input that keeps whatever is typed;
        # page script lists address suggestions under it (two role=status regions announce
        # them). Posted natively.
        typed = current or ""
        settings = {"id": dom, "suggestions": list(f.suggestions)}
        return (
            f'<div class="field"><label for="{esc(dom)}">{esc(f.label)}{marker}</label>{err}'
            f'<div class="pcty-address"{_widget_attrs("address-autocomplete", settings)}>'
            f'<input role="combobox" aria-autocomplete="list" aria-controls="{esc(dom)}-autocomplete-list" '
            f'aria-expanded="false" autocomplete="off" maxlength="50"{" required" if f.required else ""} '
            f'type="text" id="{esc(dom)}" name="{esc(f.name)}" value="{esc(typed)}">'
            '<div role="status" aria-live="polite" class="visually-hidden"></div>'
            '<div role="status" aria-live="assertive" class="visually-hidden"></div>'
            f'<ul id="{esc(dom)}-autocomplete-list" role="listbox" class="pcty-address-list" hidden></ul></div></div>'
        )

    if f.kind == "fab_select":
        # BambooHR markup (Fabric): the <label> names the hidden proxy <select>, which holds
        # only the chosen option's id; the menu button beside it repeats the label and what
        # it shows in its aria-label. Page script renders the menu on its first opening.
        chosen = current if current is not None else f.prefill
        shown = next((o.label for o in f.options if o.value == chosen), "")
        menu = dom.replace("fab-select", "fab-menu") if dom.startswith("fab-select") else f"fab-menu-{dom}"
        settings = {"name": f.name, "menu": menu, "label": f.label, "options": options,
                    "initial": chosen if shown else None, "clearable": f.clearable,
                    "ids": f.display == "ids"}
        content = (f'<div class="fab-SelectToggle__content">{esc(shown)}</div>' if shown
                   else f'<div class="fab-SelectToggle__placeholder">{FAB_PLACEHOLDER}</div>')
        asterisk = '<span aria-hidden="true" class="fab-asterisk"> *</span>' if f.required else ""
        return (
            '<div class="field fab-field" data-fabric-component="SelectField InputWrapper">'
            f'<div class="fab-labelWrapper"><label for="{esc(dom)}">{esc(f.label)}{asterisk}</label></div>{err}'
            f'<div class="fab-box"><div class="fab-Select" data-fabric-component="Select"'
            f'{_widget_attrs("fab-select", settings)}><div style="display: inline-block;">'
            f'<div class="fab-SelectToggle__container"><button data-menu-id="{esc(menu)}" '
            'aria-expanded="false" aria-haspopup="true" '
            f'aria-label="{esc(f.label)} {esc(shown or FAB_PLACEHOLDER)}" tabindex="0" aria-disabled="false" '
            f'class="fab-SelectToggle" type="button"><div class="fab-SelectToggle__guts">{content}'
            '<div class="fab-SelectToggle__toggleButton"><svg aria-hidden="true" viewBox="0 0 320 512"></svg>'
            "</div></div></button></div></div>"
            f'<select aria-hidden="true" class="chzn-ignore fab-proxy" id="{esc(dom)}" name="{esc(f.name)}" '
            f'readonly{" required" if f.required else ""} tabindex="-1">'
            f'<option value="{esc(chosen) if shown and chosen else ""}"></option></select></div></div></div>'
        )

    if f.kind in ("react_select", "react_multi", "react_async"):
        multi = f.kind == "react_multi"
        config: dict[str, Any] = {
            "id": dom, "name": f.name, "multi": multi, "display": f.display,
            "openOnFocus": f.open_on == "focus", "async": f.remote,
            "options": [] if f.remote else options,
            "initial": [v for v in posted if v] if multi else current,
            "inline": f.inline, "required": f.required, "clearable": f.clearable,
            "linksPhone": f.links_phone,
        }
        if f.remote and current:
            config["options"] = [[current, current]]
        container = "select__value-container" + (" select__value-container--is-multi" if multi else "")
        control = (
            f'<div class="select__control"><div class="{container}">'
            '<div class="select__input-container">'
            f'<input class="select__input" autocapitalize="none" autocomplete="off" autocorrect="off" '
            f'id="{esc(dom)}" spellcheck="false" tabindex="0" type="text" aria-autocomplete="list" '
            f'aria-expanded="false" aria-haspopup="true" aria-labelledby="{esc(dom)}-label"{required} '
            'role="combobox" value=""></div></div>'
        )
        if f.inline:
            # Greenhouse markup: the control sits in an unnamed wrapper that the in-form
            # menu becomes a sibling of, with a real "Toggle flyout" indicator button.
            control = (
                f"<div>{control}"
                '<div class="select__indicators"><span class="select__indicator-separator"></span>'
                '<button type="button" tabindex="-1" aria-label="Toggle flyout" class="select__indicator">'
                '<span aria-hidden="true">▾</span></button></div></div></div>'
            )
        else:
            control += (
                '<div class="select__indicators" aria-hidden="true"><span class="select__indicator-separator">'
                "</span><div class=\"select__indicator\">▾</div></div></div>"
            )
        return (
            f'<div class="field"><label id="{esc(dom)}-label" for="{esc(dom)}" class="label select__label">'
            f"{esc(f.label)}{marker}</label>{err}"
            f'<div class="select-shell"{_widget_attrs("react-select", config)}>{control}</div></div>'
        )

    if f.kind == "div_combobox" and f.popover:
        # Rippling markup. A labelled control (EEOC questions) names its question with
        # aria-labelledby; a custom question only has the paragraph before it.
        labelled = f.labelled
        placeholder = "Select..." if labelled else "Select"
        config = {"name": f.name, "options": [o.label for o in f.options], "popover": True,
                  "placeholder": placeholder, "initial": current}
        text_id = f' id="{esc(dom)}-label"' if labelled else ""
        labelledby = f' aria-labelledby="{esc(dom)}-label"' if labelled else ""
        return (
            '<div class="rip-question rip-question--popover">'
            f'<div class="rip-question-text"><p{text_id}>{esc(f.label)}{" *" if f.required else ""}</p></div>'
            f'<div class="rip-input"><div id="{esc(dom)}" role="combobox" tabindex="0" '
            f'aria-haspopup="listbox" aria-autocomplete="list" aria-expanded="false" '
            f'aria-label="{placeholder}"{labelledby}{required} aria-invalid="false" aria-disabled="false" '
            f'class="rip-select"{_widget_attrs("div-combobox", config)}><p>{placeholder}</p></div></div>'
            f"{err}</div>"
        )

    if f.kind == "div_combobox":
        config = {"name": f.name, "options": [o.label for o in f.options],
                  "open": f.open_on or "click", "initial": current}
        return (
            '<div class="rip-question">'
            f'<div class="rip-question-text"><p>{esc(f.label)}{" *" if f.required else ""}</p></div>'
            f'<div class="rip-input"><div id="{esc(dom)}" role="combobox" tabindex="0" '
            f'aria-haspopup="listbox" aria-autocomplete="list" aria-expanded="false"{required} '
            f'class="rip-select"{_widget_attrs("div-combobox", config)}><p>Select</p></div></div>'
            f"{err}</div>"
        )

    def search_input(field: Field, value: str | None) -> str:
        cfg = {"name": field.name, "options": [o.label for o in field.options],
               "initial": value, "idle": field.idle, "showAll": field.show_all,
               "remote": field.remote, "popover": field.popover, "lazy": 1500,
               "aliases": ({name: abbr for name, abbr in STATE_ABBREVIATIONS.items()}
                           if field is RP_STATE else {})}
        role = "" if field.kind == "remote_lookup" else ' role="combobox"'
        testid = "" if field.kind == "remote_lookup" else ' data-testid="input-select-search-input"'
        req = ' aria-required="true"' if field.required else ""
        dom_id = esc(field.dom_id or field.name)
        # Rippling's location input: no role, never aria-expanded, a fallback aria-label.
        expanded = (f' aria-label="textbox" aria-labelledby="{dom_id}-label"' if field.popover
                    else ' aria-expanded="false"')
        return (
            f'<input id="{dom_id}" type="text"{role} aria-haspopup="listbox" '
            f'aria-autocomplete="list"{expanded}{testid} autocomplete="off"{req} '
            f'value="{esc(value or "")}"{_widget_attrs("search-combobox", cfg)}>'
        )

    if f.kind in ("search_combobox", "remote_lookup"):
        value = current if current is not None else f.prefill
        if f.kind == "remote_lookup":
            label_id = f' id="{esc(dom)}-label"' if f.popover else ""
            popover = ' style="position:relative"' if f.popover else ""
            return (f'<div class="field"{popover}><label{label_id} for="{esc(dom)}">{esc(f.label)}{marker}</label>'
                    f'{err}<div class="rip-input">{search_input(f, value)}</div></div>')
        # Rendered by page script like the rest of a React form: the question is only the
        # preceding paragraph, with no label association.
        mount = {"html": search_input(f, value)}
        return (
            '<div class="rip-question">'
            f'<div class="rip-question-text"><p>{esc(f.label)}{" *" if f.required else ""}</p></div>'
            f'<div class="rip-input" data-widget-mount="{esc(json.dumps(mount))}"></div>{err}</div>'
        )

    if f.kind == "rippling_phone":
        code = RP_PHONE_CODE
        code_posted = values.get(code.name, [])
        code_value = code_posted[0] if code_posted else code.prefill
        auto = f' autocomplete="{f.autocomplete}"' if f.autocomplete else ""
        return (
            '<div class="rip-question">'
            f'<div class="rip-question-text"><p>{esc(f.label)}{" *" if f.required else ""}</p></div>'
            '<div class="rip-input rip-phone"><div class="rip-country">'
            f'<label class="visually-hidden" for="{esc(code.dom_id or code.name)}">{esc(code.label)}</label>'
            f"{search_input(code, code_value)}</div>"
            f'<label class="visually-hidden" for="{esc(dom)}">{esc(f.label)}</label>'
            f'<input type="tel" id="{esc(dom)}" name="{esc(f.name)}" value="{esc(current or "")}"'
            f"{auto}{required}></div>{err}</div>"
        )

    if f.kind == "intl_tel":
        country = (values.get("phone_country") or ["us"])[0]
        chosen = next((c for c in ITI_COUNTRIES if c[0] == country), ITI_COUNTRIES[-1])
        config = {"name": "phone_country", "initial": chosen[0],
                  "countries": [list(c) for c in ITI_COUNTRIES]}
        items = "".join(
            f'<li id="iti-0__item-{iso}" class="iti__country" role="option" data-dial-code="{code}" '
            f'data-country-code="{iso}" aria-selected="{"true" if iso == chosen[0] else "false"}">'
            f'<div class="iti__flag iti__{iso}"></div><span class="iti__country-name">{esc(name)}</span>'
            f'<span class="iti__dial-code">+{code}</span></li>'
            for iso, name, code in ITI_COUNTRIES
        )
        auto = f' autocomplete="{f.autocomplete}"' if f.autocomplete else ""
        if f.display == "separate":
            # intl-tel-input 18 with separateDialCode (Workable): a role=combobox flag
            # whose name says nothing of the code; the code is its own element.
            config["separate"] = True
            return (
                f'<div class="field"><label for="{esc(f.name)}">{esc(f.label)}{marker}</label>{err}'
                f'<div class="iti iti--allow-dropdown iti--separate-dial-code"{_widget_attrs("intl-tel", config)}>'
                '<div class="iti__flag-container"><div class="iti__selected-flag" role="combobox" '
                'aria-haspopup="listbox" aria-controls="iti-0__country-listbox" aria-expanded="false" '
                f'aria-label="{esc(f.picker_label or "Telephone country code")}" tabindex="0" title="{esc(chosen[1])}">'
                f'<div class="iti__flag iti__{chosen[0]}"></div>'
                f'<div class="iti__selected-dial-code">+{chosen[2]}</div><div class="iti__arrow"></div></div>'
                f'<ul id="iti-0__country-listbox" class="iti__country-list iti__hide" role="listbox" '
                f'aria-label="List of countries">{items}</ul></div>'
                f'<input type="tel" id="{esc(f.name)}" name="{esc(f.name)}" value="{esc(current or "")}"'
                f"{auto}{' required' if f.required else ''}></div></div>"
            )
        return (
            f'<div class="field"><label for="{esc(f.name)}">{esc(f.label)}{marker}</label>{err}'
            f'<div class="iti iti--allow-dropdown"{_widget_attrs("intl-tel", config)}>'
            '<div class="iti__country-container">'
            '<button type="button" class="iti__selected-country" aria-haspopup="dialog" '
            'aria-controls="iti-0__dropdown-content" aria-expanded="false" '
            f'aria-label="Change country, selected {esc(chosen[1])} (+{chosen[2]})" '
            f'title="{esc(chosen[1])} (+{chosen[2]})"><div class="iti__flag iti__{chosen[0]}"></div>'
            '<div class="iti__arrow" aria-hidden="true">▾</div></button>'
            '<div id="iti-0__dropdown-content" class="iti__dropdown-content iti__hide" role="dialog" '
            'aria-modal="true" aria-label="Select country">'
            '<label class="visually-hidden" for="iti-0__search-input">Search</label>'
            '<input id="iti-0__search-input" type="search" class="iti__search-input" role="combobox" '
            'aria-expanded="true" aria-autocomplete="list" aria-controls="iti-0__country-listbox" '
            'autocomplete="off" placeholder="Search">'
            f'<ul id="iti-0__country-listbox" class="iti__country-list" role="listbox" '
            f'aria-label="List of countries">{items}</ul></div></div>'
            f'<input type="tel" id="{esc(f.name)}" name="{esc(f.name)}" value="{esc(current or "")}"'
            f"{auto}{' required' if f.required else ''}></div></div>"
        )

    raise ValueError(f"unknown widget kind {f.kind}")


def _uploader_key(f: Field) -> str:
    """Id prefix of a custom uploader's elements (``resume``, ``cover-letter``)."""
    return f.name.replace("_", "-")


def render_uploader(f: Field, error: str | None) -> str:
    """The custom-uploader widgets. Page script (SCENARIO_JS) mounts this markup like the
    rest of a client-rendered form, so the static HTML holds no unlabeled control.

    ``custom_file``: a styled button and drop zone over a hidden input no label names;
    ``label_file``: a visually hidden input inside its label. A re-render after a rejected
    POST shows them empty (the files are not retained)."""
    key = _uploader_key(f)
    noun = f.name.replace("_", " ")
    marker = (' <span aria-hidden="true">*</span>' if f.required
              else ' <span class="optional">(optional)</span>')
    err = (f'  <p class="error" id="{key}-error"><span class="visually-hidden">Error: </span>'
           f"{esc(error)}</p>\n") if error else ""
    described = f' aria-describedby="{key}-error"' if error else ""
    heading = f'  <div class="label" id="{key}-label">{esc(f.label)}{marker}</div>\n'
    accept = f' accept="{esc(f.accept)}"' if f.accept else ""
    if f.kind == "custom_file":
        required = ' aria-required="true"' if f.required else ""
        hint = f'  <p class="hint">{esc(f.hint)}</p>\n' if f.hint else ""
        markup = (
            f'<div class="field uploader" id="{key}-field" role="group" '
            f'aria-labelledby="{key}-label"{required}{described}>\n'
            f"{heading}{err}"
            f'  <div class="dropzone" id="{key}-dropzone" data-testid="{key}-dropzone">\n'
            '    <span class="dropzone__text">Drop or select a file</span>\n'
            f'    <button type="button" class="upload-button" id="{key}-button">Upload {esc(noun)}</button>\n'
            "  </div>\n"
            f"{hint}"
            f'  <input type="file" id="{key}-input" name="{esc(f.name)}"{accept} style="display:none">\n'
            f'  <div class="file-chip" id="{key}-chip" hidden></div>\n'
            f'  <div class="upload-notice" id="{key}-notice" role="status" aria-live="polite"></div>\n'
            "</div>"
        )
    else:
        markup = (
            f'<div class="field uploader" id="{key}-field">\n'
            f"{heading}{err}"
            f'  <label class="upload-label" data-testid="{esc(f.name)}">\n'
            f"    <span>Attach {esc(noun)}</span>\n"
            f'    <input type="file" id="{key}-input" name="{esc(f.name)}"{accept} '
            f'class="visually-hidden"{described}>\n'
            "  </label>\n"
            f'  <div class="file-chip" id="{key}-chip" hidden></div>\n'
            "</div>"
        )
    return f'<div data-mount-html="{esc(markup)}"></div>'


LINKEDIN_APPLY_HTML = (
    '<div class="awli" id="awli">\n'
    '  <button type="button" id="linkedin-apply" class="awli-button" aria-busy="true">'
    "Loading\u2026</button>\n"
    '  <div class="awli-overlay" id="linkedin-overlay" title="Apply with LinkedIn"></div>\n'
    "</div>"
)
"""linkedin-autofill's button under a transparent overlay, before the first field block."""


def _field_layout(job: Job, blocks: list[tuple[Field, str]]) -> str:
    """The rendered field blocks as the job's form lays them out: after the LinkedIn button
    (linkedin-autofill) or with the contact fields inside the React root (react-controlled)."""
    joined = "".join(block for _, block in blocks)
    if job.slug == "linkedin-autofill":
        return LINKEDIN_APPLY_HTML + joined
    if job.slug in ("react-controlled", "react-controlled-narrative"):
        root_fields = REACT_ROOT_FIELDS | {"why_brambleway"}
        inside = "".join(block for f, block in blocks if f.name in root_fields)
        outside = "".join(block for f, block in blocks if f.name not in root_fields)
        return f'<div id="react-root">{inside}</div>{outside}'
    return joined


_BUBBLE_STYLE = ("transform:translateX(-100%);position:absolute;pointer-events:none;opacity:0;"
                 "margin:0;width:16px;height:16px")


def _render_aria_choices(f: Field, posted: list[str], current: str, error: str | None, marker: str,
                         hint: str, err: str) -> str:
    """A ``Field.aria`` question: Radix-style ``button[role=checkbox|radio]`` options, each
    with a ``<label for>`` and an aria-hidden native bubble input for the form post."""
    fid = f"f-{f.name}"
    role = "radio" if f.kind == "radio" else "checkbox"
    bubble = "radio" if f.kind == "radio" else "checkbox"
    choices = f.options if f.kind != "checkbox" else (Option("yes", f.label),)
    required = ' aria-required="true"' if f.required else ""
    invalid = ' aria-invalid="true"' if error else ""
    items = []
    for i, o in enumerate(choices):
        bid = f"{fid}-btn-{i}"
        on = (o.value in posted) if f.kind != "checkbox" else current == "yes"
        state = "checked" if on else "unchecked"
        own_required = required if f.kind == "checkbox" else ""
        text = f"{esc(o.label)}{marker}" if f.kind == "checkbox" else esc(o.label)
        items.append(
            f'<div class="aria-option"><button type="button" role="{role}" id="{bid}" value="on" '
            f'aria-checked="{"true" if on else "false"}" data-state="{state}" class="aria-choice"'
            f"{own_required}{invalid}></button>"
            f'<input type="{bubble}" aria-hidden="true" tabindex="-1" name="{f.name}" value="{esc(o.value)}"'
            f'{" checked" if on else ""} style="{_BUBBLE_STYLE}">'
            f'<label for="{bid}" class="aria-label">{text}</label></div>'
        )
    if f.kind == "checkbox":
        return f'<div class="field aria-field" id="{fid}">{hint}{err}{items[0]}</div>'
    if f.kind == "radio":
        return (f'<div class="field aria-field"><div class="label" id="{fid}-label">{esc(f.label)}{marker}</div>'
                f'{hint}{err}<div role="radiogroup" id="{fid}" aria-labelledby="{fid}-label"{required}>'
                + "".join(items) + "</div></div>")
    return (f'<fieldset class="field aria-field" id="{fid}"{required}><legend>{esc(f.label)}{marker}</legend>'
            f"{hint}{err}" + "".join(items) + "</fieldset>")


def _reveals_template(f: Field, fid: str, values: dict[str, list[str]]) -> str:
    """A radio group's or select's follow-up questions, inert until REVEAL_JS mounts them
    after its block (with ``reveal_hint`` added to the block itself)."""
    if not f.reveals:
        return ""
    on = f' data-reveals-on="{esc(f.reveals_on)}"' if f.reveals_on else ""
    hint = f' data-reveal-hint="{esc(f.reveal_hint)}"' if f.reveal_hint else ""
    return (
        f'<template id="{fid}-reveals" data-reveals-for="{esc(f.name)}"{on}{hint}>'
        + "".join(render_field(r, values, None) for r in f.reveals)
        + "</template>"
    )


def render_field(
    f: Field,
    values: dict[str, list[str]],
    error: str | None,
    retained: dict[str, Any] | None = None,
) -> str:
    if f.lazy:
        # Teamtailor renders the question late: LAZY_JS mounts it once its (empty)
        # placeholder scrolls into view.
        inner = render_field(replace(f, lazy=False), values, error, retained)
        return (f'<div class="lazy-block" data-lazy-block style="min-height:3rem">'
                f"<template data-lazy-question>{inner}</template></div>")
    if f.widget:
        return render_widget(f, values, error)
    if f.kind in UPLOADER_KINDS:
        return render_uploader(f, error)
    fid = f"f-{f.name}"
    posted = values.get(f.name, [])
    current = posted[0] if posted else ""
    described: list[str] = []
    hint = ""
    if f.hint:
        hint = f'<p class="hint" id="{fid}-hint">{esc(f.hint)}</p>'
        described.append(f"{fid}-hint")
    err = ""
    if error:
        err = (
            f'<p class="error" id="{fid}-error"><span class="visually-hidden">Error: </span>'
            f"{esc(error)}</p>"
        )
        described.append(f"{fid}-error")
    if f.kind == "file" and retained:
        described.append(f"{fid}-current")
    aria = f' aria-describedby="{" ".join(described)}"' if described else ""
    if error:
        aria += ' aria-invalid="true"'
    required = " required" if f.required else ""
    if f.disabled:
        required = " disabled"
    marker = (
        ' <span aria-hidden="true">*</span>'
        if f.required
        else ' <span class="optional">(optional)</span>'
    )
    label = f'<label for="{fid}">{esc(f.label)}{marker}</label>'

    if f.kind in ("text", "email", "tel", "url"):
        auto = f' autocomplete="{f.autocomplete}"' if f.autocomplete else ""
        if f.fabric_id is not None:
            # BambooHR's Fabric text field: a generated id FABRIC_JS hands out afresh.
            dom = f"FabricTextField-{f.fabric_id}"
            name = "" if f.unnamed else f' name="{f.name}"'
            placeholder = f' placeholder="{esc(f.placeholder)}"' if f.placeholder else ""
            return (
                f'<div class="field fab-TextField"><label for="{dom}">{esc(f.label)}{marker}</label>'
                f'{hint}{err}<input type="{f.kind}" id="{dom}"{name} data-fabric value="{esc(current)}"'
                f"{placeholder}{auto}{required}{aria}></div>"
            )
        control = (
            f'<input type="{f.kind}" id="{fid}" name="{f.name}" value="{esc(current)}"'
            f"{auto}{required}{aria}>"
        )
        if f.disabled_by:
            return (f'<div class="field" data-disabled-by="{esc(f.disabled_by)}"'
                    f'{" data-required" if f.required else ""}>{label}{hint}{err}{control}</div>')
        return f'<div class="field">{label}{hint}{err}{control}</div>'

    if f.kind == "textarea":
        control = (
            f'<textarea id="{fid}" name="{f.name}" rows="6" maxlength="{MAX_TEXT_CHARS}"'
            f"{required}{aria}>{esc(current)}</textarea>"
        )
        return f'<div class="field">{label}{hint}{err}{control}</div>'

    if f.kind in ("select", "multiselect"):
        multiple = f' multiple size="{len(f.options)}"' if f.multi else ""
        opts = [] if f.multi else ['<option value="">Select an answer</option>']
        for o in f.options:
            selected = " selected" if o.value in posted else ""
            selected += " disabled" if o.disabled else ""
            opts.append(f'<option value="{esc(o.value)}"{selected}>{esc(o.label)}</option>')
        control = (
            f'<select id="{fid}" name="{f.name}"{multiple}{required}{aria}>'
            + "".join(opts)
            + "</select>"
        )
        block = f'<div class="field">{label}{hint}{err}{control}</div>'
        return block + _reveals_template(f, fid, values)

    if f.aria and f.kind in ("radio", "checkbox_group", "checkbox"):
        return _render_aria_choices(f, posted, current, error, marker, hint, err)

    if f.kind in ("radio", "checkbox_group"):
        input_type = "radio" if f.kind == "radio" else "checkbox"
        # A question required only once another is answered shows no marker until then.
        legend_marker = "" if f.required_after and not f.required else marker
        choices = []
        for i, o in enumerate(f.options):
            oid = f"{fid}-{i}"
            checked = " checked" if o.value in posted else ""
            invalid = ' aria-invalid="true"' if error else ""
            choices.append(
                f'<div class="choice"><input type="{input_type}" id="{oid}" name="{f.name}" '
                f'value="{esc(o.value)}"{checked}{required}{invalid}>'
                f'<label for="{oid}">{esc(o.label)}</label></div>'
            )
        group_aria = f' aria-describedby="{" ".join(described)}"' if described else ""
        role = ' role="radiogroup"' if f.kind == "radio" else ""
        req = ' aria-required="true"' if f.required and f.kind == "radio" else ""
        if f.required_after:
            req += f' data-required-after="{esc(f.required_after)}"'
        group = (
            f'<fieldset class="field" id="{fid}"{role}{req}{group_aria}>'
            f"<legend>{esc(f.label)}{legend_marker}</legend>{hint}{err}"
            + "".join(choices)
            + "</fieldset>"
        )
        return group + _reveals_template(f, fid, values)

    if f.kind == "checkbox":
        checked = " checked" if current == "yes" else ""
        control = (
            f'<input type="checkbox" id="{fid}" name="{f.name}" value="yes"'
            f"{checked}{required}{aria}>"
        )
        terms = f'<p class="terms">{esc(f.terms)}</p>' if f.terms else ""
        html_ = f'<div class="field choice">{control} {label}{hint}{err}{terms}</div>'
        if f.legend:
            html_ = f'<fieldset class="field"><legend>{esc(f.legend)}</legend>{html_}</fieldset>'
        return html_

    if f.kind == "custom_combobox":
        # An ARIA widget backed by a hidden input: not a native form control.
        selected = next((o for o in f.options if o.value == current), None)
        items = "".join(
            f'<li role="option" id="{fid}-opt-{i}" data-value="{esc(o.value)}" '
            f'aria-selected="{"true" if selected is o else "false"}">{esc(o.label)}</li>'
            for i, o in enumerate(f.options)
        )
        invalid = ' aria-invalid="true"' if error else ""
        invalid += ' aria-required="true"' if f.required else ""
        return (
            f'<div class="field"><span class="label" id="{fid}-label">{esc(f.label)}{marker}</span>'
            f"{hint}{err}"
            f'<div id="{fid}" class="combo" role="combobox" tabindex="0" '
            f'aria-labelledby="{fid}-label" aria-haspopup="listbox" aria-expanded="false" '
            f'aria-controls="{fid}-list"{invalid}>'
            f"{esc(selected.label) if selected else 'Choose an option'}</div>"
            f'<ul id="{fid}-list" role="listbox" aria-labelledby="{fid}-label" hidden>{items}</ul>'
            f'<input type="hidden" name="{f.name}" value="{esc(current)}">'
            "<script>(function(){"
            f"var box=document.getElementById('{fid}'),list=document.getElementById('{fid}-list');"
            "var input=box.parentNode.querySelector('input[type=hidden]');"
            "box.addEventListener('click',function(){list.hidden=!list.hidden;"
            "box.setAttribute('aria-expanded',String(!list.hidden));});"
            "list.addEventListener('click',function(e){var li=e.target.closest('[role=option]');"
            "if(!li)return;input.value=li.dataset.value;box.textContent=li.textContent;"
            "list.querySelectorAll('[role=option]').forEach(function(o){"
            "o.setAttribute('aria-selected',String(o===li));});"
            "list.hidden=true;box.setAttribute('aria-expanded','false');});"
            "})();</script></div>"
        )

    if f.kind == "file":
        current_file = ""
        needs_file = f.required
        if retained:
            needs_file = False
            current_file = (
                f'<p class="hint" id="{fid}-current">Currently attached: '
                f'{esc(retained["filename"])} ({retained["size"]} bytes). '
                "Choose a new file only if you want to replace it.</p>"
                f'<input type="hidden" name="resume_upload_id" value="{esc(retained["upload_id"])}">'
            )
        accept = f' accept="{esc(f.accept)}"' if f.accept else ""
        if f.uploader in ("greenhouse", "greenhouse-async"):
            # Greenhouse markup: a labelled group; the input has no name (its file lives in
            # page state) and is labelled only with its button's verb.
            marker = '<span class="required">*</span>' if f.required else ""
            upload_config = {"name": f.name, "async": 2500 if f.uploader == "greenhouse-async" else 0}
            return (
                '<div class="field-wrapper">'
                f'<div role="group" aria-labelledby="upload-label-{f.name}" aria-required="{str(f.required).lower()}" '
                f'class="file-upload" data-allow-s3="false"{_widget_attrs("gh-upload", upload_config)}>'
                f'<div id="upload-label-{f.name}" class="label upload-label">{esc(f.label)}{marker}</div>'
                '<div class="file-upload__wrapper"><div class="button-container"><div class="secondary-button">'
                '<div><button type="button" class="btn btn--pill attach">Attach</button>'
                f'<label class="visually-hidden" for="{f.name}">Attach</label>'
                f'<input id="{f.name}" class="visually-hidden" type="file"{accept}></div></div>'
                '<div class="secondary-button"><button type="button" class="btn btn--pill">Dropbox</button></div>'
                '<p class="file-upload__filetypes">Accepted file types: pdf, doc, docx, txt</p>'
                f"</div></div>{err}</div></div>"
            )
        if f.uploader == "teamtailor":
            # Teamtailor markup: Dropzone mounts the input (see ttUpload); the preview
            # template carries the stored file's URL input under the same id.
            dom = f"candidate_{f.name}_remote_url"
            marker = ('<sup aria-hidden="true">*</sup><span class="visually-hidden">Required</span>'
                      if f.required else "")
            upload_config = {"name": f.name, "id": dom, "accept": f.accept or "", "required": f.required}
            return (
                f'<div class="field"><div class="tt-upload" id="upload_{f.name}_field"'
                f'{_widget_attrs("tt-upload", upload_config)}>'
                f'<label for="{dom}">{esc(f.label)}{marker}</label>{err}'
                '<div class="tt-trigger" data-target="trigger"><div><span class="dz-message">'
                "Drop your file or <u>upload</u></span></div></div>"
                '<div data-target="previews"></div>'
                '<template data-target="preview"><div class="tt-preview">'
                '<div class="tt-hidden" data-target="name"><a data-dz-name href="javascript:void(0);"></a>'
                '<button type="button" title="Clear file selection" data-dz-remove>&times;</button></div>'
                '<div data-target="progress"><div><span>Uploading…</span>'
                '<a href="javascript:void(0);" data-dz-remove>&times;</a></div>'
                '<div class="tt-bar"><span></span></div></div>'
                f'<input value="" class="tt-hidden" disabled type="text" name="candidate[{f.name}_remote_url]" '
                f'id="{dom}"></div></template></div></div>'
            )
        if f.uploader == "jobvite":
            # Jobvite markup: the heading names a Select button that opens an attachment
            # popup; page script appends the popup, file input included, to <body> (jvUpload).
            upload_config = {"name": f.name, "document": "Resume", "accept": f.accept or ""}
            return (
                f'<div class="field jv-apply-section"><h3 class="jv-step-header" id="jv-{f.name}-header">'
                f'{esc(f.label)}{"*" if f.required else ""}</h3>{err}'
                f'<div class="jv-apply-with" id="attach-{f.name}"{_widget_attrs("jv-upload", upload_config)}>'
                '<div class="jv-select"><button type="button" class="jv-button" aria-haspopup="true" '
                f'aria-labelledby="jv-{f.name}-header" aria-expanded="false" '
                f'aria-required="{str(f.required).lower()}">Select</button></div>'
                '<ul class="jv-file-list"></ul></div></div>'
            )
        if f.uploader == "dropzone":
            return (
                f'<div class="field"><label for="input_files_input_{f.name}">{esc(f.label)}'
                f'{" *" if f.required else ""}</label>{err}'
                f'<div class="dropzone"{_widget_attrs("dropzone", {"name": f.name})}>'
                '<p>Drop your file here or choose one</p>'
                f'<input type="file" id="input_files_input_{f.name}" class="visually-hidden"{accept}>'
                '<div data-role="preview"></div></div></div>'
            )
        control = (
            f'<input type="file" id="{fid}" name="{f.name}"{accept}'
            f'{" required" if needs_file else ""}{aria}>'
        )
        return f'<div class="field">{label}{hint}{err}{current_file}{control}</div>'

    raise ValueError(f"unknown field kind {f.kind}")


def render_error_summary(entries: list[tuple[str, str, str]]) -> str:
    """entries: (anchor id, label, message)."""
    if not entries:
        return ""
    items = "".join(
        f'<li><a href="#{anchor}">{esc(label)}: {esc(message)}</a></li>'
        for anchor, label, message in entries
    )
    return (
        '<div class="error-summary" role="alert" aria-labelledby="error-summary-title" '
        'tabindex="-1"><h2 id="error-summary-title">There is a problem with your application</h2>'
        f"<ul>{items}</ul></div>"
    )


def _summary_entries(fields: tuple[Field, ...], errors: dict[str, str]) -> list[tuple[str, str, str]]:
    return [(_anchor(f), f.label, errors[f.name]) for f in fields if f.name in errors]


def _anchor(f: Field) -> str:
    """What an error summary entry links to: a custom uploader's block, else the control."""
    return f"{_uploader_key(f)}-field" if f.kind in UPLOADER_KINDS else f"f-{f.name}"


def render_captcha(token: str, error: str | None) -> str:
    err = ""
    aria = ' aria-describedby="f-captcha_answer-hint"'
    if error:
        err = (
            '<p class="error" id="f-captcha_answer-error"><span class="visually-hidden">Error: '
            f"</span>{esc(error)}</p>"
        )
        aria = ' aria-describedby="f-captcha_answer-hint f-captcha_answer-error" aria-invalid="true"'
    return (
        '<fieldset class="field" id="human-verification"><legend>Verify you are human (CAPTCHA)</legend>'
        f'<img src="/captcha/{esc(token)}.svg" width="180" height="56" '
        'alt="CAPTCHA image containing distorted characters">'
        f'<input type="hidden" name="captcha_token" value="{esc(token)}">'
        '<label for="f-captcha_answer">Characters shown in the image <span aria-hidden="true">*</span></label>'
        '<p class="hint" id="f-captcha_answer-hint">Letters are not case sensitive.</p>'
        f'{err}<input type="text" id="f-captcha_answer" name="captcha_answer" autocomplete="off" '
        f"required{aria}></fieldset>"
    )


def render_captcha_widget(error: str | None) -> str:
    """An invisible reCAPTCHA-style badge: the sitekey container, a small badge iframe
    and the hidden token textarea the real widget fills. Nothing is solved on the page;
    the server checks only that the token is non-empty when the form is posted."""
    err = (
        f'<p class="error" id="{CAPTCHA_WIDGET_FIELD}-error"><span class="visually-hidden">Error: '
        f"</span>{esc(error)}</p>"
        if error
        else ""
    )
    return (
        '<div class="captcha-widget">'
        f'<label for="{CAPTCHA_WIDGET_FIELD}" hidden>reCAPTCHA response</label>'
        '<div class="g-recaptcha" data-sitekey="fixture-site-key" data-size="invisible"></div>'
        f'<textarea id="{CAPTCHA_WIDGET_FIELD}" name="{CAPTCHA_WIDGET_FIELD}" '
        'style="display:none" required></textarea>'
        '<iframe src="/captcha/widget.html" title="reCAPTCHA" width="256" height="60" '
        'style="position:fixed;right:1rem;bottom:1rem;border:0"></iframe>'
        f"{err}</div>"
    )


def render_delayed(form_html: str) -> str:
    """An SPA-style page: a loading indicator first, the form injected by page script
    1.5 s later (no network involved)."""
    return (
        '<p id="application-loading" aria-busy="true">Fetching application form</p>'
        f'<template id="application-template">{form_html}</template>'
        "<script>setTimeout(function () {"
        'var loading = document.getElementById("application-loading");'
        'var template = document.getElementById("application-template");'
        "loading.replaceWith(template.content.cloneNode(true));"
        "template.remove();"
        "}, 1500);</script>"
    )


def render_flash_closed(body_html: str) -> str:
    """An SPA whose first render says "Job not found" (HTTP 200) until its data arrives
    800 ms later; then the posting and its form replace it (no network involved)."""
    return (
        '<div id="not-found"><h1>Job not found</h1><p>The job you requested was not found.</p></div>'
        f'<template id="posting-template">{body_html}</template>'
        "<script>setTimeout(function () {"
        'var notFound = document.getElementById("not-found");'
        'var template = document.getElementById("posting-template");'
        "notFound.replaceWith(template.content.cloneNode(true));"
        "template.remove();"
        "}, 800);</script>"
    )


ONETRUST_COOKIE = "OptanonAlertBoxClosed"
ONETRUST_STYLE = """
#onetrust-banner-sdk{position:fixed;left:0;right:0;bottom:0;z-index:2147483645;min-height:52vh;background:#fff;
box-shadow:0 -2px 12px rgba(0,0,0,.25);padding:1.25rem 1.5rem;box-sizing:border-box;display:none}
#onetrust-banner-sdk.ot-show{display:block}
#onetrust-button-group{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:1rem}
#onetrust-button-group button{padding:.6rem 1rem}
"""


def render_onetrust_banner() -> str:
    """OneTrust's consent banner as sites ship it: a fixed, non-modal region over the
    bottom half of the page that appears shortly after the page loads (``?cookie_ms``,
    600 ms) and takes the pointer there. "Reject All" and "Accept All Cookies" record the
    choice (``window.__cookieChoice`` and the ``OptanonAlertBoxClosed`` cookie, so the next
    page shows no banner) and hide it; "Cookies Settings" only records that it was opened."""
    return (
        '<div id="onetrust-consent-sdk"><div id="onetrust-banner-sdk" class="otFlat" tabindex="0" '
        'role="region" aria-label="Cookie banner"><div id="onetrust-group-container">'
        '<div id="onetrust-policy"><h2 id="onetrust-policy-title">We value your privacy</h2>'
        '<p id="onetrust-policy-text">We use cookies to personalise content and ads, to provide social '
        "media features and to analyse our traffic. You can accept all cookies or reject the ones that "
        "are not necessary.</p></div></div>"
        '<div id="onetrust-button-group-parent"><div id="onetrust-button-group">'
        '<button id="onetrust-pc-btn-handler" type="button">Cookies Settings</button>'
        '<button id="onetrust-reject-all-handler" type="button">Reject All</button>'
        '<button id="onetrust-accept-btn-handler" type="button">Accept All Cookies</button>'
        "</div></div></div></div>"
        f"<script>{ONETRUST_JS}</script>"
    )


def render_cookie_banner() -> str:
    """A modal consent dialog over the whole viewport. The page's ``main`` is inert and
    aria-hidden while it is shown; accepting or declining removes it, restores ``main``
    and sets the consent cookie so later visits show no banner."""
    return (
        '<div id="cookie-banner" role="dialog" aria-modal="true" aria-labelledby="cookie-title" '
        'style="position:fixed;inset:0;z-index:1000;background:rgba(29,35,48,.6);display:flex;'
        'align-items:flex-end;justify-content:center">'
        '<div style="background:#fff;padding:1rem 1.5rem;margin:1rem;max-width:40rem;border-radius:6px">'
        '<h2 id="cookie-title">This website uses cookies</h2>'
        "<p>We use cookies to remember your preferences and to measure how the careers site "
        "is used.</p>"
        '<button type="button" id="cookie-settings">Cookies settings</button> '
        '<button type="button" id="cookie-accept">Accept all</button> '
        '<button type="button" id="cookie-decline">Decline all</button>'
        "</div></div>"
        "<script>(function () {"
        "function consent(choice) {"
        f'document.cookie = "{CONSENT_COOKIE}=" + choice + "; path=/; max-age=86400";'
        'document.getElementById("cookie-banner").remove();'
        'var main = document.getElementById("main");'
        'main.removeAttribute("inert"); main.removeAttribute("aria-hidden");'
        "}"
        'document.getElementById("cookie-accept").addEventListener("click", function () { consent("accepted"); });'
        'document.getElementById("cookie-decline").addEventListener("click", function () { consent("declined"); });'
        "})();</script>"
    )


def captcha_svg(answer: str) -> str:
    glyphs = []
    for i, ch in enumerate(answer):
        x = 20 + i * 30
        y = 38 if i % 2 else 32
        angle = -14 if i % 2 else 11
        glyphs.append(
            f'<text x="{x}" y="{y}" transform="rotate({angle} {x} {y})">{esc(ch)}</text>'
        )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="180" height="56" viewBox="0 0 180 56">'
        '<rect width="180" height="56" fill="#f1efe6"/>'
        '<path d="M0 30 C40 5, 80 55, 180 20" stroke="#8a7f65" stroke-width="2" fill="none"/>'
        '<path d="M0 12 C60 50, 120 0, 180 44" stroke="#b3a88c" stroke-width="1.5" fill="none"/>'
        '<g font-family="Georgia,serif" font-size="28" fill="#2f2a1f">'
        + "".join(glyphs)
        + "</g></svg>"
    )


def display_value(f: Field, value: Any) -> str:
    if f.kind == "checkbox":
        return "Yes" if value == "yes" else "No"
    if value in (None, "", []):
        return "Not provided"
    if f.kind == "file":
        return f"{value['filename']} ({value['size']} bytes)"
    if isinstance(value, list):
        return ", ".join(f.option_label(v) for v in value)
    return f.option_label(value)


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

SLUG = r"(?P<slug>[a-z0-9-]+)"
DRAFT = r"(?P<draft_id>dft_\d{6})"
ROUTES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(p), method, name)
    for p, method, name in (
        (r"/", "GET", "get_index"),
        (r"/favicon\.ico", "GET", "get_favicon"),
        (rf"/jobs/{SLUG}", "GET", "get_job"),
        (rf"/jobs/{SLUG}/apply", "GET", "get_apply"),
        (rf"/jobs/{SLUG}/apply", "POST", "post_apply"),
        (rf"/jobs/{SLUG}/apply/{DRAFT}/step/(?P<step>\d+)", "GET", "get_step"),
        (rf"/jobs/{SLUG}/apply/{DRAFT}/step/(?P<step>\d+)", "POST", "post_step"),
        (rf"/jobs/{SLUG}/apply/{DRAFT}/review", "GET", "get_review"),
        (rf"/jobs/{SLUG}/apply/{DRAFT}/submit", "POST", "post_submit"),
        (rf"/jobs/{SLUG}/application-status", "GET", "get_status"),
        (rf"/jobs/{MODAL_WIZARD}/easy-apply", "POST", "post_easy_apply"),
        (r"/embed/job_app", "GET", "get_embed_job_app"),
        (rf"/jobs/{APPLY_IN_ALERT_FORM}/start", "POST", "post_alert_start"),
        (rf"/jobs/{APPLY_IN_ALERT_FORM}/alerts", "POST", "post_alert_subscribe"),
        (rf"/jobs/{APPLY_IN_ALERT_FORM}/apply/manual", "GET", "get_manual_apply"),
        (r"/applications/(?P<submission_id>sub_\d{6})", "GET", "get_confirmation"),
        (r"/login", "GET", "get_login"),
        (r"/login", "POST", "post_login"),
        (r"/captcha/(?P<token>cap_\d{6})\.svg", "GET", "get_captcha_svg"),
        (r"/captcha/widget\.html", "GET", "get_captcha_widget"),
        (r"/closed", "GET", "get_closed"),
        (r"/closed/not-found", "GET", "get_closed_not_found"),
        (r"/postings/with-select", "GET", "get_posting_with_select"),
        (r"/forms/unlabeled-custom-questions", "GET", "get_unlabeled_custom_questions"),
        (r"/forms/choices-without-values", "GET", "get_choices_without_values"),
        (r"/forms/breezy-like", "GET", "get_breezy_like"),
        (r"/forms/ashby-like", "GET", "get_ashby_like"),
        (r"/postings/apply-wording", "GET", "get_apply_wording_posting"),
        (r"/postings/go-apply", "POST", "post_go_apply"),
        (r"/__fixture__/cities", "GET", "get_fixture_cities"),
        (rf"/jobs/{WD.SLUG}/apply/(?P<route>autofillWithResume|applyManually|useMyLastApplication)",
         "GET", "get_wd_route"),
        (rf"/jobs/{WD.SLUG}/account", "POST", "post_wd_account"),
        (rf"/jobs/{WD.SLUG}/wizard/(?P<action>save|upload|submit)", "POST", "post_wd_wizard"),
        (rf"/jobs/{WD.SLUG}/wizard/submitted", "GET", "get_wd_submitted"),
        (r"/__test__/workday", "GET", "test_workday"),
        (r"/__test__/health", "GET", "test_health"),
        (r"/__test__/jobs", "GET", "test_jobs"),
        (r"/__test__/submissions", "GET", "test_submissions"),
        (r"/__test__/submissions/(?P<submission_id>sub_\d{6})", "GET", "test_submission"),
        (r"/__test__/submissions/(?P<submission_id>sub_\d{6})/reveal", "POST", "test_reveal"),
        (r"/__test__/captcha/(?P<token>cap_\d{6})", "GET", "test_captcha"),
        (r"/__test__/reset", "POST", "test_reset"),
        (r"/__test__/shutdown", "POST", "test_shutdown"),
    )
]


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: MockATS):
        self.app = app
        super().__init__(address, Handler)

    def server_bind(self) -> None:
        # Skip HTTPServer's reverse DNS lookup (socket.getfqdn), which can stall.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "BramblewayMockATS/1.0"

    @property
    def app(self) -> MockATS:
        return self.server.app

    @property
    def store(self) -> Store:
        return self.app.store

    def log_message(self, format: str, *args: Any) -> None:
        if self.app.verbose:
            super().log_message(format, *args)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        split = urlsplit(self.path)
        self.query = parse_qs(split.query, keep_blank_values=True)
        path = split.path
        try:
            matched_path = False
            for pattern, route_method, name in ROUTES:
                match = pattern.fullmatch(path)
                if match:
                    matched_path = True
                    if route_method == method:
                        getattr(self, name)(**match.groupdict())
                        return
            if matched_path:
                raise HttpError(HTTPStatus.METHOD_NOT_ALLOWED)
            raise HttpError(HTTPStatus.NOT_FOUND)
        except HttpError as exc:
            if path.startswith("/__test__/"):
                self._send_json(exc.status, {"error": exc.message})
            else:
                body = f"<h1>{esc(exc.status.phrase)}</h1><p>{esc(exc.message)}</p>"
                self._send_html(exc.status, page(exc.status.phrase, body))
        except (BrokenPipeError, ConnectionResetError):
            pass

    # response helpers
    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: HTTPStatus, document: str, headers=()) -> None:
        self._send(status, document.encode("utf-8"), "text/html; charset=utf-8", headers)

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self._send(status, body, "application/json")

    def _redirect(self, location: str, headers: tuple[tuple[str, str], ...] = ()) -> None:
        self._send(
            HTTPStatus.SEE_OTHER,
            b"",
            "text/plain; charset=utf-8",
            (("Location", location), *headers),
        )

    # request helpers
    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if length is None:
            if self.headers.get("Transfer-Encoding"):
                raise HttpError(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
            return b""
        try:
            size = int(length)
        except ValueError:
            raise HttpError(HTTPStatus.BAD_REQUEST, "invalid Content-Length") from None
        if size < 0:
            raise HttpError(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
        if size > MAX_BODY_BYTES:
            raise HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        return self.rfile.read(size)

    def _read_form(self) -> tuple[dict[str, list[str]], dict[str, list[Upload]]]:
        content_type = self.headers.get("Content-Type", "")
        body = self._read_body()
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type == "multipart/form-data":
            return parse_multipart(body, content_type)
        if media_type == "application/x-www-form-urlencoded":
            return parse_qs(body.decode("ascii", "replace"), keep_blank_values=True), {}
        if not body:
            return {}, {}
        raise HttpError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "expected an HTML form submission")

    def _job(self, slug: str) -> Job:
        job = JOBS.get(slug)
        if job is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "No such job.")
        return job

    def _param(self, name: str) -> str:
        """The first value of a query parameter ("" when absent)."""
        return (self.query.get(name) or [""])[0]

    def _signed_in(self) -> bool:
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        morsel = cookie.get(SESSION_COOKIE)
        return bool(morsel and self.store.has_session(morsel.value))

    def _redirect_to_login(self, job: Job) -> None:
        self._redirect(f"/login?next={quote(f'/jobs/{job.slug}/apply')}")

    def _consented(self) -> bool:
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        morsel = cookie.get(CONSENT_COOKIE)
        return bool(morsel and morsel.value in ("accepted", "declined"))

    def _data_consented(self) -> bool:
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        morsel = cookie.get(JV_CONSENT_COOKIE)
        return bool(morsel and morsel.value == "accepted")

    def _render_data_consent(self, job: Job) -> None:
        """Jobvite's "Data Consent" page (a posting's apply URL until the consent is
        accepted): choosing the policy shows it with "I Accept" (a submit button that posts
        the policy ids back to the apply URL) and "I Decline" (a link to the posting).
        ``?policies=regional`` offers one policy per location of residence and language
        instead of the one global policy."""
        if self.query.get("policies", [""])[0] == "regional":
            policies = "".join(f'<option value="{value}">{label}</option>' for value, label in JV_REGIONAL)
        else:
            policies = (f'<option value="{JV_POLICY_ID}">Global {COMPANY.upper()} APPLICANT AND '
                        "CANDIDATE PRIVACY POLICY</option>")
        body = (
            f'<h1 class="jv-logo">{COMPANY} Careers</h1>'
            '<article class="jv-page-body"><h3>Data Consent</h3>'
            f'<form name="consentForm" class="jv-form" method="POST" action="/jobs/{job.slug}/apply">'
            '<div><label for="jv-country-select">Location of Residence and Language:</label></div>'
            '<select id="jv-country-select" required>'
            '<option value="" selected>Select your location of residence and language</option>'
            f"{policies}</select>"
            f'<div id="jv-back"><a class="jv-button" href="/jobs/{job.slug}">Back</a></div>'
            '<div id="jv-policy" hidden><p class="jv-policy-text">This fictional privacy policy '
            f"explains how {COMPANY} processes the personal data in your application.</p>"
            '<div id="jv-accept-reject"></div></div></form></article>'
            f"<script>{JV_CONSENT_JS}</script>"
        )
        self._send_html(HTTPStatus.OK, page(f"{COMPANY} Careers", body))

    # public pages
    def get_index(self) -> None:
        items = "".join(
            f'<li><a href="/jobs/{job.slug}">{esc(job.title)}</a> '
            f'<span class="meta">— {esc(job.department)}, {esc(job.location)}</span></li>'
            for job in JOBS.values()
        )
        body = f"<h1>Open roles at {COMPANY}</h1><ul>{items}</ul>"
        self._send_html(HTTPStatus.OK, page("Open roles", body))

    def get_favicon(self) -> None:
        # Keeps browser consoles free of an unrelated 404.
        self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")

    def _posting_head(self, job: Job) -> str:
        """The canonical link and schema.org ``JobPosting`` JSON-LD block of a posting."""
        ld = {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": job.title,
            "identifier": {"@type": "PropertyValue", "name": COMPANY, "value": job.code},
            "hiringOrganization": {"@type": "Organization", "name": COMPANY},
            "jobLocation": {
                "@type": "Place",
                "address": {"@type": "PostalAddress", "addressLocality": job.location},
            },
            "employmentType": "FULL_TIME",
            "datePosted": "2026-09-01",
            "description": f"{job.title} on the {job.department} team at {COMPANY}.",
        }
        return (
            f'<link rel="canonical" href="{esc(self.app.origin)}/jobs/{job.slug}">'
            '<script type="application/ld+json">'
            + json.dumps(ld).replace("</", "<\\/")
            + "</script>"
        )

    def get_job(self, slug: str) -> None:
        job = self._job(slug)
        if slug == WD.SLUG:
            self._send_html(HTTPStatus.OK, WD.posting_html(self.app.origin))
            return
        if job.slug == MODAL_WIZARD:
            self._render_easy_apply(job, auto_open=False)
            return
        if job.slug == IFRAME_EMBED:
            self._render_careers_embed(job)
            return
        if job.slug == APPLY_IN_ALERT_FORM:
            self._render_alert_posting(job)
            return
        head = self._posting_head(job)
        body = (
            _job_heading(job)
            + f"<h2>About the role</h2><p>{COMPANY} builds forecasting tools for regional "
            f"logistics networks. As a {esc(job.title)} on the {esc(job.department)} team, you "
            "will design, build and operate production systems with a small, collaborative "
            "group.</p>"
            "<h2>What we offer</h2><ul><li>Health, dental and vision coverage</li>"
            "<li>Flexible hours</li><li>Annual learning budget</li></ul>"
            f'<p><a class="button" href="/jobs/{job.slug}/apply">Apply for this job</a></p>'
            f'<p><a href="/jobs/{job.slug}/application-status">Already applied? '
            "Check your application status</a></p>"
        )
        self._send_html(HTTPStatus.OK, page(job.title, body, head))

    def _fields(self, job: Job) -> tuple[Field, ...]:
        """The single-step form's questions as the server currently serves them."""
        if job.added_on_reload is not None and self.store.loads(job.slug) > 1:
            return (*job.fields, job.added_on_reload)
        return job.fields

    def get_apply(self, slug: str) -> None:
        job = self._job(slug)
        if slug == WD.SLUG:
            self._send_html(HTTPStatus.OK, WD.apply_page_html(self.app.origin))
            return
        if job.requires_signin and not self._signed_in():
            self._redirect_to_login(job)
            return
        if job.added_on_reload is not None:
            self.store.count_load(job.slug)
        if job.data_consent and not self._data_consented():
            self._render_data_consent(job)
            return
        if job.slug == MODAL_WIZARD:  # the SDUI apply URL: the job view with its dialog open
            self._render_easy_apply(job, auto_open=True)
            return
        if job.slug == STEPPER_AMBIGUOUS:
            self._render_jazzhr(job, {}, {}, {}, HTTPStatus.OK)
            return
        if job.slug == APPLY_IN_ALERT_FORM and self._param("flowSelection") == "true":
            self._render_flow_selection(job)
            return
        if job.multistep:
            self._render_step(job, None, 1, {}, {}, None, HTTPStatus.OK)
        else:
            self._render_single(job, {}, {}, {}, None, HTTPStatus.OK)

    def post_apply(self, slug: str) -> None:
        job = self._job(slug)
        if slug == WD.SLUG:
            self._read_body()
            raise HttpError(HTTPStatus.METHOD_NOT_ALLOWED)
        if job.requires_signin and not self._signed_in():
            self._read_body()
            self._redirect_to_login(job)
            return
        if job.slug == MODAL_WIZARD:
            self._read_body()
            raise HttpError(HTTPStatus.METHOD_NOT_ALLOWED, "Easy Apply applications are sent from the dialog only.")
        form, uploads = self._read_form()
        if job.data_consent and form.get("policyIds"):
            # Jobvite: "I Accept" posts the chosen policy back to the apply URL, which
            # records the consent and returns the form.
            self.store.add_consent(job, form["policyIds"][0])
            self._render_single(job, {}, {}, {}, None, HTTPStatus.OK)
            return
        if job.data_consent and not self._data_consented():
            self._render_data_consent(job)  # no application is taken before the consent
            return
        if job.multistep:
            self._post_step(job, None, 1, form, uploads)
            return

        retained: dict[str, dict[str, Any]] = {}
        prior = self.store.get_upload((form.get("resume_upload_id") or [""])[0])
        if prior:
            retained["resume"] = prior
        fields = revealed_fields(self._fields(job), form)
        values, files, errors = validate(
            fields, form, uploads, retained, strict_phone=job.strict_phone
        )
        if (job.slug == STEPPER_AMBIGUOUS and values.get(JZ_RESUME_TEXT.name)
                and errors.get(JZ_RESUME.name) == _required_message(JZ_RESUME)):
            del errors[JZ_RESUME.name]  # a pasted resume stands in for the file
        if job.fixture_identity:
            for name, message in fixture_identity_errors(job.fields, values, files).items():
                if name not in errors:
                    errors[name] = message
                    files.pop(name, None)  # like an invalid upload, a wrong file is not kept
        files_meta = self._store_files(files)
        captcha_error = None
        if job.captcha:
            token = (form.get("captcha_token") or [""])[0]
            answer = (form.get("captcha_answer") or [""])[0]
            if not answer.strip():
                captcha_error = "Enter the characters shown in the image."
            elif not self.store.consume_captcha(token, answer):
                captcha_error = "The characters did not match. Try the new image."
            if captcha_error:
                errors["captcha_answer"] = captcha_error
        if job.honeypot and (form.get(HONEYPOT_FIELD) or [""])[0].strip():
            errors[HONEYPOT_FIELD] = "Your submission was flagged as automated."
        if job.captcha_widget and not (form.get(CAPTCHA_WIDGET_FIELD) or [""])[0].strip():
            errors[CAPTCHA_WIDGET_FIELD] = "Please complete the CAPTCHA."
        if errors:
            self.store.add_rejection(job, errors)
            if job.slug == STEPPER_AMBIGUOUS:
                self._render_jazzhr(job, form, errors, files_meta, HTTPStatus.UNPROCESSABLE_ENTITY)
                return
            self._render_single(
                job, form, errors, files_meta, captcha_error, HTTPStatus.UNPROCESSABLE_ENTITY
            )
            return

        record = self.store.add_submission(job, values, _extra_fields(fields, form), files_meta)
        if job.generic_thanks:
            # Accepted and counted; the page proves nothing about which application.
            body = "<h1>Thank you!</h1><p>We appreciate your interest.</p>"
            self._send_html(HTTPStatus.OK, page("Thank you", body))
            return
        if job.visible_confirmation:
            self._redirect(f"/applications/{record['submission_id']}")
            return
        # Accepted and counted, but the response withholds any confirmation.
        body = (
            "<h1>Something went wrong</h1>"
            "<p>The server did not respond in time. Please try again later.</p>"
            f'<p><a href="/jobs/{job.slug}">Return to the job posting</a></p>'
        )
        self._send_html(HTTPStatus.BAD_GATEWAY, page("Error", body))

    def _store_files(
        self, files: dict[str, Upload | dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        return {
            name: self.store.store_upload(f) if isinstance(f, Upload) else f
            for name, f in files.items()
        }

    def _render_single(
        self,
        job: Job,
        values: dict[str, list[str]],
        errors: dict[str, str],
        retained: dict[str, dict[str, Any]],
        captcha_error: str | None,
        status: HTTPStatus,
    ) -> None:
        title, body, head = self._single_form(job, values, errors, retained, captcha_error, status)
        main_attrs, after_main = "", ""
        if job.cookie_banner and not self._consented():
            main_attrs, after_main = ' inert aria-hidden="true"', render_cookie_banner()
        self._send_html(status, page(title, body, head, main_attrs=main_attrs, after_main=after_main))

    def _single_form(
        self,
        job: Job,
        values: dict[str, list[str]],
        errors: dict[str, str],
        retained: dict[str, dict[str, Any]],
        captcha_error: str | None,
        status: HTTPStatus,
    ) -> tuple[str, str, str]:
        """(title, main body, head extra) of a single-page application form."""
        # The fields the server serves now (a reload may add one; a posted choice shows
        # its follow-up questions, as page script would after the choice).
        fields = revealed_fields(self._fields(job), values)
        entries = _summary_entries(fields, errors)
        if captcha_error:
            entries.append(("f-captcha_answer", "Characters shown in the image", captcha_error))
        if errors.get(CAPTCHA_WIDGET_FIELD):
            entries.append((CAPTCHA_WIDGET_FIELD, "CAPTCHA", errors[CAPTCHA_WIDGET_FIELD]))
        fields_html = _field_layout(job, [
            (f, render_field(f, values, errors.get(f.name), retained.get(f.name))) for f in fields
        ])
        if job.captcha:
            fields_html += render_captcha(self.store.new_captcha(), captcha_error)
        if job.honeypot:
            # Classic visually hidden anti-spam trap; people and correct runtimes leave it blank.
            fields_html += (
                '<div aria-hidden="true" style="position:absolute;left:-10000px;top:auto;'
                'width:1px;height:1px;overflow:hidden">'
                f'<label for="f-{HONEYPOT_FIELD}">Leave this field blank</label>'
                f'<input type="text" id="f-{HONEYPOT_FIELD}" name="{HONEYPOT_FIELD}" '
                'tabindex="-1" autocomplete="off"></div>'
            )
        if job.captcha_widget:
            fields_html += render_captcha_widget(errors.get(CAPTCHA_WIDGET_FIELD))
        if job.formless:
            # No <form> element: page script collects the questions and posts them.
            form_html = (
                f'<div id="application" class="application" data-action="/jobs/{job.slug}/apply">'
                '<h2 id="form-title">Application form</h2>'
                '<p class="hint">Fields marked with * are required.</p>'
                + fields_html
                + '<button type="button" id="submit-application">Submit application</button></div>'
            )
        else:
            submit = '<button type="submit">Submit application</button>'
            if any(f.uploader == "greenhouse-async" for f in fields):
                # Re-rendered once the upload completes, one level up (see ghUpload).
                submit = f'<div class="form-actions"><div class="actions-row">{submit}</div></div>'
            form_html = (
                f'<form method="post" action="/jobs/{job.slug}/apply" enctype="multipart/form-data" '
                'aria-labelledby="form-title"><h2 id="form-title">Application form</h2>'
                '<p class="hint">Fields marked with * are required.</p>'
                + fields_html
                + submit + "</form>"
            )
        widgets = any(f.scripted for f in fields)
        if widgets:
            form_html += f"<script>{WIDGETS_JS}</script>"
        if any(f.reveals or f.required_after for f in fields):
            form_html += f"<script>{REVEAL_JS}</script>"
        if any(f.fabric_id is not None for f in fields):
            form_html += f"<script>{FABRIC_JS}</script>"
        if any(f.lazy for f in fields):
            form_html += f"<script>{LAZY_JS}</script>"
        if any(f.aria for f in fields):
            form_html += f"<script>{ARIA_CHOICE_JS}</script>"
        if any(f.disabled_by for f in fields):
            form_html += f"<script>{DISABLE_JS}</script>"
        if job.formless:
            form_html += f"<script>{FORMLESS_JS}</script>"
        if job.validity:
            form_html += f"<script>{VALIDITY_JS}</script>"
        if job.autofill:
            form_html = (
                '<div class="autofill"><button type="button" id="autofill-application">'
                "Autofill my application</button></div>" + form_html + f"<script>{AUTOFILL_JS}</script>"
            )
        scripted = job.slug in SCENARIO_JOBS
        if scripted:
            form_html += f'<script data-scenario="{esc(job.slug)}">{SCENARIO_JS}</script>'
        if job.spa_loading and status == HTTPStatus.OK:
            form_html = render_delayed(form_html)
        # Jobvite's apply page names the job by its title only (no job id on the page).
        heading = f"<h2>{esc(job.title)}</h2>" if job.data_consent else _job_heading(job)
        if job.description_px:
            heading += _job_description(job)
        body = heading + render_error_summary(entries) + form_html
        if job.flash_closed and status == HTTPStatus.OK:
            body = render_flash_closed(body)
        title = f"Apply: {job.title}" if not errors else f"Error: Apply: {job.title}"
        head = f"<style>{WIDGET_STYLE}</style>" if widgets else ""
        if scripted:
            head += f"<style>{SCENARIO_STYLE}</style>"
        return title, body, head


    # multistep
    def _draft(self, job: Job, draft_id: str) -> dict[str, Any]:
        draft = self.store.get_draft(draft_id)
        if draft is None or draft["job_id"] != job.slug:
            raise HttpError(HTTPStatus.NOT_FOUND, "This application draft does not exist.")
        return draft

    @staticmethod
    def _first_incomplete(job: Job, draft: dict[str, Any]) -> int | None:
        return next(
            (n for n in range(1, len(job.steps) + 1) if str(n) not in draft["steps"]), None
        )

    def _step_number(self, job: Job, step: str) -> int:
        n = int(step)
        if not job.multistep or not 1 <= n <= len(job.steps):
            raise HttpError(HTTPStatus.NOT_FOUND, "No such step.")
        return n

    def get_step(self, slug: str, draft_id: str, step: str) -> None:
        job = self._job(slug)
        n = self._step_number(job, step)
        draft = self._draft(job, draft_id)
        first = self._first_incomplete(job, draft)
        if first is not None and n > first:
            self._redirect(f"/jobs/{slug}/apply/{draft_id}/step/{first}")
            return
        values = _as_lists(draft["steps"].get(str(n), {}))
        self._render_step(job, draft, n, values, {}, None, HTTPStatus.OK)

    def post_step(self, slug: str, draft_id: str, step: str) -> None:
        job = self._job(slug)
        n = self._step_number(job, step)
        draft = self._draft(job, draft_id)
        form, uploads = self._read_form()
        self._post_step(job, draft, n, form, uploads)

    def _post_step(
        self,
        job: Job,
        draft: dict[str, Any] | None,
        n: int,
        form: dict[str, list[str]],
        uploads: dict[str, list[Upload]],
    ) -> None:
        step_fields = revealed_fields(job.steps[n - 1].fields, form)
        retained = {
            f.name: draft["files"][f.name]
            for f in step_fields
            if draft and f.name in draft["files"]
        }
        values, files, errors = validate(step_fields, form, uploads, retained)
        files_meta = self._store_files(files)
        if errors:
            self.store.add_rejection(job, errors, step=n)
            self._render_step(
                job, draft, n, form, errors, retained | files_meta, HTTPStatus.UNPROCESSABLE_ENTITY
            )
            return
        draft = self.store.save_step(
            job, draft["draft_id"] if draft else None, n, values, files_meta
        )
        base = f"/jobs/{job.slug}/apply/{draft['draft_id']}"
        self._redirect(f"{base}/step/{n + 1}" if n < len(job.steps) else f"{base}/review")

    def _progress(self, job: Job, current: int) -> str:
        titles = [s.title for s in job.steps] + ["Review and submit"]
        current_attr = ' aria-current="step"'
        items = "".join(
            f"<li{current_attr if i == current else ''}>{esc(t)}</li>"
            for i, t in enumerate(titles, start=1)
        )
        return (
            f'<nav aria-label="Application progress"><ol class="progress">{items}</ol></nav>'
            f'<p class="meta">Step {current} of {len(titles)}</p>'
        )

    def _render_step(
        self,
        job: Job,
        draft: dict[str, Any] | None,
        n: int,
        values: dict[str, list[str]],
        errors: dict[str, str],
        retained: dict[str, dict[str, Any]] | None,
        status: HTTPStatus,
    ) -> None:
        step = job.steps[n - 1]
        if retained is None:
            retained = draft["files"] if draft else {}
        if draft is None:
            action = f"/jobs/{job.slug}/apply"
        else:
            action = f"/jobs/{job.slug}/apply/{draft['draft_id']}/step/{n}"
        fields = revealed_fields(step.fields, values)
        has_file = any(f.kind == "file" for f in fields)
        enctype = "multipart/form-data" if has_file else "application/x-www-form-urlencoded"
        submit_id = f' id="{esc(job.next_id)}"' if job.next_id else ""
        # An input-select's input stays empty (its value lives in page state) while it is
        # required: like Paylocity, the step validates in script, not natively.
        novalidate = " novalidate" if any(f.kind == "pcty_select" for f in fields) else ""
        back = ""
        if n > 1 and draft is not None:
            back = f' <a href="/jobs/{job.slug}/apply/{draft["draft_id"]}/step/{n - 1}">Back</a>'
        body = (
            _job_heading(job)
            + self._progress(job, n)
            + render_error_summary(_summary_entries(fields, errors))
            + f'<form method="post" action="{action}" enctype="{enctype}"{novalidate} '
            f'aria-labelledby="form-title"><h2 id="form-title">{esc(step.title)}</h2>'
            '<p class="hint">Fields marked with * are required.</p>'
            + "".join(
                render_field(f, values, errors.get(f.name), retained.get(f.name))
                for f in fields
            )
            + f'<button type="submit"{submit_id}>Continue</button>{back}</form>'
        )
        widgets = any(f.scripted for f in fields)
        if widgets:
            body += f"<script>{WIDGETS_JS}</script>"
        if any(f.reveals or f.required_after for f in fields):
            body += f"<script>{REVEAL_JS}</script>"
        head = f"<style>{WIDGET_STYLE}</style>" if widgets else ""
        after_main = ""
        if job.onetrust:
            head += f"<style>{ONETRUST_STYLE}</style>"
            after_main = render_onetrust_banner()
        prefix = "Error: " if errors else ""
        self._send_html(status, page(f"{prefix}{step.title}: {job.title}", body, head, after_main=after_main))

    def get_review(self, slug: str, draft_id: str) -> None:
        job = self._job(slug)
        draft = self._draft(job, draft_id)
        first = self._first_incomplete(job, draft)
        if first is not None:
            self._redirect(f"/jobs/{slug}/apply/{draft_id}/step/{first}")
            return
        sections = []
        for n, step in enumerate(job.steps, start=1):
            stored = draft["steps"][str(n)]
            rows = "".join(
                f"<dt>{esc(f.label)}</dt><dd>"
                + esc(
                    display_value(
                        f, draft["files"].get(f.name) if f.kind == "file" else stored.get(f.name)
                    )
                )
                + "</dd>"
                for f in step.fields
            )
            sections.append(
                f"<section><h3>{esc(step.title)}</h3>"
                f'<p><a href="/jobs/{slug}/apply/{draft_id}/step/{n}">Edit {esc(step.title.lower())}</a></p>'
                f'<dl class="review">{rows}</dl></section>'
            )
        body = (
            _job_heading(job)
            + self._progress(job, len(job.steps) + 1)
            + '<h2 id="form-title">Review your application</h2>'
            + "".join(sections)
            + f'<form method="post" action="/jobs/{slug}/apply/{draft_id}/submit" '
            'aria-labelledby="form-title"><button type="submit">Submit application</button> '
            f'<a href="/jobs/{slug}/apply/{draft_id}/step/{len(job.steps)}">Back</a></form>'
        )
        head, after_main = "", ""
        if job.onetrust:
            head, after_main = f"<style>{ONETRUST_STYLE}</style>", render_onetrust_banner()
        self._send_html(HTTPStatus.OK, page(f"Review: {job.title}", body, head, after_main=after_main))

    def post_submit(self, slug: str, draft_id: str) -> None:
        job = self._job(slug)
        draft = self._draft(job, draft_id)
        self._read_body()
        first = self._first_incomplete(job, draft)
        if first is not None:
            self._redirect(f"/jobs/{slug}/apply/{draft_id}/step/{first}")
            return
        values: dict[str, Any] = {}
        for n in range(1, len(job.steps) + 1):
            values.update(draft["steps"][str(n)])
        record = self.store.add_submission(job, values, {}, dict(draft["files"]), draft_id)
        self._redirect(f"/applications/{record['submission_id']}")

    # confirmation and status
    def get_confirmation(self, submission_id: str) -> None:
        record = self.store.get_submission(submission_id)
        if record is None or not record["confirmation_visible"]:
            raise HttpError(HTTPStatus.NOT_FOUND, "Page not found.")
        job = JOBS[record["job_id"]]
        name = record["fields"].get("first_name", "")
        body = (
            "<h1>Application submitted</h1>"
            '<div role="status">'
            f"<p>Thank you{', ' + esc(name) if name else ''}. Your application for "
            f"<strong>{esc(job.title)}</strong> (Job ID {esc(job.code)}) at {COMPANY} was "
            f"received on {esc(record['received_at'])}.</p>"
            f"<p>Confirmation reference: <strong>{esc(record['confirmation_reference'])}</strong></p>"
            "</div>"
            f'<p><a href="/jobs/{job.slug}">Back to the job posting</a></p>'
        )
        self._send_html(HTTPStatus.OK, page("Application submitted", body))

    def get_status(self, slug: str) -> None:
        job = self._job(slug)
        email = (self.query.get("email") or [""])[0].strip()
        result = ""
        if email:
            records = self.store.find_submissions(job.slug, email)
            if not records:
                message = "<p>We could not find an application from this email address for this job.</p>"
            else:
                items = []
                for r in records:
                    if r["confirmation_visible"]:
                        items.append(
                            f"<li>Application received on {esc(r['received_at'])}. "
                            f"Confirmation reference: <strong>{esc(r['confirmation_reference'])}</strong>. "
                            f'<a href="/applications/{r["submission_id"]}">View confirmation</a></li>'
                        )
                    else:
                        items.append(
                            "<li>We are still processing a recent application from this email "
                            "address and cannot confirm it yet. Check back later.</li>"
                        )
                message = f"<ul>{''.join(items)}</ul>"
            result = (
                '<section role="status" aria-labelledby="status-result-title">'
                f'<h2 id="status-result-title">Status for {esc(email)}</h2>{message}</section>'
            )
        body = (
            _job_heading(job)
            + "<h2>Check your application status</h2>"
            + f'<form method="get" action="/jobs/{job.slug}/application-status">'
            '<div class="field"><label for="f-status-email">Email used on your application</label>'
            f'<input type="email" id="f-status-email" name="email" value="{esc(email)}" '
            'autocomplete="email" required></div>'
            '<button type="submit">Check status</button></form>'
            + result
        )
        self._send_html(HTTPStatus.OK, page(f"Application status: {job.title}", body))

    # workday-wizard (scripts/mock_workday.py)
    def _wd_email(self) -> str | None:
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        morsel = cookie.get(WD.SESSION_COOKIE)
        return self.store.session_email(morsel.value) if morsel else None

    def get_wd_route(self, route: str) -> None:
        self.store.wd_note("route_visits", route)
        email = self._wd_email()
        if email is None:
            mode = "signin" if (self.query.get("view") or [""])[0] == "signin" else "create"
            self._send_html(HTTPStatus.OK, WD.account_html(self.app.origin, mode=mode))
            return
        if route != "applyManually":
            body = f"<h3>{esc(route)}</h3><p>This route is not part of the fixture flow.</p>"
            self._send_html(HTTPStatus.OK, page(route, body))
            return
        draft = self.store.wd_draft(email)
        self._send_html(HTTPStatus.OK, WD.wizard_html(
            self.app.origin, email=email, saved=draft["pages"], resume=draft["resume"]))

    def post_wd_account(self) -> None:
        form, _ = self._read_form()
        mode = (form.get("mode") or ["create"])[0]
        email = (form.get("email") or [""])[0].strip().lower()
        password = (form.get("password") or [""])[0]
        error = None
        if not email or not password:
            error = "Enter your email address and password."
        elif mode == "create":
            verify = (form.get("verifyPassword") or [""])[0]
            if password != verify:
                error = "The passwords do not match."
            elif not (form.get("createAccountCheckbox") or [""])[0]:
                error = "Acknowledge the Candidate Privacy Notice to create an account."
            elif not self.store.wd_create_account(email, password):
                error = "An account with this email address already exists. Sign in instead."
        elif not ((email == SIGNIN_EMAIL and password == SIGNIN_PASSWORD)
                  or self.store.wd_account_ok(email, password)):
            error = "Wrong email address or password."
        if error is not None:
            self._send_html(HTTPStatus.UNPROCESSABLE_ENTITY,
                            WD.account_html(self.app.origin, mode=mode, error=error, email=email))
            return
        token = self.store.new_session(email)
        cookie = f"{WD.SESSION_COOKIE}={token}; Path=/; Max-Age=86400; HttpOnly; SameSite=Lax"
        self._redirect(f"/jobs/{WD.SLUG}/apply/applyManually", (("Set-Cookie", cookie),))

    def post_wd_wizard(self, action: str) -> None:
        email = self._wd_email()
        if email is None:
            self._read_body()
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Sign in to continue."})
            return
        job = self._job(WD.SLUG)
        if action == "upload":
            _, files = self._read_form()
            upload = (files.get("file") or [None])[0]
            if upload is None or not upload.filename.lower().endswith((".pdf", ".doc", ".docx")):
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "Upload a .pdf, .doc or .docx file."})
                return
            meta = self.store.store_upload(upload)
            self.store.wd_set_resume(email, meta)
            self._send_json(HTTPStatus.OK, {"filename": meta["filename"], "upload_id": meta["upload_id"]})
            return
        try:
            payload = json.loads(self._read_body() or b"{}")
        except ValueError:
            raise HttpError(HTTPStatus.BAD_REQUEST, "expected JSON") from None
        draft = self.store.wd_draft(email)
        if action == "save":
            page_id = str(payload.get("page") or "")
            values = payload.get("values") or {}
            errors = WD.validate_page(page_id, values, draft["resume"])
            self.store.wd_note("saves", {"page": page_id, "ok": not errors, "errors": errors,
                                         "values": values})
            if errors:
                step = WD.PAGE_IDS.index(page_id) + 2 if page_id in WD.PAGE_IDS else None
                self.store.add_rejection(job, errors, step)
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"errors": errors})
                return
            self.store.wd_save_page(email, page_id, values)
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        # submit: the only request that records (and counts) an application
        self.store.wd_note("submit_calls", email)
        missing = [p for p in WD.PAGE_IDS if p not in draft["pages"]
                   or WD.validate_page(p, draft["pages"][p], draft["resume"])]
        if missing:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY,
                            {"error": "Complete every page before submitting: " + ", ".join(missing)})
            return
        fields = {name: value for p in WD.PAGE_IDS for name, value in draft["pages"][p].items()
                  if value not in (None, "", [], False)}
        resume = {k: v for k, v in (draft["resume"] or {}).items()}
        record = self.store.add_submission(job, fields, {}, {"resume": resume} if resume else {})
        self._send_json(HTTPStatus.OK, {"reference": record["confirmation_reference"]})

    def get_wd_submitted(self) -> None:
        reference = (self.query.get("ref") or [""])[0]
        self._send_html(HTTPStatus.OK, WD.submitted_html(self.app.origin, reference))

    # sign-in
    def _login_page(self, next_path: str, error: str | None, email: str, status: HTTPStatus) -> None:
        alert = f'<div class="error-summary" role="alert"><p>{esc(error)}</p></div>' if error else ""
        body = (
            "<h1>Sign in to continue your application</h1>"
            + alert
            + '<form method="post" action="/login">'
            f'<input type="hidden" name="next" value="{esc(next_path)}">'
            '<div class="field"><label for="f-login-email">Email</label>'
            f'<input type="email" id="f-login-email" name="email" value="{esc(email)}" '
            'autocomplete="username" required></div>'
            '<div class="field"><label for="f-login-password">Password</label>'
            '<input type="password" id="f-login-password" name="password" '
            'autocomplete="current-password" required></div>'
            '<button type="submit">Sign in</button></form>'
        )
        self._send_html(status, page("Sign in", body))

    @staticmethod
    def _safe_next(value: str) -> str:
        return value if value.startswith("/") and not value.startswith("//") else "/"

    def get_login(self) -> None:
        next_path = self._safe_next((self.query.get("next") or ["/"])[0])
        self._login_page(next_path, None, "", HTTPStatus.OK)

    def post_login(self) -> None:
        form, _ = self._read_form()
        email = (form.get("email") or [""])[0].strip()
        password = (form.get("password") or [""])[0]
        next_path = self._safe_next((form.get("next") or ["/"])[0])
        if email.lower() != SIGNIN_EMAIL or password != SIGNIN_PASSWORD:
            self._login_page(
                next_path, "Incorrect email or password.", email, HTTPStatus.UNAUTHORIZED
            )
            return
        token = self.store.new_session(email.lower())
        cookie = f"{SESSION_COOKIE}={token}; Path=/; Max-Age=86400; HttpOnly; SameSite=Lax"
        self._redirect(next_path, (("Set-Cookie", cookie),))

    def get_captcha_svg(self, token: str) -> None:
        challenge = self.store.captcha(token)
        if challenge is None:
            raise HttpError(HTTPStatus.NOT_FOUND)
        self._send(HTTPStatus.OK, captcha_svg(challenge["answer"]).encode(), "image/svg+xml")

    def get_posting_with_select(self) -> None:
        # The `standard` posting the way some ATS vendors render it: "Apply now" buttons
        # that navigate by script, a share widget and a language select, all outside
        # any form. It is a job description, not an application form.
        job = JOBS["standard"]
        apply_button = (
            '<button type="button" class="apply" '
            f"onclick=\"location.href='/jobs/{job.slug}/apply'\">Apply now</button>"
        )
        body = (
            _job_heading(job)
            + f"<p>{apply_button}</p>"
            + f"<h2>About the role</h2><p>{COMPANY} builds forecasting tools for regional "
            f"logistics networks. As a {esc(job.title)} you will design, build and operate "
            "production systems with a small, collaborative group.</p>"
            '<div class="share" aria-label="Share this job"><span>Share:</span> '
            '<button type="button">Share</button> <button type="button">Copy link</button></div>'
            + f"<p>{apply_button}</p>"
            '<div class="locale"><label for="locale">Language</label>'
            '<select id="locale" name="locale">'
            '<option value="en-US" selected>United States (English)</option>'
            '<option value="fr-CA">Canada (Français)</option></select></div>'
        )
        self._send_html(HTTPStatus.OK, page(job.title, body))

    def get_unlabeled_custom_questions(self) -> None:
        # Custom questions the way some ATS vendors render them: no <label>, the visible
        # question in a preceding block ending with a required marker, and inputs named
        # after an opaque card id. Only the two contact fields carry a required attribute.
        card = "cards[2a269d5e-6f40-4ed1-ae97-47dc53f45611]"

        def yes_no(name: str) -> str:
            return (
                f'<label><input type="radio" name="{name}" value="yes">YES</label>'
                f'<label><input type="radio" name="{name}" value="no">NO</label>'
            )

        def question(text: str, control: str) -> str:
            return (
                '<li class="application-question custom-question">'
                f'<div class="application-label">{esc(text)} \u2731</div>'
                f'<div class="application-field">{control}</div></li>'
            )

        body = (
            "<h1>Customer Success Manager</h1>"
            f'<p class="meta">{COMPANY} \u00b7 Customer \u00b7 Remote (US) \u00b7 Job ID BWA-CSM-115</p>'
            '<form method="post" action="/forms/unlabeled-custom-questions" '
            'aria-labelledby="form-title"><h2 id="form-title">Application</h2>'
            '<div class="field"><label for="f-full_name">Full name <span aria-hidden="true">*</span>'
            '</label><input type="text" id="f-full_name" name="name" required autocomplete="name"></div>'
            '<div class="field"><label for="f-email">Email <span aria-hidden="true">*</span></label>'
            '<input type="email" id="f-email" name="email" required autocomplete="email"></div>'
            '<ul class="custom-questions">'
            + question("How Did You Hear About Us?", f'<input type="text" name="{card}[field1]">')
            + question("What is your desired start date?", f'<input type="text" name="{card}[field2]">')
            + question(
                "Are you willing to relocate?",
                f'<select name="{card}[field3]"><option value="">Select...</option>'
                '<option value="yes">Yes</option><option value="no">No</option></select>',
            )
            # Yes/no radio groups the way some ATS vendors render them: no fieldset or
            # legend, each radio labelled only by its option text, the question in a
            # block before the group with a leading required marker, no required attribute.
            + '<li class="application-question">'
            '<div class="application-label">* Do you currently live in the United States?</div>'
            f'<div class="application-field">{yes_no("CA_9001")}</div></li>'
            + '</ul><div class="flat-questions">'
            '<p class="application-label">* Are you legally authorized to work in the United States?</p>'
            + yes_no("CA_9002")
            + '<p class="application-label">* Will you now or in the future require sponsorship?</p>'
            + yes_no("CA_9003")
            + '</div><button type="submit">Submit application</button></form>'
        )
        self._send_html(HTTPStatus.OK, page("Apply: Customer Success Manager", body))

    def get_choices_without_values(self) -> None:
        # Radio groups whose members share an opaque name and carry no value attribute
        # (the label is posted separately by page script); the question is a block
        # before the group. The second group's inputs have ids, the first's do not.
        years = "1b346bc6-2f2e-4c37-9d0f-3a1b2c4d5e6f_b888cd37-7b1a-4b6e-8f2a-9c0d1e2f3a4b"
        platform = "1b346bc6-2f2e-4c37-9d0f-3a1b2c4d5e6f_c9a1e0d2-3f4b-4c5d-8e6f-7a8b9c0d1e2f"

        def group(question: str, name: str, labels: list[tuple[str, str]]) -> str:
            radios = "".join(
                f'<label><input type="radio" name="{name}" value=""{f" id={esc(i)}" if i else ""}>'
                f"{esc(label)}</label>"
                for i, label in labels
            )
            return (
                f'<div class="choice-question"><div class="choice-label">{esc(question)}</div>'
                f'<div class="choice-options">{radios}</div></div>'
            )

        body = (
            "<h1>Marketing Operations Lead</h1>"
            f'<p class="meta">{COMPANY} \u00b7 Marketing \u00b7 Remote (US) \u00b7 Job ID BWA-MKT-116</p>'
            '<form method="post" action="/forms/choices-without-values" aria-labelledby="form-title">'
            '<h2 id="form-title">Application</h2>'
            + group("How many years of marketing experience do you have?", years,
                    [("", "Less than 3 years"), ("", "3\u20135 years"), ("", "6\u20138 years"),
                     ("", "9+ years")])
            + group("Which marketing automation platform have you used most?", platform,
                    [("opt-hubspot", "HubSpot"), ("opt-marketo", "Marketo"),
                     ("opt-pardot", "Pardot / Account Engagement"), ("opt-other", "Other"),
                     ("opt-none", "None")])
            + '<button type="submit">Submit application</button></form>'
        )
        self._send_html(HTTPStatus.OK, page("Apply: Marketing Operations Lead", body))

    def get_breezy_like(self) -> None:
        # A Breezy HR application as it renders live: no <label> anywhere. Each question is
        # an <h3> (with a "*" span) before its control, custom questions are named
        # section_<digits>_question_<n>, choice options sit in <ul class="options"> (the
        # checkboxes have neither labels nor values), an SMS consent checkbox states its
        # own text after the phone input, and the desired salary block has a currency
        # select, an input and an unnamed pay-period select under one heading.
        section = "section_1787064635874_question"

        def heading(text: str, required: bool = True) -> str:
            star = '<span title="Required" class="required">*</span>' if required else ""
            return f'<h3><span class="polygot">{esc(text)}</span>{star}</h3>'

        def question(text: str, control: str, kind: str = "") -> str:
            return f'<li class="question"><div class="{kind}">{heading(text)}{control}</div></li>'

        def options(name: str, labels: list[str], kind: str) -> str:
            if kind == "radio":
                items = "".join(
                    f'<li class="option"><label><input type="radio" value="{esc(o)}" name="{name}" '
                    f'required><span>{esc(o)}</span></label></li>' for o in labels)
            else:
                items = "".join(
                    f'<li class="option"><input type="checkbox" name="{name}"><span>{esc(o)}</span></li>'
                    for o in labels)
            return f'<ul class="options">{items}</ul>'

        divider = '<div class="form-divider"></div>'
        body = (
            "<h1>Director of Paid Media</h1>"
            f'<p class="meta">{COMPANY} · Marketing · Remote (US) · Job ID BWA-BZ-172</p>'
            '<form method="post" action="/forms/breezy-like" name="form" novalidate>'
            '<div class="section"><div class="section-header"><h2>Personal Details</h2></div>'
            + heading("Full Name") + '<input name="cName" type="text" placeholder="Full Name" required>'
            + divider + heading("Email Address")
            + '<input name="cEmail" type="email" placeholder="Email Address" required>'
            + divider + heading("Phone Number", required=False)
            + '<input name="cPhoneNumber" type="text" placeholder="Phone Number">'
            '<ul class="options"><li class="option consent-form"><input type="checkbox" name="smsConsent">'
            f"<span>By providing your phone number you agree to receive informational text messages "
            f"from {COMPANY}. Message &amp; data rates may apply, reply STOP to opt out at any time."
            "</span></li></ul>" + divider
            + '<div class="desired-salary">' + heading("Desired Salary")
            + '<span><select name="salaryCurrency"><option value="USD">US Dollar ($)</option>'
            '<option value="CAD">Canadian Dollar ($)</option><option value="EUR">Euro (€)</option>'
            '</select></span><input name="cSalary" type="text" placeholder="Desired Salary" required>'
            '<select><option value="hourly">Hourly</option><option value="weekly">Weekly</option>'
            '<option value="monthly">Monthly</option><option value="yearly" selected>Yearly</option>'
            "</select></div></div>"
            '<div class="section questions"><div class="section-header"><h2>Questions</h2></div><ul>'
            + question("How many years have you spent leading a team of media buyers?",
                       f'<input type="text" name="{section}_0" required>')
            + question("What is your target salary for this role?",
                       f'<input type="text" name="{section}_1" required>')
            + question("Briefly describe the largest paid media budget you have owned.",
                       f'<textarea name="{section}_2" required></textarea>')
            + question("Have you managed paid media for more than 100 client accounts at one time?",
                       options(f"{section}_3", ["Yes", "No"], "radio"), "multiplechoice")
            + question("Which ad platforms have you managed budgets on?",
                       options(f"{section}_4", ["Google Ads", "Meta", "LinkedIn", "TikTok"], "checkbox"),
                       "checkboxes")
            + "</ul></div>"
            '<div class="section questions"><p>Completing this survey is voluntary.</p><ul>'
            '<li class="question"><div class="multiplechoice"><h3 class="polygot">Race or Ethnicity</h3>'
            '<ul class="options">'
            + "".join(
                f'<li class="option"><input id="race_{v}" type="radio" name="race_ethnicity" value="{v}">'
                f'<label for="race_{v}"><span class="polygot">{esc(t)}</span></label></li>'
                for v, t in (("white", "White (not Hispanic or Latino)"),
                             ("black", "Black or African-American (not Hispanic or Latino)"),
                             ("hispanic", "Hispanic or Latino"), ("decline", "I don't wish to answer")))
            + "</ul></div></li></ul></div>"
            '<button type="submit">Submit Application</button></form>'
        )
        self._send_html(HTTPStatus.OK, page("Apply: Director of Paid Media", body))

    def get_ashby_like(self) -> None:
        # An Ashby application as it renders live: no <form> (page script posts it), each
        # question a field entry whose title is a <label for="<field path>">. Custom text
        # questions take that id; a date picker's input has no id or name, so its label
        # labels nothing, and it opens a react-datepicker calendar (a month listbox of day
        # options and two unnamed month buttons) inside its entry while focused. A radio
        # group shares one name; a checkbox group (and a second radio group) names each
        # option after its own text ("Yes", "No"); a yes/no question is two type="submit"
        # buttons with aria-pressed over a display:none checkbox. Placeholders only say
        # what to do ("Type here...", "Pick date...").
        entry = "ashby-application-form-field-entry"
        title = "ashby-application-form-question-title"
        referral, work, visa, relocate = (
            "7c1e2a90-5b3d-4f6e-8a1b-2c3d4e5f6a7b_25b7ff0a-0000-4000-8000-00000000a001",
            "0f9e8d7c-6b5a-4c3d-9e2f-1a0b9c8d7e6f",
            "cc031c31-0000-4000-8000-00000000a003",
            "c60ace77-0000-4000-8000-00000000a004",
        )

        def text(path: str, label: str, placeholder: str = "Type here...", *, required: bool = True) -> str:
            req = " required" if required else ""
            return (f'<div class="{entry}" data-field-path="{path}"><label class="{title}" for="{path}">'
                    f'{esc(label)}</label><input type="text" id="{path}" name="{path}" '
                    f'placeholder="{esc(placeholder)}"{req}></div>')

        def group(kind: str, path: str, question: str, opts: list[str], name: str | None) -> str:
            items = "".join(
                f'<div class="ashby-application-form-input-{kind}-group-option"><span>'
                f'<input type="{kind}" id="{path}-labeled-{kind}-{i}" name="{esc(name or o)}"></span>'
                f'<label for="{path}-labeled-{kind}-{i}">{esc(o)}</label></div>'
                for i, o in enumerate(opts))
            return (f'<div data-field-path="{path}"><fieldset class="{entry} '
                    f'ashby-application-form-input-{kind}-group"><label class="{title}" for="{path}">'
                    f"{esc(question)}</label>{items}</fieldset></div>")

        body = (
            "<h1>Performance Marketing Manager</h1>"
            f'<p class="meta">{COMPANY} · Marketing · Remote (US) · Job ID BWA-AS-173</p>'
            '<div id="form" class="ashby-application-form-container">'
            + text("_systemfield_name", "Name")
            + text("_systemfield_email", "Email", "hello@example.com...")
            # The location lookup: its title labels nothing (the input has no id), and its
            # suggestion list mounts in a portal of its own (ASHBY_LIKE_JS).
            + f'<div class="{entry}" data-field-path="_systemfield_location"><label class="{title}" '
            'for="_systemfield_location">Location</label><div class="ashby-autocomplete-container">'
            '<input class="ashby-application-form-input-autocomplete" placeholder="Start typing..." '
            'aria-autocomplete="list" aria-expanded="false" aria-haspopup="listbox" role="combobox" value="">'
            "</div></div>"
            + text("bad815aa-0000-4000-8000-00000000a005", "What is your expected salary?")
            + f'<div class="{entry}" data-field-path="52938440-0000-4000-8000-00000000a006">'
            f'<label class="{title}" for="52938440-0000-4000-8000-00000000a006">If you were to receive '
            "an offer, what is the earliest you could start?</label>"
            '<div class="react-datepicker-wrapper"><div class="react-datepicker__input-container">'
            '<input type="text" placeholder="Pick date..." class="ashby-application-form-input-date">'
            "</div></div></div>"
            + group("radio", referral, "How did you first hear about us?",
                    ["Referral (Friend or Colleague)", "Recruiter Outreach", "Job board", "LinkedIn"],
                    referral)
            + group("radio", work, "How would you like to work?", ["On-site", "Hybrid", "Remote"], None)
            + group("checkbox", visa, "Will you now or in the future require sponsorship?", ["Yes", "No"], None)
            # Required, as Ashby marks it: a class whose ::after draws the "*".
            + f'<div class="{entry}" data-field-path="{relocate}"><label class="{title} _required_f7cvd_91" '
            f'for="{relocate}">Are you willing to relocate to Denver?</label>'
            '<div class="ashby-application-form-input-yesno">'
            '<button type="submit" aria-pressed="false" data-option="yes">Yes</button>'
            '<button type="submit" aria-pressed="false" data-option="no">No</button>'
            f'<input type="checkbox" tabindex="-1" name="{relocate}" style="display:none"></div></div>'
            # A question after the yes/no: its write follows a pressed option (Sanity).
            + text("5249543b-0000-4000-8000-00000000a007", "LinkedIn Profile")
            + '<button type="button" class="ashby-application-form-submit-button">Submit Application</button>'
            "</div><script>" + ASHBY_LIKE_JS + "</script>"
        )
        self._send_html(HTTPStatus.OK, page("Apply: Performance Marketing Manager", body,
                                            '<style>._required_f7cvd_91::after { content: "*"; color: #b00; }</style>'))

    def get_apply_wording_posting(self) -> None:
        # The `standard` posting with one apply control of the requested wording and
        # kind: a link, a script-navigating button, or a submit button in a form with
        # no fillable field (a navigation form posting to /postings/go-apply).
        text = (self.query.get("text") or ["Apply now"])[0][:80]
        kind = (self.query.get("kind") or ["link"])[0]
        job = JOBS["standard"]
        if kind == "button":
            control = (
                f"<button type=\"button\" onclick=\"location.href='/jobs/{job.slug}/apply'\">"
                f"{esc(text)}</button>"
            )
        elif kind == "form":
            control = (
                '<form method="post" action="/postings/go-apply">'
                f'<input type="hidden" name="job" value="{job.slug}">'
                f'<button type="submit">{esc(text)}</button></form>'
            )
        else:
            control = f'<a class="button" href="/jobs/{job.slug}/apply">{esc(text)}</a>'
        body = (
            _job_heading(job)
            + f"<h2>About the role</h2><p>{COMPANY} builds forecasting tools for regional "
            "logistics networks.</p>"
            + f"<div>{control}</div>"
        )
        self._send_html(HTTPStatus.OK, page(job.title, body))

    def post_go_apply(self) -> None:
        form, _ = self._read_form()
        job = self._job((form.get("job") or ["standard"])[0])
        self._redirect(f"/jobs/{job.slug}/apply")

    def get_fixture_cities(self) -> None:
        # The site's own city suggestions (fetched by the lookup widgets' page script),
        # with a fixed 250 ms latency. Every query word must begin a word of the city;
        # US state names and abbreviations and "USA"/"United States" are equivalent.
        query = (self.query.get("q") or [""])[0]
        cities = CITIES_SHORT if (self.query.get("style") or [""])[0] == "short" else CITIES_LONG
        time.sleep(0.25)
        self._send_json(HTTPStatus.OK, [c for c in cities if city_matches(query, c)][:10])

    def get_captcha_widget(self) -> None:
        # The badge iframe of the fixture widget: a tiny static document.
        body = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>reCAPTCHA</title>'
            '</head><body style="margin:0;background:#f9f9f9;font:12px system-ui">'
            '<p style="margin:.5rem">Fixture badge</p></body></html>'
        )
        self._send(HTTPStatus.OK, body.encode("utf-8"), "text/html; charset=utf-8")

    def get_closed(self) -> None:
        # A job that is gone, worded the way some ATS vendors word it (HTTP 200).
        body = (
            "<h1>Careers</h1>"
            "<p>We're sorry, that job does not exist or is not currently active.</p>"
            '<p><a href="/">See all open roles</a></p>'
        )
        self._send_html(HTTPStatus.OK, page("Job not available", body))

    def get_closed_not_found(self) -> None:
        # A removed job worded as "not found" on an application URL, with HTTP 200 (Ashby).
        body = (
            "<h1>Job not found</h1>"
            "<p>The job you requested was not found.</p>"
            '<p><a href="/">View all open roles</a></p>'
        )
        self._send_html(HTTPStatus.OK, page("Jobs", body))

    # replicas of real application flows (fictional; see MOCK_ATS.md)
    def _render_easy_apply(self, job: Job, *, auto_open: bool) -> None:
        """A LinkedIn-style job view. Page script (EASY_APPLY_JS) builds the Easy Apply dialog
        from a JSON island: ``/apply`` opens it 300 ms after load, the ``trigger=button``
        control 400 ms after a click. Variants: ``trigger``, ``resumes``, ``shadow`` and ``import``."""
        resumes = self._param("resumes")
        if resumes not in EASY_APPLY_RESUMES:
            resumes = "match"
        variant = [(key, self._param(key)) for key in ("trigger", "resumes", "shadow", "import") if key in self.query]
        if self._param("trigger") == "button":
            # No type attribute and no form: only its click handler does anything.
            control = (
                '<button id="jobs-apply-button-id" class="jobs-apply-button artdeco-button artdeco-button--3 '
                f'artdeco-button--primary" aria-label="Easy Apply to {esc(job.title)} at {COMPANY}" '
                'data-live-test-job-apply-button><svg aria-hidden="true"></svg>'
                '<span class="artdeco-button__text">Easy Apply</span></button>'
            )
        else:
            href = f"/jobs/{job.slug}/apply?" + urlencode([("openSDUIApplyFlow", "true"), *variant])
            control = (
                f'<a aria-label="Easy Apply to this job" href="{esc(href)}"><span>'
                '<svg aria-hidden="true"></svg><span>Easy Apply</span></span></a>'
            )
        questions: dict[str, dict[str, Any]] = {
            key: {"id": f.dom_id, "label": f.label, "kind": f.kind, "options": [o.label for o in f.options],
                  "value": f.prefill}
            for key, f in EASY_APPLY_QUESTIONS.items()
        }
        questions["work_authorization"]["group"] = _ea_id("radio-button-form-component", 9005, "multipleChoice")
        config = {
            "autoOpen": auto_open,
            "shadow": self._param("shadow") == "1",
            "importOffer": self._param("import") == "1",
            "job": {"title": job.title, "code": job.code, "company": COMPANY},
            "profile": EASY_APPLY_PROFILE,
            "placeholder": EASY_APPLY_PLACEHOLDER,
            "questions": questions,
            "upload": {"id": EA_RESUME.dom_id, "accept": EA_RESUME.accept},
            "cards": [{"name": name, "meta": meta} for name, meta in EASY_APPLY_RESUMES[resumes]],
            "submitUrl": f"/jobs/{job.slug}/easy-apply?" + urlencode({"resumes": resumes}),
        }
        # Page-behind decoys: a search form, an "Easy Apply" search filter and two fillable
        # controls outside any form. A runtime must ignore them once the dialog is open.
        body = (
            '<div class="jobs-search-bar">'
            f'<form role="search" method="get" action="/jobs/{job.slug}">'
            '<label for="jobs-search-keywords" class="visually-hidden">Search jobs</label>'
            '<input type="search" id="jobs-search-keywords" name="keywords" aria-label="Search jobs">'
            '<button type="submit">Search</button></form>'
            '<div class="jobs-search-filters" role="group" aria-label="Search filters">'
            '<button type="button" aria-pressed="false" class="filter-pill">Easy Apply</button> '
            '<button type="button" aria-pressed="false" class="filter-pill">Remote</button></div></div>'
            + _job_heading(job)
            + f'<div class="jobs-apply-control">{control}</div>'
            + "<h2>About the job</h2>"
            f"<p>{COMPANY} is hiring a {esc(job.title)} to own acquisition and lifecycle programs for its "
            "forecasting products, from experiment design to reporting.</p>"
            '<section class="jobs-hiring-team"><h2>Meet the hiring team</h2>'
            f"<p>The {COMPANY} growth team is hiring for this role.</p>"
            '<textarea rows="3" aria-label="Write a message to the hiring team"></textarea></section>'
            '<section class="jobs-notes"><h2>My notes</h2>'
            '<input type="text" aria-label="Add a note about this job"></section>'
        )
        head = (
            self._posting_head(job)
            + f"<style>{EASY_APPLY_PAGE_STYLE}</style>"
            + f'<style id="easy-apply-dialog-style">{EASY_APPLY_DIALOG_STYLE}</style>'
        )
        after = _json_island("easy-apply-config", config) + f"<script>{EASY_APPLY_JS}</script>"
        self._send_html(HTTPStatus.OK, page(job.title, body, head, after_main=after))

    def post_easy_apply(self) -> None:
        # The dialog's one request (multipart, by fetch). The resume is an uploaded file
        # ("resume") or the name of an offered saved card ("resume_choice"); the cards
        # offered are those of the ?resumes= variant in the request URL. Answers JSON.
        job = self._job(MODAL_WIZARD)
        form, uploads = self._read_form()
        offered = dict(EASY_APPLY_RESUMES.get(self._param("resumes"), EASY_APPLY_RESUMES["match"]))
        attached = [u for u in uploads.get(EA_RESUME.name, []) if u.filename]
        choice = (form.get("resume_choice") or [""])[0]
        retained: dict[str, dict[str, Any]] = {}
        choice_error = None
        if not attached and choice in offered:
            retained[EA_RESUME.name] = {"source": "saved_resume", "filename": choice, "details": offered[choice]}
        elif not attached and choice:
            choice_error = "Select one of your saved resumes or upload a resume."
        fields = self._fields(job)
        values, files, errors = validate(fields, form, uploads, retained)
        if choice_error:
            errors[EA_RESUME.name] = choice_error
        elif attached and EA_RESUME.name not in errors:
            upload = attached[0]
            if not upload.filename.lower().endswith(EASY_APPLY_EXTENSIONS):
                errors[EA_RESUME.name] = "Upload a DOC, DOCX or PDF file."
            elif len(upload.data) > EASY_APPLY_MAX_UPLOAD:
                errors[EA_RESUME.name] = "The file must be 2 MB or smaller."
        sql_years = values.get(EA_SQL_YEARS.name)
        if sql_years is not None and not SQL_YEARS_RE.match(sql_years):
            errors[EA_SQL_YEARS.name] = SQL_YEARS_MESSAGE
        if errors:
            self.store.add_rejection(job, errors)
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"accepted": False, "errors": errors})
            return
        extra = {k: v for k, v in _extra_fields(fields, form).items() if k != "resume_choice"}
        record = self.store.add_submission(job, values, extra, self._store_files(files))
        self._send_json(HTTPStatus.OK, {
            "accepted": True, "submission_id": record["submission_id"],
            "confirmation_reference": record["confirmation_reference"],
            "job_id": job.slug, "job_code": job.code,
        })

    def _render_careers_embed(self, job: Job) -> None:
        """An employer careers page (careers.<employer>?gh_jid=… style): Role overview and
        Application tabs; page script injects the Greenhouse-style iframe 300 ms after load."""
        visible = self._param("panel") == "visible"
        query = [("for", EMBED_BOARD), ("token", EMBED_TOKEN)]
        if self._param("embedded_only") == "1":
            query.append(("embedded_only", "1"))
        hidden = ' style="display:none"'

        def tab(dom_id: str, panel: str, text: str, selected: bool) -> str:
            return (
                f'<button id="{dom_id}" role="tab" aria-controls="{panel}" '
                f'aria-selected="{"true" if selected else "false"}" tabindex="{0 if selected else -1}">'
                f"{text}</button>"
            )

        body = (
            f'<form role="search" method="get" action="/jobs/{job.slug}" hidden>'
            '<label for="careers-search">Search open roles</label>'
            '<input type="search" id="careers-search" name="q"></form>'
            + _job_heading(job)
            + '<div id="main-content" role="tablist" aria-label="Job details">'
            + tab("tab-overview", "job-detail-panel", "Role overview", not visible)
            + tab("tab-application", "job-application-panel", "Application", visible)
            + "</div>"
            f'<div id="job-detail-panel" role="tabpanel" aria-labelledby="tab-overview"{hidden if visible else ""}>'
            f"<h2>About the role</h2><p>{COMPANY} is looking for a {esc(job.title)} to build and grow "
            "partnerships with logistics platforms and data providers.</p>"
            '<button class="apply-btn" aria-label="Switch to application form">Apply Now</button></div>'
            '<div id="job-application-panel" role="tabpanel" aria-labelledby="tab-application"'
            f'{"" if visible else hidden}><div id="grnhse_app"></div></div>'
        )
        after = (_json_island("careers-config", {"src": "/embed/job_app?" + urlencode(query)})
                 + f"<script>{CAREERS_EMBED_JS}</script>")
        self._send_html(HTTPStatus.OK, page(job.title, body, self._posting_head(job), after_main=after))

    def get_embed_job_app(self) -> None:
        # The embedded job application (boards.greenhouse.io/embed/job_app style): exactly the
        # iframe-embed form page. With embedded_only=1 it is served only into an iframe.
        job = self._job(IFRAME_EMBED)
        if self._param("for") != EMBED_BOARD or self._param("token") != EMBED_TOKEN:
            raise HttpError(HTTPStatus.NOT_FOUND, "No such job application.")
        if self._param("embedded_only") == "1" and self.headers.get("Sec-Fetch-Dest") != "iframe":
            body = (
                "<h1>Application unavailable</h1>"
                f"<p>This application form can only be shown on the {COMPANY} careers page.</p>"
            )
            self._send_html(HTTPStatus.FORBIDDEN, page("Application unavailable", body))
            return
        self._render_single(job, {}, {}, {}, None, HTTPStatus.OK)

    def _render_jazzhr(
        self,
        job: Job,
        values: dict[str, list[str]],
        errors: dict[str, str],
        retained: dict[str, dict[str, Any]],
        status: HTTPStatus,
    ) -> None:
        """A JazzHR-style application page whose only action controls are anchors. The
        ``?sections=2`` variant splits the form into two client-side sections."""
        sections = self._param("sections") == "2"
        action = f"/jobs/{job.slug}/apply" + ("?sections=2" if sections else "")
        hidden = ' style="display:none"'
        marker = ' <span class="required" aria-hidden="true">*</span>'

        def feedback(dom: str, name: str) -> tuple[str, str, str]:
            """(group class suffix, control attributes, message) for a field's error."""
            error = errors.get(name)
            if not error:
                return "", "", ""
            note = f'<span class="help-block" id="{dom}-error" role="alert">{esc(error)}</span>'
            return " has-error", f' aria-invalid="true" aria-describedby="{dom}-error"', note

        def group(f: Field) -> str:
            dom = f.dom_id or f.name
            current = (values.get(f.name) or [""])[0]
            css, aria, note = feedback(dom, f.name)
            aria = (' aria-required="true"' if f.required else "") + aria
            if f.kind == "select":
                options = '<option value="">- Select One -</option>' + "".join(
                    f'<option value="{esc(o.value)}"{" selected" if o.value == current else ""}>'
                    f"{esc(o.label)}</option>"
                    for o in f.options
                )
                control = f'<select class="form-control" id="{dom}" name="{f.name}"{aria}>{options}</select>'
            else:
                auto = f' autocomplete="{f.autocomplete}"' if f.autocomplete else ""
                control = (
                    f'<input type="{f.kind}" class="form-control" id="{dom}" name="{f.name}" '
                    f'value="{esc(current)}"{auto}{aria}>'
                )
            return (
                f'<div class="form-group{css}" id="{dom.removesuffix("-value")}">'
                f'<label class="control-label" for="{dom}">{esc(f.label)}{marker if f.required else ""}</label>'
                f"{control}{note}</div>"
            )

        prior = retained.get(JZ_RESUME.name)
        pasted = (values.get(JZ_RESUME_TEXT.name) or [""])[0]
        css, aria, note = feedback(JZ_RESUME.dom_id or "", JZ_RESUME.name)
        attached = kept = ""
        if prior:
            attached = f"Currently attached: {esc(prior['filename'])} ({prior['size']} bytes)."
            kept = f'<input type="hidden" name="resume_upload_id" value="{esc(prior["upload_id"])}">'
        resume = (
            f'<div class="form-group{css}" id="resumator-resume">'
            f'<label class="control-label" for="resumator-resume-file">Resume{marker}</label>'
            '<div class="resumator-resume-choices"><a id="resumator-choose-upload" href="#">Attach resume</a> '
            'or <a id="resumator-choose-paste" href="#">Paste resume</a></div>'
            f'<p class="help-block" id="resumator-resume-filename">{attached}</p>{kept}'
            '<input type="file" id="resumator-resume-file" name="resume" accept=".pdf,.doc,.docx" '
            f'aria-label="Resume" aria-required="true" style="display:none"{aria}>'
            f'<div id="resumator-resume-paste"{"" if pasted else hidden}>'
            '<label class="control-label" for="resumator-resume-value">Paste resume</label>'
            '<textarea class="form-control" id="resumator-resume-value" name="resume_text" rows="8">'
            f"{esc(pasted)}</textarea></div>{note}</div>"
        )
        contact_fields = (JZ_FIRST_NAME, JZ_LAST_NAME, JZ_EMAIL, JZ_PHONE)
        contact = "".join(group(f) for f in contact_fields)
        questions = group(JZ_SALARY) + group(JZ_HEARD)
        submit = (
            '<div id="resumator-submit" class="form-group">'
            '<a id="resumator-submit-resume" class="btn btn-primary" href="#">Submit Application</a></div>'
        )
        if sections:
            # A rejected POST reopens the section holding the first error.
            second = bool(errors) and not any(f.name in errors for f in contact_fields)
            fields_html = (
                '<div class="job-form-fields">'
                f'<div class="resumator-form-section" id="resumator-section-1"{hidden if second else ""}>'
                "<h3>Contact information</h3>" + contact
                + '<div class="form-group"><a class="btn" href="#">Next</a> <a class="btn" href="#">Save</a></div>'
                '<p class="help-block" id="resumator-save-status" role="status"></p></div>'
                f'<div class="resumator-form-section" id="resumator-section-2"{"" if second else hidden}>'
                "<h3>Resume and questions</h3>" + resume + questions
                + '<div class="form-group"><a class="btn" href="#">Back</a></div>' + submit + "</div></div>"
            )
        else:
            fields_html = f'<div class="job-form-fields">{contact}{questions}{resume}</div>{submit}'
        entries = [(f.dom_id or f.name, f.label, errors[f.name]) for f in job.fields if f.name in errors]
        body = (
            '<div id="resumator-cookie-consent" class="cookie-consent-bar" role="region" aria-label="Cookie consent">'
            "<p>This website uses cookies to ensure you get the best experience on our website.</p>"
            "<button>Dismiss</button> <button>ALLOW</button> <button>REJECT ALL</button></div>"
            '<div class="job-header"><div>' + _job_heading(job) + "</div>"
            '<a role="button" href="#" class="share">Share</a></div>'
            '<button id="resumator-mobile-apply-button" type="button" style="display:none">Apply</button>'
            f"<h2>Description</h2><p>{COMPANY} is hiring a {esc(job.title)} to plan and run paid search and "
            "paid social programs across its product lines.</p>"
            + render_error_summary(entries)
            + '<div id="resumator-job-form"><h2>Apply for this position</h2>'
            f'<form id="form_submit_new_resume" method="post" action="{esc(action)}" enctype="multipart/form-data">'
            + "".join(f'<input type="hidden" name="{name}" value="{esc(value)}">' for name, value in JAZZHR_HIDDEN)
            + fields_html
            + "</form></div>"
            + f"<script>{JAZZHR_JS}</script>"
        )
        title = f"Apply: {job.title}" if not errors else f"Error: Apply: {job.title}"
        self._send_html(status, page(title, body, f"<style>{JAZZHR_STYLE}</style>"))

    def _alert_routes(self, job: Job) -> dict[str, Any]:
        """Configuration of DAYFORCE_JS: its client-side routes and their content."""
        title, form_body, _ = self._single_form(job, {}, {}, {}, None, HTTPStatus.OK)
        flow = f"/jobs/{job.slug}/apply?flowSelection=true"
        return {
            "delayMs": 2500,
            "signIn": f"/login?next={quote(flow)}",
            "routes": {
                "flow": {"url": flow, "title": f"How would you like to apply? | {COMPANY} Careers",
                         "html": _flow_selection_html(job)},
                "manual": {"url": f"/jobs/{job.slug}/apply/manual", "title": f"{title} | {COMPANY} Careers",
                           "html": form_body},
            },
        }

    def _render_alert_posting(
        self, job: Job, *, notice: str = "", error: str = "", email: str = "",
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        """The Dayforce-style posting. Legacy portal (default): the whole content is one
        ASP.NET-style form in which Apply posts to ``/start`` and Subscribe (a ``formaction``)
        to ``/alerts``. ``?nav=spa`` (current portal): no forms; Apply routes on the client."""
        head = self._posting_head(job) + f"<style>{DAYFORCE_STYLE}</style>"
        description = (
            f"<h2>Job description</h2><p>{COMPANY} is hiring a {esc(job.title)} to plan and run "
            "multi-channel marketing projects, from briefs and budgets to launch reviews.</p>"
        )
        apply_label = f"Apply for {esc(job.title)}"
        if self._param("nav") == "spa":
            body = (
                _job_heading(job)
                + '<div class="job-actions">'
                '<button type="button" class="ant-btn ant-btn-primary" test-id="apply-button" '
                f'aria-label="{apply_label}">Apply</button>'
                '<button type="button" class="ant-btn ant-btn-default">Share</button></div>'
                + description
            )
            after = _json_island("dayforce-config", self._alert_routes(job)) + f"<script>{DAYFORCE_JS}</script>"
            self._send_html(status, page(job.title, body, head, after_main=after))
            return
        message, invalid = "", ""
        if notice:
            message = f'<p class="notice" role="status">{esc(notice)}</p>'
        if error:
            message = f'<p class="error" id="alert-email-error" role="alert">{esc(error)}</p>'
            invalid = ' aria-invalid="true" aria-describedby="alert-email-error"'
        body = (
            f'<form id="aspnetForm" method="post" action="/jobs/{job.slug}/start">'
            '<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="/wEPDwUKMTEzMzAxNjU0N2Rk">'
            '<input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="/wEdAAKBWADF133">'
            + _job_heading(job)
            + f'<div class="job-actions"><button type="submit" name="apply" value="1" aria-label="{apply_label}">'
            "Apply</button></div>"
            + description
            + '<div class="job-alerts" role="region" aria-labelledby="job-alerts-title">'
            '<h2 id="job-alerts-title">Get job alerts</h2>'
            "<p>Get an email when new jobs like this one are posted.</p>"
            + message
            + '<div class="field"><label for="alert-email">Email address for job alerts</label>'
            f'<input type="email" id="alert-email" name="alert_email" value="{esc(email)}"{invalid}></div>'
            f'<button type="submit" formaction="/jobs/{job.slug}/alerts" name="subscribe" value="1">'
            "Subscribe</button></div></form>"
        )
        self._send_html(status, page(job.title, body, head))

    def _render_flow_selection(self, job: Job) -> None:
        after = _json_island("dayforce-config", self._alert_routes(job)) + f"<script>{DAYFORCE_JS}</script>"
        head = f"<style>{DAYFORCE_STYLE}</style>"
        self._send_html(
            HTTPStatus.OK, page("How would you like to apply?", _flow_selection_html(job), head, after_main=after)
        )

    def post_alert_start(self) -> None:
        # The legacy portal's form target: Apply (and Enter in the alert email) posts here.
        # A non-empty alert email is a job-alert subscription, never part of an application.
        job = self._job(APPLY_IN_ALERT_FORM)
        form, _ = self._read_form()
        email = (form.get("alert_email") or [""])[0].strip()
        if email:
            self.store.add_alert(job, email)
        self._redirect(f"/jobs/{job.slug}/apply?flowSelection=true")

    def post_alert_subscribe(self) -> None:
        job = self._job(APPLY_IN_ALERT_FORM)
        form, _ = self._read_form()
        email = (form.get("alert_email") or [""])[0].strip()
        if not email:
            self._render_alert_posting(
                job, error="Enter an email address for job alerts.", status=HTTPStatus.UNPROCESSABLE_ENTITY
            )
            return
        self.store.add_alert(job, email)
        self._render_alert_posting(job, notice=f"You are subscribed to job alerts at {email}.", email=email)

    def get_manual_apply(self) -> None:
        # "Apply without an Account": the application form of apply-in-alert-form.
        job = self._job(APPLY_IN_ALERT_FORM)
        self._render_single(job, {}, {}, {}, None, HTTPStatus.OK)

    # test-only API: assertions and fixture control, never product runtime
    def test_health(self) -> None:
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "origin": self.app.origin, "state_dir": str(self.store.state_dir)},
        )

    def test_jobs(self) -> None:
        self._send_json(
            HTTPStatus.OK,
            {
                "company": COMPANY,
                "signin": {"email": SIGNIN_EMAIL, "password": SIGNIN_PASSWORD},
                "jobs": [job.describe() for job in JOBS.values()],
            },
        )

    def test_workday(self) -> None:
        self._send_json(HTTPStatus.OK, self.store.wd_summary())

    def test_submissions(self) -> None:
        job_id = (self.query.get("job_id") or [None])[0]
        self._send_json(HTTPStatus.OK, self.store.summary(job_id))

    def test_submission(self, submission_id: str) -> None:
        record = self.store.get_submission(submission_id)
        if record is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "no such submission")
        self._send_json(HTTPStatus.OK, record)

    def test_reveal(self, submission_id: str) -> None:
        self._read_body()
        record = self.store.reveal(submission_id)
        if record is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "no such submission")
        self._send_json(HTTPStatus.OK, record)

    def test_captcha(self, token: str) -> None:
        challenge = self.store.captcha(token)
        if challenge is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "no such captcha")
        self._send_json(HTTPStatus.OK, {"token": token, **challenge})

    def test_reset(self) -> None:
        self._read_body()
        self.store.reset()
        self._send_json(HTTPStatus.OK, {"reset": True})

    def test_shutdown(self) -> None:
        self._read_body()
        self._send_json(HTTPStatus.OK, {"stopping": True})
        threading.Thread(target=self.server.shutdown, daemon=True).start()


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------


def _check_loopback(host: str) -> None:
    if host == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(f"host must be localhost or an IPv4 loopback address, got {host!r}") from None
    if address.version != 4 or not address.is_loopback:
        raise ValueError(f"host must be localhost or an IPv4 loopback address, got {host!r}")


class MockATS:
    """In-process handle: ``MockATS(port=0, state_dir=...).start()`` then ``.origin``."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        state_dir: str | Path | None = None,
        verbose: bool = False,
    ):
        _check_loopback(host)
        self.state_dir = Path(state_dir or tempfile.mkdtemp(prefix="mock-ats-")).resolve()
        self.store = Store(self.state_dir)
        self.verbose = verbose
        self.httpd = _Server((host, port), self)
        self.port: int = self.httpd.server_address[1]
        self.origin = f"http://{host}:{self.port}"
        self._thread: threading.Thread | None = None

    def serve_forever(self) -> None:
        self.httpd.serve_forever(poll_interval=0.1)

    def start(self) -> MockATS:
        self._thread = threading.Thread(target=self.serve_forever, name="mock-ats", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is not None:
            self.httpd.shutdown()
            self._thread.join()
            self._thread = None
        self.httpd.server_close()

    def __enter__(self) -> MockATS:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic localhost mock ATS (fictional Brambleway Analytics)."
    )
    parser.add_argument("--host", default="127.0.0.1", help="loopback host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0, help="port; 0 picks a free one (default)")
    parser.add_argument(
        "--state-dir", help="directory for state.json and uploads (default: new temp directory)"
    )
    parser.add_argument(
        "--ready-file", help="write {origin, state_dir, pid} JSON here once listening"
    )
    parser.add_argument("--verbose", action="store_true", help="log requests to stderr")
    args = parser.parse_args(argv)

    try:
        ats = MockATS(args.host, args.port, args.state_dir, args.verbose)
    except (ValueError, OSError) as exc:
        print(f"mock_ats: {exc}", file=sys.stderr)
        return 2

    def request_stop(signum: int, frame: object) -> None:
        threading.Thread(target=ats.httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    ready_file = Path(args.ready_file).resolve() if args.ready_file else None
    if ready_file:
        tmp = ready_file.with_name(ready_file.name + ".tmp")
        tmp.write_text(
            json.dumps({"origin": ats.origin, "state_dir": str(ats.state_dir), "pid": os.getpid()}),
            "utf-8",
        )
        os.replace(tmp, ready_file)
    print(f"MOCK_ATS_ORIGIN={ats.origin}", flush=True)
    print(f"MOCK_ATS_STATE_DIR={ats.state_dir}", flush=True)
    print(f"MOCK_ATS_PID={os.getpid()}", flush=True)
    try:
        ats.serve_forever()
    finally:
        ats.httpd.server_close()
        if ready_file:
            ready_file.unlink(missing_ok=True)
    print("MOCK_ATS_STOPPED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
