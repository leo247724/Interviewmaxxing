"""Round 8: Ashby (Sanity) marks the rest of the page aria-hidden while a lookup's
suggestions show, and saves each choice before showing it (fictional data, headless
Chromium, nothing submitted). Reduced live shapes; the full flow is the ``?ui=floating``
variant of ``/forms/ashby-like`` in test_custom_widgets_round7.py.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from playwright.async_api import async_playwright

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_browser.aria import probe_menu, select_accessible
from interviewmaxxing_browser.driver import PlaywrightDriver
from interviewmaxxing_core import BrowserOptions, PageKind

# Sanity's form while the location suggestions show, reduced: Floating UI has marked
# everything outside the combobox and its portal aria-hidden with data-aria-hidden.
MARK = ' aria-hidden="true" data-aria-hidden="true"'
SANITY_OPEN = """<div id="root"><div id="form">
<div class="ashby-application-form-field-entry"{m}><label class="ashby-application-form-question-title"
 for="_systemfield_email"{m}>Email</label><input id="_systemfield_email" name="_systemfield_email" type="email"
 placeholder="hello@example.com..." required></div>
<div class="ashby-application-form-field-entry"><label class="ashby-application-form-question-title"
 for="47320a0f-1493-43f8-be17-a141070c99f7"{m}>Where are you based?</label><div class="_inputContainer">
<input class="ashby-application-form-input-autocomplete" placeholder="Start typing..." aria-autocomplete="list"
 aria-expanded="{expanded}" aria-haspopup="listbox" role="combobox" value="Denver"{controls}>
<button class="_toggleButton"{m}><svg aria-hidden="true" width="8" height="8"></svg></button></div></div>
<div class="ashby-application-form-field-entry"{m}><label class="ashby-application-form-question-title"
 for="5249543b-8ff7-4c35-a6fb-95e04bd075b8">LinkedIn Profile</label><input id="5249543b-8ff7-4c35-a6fb-95e04bd075b8"
 name="5249543b-8ff7-4c35-a6fb-95e04bd075b8" placeholder="Type here..." required></div>
<button type="submit" class="ashby-application-form-submit-button"{m}>Submit Application</button>
</div></div>{portal}{dialog}"""
PORTAL = """<div id=":r2:" data-floating-ui-portal=""><div role="listbox" id=":r0:" tabindex="-1">
<div role="option" id=":r6:" aria-selected="true">Denver, Colorado, United States</div>
<div role="option" id=":r7:" aria-selected="false">Denver City, Texas, United States</div></div>
<button type="button" tabindex="-1" style="position:fixed;opacity:0;width:1px;height:1px"></button></div>"""


def _page(*, marked: bool, modal: bool = False) -> str:
    return SANITY_OPEN.format(
        m=MARK if marked else "", expanded="true" if marked else "false",
        controls=' aria-controls=":r0:"' if marked else "", portal=PORTAL if marked else "",
        dialog='<div role="dialog" aria-modal="true"><p>Session expiring</p><button type="button">Stay</button></div>'
        if modal else "")


def _inspect(options: BrowserOptions, html: str) -> Any:
    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.page.route("https://example.test/apply", lambda route: route.fulfill(
                content_type="text/html; charset=utf-8", body=f"<title>Apply</title><h1>Fictional role</h1>{html}"))
            return await browser.open("https://example.test/apply")
        finally:
            await browser.close()

    return asyncio.run(scenario())


def _shape(page: Any) -> list[tuple[str, str, bool]]:
    assert page.kind is PageKind.APPLICATION_FORM, page.message
    return [(f.id, f.label, f.required) for f in page.form.fields]


def test_marks_a_popup_puts_on_the_rest_of_the_page_change_nothing(options: BrowserOptions) -> None:
    """With the suggestions shown, every question keeps its title, visibility and
    requiredness, and the page keeps its submit action. (The lookup itself is open here,
    so this inspection cannot probe it; a fill probes it while closed.)"""
    closed = _inspect(options, _page(marked=False))
    shown = _inspect(options, _page(marked=True))
    assert _shape(shown) == _shape(closed)
    types = {f.id: f.control_type for f in shown.form.fields}
    assert types == {f.id: f.control_type for f in closed.form.fields} | {
        "47320a0f-1493-43f8-be17-a141070c99f7": types["47320a0f-1493-43f8-be17-a141070c99f7"]}
    assert [f.label for f in shown.form.fields] == ["Email", "Where are you based?", "LinkedIn Profile"]
    assert shown.form.submit_selector is not None


def test_the_same_marks_hide_the_page_behind_a_modal_dialog(options: BrowserOptions) -> None:
    """Behind an open modal dialog, marked content stays hidden: only the dialog counts."""
    page = _inspect(options, _page(marked=True, modal=True))
    labels = [f.label for f in page.form.fields] if page.form is not None else []
    assert "Email" not in labels and "LinkedIn Profile" not in labels


# Ashby's value select (the referral question): an input combobox whose static options show
# on click; a choice empties the input until the site's save returns, then shows it.
SAVED_SELECT = """<!doctype html><title>Fictional form</title><form><label id="l">Where did you first hear about us?</label>
<input id="source" role="combobox" aria-haspopup="listbox" aria-expanded="false" aria-labelledby="l" placeholder="Start typing...">
</form>
<script>
const box = document.getElementById("source");
const names = ["LinkedIn", "Careers Page", "Glassdoor", "Other"];
const open = () => {
  const list = document.createElement("ul"); list.id = "source-list"; list.setAttribute("role", "listbox");
  names.forEach((name, i) => {
    const li = document.createElement("li"); li.id = "source-option-" + i; li.setAttribute("role", "option");
    li.setAttribute("aria-selected", "false"); li.textContent = name;
    li.addEventListener("mousedown", (e) => e.preventDefault());
    li.addEventListener("click", () => { close(); box.value = ""; setTimeout(() => { box.value = name; }, window.__saveMs); });
    list.appendChild(li);
  });
  box.after(list); box.setAttribute("aria-controls", "source-list"); box.setAttribute("aria-expanded", "true");
};
const close = () => {
  const list = document.getElementById("source-list"); if (list) list.remove();
  box.removeAttribute("aria-controls"); box.setAttribute("aria-expanded", "false");
};
box.addEventListener("click", () => box.getAttribute("aria-expanded") === "true" ? close() : open());
box.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
</script>"""


@pytest.mark.parametrize(("save_ms", "expected"), [(0, ["value"]), (400, ["value"]), (4000, ["mismatch"])],
                         ids=["immediate", "after-save", "never-within-3s"])
def test_a_choice_the_site_saves_first_is_read_back_once_shown(save_ms: int, expected: list[str]) -> None:
    async def scenario() -> list[str]:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(SAVED_SELECT)
                await page.evaluate(f"() => {{ window.__saveMs = {save_ms}; }}")
                driver = PlaywrightDriver(page)
                observation = await probe_menu(driver, "#source")
                binding = observation.binding({"value": "", "expanded": False})
                assert binding is not None
                wanted = next(o["value"] for o in binding["options"] if o["label"] == "LinkedIn")
                got = await select_accessible(driver, "#source", [wanted], binding)
                return ["value"] if got == [wanted] else ["mismatch"]
            finally:
                await browser.close()

    assert asyncio.run(scenario()) == expected
