"""``prepare-batch --retry BATCH_ID``: run the held and failed applications of an
earlier batch again, after a fix or after their questions were answered.

The applications come from the batch's ledger, each listing at its latest line, and
are selected by where they stand now in the application store
(``triage.current_outcome``):

* ``prepared``, ``closed``, ``duplicate`` and ``blocked`` are never run again, whether
  the ledger line or the store says so.
* ``--outcomes`` picks among ``needs_input``, ``failed_retryable``, ``unknown`` (a run
  started and recorded no outcome: it timed out or crashed) and ``error`` (the job
  never recorded an application); all four by default.
* A ``needs_input`` application whose open holds are all EXPLICIT_ANSWER_REQUIRED is
  skipped unless ``--include-explicit``: only the person can answer those. A hold the
  person answered after the application stopped, on it or with a saved answer for its
  wording, is no longer open (``triage.recorded_holds``).

Each selected application is continued with ``interviewmaxxing resume APP --json`` (an
``error`` row without an application runs ``apply URL`` again) by ``batch.run_batch``:
one subprocess per job, per-slot browser profiles, the per-job timeout, the pipeline
card bookkeeping, and the original batch's recorded run options (``summary.json``
``run_options``) for anything not given again. Nothing is submitted. The retry is a
batch of its own, with its own ledger, whose lines carry ``retry_of``,
``previous_outcome``, ``holds_before`` and ``holds_cleared``; running it again under the
same batch id continues it like any batch.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from interviewmaxxing_core import (
    Application,
    ApplicationStore,
    LocalPaths,
    MissingReason,
    NotFound,
)

from .batch import (
    LEDGER_NAME,
    BatchOptions,
    BatchRow,
    BatchRunOptions,
    BatchSummary,
    LedgerEntry,
    RetryStats,
    _plain_batch_id,
    latest_entries,
    read_ledger_lines,
    run_batch,
)
from .triage import candidate_saved_answers, current_outcome, hold_key, recorded_holds

RETRY_OUTCOMES: tuple[str, ...] = ("needs_input", "failed_retryable", "unknown", "error")
"""What ``--outcomes`` may select; all of them by default."""
NEVER_RETRIED: tuple[str, ...] = ("prepared", "closed", "duplicate", "blocked")
INHERITED: tuple[str, ...] = (
    "candidate_id", "workers", "per_job_timeout_s", "retry_retryable", "sync_closed",
    "browser", "opencli_profile", "ai_routing", "env_file", "writer_model",
    "rag_connection_file", "writer_effort",
)
"""``BatchOptions`` a retry takes from the original batch's ``run_options`` unless given
again (``--max-prepared`` is the retry's own; the browser is always headless)."""
_PATH_OPTIONS = ("env_file", "rag_connection_file")
_RETRY_SUFFIX = re.compile(r"-retry-\d{8}T\d{6}Z$")


def default_retry_id(batch_id: str, now: datetime | None = None) -> str:
    """``<batch id>-retry-YYYYmmddTHHMMSSZ``; retrying a retry replaces its suffix."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{_RETRY_SUFFIX.sub('', batch_id)}-retry-{stamp}"


def retry_options(paths: LocalPaths, *, retry_of: str, batch_id: str,
                  recorded: BatchRunOptions | None, cli: Mapping[str, Any],
                  explicit: Collection[str], max_prepared: int | None = None,
                  command: Sequence[str] = ()) -> BatchOptions:
    """The retry batch's options: the original batch's recorded run options, each
    replaced by its command-line value when that flag was given (``explicit``). A batch
    that recorded none (it predates ``run_options``) takes every value from ``cli``.
    Raises ``ValueError`` (a pydantic ``ValidationError``) for combinations that cannot
    work."""
    values: dict[str, Any] = {}
    for name in INHERITED:
        if recorded is None or name in explicit:
            values[name] = cli[name]
        else:
            values[name] = getattr(recorded, name)
    for name in _PATH_OPTIONS:
        if values[name] is not None:
            values[name] = Path(values[name])
    if batch_id == retry_of:
        raise ValueError("a retry is a batch of its own; give it another --batch-id than "
                         "the batch it retries")
    return BatchOptions(paths=paths, batch_id=batch_id, retry_of=retry_of,
                        max_prepared=max_prepared, command=list(command), **values)


@dataclass(frozen=True, slots=True)
class RetryItem:
    row: BatchRow
    """The ledger row, with the application to resume (None: ``apply`` its URL)."""
    previous_outcome: str
    holds_before: tuple[str, ...]
    """``hold_key`` of every hold of the application's current stop."""


@dataclass(frozen=True, slots=True)
class RetryPlan:
    retry_of: str
    items: tuple[RetryItem, ...]
    stats: RetryStats
    """What was considered, selected and skipped (``summarize`` adds the results)."""


def _application(store: ApplicationStore | None, candidate_id: str,
                 entry: LedgerEntry) -> Application | None:
    """The candidate's stored application for the row: the one the line names, else
    the one the store holds for its URL (a job that timed out before printing one)."""
    if store is None:
        return None
    if entry.application_id:
        try:
            app = store.get_application(entry.application_id)
        except NotFound:
            app = None
        if app is not None and app.candidate_id == candidate_id:
            return app
    return store.find_application(candidate_id, entry.application_url)


def _row(entry: LedgerEntry, application_id: str | None) -> BatchRow:
    return BatchRow(listing_id=entry.listing_id, pipeline_id=entry.pipeline_id,
                    company=entry.company, title=entry.title,
                    application_url=entry.application_url, backend=entry.backend,
                    status=entry.status, application_id=application_id)


def plan_retry(paths: LocalPaths, batch_id: str, *, candidate_id: str,
               outcomes: Collection[str] = RETRY_OUTCOMES, include_explicit: bool = False,
               backends: Collection[str] | None = None,
               limit: int | None = None) -> RetryPlan:
    """Select the applications of ``batch_id``'s ledger to run again (see the module
    docstring), in ledger order. Raises ``ValueError`` for a batch id that is not a
    plain name or an outcome that is never retried, and ``FileNotFoundError`` for a
    batch without a ledger. Reads only; creates nothing."""
    unknown = sorted(set(outcomes) - set(RETRY_OUTCOMES))
    if unknown:
        raise ValueError(f"cannot retry {', '.join(unknown)}; choose from "
                         f"{', '.join(RETRY_OUTCOMES)}")
    root = paths.home / "batches" / _plain_batch_id(batch_id)
    ledger = root / LEDGER_NAME
    if not ledger.is_file():
        raise FileNotFoundError(f"no ledger for batch {batch_id!r} under {root.parent}")
    wanted = set(outcomes)
    saved = candidate_saved_answers(paths, candidate_id)
    skipped: Counter[str] = Counter()
    items: list[RetryItem] = []
    chosen: set[str] = set()
    considered = 0
    entries, ignored = read_ledger_lines(ledger)
    store = ApplicationStore.open(paths.state_db) if paths.state_db.is_file() else None
    try:
        for entry in latest_entries(entries).values():
            if backends and entry.backend not in backends:
                continue
            considered += 1
            if entry.outcome in NEVER_RETRIED:
                skipped[entry.outcome] += 1
                continue
            app = _application(store, candidate_id, entry)
            if app is None or store is None:
                if entry.outcome != "error":
                    skipped["application not found"] += 1
                elif "error" not in wanted:
                    skipped["not selected (error)"] += 1
                else:
                    items.append(RetryItem(row=_row(entry, None), previous_outcome="error",
                                           holds_before=()))
                continue
            if app.id in chosen:  # another listing (an alias URL) of the same application
                skipped["same application as another listing"] += 1
                continue
            events = store.list_events(app.id)
            current = current_outcome(app, events)
            if current in NEVER_RETRIED:
                skipped[current] += 1
                continue
            if current not in wanted:
                skipped[f"not selected ({current})"] += 1
                continue
            holds = recorded_holds(store, app, events, saved, store.get_job(app.job_id))
            if (not include_explicit and holds.open and all(
                    m.reason is MissingReason.EXPLICIT_ANSWER_REQUIRED for m in holds.open)):
                skipped["explicit answers only"] += 1
                continue
            chosen.add(app.id)
            items.append(RetryItem(row=_row(entry, app.id), previous_outcome=current,
                                   holds_before=tuple(hold_key(m.label)
                                                      for m in holds.recorded)))
    finally:
        if store is not None:
            store.close()
    if limit is not None and len(items) > limit:
        skipped["over --limit"] += len(items) - limit
        items = items[:max(limit, 0)]
    stats = RetryStats(retry_of=batch_id, outcomes=[o for o in RETRY_OUTCOMES if o in wanted],
                       include_explicit=include_explicit, considered=considered,
                       selected=len(items), skipped=dict(skipped.most_common()),
                       ledger_lines_ignored=ignored)
    return RetryPlan(retry_of=batch_id, items=tuple(items), stats=stats)


def holds_cleared(before: Sequence[str], entry: LedgerEntry) -> int | None:
    """How many of the holds an application had before the retry (``hold_key``s) no
    longer hold it after ``entry``: all of them once it is prepared; for ``needs_input``,
    those whose wording is not asked again (a question asked twice counts twice). None
    for any other outcome."""
    if entry.outcome == "prepared":
        return len(before)
    if entry.outcome == "needs_input":
        after = Counter(hold_key(m.label) for m in entry.missing_items)
        return sum((Counter(before) - after).values())
    return None


async def run_retry(options: BatchOptions, plan: RetryPlan, *,
                    on_entry: Callable[[LedgerEntry], None] | None = None) -> BatchSummary:
    """Run ``plan`` as the batch ``options.batch_id`` (``options.retry_of`` must name the
    retried batch) and return its summary with the retry statistics."""
    if options.retry_of != plan.retry_of:
        raise ValueError("the options and the plan retry different batches")
    selected = {item.row.listing_id: item for item in plan.items}

    def annotate(entry: LedgerEntry) -> LedgerEntry:
        item = selected.get(entry.listing_id)
        if item is None:
            return entry
        return entry.model_copy(update={
            "previous_outcome": item.previous_outcome,
            "holds_before": len(item.holds_before),
            "holds_cleared": holds_cleared(item.holds_before, entry),
        })

    return await run_batch(options, [item.row for item in plan.items], on_entry=on_entry,
                           annotate=annotate, retry=plan.stats)


__all__ = [
    "INHERITED",
    "NEVER_RETRIED",
    "RETRY_OUTCOMES",
    "RetryItem",
    "RetryPlan",
    "default_retry_id",
    "holds_cleared",
    "plan_retry",
    "retry_options",
    "run_retry",
]
