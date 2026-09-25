"""Round 7: pilot-7 findings and review items on the localhost mock ATS and synthetic
pages (fictional data, real headless Chromium, nothing submitted).

Closed wording must persist before a posting counts as closed; a restored upload is
annotated as it is returned; menus inside a dialog close by their own toggle; waiting for
the user never probes; typed filter text is cleared; a lookup list may carry an
attribution link; a Yes/No question is consent only when it asks for it.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import async_playwright

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_browser.ai.classification import binding_hash
from interviewmaxxing_browser.aria import probe_menu, select_accessible
from interviewmaxxing_browser.driver import NotActionable, PlaywrightDriver
from interviewmaxxing_browser.semantics import classify
from interviewmaxxing_core import (
    AnswerSource,
    ApplicationForm,
    ApplicationPacket,
    BrowserOptions,
    ControlType,
    FieldFillStatus,
    PacketAnswer,
    PageKind,
    Provenance,
    SemanticType,
    TextValue,
    UserInput,
)

CONTACT = {"first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test"}
INLINE = "/jobs/react-select-inline/apply"


# --- M2: closed wording must persist ----------------------------------------------------------


def test_closed_wording_that_gives_way_to_the_form_is_not_closed(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            return await browser.open(server.url("/jobs/flash-closed/apply"))
        finally:
            await browser.close()

    page = kit.run(scenario())
    # "Job not found" for 800 ms, then the posting's form: not a closed job.
    assert page.kind is PageKind.APPLICATION_FORM, page.message


def test_closed_wording_that_persists_or_comes_with_404_is_closed(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, float]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            persisting = await browser.open(server.url("/closed/not-found"))
            await browser.page.route("**/gone", lambda route: route.fulfill(
                status=404, content_type="text/html",
                body="<!doctype html><title>Jobs</title><h1>Job not found</h1>"))
            started = time.monotonic()
            gone = await browser.open(server.url("/gone"))
            return persisting, gone, time.monotonic() - started
        finally:
            await browser.close()

    persisting, gone, seconds = kit.run(scenario())
    assert persisting.kind is PageKind.JOB_CLOSED  # HTTP 200, but it stays closed
    assert gone.kind is PageKind.JOB_CLOSED and seconds < 1.9  # HTTP 404: no waiting


# --- M4: a restored upload is annotated as it is returned -------------------------------------


class RecordingAnnotator:
    """Returns forms unchanged and records what it was asked to annotate (Jev remembers
    its report under the annotated form's binding hash)."""

    def __init__(self) -> None:
        self.forms: list[ApplicationForm] = []

    def annotate(self, form: ApplicationForm, *, document_id: str,
                 schema_hints: dict[str, Any] | None = None) -> ApplicationForm:
        self.forms.append(form)
        return form


def test_a_form_with_a_restored_upload_is_the_one_annotated(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, ApplicationForm, RecordingAnnotator]:
        browser = await PlaywrightSessionFactory().start(options)
        annotator = RecordingAnnotator()
        browser.annotator = annotator
        try:
            page = await browser.open(server.url(INLINE))
            fill = await browser.fill(page.form, kit.build(page.form, {
                **CONTACT, "question_9004": "United States +1", "phone": "+1 (303) 555-0142",
                "resume": kit.RESUME, "question_9001": "Yes", "years_experience": "6 to 9 years",
                "question_9002": "No", "question_9003": "LinkedIn",
                "why_brambleway": "Fictional answer for tests."}).packet)
            assert fill.ok, fill.fields
            again = await browser.inspect()
            return fill, again.form, annotator
        finally:
            await browser.close()

    _, form, annotator = kit.run(scenario())
    # The upload replaced the input; the returned form still has the answered question,
    # and so had the form the provider annotated: a resolve reuses that report.
    assert form.find("resume") is not None
    assert binding_hash(annotator.forms[-1]) == binding_hash(form)


# --- L4: a menu inside a dialog closes by its own toggle --------------------------------------

DIALOG_MENU = """<!doctype html><title>Fictional form</title>
<div role="dialog" aria-modal="true" id="wizard"><form>
<label id="l" for="size">Team size</label>
<div id="size" role="combobox" tabindex="0" aria-haspopup="listbox" aria-labelledby="l" aria-expanded="false">Select</div>
</form></div>
<script>
const box = document.getElementById("size");
const open = () => {
  const list = document.createElement("ul");
  list.id = "size-list"; list.setAttribute("role", "listbox");
  for (const [i, text] of ["1-10", "11-50", "51+"].entries()) {
    const li = document.createElement("li"); li.id = "size-option-" + i; li.setAttribute("role", "option");
    li.textContent = text; list.appendChild(li);
  }
  box.after(list); box.setAttribute("aria-controls", "size-list"); box.setAttribute("aria-expanded", "true");
};
const close = () => {
  const list = document.getElementById("size-list"); if (list) list.remove();
  box.removeAttribute("aria-controls"); box.setAttribute("aria-expanded", "false");
};
box.addEventListener("click", () => box.getAttribute("aria-expanded") === "true" ? close() : open());
// Like a modal wizard: Escape (which the menu does not stop) closes the whole dialog.
document.addEventListener("keydown", (e) => { if (e.key === "Escape") document.getElementById("wizard").remove(); });
</script>"""


def test_a_menu_inside_a_dialog_closes_by_its_toggle_not_escape() -> None:
    async def scenario() -> tuple[Any, bool]:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(DIALOG_MENU)
                observation = await probe_menu(PlaywrightDriver(page), "#size")
                return observation, await page.evaluate("() => !!document.getElementById('wizard')")
            finally:
                await browser.close()

    observation, dialog_open = asyncio.run(scenario())
    assert observation.kind == "select" and [o["label"] for o in observation.options] == ["1-10", "11-50", "51+"]
    assert observation.close_method == "toggle" and dialog_open


# --- L5: waiting for the user never opens a menu ----------------------------------------------


def test_waiting_for_the_user_never_probes(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> tuple[int, int]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.open(server.url(INLINE))
            probes = len(browser.menus.log)
            browser.menus.reset(browser.menus.document)  # nothing is cached any more
            await browser.wait_for_user("operate the menus", timeout_s=0.0)
            return probes, len(browser.menus.log)
        finally:
            await browser.close()

    probes, after = kit.run(scenario())
    assert probes == 4 and after == probes


# --- L6: typed filter text is not left behind -------------------------------------------------

LONG_MENU = """<!doctype html><title>Fictional form</title><form>
<label for="city">City</label>
<input id="city" role="combobox" aria-haspopup="listbox" aria-autocomplete="list" aria-expanded="false" value="">
</form>
<script>
const input = document.getElementById("city");
const names = Array.from({length: 24}, (_, i) => "City " + (i + 1));
const open = () => {
  if (document.getElementById("city-list")) return;
  const list = document.createElement("ul"); list.id = "city-list"; list.setAttribute("role", "listbox");
  names.forEach((name, i) => {
    const li = document.createElement("li"); li.id = "city-option-" + i; li.setAttribute("role", "option");
    li.textContent = name;
    if (name === "City 24") li.style.display = "none";  // never rendered visibly
    list.appendChild(li);
  });
  input.after(list); input.setAttribute("aria-controls", "city-list"); input.setAttribute("aria-expanded", "true");
};
const close = () => {
  const list = document.getElementById("city-list"); if (list) list.remove();
  input.removeAttribute("aria-controls"); input.setAttribute("aria-expanded", "false");
};
input.addEventListener("click", open);
input.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
</script>"""


def test_typed_filter_text_is_cleared_when_no_option_shows() -> None:
    async def scenario() -> tuple[str, str]:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(LONG_MENU)
                driver = PlaywrightDriver(page)
                observation = await probe_menu(driver, "#city")
                binding = observation.binding({"value": "", "expanded": False})
                assert binding is not None
                try:
                    await select_accessible(driver, "#city", ["City 24"], binding)
                    outcome = "selected"
                except NotActionable as exc:
                    outcome = str(exc)
                return outcome, await page.input_value("#city")
            finally:
                await browser.close()

    outcome, left = asyncio.run(scenario())
    assert "no unique visible option" in outcome
    assert left == ""  # the filter typed to find it is gone


# --- L7: a lookup list may carry an attribution link -----------------------------------------

ATTRIBUTED_LOOKUP = """<!doctype html><title>Fictional form</title><form method="post" action="/nowhere">
<label for="first">First name</label><input id="first" name="first_name" required>
<label for="where">Where are you located?</label>
<input id="where" name="where" aria-haspopup="listbox" aria-autocomplete="list" autocomplete="off" required>
<button type="submit">Submit application</button></form>
<script>
const input = document.getElementById("where");
const cities = ["Austin, TX, USA", "Austin, MN, USA", "Denver, CO, USA"];
let list = null;
input.addEventListener("input", () => {
  if (list) { list.remove(); list = null; input.removeAttribute("aria-controls"); }
  const text = input.value.trim().toLowerCase();
  if (text.length < 3) return;
  list = document.createElement("div"); list.id = "where-list"; list.setAttribute("role", "listbox");
  cities.filter((c) => c.toLowerCase().startsWith(text)).forEach((c, i) => {
    const o = document.createElement("div"); o.id = "where-option-" + i; o.setAttribute("role", "option");
    o.textContent = c; o.addEventListener("click", () => { input.value = c; list.remove(); list = null;
      input.removeAttribute("aria-controls"); }); list.appendChild(o);
  });
  const credit = document.createElement("a"); credit.href = "https://maps.example.test/";
  credit.textContent = "powered by Example Maps"; list.appendChild(credit);
  input.after(list); input.setAttribute("aria-controls", "where-list");
});
</script>"""


def _lookup_packet(kit: SimpleNamespace, form: ApplicationForm, answers: dict[str, Any]) -> ApplicationPacket:
    lookups = {k: v for k, v in answers.items() if form.field(k).control_type is ControlType.TYPEAHEAD}
    built = kit.build(form, {k: v for k, v in answers.items() if k not in lookups}).packet
    typed = []
    for field_id, text in lookups.items():
        value = TextValue(text=text)
        user = UserInput.for_field(form, field_id, value)
        typed.append(PacketAnswer(field_id=field_id, semantic_type=form.field(field_id).semantic_type,
                                  value=value, provenance=Provenance(source=AnswerSource.USER_INPUT,
                                                                     reference_ids=[user.id])))
    return ApplicationPacket.model_validate({
        **built.model_dump(),
        "answers": [*(a.model_dump() for a in built.answers), *(a.model_dump() for a in typed)],
        "missing_inputs": [m.model_dump() for m in built.missing_inputs if m.field_id not in lookups],
    })


def test_a_lookup_list_with_an_attribution_link_still_commits(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, str]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.page.goto(server.url("/"))
            await browser.page.set_content(ATTRIBUTED_LOOKUP)
            form = (await browser.inspect()).form
            assert form is not None
            fill = await browser.fill(form, _lookup_packet(kit, form, {"first_name": "Avery",
                                                                        "where": "Austin, TX"}))
            return form, fill, await browser.page.input_value("#where")
        finally:
            await browser.close()

    form, fill, value = kit.run(scenario())
    assert form.field("where").control_type is ControlType.TYPEAHEAD
    result = next(f for f in fill.fields if f.field_id == "where")
    assert result.status is FieldFillStatus.FILLED, result
    assert value == "Austin, TX, USA"


# --- item 5: a Yes/No question is consent only when it asks for it ----------------------------


@pytest.mark.parametrize(("label", "expected"), [
    ("Have you worked in a performance marketing agency environment?", SemanticType.CUSTOM_SELECT),
    ("Do you have at least 8 years of total experience in performance marketing?", SemanticType.CUSTOM_SELECT),
    ("Do you consent to a background check?", SemanticType.CONSENT),
    ("Do you agree to our privacy policy?", SemanticType.CONSENT),
    ("Would you like to receive marketing emails?", SemanticType.CONSENT),
    ("Do you acknowledge that this role requires travel?", SemanticType.ATTESTATION),
])
def test_consent_needs_a_consent_act_on_choice_questions(label: str, expected: SemanticType) -> None:
    assert classify(label=label, control_type=ControlType.SELECT) is expected
    assert classify(label=label, control_type=ControlType.RADIO) is expected


def test_a_consent_checkbox_needs_no_verb() -> None:
    assert classify(label="Send me marketing emails", control_type=ControlType.CHECKBOX) is SemanticType.CONSENT


# --- item 12: a multi-line textarea is retyped without key presses ---------------------------


def test_a_multiline_narrative_lost_by_a_react_form_is_written_again(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """The React-controlled page loses the first typed change and re-renders; the narrative
    (newlines, a tab) is then entered once more as one input event, never as Enter keys."""
    text = "I have led lifecycle programs for six years.\nHighlights:\n\n\t- retention up 12%"

    async def scenario() -> tuple[Any, str, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url(
                "/jobs/react-controlled-narrative/apply?lose_first=1&revert=sync"))
            fill = await browser.fill_fields(page.form, kit.build(page.form, {"why_brambleway": text}).packet,
                                             ["why_brambleway"])
            value = await browser.page.evaluate("() => document.querySelector('textarea').value")
            return fill, value, await browser.page.evaluate("() => window.__mock.renders")
        finally:
            await browser.close()

    fill, value, renders = kit.run(scenario())
    [result] = fill.fields
    assert result.status is FieldFillStatus.FILLED, result
    assert result.detail == "typed again after the page re-rendered it"
    assert value.replace("\r\n", "\n") == text and renders >= 1


# --- items 14 and 15: unverified bytes are reported; an attached file is not re-uploaded --------

INLINE_ANSWERS = {
    "question_9004": "United States +1", "phone": "+1 (303) 555-0142", "question_9001": "Yes",
    "years_experience": "6 to 9 years", "question_9002": "No", "question_9003": "LinkedIn",
    "why_brambleway": "Fictional answer for tests.",
}


@pytest.mark.parametrize(("hooks", "unreadable"), [
    ("chipText: {resume: 'resume_av…quill.pdf'}", False),
    ("chipText: {resume: '1 file selected'}", False),
    ("", True),
], ids=["shortened-chip", "count-chip", "bytes-unreadable"])
def test_an_attached_upload_is_reported_honestly_and_not_uploaded_again(
    hooks: str, unreadable: bool, kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, int]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            script = f"window.__widgetHooks = {{selectNext: {{}}, {hooks}}};" if hooks else ""
            if unreadable:  # a page that cannot hash what it was handed
                script += "Object.defineProperty(window.crypto, 'subtle', {value: undefined});"
            if script:
                await browser.page.add_init_script(script)
            page = await browser.open(server.url(INLINE))
            packet = kit.build(page.form, {**CONTACT, **INLINE_ANSWERS, "resume": kit.RESUME}).packet
            first = await browser.fill(page.form, packet)
            again = await browser.fill(page.form, packet)
            return first, again, await browser.page.evaluate("() => window.__filesTaken")
        finally:
            await browser.close()

    first, again, taken = kit.run(scenario())
    one = next(f for f in first.fields if f.field_id == "resume")
    two = next(f for f in again.fields if f.field_id == "resume")
    assert one.status is two.status is FieldFillStatus.FILLED, (one, two)
    assert ("not verified" in (one.detail or "")) is unreadable
    assert two.detail == "the uploader already shows this file" and taken == 1


# --- item 16: a display naming no option must still be consistent with the choice ------------

DOWNSHIFT_MENU = """<!doctype html><title>Fictional form</title><form>
<label id="l">Country of residence</label>
<div id="country" role="combobox" tabindex="0" aria-haspopup="listbox" aria-labelledby="l" aria-expanded="false">Country *</div>
</form>
<script>
const box = document.getElementById("country");
const names = ["United States +1", "Canada +1", "Mexico +52"];
let chosen = null;
const open = () => {
  const list = document.createElement("ul"); list.id = "country-list"; list.setAttribute("role", "listbox");
  names.forEach((name, i) => {
    const li = document.createElement("li"); li.id = "country-option-" + i; li.setAttribute("role", "option");
    // Downshift/APG: the highlighted option is aria-selected, and opening highlights option 0.
    li.setAttribute("aria-selected", String(i === 0)); li.textContent = name;
    li.addEventListener("click", () => { if (!window.__ignoreClicks) { chosen = name; close(); } });
    list.appendChild(li);
  });
  box.after(list); box.setAttribute("aria-controls", "country-list"); box.setAttribute("aria-expanded", "true");
  box.setAttribute("aria-activedescendant", "country-option-0");
};
const close = () => {
  const list = document.getElementById("country-list"); if (list) list.remove();
  box.removeAttribute("aria-controls"); box.removeAttribute("aria-activedescendant");
  box.setAttribute("aria-expanded", "false"); box.textContent = chosen ? chosen.replace(/ \\+\\d+$/, "") : "Country *";
};
box.addEventListener("click", () => box.getAttribute("aria-expanded") === "true" ? close() : open());
box.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
</script>"""


@pytest.mark.parametrize(("ignored", "expected"), [(True, "mismatch"), (False, "United States +1")])
def test_a_click_that_did_not_take_is_not_confirmed_by_a_highlight(ignored: bool, expected: str) -> None:
    """The menu highlights (aria-selected) option 0 on opening. If the click on "United
    States +1" did not take, the display still reads "Country *": no confirmation."""
    async def scenario() -> list[str]:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(DOWNSHIFT_MENU)
                await page.evaluate(f"() => {{ window.__ignoreClicks = {str(ignored).lower()}; }}")
                driver = PlaywrightDriver(page)
                observation = await probe_menu(driver, "#country")
                binding = observation.binding({"value": "", "expanded": False})
                assert binding is not None
                return await select_accessible(driver, "#country", ["United States +1"], binding)
            finally:
                await browser.close()

    values = asyncio.run(scenario())
    if expected == "mismatch":
        assert values != ["United States +1"], values
    else:
        assert values == ["United States +1"]  # "United States" is a prefix of the label


# --- item 17: a lookup's own suggestion portal is not a page change -------------------------

ASHBY_LOOKUP = "_systemfield_location"
ASHBY_AFTER = {"bad815aa-0000-4000-8000-00000000a005": "$120,000", "c60ace77-0000-4000-8000-00000000a004": "Yes"}


def _with_lookup(kit: SimpleNamespace, form: ApplicationForm, answers: dict[str, Any], lookup: dict[str, str]) -> Any:
    """``kit.build`` plus typed answers for lookups (a lookup's answer is its text)."""
    built = kit.build(form, answers).packet
    typed = []
    for field_id, text in lookup.items():
        value = TextValue(text=text)
        user = UserInput.for_field(form, field_id, value)
        typed.append(PacketAnswer(field_id=field_id, semantic_type=form.field(field_id).semantic_type, value=value,
                                  provenance=Provenance(source=AnswerSource.USER_INPUT, reference_ids=[user.id])))
    return ApplicationPacket.model_validate({
        **built.model_dump(),
        "answers": [*(a.model_dump() for a in built.answers), *(a.model_dump() for a in typed)],
        "missing_inputs": [m.model_dump() for m in built.missing_inputs if m.field_id not in lookup],
    })


@pytest.mark.parametrize("query", ["", "?owns=listbox", "?portal=inline", "?portal=inline&owns=listbox"],
                         ids=["portal-wrapper", "portal-listbox", "inline-wrapper", "inline-listbox"])
def test_a_lookup_whose_suggestions_mount_a_portal_is_filled_and_the_fill_goes_on(
    query: str, kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """Ashby (Sanity): the location lookup's suggestions mount in a portal of their own,
    named by the input's aria-controls (the portal's wrapper, or the listbox), in <body> or
    right after the field, shifting the later questions' element paths while it is open.
    The lookup is chosen and read back and the questions after it are still filled."""
    async def scenario() -> tuple[Any, str]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url("/forms/ashby-like" + query))
            form = page.form
            lookup = form.field(ASHBY_LOOKUP)
            assert (lookup.control_type, lookup.label, lookup.placeholder) == (
                ControlType.TYPEAHEAD, "Location", "Start typing...")
            answers = {"_systemfield_name": "Avery Quill", "_systemfield_email": "avery.quill@example.test",
                       **ASHBY_AFTER}
            result = await browser.fill(form, _with_lookup(kit, form, answers, {ASHBY_LOOKUP: "Denver, Colorado"}))
            value = await browser.page.evaluate(
                "() => document.querySelector('.ashby-application-form-input-autocomplete').value")
            return result, value
        finally:
            await browser.close()

    result, value = kit.run(scenario())
    statuses = {f.field_id: f.status for f in result.fields}
    assert statuses[ASHBY_LOOKUP] is FieldFillStatus.FILLED, result
    assert all(statuses[fid] is FieldFillStatus.FILLED for fid in ASHBY_AFTER), result
    assert value == "Denver, Colorado, United States"


def test_a_list_inside_a_popup_a_combobox_owns_is_never_a_question(server: Any, options: BrowserOptions) -> None:
    """While Ashby's suggestions show, the listbox inside the portal the input names is the
    lookup's, not a new question (it was reported as one, so the fill guard stopped)."""
    async def scenario() -> tuple[list[str], list[str]]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url("/forms/ashby-like"))
            before = [f.id for f in page.form.fields]
            await browser.page.locator(".ashby-application-form-input-autocomplete").press_sequentially("Den")
            await browser.page.wait_for_selector("#ashby-location-listbox [role=option]")
            now = await browser.inspect()
            return before, [f.id for f in now.form.fields]
        finally:
            await browser.close()

    before, now = asyncio.run(scenario())
    assert now == before
