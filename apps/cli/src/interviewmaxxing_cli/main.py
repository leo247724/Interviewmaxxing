"""``interviewmaxxing`` entry point.

``apply`` runs the whole supplied-URL flow through ``LocalApplicationRunner``: it
records the request, inspects the live form in a real browser, answers from the
verified profile, stops with a recorded NEEDS_INPUT result when a required answer
is missing, submits once and reports SUBMITTED only when the site's confirmation
tied to the job was observed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from interviewmaxxing_browser.driver import DriverError
from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_core import (
    AnswerReuse,
    Application,
    ApplicationState,
    ApplicationStore,
    ApplyOutcome,
    ClaimUnavailable,
    InvalidApplicationUrl,
    LocalPaths,
    MissingInput,
    MissingReason,
    Receipt,
    StoreError,
    UserInput,
    UserInteraction,
    normalize_application_url,
)

from .answers import AnswerError, describe, user_input_for
from .interaction import TerminalInteraction
from .runner import (
    LocalApplicationRunner,
    NoninteractiveInteraction,
    create_runner,
    pending_inputs,
)

S = ApplicationState

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INCOMPLETE = 3
"""Not submitted: waiting for your answers or action, or stopped (retryable/permanent)."""
EXIT_BLOCKED = 4
"""Nothing was done because the stored state forbids it (already submitted, duplicate,
another run in progress) or no confirmation exists."""
EXIT_UNCERTAIN = 5
"""A submit may have reached the employer without a confirmation; reconcile it."""
EXIT_INTERRUPTED = 130

PROG = "interviewmaxxing"
DESCRIPTION = """\
Prepare job applications you choose, using your verified profile and resume.

Runs stop at the final review step without submitting. You are asked for missing
required information or for actions such as sign-in or CAPTCHA. Resuming a
preparation-only application keeps submission disabled.
"""
EPILOG = """\
typical flow:
  interviewmaxxing apply URL                 run; stops with questions if any
  interviewmaxxing answer APP --set F=V ...  answer the recorded questions
  interviewmaxxing resume APP                continue from the site
  interviewmaxxing reconcile APP             re-check an uncertain submission

local data (never in source control):
  IMX_HOME           root directory (default ~/.interviewmaxxing)
  IMX_PROFILE_DIR    candidate profile and resume     ($IMX_HOME/profile)
  IMX_STATE_DB       application state database        ($IMX_HOME/state/imx.sqlite3)
  IMX_ARTIFACTS_DIR  confirmation evidence             ($IMX_HOME/artifacts)
  IMX_BROWSER_DIR    persistent browser profile        ($IMX_HOME/browser)
  IMX_CANDIDATE_ID   candidate used by apply           (default)

