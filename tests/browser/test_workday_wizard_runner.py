"""Workday-style wizard end to end: prepare-only runs of the real runner over the real
store and the mock's ``workday-wizard`` tenant (``scripts/mock_workday.py``).

The posting's Apply opens the "Start Your Application" chooser; Apply Manually leads to
the account step, which only the person can pass (signing in is a user action; the
runner never types credentials or creates an account). Signed in, the one-document
wizard has five question pages and a Review: "Save and Continue" saves each page as a
draft, the résumé is uploaded on attach, and only "Submit" records an application.

The fictional candidate (Avery Quill) has saved answers for every question, so a
signed-in run fills every page, stops at the Review and submits nothing. All traffic is
localhost; the ``/__test__/`` reads are test assertions only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import time
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Page, async_playwright

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_cli.runner import (
    LocalApplicationRunner,
    NoninteractiveInteraction,
    pending_inputs,
)
from interviewmaxxing_core import (
    AnswerValue,
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    ApplyOutcome,
    BooleanValue,
    BrowserOptions,
    ChoiceValue,
    FieldOption,
    FileValue,
    LocalPaths,
    MissingReason,
    MultiChoiceValue,
    PageInspection,
    SemanticType,
    TextValue,
)

REPO = Path(__file__).resolve().parents[2]
RESUME_PATH = REPO / "tests" / "fixtures" / "browser" / "resume_avery_quill.pdf"
VERIFIED_AT = "2026-09-01T12:00:00Z"
JOB = "workday-wizard"
POSTING = f"/jobs/{JOB}"
APPLY_MANUALLY = f"/jobs/{JOB}/apply/applyManually"
EMAIL = "avery.quill@example.test"
PASSWORD = "fixture-password-123"  # the mock's fixture account (SIGNIN_EMAIL / SIGNIN_PASSWORD)
SESSION_COOKIE = "bwa_wd_session"
WIZARD_SHOWN = "[data-automation-id=applyFlowPage-myInformation]"
PAGE_IDS = ["myInformation", "myExperience", "primaryQuestionnaire", "voluntaryDisclosures",
            "selfIdentify"]
PHONE_CODE_REJECTED = ("Phone Number: enter the number without the country phone code; "
                       "choose it in Country Phone Code.")
PHONE_CODE_QUESTION = "Country Phone Code\nSearch"  # a single-select search picker
PHONE_CODE_FIELD = "phoneNumber--countryPhoneCode"

SAVED_ANSWERS: dict[str, tuple[str | None, Any]] = {
    # Question wording as the runtime reads it (label, then a hint or placeholder):
    # (semantic type, value).
    "How Did You Hear About Us?\nSearch": ("REFERRAL_SOURCE", "LinkedIn"),
    "Have you previously worked for Brambleway Analytics?": (None, "No"),
    "Phone Device Type": (None, "Mobile"),
    PHONE_CODE_QUESTION: ("COUNTRY", "United States of America (+1)"),
    "Are you legally authorized to work in the United States?": ("WORK_AUTHORIZATION", "Yes"),
    "Will you now or in the future require sponsorship for employment visa status?":
        ("SPONSORSHIP", "No"),
    "How many years of B2B growth marketing experience do you have?": (None, "6-9 years"),
    "Gender": ("EEO_GENDER", "I do not wish to self-identify"),
    "Ethnicity": ("EEO_RACE_ETHNICITY", "I do not wish to self-identify"),
    "Veteran Status": ("EEO_VETERAN_STATUS", "I am not a protected veteran"),
    "Yes, I have read and consent to the terms and conditions\nBrambleway Analytics collects "
    "this information for its equal employment opportunity reporting. Providing it is "
    "voluntary.": ("CONSENT", True),
    "Name": (None, "Avery Quill"),
    "Date\nMM/DD/YYYY": (None, "09/24/2026"),
    "Disability Status\nPlease check one of the boxes below:":
        ("EEO_DISABILITY_STATUS", "I do not want to answer"),
}

STEP_QUESTIONS: dict[int, set[str]] = {
    # The questions each wizard step's packet answers (form.step is the progress step - 1).
    1: {"source--source", "candidateIsPreviousWorker", "country", "firstName", "lastName",
        "addressLine1", "city", "state", "postalCode", "email", "phoneType", PHONE_CODE_FIELD,
        "phoneNumber"},  # "extension" is optional and stays blank
    2: {"resumeAttachments--attachments-input", "linkedin"},
    3: {"workAuthorization", "sponsorship", "growthYears"},
    4: {"gender", "ethnicity", "veteranStatus", "acceptTerms"},
    5: {"selfIdName", "selfIdentifiedDisabilityData--dateSignedOn-dateSectionMonth-input",
        "disabilityStatus"},  # every Self Identify question is required
    6: set(),  # the Review asks nothing
}

PREPARED_DRAFT: dict[str, dict[str, Any]] = {
    # What the site saved, page by page. Menus hold the site's opaque option ids.
    "myInformation": {
        "source": ["LinkedIn"],  # the multi-select prompt's chosen item
        "candidateIsPreviousWorker": "false",
        "country": "wd_country_1",  # United States of America
        "firstName": "Avery", "lastName": "Quill",
        "addressLine1": "1200 Larimer St", "city": "Denver",
        "state": "wd_state_2",  # Colorado
        "postalCode": "80202", "email": EMAIL,
        "phoneType": "wd_phonetype_1",  # Mobile
        "countryPhoneCode": ["United States of America (+1)"],  # the picker's one item
        "phoneNumber": "(303) 555-0142",  # national: the code is chosen in its own picker
        "extension": "",
    },
    "myExperience": {"linkedin": "https://www.linkedin.example.test/in/avery-quill"},
    "primaryQuestionnaire": {
        "workAuthorization": "wd_yn_1",  # Yes
        "sponsorship": "wd_yn_2",  # No
        "growthYears": "wd_years_3",  # 6-9 years
    },
    "voluntaryDisclosures": {
        "gender": "wd_gender_3", "ethnicity": "wd_ethnicity_6",  # I do not wish to self-identify
        "veteranStatus": "wd_veteran_1",  # I am not a protected veteran
        "acceptTerms": True,
    },
    "selfIdentify": {
        "selfIdName": "Avery Quill",
        "selfIdDate": "09/24/2026",  # typed across the Month / Day / Year spinbuttons
        "disabilityStatus": ["wd_disability_3"],  # one box: I do not want to answer
    },
}


def _write_profile(paths: LocalPaths,
                   answers: Mapping[str, tuple[str | None, Any]] | None = None) -> None:
    """The fictional candidate, with the street address Workday asks for."""
    directory = paths.profile_dir / "default"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RESUME_PATH, directory / "resume.pdf")
    saved = [{"id": f"sa.{n}", "scope": "GLOBAL", "semantic_type": semantic, "question": question,
              "value": value, "confirmed_at": VERIFIED_AT}
             for n, (question, (semantic, value)) in enumerate((answers or SAVED_ANSWERS).items())]
    profile = {
        "id": "default",
        "identity": {
            "first_name": "Avery", "last_name": "Quill", "email": EMAIL,
            "phone": "+1 (303) 555-0142",
            "linkedin_url": "https://www.linkedin.example.test/in/avery-quill",
            "address": {"street": "1200 Larimer St", "city": "Denver", "region": "Colorado",
                        "postal_code": "80202", "country": "United States"},
            "verified_at": VERIFIED_AT,
        },
        "resume": {"id": "resume_supplied", "path": "resume.pdf"},
        "facts": [],
        "saved_answers": saved,
    }
    (directory / "profile.json").write_text(json.dumps(profile, indent=2))


async def _sign_in(page: Page) -> None:
    """The person on the account step: switch to Sign In, enter the fixture account and
    continue. The site sets its session cookie and returns to the application."""
    await page.click("[data-automation-id=signInLink]")
    await page.wait_for_url(re.compile(r"view=signin"))
    await page.fill("[data-automation-id=email]", EMAIL)
    await page.fill("[data-automation-id=password]", PASSWORD)
    await page.click("[data-automation-id=click_filter]")  # the overlay over the hidden submit
    await page.wait_for_selector(WIZARD_SHOWN)


class SigningInFactory:
    """Playwright sessions whose person signs in when the runner waits for them
    (``wait_for_user``): while the real wait polls the account step, or just before it
    starts (a prompt that returns only once the person says they are done)."""

    def __init__(self, *, during_wait: bool) -> None:
        self.during_wait = during_wait
        self.reasons: list[str] = []
        self.log: list[str] = []

    async def start(self, options: BrowserOptions) -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        wait = browser.wait_for_user

        async def person_signs_in(reason: str, timeout_s: float | None = None) -> PageInspection:
            self.reasons.append(reason)
            if self.during_wait:
                waiting = asyncio.ensure_future(wait(reason, timeout_s))
                self.log.append("wait started")
                try:
                    await asyncio.sleep(0.6)  # the wait polls the account step meanwhile
                    self.log.append("wait ended early" if waiting.done() else "still waiting")
                    await _sign_in(browser.page)
                    self.log.append("signed in")
                    page = await waiting
                finally:
                    if not waiting.done():
                        waiting.cancel()
            else:
                await _sign_in(browser.page)
                self.log.append("signed in")
                self.log.append("wait started")
                page = await wait(reason, timeout_s)
            self.log.append(f"wait returned {page.kind.name}")
            return page

        browser.wait_for_user = person_signs_in
        return browser


def _runner(paths: LocalPaths, *, act: bool = False, factory: Any = None) -> LocalApplicationRunner:
    return LocalApplicationRunner(
        paths=paths, interaction=NoninteractiveInteraction(allow_browser_action=act),
        headless=True, browser_factory=factory or PlaywrightSessionFactory(), prepare_only=True)


def _answers(packet: ApplicationPacket) -> dict[str, AnswerValue]:
    return {a.field_id: a.value for a in packet.answers}


def _assert_nothing_submitted(server: Any, store: ApplicationStore,
                              result: ApplyOutcome) -> dict[str, Any]:
    """Nothing reached "Submit": no submit call, no accepted application, no submission
    attempt or receipt. Returns the mock's Workday state."""
    workday: dict[str, Any] = server.api("GET", "/__test__/workday")
    assert (workday["submit_call_count"], workday["accepted_count"]) == (0, 0), workday
    assert server.submissions(JOB)["accepted_count"] == 0
    assert store.list_attempts(result.application_id) == []
    assert result.receipt is None and store.get_receipt(result.application_id) is None
    return workday


