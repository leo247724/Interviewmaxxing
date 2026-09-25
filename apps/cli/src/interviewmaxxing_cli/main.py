"""``interviewmaxxing`` entry point.

``apply`` runs the whole supplied-URL flow through ``LocalApplicationRunner``: it
records the request, inspects the live form in a real browser, answers from the
verified profile, stops with a recorded NEEDS_INPUT result when a required answer
is missing, and stops at the final review step without submitting (prepare-only).

Submitting is a separate, explicit path (``docs/submission.md``): ``approve APP``
records the user's approval of the prepared packet, and ``submit APP --yes`` (only with
``IMX_ALLOW_SUBMISSION=1``) authorizes it and submits exactly the approved packet,
reporting SUBMITTED only when the site's confirmation tied to the job was observed.
``submit-approved`` does the same for many approved applications.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
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
    AnswerValue,
    Application,
    ApplicationState,
    ApplicationStore,
    ApplyOutcome,
    BooleanValue,
    ChoiceValue,
    ClaimUnavailable,
    FileValue,
    InvalidApplicationUrl,
    LocalPaths,
    MissingInput,
    MissingReason,
    MultiChoiceValue,
    Receipt,
    StoreError,
    SubmissionBlocked,
    TextValue,
    UserInput,
    UserInteraction,
    normalize_application_url,
)

from .answers import AnswerError, describe, user_input_for
from .interaction import TerminalInteraction
from .runner import (
    ALLOW_SUBMISSION_ENV,
    BUSY_MESSAGE,
    CLAIMED_MESSAGE,
    KEPT_DRAFT_MESSAGE,
    KEPT_DRAFT_REASON,
    NEEDS_INPUT_EVENT,
    NOT_AUTHORIZED_MESSAGE,
    LocalApplicationRunner,
    NoninteractiveInteraction,
    create_runner,
    create_submission_runner,
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
preparation-only application keeps submission disabled. Nothing is submitted until
you approve a prepared application and run `submit APP --yes` with
IMX_ALLOW_SUBMISSION=1; it then submits exactly what you approved.
"""
EPILOG = """\
typical flow:
  interviewmaxxing apply URL                 run; stops with questions if any
  interviewmaxxing answer APP --set F=V ...  answer the recorded questions
  interviewmaxxing resume APP                continue from the site
  interviewmaxxing approve APP               approve the prepared answers you reviewed
  IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit APP --yes
                                             submit exactly what you approved
  interviewmaxxing reconcile APP             re-check an uncertain submission

many jobs (preparation only):
  interviewmaxxing prepare-batch --inventory FILE   prepare Saved jobs, --workers at a time
  interviewmaxxing batch-report [BATCH_ID ...]      outcomes, questions, fill failures
  interviewmaxxing holds                            open questions, each with its answer line
  interviewmaxxing holds --sheet FILE               ... or as an answer sheet to fill in once
  interviewmaxxing answer --sheet FILE              apply the filled-in sheet everywhere
  interviewmaxxing prepare-batch --retry BATCH_ID   run the held and failed ones again

local data (never in source control):
  IMX_HOME           root directory (default ~/.interviewmaxxing)
  IMX_PROFILE_DIR    candidate profile and resume     ($IMX_HOME/profile)
  IMX_STATE_DB       application state database        ($IMX_HOME/state/imx.sqlite3)
  IMX_ARTIFACTS_DIR  confirmation evidence             ($IMX_HOME/artifacts)
  IMX_BROWSER_DIR    persistent browser profile        ($IMX_HOME/browser)
  IMX_CANDIDATE_ID   candidate used by apply           (default)
  IMX_ALLOW_SUBMISSION  must be 1 for submit / submit-approved (never read by apply)

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


KEPT_DRAFT_STEP = ("submit it in the browser yourself (the site keeps a draft of it; preparing "
                   "it again reopens that draft)")
"""The next step after a submission run stopped at a kept draft (``runner.KEPT_DRAFT_MESSAGE``);
``resume`` would prepare it again into the same draft."""


def _next_steps(outcome: ApplyOutcome) -> list[str]:
    app = outcome.application_id
    state = outcome.state
    if state is S.NEEDS_INPUT and outcome.message.startswith(KEPT_DRAFT_MESSAGE):
        return [KEPT_DRAFT_STEP]
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
    without submitting (state NEEDS_INPUT, nothing asked, and its ``preparation.ready``
    right behind the current stop, ``triage.prepared_stop``: a later run's stop, such as
    a submission run that found the form changed or a kept draft, is not a
    preparation). Empty for every other application."""
    from .triage import prepared_stop

    if state is not S.NEEDS_INPUT or waiting or not prepared_stop(events):
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


