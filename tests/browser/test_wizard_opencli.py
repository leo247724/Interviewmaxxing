"""OpenCLI parity for dialog wizards: the localhost mock's LinkedIn-style Easy Apply
dialog, its open-shadow-root variant and a Greenhouse-style embedded application page,
driven through the OpenCLI driver primitives.

``BridgeReplica`` stands in for the ``opencli`` process. It answers ``opencli browser``
argv lists the way OpenCLI 1.8.6 does (verified live on 2026-09-24) by acting on one
real headless Chromium page of the mock: CSS targets are the document's own
``querySelectorAll`` and never enter shadow roots (the driver uses neither semantic
locators nor refs), and Browser Bridge refuses ``upload``. The real ``opencli`` binary is
never run. Everything is fictional (Brambleway Analytics, Avery Quill) and stays on
localhost.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import shutil
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import async_playwright

from interviewmaxxing_browser import (
    CapabilityUnsupported,
    CommandResult,
    NotActionable,
    OpenCliApplicationBrowser,
    OpenCliConfig,
    OpenCliSessionFactory,
    inspector_script,
)
from interviewmaxxing_browser import opencli as oc
from interviewmaxxing_cli.runner import (
    LocalApplicationRunner,
    NoninteractiveInteraction,
    pending_inputs,
)
from interviewmaxxing_core import (
    ApplicationField,
    ApplicationForm,
    ApplicationState,
    ApplicationStore,
    BrowserOptions,
    ControlType,
    FieldFillStatus,
    FillResult,
    LocalPaths,
    MissingReason,
    PageInspection,
    PageKind,
    SemanticType,
)

CONFIG = OpenCliConfig(profile="fixture-profile", navigation_grace_s=0.05, poll_interval_s=0.01)
SETTLE_S = 10.0
WIZARD = "/jobs/modal-wizard"
EASY = "() => JSON.parse(JSON.stringify(window.__easyApply))"
CONTACT = {"Email address": "avery.quill@example.test", "Phone country code": "United States (+1)",
           "Mobile phone number": "+1 (303) 555-0142", "City": "Denver"}
"""The contact step's answers: the first three are what the dialog already shows."""
OTHER_CONTACT = {"Email address": "a.quill@example.test", "Phone country code": "Canada (+1)",
                 "Mobile phone number": "+1 (720) 555-0199", "City": "Aurora"}
"""Answers that differ from every pre-filled value, so each field needs a write."""
SQL = "How many years of work experience do you have with SQL?"
AUTHORIZED = "Are you legally authorized to work in the United States?"
SPONSORSHIP = "Will you now or in the future require sponsorship for employment visa status?"
QUESTIONS = {SQL: "5", AUTHORIZED: "Yes", SPONSORSHIP: "No"}
FOLLOW = "Follow Brambleway Analytics to stay up to date with their page."
ATTACH = "Attach your resume in the browser window"
PINNED = "resume_avery_quill.pdf"
KEPT_SELECT = "already selected; left as it is"
KEPT_TEXT = "already shows this value; left as it is"
SHADOW = "inside the page's shadow root; do this step yourself in the browser window, then continue"
VERIFIED_AT = "2026-09-01T12:00:00Z"
FILLED, FAILED = FieldFillStatus.FILLED, FieldFillStatus.FAILED

_DESCRIBE = """(e) => ({tag: e.tagName.toLowerCase(), id: e.id || null, for: e.htmlFor || null,
  label: e.getAttribute('aria-label'), value: 'value' in e ? String(e.value) : null,
  text: (e.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 120)})"""
"""Read-only facts of the element a targeted command resolved to (``BridgeReplica.hits``)."""


