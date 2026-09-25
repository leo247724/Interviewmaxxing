"""Replicated application flows end to end: prepare-only runs of the real runner over the
real store and the localhost mock's LinkedIn-style Easy Apply dialog, JazzHR-style anchor
form and Dayforce-style posting inside a job-alert form.

The fictional candidate (Avery Quill) has saved answers for the questions her profile
cannot answer, keyed by their exact wording. Her pinned resume file is named like one of
the resumes the Easy Apply mock keeps ("resume_avery_quill.pdf"). Every run stops at the
final review step or at an action for the person, and submits nothing. Only fixtures and
assertions use the mock's test API.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_cli.runner import (
    LocalApplicationRunner,
    NoninteractiveInteraction,
    RunLimits,
    pending_inputs,
)
from interviewmaxxing_core import (
    ApplicationForm,
    ApplicationState,
    ApplicationStore,
    ApplyOutcome,
    BrowserOptions,
    ChoiceValue,
    FileValue,
    JobRecord,
    LocalPaths,
    MissingReason,
    SemanticType,
    TextValue,
)

REPO = Path(__file__).resolve().parents[2]
RESUME_PATH = REPO / "tests" / "fixtures" / "browser" / "resume_avery_quill.pdf"
PINNED = "resume_avery_quill.pdf"
"""The pinned resume's file name: the Easy Apply mock's matching saved resume card."""
LINKEDIN = "https://www.linkedin.example.test/in/avery-quill"
VERIFIED_AT = "2026-09-01T12:00:00Z"
PREPARED = "Prepared to the final review step"
ATTACH = "Attach your resume in the browser window"
UPLOAD_INPUT = "#jobs-document-upload-file-input-upload-resume"
JAZZHR = "/jobs/stepper-ambiguous/apply"
DAYFORCE = "/jobs/apply-in-alert-form"
DAYFORCE_FORM = "/jobs/apply-in-alert-form/apply/manual"

WIZARD_ANSWERS: list[tuple[str, SemanticType | None, Any]] = [
    ("Phone country code", None, "United States (+1)"),
    ("How many years of work experience do you have with SQL?", None, "6"),
    ("Are you legally authorized to work in the United States?", None, "Yes"),
    ("Will you now or in the future require sponsorship for employment visa status?", None, "No"),
]
"""The Easy Apply questions the profile cannot answer, worded exactly as the dialog asks."""
WIZARD_PACKETS = {
    0: sorted([("avery.quill@example.test", "PROFILE_IDENTITY"), ("United States (+1)", "SAVED_ANSWER"),
               ("+1 (303) 555-0142", "PROFILE_IDENTITY"), ("Denver", "PROFILE_IDENTITY")]),
    33: [(PINNED, "RESUME")],
    67: sorted([("6", "SAVED_ANSWER"), ("Yes", "SAVED_ANSWER"), ("No", "SAVED_ANSWER")]),
    100: [],
}
"""One packet per step, each step identified by the dialog's progress bar (0, 33, 67, 100):
what answers it and from which source."""

EASY_APPLY = """() => {
  const host = document.getElementById('interop-outlet');
  const root = host && host.shadowRoot ? host.shadowRoot : document;
  const value = (sel) => { const el = document.querySelector(sel); return el ? el.value : null; };
  return {
    easy: JSON.parse(JSON.stringify(window.__easyApply || null)),
    cards: Array.from(root.querySelectorAll('.ui-attachment')).map((c) =>
      [c.querySelector('h3').textContent.trim(), c.querySelector('input[type=radio]').checked]),
    decoys: [value('input[type=search]'), value('textarea[aria-label="Write a message to the hiring team"]'),
             value('input[aria-label="Add a note about this job"]'),
             ...Array.from(document.querySelectorAll('.filter-pill')).map((p) => p.getAttribute('aria-pressed'))],
  };
}"""
"""The Easy Apply page state (``window.__easyApply``, the saved resume cards, the page-behind
decoys: search box, message box, note field and the two filter pills)."""

FORM_STATE = """(names) => {
  const form = document.getElementById('form_submit_new_resume') || document.querySelector('form');
  const shown = (id) => { const el = document.getElementById(id);
    return !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length); };
  return {
    path: location.pathname,
    values: Object.fromEntries(names.map((name) => [name, form.elements[name].value])),
    resume: Array.from(form.elements.resume.files).map((f) => f.name),
    saved: sessionStorage.getItem('resumator-saved-application'),
    sections: [shown('resumator-section-1'), shown('resumator-section-2')],
  };
}"""
"""A single-page form's state: its values by name, the attached resume, JazzHR's Save
storage and which of JazzHR's sections is shown."""


