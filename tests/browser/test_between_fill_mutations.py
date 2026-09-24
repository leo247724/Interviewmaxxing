"""Re-inspect between writes in real Chromium; the OpenCLI transport is local/fake."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

from interviewmaxxing_browser.opencli import CommandResult, OpenCliConfig, OpenCliDriver
from interviewmaxxing_browser.runtime import GenericApplicationBrowser
from interviewmaxxing_browser.session import PlaywrightSessionFactory
from interviewmaxxing_core import FieldFillStatus

URL = 'https://synthetic.test/apply'
FORM = '''<!doctype html><title>Job Application for Tester at Example Co</title>
<meta property="og:site_name" content="Example Co"><h1>Tester</h1><p>Job ID TEST-101</p>
<script type="application/ld+json">{"@type":"JobPosting","identifier":"TEST-101",
"title":"Tester","hiringOrganization":{"name":"Example Co"}}</script>
<form id="application" method="post" action="/submit">
<div><label for="first">First name</label><input id="first" name="first_name" required></div>
<div><label for="email">Email</label><input id="email" name="email" type="email"
 aria-describedby="email-help" required><span id="email-help">Your contact email</span></div>
<div><label for="office">Preferred office</label><select id="office" name="office" required>
<option value="">Choose</option><option value="remote">Remote</option><option value="local">Local</option>
</select></div>
<div><label for="last">Last name</label><input id="last" name="last_name"></div>
<button type="submit">Submit application</button></form>
<script>window.submissions=0; document.querySelector('form').onsubmit=e=>{window.submissions++;e.preventDefault()};</script>'''


class CountingAnnotator:
    def __init__(self) -> None:
        self.calls = 0
        self.callback: Any = None

    def annotate(self, form: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.callback is not None:
            self.callback()
        return form


class LocalOpenCli:
    """Exercise the real OpenCliDriver against an isolated headless test page.

    No subprocess, extension, profile, live lease, or user browser is involved.
    Its normal read-only script allowlist and per-command verification still run.
    """

    def __init__(self, page: Any) -> None:
        self.page = page
        self.mutations: list[tuple[str, str]] = []

    async def __call__(self, argv: Sequence[str], timeout_s: float) -> CommandResult:
        args = list(argv)
        i = args.index('browser')
        command = args[i + 2]
        positional = args[args.index('--') + 1:] if '--' in args else []
        payload: Any
        if command == 'tab':
            payload = [{'page': 'LOCAL-FIXTURE'}] if args[i + 3] == 'list' else {'page': 'LOCAL-FIXTURE'}
        elif command == 'open':
            await self.page.goto(positional[0])
            payload = {'page': 'LOCAL-FIXTURE', 'url': self.page.url}
        elif command == 'eval':
            payload = await self.page.evaluate(positional[0])
        elif command == 'screenshot':
            await self.page.screenshot(path=positional[0])
            payload = {'ok': True}
        else:
            selector = positional[0]
            locator = self.page.locator(selector)
            assert await locator.count() == 1
            self.mutations.append((command, selector))
            payload = {'matches_n': 1, 'match_level': 'exact'}
            if command == 'fill':
                await locator.fill(positional[1])
                payload.update(verified=True, actual=await locator.input_value())
            elif command == 'select':
                await locator.select_option(value=positional[1])
            elif command == 'click':
                await locator.click()
            elif command in ('check', 'uncheck'):
                await locator.set_checked(command == 'check')
                payload['checked'] = await locator.is_checked()
            else:
                raise AssertionError(f'unexpected mutation {command}')
        return CommandResult(0, json.dumps(payload), '')


async def run_case(kind: str, options: Any, callback: Any, *, html: str = FORM,
                   annotated: bool = True) -> None:
    router = CountingAnnotator() if annotated else None
    headless = await PlaywrightSessionFactory(annotator=router).start(
        replace(options, allow_submission=False))
    posts: list[str] = []

    async def serve(route: Any) -> None:
        if route.request.method != 'GET':
            posts.append(route.request.url)
        await route.fulfill(content_type='text/html', body=html)

    await headless.page.route('https://synthetic.test/**', serve)
    bridge = LocalOpenCli(headless.page)
    browser = headless if kind == 'playwright' else GenericApplicationBrowser(
        OpenCliDriver(OpenCliConfig(session='imx-fixture-no-real-lease'), runner=bridge),
        replace(options, allow_submission=False), annotator=router,
    )
    try:
        form = (await browser.open(URL)).form
        await callback(browser, headless.page, form, router, bridge)
        assert await headless.page.evaluate('window.submissions') == 0
        assert posts == []
    finally:
        await headless.close()


MUTATIONS = [
    "document.querySelector('[for=email]').textContent=\"Mother's maiden name\";document.querySelector('#email').type='text'",
    "document.querySelector('#email-help').textContent='Enter your account recovery answer'",
    "document.querySelector('#email').required=false",
    "document.querySelector('#email').maxLength=4",
    "document.querySelector('#office option[value=remote]').textContent='Relocation required'",
    "document.querySelector('#office option[value=remote]').disabled=true",
    "document.querySelector('form').action='/different-employer/submit'",
    "document.querySelector('button').textContent='Continue to payment'",
    "document.title='Job Application for Another Role at Different Employer'",
    "document.querySelector('h1').textContent='Different Role'",
    "document.querySelector('meta').content='Different Employer'",
    "document.querySelector('script[type=\"application/ld+json\"]').textContent=JSON.stringify({'@type':'JobPosting',identifier:'TEST-999',title:'Tester',hiringOrganization:{name:'Different Employer'}})",
]


@pytest.mark.parametrize('kind', ['playwright', 'opencli'])
@pytest.mark.parametrize('mutation', MUTATIONS)
def test_first_write_cannot_authorize_changed_later_questions(
    kind: str, mutation: str, options: Any, kit: Any,
) -> None:
    async def check(browser: Any, page: Any, form: Any, router: Any, bridge: Any) -> None:
        packet = kit.build(form, {'first_name': 'Avery', 'email': 'avery@example.test',
                                  'office': 'Remote', 'last_name': 'Quill'}).packet
        await page.evaluate('(code)=>document.querySelector("#first").addEventListener("input",()=>{(0,eval)(code)},{once:true})', mutation)
        calls = router.calls
        result = await browser.fill(form, packet)
        assert not result.ok
        assert any('changed while filling' in error for error in result.page_errors)
        assert await page.locator('#first').input_value() == 'Avery'
        assert await page.locator('#email').input_value() == ''
        assert await page.locator('#office').input_value() == ''
        assert await page.locator('#last').input_value() == ''
        assert not any(f.field_id == 'email' and f.status is FieldFillStatus.FILLED for f in result.fields)
        assert router.calls - calls == 1  # initial fill observation only, no per-write classification
        assert browser._active_fill_signature is None
        if kind == 'opencli':
            assert bridge.mutations == [('fill', '#first')]
        assert not (await browser.submit()).dispatched
        with pytest.raises(ValueError, match='inspect it again first'):
            await browser.fill(form, packet)
    asyncio.run(run_case(kind, options, check))


@pytest.mark.parametrize('kind', ['playwright', 'opencli'])
def test_freshness_guard_also_applies_without_semantic_provider(kind: str, options: Any, kit: Any) -> None:
    async def check(browser: Any, page: Any, form: Any, router: Any, bridge: Any) -> None:
        packet = kit.build(form, {'first_name': 'Avery', 'email': 'avery@example.test'}).packet
        await page.evaluate("() => {document.querySelector('#first').oninput=()=>document.querySelector('[for=email]').textContent='Secret answer'}")
        result = await browser.fill(form, packet)
        assert not result.ok and await page.locator('#email').input_value() == ''
    asyncio.run(run_case(kind, options, check, annotated=False))


@pytest.mark.parametrize('kind', ['playwright', 'opencli'])
def test_unchanged_form_fills_all_fields_without_per_write_provider_calls(kind: str, options: Any, kit: Any) -> None:
    async def check(browser: Any, page: Any, form: Any, router: Any, bridge: Any) -> None:
        packet = kit.build(form, {'first_name': 'Avery', 'email': 'avery@example.test',
                                  'office': 'Remote', 'last_name': 'Quill'}).packet
        calls = router.calls
        result = await browser.fill(form, packet)
        assert result.ok, result
        assert await page.locator('#email').input_value() == 'avery@example.test'
        assert await page.locator('#office').input_value() == 'remote'
        assert await page.locator('#last').input_value() == 'Quill'
        assert router.calls - calls == 2  # existing before/after model calls only
        assert browser._active_fill_signature is None
        assert not (await browser.submit()).dispatched
    asyncio.run(run_case(kind, options, check))


@pytest.mark.parametrize('kind', ['playwright', 'opencli'])
def test_checkbox_group_rechecks_between_individual_option_writes(kind: str, options: Any, kit: Any) -> None:
    html = FORM.replace('<div><label for="first">', '''<fieldset><legend>Work arrangements</legend>
    <input id="remote" type="checkbox" name="arrangements" value="remote"><label for="remote">Remote</label>
    <input id="hybrid" type="checkbox" name="arrangements" value="hybrid"><label for="hybrid">Hybrid</label>
    </fieldset><div><label for="first">''')

    async def check(browser: Any, page: Any, form: Any, router: Any, bridge: Any) -> None:
        packet = kit.build(form, {'arrangements': ['Remote', 'Hybrid'], 'email': 'avery@example.test'}).packet
        await page.evaluate("() => {document.querySelector('#remote').onchange=()=>document.querySelector('[for=hybrid]').textContent='I certify a new attestation'}")
        result = await browser.fill(form, packet)
        assert not result.ok
        assert await page.locator('#remote').is_checked()
        assert not await page.locator('#hybrid').is_checked()
        assert await page.locator('#email').input_value() == ''
        if kind == 'opencli':
            assert bridge.mutations == [('check', '#remote')]
    asyncio.run(run_case(kind, options, check, html=html))


@pytest.mark.parametrize('kind', ['playwright', 'opencli'])
def test_section_subject_change_between_fills_holds(kind: str, options: Any, kit: Any) -> None:
    html = FORM.replace('<div><label for="email">', '<section id="subject" aria-label="Your contact details"><div><label for="email">')
    html = html.replace('</span></div>', '</span></div></section>')

    async def check(browser: Any, page: Any, form: Any, router: Any, bridge: Any) -> None:
        # The section subject is model context, not question wording; changing it
        # still invalidates the observation the fill was authorized against.
        assert form.field('email').section_context == ['Your contact details']
        assert 'Your contact details' not in (form.field('email').help_text or '')
        packet = kit.build(form, {'first_name': 'Avery', 'email': 'avery@example.test'}).packet
        await page.evaluate("() => {document.querySelector('#first').oninput=()=>document.querySelector('#subject').setAttribute('aria-label', 'Your supervisor contact details')}")
        result = await browser.fill(form, packet)
        assert not result.ok and await page.locator('#email').input_value() == ''
    asyncio.run(run_case(kind, options, check, html=html))


COMBO = FORM.replace(
    '<label for="office">Preferred office</label><select id="office" name="office" required>\n'
    '<option value="">Choose</option><option value="remote">Remote</option><option value="local">Local</option>\n'
    '</select>',
    '<label id="office-label">Preferred office</label><button type="button" id="office" role="combobox" '
    'aria-labelledby="office-label" aria-describedby="office-help" aria-haspopup="listbox" '
    'aria-expanded="false" aria-controls="office-menu" aria-required="true">Choose</button>'
    '<span id="office-help">For yourself</span><input type="hidden" name="office" value="">',
) + '''
<div role="listbox" id="office-menu" hidden>
<div role="option" id="remote-option" data-value="remote" aria-selected="false">Remote</div>
<div role="option" id="local-option" data-value="local" aria-selected="false">Local</div></div>
<script>
window.optionClicks=0;
document.querySelector('#office').onclick=()=>{
 document.querySelector('#office-menu').hidden=false;
 document.querySelector('#office').setAttribute('aria-expanded','true');
};
document.querySelector('#office-menu').onclick=e=>{
 const option=e.target.closest('[role=option]'); if(!option)return;
 window.optionClicks++;
 document.querySelector('#office').textContent=option.textContent;
 document.querySelector('[name=office]').value=option.dataset.value;
 option.setAttribute('aria-selected','true');
 document.querySelector('#office-menu').hidden=true;
 document.querySelector('#office').setAttribute('aria-expanded','false');
};
</script>'''


@pytest.mark.parametrize('kind', ['playwright', 'opencli'])
@pytest.mark.parametrize('mutation', [
    "document.querySelector('#office-help').textContent='For your most recent supervisor'",
    "document.title='Application to a different employer'",
    "document.querySelector('#remote-option').textContent='On-site only'",
    "document.querySelector('form').action='/new-employer/submit'",
])
def test_aria_open_cannot_authorize_choice_after_question_changes(
    kind: str, mutation: str, options: Any, kit: Any,
) -> None:
    async def check(browser: Any, page: Any, form: Any, router: Any, bridge: Any) -> None:
        packet = kit.build(form, {'first_name': 'Avery', 'email': 'avery@example.test',
                                  'office': 'Remote', 'last_name': 'Quill'}).packet
        await page.evaluate('(code)=>document.querySelector("#office").addEventListener("click",()=>{(0,eval)(code)},{once:true})', mutation)
        result = await browser.fill(form, packet)
        assert not result.ok
        assert await page.evaluate('window.optionClicks') == 0
        assert await page.locator('#last').input_value() == ''
        assert not any(f.field_id == 'office' and f.status is FieldFillStatus.FILLED for f in result.fields)
        if kind == 'opencli':
            assert [command for command in bridge.mutations if command[0] == 'click'] == [('click', '#office')]
    asyncio.run(run_case(kind, options, check, html=COMBO))


@pytest.mark.parametrize('kind', ['playwright', 'opencli'])
def test_unchanged_aria_open_remains_supported(kind: str, options: Any, kit: Any) -> None:
    async def check(browser: Any, page: Any, form: Any, router: Any, bridge: Any) -> None:
        packet = kit.build(form, {'first_name': 'Avery', 'email': 'avery@example.test',
                                  'office': 'Remote', 'last_name': 'Quill'}).packet
        calls = router.calls
        result = await browser.fill(form, packet)
        assert result.ok, result
        assert await page.evaluate('window.optionClicks') == 1
        assert await page.locator('#last').input_value() == 'Quill'
        assert router.calls - calls == 2
    asyncio.run(run_case(kind, options, check, html=COMBO))


@pytest.mark.parametrize('kind', ['playwright', 'opencli'])
@pytest.mark.parametrize('mutation', MUTATIONS[8:])
def test_context_changed_after_inspection_refuses_stale_packet(
    kind: str, mutation: str, options: Any, kit: Any,
) -> None:
    async def check(browser: Any, page: Any, form: Any, router: Any, bridge: Any) -> None:
        packet = kit.build(form, {'first_name': 'Avery', 'email': 'avery@example.test'}).packet
        await page.evaluate(mutation)
        with pytest.raises(ValueError, match='changed'):
            await browser.fill(form, packet)
        assert await page.locator('#first').input_value() == ''
        assert await page.locator('#email').input_value() == ''
        if kind == 'opencli':
            assert bridge.mutations == []
    asyncio.run(run_case(kind, options, check))


@pytest.mark.parametrize('kind', ['playwright', 'opencli'])
@pytest.mark.parametrize('mutation', MUTATIONS[8:])
def test_context_changed_during_provider_await_is_reobserved(
    kind: str, mutation: str, options: Any,
) -> None:
    async def check(browser: Any, page: Any, form: Any, router: Any, bridge: Any) -> None:
        loop = asyncio.get_running_loop()
        before = browser.annotation_document_id
        calls = router.calls
        changed = False

        def change_once() -> None:
            nonlocal changed
            if not changed:
                changed = True
                asyncio.run_coroutine_threadsafe(page.evaluate(mutation), loop).result(5)

        router.callback = change_once
        fresh = await browser.inspect()
        assert fresh.form is not None
        assert router.calls - calls == 2  # stale annotation rejected, one bounded fresh retry
        assert browser.annotation_document_id != before
        assert await page.locator('#first').input_value() == ''
        if kind == 'opencli':
            assert bridge.mutations == []
    asyncio.run(run_case(kind, options, check))
