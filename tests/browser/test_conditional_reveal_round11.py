"""Round 11: a choice that reveals follow-up questions makes the fill re-inspect, not fail.

BambooHR shows conditional fields the moment a Yes/No is chosen. The fill guard used to
read the additional questions as a lost context and fail the field being written and
the run (``FAILED_RETRYABLE``). Now, when only *additional* questions appeared after a
choice (no existing question reworded or removed, actions and employer context as
approved), the fill stops with a re-inspect page error, nothing is reported failed, the
answered choice stays answered, and the runner inspects and resolves the step again.
Everything else still stops as before.

Round 13: the fill no longer stops at the reveal. The approved questions are all still
there, so their answers are written (the Austin question too); the follow-ups are then
reported as not yet attempted and the step is inspected and resolved again. Questions that
appear after a typed answer are treated the same (Teamtailor renders one late), unless
they arrive already answered.

Real headless Chromium against the local mock ATS (``bamboohr-conditional``); nothing
is submitted."""

from __future__ import annotations

import contextlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_cli.runner import LocalApplicationRunner, NoninteractiveInteraction
from interviewmaxxing_core import (
    ApplicationForm,
    ApplicationState,
    ApplicationStore,
    BrowserOptions,
    ChoiceValue,
    FieldFillStatus,
    LocalPaths,
    SemanticType,
)

REPO = Path(__file__).resolve().parents[2]
RESUME_PATH = REPO / "tests" / "fixtures" / "browser" / "resume_avery_quill.pdf"
VERIFIED_AT = "2026-09-01T12:00:00Z"
APPLY = "/jobs/bamboohr-conditional/apply"
INITIAL = ["first_name", "last_name", "email", "sponsorship", "located_austin"]
REVEALED = ["first_name", "last_name", "email", "sponsorship", "authorization_basis",
            "authorization_proof", "located_austin"]
FIRST = {"first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
         "sponsorship": "No", "located_austin": "Yes"}
FOLLOW_UPS = {"authorization_basis": "U.S. citizen or national", "authorization_proof": "Yes"}
PAGE_STATE = """() => ({
  sponsorship: (document.querySelector('input[name=sponsorship]:checked') || {}).value || null,
  located: (document.querySelector('input[name=located_austin]:checked') || {}).value || null,
  basis: (document.querySelector('#f-authorization_basis') || {}).value || null,
  proof: (document.querySelector('input[name=authorization_proof]:checked') || {}).value || null,
  reveals: (window.__mock || {}).reveals || 0,
})"""
REWORD_LOCATED = """() => document.querySelectorAll('input[name=sponsorship]').forEach((r) =>
  r.addEventListener('change', () => {
    document.querySelector('#f-located_austin legend').textContent =
      'Are you willing to relocate to Austin, Texas at your own expense?';
  }))"""
CHANGE_ACTION = """() => document.querySelectorAll('input[name=sponsorship]').forEach((r) =>
  r.addEventListener('change', () => {
    document.querySelector('form button[type=submit]').textContent = 'Continue to payment';
  }))"""


async def _open(options: BrowserOptions, url: str) -> Any:
    browser = await PlaywrightSessionFactory().start(replace(options, allow_submission=False))
    page = await browser.open(url)
    assert page.form is not None, page.message
    return browser


def _statuses(result: Any) -> dict[str, FieldFillStatus]:
    return {f.field_id: f.status for f in result.fields}


