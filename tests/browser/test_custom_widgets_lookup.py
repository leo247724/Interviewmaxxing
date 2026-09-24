"""Lookups (Layer C) and phones with a country picker (Layer D) on the localhost mock ATS.

A lookup commits only one of the site's own suggestions: the one the typed value
matches (a verbatim label always wins); otherwise the input is emptied again and the
observed suggestions are returned as ``NEEDS_CHOICE``. ``fill_fields`` then types a
chosen label into just that field. A tel input with a country picker is typed as
given (``+<code><digits>``) and read back by digits and dial code. Fictional data,
real headless Chromium, nothing submitted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import async_playwright

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_browser.aria import COMBO_STATE, fill_lookup
from interviewmaxxing_browser.driver import PlaywrightDriver
from interviewmaxxing_browser.signals import lookup_matches
from interviewmaxxing_core import (
    AnswerSource,
    ApplicationForm,
    ApplicationPacket,
    BrowserOptions,
    ControlType,
    FieldFillStatus,
    PacketAnswer,
    Provenance,
    SelectiveFill,
    TextValue,
    UserInput,
)

LOOKUPS = "/jobs/typeahead/apply"
CONTACT = {"first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test"}
STATE = "() => JSON.parse(JSON.stringify(window.__widgetState))"
INPUTS = "() => Object.fromEntries(Array.from(document.querySelectorAll('input')).map((i) => [i.id, i.value]))"
SHOWN = ("() => Array.from(document.querySelectorAll('.select__single-value, .select__placeholder'))"
         ".map((e) => e.textContent)")


def packet(kit: SimpleNamespace, form: ApplicationForm, answers: dict[str, Any]) -> ApplicationPacket:
    """``kit.build`` for forms with lookups: a lookup's answer is the typed text."""
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


