"""Round 12 addendum: four live fill failures of retry five, reproduced on fictional pages.

A. Lever: the resume upload finished ("Success!" and the file's name), but the "Apply with
   LinkedIn" helper in the same form stayed "Loading..." and the upload was reported still
   in progress. Progress now counts only in the upload's own field, never a marker that
   already outlasted a bounded wait, and an uploader's own progress is waited for longer.
B. BambooHR: answering work authorization made the sponsorship question required (its
   asterisk); nothing appeared, so the round-11 reveal path did not apply and the fill
   failed as a changed page. Requiredness a choice changes now re-inspects too, and a
   changed page names what changed.
C. Greenhouse (embedded): a fixed cookie dialog ("I do not accept" / "I accept") covered
   the checkbox group, so both clicks timed out. The dialog is declined; a click that
   something else takes is dispatched to the checkbox itself.
D. Greenhouse: the site formats the phone number as it is typed. Phone readback compares
   digits only.

Real headless Chromium, fictional data, temp IMX_HOME; nothing is submitted.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import time
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
CONTACT = {"first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test"}


async def _serve(options: BrowserOptions, body: str, **factory: Any) -> Any:
    """A browser on ``https://example.test/apply`` answered with ``body`` locally."""
    browser = await PlaywrightSessionFactory(**factory).start(options)
    await browser.page.route("https://example.test/apply", lambda route: route.fulfill(
        content_type="text/html; charset=utf-8", body=body))
    return browser


def _status(result: Any, field_id: str) -> Any:
    return next(f for f in result.fields if f.field_id == field_id)


# --- A. the upload's own progress, not a helper's ----------------------------------------

LEVER = "/jobs/linkedin-autofill/apply?loading_ms=600000&prompt=none"


