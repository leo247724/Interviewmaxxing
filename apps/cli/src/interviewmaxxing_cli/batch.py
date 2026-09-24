"""Bulk preparation harness: prepare many Saved jobs to their final review step.

``run_batch`` takes an inventory of Saved jobs and prepares each one through the
ordinary ``interviewmaxxing apply URL --json --headless`` command, one subprocess
per job, so the canonical runner, the store claims, the durable no-submit
restriction and the evidence apply unchanged. Nothing here talks to a browser or
to the site directly, and nothing here can submit.

Guarantees:

* Never submits. Every job runs through the preparation-only ``apply`` command; a
  fully prepared application stays ``NEEDS_INPUT`` with a ``preparation.ready``
  event, exactly as a single ``apply`` would leave it. No option of this harness
  can enable submission.
* Bounded parallelism. At most ``BatchOptions.workers`` subprocesses run at once.
  Each concurrent slot owns its own browser profile directory
  (``$IMX_HOME/browser-workers/w<slot>``, via ``IMX_BROWSER_DIR``), because the
  runner's OS lock is per browser directory. Every worker shares the same
  ``IMX_HOME``, state database, candidate profile and artifacts directory, which
  are passed explicitly so per-path overrides are preserved. OpenCLI drives one
  owned Chrome session, so ``--browser opencli`` is limited to one worker.
* Durable, resumable ledger. Each finished job appends one JSON line to
  ``<batch dir>/ledger.jsonl`` (owner-only). Running the same batch id again skips
  rows whose latest entry is settled and retries only ``failed_retryable`` and
  ``error`` rows, at most ``retry_retryable`` extra times.
* Nothing private is printed. Subprocess stdout is parsed, never echoed; stderr is
  kept only as a truncated failure message in the ledger under ``IMX_HOME``.
* A hung job is killed (its whole process group) after ``per_job_timeout_s`` and
  recorded as ``error``; the store then holds a lapsing claim the next ``apply`` or
  ``resume`` of that application takes over.
* Pipeline bookkeeping (``sync_pipeline_card``). Once a job's outcome is known, and
  before its ledger line is written, a row with a ``pipeline_id`` has its application
  linked to that Saved card (``PipelineItem.application_id``, the link the service's
  handoff writes), and a job that no longer accepts applications moves its card from
  Saved to Closed with a dated history note. Cards are never created, another
  application's link is never overwritten, and no card is ever moved to Applied.

``build_report`` reads the ledgers back (``interviewmaxxing batch-report``): outcomes,
holds grouped into categories and by question wording (with the ``answer`` line that
clears each question), fill failures grouped by detail, a per-backend readiness table,
durations and pipeline links. ``prepare-batch --retry`` (``interviewmaxxing_cli.retry``)
runs the held and failed applications of a ledger again through ``resume`` with the same
harness; its ledger lines carry ``retry_of``.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import Field, ValidationError, model_validator

from interviewmaxxing_core import (
    Application,
    ApplicationState,
    ApplicationStore,
    ApplyOutcome,
    Contract,
    InvalidApplicationUrl,
    LocalPaths,
    MissingInput,
    NotFound,
    normalize_application_url,
)

from .dynamic import DynamicOptions
from .triage import (
    HOLD_CATEGORIES,
    LEDGER_LABEL_LIMIT,
    NO_FORM_PREFIX,
    PROG,
    QUESTION_LIMIT,
    FailureGroup,
    FailureOccurrence,
    FieldFailure,
    HoldGroup,
    HoldOccurrence,
    categorize_hold,
    group_failures,
    group_holds,
    render_failure_groups,
    render_hold_groups,
    stored_failure,
    truncate,
    without_cost_note,
)

if TYPE_CHECKING:
    from interviewmaxxing_pipeline import BoardLanes, PipelineItem, PipelineStore

S = ApplicationState

BatchOutcome = Literal[
    "prepared",
    "needs_input",
    "failed_retryable",
    "closed",
    "duplicate",
    "blocked",
    "error",
    "already_recorded",
]
"""How one job ended in this batch:

``prepared``          NEEDS_INPUT at the final review step (``preparation.ready``).
``needs_input``       NEEDS_INPUT earlier: questions, sign-in, CAPTCHA, custom controls.
``failed_retryable``  FAILED_RETRYABLE (browser error, ambiguous control, a loop).
``closed``            FAILED_PERMANENT (the job no longer accepts applications).
``duplicate``         DUPLICATE, or the site/store already has this application.
``blocked``           a submission state; impossible in prepare-only, classified anyway.
``error``             the subprocess printed no parseable outcome, or timed out.
``already_recorded``  skipped: the store already holds an application for this URL.
"""

OUTCOMES: tuple[BatchOutcome, ...] = (
    "prepared", "needs_input", "failed_retryable", "closed", "duplicate", "blocked", "error",
    "already_recorded",
)
SETTLED_OUTCOMES: frozenset[str] = frozenset(
    {"prepared", "needs_input", "closed", "duplicate", "blocked", "already_recorded"}
)
"""Latest-ledger outcomes that a rerun of the same batch never launches again."""
RETRYABLE_OUTCOMES: frozenset[str] = frozenset({"failed_retryable", "error"})

PREPARED_PREFIX = "Prepared to the final review step"
"""The runner's message for a completed preparation (``runner._submit``)."""
DEFAULT_STATUSES: frozenset[str] = frozenset({"resolved"})
LEDGER_NAME = "ledger.jsonl"
SUMMARY_NAME = "summary.json"
WORKERS_DIR = "browser-workers"
MESSAGE_LIMIT = 300
LABEL_LIMIT = LEDGER_LABEL_LIMIT
TERM_GRACE_S = 15.0
"""How long a timed-out job may take to exit after SIGTERM before SIGKILL."""
_LAUNCH_STATES: frozenset[ApplicationState] = frozenset(
    {S.FAILED_RETRYABLE, S.REQUESTED, S.INSPECTING, S.PACKET_READY, S.FILLING}
)
"""Stored states that do not count as ``already_recorded``: ``apply`` resumes them (a run
stopped mid-fill, e.g. by a timeout, is left in PACKET_READY or FILLING)."""
SAVED_LANE = "saved"
CLOSED_LANE = "closed"
LINK_ATTEMPTS = 3
"""Reads and revision-checked writes of one card before a concurrent edit wins."""
NOTE_REASON_LIMIT = 200
CLOSED_WORDING = "The job is no longer accepting applications"
"""How the runner reports a job it saw closed (a JOB_CLOSED page). Only a FAILED_PERMANENT
stop with this reason moves a Saved card to Closed; any other permanent failure leaves
the card where it is."""
REPORT_LABEL_LIMIT = QUESTION_LIMIT
"""Question wording in ``batch-report`` output; the ledger keeps ``LABEL_LIMIT``."""
BUSY_MESSAGES = ("Another run is working on this application.",
                 "another run is using the browser profile")
"""Outcome messages of a job that did not run because another run held the application
(a lapsing claim, e.g. after a timeout) or the slot's browser profile."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def default_batch_id(now: datetime | None = None) -> str:
    return (now or _now()).astimezone(UTC).strftime("batch-%Y%m%dT%H%M%SZ")


def _plain_batch_id(batch_id: str) -> str:
    if not batch_id or batch_id in (".", "..") or "/" in batch_id or "\\" in batch_id:
        raise ValueError(f"batch id must be a plain name, got {batch_id!r}")
    return batch_id


def private_dirs(path: Path) -> Path:
    """Create ``path`` and each missing parent one level at a time, every one owner-only
    (0700): ``mkdir(parents=True, mode=...)`` gives the parents it creates the default
    mode. Existing directories are left as they are."""
    missing: list[Path] = []
    current = path
    while not current.exists() and current != current.parent:
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
    return path


def default_batch_dir(paths: LocalPaths, batch_id: str) -> Path:
    """``$IMX_HOME/batches/<batch id>``, created owner-only at every level, after the
    local directories (``LocalPaths.ensure``)."""
    directory = paths.home / "batches" / _plain_batch_id(batch_id)
    paths.ensure()
    return private_dirs(directory)


def default_command() -> list[str]:
    """The installed ``interviewmaxxing`` console script next to this interpreter when
    it exists (what the end-to-end suite runs), else ``python -m interviewmaxxing_cli``."""
    script = Path(sys.executable).with_name("interviewmaxxing")
    if script.exists():
        return [str(script)]
    return [sys.executable, "-m", "interviewmaxxing_cli"]


# --- inventory ------------------------------------------------------------------------


class BatchRow(Contract):
    """One Saved job from the application URL inventory, or one application of an
    earlier ledger that ``prepare-batch --retry`` runs again."""

    listing_id: str
    pipeline_id: str | None = None
    company: str = ""
    title: str = ""
    application_url: str
    backend: str = ""
    status: str = ""
    application_id: str | None = None
    """Set for a retry: the stored application that ``interviewmaxxing resume`` continues.
    None runs ``interviewmaxxing apply`` on the URL."""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _optional_text(value: Any) -> str | None:
    text = _text(value) if not isinstance(value, int | float) or isinstance(value, bool) else str(value)
    return text or None


def read_inventory(path: Path, *, backends: set[str] | None = None,
                   statuses: Iterable[str] = DEFAULT_STATUSES,
                   limit: int | None = None) -> tuple[list[BatchRow], int]:
    """``load_inventory`` plus the number of rows skipped for a missing or invalid
    ``source_application_url``."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        data = data["rows"]
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of inventory rows")
    wanted_statuses = {s.strip() for s in statuses if s.strip()}
    wanted_backends = {b.strip() for b in backends if b.strip()} if backends else None
    rows: list[tuple[str, int, BatchRow]] = []
    invalid = 0
    for ordinal, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        status = _text(item.get("status"))
        backend = _text(item.get("backend"))
        if wanted_statuses and status not in wanted_statuses:
            continue
        if wanted_backends is not None and backend not in wanted_backends:
            continue
        url = _text(item.get("source_application_url"))
        if not url:
            invalid += 1
            continue
        try:
            normalized = normalize_application_url(url)
        except InvalidApplicationUrl:
            invalid += 1
            continue
        rows.append((backend, ordinal, BatchRow(
            listing_id=_optional_text(item.get("listing_id")) or f"url:{normalized}",
            pipeline_id=_optional_text(item.get("pipeline_id")),
            company=_text(item.get("company")), title=_text(item.get("title")),
            application_url=url, backend=backend, status=status,
        )))
    rows.sort(key=lambda entry: (entry[0], entry[1]))
    selected = [row for _, _, row in rows]
    if limit is not None:
        selected = selected[:max(limit, 0)]
    return selected, invalid