def _assert_prepared(server: Any, paths: LocalPaths, result: ApplyOutcome, *,
                     visits: int) -> None:
    """Every wizard page filled and saved, stopped at the Review, nothing submitted."""
    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    assert "Prepared to the final review step" in result.message, result.message
    assert result.missing_inputs == []
    resume_sha256 = hashlib.sha256(RESUME_PATH.read_bytes()).hexdigest()
    with ApplicationStore.open(paths.state_db) as store:
        workday = _assert_nothing_submitted(server, store, result)
        events = store.list_events(result.application_id)
        [ready] = [e for e in events if e.event == "preparation.ready"]
        packets = [store.get_packet(e.metadata["packet_id"]) for e in events
                   if e.event == "packet.saved"]
        latest = store.latest_packet(result.application_id)
        pinned = store.pinned_resume(result.application_id)
        assert pending_inputs(store, result.application_id) == []
    assert (events[-1].event, events[-1].metadata) == ("application.needs_input", {
        "missing_inputs": [], "reason": "prepared for final review; submission disabled"})
    assert ready.metadata["submitted"] is False
    assert ready.metadata["form_step"] == 6
    assert ready.metadata["form_url"] == server.url(APPLY_MANUALLY)  # one document throughout

    # One complete packet per step, in order. The latest is the Review's (it answers
    # nothing) and is the one the preparation names.
    assert [p.form_step for p in packets] == [1, 2, 3, 4, 5, 6]
    assert all(p.is_complete and p.missing_inputs == [] for p in packets)
    assert {p.form_step: set(_answers(p)) for p in packets} == STEP_QUESTIONS
    assert latest is not None and latest.id == packets[-1].id == ready.metadata["packet_id"]
    assert (latest.form_step, latest.answers) == (6, [])
    information, experience, _, disclosures, self_identify, _ = map(_answers, packets)
    assert information["phoneNumber"] == TextValue(text="+1 (303) 555-0142")  # the profile's
    assert information[PHONE_CODE_FIELD] == TextValue(text="United States of America (+1)")
    assert information["state"] == ChoiceValue(value="wd_state_2", label="Colorado")
    assert disclosures["acceptTerms"] == BooleanValue(checked=True)
    assert self_identify == {
        "selfIdName": TextValue(text="Avery Quill"),
        "selfIdentifiedDisabilityData--dateSignedOn-dateSectionMonth-input":
            TextValue(text="09/24/2026"),
        "disabilityStatus": MultiChoiceValue(choices=[
            FieldOption(value="wd_disability_3", label="I do not want to answer")]),
    }
    resume = experience["resumeAttachments--attachments-input"]
    assert isinstance(resume, FileValue)
    assert pinned is not None and resume.artifact.sha256 == pinned.sha256 == resume_sha256

    # What the site holds: five saves in order, each accepted once, and the draft.
    assert [(s["page"], s["ok"], s["errors"]) for s in workday["saves"]] == [
        (page_id, True, {}) for page_id in PAGE_IDS]
    draft = workday["drafts"][EMAIL]
    assert draft["pages"] == PREPARED_DRAFT
    assert {k: draft["resume"][k] for k in ("filename", "content_type", "size", "sha256")} == {
        "filename": "resume.pdf", "content_type": "application/pdf",
        "size": RESUME_PATH.stat().st_size, "sha256": resume_sha256}
    assert workday["uploads"] == [draft["resume"]["upload_id"]]  # attached exactly once
    assert server.submissions(JOB)["rejected_count"] == 0
    assert workday["route_visits"] == {"applyManually": visits}  # never the other routes
    assert workday["accounts"] == []  # the fixture account signed in; none was created


