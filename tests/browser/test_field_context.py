"""Section headings and group labels are model context (``section_context``), never
part of the question the user sees: ``question_text`` and the fingerprint stay the
bare wording, so exact-wording saved answers keep matching, while the observation
signature (routing) still notices a changed heading."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_browser.annotations import observation_signature
from interviewmaxxing_core import BrowserOptions, PageKind


def test_section_subjects_are_context_not_question_wording(
    kit: SimpleNamespace, server: Any, options: BrowserOptions,
) -> None:
    async def scenario() -> None:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.page.route("**/context", lambda route: route.fulfill(
                content_type="text/html", body="""
                <form method="post"><section><h2>Applicant contact</h2>
                  <label>Email<input id="self_email" type="email"></label></section>
                <section><h2 id="reference_heading">Professional reference</h2>
                  <label>Email<input id="reference_email" type="email"></label></section>
                <button type="submit">Submit application</button></form>"""))
            first = await browser.observe(server.url("/context"))
            assert first.kind is PageKind.APPLICATION_FORM and first.form is not None
            assert browser.last_page is not None
            signature = observation_signature(browser.last_page)
            self_email = first.form.field("self_email")
            reference = first.form.field("reference_email")
            assert self_email.section_context == ["Applicant contact"]
            assert reference.section_context == ["Professional reference"]
            assert self_email.help_text is None and reference.help_text is None
            assert self_email.question_text == reference.question_text == "Email"
            # Same bare question: an exact-wording saved answer for "Email" fits both.
            assert self_email.fingerprint == reference.fingerprint
            await browser.page.locator("#reference_heading").evaluate(
                "node => node.textContent = 'Most recent supervisor'")
            fresh = await browser.inspect()
            assert fresh.form is not None
            assert fresh.form.field("reference_email").section_context == ["Most recent supervisor"]
            assert fresh.form.field("reference_email").fingerprint == reference.fingerprint
            # The changed subject still invalidates the page observation used for routing.
            assert observation_signature(browser.last_page) != signature
        finally:
            await browser.close()
    kit.run(scenario())


def test_nested_history_and_aria_group_context_do_not_include_other_form(
    kit: SimpleNamespace, server: Any, options: BrowserOptions,
) -> None:
    async def scenario() -> None:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.page.route("**/context", lambda route: route.fulfill(
                content_type="text/html", body="""
                <form><h2>Unrelated newsletter</h2><input name="search"></form>
                <form method="post"><section><h2>Employment history</h2>
                 <fieldset><legend>Previous employer</legend>
                  <label>City<input name="city"></label></fieldset></section>
                 <div role="group" aria-label="Emergency contact">
                  <label>Phone<input name="phone" type="tel"></label></div>
                 <button type="submit">Submit application</button></form>"""))
            result = await browser.observe(server.url("/context"))
            assert result.form is not None
            city = result.form.field("city")
            phone = result.form.field("phone")
            assert city.help_text == "Previous employer"  # its own legend stays question text
            assert city.section_context == ["Employment history", "Previous employer"]
            assert phone.help_text == "Emergency contact"  # its own group label, likewise
            assert phone.section_context == ["Emergency contact"]
            for f in result.form.fields:
                assert "newsletter" not in (f.help_text or "")
                assert not any("newsletter" in part for part in f.section_context)
        finally:
            await browser.close()
    kit.run(scenario())


def test_mock_form_questions_are_bare_wording_with_the_heading_as_context(
    kit: SimpleNamespace, server: Any, options: BrowserOptions,
) -> None:
    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            return (await browser.open(server.url("/jobs/standard/apply"))).form
        finally:
            await browser.close()

    form = kit.run(scenario())
    work_auth = form.field("work_authorization")
    assert work_auth.question_text == "Are you legally authorized to work in the United States?"
    assert work_auth.help_text is None and work_auth.section_context == ["Application form"]
    skills = form.field("skills")
    assert skills.question_text == "Primary skills\nHold Ctrl or Command to select more than one."
    assert skills.section_context == ["Application form"]
    assert all(f.section_context == ["Application form"] for f in form.fields)
