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
* A ``needs_input`` application runs again only when something changed for it: at
  least one of its holds was answered after it stopped, on it (``answer``, or ``answer
  --sheet``, which saves each entry on each of its applications) or with a saved answer
  for its wording (``triage.recorded_holds``). ``--all`` runs every held one (after a
  fix). ``failed_retryable``, ``unknown`` and ``error`` applications always run again.
* A ``needs_input`` application whose open holds are all browser actions (sign-in,
  CAPTCHA, a custom control, a file: ``triage.needs_browser``) has nothing that can be
  answered, and a headless retry usually meets the same page again, so it is skipped as
  ``browser actions only`` unless ``--user-actions`` (or ``--all``); ``resume APP
  --act`` clears it in a visible browser.
* ``--only-app`` keeps only the named applications of the ledger (each must be one of
  them) and runs each held one of them even with nothing answered: naming it is the
  reason to run it.
* ``RetryStats.selected_by`` counts each selected application under the first reason
  that applies: its outcome (``failed_retryable``, ``unknown``, ``error``) or, held,
  ``answered since the stop``, ``--only-app``, ``user actions`` (browser actions only,
  ``--user-actions``), ``--all``.
* A ``needs_input`` application whose open holds are all EXPLICIT_ANSWER_REQUIRED is
  skipped unless ``--include-explicit``: only the person can answer those.

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
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
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
    BROWSER_ONLY_SKIP,
    LEDGER_NAME,
    BatchOptions,
    BatchRow,
    BatchRunOptions,
    BatchSummary,
    LedgerEntry,
    RetryStats,
    _plain_batch_id,
    batch_ledger,
    latest_entries,
    read_ledger,
    read_ledger_lines,
    require_ledgers,
    run_batch,
    with_saved_listings,
)
from .triage import (
    candidate_saved_answers,
    current_outcome,
    hold_key,
    needs_browser,
    recorded_holds,
)

RETRY_OUTCOMES: tuple[str, ...] = ("needs_input", "failed_retryable", "unknown", "error")
"""What ``--outcomes`` may select; all of them by default."""
NEVER_RETRIED: tuple[str, ...] = ("prepared", "closed", "duplicate", "blocked")
ANSWERED = "answered since the stop"
"""``RetryStats.selected_by`` for a held application with a question answered since it
stopped (by ``answer``, ``answer --sheet`` or a saved answer for its wording)."""
USER_ACTIONS = "user actions"
"""``RetryStats.selected_by`` for a held application with nothing answered whose open
holds are all browser actions, run because of ``--user-actions``."""
INHERITED: tuple[str, ...] = (
    "candidate_id", "workers", "per_job_timeout_s", "retry_retryable", "sync_closed",
    "browser", "opencli_profile", "ai_routing", "env_file", "writer_model",
    "rag_connection_file", "writer_effort", "captcha_solver", "captcha_budget_usd",
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
                    status=entry.status, application_id=application_id,
                    location=entry.location)


def _selection_reason(current: str, answered: int, *, browser_only: bool, rerun_all: bool,
                      user_actions: bool, only: bool) -> str | None:
    """Why an application that stands at ``current`` runs again (``RetryStats.selected_by``),
    or None when it does not. A held one needs a hold answered since its stop, to be named
    by ``--only-app``, ``--user-actions`` with only browser actions open, or ``--all``;
    the most specific reason that applies counts."""
    if current != "needs_input":
        return current
    if answered:
        return ANSWERED
    if only:
        return "--only-app"
    if user_actions and browser_only:
        return USER_ACTIONS
    return "--all" if rerun_all else None