@pytest.mark.parametrize("query", ["", "?reveal_ms=400"], ids=["synchronous", "delayed"])
def test_a_choice_that_reveals_follow_up_questions_halts_for_re_inspection_and_keeps_the_choice(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, query: str
) -> None:
    async def scenario() -> None:
        browser = await _open(options, server.url(APPLY + query))
        try:
            form = (await browser.inspect()).form
            assert [f.id for f in form.fields] == INITIAL
            first = await browser.fill(form, kit.build(form, FIRST).packet)

            # Nothing is reported failed and every approved answer is written. A synchronous
            # reveal is met by the next write's freshness check; the follow-ups are reported
            # as not yet attempted and the step asks to be re-inspected. One that lands a
            # moment later is met there, by the readback of the step, or only by the
            # inspection that follows; never a failure.
            assert first.failed_field_ids() == [], first.fields
            statuses = _statuses(first)
            assert all(statuses[f] is FieldFillStatus.FILLED for f in INITIAL), statuses
            assert all(statuses[f] is FieldFillStatus.SKIPPED for f in FOLLOW_UPS if f in statuses)
            for error in first.page_errors:
                assert "2 question(s) appeared" in error and "after the answer to" in error
                assert "inspect this step and resolve it again" in error
                assert "changed while filling" not in error
            if not query:
                assert statuses == {**dict.fromkeys(INITIAL, FieldFillStatus.FILLED),
                                    **dict.fromkeys(FOLLOW_UPS, FieldFillStatus.SKIPPED)}
                [error] = first.page_errors
                assert "after the answer to 'Will you now or will you in the future require employment" in error
                assert (await browser.page.evaluate(PAGE_STATE)) == {
                    "sponsorship": "no", "located": "yes", "basis": None, "proof": None, "reveals": 1}
                assert browser._active_fill_signature is None
                with pytest.raises(ValueError, match="inspect it again first"):
                    await browser.fill(form, kit.build(form, FIRST).packet)

            # Re-inspected: the follow-ups are questions now, the choice is still answered.
            await browser.page.wait_for_function("() => (window.__mock || {}).reveals === 1")
            revealed = (await browser.inspect()).form
            assert [f.id for f in revealed.fields] == REVEALED
            assert revealed.find("sponsorship").fingerprint == form.find("sponsorship").fingerprint
            second = await browser.fill(revealed, kit.build(revealed, {**FIRST, **FOLLOW_UPS}).packet)
            assert second.ok, (second.fields, second.page_errors)
            assert set(_statuses(second)) == set(REVEALED)
            assert all(s is FieldFillStatus.FILLED for s in _statuses(second).values())
            assert await browser.page.evaluate(PAGE_STATE) == {
                "sponsorship": "no", "located": "yes", "basis": "citizen", "proof": "yes", "reveals": 1}
        finally:
            await browser.close()

    kit.run(scenario())
    assert server.submissions("bamboohr-conditional")["accepted_count"] == 0


def test_a_reveal_after_the_last_written_choice_is_reported_for_re_inspection_not_as_failures(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> None:
        browser = await _open(options, server.url(APPLY))
        try:
            form = (await browser.inspect()).form
            packet = kit.build(form, FIRST).packet
            # A selective fill that ends on the choice: the follow-ups appear after the
            # last write and are found by the readback of the whole step.
            result = await browser.fill_fields(form, packet, ["first_name", "last_name", "email", "sponsorship"])
            assert result.failed_field_ids() == [], result.fields
            statuses = _statuses(result)
            assert statuses["sponsorship"] is FieldFillStatus.FILLED
            assert statuses["authorization_basis"] is FieldFillStatus.SKIPPED
            assert statuses["authorization_proof"] is FieldFillStatus.SKIPPED
            [error] = result.page_errors
            assert "2 question(s) appeared" in error and "inspect this step and resolve it again" in error
            with pytest.raises(ValueError, match="inspect it again first"):
                await browser.fill(form, packet)
            revealed = (await browser.inspect()).form
            assert [f.id for f in revealed.fields] == REVEALED
            second = await browser.fill(revealed, kit.build(revealed, {**FIRST, **FOLLOW_UPS}).packet)
            assert second.ok, (second.fields, second.page_errors)
        finally:
            await browser.close()

    kit.run(scenario())


@pytest.mark.parametrize("mutation, expect_failed", [
    (REWORD_LOCATED, ["located_austin"]),
    (CHANGE_ACTION, ["located_austin"]),
], ids=["reworded question", "changed action"])
def test_a_reveal_that_also_rewords_a_question_or_changes_an_action_still_stops(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, mutation: str, expect_failed: list[str]
) -> None:
    async def scenario() -> None:
        browser = await _open(options, server.url(APPLY))
        try:
            form = (await browser.inspect()).form
            await browser.page.evaluate(mutation)
            result = await browser.fill(form, kit.build(form, FIRST).packet)
            assert result.failed_field_ids() == expect_failed, result.fields
            [failed] = [f for f in result.fields if f.field_id in expect_failed]
            assert "questions, bindings, actions, or employer context changed while filling" in (failed.detail or "")
            assert any("changed while filling" in e for e in result.page_errors)
            assert not any("follow-up" in e for e in result.page_errors)
            assert (await browser.page.evaluate(PAGE_STATE))["located"] is None
        finally:
            await browser.close()

    kit.run(scenario())


def test_questions_that_appear_after_a_typed_answer_are_inspected_again_too(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """Round 13 (Teamtailor renders a question late, whatever was written last): unanswered
    questions that appear after a typed value are follow-ups as well. A question that
    arrives already answered, or a changed existing question, still stops the fill
    (``test_between_fill_mutations.py``, ``test_review_c4r.py``)."""
    async def scenario() -> None:
        browser = await _open(options, server.url(APPLY))
        try:
            form = (await browser.inspect()).form
            await browser.page.evaluate("""() => document.querySelector('#f-email').addEventListener('input', () => {
              const t = document.querySelector('#f-sponsorship-reveals');
              t.parentNode.insertBefore(t.content.cloneNode(true), t.nextSibling);
            }, {once: true})""")
            result = await browser.fill(form, kit.build(form, FIRST).packet)
            assert result.failed_field_ids() == [], result.fields
            statuses = _statuses(result)
            assert all(statuses[f] is FieldFillStatus.FILLED for f in INITIAL), statuses
            assert all(statuses[f] is FieldFillStatus.SKIPPED for f in FOLLOW_UPS), statuses
            [error] = result.page_errors
            assert error.startswith("2 question(s) appeared (What is the basis of your current"), error
            assert "while filling; inspect this step and resolve it again" in error, error
        finally:
            await browser.close()

    kit.run(scenario())


# --- the runner -------------------------------------------------------------------------


async def _revealed_form(options: BrowserOptions, url: str) -> ApplicationForm:
    """The form with its follow-up questions shown (the choice made by hand)."""
    browser = await PlaywrightSessionFactory().start(options)
    try:
        page = await browser.open(url)
        assert page.form is not None, page.message
        await browser.page.locator("#f-sponsorship-1").check()
        form = (await browser.inspect()).form
        assert form is not None and [f.id for f in form.fields] == REVEALED
        return form
    finally:
        await browser.close()


def _write_profile(paths: LocalPaths, form: ApplicationForm) -> None:
    """The fictional candidate with saved answers worded as this form asks them."""
    directory = paths.profile_dir / "default"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RESUME_PATH, directory / "resume.pdf")

    def saved(field_id: str, value: str) -> dict[str, Any]:
        field = form.field(field_id)
        semantic = None if field.semantic_type is SemanticType.UNKNOWN else field.semantic_type.value
        return {"id": f"sa.{field_id}", "scope": "GLOBAL", "semantic_type": semantic,
                "question": field.question_text, "value": value, "confirmed_at": VERIFIED_AT}

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
        "facts": [],
        "saved_answers": [saved(field_id, value) for field_id, value in
                          {"sponsorship": "No", **FOLLOW_UPS, "located_austin": "Yes"}.items()],
    }
    (directory / "profile.json").write_text(json.dumps(profile, indent=2))