def test_lookups_are_typeaheads_with_no_options(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            return await browser.open(server.url(LOOKUPS))
        finally:
            await browser.close()

    form = kit.run(scenario()).form
    for field_id, label in (("candidate-location", "Location (City)"), ("field-42", "Location"),
                            ("field-43", "What state do you live in?")):
        field = form.field(field_id)
        assert field.control_type is ControlType.TYPEAHEAD and field.options is None, field
        assert field.label == label and field.help_text is None
    assert form.field("candidate-location").required and form.field("field-43").required
    assert not form.field("field-42").required  # the role-less input has no required marker


def test_unique_matches_are_committed_and_read_back(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url(LOOKUPS))
            fill = await browser.fill(page.form, packet(kit, page.form, {
                **CONTACT, "candidate-location": "Austin, TX", "field-42": "Austin, Texas",
                "field-43": "TX"}))
            review = await browser.prepare_review()
            return (fill, review, await browser.page.evaluate(STATE),
                    await browser.page.evaluate(INPUTS), await browser.page.evaluate(SHOWN))
        finally:
            await browser.close()

    fill, review, state, inputs, shown = kit.run(scenario())
    assert fill.ok, fill
    statuses = {f.field_id: f.status for f in fill.fields}
    assert [statuses[i] for i in ("candidate-location", "field-42", "field-43")] == [FieldFillStatus.FILLED] * 3
    assert state == {"candidate_location": {"value": "Austin, Texas, United States"},
                     "location_short": {"value": "Austin, TX, USA"}, "state": {"value": "Texas"}}
    # A React select shows the choice beside an emptied input; the others in the input.
    assert shown == ["Austin, Texas, United States"] and inputs["candidate-location"] == ""
    assert inputs["field-42"] == "Austin, TX, USA" and inputs["field-43"] == "Texas"
    assert review.form.page_errors == []


def test_ambiguous_or_unmatched_lookups_need_a_choice_and_a_chosen_label_commits(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url(LOOKUPS))
            form = page.form
            first = await browser.fill(form, packet(kit, form, {
                **CONTACT, "candidate-location": "Austin", "field-42": "Zzyzx", "field-43": "Texas"}))
            left = (await browser.page.evaluate(INPUTS), await browser.page.evaluate(STATE))
            review = await browser.prepare_review()
            # The person edits another answer meanwhile; a selective fill never touches it.
            await browser.page.fill("#f-first_name", "Avery J.")
            chosen = packet(kit, form, {**CONTACT, "candidate-location": "Austin, Minnesota, United States",
                                        "field-43": "Texas"})
            again = await browser.fill_fields(form, chosen, ["candidate-location"])
            after = (await browser.page.evaluate(INPUTS), await browser.page.evaluate(STATE))
            return first, left, review, again, after, isinstance(browser, SelectiveFill)
        finally:
            await browser.close()

    first, left, review, again, after, selective = kit.run(scenario())
    results = {f.field_id: f for f in first.fields}
    location = results["candidate-location"]
    assert location.status is FieldFillStatus.NEEDS_CHOICE
    assert location.suggestions == [
        "Austin, Texas, United States", "Austin, Minnesota, United States",
        "Austintown, Ohio, United States", "Austin, Indiana, United States",
        "Austin, Arkansas, United States"]  # in the order shown
    nothing = results["field-42"]
    assert nothing.status is FieldFillStatus.NEEDS_CHOICE and nothing.suggestions == []
    assert "no suggestions" in (nothing.detail or "")
    assert results["field-43"].status is FieldFillStatus.FILLED
    assert not first.ok and first.failed_field_ids() == []
    assert [f.field_id for f in first.needs_choice()] == ["candidate-location", "field-42"]
    inputs, state = left
    assert inputs["candidate-location"] == "" and inputs["field-42"] == ""  # nothing half-typed
    assert state["candidate_location"] == {"value": None} and state["location_short"] == {"value": None}
    assert "candidate-location: required control is not completed" in review.form.page_errors
    # The chosen suggestion, typed verbatim, commits exactly that suggestion.
    assert selective
    assert [(f.field_id, f.status) for f in again.fields] == [("candidate-location", FieldFillStatus.FILLED)]
    inputs, state = after
    assert state["candidate_location"] == {"value": "Austin, Minnesota, United States"}
    assert inputs["f-first_name"] == "Avery J."


def test_a_second_fill_of_the_same_form_is_clean(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            page = await browser.open(server.url(LOOKUPS))
            answers = {**CONTACT, "candidate-location": "Round Rock, Texas", "field-42": "Denver, CO",
                       "field-43": "Colorado"}
            first = await browser.fill(page.form, packet(kit, page.form, answers))
            second = await browser.fill(page.form, packet(kit, page.form, answers))
            return first, second, await browser.page.evaluate(STATE)
        finally:
            await browser.close()

    first, second, state = kit.run(scenario())
    assert first.ok and second.ok, (first, second)
    assert state == {"candidate_location": {"value": "Round Rock, Texas, United States"},
                     "location_short": {"value": "Denver, CO, USA"}, "state": {"value": "Colorado"}}


def test_selective_fill_keeps_the_fill_guards(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> list[str]:
        browser = await PlaywrightSessionFactory().start(options)
        errors = []
        try:
            page = await browser.open(server.url(LOOKUPS))
            form = page.form
            good = packet(kit, form, {**CONTACT, "field-43": "Texas"})
            for field_ids, bad in ((["nowhere"], good), (["field-43"], good.model_copy(
                    update={"form_fingerprint": "0" * 64}))):
                with pytest.raises(ValueError) as caught:
                    await browser.fill_fields(form, bad, field_ids)
                errors.append(str(caught.value))
            await browser.open(server.url("/jobs/phone-widget/apply"))
            with pytest.raises(ValueError) as caught:
                await browser.fill_fields(form, good, ["field-43"])
            errors.append(str(caught.value))
            return errors
        finally:
            await browser.close()

    errors = kit.run(scenario())
    assert "not on this form" in errors[0]
    assert "does not fit this form" in errors[1]
    assert "no longer shows the inspected form" in errors[2]


# --- suggestion matching ----------------------------------------------------------------

LONG = ["Austin, Texas, United States", "Austin, Minnesota, United States",
        "Austintown, Ohio, United States", "Round Rock, Texas, United States"]
SHORT = ["Austin, TX, USA", "Austin, MN, USA", "Austintown, OH, USA"]


@pytest.mark.parametrize(("typed", "suggestions", "expected"), [
    ("Austin, TX", LONG, [0]),
    ("Austin, Tex", LONG, [0]),
    ("austin texas", LONG, []),  # no comma: "austin texas" is not a place segment
    ("Austin", LONG, [0, 1]),  # never "Austintown"
    ("Austin, Minnesota, United States", LONG, [1]),  # verbatim label wins
    ("Austin, TX, USA", LONG, [0]),
    ("Austin, Texas", SHORT, [0]),
    ("Austin, Texas, United States of America", SHORT, [0]),
    ("Round Rock, TX", LONG, [3]),
    ("TX", ["Texas", "Tennessee"], [0]),
    ("Zzyzx", LONG, []),
    ("", LONG, []),
])
def test_lookup_matching(typed: str, suggestions: list[str], expected: list[int]) -> None:
    assert lookup_matches(typed, suggestions) == expected


SYNTHETIC = """<!doctype html><title>Fictional lookup</title><form>
<label for="q">Office city</label>
<input id="q" type="text" aria-haspopup="listbox" aria-autocomplete="list">
</form><script>
const q = document.getElementById('q');
q.oninput = () => {
  let list = document.getElementById('q-list');
  if (!list) {
    list = document.createElement('ul'); list.id = 'q-list'; list.setAttribute('role', 'listbox');
    q.after(list); q.setAttribute('aria-controls', 'q-list'); q.setAttribute('aria-expanded', 'true');
  }
  list.textContent = '';
  if (!q.value) { list.remove(); q.removeAttribute('aria-controls'); q.setAttribute('aria-expanded', 'false'); return; }
  for (let i = 0; i < 30; i++) {
    const li = document.createElement('li'); li.id = 'q-option-' + i; li.setAttribute('role', 'option');
    li.textContent = 'Suggestion ' + i + ' ' + 'x'.repeat(240); list.appendChild(li);
  }
};
</script>"""


def test_suggestions_are_capped_and_the_input_is_emptied() -> None:
    async def scenario() -> tuple[Any, str]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(SYNTHETIC)
                driver = PlaywrightDriver(page, action_timeout_s=1.0)
                state = await driver.evaluate(COMBO_STATE, {"selector": "#q", "fields": False})
                binding = {"origin": state["origin"], "url": state["url"], "control": state["control"]}
                outcome = await fill_lookup(driver, "#q", "Sugg", binding, lookup_matches)
                return outcome, await page.input_value("#q")
            finally:
                await browser.close()

    outcome, left = asyncio.run(scenario())
    assert outcome.chosen is None and left == ""
    assert len(outcome.suggestions) == 20 and all(len(s) <= 200 for s in outcome.suggestions)
    assert outcome.suggestions[0].startswith("Suggestion 0 ") and outcome.suggestions[19].startswith("Suggestion 19 ")


# --- phones with a country picker --------------------------------------------------------


def test_international_phone_picks_the_country_and_reads_back_digits(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await PlaywrightSessionFactory().start(options)
        picker = "() => document.querySelector('.iti__selected-country').getAttribute('aria-label')"
        try:
            page = await browser.open(server.url("/jobs/phone-widget/apply"))
            form = page.form
            us = await browser.fill(form, kit.build(form, {**CONTACT, "phone": "+15615550100"}).packet)
            shown_us = (await browser.page.input_value("#phone"), await browser.page.evaluate(picker))
            uk = await browser.fill(form, kit.build(form, {**CONTACT, "phone": "+442079460958"}).packet)
            shown_uk = (await browser.page.input_value("#phone"), await browser.page.evaluate(picker))
            return form, us, shown_us, uk, shown_uk
        finally:
            await browser.close()

    form, us, shown_us, uk, shown_uk = kit.run(scenario())
    assert form.field("phone").expects_international_phone is True
    assert {f.field_id: f.status for f in us.fields}["phone"] is FieldFillStatus.FILLED
    assert shown_us == ("+1 561-555-0100", "Change country, selected United States (+1)")
    # The picker follows the typed dial code; its button is not a question or action.
    assert uk.ok and {f.field_id: f.status for f in uk.fields}["phone"] is FieldFillStatus.FILLED
    assert shown_uk == ("+44 207946 0958", "Change country, selected United Kingdom (+44)")


def test_phone_hint_and_plain_tel_inputs(kit: SimpleNamespace, server: Any, options: BrowserOptions) -> None:
    async def scenario() -> tuple[Any, ...]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            rippling = (await browser.open(server.url("/jobs/div-combobox/apply"))).form
            react = (await browser.open(server.url("/jobs/react-select/apply"))).form
            plain = (await browser.open(server.url("/jobs/validation/apply"))).form
            fill = await browser.fill(plain, kit.build(plain, {"phone": "3035550142"}).packet)
            return rippling, react, plain, fill, await browser.page.input_value("#f-phone")
        finally:
            await browser.close()

    rippling, react, plain, fill, typed = kit.run(scenario())
    assert rippling.field("phone").expects_international_phone is True  # "+1 US" code picker
    assert react.field("phone").expects_international_phone is False  # "Country" is its own question
    assert plain.field("phone").expects_international_phone is False
    assert {f.field_id: f.status for f in fill.fields}["phone"] is FieldFillStatus.FILLED
    assert typed == "3035550142"  # a plain tel input is typed and read back exactly