def load_inventory(path: Path, *, backends: set[str] | None = None,
                   statuses: Iterable[str] = DEFAULT_STATUSES,
                   limit: int | None = None) -> list[BatchRow]:
    """Rows of a JSON inventory (a list of objects with ``listing_id``,
    ``pipeline_id``, ``company``, ``title``, ``source_application_url``, ``backend``
    and ``status``; other keys are ignored), filtered to the wanted statuses and
    backends, ordered by backend then by position in the file, then cut to ``limit``.
    Rows without a valid http(s) ``source_application_url`` are skipped."""
    rows, _ = read_inventory(path, backends=backends, statuses=statuses, limit=limit)
    return rows


# --- ledger ---------------------------------------------------------------------------


class MissingItem(Contract):
    """One recorded question or action of a run, with its reason and control paired
    (``missing_labels``/``missing_reasons`` keep them apart), so holds can be grouped."""

    label: str
    """The question wording, whitespace collapsed, truncated to ``LABEL_LIMIT``."""
    reason: str
    """A ``MissingReason`` value."""
    control_type: str | None = None
    """A ``ControlType`` value (``TYPEAHEAD`` for a lookup), when the runner knew it."""
    field_id: str | None = None
    """The question's field id (what ``interviewmaxxing answer APP --set FIELD=...``
    takes); None for a browser action, and on lines written before it was recorded."""
    semantic_type: str | None = None
    """A ``SemanticType`` value; None on lines written before it was recorded."""


class LedgerEntry(Contract):
    """One finished (or skipped) job, as appended to ``ledger.jsonl``. Lives under
    ``IMX_HOME``: the labels of missing questions are private.

    Fields added later have defaults, so earlier ledger lines still read."""

    batch_id: str
    listing_id: str
    pipeline_id: str | None = None
    company: str = ""
    title: str = ""
    application_url: str
    backend: str = ""
    status: str = ""
    attempt: int = Field(ge=1)
    worker_slot: int | None = None
    application_id: str | None = None
    state: ApplicationState | None = None
    outcome: BatchOutcome
    message: str = ""
    missing_reasons: list[str] = Field(default_factory=list)
    missing_labels: list[str] = Field(default_factory=list)
    missing_items: list[MissingItem] = Field(default_factory=list)
    exit_code: int | None = None
    started_at: datetime
    finished_at: datetime
    duration_s: float = Field(ge=0.0)
    linked: bool | None = None
    """Whether the application is linked to the row's pipeline card; None when the row
    has no ``pipeline_id`` (or the line predates linking)."""
    link_reason: str | None = None
    """Why it is not linked, when ``linked`` is False."""
    linked_application_id: str | None = None
    """The application the card links to: this run's application, or the one it was
    found to duplicate (the application ``record_request`` returns for the URL)."""
    closed_synced: bool | None = None
    """For a job that no longer accepts applications, with a card: True when its card
    was moved from Saved to Closed, False (with ``closed_sync_reason``) when it could not
    be. None otherwise: no card, not closed, ``--no-sync-closed``, or the card was
    already in Closed."""
    closed_sync_reason: str | None = None
    provider_cost_usd: float | None = None
    """Known AI provider cost of this application so far (every ``provider.budget`` event
    in the store), read after the job finished; None when no provider was used."""
    provider_calls: int | None = None
    """AI provider calls of this application so far; None when no provider was used."""
    retry_of: str | None = None
    """The batch whose ledger this ``prepare-batch --retry`` line ran again; None for a
    line of an ordinary batch."""
    previous_outcome: str | None = None
    """For a retry: where the application stood when it was selected
    (``needs_input``, ``failed_retryable``, ``unknown`` or ``error``)."""
    holds_before: int | None = None
    """For a retry: the questions and actions the application was held on when selected."""
    holds_cleared: int | None = None
    """For a retry: how many of ``holds_before`` no longer hold it after this run (all of
    them when it is now prepared, matched by question wording when it is held again).
    None when the run ended any other way."""


_truncate = truncate


def _open_private_append(path: Path) -> int:
    private_dirs(path.parent)
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)


def append_ledger(path: Path, entry: LedgerEntry) -> None:
    """Append one JSON line (owner-only file; one ``write`` per entry). A last line cut
    short by a crash mid-write is ended first, so this entry does not join it (and get
    lost with it); the cut line stays unreadable (``read_ledger_lines`` counts it)."""
    line = json.dumps(entry.model_dump(mode="json"), sort_keys=True) + "\n"
    fd = _open_private_append(path)
    try:
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            line = "\n" + line
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def read_ledger_lines(path: Path) -> tuple[list[LedgerEntry], int]:
    """Every readable entry in file order, and the number of non-blank lines that could
    not be read: a truncated last line (a crash mid-write), a hand edit, or a line written
    by a newer version with fields this one does not know. Their rows look unrecorded, so
    a rerun launches them again and ``--max-prepared`` does not count them."""
    if not path.exists():
        return [], 0
    entries: list[LedgerEntry] = []
    ignored = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(LedgerEntry.model_validate_json(line))
        except ValidationError:
            ignored += 1
    return entries, ignored


def read_ledger(path: Path) -> list[LedgerEntry]:
    """Every readable entry in file order (``read_ledger_lines`` without the count)."""
    return read_ledger_lines(path)[0]


def classify(outcome: ApplyOutcome) -> BatchOutcome:
    """Map a CLI ``ApplyOutcome`` (of ``apply`` or ``resume``) to a batch outcome. A job
    that did not run because another run held the application or the browser profile
    is ``failed_retryable``, whatever the stored state it reports."""
    state = outcome.state
    if any(outcome.message.startswith(busy) for busy in BUSY_MESSAGES):
        return "failed_retryable"
    if state is S.NEEDS_INPUT:
        return "prepared" if outcome.message.startswith(PREPARED_PREFIX) else "needs_input"
    if state is S.FAILED_PERMANENT:
        return "closed"
    if state is S.DUPLICATE or "already has application" in outcome.message:
        return "duplicate"
    if state in (S.SUBMITTING, S.SUBMISSION_UNKNOWN, S.SUBMITTED, S.WITHDRAWN):
        return "blocked"
    return "failed_retryable"


# --- options ----------------------------------------------------------------------------


class BatchOptions(Contract):
    """Everything ``run_batch`` needs; combinations that cannot work are rejected
    on construction (``ValueError``, also surfaced as a pydantic ``ValidationError``)."""

    model_config = Contract.model_config | {"arbitrary_types_allowed": True}

    paths: LocalPaths
    candidate_id: str
    batch_id: str
    workers: int = Field(default=1, ge=1, le=8)
    per_job_timeout_s: float = Field(default=900.0, gt=0)
    retry_retryable: int = Field(default=1, ge=0, le=2)
    max_prepared: int | None = Field(default=None, ge=1)
    """A hard bound on ``prepared`` applications in the batch (retained review
    pages): a job is launched only while the prepared count plus the jobs still
    running is below it, and launching stops once the count is reached."""
    include_existing: bool = False
    sync_closed: bool = True
    """Move the linked Saved card of a job that no longer accepts applications to
    Closed (``sync_pipeline_card``)."""
    browser: Literal["playwright", "opencli"] = "playwright"
    opencli_profile: str | None = None
    ai_routing: bool = False
    env_file: Path | None = None
    writer_model: str | None = None
    rag_connection_file: Path | None = None
    headless: bool = True
    command: list[str] = Field(default_factory=list)
    """argv prefix of the CLI; ``apply URL --json ...`` is appended. Empty means
    ``default_command()``, resolved when a job is launched."""
    retry_of: str | None = None
    """Set by ``prepare-batch --retry``: the batch whose applications this batch runs
    again. Every ledger line carries it."""

    @model_validator(mode="after")
    def _check(self) -> Self:
        self.check()
        return self

    @property
    def dynamic(self) -> DynamicOptions:
        return DynamicOptions(browser=self.browser, opencli_profile=self.opencli_profile,
                              ai_routing=self.ai_routing, env_file=self.env_file,
                              writer_model=self.writer_model,
                              rag_connection_file=self.rag_connection_file)

    def check(self) -> None:
        """Raise ``ValueError`` for combinations that cannot work."""
        if not self.batch_id.strip():
            raise ValueError("batch id must not be empty")
        if self.browser == "opencli" and self.workers != 1:
            raise ValueError("--browser opencli drives one owned Chrome session; use --workers 1")
        self.dynamic.validate()

    @property
    def batch_dir(self) -> Path:
        return self.paths.home / "batches" / self.batch_id

    def dynamic_argv(self) -> list[str]:
        """The run flags exactly as ``interviewmaxxing apply`` expects them."""
        argv: list[str] = []
        if self.headless:
            argv.append("--headless")
        if self.browser != "playwright":
            argv += ["--browser", self.browser]
        if self.opencli_profile:
            argv += ["--opencli-profile", self.opencli_profile]
        if self.ai_routing:
            argv.append("--ai-routing")
        if self.env_file is not None:
            argv += ["--env-file", str(self.env_file)]
        if self.writer_model:
            argv += ["--writer-model", self.writer_model]
        if self.rag_connection_file is not None:
            argv += ["--rag-connection-file", str(self.rag_connection_file)]
        return argv

    def argv(self, url: str) -> list[str]:
        return [*(self.command or default_command()), "apply", url, "--json",
                "--candidate", self.candidate_id, *self.dynamic_argv()]

    def resume_argv(self, application_id: str) -> list[str]:
        """``resume APP --json`` with the same run flags as ``apply``: a stored
        application keeps its own candidate and its preparation-only restriction."""
        return [*(self.command or default_command()), "resume", application_id, "--json",
                *self.dynamic_argv()]

    def job_argv(self, row: BatchRow) -> list[str]:
        return self.resume_argv(row.application_id) if row.application_id else self.argv(
            row.application_url)

    def run_options(self) -> BatchRunOptions:
        """What ``summary.json`` records so that ``prepare-batch --retry`` runs this
        batch's applications with the same harness settings."""
        return BatchRunOptions(
            candidate_id=self.candidate_id, workers=self.workers,
            per_job_timeout_s=self.per_job_timeout_s, retry_retryable=self.retry_retryable,
            max_prepared=self.max_prepared, include_existing=self.include_existing,
            sync_closed=self.sync_closed, browser=self.browser,
            opencli_profile=self.opencli_profile, ai_routing=self.ai_routing,
            env_file=str(self.env_file) if self.env_file is not None else None,
            writer_model=self.writer_model,
            rag_connection_file=(str(self.rag_connection_file)
                                 if self.rag_connection_file is not None else None),
            headless=self.headless)

    def worker_browser_dir(self, slot: int) -> Path:
        return self.paths.home / WORKERS_DIR / f"w{slot}"

    def environment(self, slot: int) -> dict[str, str]:
        """The parent environment minus every ``IMX_*`` variable, then this batch's
        paths and the slot's own browser directory."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("IMX_")}
        env["IMX_HOME"] = str(self.paths.home)
        env["IMX_CANDIDATE_ID"] = self.candidate_id
        env["IMX_PROFILE_DIR"] = str(self.paths.profile_dir)
        env["IMX_STATE_DB"] = str(self.paths.state_db)
        env["IMX_ARTIFACTS_DIR"] = str(self.paths.artifacts_dir)
        env["IMX_BROWSER_DIR"] = str(self.worker_browser_dir(slot))
        return env


# --- summary ----------------------------------------------------------------------------


class LabelCount(Contract):
    label: str
    count: int = Field(ge=1)


class BatchRunOptions(Contract):
    """The harness settings of a batch run, recorded in ``summary.json``. ``prepare-batch
    --retry`` takes the worker count, timeout, retry count, closed-card sync, candidate
    and runtime flags from here unless they are given again."""

    candidate_id: str
    workers: int = Field(ge=1, le=8)
    per_job_timeout_s: float = Field(gt=0)
    retry_retryable: int = Field(ge=0, le=2)
    max_prepared: int | None = None
    include_existing: bool = False
    sync_closed: bool = True
    browser: Literal["playwright", "opencli"] = "playwright"
    opencli_profile: str | None = None
    ai_routing: bool = False
    env_file: str | None = None
    writer_model: str | None = None
    rag_connection_file: str | None = None
    headless: bool = True


class RetryStats(Contract):
    """What a ``prepare-batch --retry`` batch selected and what its runs cleared."""

    retry_of: str
    """The batch whose ledger was run again."""
    outcomes: list[str] = Field(default_factory=list)
    """The outcomes selected (``--outcomes``)."""
    include_explicit: bool = False
    considered: int = Field(default=0, ge=0)
    """Listings in that ledger (after ``--backends``)."""
    selected: int = Field(default=0, ge=0)
    """Listings chosen to run again (after ``--limit``)."""
    skipped: dict[str, int] = Field(default_factory=dict)
    """Why the others were not: ``prepared``, ``closed``, ``duplicate``, ``blocked``,
    ``not selected (<outcome>)``, ``explicit answers only``, ``application not found``,
    ``same application as another listing``, ``over --limit``."""
    retried: int = Field(default=0, ge=0)
    """Listings of this batch with a finished retry line (each at its latest line)."""
    prepared: int = Field(default=0, ge=0)
    """Of those, now prepared to the final review step."""
    holds_before: int = Field(default=0, ge=0)
    """Questions and actions the retried applications were held on when selected."""
    holds_cleared: int = Field(default=0, ge=0)
    """How many of those no longer hold them."""
    holds_open: int = Field(default=0, ge=0)
    """Questions and actions holding the retried applications now (new ones included)."""
    transitions: dict[str, dict[str, int]] = Field(default_factory=dict)
    """Previous outcome -> outcome of this batch -> listings."""
    ledger_lines_ignored: int = Field(default=0, ge=0)
    """Unreadable lines in the retried batch's ledger (``read_ledger_lines``)."""


