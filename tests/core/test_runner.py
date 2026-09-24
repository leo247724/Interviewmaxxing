"""LocalApplicationRunner control flow with a scripted browser.

The real store, the real factual resolver (C3) and a fictional candidate are used;
only the browser is scripted, to force cases a live page cannot produce on demand:
cancellation mid-submit, loops, ambiguous controls, lock and claim contention,
threads. Real-Chromium coverage of the same runner is in ``e2e/``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from interviewmaxxing_browser import AmbiguousAction
from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_cli.runner import (
    REJECTION_EVENT,
    SUGGESTION_EVENT,
    LocalApplicationRunner,
    NoninteractiveInteraction,
    RunLimits,
    browser_profile_lock,
    pending_inputs,
    rejection_epochs,
)
from interviewmaxxing_core import (
    AnswerReuse,
    AnswerSource,
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
    MissingReason,
    NavigationResult,
    NotSubmittedNext,
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
from interviewmaxxing_generation import FactualPacketResolver

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
          identity: JobIdentityObservation | None = IDENTITY, *,
          captcha_pending: bool = False) -> PageInspection:
    return PageInspection(kind=kind, observed_url=URL, form=form, job_identity=identity,
                          message="Sign in to continue" if kind is PageKind.SIGN_IN_REQUIRED else None,
                          captcha_pending=captcha_pending)


ACCEPTED = SubmissionObservation(outcome=SubmissionOutcome.ACCEPTED,
                                 signals=["heading 'Application submitted'", "job id 'F-1' shown"],
                                 confirmation_reference="F-REF-1")


@dataclass
class Script:
    pages: list[PageInspection] = field(default_factory=lambda: [_page(_form())])
    advance: list[NavigationResult | Exception] = field(default_factory=list)
    submit: SubmitActionResult | BaseException = field(
        default_factory=lambda: SubmitActionResult(dispatched=True))
    confirm: SubmissionObservation | BaseException | list[SubmissionObservation] = field(
        default_factory=lambda: ACCEPTED)
    """One observation for every submit, or a list consumed one per submit."""
    inspect_pages: list[PageInspection] = field(default_factory=list)
    """Pages ``inspect()`` returns after a submit or fill, in order (then the current page)."""
    after_user: PageInspection | None = None
    on_wait: Callable[[], Awaitable[None]] | None = None
    """Runs inside ``wait_for_user`` (e.g. to pass virtual time)."""
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
        if self.s.inspect_pages:
            self.current = self.s.inspect_pages.pop(0)
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
        if isinstance(self.s.confirm, list):
            return self.s.confirm.pop(0)
        return self.s.confirm

    async def wait_for_user(self, reason: str, timeout_s: float | None = None) -> PageInspection:
        self.s.calls.append("wait_for_user")
        if self.s.on_wait is not None:
            await self.s.on_wait()
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


def _runner(paths, candidate, script, *, interaction=None, limits=None,
            clock=None, prepare_only=False) -> LocalApplicationRunner:
    # These legacy tests exercise confirmed submission against a scripted browser.
    # Preparation tests below explicitly retain the production no-submit default.
    extra = {"clock": clock} if clock is not None else {}
    return LocalApplicationRunner(
        paths=paths, interaction=interaction or NoninteractiveInteraction(), headless=True,
        browser_factory=ScriptedFactory(script), candidates=Candidates(candidate),
        limits=limits or RunLimits(max_steps=6, max_same_form=2),
        prepare_only=prepare_only, **extra,
    )


def _store(paths, clock=None) -> ApplicationStore:
    return ApplicationStore.open(paths.state_db, **({"clock": clock} if clock else {}))


# --- happy path, threads -----------------------------------------------------------------


def test_default_runner_reaches_final_review_without_submission(
    isolated_imx_home, fictional_candidate
):
    script = Script(pages=[_page(_form(final=False))], advance=[
        NavigationResult(advanced=True, inspection=_page(_form(step=1)))])
    factory = ScriptedFactory(script)
    runner = LocalApplicationRunner(
        paths=isolated_imx_home, candidates=Candidates(fictional_candidate),
        interaction=NoninteractiveInteraction(), browser_factory=factory,
    )
    result = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and result.receipt is None
    assert "final review" in result.message and "Nothing was submitted" in result.message
    assert script.calls.count("fill") == 2 and script.calls.count("advance") == 1
    assert "submit" not in script.calls and "confirm" not in script.calls
    assert factory.options[-1].allow_submission is False
    with _store(isolated_imx_home) as store:
        assert store.is_preparation_only(result.application_id)
        assert store.list_attempts(result.application_id) == []
        assert store.get_receipt(result.application_id) is None
        ready = [e for e in store.list_events(result.application_id)
                 if e.event == "preparation.ready"]
        assert len(ready) == 1 and ready[0].metadata["form_step"] == 1


def test_preparation_restriction_survives_restart_resume_and_repeated_apply(
    isolated_imx_home, fictional_candidate
):
    first = _runner(isolated_imx_home, fictional_candidate, Script(), prepare_only=True)
    result = asyncio.run(first.apply(URL, candidate_id="c1"))
    for repeat_request in (False, True):
        script = Script()
        runner = _runner(isolated_imx_home, fictional_candidate, script, prepare_only=False)
        call = (runner.apply(URL, candidate_id="c1") if repeat_request
                else runner.resume(result.application_id))
        resumed = asyncio.run(call)
        assert resumed.application_id == result.application_id
        assert resumed.state is S.NEEDS_INPUT and resumed.receipt is None
        assert "submit" not in script.calls
        assert runner.browser_factory.options[-1].allow_submission is False
    with _store(isolated_imx_home) as store:
        assert store.list_attempts(result.application_id) == []
        assert sum(e.event == "application.preparation_only"
                   for e in store.list_events(result.application_id)) == 1


def test_changed_final_form_is_not_reported_ready(isolated_imx_home, fictional_candidate):
    script = Script(inspect_pages=[_page(_form(extra_label="Describe a missing qualification"))])
    runner = _runner(isolated_imx_home, fictional_candidate, script, prepare_only=True)
    result = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and result.missing_inputs
    assert "submit" not in script.calls
    with _store(isolated_imx_home) as store:
        assert not any(e.event == "preparation.ready" for e in store.list_events(result.application_id))


def test_final_validation_errors_are_not_reported_ready(isolated_imx_home, fictional_candidate):
    invalid = _form().model_copy(update={"page_errors": ["Please correct the form"]})
    script = Script(inspect_pages=[_page(invalid)])
    runner = _runner(isolated_imx_home, fictional_candidate, script, prepare_only=True)
    result = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and "validation errors" in result.message
    assert "submit" not in script.calls
    with _store(isolated_imx_home) as store:
        assert not any(e.event == "preparation.ready" for e in store.list_events(result.application_id))


def test_preparation_notes_a_pending_captcha_widget(isolated_imx_home, fictional_candidate):
    script = Script(pages=[_page(_form(), captcha_pending=True)])
    runner = _runner(isolated_imx_home, fictional_candidate, script, prepare_only=True)
    result = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and result.missing_inputs == []
    assert "Prepared to the final review step" in result.message
    assert "A CAPTCHA on this form must be solved in the browser before it can be submitted." in result.message
    assert "submit" not in script.calls and "wait_for_user" not in script.calls
    with _store(isolated_imx_home) as store:
        [ready] = [e for e in store.list_events(result.application_id)
                   if e.event == "preparation.ready"]
        assert ready.metadata["captcha_pending"] is True


CAPTCHA_REFUSED = SubmissionObservation(
    outcome=SubmissionOutcome.NOT_SUBMITTED,
    signals=["submit was not dispatched: a CAPTCHA on this form must be solved by the user "
             "before submitting"],
    next_state=NotSubmittedNext.NEEDS_INPUT,
    detail="a CAPTCHA on this form must be solved by the user before submitting",
)


def test_a_submit_refused_for_a_pending_captcha_stops_as_a_user_action(
    isolated_imx_home, fictional_candidate
):
    script = Script(pages=[_page(_form(), captcha_pending=True)],
                    submit=SubmitActionResult(dispatched=False, detail=CAPTCHA_REFUSED.detail),
                    confirm=CAPTCHA_REFUSED)
    runner = _runner(isolated_imx_home, fictional_candidate, script, prepare_only=False)
    result = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT
    [need] = result.missing_inputs
    assert (need.reason, need.label, need.field_id) == (MissingReason.USER_ACTION, "Solve the CAPTCHA", None)
    assert script.calls.count("submit") == 1 and "wait_for_user" not in script.calls
    with _store(isolated_imx_home) as store:
        assert [n.label for n in pending_inputs(store, result.application_id)] == ["Solve the CAPTCHA"]
        assert [a.outcome for a in store.list_attempts(result.application_id)] == [
            SubmissionOutcome.NOT_SUBMITTED]


def test_a_pending_captcha_is_waited_for_when_the_user_is_present_then_submitted(
    isolated_imx_home, fictional_candidate
):
    script = Script(pages=[_page(_form(), captcha_pending=True)],
                    submit=SubmitActionResult(dispatched=False, detail=CAPTCHA_REFUSED.detail),
                    confirm=[CAPTCHA_REFUSED, ACCEPTED], after_user=_page(_form()))
    runner = _runner(isolated_imx_home, fictional_candidate, script,
                     interaction=NoninteractiveInteraction(allow_browser_action=True),
                     prepare_only=False)
    result = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert result.state is S.SUBMITTED and result.receipt is not None
    assert script.calls.count("wait_for_user") == 1 and script.calls.count("submit") == 2


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


def _expected_application(paths):
    with _store(paths) as store:
        app = store.record_request("c1", URL).application
        store.pin_expected_job_identity(app.id, IDENTITY.identity_key)
        return app


@pytest.mark.parametrize("identity", [None, IDENTITY.model_copy(update={"external_job_id": "other"})])
def test_expected_job_blocks_wrong_or_unidentified_page_across_restarts(
    isolated_imx_home, fictional_candidate, identity
):
    app = _expected_application(isolated_imx_home)
    for _ in range(2):
        script = Script(pages=[_page(_form(), identity=identity)])
        result = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script).resume(app.id))
        assert result.state is S.FAILED_RETRYABLE and "selected job" in result.message
        assert script.calls == ["open", "close"]
        with _store(isolated_imx_home) as store:
            assert store.expected_job_identity(app.id) == IDENTITY.identity_key
            assert store.get_job(app.job_id).identity_key is None
            assert store.list_attempts(app.id) == [] and store.get_receipt(app.id) is None
    # A later fresh page supplies matching evidence; the expectation itself never did.
    correct = Script()
    result = asyncio.run(_runner(isolated_imx_home, fictional_candidate, correct).resume(app.id))
    assert result.state is S.SUBMITTED and correct.calls.count("submit") == 1


@pytest.mark.parametrize("identity", [None, IDENTITY.model_copy(update={"external_job_id": "other"})])
@pytest.mark.parametrize("boundary", ["before_fill", "before_submit", "before_advance"])
def test_expected_job_is_rechecked_before_every_form_action(
    isolated_imx_home, fictional_candidate, identity, boundary
):
    app = _expected_application(isolated_imx_home)
    form = _form(final=boundary != "before_advance")
    correct = _page(form)
    changed = _page(form, identity=identity)
    inspections = [changed] if boundary == "before_fill" else [correct, changed]
    script = Script(pages=[correct], inspect_pages=inspections)
    result = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script).resume(app.id))
    assert result.state is S.FAILED_RETRYABLE and "selected job" in result.message
    assert script.calls.count("fill") == (0 if boundary == "before_fill" else 1)
    assert "submit" not in script.calls and "advance" not in script.calls
    with _store(isolated_imx_home) as store:
        assert store.list_attempts(app.id) == [] and store.get_receipt(app.id) is None
        assert store.get_job(app.job_id).identity_key == IDENTITY.identity_key


def test_expected_job_survives_login_stop_before_identity_is_observed(
    isolated_imx_home, fictional_candidate
):
    app = _expected_application(isolated_imx_home)
    login = Script(pages=[_page(kind=PageKind.SIGN_IN_REQUIRED, identity=None)])
    first = asyncio.run(_runner(isolated_imx_home, fictional_candidate, login).resume(app.id))
    assert first.state is S.NEEDS_INPUT
    changed = Script(pages=[_page(_form(), identity=IDENTITY.model_copy(
        update={"external_job_id": "other"}))])
    resumed = asyncio.run(_runner(isolated_imx_home, fictional_candidate, changed).resume(app.id))
    assert resumed.state is S.FAILED_RETRYABLE and "fill" not in changed.calls


def test_expected_job_checks_identity_after_submit_progress_callback(
    isolated_imx_home, fictional_candidate
):
    app = _expected_application(isolated_imx_home)
    script = Script()

    class PageChanges(NoninteractiveInteraction):
        async def progress(self, message):
            if message == "Submitting the application":
                script.inspect_pages.append(_page(_form(), identity=None))

    result = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script,
                                 interaction=PageChanges()).resume(app.id))
    assert result.state is S.FAILED_RETRYABLE and "submit" not in script.calls
    with _store(isolated_imx_home) as store:
        assert store.list_attempts(app.id) == []


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


@pytest.mark.parametrize("profile_available", [False, True])
def test_a_held_browser_profile_refuses_a_second_run(
    isolated_imx_home, fictional_candidate, profile_available
):
    script = Script()
    runner = _runner(isolated_imx_home, fictional_candidate, script)
    if not profile_available:
        runner.candidates = LocalCandidateStore.from_paths(isolated_imx_home)
    with browser_profile_lock(isolated_imx_home.browser_dir):
        outcome = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert "another run is using the browser profile" in outcome.message
    assert outcome.state is S.REQUESTED and script.starts == 0


@pytest.mark.parametrize("profile_available", [False, True])
def test_a_claimed_application_is_not_run_twice(
    isolated_imx_home, fictional_candidate, profile_available
):
    isolated_imx_home.ensure()
    with _store(isolated_imx_home) as store:
        app = store.record_request("c1", URL).application
        store.claim(app.id, "someone-else", ttl=timedelta(minutes=5))
    script = Script()
    runner = _runner(isolated_imx_home, fictional_candidate, script)
    if not profile_available:
        runner.candidates = LocalCandidateStore.from_paths(isolated_imx_home)
    outcome = asyncio.run(runner.resume(app.id))
    assert "Another run is working" in outcome.message and script.starts == 0
    assert outcome.state is S.REQUESTED
    with _store(isolated_imx_home) as store:
        assert store.get_application(app.id).failure_reason is None
        assert store.get_application(app.id).claim_owner == "someone-else"


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


# --- claim heartbeat: long user waits, takeover fencing, cancellation (I1R, B1) ------------------

TTL = 300.0
LONG_WAIT = 599.0
"""The user takes ten minutes; without the heartbeat the 300 s lease lapses at 301 s."""


async def _until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition not met in time"
        await asyncio.sleep(0.005)


class VirtualWait:
    """Passes ``LONG_WAIT`` seconds of virtual time in 100 s steps while the runner
    waits, checking after every step that the heartbeat renewed the lease past the
    new now (like a user who takes ten minutes to answer or sign in)."""

    def __init__(self, paths, clock) -> None:
        self.paths, self.clock, self.steps = paths, clock, 0

    async def __call__(self) -> None:
        with _store(self.paths, self.clock) as store:
            [app] = store.list_applications()
            elapsed = 0.0
            while elapsed < LONG_WAIT:
                step = min(100.0, LONG_WAIT - elapsed)
                now = self.clock.advance(seconds=step)
                elapsed += step
                self.steps += 1
                await self._renewed_past(store, app.id, now + timedelta(seconds=TTL - 1))

    @staticmethod
    async def _renewed_past(store, app_id, moment) -> None:
        await _until(lambda: (store.get_application(app_id).claim_expires_at or moment) > moment)


def _heartbeat_limits(**overrides):
    return RunLimits(max_steps=6, max_same_form=2, claim_ttl_s=TTL, claim_heartbeat_s=0.01,
                     **overrides)


@pytest.mark.parametrize("wait_point", ["request_inputs", "request_action", "wait_for_user"])
def test_the_claim_is_kept_alive_while_the_user_takes_longer_than_the_ttl(
    isolated_imx_home, fictional_candidate, clock, wait_point
):
    paths, passing = isolated_imx_home, VirtualWait(isolated_imx_home, clock)

    class SlowUser(Answering):
        async def request_inputs(self, missing):
            await passing()
            return await super().request_inputs(missing)

        async def request_action(self, message):
            await passing()
            return True

    if wait_point == "request_inputs":
        script = Script(pages=[_page(_form(extra_label="Why this role?"))])
    else:
        sign_in = _page(kind=PageKind.SIGN_IN_REQUIRED, identity=None)
        script = Script(pages=[sign_in], after_user=_page(_form()),
                        on_wait=passing if wait_point == "wait_for_user" else None)
    interaction = SlowUser("Because.") if wait_point != "wait_for_user" else \
        NoninteractiveInteraction(allow_browser_action=True)
    runner = _runner(paths, fictional_candidate, script, interaction=interaction,
                     limits=_heartbeat_limits(), clock=clock)
    outcome = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert outcome.state is S.SUBMITTED, outcome.message
    assert passing.steps == 6  # 599 s of virtual time passed while waiting
    with _store(paths, clock) as store:
        app = store.get_application(outcome.application_id)
        assert app.claim_owner is None
        if wait_point == "request_inputs":
            [saved] = store.list_user_inputs(app.id)
            assert saved.value == TextValue(text="Because.")


def test_without_the_heartbeat_a_wait_past_the_ttl_loses_the_lease(
    isolated_imx_home, fictional_candidate, clock
):
    """The situation I1 shipped with: 299 s is fine, 301 s loses the answers."""

    class Jump(Answering):
        def __init__(self, seconds):
            super().__init__("Because.")
            self.seconds = seconds

        async def request_inputs(self, missing):
            clock.advance(seconds=self.seconds)
            return await super().request_inputs(missing)

    slow = RunLimits(max_steps=6, max_same_form=2, claim_ttl_s=TTL, claim_heartbeat_s=3600.0)
    script = Script(pages=[_page(_form(extra_label="Why this role?"))])
    fine = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script, interaction=Jump(299),
                               limits=slow, clock=clock).apply(URL, candidate_id="c1"))
    assert fine.state is S.SUBMITTED
    script = Script(pages=[_page(_form(extra_label="Why this role?", url=URL + "?v=2"),
                                 identity=None)])  # another job
    lost = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script, interaction=Jump(301),
                               limits=slow, clock=clock).apply(URL + "?v=2", candidate_id="c1"))
    assert lost.state is S.INSPECTING and "no longer holds" in lost.message
    with _store(isolated_imx_home, clock) as store:
        assert store.list_user_inputs(lost.application_id) == []


def test_a_claim_taken_over_during_a_wait_is_never_written_to_or_released(
    isolated_imx_home, fictional_candidate, clock
):
    """Genuine takeover: the lease lapsed (heartbeat too slow), another run claimed the
    application and moved it on. The stale run's answers are refused (fenced by the
    claim token), it releases nothing that is not its own, and it reports the loss."""
    paths = isolated_imx_home

    class TakenOver(Answering):
        async def request_inputs(self, missing):
            clock.advance(seconds=TTL + 1)
            with _store(paths, clock) as other:
                [app] = other.list_applications()
                claim = other.claim(app.id, "other-run")
                other.transition(claim, S.NEEDS_INPUT,
                                 metadata={"missing_inputs": [], "reason": "taken over"})
            return await super().request_inputs(missing)

    script = Script(pages=[_page(_form(extra_label="Why this role?"))])
    slow = RunLimits(max_steps=6, max_same_form=2, claim_ttl_s=TTL, claim_heartbeat_s=3600.0)
    outcome = asyncio.run(_runner(paths, fictional_candidate, script, interaction=TakenOver("Because."),
                                  limits=slow, clock=clock).apply(URL, candidate_id="c1"))
    assert "no longer holds" in outcome.message and outcome.state is S.NEEDS_INPUT
    assert script.calls == ["open", "close"]  # nothing filled or submitted afterwards
    with _store(paths, clock) as store:
        app = store.get_application(outcome.application_id)
        assert app.claim_owner == "other-run"  # the stale run did not release the new owner's claim
        assert store.list_user_inputs(app.id) == []
        assert store.list_events(app.id)[-1].actor == "other-run"


def test_a_lost_claim_cancels_the_wait_promptly(isolated_imx_home, fictional_candidate, clock):
    """The heartbeat notices a lapsed lease (here: the clock jumped past the TTL, as
    after a laptop sleep) and cancels the prompt instead of letting the user type
    answers that could not be saved."""
    flags: list[str] = []

    class Stuck(NoninteractiveInteraction):
        async def request_inputs(self, missing):
            clock.advance(seconds=TTL + 1)
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                flags.append("prompt cancelled")
                raise
            return []

    script = Script(pages=[_page(_form(extra_label="Why this role?"))])
    started = time.monotonic()
    outcome = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script, interaction=Stuck(),
                                  limits=_heartbeat_limits(), clock=clock)
                          .apply(URL, candidate_id="c1"))
    assert time.monotonic() - started < 5
    assert flags == ["prompt cancelled"] and "no longer holds" in outcome.message
    assert outcome.state is S.INSPECTING and script.calls == ["open", "close"]


def test_cancelling_a_run_during_a_wait_stops_the_heartbeat_and_releases_the_claim(
    isolated_imx_home, fictional_candidate
):
    started = threading.Event()

    class Blocking(NoninteractiveInteraction):
        async def request_inputs(self, missing):
            started.set()
            await asyncio.sleep(3600)
            return []

    script = Script(pages=[_page(_form(extra_label="Why this role?"))])
    runner = _runner(isolated_imx_home, fictional_candidate, script, interaction=Blocking(),
                     limits=_heartbeat_limits())

    async def scenario() -> None:
        task = asyncio.create_task(runner.apply(URL, candidate_id="c1"))
        await asyncio.to_thread(started.wait, 10)
        await asyncio.sleep(0.05)  # a few heartbeats
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.02)
        assert {t for t in asyncio.all_tasks() if t is not asyncio.current_task()} == set()

    asyncio.run(scenario())
    assert script.calls == ["open", "close"]
    with _store(isolated_imx_home) as store:
        [app] = store.list_applications()
        assert app.state is S.INSPECTING and app.claim_owner is None


# --- custom controls the user does not operate; capped question rounds (I1R, B2) -------------


def _custom_form(*, operated: bool = False) -> ApplicationForm:
    return ApplicationForm(url=URL, step=0, is_final_step=True, submit_selector="#submit", fields=[
        ApplicationField(id="first_name", label="First name", selector="#first_name",
                         semantic_type=SemanticType.FIRST_NAME, control_type=ControlType.TEXT,
                         required=True),
        ApplicationField(id="availability", label="Availability picker", selector="#availability",
                         semantic_type=SemanticType.CUSTOM_TEXT, control_type=ControlType.UNSUPPORTED,
                         required=not operated),
    ])


def test_an_unoperated_custom_control_stops_as_needs_input_and_resumes_once_operated(
    isolated_imx_home, fictional_candidate
):
    page = _page(_custom_form())
    script = Script(pages=[page], after_user=page)  # the wait ends with the control untouched
    outcome = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script,
                                  interaction=NoninteractiveInteraction(allow_browser_action=True))
                          .apply(URL, candidate_id="c1"))
    assert outcome.state is S.NEEDS_INPUT and "Still waiting" in outcome.message
    assert script.calls.count("wait_for_user") == 1 and "submit" not in script.calls
    [item] = outcome.missing_inputs
    assert (item.reason, item.field_id, item.label) == (
        MissingReason.UNSUPPORTED_CONTROL, "availability", "Availability picker")
    assert item.form_step == 0 and item.field_fingerprint is not None
    with _store(isolated_imx_home) as store:  # a new process sees the same item
        assert [m.field_id for m in pending_inputs(store, outcome.application_id)] == ["availability"]

    # In a later run the user operates the control during the wait: the run continues.
    later = Script(pages=[page], after_user=_page(_custom_form(operated=True)))
    resumed = asyncio.run(_runner(isolated_imx_home, fictional_candidate, later,
                                  interaction=NoninteractiveInteraction(allow_browser_action=True))
                          .resume(outcome.application_id))
    assert resumed.state is S.SUBMITTED and later.calls.count("wait_for_user") == 1


class CountingAnswering(Answering):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.asked = 0

    async def request_inputs(self, missing):
        self.asked += 1
        return await super().request_inputs(missing)


def test_a_step_that_keeps_needing_input_stops_after_max_input_rounds(
    isolated_imx_home, fictional_candidate
):
    form = _form()
    form = form.model_copy(update={"fields": [*form.fields, ApplicationField(
        id="q0", label="Why this role?", selector="#q0", semantic_type=SemanticType.CUSTOM_TEXT,
        control_type=ControlType.TEXT, required=True, max_length=5)]})
    script = Script(pages=[_page(form)])
    interaction = CountingAnswering("This answer is far too long for the field")
    outcome = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script, interaction=interaction,
                                  limits=RunLimits(max_steps=6, max_same_form=2, max_input_rounds=3))
                          .apply(URL, candidate_id="c1"))
    assert outcome.state is S.NEEDS_INPUT and "after 3 attempts" in outcome.message
    assert interaction.asked == 3 and "submit" not in script.calls
    [item] = outcome.missing_inputs
    assert item.field_id == "q0" and "cannot be used" in item.prompt


# --- rejection epochs: corrections survive restarts, re-rejections are asked again (I1R, B3)


def _shown_again(form: ApplicationForm, field_id: str, message: str) -> ApplicationForm:
    return form.model_copy(update={"fields": [
        f.model_copy(update={"validation_error": message}) if f.id == field_id else f
        for f in form.fields]})


REJECTED = SubmissionObservation(
    outcome=SubmissionOutcome.NOT_SUBMITTED,
    signals=["the site showed the form again with validation errors"],
    validation_errors=["First name: Enter a valid name"],
    next_state=NotSubmittedNext.NEEDS_INPUT, detail="rejected by the site's validation",
)


@pytest.mark.parametrize("input_case", ["none", "older", "other_step", "other_question"])
def test_unresolved_rejection_survives_a_fresh_form_without_validation_text(
    isolated_imx_home, fictional_candidate, input_case
):
    form = _form()
    if input_case == "older":
        with _store(isolated_imx_home) as store:
            app = store.record_request("c1", URL).application
            claim = store.claim(app.id, "earlier-answer")
            store.save_user_inputs(claim, [UserInput.for_field(
                form, "first_name", TextValue(text="Avery"))])
            store.release(claim)
    again = _page(_shown_again(form, "first_name", "Enter a valid name"))
    script = Script(pages=[_page(form)], confirm=[REJECTED], inspect_pages=[again])
    first = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script)
                        .apply(URL, candidate_id="c1"))
    assert first.state is S.NEEDS_INPUT
    [question] = first.missing_inputs
    if input_case in ("other_step", "other_question"):
        correction = UserInput.answering(question, TextValue(text="Avery"))
        correction = correction.model_copy(update={
            "form_step": 1} if input_case == "other_step" else {"field_fingerprint": "0" * 64})
        with _store(isolated_imx_home) as store:
            claim = store.claim(first.application_id, "unrelated-answer")
            store.save_user_inputs(claim, [correction])
            store.release(claim)

    clean = Script(pages=[_page(form)])
    second = asyncio.run(_runner(isolated_imx_home, fictional_candidate, clean)
                         .resume(first.application_id))
    assert second.state is S.NEEDS_INPUT
    [pending] = second.missing_inputs
    assert pending.field_id == "first_name" and pending.form_step == 0
    assert pending.field_fingerprint == question.field_fingerprint
    assert "Enter a valid name" in pending.prompt
    assert clean.calls == ["open", "close"]
    with _store(isolated_imx_home) as store:
        assert len(store.list_attempts(first.application_id)) == 1
        assert [e.event for e in store.list_events(first.application_id)].count(REJECTION_EVENT) == 1
        assert store.latest_packet(first.application_id).answer_for("first_name") is None


@pytest.mark.parametrize("sticky_message", [False, True])
def test_a_rejection_persists_and_a_correction_from_another_process_is_used(
    isolated_imx_home, fictional_candidate, sticky_message
):
    form = _form()
    again = _page(_shown_again(form, "first_name", "Enter a valid name"))
    script = Script(pages=[_page(form)], confirm=[REJECTED], inspect_pages=[again])
    first = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script)
                        .apply(URL, candidate_id="c1"))
    assert first.state is S.NEEDS_INPUT
    [question] = first.missing_inputs
    assert question.field_id == "first_name" and "rejected" in question.prompt
    with _store(isolated_imx_home) as store:
        app_id = first.application_id
        assert [a.outcome for a in store.list_attempts(app_id)] == [SubmissionOutcome.NOT_SUBMITTED]
        rejections, _ = rejection_epochs(store, app_id)
        assert set(rejections) == {(0, "first_name", form.field("first_name").fingerprint)}
        # `interviewmaxxing answer` in another process
        claim = store.claim(app_id, "answer-cli")
        store.save_user_inputs(claim, [UserInput.answering(question, TextValue(text="Avery"))])
        store.release(claim)

    # A new process accepts the correction whether or not the old message is visible.
    sticky = Script(pages=[again if sticky_message else _page(form)])
    second = asyncio.run(_runner(isolated_imx_home, fictional_candidate, sticky).resume(app_id))
    assert second.state is S.SUBMITTED, second.message
    assert sticky.calls.count("submit") == 1
    with _store(isolated_imx_home) as store:
        packet = store.latest_packet(app_id)
        assert packet is not None
        answer = packet.answer_for("first_name")
        assert answer is not None and answer.provenance.source is AnswerSource.USER_INPUT
        assert answer.value == TextValue(text="Avery")
        events = [e.event for e in store.list_events(app_id)]
        assert events.count(REJECTION_EVENT) == 1  # the stale message was not a new rejection


def test_a_correction_the_site_rejects_again_is_asked_again(isolated_imx_home, fictional_candidate):
    form = _form()
    again = _page(_shown_again(form, "first_name", "Enter a valid name"))
    script = Script(pages=[_page(form)], confirm=[REJECTED, REJECTED, ACCEPTED],
                    inspect_pages=[again, again])
    interaction = CountingAnswering("Avery")
    outcome = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script, interaction=interaction,
                                  limits=RunLimits(max_steps=8, max_same_form=4))
                          .apply(URL, candidate_id="c1"))
    assert outcome.state is S.SUBMITTED, outcome.message
    assert interaction.asked == 2 and script.calls.count("submit") == 3
    with _store(isolated_imx_home) as store:
        assert [a.outcome for a in store.list_attempts(outcome.application_id)] == [
            SubmissionOutcome.NOT_SUBMITTED, SubmissionOutcome.NOT_SUBMITTED, SubmissionOutcome.ACCEPTED]
        events = [e.event for e in store.list_events(outcome.application_id)]
        assert events.count(REJECTION_EVENT) == 2


def test_a_validation_message_on_a_fresh_open_is_not_a_rejection(isolated_imx_home, fictional_candidate):
    """Nothing was posted in this run, so a message the site shows on first sight is
    stale (a draft's old value); the profile answer is filled and submitted."""
    again = _page(_shown_again(_form(), "first_name", "Enter a valid name"))
    script = Script(pages=[again])
    outcome = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script)
                          .apply(URL, candidate_id="c1"))
    assert outcome.state is S.SUBMITTED and script.calls.count("submit") == 1


