"""Real headless Chromium filling, navigating and submitting localhost mock forms.

Server-side outcomes are asserted independently through the mock's test-only API;
the runtime itself only reads pages.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser import (
    AmbiguousAction,
    ConfirmationTie,
    PlaywrightSessionFactory,
    SubmissionRefused,
    reconciliation_from,
)
from interviewmaxxing_core import (
    AnswerSource,
    ApplicationState,
    ApplicationStore,
    BrowserOptions,
    EvidenceKind,
    FieldFillStatus,
    NotSubmittedNext,
    PageKind,
    Provenance,
    ReconciliationMethod,
    RequestDisposition,
    SemanticType,
    SubmissionOutcome,
    UserInput,
)


def _evidence_files_exist(options: BrowserOptions, evidence: list[Any]) -> None:
    assert evidence
    for ref in evidence:
        if ref.path:
            assert ref.path.startswith("app_test/")
            path = options.artifacts_root / ref.path
            assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == ref.sha256


def test_standard_application_is_filled_submitted_and_confirmed(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            inspection = await browser.open(server.url("/jobs/standard"))
            form = inspection.form
            built = kit.build(form, {**kit.CORE, **kit.STANDARD})
            assert built.packet.is_complete
            fill = await browser.fill(form, built.packet)
            assert server.submissions()["accepted_count"] == 0  # filling never submits
            with pytest.raises(SubmissionRefused):
                await browser.advance()  # the step's primary action is the final submit
            action = await browser.submit()
            observation = await browser.confirm()
            with pytest.raises(SubmissionRefused):
                await browser.submit()  # accepted: never again in this session
            return form, fill, action, observation
        finally:
            await browser.close()

    _form, fill, action, observation = kit.run(scenario())
    assert fill.ok and fill.page_errors == []
    statuses = {r.field_id: r.status for r in fill.fields}
    assert statuses["open_to_relocation"] is FieldFillStatus.SKIPPED
    assert all(s is FieldFillStatus.FILLED for k, s in statuses.items() if k != "open_to_relocation")
    _evidence_files_exist(options, fill.evidence)
    assert action.dispatched and action.dispatched_at is not None

    summary = server.submissions("standard")
    assert summary["accepted_count"] == 1
    record = summary["submissions"][0]
    assert observation.outcome is SubmissionOutcome.ACCEPTED
    assert observation.confirmation_reference == record["confirmation_reference"]
    assert any("BWA-ENG-101" in s for s in observation.signals)
    assert any("acceptance text" in s for s in observation.signals)
    assert observation.observed_url == server.url(f"/applications/{record['submission_id']}")
    assert any(e.kind is EvidenceKind.CONFIRMATION_URL for e in observation.evidence)
    _evidence_files_exist(options, [e for e in observation.evidence if e.path])
    assert record["fields"] == {
        "first_name": "Avery",
        "last_name": "Quill",
        "email": "avery.quill@example.test",
        "phone": "+1 (303) 555-0142",
        "linkedin_url": "https://www.linkedin.example.test/in/avery-quill",
        "work_authorization": "wa_authorized",
        "years_experience": "yrs_6_9",
        "sponsorship": "no_sponsorship",
        "skills": ["sk_python", "sk_sql", "sk_spark", "sk_dbt"],
        "work_arrangements": ["arr_remote", "arr_hybrid"],
        "why_brambleway": kit.CANDIDATE["saved_answers"]["why_brambleway"],
    }
    upload = record["files"]["resume"]
    assert upload["sha256"] == hashlib.sha256(kit.RESUME_PATH.read_bytes()).hexdigest()
    assert upload["filename"] == kit.RESUME_PATH.name


def test_fill_refuses_packets_with_problems_and_stale_forms(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> None:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            form = (await browser.open(server.url("/jobs/missing-required/apply"))).form
            answers = {**kit.CORE, "salary_expectation": "150000",
                       "faa_part_107": "No", "notice_period": "3 months or more (no longer offered)"}
            with pytest.raises(ValueError, match="disabled"):
                await browser.fill(form, kit.build(form, answers).packet)
            # An attestation answered from a mere fact, mislabelled as a plain boolean,
            # is refused because the *inspected* field is an ATTESTATION.
            attest_form = (await browser.open(server.url("/jobs/attestation/apply"))).form
            fact = Provenance(source=AnswerSource.CANDIDATE_FACT, reference_ids=["fact.x"])
            bad = kit.build(attest_form, {**kit.CORE, "attest_accuracy": True},
                            source_overrides={"attest_accuracy": fact},
                            semantic_overrides={"attest_accuracy": SemanticType.CUSTOM_BOOLEAN})
            with pytest.raises(ValueError, match="ATTESTATION"):
                await browser.fill(attest_form, bad.packet)
            # A packet for a form that is no longer on the page is refused.
            stale = kit.build(form, {**kit.CORE, "salary_expectation": "1", "faa_part_107": "No",
                                     "notice_period": "2 weeks"})
            with pytest.raises(ValueError):
                await browser.fill(form, stale.packet)
            assert await browser.page.input_value("#f-first_name") == ""
        finally:
            await browser.close()

    kit.run(scenario())
    assert server.submissions()["accepted_count"] == 0


def test_saved_attestation_is_filled_and_accepted(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            form = (await browser.open(server.url("/jobs/attestation/apply"))).form
            # The user's own answers (USER_INPUT provenance) authorize the attestations.
            built = kit.build(form, {**kit.CORE, "attest_accuracy": True, "attest_privacy_notice": True})
            assert (await browser.fill(form, built.packet)).ok
            await browser.submit()
            return await browser.confirm()
        finally:
            await browser.close()

    observation = kit.run(scenario())
    assert observation.outcome is SubmissionOutcome.ACCEPTED
    record = server.submissions("attestation")["submissions"][0]
    assert record["fields"]["attest_accuracy"] == "yes"
    assert record["fields"]["attest_privacy_notice"] == "yes"


def test_browser_validation_blocks_dispatch(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> tuple[Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            form = (await browser.open(server.url("/jobs/standard/apply"))).form
            # An incomplete packet (the runner would never submit it) leaves required fields empty.
            await browser.fill(form, kit.build(form, {"first_name": "Avery"}).packet)
            return await browser.submit(), await browser.confirm()
        finally:
            await browser.close()

    action, observation = kit.run(scenario())
    assert not action.dispatched
    assert observation.outcome is SubmissionOutcome.NOT_SUBMITTED
    assert observation.next_state is NotSubmittedNext.FILLING
    assert any(e.startswith("Last name:") for e in observation.validation_errors)
    summary = server.submissions()
    assert (summary["accepted_count"], summary["rejected_count"]) == (0, 0)  # nothing was sent


def test_site_validation_rejection_then_corrected_resubmission(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            form = (await browser.open(server.url("/jobs/validation/apply"))).form
            await browser.fill(form, kit.build(form, kit.CORE).packet)
            await browser.submit()
            rejected = await browser.confirm()
            # NOT_SUBMITTED is definite, so a corrected retry is allowed.
            again = await browser.inspect()
            phone = again.form.field("phone")
            corrected = kit.build(again.form, {**kit.pick(again.form, kit.CORE), "phone": "3035550142"})
            fill = await browser.fill(again.form, corrected.packet)
            await browser.submit()
            return rejected, again, phone, (fill, await browser.confirm())
        finally:
            await browser.close()

    rejected, again, phone, (fill, accepted) = kit.run(scenario())
    assert rejected.outcome is SubmissionOutcome.NOT_SUBMITTED
    assert rejected.next_state is NotSubmittedNext.NEEDS_INPUT
    assert any("10-digit US phone number" in e for e in rejected.validation_errors)
    assert "10-digit US phone number" in (phone.validation_error or "")
    assert phone.fingerprint == next(f for f in again.form.fields if f.id == "phone").fingerprint
    assert not again.form.field("resume").required  # the site kept the upload
    assert fill.ok
    assert accepted.outcome is SubmissionOutcome.ACCEPTED
    summary = server.submissions("validation")
    assert (summary["accepted_count"], summary["rejected_count"]) == (1, 1)
    assert summary["submissions"][0]["fields"]["phone"] == "3035550142"


def test_multistep_navigation_review_and_submit(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> dict[str, Any]:
        seen: dict[str, Any] = {"steps": []}
        browser = await PlaywrightSessionFactory().start(options)
        try:
            inspection = await browser.open(server.url("/jobs/multistep"))
            answers = {**kit.CORE, **kit.STANDARD}
            # A step rejected by the site does not advance.
            form = inspection.form
            await browser.fill(form, kit.build(form, {"first_name": "Avery"}).packet)
            assert form.is_final_step is False
            # The browser's own validation blocks the step: nothing is clicked.
            seen["blocked_nav"] = await browser.advance()
            # A site that skips browser validation rejects the step server-side instead.
            await browser.page.evaluate("() => { document.forms[0].noValidate = true; }")
            nav = await browser.advance()
            seen["rejected_nav"] = nav
            while True:
                inspection = await browser.inspect()
                form = inspection.form
                seen["steps"].append((form.step, form.is_final_step, [f.id for f in form.fields]))
                await browser.fill(form, kit.build(form, kit.pick(form, answers)).packet)
                if form.is_final_step:
                    break
                nav = await browser.advance()
                assert nav.advanced, nav.validation_errors
                assert server.submissions()["accepted_count"] == 0
            with pytest.raises(SubmissionRefused):
                await browser.advance()
            seen["action"] = await browser.submit()
            seen["observation"] = await browser.confirm()
            return seen
        finally:
            await browser.close()

    seen = kit.run(scenario())
    blocked = seen["blocked_nav"]
    assert not blocked.advanced
    assert any(e.startswith("Last name:") for e in blocked.validation_errors)
    rejected = seen["rejected_nav"]
    assert not rejected.advanced
    assert "Last name: This field is required." in " ".join(rejected.validation_errors)
    assert rejected.inspection.form.step == 0
    assert server.submissions("multistep")["rejected_count"] == 1
    assert seen["steps"] == [
        (0, False, ["first_name", "last_name", "email", "phone", "linkedin_url"]),
        (1, False, ["resume", "years_experience", "work_authorization", "sponsorship"]),
        (2, False, ["skills", "work_arrangements", "open_to_relocation", "why_brambleway"]),
        (3, True, []),
    ]
    assert seen["observation"].outcome is SubmissionOutcome.ACCEPTED
    summary = server.submissions("multistep")
    assert summary["accepted_count"] == 1
    record = summary["submissions"][0]
    assert record["fields"]["skills"] == ["sk_python", "sk_sql", "sk_spark", "sk_dbt"]
    assert record["files"]["resume"]["sha256"] == hashlib.sha256(kit.RESUME_PATH.read_bytes()).hexdigest()


def test_captcha_is_left_to_the_user_then_submitted(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> tuple[Any, Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            first = await browser.open(server.url("/jobs/captcha/apply"))
            token = await browser.page.get_attribute("input[name=captcha_token]", "value")
            answer = server.api("GET", f"/__test__/captcha/{token}")["answer"]  # the "person"
            waiter = asyncio.create_task(browser.wait_for_user("solve the CAPTCHA", timeout_s=10))
            await browser.page.fill("#f-captcha_answer", answer)
            ready = await waiter
            form = ready.form
            await browser.fill(form, kit.build(form, kit.CORE).packet)
            await browser.submit()
            return first, ready, await browser.confirm()
        finally:
            await browser.close()

    first, ready, observation = kit.run(scenario())
    assert first.kind is PageKind.CAPTCHA
    assert ready.kind is PageKind.APPLICATION_FORM
    assert "captcha_answer" not in {f.id for f in ready.form.fields}  # never touched by the runtime
    assert observation.outcome is SubmissionOutcome.ACCEPTED
    assert server.submissions("captcha")["accepted_count"] == 1


def test_uncertain_outcome_is_unknown_blocks_retry_and_reconciles_later(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, store: ApplicationStore
) -> None:
    """Runner recipe against the real store: SUBMITTING before the click, UNKNOWN
    afterwards, retry blocked, then reconciliation through the public status page."""
    url = server.url("/jobs/uncertain")
    request = store.record_request("cand_fixture_avery_quill", url)
    app = request.application
    claim = store.claim(app.id, "c4-test")

    async def attempt() -> tuple[Any, Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(url)
            store.transition(claim, ApplicationState.INSPECTING)
            bound = store.bind_job_identity(claim, page.job_identity)
            built = kit.build(page.form, kit.CORE, application_id=app.id, job_id=bound.job.id,
                              candidate_id=app.candidate_id)
            store.save_packet(claim, built.packet)
            store.transition(claim, ApplicationState.PACKET_READY)
            store.transition(claim, ApplicationState.FILLING)
            await browser.fill(page.form, built.packet)
            sub = store.begin_submission(claim, packet_id=built.packet.id)
            assert store.get_application(app.id).state is ApplicationState.SUBMITTING
            await browser.submit()
            observation = await browser.confirm()
            with pytest.raises(SubmissionRefused):
                await browser.submit()  # the same session never retries an unknown outcome
            return sub, observation, bound.job
        finally:
            await browser.close()

    sub, observation, job = kit.run(attempt())
    assert observation.outcome is SubmissionOutcome.UNKNOWN
    assert server.submissions("uncertain")["accepted_count"] == 1  # it was received
    after = store.record_submission_outcome(claim, sub.id, observation)
    assert after.state is ApplicationState.SUBMISSION_UNKNOWN
    assert store.record_request(app.candidate_id, url).disposition is RequestDisposition.SUBMISSION_UNKNOWN

    tie = ConfirmationTie.from_job(job)
    assert tie.external_job_id == "BWA-SRE-108"

    async def reread() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            return await browser.reconcile(url, tie=tie, lookup_email=kit.CORE["email"])
        finally:
            await browser.close()

    pending = kit.run(reread())
    assert pending.outcome is SubmissionOutcome.UNKNOWN
    assert any("still processing" in s for s in pending.signals)
    assert reconciliation_from(pending) is None

    record = server.submissions("uncertain")["submissions"][0]
    server.api("POST", f"/__test__/submissions/{record['submission_id']}/reveal")  # site catches up
    confirmed = kit.run(reread())
    assert confirmed.outcome is SubmissionOutcome.ACCEPTED
    assert confirmed.confirmation_reference == record["confirmation_reference"]
    reconciliation = reconciliation_from(confirmed, method=ReconciliationMethod.ATS_CANDIDATE_PORTAL)
    assert reconciliation is not None
    final = store.reconcile_submission(claim, reconciliation)
    assert final.state is ApplicationState.SUBMITTED
    receipt = store.get_receipt(app.id)
    assert receipt is not None and receipt.confirmation_reference == record["confirmation_reference"]
    repeat = store.record_request(app.candidate_id, url)
    assert repeat.disposition is RequestDisposition.ALREADY_SUBMITTED and not repeat.may_proceed
    assert server.submissions("uncertain")["accepted_count"] == 1  # never resubmitted


def test_generic_thank_you_is_not_acceptance(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> tuple[Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url("/jobs/vague-confirmation"))
            await browser.fill(page.form, kit.build(page.form, kit.CORE).packet)
            await browser.submit()
            observation = await browser.confirm()
            tie = ConfirmationTie.from_identity(page.job_identity, None)
            later = await browser.reconcile(server.url("/jobs/vague-confirmation"), tie=tie,
                                            lookup_email=kit.CORE["email"])
            return observation, later
        finally:
            await browser.close()

    observation, later = kit.run(scenario())
    assert observation.outcome is SubmissionOutcome.UNKNOWN  # "Thank you!" names nothing
    assert any("new page loaded" in s for s in observation.signals)
    assert later.outcome is SubmissionOutcome.ACCEPTED
    record = server.submissions("vague-confirmation")["submissions"][0]
    assert later.confirmation_reference == record["confirmation_reference"]
    assert server.submissions()["accepted_count"] == 1


def test_unsupported_control_blocks_submit_until_the_user_operates_it(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url("/jobs/custom-control/apply"))
            form = page.form
            built = kit.build(form, kit.CORE)  # preferred_office reported missing
            assert built.packet.unresolved_fields == ["preferred_office"]
            await browser.fill(form, built.packet)
            refused = await browser.submit()
            blocked = await browser.confirm()
            waiter = asyncio.create_task(browser.wait_for_user("choose an office", timeout_s=10))
            await browser.page.click("#f-preferred_office")
            await browser.page.click("#f-preferred_office-list [data-value=office_den]")
            ready = (await waiter).form
            await browser.fill(ready, kit.build(ready, kit.CORE).packet)
            await browser.submit()
            return refused, blocked, await browser.confirm()
        finally:
            await browser.close()

    refused, blocked, accepted = kit.run(scenario())
    assert not refused.dispatched and "preferred_office" in (refused.detail or "")
    assert blocked.outcome is SubmissionOutcome.NOT_SUBMITTED
    assert blocked.next_state is NotSubmittedNext.NEEDS_INPUT
    assert accepted.outcome is SubmissionOutcome.ACCEPTED
    record = server.submissions("custom-control")["submissions"][0]
    assert record["fields"]["preferred_office"] == "office_den"
    assert "website_hp" not in record["extra_fields"]  # honeypot left blank


def test_ambiguous_primary_action_is_never_clicked(kit: SimpleNamespace, server: Any, options: BrowserOptions, tmp_path: Path) -> None:
    page_html = """<!doctype html><html><body><h1>Apply</h1>
      <form method="post" action="/jobs/standard/apply">
        <label for="n">Full name</label><input id="n" name="full_name" required>
        <label for="e">Email</label><input id="e" name="email" type="email" required>
        <button type="submit">Go</button><button type="submit" name="x">Next</button>
      </form></body></html>"""

    async def scenario() -> tuple[Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.page.goto(server.url("/"))
            await browser.page.set_content(page_html)
            inspection = await browser.inspect()
            with pytest.raises(AmbiguousAction):
                await browser.advance()
            return inspection, await browser.submit()
        finally:
            await browser.close()

    inspection, action = kit.run(scenario())
    assert inspection.kind is PageKind.APPLICATION_FORM
    assert inspection.form.is_final_step is None and inspection.message
    assert not action.dispatched
    assert server.submissions()["accepted_count"] == 0


def test_user_inputs_bind_to_the_inspected_question(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    """A UserInput answered for this inspection matches it; the same field id on a
    re-rendered question with different wording would not."""

    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            return (await browser.open(server.url("/jobs/missing-required/apply"))).form
        finally:
            await browser.close()

    form = kit.run(scenario())
    built = kit.build(form, {**kit.CORE, "notice_period": "2 weeks", "salary_expectation": "150000",
                             "faa_part_107": "No"})
    assert built.packet.problems_against(form) == []
    user = next(u for u in built.user_inputs if u.field_id == "salary_expectation")
    assert isinstance(user, UserInput) and user.matches(form)
    reworded = form.model_copy(update={"fields": [
        f.model_copy(update={"help_text": "Include bonus"}) if f.id == "salary_expectation" else f
        for f in form.fields
    ]})
    assert not user.matches(reworded)


PRECHECKED = """<!doctype html><html><body><h1>Apply</h1>
  <form method="post" action="/jobs/standard/apply">
    <label for="e">Email</label><input id="e" name="email" type="email" required>
    <input type="checkbox" id="m" name="marketing" value="yes" checked>
    <label for="m">Send me marketing emails about future roles</label>
    <button type="submit">Submit application</button>
  </form></body></html>"""


def test_prechecked_consent_is_not_left_as_an_answer(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> tuple[Any, Any, bool]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.page.goto(server.url("/"))
            await browser.page.set_content(PRECHECKED)
            form = (await browser.inspect()).form
            fill = await browser.fill(form, kit.build(form, {"email": kit.CORE["email"]}).packet)
            return form, fill, await browser.page.is_checked("#m")
        finally:
            await browser.close()

    form, fill, still_checked = kit.run(scenario())
    assert form.field("marketing").semantic_type is SemanticType.CONSENT
    result = next(r for r in fill.fields if r.field_id == "marketing")
    assert result.status is FieldFillStatus.SKIPPED and "pre-checked" in (result.detail or "")
    assert not still_checked


def test_questions_added_after_filling_block_submit(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> tuple[Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            form = (await browser.open(server.url("/jobs/standard/apply"))).form
            await browser.fill(form, kit.build(form, {**kit.CORE, **kit.STANDARD}).packet)
            # The site reveals a follow-up question after an answer was chosen.
            await browser.page.evaluate("""() => {
                const d = document.createElement('div');
                d.innerHTML = '<label for="f-x">Which visa type do you hold?</label>' +
                              '<input id="f-x" name="visa_type" required>';
                document.querySelector('form').insertBefore(d, document.querySelector('form button'));
            }""")
            return await browser.submit(), await browser.confirm()
        finally:
            await browser.close()

    action, observation = kit.run(scenario())
    assert not action.dispatched and "changed after filling" in (action.detail or "")
    assert observation.outcome is SubmissionOutcome.NOT_SUBMITTED
    assert observation.next_state is NotSubmittedNext.FILLING
    assert server.submissions()["accepted_count"] == 0


ROUTED_FORM = """<!doctype html><html><body><h1>Widget Engineer</h1>
  <p>Job ID ABC-123</p>
  <form method="post" action="/routed-submit">
    <label for="n">Full name</label><input id="n" name="full_name" required>
    <label for="e">Email</label><input id="e" name="email" type="email" required>
    <button type="submit">Submit application</button>
  </form></body></html>"""


@pytest.mark.parametrize(
    ("result_body", "expected"),
    [
        ("<h1>Application submitted</h1><p>Thanks for your interest.</p>", SubmissionOutcome.UNKNOWN),
        ("<h1>Application submitted</h1><p>Widget Engineer · Job ID XYZ-999</p>", SubmissionOutcome.UNKNOWN),
        ("<h1>Thank you!</h1><p>Job ID ABC-123</p>", SubmissionOutcome.UNKNOWN),
        ("<h1>Application submitted</h1><p>Job ID ABC-123</p>", SubmissionOutcome.ACCEPTED),
        ("<h1>Thank you for applying</h1><p>Reference: RX-4471</p>", SubmissionOutcome.ACCEPTED),
    ],
)
def test_acceptance_requires_a_tie_to_this_application(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, result_body: str, expected: SubmissionOutcome
) -> None:
    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            async def respond(route: Any) -> None:
                await route.fulfill(status=200, content_type="text/html",
                                    body=f"<!doctype html><html><body>{result_body}</body></html>")

            await browser.page.route("**/routed-submit", respond)
            await browser.page.goto(server.url("/"))
            await browser.page.set_content(ROUTED_FORM)
            form = (await browser.inspect()).form
            await browser.fill(form, kit.build(form, {"full_name": "Avery Quill",
                                                     "email": kit.CORE["email"]}).packet)
            assert (await browser.submit()).dispatched
            return await browser.confirm()
        finally:
            await browser.close()

    observation = kit.run(scenario())
    assert observation.outcome is expected, observation.signals
