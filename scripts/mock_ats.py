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
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from email.utils import collapse_rfc2231_value
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

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
    # remote_lookup (role-less lookup input), rippling_phone and intl_tel.
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
    """Element id of a script-driven widget (``question_6001``, ``field-3``)."""
    display: str | None = None
    """``dial``: a react_select shows only the dial code of the chosen label."""
    open_on: str | None = None
    """``click`` (default), ``keyboard`` (focus + ArrowDown only) or ``focus``."""
    remote: str | None = None
    """Suggestion URL prefix of a lookup (the query is appended)."""
    prefill: str | None = None
    """Initial value of a search combobox (a chosen value, like "+1 US")."""
    idle: str | None = None
    """Notice a search combobox shows when opened before anything is typed."""
    show_all: bool = False
    """A search combobox that lists every option when opened (a static menu)."""
    embedded: bool = False
    """Rendered inside another widget's block (a phone's country code)."""

    @property
    def multi(self) -> bool:
        return self.kind in ("checkbox_group", "multiselect", "react_multi")

    @property
    def widget(self) -> bool:
        return self.kind in WIDGET_KINDS

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
        }


WIDGET_KINDS = frozenset({
    "react_select", "react_multi", "react_async", "div_combobox", "search_combobox",
    "remote_lookup", "rippling_phone", "intl_tel",
})


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
GH_LOCATION = Field("candidate_location", "Location (City)", "react_async", True, _labels(*CITIES_LONG),
                    dom_id="candidate-location", remote="/__fixture__/cities?style=long&q=")
RP_LOCATION = Field("location_short", "Location", "remote_lookup", options=_labels(*CITIES_SHORT),
                    dom_id="field-42", remote="/__fixture__/cities?style=short&q=")
RP_STATE = Field("state", "What state do you live in?", "search_combobox", True,
                 _labels(*US_STATE_NAMES), dom_id="field-43", idle="Start typing to search")

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
    cookie_banner: bool = False
    """A modal cookie-consent dialog covers the page (main is inert) until dismissed."""

    @property
    def multistep(self) -> bool:
        return len(self.steps) > 1

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
            "cookie_banner": self.cookie_banner,
            "multistep": self.multistep,
            "steps": [
                {"title": s.title, "fields": [f.describe() for f in s.fields]}
                for s in self.steps
            ],
        }


