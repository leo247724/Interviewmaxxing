"""LocalApplicationRunner control flow with a scripted browser.

The real store, the real factual resolver (C3) and a fictional candidate are used;
only the browser is scripted, to force cases a live page cannot produce on demand:
cancellation mid-submit, loops, ambiguous controls, lock and claim contention,
threads. Real-Chromium coverage of the same runner is in ``e2e/``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest

from interviewmaxxing_browser import AmbiguousAction
from interviewmaxxing_browser.ai import AIFormRouter, BoundedDecisions, DynamicPacketResolver
from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_cli.batch import provider_cost
from interviewmaxxing_cli.runner import (
    PROVIDER_EVENT,
    REJECTION_EVENT,
    SUGGESTION_EVENT,
    LocalApplicationRunner,
    NoninteractiveInteraction,
    RunLimits,
    browser_profile_lock,
    pending_inputs,
    redact_detail,
    rejection_epochs,
)
from interviewmaxxing_core import (
    AnswerReuse,
    AnswerSource,
    ApplicationEvent,
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
    FieldOption,
    FillResult,
    IdentityEvidenceKind,
    JobIdentityObservation,
    MissingInput,
    MissingReason,
    NavigationResult,
    NotSubmittedNext,
    PacketContext,
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
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient

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


# --- round 2: reworded saved answers and fact-grounded screeners through the runner (WP2) ----

class ScriptedJev:
    """Jev for runner tests: every field is a literal COPY_KNOWN question with the scope
    given per field index; named questions answer from ``choices`` and nouls from ``noul``.
    Anything unscripted holds."""

    def __init__(self, scopes: dict[int, str], choices: dict[str, dict[str, float]],
                 noul: Callable[[str, dict[str, Any]], float] | None = None) -> None:
        self.scopes, self.choices = scopes, choices
        self.noul = noul or (lambda name, state: 1.0)
        self.requests: list[dict[str, Any]] = []

    def asked(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if name in r["questions"]]

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        self.requests.append(request)
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": self.noul(name, request["state"])}
                continue
            criteria = list(question["criteria"])
            confidence = 0.99
            if name[0] in "rnusd" and name[1:].isdigit():
                choice = {"r": "COPY_KNOWN", "n": "literal",
                          "u": self.scopes.get(int(name[1:]), "APPLICANT_CURRENT"),
                          "s": "CUSTOM_BOOLEAN", "d": "APPLICATION_ATTACHMENT"}[name[0]]
                probabilities = {key: float(key == choice) for key in criteria}
                confidence = 1.0
            elif name in self.choices:
                probabilities = {key: self.choices[name].get(key, 0.0) for key in criteria}
            else:
                held = "hold" if "hold" in criteria else "NONE" if "NONE" in criteria else criteria[0]
                probabilities = {key: float(key == held) for key in criteria}
            choice = max(probabilities, key=probabilities.__getitem__)
            answers[name] = {"type": "choice", "choice": choice, "confidence": confidence,
                             "probabilities": probabilities}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())


def _dynamic_runner(paths, candidate: CandidateProfile, script: Script,
                    jev: ScriptedJev) -> LocalApplicationRunner:
    decisions = BoundedDecisions(JevClient(ApiKey("synthetic-runner-key", source="test"),
                                           transport=jev, max_attempts=1))
    return LocalApplicationRunner(
        paths=paths, interaction=NoninteractiveInteraction(), headless=True,
        browser_factory=ScriptedFactory(script), candidates=Candidates(candidate),
        resolver=DynamicPacketResolver(decisions, router=AIFormRouter(decisions)),
        limits=RunLimits(max_steps=6, max_same_form=2), prepare_only=True)


def _stored_packet(paths, app_id: str, form: ApplicationForm,
                   candidate: CandidateProfile) -> tuple[ApplicationPacket, list[str]]:
    """The latest saved packet and everything its canonical context finds wrong with it."""
    with _store(paths) as store:
        app = store.get_application(app_id)
        packet = store.latest_packet(app_id)
        assert packet is not None
        ctx = PacketContext(application=app, job=store.get_job(app.job_id), form=form,
                            candidate=candidate.model_copy(update={"id": app.candidate_id}),
                            user_inputs=store.get_user_inputs(app_id, form))
        return packet, ctx.problems(packet)


def test_runner_fills_a_reworded_sponsorship_question_from_the_global_saved_answer(
    isolated_imx_home, fictional_candidate
):
    sponsorship = ApplicationField(
        id="sponsorship", label="Will you require sponsorship in the future?",
        selector="#sponsorship", semantic_type=SemanticType.SPONSORSHIP,
        control_type=ControlType.RADIO, required=True,
        options=[FieldOption(value="yes", label="Yes, I will require sponsorship"),
                 FieldOption(value="no", label="No, I will not require sponsorship")])
    form = _form().model_copy(update={"fields": [*_form().fields, sponsorship]})
    jev = ScriptedJev({0: "APPLICANT_CURRENT", 1: "EXPLICIT_ANSWER"}, {
        "wording": {"q0": 0.98, "NONE": 0.02},
        "equivalent_0": {"o0": 0.005, "o1": 0.99, "NONE": 0.005}})
    script = Script(pages=[_page(form)])
    result = asyncio.run(_dynamic_runner(isolated_imx_home, fictional_candidate, script, jev)
                         .apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and "Prepared to the final review step" in result.message
    assert result.missing_inputs == [] and "submit" not in script.calls
    assert jev.asked("wording") and jev.asked("equivalent_0")
    packet, problems = _stored_packet(isolated_imx_home, result.application_id, form,
                                      fictional_candidate)
    assert problems == [] and packet.is_complete
    answer = packet.answer_for("sponsorship")
    assert answer is not None
    assert (answer.value.value, answer.value.label) == ("no", "No, I will not require sponsorship")
    assert answer.provenance.source is AnswerSource.SAVED_ANSWER
    assert answer.provenance.reference_ids == ["sa.sponsorship"]
    assert "question wording mapped by Jev" in (answer.provenance.note or "")
    first_name = packet.answer_for("first_name")
    assert first_name is not None and first_name.provenance.source is AnswerSource.PROFILE_IDENTITY


def test_runner_answers_a_yes_no_experience_screener_from_verified_facts(
    isolated_imx_home, fictional_candidate
):
    agency = fictional_candidate.verified_facts()[0].model_copy(update={
        "id": "fact.agency", "key": "employment",
        "value": "SEO specialist at Fictional Search Agency, a digital marketing agency",
        "evidence": ["SEO specialist at Fictional Search Agency, a digital marketing agency"]})
    candidate = fictional_candidate.model_copy(update={"facts": [*fictional_candidate.facts, agency]})
    screener = ApplicationField(
        id="agency", label="Do you have experience working at a digital marketing agency?",
        selector="#agency", semantic_type=SemanticType.CUSTOM_BOOLEAN,
        control_type=ControlType.RADIO, required=True,
        options=[FieldOption(value="1", label="Yes"), FieldOption(value="0", label="No")])
    form = _form().model_copy(update={"fields": [*_form().fields, screener]})

    def noul(name: str, state: dict[str, Any]) -> float:
        if "canonical_alternatives" in state:  # consistency with the other verified facts
            return 1.0
        if name.startswith("has_"):
            return 1.0 if state["facts"][name.removeprefix("has_")]["id"] == "fact.agency" else 0.0
        if name.startswith("lacks_"):
            return 0.0
        return 1.0

    jev = ScriptedJev({0: "APPLICANT_CURRENT", 1: "HISTORICAL_OR_CONTEXTUAL"},
                      {"experience": {"YES": 0.99, "UNKNOWN": 0.01}}, noul)
    script = Script(pages=[_page(form)])
    result = asyncio.run(_dynamic_runner(isolated_imx_home, candidate, script, jev)
                         .apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and "Prepared to the final review step" in result.message
    assert result.missing_inputs == [] and "submit" not in script.calls
    [request] = jev.asked("experience")
    assert set(request["questions"]["experience"]["criteria"]) == {
        "YES", "NO", "UNKNOWN", "NOT_EXPERIENCE"}
    packet, problems = _stored_packet(isolated_imx_home, result.application_id, form, candidate)
    assert problems == [] and packet.is_complete
    answer = packet.answer_for("agency")
    assert answer is not None
    assert (answer.value.value, answer.value.label) == ("1", "Yes")
    assert answer.provenance.source is AnswerSource.GENERATED_FROM_FACTS
    assert answer.provenance.reference_ids == ["fact.agency"]


def test_runner_answers_a_residence_question_from_the_verified_address(
    isolated_imx_home, fictional_candidate
):
    residence = ApplicationField(
        id="residence", label="Do you currently live in the United States?",
        selector="#residence", semantic_type=SemanticType.COUNTRY,
        control_type=ControlType.RADIO, required=True,
        options=[FieldOption(value="yes", label="Yes"), FieldOption(value="no", label="No")])
    form = _form().model_copy(update={"fields": [*_form().fields, residence]})
    jev = ScriptedJev({0: "APPLICANT_CURRENT", 1: "APPLICANT_CURRENT"},
                      {"residence": {"o0": 0.99, "o1": 0.0, "UNKNOWN": 0.01}})
    script = Script(pages=[_page(form)])
    result = asyncio.run(_dynamic_runner(isolated_imx_home, fictional_candidate, script, jev)
                         .apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and "Prepared to the final review step" in result.message
    assert result.missing_inputs == [] and "submit" not in script.calls
    [request] = jev.asked("residence")
    assert request["state"]["applicant_address"] == {
        "city": "Springfield", "region": "OR", "country": "United States"}
    assert not jev.asked("equivalent_0")
    packet, problems = _stored_packet(isolated_imx_home, result.application_id, form,
                                      fictional_candidate)
    assert problems == [] and packet.is_complete
    answer = packet.answer_for("residence")
    assert answer is not None
    assert (answer.value.value, answer.value.label) == ("yes", "Yes")
    assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY
    assert answer.provenance.note == "verified identity address; residence question answered by Jev"


# --- round 4: per-application provider cost (deliverable 3) --------------------------------

PREPARED = ("Prepared to the final review step. Nothing was submitted. Submission remains "
            "disabled when this application is resumed.")
RESIDENCE = ApplicationField(
    id="residence", label="Do you currently live in the United States?",
    selector="#residence", semantic_type=SemanticType.COUNTRY,
    control_type=ControlType.RADIO, required=True,
    options=[FieldOption(value="yes", label="Yes"), FieldOption(value="no", label="No")])
RESIDENCE_SCOPES = {0: "APPLICANT_CURRENT", 1: "APPLICANT_CURRENT"}
RESIDENCE_CHOICES = {"residence": {"o0": 0.99, "o1": 0.0, "UNKNOWN": 0.01}}
USAGE_KEYS = {"calls", "known_cost_usd", "unknown_cost_calls", "latency_seconds"}
"""One usage bucket of a ``provider.budget`` event: metadata only, never prompts or values."""


def _residence_form(*, final: bool = True) -> ApplicationForm:
    """First name plus a residence radio: one full-form routing call and one residence
    screener call to Jev."""
    form = _form(final=final)
    return form.model_copy(update={"fields": [*form.fields, RESIDENCE]})


class UncostedJev(ScriptedJev):
    """``ScriptedJev`` that reports no cost for requests asking the ``uncosted`` question."""

    def __init__(self, *args: Any, uncosted: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.uncosted = uncosted

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        response = super().__call__(url, headers, body, timeout)
        if self.uncosted not in self.requests[-1]["questions"]:
            return response
        payload = json.loads(response.body)
        del payload["usage"]
        return HttpResponse(response.status, response.headers, json.dumps(payload).encode())


def _provider_events(paths, app_id: str) -> list[ApplicationEvent]:
    with _store(paths) as store:
        return [e for e in store.list_events(app_id) if e.event == PROVIDER_EVENT]


def _cost_suffix(usage: dict[str, Any]) -> str:
    return f" Provider cost: USD {usage['known_cost_usd']:.4f} for {usage['calls']} call(s)."


def test_a_prepared_run_records_its_provider_cost_once_and_reports_it(
    isolated_imx_home, fictional_candidate
):
    jev = ScriptedJev(RESIDENCE_SCOPES, RESIDENCE_CHOICES)
    runner = _dynamic_runner(isolated_imx_home, fictional_candidate,
                             Script(pages=[_page(_residence_form())]), jev)
    result = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and result.missing_inputs == []
    assert result.message.startswith("Prepared to the final review step")
    receipts = runner.resolver.decisions.budget.receipts
    assert len(receipts) == len(jev.requests) >= 2 and jev.asked("residence")

    [event] = _provider_events(isolated_imx_home, result.application_id)
    usage = event.metadata
    assert set(usage) == USAGE_KEYS | {"by_purpose", "limits"}
    # A fixed test budget keeps its limits; production budgets scale with the form (round 6).
    assert usage["limits"] == {"max_calls": 48, "max_usd": 0.5}
    assert usage["calls"] == len(receipts) and usage["unknown_cost_calls"] == 0
    assert usage["known_cost_usd"] == round(sum(r.cost_usd for r in receipts), 6) \
        == round(0.0001 * len(receipts), 6)
    by_purpose = usage["by_purpose"]
    assert {"full_form_routes", "residence_screener"} <= set(by_purpose)
    assert all(set(bucket) == USAGE_KEYS for bucket in by_purpose.values())
    assert sum(bucket["calls"] for bucket in by_purpose.values()) == usage["calls"]
    assert "synthetic-runner-key" not in json.dumps(usage)
    assert result.message == PREPARED + _cost_suffix(usage)

    with _store(isolated_imx_home) as store:
        events = store.list_events(result.application_id)
    # Recorded under the run's claim, after the preparation and before the final stop; the
    # stored history (the NEEDS_INPUT reason included) never carries the cost text.
    [ready] = [e for e in events if e.event == "preparation.ready"]
    stop = [e for e in events if e.to_state is S.NEEDS_INPUT][-1]
    assert ready.sequence < event.sequence < stop.sequence and event.actor == stop.actor
    assert stop.metadata["reason"] == "prepared for final review; submission disabled"
    assert not any("Provider cost" in json.dumps(e.metadata) for e in events)
    assert provider_cost(isolated_imx_home, result.application_id) == (
        usage["known_cost_usd"], usage["calls"])


def test_a_run_with_the_factual_resolver_records_no_provider_cost(
    isolated_imx_home, fictional_candidate
):
    runner = _runner(isolated_imx_home, fictional_candidate, Script(), prepare_only=True)
    result = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and result.message == PREPARED
    assert _provider_events(isolated_imx_home, result.application_id) == []
    assert provider_cost(isolated_imx_home, result.application_id) == (None, None)


def test_a_provider_response_without_a_cost_is_counted_as_unknown(
    isolated_imx_home, fictional_candidate
):
    jev = UncostedJev(RESIDENCE_SCOPES, RESIDENCE_CHOICES, uncosted="residence")
    runner = _dynamic_runner(isolated_imx_home, fictional_candidate,
                             Script(pages=[_page(_residence_form())]), jev)
    result = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and result.missing_inputs == []
    [_] = jev.asked("residence")
    calls = len(jev.requests)
    assert [r.cost_usd for r in runner.resolver.decisions.budget.receipts].count(None) == 1

    [event] = _provider_events(isolated_imx_home, result.application_id)
    usage = event.metadata
    assert (usage["calls"], usage["unknown_cost_calls"]) == (calls, 1)
    assert usage["known_cost_usd"] == round(0.0001 * (calls - 1), 6)
    screener = usage["by_purpose"]["residence_screener"]
    assert (screener["calls"], screener["unknown_cost_calls"], screener["known_cost_usd"]) == \
        (1, 1, 0.0)
    assert usage["by_purpose"]["full_form_routes"]["unknown_cost_calls"] == 0
    assert result.message == (PREPARED + f" Provider cost: USD {usage['known_cost_usd']:.4f} "
                              f"for {calls} call(s), 1 without a reported cost.")


def test_a_failed_run_reports_its_provider_cost_but_not_in_the_failure_reason(
    isolated_imx_home, fictional_candidate
):
    script = Script(pages=[_page(_residence_form(final=False))],
                    advance=[RuntimeError("fictional page crash")])
    jev = ScriptedJev(RESIDENCE_SCOPES, RESIDENCE_CHOICES)
    runner = _dynamic_runner(isolated_imx_home, fictional_candidate, script, jev)
    result = asyncio.run(runner.apply(URL, candidate_id="c1"))
    reason = ("Stopped by a browser error (RuntimeError: fictional page crash). Nothing was "
              "submitted; resume to retry.")
    assert result.state is S.FAILED_RETRYABLE and "submit" not in script.calls
    [event] = _provider_events(isolated_imx_home, result.application_id)
    assert event.metadata["calls"] == len(jev.requests) >= 2
    assert result.message == reason + _cost_suffix(event.metadata)
    with _store(isolated_imx_home) as store:
        assert store.get_application(result.application_id).failure_reason == reason


def test_each_run_records_only_its_own_provider_calls(isolated_imx_home, fictional_candidate):
    jev = ScriptedJev(RESIDENCE_SCOPES, RESIDENCE_CHOICES)
    runner = _dynamic_runner(isolated_imx_home, fictional_candidate,
                             Script(pages=[_page(_residence_form())]), jev)
    first = asyncio.run(runner.apply(URL, candidate_id="c1"))
    app_id, before = first.application_id, len(jev.requests)
    assert first.message == PREPARED + _cost_suffix(
        _provider_events(isolated_imx_home, app_id)[0].metadata)

    # The same runtime on the same page: every decision comes from its cache, so this run
    # used no provider call, records no event and reports no cost.
    again = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert again.application_id == app_id and again.message == PREPARED
    assert len(jev.requests) == before
    assert len(_provider_events(isolated_imx_home, app_id)) == 1

    # The same runtime on a changed page: the event counts this run's calls, not the
    # runtime's earlier ones.
    changed = LocalApplicationRunner(
        paths=isolated_imx_home, interaction=NoninteractiveInteraction(), headless=True,
        browser_factory=ScriptedFactory(Script(pages=[_page(_form())])),
        candidates=Candidates(fictional_candidate), resolver=runner.resolver,
        limits=RunLimits(max_steps=6, max_same_form=2), prepare_only=True)
    third = asyncio.run(changed.apply(URL, candidate_id="c1"))
    new = len(jev.requests) - before
    assert third.application_id == app_id and new >= 1
    first_event, third_event = _provider_events(isolated_imx_home, app_id)
    assert third_event.metadata["calls"] == new
    assert third_event.metadata["known_cost_usd"] == round(0.0001 * new, 6)
    assert third.message == PREPARED + _cost_suffix(third_event.metadata)
    # The batch harness reads the application's cost over all of its runs.
    total = len(jev.requests)
    assert first_event.metadata["calls"] + new == total
    assert provider_cost(isolated_imx_home, app_id) == (round(0.0001 * total, 6), total)


def test_a_submitted_run_records_its_provider_cost_before_the_submission(
    isolated_imx_home, fictional_candidate
):
    # The terminal submission outcome releases the claim, so the cost is recorded first.
    jev = ScriptedJev(RESIDENCE_SCOPES, RESIDENCE_CHOICES)
    prepared = _dynamic_runner(isolated_imx_home, fictional_candidate,
                               Script(pages=[_page(_residence_form())]), jev)
    runner = LocalApplicationRunner(
        paths=isolated_imx_home, interaction=NoninteractiveInteraction(), headless=True,
        browser_factory=ScriptedFactory(Script(pages=[_page(_residence_form())])),
        candidates=Candidates(fictional_candidate), resolver=prepared.resolver,
        limits=RunLimits(max_steps=6, max_same_form=2), prepare_only=False)
    result = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert result.state is S.SUBMITTED
    [event] = _provider_events(isolated_imx_home, result.application_id)
    assert event.metadata["calls"] == len(jev.requests) >= 2
    assert result.message == ("Submitted; the site confirmed it. Receipt saved."
                              + _cost_suffix(event.metadata))
    with _store(isolated_imx_home) as store:
        events = store.list_events(result.application_id)
    submitting = next(e for e in events if e.to_state is S.SUBMITTING)
    assert event.sequence < submitting.sequence


def test_a_duplicate_found_after_provider_calls_still_records_the_cost(
    isolated_imx_home, fictional_candidate
):
    first = asyncio.run(_runner(isolated_imx_home, fictional_candidate, Script(),
                                prepare_only=True).apply(URL, candidate_id="c1"))
    # Step 1 carries no job identity, so it is routed (Jev calls) before step 2 shows the
    # identity the first application already holds.
    script = Script(pages=[_page(_residence_form(final=False), identity=None)],
                    advance=[NavigationResult(advanced=True, inspection=_page(_form(step=1)))])
    jev = ScriptedJev(RESIDENCE_SCOPES, RESIDENCE_CHOICES)
    runner = _dynamic_runner(isolated_imx_home, fictional_candidate, script, jev)
    result = asyncio.run(runner.apply(URL + "?source=fictional-board", candidate_id="c1"))
    assert result.application_id != first.application_id and jev.requests
    [event] = _provider_events(isolated_imx_home, result.application_id)
    assert event.metadata["calls"] == len(jev.requests)
    assert result.message == (f"This job already has application {first.application_id}; "
                              "not applying twice." + _cost_suffix(event.metadata))


# --- round 5: a step's lookups are decided together, one choice round each -------------------

LOOKUP_CHOICES: dict[str, list[str]] = {
    "location": SUGGESTIONS,
    "city": ["Austin, Minnesota", "Austin, Texas"],
    "state": ["Texas, United States", "Texas, USA"],
    "school": ["Fictional State College", "Fictional State University"],
}
"""The suggestions each lookup offers when the text typed into it does not commit."""
LOOKUP_ORDER = ("location", "city", "state")
"""The lookups of ``_three_lookups_form``, in form order."""
TYPED = {"location": "Austin, TX", "city": "Austin, TX", "state": "Texas"}
"""What the resolver types into each lookup from the ``_austin`` identity."""
RIGHT_LABELS = {"location": TEXAS, "city": "Austin, Texas", "state": "Texas, United States"}
"""For each lookup, the suggestion that denotes exactly what was typed."""
LOOKUP_S1 = {"s1": 0.99, "NONE": 0.01}
"""A Jev lookup decision for the second suggestion (the right one for location and city)."""


def _three_lookups_form(*, state_max_length: int | None = None) -> ApplicationForm:
    """First name, then required location, city and state lookups."""
    state = ApplicationField(id="state", label="State (search)", selector="#state",
                             semantic_type=SemanticType.STATE, control_type=ControlType.TYPEAHEAD,
                             required=True, max_length=state_max_length)
    return _lookup_form(_lookup_field("city", semantic=SemanticType.CITY), state)


class PerLookupBrowser(SelectiveLookupBrowser):
    """``SelectiveLookupBrowser`` whose lookups each offer their own suggestions
    (``LOOKUP_CHOICES``). With ``reverse`` it reports a step's results in reverse form
    order, so fill order and form order differ."""

    reverse = False

    def _results(self, form: ApplicationForm, packet: ApplicationPacket,
                 only: set[str] | None = None) -> FillResult:
        base = super()._results(form, packet, only)
        fields = [FieldFillResult(field_id=r.field_id, status=r.status,
                                  suggestions=LOOKUP_CHOICES[r.field_id])
                  if r.status is FieldFillStatus.NEEDS_CHOICE else r for r in base.fields]
        return FillResult(form_step=base.form_step, fields=fields[::-1] if self.reverse else fields)


class ReversedLookupBrowser(PerLookupBrowser):
    reverse = True


def _fill_order(browser: type[PerLookupBrowser]) -> list[str]:
    return list(LOOKUP_ORDER[::-1] if browser.reverse else LOOKUP_ORDER)


class BatchChooser(FactualPacketResolver):
    """A resolver that decides a step's lookups together (``choose_suggestions``): it
    returns ``reply`` applied to the labels ``picks`` gives the lookups, or raises
    ``error``. It also has ``choose_suggestion``, which the runner must then leave unused."""

    def __init__(self, picks: dict[str, Any], *, reply: Callable[[list[Any]], Any] = list,
                 error: Exception | None = None) -> None:
        super().__init__()
        self.picks, self.reply, self.error = picks, reply, error
        self.batches: list[list[tuple[str, str, list[str]]]] = []
        self.singles: list[str] = []

    async def choose_suggestions(self, context, lookups):  # type: ignore[no-untyped-def]
        for fld, _, _ in lookups:
            assert context.form.find(fld.id) == fld
        batch = [(fld.id, typed, list(suggestions)) for fld, typed, suggestions in lookups]
        self.batches.append(batch)
        if self.error is not None:
            raise self.error
        return self.reply([self.picks.get(field_id) for field_id, _, _ in batch])

    async def choose_suggestion(self, context, field, typed_value, suggestions):  # type: ignore[no-untyped-def]
        self.singles.append(field.id)
        return RIGHT_LABELS.get(field.id)

    def suggestion_decision(self, field_id: str) -> dict[str, object]:
        return {"stage": "suggestion_choice", "field_id": field_id, "batch": True}


class EachChooser(FactualPacketResolver):
    """A resolver with ``choose_suggestion`` only: asked once per lookup. A pick that is an
    exception is raised for that lookup."""

    def __init__(self, picks: dict[str, Any]) -> None:
        super().__init__()
        self.picks = picks
        self.calls: list[tuple[str, str, list[str]]] = []

    async def choose_suggestion(self, context, field, typed_value, suggestions):  # type: ignore[no-untyped-def]
        assert context.form.find(field.id) == field
        self.calls.append((field.id, typed_value, list(suggestions)))
        pick = self.picks.get(field.id)
        if isinstance(pick, Exception):
            raise pick
        return pick


class PicksFor(NoninteractiveInteraction):
    """Answers the questions of the fields ``picks`` names with that text (a list: one text
    per round) and records every round's questions."""

    def __init__(self, picks: dict[str, str | list[str]]) -> None:
        super().__init__()
        self.picks = picks
        self.asked: list[list[MissingInput]] = []

    async def request_inputs(self, missing: Sequence[MissingInput]) -> Sequence[UserInput]:
        self.asked.append(list(missing))
        inputs = []
        for item in missing:
            pick = self.picks.get(item.field_id or "")
            text = pick.pop(0) if isinstance(pick, list) else pick
            if text is not None:
                inputs.append(UserInput.answering(item, TextValue(text=text)))
        return inputs


