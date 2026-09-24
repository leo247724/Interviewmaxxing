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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, ValidationError, model_validator

from interviewmaxxing_core import (
    ApplicationState,
    ApplicationStore,
    ApplyOutcome,
    Contract,
    InvalidApplicationUrl,
    LocalPaths,
    normalize_application_url,
)

from .dynamic import DynamicOptions

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
LABEL_LIMIT = 120
TERM_GRACE_S = 15.0
"""How long a timed-out job may take to exit after SIGTERM before SIGKILL."""
_LAUNCH_STATES: frozenset[ApplicationState] = frozenset(
    {S.FAILED_RETRYABLE, S.REQUESTED, S.INSPECTING}
)
"""Stored states that do not count as ``already_recorded``: ``apply`` resumes them."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def default_batch_id(now: datetime | None = None) -> str:
    return (now or _now()).astimezone(UTC).strftime("batch-%Y%m%dT%H%M%SZ")


def default_batch_dir(paths: LocalPaths, batch_id: str) -> Path:
    """``$IMX_HOME/batches/<batch id>``, created owner-only."""
    if not batch_id or batch_id in (".", "..") or "/" in batch_id or "\\" in batch_id:
        raise ValueError(f"batch id must be a plain name, got {batch_id!r}")
    directory = paths.home / "batches" / batch_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def default_command() -> list[str]:
    """The installed ``interviewmaxxing`` console script next to this interpreter when
    it exists (what the end-to-end suite runs), else ``python -m interviewmaxxing_cli``."""
    script = Path(sys.executable).with_name("interviewmaxxing")
    if script.exists():
        return [str(script)]
    return [sys.executable, "-m", "interviewmaxxing_cli"]


# --- inventory ------------------------------------------------------------------------


class BatchRow(Contract):
    """One Saved job from the application URL inventory."""

    listing_id: str
    pipeline_id: str | None = None
    company: str = ""
    title: str = ""
    application_url: str
    backend: str = ""
    status: str = ""


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


class LedgerEntry(Contract):
    """One finished (or skipped) job, as appended to ``ledger.jsonl``. Lives under
    ``IMX_HOME``: the labels of missing questions are private."""

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
    exit_code: int | None = None
    started_at: datetime
    finished_at: datetime
    duration_s: float = Field(ge=0.0)


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _open_private_append(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)


def append_ledger(path: Path, entry: LedgerEntry) -> None:
    """Append one JSON line (owner-only file; one ``write`` per entry)."""
    line = json.dumps(entry.model_dump(mode="json"), sort_keys=True) + "\n"
    fd = _open_private_append(path)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def read_ledger(path: Path) -> list[LedgerEntry]:
    """Every entry in file order. A truncated last line (a crash mid-write) is ignored."""
    if not path.exists():
        return []
    entries: list[LedgerEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(LedgerEntry.model_validate_json(line))
        except ValidationError:
            continue
    return entries


def classify(outcome: ApplyOutcome) -> BatchOutcome:
    """Map a CLI ``ApplyOutcome`` to a batch outcome."""
    state = outcome.state
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


def summarize(batch_id: str, entries: Sequence[LedgerEntry], *, started_at: datetime,
              finished_at: datetime, rows: int, launched: int, skipped_settled: int,
              skipped_invalid_url: int, ledger_path: Path,
              stopped_at_max_prepared: bool = False) -> BatchSummary:
    """Aggregate a ledger: totals and per-backend counts use each row's latest entry;
    durations and missing-input tallies use every launched attempt in this ledger."""
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
        skipped_invalid_url=skipped_invalid_url,
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
    )


def write_summary(summary: BatchSummary, batch_dir: Path) -> Path:
    """Write ``summary.json`` owner-only (replacing any earlier one) and return its path."""
    path = batch_dir / SUMMARY_NAME
    batch_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        f"already settled: {summary.skipped_settled}; invalid URL: {summary.skipped_invalid_url}",
        "- nothing was submitted (preparation only)"
        + ("; stopped launching at --max-prepared" if summary.stopped_at_max_prepared else ""),
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
        lines += ["| " + backend + " | " + " | ".join(str(counts.get(k, 0)) for k in columns) + " |"
                  for backend, counts in summary.by_backend.items()]
    if summary.duration_median_s is not None:
        lines += ["", f"- duration per job: median {summary.duration_median_s}s, "
                      f"p95 {summary.duration_p95_s}s"]
    if summary.top_missing_reasons:
        lines += ["", "## Missing input", "",
                  "reasons: " + ", ".join(f"{r.label} ({r.count})" for r in summary.top_missing_reasons)]
        lines += ["", "| question | count |", "| --- | --- |"]
        lines += [f"| {_truncate(item.label, 80)} | {item.count} |" for item in summary.top_missing_labels]
    lines += ["", f"ledger: {summary.ledger_path}"]
    return "\n".join(lines) + "\n"


# --- the run ----------------------------------------------------------------------------


def _existing_application(options: BatchOptions, url: str) -> tuple[str, ApplicationState] | None:
    """The stored application for ``url`` when it would not be run again by ``apply``."""
    if not options.paths.state_db.exists():
        return None
    with ApplicationStore.open(options.paths.state_db) as store:
        app = store.find_application(options.candidate_id, url)
    if app is None or app.state in _LAUNCH_STATES:
        return None
    return app.id, app.state


def _entry(options: BatchOptions, row: BatchRow, *, attempt: int, slot: int | None,
           outcome: BatchOutcome, started_at: datetime, message: str = "",
           application_id: str | None = None, state: ApplicationState | None = None,
           missing_reasons: Sequence[str] = (), missing_labels: Sequence[str] = (),
           exit_code: int | None = None) -> LedgerEntry:
    finished_at = _now()
    return LedgerEntry(
        batch_id=options.batch_id, **row.model_dump(), attempt=attempt, worker_slot=slot,
        application_id=application_id, state=state, outcome=outcome,
        message=_truncate(message, MESSAGE_LIMIT),
        missing_reasons=list(missing_reasons),
        missing_labels=[_truncate(label, LABEL_LIMIT) for label in missing_labels],
        exit_code=exit_code, started_at=started_at, finished_at=finished_at,
        duration_s=max((finished_at - started_at).total_seconds(), 0.0),
    )


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the job's process group (the CLI and its browser), then SIGKILL."""
    if proc.returncode is not None:
        return
    for sig, wait in ((signal.SIGTERM, TERM_GRACE_S), (signal.SIGKILL, TERM_GRACE_S)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), wait)
            return
        except TimeoutError:
            continue


