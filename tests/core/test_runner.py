"""LocalApplicationRunner control flow with a scripted browser.

The real store, the real factual resolver (C3) and a fictional candidate are used;
only the browser is scripted, to force cases a live page cannot produce on demand:
cancellation mid-submit, loops, ambiguous controls, lock and claim contention,
threads. Real-Chromium coverage of the same runner is in ``e2e/``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from interviewmaxxing_browser import AmbiguousAction
from interviewmaxxing_cli.runner import (
    LocalApplicationRunner,
    NoninteractiveInteraction,
    RunLimits,
    browser_profile_lock,
    pending_inputs,
)
from interviewmaxxing_core import (
    AnswerReuse,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    BrowserOptions,
    CandidateProfile,
    ControlType,
    FieldFillResult,
    FieldFillStatus,
    FillResult,
    IdentityEvidenceKind,
    JobIdentityObservation,
    MissingInput,
    NavigationResult,
    PageInspection,
    PageKind,
    SavedAnswer,
    SemanticType,
    SubmissionObservation,
    SubmissionOutcome,
    SubmitActionResult,
    TextValue,
    UserInput,
)

S = ApplicationState
URL = "http://127.0.0.1:9/jobs/fictional/apply"


def _form(step: int = 0, *, final: bool | None = True, extra_label: str | None = None,
          url: str = URL) -> ApplicationForm:
    fields = [ApplicationField(id="first_name", label="First name", selector="#first_name",
                               semantic_type=SemanticType.FIRST_NAME,
                               control_type=ControlType.TEXT, required=True)]
    if extra_label:
        fields.append(ApplicationField(id=f"q{step}", label=extra_label, selector=f"#q{step}",
                                       semantic_type=SemanticType.CUSTOM_TEXT,
                                       control_type=ControlType.TEXT, required=True))
    return ApplicationForm(url=url, step=step, fields=fields, is_final_step=final,
                           submit_selector="#submit" if final else None,
                           next_selector=None if final else "#next")


IDENTITY = JobIdentityObservation(ats_type="mock", ats_tenant="fictional", external_job_id="F-1",
                                  evidence_kind=IdentityEvidenceKind.ATS_JOB_ID_ON_PAGE,
                                  evidence="Job ID F-1 on page", title="Fictional Analyst")


def _page(form: ApplicationForm | None = None, kind: PageKind = PageKind.APPLICATION_FORM,
          identity: JobIdentityObservation | None = IDENTITY) -> PageInspection:
    return PageInspection(kind=kind, observed_url=URL, form=form, job_identity=identity,
                          message="Sign in to continue" if kind is PageKind.SIGN_IN_REQUIRED else None)


ACCEPTED = SubmissionObservation(outcome=SubmissionOutcome.ACCEPTED,
                                 signals=["heading 'Application submitted'", "job id 'F-1' shown"],
                                 confirmation_reference="F-REF-1")


@dataclass
class Script:
    pages: list[PageInspection] = field(default_factory=lambda: [_page(_form())])
    advance: list[NavigationResult | Exception] = field(default_factory=list)
    submit: SubmitActionResult | BaseException = field(
        default_factory=lambda: SubmitActionResult(dispatched=True))
    confirm: SubmissionObservation | BaseException = field(default_factory=lambda: ACCEPTED)
    after_user: PageInspection | None = None
    reconcile: SubmissionObservation | None = None
    calls: list[str] = field(default_factory=list)
    starts: int = 0


class ScriptedBrowser:
    def __init__(self, script: Script) -> None:
        self.s = script
        self.current = script.pages[0]

    async def open(self, url: str) -> PageInspection:
        self.s.calls.append("open")
        self.current = self.s.pages[0]
        return self.current

    async def inspect(self) -> PageInspection:
        self.s.calls.append("inspect")
        return self.current

    async def fill(self, form: ApplicationForm, packet: ApplicationPacket) -> FillResult:
        self.s.calls.append("fill")
        assert packet.problems_against(form) == []
        return FillResult(form_step=form.step, fields=[
            FieldFillResult(field_id=a.field_id, status=FieldFillStatus.FILLED) for a in packet.answers])

    async def advance(self) -> NavigationResult:
        self.s.calls.append("advance")
        step = self.s.advance.pop(0)
        if isinstance(step, Exception):
            raise step
        self.current = step.inspection
        return step

    async def submit(self) -> SubmitActionResult:
        self.s.calls.append("submit")
        if isinstance(self.s.submit, BaseException):
            raise self.s.submit
        return self.s.submit

    async def confirm(self) -> SubmissionObservation:
        self.s.calls.append("confirm")
        if isinstance(self.s.confirm, BaseException):
            raise self.s.confirm
        return self.s.confirm

    async def wait_for_user(self, reason: str, timeout_s: float | None = None) -> PageInspection:
        self.s.calls.append("wait_for_user")
        assert self.s.after_user is not None
        self.current = self.s.after_user
        return self.current

    async def reconcile(self, url: str, *, tie: object, lookup_email: str | None = None,
                        max_hops: int = 3) -> SubmissionObservation:
        self.s.calls.append("reconcile")
        assert self.s.reconcile is not None
        return self.s.reconcile

    async def close(self) -> None:
        self.s.calls.append("close")


class ScriptedFactory:
    def __init__(self, script: Script) -> None:
        self.script = script
        self.options: list[BrowserOptions] = []

    async def start(self, options: BrowserOptions) -> ScriptedBrowser:
        self.script.starts += 1
        self.options.append(options)
        return ScriptedBrowser(self.script)


class Candidates:
    def __init__(self, profile: CandidateProfile) -> None:
        self.profile = profile
        self.saved: list[SavedAnswer] = []

    def load(self, candidate_id: str) -> CandidateProfile:
        return self.profile.model_copy(update={"id": candidate_id})

    def save_answer(self, candidate_id: str, answer: SavedAnswer) -> None:
        self.saved.append(answer)


class Answering(NoninteractiveInteraction):
    """Answers every question with fixed text (like --interactive, but scripted)."""

    def __init__(self, text: str, reuse: AnswerReuse = AnswerReuse.APPLICATION) -> None:
        super().__init__()
        self.text, self.reuse = text, reuse

    async def request_inputs(self, missing: Sequence[MissingInput]) -> Sequence[UserInput]:
        return [UserInput.answering(m, TextValue(text=self.text), reuse=self.reuse)
                for m in missing]


def _runner(paths, candidate, script, *, interaction=None, limits=None) -> LocalApplicationRunner:
    return LocalApplicationRunner(
        paths=paths, interaction=interaction or NoninteractiveInteraction(), headless=True,
        browser_factory=ScriptedFactory(script), candidates=Candidates(candidate),
        limits=limits or RunLimits(max_steps=6, max_same_form=2),
    )


def _store(paths) -> ApplicationStore:
    return ApplicationStore.open(paths.state_db)


# --- happy path, threads -----------------------------------------------------------------


def test_constructs_without_side_effects_and_runs_on_a_service_thread(
    isolated_imx_home, fictional_candidate
):
    script = Script()
    runner = _runner(isolated_imx_home, fictional_candidate, script)
    assert not isolated_imx_home.state_db.exists()  # construction opens nothing
    result: dict[str, object] = {}

    def service_thread() -> None:  # like S1: one loop on a background thread, no TTY
        loop = asyncio.new_event_loop()
        try:
            result["outcome"] = loop.run_until_complete(runner.apply(URL, candidate_id="c1"))
        finally:
            loop.close()

    thread = threading.Thread(target=service_thread)
    thread.start()
    thread.join(30)
    outcome = result["outcome"]
    assert outcome.state is S.SUBMITTED and outcome.receipt is not None  # type: ignore[attr-defined]
    assert outcome.receipt.confirmation_reference == "F-REF-1"  # type: ignore[attr-defined]
    assert script.calls == ["open", "fill", "submit", "confirm", "close"]
    with _store(isolated_imx_home) as store:
        app = store.get_application(outcome.application_id)  # type: ignore[attr-defined]
        assert store.pinned_resume(app.id) == fictional_candidate.resume
        assert store.get_job(app.job_id).title == "Fictional Analyst"


# --- submission safety -----------------------------------------------------------------------


@pytest.mark.parametrize("failure", [asyncio.CancelledError(), KeyboardInterrupt()])
def test_interruption_during_submit_records_unknown_and_blocks_retry(
    isolated_imx_home, fictional_candidate, failure
):
    script = Script(submit=failure)
    runner = _runner(isolated_imx_home, fictional_candidate, script)
    with pytest.raises(type(failure)):
        asyncio.run(runner.apply(URL, candidate_id="c1"))
    with _store(isolated_imx_home) as store:
        [app] = store.list_applications()
        assert app.state is S.SUBMISSION_UNKNOWN and app.claim_owner is None
        [attempt] = store.list_attempts(app.id)
        assert attempt.outcome is SubmissionOutcome.UNKNOWN
    again = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert again.state is S.SUBMISSION_UNKNOWN
    assert script.calls.count("submit") == 1 and script.starts == 1


def test_error_while_confirming_is_unknown_not_a_retryable_failure(
    isolated_imx_home, fictional_candidate
):
    script = Script(confirm=RuntimeError("page crashed"))
    outcome = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script)
                          .apply(URL, candidate_id="c1"))
    assert outcome.state is S.SUBMISSION_UNKNOWN
    assert asyncio.run(_runner(isolated_imx_home, fictional_candidate, script)
                       .resume(outcome.application_id)).state is S.SUBMISSION_UNKNOWN
    assert script.calls.count("submit") == 1


# --- no infinite browser loops, unambiguous next vs submit ---------------------------------


def test_the_same_form_coming_back_stops_the_run(isolated_imx_home, fictional_candidate):
    page = _page(_form(final=False))
    script = Script(pages=[page], advance=[NavigationResult(advanced=True, inspection=page)] * 5)
    outcome = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script)
                          .apply(URL, candidate_id="c1"))
    assert outcome.state is S.FAILED_RETRYABLE and "kept coming back" in outcome.message
    assert "submit" not in script.calls


def test_a_run_stops_after_max_steps(isolated_imx_home, fictional_candidate):
    pages = [_page(_form(i, final=False)) for i in range(10)]
    script = Script(pages=pages, advance=[NavigationResult(advanced=True, inspection=p)
                                          for p in pages[1:]])
    outcome = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script)
                          .apply(URL, candidate_id="c1"))
    assert outcome.state is S.FAILED_RETRYABLE and "Stopped after 6 pages" in outcome.message
    assert "submit" not in script.calls


def test_an_ambiguous_next_or_submit_control_is_never_clicked(isolated_imx_home, fictional_candidate):
    script = Script(pages=[_page(_form(final=None))],
                    advance=[AmbiguousAction("both 'Next' and 'Submit' are offered")])
    outcome = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script)
                          .apply(URL, candidate_id="c1"))
    assert outcome.state is S.FAILED_RETRYABLE and "Nothing was submitted" in outcome.message
    assert "submit" not in script.calls


# --- contention ---------------------------------------------------------------------------------


def test_a_held_browser_profile_refuses_a_second_run(isolated_imx_home, fictional_candidate):
    script = Script()
    with browser_profile_lock(isolated_imx_home.browser_dir):
        outcome = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script)
                              .apply(URL, candidate_id="c1"))
    assert "another run is using the browser profile" in outcome.message
    assert outcome.state is S.REQUESTED and script.starts == 0


def test_a_claimed_application_is_not_run_twice(isolated_imx_home, fictional_candidate):
    isolated_imx_home.ensure()
    with _store(isolated_imx_home) as store:
        app = store.record_request("c1", URL).application
        store.claim(app.id, "someone-else", ttl=timedelta(minutes=5))
    script = Script()
    outcome = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script)
                          .resume(app.id))
    assert "Another run is working" in outcome.message and script.starts == 0


# --- missing input and user action --------------------------------------------------------------


def test_missing_answers_are_durable_and_reused_after_a_restart(isolated_imx_home, fictional_candidate):
    form = _form(extra_label="Why this role?")
    script = Script(pages=[_page(form)])
    first = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script)
                        .apply(URL, candidate_id="c1"))
    assert first.state is S.NEEDS_INPUT
    assert [m.field_id for m in first.missing_inputs] == ["q0"]
    assert "submit" not in script.calls
    with _store(isolated_imx_home) as store:  # a new process sees the same questions
        [question] = pending_inputs(store, first.application_id)
        assert question.label == "Why this role?"
        claim = store.claim(first.application_id, "answer-cli")
        store.save_user_inputs(claim, [UserInput.answering(question, TextValue(text="Because."))])
        store.release(claim)
    second = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script)
                         .resume(first.application_id))
    assert second.state is S.SUBMITTED


def test_interactive_answers_are_saved_with_the_chosen_reuse(isolated_imx_home, fictional_candidate):
    script = Script(pages=[_page(_form(extra_label="Why this role?"))])
    runner = _runner(isolated_imx_home, fictional_candidate, script,
                     interaction=Answering("Because.", AnswerReuse.GLOBAL))
    outcome = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert outcome.state is S.SUBMITTED
    [saved] = runner.candidates.saved  # type: ignore[attr-defined]
    assert saved.scope.value == "GLOBAL" and saved.question == "Why this role?"


def test_sign_in_waits_for_the_user_only_when_allowed(isolated_imx_home, fictional_candidate):
    sign_in = _page(kind=PageKind.SIGN_IN_REQUIRED, identity=None)
    script = Script(pages=[sign_in], after_user=_page(_form()))
    declined = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script)
                           .apply(URL, candidate_id="c1"))
    assert declined.state is S.NEEDS_INPUT
    assert [(m.reason.value, m.field_id) for m in declined.missing_inputs] == [("USER_ACTION", None)]
    assert "wait_for_user" not in script.calls
    allowed = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script,
                                  interaction=NoninteractiveInteraction(allow_browser_action=True))
                          .resume(declined.application_id))
    assert allowed.state is S.SUBMITTED and "wait_for_user" in script.calls


# --- reconciliation -------------------------------------------------------------------------------


def _unknown(isolated_imx_home, fictional_candidate) -> str:
    script = Script(confirm=SubmissionObservation(outcome=SubmissionOutcome.UNKNOWN,
                                                  signals=["HTTP 502"]))
    outcome = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script)
                          .apply(URL, candidate_id="c1"))
    assert outcome.state is S.SUBMISSION_UNKNOWN
    return outcome.application_id


def test_reconcile_records_only_site_acceptance(isolated_imx_home, fictional_candidate):
    app_id = _unknown(isolated_imx_home, fictional_candidate)
    still = Script(reconcile=SubmissionObservation(outcome=SubmissionOutcome.UNKNOWN,
                                                   signals=["observed: still processing"]))
    outcome = asyncio.run(_runner(isolated_imx_home, fictional_candidate, still).reconcile(app_id))
    assert outcome.state is S.SUBMISSION_UNKNOWN and outcome.receipt is None
    assert still.calls == ["reconcile", "close"]  # re-read only; no form actions

    confirmed = Script(reconcile=ACCEPTED)
    outcome = asyncio.run(_runner(isolated_imx_home, fictional_candidate, confirmed)
                          .reconcile(app_id))
    assert outcome.state is S.SUBMITTED
    assert outcome.receipt.reconciliation_method.value == "SITE_CONFIRMATION"  # type: ignore[union-attr]
    with _store(isolated_imx_home) as store:
        events = [e.event for e in store.list_events(app_id)]
        assert "reconcile.unconfirmed" in events
        assert len(store.list_attempts(app_id)) == 1


def test_reconcile_without_a_job_identity_never_accepts(isolated_imx_home, fictional_candidate):
    script = Script(pages=[_page(_form(), identity=None)],
                    confirm=SubmissionObservation(outcome=SubmissionOutcome.UNKNOWN, signals=["x"]))
    outcome = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script)
                          .apply(URL, candidate_id="c1"))
    check = Script(reconcile=ACCEPTED)
    result = asyncio.run(_runner(isolated_imx_home, fictional_candidate, check)
                         .reconcile(outcome.application_id))
    assert result.state is S.SUBMISSION_UNKNOWN and "cannot be tied" in result.message
    assert check.starts == 0


# --- pinned resume ---------------------------------------------------------------------------------


def test_resume_uses_the_pinned_resume_not_the_profiles_current_one(
    isolated_imx_home, fictional_candidate, tmp_path
):
    script = Script(pages=[_page(_form(extra_label="Why this role?"))])
    first = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script)
                        .apply(URL, candidate_id="c1"))
    other_file = tmp_path / "resume-b.pdf"
    other_file.write_bytes(b"%PDF-1.4 fictional resume B")
    resume_b = type(fictional_candidate.resume).from_file(
        other_file, id="resume_b", media_type="application/pdf")
    changed = fictional_candidate.model_copy(update={"resume": resume_b})
    runner = _runner(isolated_imx_home, changed, script, interaction=Answering("Because."))
    asyncio.run(runner.resume(first.application_id))
    with _store(isolated_imx_home) as store:
        assert store.pinned_resume(first.application_id) == fictional_candidate.resume
        packet = store.latest_packet(first.application_id)
        assert packet is not None
        assert store.get_application(first.application_id).state is S.SUBMITTED