def _kept_draft_stop(state: ApplicationState, events: Sequence[Any]) -> bool:
    """True when the application's current stop is a submission run that found the site's
    kept draft at a later page and could not go back to the approved pages before it
    (``runner.KEPT_DRAFT_REASON``)."""
    if state is not S.NEEDS_INPUT:
        return False
    stop = next((e for e in reversed(events) if getattr(e, "event", None) == NEEDS_INPUT_EVENT),
                None)
    return stop is not None and (getattr(stop, "metadata", {}) or {}).get("reason") == KEPT_DRAFT_REASON


def _review_steps(application_id: str, artifacts_dir: Path) -> list[str]:
    return [f"review the filled form evidence under {artifacts_dir / application_id}/",
            f"{PROG} events {application_id}   (preparation.ready records the final step)",
            f"{PROG} resume {application_id}   (re-prepares from the site; submission stays disabled)",
            f"{PROG} approve {application_id}   (approve these answers; nothing is submitted yet)"]


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
                          rag_connection_file=Path(args.rag_connection_file) if args.rag_connection_file else None,
                          writer_effort=args.writer_effort)


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


# --- approval and submission ------------------------------------------------------


SUBMISSION_DISABLED = f"""\
Submission is disabled; nothing was submitted.
To submit applications you prepared, reviewed and approved (`{PROG} approve APP`),
enable submission for this command only:
  {ALLOW_SUBMISSION_ENV}=1 {PROG} submit APP --yes
Each application is then submitted exactly as approved; a form that changed since is
not submitted. See docs/submission.md."""


def _approver() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # no login name in this environment
        return "cli"


def _value_text(value: AnswerValue) -> str:
    if isinstance(value, TextValue):
        text = " ".join(value.text.split())
    elif isinstance(value, ChoiceValue):
        text = value.label
    elif isinstance(value, MultiChoiceValue):
        text = "; ".join(c.label for c in value.choices)
    elif isinstance(value, BooleanValue):
        text = "yes" if value.checked else "no"
    elif isinstance(value, FileValue):
        text = f"file {value.artifact.filename}"
    else:  # pragma: no cover - the union is exhaustive
        text = str(value)
    return text if len(text) <= 100 else text[:99] + "…"


def _submission_gate(args: argparse.Namespace, command: str) -> int | None:
    """EXIT_BLOCKED (with instructions) unless IMX_ALLOW_SUBMISSION=1 and --yes.
    ``command``: the command line to show, without ``--yes``."""
    if os.environ.get(ALLOW_SUBMISSION_ENV) != "1":
        print(SUBMISSION_DISABLED.replace(f"{PROG} submit APP --yes", f"{PROG} {command} --yes"),
              file=sys.stderr)
        return EXIT_BLOCKED
    if not args.yes:
        print(f"Refusing to submit without --yes; nothing was submitted. Add --yes to confirm "
              f"that `{PROG} {command}` may submit what you approved.", file=sys.stderr)
        return EXIT_BLOCKED
    return None