# --- helpers --------------------------------------------------------------------------


async def _inspect_form(options: BrowserOptions, url: str) -> ApplicationForm:
    browser = await PlaywrightSessionFactory(settle_timeout_s=10.0).start(options)
    try:
        page = await browser.open(url)
        assert page.form is not None, page.message
        return page.form
    finally:
        await browser.close()


def _worded(form: ApplicationForm, values: Mapping[str, Any]) -> list[tuple[str, SemanticType | None, Any]]:
    """Saved answers for these fields of ``form``, keyed by each question's exact wording."""
    answers: list[tuple[str, SemanticType | None, Any]] = []
    for field_id, value in values.items():
        field = form.field(field_id)
        answers.append((field.question_text,
                        None if field.semantic_type is SemanticType.UNKNOWN else field.semantic_type, value))
    return answers


def _write_profile(paths: LocalPaths, answers: Sequence[tuple[str, SemanticType | None, Any]]) -> None:
    """The fictional candidate with GLOBAL saved answers; the resume is copied into the
    profile under the name of the mock's matching resume card."""
    directory = paths.profile_dir / "default"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RESUME_PATH, directory / PINNED)
    profile = {
        "id": "default",
        "identity": {
            "first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
            "phone": "+1 (303) 555-0142", "linkedin_url": LINKEDIN,
            "address": {"city": "Denver", "region": "CO", "country": "United States"},
            "verified_at": VERIFIED_AT,
        },
        "resume": {"id": "resume_supplied", "path": PINNED},
        "facts": [],
        "saved_answers": [
            {"id": f"sa.{index}", "scope": "GLOBAL", "semantic_type": semantic.value if semantic else None,
             "question": question, "value": value, "confirmed_at": VERIFIED_AT}
            for index, (question, semantic, value) in enumerate(answers)
        ],
    }
    (directory / "profile.json").write_text(json.dumps(profile, indent=2))


class RecordingFactory:
    """The Playwright factory these pages need (a 10 s settle timeout covers Dayforce's 2.5 s
    client routes), recording the page as the run closes it. ``attaches_files=False`` starts
    sessions that cannot attach files, like OpenCLI's Browser Bridge. With ``person`` the
    (fictional) person attaches the resume in the browser window whenever the run waits."""

    def __init__(self, page_state: str, arg: Any = None, *, attaches_files: bool = True,
                 person: bool = False) -> None:
        self.page_state, self.arg = page_state, arg
        self.attaches_files, self.person = attaches_files, person
        self.pages: list[dict[str, Any]] = []
        self.waits: list[str] = []

    @property
    def last(self) -> dict[str, Any]:
        """The page the run closed on: its recorded state and pending user controls."""
        assert self.pages and "error" not in self.pages[-1], self.pages
        return self.pages[-1]

    async def start(self, options: BrowserOptions) -> Any:
        browser = await PlaywrightSessionFactory(settle_timeout_s=10.0).start(options)
        if not self.attaches_files:
            browser.driver.attaches_files = False
        close, wait = browser.close, browser.wait_for_user

        async def close_and_record() -> None:
            try:
                last = browser.last_page
                self.pages.append({
                    "state": await browser.page.evaluate(self.page_state, self.arg),
                    "pending": list(last.unsupported_pending) if last else None,
                })
            except Exception as exc:  # reported by the assertions on the recorded page
                self.pages.append({"error": repr(exc)})
            await close()

        async def wait_for_person(reason: str, timeout_s: float | None = None) -> Any:
            self.waits.append(reason)
            if self.person:
                await browser.page.set_input_files(UPLOAD_INPUT, str(RESUME_PATH))
            return await wait(reason, timeout_s)

        browser.close = close_and_record
        browser.wait_for_user = wait_for_person
        return browser