class BatchSummary(Contract):
    batch_id: str
    started_at: datetime
    finished_at: datetime
    rows: int = Field(ge=0)
    """Inventory rows considered by this run (after filtering and limits)."""
    launched: int = Field(ge=0)
    """Jobs this run actually started as subprocesses."""
    skipped_settled: int = Field(ge=0)
    """Rows skipped because an earlier run of this batch already settled them."""
    skipped_invalid_url: int = Field(ge=0)
    skipped_same_url: int = Field(default=0, ge=0)
    """Rows not launched because an earlier row of this run has the same normalized URL
    (or, in a retry, the same application): run together they would collide on one
    application. A later run of the batch records them (``already_recorded``)."""
    ledger_lines_ignored: int = Field(default=0, ge=0)
    """Ledger lines that could not be read (``read_ledger_lines``); their rows count as
    unrecorded, so they run again and ``--max-prepared`` does not count them."""
    totals: dict[str, int] = Field(default_factory=dict)
    """Latest outcome per row, counted."""
    by_backend: dict[str, dict[str, int]] = Field(default_factory=dict)
    duration_median_s: float | None = None
    duration_p95_s: float | None = None
    top_missing_labels: list[LabelCount] = Field(default_factory=list)
    top_missing_reasons: list[LabelCount] = Field(default_factory=list)
    application_ids: dict[str, list[str]] = Field(default_factory=dict)
    ledger_path: str
    stopped_at_max_prepared: bool = False
    pipeline_linked: int = Field(default=0, ge=0)
    """Rows whose application is linked to their pipeline card."""
    pipeline_not_linked: int = Field(default=0, ge=0)
    pipeline_closed: int = Field(default=0, ge=0)
    """Rows whose card was moved from Saved to Closed."""
    pipeline_problems: list[LabelCount] = Field(default_factory=list)
    """Why cards were not linked or not moved (a move skipped only because the link
    failed is counted once, under the link reason)."""
    run_options: BatchRunOptions | None = None
    """The harness settings of the latest run (None in summaries written before they
    were recorded)."""
    retry: RetryStats | None = None
    """Set for a ``prepare-batch --retry`` batch."""


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def latest_entries(entries: Iterable[LedgerEntry]) -> dict[str, LedgerEntry]:
    """The latest ledger entry per listing id, in first-seen order."""
    latest: dict[str, LedgerEntry] = {}
    for entry in entries:
        latest[entry.listing_id] = entry
    return latest


def _card_problems(linked: Iterable[LedgerEntry], closed: Iterable[LedgerEntry]) -> list[LabelCount]:
    """Link reasons of not-linked entries plus reasons of skipped Closed moves; a move
    skipped only because the link failed is already counted under the link reason."""
    problems: Counter[str] = Counter()
    for entry in linked:
        if entry.linked is False and entry.link_reason:
            problems[entry.link_reason] += 1
    for entry in closed:
        if entry.closed_synced is False and entry.linked is not False and entry.closed_sync_reason:
            problems[entry.closed_sync_reason] += 1
    return [LabelCount(label=reason, count=count) for reason, count in problems.most_common()]


def retry_stats(selection: RetryStats, latest: Iterable[LedgerEntry]) -> RetryStats:
    """``selection`` completed from the retry lines among each listing's latest entry."""
    retried = [e for e in latest if e.previous_outcome is not None]
    transitions: dict[str, Counter[str]] = defaultdict(Counter)
    for entry in retried:
        transitions[entry.previous_outcome or ""][entry.outcome] += 1
    return selection.model_copy(update={
        "retried": len(retried),
        "prepared": sum(1 for e in retried if e.outcome == "prepared"),
        "holds_before": sum(e.holds_before or 0 for e in retried),
        "holds_cleared": sum(e.holds_cleared or 0 for e in retried),
        "holds_open": sum(len(e.missing_items) for e in retried if e.outcome == "needs_input"),
        "transitions": {previous: {k: counts[k] for k in OUTCOMES if counts[k]}
                        for previous, counts in transitions.items()},
    })


def summarize(batch_id: str, entries: Sequence[LedgerEntry], *, started_at: datetime,
              finished_at: datetime, rows: int, launched: int, skipped_settled: int,
              skipped_invalid_url: int, ledger_path: Path,
              stopped_at_max_prepared: bool = False,
              run_options: BatchRunOptions | None = None,
              retry: RetryStats | None = None, skipped_same_url: int = 0,
              ledger_lines_ignored: int = 0) -> BatchSummary:
    """Aggregate a ledger: totals and per-backend counts use each row's latest entry;
    durations and missing-input tallies use every launched attempt in this ledger. For
    a retry batch, ``retry`` (what was selected) is completed from its lines."""
    latest = latest_entries(entries)
    totals: Counter[str] = Counter(e.outcome for e in latest.values())
    by_backend: dict[str, Counter[str]] = defaultdict(Counter)
    ids: dict[str, list[str]] = defaultdict(list)
    for entry in latest.values():
        by_backend[entry.backend or "(none)"][entry.outcome] += 1
        if entry.application_id:
            ids[entry.outcome].append(entry.application_id)
    launched_entries = [e for e in entries if e.outcome != "already_recorded"]
    durations = [e.duration_s for e in launched_entries]
    labels: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for entry in latest.values():
        labels.update(entry.missing_labels)
        reasons.update(entry.missing_reasons)
    return BatchSummary(
        batch_id=batch_id, started_at=started_at, finished_at=finished_at, rows=rows,
        launched=launched, skipped_settled=skipped_settled,
        skipped_invalid_url=skipped_invalid_url, skipped_same_url=skipped_same_url,
        ledger_lines_ignored=ledger_lines_ignored,
        totals={k: totals[k] for k in OUTCOMES if totals[k]},
        by_backend={backend: {k: counts[k] for k in OUTCOMES if counts[k]}
                    for backend, counts in sorted(by_backend.items())},
        duration_median_s=round(statistics.median(durations), 2) if durations else None,
        duration_p95_s=round(_percentile(durations, 0.95), 2) if durations else None,
        top_missing_labels=[LabelCount(label=label, count=count)
                            for label, count in labels.most_common(15)],
        top_missing_reasons=[LabelCount(label=reason, count=count)
                             for reason, count in reasons.most_common()],
        application_ids={k: ids[k] for k in OUTCOMES if ids[k]},
        ledger_path=str(ledger_path),
        stopped_at_max_prepared=stopped_at_max_prepared,
        pipeline_linked=sum(1 for e in latest.values() if e.linked is True),
        pipeline_not_linked=sum(1 for e in latest.values() if e.linked is False),
        pipeline_closed=sum(1 for e in latest.values() if e.closed_synced is True),
        pipeline_problems=_card_problems(latest.values(), latest.values()),
        run_options=run_options,
        retry=retry_stats(retry, latest.values()) if retry is not None else None,
    )