# --- browser start failure; interrupted submit guidance (I1R) ------------------------------------


@pytest.mark.parametrize("profile_problem", ["removed", "corrupt"])
@pytest.mark.parametrize("initial_state", [S.REQUESTED, S.NEEDS_INPUT, S.FAILED_RETRYABLE])
def test_profile_failure_after_admission_is_durable_and_retryable(
    isolated_imx_home, fictional_candidate, profile_problem, initial_state
):
    paths = isolated_imx_home
    profile = fictional_candidate.model_copy(update={"id": "c1"})
    profile_path = paths.profile_dir / "c1" / "profile.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(profile.model_dump_json())
    candidates = LocalCandidateStore.from_paths(paths)
    assert candidates.load("c1").id == "c1"  # valid when the request is admitted
    with _store(paths) as store:
        app = store.record_request("c1", URL).application
        store.pin_resume(app.id, profile.resume)
        if initial_state is not S.REQUESTED:
            claim = store.claim(app.id, "admission")
            if initial_state is S.NEEDS_INPUT:
                store.transition(claim, initial_state, metadata={"reason": "answer needed"})
            else:
                store.transition(claim, initial_state, failure_reason="Previous browser failure")
            store.release(claim)
    if profile_problem == "removed":
        profile_path.unlink()
    else:
        profile_path.write_text("{not valid JSON")

    script = Script()
    runner = LocalApplicationRunner(paths=paths, interaction=NoninteractiveInteraction(),
                                    candidates=candidates, browser_factory=ScriptedFactory(script))
    result = asyncio.run(runner.resume(app.id))
    assert result.state is S.FAILED_RETRYABLE
    assert "Candidate profile unavailable" in result.message
    assert "profile.json" in result.message and str(profile_path) not in result.message
    assert script.starts == 0
    with _store(paths) as store:
        saved = store.get_application(app.id)
        assert saved.state is S.FAILED_RETRYABLE and saved.failure_reason == result.message
        assert saved.claim_owner is None
        assert store.pinned_resume(app.id) == profile.resume
        assert store.list_attempts(app.id) == [] and store.get_receipt(app.id) is None
        failures = [e for e in store.list_events(app.id) if e.event == "application.failed_retryable"]
        assert failures and failures[-1].metadata["failure_reason"] == result.message