def test_a_helper_stuck_loading_elsewhere_in_the_form_is_not_the_uploads_progress(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """Lever: "Apply with LinkedIn" never finishes loading; the resume is still attached,
    read back and reported filled at once."""
    async def scenario() -> tuple[Any, float]:
        browser = await PlaywrightSessionFactory(settle_timeout_s=3.0).start(options)
        try:
            form = (await browser.open(server.url(LEVER))).form
            started = time.monotonic()
            result = await browser.fill(form, kit.build(form, {**kit.pick(form, kit.CORE), "resume": kit.RESUME}).packet)
            return result, time.monotonic() - started
        finally:
            await browser.close()

    result, took = kit.run(scenario())
    resume = _status(result, "resume")
    assert resume.status is FieldFillStatus.FILLED, resume
    assert result.ok, result.fields
    assert took < 20, took


STUCK_IN_FIELD = """<!doctype html><title>Apply</title><h1>Fictional role</h1>
<form method="post" action="/apply">
<div class="field"><label for="resume">Resume/CV</label><input type="file" id="resume" name="resume">
<button type="button" id="awli" aria-busy="true">Loading…</button></div>
<div class="field"><label for="name">Full name</label><input id="name" name="full_name" required></div>
<button type="submit">Submit application</button></form>"""


def test_a_marker_that_outlasted_a_bounded_wait_is_not_waited_for_again_by_an_upload(
    kit: SimpleNamespace, options: BrowserOptions
) -> None:
    """The helper sits in the resume's own field and never stops loading: opening the page
    waited it out once (bounded), so the upload does not wait for it again."""
    async def scenario() -> Any:
        browser = await _serve(options, STUCK_IN_FIELD, settle_timeout_s=2.0)
        try:
            form = (await browser.open("https://example.test/apply")).form
            return await browser.fill(form, kit.build(form, {"resume": kit.RESUME, "full_name": "Avery Quill"}).packet)
        finally:
            await browser.close()

    result = kit.run(scenario())
    assert _status(result, "resume").status is FieldFillStatus.FILLED, result.fields
    assert result.ok


SLOW_UPLOADER = """<!doctype html><title>Apply</title><h1>Fictional role</h1>
<form method="post" action="/apply">
<div class="field"><label for="resume">Resume/CV</label><input type="file" id="resume" name="resume">
<span id="status"></span></div>
<div class="field"><label for="name">Full name</label><input id="name" name="full_name" required></div>
<button type="submit">Submit application</button></form>
<script>
document.getElementById("resume").addEventListener("change", (e) => {
  const status = document.getElementById("status");
  status.textContent = "Uploading resume...";
  setTimeout(() => { status.textContent = "Success! " + e.target.files[0].name; }, {ms});
});
</script>"""


@pytest.mark.parametrize(("upload_ms", "status"), [
    (4000, FieldFillStatus.FILLED),
], ids=["longer-than-the-settle-bound"])
def test_an_uploaders_own_progress_is_waited_for_beyond_the_settle_bound(
    upload_ms: int, status: FieldFillStatus, kit: SimpleNamespace, options: BrowserOptions
) -> None:
    """The uploader says "Uploading resume..." in its own field for 4 s, longer than this
    session's 2 s settle bound (the old limit of the whole wait)."""
    async def scenario() -> Any:
        browser = await _serve(options, SLOW_UPLOADER.replace("{ms}", str(upload_ms)), settle_timeout_s=2.0)
        try:
            form = (await browser.open("https://example.test/apply")).form
            return await browser.fill(form, kit.build(form, {"resume": kit.RESUME, "full_name": "Avery Quill"}).packet)
        finally:
            await browser.close()

    result = kit.run(scenario())
    assert _status(result, "resume").status is status, result.fields


# --- B. a choice that makes another question required ------------------------------------

REQUIRED = "/jobs/bamboohr-required/apply"
AUTH = "customQuestionAnswers.yes_no_2101"
SPONSOR = "customQuestionAnswers.yes_no_2102"
AUSTIN = "customQuestionAnswers.yes_no_2103"
ONSITE = "customQuestionAnswers.yes_no_2104"
YES_NO = {**CONTACT, AUTH: "Yes", SPONSOR: "No", AUSTIN: "Yes", ONSITE: "Yes"}
RADIOS = f"""() => Object.fromEntries({json.dumps([AUTH, SPONSOR, AUSTIN, ONSITE])}.map((name) =>
  [name, (document.querySelector('input[name="' + name + '"]:checked') || {{}}).value || null]))"""
REWORD_AUSTIN = f"""() => document.querySelectorAll('input[name="{AUTH}"]').forEach((r) =>
  r.addEventListener('change', () => {{
    document.querySelector('[id="f-{AUSTIN}"] legend').textContent =
      'Are you willing to relocate to Austin at your own expense?';
  }}))"""


def test_a_choice_that_makes_a_question_required_is_inspected_again_not_failed(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> None:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            form = (await browser.open(server.url(REQUIRED))).form
            assert not form.field(SPONSOR).required
            result = await browser.fill(form, kit.build(form, YES_NO).packet)
            assert result.failed_field_ids() == [], result.fields
            [error] = result.page_errors
            assert "1 question(s) ('Will you now or will you in the future require" in error, error
            assert "became required after the answer to 'Are you legally authorized" in error, error
            assert "inspect this step and resolve it again" in error
            assert "changed while filling" not in error
            # Authorization stays answered; nothing after it was written.
            assert _status(result, AUTH).status is FieldFillStatus.FILLED
            assert {f.field_id for f in result.fields}.isdisjoint({SPONSOR, AUSTIN, ONSITE})
            again = (await browser.inspect()).form
            assert again.field(SPONSOR).required
            assert again.field(SPONSOR).fingerprint == form.field(SPONSOR).fingerprint
            second = await browser.fill(again, kit.build(again, YES_NO).packet)
            assert second.ok, (second.fields, second.page_errors)
            assert await browser.page.evaluate(RADIOS) == {AUTH: "Yes", SPONSOR: "No", AUSTIN: "Yes", ONSITE: "Yes"}
        finally:
            await browser.close()

    kit.run(scenario())
    assert server.submissions("bamboohr-required")["accepted_count"] == 0


def test_a_reworded_question_still_stops_and_the_failure_names_what_changed(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            form = (await browser.open(server.url(REQUIRED))).form
            await browser.page.evaluate(REWORD_AUSTIN)
            return await browser.fill(form, kit.build(form, YES_NO).packet)
        finally:
            await browser.close()

    result = kit.run(scenario())
    assert result.failed_field_ids() == [SPONSOR, AUSTIN, ONSITE], result.fields
    detail = _status(result, SPONSOR).detail or ""
    assert "questions, bindings, actions, or employer context changed while filling (" in detail, detail
    # Positions and field ids with the kind of change: no wording, no value.
    assert f"#5 {SPONSOR} (required)" in detail and f"#6 {AUSTIN} (wording)" in detail, detail
    assert "relocate" not in detail


async def _inspected(options: BrowserOptions, url: str) -> ApplicationForm:
    browser = await PlaywrightSessionFactory().start(options)
    try:
        form = (await browser.open(url)).form
        assert form is not None
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
        return {"id": f"sa.{field_id[-4:]}", "scope": "GLOBAL", "semantic_type": semantic,
                "question": field.question_text, "value": value, "confirmed_at": VERIFIED_AT}

    profile = {
        "id": "default",
        "identity": {**CONTACT, "phone": "+1 (303) 555-0142", "verified_at": VERIFIED_AT},
        "resume": {"id": "resume_supplied", "path": "resume.pdf"},
        "facts": [],
        "saved_answers": [saved(field_id, YES_NO[field_id]) for field_id in (AUTH, SPONSOR, AUSTIN, ONSITE)],
    }
    (directory / "profile.json").write_text(json.dumps(profile, indent=2))


class RecordingFactory:
    """The Playwright factory, recording the radios as each browser closes."""

    def __init__(self) -> None:
        self.states: list[dict[str, Any]] = []

    async def start(self, options: BrowserOptions) -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        close = browser.close

        async def close_and_record() -> None:
            with contextlib.suppress(Exception):
                self.states.append(await browser.page.evaluate(RADIOS))
            await close()

        browser.close = close_and_record
        return browser


def test_the_runner_prepares_the_form_whose_choice_makes_a_question_required(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, isolated_imx_home: LocalPaths
) -> None:
    url = server.url(REQUIRED)
    _write_profile(isolated_imx_home, kit.run(_inspected(options, url)))
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
    assert names.count("packet.saved") == 2, names  # resolved again once the question was required
    assert "preparation.ready" in names
    failures = [e for e in events if e.event.startswith("application.") and "FAILED" in json.dumps(e.metadata)]
    assert failures == [], failures
    answers = {a.field_id: a.value for a in packet.answers}
    assert answers[SPONSOR] == ChoiceValue(value="No", label="No")
    assert factory.states[-1] == {AUTH: "Yes", SPONSOR: "No", AUSTIN: "Yes", ONSITE: "Yes"}
    assert server.submissions("bamboohr-required")["accepted_count"] == 0


# --- C. a fixed cookie dialog over a checkbox group ---------------------------------------

LONG_FORM = """<!doctype html><title>Apply</title><h1>Fictional role</h1>
<style>.spacer{height:1600px}</style>
<form method="post" action="/apply">
<label for="n">Full name</label><input id="n" name="full_name" required>
<div class="spacer"></div>
<fieldset><legend>Location Preference</legend>
<input type="checkbox" id="loc-0" name="location_preference" value="austin"><label for="loc-0">Austin, TX</label>
<input type="checkbox" id="loc-1" name="location_preference" value="remote"><label for="loc-1">Remote</label>
</fieldset>
<div class="spacer"></div>
<button type="submit">Submit application</button></form>
{overlay}
<script>window.__clicked = [];
document.addEventListener("click", (e) => {
  const box = e.target.closest && e.target.closest("#cookie, #panel");
  if (!box || !e.target.id) return;
  window.__clicked.push(e.target.id);
  if (box.id === "cookie" && e.target.id !== "manage") box.remove();
});</script>"""
COOKIE_DIALOG = """<div id="cookie" role="dialog" aria-label="Cookie consent"
 style="position:fixed;left:15%;right:15%;top:30%;height:40%;z-index:10;background:#fff;border:1px solid #333">
<p>This website uses cookies. You consent to our cookies if you click "I Accept".</p>
<button type="button" id="manage">Manage cookies</button>
<button type="button" id="decline">I do not accept</button>
<button type="button" id="accept">I accept</button></div>"""
CHAT_PANEL = """<div id="panel" style="position:fixed;inset:0;z-index:10;background:rgba(255,255,255,.6)">
<p>Questions? Chat with our recruiting team.</p><button type="button" id="chat">Start chat</button></div>"""
LOCATION = {"full_name": "Avery Quill", "location_preference": ["Austin, TX"]}
CHECKED = "() => Array.from(document.querySelectorAll('input[name=location_preference]:checked'), (i) => i.value)"


def test_a_cookie_dialog_over_the_form_is_declined_and_the_checkbox_is_clicked(
    kit: SimpleNamespace, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, list[str], list[str]]:
        browser = await _serve(options, LONG_FORM.replace("{overlay}", COOKIE_DIALOG), action_timeout_s=2.0)
        try:
            form = (await browser.open("https://example.test/apply")).form
            result = await browser.fill(form, kit.build(form, LOCATION).packet)
            return result, await browser.page.evaluate(CHECKED), await browser.page.evaluate("() => window.__clicked")
        finally:
            await browser.close()

    result, checked, clicked = kit.run(scenario())
    assert result.ok, result.fields
    assert checked == ["austin"]
    assert clicked == ["decline"]  # never "I accept"


def test_a_checkbox_under_a_panel_that_takes_the_pointer_is_checked_on_the_input_itself(
    kit: SimpleNamespace, options: BrowserOptions
) -> None:
    """A panel that is not a cookie banner covers the page: nothing on it is clicked; the
    click is dispatched to the checkbox and read back."""
    async def scenario() -> tuple[Any, list[str], list[str]]:
        browser = await _serve(options, LONG_FORM.replace("{overlay}", CHAT_PANEL), action_timeout_s=1.5)
        try:
            form = (await browser.open("https://example.test/apply")).form
            result = await browser.fill(form, kit.build(form, LOCATION).packet)
            return result, await browser.page.evaluate(CHECKED), await browser.page.evaluate("() => window.__clicked")
        finally:
            await browser.close()

    result, checked, clicked = kit.run(scenario())
    assert _status(result, "location_preference").status is FieldFillStatus.FILLED, result.fields
    assert checked == ["austin"]
    assert clicked == []


# --- D. a phone number the site formats ---------------------------------------------------

PHONE_FORM = """<!doctype html><title>Apply</title><h1>Fictional role</h1>
<form method="post" action="/apply">
<label for="phone">Phone Number</label><input type="tel" id="phone" name="phone" autocomplete="tel" required>
<button type="submit">Submit application</button></form>
<script>
document.getElementById("phone").addEventListener("input", (e) => {
  let d = e.target.value.replace(/\\D/g, "");
  if (d.length === 11 && d[0] === "1") d = d.slice(1);
  d = d.slice(0, {keep});
  e.target.value = d.length === 10 ? "(" + d.slice(0, 3) + ") " + d.slice(3, 6) + "-" + d.slice(6) : d;
});
</script>"""


@pytest.mark.parametrize(("keep", "status", "shown"), [
    (10, FieldFillStatus.FILLED, "(303) 555-0142"),
    # Typed again key by key after the mismatch: the site keeps the first seven digits.
    (7, FieldFillStatus.VERIFICATION_MISMATCH, "1303555"),
], ids=["formatted", "digits-lost"])
def test_a_phone_number_reads_back_by_its_digits(
    keep: int, status: FieldFillStatus, shown: str, kit: SimpleNamespace, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any]:
        browser = await _serve(options, PHONE_FORM.replace("{keep}", str(keep)))
        try:
            form = (await browser.open("https://example.test/apply")).form
            assert form.field("phone").semantic_type is SemanticType.PHONE
            result = await browser.fill(form, kit.build(form, {"phone": "+1 303 555 0142"}).packet)
            return result, await browser.page.input_value("#phone")
        finally:
            await browser.close()

    result, value = kit.run(scenario())
    phone = _status(result, "phone")
    assert phone.status is status, phone
    assert value == shown
    if status is FieldFillStatus.FILLED:
        assert phone.detail == f"the site formats it as {shown!r}"
