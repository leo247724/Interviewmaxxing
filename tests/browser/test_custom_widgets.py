"""Custom menu widgets on the localhost mock ATS (fictional employer, fictional data).

Layer A: closed React-select and Rippling-style menus are probed once per document
during inspection (opened, read through their own listbox, closed, verified unchanged)
and become canonical ``SELECT`` fields; multi-select menus stay ``UNSUPPORTED``.
Layer B: probed menus are operated by opening them the recorded way, choosing the one
matching option and reading the choice back three ways. Real headless Chromium; the
OpenCLI transport is a local bridge onto the same isolated page (no subprocess,
extension, profile or user browser). Nothing here is ever submitted.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import async_playwright

from interviewmaxxing_browser import (
    CommandResult,
    OpenCliConfig,
    OpenCliDriver,
    PlaywrightSessionFactory,
    unsupported_control_needs,
)
from interviewmaxxing_browser.aria import (
    COMBO_STATE,
    PHONE_STATE,
    MenuObservation,
    MenuProbe,
    display_matches,
    display_option,
    probe_menu,
)
from interviewmaxxing_browser.driver import PlaywrightDriver
from interviewmaxxing_browser.opencli import _ALLOWED_SCRIPTS, _lint_read_script
from interviewmaxxing_browser.runtime import GenericApplicationBrowser
from interviewmaxxing_browser.session import linux_user_agent
from interviewmaxxing_core import BrowserOptions, ControlType, FieldFillStatus, PageKind

REACT = "/jobs/react-select/apply"
RIPPLING = "/jobs/div-combobox/apply"
MENUS = ["question_6004", "question_6001", "question_6002", "question_6003"]
RS_STATE = "() => JSON.parse(JSON.stringify(window.__widgetState))"
OPEN_MENUS = """() => ({
  expanded: Array.from(document.querySelectorAll('[aria-expanded=true]')).map((e) => e.id),
  portals: document.querySelectorAll('.select__menu').length,
  shown: Array.from(document.querySelectorAll('.select__single-value, .select__placeholder, .rip-select'))
    .map((e) => e.textContent),
})"""
COUNT_OPENS = """() => {
  window.__opens = 0;
  new MutationObserver((records) => {
    for (const r of records) if (r.target.getAttribute('aria-expanded') === 'true') window.__opens++;
  }).observe(document.body, {subtree: true, attributes: true, attributeFilter: ['aria-expanded']});
}"""


async def _session(options: BrowserOptions) -> Any:
    return await PlaywrightSessionFactory().start(options)


def test_probing_turns_react_selects_into_canonical_selects(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, Any, list[Any], int]:
        browser = await _session(options)
        try:
            page = await browser.open(server.url(REACT))
            after = await browser.page.evaluate(OPEN_MENUS)
            await browser.page.evaluate(COUNT_OPENS)
            again = await browser.inspect()
            await browser.inspect()
            log = list(browser.menus.log)
            return page, again, after, log, await browser.page.evaluate("() => window.__opens")
        finally:
            await browser.close()

    page, again, after, log, reopened = kit.run(scenario())
    assert page.kind is PageKind.APPLICATION_FORM, page.message
    form = page.form
    by_id = {f.id: f for f in form.fields}
    for field_id in MENUS:
        assert by_id[field_id].control_type is ControlType.SELECT, field_id
        assert by_id[field_id].required
        assert by_id[field_id].help_text is None  # the "Select..." placeholder is not wording
    assert by_id["question_6001"].label == "Are you legally authorized to work in the United States?"
    assert [(o.value, o.label) for o in by_id["question_6001"].options or []] == [("Yes", "Yes"), ("No", "No")]
    assert [o.label for o in by_id["question_6003"].options or []] == [
        "LinkedIn", "Indeed", "Company website", "Referral", "Other"]
    country = [(o.value, o.label) for o in by_id["question_6004"].options or []]
    assert len(country) == 31
    assert ("United States +1", "United States +1") in country and ("Canada +1", "Canada +1") in country
    # Every menu was closed again and shows what it showed before; no portal is left.
    assert after == {"expanded": [], "portals": 0, "shown": ["Select..."] * 4}
    # Cached per document: later inspections reuse the options without reopening.
    assert [selector for selector, _, _ in log] == [f"#{m}" for m in MENUS]
    assert reopened == 0 and again.form.fingerprint == form.fingerprint
    assert all(kind == "select" and seconds < 3.0 for _, kind, seconds in log)


def test_probing_rippling_div_menus_including_the_keyboard_only_one(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, dict[str, Any]]:
        browser = await _session(options)
        try:
            page = await browser.open(server.url(RIPPLING))
            lists = await browser.page.evaluate(
                "() => Array.from(document.querySelectorAll('ul[role=listbox]')).map((u) => "
                "[u.id, getComputedStyle(u).display])")
            return page, lists, dict(browser.menus.observations)
        finally:
            await browser.close()

    page, lists, observations = kit.run(scenario())
    form = page.form
    click, keyboard = form.field("field-3"), form.field("field-4")
    for field in (click, keyboard):
        assert field.control_type is ControlType.SELECT and field.required
        assert [o.label for o in field.options or []] == ["No", "Yes"]
    # The question is the preceding paragraph; there is no label association.
    assert click.label == "Are you legally authorized to work in the United States?"
    assert keyboard.label == "Will you now or in the future require visa sponsorship?"
    methods = {json.loads(key)[0]: o.open_method for key, o in observations.items()}
    assert methods["field-3"] == "click" and methods["field-4"] == "arrowdown"
    # Escape left the keyboard-opened list in the document (hidden); it is not a field.
    assert ["field-4-list", "none"] in lists
    assert "field-4-list" not in [f.id for f in form.fields]
    # A static search menu becomes a select; the pre-filled code search is a lookup.
    assert form.field("field-9").control_type is ControlType.SELECT
    assert form.field("field-7-country").control_type is ControlType.TYPEAHEAD


def test_probe_reads_only_the_owned_listbox_never_the_phone_country_list(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, list[Any]]:
        browser = await _session(options)
        try:
            page = await browser.open(server.url("/jobs/phone-widget/apply"))
            return page, list(browser.menus.log)
        finally:
            await browser.close()

    page, log = kit.run(scenario())
    heard = page.form.field("question_7003")
    assert [o.label for o in heard.options or []] == [
        "LinkedIn", "Indeed", "Company website", "Referral", "Other"]
    # The always-present country listbox (32 "Name+code" options) and the dialog's
    # search combobox are never probed or turned into questions.
    assert [selector for selector, _, _ in log] == ["#question_7003"]
    assert [f.id for f in page.form.fields] == ["first_name", "last_name", "email", "phone", "question_7003"]


def test_multiselect_menu_is_held_for_the_user(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, Any, Any]:
        browser = await _session(options)
        try:
            page = await browser.open(server.url("/jobs/multiselect-react/apply"))
            form = page.form
            fill = await browser.fill(form, kit.build(form, {
                "first_name": "Avery", "question_8002": "Referral"}).packet)
            state = await browser.page.evaluate(RS_STATE)
            # The person chooses a channel in the browser; the runtime reports it done.
            waiter = asyncio.create_task(browser.wait_for_user("choose channels", timeout_s=10))
            await asyncio.sleep(0.3)
            await browser.page.click("#question_8001")
            await browser.page.click("#react-select-question_8001-listbox [role=option] >> nth=0")
            await browser.page.keyboard.press("Escape")
            return page, fill, state, await waiter
        finally:
            await browser.close()

    page, fill, state, after = kit.run(scenario())
    channels = page.form.field("question_8001")
    assert channels.control_type is ControlType.UNSUPPORTED and channels.required
    assert [n.field_id for n in unsupported_control_needs(page.form)] == ["question_8001"]
    assert page.form.field("question_8002").control_type is ControlType.SELECT
    by_id = {f.field_id: f for f in fill.fields}
    assert by_id["question_8001"].status is FieldFillStatus.SKIPPED
    assert by_id["question_8002"].status is FieldFillStatus.FILLED
    assert state["question_8001"] == {"value": []}  # never chosen by the runtime
    assert state["question_8002"] == {"value": "src_referral"}
    assert not after.form.field("question_8001").required  # operated by the user now


def test_probing_budget_holds_the_rest(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> tuple[Any, list[Any], dict[str, Any], dict[str, Any]]:
        browser = await _session(options)
        browser.menus = MenuProbe(max_probes=2)
        try:
            page = await browser.open(server.url(REACT))
            limited = dict(browser.menus.observations)
            browser.menus = MenuProbe(max_seconds=0.01)
            await browser.page.reload()
            timed = await browser.inspect()
            return page, [f for f in timed.form.fields if f.id in MENUS], limited, \
                dict(browser.menus.observations)
        finally:
            await browser.close()

    page, timed, limited, observations = kit.run(scenario())
    types = {f.id: f.control_type for f in page.form.fields if f.id in MENUS}
    assert types == {"question_6004": ControlType.SELECT, "question_6001": ControlType.SELECT,
                     "question_6002": ControlType.UNSUPPORTED, "question_6003": ControlType.UNSUPPORTED}
    assert sum(o.kind == "select" for o in limited.values()) == 2
    assert {o.reason for o in limited.values() if o.kind == "unobservable"} == {
        "the menu probing budget for this page is spent"}
    # The time budget: after the first probe, nothing else is opened in this document.
    assert [f.control_type for f in timed] == [ControlType.SELECT] + [ControlType.UNSUPPORTED] * 3
    assert sum(o.kind == "select" for o in observations.values()) == 1


def test_selects_read_back_including_a_dial_code_display(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, Any, Any]:
        browser = await _session(options)
        try:
            page = await browser.open(server.url(REACT))
            form = page.form
            packet = kit.build(form, {**kit.pick(form, kit.CORE), **kit.pick(form, kit.STANDARD),
                                      "question_6004": "United States +1", "question_6001": "Yes",
                                      "question_6002": "No", "question_6003": "LinkedIn"}).packet
            fill = await browser.fill(form, packet)
            shown = await browser.page.evaluate(OPEN_MENUS)
            review = await browser.prepare_review()
            bound = browser.last_page.bindings["question_6004"].value
            return fill, await browser.page.evaluate(RS_STATE), shown, review, bound
        finally:
            await browser.close()

    fill, state, shown, review, bound = kit.run(scenario())
    assert fill.ok, [f for f in fill.fields if f.status is not FieldFillStatus.FILLED]
    assert {f.field_id: f.status for f in fill.fields if f.field_id in MENUS} == dict.fromkeys(
        MENUS, FieldFillStatus.FILLED)
    assert state == {"question_6004": {"value": "us"}, "question_6001": {"value": "rs_wa_yes"},
                     "question_6002": {"value": "rs_sp_no"}, "question_6003": {"value": "src_linkedin"}}
    # "United States +1" is displayed only as its dial code, which "Canada +1" shares: the
    # reopened menu's selected option confirmed it, and later reads still bind it.
    assert shown == {"expanded": [], "portals": 0, "shown": ["+1", "Yes", "No", "LinkedIn"]}
    assert bound == "United States +1"
    assert review.kind is PageKind.APPLICATION_FORM and review.form.page_errors == []


def test_an_already_open_menu_is_not_toggled_closed(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, Any]:
        browser = await _session(options)
        try:
            page = await browser.open(server.url(REACT))
            await browser.page.click("#question_6001")  # the person left this menu open
            opened = await browser.page.evaluate(OPEN_MENUS)
            fill = await browser.fill(page.form, kit.build(page.form, {"question_6001": "No"}).packet)
            return opened, fill, await browser.page.evaluate(RS_STATE)
        finally:
            await browser.close()

    opened, fill, state = kit.run(scenario())
    assert opened["expanded"] == ["question_6001"] and opened["portals"] == 1
    assert {f.field_id: f.status for f in fill.fields}["question_6001"] is FieldFillStatus.FILLED
    assert state["question_6001"] == {"value": "rs_wa_no"}


@pytest.mark.parametrize(("job", "field_id", "label", "selected"), [
    ("react-select", "question_6001", "Yes", "rs_wa_no"),
    ("div-combobox", "field-4", "No", "Yes"),
])
def test_a_different_selected_option_is_a_verification_mismatch(
    job: str, field_id: str, label: str, selected: str, kit: SimpleNamespace, server: Any,
    options: BrowserOptions,
) -> None:
    async def scenario() -> tuple[Any, Any]:
        browser = await _session(options)
        try:
            page = await browser.open(server.url(f"/jobs/{job}/apply"))
            # Fixture control: the widget selects the option after the one clicked.
            await browser.page.evaluate("(id) => { window.__widgetHooks.selectNext[id] = true; }", field_id)
            fill = await browser.fill(page.form, kit.build(page.form, {field_id: label}).packet)
            return fill, await browser.page.evaluate(RS_STATE)
        finally:
            await browser.close()

    fill, state = kit.run(scenario())
    [result] = [f for f in fill.fields if f.field_id == field_id]
    assert result.status is FieldFillStatus.VERIFICATION_MISMATCH, result
    assert not fill.ok and fill.failed_field_ids() == [field_id]
    assert state[field_id] == {"value": selected}  # the page really holds the other option


MAC_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
             "HeadlessChrome/140.0.0.0 Safari/537.36")
"""A fictional Mac headless user agent: react-select (and the mock) then expose neither
aria-selected nor aria-activedescendant, only the select__option--is-selected class."""


@pytest.mark.parametrize(("case", "agent", "field_id", "label", "hook", "status", "committed", "opens",
                          "detail"), [
    # Linux (the headless default): the reopened menu's aria-selected confirms "+1".
    ("linux, shared +1", None, "question_6004", "United States +1", None,
     FieldFillStatus.FILLED, "us", 2, None),
    # Mac: the display shows the whole label, so rules (1) and (2) confirm without a reopen.
    ("mac, full label", MAC_AGENT, "question_6001", "Yes", None,
     FieldFillStatus.FILLED, "rs_wa_yes", 1, None),
    # Mac: "+1" is confirmed by the selected class, the only signal react-select keeps.
    ("mac, shared +1", MAC_AGENT, "question_6004", "United States +1", None,
     FieldFillStatus.FILLED, "us", 2, None),
    ("linux, Canada committed", None, "question_6004", "United States +1", ("selectValue", "ca"),
     FieldFillStatus.VERIFICATION_MISMATCH, "ca", 2, "aria-selected on 'Canada +1'"),
    ("mac, Canada committed", MAC_AGENT, "question_6004", "United States +1", ("selectValue", "ca"),
     FieldFillStatus.VERIFICATION_MISMATCH, "ca", 2, "selected class on 'Canada +1'"),
    ("no selection signal", None, "question_6004", "United States +1", ("hideSelection", True),
     FieldFillStatus.VERIFICATION_MISMATCH, "us", 2, "no option is marked selected"),
    ("no selection signal, unique code", None, "question_6004", "India +91", ("hideSelection", True),
     FieldFillStatus.VERIFICATION_MISMATCH, "in", 2, "no option is marked selected"),
])
def test_a_suffix_display_is_confirmed_by_the_reopened_selection(
    case: str, agent: str | None, field_id: str, label: str, hook: tuple[str, Any] | None,
    status: FieldFillStatus, committed: str, opens: int, detail: str | None,
    kit: SimpleNamespace, server: Any, options: BrowserOptions,
) -> None:
    async def scenario() -> tuple[Any, Any, Any, int, str]:
        browser = await PlaywrightSessionFactory(user_agent=agent).start(options)
        try:
            page = await browser.open(server.url(REACT))
            if hook is not None:
                await browser.page.evaluate(
                    "([name, id, value]) => { window.__widgetHooks[name] = {[id]: value}; }",
                    [hook[0], field_id, hook[1]])
            await browser.page.evaluate(COUNT_OPENS)
            fill = await browser.fill(page.form, kit.build(page.form, {field_id: label}).packet)
            shown = await browser.page.evaluate(OPEN_MENUS)
            return (fill, await browser.page.evaluate(RS_STATE), shown,
                    await browser.page.evaluate("() => window.__opens"),
                    await browser.page.evaluate("() => navigator.userAgent"))
        finally:
            await browser.close()

    fill, state, shown, opened, user_agent = kit.run(scenario())
    [result] = [f for f in fill.fields if f.field_id == field_id]
    assert result.status is status, (case, result)
    assert state[field_id] == {"value": committed}
    assert opened == opens  # one open to choose, one more only to confirm a suffix display
    assert shown["expanded"] == [] and shown["portals"] == 0  # read back, then closed again
    assert ("Macintosh" in user_agent) is (agent is not None)
    if detail is not None:
        assert detail in (result.detail or ""), result.detail


def test_headless_sessions_present_a_linux_platform_user_agent(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, tmp_path: Any
) -> None:
    async def agents() -> list[str]:
        seen = []
        for profile in (None, tmp_path / "profile"):  # a plain and a persistent context
            browser = await PlaywrightSessionFactory().start(replace(options, profile_dir=profile))
            try:
                await browser.page.goto(server.url(REACT))
                seen.append(await browser.page.evaluate("() => navigator.userAgent"))
            finally:
                await browser.close()
        return seen

    for agent in kit.run(agents()):
        assert "(X11; Linux x86_64)" in agent and "Macintosh" not in agent, agent
        assert "AppleWebKit/537.36" in agent and "Chrome/" in agent  # the browser's own tokens
    assert linux_user_agent(MAC_AGENT) == (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36")


def test_rippling_menus_are_filled_and_read_back(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, Any]:
        browser = await _session(options)
        try:
            page = await browser.open(server.url(RIPPLING))
            form = page.form
            fill = await browser.fill(form, kit.build(form, {
                "first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
                "phone": "+13035550142", "field-3": "Yes", "field-4": "No",
                "field-9": "They/them"}).packet)
            return fill, await browser.page.evaluate(RS_STATE), await browser.prepare_review()
        finally:
            await browser.close()

    fill, state, review = kit.run(scenario())
    assert fill.ok, [f for f in fill.fields if f.status is not FieldFillStatus.FILLED]
    statuses = {f.field_id: f.status for f in fill.fields}
    assert {statuses[i] for i in ("phone", "field-3", "field-4", "field-9")} == {FieldFillStatus.FILLED}
    assert state["field-3"] == {"value": "Yes"} and state["field-4"] == {"value": "No"}
    assert state["pronouns"] == {"value": "They/them"}
    assert state["phone_country_code"] == {"value": "+1 US"}  # an unanswered lookup is untouched
    assert review.form.page_errors == []


# --- the same runtime over the OpenCLI driver ---------------------------------------------


class Bridge:
    """Answers OpenCLI argv lists by operating an isolated headless page."""

    def __init__(self, page: Any, *, keys: bool = True) -> None:
        self.page = page
        self.keys = keys
        self.commands: list[str] = []

    async def __call__(self, argv: Sequence[str], timeout_s: float) -> CommandResult:
        args = list(argv)
        i = args.index("browser")
        command = args[i + 2]
        positional = args[args.index("--") + 1:] if "--" in args else []
        self.commands.append(command)
        payload: Any
        if command == "tab":
            payload = [{"page": "LOCAL"}] if args[i + 3] == "list" else {"page": "LOCAL"}
        elif command == "open":
            await self.page.goto(positional[0])
            payload = {"page": "LOCAL", "url": self.page.url}
        elif command == "eval":
            payload = await self.page.evaluate(positional[0])
        elif command == "screenshot":
            await self.page.screenshot(path=positional[0])
            payload = {"ok": True}
        elif command == "keys":
            if not self.keys:
                error = {"error": {"code": "unknown_command", "message": "keys is not available"}}
                return CommandResult(1, json.dumps(error), "")
            await self.page.keyboard.press(positional[0])
            payload = {"pressed": positional[0]}
        else:
            locator = self.page.locator(positional[0])
            assert await locator.count() == 1, positional
            payload = {"matches_n": 1, "match_level": "exact"}
            if command == "fill":
                await locator.fill(positional[1])
                payload.update(verified=True, actual=await locator.input_value())
            elif command == "click":
                await locator.click()
            elif command == "focus":
                await locator.focus()
                payload["focused"] = True
            elif command == "type":
                await locator.click()
                await locator.press_sequentially(positional[1])
                payload["typed"] = True
            elif command == "select":
                await locator.select_option(value=positional[1])
            elif command in ("check", "uncheck"):
                await locator.set_checked(command == "check")
                payload["checked"] = await locator.is_checked()
            else:
                raise AssertionError(f"unexpected command {command}")
        return CommandResult(0, json.dumps(payload), "")


@pytest.mark.parametrize("keys", [True, False])
def test_opencli_probes_and_selects_or_holds_without_keys(
    keys: bool, kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, Any, list[str]]:
        headless = await _session(replace(options, allow_submission=False))
        bridge = Bridge(headless.page, keys=keys)
        browser = GenericApplicationBrowser(
            OpenCliDriver(OpenCliConfig(session="imx-fixture-custom-widgets"), runner=bridge),
            replace(options, allow_submission=False))
        try:
            page = await browser.open(server.url(REACT))
            answers = {"question_6001": "Yes", "question_6004": "United States +1"}
            fill = await browser.fill(page.form, kit.build(page.form, {
                k: v for k, v in answers.items()
                if page.form.field(k).control_type is ControlType.SELECT}).packet)
            return page, fill, await headless.page.evaluate(RS_STATE), bridge.commands
        finally:
            await headless.close()

    page, fill, state, commands = kit.run(scenario())
    types = {f.id: f.control_type for f in page.form.fields if f.id in MENUS}
    if keys:
        assert set(types.values()) == {ControlType.SELECT}
        statuses = {f.field_id: f.status for f in fill.fields}
        assert statuses["question_6001"] is statuses["question_6004"] is FieldFillStatus.FILLED
        assert state["question_6001"] == {"value": "rs_wa_yes"} and state["question_6004"] == {"value": "us"}
        assert {"focus", "keys", "type", "click"} <= set(commands)
    else:
        # Without keys a probed menu could not be closed or re-verified by Escape: every
        # menu is held for the user, and nothing was chosen.
        assert set(types.values()) == {ControlType.UNSUPPORTED}
        assert all(value == {"value": None} for value in state.values())


# --- synthetic pages ---------------------------------------------------------------------

VIRTUAL = """<!doctype html><title>Fictional form</title><form>
<label id="l">Office</label>
<div id="office" role="combobox" tabindex="0" aria-haspopup="listbox" aria-labelledby="l"
 aria-expanded="false">Choose</div>