@pytest.mark.parametrize("blocked_state", [S.SUBMITTED, S.SUBMITTING, S.SUBMISSION_UNKNOWN])
def test_missing_profile_does_not_replace_submission_state(isolated_imx_home, blocked_state):
    paths = isolated_imx_home
    with _store(paths) as store:
        app = store.record_request("c1", URL).application
        claim = store.claim(app.id, "original-run")
        store.bind_job_identity(claim, IDENTITY)
        for state in (S.INSPECTING, S.PACKET_READY, S.FILLING):
            store.transition(claim, state)
        attempt = store.begin_submission(claim)
        if blocked_state is not S.SUBMITTING:
            observation = ACCEPTED if blocked_state is S.SUBMITTED else SubmissionObservation(
                outcome=SubmissionOutcome.UNKNOWN, signals=["No confirmation"])
            store.record_submission_outcome(claim, attempt.id, observation)
            store.release(claim)
        receipt = store.get_receipt(app.id)
        failure_reason = store.get_application(app.id).failure_reason
        events = store.list_events(app.id)
    script = Script()
    runner = LocalApplicationRunner(paths=paths, interaction=NoninteractiveInteraction(),
                                    browser_factory=ScriptedFactory(script))
    result = asyncio.run(runner.resume(app.id))
    assert result.state is blocked_state and script.starts == 0
    with _store(paths) as store:
        assert store.get_application(app.id).failure_reason == failure_reason
        assert store.get_receipt(app.id) == receipt
        assert store.list_events(app.id) == events
        assert len(store.list_attempts(app.id)) == 1


