"""The reusable local application runner (I1).

``LocalApplicationRunner`` drives one application at a time through the canonical
pieces: the candidate store (C2), the factual packet resolver (C3), the browser
runtime (C4) and the ``ApplicationStore``, which remains the only state authority.
The CLI and the local HTTP service (S1) both use it::

    runner = create_runner(LocalPaths.from_env(), headless=False, interaction=interaction)
    outcome = await runner.apply(url, candidate_id="default")
    outcome = await runner.resume(application_id)
    outcome = await runner.reconcile(application_id)

Construction opens nothing (no database, browser or terminal), so a runner can be
built on any thread. Each call opens its own store connection on the thread running
it and closes it before returning.

Guarantees:

* The user's request to apply authorizes submission; nothing else is confirmed.
  Questions are asked only for required answers the verified data cannot give, and
  the user is asked to act only for sign-in, CAPTCHA or custom controls.
* ``SUBMITTING`` is durably recorded before the submit click, and any interruption
  (exception, cancellation, Ctrl-C, SIGTERM) during the submit records
  ``SUBMISSION_UNKNOWN``. Only site acceptance tied to this job yields SUBMITTED.
* Missing input is durable: the packet (with scoped ``MissingInput`` items) and the
  ``application.needs_input`` event carry the exact questions, so a later process
  re-presents them (``pending_inputs``).
* Each application keeps the resume it started with: the first run pins the
  profile's resume (``ApplicationStore.pin_resume``) and every later run uses the
  pinned file, whatever the profile says now. If that file is missing or changed the
  run stops; another resume is never substituted.
* One run per browser profile (an OS file lock) and per application (store claim).
* Advancing and submitting are distinct; an ambiguous step stops the run. A run
  stops after ``RunLimits.max_steps`` pages or when the same form comes back
  ``max_same_form`` times, so it cannot loop.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import socket
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from interviewmaxxing_browser import (
    AmbiguousAction,
    ConfirmationTie,
    PlaywrightSessionFactory,
    SubmissionRefused,
    reconciliation_from,
    user_action_needs,
)
from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_core import (
    PRE_SUBMISSION_STATES,
    SUBMISSION_BLOCKING_STATES,
    TERMINAL_STATES,
    Application,
    ApplicationBrowser,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    ApplyOutcome,
    BrowserOptions,
    BrowserSessionFactory,
    CandidateLoader,
    CandidateNotFound,
    CandidateProfile,
    CandidateProfileInvalid,
    Claim,
    ClaimLost,
    ClaimUnavailable,
    IdentityConflict,
    LocalPaths,
    MissingInput,
    MissingReason,
    PacketContext,
    PacketResolver,
    PageInspection,
    PageKind,
    ReconciliationMethod,
    RequestDisposition,
    ResumeArtifact,
    SavedAnswer,
    SubmissionObservation,
    SubmissionOutcome,
    UserInput,
    UserInteraction,
)
from interviewmaxxing_generation import FactualPacketResolver

S = ApplicationState

NEEDS_INPUT_EVENT = "application.needs_input"
RUN_LOCK_NAME = ".interviewmaxxing-run.lock"


class SavedAnswerStore(CandidateLoader, Protocol):
    def save_answer(self, candidate_id: str, answer: SavedAnswer) -> None: ...


@dataclass(frozen=True, slots=True)
class RunLimits:
    max_steps: int = 12
    """Pages inspected in one run before it stops (FAILED_RETRYABLE)."""
    max_same_form: int = 3
    """Times the same form (fingerprint) may be shown in one run before it stops."""
    user_action_timeout_s: float = 600.0
    """How long ``wait_for_user`` waits for sign-in, CAPTCHA or custom controls."""
    claim_ttl_s: float = 300.0


class RunnerBusy(RuntimeError):
    """Another run holds the browser profile."""


class _Stop(Exception):
    """Internal: end the run with this outcome."""

    def __init__(self, outcome: ApplyOutcome) -> None:
        super().__init__(outcome.message)
        self.outcome = outcome


@contextlib.contextmanager
def browser_profile_lock(browser_dir: Path) -> Iterator[None]:
    """Exclusive, non-blocking OS lock for the persistent browser profile. The lock
    disappears with the process, so a crash never leaves it stale."""
    browser_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(browser_dir / RUN_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunnerBusy("another run is using the browser profile") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def pending_inputs(store: ApplicationStore, application_id: str) -> list[MissingInput]:
    """The questions and actions a NEEDS_INPUT application is waiting for, exactly as
    last recorded (from the latest ``application.needs_input`` event). Empty for any
    other state."""
    app = store.get_application(application_id)
    if app.state is not S.NEEDS_INPUT:
        return []
    for event in reversed(store.list_events(application_id)):
        if event.event == NEEDS_INPUT_EVENT:
            return [MissingInput.model_validate(m) for m in event.metadata.get("missing_inputs", [])]
    return []


def _outcome(store: ApplicationStore, application_id: str, message: str,
             missing: Sequence[MissingInput] = ()) -> ApplyOutcome:
    app = store.get_application(application_id)
    if app.state is S.NEEDS_INPUT and not missing:
        missing = pending_inputs(store, application_id)
    return ApplyOutcome(
        application_id=application_id,
        state=app.state,
        receipt=store.get_receipt(application_id),
        missing_inputs=list(missing),
        message=message,
    )


_STATE_MESSAGES: dict[ApplicationState, str] = {
    S.SUBMITTED: "Already submitted; the site confirmed it. See the receipt.",
    S.SUBMITTING: "A submission is in progress or was interrupted; it will not be repeated.",
    S.SUBMISSION_UNKNOWN: "A previous submit may have reached the employer. It will not be "
                          "retried; reconcile it first.",
    S.DUPLICATE: "This job duplicates an application that already exists.",
    S.WITHDRAWN: "The application was withdrawn.",
    S.FAILED_PERMANENT: "The application cannot be completed.",
}


class LocalApplicationRunner:
    """Concrete ``ApplicationRunner`` over local storage and a real browser."""

    def __init__(
        self,
        *,
        paths: LocalPaths,
        interaction: UserInteraction,
        headless: bool = False,
        browser_factory: BrowserSessionFactory | None = None,
        candidates: SavedAnswerStore | None = None,
        resolver: PacketResolver | None = None,
        limits: RunLimits | None = None,
        owner: str | None = None,
    ) -> None:
        self.paths = paths
        self.interaction = interaction
        self.headless = headless
        self.browser_factory = browser_factory or PlaywrightSessionFactory()
        self.candidates: SavedAnswerStore = candidates or LocalCandidateStore.from_paths(paths)
        self.resolver = resolver or FactualPacketResolver()
        self.limits = limits or RunLimits()
        self.owner = owner or f"runner:{socket.gethostname()}:{os.getpid()}"

    # --- public API -------------------------------------------------------------------

    async def apply(self, application_url: str, *, candidate_id: str) -> ApplyOutcome:
        """Record the request (idempotent) and run it unless the stored state forbids
        it. A repeated request for a submitted, in-flight or uncertain application
        returns that state without touching the browser."""
        with self._store() as store:
            result = store.record_request(candidate_id, application_url)
            app_id = result.application.id
            if result.disposition not in (RequestDisposition.NEW, RequestDisposition.RESUMABLE):
                return _outcome(store, app_id, _STATE_MESSAGES.get(
                    result.application.state, "This application cannot be run."))
            return await self._run(store, app_id, application_url)

    async def resume(self, application_id: str) -> ApplyOutcome:
        """Continue a stopped application (missing input answered, sign-in done, a
        retryable failure) from a fresh inspection of the site."""
        with self._store() as store:
            app = store.get_application(application_id)
            if app.state in SUBMISSION_BLOCKING_STATES or app.state in TERMINAL_STATES:
                return _outcome(store, application_id,
                                _STATE_MESSAGES.get(app.state, "Nothing to resume."))
            url = store.list_requests(application_id)[0].application_url
            return await self._run(store, application_id, url)

    async def reconcile(self, application_id: str) -> ApplyOutcome:
        """Re-read the site for a SUBMISSION_UNKNOWN application, never resubmitting.
        Only acceptance tied to this job (its id or title shown with confirmation
        wording) is recorded, as SUBMITTED with a receipt; otherwise it stays unknown."""
        with self._store() as store:
            store.recover_interrupted_submissions()
            app = store.get_application(application_id)
            if app.state is not S.SUBMISSION_UNKNOWN:
                return _outcome(store, application_id,
                                f"Only an uncertain submission can be reconciled; this one is "
                                f"{app.state.value}.")
            job = store.get_job(app.job_id)
            tie = ConfirmationTie.from_job(job)
            if not (tie.external_job_id or tie.job_title):
                return _outcome(store, application_id,
                                "The job's identity (id or title) was never observed, so a "
                                "confirmation cannot be tied to it. Check with the employer and "
                                "record the result with `interviewmaxxing reconcile`.")
            email = self._lookup_email(app.candidate_id)
            url = job.application_url
            try:
                with browser_profile_lock(self.paths.browser_dir):
                    claim = store.claim(application_id, self.owner, ttl=self._ttl)
                    try:
                        browser = await self._start_browser(application_id)
                        try:
                            observation = await browser.reconcile(  # type: ignore[attr-defined]
                                url, tie=tie, lookup_email=email)
                        finally:
                            await browser.close()
                        return self._record_reconciliation(store, claim, observation)
                    finally:
                        store.release(claim)
            except RunnerBusy as exc:
                return _outcome(store, application_id, str(exc))
            except ClaimUnavailable:
                return _outcome(store, application_id, "Another run is working on this application.")

    # --- plumbing -------------------------------------------------------------------------

    @property
    def _ttl(self) -> timedelta:
        return timedelta(seconds=self.limits.claim_ttl_s)

    @contextlib.contextmanager
    def _store(self) -> Iterator[ApplicationStore]:
        self.paths.ensure()
        store = ApplicationStore.open(self.paths.state_db)
        try:
            yield store
        finally:
            store.close()

    async def _start_browser(self, application_id: str) -> ApplicationBrowser:
        return await self.browser_factory.start(BrowserOptions(
            artifacts_dir=self.paths.application_artifacts(application_id),
            artifacts_root=self.paths.artifacts_dir,
            profile_dir=self.paths.browser_dir,
            headless=self.headless,
        ))

    def _lookup_email(self, candidate_id: str) -> str | None:
        try:
            return self.candidates.load(candidate_id).identity.email
        except (CandidateNotFound, CandidateProfileInvalid):
            return None

    def _record_reconciliation(self, store: ApplicationStore, claim: Claim,
                               observation: SubmissionObservation) -> ApplyOutcome:
        app_id = claim.application_id
        reconciliation = reconciliation_from(observation, method=ReconciliationMethod.SITE_CONFIRMATION)
        if reconciliation is not None:
            store.reconcile_submission(claim, reconciliation)
            return _outcome(store, app_id, "The site confirms this application. Receipt saved.")
        if observation.evidence:
            store.add_evidence(claim, observation.evidence)
        store.append_event(claim, "reconcile.unconfirmed", {
            "signals": observation.signals, "observed_url": observation.observed_url,
        })
        return _outcome(store, app_id, "The site does not confirm this application yet; it stays "
                                       "SUBMISSION_UNKNOWN and will not be resubmitted.")

    # --- the run --------------------------------------------------------------------------

    async def _run(self, store: ApplicationStore, app_id: str, url: str) -> ApplyOutcome:
        app = store.get_application(app_id)
        try:
            candidate = self.candidates.load(app.candidate_id)
        except (CandidateNotFound, CandidateProfileInvalid) as exc:
            return _outcome(store, app_id, f"Candidate profile unavailable: {exc}")
        try:
            with browser_profile_lock(self.paths.browser_dir):
                try:
                    claim = store.claim(app_id, self.owner, ttl=self._ttl)
                except ClaimUnavailable:
                    return _outcome(store, app_id, "Another run is working on this application.")
                try:
                    app = store.get_application(app_id)
                    if app.state in SUBMISSION_BLOCKING_STATES or app.state in TERMINAL_STATES:
                        return _outcome(store, app_id, _STATE_MESSAGES.get(app.state, ""))
                    pinned = store.pin_resume(app_id, candidate.resume)
                    if not pinned.verify():
                        return self._pinned_resume_missing(store, claim, app, pinned)
                    candidate = candidate.model_copy(update={"resume": pinned})
                    browser = await self._start_browser(app_id)
                    try:
                        return await _Run(self, store, claim, candidate, browser, url).execute()
                    finally:
                        await browser.close()
                finally:
                    with contextlib.suppress(Exception):
                        store.release(claim)
        except RunnerBusy as exc:
            return _outcome(store, app_id, str(exc))

    @staticmethod
    def _pinned_resume_missing(store: ApplicationStore, claim: Claim, app: Application,
                               pinned: ResumeArtifact) -> ApplyOutcome:
        """The application's own resume is gone or changed. Stop; never substitute the
        profile's current resume, which may belong to another application."""
        message = (f"The resume this application uses ({pinned.filename}, sha256 "
                   f"{pinned.sha256[:12]}...) is missing or changed at {pinned.path}. Restore "
                   "that file and resume; another resume is never substituted.")
        if app.state is S.NEEDS_INPUT:
            store.transition(claim, S.INSPECTING)
        if store.get_application(app.id).state is not S.FAILED_RETRYABLE:
            store.transition(claim, S.FAILED_RETRYABLE, failure_reason=message)
        return _outcome(store, app.id, message)


