"""Round 7, item 3: BambooHR's pre-filled custom selects on the localhost mock ATS and on a
static copy of the live markup (fictional data, real headless Chromium, nothing submitted).

A Fabric select is a role-less menu button (aria-haspopup, data-menu-id, an aria-label
that repeats what it shows) over a hidden proxy <select> that holds only the chosen
option's id and that the <label> names; its menu is a body portal with a search box and a
role=menu of menu items. It is one field, labelled, named and made required by its proxy,
probed through data-menu-id, verified without opening when it already shows our value,
and otherwise chosen from the menu and read back.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import async_playwright

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_browser.aria import COMBO_STATE
from interviewmaxxing_browser.snapshot import DomSnapshot, inspector_script
from interviewmaxxing_core import BrowserOptions, ControlType, FieldFillStatus

APPLY = "/jobs/bamboohr-like/apply"
PLACEHOLDER = "\N{EN DASH}Select\N{EN DASH}"
CONTACT = {"first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test"}
SHOWN = """() => Object.fromEntries(Array.from(document.querySelectorAll('.fab-Select')).map((s) => [
  s.querySelector('select').name,
  {display: s.querySelector('button.fab-SelectToggle').textContent.trim(),
   proxy: s.querySelector('select').value,
   expanded: s.querySelector('button.fab-SelectToggle').getAttribute('aria-expanded')}]))"""
PORTALS = """() => Array.from(document.querySelectorAll('[data-helium-id]')).map((p) => ({
  id: p.getAttribute('data-helium-id'), shown: getComputedStyle(p).visibility !== 'hidden' && p.style.display !== 'none'}))"""
COUNT_OPENS = """() => {
  window.__opens = {};
  new MutationObserver((records) => {
    for (const r of records) {
      const menu = r.target.getAttribute('data-menu-id');
      if (menu && r.target.getAttribute('aria-expanded') === 'true') window.__opens[menu] = (window.__opens[menu] || 0) + 1;
    }
  }).observe(document.body, {subtree: true, attributes: true, attributeFilter: ['aria-expanded']});
}"""


async def _fill(kit: SimpleNamespace, server: Any, options: BrowserOptions, answers: dict[str, Any],
                hooks: str = "") -> tuple[Any, Any, dict[str, Any], dict[str, int]]:
    browser = await PlaywrightSessionFactory().start(options)
    try:
        if hooks:
            await browser.page.add_init_script(f"window.__widgetHooks = {hooks};")
        page = await browser.open(server.url(APPLY))
        await browser.page.evaluate(COUNT_OPENS)
        packet = kit.build(page.form, {**CONTACT, **answers}).packet
        result = await browser.fill(page.form, packet)
        return page, result, await browser.page.evaluate(SHOWN), await browser.page.evaluate("() => window.__opens")
    finally:
        await browser.close()


def _status(result: Any, field_id: str) -> Any:
    return next(f for f in result.fields if f.field_id == field_id)


def test_a_fabric_select_is_one_probed_field_named_by_its_label(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url(APPLY))
            model = browser.last_page
            return (page, model.bindings, [b.text for b in model.snapshot.buttons],
                    list(browser.menus.observations.values()), await browser.page.evaluate(SHOWN),
                    await browser.page.evaluate(PORTALS))
        finally:
            await browser.close()

    page, bindings, buttons, observations, shown, portals = kit.run(scenario())
    assert page.kind.value == "APPLICATION_FORM"
    fields = {f.id: f for f in page.form.fields}
    assert list(fields) == ["first_name", "last_name", "email", "state.value", "countryId.value",
                            "educationLevelId"]  # one field per select, never its proxy
    state, country, education = fields["state.value"], fields["countryId.value"], fields["educationLevelId"]
    assert (state.label, state.control_type, state.required, len(state.options or [])) == (
        "State", ControlType.SELECT, True, 51)
    assert (country.label, country.control_type, country.required) == ("Country", ControlType.SELECT, True)
    assert [o.label for o in country.options or []][:3] == ["United States", "Canada", "Australia"]
    assert (education.label, education.required) == ("Highest Education Obtained", False)
    # What each shows now: Country its value, State its placeholder.
    assert bindings["countryId.value"].value == "United States" and bindings["state.value"].value == ""
    # The menu buttons and "Clear Selection" belong to their fields, never page actions.
    assert buttons == ["Submit application"]
    assert [(o.kind, o.close_method) for o in observations] == [("select", "escape")] * 3
    assert {o.listbox_id for o in observations} == {"fab-menu341", "fab-menu343", "fab-menu-educationLevelId"}
    # Probing changed nothing: the menus are closed (hidden, still in the document).
    assert shown["countryId.value"] == {"display": "United States", "proxy": "1", "expanded": "false"}
    assert shown["state.value"] == {"display": PLACEHOLDER, "proxy": "", "expanded": "false"}
    assert len(portals) == 3 and not any(p["shown"] for p in portals)


def test_a_prefilled_country_is_verified_without_opening_it_and_a_state_is_chosen(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    _, result, shown, opens = kit.run(_fill(kit, server, options, {
        "countryId.value": "United States", "state.value": "Colorado"}))
    country, state = _status(result, "countryId.value"), _status(result, "state.value")
    assert country.status is FieldFillStatus.FILLED, country
    assert state.status is FieldFillStatus.FILLED, state
    assert opens == {"fab-menu341": 1}  # the State menu once; Country never opened
    assert shown["countryId.value"] == {"display": "United States", "proxy": "1", "expanded": "false"}
    assert shown["state.value"] == {"display": "Colorado", "proxy": "6", "expanded": "false"}


def test_another_country_is_chosen_from_the_menu_and_read_back(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    _, result, shown, opens = kit.run(_fill(kit, server, options, {"countryId.value": "Canada"}))
    assert _status(result, "countryId.value").status is FieldFillStatus.FILLED
    assert opens == {"fab-menu343": 1}
    assert shown["countryId.value"] == {"display": "Canada", "proxy": "2", "expanded": "false"}


@pytest.mark.parametrize(("hooks", "display"), [
    ("{selectNext: {}, fabIgnore: {'state.value': true}}", PLACEHOLDER),
    ("{selectNext: {'state.value': true}}", "Connecticut"),
], ids=["click-ignored", "next-item-taken"])
def test_a_choice_the_menu_did_not_take_is_a_mismatch(
    hooks: str, display: str, kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    _, result, shown, _ = kit.run(_fill(kit, server, options, {"state.value": "Colorado"}, hooks))
    state = _status(result, "state.value")
    assert state.status is FieldFillStatus.VERIFICATION_MISMATCH, state
    assert shown["state.value"]["display"] == display


# --- the live markup (zdfirm careers form, closed menus), fictional values ---------------------

LIVE_FABRIC = """<!doctype html><title>Fictional form</title>
<form id="job-application-form">
<div class="MuiFormControl-root" data-fabric-component="SelectField InputWrapper">
 <div class="fabric-SelectField-label-labelWrapper"><label class="MuiFormLabel-root" data-shrink="false"
   for="fab-select341">State<span aria-hidden="true" class="MuiFormLabel-asterisk"> *</span></label></div>
 <div class="MuiBox-root"><div class="fab-Select" data-fabric-component="Select"><div style="display: inline-block;">
  <div class="fab-SelectToggle__container"><button data-menu-id="fab-menu64" aria-expanded="false"
    aria-haspopup="true" aria-label="State \N{EN DASH}Select\N{EN DASH}" tabindex="0" aria-disabled="false"
    class="fab-SelectToggle fab-SelectToggle--width4" type="button"><div class="fab-SelectToggle__outerFacade">
    <div class="fab-SelectToggle__innerFacade"></div></div><div class="fab-SelectToggle__guts">
    <div class="fab-SelectToggle__placeholder">\N{EN DASH}Select\N{EN DASH}</div><div class="fab-SelectToggle__toggleButton">
    <svg viewBox="0 0 320 512" aria-hidden="true"></svg></div></div></button></div></div>
  <select data-has-default-value="" aria-hidden="true" class="chzn-ignore" id="fab-select341" name="state.value"
    readonly required tabindex="-1" style="border-style: none; opacity: 0; position: absolute;"><option value=""></option></select>
 </div></div>
