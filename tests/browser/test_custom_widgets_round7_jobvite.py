"""Round 7, item 4: Jobvite postings (fictional data, localhost mock ATS and synthetic
pages in real headless Chromium; nothing is submitted and no consent is accepted by the
runtime).

Jobvite's Apply leads to a "Data Consent" page: choose a location of residence and
language, then "I Accept". That is the person's decision, so the page is reported as one
the user must act on (``SIGN_IN_REQUIRED``, the user-action page the runner asks about and
waits for) with a message saying what to do; once the user has accepted, the application
form is inspected as usual. Its résumé "Select" button opens an attachment popup appended
to <body>, whose hidden file input is outside the form: that input is the résumé question,
named by the button's heading.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from playwright.async_api import async_playwright

from interviewmaxxing_browser import (
    GenericApplicationBrowser,
    PlaywrightSessionFactory,
    user_action_needs,
)
from interviewmaxxing_browser.normalize import build_page
from interviewmaxxing_browser.snapshot import DomSnapshot, inspector_script
from interviewmaxxing_core import (
    ApplicationForm,
    BrowserOptions,
    ControlType,
    FieldFillStatus,
    IdentityEvidenceKind,
    JobIdentityObservation,
    MissingReason,
    PageInspection,
    PageKind,
    SemanticType,
)

POSTING = "/jobs/jobvite-like"
APPLY = "/jobs/jobvite-like/apply"
RESUME_ID = "file-input-0"


def _answers(kit: SimpleNamespace) -> dict[str, Any]:
    return {
        **{k: kit.CORE[k] for k in ("first_name", "last_name", "email", "phone")},
        RESUME_ID: kit.RESUME,
        "referred": "No, I was not referred",
        "sponsorship": "No, I will NOT ever require any work sponsorship",
    }


async def _consent_already_given(browser: Any, server: Any) -> None:
    """The site remembers an earlier acceptance (its cookie), as after the user's own."""
    await browser.page.context.add_cookies(
        [{"name": "bwa_jv_consent", "value": "accepted", "url": server.url("/")}])


# --- the consent page ---------------------------------------------------------------------


def test_a_data_consent_page_is_left_to_the_user(server: Any, options: BrowserOptions) -> None:
    async def scenario() -> tuple[Any, Any, dict[str, Any]]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            opened = await browser.open(server.url(POSTING))
            again = await browser.inspect()
            state = await browser.page.evaluate("""() => ({
              chosen: document.getElementById("jv-country-select").value,
              accept: !!Array.from(document.querySelectorAll("button")).find((b) => /accept/i.test(b.textContent)),
              url: location.pathname})""")
            return opened, again, state
        finally:
            await browser.close()

    opened, again, state = asyncio.run(scenario())
    assert opened.kind is again.kind is PageKind.SIGN_IN_REQUIRED, (opened, again)
    assert opened.form is None and opened.message == again.message
    assert "accept its data-processing consent before the application form" in (opened.message or "")
    assert "location of residence and language" in (opened.message or "")
    # Nothing on the page was chosen or accepted, and nothing reached the site.
    assert state == {"chosen": "", "accept": False, "url": APPLY}
    summary = server.submissions("jobvite-like")
    assert summary["consents"] == [] and summary["accepted_count"] == 0
    (need,) = user_action_needs(opened)
    assert need.reason is MissingReason.USER_ACTION and need.field_id is None
    assert need.label == "Accept the data-processing consent" and need.prompt == opened.message