async def _run_one(options: BatchOptions, row: BatchRow, *, attempt: int, slot: int) -> LedgerEntry:
    started_at = _now()
    browser_dir = options.worker_browser_dir(slot)
    browser_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        proc = await asyncio.create_subprocess_exec(
            *options.argv(row.application_url), env=options.environment(slot),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
    except OSError as exc:
        return _entry(options, row, attempt=attempt, slot=slot, outcome="error",
                      started_at=started_at, message=f"could not start the CLI: {exc}")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), options.per_job_timeout_s)
    except TimeoutError:
        await _terminate(proc)
        return _entry(options, row, attempt=attempt, slot=slot, outcome="error",
                      started_at=started_at, exit_code=proc.returncode,
                      message=f"timed out after {options.per_job_timeout_s:g} s; the run was "
                              "stopped and nothing was submitted")
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
    return _entry(
        options, row, attempt=attempt, slot=slot, outcome=classify(outcome),
        started_at=started_at, message=outcome.message, application_id=outcome.application_id,
        state=outcome.state, exit_code=code,
        missing_reasons=sorted({m.reason.value for m in outcome.missing_inputs}),
        missing_labels=[m.label for m in outcome.missing_inputs],
    )


def plan(options: BatchOptions, rows: Sequence[BatchRow], history: Sequence[LedgerEntry]
         ) -> tuple[list[tuple[BatchRow, int]], int]:
    """Which rows to launch (with their attempt number) given the ledger so far, and
    how many rows are skipped as already settled or out of retries."""
    latest = latest_entries(history)
    attempts: Counter[str] = Counter(
        e.listing_id for e in history if e.outcome != "already_recorded")
    launch: list[tuple[BatchRow, int]] = []
    skipped = 0
    seen: set[str] = set()
    for row in rows:
        if row.listing_id in seen:
            continue
        seen.add(row.listing_id)
        previous = latest.get(row.listing_id)
        if previous is None:
            launch.append((row, 1))
        elif previous.outcome in RETRYABLE_OUTCOMES and attempts[row.listing_id] <= options.retry_retryable:
            launch.append((row, attempts[row.listing_id] + 1))
        else:
            skipped += 1
    return launch, skipped