exit status: 0 submitted/ok, 1 error, 2 usage, 3 not submitted (input needed or
stopped), 4 blocked by state, 5 submission uncertain, 130 interrupted
"""


def _owner() -> str:
    return f"cli:{socket.gethostname()}:{os.getpid()}"


def _dump(model: BaseModel | Sequence[BaseModel] | dict[str, Any]) -> str:
    if isinstance(model, BaseModel):
        data: Any = model.model_dump(mode="json")
    elif isinstance(model, dict):
        data = {
            k: (v.model_dump(mode="json") if isinstance(v, BaseModel) else
                [i.model_dump(mode="json") for i in v] if isinstance(v, list) else v)
            for k, v in model.items()
        }
    else:
        data = [m.model_dump(mode="json") for m in model]
    return json.dumps(data, indent=2, sort_keys=False)


def _fmt_time(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ") if value else "-"


def _paths(args: argparse.Namespace) -> LocalPaths:
    return LocalPaths.from_env(home=Path(args.home) if args.home else None)


def _open_existing(paths: LocalPaths) -> ApplicationStore | None:
    if not paths.state_db.exists():
        return None
    return ApplicationStore.open(paths.state_db)


def _state_label(app: Application) -> str:
    label = app.state.value
    if app.state is S.SUBMITTING and (
        app.claim_expires_at is None or app.claim_expires_at <= datetime.now(UTC)
    ):
        label += " (interrupted; will become SUBMISSION_UNKNOWN)"
    return label


def _next_steps(outcome: ApplyOutcome) -> list[str]:
    app = outcome.application_id
    state = outcome.state
    if state is S.NEEDS_INPUT:
        steps = []
        if any(m.field_id is not None for m in outcome.missing_inputs):
            steps.append(f"{PROG} answer {app} --set FIELD=VALUE ...   (then resume)")
        if any(m.reason is MissingReason.USER_ACTION or
               m.reason is MissingReason.UNSUPPORTED_CONTROL for m in outcome.missing_inputs):
            steps.append(f"{PROG} resume {app} --act   (a visible browser opens; complete the "
                         "step there)")
        steps.append(f"{PROG} resume {app}")
        return steps
    if state is S.SUBMISSION_UNKNOWN:
        return [f"{PROG} reconcile {app}   (re-checks the site; never resubmits)"]
    if state is S.SUBMITTING:
        return [f"{PROG} status {app}   (a submit is in progress; it is never repeated)",
                f"{PROG} reconcile {app}   (once that run is gone and its lease has lapsed: "
                "re-checks the site, never resubmits)"]
    if state in (S.FAILED_RETRYABLE, S.REQUESTED, S.INSPECTING, S.PACKET_READY, S.FILLING):
        return [f"{PROG} resume {app}"]
    if state is S.SUBMITTED:
        return [f"{PROG} receipt {app}"]
    return []


PREPARED_MESSAGE_PREFIX = "Prepared to the final review step"


def _preparation_lines(state: ApplicationState, events: Sequence[Any],
                       waiting: Sequence[MissingInput]) -> list[str]:
    """Status lines for an application that reached its final review step and stopped
    without submitting (state NEEDS_INPUT, nothing asked, a ``preparation.ready``
    event). Empty for every other application."""
    if state is not S.NEEDS_INPUT or waiting:
        return []
    ready = [e for e in events if getattr(e, "event", None) == "preparation.ready"]
    if not ready:
        return []
    metadata = getattr(ready[-1], "metadata", {}) or {}
    step = metadata.get("form_step")
    lines = ["prepared:     final review step reached"
             + (f" (form step {step})" if step is not None else "")
             + "; nothing was submitted"]
    if metadata.get("captcha_pending"):
        lines.append("captcha:      a CAPTCHA on this form must be solved in the browser before submission")
    return lines


def _review_steps(application_id: str, artifacts_dir: Path) -> list[str]:
    return [f"review the filled form evidence under {artifacts_dir / application_id}/",
            f"{PROG} events {application_id}   (preparation.ready records the final step)",
            f"{PROG} resume {application_id}   (re-prepares from the site; submission stays disabled)"]


def _print_receipt(receipt: Receipt) -> None:
    print(f"SUBMITTED  {receipt.application_id}")
    job = " - ".join(x for x in (receipt.company, receipt.title) if x) or receipt.job_id
    print(f"job:           {job}")
    print(f"url:           {receipt.application_url}")
    print(f"submitted at:  {_fmt_time(receipt.submitted_at)}")
    print(f"confirmed at:  {_fmt_time(receipt.confirmed_at)}")
    print(f"reference:     {receipt.confirmation_reference or '(none shown by site)'}")
    if receipt.reconciliation_method:
        print(f"established:   {receipt.reconciliation_method.value}")
    for signal_text in receipt.signals:
        print(f"signal:        {signal_text}")
    for ev in receipt.evidence:
        print(f"evidence:      {ev.kind.value} {ev.path or ev.uri or ev.description}")


def _print_outcome(outcome: ApplyOutcome, *, as_json: bool) -> None:
    if as_json:
        print(_dump(outcome))
        return
    print(f"application: {outcome.application_id}")
    print(f"state:       {outcome.state.value}")
    if outcome.message:
        print(f"result:      {outcome.message}")
    if outcome.receipt is not None and outcome.state is S.SUBMITTED:
        print()
        _print_receipt(outcome.receipt)
    if outcome.missing_inputs:
        print("\nneeded from you:")
        for item in outcome.missing_inputs:
            text = describe(item) if item.field_id else f"{item.label}\n  {item.prompt}"
            print("- " + text.replace("\n", "\n  "))
    if (outcome.state is S.NEEDS_INPUT and not outcome.missing_inputs
            and outcome.message.startswith(PREPARED_MESSAGE_PREFIX)):
        steps = _review_steps(outcome.application_id, LocalPaths.from_env().artifacts_dir)
    else:
        steps = _next_steps(outcome)
    if steps:
        print("\nnext:")
        for step in steps:
            print(f"  {step}")


def _exit_code(outcome: ApplyOutcome, *, already: bool = False) -> int:
    if outcome.state is S.SUBMITTED:
        return EXIT_BLOCKED if already else EXIT_OK
    if outcome.state is S.SUBMISSION_UNKNOWN:
        return EXIT_UNCERTAIN
    if outcome.state in (S.DUPLICATE, S.SUBMITTING, S.WITHDRAWN):
        return EXIT_BLOCKED
    return EXIT_INCOMPLETE


def _interaction(args: argparse.Namespace) -> UserInteraction:
    if getattr(args, "interactive", False):
        return TerminalInteraction(reuse=AnswerReuse(args.reuse.upper()))
    return NoninteractiveInteraction(allow_browser_action=getattr(args, "act", False))


def _check_run_options(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Usage errors for run flags that cannot work together (exit 2, before any
    state is touched)."""
    if getattr(args, "act", False) and getattr(args, "headless", False):
        parser.error("--act means you will act in the browser window, which --headless hides; "
                     "drop --headless (or drop --act to stop as NEEDS_INPUT instead)")
    if getattr(args, "interactive", False) and not sys.stdin.isatty():
        parser.error("--interactive needs a terminal; use `answer` and `resume` instead")
    if hasattr(args, "browser"):
        try:
            _dynamic_options(args).validate()
        except ValueError as exc:
            parser.error(str(exc))