def _apply(kit: SimpleNamespace, paths: LocalPaths, factory: RecordingFactory, url: str, *,
           person: bool = False) -> ApplyOutcome:
    runner = LocalApplicationRunner(
        paths=paths, interaction=NoninteractiveInteraction(allow_browser_action=person), headless=True,
        browser_factory=factory, prepare_only=True, limits=RunLimits(user_action_timeout_s=10.0))
    outcome: ApplyOutcome = kit.run(runner.apply(url, candidate_id="default"))
    return outcome


def _shown(value: Any) -> str:
    if isinstance(value, TextValue):
        return value.text
    if isinstance(value, ChoiceValue):
        return value.label
    if isinstance(value, FileValue):
        return value.artifact.filename
    return repr(value)


@dataclass(frozen=True)
class Stored:
    state: ApplicationState
    ready: list[dict[str, Any]]
    """Metadata of every ``preparation.ready`` event."""
    packets: dict[int, list[tuple[str, str]]]
    """Form step -> (answer as shown, its source) of the last packet saved for that step."""
    attempts: int
    job: JobRecord
    pending: list[str]
    """Ids of the recorded missing inputs (what a later status or resume shows)."""


def _stored(paths: LocalPaths, application_id: str) -> Stored:
    with ApplicationStore.open(paths.state_db) as store:
        events = store.list_events(application_id)
        packets: dict[int, list[tuple[str, str]]] = {}
        for event in events:
            if event.event == "packet.saved":
                packet = store.get_packet(event.metadata["packet_id"])
                packets[packet.form_step] = sorted(
                    (_shown(a.value), a.provenance.source.value) for a in packet.answers)
        app = store.get_application(application_id)
        return Stored(
            state=app.state,
            ready=[e.metadata for e in events if e.event == "preparation.ready"],
            packets=packets,
            attempts=len(store.list_attempts(application_id)),
            job=store.get_job(app.job_id),
            pending=[m.id for m in pending_inputs(store, application_id)],
        )


def _assert_prepared(result: ApplyOutcome, stored: Stored, url: str, step: int) -> None:
    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    assert PREPARED in result.message, result.message
    assert result.missing_inputs == [] and stored.pending == []
    assert stored.state is ApplicationState.NEEDS_INPUT
    [ready] = stored.ready
    assert (ready["form_url"], ready["form_step"], ready["submitted"], ready["captcha_pending"]) == (
        url, step, False, False)
    assert stored.attempts == 0  # no submission was ever begun


# --- modal-wizard: LinkedIn-style Easy Apply ------------------------------------------------