class Boom:
    async def start(self, options: BrowserOptions) -> ScriptedBrowser:
        raise RuntimeError("Executable doesn't exist at /fictional/chrome")


def test_a_browser_that_cannot_start_is_a_retryable_outcome_not_a_traceback(
    isolated_imx_home, fictional_candidate
):
    runner = LocalApplicationRunner(paths=isolated_imx_home, interaction=NoninteractiveInteraction(),
                                    headless=True, browser_factory=Boom(),
                                    candidates=Candidates(fictional_candidate), prepare_only=False)
    outcome = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert outcome.state is S.FAILED_RETRYABLE
    assert "Could not start the browser" in outcome.message and "playwright install" in outcome.message
    with _store(isolated_imx_home) as store:
        app = store.get_application(outcome.application_id)
        assert app.claim_owner is None and "Executable" in (app.failure_reason or "")
    script = Script()
    assert asyncio.run(_runner(isolated_imx_home, fictional_candidate, script)
                       .resume(outcome.application_id)).state is S.SUBMITTED


def test_a_browser_that_cannot_start_leaves_an_uncertain_submission_unknown(
    isolated_imx_home, fictional_candidate
):
    app_id = _unknown(isolated_imx_home, fictional_candidate)
    runner = LocalApplicationRunner(paths=isolated_imx_home, interaction=NoninteractiveInteraction(),
                                    headless=True, browser_factory=Boom(),
                                    candidates=Candidates(fictional_candidate))
    result = asyncio.run(runner.reconcile(app_id))
    assert result.state is S.SUBMISSION_UNKNOWN and "Could not start the browser" in result.message
    with _store(isolated_imx_home) as store:
        assert len(store.list_attempts(app_id)) == 1 and store.get_receipt(app_id) is None
        assert store.get_application(app_id).claim_owner is None


