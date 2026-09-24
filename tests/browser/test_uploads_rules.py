"""Upload controls, autofill helpers and busy overlays as the inspector and the rules see
them.

The pages are fictional, shaped like hosted application forms inspected read-only on
2026-09-24: a label wrapping the question, an "ATTACH RESUME/CV" link over an invisible
input and hidden parse status, next to a LinkedIn helper that reads "Loading..." first
(Lever); a group-labelled upload whose visually hidden label says only "Attach"
(Greenhouse); clip-hidden inputs behind "Upload File" buttons, one of them a separate
"Autofill from resume" uploader (Ashby); a react-dropzone-style hidden input.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_browser.annotations import observation_signature
from interviewmaxxing_browser.normalize import PageModel, build_page
from interviewmaxxing_browser.signals import (
    THIRD_PARTY_ASSIST,
    ButtonIntent,
    autofill_decline,
    button_intent,
)
from interviewmaxxing_browser.snapshot import DomSnapshot, inspector_script
from interviewmaxxing_browser.uploads import UploadState
from interviewmaxxing_core import BrowserOptions, ControlType, PageKind

HIDDEN = ("border:0;clip:rect(0,0,0,0);clip-path:inset(50%);height:1px;margin:0 -1px -1px 0;"
          "overflow:hidden;padding:0;position:absolute;width:1px;white-space:nowrap")

LEVER_LIKE = """<!doctype html><title>Apply: Fictional Analyst</title><h1>Fictional Analyst</h1>
<form id="application-form" method="post" action="/apply"><ul>
<li class="application-question awli-application-row"><div class="application-label">LinkedIn profile</div>
 <div class="application-field"><button type="button" class="awli-button state-loading" id="awli">
  <div class="loading">Loading...</div><div class="ready" style="display:none">Apply with LinkedIn</div>
 </button></div></li>
<li class="application-question resume"><label><div class="application-label">Resume/CV <span class="required">✱</span></div>
 <div class="application-field"><a href="#" class="visible-resume-upload" style="position:relative;display:inline-block;width:230px;height:40px">
  <span class="filename"></span><span class="default-label">ATTACH RESUME/CV</span>
  <input class="invisible-resume-upload" id="resume-upload-input" name="resume" tabindex="-1" type="file"
   style="position:absolute;left:0;top:0;opacity:0;width:230px;height:40px"></a>
  <span class="resume-upload-working" style="display:none"><div class="loading-indicator"></div><div class="resume-upload-label">Analyzing resume...</div></span>
  <span class="resume-upload-success" style="display:none"><div class="resume-upload-label">Success!</div></span>
 </div></label></li>
<li class="application-question"><label><div class="application-label">Full name<span class="required">✱</span></div>
 <div class="application-field"><input type="text" name="name" required></div></label></li>
<li class="application-question"><label><div class="application-label">Email<span class="required">✱</span></div>
 <div class="application-field"><input type="email" name="email" required></div></label></li>
</ul><button type="button" id="btn-submit">Submit application</button></form>"""

GREENHOUSE_LIKE = """<!doctype html><title>Job Application for Fictional Manager</title>
<style>.visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}</style>
<div class="application--header"><button type="button" class="btn btn--pill">Autofill my application</button></div>
<form id="application-form" method="post" action="/apply">
<div class="field-wrapper"><label for="first_name">First Name<span>*</span></label><input id="first_name" aria-required="true"></div>
<div class="field-wrapper"><div role="group" aria-labelledby="upload-label-resume" aria-required="true" class="file-upload">
 <div id="upload-label-resume" class="label upload-label">Resume/CV<span class="required">*</span></div>
 <div class="file-upload__wrapper"><div class="button-container">
  <div class="secondary-button"><div><button type="button" class="btn btn--pill">Attach</button>
   <label class="visually-hidden" for="resume">Attach</label>
   <input id="resume" class="visually-hidden" type="file" accept=".pdf,.doc,.docx,.txt,.rtf"></div></div>
  <div class="secondary-button"><button type="button" class="btn btn--pill" data-testid="resume-dropbox">Dropbox</button></div>
  <div class="secondary-button"><div><button type="button" class="btn btn--pill" data-testid="resume-text">Enter manually</button>
   <label class="visually-hidden" for="resume_text">Enter manually</label></div></div>
  <p class="file-upload__filetypes">Accepted file types: pdf, doc, docx, txt, rtf</p></div></div></div></div>
