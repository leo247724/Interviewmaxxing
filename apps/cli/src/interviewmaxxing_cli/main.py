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

many jobs (preparation only):
  interviewmaxxing prepare-batch --inventory FILE   prepare Saved jobs, --workers at a time
  interviewmaxxing batch-report [BATCH_ID ...]      outcomes, questions, fill failures
  interviewmaxxing holds                            open questions, each with its answer line
  interviewmaxxing prepare-batch --retry BATCH_ID   run the held and failed ones again

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
    if hasattr(args, "browser") and not getattr(args, "retry", None):
        # A retry checks its flags once merged with the retried batch's run options.
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


RETRY_FLAGS: dict[str, str] = {
    "candidate": "candidate_id", "workers": "workers", "per_job_timeout": "per_job_timeout_s",
    "retry_retryable": "retry_retryable", "sync_closed": "sync_closed", "browser": "browser",
    "opencli_profile": "opencli_profile", "ai_routing": "ai_routing", "env_file": "env_file",
    "writer_model": "writer_model", "rag_connection_file": "rag_connection_file",
}
"""``prepare-batch`` flags (argparse dest -> ``BatchOptions`` name) that a retry takes from
the retried batch's recorded run options unless they are given again."""


def _given_retry_flags(argv: Sequence[str]) -> set[str]:
    """The ``RETRY_FLAGS`` options given on this command line: ``argv`` parsed again with
    a marker as their default, so a flag given with its default value still counts."""
    marker = object()
    parsed = build_parser(batch_defaults=dict.fromkeys(RETRY_FLAGS, marker)).parse_args(argv)
    return {name for dest, name in RETRY_FLAGS.items() if getattr(parsed, dest) is not marker}


def _batch_flag_values(args: argparse.Namespace, paths: LocalPaths) -> dict[str, Any]:
    """The ``RETRY_FLAGS`` options of this command line, defaults included."""
    return {
        "candidate_id": args.candidate or paths.candidate_id, "workers": args.workers,
        "per_job_timeout_s": args.per_job_timeout, "retry_retryable": args.retry_retryable,
        "sync_closed": args.sync_closed, "browser": args.browser,
        "opencli_profile": args.opencli_profile, "ai_routing": args.ai_routing,
        "env_file": Path(args.env_file) if args.env_file else None,
        "writer_model": args.writer_model,
        "rag_connection_file": Path(args.rag_connection_file) if args.rag_connection_file else None,
    }


def _cli_prefix(args: argparse.Namespace) -> list[str]:
    """How to call this CLI again for the same data: ``interviewmaxxing [--home DIR]``."""
    return [PROG, "--home", args.home] if args.home else [PROG]


def _problems(exc: Exception) -> str:
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        return "; ".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())
    return str(exc)


def _run_batch_command(args: argparse.Namespace, header: str, batch_dir: Path,
                       run: Callable[[Callable[[Any], None]], Awaitable[Any]]) -> Any:
    """Run a batch on a fresh loop with progress lines (stderr with --json, else stdout);
    Ctrl-C or SIGTERM stops the running jobs. Returns the summary, or None when
    interrupted."""
    from .batch import format_entry

    progress = sys.stderr if args.json else sys.stdout

    def show(entry: Any) -> None:
        print(format_entry(entry), file=progress, flush=True)

    async def batch() -> Any:
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            return await run(show)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)

    print(header, file=progress, flush=True)
    try:
        with asyncio.Runner() as runner:
            return runner.run(batch())
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(f"Interrupted. Finished jobs are in {batch_dir}; run the same --batch-id "
              "again to continue.", file=sys.stderr)
        return None


def _print_summary(args: argparse.Namespace, summary: Any, batch_dir: Path) -> None:
    from .batch import render_summary_markdown

    if args.json:
        print(_dump(summary))
    else:
        print()
        print(render_summary_markdown(summary), end="")
        print(f"batch directory: {batch_dir}")


def cmd_prepare_batch(args: argparse.Namespace) -> int:
    """Prepare every matching inventory row through ``apply`` subprocesses, or with
    ``--retry`` the held and failed applications of an earlier batch through ``resume``;
    see ``interviewmaxxing_cli.batch`` and ``interviewmaxxing_cli.retry``. Always headless;
    never submits."""
    if args.retry is not None:
        return _retry_batch(args)
    if args.outcomes is not None or args.include_explicit:
        print("error: --outcomes and --include-explicit apply to --retry", file=sys.stderr)
        return EXIT_USAGE
    from pydantic import ValidationError

    from .batch import BatchOptions, default_batch_id, read_inventory, run_batch

    paths = _paths(args)
    try:
        rows, invalid = read_inventory(Path(args.inventory), backends=_csv(args.backends),
                                       statuses=_csv(args.statuses) or set(), limit=args.limit)
        options = BatchOptions(
            paths=paths, batch_id=args.batch_id or default_batch_id(),
            max_prepared=args.max_prepared, include_existing=args.include_existing,
            **_batch_flag_values(args, paths),
        )
    except (ValidationError, OSError, ValueError) as exc:
        print(f"error: {_problems(exc)}", file=sys.stderr)
        return EXIT_USAGE
    if not rows:
        print(f"error: no inventory rows match (skipped {invalid} without a valid URL)",
              file=sys.stderr)
        return EXIT_USAGE
    summary = _run_batch_command(
        args, f"batch {options.batch_id}: {len(rows)} row(s), {options.workers} worker(s), "
              f"preparation only; ledger in {options.batch_dir}", options.batch_dir,
        lambda show: run_batch(options, rows, on_entry=show, skipped_invalid_url=invalid))
    if summary is None:
        return EXIT_INTERRUPTED
    _print_summary(args, summary, options.batch_dir)
    return EXIT_OK if sum(summary.totals.values()) else EXIT_ERROR