def test_an_interrupted_submit_is_left_alone_while_leased_then_settled_for_reconcile(
    isolated_imx_home, fictional_candidate, clock
):
    isolated_imx_home.ensure()
    with _store(isolated_imx_home, clock) as store:
        app = store.record_request("c1", URL).application
        claim = store.claim(app.id, "cli:other-host:4242")
        for state in (S.INSPECTING, S.PACKET_READY, S.FILLING):
            store.transition(claim, state)
        store.begin_submission(claim)  # that process dies here; its lease lasts 10 min
    script = Script()
    live = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script, clock=clock)
                       .apply(URL, candidate_id="c1"))
    assert live.state is S.SUBMITTING and "in progress in another run" in live.message
    assert "cli:other-host:4242" in live.message and script.starts == 0

    clock.advance(minutes=11)  # the lease lapses: settle it, never retry it
    lapsed = asyncio.run(_runner(isolated_imx_home, fictional_candidate, script, clock=clock)
                         .apply(URL, candidate_id="c1"))
    assert lapsed.state is S.SUBMISSION_UNKNOWN and "reconcile" in lapsed.message
    assert asyncio.run(_runner(isolated_imx_home, fictional_candidate, script, clock=clock)
                       .resume(app.id)).state is S.SUBMISSION_UNKNOWN
    assert script.starts == 0
    with _store(isolated_imx_home, clock) as store:
        assert [a.outcome for a in store.list_attempts(app.id)] == ["INTERRUPTED"]