class RecordingFactory:
    """The Playwright factory, recording the page's answers as the run closes."""

    def __init__(self) -> None:
        self.states: list[dict[str, Any]] = []

    async def start(self, options: BrowserOptions) -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        close = browser.close

        async def close_and_record() -> None:
            with contextlib.suppress(Exception):
                self.states.append(await browser.page.evaluate(PAGE_STATE))
            await close()

        browser.close = close_and_record
        return browser


def test_the_runner_reaches_the_final_review_in_two_resolutions_with_every_answer_in_place(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, isolated_imx_home: LocalPaths
) -> None:
    url = server.url(APPLY)
    _write_profile(isolated_imx_home, kit.run(_revealed_form(options, url)))
    factory = RecordingFactory()
    runner = LocalApplicationRunner(paths=isolated_imx_home, interaction=NoninteractiveInteraction(),
                                    headless=True, browser_factory=factory, prepare_only=True)

    result = kit.run(runner.apply(url, candidate_id="default"))

    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    assert "Prepared to the final review step" in result.message, result.message
    assert result.missing_inputs == []
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        events = store.list_events(result.application_id)
        packet = store.latest_packet(result.application_id)
        assert packet is not None
        assert store.list_attempts(result.application_id) == []
    names = [e.event for e in events]
    # Resolved twice: against the initial questions, then against the revealed ones.
    assert names.count("packet.saved") == 2, names
    assert "preparation.ready" in names
    failures = [e for e in events if e.event.startswith("application.") and "FAILED" in json.dumps(e.metadata)]
    assert failures == [], failures
    answers = {a.field_id: a.value for a in packet.answers}
    assert set(answers) == set(REVEALED)
    assert answers["sponsorship"] == ChoiceValue(value="no", label="No")
    assert answers["authorization_basis"] == ChoiceValue(value="citizen", label="U.S. citizen or national")
    assert answers["authorization_proof"] == ChoiceValue(value="yes", label="Yes")
    assert answers["located_austin"] == ChoiceValue(value="yes", label="Yes")
    # The page holds every answer, the follow-ups mounted exactly once, nothing submitted.
    assert factory.states[-1] == {"sponsorship": "no", "located": "yes", "basis": "citizen",
                                  "proof": "yes", "reveals": 1}
    assert server.submissions("bamboohr-conditional")["accepted_count"] == 0