def cmd_approve(args: argparse.Namespace) -> int:
    paths = _paths(args)
    store = _open_existing(paths)
    if store is None:
        print("No state database.", file=sys.stderr)
        return EXIT_ERROR
    with store:
        app = store.get_application(args.application_id)
        packet_id = args.packet or store.prepared_packet(app.id)
        if packet_id is None:
            print(f"{app.id} is {app.state.value} and is not stopped at a completed preparation, "
                  f"so there is nothing to approve. Prepare it (`{PROG} resume {app.id}`) and "
                  "review it first.", file=sys.stderr)
            return EXIT_BLOCKED
        try:
            claim = store.claim(app.id, _owner())
        except ClaimUnavailable:
            print("Another run is working on this application; try again shortly.",
                  file=sys.stderr)
            return EXIT_BLOCKED
        try:
            approval = store.approve_submission(claim, packet_id=packet_id, approver=_approver())
        except SubmissionBlocked as exc:
            print(f"Not approved: {exc}.", file=sys.stderr)
            return EXIT_BLOCKED
        finally:
            store.release(claim)
        packets = [store.get_packet(step.packet_id) for step in approval.steps]
    if args.json:
        from .redaction import public_value

        # The approved step's URL can carry a per-session draft token: page address only.
        print(json.dumps(public_value(approval.model_dump(mode="json")), indent=2))
        return EXIT_OK
    answers = sum(len(p.answers) for p in packets)
    print(f"approved:    {approval.application_id}")
    print(f"packet:      {approval.packet_id} ({answers} answer(s) on {len(packets)} step(s))")
    print(f"approver:    {approval.approver} at {_fmt_time(approval.approved_at)}")
    for packet in packets:
        for answer in packet.answers:
            print(f"  step {packet.form_step + 1}  {answer.field_id}: {_value_text(answer.value)} "
                  f"({answer.provenance.source.value.lower()})")
    print("\nNothing was submitted. Approving records your review; submission stays disabled.")
    print(f"next:        {ALLOW_SUBMISSION_ENV}=1 {PROG} submit {approval.application_id} --yes")
    return EXIT_OK


def _submission_runner(args: argparse.Namespace) -> LocalApplicationRunner:
    kwargs: dict[str, Any] = {}
    if args.ai_routing or args.browser != "playwright":
        kwargs["dynamic_options"] = _dynamic_options(args)
    return create_submission_runner(
        _paths(args), headless=args.headless,
        interaction=NoninteractiveInteraction(allow_browser_action=args.act), **kwargs)


def _refused(app: Application, message: str, *, as_json: bool, code: int = EXIT_BLOCKED) -> int:
    if as_json:
        print(_dump(ApplyOutcome(application_id=app.id, state=app.state, message=message)))
    else:
        print(message, file=sys.stderr)
    return code


def cmd_submit(args: argparse.Namespace) -> int:
    gate = _submission_gate(args, f"submit {args.application_id}")
    if gate is not None:
        return gate
    paths = _paths(args)
    store = _open_existing(paths)
    if store is None:
        print("No state database.", file=sys.stderr)
        return EXIT_ERROR
    with store:
        app = store.get_application(args.application_id)
        if app.state in (S.SUBMITTED, S.SUBMITTING, S.SUBMISSION_UNKNOWN):
            return _refused(app, f"Not submitted: {app.id} is {_state_label(app)}; it is never "
                                 "submitted again.", as_json=args.json,
                            code=EXIT_UNCERTAIN if app.state is S.SUBMISSION_UNKNOWN else EXIT_BLOCKED)
        if store.approved_packet(app.id) is None:
            return _refused(app, f"Not submitted: {app.id} has no valid approval. Prepare it, "
                                 f"review it and approve it (`{PROG} approve {app.id}`) first.",
                            as_json=args.json)
        try:
            claim = store.claim(app.id, _owner())
        except ClaimUnavailable:
            return _refused(app, "Not submitted: another run is working on this application; "
                                 "try again shortly.", as_json=args.json)
        try:
            store.authorize_submission(claim)
        except SubmissionBlocked as exc:
            return _refused(app, f"Not submitted: {exc}.", as_json=args.json)
        finally:
            store.release(claim)
    runner = _submission_runner(args)
    try:
        outcome = _run(lambda: runner.submit(app.id))
    except (KeyboardInterrupt, asyncio.CancelledError):
        return _interrupted(paths, app.id)
    _print_outcome(outcome, as_json=args.json)
    if outcome.message.startswith((NOT_AUTHORIZED_MESSAGE, BUSY_MESSAGE, CLAIMED_MESSAGE)):
        return EXIT_BLOCKED  # nothing was run
    return _exit_code(outcome)