class BridgeReplica:
    """Answers ``opencli browser`` argv lists the way OpenCLI 1.8.6 does, on one real
    Playwright page of the localhost mock: CSS targets are the document's own
    ``querySelectorAll`` (they do not enter shadow roots, verified live on 2026-09-24),
    envelopes carry ``matches_n``/``match_level``, ``fill`` reports ``verified``/``actual``,
    and ``upload`` is refused like Browser Bridge refuses it.

    Test instrumentation only (no effect on the page): ``results`` holds every answer and
    ``hits`` describes the element each targeted command resolved to, read before acting."""

    TAB = "REPLICA0000TAB1"

    def __init__(self, page: Any) -> None:
        self.page = page
        self.calls: list[list[str]] = []
        self.results: list[CommandResult] = []
        self.hits: list[dict[str, Any]] = []

    @staticmethod
    def ok(payload: Any) -> CommandResult:
        return CommandResult(0, json.dumps(payload) + "\n", "")

    @staticmethod
    def error(code: str, message: str) -> CommandResult:
        return CommandResult(1, json.dumps({"error": {"code": code, "message": message}}), f"✖  [{code}] {message}\n")

    def commands(self) -> list[str]:
        return [argv[argv.index("browser") + 2] for argv in self.calls]

    def targets(self, command: str) -> list[str]:
        out = []
        for argv in self.calls:
            if argv[argv.index("browser") + 2] == command and "--" in argv:
                out.append(argv[argv.index("--") + 1])
        return out

    async def _count(self, selector: str) -> int:
        return int(await self.page.evaluate(
            "(s) => { try { return document.querySelectorAll(s).length; } catch (e) { return -1; } }", selector))

    async def __call__(self, argv: Sequence[str], timeout_s: float) -> CommandResult:
        result = await self._answer(argv)
        self.results.append(result)
        return result

    async def _answer(self, argv: Sequence[str]) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        i = argv.index("browser")
        command = argv[i + 2]
        positionals = argv[argv.index("--") + 1:] if "--" in argv else []
        if command == "tab":
            sub = argv[i + 3]
            if sub == "new":
                return self.ok({"page": self.TAB, "url": None})
            if sub == "list":
                return self.ok([{"page": self.TAB}])
            return self.ok({"closed": positionals[0] if positionals else self.TAB})
        if command == "close":
            return self.ok({"closed": True})
        if command == "open":
            await self.page.goto(positionals[0], wait_until="load")
            return self.ok({"url": self.page.url, "page": self.TAB})
        if command == "eval":
            value = await self.page.evaluate(positionals[0])
            return CommandResult(0, (value if isinstance(value, str) else json.dumps(value)) + "\n", "")
        if command == "keys":
            await self.page.keyboard.press(positionals[0])
            return self.ok({"pressed": positionals[0]})
        if command == "screenshot":
            await self.page.screenshot(path=positionals[0], full_page=True)
            return self.ok({"path": positionals[0]})
        if command == "upload":
            return self.error("upload_failed", "Browser Bridge refused to set files on this input")
        selector = positionals[0]
        n = await self._count(selector)
        if n < 0:
            return self.error("invalid_selector", f"Invalid CSS selector: {selector}")
        if n == 0:
            return self.error("selector_not_found", f"CSS selector {selector!r} matched 0 elements")
        if n > 1:
            return self.error("selector_ambiguous", f"CSS selector {selector!r} matched {n} elements")
        target = self.page.locator("css:light=" + selector)
        self.hits.append({"call": len(self.calls) - 1, "command": command, "selector": selector,
                          **await target.evaluate(_DESCRIBE)})
        found = {"matches_n": 1, "match_level": "exact", "target": selector}
        if command == "fill":
            await target.fill(positionals[1])
            actual = await target.input_value()
            return self.ok({"filled": True, "verified": actual == positionals[1], "text": positionals[1],
                            "actual": actual, **found})
        if command == "type":
            await target.click()
            await target.press_sequentially(positionals[1])
            return self.ok({"typed": True, "text": positionals[1], **found})
        if command == "click":
            await target.click(no_wait_after=True)
            return self.ok({"clicked": True, **found})
        if command == "focus":
            await target.focus()
            return self.ok({"focused": True, **found})
        if command in ("check", "uncheck"):
            wanted = command == "check"
            before = await target.is_checked()
            if before != wanted:
                await target.dispatch_event("click")
            after = await target.is_checked()
            return self.ok({"checked": after, "changed": before != after, **found})
        if command == "select":
            option = positionals[1]
            try:
                await target.select_option(label=option)
            except Exception:
                await target.select_option(value=option)
            return self.ok({"selected": option, **found})
        return self.error("unknown_command", command)


# --- helpers ------------------------------------------------------------------------------


def command_of(argv: Sequence[str]) -> str:
    return argv[list(argv).index("browser") + 2]


def after_session(argv: Sequence[str]) -> list[str]:
    """The argv after ``browser <session>``: the command, its options and positionals."""
    return list(argv[list(argv).index("browser") + 2:])