</div>
<div class="MuiFormControl-root" data-fabric-component="SelectField InputWrapper">
 <div class="fabric-SelectField-label-labelWrapper"><label class="MuiFormLabel-root" data-shrink="false"
   for="fab-select343">Country<span aria-hidden="true" class="MuiFormLabel-asterisk"> *</span></label></div>
 <div class="MuiBox-root"><div class="fab-Select" data-fabric-component="Select"><div style="display: inline-block;">
  <div class="fab-SelectToggle__container"><button data-menu-id="fab-menu28" aria-expanded="false"
    aria-haspopup="true" aria-label="Country United States" tabindex="0" aria-disabled="false"
    class="fab-SelectToggle fab-SelectToggle--clearable" type="button"><div class="fab-SelectToggle__outerFacade">
    <div class="fab-SelectToggle__innerFacade"></div></div><div class="fab-SelectToggle__guts">
    <div class="fab-SelectToggle__content">United States</div><div class="fab-SelectToggle__clearButtonPlaceholder"></div>
    <div class="fab-SelectToggle__toggleButton"><svg viewBox="0 0 320 512" aria-hidden="true"></svg></div></div></button>
   <div class="fab-SelectToggle__clearButtonContainer"><button class="MuiButtonBase-root" tabindex="0" type="button"
     data-fabric-component="IconButton" aria-label="Clear Selection"><span><svg aria-hidden="true"></svg></span></button></div>
  </div></div>
  <select aria-hidden="true" class="chzn-ignore" id="fab-select343" name="countryId.value" readonly required
    tabindex="-1" style="border-style: none; opacity: 0; position: absolute;"><option value="1"></option></select>
 </div></div>