async def run_batch(options: BatchOptions, rows: Sequence[BatchRow], *,
                    on_entry: Callable[[LedgerEntry], None] | None = None,
                    skipped_invalid_url: int = 0) -> BatchSummary:
    """Prepare ``rows`` with at most ``options.workers`` concurrent ``apply``
    subprocesses, appending to the batch ledger as each finishes, and return the
    summary of the whole ledger (this run and earlier runs of the same batch id)."""
    started_at = _now()
    batch_dir = default_batch_dir(options.paths, options.batch_id)
    options.paths.ensure()
    ledger_path = batch_dir / LEDGER_NAME
    history = read_ledger(ledger_path)
    queue, skipped_settled = plan(options, rows, history)
    prepared = sum(1 for e in latest_entries(history).values() if e.outcome == "prepared")
    launched = 0
    running = 0
    stopped = False
    slots: asyncio.Queue[int] = asyncio.Queue()
    for slot in range(options.workers):
        slots.put_nowait(slot)
    # Held while the queue, counters and ledger change; notified when a job ends.
    changed = asyncio.Condition()

    def record(entry: LedgerEntry) -> None:
        nonlocal prepared
        append_ledger(ledger_path, entry)
        history.append(entry)
        if entry.outcome == "prepared":
            prepared += 1
        if on_entry is not None:
            on_entry(entry)

    def bound_reached() -> bool:
        return options.max_prepared is not None and prepared >= options.max_prepared

    def bound_reserved() -> bool:
        """Every remaining prepared slot is taken by a job still running."""
        return options.max_prepared is not None and prepared + running >= options.max_prepared

    async def worker() -> None:
        nonlocal launched, running, stopped
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
                if not options.include_existing:
                    existing = await asyncio.to_thread(_existing_application, options,
                                                       row.application_url)
                    if existing is not None:
                        app_id, state = existing
                        record(_entry(options, row, attempt=attempt, slot=None,
                                      outcome="already_recorded", started_at=_now(),
                                      application_id=app_id, state=state,
                                      message=f"an application already exists ({state.value})"))
                        continue
                launched += 1
                running += 1
            slot = await slots.get()
            try:
                entry = await _run_one(options, row, attempt=attempt, slot=slot)
            finally:
                slots.put_nowait(slot)
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
    summary = summarize(options.batch_id, history, started_at=started_at, finished_at=_now(),
                        rows=len(rows), launched=launched, skipped_settled=skipped_settled,
                        skipped_invalid_url=skipped_invalid_url, ledger_path=ledger_path,
                        stopped_at_max_prepared=stopped)
    write_summary(summary, batch_dir)
    return summary


def format_entry(entry: LedgerEntry) -> str:
    """One compact progress line; never includes questions or messages."""
    job = " — ".join(x for x in (entry.company, entry.title) if x) or entry.listing_id
    line = f"{entry.outcome:<16} {entry.backend or '-':<12} {job} ({entry.duration_s:.1f}s)"
    if entry.application_id:
        line += f" [{entry.application_id}]"
    return line


__all__ = [
    "OUTCOMES",
    "PREPARED_PREFIX",
    "BatchOptions",
    "BatchOutcome",
    "BatchRow",
    "BatchSummary",
    "LedgerEntry",
    "append_ledger",
    "classify",
    "default_batch_dir",
    "default_batch_id",
    "default_command",
    "format_entry",
    "load_inventory",
    "plan",
    "read_inventory",
    "read_ledger",
    "render_summary_markdown",
    "run_batch",
    "summarize",
    "write_summary",
]