def cmd_submit_approved(args: argparse.Namespace) -> int:
    """Submit every approved application of one prepare batch (or all of them) through
    ``submit`` subprocesses; see ``interviewmaxxing_cli.batch.run_submissions``."""
    from pydantic import ValidationError

    from .batch import (
        SubmissionEntry,
        SubmitBatchOptions,
        approved_targets,
        default_submission_batch_id,
        format_submission,
        render_submissions_markdown,
        run_submissions,
    )

    scope = f"--batch {args.batch}" if args.batch else "--all-approved"
    gate = _submission_gate(args, f"submit-approved {scope} --slots {args.slots}")
    if gate is not None:
        return gate
    paths = _paths(args)
    candidate_id = args.candidate or paths.candidate_id
    try:
        targets = approved_targets(paths, candidate_id, source_batch=args.batch)
        options = SubmitBatchOptions(
            paths=paths, candidate_id=candidate_id,
            batch_id=args.batch_id or args.batch or default_submission_batch_id(),
            workers=args.slots, per_job_timeout_s=args.per_job_timeout,
            browser=args.browser, opencli_profile=args.opencli_profile,
            ai_routing=args.ai_routing, env_file=Path(args.env_file) if args.env_file else None,
            writer_model=args.writer_model,
            rag_connection_file=Path(args.rag_connection_file) if args.rag_connection_file else None,
        )
    except ValidationError as exc:
        problems = "; ".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())
        print(f"error: {problems}", file=sys.stderr)
        return EXIT_USAGE
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    progress = sys.stderr if args.json else sys.stdout
    if not targets:
        scope = f"batch {args.batch}" if args.batch else "the store"
        print(f"No approved application to submit in {scope}; nothing was submitted.",
              file=progress)
        if args.json:
            print("null")
        return EXIT_OK

    def show(entry: SubmissionEntry) -> None:
        print(format_submission(entry), file=progress, flush=True)

    async def submissions() -> Any:
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            return await run_submissions(options, targets, source_batch=args.batch, on_entry=show)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)

    print(f"submitting {len(targets)} approved application(s) with {options.workers} slot(s); "
          f"ledger in {options.batch_dir}", file=progress, flush=True)
    try:
        with asyncio.Runner() as runner:
            summary = runner.run(submissions())
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(f"Interrupted. Finished submissions are in {options.batch_dir}; an interrupted "
              "submit is recorded as uncertain and never repeated.", file=sys.stderr)
        return EXIT_INTERRUPTED
    if args.json:
        print(_dump(summary))
    else:
        print()
        print(render_submissions_markdown(summary), end="")
    if summary.totals.get("uncertain"):
        return EXIT_UNCERTAIN
    if any(summary.totals.get(k) for k in ("blocked", "needs_input", "error")):
        return EXIT_INCOMPLETE
    return EXIT_OK


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


def _answer_sheet(args: argparse.Namespace) -> int:
    """``answer --sheet FILE``: see ``interviewmaxxing_cli.sheet.apply_sheet``. Prints
    counts and the wordings not applied, never an answer, then the retry line."""
    from .sheet import apply_sheet, newest_batch_id, read_sheet, render_result, retry_line

    if args.application_id or args.set or args.answers:
        print("error: --sheet takes no APPLICATION_ID, --set or --answers (the sheet names "
              "them)", file=sys.stderr)
        return EXIT_USAGE
    paths = _paths(args)
    path = Path(args.sheet)
    try:
        sheet = read_sheet(path)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        result = apply_sheet(paths, sheet, owner=_owner())
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    for line in render_result(result, path):
        print(line)
    batch_id = args.batch_id or newest_batch_id(paths)
    if batch_id is not None:
        print(f"next: {retry_line(_cli_prefix(args), batch_id)}")
    else:
        print(f"next: {PROG} resume APP (no batch under {paths.home / 'batches'} to retry)")
    return EXIT_OK


