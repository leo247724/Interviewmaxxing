"""Synthetic Chromium checks: owned ARIA options only, no employer/browser leases."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from playwright.async_api import async_playwright

from interviewmaxxing_browser.aria import (
    ARIA_EXPANSION,
    ARIA_OBSERVE,
    ARIA_STATE,
    expand_accessible,
)
from interviewmaxxing_browser.driver import NotActionable, PageContextLost, PlaywrightDriver
from interviewmaxxing_browser.normalize import build_page, detect_ats
from interviewmaxxing_browser.opencli import _ALLOWED_SCRIPTS, _lint_read_script
from interviewmaxxing_browser.snapshot import DomSnapshot, inspector_script
from interviewmaxxing_core import ControlType

FIXTURE = """<!doctype html><title>Apply for a fictional job</title>
<form id="application" onsubmit="window.submissions++; event.preventDefault()">
<label for="choice">Work authorization</label>
<button type="button" id="choice" role="combobox" aria-haspopup="listbox"
 aria-controls="choices" aria-expanded="false" aria-required="true">Choose</button>
<label for="resume">Resume</label><input id="resume" type="file" hidden>
<button type="submit">Submit application</button></form>
<div role="listbox" id="countries" hidden><div role="option" id="foreign">Afghanistan +93</div></div>
<div role="listbox" id="choices" hidden>
 <div role="option" id="yes" data-value="yes" aria-selected="false">Yes</div>
 <div role="option" id="no" data-value="no" aria-selected="false">No</div>
