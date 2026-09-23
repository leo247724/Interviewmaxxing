"""Browser runtime for Interviewmaxxing: inspect, fill, navigate, submit, confirm.

Public entry points:

* :class:`PlaywrightSessionFactory` — ``BrowserSessionFactory``; ``await
  factory.start(BrowserOptions(...))`` returns a :class:`PlaywrightApplicationBrowser`
  (an ``ApplicationBrowser``).
* :class:`GenericApplicationBrowser` — the same runtime over any :class:`PageDriver`
  (the seam for a user-present OpenCLI driver).
* :class:`GenericAdapter` — ``ATSAdapter`` for native, accessible forms.
* :meth:`GenericApplicationBrowser.reconcile` and :func:`reconciliation_from` —
  re-read the site to settle a ``SUBMISSION_UNKNOWN`` application.
* :mod:`~interviewmaxxing_browser.needs` — user-action and unsupported-control items.
* :class:`OpenCliSessionFactory` / :class:`OpenCliDriver` — the same runtime in the
  user's own Chrome through OpenCLI Browser Bridge (owned background tab).
"""

from .adapter import GenericAdapter
from .driver import DriverError, NotActionable, PageContextLost, PageDriver, PlaywrightDriver
from .needs import attestation_fields, unsupported_control_needs, user_action_needs
from .normalize import PageModel, build_page, detect_ats, extract_job_identity
from .opencli import (
    DEFAULT_SESSION,
    CapabilityUnsupported,
    CommandResult,
    OpenCliApplicationBrowser,
    OpenCliConfig,
    OpenCliContextLost,
    OpenCliDriver,
    OpenCliError,
    OpenCliSessionFactory,
    OpenCliTargetError,
    OpenCliTimeout,
    OpenCliUnavailable,
    UnverifiedAction,
)
from .runtime import (
    ActionPolicy,
    AmbiguousAction,
    ConfirmationTie,
    GenericApplicationBrowser,
    SubmissionRefused,
    reconciliation_from,
)
from .semantics import classify
from .session import PlaywrightApplicationBrowser, PlaywrightSessionFactory
from .snapshot import DomSnapshot, inspector_script

__all__ = [
    "DEFAULT_SESSION",
    "ActionPolicy",
    "AmbiguousAction",
    "CapabilityUnsupported",
    "CommandResult",
    "ConfirmationTie",
    "DomSnapshot",
    "DriverError",
    "GenericAdapter",
    "GenericApplicationBrowser",
    "NotActionable",
    "OpenCliApplicationBrowser",
    "OpenCliConfig",
    "OpenCliContextLost",
    "OpenCliDriver",
    "OpenCliError",
    "OpenCliSessionFactory",
    "OpenCliTargetError",
    "OpenCliTimeout",
    "OpenCliUnavailable",
    "PageContextLost",
    "PageDriver",
    "PageModel",
    "PlaywrightApplicationBrowser",
    "PlaywrightDriver",
    "PlaywrightSessionFactory",
    "SubmissionRefused",
    "UnverifiedAction",
    "attestation_fields",
    "build_page",
    "classify",
    "detect_ats",
    "extract_job_identity",
    "inspector_script",
    "reconciliation_from",
    "unsupported_control_needs",
    "user_action_needs",
]