def cmd_answer(args: argparse.Namespace) -> int:
    if args.sheet:
        return _answer_sheet(args)
    if not args.application_id:
        print("error: give APPLICATION_ID, or --sheet FILE", file=sys.stderr)
        return EXIT_USAGE
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
            print(f"No applications yet (no state database; `{PROG} paths` shows where it "
                  "is kept).")
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
            from .redaction import public_value

            # Form step URLs carry per-session draft tokens: page addresses only. The job's
            # and requests' application URLs are the person's own and stay as given.
            approval = store.submission_approval(app.id)
            packet = store.latest_packet(app.id)
            data = json.loads(_dump({"application": app, "job": job,
                                     "requests": store.list_requests(app.id)}))
            data |= public_value({
                "attempts": [a.model_dump(mode="json") for a in attempts],
                "pending_inputs": [m.model_dump(mode="json") for m in waiting],
                "packet": packet.model_dump(mode="json") if packet else None,
                "approval": approval.model_dump(mode="json") if approval else None})
            print(json.dumps({k: data[k] for k in ("application", "job", "attempts",
                                                   "pending_inputs", "packet", "requests",
                                                   "approval")}, indent=2))
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
        kept_draft = _kept_draft_stop(app.state, events)
        if kept_draft:
            print("kept draft:   the site resumed a draft it kept at a later page, so the "
                  "submission run could not check the approved pages before it; nothing was "
                  "submitted and the approval was withdrawn")
        approval = store.submission_approval(app.id)
        if approval is not None and app.state not in (S.SUBMITTED, S.SUBMITTING,
                                                      S.SUBMISSION_UNKNOWN):
            print(f"approved:     packet {approval.packet_id} by {approval.approver} at "
                  f"{_fmt_time(approval.approved_at)}; nothing is submitted until "
                  f"`{ALLOW_SUBMISSION_ENV}=1 {PROG} submit {app.id} --yes`")
        print(f"events:       {len(events)} ({PROG} events {app.id})")
        steps = (_review_steps(app.id, paths.artifacts_dir) if prepared
                 else [KEPT_DRAFT_STEP] if kept_draft
                 else _next_steps(ApplyOutcome(application_id=app.id, state=app.state,
                                               missing_inputs=waiting)))
        for step in steps:
            print(f"next:         {step}")
    return EXIT_OK


