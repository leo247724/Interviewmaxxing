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
* The claim on the application stays alive while the run waits for the user or the
  browser (questions, a sign-in, a CAPTCHA, custom controls): a heartbeat renews it
  every ``RunLimits.claim_heartbeat_s``. If the claim is lost anyway (the process was
  suspended past the TTL, or another run took over) the wait is cancelled and the run
  ends without writing anything more; every store write is fenced by the claim token.
* Each application keeps the resume it started with: the first run pins the
  profile's resume (``ApplicationStore.pin_resume``) and every later run uses the
  pinned file, whatever the profile says now. If that file is missing or changed the
  run stops; another resume is never substituted.
* One run per browser profile (an OS file lock) and per application (store claim).
* Advancing and submitting are distinct; an ambiguous step stops the run. A run
  stops after ``RunLimits.max_steps`` pages, when the same form comes back
  ``max_same_form`` times, or when one step has gone back to the user
  ``max_input_rounds`` times, so it cannot loop.
* An answer the site rejects is asked again. Rejections are persisted as
  ``validation.rejected`` events (one per inspection of a step this run acted on), so
  a correction stored later, in this run or by ``interviewmaxxing answer`` in another
  process, is used even if the site still shows the old message; a correction the
  site rejects again is asked again.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import socket
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TypeVar

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
    AnswerSource,
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
    ResumeArtifact,
    SavedAnswer,
    SubmissionObservation,
    SubmissionOutcome,
    UserInput,
    UserInteraction,
    utc_now,
)
from interviewmaxxing_generation import FactualPacketResolver

S = ApplicationState
T = TypeVar("T")

NEEDS_INPUT_EVENT = "application.needs_input"
INPUT_EVENT = "input.received"
REJECTION_EVENT = "validation.rejected"
"""Emitted by the runner (``append_event``) when a step it acted on comes back with
field validation messages: a rejection epoch for those questions."""
RUN_LOCK_NAME = ".interviewmaxxing-run.lock"


class SavedAnswerStore(CandidateLoader, Protocol):
    def save_answer(self, candidate_id: str, answer: SavedAnswer) -> None: ...


@dataclass(frozen=True, slots=True)
class RunLimits:
    max_steps: int = 12
    """Pages inspected in one run before it stops (FAILED_RETRYABLE)."""
    max_same_form: int = 3
    """Times the same form (fingerprint) may be shown in one run before it stops."""
    max_input_rounds: int = 5
    """Times one form step may go back to the user (questions or a browser action)
    before the run stops as NEEDS_INPUT with the open questions recorded."""
    user_action_timeout_s: float = 600.0
    """How long ``wait_for_user`` waits for sign-in, CAPTCHA or custom controls."""
    claim_ttl_s: float = 300.0
    """Lease on the application; renewed at every step and by the heartbeat."""
    claim_heartbeat_s: float | None = None
    """How often the claim is renewed while the run waits for the user or the
    browser. Default: a third of ``claim_ttl_s``."""

    @property
    def heartbeat_s(self) -> float:
        if self.claim_heartbeat_s is not None:
            return self.claim_heartbeat_s
        return self.claim_ttl_s / 3


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


def rejection_epochs(
    store: ApplicationStore, application_id: str
) -> tuple[dict[tuple[int, str, str], int], dict[str, int]]:
    """The persisted rejection epochs of an application.

    Returns the latest ``validation.rejected`` event sequence per question
    ``(form step, field id, field fingerprint)`` and the ``input.received`` event
    sequence of every stored user input. A rejected answer is asked again only while
    its rejection is newer than the user's answer to that question."""
    rejections, received = _rejection_history(store, application_id)
    return {key: epoch for key, (epoch, _) in rejections.items()}, received


def _rejection_history(
    store: ApplicationStore, application_id: str
) -> tuple[dict[tuple[int, str, str], tuple[int, str]], dict[str, int]]:
    """Keep each rejection's message with its epoch even after the DOM clears it."""
    rejections: dict[tuple[int, str, str], tuple[int, str]] = {}
    received: dict[str, int] = {}
    for event in store.list_events(application_id):
        if event.event == REJECTION_EVENT:
            step = event.metadata.get("form_step")
            if step is None:
                continue
            for item in event.metadata.get("fields", []):
                rejections[(int(step), item["field_id"], item["field_fingerprint"])] = (
                    event.sequence, item["message"])
        elif event.event == INPUT_EVENT:
            for item in event.metadata.get("inputs", []):
                received[item["id"]] = event.sequence
    return rejections, received


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


def _detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}".splitlines()[0][:300]