def actions(calls: Sequence[Sequence[str]]) -> list[str]:
    """Commands that can act on the page, in order (reads and evidence left out)."""
    return [command_of(argv) for argv in calls if command_of(argv) not in ("eval", "screenshot")]


def releases(calls: Sequence[Sequence[str]]) -> list[list[str]]:
    """The ``tab close`` and ``close`` commands among ``calls`` (a released tab and lease)."""
    return [after_session(argv) for argv in calls
            if after_session(argv)[:1] == ["close"] or after_session(argv)[:2] == ["tab", "close"]]


def prepare_only(options: BrowserOptions) -> BrowserOptions:
    """``BrowserOptions`` allow submission by default; preparation never does."""
    return dataclasses.replace(options, allow_submission=False)


def form_of(inspection: PageInspection) -> ApplicationForm:
    assert inspection.kind is PageKind.APPLICATION_FORM and inspection.form is not None, inspection.message
    return inspection.form


def field_labelled(form: ApplicationForm, label: str) -> ApplicationField:
    [found] = [f for f in form.fields if f.label == label]
    return found


def by_label(form: ApplicationForm, specs: Mapping[str, Any]) -> dict[str, Any]:
    """``kit.build`` answers (keyed by field id) from label -> value specs."""
    return {field_labelled(form, label).id: value for label, value in specs.items()}


def results_by_label(form: ApplicationForm, fill: FillResult) -> dict[str, tuple[FieldFillStatus, str | None]]:
    results = {r.field_id: r for r in fill.fields}
    return {f.label: (results[f.id].status, results[f.id].detail) for f in form.fields if f.id in results}


_HEAD, _REST = oc._wrap("\0", None).split("\0")
_TAIL = _REST.removeprefix(")(null")


def unwrap(expression: str) -> tuple[str, Any]:
    """The allowlisted script and JSON argument an ``eval`` expression carries. It must be
    exactly the driver's own wrapper around one of ``oc._ALLOWED_SCRIPTS``, nothing more."""
    for script in oc._ALLOWED_SCRIPTS:
        opening = _HEAD + script + ")("
        if not (expression.startswith(opening) and expression.endswith(_TAIL)):
            continue
        try:
            arg = json.loads(expression[len(opening):len(expression) - len(_TAIL)])
        except ValueError:
            continue
        if oc._wrap(script, arg) == expression:
            return script, arg
    raise AssertionError(f"eval of a script outside the allowlist: {expression[:160]!r}")


@asynccontextmanager
async def opencli_browser(
    options: BrowserOptions,
) -> AsyncIterator[tuple[OpenCliApplicationBrowser, BridgeReplica]]:
    """One headless Chromium page of the mock behind a ``BridgeReplica``, and the OpenCLI
    browser over it (``attach_files`` is off, as with Browser Bridge)."""
    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(headless=True)
        try:
            replica = BridgeReplica(await chromium.new_page())
            browser = await OpenCliSessionFactory(CONFIG, runner=replica, settle_timeout_s=SETTLE_S).start(options)
            yield browser, replica
        finally:
            await chromium.close()


async def walk_to_review(server: Any, options: BrowserOptions, kit: SimpleNamespace) -> SimpleNamespace:
    """The Easy Apply dialog from the posting to its review step over the replica, with
    the packets of the verified Playwright walk, then ``prepare_review``, ``submit`` and
    ``close``. Records each step's form and fill, the calls it sent (``spans``) and the
    dialog's ``window.__easyApply`` after it."""
    seen = SimpleNamespace(forms=[], models=[], fills=[], spans=[], states=[])
    async with opencli_browser(prepare_only(options)) as (browser, replica):
        seen.opened = await browser.open(server.url(WIZARD))
        seen.forms.append(form_of(seen.opened))
        seen.models.append(browser.last_page)

        async def fill(form: ApplicationForm, answers: Mapping[str, Any]) -> None:
            start = len(replica.calls)
            seen.fills.append(await browser.fill(form, kit.build(form, answers).packet))
            seen.spans.append((start, len(replica.calls)))
            seen.states.append(await replica.page.evaluate(EASY))

        async def advance() -> ApplicationForm:
            nav = await browser.advance()
            assert nav.advanced, nav.validation_errors
            seen.forms.append(form_of(nav.inspection))
            seen.models.append(browser.last_page)
            return seen.forms[-1]

        await fill(seen.forms[0], by_label(seen.forms[0], CONTACT))
        resume = await advance()
        await fill(resume, {resume.fields[0].id: kit.RESUME})
        questions = await advance()
        await fill(questions, by_label(questions, QUESTIONS))
        await advance()
        start = len(replica.calls)
        seen.review = await browser.prepare_review()
        seen.submit = await browser.submit()
        seen.at_review = (start, len(replica.calls))
        seen.final = await replica.page.evaluate(EASY)
        start = len(replica.calls)
        await browser.close()
        seen.closing = replica.calls[start:]
        seen.tab_after_close = browser.driver.tab
        seen.calls, seen.hits, seen.opens = replica.calls, replica.hits, replica.targets("open")
    return seen