def read_summary(paths: LocalPaths, batch_id: str) -> BatchSummary | None:
    """The batch's ``summary.json``, or None when it is missing or unreadable. Raises
    ``ValueError`` for a batch id that is not a plain name. Creates nothing."""
    path = paths.home / "batches" / _plain_batch_id(batch_id) / SUMMARY_NAME
    try:
        return BatchSummary.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError):
        return None


def write_summary(summary: BatchSummary, batch_dir: Path) -> Path:
    """Write ``summary.json`` owner-only (replacing any earlier one) and return its path."""
    path = batch_dir / SUMMARY_NAME
    private_dirs(batch_dir)
    text = json.dumps(summary.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    tmp.replace(path)
    return path


def render_summary_markdown(summary: BatchSummary) -> str:
    total = sum(summary.totals.values())
    lines = [
        f"# Batch {summary.batch_id}",
        "",
        f"- started: {_iso(summary.started_at)}",
        f"- finished: {_iso(summary.finished_at)}",
        f"- rows considered: {summary.rows}; launched this run: {summary.launched}; "
        f"already settled: {summary.skipped_settled}; invalid URL: {summary.skipped_invalid_url}"
        + (f"; same URL as another row: {summary.skipped_same_url}"
           if summary.skipped_same_url else ""),
        "- nothing was submitted (preparation only)"
        + ("; stopped launching at --max-prepared" if summary.stopped_at_max_prepared else ""),
        *([f"- unreadable ledger lines ignored: {summary.ledger_lines_ignored} (their rows "
           "count as unrecorded)"] if summary.ledger_lines_ignored else []),
        "",
        "## Totals",
        "",
        "| outcome | count |",
        "| --- | --- |",
    ]
    lines += [f"| {k} | {v} |" for k, v in summary.totals.items()]
    lines.append(f"| **all** | {total} |")
    if summary.by_backend:
        columns = [k for k in OUTCOMES if any(k in c for c in summary.by_backend.values())]
        lines += ["", "## By backend", "", "| backend | " + " | ".join(columns) + " |",
                  "| --- |" + " --- |" * len(columns)]
        lines += ["| " + _cell(backend) + " | " + " | ".join(str(counts.get(k, 0)) for k in columns)
                  + " |" for backend, counts in summary.by_backend.items()]
    if summary.duration_median_s is not None:
        lines += ["", f"- duration per job: median {summary.duration_median_s}s, "
                      f"p95 {summary.duration_p95_s}s"]
    if summary.pipeline_linked or summary.pipeline_not_linked:
        lines += ["", f"- pipeline cards: {summary.pipeline_linked} linked, "
                      f"{summary.pipeline_not_linked} not linked, "
                      f"{summary.pipeline_closed} moved to Closed"]
        if summary.pipeline_problems:
            lines.append("- pipeline problems: " + ", ".join(
                f"{p.label} ({p.count})" for p in summary.pipeline_problems))
    if summary.top_missing_reasons:
        lines += ["", "## Missing input", "",
                  "reasons: " + ", ".join(f"{r.label} ({r.count})" for r in summary.top_missing_reasons)]
        lines += ["", "| question | count |", "| --- | --- |"]
        lines += [f"| {_cell(_truncate(item.label, REPORT_LABEL_LIMIT))} | {item.count} |"
                  for item in summary.top_missing_labels]
    if summary.retry is not None:
        lines += ["", *render_retry_markdown(summary.retry)]
    lines += ["", f"ledger: {summary.ledger_path}"]
    return "\n".join(lines) + "\n"


def render_retry_markdown(retry: RetryStats) -> list[str]:
    skipped = ", ".join(f"{reason} ({count})" for reason, count in retry.skipped.items())
    lines = [f"## Retry of {retry.retry_of}", "",
             f"- selected {retry.selected} of {retry.considered} listing(s) "
             f"(outcomes: {', '.join(retry.outcomes) or '-'}"
             + ("; explicit-only holds included" if retry.include_explicit else "") + ")",
             f"- skipped: {skipped or 'none'}",
             f"- retried: {retry.retried}; now prepared: {retry.prepared}",
             f"- holds cleared: {retry.holds_cleared} of {retry.holds_before}; "
             f"open now: {retry.holds_open}"]
    if retry.transitions:
        columns = [k for k in OUTCOMES if any(k in c for c in retry.transitions.values())]
        lines += ["", "| was | " + " | ".join(columns) + " |",
                  "| --- |" + " --- |" * len(columns)]
        lines += ["| " + previous + " | " + " | ".join(str(counts.get(k, 0)) for k in columns)
                  + " |" for previous, counts in retry.transitions.items()]
    return lines


# --- state reads -------------------------------------------------------------------------


class StateReader:
    """The harness's one connection to the existing state database, for its own reads:
    whether a row's URL already has an application, an application's provider cost, and
    the canonical application a card is linked to. Opened on first use and only once the
    database exists (never created), then used only on this reader's own thread, because
    SQLite connections are bound to the thread that opened them: the event loop awaits
    ``run(fn)``, other threads (card bookkeeping) block on ``call(fn)``. A batch thus opens
    the store, and runs its schema check (a write transaction), once instead of two or
    three times per job while other jobs write to it. Reads are short; no card or pipeline
    write ever runs on this thread."""

    def __init__(self, paths: LocalPaths) -> None:
        self.paths = paths
        self.opens = 0
        """Times the store was opened (at most once)."""
        self._store: ApplicationStore | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="imx-batch-state")

    def _with_store[T](self, fn: Callable[[ApplicationStore | None], T]) -> T:
        if self._store is None and self.paths.state_db.is_file():
            self._store = ApplicationStore.open(self.paths.state_db)
            self.opens += 1
        return fn(self._store)

    def call[T](self, fn: Callable[[ApplicationStore | None], T]) -> T:
        """``fn(store)`` on the reader's thread (``store`` is None without a database);
        blocks the calling thread, which must not be the reader's own."""
        return self._executor.submit(self._with_store, fn).result()

    async def run[T](self, fn: Callable[[ApplicationStore | None], T]) -> T:
        return await asyncio.wrap_future(self._executor.submit(self._with_store, fn))

    def close(self) -> None:
        def shut() -> None:
            if self._store is not None:
                self._store.close()
                self._store = None

        self._executor.submit(shut).result()
        self._executor.shutdown()


def _read[T](paths: LocalPaths, reader: StateReader | None,
             fn: Callable[[ApplicationStore | None], T]) -> T:
    """``fn`` over the existing state database: through ``reader`` when given, else on a
    connection of its own; ``fn(None)`` when there is no database. Never creates it."""
    if reader is not None:
        return reader.call(fn)
    if not paths.state_db.is_file():
        return fn(None)
    with ApplicationStore.open(paths.state_db) as store:
        return fn(store)


# --- pipeline cards -----------------------------------------------------------------------


class _Refused(Exception):
    """A link or move that must not happen; the message is the ledger reason."""


def _closes(entry: LedgerEntry) -> bool:
    """The row's application stopped FAILED_PERMANENT: ``closed`` in this run or, for
    ``already_recorded``, in an earlier one. Whether that stop saw the job closed is
    ``_closed_reason``'s question."""
    return entry.outcome == "closed" or (
        entry.outcome == "already_recorded" and entry.state is S.FAILED_PERMANENT)


def _stored_failure_reason(paths: LocalPaths, application_id: str | None,
                           reader: StateReader | None = None) -> str | None:
    def read(store: ApplicationStore | None) -> str | None:
        if store is None or not application_id:
            return None
        return store.get_application(application_id).failure_reason

    try:
        return _read(paths, reader, read)
    except Exception:
        return None


def _closed_reason(paths: LocalPaths, entry: LedgerEntry, target: Application | None,
                   reader: StateReader | None = None) -> str | None:
    """The reason a FAILED_PERMANENT row gives when it is the runner's closed wording
    (``CLOSED_WORDING``), without the provider cost note; None for any other row,
    including a permanent failure for another reason. A ``closed`` row gives its CLI
    message; an ``already_recorded`` one its stored application's failure reason."""
    if not _closes(entry):
        return None
    if entry.outcome == "closed":
        reason = entry.message
    elif target is not None:
        reason = target.failure_reason or ""
    else:
        reason = _stored_failure_reason(paths, entry.application_id, reader) or ""
    reason = without_cost_note(reason).strip()
    return reason if reason.startswith(CLOSED_WORDING) else None


def _pipeline_db(paths: LocalPaths) -> Path:
    """The existing pipeline database; a batch never creates it. The pipeline package
    is a workspace member the CLI does not declare, so it is imported on use."""
    try:
        from interviewmaxxing_pipeline import default_pipeline_db
    except ImportError as exc:
        raise _Refused("pipeline package not installed") from exc
    db = default_pipeline_db(paths)
    if not db.is_file():
        raise _Refused("no pipeline database")
    return db


def _link_target(paths: LocalPaths, candidate_id: str, entry: LedgerEntry,
                 reader: StateReader | None = None) -> Application:
    """The canonical application for the row's URL: what ``record_request`` (and so the
    service's handoff) returns for it. It must be the entry's own application, or the
    application the entry's one was found to duplicate."""
    application_id = entry.application_id
    assert application_id is not None

    def read(store: ApplicationStore | None) -> tuple[Application, Application | None] | None:
        if store is None:
            return None
        try:
            app = store.get_application(application_id)
        except NotFound:
            return None
        return app, store.find_application(candidate_id, entry.application_url)

    found = _read(paths, reader, read)
    if found is None or found[0].candidate_id != candidate_id:
        raise _Refused("application not found")
    app, current = found
    if current is None or (current.id != app.id and app.duplicate_of != current.id):
        raise _Refused("application does not match the row's URL")
    return current


def _card(store: PipelineStore, candidate_id: str, pipeline_id: str) -> PipelineItem:
    from interviewmaxxing_pipeline import ItemNotFound

    try:
        return store.get_item(candidate_id, pipeline_id)
    except ItemNotFound:
        raise _Refused("card not found") from None