# --- lookups the site could not commit: one choice round, never a failed fill (WP2) ---------------

TEXAS = "Austin, Texas, United States"
MINNESOTA = "Austin, Minnesota, United States"
SUGGESTIONS = [MINNESOTA, TEXAS]


def _austin(candidate: CandidateProfile) -> CandidateProfile:
    identity = candidate.identity
    address = identity.address.model_copy(update={"city": "Austin", "region": "TX",
                                                  "country": "United States"})
    return candidate.model_copy(update={"identity": identity.model_copy(update={"address": address})})


def _lookup_field(field_id: str = "location", *, required: bool = True,
                  semantic: SemanticType = SemanticType.LOCATION) -> ApplicationField:
    return ApplicationField(id=field_id, label=f"{field_id.title()} (search)", selector=f"#{field_id}",
                            semantic_type=semantic, control_type=ControlType.TYPEAHEAD,
                            required=required)


def _lookup_form(*extra: ApplicationField, required: bool = True) -> ApplicationForm:
    base = _form()
    return base.model_copy(update={"fields": [*base.fields, _lookup_field(required=required),
                                              *extra]})


class LookupBrowser(ScriptedBrowser):
    """A lookup commits only when the typed text is exactly one of ``commits``;
    otherwise it reports NEEDS_CHOICE with ``suggestions`` and stays empty."""

    def __init__(self, script: Script, *, commits: set[str], suggestions: list[str],
                 failing: frozenset[str] = frozenset(), typed: list[dict[str, str]]) -> None:
        super().__init__(script)
        self.commits, self.suggestions, self.failing = commits, suggestions, failing
        self.typed = typed

    def _results(self, form: ApplicationForm, packet: ApplicationPacket,
                 only: set[str] | None = None) -> FillResult:
        typed: dict[str, str] = {}
        results = []
        for answer in packet.answers:
            if only is not None and answer.field_id not in only:
                continue
            field = form.field(answer.field_id)
            if answer.field_id in self.failing:
                results.append(FieldFillResult(field_id=field.id, status=FieldFillStatus.FAILED,
                                               detail="scripted failure"))
            elif field.control_type is ControlType.TYPEAHEAD:
                assert isinstance(answer.value, TextValue)
                typed[field.id] = answer.value.text
                if answer.value.text in self.commits:
                    results.append(FieldFillResult(field_id=field.id, status=FieldFillStatus.FILLED))
                else:
                    results.append(FieldFillResult(field_id=field.id,
                                                   status=FieldFillStatus.NEEDS_CHOICE,
                                                   suggestions=self.suggestions))
            else:
                results.append(FieldFillResult(field_id=field.id, status=FieldFillStatus.FILLED))
        self.typed.append(typed)
        return FillResult(form_step=form.step, fields=results)

    async def fill(self, form: ApplicationForm, packet: ApplicationPacket) -> FillResult:
        self.s.calls.append("fill")
        assert packet.problems_against(form) == []
        return self._results(form, packet)


