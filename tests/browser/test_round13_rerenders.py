"""Round 13: forms that re-render while being filled (retry six on 9824540).

A. BambooHR re-mounts its Fabric text fields under new generated ids once a Yes/No is
   answered: "Date Available" (no name) is the same question under another id, and the
   address fields keep their names with new selectors. The fill goes on; the question is
   written through its re-resolved control.
B. Greenhouse shows "Please identify your race" right after "Are you Hispanic/Latino?" once
   "No" is chosen, and the Hispanic/Latino question's help text changes. The fill goes on
   with the approved answers; the step is then inspected and resolved again.
C. Teamtailor renders its "Linkedin profile" question only once it scrolls into view, that
   is while the first answers are typed. Same treatment: the question is named, reported as
   not yet answered, and answered after a fresh inspection.

Real headless Chromium against the local mock ATS, fictional data, temp IMX_HOME; nothing
is submitted.
"""

from __future__ import annotations

import contextlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_cli.runner import LocalApplicationRunner, NoninteractiveInteraction
from interviewmaxxing_core import (
    ApplicationForm,
    ApplicationState,
    ApplicationStore,
    BrowserOptions,
    FieldFillStatus,
    LocalPaths,
    SemanticType,
)

REPO = Path(__file__).resolve().parents[2]
RESUME_PATH = REPO / "tests" / "fixtures" / "browser" / "resume_avery_quill.pdf"
VERIFIED_AT = "2026-09-01T12:00:00Z"
CONTACT = {"first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test"}


def _statuses(result: Any) -> dict[str, FieldFillStatus]:
    return {f.field_id: f.status for f in result.fields}


def _status(result: Any, field_id: str) -> Any:
    return next(f for f in result.fields if f.field_id == field_id)


async def _open(options: BrowserOptions, url: str) -> Any:
    browser = await PlaywrightSessionFactory().start(options)
    page = await browser.open(url)
    assert page.form is not None, page.message
    return browser


# --- A. BambooHR: generated ids churn on every Yes/No ------------------------------------

CHURN = "/jobs/bamboohr-churn/apply"
AUTH = "customQuestionAnswers.yes_no_2101"
SPONSOR = "customQuestionAnswers.yes_no_2102"
DATE_ID = "FabricTextField-51"
CHURN_ANSWERS = {**CONTACT, "streetAddress.value": "1234 Fictional Avenue", "city.value": "Denver",
                 "zip.value": "80202", AUTH: "Yes", SPONSOR: "No", DATE_ID: "10/01/2026"}
CHURN_STATE = f"""() => ({{
  rerenders: window.__mock.rerenders,
  date: document.querySelector('input[data-fabric]:not([name])').value,
  dateId: document.querySelector('input[data-fabric]:not([name])').id,
  street: document.querySelector('input[name="streetAddress.value"]').value,
  zip: document.querySelector('input[name="zip.value"]').value,
  auth: (document.querySelector('input[name="{AUTH}"]:checked') || {{}}).value || null,
  sponsor: (document.querySelector('input[name="{SPONSOR}"]:checked') || {{}}).value || null,
}})"""