def cmd_events(args: argparse.Namespace) -> int:
    """The event history, with metadata as ``redaction.public_metadata`` prints it: form
    URLs as page addresses, and questions' prompts and candidates, lookup suggestions and
    chosen labels, the site's rejection messages and routing traces only with
    ``--verbose``."""
    from .redaction import public_metadata

    store = _open_existing(_paths(args))
    if store is None:
        print("No state database.", file=sys.stderr)
        return EXIT_ERROR
    with store:
        store.get_application(args.application_id)
        events = [e.model_copy(update={"metadata": public_metadata(
            e.event, e.metadata, verbose=args.verbose)})
            for e in store.list_events(args.application_id)]
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
    "writer_effort": "writer_effort",
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
        "writer_effort": args.writer_effort,
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
    if args.outcomes is not None or args.include_explicit or args.rerun_all or args.user_actions:
        print("error: --outcomes, --include-explicit, --all and --user-actions apply to --retry",
              file=sys.stderr)
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
                          include_explicit=args.include_explicit, rerun_all=args.rerun_all,
                          user_actions=args.user_actions, backends=_csv(args.backends),
                          limit=args.limit)
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
    if args.sheet:
        from .sheet import build_sheet, write_sheet
        from .triage import command_line

        sheet = build_sheet(paths, args.candidate or paths.candidate_id, cli=_cli_prefix(args))
        try:
            write_sheet(sheet, Path(args.sheet), force=args.force)
        except (OSError, FileExistsError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        proposed = sum(1 for q in sheet.questions if q.proposal is not None)
        print(f"wrote {len(sheet.questions)} question(s) ({proposed} with an unconfirmed "
              f"proposal) and {len(sheet.actions)} browser action(s) for {sheet.held} held "
              f"application(s) to {args.sheet} (owner-only). Fill in `answer`, then: "
              f"{command_line(_cli_prefix(args), 'answer', '--sheet', args.sheet)}")
        return EXIT_OK
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
    p.add_argument("--writer-effort", choices=["low", "medium", "high"],
                   help="reasoning effort for cover letters and narrative answers (default high); "
                        "reviews and short factual decisions stay low")


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
        "failed, or are held with a question answered since they stopped (--all: every "
        "held one; --user-actions: also those held only on browser actions), are "
        "continued with `resume` as a new batch, with the original batch's worker count, "
        "timeout and runtime flags unless given again; prepared and closed applications are "
        "never run again.",
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
    p.add_argument("--all", dest="rerun_all", action="store_true",
                   help="with --retry: also run held applications with nothing answered "
                        "since they stopped (after a fix); by default only those with an "
                        "answered question run again, with every failed or unknown one")
    p.add_argument("--include-explicit", action="store_true",
                   help="with --retry: also run applications whose open questions all need "
                        "your explicit answer (skipped by default)")
    p.add_argument("--user-actions", action="store_true",
                   help="with --retry: also run held applications whose open holds are all "
                        "browser actions (sign-in, CAPTCHA, custom controls, files), which "
                        "nothing can answer; skipped by default as 'browser actions only', "
                        "because a headless retry usually meets them again (`resume APP "
                        "--act` clears one in a visible browser)")
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
    p.add_argument("--sheet", metavar="FILE",
                   help="instead of printing the groups, write an answer sheet (owner-only "
                        "JSON): one entry per distinct question with its options, its field "
                        "id on every application, `reuse` and `answer: null`, plus browser "
                        "actions; fill it in and run `answer --sheet FILE`")
    p.add_argument("--force", action="store_true",
                   help="with --sheet: replace an existing sheet file")
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
        "says otherwise. Then run `resume`. With --sheet FILE (no APPLICATION_ID), apply "
        "every filled-in entry of an answer sheet written by `holds --sheet` to each of its "
        "applications with the entry's `reuse`; an entry that does not fit a question as "
        "recorded is reported by its wording and skipped, a hold answered since its stop is "
        "left alone, and the output never shows an answer. Then run `prepare-batch --retry`.",
    )
    p.add_argument("application_id", nargs="?", metavar="APPLICATION_ID")
    p.add_argument("--set", action="append", metavar="FIELD=VALUE", help="one answer (repeatable)")
    p.add_argument("--answers", metavar="FILE", help="JSON object mapping field id to value")
    p.add_argument("--reuse", choices=["application", "job", "global"], default="application",
                   help="application: this application only (default); job: this job; "
                        "global: any job")
    p.add_argument("--sheet", metavar="FILE",
                   help="apply the filled-in answer sheet (from `holds --sheet FILE`) to every "
                        "application it names")
    p.add_argument("--batch-id", metavar="ID",
                   help="with --sheet: the batch to name in the `prepare-batch --retry` line "
                        "printed at the end (default: the newest batch under $IMX_HOME/batches)")
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

    p = sub.add_parser(
        "approve",
        help="approve the prepared answers of an application you reviewed (submits nothing)",
        description="Record your approval of the packet a prepare-only run stopped with at the "
        "final review step (default: the prepared packet). Only an application stopped right "
        "after its preparation can be approved. Approving submits nothing and keeps submission "
        "disabled; `submit APP --yes` later submits exactly what you approved. A new "
        "preparation or a form that changed withdraws the approval.",
    )
    p.add_argument("application_id", metavar="APPLICATION_ID")
    p.add_argument("--packet", metavar="PACKET_ID",
                   help="the prepared packet to approve (default: the latest prepared packet)")
    p.add_argument("--json", action="store_true", help="print the approval as JSON")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser(
        "submit",
        help="submit exactly what you approved (needs IMX_ALLOW_SUBMISSION=1 and --yes)",
        description="Submit an approved application: authorize its approved packet, open and "
        "inspect the site again, fill every step from the approved packets (nothing is "
        "re-resolved or regenerated) and submit once. A form that no longer matches the "
        "approval (new, missing or changed question, options, required flag, a value that "
        "does not read back) stops as NEEDS_INPUT before submitting and withdraws the "
        "approval. Refuses (exit 4) unless IMX_ALLOW_SUBMISSION=1, --yes and a valid approval. "
        "Exit status: 0 submitted, 3 not submitted, 4 blocked, 5 uncertain.",
    )
    p.add_argument("application_id", metavar="APPLICATION_ID")
    p.add_argument("--yes", action="store_true",
                   help="confirm that this application may be submitted as approved")
    p.add_argument("--act", action="store_true",
                   help="you will sign in, solve a CAPTCHA or set custom controls in the visible "
                        "browser window when asked")
    _run_options(p, interactive=False)
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser(
        "submit-approved",
        help="submit every approved application of a batch, or all approved ones",
        description="Run `submit APP --yes` for every approved application that nothing was "
        "submitted for yet: those of one prepare batch (--batch) or every approved one "
        "(--all-approved), at most --slots at a time, each slot with its own browser profile. "
        "Each result is appended to $IMX_HOME/batches/<id>/ledger.jsonl (outcome submitted, "
        "uncertain, blocked, needs_input or error, with the receipt id); the id is --batch-id, "
        "else the --batch id, else approved-<UTC time>. Needs IMX_ALLOW_SUBMISSION=1 and --yes. "
        "Exit status: 0 all submitted (or nothing to submit), 3 some not submitted, "
        "5 some uncertain, 4 refused.",
    )
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--batch", metavar="BATCH_ID",
                       help="the prepare batch whose approved applications to submit")
    scope.add_argument("--all-approved", action="store_true",
                       help="every approved application of the candidate")
    p.add_argument("--slots", type=_int_range(1, 8), default=1, metavar="N",
                   help="concurrent submissions, each in its own browser profile (1..8)")
    p.add_argument("--yes", action="store_true",
                   help="confirm that the approved applications may be submitted as approved")
    p.add_argument("--batch-id", metavar="ID",
                   help="ledger to record the submissions in (default: the --batch id, else "
                        "approved-<UTC time>); reuse it to continue")
    p.add_argument("--per-job-timeout", type=float, default=900.0, metavar="SECONDS",
                   help="stop a submission that runs longer than this (default 900); an "
                        "interrupted submit is recorded as uncertain")
    p.add_argument("--candidate", metavar="ID", help="candidate id (default: IMX_CANDIDATE_ID)")
    _dynamic_flags(p)
    p.add_argument("--json", action="store_true", help="print the summary as JSON")
    p.set_defaults(func=cmd_submit_approved)

    p = sub.add_parser(
        "status", help="list applications or show one in detail",
        description="List the applications, or show one in detail: its state, attempts, "
        "the questions it waits for and what to run next. This is your own working view: "
        "it prints what you need to answer each question (its prompt, answer candidates "
        "and options, a lookup's suggestions), which `events` hides without --verbose. "
        "With --json it adds the pending questions as recorded and the latest packet with "
        "its answers; form URLs are reduced to page addresses and nothing else is hidden, "
        "so do not keep or share that output (share `events` instead).")
    p.add_argument("application_id", nargs="?", metavar="APPLICATION_ID")
    p.add_argument("--candidate", metavar="ID", help="only this candidate's applications")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser(
        "events", help="show an application's event history",
        description="Print the application's events. Form URLs are shown as page addresses "
        "(scheme, host and path: sites put draft tokens in the rest). The prompts, answer "
        "candidates and lookup suggestions of recorded questions, chosen lookup labels, the "
        "site's rejection messages and routing traces can quote your own values and are "
        "shown only with --verbose.")
    p.add_argument("application_id", metavar="APPLICATION_ID")
    p.add_argument("--json", action="store_true")
    p.add_argument("--verbose", action="store_true",
                   help="also show prompts, candidates, suggestions, chosen labels, rejection "
                        "messages and traces")
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