class SelectiveLookupBrowser(LookupBrowser):
    async def fill_fields(self, form: ApplicationForm, packet: ApplicationPacket,
                          field_ids: Sequence[str]) -> FillResult:
        self.s.calls.append("fill_fields:" + ",".join(field_ids))
        assert packet.problems_against(form) == []
        return self._results(form, packet, set(field_ids))


class LookupFactory(ScriptedFactory):
    def __init__(self, script: Script, browser: type[LookupBrowser] = LookupBrowser, *,
                 commits: frozenset[str] = frozenset({TEXAS}), suggestions: list[str] | None = None,
                 failing: frozenset[str] = frozenset()) -> None:
        super().__init__(script)
        self.browser, self.commits, self.failing = browser, set(commits), failing
        self.suggestions = SUGGESTIONS if suggestions is None else suggestions
        self.typed: list[dict[str, str]] = []

    async def start(self, options: BrowserOptions) -> LookupBrowser:  # type: ignore[override]
        self.script.starts += 1
        self.options.append(options)
        return self.browser(self.script, commits=self.commits, suggestions=self.suggestions,
                            failing=self.failing, typed=self.typed)


class Chooser(FactualPacketResolver):
    """A resolver whose model picks ``pick`` (None: no suggestion is clearly right)."""

    def __init__(self, pick: str | None) -> None:
        super().__init__()
        self.pick = pick
        self.calls: list[tuple[str, str, list[str]]] = []

    async def choose_suggestion(self, context, field, typed_value, suggestions):  # type: ignore[no-untyped-def]
        assert context.form.find(field.id) == field
        self.calls.append((field.id, typed_value, list(suggestions)))
        return self.pick

    def suggestion_decision(self, field_id: str) -> dict[str, object]:
        return {"stage": "suggestion_choice", "choice": "s1", "confidence": 0.99}


def _lookup_runner(paths, candidate, factory, *, resolver=None, interaction=None,
                   prepare_only: bool = True) -> LocalApplicationRunner:
    return LocalApplicationRunner(
        paths=paths, interaction=interaction or NoninteractiveInteraction(), headless=True,
        browser_factory=factory, candidates=Candidates(candidate), resolver=resolver,
        limits=RunLimits(max_steps=6, max_same_form=2), prepare_only=prepare_only)