def _dynamic_options(args: argparse.Namespace) -> Any:
    from .dynamic import DynamicOptions

    return DynamicOptions(browser=args.browser, opencli_profile=args.opencli_profile,
                          ai_routing=args.ai_routing,
                          env_file=Path(args.env_file) if args.env_file else None,
                          writer_model=args.writer_model,
                          rag_connection_file=Path(args.rag_connection_file) if args.rag_connection_file else None)


def _runner(args: argparse.Namespace) -> LocalApplicationRunner:
    kwargs: dict[str, Any] = {}
    if args.ai_routing or args.browser != "playwright":
        kwargs["dynamic_options"] = _dynamic_options(args)
    return create_runner(_paths(args), headless=args.headless, interaction=_interaction(args), **kwargs)


def cmd_classify(args: argparse.Namespace) -> int:
    from .dynamic import classify_url

    try:
        url = normalize_application_url(args.url)
        result = asyncio.run(classify_url(
            url, options=_dynamic_options(args), headless=args.headless,
            artifacts_dir=_paths(args).artifacts_dir / "classification",
        ))
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(result, indent=2, default=str))
    return EXIT_OK


def _run(call: Callable[[], Awaitable[ApplyOutcome]]) -> ApplyOutcome:
    """Run on a fresh loop. Ctrl-C or SIGTERM cancels the run; a cancellation during a
    submit is recorded as SUBMISSION_UNKNOWN by the runner before it propagates."""

    async def main() -> ApplyOutcome:
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            return await call()
        finally:
            loop.remove_signal_handler(signal.SIGTERM)

    with asyncio.Runner() as runner:
        return runner.run(main())


def _interrupted(paths: LocalPaths, application_id: str | None) -> int:
    message = "Interrupted."
    store = _open_existing(paths)
    if store is not None and application_id:
        with store:
            app = store.get_application(application_id)
            message += f" Application {app.id} is {_state_label(app)}."
    print(message, file=sys.stderr)
    return EXIT_INTERRUPTED


# --- commands --------------------------------------------------------------------