def plan_retry(paths: LocalPaths, batch_id: str, *, candidate_id: str,
               outcomes: Collection[str] = RETRY_OUTCOMES, include_explicit: bool = False,
               rerun_all: bool = False, user_actions: bool = False,
               backends: Collection[str] | None = None, limit: int | None = None,
               only_apps: Collection[str] | None = None,
               jobs_db: Path | None = None) -> RetryPlan:
    """Select the applications of ``batch_id``'s ledger to run again (see the module
    docstring), in ledger order; with ``only_apps``, only those applications (a listing
    counts when its line or the store's application for its URL is one of them).
    ``user_actions`` also runs held applications with nothing answered whose open holds
    are all browser actions. Raises ``ValueError`` for a batch id that is not a plain
    name, an outcome that is never retried or an ``only_apps`` id that is none of the
    ledger's applications, and ``FileNotFoundError`` for a batch without a ledger. An
    application with a valid approval (a submission run of it stopped) is left to
    ``submit-approved``: preparing it again would withdraw the approval. Reads only;
    creates nothing."""
    unknown = sorted(set(outcomes) - set(RETRY_OUTCOMES))
    if unknown:
        raise ValueError(f"cannot retry {', '.join(unknown)}; choose from "
                         f"{', '.join(RETRY_OUTCOMES)}")
    root = paths.home / "batches" / _plain_batch_id(batch_id)
    ledger = root / LEDGER_NAME
    if not ledger.is_file():
        raise FileNotFoundError(f"no ledger for batch {batch_id!r} under {root.parent}")
    wanted = set(outcomes)
    only = list(dict.fromkeys(only_apps)) if only_apps else []
    only_set = set(only)
    saved = candidate_saved_answers(paths, candidate_id)
    skipped: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    items: list[RetryItem] = []
    chosen: set[str] = set()
    named: set[str] = set()
    considered = 0
    entries, ignored = read_ledger_lines(ledger)
    store = ApplicationStore.open(paths.state_db) if paths.state_db.is_file() else None
    try:
        for entry in latest_entries(entries).values():
            if backends and entry.backend not in backends:
                continue
            app = _application(store, candidate_id, entry) if only else None
            if only:
                ids = {i for i in (entry.application_id, app.id if app else None) if i} & only_set
                if not ids:
                    continue
                named |= ids
            considered += 1
            if entry.outcome in NEVER_RETRIED:
                skipped[entry.outcome] += 1
                continue
            if not only:
                app = _application(store, candidate_id, entry)
            if app is None or store is None:
                if entry.outcome != "error":
                    skipped["application not found"] += 1
                elif "error" not in wanted:
                    skipped["not selected (error)"] += 1
                else:
                    reasons["error"] += 1
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
            if store.submission_approval(app.id) is not None:
                skipped["approved (left to submit-approved)"] += 1
                continue
            if current not in wanted:
                skipped[f"not selected ({current})"] += 1
                continue
            holds = recorded_holds(store, app, events, saved, store.get_job(app.job_id))
            browser_only = bool(holds.open) and all(needs_browser(m) for m in holds.open)
            reason = _selection_reason(current, holds.answered, browser_only=browser_only,
                                       rerun_all=rerun_all, user_actions=user_actions,
                                       only=bool(only))
            if reason is None:
                # Browser actions cannot be answered, so they are told apart from holds
                # that wait for an answer.
                skipped[BROWSER_ONLY_SKIP if browser_only else "nothing answered since the stop"] += 1
                continue
            if (not include_explicit and holds.open and all(
                    m.reason is MissingReason.EXPLICIT_ANSWER_REQUIRED for m in holds.open)):
                skipped["explicit answers only"] += 1
                continue
            chosen.add(app.id)
            reasons[reason] += 1
            items.append(RetryItem(row=_row(entry, app.id), previous_outcome=current,
                                   holds_before=tuple(hold_key(m.label)
                                                      for m in holds.recorded)))
    finally:
        if store is not None:
            store.close()
    missing = [app_id for app_id in only if app_id not in named]
    if missing:
        raise ValueError(f"--only-app: not an application of batch {batch_id!r} for candidate "
                         f"{candidate_id!r}: {', '.join(missing)}")
    if limit is not None and len(items) > limit:
        skipped["over --limit"] += len(items) - limit
        items = items[:max(limit, 0)]
    stats = RetryStats(retry_of=batch_id, outcomes=[o for o in RETRY_OUTCOMES if o in wanted],
                       include_explicit=include_explicit, rerun_all=rerun_all,
                       user_actions=user_actions, only_apps=only, considered=considered,
                       selected=len(items), selected_by=dict(reasons.most_common()),
                       skipped=dict(skipped.most_common()), ledger_lines_ignored=ignored)
    if jobs_db is not None and items:
        # A row keeps its ledger line's listing location; an older line without one takes
        # its saved listing's from the jobs store (round 5).
        rows, _ = with_saved_listings([item.row for item in items], jobs_db)
        items = [replace(item, row=row) for item, row in zip(items, rows, strict=True)]
    return RetryPlan(retry_of=batch_id, items=tuple(items), stats=stats)


def batch_applications(paths: LocalPaths, batch_ids: Iterable[str], *,
                       candidate_id: str) -> set[str]:
    """The applications ``holds --batch-id`` reads: of each listing of each batch at its
    latest line there, the ones held or failed there (``needs_input``,
    ``failed_retryable``, ``error`` or ``already_recorded``: what ``--retry`` of the batch
    considers), resolved as ``plan_retry`` resolves them (the line's application of this
    candidate, else the store's application for its URL). Raises ``ValueError`` and
    ``FileNotFoundError`` as ``batch.require_ledgers``. Reads only; creates nothing."""
    ids = require_ledgers(paths, batch_ids)
    if not paths.state_db.is_file():
        return set()
    found: set[str] = set()
    with ApplicationStore.open(paths.state_db) as store:
        for batch_id in ids:
            for entry in latest_entries(read_ledger(batch_ledger(paths, batch_id))).values():
                if entry.outcome in NEVER_RETRIED:
                    continue
                app = _application(store, candidate_id, entry)
                if app is not None:
                    found.add(app.id)
    return found


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
    "ANSWERED",
    "INHERITED",
    "NEVER_RETRIED",
    "RETRY_OUTCOMES",
    "USER_ACTIONS",
    "RetryItem",
    "RetryPlan",
    "batch_applications",
    "default_retry_id",
    "holds_cleared",
    "plan_retry",
    "retry_options",
    "run_retry",
]