@pytest.mark.parametrize("browser", [SelectiveLookupBrowser, LookupBrowser])
def test_a_chosen_suggestion_is_typed_verbatim_and_the_step_completes(
    isolated_imx_home, fictional_candidate, browser
):
    script = Script(pages=[_page(_lookup_form())])
    factory = LookupFactory(script, browser)
    chooser = Chooser(TEXAS)
    result = asyncio.run(_lookup_runner(isolated_imx_home, _austin(fictional_candidate), factory,
                                        resolver=chooser).apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and "Prepared to the final review step" in result.message
    assert result.missing_inputs == [] and "submit" not in script.calls
    assert chooser.calls == [("location", "Austin, TX", SUGGESTIONS)]
    if browser is SelectiveLookupBrowser:  # only the chosen lookup is filled again
        assert script.calls.count("fill") == 1 and "fill_fields:location" in script.calls
        assert factory.typed == [{"location": "Austin, TX"}, {"location": TEXAS}]
    else:  # without selective fill the whole packet is filled again
        assert script.calls.count("fill") == 2
        assert factory.typed[-1] == {"location": TEXAS}
    with _store(isolated_imx_home) as store:
        packet = store.latest_packet(result.application_id)
        answer = packet.answer_for("location")
        assert answer.value == TextValue(text=TEXAS)
        assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY
        assert answer.provenance.note == ("verified identity: location; site suggestion chosen "
                                          "for the typed value 'Austin, TX'")
        [event] = [e for e in store.list_events(result.application_id)
                   if e.event == SUGGESTION_EVENT]
        assert event.metadata["chosen_label"] == TEXAS
        assert event.metadata["field_id"] == "location" and event.metadata["form_step"] == 0
        assert event.metadata["source"] == "PROFILE_IDENTITY"
        assert event.metadata["decision"] == {"stage": "suggestion_choice", "choice": "s1",
                                              "confidence": 0.99}
        assert any(e.event == "preparation.ready" for e in store.list_events(result.application_id))


def test_without_a_choice_the_user_picks_a_suggestion_which_is_typed_verbatim(
    isolated_imx_home, fictional_candidate
):
    script = Script(pages=[_page(_lookup_form())])
    factory = LookupFactory(script)
    candidate = _austin(fictional_candidate)
    first = asyncio.run(_lookup_runner(isolated_imx_home, candidate, factory,
                                       resolver=FactualPacketResolver()).apply(URL, candidate_id="c1"))
    assert first.state is S.NEEDS_INPUT and "Could not fill" not in first.message
    [question] = first.missing_inputs
    assert (question.field_id, question.reason) == ("location", MissingReason.NO_ANSWER)
    assert question.control_type is ControlType.TYPEAHEAD and question.required
    assert [o.label for o in question.options or []] == SUGGESTIONS
    assert [o.value for o in question.options or []] == SUGGESTIONS
    assert "'Austin, TX'" in question.prompt and TEXAS in question.prompt
    with _store(isolated_imx_home) as store:
        [pending] = pending_inputs(store, first.application_id)
        assert pending == question
        assert store.get_application(first.application_id).state is S.NEEDS_INPUT
        assert not [e for e in store.list_events(first.application_id) if e.event == SUGGESTION_EVENT]
        claim = store.claim(first.application_id, "answer-cli")
        store.save_user_inputs(claim, [UserInput.answering(pending, TextValue(text=TEXAS))])
        store.release(claim)
    second = asyncio.run(_lookup_runner(isolated_imx_home, candidate, factory,
                                        resolver=FactualPacketResolver())
                         .resume(first.application_id))
    assert second.state is S.NEEDS_INPUT and "Prepared to the final review step" in second.message
    assert factory.typed[-1] == {"location": TEXAS}
    with _store(isolated_imx_home) as store:
        answer = store.latest_packet(first.application_id).answer_for("location")
        assert answer.value == TextValue(text=TEXAS)
        assert answer.provenance.source is AnswerSource.USER_INPUT


def test_an_interactive_user_pick_completes_the_step_in_the_same_run(
    isolated_imx_home, fictional_candidate
):
    class PicksTexas(NoninteractiveInteraction):
        def __init__(self) -> None:
            super().__init__()
            self.asked: list[MissingInput] = []

        async def request_inputs(self, missing):
            self.asked.extend(missing)
            return [UserInput.answering(m, TextValue(text=TEXAS)) for m in missing]

    script = Script(pages=[_page(_lookup_form())])
    factory = LookupFactory(script)
    interaction = PicksTexas()
    result = asyncio.run(_lookup_runner(isolated_imx_home, _austin(fictional_candidate), factory,
                                        resolver=Chooser(None), interaction=interaction)
                         .apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and "Prepared to the final review step" in result.message
    assert [m.field_id for m in interaction.asked] == ["location"]
    assert factory.typed == [{"location": "Austin, TX"}, {"location": TEXAS}]


def test_a_label_that_still_does_not_commit_gets_no_second_round(
    isolated_imx_home, fictional_candidate
):
    script = Script(pages=[_page(_lookup_form())])
    factory = LookupFactory(script, SelectiveLookupBrowser, commits=frozenset())
    chooser = Chooser(TEXAS)
    result = asyncio.run(_lookup_runner(isolated_imx_home, _austin(fictional_candidate), factory,
                                        resolver=chooser).apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and "Could not fill" not in result.message
    assert len(chooser.calls) == 1
    [question] = result.missing_inputs
    assert question.field_id == "location" and f"'{TEXAS}'" in question.prompt
    assert [o.label for o in question.options or []] == SUGGESTIONS
    with _store(isolated_imx_home) as store:
        app = store.get_application(result.application_id)
        assert app.state is S.NEEDS_INPUT and app.failure_reason is None


@pytest.mark.parametrize("pick", ["Austin, Nowhere", None])
def test_a_pick_that_is_not_an_observed_suggestion_is_never_typed(
    isolated_imx_home, fictional_candidate, pick
):
    script = Script(pages=[_page(_lookup_form())])
    factory = LookupFactory(script, SelectiveLookupBrowser)
    result = asyncio.run(_lookup_runner(isolated_imx_home, _austin(fictional_candidate), factory,
                                        resolver=Chooser(pick)).apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and [m.field_id for m in result.missing_inputs] == ["location"]
    assert factory.typed == [{"location": "Austin, TX"}]
    assert not any(call.startswith("fill_fields") for call in script.calls)


def test_an_optional_lookup_without_a_choice_is_left_blank(isolated_imx_home, fictional_candidate):
    script = Script(pages=[_page(_lookup_form(required=False))])
    factory = LookupFactory(script, SelectiveLookupBrowser)
    result = asyncio.run(_lookup_runner(isolated_imx_home, _austin(fictional_candidate), factory,
                                        resolver=Chooser(None)).apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and "Prepared to the final review step" in result.message
    assert result.missing_inputs == []
    with _store(isolated_imx_home) as store:
        packet = store.latest_packet(result.application_id)
        assert packet.answer_for("location") is None and packet.is_complete


def test_needs_choice_beside_a_real_failure_reports_only_the_failure(
    isolated_imx_home, fictional_candidate
):
    script = Script(pages=[_page(_lookup_form())])
    factory = LookupFactory(script, failing=frozenset({"first_name"}))
    chooser = Chooser(TEXAS)
    result = asyncio.run(_lookup_runner(isolated_imx_home, _austin(fictional_candidate), factory,
                                        resolver=chooser).apply(URL, candidate_id="c1"))
    assert result.state is S.FAILED_RETRYABLE
    assert result.message == "Could not fill first_name reliably; nothing was submitted."
    assert chooser.calls == []


def test_a_choice_is_reapplied_after_the_user_answers_another_lookup(
    isolated_imx_home, fictional_candidate
):
    school = SavedAnswer(id="sa.school", scope="GLOBAL", semantic_type=SemanticType.UNIVERSITY,
                         question="School (search)", value="Fictional State",
                         confirmed_at="2026-09-01T12:00:00Z")
    candidate = _austin(fictional_candidate)
    candidate = candidate.model_copy(update={"saved_answers": [*candidate.saved_answers, school]})
    form = _lookup_form(_lookup_field("school", semantic=SemanticType.UNIVERSITY))
    script = Script(pages=[_page(form)])
    factory = LookupFactory(script, SelectiveLookupBrowser,
                            commits=frozenset({TEXAS, "Fictional State University"}))

    class SelectiveChooser(Chooser):
        async def choose_suggestion(self, context, field, typed_value, suggestions):  # type: ignore[no-untyped-def]
            self.calls.append((field.id, typed_value, list(suggestions)))
            return TEXAS if field.id == "location" else None

    class PicksSchool(NoninteractiveInteraction):
        async def request_inputs(self, missing):
            return [UserInput.answering(m, TextValue(text="Fictional State University"))
                    for m in missing]

    chooser = SelectiveChooser(None)
    result = asyncio.run(_lookup_runner(isolated_imx_home, candidate, factory, resolver=chooser,
                                        interaction=PicksSchool()).apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and "Prepared to the final review step" in result.message
    assert [call[0] for call in chooser.calls] == ["location", "school"]  # one round each
    assert factory.typed[0] == {"location": "Austin, TX", "school": "Fictional State"}
    # After the user's pick the step is filled again with the remembered choice.
    assert factory.typed[-1] == {"location": TEXAS, "school": "Fictional State University"}
    with _store(isolated_imx_home) as store:
        packet = store.latest_packet(result.application_id)
        assert packet.answer_for("location").value == TextValue(text=TEXAS)
        assert packet.answer_for("school").provenance.source is AnswerSource.USER_INPUT
