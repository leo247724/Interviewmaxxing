"""Background execution of the canonical async runner, one application at a time.

The service never implements application control flow. It records the request in the
store, then hands the work to an ``ApplicationExecutor`` (the I1 runner) on a single
background thread that owns an asyncio event loop. The runner opens its own store
connection in that thread; HTTP handler threads open theirs per request, so every
SQLite connection stays on the thread that created it.

Only one run happens at a time because all runs share the persistent browser profile.
A request that would start a second run is refused (``ExecutorBusy``) instead of
queued, so nothing acts on the user's behalf later without them seeing it.

Submission runs (``submit``) come from a separate factory, ``submission_factory``,
which exists only in a service started with ``IMX_ALLOW_SUBMISSION=1``; every other
run uses the preparation-only ``factory``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from interviewmaxxing_core import ApplyOutcome, MissingInput, UserInput

from .views import RunStatus

log = logging.getLogger("interviewmaxxing.service")

RunKind = Literal["apply", "resume", "reconcile", "submit"]


@runtime_checkable
class ApplicationExecutor(Protocol):
    """What the service needs from the I1 runner. Every method records its work
    through the canonical store and returns the stored outcome."""

    async def apply(self, application_url: str, *, candidate_id: str) -> ApplyOutcome: ...

    async def resume(self, application_id: str) -> ApplyOutcome: ...

    async def reconcile(self, application_id: str) -> ApplyOutcome:
        """Recheck a SUBMISSION_UNKNOWN application against the site in the browser,
        without resubmitting. Records ``reconcile_submission`` only on proof."""
        ...


@runtime_checkable
class SubmissionExecutor(Protocol):
    """The runner behind ``interviewmaxxing submit`` (``create_submission_runner``):
    submits exactly the approved packets of an authorized application."""

    async def submit(self, application_id: str) -> ApplyOutcome: ...


class ServiceInteraction:
    """Noninteractive ``UserInteraction`` for runs started from the frontend.

    Questions are never asked inline: the runner records NEEDS_INPUT and the frontend
    asks. A browser action (sign-in, CAPTCHA) is accepted only for a run the user
    started with Continue after being told to act in the browser window."""

    def __init__(self, *, allow_browser_action: bool) -> None:
        self.allow_browser_action = allow_browser_action
        self.last_progress: str | None = None

    async def request_inputs(self, missing: Sequence[MissingInput]) -> Sequence[UserInput]:
        return []

    async def request_action(self, message: str) -> bool:
        return self.allow_browser_action

    async def progress(self, message: str) -> None:
        # Kept in memory only; progress text can contain page content.
        self.last_progress = message


ExecutorFactory = Callable[[ServiceInteraction], ApplicationExecutor]
SubmissionFactory = Callable[[ServiceInteraction], SubmissionExecutor]
RunCall = Callable[[Any], Awaitable[ApplyOutcome]]
AfterRun = Callable[["Run"], None]


class ExecutorBusy(RuntimeError):
    def __init__(self, application_id: str) -> None:
        super().__init__("another application is running")
        self.application_id = application_id


class SubmissionUnavailable(RuntimeError):
    """A submit run was asked of a dispatcher built without a submission factory."""


@dataclass
class Run:
    application_id: str
    kind: RunKind
    started_at: datetime
    done: threading.Event = field(default_factory=threading.Event)
    outcome: ApplyOutcome | None = None
    error: BaseException | None = None

    @property
    def status(self) -> RunStatus:
        return RunStatus(kind=self.kind, started_at=self.started_at)


class Dispatcher:
    """Runs one executor call at a time on a dedicated event-loop thread."""

    def __init__(
        self, factory: ExecutorFactory, *, submission_factory: SubmissionFactory | None = None
    ) -> None:
        self._factory = factory
        self._submission_factory = submission_factory
        self.lock = threading.RLock()
        """Hold while checking ``busy`` and starting a run, so the check and the start
        are atomic with the store write between them."""
        self._current: Run | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="imx-executor", daemon=True
        )
        self._thread.start()

    @property
    def current(self) -> Run | None:
        with self.lock:
            run = self._current
            return run if run is not None and not run.done.is_set() else None

    @property
    def busy(self) -> bool:
        return self.current is not None

    @property
    def can_submit(self) -> bool:
        """A submission factory was configured (``IMX_ALLOW_SUBMISSION=1``)."""
        return self._submission_factory is not None

    def status(self, application_id: str) -> RunStatus | None:
        run = self.current
        return run.status if run is not None and run.application_id == application_id else None

    def submit(
        self,
        application_id: str,
        kind: RunKind,
        call: RunCall,
        *,
        allow_browser_action: bool = False,
        after: AfterRun | None = None,
    ) -> Run:
        if kind == "submit" and self._submission_factory is None:
            raise SubmissionUnavailable("submission is not enabled in this service")
        with self.lock:
            current = self.current
            if current is not None:
                raise ExecutorBusy(current.application_id)
            run = Run(application_id=application_id, kind=kind, started_at=datetime.now(UTC))
            self._current = run
        coro = self._execute(run, call, allow_browser_action, after)
        future: Future[None] = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(lambda _f: None)
        return run

    async def _execute(
        self, run: Run, call: RunCall, allow_browser_action: bool, after: AfterRun | None
    ) -> None:
        try:
            interaction = ServiceInteraction(allow_browser_action=allow_browser_action)
            executor: Any
            if run.kind == "submit":
                assert self._submission_factory is not None  # checked by ``submit``
                executor = self._submission_factory(interaction)
            else:
                executor = self._factory(interaction)
            run.outcome = await call(executor)
        except BaseException as exc:  # recorded and handled by ``after``
            run.error = exc
            # Never log the message: it may contain page text or personal data.
            log.warning("run %s for an application stopped: %s", run.kind, type(exc).__name__)
        finally:
            try:
                if after is not None:
                    after(run)
            except Exception as exc:
                log.warning("after-run bookkeeping failed: %s", type(exc).__name__)
            run.done.set()
            if isinstance(run.error, (KeyboardInterrupt, SystemExit)):
                raise run.error

    def shutdown(self, timeout: float = 5.0, *, cancel_timeout: float = 10.0) -> None:
        """Stop the loop.

        A run still in progress after ``timeout`` is cancelled, and the loop keeps
        running until the cancelled coroutines have finished (bounded by
        ``cancel_timeout``), so the runner's ``finally`` blocks can await their cleanup
        (close the browser, release the claim). Only then is the loop stopped. An
        interrupted submit stays durable through the store's claim and submission
        lease and becomes SUBMISSION_UNKNOWN, never a retry."""
        run = self.current
        if run is not None:
            run.done.wait(timeout)

        async def _cancel_and_wait() -> None:
            me = asyncio.current_task()
            tasks = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=cancel_timeout)

        if self._loop.is_running():
            done = asyncio.run_coroutine_threadsafe(_cancel_and_wait(), self._loop)
            try:
                done.result(cancel_timeout + 1.0)
            except Exception as exc:  # timeout or loop already closing
                log.warning("executor shutdown did not finish cleanly: %s", type(exc).__name__)
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout)