<button type="submit">Submit application</button></form>
<script>
const box = document.getElementById('office');
box.onclick = () => {
  const list = document.createElement('div');
  list.id = 'office-list'; list.setAttribute('role', 'listbox');
  list.style.cssText = 'height:120px;overflow-y:auto;position:relative';
  const spacer = document.createElement('div'); spacer.style.height = (100 * 30) + 'px';
  list.appendChild(spacer);
  const draw = () => {
    list.querySelectorAll('[role=option]').forEach((o) => o.remove());
    const first = Math.floor(list.scrollTop / 30);
    for (let i = first; i < first + 6; i++) {
      const o = document.createElement('div');
      o.id = 'office-option-' + i; o.setAttribute('role', 'option'); o.textContent = 'Office ' + i;
      o.style.cssText = 'position:absolute;height:30px;top:' + (i * 30) + 'px';
      list.appendChild(o);
    }
  };
  list.onscroll = draw; draw();
  box.after(list);
  box.setAttribute('aria-controls', 'office-list'); box.setAttribute('aria-expanded', 'true');
};
box.onkeydown = (e) => {
  if (e.key !== 'Escape') return;
  document.getElementById('office-list').remove();
  box.removeAttribute('aria-controls'); box.setAttribute('aria-expanded', 'false');
};
</script>"""


def test_a_virtualized_menu_is_unobservable_and_closed_again() -> None:
    async def scenario() -> tuple[Any, Any]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(VIRTUAL)
                observation = await probe_menu(PlaywrightDriver(page, action_timeout_s=1.0), "#office")
                return observation, await page.evaluate(
                    "() => [document.getElementById('office').getAttribute('aria-expanded'), "
                    "!!document.getElementById('office-list')]")
            finally:
                await browser.close()

    observation, state = asyncio.run(scenario())
    assert observation.kind == "unobservable"
    assert "virtualized" in observation.reason
    assert state == ["false", False]


def test_menu_scripts_are_fixed_read_only_opencli_scripts() -> None:
    for script in (COMBO_STATE, PHONE_STATE):
        assert script in _ALLOWED_SCRIPTS
        _lint_read_script(script)


@pytest.mark.parametrize(("display", "expected"), [
    ("Yes", 0), ("yes", 0), ("+1", 2), ("+44", 1), ("1", None), ("", None),
    ("+4", None), ("Maybe", None),
])
def test_display_names_one_option_or_none(display: str, expected: int | None) -> None:
    assert display_option(display, ["Yes", "United Kingdom +44", "United States +1"]) == expected
    # A dial code shared by two options names neither of them.
    assert display_option("+1", ["Canada +1", "United States +1"]) is None


@pytest.mark.parametrize(("display", "label", "expected"), [
    ("United States +1", "United States +1", "equal"),
    ("+44", "United Kingdom +44", "suffix"),
    ("+1", "United States +1", "shared"),
    ("+1", "Canada +1", "shared"),
    ("+44", "United States +1", None),
    ("1", "United States +1", None),
    ("", "United States +1", None),
])
def test_display_relation_to_the_chosen_option(display: str, label: str, expected: str | None) -> None:
    labels = ["Canada +1", "United Kingdom +44", "United States +1"]
    assert display_matches(display, label, labels) == expected


def test_a_confirmed_selection_names_a_shared_display_only_while_it_fits() -> None:
    options = tuple({"label": label, "value": label, "disabled": False, "index": i, "selected": False}
                    for i, label in enumerate(["Canada +1", "United Kingdom +44", "United States +1"]))
    observed = MenuObservation("select", selector="#country", options=options)
    closed = {"value": "+1", "expanded": False}
    binding = observed.binding(closed)
    assert binding is not None and binding["value"] == ""  # "+1" alone is not an answer
    binding = observed.binding(closed, "United States +1")
    assert binding is not None and binding["value"] == "United States +1"
    binding = observed.binding({"value": "+44", "expanded": False}, "United States +1")
    assert binding is not None and binding["value"] == "United Kingdom +44"  # the display wins