def cmd_apply(args: argparse.Namespace) -> int:
    paths = _paths(args)
    candidate_id = args.candidate or paths.candidate_id
    try:
        normalize_application_url(args.url)
    except InvalidApplicationUrl as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    paths.ensure()
    with ApplicationStore.open(paths.state_db) as store:
        existing = store.find_application(candidate_id, args.url)
    already = existing is not None and existing.state is S.SUBMITTED
    runner = _runner(args)
    try:
        outcome = _run(lambda: runner.apply(args.url, candidate_id=candidate_id))
    except (KeyboardInterrupt, asyncio.CancelledError):
        with ApplicationStore.open(paths.state_db) as store:
            app = store.find_application(candidate_id, args.url)
        return _interrupted(paths, app.id if app else None)
    _print_outcome(outcome, as_json=args.json)
    return _exit_code(outcome, already=already)


def cmd_resume(args: argparse.Namespace) -> int:
    paths = _paths(args)
    if _open_existing(paths) is None:
        print("No state database.", file=sys.stderr)
        return EXIT_ERROR
    runner = _runner(args)
    try:
        outcome = _run(lambda: runner.resume(args.application_id))
    except (KeyboardInterrupt, asyncio.CancelledError):
        return _interrupted(paths, args.application_id)
    _print_outcome(outcome, as_json=args.json)
    return _exit_code(outcome)


def cmd_reconcile(args: argparse.Namespace) -> int:
    paths = _paths(args)
    if _open_existing(paths) is None:
        print("No state database.", file=sys.stderr)
        return EXIT_ERROR
    runner = _runner(args)
    try:
        outcome = _run(lambda: runner.reconcile(args.application_id))
    except (KeyboardInterrupt, asyncio.CancelledError):
        return _interrupted(paths, args.application_id)
    _print_outcome(outcome, as_json=args.json)
    if outcome.state is S.SUBMITTED:
        return EXIT_OK
    return EXIT_UNCERTAIN if outcome.state is S.SUBMISSION_UNKNOWN else EXIT_BLOCKED


def _raw_answers(args: argparse.Namespace) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if args.answers:
        data = json.loads(Path(args.answers).read_text())
        if not isinstance(data, dict):
            raise AnswerError("--answers must be a JSON object of field id to value")
        raw.update(data)
    for item in args.set or []:
        field_id, sep, value = item.partition("=")
        if not sep or not field_id.strip():
            raise AnswerError(f"--set expects FIELD=VALUE, got {item!r}")
        raw[field_id.strip()] = value
    if not raw:
        raise AnswerError("give answers with --set FIELD=VALUE or --answers FILE.json")
    return raw


