"""An embedded CAPTCHA widget (invisible badge + token field) is only needed when the
form is submitted: the form is inspected, filled and prepared as usual and reported
as ``captcha_pending``; only a real submit waits for the user. Real headless Chromium
against the localhost mock; a text CAPTCHA challenge still blocks as before.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_cli.runner import (
    LocalApplicationRunner,
    NoninteractiveInteraction,
    pending_inputs,
)
from interviewmaxxing_core import (
    ApplicationForm,
    ApplicationState,
    ApplicationStore,
    BrowserOptions,
    LocalPaths,
    MissingReason,
    NotSubmittedNext,
    PageKind,
    SubmissionOutcome,
)

REPO = Path(__file__).resolve().parents[2]
RESUME_PATH = REPO / "tests" / "fixtures" / "browser" / "resume_avery_quill.pdf"
WIDGET = "/jobs/captcha-widget/apply"
TOKEN = "textarea[name='g-recaptcha-response']"
VERIFIED_AT = "2026-09-01T12:00:00Z"
STANDARD_IDS = [
    "first_name", "last_name", "email", "phone", "linkedin_url", "resume",
    "work_authorization", "years_experience", "sponsorship", "skills",
    "work_arrangements", "open_to_relocation", "why_brambleway",
]


def test_text_challenge_still_blocks_as_a_captcha_page(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            return await browser.open(server.url("/jobs/captcha/apply"))
        finally:
            await browser.close()

    inspection = kit.run(scenario())
    assert inspection.kind is PageKind.CAPTCHA and inspection.form is None
    assert inspection.captcha_pending is False


def test_widget_form_is_filled_and_prepared_with_the_captcha_pending(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)  # preparation: no submission
        try:
            page = await browser.open(server.url(WIDGET))
            assert page.kind is PageKind.APPLICATION_FORM, page.message
            form = page.form
            fill = await browser.fill(form, kit.build(form, {**kit.CORE, **kit.STANDARD}).packet)
            review = await browser.prepare_review()
            submit = await browser.submit()
            return page, fill, review, submit
        finally:
            await browser.close()

    page, fill, review, submit = kit.run(scenario())
    assert page.captcha_pending is True
    assert [f.id for f in page.form.fields] == STANDARD_IDS  # the token field is not a question
    assert page.form.is_final_step is True and page.form.submit_selector
    assert fill.ok, [f for f in fill.fields if f.detail]
    assert review.kind is PageKind.APPLICATION_FORM and review.captcha_pending is True
    assert review.form.is_final_step is True
    assert review.form.page_errors == []  # the hidden token field is not a validation error
    assert not any(f.validation_error for f in review.form.fields)
    assert review.evidence
    assert not submit.dispatched  # preparation never submits
    assert server.submissions("captcha-widget")["accepted_count"] == 0


def test_submission_waits_for_the_user_to_solve_the_widget(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await PlaywrightSessionFactory().start(replace(options, allow_submission=True))
        try:
            page = await browser.open(server.url(WIDGET))
            form = page.form
            assert (await browser.fill(form, kit.build(form, {**kit.CORE, **kit.STANDARD}).packet)).ok
            refused = await browser.submit()
            observation = await browser.confirm()
            before = server.submissions("captcha-widget")["accepted_count"]
            # The person solves the CAPTCHA: the widget writes its token into the page.
            waiter = asyncio.create_task(browser.wait_for_user("solve the CAPTCHA", timeout_s=10))
            await asyncio.sleep(0.6)
            await browser.page.evaluate(
                "(sel) => { document.querySelector(sel).value = 'fixture-token'; }", TOKEN
            )
            solved = await waiter
            dispatched = await browser.submit()
            return refused, observation, before, solved, dispatched, await browser.confirm()
        finally:
            await browser.close()

    refused, observation, before, solved, dispatched, final = kit.run(scenario())
    assert not refused.dispatched and "CAPTCHA" in (refused.detail or "")
    assert observation.outcome is SubmissionOutcome.NOT_SUBMITTED
    assert observation.next_state is NotSubmittedNext.NEEDS_INPUT
    assert before == 0
    assert solved.kind is PageKind.APPLICATION_FORM and solved.captcha_pending is False
    assert dispatched.dispatched
    assert final.outcome is SubmissionOutcome.ACCEPTED
    summary = server.submissions("captcha-widget")
    assert summary["accepted_count"] == 1
    [record] = summary["submissions"]
    assert "g-recaptcha-response" not in record["extra_fields"]
    assert record["fields"]["first_name"] == "Avery"


# --- the real runner over the real store, browser and mock -----------------------------


async def _inspect_form(options: BrowserOptions, url: str) -> ApplicationForm:
    browser = await PlaywrightSessionFactory().start(options)
    try:
        page = await browser.open(url)
        assert page.form is not None, page.message
        return page.form
    finally:
        await browser.close()


def _write_profile(paths: LocalPaths, form: ApplicationForm, job_url: str) -> None:
    """The fictional candidate (Avery Quill) with saved answers for exactly the
    questions this form asks, worded as the form renders them (the way an earlier
    run would have saved them). Nothing here is real."""
    directory = paths.profile_dir / "default"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RESUME_PATH, directory / "resume.pdf")

    def saved(answer_id: str, field_id: str, value: Any, semantic_type: str | None = None,
              **scope: Any) -> dict[str, Any]:
        return {
            "id": answer_id, "scope": scope.pop("scope", "GLOBAL"), "semantic_type": semantic_type,
            "question": form.field(field_id).question_text, "value": value,
            "confirmed_at": VERIFIED_AT, **scope,
        }

    profile = {
        "id": "default",
        "identity": {
            "first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
            "phone": "+1 (303) 555-0142",
            "linkedin_url": "https://www.linkedin.example.test/in/avery-quill",
            "address": {"city": "Denver", "region": "CO", "country": "United States"},
            "verified_at": VERIFIED_AT,
        },
        "resume": {"id": "resume_supplied", "path": "resume.pdf"},
        "facts": [{"id": "fact.years", "key": "years_professional_experience", "value": 7,
                   "source": "user",
                   "verification": {"status": "VERIFIED", "method": "USER_STATED",
                                    "verified_at": VERIFIED_AT}}],
        "saved_answers": [
            saved("sa.work_auth", "work_authorization", "Yes, I am authorized to work in the US",
                  "WORK_AUTHORIZATION"),
            saved("sa.sponsorship", "sponsorship", "No, I will not require sponsorship",
                  "SPONSORSHIP"),
            saved("sa.years", "years_experience", "6 to 9 years"),
            saved("sa.skills", "skills", ["Python", "SQL", "Apache Spark", "dbt"]),
            saved("sa.why", "why_brambleway", "I have built data platforms for seven years and "
                  "want to work on logistics forecasting.", scope="JOB", job_url=job_url,
                  employer="Brambleway Analytics"),
        ],
    }
    (directory / "profile.json").write_text(json.dumps(profile, indent=2))


def _runner(paths: LocalPaths, *, prepare_only: bool) -> LocalApplicationRunner:
    return LocalApplicationRunner(paths=paths, interaction=NoninteractiveInteraction(),
                                  headless=True, prepare_only=prepare_only,
                                  submit_unapproved=not prepare_only)


def test_runner_prepares_the_widget_form_and_notes_the_pending_captcha(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, isolated_imx_home: LocalPaths
) -> None:
    url = server.url(WIDGET)
    _write_profile(isolated_imx_home, kit.run(_inspect_form(options, url)), url)
    result = kit.run(_runner(isolated_imx_home, prepare_only=True).apply(url, candidate_id="default"))
    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    assert "Prepared to the final review step" in result.message, result.message
    assert "A CAPTCHA on this form must be solved in the browser before it can be submitted." in result.message
    assert result.missing_inputs == []
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        [ready] = [e for e in store.list_events(result.application_id) if e.event == "preparation.ready"]
        assert ready.metadata["captcha_pending"] is True and ready.metadata["submitted"] is False
        assert store.list_attempts(result.application_id) == []
    assert server.submissions("captcha-widget")["accepted_count"] == 0


def test_runner_submission_stops_for_the_captcha_as_a_user_action(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, isolated_imx_home: LocalPaths
) -> None:
    url = server.url(WIDGET)
    _write_profile(isolated_imx_home, kit.run(_inspect_form(options, url)), url)
    result = kit.run(_runner(isolated_imx_home, prepare_only=False).apply(url, candidate_id="default"))
    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    [need] = result.missing_inputs
    assert (need.reason, need.label, need.field_id) == (MissingReason.USER_ACTION, "Solve the CAPTCHA", None)
    assert "CAPTCHA" in need.prompt
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert [n.label for n in pending_inputs(store, result.application_id)] == ["Solve the CAPTCHA"]
        # The browser refused to dispatch, so the one attempt is definitely not submitted.
        assert [a.outcome for a in store.list_attempts(result.application_id)] == [
            SubmissionOutcome.NOT_SUBMITTED
        ]
    assert server.submissions("captcha-widget")["accepted_count"] == 0
