"""Round 14: the last live holds of retry seven, reproduced on fictional pages.

1. Questions that appear mid-fill (Greenhouse's EEO block, Teamtailor's late question) are
   resolved in the same run: the fill reports them for a fresh inspection instead of
   failing, even when the page changed other controls at the same time.
2. Paylocity's "Address Line 1" is a text answer even when its menu was never probed.
3. and 4. ARIA checkboxes and radios (Greenhouse draws its choices as role="checkbox"
   buttons with hidden bubble inputs) are questions with observed options, operated and
   read back by aria-checked; the single "double-check" attestation is one of them.

Real headless Chromium against the local mock ATS, fictional data, temp IMX_HOME; nothing
is submitted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_browser.ai import AIFormRouter, BoundedDecisions, CallBudget
from interviewmaxxing_browser.ai.routing import DynamicPacketResolver
from interviewmaxxing_cli.runner import LocalApplicationRunner, NoninteractiveInteraction
from interviewmaxxing_core import (
    AnswerSource,
    Application,
    ApplicationForm,
    ApplicationState,
    ApplicationStore,
    BooleanValue,
    BrowserOptions,
    CandidateProfile,
    ControlType,
    FieldFillStatus,
    JobRecord,
    LocalPaths,
    MissingReason,
    PacketContext,
    SemanticType,
    TextValue,
)
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient

REPO = Path(__file__).resolve().parents[2]
RESUME_PATH = REPO / "tests" / "fixtures" / "browser" / "resume_avery_quill.pdf"
VERIFIED_AT = "2026-09-01T12:00:00Z"
CONTACT = {"first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test"}


def _statuses(result: Any) -> dict[str, FieldFillStatus]:
    return {f.field_id: f.status for f in result.fields}


async def _open(options: BrowserOptions, url: str) -> Any:
    browser = await PlaywrightSessionFactory().start(options)
    page = await browser.open(url)
    assert page.form is not None, page.message
    return browser


# --- 3. and 4. ARIA checkboxes and radios ---------------------------------------------------

ARIA = "/jobs/greenhouse-aria/apply"
CLIENTS = "question_7201[]"
BUDGETS = "question_7202"
DOUBLE_CHECK = "question_7203"
ARIA_ANSWERS = {**CONTACT, CLIENTS: ["4-7", "8 or more"], BUDGETS: "$50k to $250k", DOUBLE_CHECK: True}
ARIA_STATE = """() => ({
  checked: Array.from(document.querySelectorAll('button[role="checkbox"], button[role="radio"]'))
    .filter((b) => b.getAttribute('aria-checked') === 'true').map((b) => b.nextElementSibling.value),
  bubbles: Array.from(document.querySelectorAll('input[aria-hidden="true"]:checked')).map((i) => i.name + '=' + i.value),
})"""


def test_aria_checkboxes_and_radios_are_questions_with_their_options(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> ApplicationForm:
        browser = await _open(options, server.url(ARIA))
        try:
            return (await browser.inspect()).form
        finally:
            await browser.close()

    form = kit.run(scenario())
    clients, budgets, double_check = form.field(CLIENTS), form.field(BUDGETS), form.field(DOUBLE_CHECK)
    assert clients.control_type is ControlType.CHECKBOX_GROUP, clients
    assert clients.label == "How many clients do you currently support?" and clients.required
    assert [(o.value, o.label) for o in clients.options or []] == [("71", "1-3"), ("72", "4-7"), ("73", "8 or more")]
    assert budgets.control_type is ControlType.RADIO, budgets
    assert budgets.label == "What range of monthly budgets are you used to working with?" and budgets.required
    assert [o.label for o in budgets.options or []] == ["Under $50k", "$50k to $250k", "Over $250k"]
    assert double_check.control_type is ControlType.CHECKBOX, double_check
    assert double_check.label.startswith("Please double-check all the information provided above.")
    assert double_check.required and double_check.semantic_type is SemanticType.ATTESTATION
    assert [f.id for f in form.fields] == ["first_name", "last_name", "email", CLIENTS, BUDGETS, DOUBLE_CHECK]


def test_aria_choices_are_clicked_and_read_back_by_aria_checked(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, dict[str, Any]]:
        browser = await _open(options, server.url(ARIA))
        try:
            form = (await browser.inspect()).form
            result = await browser.fill(form, kit.build(form, ARIA_ANSWERS).packet)
            return result, await browser.page.evaluate(ARIA_STATE)
        finally:
            await browser.close()

    result, state = kit.run(scenario())
    assert result.ok, (result.fields, result.page_errors)
    assert all(s is FieldFillStatus.FILLED for s in _statuses(result).values()), result.fields
    assert state["checked"] == ["72", "73", "82", "yes"]
    # The bubble inputs the form posts follow the buttons.
    assert state["bubbles"] == [f"{CLIENTS}=72", f"{CLIENTS}=73", f"{BUDGETS}=82", f"{DOUBLE_CHECK}=yes"]
    assert server.submissions("greenhouse-aria")["accepted_count"] == 0


# --- 2. Paylocity's address line when its menu was never probed ---------------------------

LATE_LIST = "/jobs/paylocity-address/apply?address_list=late"
ADDRESS = "address.address1"


def test_an_address_combobox_whose_list_cannot_be_probed_is_a_text_answer(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> tuple[Any, Any, dict[str, Any]]:
        browser = await _open(options, server.url(LATE_LIST))
        try:
            form = (await browser.inspect()).form
            address = form.field(ADDRESS)
            result = await browser.fill_fields(form, kit.build(form, {ADDRESS: "1234 Fictional Avenue"}).packet, [ADDRESS])
            state = await browser.page.evaluate("""() => ({
              value: document.getElementById('public-site-address-address-1').value,
              picked: window.__addressPicked,
              open: !!document.querySelector('#public-site-address-address-1-autocomplete-list:not([hidden])'),
            })""")
            return address, result, state
        finally:
            await browser.close()

    address, result, state = kit.run(scenario())
    assert address.control_type is ControlType.TEXT, address
    assert address.semantic_type is SemanticType.ADDRESS and address.label == "Address Line 1"
    assert [f.status for f in result.fields] == [FieldFillStatus.FILLED], result.fields
    assert state == {"value": "1234 Fictional Avenue", "picked": None, "open": False}


# --- 1. questions that appear mid-fill, with other controls changing too ------------------

EEO = "/jobs/greenhouse-eeo/apply?reveal_extra=1"
EEO_ANSWERS = {**CONTACT, "question_7001": "Yes", "gender": "Decline To Self Identify",
               "hispanic_ethnicity": "No", "veteran_status": "I am not a protected veteran"}
LATE = "/jobs/teamtailor-late/apply?lazy_extra=1"
LINKEDIN = "candidate[answers_attributes][0][text]"
LATE_ANSWERS = {**CONTACT, "phone": "+1 303 555 0142"}


@pytest.mark.parametrize(("url", "answers", "appeared", "extra"), [
    (EEO, EEO_ANSWERS, "Please identify your race (optional)", {"race": "Decline To Self Identify"}),
    (LATE, LATE_ANSWERS, "Linkedin profile", {LINKEDIN: "https://www.linkedin.example.test/in/avery-quill"}),
], ids=["greenhouse-eeo", "teamtailor-late"])
def test_a_question_that_appears_with_other_controls_is_inspected_again_not_failed(
    url: str, answers: dict[str, Any], appeared: str, extra: dict[str, Any],
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    """The page adds a button that submits nothing together with the question (a
    definitions button, a "Clear" button): the fill stops writing and asks for a fresh
    inspection instead of failing; the next fill answers the new question."""
    async def scenario() -> None:
        browser = await _open(options, server.url(url))
        try:
            form = (await browser.inspect()).form
            first = await browser.fill(form, kit.build(form, answers).packet)
            assert first.failed_field_ids() == [], first.fields
            [error] = first.page_errors
            assert f"1 question(s) appeared ({appeared})" in error, error
            assert "inspect this step and resolve it again" in error and "changed while filling" not in error
            again = (await browser.inspect()).form
            second = await browser.fill(again, kit.build(again, {**answers, **extra}).packet)
            assert second.ok, (second.fields, second.page_errors)
        finally:
            await browser.close()

    kit.run(scenario())


# --- the runner prepares each form to its final review step -------------------------------

def _write_profile(paths: LocalPaths, forms: list[ApplicationForm], answers: dict[str, Any]) -> None:
    """The fictional candidate, with saved answers worded as these forms ask them."""
    directory = paths.profile_dir / "default"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RESUME_PATH, directory / "resume.pdf")
    saved: list[dict[str, Any]] = []
    for form in forms:
        for field in form.fields:
            if field.id not in answers or field.id in CONTACT or field.id in {s["field"] for s in saved}:
                continue
            # A list of labels for several choices, a boolean for a checkbox, else the text.
            semantic = None if field.semantic_type is SemanticType.UNKNOWN else field.semantic_type.value
            saved.append({"field": field.id, "id": f"sa.{len(saved)}", "scope": "GLOBAL", "semantic_type": semantic,
                          "question": field.question_text, "value": answers[field.id], "confirmed_at": VERIFIED_AT})
    profile = {
        "id": "default",
        "identity": {
            **CONTACT, "phone": "+1 (303) 555-0142",
            "linkedin_url": "https://www.linkedin.example.test/in/avery-quill",
            "address": {"street": "1234 Fictional Avenue", "city": "Denver", "region": "CO",
                        "postal_code": "80202", "country": "United States"},
            "verified_at": VERIFIED_AT,
        },
        "resume": {"id": "resume_supplied", "path": "resume.pdf"},
        "facts": [],
        "saved_answers": [{k: v for k, v in s.items() if k != "field"} for s in saved],
    }
    (directory / "profile.json").write_text(json.dumps(profile, indent=2))


class RecordingFactory:
    """The Playwright factory, recording the page as each browser closes."""

    def __init__(self, script: str) -> None:
        self.script = script
        self.states: list[Any] = []

    async def start(self, options: BrowserOptions) -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        close = browser.close

        async def close_and_record() -> None:
            with contextlib.suppress(Exception):
                self.states.append(await browser.page.evaluate(self.script))
            await close()

        browser.close = close_and_record
        return browser


async def _forms(options: BrowserOptions, url: str, reveal: str | None = None) -> list[ApplicationForm]:
    browser = await _open(options, url)
    try:
        forms = [(await browser.inspect()).form]
        if reveal:
            await browser.page.evaluate(reveal)
            await browser.page.wait_for_timeout(300)
            forms.append((await browser.inspect()).form)
        return forms
    finally:
        await browser.close()


def _prepare(kit: SimpleNamespace, paths: LocalPaths, url: str, script: str) -> tuple[list[str], Any]:
    factory = RecordingFactory(script)
    runner = LocalApplicationRunner(paths=paths, interaction=NoninteractiveInteraction(),
                                    headless=True, browser_factory=factory, prepare_only=True)
    result = kit.run(runner.apply(url, candidate_id="default"))
    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    assert "Prepared to the final review step" in result.message, result.message
    assert result.missing_inputs == []
    with ApplicationStore.open(paths.state_db) as store:
        events = store.list_events(result.application_id)
        assert store.list_attempts(result.application_id) == []
    failures = [e for e in events if e.event.startswith("application.") and "FAILED" in json.dumps(e.metadata)]
    assert failures == [], failures
    assert [e.metadata["submitted"] for e in events if e.event == "preparation.ready"] == [False]
    return [e.event for e in events], factory.states[-1]


def test_the_runner_prepares_the_form_whose_choices_are_aria_widgets(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, isolated_imx_home: LocalPaths
) -> None:
    url = server.url(ARIA)
    _write_profile(isolated_imx_home, kit.run(_forms(options, url)), ARIA_ANSWERS)
    _, state = _prepare(kit, isolated_imx_home, url, ARIA_STATE)
    assert state["checked"] == ["72", "73", "82", "yes"]
    assert server.submissions("greenhouse-aria")["accepted_count"] == 0


REVEAL_RACE = """() => { const s = document.querySelector('select[name="hispanic_ethnicity"]');
  s.value = 'No'; s.dispatchEvent(new Event('change', {bubbles: true})); }"""
EEO_STATE = """() => Object.fromEntries(['hispanic_ethnicity', 'race', 'veteran_status']
  .map((name) => [name, (document.querySelector('select[name="' + name + '"]') || {}).value ?? null]))"""
SCROLL_TO_FORM = "() => document.querySelector('form').scrollIntoView()"
LATE_STATE = "() => (document.querySelector('[name=\"candidate[answers_attributes][0][text]\"]') || {}).value ?? null"


@pytest.mark.parametrize(("url", "answers", "reveal", "script", "expected"), [
    (EEO, {**EEO_ANSWERS, "race": "Decline To Self Identify"}, REVEAL_RACE, EEO_STATE,
     {"hispanic_ethnicity": "No", "race": "7", "veteran_status": "1"}),
    (LATE, {**LATE_ANSWERS, LINKEDIN: "https://www.linkedin.example.test/in/avery-quill"}, SCROLL_TO_FORM,
     LATE_STATE, "https://www.linkedin.example.test/in/avery-quill"),
], ids=["greenhouse-eeo", "teamtailor-late"])
def test_the_runner_answers_a_question_that_appeared_in_the_same_run(
    url: str, answers: dict[str, Any], reveal: str, script: str, expected: Any,
    kit: SimpleNamespace, server: Any, options: BrowserOptions, isolated_imx_home: LocalPaths
) -> None:
    _write_profile(isolated_imx_home, kit.run(_forms(options, server.url(url), reveal)), answers)
    names, state = _prepare(kit, isolated_imx_home, server.url(url), script)
    assert names.count("packet.saved") == 2, names  # resolved again once the question appeared
    assert state == expected


def test_the_runner_prepares_paylocity_when_its_address_menu_was_never_probed(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, isolated_imx_home: LocalPaths
) -> None:
    url = server.url(LATE_LIST)
    sms = "We may use SMS during the hiring process. Do you give us permission to text you?"
    forms = kit.run(_forms(options, url))
    answers = {f.id: "Yes" for f in forms[0].fields if f.label == sms}
    answers |= {f.id: "No" for f in forms[0].fields if f.label == "Have you worked with us before?"}
    _write_profile(isolated_imx_home, forms, answers)
    # The run stops on the review page, which lists what the site saved.
    _, state = _prepare(kit, isolated_imx_home, url, """() => Object.fromEntries(Array.from(
      document.querySelectorAll('dl.review dt'), (dt) => [dt.textContent, dt.nextElementSibling.textContent]))""")
    assert state["Address Line 1"] == "1234 Fictional Avenue", state


# --- 2. Paylocity's work-history dates from the profile's most recent role ----------------

WORK = "/jobs/paylocity-work-history/apply"
START, END, BOX = ("txt-workHistory-startDate-0", "txt-workHistory-endDate-0",
                   "workHistory.currentlyWorkingHere.0")
WORK_STATE = """() => ({
  company: document.querySelector('[name="workHistory.companyName.0"]').value,
  position: document.querySelector('[name="workHistory.position.0"]').value,
  start: document.querySelector('[name="txt-workHistory-startDate-0"]').value,
  end: document.querySelector('[name="txt-workHistory-endDate-0"]').value,
  endDisabled: document.querySelector('[name="txt-workHistory-endDate-0"]').disabled,
  current: document.querySelector('[name="workHistory.currentlyWorkingHere.0"]').checked,
})"""


def _verified(fact_id: str, key: str, value: str) -> dict[str, Any]:
    return {"id": fact_id, "key": key, "value": value, "source": "resume",
            "verification": {"status": "VERIFIED", "method": "USER_CONFIRMED", "verified_at": VERIFIED_AT}}


def _write_work_profile(paths: LocalPaths, *, current: bool) -> None:
    """The fictional candidate with two resume roles; the most recent is current or ended."""
    directory = paths.profile_dir / "default"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RESUME_PATH, directory / "resume.pdf")
    (directory / "profile.json").write_text(json.dumps(_work_profile(current=current), indent=2))


def _work_profile(*, current: bool) -> dict[str, Any]:
    latest = {"id": "exp.latest", "company": "Brambleway Media", "title": "Paid Media Lead", "start": "2021-03",
              "current": current, "fact_ids": ["fact.latest"]}
    if not current:
        latest["end"] = "2025-08"
    profile = {
        "id": "default",
        "identity": {**CONTACT, "phone": "+1 (303) 555-0142", "verified_at": VERIFIED_AT},
        "resume": {"id": "resume_supplied", "path": "resume.pdf"},
        "facts": [_verified("fact.company", "current_company", "Brambleway Media"),
                  _verified("fact.title", "current_title", "Paid Media Lead"),
                  _verified("fact.latest", "employment", "Paid Media Lead, Brambleway Media (2021-03 to present)"),
                  _verified("fact.earlier", "employment", "Search Specialist, Fictional Search Agency (2018-01 to 2021-02)")],
        "experience": [
            {"id": "exp.earlier", "company": "Fictional Search Agency", "title": "Search Specialist",
             "start": "2018-01", "end": "2021-02", "fact_ids": ["fact.earlier"]},
            latest,
        ],
        "saved_answers": [],
    }
    return profile


@pytest.mark.parametrize(("current", "expected"), [
    (True, {"start": "03/2021", "end": "", "endDisabled": True, "current": True}),
    (False, {"start": "03/2021", "end": "08/2025", "endDisabled": False, "current": False}),
], ids=["current-role", "ended-role"])
def test_the_runner_writes_the_most_recent_roles_dates(
    current: bool, expected: dict[str, Any],
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    """Start Date "MM/YYYY" from the most recent role; the end date left blank (its box
    checked instead) while the role is current, else its own MM/YYYY."""
    _write_work_profile(isolated_imx_home, current=current)
    _, state = _prepare(kit, isolated_imx_home, server.url(WORK), WORK_STATE)
    assert state == {"company": "Brambleway Media", "position": "Paid Media Lead", **expected}, state


# --- 2. live route decisions: Jev reads a work-history entry as the applicant's past ---------

class HistoryJev:
    """Routes every field COPY_KNOWN as literal text, with the source scope live Paylocity
    got: the applicant's past (HISTORICAL_OR_CONTEXTUAL) for the entry's questions, split
    0.84 / 0.14 with the applicant's present at confidence 0.80 for "I currently work
    here", ``other`` for questions named in it, the present for the rest."""

    def __init__(self, other: tuple[str, ...] = ()) -> None:
        self.other = other

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        fields = request["state"].get("fields", {})
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            criteria = list(question["criteria"])
            wording = fields.get(f"f{name[1:]}", {}).get("question", "")
            confidence, probabilities = 1.0, {}
            if name[0] == "u" and name[1:].isdigit():
                if any(label in wording for label in self.other):
                    probabilities = {"OTHER_PERSON_OR_ENTITY": 1.0}
                elif "currently work here" in wording:
                    confidence = 0.80
                    probabilities = {"HISTORICAL_OR_CONTEXTUAL": 0.84, "APPLICANT_CURRENT": 0.14,
                                     "EXPLICIT_ANSWER": 0.02}
                elif wording.startswith(("Company Name", "Position", "Start Date", "End Date")):
                    probabilities = {"HISTORICAL_OR_CONTEXTUAL": 1.0}
                else:
                    probabilities = {"APPLICANT_CURRENT": 1.0}
            else:
                choice = {"r": "COPY_KNOWN", "n": "literal", "s": "CUSTOM_TEXT",
                          "d": "APPLICATION_ATTACHMENT"}.get(name[0], "NONE")
                probabilities = {choice: 1.0}
            probabilities = {key: probabilities.get(key, 0.0) for key in criteria}
            choice = max(probabilities, key=probabilities.__getitem__)
            answers[name] = {"type": "choice", "choice": choice, "confidence": confidence,
                             "probabilities": probabilities}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
                                                 "answers": answers, "usage": {"cost": 0.0001}}).encode())


