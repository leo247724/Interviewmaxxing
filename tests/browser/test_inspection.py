"""Real headless Chromium inspecting the localhost mock ATS (no submissions here)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from interviewmaxxing_browser import (
    GenericAdapter,
    PlaywrightSessionFactory,
    attestation_fields,
    unsupported_control_needs,
    user_action_needs,
)
from interviewmaxxing_core import (
    ApplicationBrowser,
    ApplicationForm,
    BrowserOptions,
    BrowserSessionFactory,
    ControlType,
    IdentityEvidenceKind,
    MissingReason,
    PageKind,
    SemanticType,
)


async def _open(options: BrowserOptions, url: str) -> tuple[Any, Any]:
    browser = await PlaywrightSessionFactory().start(options)
    return browser, await browser.open(url)


def test_factory_and_browser_satisfy_core_protocols(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> None:
        factory = PlaywrightSessionFactory()
        assert isinstance(factory, BrowserSessionFactory)
        browser = await factory.start(options)
        try:
            assert isinstance(browser, ApplicationBrowser)
        finally:
            await browser.close()

    kit.run(scenario())


def test_posting_is_followed_to_a_normalized_form(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> ApplicationForm:
        browser, inspection = await _open(options, server.url("/jobs/standard"))
        try:
            assert inspection.kind is PageKind.APPLICATION_FORM
            assert inspection.observed_url == server.url("/jobs/standard/apply")
            identity = inspection.job_identity
            assert identity is not None and identity.external_job_id == "BWA-ENG-101"
            assert identity.evidence_kind is IdentityEvidenceKind.ATS_JOB_ID_ON_PAGE
            assert identity.title == "Senior Data Platform Engineer"
            form = inspection.form
            assert form is not None and form.step == 0 and form.is_final_step is True
            assert form.submit_selector and form.next_selector is None
            # Re-inspecting and reloading keeps every question's identity.
            again = await browser.inspect()
            await browser.page.reload()
            reloaded = await browser.inspect()
            assert again.form.fingerprint == form.fingerprint == reloaded.form.fingerprint
            return form
        finally:
            await browser.close()

    form = kit.run(scenario())
    by_id = {f.id: f for f in form.fields}
    assert list(by_id) == [
        "first_name", "last_name", "email", "phone", "linkedin_url", "resume",
        "work_authorization", "years_experience", "sponsorship", "skills",
        "work_arrangements", "open_to_relocation", "why_brambleway",
    ]
    expect = {
        "first_name": (ControlType.TEXT, SemanticType.FIRST_NAME, True),
        "email": (ControlType.TEXT, SemanticType.EMAIL, True),
        "linkedin_url": (ControlType.TEXT, SemanticType.LINKEDIN, False),
        "resume": (ControlType.FILE, SemanticType.RESUME, True),
        "work_authorization": (ControlType.SELECT, SemanticType.WORK_AUTHORIZATION, True),
        "sponsorship": (ControlType.RADIO, SemanticType.SPONSORSHIP, True),
        "skills": (ControlType.MULTISELECT, SemanticType.CUSTOM_MULTISELECT, True),
        "work_arrangements": (ControlType.CHECKBOX_GROUP, SemanticType.CUSTOM_MULTISELECT, False),
        "open_to_relocation": (ControlType.CHECKBOX, SemanticType.RELOCATION, False),
        "why_brambleway": (ControlType.TEXTAREA, SemanticType.CUSTOM_LONG_TEXT, True),
    }
    for field_id, (control, semantic, required) in expect.items():
        f = by_id[field_id]
        assert (f.control_type, f.semantic_type, f.required) == (control, semantic, required), field_id
    assert by_id["email"].input_type == "email" and by_id["phone"].input_type == "tel"
    assert by_id["why_brambleway"].max_length == 5000
    assert by_id["resume"].accept == [".pdf", ".doc", ".docx", ".txt", "application/pdf", "text/plain"]
    assert by_id["resume"].help_text == "PDF, DOC, DOCX or TXT, up to 5 MB."
    # Machine values differ from visible labels; the placeholder is reported as it is.
    assert [(o.value, o.label) for o in by_id["work_authorization"].options or []] == [
        ("", "Select an answer"),
        ("wa_authorized", "Yes, I am authorized to work in the US"),
        ("wa_not_authorized", "No, I am not authorized to work in the US"),
    ]
    radios = by_id["sponsorship"].options or []
    assert [o.value for o in radios] == ["needs_sponsorship", "no_sponsorship"]
    assert all(o.selector for o in radios)
    assert by_id["work_arrangements"].help_text == "Select all that apply."
    assert by_id["linkedin_url"].label == "LinkedIn profile URL (optional)"
    assert by_id["first_name"].label == "First name"  # the aria-hidden "*" is not text


def test_disabled_options_and_explicit_questions(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> ApplicationForm:
        browser, inspection = await _open(options, server.url("/jobs/missing-required/apply"))
        try:
            return inspection.form
        finally:
            await browser.close()

    form = kit.run(scenario())
    notice = form.field("notice_period")
    assert [(o.value, o.disabled) for o in notice.options or []][-1] == ("notice_3m_plus", True)
    assert form.field("salary_expectation").semantic_type is SemanticType.SALARY_EXPECTATION
    faa = form.field("faa_part_107")
    assert faa.control_type is ControlType.RADIO and faa.semantic_type is SemanticType.CUSTOM_SELECT


def test_agreement_terms_are_part_of_each_question(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> ApplicationForm:
        browser, inspection = await _open(options, server.url("/jobs/agreement/apply"))
        try:
            return inspection.form
        finally:
            await browser.close()

    form = kit.run(scenario())
    declaration = form.field("agree_declaration")
    retention = form.field("agree_retention")
    assert declaration.label == retention.label == "I agree"
    # Legend + adjacent paragraph (not linked by aria-describedby) are captured.
    assert declaration.help_text == (
        "Candidate declaration I confirm that I have never been dismissed from employment "
        "for misconduct."
    )
    # aria-describedby text is captured.
    assert retention.help_text == (
        "Brambleway Analytics may keep my application on file for 12 months and contact "
        "me about other roles."
    )
    assert declaration.fingerprint != retention.fingerprint
    assert declaration.semantic_type is SemanticType.ATTESTATION
    assert retention.semantic_type is SemanticType.CONSENT
    assert {f.id for f in attestation_fields(form)} == {"agree_declaration", "agree_retention"}


def test_attestation_checkboxes_are_classified(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> ApplicationForm:
        browser, inspection = await _open(options, server.url("/jobs/attestation/apply"))
        try:
            return inspection.form
        finally:
            await browser.close()

    form = kit.run(scenario())
    assert form.field("attest_accuracy").semantic_type is SemanticType.ATTESTATION
    assert form.field("attest_privacy_notice").semantic_type is SemanticType.CONSENT
    assert all(f.required for f in attestation_fields(form))


def test_custom_widget_is_unsupported_and_hidden_or_disabled_controls_are_ignored(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[ApplicationForm, ApplicationForm, list[Any]]:
        browser, inspection = await _open(options, server.url("/jobs/custom-control/apply"))
        try:
            before = inspection.form
            # The person operates the widget; the runtime then reports it as done.
            waiter = asyncio.create_task(browser.wait_for_user("choose an office", timeout_s=10))
            await browser.page.click("#f-preferred_office")
            await browser.page.click("#f-preferred_office-list [data-value=office_bou]")
            after = await waiter
            return before, after.form, after.evidence
        finally:
            await browser.close()

    before, after, evidence = kit.run(scenario())
    ids = [f.id for f in before.fields]
    assert "website_hp" not in ids  # visually hidden honeypot
    assert "referral_code" not in ids  # disabled
    office = before.field("preferred_office")
    assert office.control_type is ControlType.UNSUPPORTED and office.required
    assert office.label == "Preferred office" and office.options is None
    needs = unsupported_control_needs(before)
    assert [(n.field_id, n.reason) for n in needs] == [("preferred_office", MissingReason.UNSUPPORTED_CONTROL)]
    # Same question (fingerprint), now operated by the user and no longer required.
    assert after.field("preferred_office").fingerprint == office.fingerprint
    assert not after.field("preferred_office").required
    assert unsupported_control_needs(after) == []
    assert evidence


def test_sign_in_page_needs_the_user_and_persists_in_the_profile(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, tmp_path: Any
) -> None:
    creds = server.api("GET", "/__test__/jobs")["signin"]
    persistent = BrowserOptions(
        artifacts_dir=options.artifacts_dir, artifacts_root=options.artifacts_root,
        profile_dir=tmp_path / "browser-profile", headless=True,
    )

    async def first_run() -> tuple[Any, Any, Any]:
        browser, inspection = await _open(persistent, server.url("/jobs/signin"))
        try:
            needs = user_action_needs(inspection)
            waiter = asyncio.create_task(browser.wait_for_user("sign in", timeout_s=15))
            await browser.page.fill("#f-login-email", creds["email"])
            await browser.page.fill("#f-login-password", creds["password"])
            await browser.page.click("button[type=submit]")
            return inspection, needs, await waiter
        finally:
            await browser.close()

    async def second_run() -> Any:
        browser, inspection = await _open(persistent, server.url("/jobs/signin/apply"))
        await browser.close()
        return inspection

    inspection, needs, after = kit.run(first_run())
    assert inspection.kind is PageKind.SIGN_IN_REQUIRED and inspection.form is None
    assert inspection.job_identity is not None  # carried from the posting page
    assert [(n.field_id, n.reason) for n in needs] == [(None, MissingReason.USER_ACTION)]
    assert after.kind is PageKind.APPLICATION_FORM
    assert after.observed_url == server.url("/jobs/signin/apply")
    assert (kit.run(second_run())).kind is PageKind.APPLICATION_FORM


def test_wait_for_user_times_out_without_touching_the_page(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> Any:
        browser, _ = await _open(options, server.url("/jobs/captcha/apply"))
        try:
            return await browser.wait_for_user("solve the CAPTCHA", timeout_s=0.5)
        finally:
            await browser.close()

    inspection = kit.run(scenario())
    assert inspection.kind is PageKind.CAPTCHA
    assert "CAPTCHA" in (inspection.message or "")
    assert inspection.evidence and all(not e.path.startswith("/") for e in inspection.evidence if e.path)
    assert server.submissions()["accepted_count"] == 0


def test_generic_adapter_drives_a_playwright_page(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.page.goto(server.url("/jobs/standard/apply"))
            adapter = GenericAdapter(options)
            assert await adapter.detect(browser.page)
            return await adapter.inspect(browser.page)
        finally:
            await browser.close()

    inspection = kit.run(scenario())
    assert inspection.kind is PageKind.APPLICATION_FORM and len(inspection.form.fields) == 13


def test_status_lookup_and_error_pages_are_not_application_forms(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            status = await browser.open(server.url("/jobs/standard/application-status"))
            missing = await browser.open(server.url("/jobs/no-such-job"))
            index = await browser.open(server.url("/"))
            return status, missing, index
        finally:
            await browser.close()

    status, missing, index = kit.run(scenario())
    assert status.kind is PageKind.UNKNOWN and status.form is None
    assert missing.kind is PageKind.ERROR
    assert index.kind is PageKind.UNKNOWN