</div>
<script>
window.submissions = 0; window.selections = 0;
const choice = document.getElementById('choice');
choice.onclick = () => {
 const menu = document.getElementById('choices');
 // React-like portal mount: replaces the entire popup before it becomes visible.
 menu.outerHTML = menu.outerHTML;
 document.getElementById('choices').hidden = false;
 choice.setAttribute('aria-expanded', 'true');
};
document.addEventListener('click', event => {
 const option = event.target.closest('#choices [role=option]');
 if (!option) return;
 window.selections++;
 choice.textContent = option.textContent;
 document.querySelectorAll('#choices [role=option]').forEach(o => o.setAttribute('aria-selected', String(o === option)));
 document.getElementById('choices').hidden = true;
 choice.setAttribute('aria-expanded', 'false');
});
</script>"""


async def inspect(page: Any) -> Any:
    return build_page(DomSnapshot.model_validate(await page.evaluate(inspector_script())))


async def scenario(callback: Any, html: str = FIXTURE) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(html)
        try:
            await callback(page, PlaywrightDriver(page, action_timeout_s=0.5))
            assert await page.evaluate('window.submissions') == 0
        finally:
            await browser.close()


def test_exact_owned_options_portal_selection_and_hidden_upload() -> None:
    async def check(page: Any, driver: Any) -> None:
        before = await page.content()
        model = await inspect(page)
        assert await page.content() == before  # inspector did not open a menu or write
        field = next(f for f in model.candidate_fields if f.id == 'choice')
        assert field.control_type is ControlType.SELECT
        assert 'choice' in model.unsupported_pending  # native validity does not cover ARIA widgets
        assert [(o.value, o.label) for o in field.options] == [('yes', 'Yes'), ('no', 'No')]
        assert next(f for f in model.candidate_fields if f.id == 'resume').control_type is ControlType.FILE
        binding = model.bindings['choice']
        assert await driver.select_accessible(binding.selector, ['yes'], binding.aria) == ['yes']
        after = await inspect(page)
        assert after.bindings['choice'].value == 'yes'
        assert 'choice' not in after.unsupported_pending
        assert await page.evaluate('window.selections') == 1
    asyncio.run(scenario(check))


@pytest.mark.parametrize('change', [
    "choice.removeAttribute('aria-controls')",
    "choice.setAttribute('aria-controls', 'missing')",
    "choice.setAttribute('aria-controls', 'choices countries')",
    "document.body.insertAdjacentHTML('beforeend','<div role=combobox aria-controls=choices></div>')",
    "document.getElementById('choices').setAttribute('aria-multiselectable','true')",
    "document.getElementById('choices').insertAdjacentHTML('beforeend','<div role=checkbox>Foreign</div>')",
    "document.getElementById('choices').insertAdjacentHTML('beforeend','<div role=radiogroup><div role=radio>Foreign</div></div>')",
    "document.getElementById('no').textContent='Yes'",
    "document.getElementById('no').setAttribute('data-value','yes')",
    "document.getElementById('no').removeAttribute('id')",
    "document.getElementById('yes').setAttribute('aria-setsize','20')",
    "document.getElementById('yes').innerHTML='<button type=submit>Yes</button>'",
    "document.getElementById('choices').setAttribute('aria-disabled','true')",
    "choice.removeAttribute('type')",  # default-submit buttons never become selection actions
    "document.querySelector('label').textContent='Select all that apply'",
])
def test_ambiguous_controls_hold(change: str) -> None:
    async def check(page: Any, driver: Any) -> None:
        await page.evaluate(change)
        field = next(f for f in (await inspect(page)).candidate_fields if f.id == 'choice')
        assert field.control_type is ControlType.UNSUPPORTED
        assert field.options is None
        assert await page.evaluate('window.selections') == 0
    asyncio.run(scenario(check))


def test_editable_autocomplete_holds_readonly_combobox_supports() -> None:
    async def check(page: Any, driver: Any) -> None:
        await page.evaluate("choice.outerHTML='<input id=choice role=combobox aria-controls=choices aria-autocomplete=list>'")
        field = next(f for f in (await inspect(page)).candidate_fields if f.id == 'choice')
        assert field.control_type is ControlType.UNSUPPORTED
        await page.locator('#choice').evaluate('(el) => el.readOnly = true')
        field = next(f for f in (await inspect(page)).candidate_fields if f.id == 'choice')
        assert field.control_type is ControlType.SELECT
    asyncio.run(scenario(check))


@pytest.mark.parametrize('change', [
    "document.getElementById('no').textContent='Different'",
    "document.getElementById('yes').setAttribute('aria-disabled','true')",
    "choice.setAttribute('aria-controls','countries')",
    "choice.outerHTML=choice.outerHTML.replace('Work authorization','Another') + '<div id=choice></div>'",
])
def test_stale_options_and_ownership_refuse_without_choice(change: str) -> None:
    async def check(page: Any, driver: Any) -> None:
        binding = (await inspect(page)).bindings['choice']
        await page.evaluate(change)
        with pytest.raises(NotActionable):
            await driver.select_accessible(binding.selector, ['yes'], binding.aria)
        assert await page.evaluate('window.selections') == 0
    asyncio.run(scenario(check))


def test_options_changed_while_opening_hold() -> None:
    async def check(page: Any, driver: Any) -> None:
        binding = (await inspect(page)).bindings['choice']
        await page.evaluate("choice.addEventListener('click',()=>document.getElementById('yes').textContent='Changed')")
        with pytest.raises(NotActionable, match='options or ownership changed'):
            await driver.select_accessible(binding.selector, ['yes'], binding.aria)
        assert await page.evaluate('window.selections') == 0
    asyncio.run(scenario(check))


@pytest.mark.parametrize('values', [['missing'], ['yes', 'no'], []])
def test_unknown_and_multi_choices_hold(values: list[str]) -> None:
    async def check(page: Any, driver: Any) -> None:
        binding = (await inspect(page)).bindings['choice']
        with pytest.raises(NotActionable):
            await driver.select_accessible(binding.selector, values, binding.aria)
        assert await page.evaluate('window.selections') == 0
    asyncio.run(scenario(check))


def test_context_binding_never_crosses_documents() -> None:
    async def check(page: Any, driver: Any) -> None:
        binding = (await inspect(page)).bindings['choice']
        stale = {**binding.aria, 'origin': 'different-document'}
        with pytest.raises(PageContextLost):
            await driver.select_accessible(binding.selector, ['yes'], stale)
        assert await page.evaluate('window.selections') == 0
    asyncio.run(scenario(check))


def test_readback_mismatch_is_not_success() -> None:
    async def check(page: Any, driver: Any) -> None:
        binding = (await inspect(page)).bindings['choice']
        await page.evaluate("document.addEventListener('click', e => {if(e.target.id==='yes') choice.textContent='No'})")
        with pytest.raises(NotActionable, match='readback mismatch'):
            await driver.select_accessible(binding.selector, ['yes'], binding.aria)
    asyncio.run(scenario(check))


def test_aria_observers_are_fixed_read_only_opencli_scripts() -> None:
    for script in (ARIA_EXPANSION, ARIA_OBSERVE, ARIA_STATE, inspector_script()):
        assert script in _ALLOWED_SCRIPTS
        _lint_read_script(script)


@pytest.mark.parametrize(('host', 'backend'), [
    ('acme.wd1.myworkdaysite.com', 'workday'), ('apply.workable.com', 'workable'),
    ('www.linkedin.com', 'linkedin_easy_apply'), ('jobs.smartrecruiters.com', 'smartrecruiters'),
])
def test_informational_ats_hosts(host: str, backend: str) -> None:
    assert detect_ats('https://' + host + '/job/123') == backend


def test_identical_control_replacement_while_opening_is_held() -> None:
    async def check(page: Any, driver: Any) -> None:
        binding = (await inspect(page)).bindings['choice']
        await page.evaluate("choice.addEventListener('click',()=>choice.outerHTML=choice.outerHTML)")
        with pytest.raises(NotActionable, match='node was replaced'):
            await driver.select_accessible(binding.selector, ['yes'], binding.aria)
        assert await page.evaluate('window.selections') == 0
    asyncio.run(scenario(check))


def test_disabled_option_is_observed_but_never_selected() -> None:
    async def check(page: Any, driver: Any) -> None:
        await page.evaluate("document.getElementById('yes').setAttribute('aria-disabled','true')")
        model = await inspect(page)
        binding = model.bindings['choice']
        assert binding.aria['options'][0]['disabled'] is True
        with pytest.raises(NotActionable, match='enabled observed option'):
            await driver.select_accessible(binding.selector, ['yes'], binding.aria)
        assert await page.evaluate('window.selections') == 0
    asyncio.run(scenario(check))


def test_opt_in_menu_observation_reinspects_absent_portal_without_selection() -> None:
    async def check(page: Any, driver: Any) -> None:
        await page.evaluate("""() => {
          const html = document.getElementById('choices').outerHTML;
          document.getElementById('choices').remove();
          choice.removeAttribute('aria-controls');
          choice.onclick = () => {
            document.body.insertAdjacentHTML('beforeend', html);
            document.getElementById('choices').hidden = false;
            choice.setAttribute('aria-controls', 'choices');
            choice.setAttribute('aria-expanded', 'true');
          };
        }""")
        assert (await inspect(page)).bindings['choice'].control_type is ControlType.UNSUPPORTED
        snapshot = await expand_accessible(driver, '#choice')
        model = build_page(snapshot)
        assert model.bindings['choice'].control_type is ControlType.SELECT
        assert model.bindings['choice'].aria['visible'] is True
        assert await page.locator('#choice').inner_text() == 'Choose'
        assert await page.evaluate('window.selections') == 0
        # Already expanded means re-observe only, never toggle it closed.
        await expand_accessible(driver, '#choice')
        assert await page.locator('#choices').is_visible()
    asyncio.run(scenario(check))


def test_opt_in_expansion_cannot_click_submit_or_editable_autocomplete() -> None:
    async def check(page: Any, driver: Any) -> None:
        with pytest.raises(NotActionable):
            await expand_accessible(driver, 'button[type=submit]')
        await page.evaluate("choice.outerHTML='<input id=choice role=combobox aria-haspopup=listbox aria-autocomplete=list>'")
        with pytest.raises(NotActionable):
            await expand_accessible(driver, '#choice')
        assert await page.evaluate('window.selections') == 0
    asyncio.run(scenario(check))