@pytest.mark.parametrize(("variant", "form_path"), [
    pytest.param("", "/jobs/modal-wizard/apply?openSDUIApplyFlow=true", id="apply-link"),
    pytest.param("?trigger=button", "/jobs/modal-wizard?trigger=button", id="apply-button"),
    pytest.param("?shadow=1", "/jobs/modal-wizard/apply?openSDUIApplyFlow=true&shadow=1", id="shadow-root"),
])
def test_easy_apply_run_walks_the_dialog_to_its_review_step(
    variant: str, form_path: str, kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    _write_profile(isolated_imx_home, WIZARD_ANSWERS)
    factory = RecordingFactory(EASY_APPLY)
    result = _apply(kit, isolated_imx_home, factory, server.url("/jobs/modal-wizard" + variant))
    stored = _stored(isolated_imx_home, result.application_id)
    _assert_prepared(result, stored, server.url(form_path), 100)
    assert stored.packets == WIZARD_PACKETS
    assert list(stored.packets) == [0, 33, 67, 100]
    # The identity is the posting's, read behind the dialog.
    assert (stored.job.external_job_id, stored.job.title) == ("BWA-LI-130", "Growth Marketing Lead")
    page = factory.last
    easy = page["state"]["easy"]
    assert (easy["open"], easy["opens"], easy["step"], easy["submitted"]) == (True, 1, 4, False), page
    assert {key: easy["answers"][key] for key in (
        "email", "phone_country", "phone", "city", "resume", "resume_uploaded", "sql_years",
        "work_authorization", "sponsorship")} == {
        "email": "avery.quill@example.test", "phone_country": "United States (+1)", "phone": "3035550142",
        "city": "Denver", "resume": PINNED, "resume_uploaded": False, "sql_years": "6",
        "work_authorization": "Yes", "sponsorship": "No"}
    # Pre-filled contact answers that already said the candidate's values were never written;
    # the matching saved resume was chosen, not uploaded again.
    assert [easy["writes"][key] for key in ("email", "phone_country", "phone")] == [0, 0, 0]
    assert easy["writes"]["city"] > 0 and easy["writes"]["resume"] > 0
    assert page["state"]["decoys"] == ["", "", "", "false", "false"]  # the page behind is untouched
    summary = server.submissions("modal-wizard")
    assert (summary["accepted_count"], summary["rejected_count"]) == (0, 0)


@pytest.mark.parametrize(("resumes", "wording", "cards", "selected"), [
    pytest.param("nomatch", f"{ATTACH} ({PINNED}).",
                 [["Avery_Quill_Resume_2025.pdf", True], ["AQ_CV_marketing.docx", False]],
                 "Avery_Quill_Resume_2025.pdf", id="no-usable-saved-resume"),
    pytest.param("none", f"{ATTACH}.", [], None, id="no-saved-resume"),
])
def test_a_session_that_cannot_attach_files_hands_the_resume_to_the_person(
    resumes: str, wording: str, cards: list[list[Any]], selected: str | None,
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths,
) -> None:
    _write_profile(isolated_imx_home, WIZARD_ANSWERS)
    factory = RecordingFactory(EASY_APPLY, attaches_files=False)
    result = _apply(kit, isolated_imx_home, factory, server.url(f"/jobs/modal-wizard?resumes={resumes}"))
    assert result.state is ApplicationState.NEEDS_INPUT, result.message
    [needed] = result.missing_inputs
    assert (needed.field_id, needed.reason, needed.required) == ("file", MissingReason.UNSUPPORTED_CONTROL, True)
    assert wording in needed.prompt, needed.prompt
    assert ATTACH in result.message and PREPARED not in result.message
    stored = _stored(isolated_imx_home, result.application_id)
    assert stored.state is ApplicationState.NEEDS_INPUT
    assert stored.ready == [] and stored.attempts == 0
    assert stored.pending == [needed.id]  # recorded for the person, as the run reported it
    assert stored.packets[33] == []  # nothing on the resume step is answered for the person
    page = factory.last
    assert page["pending"] == ["file"], page
    easy = page["state"]["easy"]
    # Still on the resume step, the preselected resume untouched: nothing was chosen for her.
    assert (easy["step"], easy["submitted"], easy["writes"]["resume"]) == (2, False, 0), page
    assert (easy["answers"]["resume"], easy["answers"]["resume_uploaded"]) == (selected, False)
    assert page["state"]["cards"] == cards
    summary = server.submissions("modal-wizard")
    assert (summary["accepted_count"], summary["rejected_count"]) == (0, 0)


def test_the_run_continues_once_the_person_attaches_the_resume(
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    """The person said they will act in the browser window: the run waits for the resume,
    then walks on to the review step with the file she attached."""
    _write_profile(isolated_imx_home, WIZARD_ANSWERS)
    factory = RecordingFactory(EASY_APPLY, attaches_files=False, person=True)
    result = _apply(kit, isolated_imx_home, factory, server.url("/jobs/modal-wizard?resumes=nomatch"),
                    person=True)
    [asked] = factory.waits
    assert f"{ATTACH} ({PINNED})." in asked, asked
    stored = _stored(isolated_imx_home, result.application_id)
    _assert_prepared(result, stored, server.url("/jobs/modal-wizard/apply?openSDUIApplyFlow=true&resumes=nomatch"),
                     100)
    assert stored.packets[33] == [(PINNED, "RESUME")]  # her upload is the pinned file's card
    easy = factory.last["state"]["easy"]
    assert (easy["step"], easy["submitted"], easy["answers"]["resume"], easy["answers"]["resume_uploaded"]) == (
        4, False, PINNED, True)
    assert server.submissions("modal-wizard")["accepted_count"] == 0


def test_easy_apply_without_saved_resumes_uploads_the_pinned_file(
    kit: SimpleNamespace, server: Any, isolated_imx_home: LocalPaths
) -> None:
    _write_profile(isolated_imx_home, WIZARD_ANSWERS)
    factory = RecordingFactory(EASY_APPLY)
    result = _apply(kit, isolated_imx_home, factory, server.url("/jobs/modal-wizard?resumes=none"))
    stored = _stored(isolated_imx_home, result.application_id)
    _assert_prepared(result, stored, server.url("/jobs/modal-wizard/apply?openSDUIApplyFlow=true&resumes=none"),
                     100)
    assert stored.packets[33] == [(PINNED, "RESUME")]
    easy = factory.last["state"]["easy"]
    assert (easy["answers"]["resume"], easy["answers"]["resume_uploaded"]) == (PINNED, True)
    assert server.submissions("modal-wizard")["accepted_count"] == 0


# --- stepper-ambiguous: JazzHR-style anchor actions ------------------------------------------


@pytest.mark.parametrize(("variant", "final_step", "sections"), [
    pytest.param("", 0, [False, False], id="one-page"),
    pytest.param("?sections=2", 1, [False, True], id="two-sections"),
])
def test_jazzhr_anchor_form_is_prepared_to_its_submit_anchor(
    variant: str, final_step: int, sections: list[bool], kit: SimpleNamespace, server: Any,
    options: BrowserOptions, isolated_imx_home: LocalPaths,
) -> None:
    form = kit.run(_inspect_form(options, server.url(JAZZHR)))
    assert [f.id for f in form.fields if f.required] == [
        "first_name", "last_name", "email", "phone", "heard_about", "resume"]
    _write_profile(isolated_imx_home, _worded(form, {"heard_about": "LinkedIn"}))
    names = ["first_name", "last_name", "email", "phone", "desired_salary", "heard_about"]
    factory = RecordingFactory(FORM_STATE, names)
    result = _apply(kit, isolated_imx_home, factory, server.url(JAZZHR + variant))
    stored = _stored(isolated_imx_home, result.application_id)
    _assert_prepared(result, stored, server.url(JAZZHR + variant), final_step)
    assert stored.job.external_job_id == "BWA-JZ-132"
    assert factory.last["state"] == {
        "path": JAZZHR,
        "values": {"first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
                   "phone": "+1 (303) 555-0142", "desired_salary": "", "heard_about": "hear_linkedin"},
        "resume": [PINNED],
        "saved": None,  # "Save" is never a step action
        "sections": sections,
    }
    summary = server.submissions("stepper-ambiguous")
    assert (summary["accepted_count"], summary["rejected_count"]) == (0, 0)


# --- apply-in-alert-form: a Dayforce-style posting inside a job-alert form --------------------


@pytest.mark.parametrize("variant", [
    pytest.param("", id="posting-in-alert-form"),
    pytest.param("?nav=spa", id="client-side-routes"),
])
def test_dayforce_posting_is_prepared_without_a_job_alert(
    variant: str, kit: SimpleNamespace, server: Any, options: BrowserOptions, isolated_imx_home: LocalPaths
) -> None:
    form = kit.run(_inspect_form(options, server.url(DAYFORCE_FORM)))
    _write_profile(isolated_imx_home, _worded(form, {
        "work_authorization": "Yes, I am authorized to work in the US",
        "sponsorship": "No, I will not require sponsorship",
    }))
    names = ["first_name", "last_name", "email", "phone", "linkedin_url", "work_authorization", "sponsorship"]
    factory = RecordingFactory(FORM_STATE, names)
    result = _apply(kit, isolated_imx_home, factory, server.url(DAYFORCE + variant))
    stored = _stored(isolated_imx_home, result.application_id)
    _assert_prepared(result, stored, server.url(DAYFORCE_FORM), 0)
    assert stored.job.external_job_id == "BWA-DF-133"
    state = factory.last["state"]
    assert (state["path"], state["resume"]) == (DAYFORCE_FORM, [PINNED])
    assert state["values"] == {
        "first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
        "phone": "+1 (303) 555-0142", "linkedin_url": LINKEDIN,
        "work_authorization": "wa_authorized", "sponsorship": "no_sponsorship"}
    summary = server.submissions("apply-in-alert-form")
    assert (summary["accepted_count"], summary["rejected_count"], summary["alert_count"]) == (0, 0, 0)
