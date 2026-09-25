"""Round 7, item 2: Teamtailor's résumé uploader (pilot 7 ended FAILED_RETRYABLE on
``candidate_resume_remote_url``).

That id is not an import field: it is Dropzone's hidden file input, handed its label's id
by the site's controller. A chosen file hides and disables the input; Dropzone replaces
it with a fresh one, which gets the id back only once the upload ends, and the preview it
adds carries a hidden URL input with the same id. So after the attach the selector names
the URL input, then two elements: the readback must read the uploader's container taken
before attaching. Fictional data, real headless Chromium, nothing submitted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import async_playwright

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_browser.driver import FILE_SHOWN, shown_names
from interviewmaxxing_browser.normalize import build_page
from interviewmaxxing_browser.snapshot import DomSnapshot, inspector_script
from interviewmaxxing_browser.uploads import UPLOAD_STATE, UploadState
from interviewmaxxing_core import BrowserOptions, ControlType, FieldFillStatus, SemanticType

TEAMTAILOR = "/jobs/teamtailor-like/apply"
CONTACT = {"first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
           "phone": "+1 (303) 555-0142"}
RESUME_ID = "candidate_resume_remote_url"
SHOWN = """() => {
  const ids = [...document.querySelectorAll('#candidate_resume_remote_url')];
  const name = document.querySelector('#upload_resume_field [data-dz-name]');
  return {ids: ids.map((e) => e.type), taken: window.__filesTaken || 0,
          name: name && name.offsetParent !== null ? name.textContent : null,
          url: (ids.find((e) => e.type === 'text') || {}).value || null};
}"""


@pytest.mark.parametrize("delay_ms", [None, 150, 4500], ids=["live-1200ms", "fast", "slow-4500ms"])
def test_a_dropzone_that_hands_its_id_on_reads_back_by_its_container(
    delay_ms: int | None, kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """The attach is verified while the selector names the hidden URL input, then two
    elements, and while "Uploading…" shows (longer than the 3 s display wait); a second
    fill does not upload again."""
    async def scenario() -> tuple[Any, ...]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            if delay_ms is not None:
                await browser.page.add_init_script(
                    f"window.__widgetHooks = {{selectNext: {{}}, uploadDelayMs: {delay_ms}}};")
            page = await browser.open(server.url(TEAMTAILOR))
            packet = kit.build(page.form, {**CONTACT, RESUME_ID: kit.RESUME}).packet
            first = await browser.fill(page.form, packet)
            shown = await browser.page.evaluate(SHOWN)
            again = await browser.fill(page.form, packet)
            review = await browser.prepare_review()
            return page, first, again, shown, await browser.page.evaluate(SHOWN), review
        finally:
            await browser.close()

    page, first, again, shown, later, review = kit.run(scenario())
    resume = page.form.field(RESUME_ID)
    assert resume.control_type is ControlType.FILE and resume.semantic_type is SemanticType.RESUME
    one = next(f for f in first.fields if f.field_id == RESUME_ID)
    # The bytes the uploader was handed were verified (no "not verified" suffix).
    assert one.status is FieldFillStatus.FILLED, one
    assert one.detail == "the uploader shows the file; its input is empty or replaced", one
    assert first.ok, first
    # The page ends with the fresh input and the URL input under the same id.
    assert shown["ids"] == ["file", "text"] and shown["name"] == kit.RESUME_PATH.name
    assert shown["url"] and shown["taken"] == 1
    two = next(f for f in again.fields if f.field_id == RESUME_ID)
    assert two.status is FieldFillStatus.FILLED and two.detail == "the uploader already shows this file", two
    assert later["taken"] == 1
    assert review.form is not None and review.form.page_errors == []
    assert review.form.field(RESUME_ID).fingerprint == resume.fingerprint
    # The preview's URL input (now holding the stored file's address) is not a question.
    assert [f.id for f in review.form.fields] == [f.id for f in page.form.fields]


# --- the upload reads follow the uploader's container once the id moves --------------------

NAME = "resume_avery_quill.pdf"
UPLOADER = """<!doctype html><title>Fictional form</title><form>
<div id="uploader"><label for="up">Upload resume</label>
 <div id="trigger"><input type="file" id="up"></div><div id="previews"></div></div>