def _events(paths, app_id: str, name: str) -> list[ApplicationEvent]:
    with _store(paths) as store:
        return [e for e in store.list_events(app_id) if e.event == name]


def _refilled(script: Script) -> list[str]:
    return [call for call in script.calls if call.startswith("fill_fields")]


@pytest.mark.parametrize("browser", [PerLookupBrowser, ReversedLookupBrowser])
def test_a_batch_chooser_decides_every_lookup_of_the_step_in_one_call(
    isolated_imx_home, fictional_candidate, browser
):
    script = Script(pages=[_page(_three_lookups_form())])
    factory = LookupFactory(script, browser, commits=frozenset(RIGHT_LABELS.values()))
    chooser = BatchChooser(RIGHT_LABELS)
    result = asyncio.run(_lookup_runner(isolated_imx_home, _austin(fictional_candidate), factory,
                                        resolver=chooser).apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and result.message == PREPARED
    assert result.missing_inputs == [] and "submit" not in script.calls
    order = _fill_order(browser)
    # One call for the whole step, every lookup in fill order; never one lookup at a time.
    assert chooser.batches == [[(fid, TYPED[fid], LOOKUP_CHOICES[fid]) for fid in order]]
    assert chooser.singles == []
    # Only the chosen lookups are typed again, each with its own label verbatim.
    assert script.calls.count("fill") == 1
    assert _refilled(script) == ["fill_fields:" + ",".join(order)]
    assert factory.typed == [TYPED, RIGHT_LABELS]
    events = _events(isolated_imx_home, result.application_id, SUGGESTION_EVENT)
    assert [e.metadata["field_id"] for e in events] == order
    for event in events:
        fid = event.metadata["field_id"]
        assert event.metadata["chosen_label"] == RIGHT_LABELS[fid]
        assert (event.metadata["form_step"], event.metadata["suggestion_count"]) == (0, 2)
        assert event.metadata["source"] == "PROFILE_IDENTITY"
        assert event.metadata["chooser"] == "BatchChooser"
        assert event.metadata["decision"] == {"stage": "suggestion_choice", "field_id": fid,
                                              "batch": True}
    with _store(isolated_imx_home) as store:
        packet = store.latest_packet(result.application_id)
    for fid in LOOKUP_ORDER:
        answer = packet.answer_for(fid)
        assert answer is not None and answer.value == TextValue(text=RIGHT_LABELS[fid])
        assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY
        assert answer.provenance.note == (f"verified identity: {fid}; site suggestion chosen for "
                                          f"the typed value {TYPED[fid]!r}")


@pytest.mark.parametrize("wrong", [TEXAS, "austin, texas", " Austin, Texas", 42, None],
                         ids=["another-lookups-label", "other-case", "padded", "not-text", "none"])
def test_a_batch_label_counts_only_for_its_own_lookup_and_only_if_it_fits(
    isolated_imx_home, fictional_candidate, wrong
):
    # location: one of its own suggestions; city: not verbatim one of city's suggestions;
    # state: one of state's suggestions verbatim, but longer than the state field takes.
    script = Script(pages=[_page(_three_lookups_form(state_max_length=12))])
    factory = LookupFactory(script, PerLookupBrowser,
                            commits=frozenset({TEXAS, "Austin, Texas", "Texas, USA"}))
    chooser = BatchChooser({"location": TEXAS, "city": wrong, "state": "Texas, United States"})
    interaction = PicksFor({"city": "Austin, Texas", "state": "Texas, USA"})
    result = asyncio.run(_lookup_runner(isolated_imx_home, _austin(fictional_candidate), factory,
                                        resolver=chooser, interaction=interaction)
                         .apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and result.message == PREPARED
    assert len(chooser.batches) == 1 and chooser.singles == []
    # Only city and state go to the user, each listing its own suggestions.
    [questions] = interaction.asked
    assert [q.field_id for q in questions] == ["city", "state"]
    for question in questions:
        assert [o.label for o in question.options or []] == LOOKUP_CHOICES[question.field_id]
        assert repr(TYPED[question.field_id]) in question.prompt
    # Nothing is typed again before the user picks; then location keeps its chosen label.
    assert _refilled(script) == []
    assert factory.typed == [TYPED, {"location": TEXAS, "city": "Austin, Texas",
                                     "state": "Texas, USA"}]
    [event] = _events(isolated_imx_home, result.application_id, SUGGESTION_EVENT)
    assert (event.metadata["field_id"], event.metadata["chosen_label"]) == ("location", TEXAS)
    with _store(isolated_imx_home) as store:
        packet = store.latest_packet(result.application_id)
    location = packet.answer_for("location")
    assert location is not None and location.value == TextValue(text=TEXAS)
    assert location.provenance.source is AnswerSource.PROFILE_IDENTITY
    assert {packet.answer_for(fid).provenance.source for fid in ("city", "state")} == {
        AnswerSource.USER_INPUT}


BATCH_FAILURES: dict[str, dict[str, Any]] = {
    "raises": {"error": RuntimeError("fictional provider outage")},
    "too-short": {"reply": lambda labels: labels[:-1]},
    "too-long": {"reply": lambda labels: [*labels, TEXAS]},
    "not-a-list": {"reply": lambda labels: None},
    "declines": {"reply": lambda labels: [None] * len(labels)},
}
"""How a batch can fail; "declines" (no suggestion is clearly right) is the baseline."""


@pytest.mark.parametrize("failure", list(BATCH_FAILURES))
def test_a_failed_batch_leaves_every_lookup_of_the_step_to_the_user(
    isolated_imx_home, fictional_candidate, failure
):
    # Every failure of the batch ends the step exactly as a batch that chose nothing: no
    # label applied (not even the valid ones of a short reply), no exception escapes.
    script = Script(pages=[_page(_three_lookups_form())])
    factory = LookupFactory(script, PerLookupBrowser, commits=frozenset(RIGHT_LABELS.values()))
    chooser = BatchChooser(RIGHT_LABELS, **BATCH_FAILURES[failure])
    result = asyncio.run(_lookup_runner(isolated_imx_home, _austin(fictional_candidate), factory,
                                        resolver=chooser).apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT
    assert result.message == "3 required question(s) need your answer."
    assert len(chooser.batches) == 1 and chooser.singles == []
    assert [m.field_id for m in result.missing_inputs] == list(LOOKUP_ORDER)
    for question in result.missing_inputs:
        assert (question.reason, question.control_type) == (MissingReason.NO_ANSWER,
                                                            ControlType.TYPEAHEAD)
        assert [o.label for o in question.options or []] == LOOKUP_CHOICES[question.field_id]
        assert repr(TYPED[question.field_id]) in question.prompt
    assert factory.typed == [TYPED] and script.calls.count("fill") == 1
    assert _refilled(script) == []
    with _store(isolated_imx_home) as store:
        app = store.get_application(result.application_id)
        assert app.state is S.NEEDS_INPUT and app.failure_reason is None
        assert pending_inputs(store, result.application_id) == result.missing_inputs
        packet = store.latest_packet(result.application_id)
        assert [a.field_id for a in packet.answers] == ["first_name"]
        assert not [e for e in store.list_events(result.application_id)
                    if e.event == SUGGESTION_EVENT]


@pytest.mark.parametrize("browser", [PerLookupBrowser, ReversedLookupBrowser])
def test_a_chooser_without_a_batch_method_is_asked_per_lookup_and_a_failure_stays_open(
    isolated_imx_home, fictional_candidate, browser
):
    script = Script(pages=[_page(_three_lookups_form())])
    factory = LookupFactory(script, browser, commits=frozenset(RIGHT_LABELS.values()))
    chooser = EachChooser({"location": TEXAS, "city": RuntimeError("fictional model error"),
                           "state": "Texas, United States"})
    result = asyncio.run(_lookup_runner(isolated_imx_home, _austin(fictional_candidate), factory,
                                        resolver=chooser).apply(URL, candidate_id="c1"))
    order = _fill_order(browser)
    assert chooser.calls == [(fid, TYPED[fid], LOOKUP_CHOICES[fid]) for fid in order]
    # Only the lookup whose decision raised goes to the user.
    assert result.state is S.NEEDS_INPUT
    assert result.message == "1 required question(s) need your answer."
    [question] = result.missing_inputs
    assert question.field_id == "city"
    assert [o.label for o in question.options or []] == LOOKUP_CHOICES["city"]
    events = _events(isolated_imx_home, result.application_id, SUGGESTION_EVENT)
    assert [(e.metadata["field_id"], e.metadata["chosen_label"]) for e in events] == [
        (fid, RIGHT_LABELS[fid]) for fid in order if fid != "city"]
    assert all(e.metadata["chooser"] == "EachChooser" and e.metadata["decision"] is None
               for e in events)
    with _store(isolated_imx_home) as store:
        packet = store.latest_packet(result.application_id)
    assert packet.answer_for("city") is None
    assert packet.answer_for("location").value == TextValue(text=TEXAS)
    assert packet.answer_for("state").value == TextValue(text="Texas, United States")
    # The user must pick first, so nothing is typed again in this run.
    assert factory.typed == [TYPED] and _refilled(script) == []


def test_a_batch_leaves_out_a_lookup_the_user_answered_and_each_label_stays_on_its_lookup(
    isolated_imx_home, fictional_candidate
):
    # Fill order location, school, city: school holds the user's own text, so the batch
    # decides location and city only, and each label lands on its own lookup.
    form = _lookup_form(_lookup_field("school", semantic=SemanticType.UNIVERSITY),
                        _lookup_field("city", semantic=SemanticType.CITY))
    script = Script(pages=[_page(form)])
    factory = LookupFactory(script, PerLookupBrowser,
                            commits=frozenset({TEXAS, "Austin, Texas", "Fictional State University"}))
    chooser = BatchChooser(RIGHT_LABELS)
    interaction = PicksFor({"school": ["Fictional State", "Fictional State University"]})
    result = asyncio.run(_lookup_runner(isolated_imx_home, _austin(fictional_candidate), factory,
                                        resolver=chooser, interaction=interaction)
                         .apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and result.message == PREPARED
    assert chooser.batches == [[("location", "Austin, TX", LOOKUP_CHOICES["location"]),
                                ("city", "Austin, TX", LOOKUP_CHOICES["city"])]]
    assert chooser.singles == []
    first, second = interaction.asked
    assert [m.field_id for m in first] == [m.field_id for m in second] == ["school"]
    assert [o.label for o in second[0].options or []] == LOOKUP_CHOICES["school"]
    assert factory.typed == [
        {"location": "Austin, TX", "school": "Fictional State", "city": "Austin, TX"},
        {"location": TEXAS, "school": "Fictional State University", "city": "Austin, Texas"}]
    events = _events(isolated_imx_home, result.application_id, SUGGESTION_EVENT)
    assert [(e.metadata["field_id"], e.metadata["chosen_label"]) for e in events] == [
        ("location", TEXAS), ("city", "Austin, Texas")]


# --- round 5: the dynamic resolver decides the lookups concurrently; sorted budget purposes --

class OverlapJev(ScriptedJev):
    """Thread-safe ``ScriptedJev`` whose ``lookup`` decisions sleep until another lookup
    decision is in flight (at most ``patience`` seconds), recording the peak number in
    flight: decisions made one after another show a peak of 1 instead of hanging."""

    def __init__(self, *args: Any, patience: float = 5.0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.patience = patience
        self.flight = threading.Condition()
        self.in_flight = self.peak = 0

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        with self.flight:
            response = super().__call__(url, headers, body, timeout)
            if "lookup" not in self.requests[-1]["questions"]:
                return response
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.flight.notify_all()
            self.flight.wait_for(lambda: self.peak >= 2, timeout=self.patience)
            self.in_flight -= 1
            return response


def _dynamic_lookup_runner(paths, candidate: CandidateProfile, factory: ScriptedFactory,
                           jev: ScriptedJev) -> LocalApplicationRunner:
    decisions = BoundedDecisions(JevClient(ApiKey("synthetic-runner-key", source="test"),
                                           transport=jev, max_attempts=1))
    return _lookup_runner(paths, candidate, factory,
                          resolver=DynamicPacketResolver(decisions, router=AIFormRouter(decisions)))


def test_the_dynamic_resolver_decides_the_lookups_of_a_step_concurrently(
    isolated_imx_home, fictional_candidate
):
    script = Script(pages=[_page(_lookup_form(_lookup_field("city", semantic=SemanticType.CITY)))])
    factory = LookupFactory(script, PerLookupBrowser, commits=frozenset({TEXAS, "Austin, Texas"}))
    jev = OverlapJev({}, {"lookup": LOOKUP_S1})
    runner = _dynamic_lookup_runner(isolated_imx_home, _austin(fictional_candidate), factory, jev)
    result = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and result.missing_inputs == []
    assert result.message.startswith(PREPARED)
    lookups = jev.asked("lookup")
    assert sorted(r["state"]["typed_value"] for r in lookups) == ["Austin, TX", "Austin, TX"]
    assert jev.peak >= 2  # both lookup decisions were in flight at the same time
    assert _refilled(script) == ["fill_fields:location,city"]
    assert factory.typed == [{"location": "Austin, TX", "city": "Austin, TX"},
                             {"location": TEXAS, "city": "Austin, Texas"}]
    # Events, decisions and traces keep fill order although the decisions overlapped.
    events = _events(isolated_imx_home, result.application_id, SUGGESTION_EVENT)
    assert [(e.metadata["field_id"], e.metadata["chosen_label"]) for e in events] == [
        ("location", TEXAS), ("city", "Austin, Texas")]
    for event in events:
        decision = event.metadata["decision"]
        assert event.metadata["chooser"] == "DynamicPacketResolver"
        assert (decision["stage"], decision["field_id"], decision["status"], decision["choice"]) \
            == ("suggestion_choice", event.metadata["field_id"], "CHOSEN", "s1")
    traces = [t["field_id"] for t in runner.resolver.narrative_traces
              if t.get("stage") == "suggestion_choice"]
    assert traces == ["location", "city"]
    purposes = [r.purpose for r in runner.resolver.decisions.budget.receipts]
    assert purposes.count("lookup_suggestion") == 2


def test_the_provider_budget_event_lists_purposes_sorted_not_in_call_order(
    isolated_imx_home, fictional_candidate, monkeypatch
):
    # The store serializes event metadata with sorted keys, so what the runner hands it is
    # captured before serialization: that is where the resolver's order shows.
    handed: list[tuple[str, Any]] = []
    append_event = ApplicationStore.append_event

    def recording(store: ApplicationStore, claim: Any, event: str,
                  metadata: dict[str, Any] | None = None) -> ApplicationEvent:
        handed.append((event, json.loads(json.dumps(metadata, default=str))))
        return append_event(store, claim, event, metadata)

    monkeypatch.setattr(ApplicationStore, "append_event", recording)
    form = _lookup_form(_lookup_field("city", semantic=SemanticType.CITY), RESIDENCE)
    script = Script(pages=[_page(form)])
    factory = LookupFactory(script, PerLookupBrowser, commits=frozenset({TEXAS, "Austin, Texas"}))
    jev = ScriptedJev({}, {**RESIDENCE_CHOICES, "lookup": LOOKUP_S1})
    runner = _dynamic_lookup_runner(isolated_imx_home, _austin(fictional_candidate), factory, jev)
    result = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and result.missing_inputs == []
    called = list(dict.fromkeys(r.purpose for r in runner.resolver.decisions.budget.receipts))
    assert called[0] == "full_form_routes"
    assert {"residence_screener", "lookup_suggestion"} <= set(called)
    assert called != sorted(called)  # so call order would show if the keys were not sorted
    [usage] = [metadata for event, metadata in handed if event == PROVIDER_EVENT]
    assert list(usage["by_purpose"]) == sorted(called)
    assert usage["by_purpose"]["lookup_suggestion"]["calls"] == 2
    assert sum(bucket["calls"] for bucket in usage["by_purpose"].values()) == usage["calls"]
    [event] = _provider_events(isolated_imx_home, result.application_id)
    assert event.metadata == usage
    assert result.message == PREPARED + _cost_suffix(usage)


# --- round 5: routing traces per resolved step; which fields a failed fill could not fill -----

def _routing_events(paths, app_id: str) -> list[ApplicationEvent]:
    from interviewmaxxing_cli.runner import ROUTING_EVENT

    return _events(paths, app_id, ROUTING_EVENT)


def test_a_dynamic_run_records_the_routing_of_its_step_once(isolated_imx_home, fictional_candidate):
    from interviewmaxxing_browser.ai.classification import PROMPT_VERSION

    form = _residence_form()
    jev = ScriptedJev(RESIDENCE_SCOPES, RESIDENCE_CHOICES)
    runner = _dynamic_runner(isolated_imx_home, fictional_candidate, Script(pages=[_page(form)]),
                             jev)
    result = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and result.missing_inputs == []
    assert result.message.startswith(PREPARED)
    [event] = _routing_events(isolated_imx_home, result.application_id)
    metadata = event.metadata
    assert metadata["form_step"] == 0 and metadata["prompt_version"] == PROMPT_VERSION
    assert [f["field_id"] for f in metadata["fields"]] == [f.id for f in form.fields]
    assert [f["field_fingerprint"] for f in metadata["fields"]] == [f.fingerprint
                                                                   for f in form.fields]
    residence = metadata["fields"][1]
    assert (residence["route"], residence["source_scope"]) == ("COPY_KNOWN", "APPLICANT_CURRENT")
    [screener] = [t for t in metadata["traces"] if t.get("stage") == "residence_screener"]
    assert (screener["field_id"], screener["status"]) == ("residence", "ANSWERED")
    assert type(runner.resolver.narrative_traces) is list
    assert isinstance(json.dumps(runner.resolver.narrative_traces, default=str), str)
    assert "synthetic-runner-key" not in json.dumps(metadata)
    [ready] = _events(isolated_imx_home, result.application_id, "preparation.ready")
    assert event.sequence < ready.sequence


def test_each_resolved_step_records_its_own_routing(isolated_imx_home, fictional_candidate):
    from interviewmaxxing_browser.ai.classification import PROMPT_VERSION

    script = Script(pages=[_page(_residence_form(final=False))],
                    advance=[NavigationResult(advanced=True, inspection=_page(_form(step=1)))])
    jev = ScriptedJev(RESIDENCE_SCOPES, RESIDENCE_CHOICES)
    runner = _dynamic_runner(isolated_imx_home, fictional_candidate, script, jev)
    result = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and result.missing_inputs == []
    assert result.message.startswith(PREPARED) and script.calls.count("advance") == 1
    first, second = _routing_events(isolated_imx_home, result.application_id)
    assert (first.metadata["form_step"], second.metadata["form_step"]) == (0, 1)
    assert [f["field_id"] for f in first.metadata["fields"]] == ["first_name", "residence"]
    assert [f["field_id"] for f in second.metadata["fields"]] == ["first_name"]
    assert first.metadata["prompt_version"] == second.metadata["prompt_version"] == PROMPT_VERSION
    assert any(t.get("stage") == "residence_screener" for t in first.metadata["traces"])
    assert first.sequence < second.sequence


def test_a_run_with_the_factual_resolver_records_no_routing(isolated_imx_home, fictional_candidate):
    result = asyncio.run(_runner(isolated_imx_home, fictional_candidate, Script(),
                                 prepare_only=True).apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT and result.message == PREPARED
    assert _routing_events(isolated_imx_home, result.application_id) == []


class FailingFillBrowser(ScriptedBrowser):
    """Fills every answer, except the fields ``outcomes`` gives another status and detail."""

    def __init__(self, script: Script,
                 outcomes: dict[str, tuple[FieldFillStatus, str | None]]) -> None:
        super().__init__(script)
        self.outcomes = outcomes

    async def fill(self, form: ApplicationForm, packet: ApplicationPacket) -> FillResult:
        self.s.calls.append("fill")
        assert packet.problems_against(form) == []
        results = []
        for answer in packet.answers:
            status, detail = self.outcomes.get(answer.field_id, (FieldFillStatus.FILLED, None))
            results.append(FieldFillResult(field_id=answer.field_id, status=status, detail=detail))
        return FillResult(form_step=form.step, fields=results)


class FailingFillFactory(ScriptedFactory):
    def __init__(self, script: Script,
                 outcomes: dict[str, tuple[FieldFillStatus, str | None]]) -> None:
        super().__init__(script)
        self.outcomes = outcomes

    async def start(self, options: BrowserOptions) -> FailingFillBrowser:  # type: ignore[override]
        self.script.starts += 1
        self.options.append(options)
        return FailingFillBrowser(self.script, self.outcomes)


EMAIL = ApplicationField(id="email", label="Email address", selector="#email",
                         semantic_type=SemanticType.EMAIL, control_type=ControlType.TEXT,
                         required=True)
LONG_DETAIL = "scripted: the fictional control was replaced while typing; " * 12


def _failed_transition(paths, app_id: str) -> tuple[ApplicationEvent, str | None]:
    """The one transition into FAILED_RETRYABLE and the stored failure reason."""
    with _store(paths) as store:
        [event] = [e for e in store.list_events(app_id) if e.to_state is S.FAILED_RETRYABLE]
        return event, store.get_application(app_id).failure_reason


@pytest.mark.parametrize(("status", "detail", "recorded"), [
    (FieldFillStatus.FAILED, "scripted: the fictional input was detached",
     "scripted: the fictional input was detached"),
    # Since 0a1bac3 quoted values are redacted and the detail is cut (runner.redact_detail).
    (FieldFillStatus.VERIFICATION_MISMATCH, LONG_DETAIL, redact_detail(LONG_DETAIL)),
    (FieldFillStatus.FAILED, "", None),
    (FieldFillStatus.FAILED, None, None),
], ids=["failed", "mismatch-long-detail", "empty-detail", "no-detail"])
def test_a_failed_fill_records_the_failed_field_with_its_label_status_and_detail(
    isolated_imx_home, fictional_candidate, status, detail, recorded
):
    form = _form().model_copy(update={"fields": [*_form().fields, EMAIL]})
    script = Script(pages=[_page(form)])
    factory = FailingFillFactory(script, {"email": (status, detail)})
    result = asyncio.run(_lookup_runner(isolated_imx_home, fictional_candidate, factory)
                         .apply(URL, candidate_id="c1"))
    reason = "Could not fill email reliably; nothing was submitted."
    assert result.state is S.FAILED_RETRYABLE and result.message == reason
    assert script.calls.count("fill") == 1 and "submit" not in script.calls
    assert len(LONG_DETAIL) > 500
    event, failure_reason = _failed_transition(isolated_imx_home, result.application_id)
    assert failure_reason == event.metadata["failure_reason"] == reason
    # Only the field that failed is listed; first_name was filled.
    assert event.metadata["failed_fields"] == [
        {"field_id": "email", "label": "Email address", "status": status.value,
         "detail": recorded}]


def test_a_failed_fill_after_provider_calls_keeps_the_cost_out_of_the_failure_reason(
    isolated_imx_home, fictional_candidate
):
    script = Script(pages=[_page(_residence_form())])
    factory = FailingFillFactory(script, {
        "residence": (FieldFillStatus.VERIFICATION_MISMATCH, "reads back 'No'")})
    jev = ScriptedJev(RESIDENCE_SCOPES, RESIDENCE_CHOICES)
    runner = _dynamic_lookup_runner(isolated_imx_home, fictional_candidate, factory, jev)
    result = asyncio.run(runner.apply(URL, candidate_id="c1"))
    reason = "Could not fill residence reliably; nothing was submitted."
    [cost] = _provider_events(isolated_imx_home, result.application_id)
    assert result.state is S.FAILED_RETRYABLE
    assert result.message == reason + _cost_suffix(cost.metadata)
    event, failure_reason = _failed_transition(isolated_imx_home, result.application_id)
    assert failure_reason == event.metadata["failure_reason"] == reason
    assert event.metadata["failed_fields"] == [
        {"field_id": "residence", "label": RESIDENCE.label, "status": "VERIFICATION_MISMATCH",
         "detail": "reads back '…'"}]
    assert "Provider cost" not in json.dumps(event.metadata)


def test_failed_fields_leave_out_a_lookup_that_only_needs_a_choice(
    isolated_imx_home, fictional_candidate
):
    script = Script(pages=[_page(_lookup_form())])
    factory = LookupFactory(script, failing=frozenset({"first_name"}))
    chooser = BatchChooser(RIGHT_LABELS)
    result = asyncio.run(_lookup_runner(isolated_imx_home, _austin(fictional_candidate), factory,
                                        resolver=chooser).apply(URL, candidate_id="c1"))
    assert result.state is S.FAILED_RETRYABLE
    assert result.message == "Could not fill first_name reliably; nothing was submitted."
    assert chooser.batches == [] and chooser.singles == []  # no choice round beside a failure
    event, _ = _failed_transition(isolated_imx_home, result.application_id)
    assert event.metadata["failed_fields"] == [
        {"field_id": "first_name", "label": "First name", "status": "FAILED",
         "detail": "scripted failure"}]


def test_a_lookup_gets_one_batch_round_per_run_even_if_its_chosen_label_later_fails(
    isolated_imx_home, fictional_candidate
):
    # The batch picks location only; the user answers city. Typed then, location's label
    # does not commit either: the lookup goes to the user, never to a second batch.
    form = _lookup_form(_lookup_field("city", semantic=SemanticType.CITY))
    script = Script(pages=[_page(form)])
    factory = LookupFactory(script, PerLookupBrowser, commits=frozenset({"Austin, Texas"}))
    chooser = BatchChooser({"location": TEXAS})
    interaction = PicksFor({"city": "Austin, Texas"})
    result = asyncio.run(_lookup_runner(isolated_imx_home, _austin(fictional_candidate), factory,
                                        resolver=chooser, interaction=interaction)
                         .apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT
    assert result.message == "1 required question(s) need your answer."
    assert chooser.batches == [[("location", "Austin, TX", LOOKUP_CHOICES["location"]),
                                ("city", "Austin, TX", LOOKUP_CHOICES["city"])]]
    assert chooser.singles == []
    assert factory.typed == [{"location": "Austin, TX", "city": "Austin, TX"},
                             {"location": TEXAS, "city": "Austin, Texas"}]
    assert [m.field_id for asked in interaction.asked for m in asked] == ["city", "location"]
    [question] = result.missing_inputs
    assert question.field_id == "location" and repr(TEXAS) in question.prompt
    assert [o.label for o in question.options or []] == LOOKUP_CHOICES["location"]
    [event] = _events(isolated_imx_home, result.application_id, SUGGESTION_EVENT)
    assert (event.metadata["field_id"], event.metadata["chosen_label"]) == ("location", TEXAS)
    with _store(isolated_imx_home) as store:
        packet = store.latest_packet(result.application_id)
    assert packet.answer_for("location") is None
    assert packet.answer_for("city").provenance.source is AnswerSource.USER_INPUT


class TimeoutLookupJev(ScriptedJev):
    """``ScriptedJev`` whose lookup decision for the lookup asking ``question`` times out."""

    def __init__(self, *args: Any, question: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.question = question

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        if "lookup" in request["questions"] and request["state"]["question"] == self.question:
            self.requests.append(request)
            raise TimeoutError("fictional provider timeout")
        return super().__call__(url, headers, body, timeout)


def test_a_timed_out_lookup_decision_in_the_dynamic_batch_leaves_only_that_lookup_open(
    isolated_imx_home, fictional_candidate
):
    script = Script(pages=[_page(_lookup_form(_lookup_field("city", semantic=SemanticType.CITY)))])
    factory = LookupFactory(script, PerLookupBrowser, commits=frozenset({TEXAS, "Austin, Texas"}))
    jev = TimeoutLookupJev({}, {"lookup": LOOKUP_S1}, question="City (search)")
    runner = _dynamic_lookup_runner(isolated_imx_home, _austin(fictional_candidate), factory, jev)
    result = asyncio.run(runner.apply(URL, candidate_id="c1"))
    assert result.state is S.NEEDS_INPUT
    assert result.message.startswith("1 required question(s) need your answer.")
    assert sorted(r["state"]["typed_value"] for r in jev.asked("lookup")) == ["Austin, TX", "Austin, TX"]
    [question] = result.missing_inputs
    assert question.field_id == "city"
    assert [o.label for o in question.options or []] == LOOKUP_CHOICES["city"]
    [event] = _events(isolated_imx_home, result.application_id, SUGGESTION_EVENT)
    assert (event.metadata["field_id"], event.metadata["chosen_label"]) == ("location", TEXAS)
    assert runner.resolver.suggestion_decision("city")["status"] == "HELD"
    with _store(isolated_imx_home) as store:
        packet = store.latest_packet(result.application_id)
    assert packet.answer_for("location").value == TextValue(text=TEXAS)
    assert packet.answer_for("city") is None
    assert factory.typed == [{"location": "Austin, TX", "city": "Austin, TX"}]