</div>
<button type="submit">Submit Application</button>
</form>"""


def test_the_live_fabric_markup_is_one_labelled_menu_control_per_select() -> None:
    async def scenario() -> tuple[DomSnapshot, list[Any], list[bool]]:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(LIVE_FABRIC)
                snapshot = DomSnapshot.model_validate(await page.evaluate(inspector_script()))
                states = [await page.evaluate(COMBO_STATE, {"selector": c.selector, "fields": False})
                          for c in snapshot.controls]
                # Dashes alone mark a placeholder too, with no class naming one.
                texts = [PLACEHOLDER, "\N{EM DASH} Select \N{EM DASH}", "-- Choose --", "Selection"]
                await page.set_content("".join(
                    f'<div id="c{i}" role="combobox" aria-haspopup="listbox" tabindex="0">{t}</div>'
                    for i, t in enumerate(texts)))
                plain = [(await page.evaluate(COMBO_STATE, {"selector": f"#c{i}", "fields": False}))["placeholder"]
                         for i in range(len(texts))]
                return snapshot, states, plain
            finally:
                await browser.close()

    snapshot, states, plain = asyncio.run(scenario())
    assert plain == [True, True, True, False]
    controls = [(c.kind, c.type, c.name, c.label, c.required) for c in snapshot.controls]
    assert controls == [("custom", "combobox", "state.value", "State", True),
                        ("custom", "combobox", "countryId.value", "Country", True)]
    facts = [(c.aria or {}) for c in snapshot.controls]
    assert [(f.get("combo"), f.get("haspopup"), f.get("value")) for f in facts] == [
        (1, "true", ""), (1, "true", "United States")]  # the en-dash "Select" is a placeholder
    assert [b.text for b in snapshot.buttons] == ["Submit Application"]
    # Closed and never opened: the menu does not exist yet, which is not an error.
    assert [(s["like"], s["placeholder"], s["menu"]) for s in states] == [(True, True, None), (True, False, None)]
    assert [s["control"]["labels"] for s in states] == [["State *"], ["Country *"]]