def _link_card(paths: LocalPaths, candidate_id: str, entry: LedgerEntry,
               reader: StateReader | None = None) -> Application:
    """Link the row's card to the canonical application for its URL (idempotent) and
    return that application. The write is the service handoff's own: ``application_id``
    through the revision-checked ``update_item``; another link is never replaced."""
    if not entry.application_id:
        raise _Refused("no application id")
    assert entry.pipeline_id is not None
    db = _pipeline_db(paths)
    target = _link_target(paths, candidate_id, entry, reader)
    from interviewmaxxing_pipeline import PipelineStore, PipelineUpdate, RevisionConflict

    real_listing = not entry.listing_id.startswith("url:")
    with PipelineStore.open(db) as store:
        for _ in range(LINK_ATTEMPTS):
            card = _card(store, candidate_id, entry.pipeline_id)
            if card.listing_id and real_listing and card.listing_id != entry.listing_id:
                raise _Refused("card is for another listing")
            if card.application_id == target.id:
                return target
            if card.application_id is not None:
                raise _Refused("card links another application")
            try:
                store.update_item(candidate_id, card.id, PipelineUpdate(application_id=target.id),
                                  expected_revision=card.revision)
            except RevisionConflict:
                continue
            return target
    raise _Refused("card kept changing")


def _lane_id(lanes: BoardLanes, wanted: str) -> str | None:
    """The lane with id ``wanted``, else the one labelled so (case-insensitively)."""
    if lanes.get(wanted) is not None:
        return wanted
    return next((lane.id for lane in lanes.lanes if lane.label.casefold() == wanted), None)


def _closed_note(entry: LedgerEntry, target: Application, reason: str) -> str:
    """The observed reason and date, for the card's history."""
    # already_recorded: the stored application observed it earlier.
    observed = entry.finished_at if entry.outcome == "closed" else target.updated_at
    return (f"Observed closed on {observed.astimezone(UTC):%Y-%m-%d} (UTC) by prepare-batch "
            f"{entry.batch_id}: {_truncate(reason, NOTE_REASON_LIMIT)} (application {target.id})")


def _close_card(paths: LocalPaths, candidate_id: str, entry: LedgerEntry,
                target: Application, reason: str) -> bool:
    """Move the card linked to ``target`` from Saved to Closed with a dated note, through
    the revision-checked ``move_item``; only the lane changes. False when the card is
    already in Closed (nothing to do, so a rerun records no problem)."""
    assert entry.pipeline_id is not None
    db = _pipeline_db(paths)
    note = _closed_note(entry, target, reason)
    from interviewmaxxing_pipeline import PipelineStore, RevisionConflict

    with PipelineStore.open(db) as store:
        lanes = store.lanes(candidate_id)
        closed = _lane_id(lanes, CLOSED_LANE)
        if closed is None:
            raise _Refused("board has no Closed lane")
        saved = _lane_id(lanes, SAVED_LANE)
        for _ in range(LINK_ATTEMPTS):
            card = _card(store, candidate_id, entry.pipeline_id)
            if card.application_id != target.id:
                raise _Refused("card not linked")
            if card.lane == closed:
                return False
            if card.lane != saved:
                raise _Refused(f"card not in Saved (in {card.lane})")
            try:
                store.move_item(candidate_id, card.id, closed, expected_revision=card.revision,
                                note=note)
            except RevisionConflict:
                continue
            return True
    raise _Refused("card kept changing")


def sync_pipeline_card(paths: LocalPaths, candidate_id: str, entry: LedgerEntry, *,
                       sync_closed: bool = True,
                       reader: StateReader | None = None) -> LedgerEntry:
    """Link the entry's application to the row's pipeline card and, when the run saw the
    job closed (and ``sync_closed``), move that card from Saved to Closed. Only a
    FAILED_PERMANENT stop with the runner's closed wording (``CLOSED_WORDING``) counts as
    closed; a permanent failure for any other reason leaves the card in its lane with
    ``closed_synced`` None. Returns the entry with ``linked``/``link_reason``/
    ``linked_application_id`` and ``closed_synced``/``closed_sync_reason`` set;
    unchanged without ``pipeline_id``.

    Never creates a card or the pipeline database, never replaces another
    application's link, never changes a card field other than ``application_id`` and
    the Saved -> Closed lane, and never raises: a failure becomes the recorded reason.
    ``reader`` (the harness's ``StateReader``) serves the state database reads."""
    if entry.pipeline_id is None:
        return entry
    target: Application | None = None
    update: dict[str, Any] = {"linked": False, "linked_application_id": None}
    try:
        target = _link_card(paths, candidate_id, entry, reader)
        update.update(linked=True, link_reason=None, linked_application_id=target.id)
    except _Refused as refused:
        update["link_reason"] = str(refused)
    except Exception as exc:  # the ledger line must still be written
        update["link_reason"] = f"link error: {type(exc).__name__}"
    reason = _closed_reason(paths, entry, target, reader) if sync_closed else None
    if reason is not None:
        try:
            if target is None:
                raise _Refused("card not linked")
            if _close_card(paths, candidate_id, entry, target, reason):
                update.update(closed_synced=True, closed_sync_reason=None)
        except _Refused as refused:
            update.update(closed_synced=False, closed_sync_reason=str(refused))
        except Exception as exc:
            update.update(closed_synced=False, closed_sync_reason=f"move error: {type(exc).__name__}")
    return entry.model_copy(update=update)


# --- the run ----------------------------------------------------------------------------


def _existing_application(store: ApplicationStore | None, candidate_id: str,
                          url: str) -> tuple[str, ApplicationState] | None:
    """The stored application for ``url`` when it would not be run again by ``apply``."""
    app = store.find_application(candidate_id, url) if store is not None else None
    if app is None or app.state in _LAUNCH_STATES:
        return None
    return app.id, app.state


def _entry(options: BatchOptions, row: BatchRow, *, attempt: int, slot: int | None,
           outcome: BatchOutcome, started_at: datetime, message: str = "",
           application_id: str | None = None, state: ApplicationState | None = None,
           missing_reasons: Sequence[str] = (), missing_labels: Sequence[str] = (),
           missing_items: Sequence[MissingItem] = (),
           exit_code: int | None = None) -> LedgerEntry:
    finished_at = _now()
    return LedgerEntry(
        batch_id=options.batch_id, **row.model_dump(exclude={"application_id"}),
        attempt=attempt, worker_slot=slot, retry_of=options.retry_of,
        application_id=application_id or row.application_id, state=state, outcome=outcome,
        message=_truncate(message, MESSAGE_LIMIT),
        missing_reasons=list(missing_reasons),
        missing_labels=[_truncate(label, LABEL_LIMIT) for label in missing_labels],
        missing_items=[item.model_copy(update={"label": _truncate(item.label, LABEL_LIMIT)})
                       for item in missing_items],
        exit_code=exit_code, started_at=started_at, finished_at=finished_at,
        duration_s=max((finished_at - started_at).total_seconds(), 0.0),
    )


PROVIDER_EVENT = "provider.budget"
"""The runner's per-run provider usage event (``interviewmaxxing_cli.runner``)."""


def _provider_cost_in(store: ApplicationStore | None,
                      application_id: str | None) -> tuple[float | None, int | None]:
    if store is None or application_id is None:
        return None, None
    try:
        events = [e for e in store.list_events(application_id) if e.event == PROVIDER_EVENT]
    except Exception:
        return None, None
    if not events:
        return None, None
    cost = sum(float(e.metadata.get("known_cost_usd") or 0.0) for e in events)
    calls = sum(int(e.metadata.get("calls") or 0) for e in events)
    return round(cost, 6), calls


def provider_cost(paths: LocalPaths, application_id: str | None, *,
                  reader: StateReader | None = None) -> tuple[float | None, int | None]:
    """Known provider cost and calls recorded for an application (all of its runs), or
    ``(None, None)`` when it has no ``provider.budget`` event."""
    if application_id is None:
        return None, None
    return _read(paths, reader, lambda store: _provider_cost_in(store, application_id))


async def _terminate(proc: asyncio.subprocess.Process) -> str:
    """SIGTERM the job's process group (the CLI and its browser), then SIGKILL, waiting
    ``TERM_GRACE_S`` after each. Returns how it ended: ``exited`` (before any signal),
    ``stopped`` (by SIGTERM), ``killed`` (by SIGKILL, SIGTERM having been ignored) or
    ``running`` (it outlived both waits and may still be running)."""
    if proc.returncode is not None:
        return "exited"
    for sig, ending in ((signal.SIGTERM, "stopped"), (signal.SIGKILL, "killed")):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return "exited" if sig is signal.SIGTERM else "stopped"
        try:
            await asyncio.wait_for(proc.wait(), TERM_GRACE_S)
            return ending
        except TimeoutError:
            continue
    return "running"


def _timeout_message(timeout_s: float, ending: str, pid: int) -> str:
    how = {
        "exited": "the run had already exited",
        "stopped": "the run was stopped (SIGTERM)",
        "killed": f"the run ignored SIGTERM for {TERM_GRACE_S:g} s and was killed (SIGKILL)",
    }.get(ending, f"the run did not exit after SIGTERM and SIGKILL (process group {pid} "
                  "may still be running)")
    return f"timed out after {timeout_s:g} s; {how}; nothing was submitted"


async def _run_one(options: BatchOptions, row: BatchRow, *, attempt: int, slot: int,
                   reader: StateReader) -> LedgerEntry:
    started_at = _now()
    private_dirs(options.worker_browser_dir(slot))
    try:
        proc = await asyncio.create_subprocess_exec(
            *options.job_argv(row), env=options.environment(slot),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
    except OSError as exc:
        return _entry(options, row, attempt=attempt, slot=slot, outcome="error",
                      started_at=started_at, message=f"could not start the CLI: {exc}")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), options.per_job_timeout_s)
    except TimeoutError:
        ending = await _terminate(proc)
        return _entry(options, row, attempt=attempt, slot=slot, outcome="error",
                      started_at=started_at, exit_code=proc.returncode,
                      message=_timeout_message(options.per_job_timeout_s, ending, proc.pid))
    except asyncio.CancelledError:
        await _terminate(proc)
        raise
    code = proc.returncode
    printed = stdout.decode("utf-8", "replace").strip()
    try:
        outcome = ApplyOutcome.model_validate_json(printed)
    except (ValidationError, ValueError):
        detail = stderr.decode("utf-8", "replace").strip() or (
            f"the CLI printed no readable outcome (exit {code})" if printed
            else f"the CLI printed nothing (exit {code})")
        return _entry(options, row, attempt=attempt, slot=slot, outcome="error",
                      started_at=started_at, exit_code=code, message=detail)
    entry = _entry(
        options, row, attempt=attempt, slot=slot, outcome=classify(outcome),
        started_at=started_at, message=outcome.message, application_id=outcome.application_id,
        state=outcome.state, exit_code=code,
        missing_reasons=sorted({m.reason.value for m in outcome.missing_inputs}),
        missing_labels=[m.label for m in outcome.missing_inputs],
        missing_items=[MissingItem(label=m.label, reason=m.reason.value,
                                   control_type=m.control_type.value if m.control_type else None,
                                   field_id=m.field_id, semantic_type=m.semantic_type.value)
                       for m in outcome.missing_inputs],
    )
    application_id = outcome.application_id
    cost, calls = await reader.run(lambda store: _provider_cost_in(store, application_id))
    return entry.model_copy(update={"provider_cost_usd": cost, "provider_calls": calls})