def cmd_answer(args: argparse.Namespace) -> int:
    paths = _paths(args)
    store = _open_existing(paths)
    if store is None:
        print("No state database.", file=sys.stderr)
        return EXIT_ERROR
    with store:
        app = store.get_application(args.application_id)
        questions = [m for m in pending_inputs(store, app.id) if m.field_id is not None]
        if app.state is not S.NEEDS_INPUT:
            print(f"{app.id} is {app.state.value} and is not waiting for answers.",
                  file=sys.stderr)
            return EXIT_BLOCKED
        if not questions:
            print(f"{app.id} is NEEDS_INPUT but no typed answers are recorded for it (it waits "
                  f"for an action in the browser, or the site's last response still has to be "
                  f"re-inspected). Run `{PROG} resume {app.id}`; questions are recorded there.",
                  file=sys.stderr)
            return EXIT_BLOCKED
        by_field = {m.field_id: m for m in questions}
        reuse = AnswerReuse(args.reuse.upper())
        try:
            raw = _raw_answers(args)
            unknown = sorted(set(raw) - set(by_field))
            if unknown:
                raise AnswerError(f"not a recorded question for {app.id}: {', '.join(unknown)}")
            inputs: list[UserInput] = [
                user_input_for(by_field[fid], value, reuse=reuse) for fid, value in raw.items()
            ]
        except (AnswerError, json.JSONDecodeError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        try:
            claim = store.claim(app.id, _owner())
        except ClaimUnavailable:
            print("Another run is working on this application; try again shortly.",
                  file=sys.stderr)
            return EXIT_BLOCKED
        try:
            store.save_user_inputs(claim, inputs)
        finally:
            store.release(claim)
        job = store.get_job(app.job_id)
        candidates = LocalCandidateStore.from_paths(paths)
        for user_input in inputs:
            saved = user_input.to_saved_answer(job=job)
            if saved is not None:
                candidates.save_answer(app.candidate_id, saved)
        answered = {(u.field_id, u.field_fingerprint) for u in store.list_user_inputs(app.id)}
        remaining = [m for m in questions if m.required
                     and (m.field_id, m.field_fingerprint) not in answered]
    print(f"saved {len(inputs)} answer(s) for {app.id} (reuse: {reuse.value.lower()})")
    if remaining:
        print("still unanswered: " + ", ".join(str(m.field_id) for m in remaining))
    print(f"next: {PROG} resume {app.id}")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    paths = _paths(args)
    store = _open_existing(paths)
    if store is None:
        if args.json:
            print("[]")
        else:
            print(f"No applications yet (no state database at {paths.state_db}).")
        return EXIT_OK if args.application_id is None else EXIT_ERROR
    with store:
        if args.application_id is None:
            apps = store.list_applications(candidate_id=args.candidate)
            if args.json:
                print(_dump(apps))
                return EXIT_OK
            if not apps:
                print("No applications yet.")
                return EXIT_OK
            print(f"{'APPLICATION':<37} {'STATE':<20} {'UPDATED':<21} JOB")
            for app in apps:
                job = store.get_job(app.job_id)
                where = " - ".join(x for x in (job.company, job.title) if x) or job.application_url
                print(f"{app.id:<37} {app.state.value:<20} {_fmt_time(app.updated_at):<21} {where}")
            return EXIT_OK

        app = store.get_application(args.application_id)
        job = store.get_job(app.job_id)
        attempts = store.list_attempts(app.id)
        events = store.list_events(app.id)
        waiting = pending_inputs(store, app.id)
        if args.json:
            print(_dump({"application": app, "job": job, "attempts": attempts,
                         "pending_inputs": waiting, "packet": store.latest_packet(app.id),
                         "requests": store.list_requests(app.id)}))
            return EXIT_OK
        print(f"application:  {app.id}")
        print(f"state:        {_state_label(app)}")
        print(f"candidate:    {app.candidate_id}")
        print(f"job:          {job.id}")
        print(f"  url:        {job.application_url}")
        for label, value in (("company", job.company), ("title", job.title),
                             ("ats", job.ats_type), ("identity", job.identity_key)):
            if value:
                print(f"  {label + ':':<11} {value}")
        if app.failure_reason:
            print(f"reason:       {app.failure_reason}")
        if app.duplicate_of:
            print(f"duplicate of: {app.duplicate_of}")
        if app.submitted_at:
            print(f"submitted:    {_fmt_time(app.submitted_at)}")
        for attempt in attempts:
            print(f"attempt {attempt.attempt_number}:    {attempt.outcome or 'IN PROGRESS'}"
                  f" (started {_fmt_time(attempt.started_at)})")
        if waiting:
            print("needed from you:")
            for item in waiting:
                text = describe(item) if item.field_id else f"{item.label}\n  {item.prompt}"
                print("- " + text.replace("\n", "\n  "))
        prepared = _preparation_lines(app.state, events, waiting)
        for line in prepared:
            print(line)
        print(f"events:       {len(events)} ({PROG} events {app.id})")
        steps = (_review_steps(app.id, paths.artifacts_dir) if prepared
                 else _next_steps(ApplyOutcome(application_id=app.id, state=app.state,
                                               missing_inputs=waiting)))
        for step in steps:
            print(f"next:         {step}")
    return EXIT_OK


def cmd_events(args: argparse.Namespace) -> int:
    store = _open_existing(_paths(args))
    if store is None:
        print("No state database.", file=sys.stderr)
        return EXIT_ERROR
    with store:
        store.get_application(args.application_id)
        events = store.list_events(args.application_id)
        if args.json:
            print(_dump(events))
            return EXIT_OK
        for e in events:
            change = f" {e.from_state or '∅'} -> {e.to_state}" if e.to_state else ""
            detail = json.dumps(e.metadata, sort_keys=True) if e.metadata else ""
            print(f"{e.sequence:>5} {_fmt_time(e.timestamp)} {e.event}{change} {detail}".rstrip())
    return EXIT_OK


def cmd_receipt(args: argparse.Namespace) -> int:
    store = _open_existing(_paths(args))
    if store is None:
        print("No state database.", file=sys.stderr)
        return EXIT_ERROR
    with store:
        app = store.get_application(args.application_id)
        receipt = store.get_receipt(app.id)
        if receipt is None:
            print(f"No receipt: application {app.id} is {_state_label(app)}; "
                  "no confirmed submission has been recorded.")
            return EXIT_BLOCKED
        if args.json:
            print(_dump(receipt))
            return EXIT_OK
        _print_receipt(receipt)
    return EXIT_OK


def _csv(value: str | None) -> set[str] | None:
    if value is None:
        return None
    return {item.strip() for item in value.split(",") if item.strip()}


def cmd_prepare_batch(args: argparse.Namespace) -> int:
    """Prepare every matching inventory row through ``apply`` subprocesses; see
    ``interviewmaxxing_cli.batch``. Always headless; never submits."""
    from pydantic import ValidationError

    from .batch import (
        BatchOptions,
        LedgerEntry,
        default_batch_id,
        format_entry,
        read_inventory,
        render_summary_markdown,
        run_batch,
    )

    paths = _paths(args)
    try:
        rows, invalid = read_inventory(Path(args.inventory), backends=_csv(args.backends),
                                       statuses=_csv(args.statuses) or set(), limit=args.limit)
        options = BatchOptions(
            paths=paths, candidate_id=args.candidate or paths.candidate_id,
            batch_id=args.batch_id or default_batch_id(), workers=args.workers,
            per_job_timeout_s=args.per_job_timeout, retry_retryable=args.retry_retryable,
            max_prepared=args.max_prepared, include_existing=args.include_existing,
            sync_closed=args.sync_closed,
            browser=args.browser, opencli_profile=args.opencli_profile,
            ai_routing=args.ai_routing, env_file=Path(args.env_file) if args.env_file else None,
            writer_model=args.writer_model,
            rag_connection_file=Path(args.rag_connection_file) if args.rag_connection_file else None,
        )
    except ValidationError as exc:
        problems = "; ".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())
        print(f"error: {problems}", file=sys.stderr)
        return EXIT_USAGE
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not rows:
        print(f"error: no inventory rows match (skipped {invalid} without a valid URL)",
              file=sys.stderr)
        return EXIT_USAGE
    progress = sys.stderr if args.json else sys.stdout

    def show(entry: LedgerEntry) -> None:
        print(format_entry(entry), file=progress, flush=True)

    async def batch() -> Any:
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            return await run_batch(options, rows, on_entry=show, skipped_invalid_url=invalid)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)

    print(f"batch {options.batch_id}: {len(rows)} row(s), {options.workers} worker(s), "
          f"preparation only; ledger in {options.batch_dir}", file=progress, flush=True)
    try:
        with asyncio.Runner() as runner:
            summary = runner.run(batch())
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(f"Interrupted. Finished jobs are in {options.batch_dir}; run the same "
              f"--batch-id again to continue.", file=sys.stderr)
        return EXIT_INTERRUPTED
    if args.json:
        print(_dump(summary))
    else:
        print()
        print(render_summary_markdown(summary), end="")
        print(f"batch directory: {options.batch_dir}")
    return EXIT_OK if sum(summary.totals.values()) else EXIT_ERROR