def test_the_form_is_inspected_once_the_user_has_accepted(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            gate = await browser.open(server.url(POSTING))

            async def person() -> None:  # the user, in the browser window
                await asyncio.sleep(0.8)
                await browser.page.select_option("#jv-country-select", kit.mock_ats.JV_POLICY_ID)
                await browser.page.click("text=I Accept")

            acting = asyncio.create_task(person())
            after = await browser.wait_for_user(gate.message or "", timeout_s=20)
            await acting
            return gate, after
        finally:
            await browser.close()

    gate, after = asyncio.run(scenario())
    assert gate.kind is PageKind.SIGN_IN_REQUIRED
    assert after.kind is PageKind.APPLICATION_FORM and after.form is not None, after
    # The form shows no job id of its own (like Jobvite's); it continues the posting that
    # was opened (its apply URL), so it carries that posting's identity.
    assert gate.job_identity is not None and after.job_identity == gate.job_identity
    assert len(server.submissions("jobvite-like")["consents"]) == 1  # the user's own
    resume = after.form.field(RESUME_ID)
    assert (resume.label, resume.control_type, resume.semantic_type, resume.required) == (
        "Add Resume", ControlType.FILE, SemanticType.RESUME, True)
    assert [f.id for f in after.form.fields if f.control_type is ControlType.FILE] == [RESUME_ID]


def test_a_posting_whose_consent_was_given_goes_straight_to_the_form(
    server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await _consent_already_given(browser, server)
            return await browser.open(server.url(POSTING))
        finally:
            await browser.close()

    page = asyncio.run(scenario())
    assert page.kind is PageKind.APPLICATION_FORM and page.form is not None, page
    assert page.observed_url.endswith(APPLY) and page.job_identity is not None
    assert server.submissions("jobvite-like")["consents"] == []


def test_the_resume_in_an_attachment_popup_is_attached_once(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, dict[str, Any]]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await _consent_already_given(browser, server)
            page = await browser.open(server.url(POSTING))
            assert page.form is not None
            packet = kit.build(page.form, _answers(kit)).packet
            first = await browser.fill(page.form, packet)
            again = await browser.fill(page.form, packet)
            state = await browser.page.evaluate("""() => ({
              taken: window.__filesTaken,
              chip: document.querySelector("#attach-resume .jv-file-list").textContent,
              select: document.querySelector("#attach-resume .jv-select").hidden})""")
            return first, again, state
        finally:
            await browser.close()

    first, again, state = asyncio.run(scenario())
    for result in (first, again):
        statuses = {f.field_id: f.status for f in result.fields}
        assert set(statuses.values()) == {FieldFillStatus.FILLED}, result.fields
    assert state["taken"] == 1 and state["select"] is True
    assert kit.RESUME_PATH.name in state["chip"]
    assert server.submissions("jobvite-like")["accepted_count"] == 0


# --- live shapes (captured read-only, names made fictional) --------------------------------

JOBVITE_CONSENT = """<!doctype html><title>Brambleway Careers</title>
<style>.ng-hide{display:none!important}</style>
<body class="jv-desktop jv-page-consentform">
<header class="jv-page-header"><h1 class="jv-logo"><a href="/brambleway/jobs">Brambleway Careers</a></h1></header>
<article class="jv-page-body" role="main"><div class="jv-wrapper"><h3>Data Consent</h3><br>
<form name="consentForm" class="jv-form ng-pristine ng-invalid ng-invalid-required" method="POST"
      action="/brambleway/job/oAbCdEfG/apply">
  <div><label for="jv-country-select">
Location of Residence and Language:
  </label></div><br>
  <select id="jv-country-select" class="ng-pristine ng-empty ng-invalid ng-invalid-required" required>
    <option value="" selected>Select your location of residence and language</option>
    <option value="815b0c56">Global BRAMBLEWAY APPLICANT AND CANDIDATE PRIVACY POLICY</option>
  </select>
  <div><br><a class="jv-button" type="button" href="/brambleway/job/oAbCdEfG">Back</a></div>
  <div class="ng-hide"><br><p style="white-space:pre-line">  </p><br>
    <div><br><a class="jv-button" type="button" href="/brambleway/job/oAbCdEfG">Back</a></div>
  </div>
</form></div></article>
<footer class="jv-footer"><a class="jv-powered-by" href="#"><span>Powered by Jobvite</span></a></footer>
</body>"""

JOBVITE_APPLY = """<!doctype html><title>Brambleway Careers - Apply for Paid Media Manager</title>
<style>.ng-hide{display:none!important}.jv-visually-hidden{position:absolute;clip:rect(0 0 0 0)}</style>
<body class="jv-desktop jv-page-apply">
<h2><div class="jv-header">Paid Media Manager</div></h2>
<div class="jv-form jv-apply-form">
<form name="scopeData.applyForm" novalidate class="ng-pristine ng-invalid ng-invalid-required">
  <div ng-form="scopeData.step1" class="jv-apply-step">
    <div>
      <h3 class="jv-step-header" id="jv-resume-header">
          Add Resume*
      </h3>
      <div class="jv-apply-with jv-apply-section" id="attachResume">
        <div>
          <button class="jv-button jv-button-primary" type="button" aria-haspopup="true"
                  aria-labelledby="jv-resume-header" aria-expanded="false" aria-required="true">
Select <i class="icon icon-arrow-down jv-text-icon"></i>
          </button>
        </div>
        <ul class="jv-file-list"></ul>
      </div>
    </div>
    <div class="jv-apply-section">
      <h3 class="jv-step-header">Personal Information</h3>
      <div class="jv-form-field"><label for="jv-field-yrKSYfwL" class="jv-form-field-label">
          First Name<span class="jv-required-label">*</span></label>
        <input id="jv-field-yrKSYfwL" name="input-yrKSYfwL" autocomplete="given-name" type="text" maxlength="100" required></div>
      <div class="jv-form-field"><label for="jv-field-ytKSYfwN" class="jv-form-field-label">
          Last Name<span class="jv-required-label">*</span></label>
        <input id="jv-field-ytKSYfwN" name="input-ytKSYfwN" autocomplete="family-name" type="text" maxlength="100" required></div>
      <div class="jv-form-field"><label for="jv-field-ysKSYfwM" class="jv-form-field-label">
          Email<span class="jv-required-label">*</span></label>
        <input id="jv-field-ysKSYfwM" name="input-ysKSYfwM" autocomplete="email" type="text" maxlength="100" required></div>
    </div>
    <div class="jv-additional-files jv-apply-section">
      <h3 class="jv-step-header" id="">Additional Files (optional)</h3>
      <span><button class="jv-button" type="button" aria-haspopup="true" aria-expanded="false"
                    aria-label="Add Cover Letter">
Add Cover Letter                </button></span>
      <ul class="jv-file-list"></ul>
    </div>
  </div>
  <div class="jv-apply-form-actions">
    <button class="jv-button jv-button-primary jv-button-large" type="button" aria-controls="applyForm"
            aria-label="Next">Next &rarr;</button>
  </div>
</form></div>
<div class="jv-add-attachment ng-hide" id="attachmentDropdown" aria-hidden="true" tabindex="-1" role="dialog" aria-label="Attachment Options">
  <div class="jv-add-attachment-item"><span class="jv-text-block jv-text-link" tabindex="0" role="button">
Dropbox                </span></div>
  <hr>
  <div class="jv-add-attachment-item">
    <label class="jv-text-block jv-text-link" for="file-input-0">
      <i class="jv-attachment-icon icon icon-upload-bottom"></i>
      <span class="needsclick" tabindex="0" role="button">File</span>
    </label><input id="file-input-0" type="file" style="position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); border: 0;">
  </div>
  <div class="jv-add-attachment-item"><span class="jv-text-block jv-text-link needsclick" tabindex="0" role="button">
Type or Paste Resume                </span></div>
  <a class="jv-close" href="" tabindex="0"><i class="icon icon-close"><span>Close</span></i></a>
  <div class="jv-add-attachment-paste ng-hide" aria-hidden="true">
    <form class="jv-form ng-pristine ng-valid">
      <label for="jv-paste-resume-textarea0" class="jv-visually-hidden">Type or paste your Resume here</label>
      <textarea id="jv-paste-resume-textarea0" placeholder="Type or paste your Resume here"></textarea>
    </form>
  </div>
</div><div class="jv-add-attachment ng-hide" id="attachmentDropdown" aria-hidden="true" tabindex="-1" role="dialog" aria-label="Attachment Options">
  <div class="jv-add-attachment-item">
    <label class="jv-text-block jv-text-link" for="file-input-1">
      <span class="needsclick" tabindex="0" role="button">File</span>
    </label><input id="file-input-1" type="file" style="position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); border: 0;">
  </div>
  <div class="jv-add-attachment-item"><span class="jv-text-block jv-text-link needsclick" tabindex="0" role="button">
Type or Paste Cover Letter                </span></div>
</div>
</body>"""


async def _live_shape(html: str, url: str) -> Any:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content(html)
            raw = await page.evaluate(inspector_script())
            snapshot = DomSnapshot.model_validate(raw).model_copy(update={"url": url})
            return build_page(snapshot)
        finally:
            await browser.close()


def test_live_shape_the_jobvite_consent_page_is_the_users_to_accept() -> None:
    url = "https://jobs.jobvite.com/brambleway/job/oAbCdEfG/apply"  # never opened
    model = asyncio.run(_live_shape(JOBVITE_CONSENT, url))
    assert model.inspection.kind is PageKind.SIGN_IN_REQUIRED
    assert (model.inspection.message or "").startswith(
        "Jobvite asks you to accept its data-processing consent before the application form "
        "(choose your location of residence and language, then accept).")


def test_live_shape_the_jobvite_popup_inputs_are_the_resume_and_cover_letter() -> None:
    url = "https://jobs.jobvite.com/brambleway/job/oAbCdEfG/apply"  # never opened
    model = asyncio.run(_live_shape(JOBVITE_APPLY, url))
    form = model.inspection.form
    assert model.inspection.kind is PageKind.APPLICATION_FORM and form is not None
    files = {f.id: (f.label, f.semantic_type, f.required) for f in form.fields
             if f.control_type is ControlType.FILE}
    assert files == {
        "file-input-0": ("Add Resume", SemanticType.RESUME, True),
        "file-input-1": ("Add Cover Letter", SemanticType.COVER_LETTER, False),
    }
    assert model.bindings["file-input-0"].selector == "#file-input-0"
    # Where the uploader shows the file: the Select button's box in the form.
    assert model.bindings["file-input-0"].upload_anchor == "#attachResume"
    # The popup's own menu of sources is not help text of the question.
    assert all(not (f.help_text or "") for f in form.fields if f.control_type is ControlType.FILE)


POSTING_WITH_A_CONSENT_BANNER = """<!doctype html><title>Paid Media Manager</title>
<h1>Paid Media Manager</h1><p>Brambleway Analytics · Remote (US)</p>
<p><a href="/jobs/paid-media/apply">Apply for this job</a></p>
<div role="dialog" aria-label="Privacy"><h2>Data Consent</h2>
<form><label><input type="checkbox" name="analytics"> Allow analytics cookies</label>
<button type="button">Save</button></form></div>"""

FORM_WITH_A_CONSENT_SECTION = """<!doctype html><title>Apply</title><form method="post">
<label for="fn">First name</label><input id="fn" name="first_name" required>
<label for="em">Email</label><input id="em" type="email" name="email" required>
<h3>Data Privacy Consent</h3>
<label><input type="checkbox" name="consent" required> I agree to the processing of my data</label>
<button type="submit">Submit application</button></form>"""


def test_a_consent_heading_elsewhere_is_not_a_consent_page() -> None:
    """A posting with a consent banner stays a posting, and a form whose section is
    headed "Data Privacy Consent" stays the application form."""
    posting = asyncio.run(_live_shape(POSTING_WITH_A_CONSENT_BANNER, "https://example.test/jobs/paid-media"))
    form = asyncio.run(_live_shape(FORM_WITH_A_CONSENT_SECTION, "https://example.test/jobs/paid-media/apply"))
    assert posting.inspection.kind is PageKind.JOB_DESCRIPTION
    assert form.inspection.kind is PageKind.APPLICATION_FORM


POSTED = "https://jobs.example.test/brambleway/job/oAbCdEfG"


@pytest.mark.parametrize(("form_url", "carried"), [
    (POSTED + "/apply", True),
    (POSTED, True),
    ("https://jobs.example.test/brambleway/job/oZyXwVuT/apply", False),  # another job
    (POSTED + "/apply/step/2", False),
    ("https://other.example.test/brambleway/job/oAbCdEfG/apply", False),
    ("https://jobs.example.test/brambleway/job/oAbCdEfGH/apply", False),
])
def test_a_form_reached_after_the_user_acted_continues_only_its_posting(
    form_url: str, carried: bool, options: BrowserOptions
) -> None:
    browser = GenericApplicationBrowser(cast(Any, None), options)
    posting = JobIdentityObservation(
        ats_type="jobvite", ats_tenant="jobs.example.test", external_job_id="oAbCdEfG",
        evidence_kind=IdentityEvidenceKind.STRUCTURED_DATA, evidence="fixture", observed_url=POSTED)
    browser._posting_identity = posting
    reached = PageInspection(kind=PageKind.APPLICATION_FORM, observed_url=form_url,
                             form=ApplicationForm(url=form_url, fields=[]))
    assert browser._continuing_posting(reached).job_identity == (posting if carried else None)