def calls_in(seen: SimpleNamespace, span: tuple[int, int]) -> list[list[str]]:
    start, end = span
    return list(seen.calls[start:end])


def hits_in(seen: SimpleNamespace, span: tuple[int, int]) -> list[dict[str, Any]]:
    start, end = span
    return [hit for hit in seen.hits if start <= hit["call"] < end]


# --- 1. the Easy Apply dialog, end to end --------------------------------------------------


def test_easy_apply_walk_over_opencli_keeps_prefilled_values_and_submits_nothing(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    seen = kit.run(walk_to_review(server, options, kit))
    contact, resume, questions, review = seen.forms

    # The posting's apply link is opened like a link (no click); the dialog is the form.
    assert "Reached the form: followed the apply link 'Easy Apply'." in (seen.opened.message or "")
    assert seen.opened.observed_url == server.url(WIZARD + "/apply?openSDUIApplyFlow=true")
    assert seen.opens == [server.url(WIZARD), server.url(WIZARD + "/apply?openSDUIApplyFlow=true")]
    assert seen.models[0].dialog_index is not None and seen.models[0].step_source == "progress"
    # Only the dialog's own questions: the search box, message box and note field behind it are not.
    assert [(f.label, f.control_type) for f in contact.fields] == [
        ("Email address", ControlType.SELECT), ("Phone country code", ControlType.SELECT),
        ("Mobile phone number", ControlType.TEXT), ("City", ControlType.TEXT)]
    assert (contact.step, contact.is_final_step) == (0, False)
    bindings = seen.models[0].bindings
    assert {f.label: bindings[f.id].value for f in contact.fields} == {
        "Email address": "avery.quill@example.test", "Phone country code": "United States (+1)",
        "Mobile phone number": "3035550142", "City": "Boulder"}

    # Contact info: what the site pre-filled is kept; only the city is written.
    assert results_by_label(contact, seen.fills[0]) == {
        "Email address": (FILLED, KEPT_SELECT), "Phone country code": (FILLED, KEPT_SELECT),
        "Mobile phone number": (FILLED, KEPT_TEXT), "City": (FILLED, None)}
    assert not any(fill.page_errors for fill in seen.fills)
    phone, city = (bindings[field_labelled(contact, label).id].selector for label in ("Mobile phone number", "City"))
    assert actions(calls_in(seen, seen.spans[0])) == ["fill"]
    [typed_city] = hits_in(seen, seen.spans[0])
    assert (typed_city["selector"], typed_city["tag"]) == (city, "input")
    fill_targets = [argv[argv.index("--") + 1] for argv in seen.calls if command_of(argv) == "fill"]
    assert phone not in fill_targets and fill_targets[0] == city
    writes, answers = seen.states[0]["writes"], seen.states[0]["answers"]
    assert (writes["email"], writes["phone_country"], writes["phone"]) == (0, 0, 0) and writes["city"] > 0
    assert (answers["phone"], answers["city"]) == ("3035550142", "Denver")

    # Resume: the saved card named like the pinned file is chosen through its own label.
    assert (resume.step, resume.is_final_step) == (33, False)
    assert [(f.label, f.control_type) for f in resume.fields] == [("Upload resume", ControlType.FILE)]
    assert results_by_label(resume, seen.fills[1]) == {
        "Upload resume": (FILLED, f"chose the resume {PINNED!r} already on the site is the pinned file")}
    assert actions(calls_in(seen, seen.spans[1])) == ["click"]
    [card] = hits_in(seen, seen.spans[1])
    assert (card["command"], card["tag"], card["for"], card["text"]) == (
        "click", "label", "jobsDocumentCardToggle-1", f"Select resume {PINNED}")
    assert "upload" not in [command_of(argv) for argv in seen.calls]
    assert seen.states[1]["answers"]["resume"] == PINNED and not seen.states[1]["answers"]["resume_uploaded"]

    # Additional questions: one fill, one check, one select, in the questions' order.
    assert (questions.step, questions.is_final_step) == (67, False)
    assert [(f.label, f.control_type) for f in questions.fields] == [
        (SQL, ControlType.TEXT), (AUTHORIZED, ControlType.RADIO), (SPONSORSHIP, ControlType.SELECT)]
    assert results_by_label(questions, seen.fills[2]) == dict.fromkeys(QUESTIONS, (FILLED, None))
    assert actions(calls_in(seen, seen.spans[2])) == ["fill", "check", "select"]
    step = seen.models[2].bindings
    sql, authorized, sponsorship = (field_labelled(questions, label).id for label in QUESTIONS)
    assert [(h["selector"], h["tag"]) for h in hits_in(seen, seen.spans[2])] == [
        (step[sql].selector, "input"), (step[authorized].option_selectors["Yes"], "input"),
        (step[sponsorship].selector, "select")]
    assert hits_in(seen, seen.spans[2])[1]["value"] == "Yes"  # the radio of the answer, not "No"
    assert {k: seen.states[2]["answers"][k] for k in ("sql_years", "work_authorization", "sponsorship")} == {
        "sql_years": "5", "work_authorization": "Yes", "sponsorship": "No"}

    # Review: the final step. Preparation reads it; the submit belongs to the person.
    assert (review.step, review.is_final_step) == (100, True)
    assert [(f.label, f.control_type, f.required) for f in review.fields] == [(FOLLOW, ControlType.CHECKBOX, False)]
    assert seen.review.kind is PageKind.APPLICATION_FORM and form_of(seen.review).page_errors == []
    assert not seen.submit.dispatched and seen.submit.detail == "submission belongs to the user in this session"
    assert actions(calls_in(seen, seen.at_review)) == []
    # Next, Review and Submit share one positional selector, so the element each click hit is
    # what counts: the steps' own controls and the card's label, never "Submit application".
    assert [(h["tag"], h["label"]) for h in seen.hits if h["command"] == "click"] == [
        ("button", "Continue to next step"), ("label", None), ("button", "Continue to next step"),
        ("button", "Review your application")]
    assert (seen.final["step"], seen.final["submitted"], seen.final["open"]) == (4, False, True)
    summary = server.submissions("modal-wizard")
    assert (summary["accepted_count"], summary["rejected_count"]) == (0, 0)

    # Nothing is left for the person: the owned tab is closed, then the lease released.
    assert [after_session(argv) for argv in seen.closing] == [["tab", "close", "--", BridgeReplica.TAB], ["close"]]
    assert seen.tab_after_close is None


# --- 2. no usable saved resume: the person attaches it, the tab stays -----------------------


def test_unusable_resume_cards_hand_the_resume_to_the_person_and_keep_the_tab(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> SimpleNamespace:
        seen = SimpleNamespace()
        async with opencli_browser(prepare_only(options)) as (browser, replica):
            form = form_of(await browser.open(server.url(WIZARD + "?resumes=nomatch")))
            seen.contact = await browser.fill(form, kit.build(form, by_label(form, CONTACT)).packet)
            nav = await browser.advance()
            form = seen.resume_form = form_of(nav.inspection)
            start = len(replica.calls)
            with pytest.raises(ValueError, match="the resume must be attached in the browser window") as refused:
                await browser.fill(form, kit.build(form, {form.fields[0].id: kit.RESUME}).packet)
            seen.refused, seen.sent_while_refusing = str(refused.value), replica.calls[start:]
            seen.again = await browser.inspect()
            seen.pending = list(browser.last_page.unsupported_pending) if browser.last_page else None
            seen.waiting = browser.waiting_for_person
            seen.state = await replica.page.evaluate(EASY)
            start = len(replica.calls)
            await browser.close()
            seen.closing, seen.tab, seen.location = replica.calls[start:], browser.driver.tab, browser.location
            seen.commands = replica.commands()
        return seen

    seen = kit.run(scenario())
    assert seen.contact.ok, seen.contact
    assert [(f.label, f.control_type) for f in seen.resume_form.fields] == [("Upload resume", ControlType.FILE)]
    assert ("none of the 2 resumes on the site ('Avery_Quill_Resume_2025.pdf', 'AQ_CV_marketing.docx') "
            f"is the pinned {PINNED!r}") in seen.refused
    assert actions(seen.sent_while_refusing) == []  # refused before anything was written

    # The next inspection makes the resume the person's: required, with the pinned file's name.
    again = form_of(seen.again)
    [field] = again.fields
    assert (again.step, field.control_type, field.required) == (33, ControlType.UNSUPPORTED, True)
    assert f"{ATTACH} ({PINNED})." in (field.help_text or "")
    assert seen.pending == [field.id] and seen.waiting is True
    # The card the site preselected is left for the person, not chosen or deselected by us.
    assert seen.state["answers"]["resume"] == "Avery_Quill_Resume_2025.pdf" and seen.state["writes"]["resume"] == 0

    # The owned tab and its lease stay for the person to attach the file in.
    assert seen.closing == [] and seen.tab == BridgeReplica.TAB
    assert BridgeReplica.TAB in seen.location and "fixture-profile" in seen.location
    assert "upload" not in seen.commands
    assert server.submissions("modal-wizard")["accepted_count"] == 0


def test_without_saved_resumes_the_resume_step_is_the_persons_at_once(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> SimpleNamespace:
        seen = SimpleNamespace()
        async with opencli_browser(prepare_only(options)) as (browser, replica):
            form = form_of(await browser.open(server.url(WIZARD + "?resumes=none")))
            seen.contact = await browser.fill(form, kit.build(form, by_label(form, CONTACT)).packet)
            seen.resume = form_of((await browser.advance()).inspection)
            seen.pending = list(browser.last_page.unsupported_pending) if browser.last_page else None
            seen.waiting = browser.waiting_for_person
            start = len(replica.calls)
            await browser.close()
            seen.closing, seen.commands = replica.calls[start:], replica.commands()
        return seen

    seen = kit.run(scenario())
    assert seen.contact.ok, seen.contact
    [field] = seen.resume.fields
    assert (seen.resume.step, field.control_type, field.required) == (33, ControlType.UNSUPPORTED, True)
    assert f"{ATTACH}." in (field.help_text or "")  # the pinned file is not known before a fill
    assert seen.pending == [field.id] and seen.waiting is True
    assert seen.closing == [] and "upload" not in seen.commands


# --- 3. the runner asks the person to attach the resume ------------------------------------


class ReplicaFactory:
    """``BrowserSessionFactory`` for the runner: each ``start`` launches headless Chromium,
    wraps one page in a ``BridgeReplica`` and starts the OpenCLI browser over it; Playwright
    closes when the browser does. The dialog's ``window.__easyApply`` is kept at close."""

    def __init__(self) -> None:
        self.replicas: list[BridgeReplica] = []
        self.options: list[BrowserOptions] = []
        self.states: list[dict[str, Any]] = []

    async def start(self, options: BrowserOptions) -> OpenCliApplicationBrowser:
        self.options.append(options)
        playwright = await async_playwright().start()
        try:
            chromium = await playwright.chromium.launch(headless=True)
            page = await chromium.new_page()
            replica = BridgeReplica(page)
            browser = await OpenCliSessionFactory(CONFIG, runner=replica, settle_timeout_s=SETTLE_S).start(options)
        except BaseException:
            await playwright.stop()
            raise
        self.replicas.append(replica)
        release = browser.close

        async def close() -> None:
            try:
                await release()
            finally:
                with contextlib.suppress(Exception):
                    self.states.append(await page.evaluate(EASY))
                await chromium.close()
                await playwright.stop()

        browser.close = close
        return browser


async def _contact_step(options: BrowserOptions, url: str) -> ApplicationForm:
    async with opencli_browser(prepare_only(options)) as (browser, _replica):
        return form_of(await browser.open(url))


def _write_profile(paths: LocalPaths, contact: ApplicationForm, resume_path: Path) -> None:
    """The fictional candidate, the resume named like the pinned file, and a saved answer for
    the dial-code select (the other contact questions come from the verified identity)."""
    directory = paths.profile_dir / "default"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(resume_path, directory / PINNED)
    country = field_labelled(contact, "Phone country code")
    profile = {
        "id": "default",
        "identity": {
            "first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
            "phone": "+1 (303) 555-0142",
            "linkedin_url": "https://www.linkedin.example.test/in/avery-quill",
            "address": {"city": "Denver", "region": "CO", "country": "United States"},
            "verified_at": VERIFIED_AT,
        },
        "resume": {"id": "resume_supplied", "path": PINNED},
        "facts": [],
        "saved_answers": [{
            "id": "sa.phone_country", "scope": "GLOBAL",
            "semantic_type": None if country.semantic_type is SemanticType.UNKNOWN else country.semantic_type.value,
            "question": country.question_text, "value": "United States (+1)", "confirmed_at": VERIFIED_AT,
        }],
    }
    (directory / "profile.json").write_text(json.dumps(profile, indent=2))


def test_runner_stops_for_the_person_to_attach_the_resume_and_keeps_the_tab(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, isolated_imx_home: LocalPaths
) -> None:
    url = server.url(WIZARD + "?resumes=nomatch")
    _write_profile(isolated_imx_home, kit.run(_contact_step(options, url)), kit.RESUME_PATH)
    factory = ReplicaFactory()
    runner = LocalApplicationRunner(paths=isolated_imx_home, interaction=NoninteractiveInteraction(),
                                    headless=True, browser_factory=factory, prepare_only=True)
    result = kit.run(runner.apply(url, candidate_id="default"))

    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    [item] = [m for m in result.missing_inputs if m.required]
    assert item.reason is MissingReason.UNSUPPORTED_CONTROL and item.form_step == 33
    assert f"{ATTACH} ({PINNED})." in item.prompt
    assert ATTACH in result.message
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert [m.id for m in pending_inputs(store, result.application_id)] == [item.id]
        assert store.list_attempts(result.application_id) == []

    # One prepare-only OpenCLI session that filled the contact step and wrote no resume.
    [options_used], [replica] = factory.options, factory.replicas
    assert options_used.allow_submission is False
    [state] = factory.states
    assert state["answers"]["city"] == "Denver" and state["writes"]["phone"] == 0
    assert state["answers"]["resume"] == "Avery_Quill_Resume_2025.pdf" and state["writes"]["resume"] == 0
    assert "upload" not in replica.commands()
    # The tab is kept for the person: neither ``tab close`` nor ``close`` was sent.
    assert releases(replica.calls) == []
    assert server.submissions("modal-wizard")["accepted_count"] == 0


# --- 4. an open shadow root: read through eval, never written ------------------------------



def test_shadow_dialog_is_read_through_eval_but_writes_are_refused(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> SimpleNamespace:
        seen = SimpleNamespace()
        async with opencli_browser(prepare_only(options)) as (browser, replica):
            seen.opened = await browser.open(server.url(WIZARD + "?shadow=1"))
            seen.model = browser.last_page
            form = seen.form = form_of(seen.opened)
            seen.dialogs = await replica.page.evaluate(
                "() => [document.querySelectorAll('[role=dialog]').length, document.getElementById("
                "'interop-outlet').shadowRoot.querySelectorAll('[role=dialog]').length]")
            start = len(replica.calls)
            seen.kept = await browser.fill(form, kit.build(form, by_label(form, CONTACT)).packet)
            seen.other = await browser.fill(form, kit.build(form, by_label(form, OTHER_CONTACT)).packet)
            seen.sent = [(argv, replica.results[i]) for i, argv in enumerate(replica.calls) if i >= start]
            seen.state = await replica.page.evaluate(EASY)
            with pytest.raises(NotActionable):
                await browser.advance()  # the Next button is out of OpenCLI's reach as well
            seen.after = await replica.page.evaluate(EASY)
            seen.commands = replica.commands()
        return seen

    seen = kit.run(scenario())
    # Inspection runs through eval and reads the open shadow root: the same dialog step.
    assert "Reached the form: followed the apply link 'Easy Apply'." in (seen.opened.message or "")
    assert seen.dialogs == [0, 1]
    assert seen.model.dialog_index is not None and seen.model.step_source == "progress"
    assert [f.label for f in seen.form.fields] == list(CONTACT)
    assert (seen.form.step, seen.form.is_final_step) == (0, False)
    selectors = {f.label: seen.model.bindings[f.id].selector for f in seen.form.fields}

    # Values read through the shadow root are kept; the one write is refused for the person.
    assert results_by_label(seen.form, seen.kept) == {
        "Email address": (FILLED, KEPT_SELECT), "Phone country code": (FILLED, KEPT_SELECT),
        "Mobile phone number": (FILLED, KEPT_TEXT),
        "City": (FAILED, f"OpenCLI cannot reach {selectors['City']} {SHADOW}")}
    # Every select and fill needs a write here, and every one is refused the same way.
    assert results_by_label(seen.form, seen.other) == {
        label: (FAILED, f"OpenCLI cannot reach {selectors[label]} {SHADOW}") for label in OTHER_CONTACT}
    writes = [(argv, result) for argv, result in seen.sent if command_of(argv) not in ("eval", "screenshot")]
    assert [command_of(argv) for argv, _ in writes] == ["fill", "select", "select", "fill", "fill"]
    assert all(json.loads(result.stdout)["error"]["code"] == "selector_not_found" for _, result in writes)

    # Nothing was written, and the step did not move.
    assert seen.state["writes"] == dict.fromkeys(seen.state["writes"], 0)
    assert {k: seen.state["answers"][k] for k in ("email", "phone_country", "phone", "city")} == {
        "email": "avery.quill@example.test", "phone_country": "United States (+1)", "phone": "3035550142",
        "city": "Boulder"}
    assert (seen.after["step"], seen.after["writes"]) == (1, seen.state["writes"])
    assert not {"upload", "check", "uncheck", "type", "keys"} & set(seen.commands)


def test_shadow_step_control_is_refused_with_a_message_for_the_person(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> None:
        async with opencli_browser(prepare_only(options)) as (browser, _replica):
            form = form_of(await browser.open(server.url(WIZARD + "?shadow=1")))
            assert (form.next_selector or "").startswith("#interop-outlet >> ")
            with pytest.raises(CapabilityUnsupported, match="inside the page's shadow root; do this step yourself"):
                await browser.advance()

    kit.run(scenario())


# --- 5. evaluation stays within the fixed read-only scripts -------------------------------


def test_every_eval_the_walk_sent_is_an_allowlisted_read_script(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    assert oc._IN_SHADOW in oc._ALLOWED_SCRIPTS
    seen = kit.run(walk_to_review(server, options, kit))
    evals = [argv[argv.index("--") + 1] for argv in seen.calls if command_of(argv) == "eval"]
    assert evals
    used = set()
    for expression in evals:
        assert any("(" + script + ")(" in expression for script in oc._ALLOWED_SCRIPTS), expression[:160]
        script, _ = unwrap(expression)
        used.add(script)
    assert {inspector_script(), oc._DOC_STATE, oc._CONTROL_STATE} <= used


# --- 6. an embedded page that refuses to load on its own ----------------------------------


def test_embedded_only_application_page_is_opened_but_not_operated_over_opencli(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    careers = server.url("/jobs/iframe-embed?embedded_only=1")
    src = server.url("/embed/job_app?for=brambleway&token=4007131&embedded_only=1")

    async def scenario() -> SimpleNamespace:
        seen = SimpleNamespace()
        async with opencli_browser(prepare_only(options)) as (browser, replica):
            seen.enter_frame = hasattr(browser.driver, "enter_frame")
            seen.opened = await browser.open(careers)
            seen.status = browser.driver.last_status
            start = len(replica.calls)
            await browser.close()
            seen.closing, seen.opens, seen.hits = replica.calls[start:], replica.targets("open"), replica.hits
            seen.commands = replica.commands()
        return seen

    seen = kit.run(scenario())
    assert seen.enter_frame is False  # the OpenCLI driver cannot operate inside a frame
    # The frame's page is opened like a link and refuses a top-level load: not a form.
    assert seen.opened.kind is PageKind.ERROR and seen.opened.form is None and seen.status == 403
    assert seen.opened.observed_url == src
    assert f"opened the application page embedded in {careers} ({src})" in (seen.opened.message or "")
    assert "operated inside the frame" not in (seen.opened.message or "")
    assert seen.opens == [careers, src]  # the careers page is not reopened to enter the frame
    # At most the careers page's panel switch was clicked (the embed script may still be pending).
    assert all((h["command"], h["label"]) == ("click", "Switch to application form") for h in seen.hits)
    assert not {"fill", "select", "check", "upload"} & set(seen.commands)
    assert [after_session(argv) for argv in seen.closing] == [["tab", "close", "--", BridgeReplica.TAB], ["close"]]
    summary = server.submissions("iframe-embed")
    assert (summary["accepted_count"], summary["rejected_count"]) == (0, 0)