def cmd_batch_report(args: argparse.Namespace) -> int:
    """Summarize one or all prepare-batch ledgers; read-only. See
    ``interviewmaxxing_cli.batch.build_report``."""
    from .batch import build_report, render_report_markdown

    paths = _paths(args)
    try:
        report = build_report(paths, None if args.batch_id is None else [args.batch_id],
                              top=args.top)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.json:
        print(_dump(report))
    else:
        print(render_report_markdown(report), end="")
    return EXIT_OK


def cmd_paths(args: argparse.Namespace) -> int:
    paths = _paths(args)
    data = {
        "home": str(paths.home),
        "profile_dir": str(paths.profile_dir),
        "state_db": str(paths.state_db),
        "artifacts_dir": str(paths.artifacts_dir),
        "browser_dir": str(paths.browser_dir),
        "candidate_id": paths.candidate_id,
    }
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        for key, value in data.items():
            print(f"{key + ':':<15} {value}")
    return EXIT_OK


# --- parser ----------------------------------------------------------------------


def _dynamic_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--browser", choices=["playwright", "opencli"], default="playwright")
    p.add_argument("--opencli-profile", help="connected OpenCLI browser profile alias")
    p.add_argument("--ai-routing", action="store_true", help="opt in to bounded Jev semantic routing")
    p.add_argument("--env-file", help="explicit OpenRouter credential env file for AI routing")
    p.add_argument("--writer-model", help="explicit narrative writer model ID for AI routing")
    p.add_argument("--rag-connection-file", help="absolute private JSON connection file for Supabase retrieval")