def _job_keys(row: BatchRow) -> set[str]:
    """What two rows run at the same time must not share: the normalized URL (one
    application per job) and, for a retry row, the application itself."""
    try:
        url = normalize_application_url(row.application_url)
    except InvalidApplicationUrl:
        url = row.application_url
    return {f"url:{url}", *([f"app:{row.application_id}"] if row.application_id else [])}


def plan(options: BatchOptions, rows: Sequence[BatchRow], history: Sequence[LedgerEntry]
         ) -> tuple[list[tuple[BatchRow, int]], int, int]:
    """Which rows to launch (with their attempt number) given the ledger so far; how
    many rows are skipped as already settled or out of retries; and how many because a
    row planned before them has the same normalized URL (or, in a retry, application).
    Run together they would collide on one application's claim; a later run of the
    batch records them (``already_recorded``) once the first one has finished."""
    latest = latest_entries(history)
    attempts: Counter[str] = Counter(
        e.listing_id for e in history if e.outcome != "already_recorded")
    launch: list[tuple[BatchRow, int]] = []
    skipped = same = 0
    seen: set[str] = set()
    queued: set[str] = set()
    for row in rows:
        if row.listing_id in seen:
            continue
        seen.add(row.listing_id)
        previous = latest.get(row.listing_id)
        if previous is None:
            attempt = 1
        elif previous.outcome in RETRYABLE_OUTCOMES and attempts[row.listing_id] <= options.retry_retryable:
            attempt = attempts[row.listing_id] + 1
        else:
            skipped += 1
            continue
        keys = _job_keys(row)
        if keys & queued:
            same += 1
            continue
        queued |= keys
        launch.append((row, attempt))
    return launch, skipped, same


async def run_batch(options: BatchOptions, rows: Sequence[BatchRow], *,
                    on_entry: Callable[[LedgerEntry], None] | None = None,
                    skipped_invalid_url: int = 0,
                    annotate: Callable[[LedgerEntry], LedgerEntry] | None = None,
                    retry: RetryStats | None = None) -> BatchSummary:
    """Prepare ``rows`` with at most ``options.workers`` concurrent ``apply`` (or, for a
    row with an ``application_id``, ``resume``) subprocesses, appending to the batch
    ledger as each finishes, and return the summary of the whole ledger (this run and
    earlier runs of the same batch id). ``annotate`` completes each finished entry before
    its card is synced and its line is written; ``retry`` is what a retry selected."""
    started_at = _now()
    batch_dir = default_batch_dir(options.paths, options.batch_id)  # ensure() first
    ledger_path = batch_dir / LEDGER_NAME
    history, ignored = read_ledger_lines(ledger_path)
    queue, skipped_settled, same_url = plan(options, rows, history)
    prepared = sum(1 for e in latest_entries(history).values() if e.outcome == "prepared")
    launched = 0
    running = 0
    stopped = False
    slots: asyncio.Queue[int] = asyncio.Queue()
    for slot in range(options.workers):
        slots.put_nowait(slot)
    # Held while the queue, counters and ledger change, never across a store or pipeline
    # lookup (a locked database would stall every worker); notified when a row settles.
    changed = asyncio.Condition()

    def record(entry: LedgerEntry) -> None:
        nonlocal prepared
        append_ledger(ledger_path, entry)
        history.append(entry)
        if entry.outcome == "prepared":
            prepared += 1
        if on_entry is not None:
            on_entry(entry)

    reader = StateReader(options.paths)

    def bookkeep(entry: LedgerEntry) -> LedgerEntry:
        """Link the Saved card (and close it) once the outcome is known; in a thread."""
        if annotate is not None:
            entry = annotate(entry)
        return sync_pipeline_card(options.paths, options.candidate_id, entry,
                                  sync_closed=options.sync_closed, reader=reader)

    def bound_reached() -> bool:
        return options.max_prepared is not None and prepared >= options.max_prepared

    def bound_reserved() -> bool:
        """Every remaining prepared slot is taken by a job still running."""
        return options.max_prepared is not None and prepared + running >= options.max_prepared

    async def settle(row: BatchRow, attempt: int) -> LedgerEntry:
        """The row's entry: ``already_recorded`` when the store has its application,
        else the finished job's. Runs outside the lock."""
        nonlocal launched
        if not options.include_existing and row.application_id is None:
            url = row.application_url
            existing = await reader.run(
                lambda store: _existing_application(store, options.candidate_id, url))
            if existing is not None:
                app_id, state = existing
                skipped = _entry(options, row, attempt=attempt, slot=None,
                                 outcome="already_recorded", started_at=_now(),
                                 application_id=app_id, state=state,
                                 message=f"an application already exists ({state.value})")
                return await asyncio.to_thread(bookkeep, skipped)
        launched += 1
        slot = await slots.get()
        try:
            entry = await _run_one(options, row, attempt=attempt, slot=slot, reader=reader)
        finally:
            slots.put_nowait(slot)
        return await asyncio.to_thread(bookkeep, entry)

    async def worker() -> None:
        nonlocal running, stopped
        while True:
            async with changed:
                while queue and not stopped and not bound_reached() and bound_reserved():
                    await changed.wait()
                if not queue or stopped:
                    return
                if bound_reached():
                    stopped = True
                    changed.notify_all()
                    return
                row, attempt = queue.pop(0)
                running += 1  # reserves a place under --max-prepared until the row settles
            entry = await settle(row, attempt)
            async with changed:
                running -= 1
                record(entry)
                changed.notify_all()

    workers = [asyncio.create_task(worker()) for _ in range(options.workers)]
    try:
        await asyncio.gather(*workers)
    except BaseException:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise
    finally:
        reader.close()
    summary = summarize(options.batch_id, history, started_at=started_at, finished_at=_now(),
                        rows=len(rows), launched=launched, skipped_settled=skipped_settled,
                        skipped_invalid_url=skipped_invalid_url, ledger_path=ledger_path,
                        stopped_at_max_prepared=stopped, run_options=options.run_options(),
                        retry=retry, skipped_same_url=same_url, ledger_lines_ignored=ignored)
    write_summary(summary, batch_dir)
    return summary


def format_entry(entry: LedgerEntry) -> str:
    """One compact progress line; never includes questions or messages."""
    job = " — ".join(x for x in (entry.company, entry.title) if x) or entry.listing_id
    line = f"{entry.outcome:<16} {entry.backend or '-':<12} {job} ({entry.duration_s:.1f}s)"
    if entry.application_id:
        line += f" [{entry.application_id}]"
    if entry.linked is not None:
        card = "card linked" if entry.linked else "card not linked"
        line += f" [{card}, moved to Closed]" if entry.closed_synced else f" [{card}]"
    return line


# --- report -------------------------------------------------------------------------------


class HoldCategory(Contract):
    category: str
    holds: int = Field(ge=0)
    """Recorded questions or actions in this category."""
    applications: int = Field(ge=0)
    """Distinct applications with at least one of them."""
    top_labels: list[LabelCount] = Field(default_factory=list)
    """Wording truncated to ``REPORT_LABEL_LIMIT``, most common first."""
    application_ids: list[str] = Field(default_factory=list)


class BackendDurations(Contract):
    runs: int = Field(ge=0)
    median_s: float | None = None
    p95_s: float | None = None


class PipelineCounts(Contract):
    rows_with_card: int = Field(default=0, ge=0)
    linked: int = Field(default=0, ge=0)
    not_linked: int = Field(default=0, ge=0)
    closed_moved: int = Field(default=0, ge=0)
    closed_skipped: int = Field(default=0, ge=0)
    problems: list[LabelCount] = Field(default_factory=list)


READINESS_COLUMNS: tuple[str, ...] = (
    "prepared", "needs_input", "failed", "closed", "no_form", "other",
)
"""Where each listing lands in the per-backend readiness table (``BackendReadiness``)."""
RAN_COLUMNS: tuple[str, ...] = READINESS_COLUMNS[:-1]


class BackendReadiness(Contract):
    """One backend's row of the readiness table; each listing counts once, as in
    ``BatchReport.rows``."""

    backend: str
    applications: int = Field(ge=0)
    """Listings on this backend."""
    prepared: int = Field(default=0, ge=0)
    needs_input: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    """``failed_retryable`` runs that reached a form, and ``error`` (no readable outcome,
    a timeout, or the CLI could not start)."""
    closed: int = Field(default=0, ge=0)
    no_form: int = Field(default=0, ge=0)
    """``failed_retryable`` runs that never reached a fillable form ("Could not reach the
    application form: ...")."""
    other: int = Field(default=0, ge=0)
    """``duplicate``, ``blocked`` and ``already_recorded``."""
    prepared_rate: float | None = None
    """``prepared`` over the listings that ran to an outcome of their own (every column
    but ``other``), 0..1 rounded to 3 decimals; None when there are none."""
    median_duration_s: float | None = None
    """Over every launched attempt on this backend in the ledgers read."""
    provider_cost_usd: float | None = None
    """Known AI provider cost of these listings (each at its latest line with a cost)."""


