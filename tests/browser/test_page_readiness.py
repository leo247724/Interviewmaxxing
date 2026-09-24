"""Page readiness before classification, against the localhost mock in real Chromium:
an SPA that renders its form late, a static UNKNOWN page, a perpetual loading
indicator (bounded wait), a modal cookie-consent dialog, and closed-job wording.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest

from interviewmaxxing_browser import PlaywrightSessionFactory, build_page, inspector_script
from interviewmaxxing_browser.signals import APPLY_LINK, JOB_CLOSED
from interviewmaxxing_browser.snapshot import DomSnapshot
from interviewmaxxing_core import BrowserOptions, PageKind

STANDARD_IDS = [
    "first_name", "last_name", "email", "phone", "linkedin_url", "resume",
    "work_authorization", "years_experience", "sponsorship", "skills",
    "work_arrangements", "open_to_relocation", "why_brambleway",
]


async def _timed_open(
    options: BrowserOptions, url: str, *, settle_timeout_s: float = 8.0
) -> tuple[Any, float]:
    browser = await PlaywrightSessionFactory(settle_timeout_s=settle_timeout_s).start(options)
    try:
        started = time.monotonic()
        inspection = await browser.open(url)
        return inspection, time.monotonic() - started
    finally:
        await browser.close()


def test_spa_form_is_classified_once_it_renders(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    url = server.url("/jobs/spa-loading/apply")
    inspection, elapsed = kit.run(_timed_open(options, url, settle_timeout_s=8.0))
    assert inspection.kind is PageKind.APPLICATION_FORM, inspection.message
    assert [f.id for f in inspection.form.fields] == STANDARD_IDS
    assert inspection.form.is_final_step is True
    # The form appears 1.5 s after load; readiness must wait for it but not much longer.
    assert 1.4 < elapsed < 8.0, elapsed
    print(f"\nspa-loading classified after {elapsed:.2f}s")

    async def observed() -> Any:
        browser = await PlaywrightSessionFactory(settle_timeout_s=8.0).start(options)
        try:
            return await browser.observe(url)
        finally:
            await browser.close()

    assert kit.run(observed()).kind is PageKind.APPLICATION_FORM


def test_static_unknown_page_classifies_quickly(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    inspection, elapsed = kit.run(_timed_open(options, server.url("/"), settle_timeout_s=15.0))
    assert inspection.kind is PageKind.UNKNOWN and inspection.form is None
    assert elapsed < 3.5, elapsed  # settle + two stable reads, never the 15 s timeout
    print(f"\nstatic UNKNOWN page classified after {elapsed:.2f}s")


def test_a_page_that_never_stops_loading_is_bounded_by_the_settle_timeout(
    kit: SimpleNamespace, options: BrowserOptions
) -> None:
    url = "data:text/html,<title>Fixture</title><p aria-busy='true'>Loading your application</p>"
    inspection, elapsed = kit.run(_timed_open(options, url, settle_timeout_s=2.0))
    assert inspection.kind is PageKind.UNKNOWN
    assert 1.9 < elapsed < 5.0, elapsed


def test_cookie_banner_is_declined_before_the_form_is_read(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, dict[str, str]]:
        browser = await PlaywrightSessionFactory(settle_timeout_s=8.0).start(options)
        try:
            url = server.url("/jobs/cookie-banner/apply")
            # Before anything is dismissed the inspector must see no usable form at all,
            # otherwise this test would prove nothing about the dismissal.
            await browser.page.goto(url)
            raw = DomSnapshot.model_validate(await browser.page.evaluate(inspector_script()))
            covered = build_page(raw)
            inspection = await browser.open(url)
            cookies = {c["name"]: c["value"] for c in await browser.page.context.cookies()}
            return covered, inspection, cookies
        finally:
            await browser.close()

    covered, inspection, cookies = kit.run(scenario())
    assert covered.inspection.kind is PageKind.UNKNOWN and covered.form is None
    assert [b.text for b in covered.snapshot.buttons] == ["Cookies settings", "Accept all", "Decline all"]
    assert inspection.kind is PageKind.APPLICATION_FORM, inspection.message
    assert [f.id for f in inspection.form.fields] == STANDARD_IDS
    assert cookies.get("bwa_consent") == "declined"  # decline is preferred over accept
    assert server.submissions("cookie-banner")["accepted_count"] == 0


def test_posting_with_apply_buttons_and_a_locale_select_is_followed_to_the_form(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """Two script-navigating "Apply now" buttons, a share widget and a language select
    outside any form are a job description, never a one-field final form."""
    url = server.url("/postings/with-select")

    async def scenario() -> tuple[Any, Any]:
        browser = await PlaywrightSessionFactory(settle_timeout_s=8.0).start(options)
        try:
            posting = await browser.observe(url)  # classifies without following anything
            opened = await browser.open(url)
            return posting, opened
        finally:
            await browser.close()

    posting, opened = kit.run(scenario())
    assert posting.kind is PageKind.JOB_DESCRIPTION and posting.form is None
    assert opened.kind is PageKind.APPLICATION_FORM, opened.message
    assert opened.observed_url == server.url("/jobs/standard/apply")
    assert [f.id for f in opened.form.fields] == STANDARD_IDS
    assert opened.form.is_final_step is True
    assert opened.job_identity is not None and opened.job_identity.external_job_id == "BWA-ENG-101"


ACCEPTED_APPLY_WORDING = [
    "Apply", "Apply now", "Apply here", "Apply online", "Apply today", "Apply To Position",
    "Apply for this Job", "Apply for the role", "Apply to this opening", "Apply for position",
    "Start your application", "Begin your application", "Begin application",
    "Continue to application", "I'm interested", "Apply now \u00bb",
]
EXCLUDED_APPLY_WORDING = [
    "Apply with LinkedIn", "Apply using Indeed", "Use my Indeed resume", "Submit application",
    "Apply now and submit", "Already applied? Check your application status", "Application status",
    "Apply filters",
]


@pytest.mark.parametrize("text", ACCEPTED_APPLY_WORDING)
def test_apply_wording_is_recognized(text: str) -> None:
    assert APPLY_LINK.search(text)


@pytest.mark.parametrize("text", EXCLUDED_APPLY_WORDING)
def test_third_party_and_submit_wording_is_not_an_apply_control(text: str) -> None:
    assert not APPLY_LINK.search(text)


async def _open_posting(options: BrowserOptions, url: str) -> tuple[Any, Any]:
    browser = await PlaywrightSessionFactory(settle_timeout_s=8.0).start(options)
    try:
        return await browser.observe(url), await browser.open(url)
    finally:
        await browser.close()


@pytest.mark.parametrize(("text", "kind"), [
    ("Apply To Position", "link"), ("Apply for this Job", "link"), ("Apply to this opening", "link"),
    ("Apply here", "link"), ("Begin your application", "link"), ("Continue to application", "link"),
    ("Apply today", "button"), ("Apply", "form"),
])
def test_apply_wording_posting_is_followed_to_the_form(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, text: str, kind: str
) -> None:
    """A link, a script-navigating button, or a submit button in a form with no
    fillable field (a navigation form): all lead to the application form."""
    url = server.url(f"/postings/apply-wording?kind={kind}&text={quote(text)}")
    posting, opened = kit.run(_open_posting(options, url))
    assert posting.kind is PageKind.JOB_DESCRIPTION and posting.form is None
    assert opened.kind is PageKind.APPLICATION_FORM, opened.message
    assert opened.observed_url == server.url("/jobs/standard/apply")
    assert [f.id for f in opened.form.fields] == STANDARD_IDS and opened.form.page_errors == []
    assert server.submissions("standard")["accepted_count"] == 0


@pytest.mark.parametrize("text", ["Apply with LinkedIn", "Use my Indeed resume", "Submit application"])
def test_excluded_apply_wording_is_not_followed(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, text: str
) -> None:
    url = server.url(f"/postings/apply-wording?kind=link&text={quote(text)}")
    posting, opened = kit.run(_open_posting(options, url))
    assert posting.kind is PageKind.UNKNOWN
    assert opened.kind is PageKind.UNKNOWN and opened.observed_url == url


def test_the_standard_submit_button_is_never_an_apply_control(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            inspection = await browser.open(server.url("/jobs/standard/apply"))
            return inspection, browser.last_page
        finally:
            await browser.close()

    inspection, model = kit.run(scenario())
    assert inspection.kind is PageKind.APPLICATION_FORM and inspection.form.submit_selector
    assert model is not None and model.apply_controls == []
    assert [b.intent.value for b in model.buttons] == ["SUBMIT"]


def test_closed_job_wording_is_job_closed(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    inspection, _ = kit.run(_timed_open(options, server.url("/closed")))
    assert inspection.kind is PageKind.JOB_CLOSED and inspection.form is None


@pytest.mark.parametrize(
    ("text", "closed"),
    [
        ("We're sorry, that job does not exist or is not currently active.", True),
        ("This job is not currently active.", True),
        ("This role is no longer active.", True),
        ("The position is no longer available.", True),
        ("This position has been filled.", True),
        ("This job has been closed.", True),
        ("This job posting has expired.", True),
        ("We are no longer accepting applications for this role.", True),
        ("Your session has expired. Please sign in again.", False),
        ("Applications are being accepted until Friday.", False),
        ("The opening is active and accepting applications.", False),
        ("Positions available in Denver and remote.", False),
    ],
)
def test_job_closed_wording(text: str, closed: bool) -> None:
    assert bool(JOB_CLOSED.search(text)) is closed
