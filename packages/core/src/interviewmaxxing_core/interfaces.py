"""Public service interfaces implemented by the downstream packages.

* ``CandidateLoader``         — packages/candidate   (candidate-brain)
* ``PacketResolver``          — packages/generation  (application-packets)
* ``BrowserSessionFactory`` / ``ApplicationBrowser`` / ``ATSAdapter``
                              — packages/browser, packages/ats (browser-ats)
* ``UserInteraction`` / ``ApplicationRunner`` — apps/cli (core, task I1)

Browser-facing and packet interfaces are asynchronous. Implementations import the
contracts from ``interviewmaxxing_core`` and never redeclare them. The runner, not
any service, owns state transitions: services return observations and the runner
records them through ``ApplicationStore``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from ._base import Contract, NonEmptyStr
from .applications import Application, ApplicationState, Receipt
from .candidate import CandidateProfile
from .execution import (
    FillResult,
    NavigationResult,
    PageInspection,
    SubmissionObservation,
    SubmitActionResult,
)
from .forms import ApplicationForm
from .jobs import JobRecord
from .packets import ApplicationPacket, MissingInput, UserInput

# --- candidate ---------------------------------------------------------------------


class CandidateNotFound(LookupError):
    pass


class CandidateProfileInvalid(ValueError):
    """The profile exists but is incomplete or unverified (e.g. missing resume)."""


@runtime_checkable
class CandidateLoader(Protocol):
    def load(self, candidate_id: str) -> CandidateProfile:
        """Load the verified profile, resume reference and saved answers from the
        local profile directory. Raises ``CandidateNotFound`` or
        ``CandidateProfileInvalid``; never fabricates missing data."""
        ...


# --- packets -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PacketContext:
    """Everything the resolver may use. Nothing else is a valid source of answers."""

    application: Application
    job: JobRecord
    form: ApplicationForm
    candidate: CandidateProfile
    user_inputs: Sequence[UserInput] = ()


@runtime_checkable
class PacketResolver(Protocol):
    async def resolve(self, context: PacketContext) -> ApplicationPacket:
        """Answer ``context.form`` from verified data and user inputs.

        Every answer carries provenance. Required questions without a grounded answer
        (and every ``EXPLICIT_ANSWER_REQUIRED`` type lacking a saved answer or user
        input) are returned as ``missing_inputs``; they are never guessed. The
        returned packet must satisfy ``packet.problems_against(context.form) == []``.
        """
        ...


# --- browser -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrowserOptions:
    artifacts_dir: Path
    """Where this application's evidence goes (``LocalPaths.application_artifacts``)."""
    artifacts_root: Path
    """``LocalPaths.artifacts_dir``; ``EvidenceRef.path`` is relative to this."""
    profile_dir: Path | None = None
    """Persistent browser profile (``LocalPaths.browser_dir``) so sign-ins persist."""
    headless: bool = False
    """The MVP shows the browser so the user can sign in or solve a CAPTCHA."""
    slow_mo_ms: int = 0


@runtime_checkable
class ApplicationBrowser(Protocol):
    """One browser session driving one application. Observes and acts; never decides
    that an application was submitted."""

    async def open(self, url: str) -> PageInspection:
        """Navigate to the user's URL (following an apply button to the form if
        needed) and inspect the resulting page."""
        ...

    async def inspect(self) -> PageInspection:
        """Inspect the current page without changing it."""
        ...

    async def fill(self, form: ApplicationForm, packet: ApplicationPacket) -> FillResult:
        """Fill and upload every answered field on the current step, then read the
        values back. Must not click next or submit."""
        ...

    async def advance(self) -> NavigationResult:
        """Go to the next step of a multi-step form. Must refuse (raise) when the
        step's primary action would submit the application."""
        ...

    async def submit(self) -> SubmitActionResult:
        """Dispatch the final submit action once. Only called after
        ``ApplicationStore.begin_submission`` returned."""
        ...

    async def confirm(self) -> SubmissionObservation:
        """Observe the result of ``submit``: ACCEPTED only with concrete site
        acceptance signals; NOT_SUBMITTED only with proof; otherwise UNKNOWN."""
        ...

    async def wait_for_user(self, reason: str, timeout_s: float | None = None) -> PageInspection:
        """Let the user act in the visible browser (sign-in, CAPTCHA), then re-inspect."""
        ...

    async def close(self) -> None: ...


@runtime_checkable
class BrowserSessionFactory(Protocol):
    async def start(self, options: BrowserOptions) -> ApplicationBrowser:
        """Launch a browser session. The caller closes it with ``close()``."""
        ...


@runtime_checkable
class ATSAdapter(Protocol):
    """ATS-specific behavior used inside the browser runtime (ARCHITECTURE.md §10).
    ``page`` is a Playwright ``Page``; core does not depend on Playwright."""

    name: str

    async def detect(self, page: Any) -> bool: ...

    async def inspect(self, page: Any) -> PageInspection: ...

    async def fill(self, page: Any, form: ApplicationForm, packet: ApplicationPacket) -> FillResult: ...

    async def next(self, page: Any) -> NavigationResult: ...

    async def submit(self, page: Any) -> SubmitActionResult: ...

    async def detect_submission(self, page: Any) -> SubmissionObservation: ...


# --- CLI integration ---------------------------------------------------------------


@runtime_checkable
class UserInteraction(Protocol):
    """How the runner reaches the user (the CLI's prompts)."""

    async def request_inputs(self, missing: Sequence[MissingInput]) -> Sequence[UserInput]:
        """Ask the user the missing questions. May return a subset (user deferred)."""
        ...

    async def request_action(self, message: str) -> bool:
        """Ask the user to act in the browser (sign in, solve CAPTCHA). Returns False
        if the user declined."""
        ...

    async def progress(self, message: str) -> None: ...


class ApplyOutcome(Contract):
    """What ``apply``/``resume`` report back. ``state`` is the stored state; success
    is claimed only when it is SUBMITTED and ``receipt`` is present."""

    application_id: NonEmptyStr
    state: ApplicationState
    receipt: Receipt | None = None
    missing_inputs: list[MissingInput] = Field(default_factory=list)
    message: str = ""

    @property
    def submitted(self) -> bool:
        return self.state is ApplicationState.SUBMITTED and self.receipt is not None


@runtime_checkable
class ApplicationRunner(Protocol):
    async def apply(self, application_url: str, *, candidate_id: str) -> ApplyOutcome: ...

    async def resume(self, application_id: str) -> ApplyOutcome: ...