def test_a_question_a_re_render_gives_a_new_generated_id_is_the_same_question(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[ApplicationForm, Any, dict[str, Any], Any]:
        browser = await _open(options, server.url(CHURN))
        try:
            form = (await browser.inspect()).form
            result = await browser.fill(form, kit.build(form, CHURN_ANSWERS).packet)
            state = await browser.page.evaluate(CHURN_STATE)
            review = await browser.prepare_review()  # the step is the one filled, renamed ids and all
            return form, result, state, review
        finally:
            await browser.close()

    form, result, state, review = kit.run(scenario())
    assert DATE_ID in [f.id for f in form.fields]
    assert result.ok, (result.fields, result.page_errors)
    assert result.page_errors == []
    assert all(s is FieldFillStatus.FILLED for s in _statuses(result).values()), result.fields
    # Two answers, two re-renders: the date was written through its re-resolved control.
    assert state["rerenders"] == 2 and state["dateId"] != DATE_ID
    assert (state["date"], state["street"], state["zip"]) == ("10/01/2026", "1234 Fictional Avenue", "80202")
    assert (state["auth"], state["sponsor"]) == ("Yes", "No")
    assert review.form is not None and state["dateId"] in [f.id for f in review.form.fields]
    assert server.submissions("bamboohr-churn")["accepted_count"] == 0


# --- B. Greenhouse: the race question follows "No" to Hispanic/Latino ---------------------

EEO = "/jobs/greenhouse-eeo/apply"
EEO_ANSWERS = {**CONTACT, "question_7001": "Yes", "gender": "Decline To Self Identify",
               "hispanic_ethnicity": "No", "veteran_status": "I am not a protected veteran"}
EEO_STATE = """() => Object.fromEntries(['question_7001', 'gender', 'hispanic_ethnicity', 'race', 'veteran_status']
  .map((name) => [name, (document.querySelector('select[name="' + name + '"]') || {}).value ?? null]))"""


def test_a_question_that_follows_a_choice_is_answered_after_the_approved_ones(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> None:
        browser = await _open(options, server.url(EEO))
        try:
            form = (await browser.inspect()).form
            assert "race" not in [f.id for f in form.fields]
            first = await browser.fill(form, kit.build(form, EEO_ANSWERS).packet)
            assert first.failed_field_ids() == [], first.fields
            statuses = _statuses(first)
            # The fill went on past the reveal: Veteran Status was written too.
            assert all(statuses[f] is FieldFillStatus.FILLED for f in EEO_ANSWERS), statuses
            race = _status(first, "race")
            assert race.status is FieldFillStatus.SKIPPED
            assert "Please identify your race" in (race.detail or "")
            [error] = first.page_errors
            # The mock marks optional questions "(optional)"; the questions are named as shown.
            assert "1 question(s) appeared (Please identify your race (optional))" in error, error
            assert "(Are you Hispanic/Latino? (optional)) changed their help text" in error, error
            assert "after the answer to 'Are you Hispanic/Latino? (optional)'" in error, error
            assert "inspect this step and resolve it again" in error
            assert "changed while filling" not in error
            again = (await browser.inspect()).form
            ids = [f.id for f in again.fields]
            assert ids.index("race") == ids.index("hispanic_ethnicity") + 1
            second = await browser.fill(again, kit.build(again, {**EEO_ANSWERS, "race": "Decline To Self Identify"}).packet)
            assert second.ok, (second.fields, second.page_errors)
            assert await browser.page.evaluate(EEO_STATE) == {
                "question_7001": "1", "gender": "3", "hispanic_ethnicity": "No", "race": "7", "veteran_status": "1"}
        finally:
            await browser.close()

    kit.run(scenario())
    assert server.submissions("greenhouse-eeo")["accepted_count"] == 0


# --- C. Teamtailor: a question rendered once it scrolls into view -------------------------

LATE = "/jobs/teamtailor-late/apply"
LINKEDIN = "candidate[answers_attributes][0][text]"
LATE_ANSWERS = {**CONTACT, "phone": "+1 303 555 0142"}


def test_a_question_rendered_late_is_named_and_answered_after_a_fresh_inspection(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> None:
        browser = await _open(options, server.url(LATE))
        try:
            form = (await browser.inspect()).form
            assert LINKEDIN not in [f.id for f in form.fields]
            first = await browser.fill(form, kit.build(form, LATE_ANSWERS).packet)
            assert first.failed_field_ids() == [], first.fields
            assert all(_statuses(first)[f] is FieldFillStatus.FILLED for f in LATE_ANSWERS)
            late = _status(first, LINKEDIN)
            assert late.status is FieldFillStatus.SKIPPED and "Linkedin profile" in (late.detail or "")
            [error] = first.page_errors
            assert error.startswith("1 question(s) appeared (Linkedin profile) while filling"), error
            again = (await browser.inspect()).form
            assert again.fields[0].id == LINKEDIN
            answers = {**LATE_ANSWERS, LINKEDIN: "https://www.linkedin.example.test/in/avery-quill"}
            second = await browser.fill(again, kit.build(again, answers).packet)
            assert second.ok, (second.fields, second.page_errors)
            assert await browser.page.input_value('[name="candidate[answers_attributes][0][text]"]') == answers[LINKEDIN]
        finally:
            await browser.close()

    kit.run(scenario())
    assert server.submissions("teamtailor-late")["accepted_count"] == 0


REWORD_ON_MOUNT = """() => new MutationObserver((changes, observer) => {
  if (!document.querySelector('[name="candidate[answers_attributes][0][text]"]')) return;
  observer.disconnect();
  document.querySelector('label[for="f-email"]').firstChild.textContent = 'Work email';
}).observe(document.querySelector('form'), {childList: true, subtree: true})"""


def test_a_failure_names_the_question_that_appeared(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """A late question together with a reworded one is a changed page: the fill stops, and
    both the failure and the new question's report say which question appeared."""
    async def scenario() -> Any:
        browser = await _open(options, server.url(LATE))
        try:
            form = (await browser.inspect()).form
            await browser.page.evaluate(REWORD_ON_MOUNT)
            return await browser.fill(form, kit.build(form, LATE_ANSWERS).packet)
        finally:
            await browser.close()

    result = kit.run(scenario())
    failed = [f for f in result.fields if f.status is FieldFillStatus.FAILED]
    assert failed, result.fields
    detail = failed[0].detail or ""
    assert "changed while filling (" in detail, detail
    assert f"appeared: #1 {LINKEDIN} (Linkedin profile)" in detail, detail
    assert "(wording)" in detail, detail


# --- D. the runner prepares each form to its final review step ----------------------------

def _write_profile(paths: LocalPaths, forms: list[ApplicationForm], answers: dict[str, str]) -> None:
    """The fictional candidate, with saved answers worded as these forms ask them."""
    directory = paths.profile_dir / "default"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RESUME_PATH, directory / "resume.pdf")
    saved = []
    for form in forms:
        for field in form.fields:
            if field.id in answers and field.id not in CONTACT:
                semantic = None if field.semantic_type is SemanticType.UNKNOWN else field.semantic_type.value
                saved.append({"id": f"sa.{len(saved)}", "scope": "GLOBAL", "semantic_type": semantic,
                              "question": field.question_text, "value": answers[field.id],
                              "confirmed_at": VERIFIED_AT})
    profile = {
        "id": "default",
        "identity": {
            **CONTACT, "phone": "+1 (303) 555-0142",
            "linkedin_url": "https://www.linkedin.example.test/in/avery-quill",
            "address": {"street": "1234 Fictional Avenue", "city": "Denver", "region": "CO",
                        "postal_code": "80202", "country": "United States"},
            "verified_at": VERIFIED_AT,
        },
        "resume": {"id": "resume_supplied", "path": "resume.pdf"},
        "facts": [],
        "saved_answers": saved,
    }
    (directory / "profile.json").write_text(json.dumps(profile, indent=2))


class RecordingFactory:
    """The Playwright factory, recording the page as each browser closes."""

    def __init__(self, script: str) -> None:
        self.script = script
        self.states: list[Any] = []

    async def start(self, options: BrowserOptions) -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        close = browser.close

        async def close_and_record() -> None:
            with contextlib.suppress(Exception):
                self.states.append(await browser.page.evaluate(self.script))
            await close()

        browser.close = close_and_record
        return browser


async def _forms(options: BrowserOptions, url: str, reveal: str | None = None) -> list[ApplicationForm]:
    """The form as first inspected, and (``reveal``: a script) as it is once it re-rendered."""
    browser = await _open(options, url)
    try:
        forms = [(await browser.inspect()).form]
        if reveal:
            await browser.page.evaluate(reveal)
            await browser.page.wait_for_timeout(300)
            forms.append((await browser.inspect()).form)
        return forms
    finally:
        await browser.close()


def _prepare(kit: SimpleNamespace, paths: LocalPaths, url: str, script: str) -> tuple[Any, list[str], Any]:
    factory = RecordingFactory(script)
    runner = LocalApplicationRunner(paths=paths, interaction=NoninteractiveInteraction(),
                                    headless=True, browser_factory=factory, prepare_only=True)
    result = kit.run(runner.apply(url, candidate_id="default"))
    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    assert "Prepared to the final review step" in result.message, result.message
    assert result.missing_inputs == []
    with ApplicationStore.open(paths.state_db) as store:
        events = store.list_events(result.application_id)
        assert store.list_attempts(result.application_id) == []
    names = [e.event for e in events]
    failures = [e for e in events if e.event.startswith("application.") and "FAILED" in json.dumps(e.metadata)]
    assert failures == [], failures
    assert [e.metadata["submitted"] for e in events if e.event == "preparation.ready"] == [False]
    return result, names, factory.states[-1]


def test_the_runner_prepares_the_form_whose_ids_churn(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, isolated_imx_home: LocalPaths
) -> None:
    url = server.url(CHURN)
    _write_profile(isolated_imx_home, kit.run(_forms(options, url)), CHURN_ANSWERS)
    _, names, state = _prepare(kit, isolated_imx_home, url, CHURN_STATE)
    assert state["auth"] == "Yes" and state["sponsor"] == "No" and state["date"] == "10/01/2026"
    assert (state["street"], state["zip"]) == ("1234 Fictional Avenue", "80202")
    assert names.count("packet.saved") <= 2, names
    assert server.submissions("bamboohr-churn")["accepted_count"] == 0


REVEAL_RACE = """() => { const s = document.querySelector('select[name="hispanic_ethnicity"]');
  s.value = 'No'; s.dispatchEvent(new Event('change', {bubbles: true})); }"""


def test_the_runner_prepares_the_form_whose_choice_shows_the_race_question(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, isolated_imx_home: LocalPaths
) -> None:
    url = server.url(EEO)
    answers = {**EEO_ANSWERS, "race": "Decline To Self Identify"}
    _write_profile(isolated_imx_home, kit.run(_forms(options, url, REVEAL_RACE)), answers)
    _, names, state = _prepare(kit, isolated_imx_home, url, EEO_STATE)
    assert names.count("packet.saved") == 2, names  # resolved again once the race question showed
    assert state == {"question_7001": "1", "gender": "3", "hispanic_ethnicity": "No", "race": "7",
                     "veteran_status": "1"}
    assert server.submissions("greenhouse-eeo")["accepted_count"] == 0


SCROLL_TO_FORM = "() => document.querySelector('form').scrollIntoView()"
LATE_STATE = """() => Object.fromEntries(['candidate[answers_attributes][0][text]', 'first_name', 'last_name', 'email', 'phone']
  .map((name) => [name, (document.querySelector('[name="' + name + '"]') || {}).value ?? null]))"""


def test_the_runner_prepares_the_form_whose_question_renders_late(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, isolated_imx_home: LocalPaths
) -> None:
    url = server.url(LATE)
    answers = {**LATE_ANSWERS, LINKEDIN: "https://www.linkedin.example.test/in/avery-quill"}
    forms = kit.run(_forms(options, url, SCROLL_TO_FORM))
    assert LINKEDIN in [f.id for f in forms[-1].fields]
    _write_profile(isolated_imx_home, forms, answers)
    _, names, state = _prepare(kit, isolated_imx_home, url, LATE_STATE)
    assert names.count("packet.saved") == 2, names  # resolved again once the question rendered
    assert state[LINKEDIN] == answers[LINKEDIN]
    assert state["first_name"] == "Avery" and state["email"] == "avery.quill@example.test"
    assert server.submissions("teamtailor-late")["accepted_count"] == 0