<div class="field-wrapper"><div role="group" aria-labelledby="upload-label-cover_letter" aria-required="false" class="file-upload">
 <div id="upload-label-cover_letter" class="label upload-label">Cover Letter</div>
 <div class="file-upload__wrapper"><div class="button-container">
  <div class="secondary-button"><div><button type="button" class="btn btn--pill">Attach</button>
   <label class="visually-hidden" for="cover_letter">Attach</label>
   <input id="cover_letter" class="visually-hidden" type="file" accept=".pdf,.doc,.docx,.txt,.rtf"></div></div>
  <p class="file-upload__filetypes">Accepted file types: pdf, doc, docx, txt, rtf</p></div></div></div></div>
<button type="submit">Submit application</button></form>"""

ASHBY_LIKE = f"""<!doctype html><title>Fictional Engineer @ Example Labs</title>
<div id="form" role="tabpanel">
 <div class="ashby-application-form-autofill-pane"><div role="presentation" class="ashby-application-form-autofill-input-root">
  <input type="file" tabindex="-1" style="{HIDDEN}">
  <div class="base-layer"><h3>Autofill from resume</h3>
   <p>Upload your resume here to autofill key application fields.</p><button>Upload file</button></div>
  <div class="pending-layer" id="pending" style="display:none"><span aria-label="Loading..." role="progressbar"></span>
   Parsing your resume. Autofilling key fields...</div></div></div>
 <div class="field-entry"><label for="_systemfield_name">Name</label><input id="_systemfield_name" name="_systemfield_name" required></div>
 <div class="field-entry"><label for="_systemfield_email">Email</label>
  <input id="_systemfield_email" name="_systemfield_email" type="email" required></div>
 <div class="field-entry"><label for="_systemfield_resume">Resume</label>
  <div role="presentation" class="ashby-application-form-input-file">
   <input type="file" tabindex="-1" id="_systemfield_resume" required style="{HIDDEN}">
   <div class="dropzone"><button class="dropzone-upload">Upload File</button>
    <p class="dropzone-instructions">or drag and drop here</p></div></div></div>
 <button class="submit-button">Submit Application</button></div>"""

DROPZONE_LIKE = """<!doctype html><title>Apply</title><form method="post" action="/apply">
<div class="field"><label for="full_name">Full name</label><input id="full_name" name="full_name" required></div>
<div class="field"><div class="question">CV *</div>
 <div class="dropzone" role="presentation" tabindex="0"><input type="file" name="candidate_cv" style="display:none">
  <p>Drag 'n' drop your CV here, or click to select a file</p></div></div>