def _with_roles(candidate: CandidateProfile, *, current: bool) -> CandidateProfile:
    """The fixture candidate with the work profile's facts and roles."""
    work = _work_profile(current=current)
    return CandidateProfile.model_validate(
        candidate.model_dump(mode="json") | {"facts": work["facts"], "experience": work["experience"]})


def _routed(form: ApplicationForm, candidate: CandidateProfile, job: JobRecord,
            jev: HistoryJev) -> tuple[Any, DynamicPacketResolver]:
    decisions = BoundedDecisions(JevClient(ApiKey("synthetic-key", source="test"), transport=jev,
                                           max_attempts=1), CallBudget())
    resolver = DynamicPacketResolver(decisions, router=AIFormRouter(decisions))
    app = Application(id="app-history-test", request_id="request-history-test", job_id=job.id,
                      candidate_id=candidate.id, state=ApplicationState.INSPECTING, version=2,
                      created_at="2026-09-25T00:00:00Z", updated_at="2026-09-25T00:00:00Z")
    context = PacketContext(application=app, job=job, form=form, candidate=candidate)
    packet = asyncio.run(resolver.resolve(context))
    assert context.problems(packet) == []
    return packet, resolver


@pytest.mark.parametrize("current", [True, False], ids=["current-role", "ended-role"])
def test_the_route_gate_admits_the_roles_dates_as_the_applicants_past(
    current: bool, kit: SimpleNamespace, server: Any, options: BrowserOptions,
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    (form,) = kit.run(_forms(options, server.url(WORK)))
    candidate = _with_roles(fictional_candidate, current=current)
    packet, resolver = _routed(form, candidate, mock_job, HistoryJev())
    answers = {a.field_id: a for a in packet.answers}
    assert answers[START].value == TextValue(text="03/2021")
    assert answers[BOX].value == BooleanValue(checked=current)
    for field_id in (START, BOX):
        assert answers[field_id].provenance.source is AnswerSource.CANDIDATE_FACT
        assert answers[field_id].provenance.reference_ids == ["fact.latest"]
    approved = {t["field_id"] for t in resolver.narrative_traces if t["stage"] == "work_history"}
    if current:
        # The end date stays blank, not routed or asked: the box is checked instead.
        assert END not in answers and approved == {START, BOX}
        (blank,) = [m for m in packet.missing_inputs if m.field_id == END]
        assert blank.required is False and "currently work here" in blank.prompt
        assert not [t for t in resolver.narrative_traces if t.get("field_id") == END]
    else:
        assert answers[END].value == TextValue(text="08/2025") and approved == {START, END, BOX}
    assert not [m for m in packet.missing_inputs if m.field_id in (START, BOX) or (m.field_id == END and m.required)]


def test_the_route_gate_never_admits_another_persons_dates(
    kit: SimpleNamespace, server: Any, options: BrowserOptions,
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    (form,) = kit.run(_forms(options, server.url(WORK)))
    candidate = _with_roles(fictional_candidate, current=False)
    packet, resolver = _routed(form, candidate, mock_job, HistoryJev(other=("Start Date",)))
    assert START not in {a.field_id for a in packet.answers}
    (held,) = [m for m in packet.missing_inputs if m.field_id == START]
    assert held.required and held.reason is MissingReason.NO_ANSWER
    assert {t["field_id"] for t in resolver.narrative_traces if t["stage"] == "work_history"} == {END, BOX}