class _Run:
    """One pass through the site for one claimed application."""

    def __init__(self, runner: LocalApplicationRunner, store: ApplicationStore, claim: Claim,
                 candidate: CandidateProfile, browser: ApplicationBrowser, url: str) -> None:
        self.runner = runner
        self.store = store
        self.claim = claim
        self.candidate = candidate
        self.browser = browser
        self.url = url
        self.limits = runner.limits
        self.interaction = runner.interaction
        self.forms_seen: Counter[str] = Counter()
        self.answered_after_rejection: set[tuple[str, str, str]] = set()

    @property
    def app_id(self) -> str:
        return self.claim.application_id

    def app(self) -> Application:
        return self.store.get_application(self.app_id)

    # state helpers ---------------------------------------------------------------------

    def _renew(self) -> None:
        self.claim = self.store.renew(self.claim, ttl=self.runner._ttl)

    def _to(self, state: ApplicationState, **kwargs: Any) -> None:
        if self.app().state is not state:
            self.store.transition(self.claim, state, **kwargs)

    def _stop(self, state: ApplicationState, message: str, *, reason: str | None = None,
              missing: Sequence[MissingInput] = ()) -> _Stop:
        current = self.app().state
        if current is S.NEEDS_INPUT and state is not S.NEEDS_INPUT:
            self._to(S.INSPECTING)  # NEEDS_INPUT only leaves through a fresh inspection
            current = S.INSPECTING
        if state is S.NEEDS_INPUT:
            self._to(S.INSPECTING)
            self.store.transition(self.claim, S.NEEDS_INPUT, metadata={
                "missing_inputs": [m.model_dump(mode="json") for m in missing],
                "reason": reason or message,
            })
        elif state in (S.FAILED_RETRYABLE, S.FAILED_PERMANENT):
            if current is not state:
                self.store.transition(self.claim, state, failure_reason=message)
        elif state is S.DUPLICATE:
            self.store.transition(self.claim, S.DUPLICATE, reason=message)
        return _Stop(_outcome(self.store, self.app_id, message, missing))

    # main loop -----------------------------------------------------------------------------

    async def execute(self) -> ApplyOutcome:
        try:
            await self.interaction.progress("Opening the application page")
            page = await self.browser.open(self.url)
            self._to(S.INSPECTING)
            for _ in range(self.limits.max_steps):
                self._renew()
                page = await self._step(page)
            raise self._stop(S.FAILED_RETRYABLE,
                             f"Stopped after {self.limits.max_steps} pages without reaching a "
                             "submission; nothing was submitted.")
        except _Stop as stop:
            return stop.outcome
        except ClaimLost:
            return _outcome(self.store, self.app_id, "This run lost its claim on the application; "
                                                     "another run took over.")
        except Exception as exc:
            # A browser or page failure before any submit: stop, keep it retryable.
            # (Failures during a submit were already recorded as SUBMISSION_UNKNOWN.)
            detail = f"{type(exc).__name__}: {exc}".splitlines()[0][:300]
            if self.app().state in PRE_SUBMISSION_STATES:
                return self._stop(S.FAILED_RETRYABLE, f"Stopped by a browser error ({detail}). "
                                  "Nothing was submitted; resume to retry.").outcome
            return _outcome(self.store, self.app_id, f"Stopped by an error: {detail}")

    async def _step(self, page: PageInspection) -> PageInspection:
        kind = page.kind
        if kind in (PageKind.SIGN_IN_REQUIRED, PageKind.CAPTCHA):
            return await self._user_action(page, user_action_needs(page))
        if kind is PageKind.ALREADY_APPLIED:
            raise self._stop(S.DUPLICATE, "The site says you have already applied to this job.")
        if kind is PageKind.JOB_CLOSED:
            raise self._stop(S.FAILED_PERMANENT, "The job is no longer accepting applications.")
        if kind is not PageKind.APPLICATION_FORM or page.form is None:
            detail = page.message or f"the page is {kind.value}, not an application form"
            raise self._stop(S.FAILED_RETRYABLE, f"Could not reach the application form: {detail}.")
        self._bind_identity(page)
        return await self._form_step(page, page.form)

    def _bind_identity(self, page: PageInspection) -> None:
        if page.job_identity is None:
            return
        try:
            result = self.store.bind_job_identity(self.claim, page.job_identity)
        except IdentityConflict as exc:
            raise self._stop(S.FAILED_RETRYABLE, f"Job identity conflict: {exc}") from exc
        if result.duplicate_of is not None:
            raise _Stop(_outcome(self.store, self.app_id,
                                 f"This job already has application {result.duplicate_of}; "
                                 "not applying twice."))

    async def _user_action(self, page: PageInspection, needs: list[MissingInput]) -> PageInspection:
        message = "; ".join(n.prompt for n in needs) or (page.message or "Action needed in the browser")
        if not await self.interaction.request_action(message):
            raise self._stop(S.NEEDS_INPUT, message, reason="user action required", missing=needs)
        self._renew()
        after = await self.browser.wait_for_user(message, self.limits.user_action_timeout_s)
        self._renew()
        if after.kind in (PageKind.SIGN_IN_REQUIRED, PageKind.CAPTCHA):
            raise self._stop(S.NEEDS_INPUT, "Still waiting for you in the browser: " + message,
                             reason="user action not completed", missing=user_action_needs(after))
        if after.kind is PageKind.APPLICATION_FORM:
            return after
        # e.g. a sign-in that returns to the posting: go back to the application URL.
        return await self.browser.open(self.url)

    async def _resolve(self, form: ApplicationForm) -> ApplicationPacket:
        app = self.app()
        job = self.store.get_job(app.job_id)
        context = PacketContext(application=app, job=job, form=form, candidate=self.candidate,
                                user_inputs=self.store.get_user_inputs(self.app_id, form))
        packet = await self.runner.resolver.resolve(context)
        problems = context.problems(packet)
        if problems:
            raise self._stop(S.FAILED_RETRYABLE, "Internal error: the answers for this step are "
                             "inconsistent (" + "; ".join(problems[:3]) + ").")
        return self._with_rejections(form, packet)

    def _with_rejections(self, form: ApplicationForm, packet: ApplicationPacket) -> ApplicationPacket:
        """Turn answers the site rejected (a field validation message) into questions for
        the user, unless the user already answered them after the rejection."""
        rejected = [
            f for f in form.fields
            if f.validation_error and packet.answer_for(f.id) is not None
            and (form.scope.key, f.id, f.fingerprint) not in self.answered_after_rejection
        ]
        if not rejected:
            return packet
        ids = {f.id for f in rejected}
        missing = [
            MissingInput.for_field(form, f, reason=MissingReason.NO_ANSWER,
                                   prompt=f"The site rejected the answer for this question: "
                                          f"{f.validation_error}. Please provide a corrected answer.")
            for f in rejected
        ]
        return ApplicationPacket.model_validate({
            **packet.model_dump(),
            "answers": [a.model_dump() for a in packet.answers if a.field_id not in ids],
            "missing_inputs": [*(m.model_dump() for m in packet.missing_inputs),
                               *(m.model_dump() for m in missing)],
        })

    async def _form_step(self, page: PageInspection, form: ApplicationForm) -> PageInspection:
        self.forms_seen[form.fingerprint] += 1
        if self.forms_seen[form.fingerprint] > self.limits.max_same_form:
            raise self._stop(S.FAILED_RETRYABLE, "The same form step kept coming back; stopped to "
                             "avoid a loop. Nothing was submitted.")
        self._to(S.INSPECTING)
        packet = await self._resolve(form)
        self.store.save_packet(self.claim, packet)
        while not packet.is_complete:
            required = [m for m in packet.missing_inputs if m.required]
            unsupported = [m for m in required if m.reason is MissingReason.UNSUPPORTED_CONTROL]
            questions = [m for m in required if m.reason is not MissingReason.UNSUPPORTED_CONTROL]
            if questions:
                inputs = list(await self.interaction.request_inputs(questions))
                if not self._accept_inputs(questions, inputs):
                    raise self._stop(S.NEEDS_INPUT, f"{len(questions)} required question(s) need "
                                     "your answer.", reason="missing answers",
                                     missing=packet.missing_inputs)
            else:
                page = await self._user_action(page, unsupported)
                if page.form is None:
                    return page
                form = page.form
            packet = await self._resolve(form)
            self.store.save_packet(self.claim, packet)
        return await self._act(form, packet)

    def _accept_inputs(self, questions: list[MissingInput], inputs: list[UserInput]) -> bool:
        wanted = {(m.field_id, m.field_fingerprint) for m in questions}
        usable = [u for u in inputs if (u.field_id, u.field_fingerprint) in wanted]
        if not usable:
            return False
        self.store.save_user_inputs(self.claim, usable)
        job = self.store.get_job(self.app().job_id)
        for user_input in usable:
            self.answered_after_rejection.add((user_input.scope.key, user_input.field_id,
                                               user_input.field_fingerprint))
            saved = user_input.to_saved_answer(job=job)
            if saved is not None:
                self.runner.candidates.save_answer(self.app().candidate_id, saved)
        return True

    async def _act(self, form: ApplicationForm, packet: ApplicationPacket) -> PageInspection:
        self._to(S.PACKET_READY)
        self._to(S.FILLING)
        try:
            fill = await self.browser.fill(form, packet)
        except ValueError:
            # The page changed under us; inspect it again rather than guess.
            self._to(S.INSPECTING)
            return await self.browser.inspect()
        if not fill.ok:
            failed = fill.failed_field_ids()
            if failed:
                raise self._stop(S.FAILED_RETRYABLE, "Could not fill " + ", ".join(failed)
                                 + " reliably; nothing was submitted.")
            self._to(S.INSPECTING)
            return await self.browser.inspect()
        if form.is_final_step is True:
            return await self._submit(packet)
        try:
            nav = await self.browser.advance()
        except (SubmissionRefused, AmbiguousAction) as exc:
            raise self._stop(S.FAILED_RETRYABLE, f"Cannot tell how to continue safely: {exc}. "
                             "Nothing was submitted.") from exc
        self._to(S.INSPECTING)
        return nav.inspection

    async def _submit(self, packet: ApplicationPacket) -> PageInspection:
        attempt = self.store.begin_submission(self.claim, packet_id=packet.id)
        try:
            await self.interaction.progress("Submitting the application")
            await self.browser.submit()
            observation = await self.browser.confirm()
        except BaseException as exc:
            # Interrupted or failed after SUBMITTING was recorded: the site may have the
            # application. Record it as unknown, never as a failure that permits a retry.
            with contextlib.suppress(Exception):
                self.store.record_submission_outcome(self.claim, attempt.id, SubmissionObservation(
                    outcome=SubmissionOutcome.UNKNOWN,
                    signals=[f"interrupted during submission: {type(exc).__name__}"],
                    detail="the run stopped while submitting; the outcome is not known",
                ))
            raise
        app = self.store.record_submission_outcome(self.claim, attempt.id, observation)
        if app.state is S.SUBMITTED:
            raise _Stop(_outcome(self.store, self.app_id, "Submitted; the site confirmed it. "
                                                          "Receipt saved."))
        if app.state is S.SUBMISSION_UNKNOWN:
            raise _Stop(_outcome(self.store, self.app_id,
                                 "The submit may have reached the employer, but no confirmation "
                                 "tied to this job was seen. It will not be retried; reconcile it."))
        if app.state in (S.FAILED_RETRYABLE, S.FAILED_PERMANENT):
            raise _Stop(_outcome(self.store, self.app_id,
                                 f"Not submitted: {app.failure_reason or observation.detail}"))
        # FILLING or NEEDS_INPUT: the site showed the form again (definitely not received).
        return await self.browser.inspect()


def create_runner(
    paths: LocalPaths,
    *,
    headless: bool,
    interaction: UserInteraction,
    limits: RunLimits | None = None,
) -> LocalApplicationRunner:
    """The production runner: local candidate store, factual resolver and Playwright."""
    return LocalApplicationRunner(paths=paths, interaction=interaction, headless=headless,
                                  limits=limits)


class NoninteractiveInteraction:
    """Asks nothing: missing questions are recorded as NEEDS_INPUT and browser actions
    are declined unless ``allow_browser_action`` is set (the user said they will act
    in the visible window)."""

    def __init__(self, *, allow_browser_action: bool = False) -> None:
        self.allow_browser_action = allow_browser_action
        self.messages: list[str] = []

    async def request_inputs(self, missing: Sequence[MissingInput]) -> Sequence[UserInput]:
        return []

    async def request_action(self, message: str) -> bool:
        return self.allow_browser_action

    async def progress(self, message: str) -> None:
        self.messages.append(message)


__all__ = [
    "NEEDS_INPUT_EVENT",
    "LocalApplicationRunner",
    "NoninteractiveInteraction",
    "RunLimits",
    "RunnerBusy",
    "browser_profile_lock",
    "create_runner",
    "pending_inputs",
]