def _retry_batch(args: argparse.Namespace) -> int:
    """``prepare-batch --retry BATCH_ID``: see ``interviewmaxxing_cli.retry``."""
    from pydantic import ValidationError

    from .batch import read_summary
    from .retry import RETRY_OUTCOMES, default_retry_id, plan_retry, retry_options, run_retry

    if args.statuses != "resolved" or args.include_existing:
        print("error: --statuses and --include-existing apply to --inventory runs",
              file=sys.stderr)
        return EXIT_USAGE
    paths = _paths(args)
    try:
        original = read_summary(paths, args.retry)
        recorded = original.run_options if original is not None else None
        options = retry_options(
            paths, retry_of=args.retry, batch_id=args.batch_id or default_retry_id(args.retry),
            recorded=recorded, cli=_batch_flag_values(args, paths),
            explicit=getattr(args, "explicit", set()), max_prepared=args.max_prepared)
        plan = plan_retry(paths, args.retry, candidate_id=options.candidate_id,
                          outcomes=args.outcomes or RETRY_OUTCOMES,
                          include_explicit=args.include_explicit,
                          backends=_csv(args.backends), limit=args.limit)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (ValidationError, OSError, ValueError) as exc:
        print(f"error: {_problems(exc)}", file=sys.stderr)
        return EXIT_USAGE
    progress = sys.stderr if args.json else sys.stdout
    if recorded is None:
        print(f"note: batch {args.retry} recorded no run options (it predates them); the "
              "flags given here are used", file=progress)
    stats = plan.stats
    skipped = ", ".join(f"{reason} ({count})" for reason, count in stats.skipped.items())
    header = (f"batch {options.batch_id}: retry of {args.retry}, {stats.selected} of "
              f"{stats.considered} listing(s) selected (skipped: {skipped or 'none'}), "
              f"{options.workers} worker(s), preparation only; ledger in {options.batch_dir}")
    summary = _run_batch_command(args, header, options.batch_dir,
                                 lambda show: run_retry(options, plan, on_entry=show))
    if summary is None:
        return EXIT_INTERRUPTED
    _print_summary(args, summary, options.batch_dir)
    return EXIT_OK


def cmd_batch_report(args: argparse.Namespace) -> int:
    """Summarize prepare-batch ledgers; read-only. See
    ``interviewmaxxing_cli.batch.build_report``."""
    from .batch import build_report, render_report_markdown

    if args.batch_ids and args.since is not None:
        print("error: give batch ids or --since, not both", file=sys.stderr)
        return EXIT_USAGE
    paths = _paths(args)
    try:
        report = build_report(paths, args.batch_ids or None, top=args.top, since=args.since,
                              cli=_cli_prefix(args))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.json:
        print(_dump(report))
    else:
        print(render_report_markdown(report, top=args.top), end="")
    return EXIT_OK


def cmd_holds(args: argparse.Namespace) -> int:
    """Open holds of every NEEDS_INPUT application, grouped by question; read-only. See
    ``interviewmaxxing_cli.triage.build_holds``."""
    from .triage import build_holds, render_holds_markdown

    paths = _paths(args)
    report = build_holds(paths, args.candidate or paths.candidate_id, cli=_cli_prefix(args))
    if args.json:
        print(_dump(report))
    else:
        print(render_holds_markdown(report), end="")
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


def _outcomes(text: str) -> list[str]:
    from .retry import NEVER_RETRIED, RETRY_OUTCOMES

    values = list(dict.fromkeys(v.strip() for v in text.split(",") if v.strip()))
    if not values:
        raise argparse.ArgumentTypeError("give at least one outcome")
    for value in values:
        if value in (*NEVER_RETRIED, "already_recorded"):
            raise argparse.ArgumentTypeError(f"{value} applications are never retried")
        if value not in RETRY_OUTCOMES:
            raise argparse.ArgumentTypeError(
                f"unknown outcome {value!r}; choose from {', '.join(RETRY_OUTCOMES)}")
    return values