class BatchReport(Contract):
    """``batch-report``: what one or more batch ledgers say, without CLI messages, with
    question wording truncated to ``REPORT_LABEL_LIMIT`` and failure details masked."""

    batches: list[str]
    batches_dir: str
    since: datetime | None = None
    """With ``--since``: only batches with a line finished at or after it were read."""
    ledger_lines_ignored: int = Field(default=0, ge=0)
    """Unreadable lines in the ledgers read (``read_ledger_lines``); left out of every count."""
    rows: int = Field(ge=0)
    """Distinct listings. Each counts once: its latest launched entry across the
    ledgers read, else its latest ``already_recorded`` entry."""
    totals: dict[str, int] = Field(default_factory=dict)
    by_backend: dict[str, dict[str, int]] = Field(default_factory=dict)
    durations: dict[str, BackendDurations] = Field(default_factory=dict)
    """Per backend, over every launched attempt."""
    duration_median_s: float | None = None
    duration_p95_s: float | None = None
    holds: list[HoldCategory] = Field(default_factory=list)
    pipeline: PipelineCounts = Field(default_factory=PipelineCounts)
    provider_cost_usd: float | None = None
    """Known AI provider cost over the rows (each application's cost so far, once)."""
    provider_calls: int = Field(default=0, ge=0)
    provider_cost_rows: int = Field(default=0, ge=0)
    """Listings with a ledger line that carries a provider cost (each counted at its latest
    such line)."""
    cost_per_prepared_usd: float | None = None
    """``provider_cost_usd`` divided by the number of prepared rows."""
    questions: list[HoldGroup] = Field(default_factory=list)
    """The rows' holds grouped by question wording, each with the ``answer`` (or
    ``resume --act``) line that clears it; every group (the Markdown shows ``--top``)."""
    fill_failures: list[FailureGroup] = Field(default_factory=list)
    """``failed_retryable`` rows grouped by failed-field detail (``failed_fields`` of the
    stored failure), else by failure reason, plus ``error`` rows by kind of error."""
    backends: list[BackendReadiness] = Field(default_factory=list)
    """The per-backend readiness table."""


def list_batches(paths: LocalPaths) -> list[str]:
    """Ids of the batches under ``$IMX_HOME/batches`` that have a ledger. Creates nothing."""
    root = paths.home / "batches"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and (p / LEDGER_NAME).is_file())


def _durations(values: Sequence[float]) -> BackendDurations:
    if not values:
        return BackendDurations(runs=0)
    return BackendDurations(runs=len(values), median_s=round(statistics.median(values), 2),
                            p95_s=round(_percentile(values, 0.95), 2))


def _recorded_missing(store: ApplicationStore, application_id: str) -> list[MissingInput]:
    """The questions the application last stopped for: its latest NEEDS_INPUT event's
    ``missing_inputs``, then its latest packet's."""
    items: list[MissingInput] = []
    for event in reversed(store.list_events(application_id)):
        raw = event.metadata.get("missing_inputs") if event.to_state is S.NEEDS_INPUT else None
        if isinstance(raw, list) and raw:
            for value in raw:
                try:
                    items.append(MissingInput.model_validate(value))
                except ValidationError:
                    continue
            break
    packet = store.latest_packet(application_id)
    if packet is not None:
        items.extend(packet.missing_inputs)
    return items


class _StoreReader:
    """The existing application store, for what a ledger line does not carry: the
    reason, control, field id and semantic type of the questions of older lines, and
    why a FAILED_RETRYABLE run failed. Opened on first use, only when the store exists
    (as ``status`` opens it); an unreadable store only loses these refinements."""

    def __init__(self, paths: LocalPaths) -> None:
        self.paths = paths
        self.store: ApplicationStore | None = None
        self.unavailable = False

    def _open(self) -> ApplicationStore | None:
        if self.store is None and not self.unavailable:
            if not self.paths.state_db.is_file():
                self.unavailable = True
                return None
            try:
                self.store = ApplicationStore.open(self.paths.state_db)
            except Exception:
                self.unavailable = True
        return self.store

    def holds(self, application_id: str | None) -> dict[str, MissingInput]:
        """The application's recorded questions by label (cut to ``LABEL_LIMIT``)."""
        store = self._open() if application_id else None
        if store is None or application_id is None:
            return {}
        try:
            recorded = _recorded_missing(store, application_id)
        except Exception:
            return {}
        known: dict[str, MissingInput] = {}
        for item in recorded:
            known.setdefault(_truncate(item.label, LABEL_LIMIT), item)
        return known

    def failure(self, application_id: str | None,
                before: datetime) -> tuple[list[FieldFailure], str | None] | None:
        """``stored_failure`` of the application's latest FAILED_RETRYABLE stop recorded
        by ``before``; None without a readable store or application."""
        store = self._open() if application_id else None
        if store is None or application_id is None:
            return None
        try:
            app = store.get_application(application_id)
            events = [e for e in store.list_events(application_id) if e.timestamp <= before]
        except Exception:
            return None
        return stored_failure(events, app)

    def close(self) -> None:
        if self.store is not None:
            self.store.close()


def _hold_items(entry: LedgerEntry, stored: _StoreReader) -> list[HoldOccurrence]:
    """One ``HoldOccurrence`` per recorded question of ``entry``. What an older line did
    not record (the reasons and controls before ``missing_items``, the field ids and
    semantic types before those) comes from the store's record of the application,
    matched by label; a label not found there takes the line's reason when it has
    exactly one."""

    def occurrence(label: str, reason: str | None, control: str | None, field_id: str | None,
                   semantic: str | None) -> HoldOccurrence:
        return HoldOccurrence(label=label, reason=reason, control_type=control,
                              semantic_type=semantic, field_id=field_id,
                              application_id=entry.application_id, backend=entry.backend)

    if entry.missing_items:
        lacking = any(m.field_id is None and m.reason != "USER_ACTION" for m in entry.missing_items)
        known = stored.holds(entry.application_id) if lacking else {}
        items: list[HoldOccurrence] = []
        for m in entry.missing_items:
            found = known.get(m.label)
            items.append(occurrence(
                m.label, m.reason, m.control_type,
                m.field_id or (found.field_id if found else None),
                m.semantic_type or (found.semantic_type.value if found else None)))
        return items
    if not entry.missing_labels:
        return []
    only = entry.missing_reasons[0] if len(entry.missing_reasons) == 1 else None
    known = stored.holds(entry.application_id)
    items = []
    for label in entry.missing_labels:
        found = known.get(label)
        if found is None:
            items.append(occurrence(label, only, None, None, None))
        else:
            items.append(occurrence(label, found.reason.value,
                                    found.control_type.value if found.control_type else None,
                                    found.field_id, found.semantic_type.value))
    return items


def _error_detail(message: str) -> str:
    """The kind of an ``error`` row. The CLI's own output (kept in the ledger) is never
    shown: it may carry anything."""
    if message.startswith("timed out after"):
        if "may still be running" in message:
            return "timed out; the job did not exit after SIGTERM and SIGKILL"
        return "timed out; the job was stopped"
    if message.startswith("could not start the CLI"):
        return "could not start the CLI"
    return "the CLI printed no readable outcome"


def _failure_items(entry: LedgerEntry, stored: _StoreReader) -> list[FailureOccurrence]:
    """``error`` and ``failed_retryable`` rows as failures: each failed field of the stored
    FAILED_RETRYABLE stop (``failed_fields``), else the stop's failure reason, else the
    row's own message (without a readable store)."""
    app, backend = entry.application_id, entry.backend
    if entry.outcome == "error":
        return [FailureOccurrence(kind="error", detail=_error_detail(entry.message),
                                  application_id=app, backend=backend)]
    if entry.outcome != "failed_retryable":
        return []
    fields, reason = stored.failure(app, entry.finished_at) or ([], None)
    if fields:
        return [FailureOccurrence(kind="field", detail=f.detail or "", status=f.status,
                                  label=f.label or f.field_id, application_id=app,
                                  backend=backend) for f in fields]
    return [FailureOccurrence(kind="run", detail=reason or entry.message, application_id=app,
                              backend=backend)]


def _readiness_column(entry: LedgerEntry) -> str:
    if entry.outcome in ("prepared", "needs_input", "closed"):
        return entry.outcome
    if entry.outcome == "failed_retryable":
        return "no_form" if entry.message.startswith(NO_FORM_PREFIX) else "failed"
    if entry.outcome == "error":
        return "failed"
    return "other"


def _readiness(rows: Sequence[LedgerEntry], runs: dict[str, list[float]],
               costed: Sequence[LedgerEntry]) -> list[BackendReadiness]:
    columns: dict[str, Counter[str]] = defaultdict(Counter)
    for entry in rows:
        columns[entry.backend or "(none)"][_readiness_column(entry)] += 1
    cost: dict[str, float] = defaultdict(float)
    for entry in costed:
        cost[entry.backend or "(none)"] += entry.provider_cost_usd or 0.0
    table: list[BackendReadiness] = []
    for backend, counts in sorted(columns.items()):
        ran = sum(counts[k] for k in RAN_COLUMNS)
        durations = runs.get(backend, [])
        table.append(BackendReadiness(
            backend=backend, applications=sum(counts.values()),
            **{k: counts[k] for k in READINESS_COLUMNS},
            prepared_rate=round(counts["prepared"] / ran, 3) if ran else None,
            median_duration_s=round(statistics.median(durations), 2) if durations else None,
            provider_cost_usd=round(cost[backend], 6) if backend in cost else None))
    return table