def _fail_retryable(store: ApplicationStore, claim: Claim, app: Application, message: str) -> None:
    """Record the current failure, re-entering INSPECTING when retrying a stopped run."""
    if app.state in (S.NEEDS_INPUT, S.FAILED_RETRYABLE):
        store.transition(claim, S.INSPECTING)
    if store.get_application(app.id).state is not S.FAILED_RETRYABLE:
        store.transition(claim, S.FAILED_RETRYABLE, failure_reason=message)


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
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.paths = paths
        self.interaction = interaction
        self.headless = headless
        self.browser_factory = browser_factory or PlaywrightSessionFactory()
        self.candidates: SavedAnswerStore = candidates or LocalCandidateStore.from_paths(paths)
        self.resolver = resolver or FactualPacketResolver()
        self.limits = limits or RunLimits()
        self.owner = owner or f"runner:{socket.gethostname()}:{os.getpid()}"
        self.clock = clock
        """The store's clock (claims, events). Tests use a virtual clock."""

    # --- public API -------------------------------------------------------------------

    async def apply(self, application_url: str, *, candidate_id: str) -> ApplyOutcome:
        """Record the request (idempotent) and run it unless the stored state forbids
        it. A repeated request for a submitted, in-flight or uncertain application
        returns that state without touching the browser."""
        with self._store() as store:
            result = store.record_request(candidate_id, application_url)
            app_id = result.application.id
            if not result.may_proceed:
                return self._blocked(store, app_id)
            return await self._run(store, app_id, application_url)

    async def resume(self, application_id: str) -> ApplyOutcome:
        """Continue a stopped application (missing input answered, sign-in done, a
        retryable failure) from a fresh inspection of the site."""
        with self._store() as store:
            app = store.get_application(application_id)
            if app.state in SUBMISSION_BLOCKING_STATES or app.state in TERMINAL_STATES:
                return self._blocked(store, application_id)
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
                        try:
                            browser = await self._start_browser(application_id)
                        except Exception as exc:
                            return _outcome(store, application_id, self._browser_start_failed(exc)
                                            + " The application stays SUBMISSION_UNKNOWN and "
                                            "nothing was resubmitted; reconcile again afterwards.")
                        try:
                            observation = await browser.reconcile(  # type: ignore[attr-defined]
                                url, tie=tie, lookup_email=email)
                        except Exception as exc:
                            with contextlib.suppress(Exception):
                                store.append_event(claim, "reconcile.failed", {"error": _detail(exc)})
                            return _outcome(store, application_id,
                                            f"Could not re-check the site ({_detail(exc)}). The "
                                            "application stays SUBMISSION_UNKNOWN and nothing was "
                                            "resubmitted; reconcile again later.")
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
        store = ApplicationStore.open(self.paths.state_db, clock=self.clock)
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

    @staticmethod
    def _browser_start_failed(exc: BaseException) -> str:
        return (f"Could not start the browser ({_detail(exc)}). Nothing was submitted; check "
                "the browser installation (`playwright install chromium`).")

    def _lookup_email(self, candidate_id: str) -> str | None:
        try:
            return self.candidates.load(candidate_id).identity.email
        except (CandidateNotFound, CandidateProfileInvalid):
            return None

    def _blocked(self, store: ApplicationStore, app_id: str) -> ApplyOutcome:
        """The stored state forbids a run. An interrupted submit whose lease has lapsed
        is settled as SUBMISSION_UNKNOWN here (never retried) so the user is pointed
        at ``reconcile``; one whose lease is still live may belong to a running
        process and is left alone."""
        app = store.get_application(app_id)
        if app.state is S.SUBMITTING:
            store.recover_interrupted_submissions()
            app = store.get_application(app_id)
        if app.state is S.SUBMITTING:
            until = (app.claim_expires_at.strftime("%Y-%m-%d %H:%M:%SZ")
                     if app.claim_expires_at else "unknown")
            return _outcome(store, app_id,
                            f"A submit is in progress in another run ({app.claim_owner or 'unknown'}"
                            f", lease until {until}). It will not be repeated. If that run is "
                            "gone, wait for the lease to lapse, then reconcile.")
        return _outcome(store, app_id, _STATE_MESSAGES.get(app.state, "This application cannot be run."))

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
                    try:
                        candidate = self.candidates.load(app.candidate_id)
                    except (CandidateNotFound, CandidateProfileInvalid) as exc:
                        # Stored failures are also shown by the HTTP service; loader
                        # errors can contain private absolute profile/resume paths.
                        detail = ("the profile could not be found" if isinstance(exc, CandidateNotFound)
                                  else "the profile or its resume could not be read or validated")
                        message = (f"Candidate profile unavailable: {detail}. "
                                   "Restore a valid profile.json and its resume, then resume.")
                        _fail_retryable(store, claim, app, message)
                        return _outcome(store, app_id, message)
                    pinned = store.pin_resume(app_id, candidate.resume)
                    if not pinned.verify():
                        return self._pinned_resume_missing(store, claim, app, pinned)
                    candidate = candidate.model_copy(update={"resume": pinned})
                    try:
                        browser = await self._start_browser(app_id)
                    except Exception as exc:
                        # A local problem (Chromium missing, profile locked by another
                        # browser): recorded, retryable, never a traceback.
                        message = self._browser_start_failed(exc) + " Then resume."
                        _fail_retryable(store, claim, app, message)
                        return _outcome(store, app_id, message)
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
        _fail_retryable(store, claim, app, message)
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
        self.acted_steps: set[int] = set()
        """Steps this run filled, advanced or submitted since their last inspection.
        Validation messages on such a step are new rejections (an epoch); messages on
        any other inspection (a fresh open, a page the user operated) are not."""

    @property
    def app_id(self) -> str:
        return self.claim.application_id

    def app(self) -> Application:
        return self.store.get_application(self.app_id)

    # state helpers ---------------------------------------------------------------------

    def _renew(self) -> None:
        self.claim = self.store.renew(self.claim, ttl=self.runner._ttl)

    async def _fenced(self, awaitable: Awaitable[T]) -> T:
        """Await a wait on the user or the browser while a heartbeat renews the claim
        every ``RunLimits.heartbeat_s``. If the claim is lost meanwhile (expired, or
        another run took it over) the wait is cancelled and ``ClaimLost`` is raised,
        so this run never acts on what the user did after losing the application."""
        waiting: asyncio.Future[T] = asyncio.ensure_future(awaitable)
        lost: ClaimLost | None = None

        async def heartbeat() -> None:
            nonlocal lost
            while True:
                await asyncio.sleep(self.limits.heartbeat_s)
                try:
                    self._renew()
                except ClaimLost as exc:
                    lost = exc
                    waiting.cancel()
                    return

        beat = asyncio.create_task(heartbeat())
        try:
            return await waiting
        except asyncio.CancelledError:
            if lost is not None:
                raise lost from None
            raise
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
            if not waiting.done():
                waiting.cancel()
                with contextlib.suppress(BaseException):
                    await waiting

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
            return _outcome(self.store, self.app_id,
                            "This run no longer holds the application (its claim lapsed or another "
                            "run took over), so it stopped without recording anything more. "
                            "Resume to continue.")
        except Exception as exc:
            # A browser or page failure before any submit: stop, keep it retryable.
            # (Failures during a submit were already recorded as SUBMISSION_UNKNOWN.)
            detail = _detail(exc)
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
        expected = self.store.expected_job_identity(self.app_id)
        if expected is not None:
            if page.job_identity is None:
                raise self._stop(S.FAILED_RETRYABLE, "Could not verify that the application page "
                                 "belongs to the selected job. Open its specific application page "
                                 "and resume; nothing was submitted.")
            if page.job_identity.identity_key != expected:
                raise self._stop(S.FAILED_RETRYABLE, "The application page identifies a different "
                                 "job from the one selected. Check the selected job's application "
                                 "link and resume; nothing was submitted.")
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

    async def _verify_expected_page(self) -> None:
        """Recheck a pinned selection after waits/fills, immediately before acting.
        An earlier step's identity cannot authorize a different page after navigation."""
        if self.store.expected_job_identity(self.app_id) is None:
            return
        page = await self._fenced(self.browser.inspect())
        self._renew()
        if page.kind is not PageKind.APPLICATION_FORM or page.form is None:
            raise self._stop(S.FAILED_RETRYABLE, "The selected job's application form is no longer "
                             "visible. Open its application page and resume; nothing was submitted.")
        self._bind_identity(page)

    async def _user_action(self, page: PageInspection, needs: list[MissingInput]) -> PageInspection:
        message = "; ".join(n.prompt for n in needs) or (page.message or "Action needed in the browser")
        if not await self._fenced(self.interaction.request_action(message)):
            raise self._stop(S.NEEDS_INPUT, message, reason="user action required", missing=needs)
        self._renew()
        after = await self._fenced(
            self.browser.wait_for_user(message, self.limits.user_action_timeout_s))
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

    def _note_rejections(self, form: ApplicationForm) -> None:
        """Persist the field validation messages of a step this run acted on as one
        ``validation.rejected`` event, the rejection epoch of those questions. A step
        counts as acted on once, until the next fill/advance/submit."""
        if form.step not in self.acted_steps:
            return
        self.acted_steps.discard(form.step)
        rejected = [f for f in form.fields if f.validation_error]
        if not rejected:
            return
        self.store.append_event(self.claim, REJECTION_EVENT, {
            "form_url": form.url,
            "form_step": form.step,
            "fields": [{"field_id": f.id, "field_fingerprint": f.fingerprint,
                        "message": f.validation_error} for f in rejected],
        })

    def _with_rejections(self, form: ApplicationForm, packet: ApplicationPacket) -> ApplicationPacket:
        """Turn answers the site rejected into questions for the user.

        A persisted rejection applies to the same question even if a fresh page no
        longer displays its message. Only a scoped user input stored after its epoch
        is a correction; it is used even if the page still shows the old message. If
        the site rejects the correction too, the newer epoch asks again."""
        flagged = [f for f in form.fields if packet.answer_for(f.id) is not None]
        if not flagged:
            return packet
        rejections, received = _rejection_history(self.store, self.app_id)
        rejected: list[Any] = []
        for f in flagged:
            rejection = rejections.get((form.step, f.id, f.fingerprint))
            if rejection is None:
                continue  # never rejected after one of our actions: a stale message
            epoch, message = rejection
            answer = packet.answer_for(f.id)
            assert answer is not None
            if answer.provenance.source is AnswerSource.USER_INPUT and any(
                received.get(ref, -1) > epoch for ref in answer.provenance.reference_ids
            ):
                continue  # corrected after the rejection
            rejected.append(f.model_copy(update={"validation_error": message}))
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
        self._note_rejections(form)
        packet = await self._resolve(form)
        self.store.save_packet(self.claim, packet)
        rounds = 0
        while not packet.is_complete:
            required = [m for m in packet.missing_inputs if m.required]
            unsupported = [m for m in required if m.reason is MissingReason.UNSUPPORTED_CONTROL]
            questions = [m for m in required if m.reason is not MissingReason.UNSUPPORTED_CONTROL]
            rounds += 1
            if rounds > self.limits.max_input_rounds:
                raise self._stop(S.NEEDS_INPUT, f"{len(required)} required question(s) are still "
                                 f"open after {self.limits.max_input_rounds} attempts; answer them "
                                 "and resume.", reason="input rounds exhausted",
                                 missing=packet.missing_inputs)
            if questions:
                inputs = list(await self._fenced(self.interaction.request_inputs(questions)))
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
            if not questions and self._still_unoperated(unsupported, packet):
                raise self._stop(S.NEEDS_INPUT, "Still waiting for you in the browser: "
                                 + "; ".join(m.prompt for m in unsupported),
                                 reason="user action not completed", missing=packet.missing_inputs)
        return await self._act(form, packet)

    @staticmethod
    def _still_unoperated(waited_for: list[MissingInput], packet: ApplicationPacket) -> bool:
        """True when every custom control the user was waited for is still required and
        unoperated: the wait ended (timeout, or the user did something else) without
        progress, so the run must stop instead of waiting again."""
        still = {(m.field_id, m.field_fingerprint) for m in packet.missing_inputs
                 if m.required and m.reason is MissingReason.UNSUPPORTED_CONTROL}
        waited = {(m.field_id, m.field_fingerprint) for m in waited_for}
        return bool(waited) and waited <= still

    def _accept_inputs(self, questions: list[MissingInput], inputs: list[UserInput]) -> bool:
        wanted = {(m.field_id, m.field_fingerprint) for m in questions}
        usable = [u for u in inputs if (u.field_id, u.field_fingerprint) in wanted]
        if not usable:
            return False
        self._renew()
        self.store.save_user_inputs(self.claim, usable)
        job = self.store.get_job(self.app().job_id)
        for user_input in usable:
            saved = user_input.to_saved_answer(job=job)
            if saved is not None:
                self.runner.candidates.save_answer(self.app().candidate_id, saved)
        return True

    async def _act(self, form: ApplicationForm, packet: ApplicationPacket) -> PageInspection:
        await self._verify_expected_page()
        self._to(S.PACKET_READY)
        self._to(S.FILLING)
        self.acted_steps.add(form.step)
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
        await self._verify_expected_page()
        try:
            nav = await self.browser.advance()
        except (SubmissionRefused, AmbiguousAction) as exc:
            raise self._stop(S.FAILED_RETRYABLE, f"Cannot tell how to continue safely: {exc}. "
                             "Nothing was submitted.") from exc
        self._to(S.INSPECTING)
        return nav.inspection

    async def _submit(self, packet: ApplicationPacket) -> PageInspection:
        has_expected_job = self.store.expected_job_identity(self.app_id) is not None
        if has_expected_job:
            await self.interaction.progress("Submitting the application")
            await self._verify_expected_page()
        attempt = self.store.begin_submission(self.claim, packet_id=packet.id)
        try:
            if not has_expected_job:
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
    "REJECTION_EVENT",
    "LocalApplicationRunner",
    "NoninteractiveInteraction",
    "RunLimits",
    "RunnerBusy",
    "browser_profile_lock",
    "create_runner",
    "pending_inputs",
    "rejection_epochs",
]