def test_account_step_stops_a_prepare_only_run_as_a_user_action(
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    _write_profile(isolated_imx_home)
    result = kit.run(_runner(isolated_imx_home).apply(server.url(POSTING), candidate_id="default"))

    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    [need] = result.missing_inputs
    assert (need.reason, need.field_id, need.label) == (MissingReason.USER_ACTION, None, "Sign in")
    host = urlsplit(server.origin).netloc  # 127.0.0.1:PORT, the site the account is for
    assert need.prompt.startswith(f"An account on {host} is needed to apply."), need.prompt
    assert result.message == need.prompt
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        workday = _assert_nothing_submitted(server, store, result)
        events = [e.event for e in store.list_events(result.application_id)]
        assert "application.needs_input" in events
        assert "preparation.ready" not in events
        assert pending_inputs(store, result.application_id) == [need]  # kept for a later run
        assert store.latest_packet(result.application_id) is None  # no form was answered
    # The chooser's Apply Manually, once: never Autofill with Resume or Use My Last
    # Application. Nothing was saved, uploaded or created.
    assert workday["route_visits"] == {"applyManually": 1}
    assert workday["saves"] == []
    assert workday.get("uploads", []) == []
    assert workday["drafts"] == {}
    assert workday["accounts"] == []


def test_signing_in_during_the_wait_lets_one_run_prepare_every_page(
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    _write_profile(isolated_imx_home)
    factory = SigningInFactory(during_wait=True)
    runner = _runner(isolated_imx_home, act=True, factory=factory)
    result = kit.run(runner.apply(server.url(POSTING), candidate_id="default"))

    # The runner's Apply Manually, the person's Sign In view and the return after signing
    # in: the run went on in that page without reopening the chooser.
    _assert_prepared(server, isolated_imx_home, result, visits=3)
    [reason] = factory.reasons
    assert reason.startswith(f"An account on {urlsplit(server.origin).netloc} is needed"), reason
    assert factory.log == ["wait started", "still waiting", "signed in",
                           "wait returned APPLICATION_FORM"]


def test_signing_in_before_the_wait_prepares_the_same_way(
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    _write_profile(isolated_imx_home)
    factory = SigningInFactory(during_wait=False)
    runner = _runner(isolated_imx_home, act=True, factory=factory)
    result = kit.run(runner.apply(server.url(POSTING), candidate_id="default"))

    _assert_prepared(server, isolated_imx_home, result, visits=3)
    [reason] = factory.reasons
    assert reason.startswith(f"An account on {urlsplit(server.origin).netloc} is needed"), reason
    assert factory.log == ["signed in", "wait started", "wait returned APPLICATION_FORM"]


async def _sign_in_once(browser_dir: Path, account_step: str) -> list[dict[str, Any]]:
    """The person signs in once, between runs, in the runner's persistent browser
    profile. Returns the profile's cookies."""
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(str(browser_dir),
                                                                     headless=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(account_step)
            await _sign_in(page)
            return [dict(cookie) for cookie in await context.cookies()]
        finally:
            await context.close()


def test_one_sign_in_in_the_persistent_profile_serves_later_runs(
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    _write_profile(isolated_imx_home)
    first = kit.run(_runner(isolated_imx_home).apply(server.url(POSTING), candidate_id="default"))
    assert first.state is ApplicationState.NEEDS_INPUT, first.message
    assert [m.reason for m in first.missing_inputs] == [MissingReason.USER_ACTION]

    cookies = kit.run(_sign_in_once(isolated_imx_home.browser_dir, server.url(APPLY_MANUALLY)))
    [session] = [c for c in cookies if c["name"] == SESSION_COOKIE]
    assert session["expires"] > time.time()  # persistent (Max-Age), kept in the profile

    # Resumed without --act: the account step would stop this run, and it never comes.
    result = kit.run(_runner(isolated_imx_home).resume(first.application_id))
    assert result.application_id == first.application_id
    # 1 (the first run) + 3 (the person: account step, Sign In view, return) + 1 (resumed).
    _assert_prepared(server, isolated_imx_home, result, visits=5)
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        stops = [e.metadata for e in store.list_events(result.application_id)
                 if e.event == "application.needs_input"]
    assert [[m["reason"] for m in stop["missing_inputs"]] for stop in stops] == [
        ["USER_ACTION"], []]


def test_a_rejected_phone_number_is_asked_again_and_nothing_is_submitted(
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    """With "United Kingdom (+44)" chosen as the country phone code, the profile's number
    is typed as given ("+1 (303) 555-0142"). The site refuses My Information, and the
    number goes back to the person as a question: not retried, skipped or submitted."""
    _write_profile(isolated_imx_home, {
        **SAVED_ANSWERS, PHONE_CODE_QUESTION: ("COUNTRY", "United Kingdom (+44)")})
    runner = _runner(isolated_imx_home, act=True, factory=SigningInFactory(during_wait=False))
    result = kit.run(runner.apply(server.url(POSTING), candidate_id="default"))

    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    assert result.message == "1 required question(s) need your answer."
    [need] = result.missing_inputs
    assert (need.field_id, need.reason, need.form_step, need.label, need.semantic_type,
            need.required) == ("phoneNumber", MissingReason.NO_ANSWER, 1, "Phone Number",
                               SemanticType.PHONE, True)
    assert need.prompt.startswith("The site rejected the answer for this question: ")
    assert PHONE_CODE_REJECTED in need.prompt, need.prompt
    assert need.prompt.endswith("Please provide a corrected answer."), need.prompt
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        workday = _assert_nothing_submitted(server, store, result)
        events = store.list_events(result.application_id)
        [rejected] = [e for e in events if e.event == "validation.rejected"]
        packets = [store.get_packet(e.metadata["packet_id"]) for e in events
                   if e.event == "packet.saved"]
        assert "preparation.ready" not in [e.event for e in events]
        assert pending_inputs(store, result.application_id) == [need]
    assert rejected.metadata["form_step"] == 1
    assert rejected.metadata["form_url"] == server.url(APPLY_MANUALLY)
    assert rejected.metadata["fields"] == [{"field_id": "phoneNumber", "message": PHONE_CODE_REJECTED,
                                            "field_fingerprint": need.field_fingerprint}]
    # The first packet typed the profile's number; the one saved after the rejection
    # leaves it out and asks for it instead.
    first, again = packets
    assert (first.form_step, again.form_step) == (1, 1)
    assert _answers(first)["phoneNumber"] == TextValue(text="+1 (303) 555-0142")
    assert "phoneNumber" not in _answers(again)
    assert again.missing_inputs == [need]
    assert _answers(again)[PHONE_CODE_FIELD] == TextValue(text="United Kingdom (+44)")
    # The site refused the page once and kept nothing; the run did not save it again or
    # go on to later pages (no résumé upload).
    [save] = workday["saves"]
    assert (save["page"], save["ok"], save["errors"]) == (
        "myInformation", False, {"phoneNumber": PHONE_CODE_REJECTED})
    assert (save["values"]["phoneNumber"], save["values"]["countryPhoneCode"]) == (
        "+1 (303) 555-0142", ["United Kingdom (+44)"])
    assert workday["drafts"][EMAIL] == {"pages": {}, "resume": None}
    assert workday.get("uploads", []) == []
    assert server.submissions(JOB)["rejected_count"] == 1
    assert workday["route_visits"] == {"applyManually": 3}