def build_report(paths: LocalPaths, batch_ids: Sequence[str] | None = None, *,
                 top: int = 10, since: datetime | None = None,
                 cli: Sequence[str] = (PROG,)) -> BatchReport:
    """Read the named batch ledgers (default: every batch), keep those with a line
    finished at or after ``since`` when it is given, and summarize them. ``cli`` is the
    command prefix of the ``answer`` lines (``interviewmaxxing`` and any ``--home``).
    Raises ``ValueError`` for a batch id that is not a plain name and
    ``FileNotFoundError`` for a named batch without a ledger. Creates nothing and writes
    no file; the existing application store is opened (as ``status`` does) only for
    ledger lines older than ``missing_items`` or its field ids, and for the failures of
    ``failed_retryable`` rows."""
    root = paths.home / "batches"
    if batch_ids is None:
        ids = list_batches(paths)
    else:
        ids = sorted({_plain_batch_id(batch_id) for batch_id in batch_ids})
        for batch_id in ids:
            if not (root / batch_id / LEDGER_NAME).is_file():
                raise FileNotFoundError(f"no ledger for batch {batch_id!r} under {root}")
    read = {batch_id: read_ledger_lines(root / batch_id / LEDGER_NAME) for batch_id in ids}
    ledgers = {batch_id: entries for batch_id, (entries, _) in read.items()}
    if since is not None:
        since = since if since.tzinfo is not None else since.replace(tzinfo=UTC)
        ids = [b for b in ids if any(e.finished_at >= since for e in ledgers[b])]
    timeline = sorted(
        ((entry.finished_at.timestamp(), b, n, entry) for b, batch_id in enumerate(ids)
         for n, entry in enumerate(ledgers[batch_id])),
        key=lambda item: item[:3])
    entries = [entry for *_, entry in timeline]
    newest: dict[str, LedgerEntry] = {}
    launched: dict[str, LedgerEntry] = {}
    link_state: dict[str, LedgerEntry] = {}
    close_state: dict[str, LedgerEntry] = {}
    with_card: set[str] = set()
    costed_rows: dict[str, LedgerEntry] = {}
    for entry in entries:
        newest[entry.listing_id] = entry
        if entry.provider_cost_usd is not None:
            # Cumulative per application: the latest costed row, even when a later attempt
            # crashed before an application id (and so a cost) was known.
            costed_rows[entry.listing_id] = entry
        if entry.outcome != "already_recorded":
            launched[entry.listing_id] = entry
        if entry.pipeline_id:
            with_card.add(entry.listing_id)
        if entry.linked is not None:
            link_state[entry.listing_id] = entry
        if entry.closed_synced is not None:
            close_state[entry.listing_id] = entry
    rows = [launched.get(listing_id, entry) for listing_id, entry in newest.items()]

    totals: Counter[str] = Counter(e.outcome for e in rows)
    by_backend: dict[str, Counter[str]] = defaultdict(Counter)
    for entry in rows:
        by_backend[entry.backend or "(none)"][entry.outcome] += 1
    runs: dict[str, list[float]] = defaultdict(list)
    for entry in entries:
        if entry.outcome != "already_recorded":
            runs[entry.backend or "(none)"].append(entry.duration_s)
    overall = _durations([d for values in runs.values() for d in values])

    labels: dict[str, Counter[str]] = defaultdict(Counter)
    app_ids: dict[str, dict[str, None]] = defaultdict(dict)
    occurrences: list[HoldOccurrence] = []
    failures: list[FailureOccurrence] = []
    stored = _StoreReader(paths)
    try:
        for entry in rows:
            for item in _hold_items(entry, stored):
                category = categorize_hold(item.label, item.reason, item.control_type)
                labels[category][_truncate(item.label, REPORT_LABEL_LIMIT)] += 1
                if entry.application_id:
                    app_ids[category][entry.application_id] = None
                occurrences.append(item)
            failures += _failure_items(entry, stored)
    finally:
        stored.close()
    holds = [
        HoldCategory(category=category, holds=sum(labels[category].values()),
                     applications=len(app_ids[category]),
                     top_labels=[LabelCount(label=label, count=count)
                                 for label, count in labels[category].most_common(top)],
                     application_ids=list(app_ids[category]))
        for category in HOLD_CATEGORIES if labels[category]
    ]
    holds.sort(key=lambda h: -h.holds)  # stable: ties keep HOLD_CATEGORIES order

    costed = list(costed_rows.values())
    total_cost = round(sum(e.provider_cost_usd or 0.0 for e in costed), 6) if costed else None
    prepared_rows = totals["prepared"]
    return BatchReport(
        provider_cost_usd=total_cost,
        provider_calls=sum(e.provider_calls or 0 for e in costed),
        provider_cost_rows=len(costed),
        cost_per_prepared_usd=(round(total_cost / prepared_rows, 6)
                               if total_cost is not None and prepared_rows else None),
        batches=ids, batches_dir=str(root), since=since, rows=len(rows),
        ledger_lines_ignored=sum(read[batch_id][1] for batch_id in ids),
        totals={k: totals[k] for k in OUTCOMES if totals[k]},
        by_backend={backend: {k: counts[k] for k in OUTCOMES if counts[k]}
                    for backend, counts in sorted(by_backend.items())},
        durations={backend: _durations(values) for backend, values in sorted(runs.items())},
        duration_median_s=overall.median_s, duration_p95_s=overall.p95_s,
        holds=holds,
        pipeline=PipelineCounts(
            rows_with_card=len(with_card),
            linked=sum(1 for e in link_state.values() if e.linked),
            not_linked=sum(1 for e in link_state.values() if e.linked is False),
            closed_moved=sum(1 for e in close_state.values() if e.closed_synced),
            closed_skipped=sum(1 for e in close_state.values() if e.closed_synced is False),
            problems=_card_problems(link_state.values(), close_state.values()),
        ),
        questions=group_holds(occurrences, cli=cli),
        fill_failures=group_failures(failures),
        backends=_readiness(rows, runs, costed),
    )


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value}"


def _readiness_lines(table: Sequence[BackendReadiness]) -> list[str]:
    lines = ["| backend | apps | prepared | needs input | failed | closed | no form | other | "
             "prepared rate | median s | cost USD |",
             "| --- |" + " --- |" * 10]
    for row in table:
        rate = "-" if row.prepared_rate is None else f"{row.prepared_rate:.0%}"
        cost = "-" if row.provider_cost_usd is None else f"{row.provider_cost_usd:.4f}"
        lines.append(f"| {_cell(row.backend)} | {row.applications} | {row.prepared} | "
                     f"{row.needs_input} | {row.failed} | {row.closed} | {row.no_form} | "
                     f"{row.other} | {rate} | {_seconds(row.median_duration_s)} | {cost} |")
    return lines


def render_report_markdown(report: BatchReport, *, top: int = 10) -> str:
    """The report as Markdown: no CLI messages, wording truncated to 80 characters, at
    most ``top`` question and failure groups (the JSON has them all)."""
    lines = ["# Batch report", ""]
    if not report.batches:
        where = f"No batch ledgers under {report.batches_dir}"
        if report.since is not None:
            where += f" with a line finished since {_iso(report.since)}"
        return "\n".join([*lines, where + "."]) + "\n"
    lines += [
        f"- batches: {', '.join(report.batches)} (in {report.batches_dir})",
        *([f"- since: {_iso(report.since)}"] if report.since is not None else []),
        f"- rows: {report.rows} (each listing once: its latest launched outcome, else "
        "already_recorded)",
        *([f"- unreadable ledger lines ignored: {report.ledger_lines_ignored}"]
          if report.ledger_lines_ignored else []),
        "- nothing was submitted (preparation only)",
        "",
        "## Totals",
        "",
        "| outcome | count |",
        "| --- | --- |",
    ]
    lines += [f"| {k} | {v} |" for k, v in report.totals.items()]
    lines.append(f"| **all** | {sum(report.totals.values())} |")
    if report.by_backend:
        columns = [k for k in OUTCOMES if any(k in c for c in report.by_backend.values())]
        lines += ["", "## By backend", "", "| backend | " + " | ".join(columns) + " |",
                  "| --- |" + " --- |" * len(columns)]
        lines += ["| " + _cell(backend) + " | " + " | ".join(str(counts.get(k, 0)) for k in columns)
                  + " |" for backend, counts in report.by_backend.items()]
    if report.backends:
        lines += ["", "## Backend readiness", "", *_readiness_lines(report.backends)]
    if report.durations:
        lines += ["", "## Durations", "", "| backend | runs | median s | p95 s |",
                  "| --- | --- | --- | --- |"]
        lines += [f"| {_cell(backend)} | {d.runs} | {_seconds(d.median_s)} | {_seconds(d.p95_s)} |"
                  for backend, d in report.durations.items()]
        lines.append(f"| **all** | {sum(d.runs for d in report.durations.values())} | "
                     f"{_seconds(report.duration_median_s)} | {_seconds(report.duration_p95_s)} |")
    if report.provider_cost_usd is not None:
        per_prepared = ("-" if report.cost_per_prepared_usd is None
                        else f"USD {report.cost_per_prepared_usd:.4f}")
        lines += ["", "## Provider cost", "",
                  f"- known cost: USD {report.provider_cost_usd:.4f} over "
                  f"{report.provider_calls} call(s) in {report.provider_cost_rows} application(s)",
                  f"- per prepared application: {per_prepared}"]
    if report.holds:
        lines += ["", "## Holds by category"]
        for category in report.holds:
            lines += ["", f"### {category.category}: {category.holds} hold(s) in "
                          f"{category.applications} application(s)",
                      "", "| question | count |", "| --- | --- |"]
            lines += [f"| {_cell(item.label)} | {item.count} |" for item in category.top_labels]
            if category.application_ids:
                lines += ["", "applications: " + ", ".join(category.application_ids)]
        lines += ["", "Inspect one with: interviewmaxxing status APP"]
    if report.questions:
        held = {a for q in report.questions for a in q.application_ids}
        lines += ["", "## Questions", "",
                  f"{len(report.questions)} distinct question(s) hold {len(held)} "
                  "application(s); an answer saved with --reuse global answers its question "
                  "for every application.",
                  "", *render_hold_groups(report.questions, top=top)]
    if report.fill_failures:
        lines += ["", "## Fill failures", "", *render_failure_groups(report.fill_failures, top=top)]
    pipeline = report.pipeline
    if pipeline.rows_with_card:
        lines += ["", "## Pipeline cards", "",
                  f"- rows with a card: {pipeline.rows_with_card}; linked {pipeline.linked}, "
                  f"not linked {pipeline.not_linked}; moved to Closed {pipeline.closed_moved}, "
                  f"not moved {pipeline.closed_skipped}"]
        if pipeline.problems:
            lines.append("- problems: " + ", ".join(f"{p.label} ({p.count})"
                                                    for p in pipeline.problems))
    return "\n".join(lines) + "\n"


__all__ = [
    "HOLD_CATEGORIES",
    "OUTCOMES",
    "PREPARED_PREFIX",
    "READINESS_COLUMNS",
    "REPORT_LABEL_LIMIT",
    "BackendDurations",
    "BackendReadiness",
    "BatchOptions",
    "BatchOutcome",
    "BatchReport",
    "BatchRow",
    "BatchRunOptions",
    "BatchSummary",
    "HoldCategory",
    "LedgerEntry",
    "MissingItem",
    "PipelineCounts",
    "RetryStats",
    "StateReader",
    "append_ledger",
    "build_report",
    "categorize_hold",
    "classify",
    "default_batch_dir",
    "default_batch_id",
    "default_command",
    "format_entry",
    "list_batches",
    "load_inventory",
    "plan",
    "private_dirs",
    "read_inventory",
    "read_ledger",
    "read_ledger_lines",
    "read_summary",
    "render_report_markdown",
    "render_retry_markdown",
    "render_summary_markdown",
    "retry_stats",
    "run_batch",
    "summarize",
    "sync_pipeline_card",
    "write_summary",
]