def _since(text: str) -> datetime:
    """A date (``YYYY-MM-DD``, midnight UTC) or an ISO date and time (UTC unless it names
    an offset)."""
    try:
        value = datetime.fromisoformat(text.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a date such as 2026-09-24 or 2026-09-24T09:00Z, got {text!r}") from None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def build_parser(*, batch_defaults: dict[str, Any] | None = None) -> argparse.ArgumentParser:
    """The command-line parser. ``batch_defaults`` replaces defaults of ``prepare-batch``
    options (used to tell which flags a command line gave, ``_given_retry_flags``)."""
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
        help="prepare many Saved jobs from an inventory file, or run a batch's held and "
             "failed applications again (headless; never submits)",
        description="Run `apply` for every matching row of a JSON inventory, at most "
        "--workers at a time, each worker with its own browser profile. Every application "
        "stops at its final review step or earlier (NEEDS_INPUT); nothing is submitted. "
        "Finished jobs are recorded in $IMX_HOME/batches/<batch id>/ledger.jsonl; running "
        "the same --batch-id again skips them and retries failures. Sign-in, CAPTCHA and "
        "custom controls stop as NEEDS_INPUT; finish them later with `resume APP --act`. "
        "A row's application is linked to its Saved pipeline card (pipeline_id); a job that "
        "no longer accepts applications moves its Saved card to Closed with a dated note. "
        "No card is created or moved to Applied. Summarize ledgers with `batch-report`. "
        "With --retry BATCH_ID instead of --inventory, the applications of that batch that "
        "are held or failed now are continued with `resume` as a new batch, with the "
        "original batch's worker count, timeout and runtime flags unless given again; "
        "prepared and closed applications are never run again.",
    )
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--inventory", metavar="FILE",
                        help="JSON list of Saved jobs (listing_id, pipeline_id, "
                             "source_application_url, backend, status, company, title)")
    source.add_argument("--retry", metavar="BATCH_ID",
                        help="run the held and failed applications of this batch again")
    p.add_argument("--outcomes", type=_outcomes, metavar="A,B",
                   help="with --retry: which applications, by where they stand now: "
                        "needs_input, failed_retryable, unknown (a run recorded no outcome), "
                        "error (no application recorded); default all four")
    p.add_argument("--include-explicit", action="store_true",
                   help="with --retry: also run applications whose open questions all need "
                        "your explicit answer (skipped by default)")
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
    p.add_argument("--batch-id", metavar="ID", help="name of the batch (default: UTC timestamp; "
                                                    "with --retry: BATCH_ID-retry-<UTC timestamp>); "
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
    if batch_defaults:
        p.set_defaults(**batch_defaults)

    p = sub.add_parser(
        "batch-report",
        help="summarize prepare-batch ledgers: outcomes, questions, fill failures, backends",
        description="Read batch ledgers ($IMX_HOME/batches/<batch id>/ledger.jsonl): the "
        "named ones, those with a line finished since --since, or all of them. Print totals "
        "by outcome and backend, a per-backend readiness table (prepared, needs input, "
        "failed, closed, no form, prepared rate, median duration, provider cost), the "
        "questions that held applications in categories and grouped by wording with the "
        "`answer ... --reuse global` line that answers each, fill failures grouped by "
        "detail, durations and pipeline card links. Writes no file; question wording is "
        "truncated to 80 characters, failure details are masked and CLI messages are never "
        "shown.",
    )
    p.add_argument("batch_ids", nargs="*", metavar="BATCH_ID",
                   help="batches to combine (default: every batch under $IMX_HOME/batches)")
    p.add_argument("--since", type=_since, metavar="DATE",
                   help="only batches with a line finished at or after DATE (UTC date or ISO "
                        "date and time)")
    p.add_argument("--top", type=_int_range(1, 100), default=10, metavar="N",
                   help="questions shown per category, and question and failure groups shown "
                        "(1..100, default 10; --json has every group)")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.set_defaults(func=cmd_batch_report)

    p = sub.add_parser(
        "holds",
        help="open questions of every held application, grouped so each is answered once",
        description="Group the open questions and actions of every NEEDS_INPUT application "
        "of the candidate by their wording, with counts, backends and the exact "
        "`interviewmaxxing answer APP --set FIELD=VALUE --reuse global` line that answers "
        "each for all of them (or `resume APP --act` for a browser action). Questions "
        "answered after their application stopped are not listed. Then run `prepare-batch "
        "--retry BATCH_ID`. Read-only; stored answer values are never shown.",
    )
    p.add_argument("--candidate", metavar="ID", help="candidate id (default: IMX_CANDIDATE_ID)")
    p.add_argument("--json", action="store_true", help="print the groups as JSON")
    p.set_defaults(func=cmd_holds)

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
    if getattr(args, "retry", None):
        args.explicit = _given_retry_flags(sys.argv[1:] if argv is None else argv)
    try:
        code: int = args.func(args)
    except (StoreError, DriverError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
