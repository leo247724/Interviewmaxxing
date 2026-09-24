"""Competing visible forms must not receive candidate values by DOM-order tiebreak."""
from __future__ import annotations

import asyncio

import pytest

from interviewmaxxing_browser.session import PlaywrightSessionFactory
from interviewmaxxing_core import ControlType, PageKind

ALERTS = '''<h2>Sign up for job alerts</h2><form id=alerts method=post>
<label>First name<input name=first_name></label>
<label>Email<input name=email type=email></label><button type=submit>Submit</button></form>'''
APPLICATION = '''<h2>Apply for this job</h2><form id=application method=post>
<label>Full name<input name=full_name></label>
<label>Email<input name=email type=email></label><button type=submit>Apply</button></form>'''


async def inspect_html(options, body):
    browser = await PlaywrightSessionFactory().start(options)
    await browser.page.route('https://example.test/apply',
        lambda route: route.fulfill(content_type='text/html', body='<h1>Senior Manager</h1>' + body))
    page = await browser.open('https://example.test/apply')
    return browser, page


@pytest.mark.parametrize('reverse', [False, True])
def test_equal_plausible_forms_hold_without_any_action(options, reverse):
    async def scenario():
        forms = [ALERTS, APPLICATION]
        browser, page = await inspect_html(options, ''.join(reversed(forms) if reverse else forms))
        try:
            assert page.kind is PageKind.UNKNOWN and page.form is None
            assert 'Multiple plausible application forms' in page.message
            assert browser.last_page.form_index is None
            assert browser.last_page.bindings == {}
            assert browser.last_page.buttons == []
            assert not (await browser.submit()).dispatched
            assert await browser.page.locator('input').evaluate_all(
                'inputs => inputs.every(input => input.value === "")')
        finally:
            await browser.close()
    asyncio.run(scenario())


def test_an_extra_alert_field_does_not_authorize_the_larger_wrong_form(options):
    async def scenario():
        larger_alert = ALERTS.replace('<button', '<label>City<input name=city></label><button')
        browser, page = await inspect_html(options, larger_alert + APPLICATION)
        try:
            assert page.kind is PageKind.UNKNOWN and page.form is None
            assert not (await browser.submit()).dispatched
        finally:
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('reverse', [False, True])
def test_unique_dominant_resume_application_is_selected(options, reverse):
    async def scenario():
        application = APPLICATION.replace('<button',
            '<label>Resume<input name=resume type=file></label><button')
        forms = [ALERTS, application]
        browser, page = await inspect_html(options, ''.join(reversed(forms) if reverse else forms))
        try:
            assert page.kind is PageKind.APPLICATION_FORM
            assert browser.last_page.form_selector == '#application'
            assert page.form.field('resume').control_type is ControlType.FILE
        finally:
            await browser.close()
    asyncio.run(scenario())


def test_two_resume_applications_remain_ambiguous(options):
    async def scenario():
        application = APPLICATION.replace('<button',
            '<label>Resume<input name=resume type=file></label><button')
        browser, page = await inspect_html(options,
            application + application.replace('id=application', 'id=second'))
        try:
            assert page.kind is PageKind.UNKNOWN and page.form is None
        finally:
            await browser.close()
    asyncio.run(scenario())


def test_small_get_lookup_does_not_hide_a_single_application(options):
    async def scenario():
        lookup = '<form method=get><label>Search<input name=q></label><button>Search</button></form>'
        browser, page = await inspect_html(options, lookup + APPLICATION)
        try:
            assert page.kind is PageKind.APPLICATION_FORM
            assert browser.last_page.form_selector == '#application'
        finally:
            await browser.close()
    asyncio.run(scenario())


def test_resume_bonus_alone_does_not_override_an_equally_large_competing_form(options):
    async def scenario():
        application = APPLICATION.replace('<button',
            '<label>Resume<input name=resume type=file></label><button')
        alerts = ALERTS.replace('<button', '<label>City<input name=city></label><button')
        browser, page = await inspect_html(options, alerts + application)
        try:
            assert page.kind is PageKind.UNKNOWN and page.form is None
        finally:
            await browser.close()
    asyncio.run(scenario())