def _int_range(low: int, high: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"expected {low}..{high}, got {value}")
        return value

    return parse


def _run_options(p: argparse.ArgumentParser, *, interactive: bool = True) -> None:
    _dynamic_flags(p)
    p.add_argument("--headless", action="store_true",
                   help="hide the browser window (sign-in, CAPTCHA and custom controls then stop "
                        "as NEEDS_INPUT; cannot be combined with --act)")
    p.add_argument("--json", action="store_true", help="print the outcome as JSON")
    if interactive:
        p.add_argument("--interactive", action="store_true",
                       help="ask missing questions on this terminal instead of stopping")
        p.add_argument("--act", action="store_true",
                       help="you will sign in, solve a CAPTCHA or set custom controls in the "
                            "visible browser window when asked")
        p.add_argument("--reuse", choices=["application", "job", "global"],
                       default="application",
                       help="with --interactive: how far your answers may be reused")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--home", metavar="DIR", help="override IMX_HOME for this command")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    p = sub.add_parser(
        "apply",
        help="prepare the application and stop before final submission",
        description="Prepare the job application at APPLICATION_URL with your verified profile. "
        "Stop at the final review step without submitting. Missing required answers or browser "
        "actions stop the run earlier. The no-submit restriction is preserved on resume.",
    )
    p.add_argument("url", metavar="APPLICATION_URL")
    p.add_argument("--candidate", metavar="ID", help="candidate id (default: IMX_CANDIDATE_ID)")
    _run_options(p)
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser(
        "prepare-batch",
        help="prepare many Saved jobs from an inventory file (headless; never submits)",
        description="Run `apply` for every matching row of a JSON inventory, at most "
        "--workers at a time, each worker with its own browser profile. Every application "
        "stops at its final review step or earlier (NEEDS_INPUT); nothing is submitted. "
        "Finished jobs are recorded in $IMX_HOME/batches/<batch id>/ledger.jsonl; running "
        "the same --batch-id again skips them and retries failures. Sign-in, CAPTCHA and "
        "custom controls stop as NEEDS_INPUT; finish them later with `resume APP --act`. "
        "A row's application is linked to its Saved pipeline card (pipeline_id); a job that "
        "no longer accepts applications moves its Saved card to Closed with a dated note. "
        "No card is created or moved to Applied. Summarize ledgers with `batch-report`.",
    )
    p.add_argument("--inventory", required=True, metavar="FILE",
                   help="JSON list of Saved jobs (listing_id, pipeline_id, "
                        "source_application_url, backend, status, company, title)")
    p.add_argument("--backends", metavar="A,B,C", help="only these backends")
    p.add_argument("--statuses", metavar="A,B", default="resolved",
                   help="only these inventory statuses (default: resolved)")
    p.add_argument("--limit", type=_int_range(1, 1_000_000), metavar="N",
                   help="at most N rows, after filtering")
    p.add_argument("--workers", type=_int_range(1, 8), default=1, metavar="N",
                   help="concurrent applications, each in its own browser profile (1..8)")
    p.add_argument("--retry-retryable", type=_int_range(0, 2), default=1, metavar="N",
                   help="extra attempts for retryable failures and errors (0..2)")
    p.add_argument("--max-prepared", type=_int_range(1, 1_000_000), metavar="N",
                   help="stop launching once N applications are prepared in this batch")
    p.add_argument("--per-job-timeout", type=float, default=900.0, metavar="SECONDS",
                   help="kill a job that runs longer than this (default 900)")
    p.add_argument("--batch-id", metavar="ID", help="name of the batch (default: UTC timestamp); "
                                                    "reuse it to resume")
    p.add_argument("--include-existing", action="store_true",
                   help="also run URLs that already have an application in the store")
    p.add_argument("--sync-closed", action=argparse.BooleanOptionalAction, default=True,
                   help="move the linked Saved card of a job that no longer accepts "
                        "applications to Closed, with a dated note (default: on)")
    p.add_argument("--candidate", metavar="ID", help="candidate id (default: IMX_CANDIDATE_ID)")
    _dynamic_flags(p)
    p.add_argument("--json", action="store_true", help="print the summary as JSON")
    p.set_defaults(func=cmd_prepare_batch)

    p = sub.add_parser(
        "batch-report",
        help="summarize prepare-batch ledgers: outcomes, holds by category, durations",
        description="Read one batch ledger ($IMX_HOME/batches/<batch id>/ledger.jsonl) or "
        "all of them and print totals by outcome and backend, the questions that held "
        "applications grouped into categories (custom_control, explicit_answer, "
        "screener_yes_no, lookup, narrative, other) with their application ids, "
        "per-backend median and p95 durations and pipeline card links. Writes no file; "
        "question wording is truncated to 80 characters and CLI messages are never shown.",
    )
    p.add_argument("batch_id", nargs="?", metavar="BATCH_ID",
                   help="one batch (default: every batch under $IMX_HOME/batches)")
    p.add_argument("--top", type=_int_range(1, 100), default=10, metavar="N",
                   help="questions shown per category (1..100, default 10)")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.set_defaults(func=cmd_batch_report)

    p = sub.add_parser("classify", help="observe one URL without filling or advancing forms")
    p.add_argument("url", metavar="APPLICATION_URL")
    _run_options(p, interactive=False)
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("resume", help="continue a stopped application from the site",
                       description="Re-inspect the site and continue, using answers saved "
                       "with `answer`. Never resubmits an uncertain or confirmed application.")
    p.add_argument("application_id", metavar="APPLICATION_ID")
    _run_options(p)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser(
        "answer",
        help="answer the questions a NEEDS_INPUT application recorded",
        description="Save answers to the exact recorded questions (see `status`). Choices "
        "may be given by option value or label; several choices are separated by ';'; "
        "checkboxes take yes or no. Answers stay with this application unless --reuse "
        "says otherwise. Then run `resume`.",
    )
    p.add_argument("application_id", metavar="APPLICATION_ID")
    p.add_argument("--set", action="append", metavar="FIELD=VALUE", help="one answer (repeatable)")
    p.add_argument("--answers", metavar="FILE", help="JSON object mapping field id to value")
    p.add_argument("--reuse", choices=["application", "job", "global"], default="application",
                   help="application: this application only (default); job: this job; "
                        "global: any job")
    p.set_defaults(func=cmd_answer)

    p = sub.add_parser(
        "reconcile",
        help="re-check an uncertain submission on the site (never resubmits)",
        description="Open the site's public pages (status page, confirmation) for a "
        "SUBMISSION_UNKNOWN application. It becomes SUBMITTED only if a confirmation tied "
        "to this job is shown; otherwise it stays unknown and is never retried.",
    )
    p.add_argument("application_id", metavar="APPLICATION_ID")
    _run_options(p, interactive=False)
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("status", help="list applications or show one in detail")
    p.add_argument("application_id", nargs="?", metavar="APPLICATION_ID")
    p.add_argument("--candidate", metavar="ID", help="only this candidate's applications")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("events", help="show an application's event history")
    p.add_argument("application_id", metavar="APPLICATION_ID")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("receipt", help="show the submission receipt of a confirmed application")
    p.add_argument("application_id", metavar="APPLICATION_ID")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_receipt)

    p = sub.add_parser("paths", help="show where profile, state and artifacts are stored")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_paths)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _check_run_options(parser, args)
    try:
        code: int = args.func(args)
    except (StoreError, DriverError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