<div class="field"><div class="question">Portfolio</div><input type="file" name="portfolio" style="display:none"></div>
<button type="submit">Submit application</button></form>"""


async def _read(page: Any) -> PageModel:
    return build_page(DomSnapshot.model_validate(await page.evaluate(inspector_script())))


def _serve(kit: SimpleNamespace, server: Any, options: BrowserOptions, html: str,
           check: Any) -> None:
    async def scenario() -> None:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.page.route("**/fixture-upload", lambda route: route.fulfill(
                content_type="text/html; charset=utf-8", body=html))
            await browser.page.goto(server.url("/fixture-upload"))
            await check(browser.page)
        finally:
            await browser.close()

    kit.run(scenario())


def test_lever_like_upload_label_leaves_out_trigger_filename_and_status(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def check(page: Any) -> None:
        model = await _read(page)
        assert model.form is not None
        resume = model.form.field("resume")
        assert resume.control_type is ControlType.FILE
        assert resume.label == "Resume/CV" and resume.required is True
        assert "ATTACH" not in (resume.help_text or "")
        before = resume.fingerprint
        # The site shows the chosen file's name and its parse progress inside the label.
        await page.evaluate("""() => {
            document.querySelector('.filename').textContent = 'avery_quill_resume.pdf';
            document.querySelector('.resume-upload-working').style.display = 'inline'; }""")
        parsing = await _read(page)
        assert parsing.form is not None and parsing.form.field("resume").fingerprint == before
        assert any("Analyzing resume" in b for b in parsing.snapshot.busy)
        await page.evaluate("""() => {
            document.querySelector('.resume-upload-working').style.display = 'none';
            document.querySelector('.resume-upload-success').style.display = 'inline'; }""")
        done = await _read(page)
        assert done.form is not None and done.form.field("resume").fingerprint == before
        assert not any("Analyzing" in b for b in done.snapshot.busy)
    _serve(kit, server, options, LEVER_LIKE, check)


def test_lever_like_linkedin_helper_is_never_the_submit_action_or_structure(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def check(page: Any) -> None:
        loading = await _read(page)
        assert loading.form is not None
        assert loading.form.is_final_step is True and loading.form.submit_selector == "#btn-submit"
        assert any("Loading..." in b for b in loading.snapshot.busy)
        await page.evaluate("""() => {
            document.querySelector('.loading').style.display = 'none';
            document.querySelector('.ready').style.display = 'block'; }""")
        ready = await _read(page)
        assert ready.form is not None
        assert [b.button.text for b in ready.buttons if b.button.selector == "#awli"] == ["Apply with LinkedIn"]
        assert ready.form.submit_selector == "#btn-submit"
        assert all(b.intent is ButtonIntent.OTHER for b in ready.buttons if b.button.selector == "#awli")
        assert ready.snapshot.busy == []
        # The helper flipping from "Loading..." is not a change of the form's structure.
        assert observation_signature(ready) == observation_signature(loading)
    _serve(kit, server, options, LEVER_LIKE, check)


def test_greenhouse_like_upload_takes_the_group_question_not_the_attach_label(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def check(page: Any) -> None:
        model = await _read(page)
        assert model.form is not None and model.inspection.kind is PageKind.APPLICATION_FORM
        resume, cover = model.form.field("resume"), model.form.field("cover_letter")
        assert (resume.control_type, resume.label, resume.required) == (ControlType.FILE, "Resume/CV", True)
        assert resume.help_text == "Accepted file types: pdf, doc, docx, txt, rtf"
        assert (cover.control_type, cover.label, cover.required) == (ControlType.FILE, "Cover Letter", False)
        autofill = [b for b in model.snapshot.buttons if b.text == "Autofill my application"]
        assert len(autofill) == 1 and THIRD_PARTY_ASSIST.search(autofill[0].text)
    _serve(kit, server, options, GREENHOUSE_LIKE, check)


def test_ashby_like_hidden_inputs_behind_upload_buttons_are_upload_fields(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def check(page: Any) -> None:
        model = await _read(page)
        assert model.form is not None
        resume = model.form.field("_systemfield_resume")
        assert (resume.control_type, resume.label, resume.required) == (ControlType.FILE, "Resume", True)
        assert resume.help_text == "or drag and drop here"
        [parser] = [f for f in model.form.fields
                    if f.control_type is ControlType.FILE and f.id != "_systemfield_resume"]
        assert parser.required is False
        assert parser.help_text == "Upload your resume here to autofill key application fields."
        assert model.snapshot.busy == []
        await page.evaluate("() => { document.getElementById('pending').style.display = 'block'; }")
        parsing = await _read(page)
        assert parsing.snapshot.busy, "the parse spinner and its text are work in progress"
    _serve(kit, server, options, ASHBY_LIKE, check)


def test_hidden_input_is_an_upload_field_only_with_a_visible_trigger(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def check(page: Any) -> None:
        model = await _read(page)
        assert model.form is not None
        ids = [f.id for f in model.form.fields]
        assert "candidate_cv" in ids and "portfolio" not in ids
        cv = model.form.field("candidate_cv")
        assert cv.control_type is ControlType.FILE and cv.label == "CV" and cv.required is True
        control = next(c for c in model.snapshot.controls if c.name == "candidate_cv")
        assert "select a file" in control.upload_trigger
    _serve(kit, server, options, DROPZONE_LIKE, check)


STUCK_LOADING = """<!doctype html><title>Apply</title>
<aside><button type="button" class="chat-widget">Loading...</button></aside>
<form method="post" action="/apply">
<div class="field"><label for="full_name">Full name</label><input id="full_name" name="full_name" required></div>
<div class="field"><label for="email">Email</label><input id="email" name="email" type="email" required></div>
<button type="submit">Submit application</button></form>"""


def test_a_loading_indicator_that_never_clears_is_waited_for_once(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[float, float, Any]:
        browser = await PlaywrightSessionFactory(settle_timeout_s=2.0).start(options)
        try:
            await browser.page.route("**/fixture-stuck", lambda route: route.fulfill(
                content_type="text/html; charset=utf-8", body=STUCK_LOADING))
            loop = asyncio.get_running_loop()
            started = loop.time()
            page = await browser.open(server.url("/fixture-stuck"))
            opened = loop.time() - started
            assert page.form is not None, page.message
            packet = kit.build(page.form, {"full_name": "Avery Quill",
                                           "email": "avery.quill@example.test"}).packet
            started = loop.time()
            fill = await browser.fill(page.form, packet)
            return opened, loop.time() - started, fill
        finally:
            await browser.close()

    opened, filled, fill = kit.run(scenario())
    assert 1.8 < opened < 6.0, opened  # open() waited the bounded 2 s for "Loading..."
    assert filled < 1.8, filled  # the same stuck indicator is not waited for again
    assert fill.ok, fill


# --- pure rules -------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Apply with LinkedIn", "Apply using Indeed", "Autofill my application",
    "Autofill with MyGreenhouse", "Import from LinkedIn", "Use my LinkedIn profile",
    "Continue with LinkedIn",
])
@pytest.mark.parametrize("submits", [True, False])
def test_third_party_autofill_helpers_are_never_step_actions(text: str, submits: bool) -> None:
    assert button_intent(text, submits_form=submits) is ButtonIntent.OTHER


@pytest.mark.parametrize(("text", "expected"), [
    ("Submit application", ButtonIntent.SUBMIT), ("Apply", ButtonIntent.SUBMIT),
    ("Next", ButtonIntent.NEXT), ("Upload file", ButtonIntent.OTHER),
])
def test_ordinary_actions_keep_their_intent(text: str, expected: ButtonIntent) -> None:
    assert button_intent(text, submits_form=True) is expected


OFFER = "Autofill your application? Import your details from LinkedIn to fill in this form faster."


@pytest.mark.parametrize(("buttons", "expected"), [
    ([("Autofill with LinkedIn", "#accept"), ("No thanks", "#decline")], "#decline"),
    ([("Import my profile", "#accept"), ("Not now", "#later"), ("\u00d7", "#x")], "#later"),
    ([("Close", "#close"), ("Use LinkedIn", "#accept")], "#close"),
    ([("Autofill with LinkedIn", "#accept"), ("Continue", "#go")], None),
    ([("Autofill", "#accept")], None),
])
def test_autofill_offer_is_declined_only_with_its_decline_control(
    buttons: list[tuple[str, str]], expected: str | None
) -> None:
    assert autofill_decline(OFFER, buttons) == expected


def test_dialogs_that_offer_no_autofill_are_left_alone() -> None:
    cookies = "This website uses cookies. We use cookies to improve your experience."
    assert autofill_decline(cookies, [("Decline all", "#d"), ("Close", "#c")]) is None
    assert autofill_decline("Are you sure you want to leave?", [("Cancel", "#c")]) is None


def test_upload_state_readback_rules() -> None:
    held = UploadState.from_raw({"connected": True, "files": [{"name": "cv.pdf", "size": 802}]})
    assert held.holds("cv.pdf", 802) and held.confirms("cv.pdf", 802)
    moved = UploadState.from_raw({"connected": True, "files": [], "chip": True})
    assert moved.shown and moved.confirms("cv.pdf", 802)
    noticed = UploadState.from_raw({"files": [], "notice": "cv.pdf uploaded"})
    assert noticed.confirms("cv.pdf", 802)
    other = UploadState.from_raw({"files": [{"name": "other.pdf", "size": 5}], "chip": True})
    assert not other.confirms("cv.pdf", 802), "another file in the input is never confirmed by a chip"
    assert not UploadState.from_raw(None).confirms("cv.pdf", 802)
    assert not UploadState.from_raw({"files": [], "busy": True}).shown