<div><label for="other">Portfolio URL</label><input id="other" type="url"></div></form>"""
# What the uploader does once it takes the file (as Teamtailor's Dropzone does): the input
# is replaced by a fresh one in the hidden trigger and a preview is added whose hidden URL
# input reuses the id; the name shows once the upload ends.
TAKE = """(arg) => {
  const trigger = document.getElementById('trigger');
  document.getElementById('up').remove();
  const fresh = document.createElement('input'); fresh.type = 'file';
  if (arg.done && arg.freshId) fresh.id = 'up';
  trigger.appendChild(fresh); trigger.style.display = 'none';
  document.getElementById('previews').innerHTML = arg.done
    ? '<div><a data-dz-name href="#">""" + NAME + """</a><input type="text" style="display:none"></div>'
    : '<div><span>Uploading…</span><a data-dz-name href="#" style="display:none">""" + NAME + """</a>'
      + '<input type="text" style="display:none" disabled></div>';
  if (arg.urlId) document.querySelector('#previews input').id = 'up';
}"""


@pytest.mark.parametrize(("done", "fresh_id", "url_id", "attached", "anchor", "expected"), [
    # Uploading: only the preview's hidden URL input has the id; its box is the preview.
    (False, False, True, False, "#uploader", {"shown": False, "busy": True, "connected": False}),
    # Done: the id names the fresh input and the URL input; the container shows the name.
    (True, True, True, False, "#uploader", {"shown": True, "busy": False, "connected": False}),
    # Without the recorded container the fresh input's own (hidden) box is all there is.
    (True, True, True, False, None, {"shown": False, "busy": False, "connected": True}),
    # The id names only the fresh input: the element the file was set on left the page.
    (True, True, False, True, "#uploader", {"shown": True, "busy": False, "connected": True}),
], ids=["uploading", "id-on-two", "no-anchor", "fresh-input-only"])
def test_the_upload_reads_follow_the_container_when_the_id_moves(
    done: bool, fresh_id: bool, url_id: bool, attached: bool, anchor: str | None, expected: dict[str, bool]
) -> None:
    async def scenario() -> tuple[Any, Any]:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(UPLOADER)
                handle = await page.query_selector("#up")
                await page.evaluate(TAKE, {"done": done, "freshId": fresh_id, "urlId": url_id})
                arg: dict[str, Any] = {"selector": "#up", "names": shown_names(NAME), "anchor": anchor}
                if attached:
                    arg["attached"] = handle
                shown = await page.evaluate(FILE_SHOWN, arg)
                state = await page.evaluate(UPLOAD_STATE, {"selector": "#up", "names": shown_names(NAME),
                                                           "anchor": anchor})
                return shown, UploadState.from_raw(state)
            finally:
                await browser.close()

    shown, state = asyncio.run(scenario())
    assert (shown["shown"], shown["busy"]) == (expected["shown"], expected["busy"]), shown
    if expected["shown"]:
        assert shown["files"] in (0, None) and not shown["alert"]
    if anchor is not None and not attached:
        # The runtime's own read (no element handle) agrees.
        assert (state.connected, state.busy) == (expected["connected"], expected["busy"]), state
        assert state.confirms(NAME, 802) is (done and not expected["busy"]), state


# --- the live shape, read-only (fractal.na.teamtailor.com, 2026-09-24; fictional text) -----

LIVE_TEAMTAILOR = """<!doctype html><title>Fictional careers site</title>
<form id="job-application-form" action="/jobs/1-fictional/applications" method="post">
<input type="hidden" name="authenticity_token" value="x">
<input type="hidden" name="candidate[linkedin_uid]" id="candidate_linkedin_uid">
<input type="hidden" name="candidate[linkedin_url]" id="candidate_linkedin_url">
<input type="hidden" name="candidate[linkedin_profile]" id="candidate_linkedin_profile">
<div><button type="button" class="linkedin-button">Apply with LinkedIn</button></div>
<div><label for="candidate_first_name">First name<span class="sr-only">Required</span></label>
<input type="text" id="candidate_first_name" name="candidate[first_name]" required></div>
<div><div><div>
<div data-controller="forms--inputs--upload" id="upload_resume_field">
 <label for="candidate_resume_remote_url">Upload resume<sup aria-hidden="true">*</sup><span class="sr-only">Required</span></label>
 <div data-forms--inputs--upload-target="trigger" style="position:relative;height:70px;overflow:hidden">
  <div><span class="dz-message"><span data-forms--inputs--upload-target="desktopText"> Drop your file or <u>upload</u> </span>
  <span class="hidden" data-forms--inputs--upload-target="mobileText"> Click to <u>upload</u> </span></span></div>
  <input type="file" class="dz-hidden-input" accept=".doc,.docx,.pptx,.pdf,.pages,.txt,.rtf" id="candidate_resume_remote_url"
   aria-label="Drop your file or upload, Upload resume" required=""
   style="position:absolute;top:0;left:0;height:100%;width:100%;cursor:pointer;opacity:0"></div>
 <div data-forms--inputs--upload-target="previewsContainer"></div>
 <button type="button" class="hidden" data-forms--inputs--upload-target="secondaryTrigger"><span>Upload more</span></button>
 <p class="hidden" data-forms--inputs--upload-target="errorContainer"><span data-forms--inputs--upload-target="errorMsg"></span></p>
 <template data-forms--inputs--upload-target="templatePreview"><div data-controller="forms--inputs--upload-preview">
  <div class="hidden" data-forms--inputs--upload-preview-target="name"><a data-dz-name href="javascript:void(0);"></a>
  <button title="Clear file selection" data-dz-remove="true">x</button></div>
  <div data-progress data-forms--inputs--upload-preview-target="progress"><div><span>Uploading…</span></div></div>
  <input value="" class="hidden" disabled type="text" name="candidate[resume_remote_url]" id="candidate_resume_remote_url">
  <input disabled value="0" type="hidden" name="candidate[delete_resume_remote_url]" id="candidate_delete_resume_remote_url">
 </div></template>
</div></div></div></div>
<button type="submit">Submit application</button></form>
<style>.hidden{display:none}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}</style>"""


def test_the_live_teamtailor_uploader_is_one_file_question_and_imports_are_not_questions() -> None:
    """Captured read-only: the résumé question is the Dropzone input named by its label;
    the LinkedIn import's hidden inputs and the preview template's URL input are not
    questions."""
    async def scenario() -> DomSnapshot:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(LIVE_TEAMTAILOR)
                return DomSnapshot.model_validate(await page.evaluate(inspector_script()))
            finally:
                await browser.close()

    model = build_page(asyncio.run(scenario()))
    assert model.form is not None
    assert [f.id for f in model.form.fields] == ["candidate[first_name]", RESUME_ID]
    resume = model.form.field(RESUME_ID)
    assert resume.control_type is ControlType.FILE and resume.label.startswith("Upload resume")
    assert resume.semantic_type is SemanticType.RESUME