def _single(*fields: Field) -> tuple[Step, ...]:
    return (Step("Application", fields),)


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
    )
}


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
    """JSON-file state: submissions, rejections, uploads, drafts, captchas, sessions.

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
            return {
                "job_id": job_id,
                "accepted_count": len(subs),
                "rejected_count": len(rejs),
                "submissions": subs,
                "rejections": rejs,
            }

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
                  "search_combobox", "react_async", "remote_lookup"):
        return "Select an answer."
    if f.kind == "checkbox":
        return "Check this box to continue."
    if f.multi:
        return "Select at least one option."
    if f.kind == "file":
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
        if f.disabled:
            continue  # browsers never submit disabled controls
        if f.kind == "file":
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


def _extra_fields(job: Job, form: dict[str, list[str]]) -> dict[str, Any]:
    declared = {f.name for f in job.fields} | INTERNAL_FIELDS
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
.rip-option--active{background:#deebff}
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
      if (cfg.display === "dial") { var m = label.match(/\+\d+$/); return m ? m[0] : label; }
      return label;
    };
    var hasValue = function () { return multi ? st.value.length > 0 : st.value !== null; };
    function renderValue() {
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
        values.insertBefore(el("div", {"class": "select__single-value"}, displayOf(st.value)), inputBox);
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
        return !text || norm(o[1]).indexOf(text) >= 0;
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
        "border-radius:4px;box-shadow:0 4px 12px rgba(0,0,0,.15);left:" + (r.left + window.scrollX) +
        "px;top:" + (r.bottom + window.scrollY + 2) + "px;width:" + r.width + "px";
      var list = el("div", {"class": "select__menu-list", role: "listbox", id: "react-select-" + id + "-listbox",
        "aria-multiselectable": String(multi)});
      list.style.cssText = "max-height:300px;overflow-y:auto;padding:4px 0";
      menu.appendChild(list);
      // Keep the focus in the input while the pointer is on the menu, like react-select.
      menu.addEventListener("mousedown", function (e) { e.preventDefault(); });
      document.body.appendChild(menu);
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

  // Rippling-style div combobox.
  function divCombobox(box) {
    var cfg = config(box);
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
    function ensureList() {
      if (list) return list;
      list = el("ul", {id: id + "-list", role: "listbox", "class": "rip-list"});
      list.addEventListener("mousedown", function (e) { e.preventDefault(); });
      input.parentNode.appendChild(list);
      input.setAttribute("aria-controls", id + "-list");
      input.setAttribute("aria-expanded", "true");
      return list;
    }
    function hide() {
      if (list) { list.remove(); list = null; }
      input.removeAttribute("aria-controls");
      input.setAttribute("aria-expanded", "false");
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
        if (text.length < 2) { hide(); return; }
        timer = setTimeout(function () {
          fetch(cfg.remote + encodeURIComponent(text)).then(function (r) { return r.json(); })
            .then(function (labels) { if (mine === seq) render(labels, ""); });
        }, 150);
        return;
      }
      render(matching(text), "No results");
    }
    input.addEventListener("input", lookup);
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
    input.addEventListener("blur", function () { hide(); });
  }

  // intl-tel-input-like phone widget.
  function intlTel(root) {
    var cfg = config(root);
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

  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-mount]"), function (holder) {
    holder.innerHTML = JSON.parse(holder.getAttribute("data-widget-mount")).html;
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-kind=react-select]"), reactSelect);
  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-kind=div-combobox]"), divCombobox);
  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-kind=search-combobox]"), searchCombobox);
  Array.prototype.forEach.call(document.querySelectorAll("[data-widget-kind=intl-tel]"), intlTel);
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

    if f.kind in ("react_select", "react_multi", "react_async"):
        multi = f.kind == "react_multi"
        config: dict[str, Any] = {
            "id": dom, "name": f.name, "multi": multi, "display": f.display,
            "openOnFocus": f.open_on == "focus", "async": f.remote,
            "options": [] if f.remote else options,
            "initial": [v for v in posted if v] if multi else current,
        }
        if f.remote and current:
            config["options"] = [[current, current]]
        container = "select__value-container" + (" select__value-container--is-multi" if multi else "")
        return (
            f'<div class="field"><label id="{esc(dom)}-label" for="{esc(dom)}" class="label select__label">'
            f"{esc(f.label)}{marker}</label>{err}"
            f'<div class="select-shell"{_widget_attrs("react-select", config)}>'
            f'<div class="select__control"><div class="{container}">'
            '<div class="select__input-container">'
            f'<input class="select__input" autocapitalize="none" autocomplete="off" autocorrect="off" '
            f'id="{esc(dom)}" spellcheck="false" tabindex="0" type="text" aria-autocomplete="list" '
            f'aria-expanded="false" aria-haspopup="true" aria-labelledby="{esc(dom)}-label"{required} '
            'role="combobox" value=""></div></div>'
            '<div class="select__indicators" aria-hidden="true"><span class="select__indicator-separator">'
            "</span><div class=\"select__indicator\">▾</div></div></div></div></div>"
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
               "remote": field.remote,
               "aliases": ({name: abbr for name, abbr in STATE_ABBREVIATIONS.items()}
                           if field is RP_STATE else {})}
        role = "" if field.kind == "remote_lookup" else ' role="combobox"'
        testid = "" if field.kind == "remote_lookup" else ' data-testid="input-select-search-input"'
        req = ' aria-required="true"' if field.required else ""
        return (
            f'<input id="{esc(field.dom_id or field.name)}" type="text"{role} aria-haspopup="listbox" '
            f'aria-autocomplete="list" aria-expanded="false"{testid} autocomplete="off"{req} '
            f'value="{esc(value or "")}"{_widget_attrs("search-combobox", cfg)}>'
        )

    if f.kind in ("search_combobox", "remote_lookup"):
        value = current if current is not None else f.prefill
        if f.kind == "remote_lookup":
            return (f'<div class="field"><label for="{esc(dom)}">{esc(f.label)}{marker}</label>{err}'
                    f'<div class="rip-input">{search_input(f, value)}</div></div>')
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


def render_field(
    f: Field,
    values: dict[str, list[str]],
    error: str | None,
    retained: dict[str, Any] | None = None,
) -> str:
    if f.widget:
        return render_widget(f, values, error)
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
        control = (
            f'<input type="{f.kind}" id="{fid}" name="{f.name}" value="{esc(current)}"'
            f"{auto}{required}{aria}>"
        )
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
        return f'<div class="field">{label}{hint}{err}{control}</div>'

    if f.kind in ("radio", "checkbox_group"):
        input_type = "radio" if f.kind == "radio" else "checkbox"
        legend_marker = marker
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
        return (
            f'<fieldset class="field" id="{fid}"{role}{req}{group_aria}>'
            f"<legend>{esc(f.label)}{legend_marker}</legend>{hint}{err}"
            + "".join(choices)
            + "</fieldset>"
        )

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
    return [(f"f-{f.name}", f.label, errors[f.name]) for f in fields if f.name in errors]


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
        (r"/applications/(?P<submission_id>sub_\d{6})", "GET", "get_confirmation"),
        (r"/login", "GET", "get_login"),
        (r"/login", "POST", "post_login"),
        (r"/captcha/(?P<token>cap_\d{6})\.svg", "GET", "get_captcha_svg"),
        (r"/captcha/widget\.html", "GET", "get_captcha_widget"),
        (r"/closed", "GET", "get_closed"),
        (r"/postings/with-select", "GET", "get_posting_with_select"),
        (r"/forms/unlabeled-custom-questions", "GET", "get_unlabeled_custom_questions"),
        (r"/forms/choices-without-values", "GET", "get_choices_without_values"),
        (r"/postings/apply-wording", "GET", "get_apply_wording_posting"),
        (r"/postings/go-apply", "POST", "post_go_apply"),
        (r"/__fixture__/cities", "GET", "get_fixture_cities"),
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

    def get_job(self, slug: str) -> None:
        job = self._job(slug)
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
        head = (
            f'<link rel="canonical" href="{esc(self.app.origin)}/jobs/{job.slug}">'
            '<script type="application/ld+json">'
            + json.dumps(ld).replace("</", "<\\/")
            + "</script>"
        )
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

    def get_apply(self, slug: str) -> None:
        job = self._job(slug)
        if job.requires_signin and not self._signed_in():
            self._redirect_to_login(job)
            return
        if job.multistep:
            self._render_step(job, None, 1, {}, {}, None, HTTPStatus.OK)
        else:
            self._render_single(job, {}, {}, {}, None, HTTPStatus.OK)

    def post_apply(self, slug: str) -> None:
        job = self._job(slug)
        if job.requires_signin and not self._signed_in():
            self._read_body()
            self._redirect_to_login(job)
            return
        form, uploads = self._read_form()
        if job.multistep:
            self._post_step(job, None, 1, form, uploads)
            return

        retained: dict[str, dict[str, Any]] = {}
        prior = self.store.get_upload((form.get("resume_upload_id") or [""])[0])
        if prior:
            retained["resume"] = prior
        values, files, errors = validate(
            job.fields, form, uploads, retained, strict_phone=job.strict_phone
        )
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
            self._render_single(
                job, form, errors, files_meta, captcha_error, HTTPStatus.UNPROCESSABLE_ENTITY
            )
            return

        record = self.store.add_submission(job, values, _extra_fields(job, form), files_meta)
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
        entries = _summary_entries(job.fields, errors)
        if captcha_error:
            entries.append(("f-captcha_answer", "Characters shown in the image", captcha_error))
        if errors.get(CAPTCHA_WIDGET_FIELD):
            entries.append((CAPTCHA_WIDGET_FIELD, "CAPTCHA", errors[CAPTCHA_WIDGET_FIELD]))
        fields_html = "".join(
            render_field(f, values, errors.get(f.name), retained.get(f.name)) for f in job.fields
        )
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
        form_html = (
            f'<form method="post" action="/jobs/{job.slug}/apply" enctype="multipart/form-data" '
            'aria-labelledby="form-title"><h2 id="form-title">Application form</h2>'
            '<p class="hint">Fields marked with * are required.</p>'
            + fields_html
            + '<button type="submit">Submit application</button></form>'
        )
        widgets = any(f.widget for f in job.fields)
        if widgets:
            form_html += f"<script>{WIDGETS_JS}</script>"
        if job.spa_loading and status == HTTPStatus.OK:
            form_html = render_delayed(form_html)
        body = _job_heading(job) + render_error_summary(entries) + form_html
        main_attrs, after_main = "", ""
        if job.cookie_banner and not self._consented():
            main_attrs, after_main = ' inert aria-hidden="true"', render_cookie_banner()
        title = f"Apply: {job.title}" if not errors else f"Error: Apply: {job.title}"
        head = f"<style>{WIDGET_STYLE}</style>" if widgets else ""
        self._send_html(status, page(title, body, head, main_attrs=main_attrs, after_main=after_main))

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
        step_fields = job.steps[n - 1].fields
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
        has_file = any(f.kind == "file" for f in step.fields)
        enctype = "multipart/form-data" if has_file else "application/x-www-form-urlencoded"
        back = ""
        if n > 1 and draft is not None:
            back = f' <a href="/jobs/{job.slug}/apply/{draft["draft_id"]}/step/{n - 1}">Back</a>'
        body = (
            _job_heading(job)
            + self._progress(job, n)
            + render_error_summary(_summary_entries(step.fields, errors))
            + f'<form method="post" action="{action}" enctype="{enctype}" '
            f'aria-labelledby="form-title"><h2 id="form-title">{esc(step.title)}</h2>'
            '<p class="hint">Fields marked with * are required.</p>'
            + "".join(
                render_field(f, values, errors.get(f.name), retained.get(f.name))
                for f in step.fields
            )
            + f'<button type="submit">Continue</button>{back}</form>'
        )
        prefix = "Error: " if errors else ""
        self._send_html(status, page(f"{prefix}{step.title}: {job.title}", body))

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
        self._send_html(HTTPStatus.OK, page(f"Review: {job.title}", body))

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
