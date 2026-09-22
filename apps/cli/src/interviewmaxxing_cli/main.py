"""``interviewmaxxing`` entry point.

``apply`` is a skeleton until task I1 wires in the candidate, packet and browser
services: it records the request and checks for an existing application, then stops
with exit status 3 (INCOMPLETE). It never opens a form, submits, or reports success.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from interviewmaxxing_core import (
    Application,
    ApplicationState,
    ApplicationStore,
    InvalidApplicationUrl,
    LocalPaths,
    ReconciliationMethod,
    RequestDisposition,
    StoreError,
    SubmissionOutcome,
    SubmissionReconciliation,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INCOMPLETE = 3
"""The requested application was not submitted (not implemented, or needs input)."""
EXIT_BLOCKED = 4
"""Nothing was done because the stored state forbids it (e.g. already submitted,
submission unknown) or no confirmation exists."""

PROG = "interviewmaxxing"
DESCRIPTION = """\
Submit job applications you choose, using your verified profile and resume.

Your request to apply authorizes submission; you are only asked for missing
required information or for actions such as sign-in or CAPTCHA. An application
is reported as submitted only after the site's confirmation is observed.
"""
EPILOG = """\
local data (never in source control):
  IMX_HOME           root directory (default ~/.interviewmaxxing)
  IMX_PROFILE_DIR    candidate profile and resume     ($IMX_HOME/profile)
  IMX_STATE_DB       application state database        ($IMX_HOME/state/imx.sqlite3)
  IMX_ARTIFACTS_DIR  confirmation evidence             ($IMX_HOME/artifacts)
  IMX_BROWSER_DIR    persistent browser profile        ($IMX_HOME/browser)
  IMX_CANDIDATE_ID   candidate used by apply           (default)

exit status: 0 ok, 1 error, 2 usage, 3 not submitted/incomplete, 4 blocked by state
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
    if app.state is ApplicationState.SUBMITTING and (
        app.claim_expires_at is None or app.claim_expires_at <= datetime.now(UTC)
    ):
        label += " (interrupted; will become SUBMISSION_UNKNOWN)"
    return label


# --- commands --------------------------------------------------------------------


def cmd_apply(args: argparse.Namespace) -> int:
    paths = _paths(args)
    candidate_id = args.candidate or paths.candidate_id
    paths.ensure()
    with ApplicationStore.open(paths.state_db) as store:
        try:
            result = store.record_request(candidate_id, args.url)
        except InvalidApplicationUrl as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        app = result.application
        job = result.job
        print(f"application: {app.id}")
        print(f"job:         {job.id}  ({job.normalized_url})")
        print(f"state:       {_state_label(app)}")
        print(f"request:     {result.disposition.value}")
        blocked = {
            RequestDisposition.ALREADY_SUBMITTED:
                f"Already submitted at {_fmt_time(app.submitted_at)}. "
                f"See: {PROG} receipt {app.id}",
            RequestDisposition.SUBMISSION_IN_PROGRESS:
                "A submission for this job is in progress; it will not be repeated.",
            RequestDisposition.SUBMISSION_UNKNOWN:
                "A previous submission may have reached the employer. It will not be "
                f"retried until reconciled: {PROG} reconcile {app.id} --help",
            RequestDisposition.CLOSED:
                f"The existing application is {app.state.value}"
                + (f": {app.failure_reason}" if app.failure_reason else "."),
        }
        if result.disposition in blocked:
            print(blocked[result.disposition])
            return EXIT_BLOCKED
    sys.stdout.flush()
    print(
        "INCOMPLETE: application execution is not implemented yet (task I1). "
        "The request was recorded; no form was opened and nothing was submitted.",
        file=sys.stderr,
    )
    return EXIT_INCOMPLETE


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
        packet = store.latest_packet(app.id)
        attempts = store.list_attempts(app.id)
        events = store.list_events(app.id)
        if args.json:
            print(_dump({"application": app, "job": job, "attempts": attempts,
                         "packet": packet if packet else None,
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
        if packet and packet.missing_inputs:
            print("missing input:")
            for item in packet.missing_inputs:
                print(f"  - {item.label or item.field_id}: {item.prompt} [{item.reason.value}]")
        print(f"events:       {len(events)} ({PROG} events {app.id})")
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
        print(f"SUBMITTED  {receipt.application_id}")
        print(f"job:           {' - '.join(x for x in (receipt.company, receipt.title) if x) or receipt.job_id}")
        print(f"url:           {receipt.application_url}")
        print(f"submitted at:  {_fmt_time(receipt.submitted_at)}")
        print(f"confirmed at:  {_fmt_time(receipt.confirmed_at)}")
        print(f"reference:     {receipt.confirmation_reference or '(none shown by site)'}")
        if receipt.reconciliation_method:
            print(f"established:   {receipt.reconciliation_method.value}")
        for signal in receipt.signals:
            print(f"signal:        {signal}")
        for ev in receipt.evidence:
            print(f"evidence:      {ev.kind.value} {ev.path or ev.uri or ev.description}")
    return EXIT_OK


def cmd_reconcile(args: argparse.Namespace) -> int:
    store = _open_existing(_paths(args))
    if store is None:
        print("No state database.", file=sys.stderr)
        return EXIT_ERROR
    with store:
        store.recover_interrupted_submissions()
        app = store.get_application(args.application_id)
        if app.state is not ApplicationState.SUBMISSION_UNKNOWN:
            print(f"{app.id} is {_state_label(app)}; only SUBMISSION_UNKNOWN can be reconciled.",
                  file=sys.stderr)
            return EXIT_BLOCKED
        claim = store.claim(app.id, _owner())
        try:
            updated = store.reconcile_submission(
                claim,
                SubmissionReconciliation(
                    outcome=SubmissionOutcome.ACCEPTED if args.accepted
                    else SubmissionOutcome.NOT_SUBMITTED,
                    method=ReconciliationMethod(args.method),
                    detail=args.detail,
                    confirmation_reference=args.reference,
                ),
            )
        finally:
            store.release(claim)
        print(f"{updated.id} is now {updated.state.value}")
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
        help="apply to the job at an application URL (incomplete: records the request only)",
        description="Apply to the job at APPLICATION_URL. Currently a skeleton: it records "
        "the request and checks for an existing application for the same job, then exits "
        "with status 3. Form filling and submission arrive with task I1.",
    )
    p.add_argument("url", metavar="APPLICATION_URL")
    p.add_argument("--candidate", metavar="ID", help="candidate id (default: IMX_CANDIDATE_ID)")
    p.set_defaults(func=cmd_apply)

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

    p = sub.add_parser(
        "reconcile",
        help="resolve a SUBMISSION_UNKNOWN application after checking with the employer",
        description="Record what you established about an uncertain submission, e.g. from "
        "a confirmation email or the ATS candidate portal. --accepted marks it SUBMITTED; "
        "--not-submitted marks it FAILED_RETRYABLE so it may be applied to again.",
    )
    p.add_argument("application_id", metavar="APPLICATION_ID")
    outcome = p.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--accepted", action="store_true", help="the employer received it")
    outcome.add_argument("--not-submitted", action="store_true",
                         help="the employer definitely did not receive it")
    p.add_argument("--detail", required=True, help="how you established this")
    p.add_argument("--reference", help="confirmation reference, if any")
    p.add_argument(
        "--method",
        default=ReconciliationMethod.USER_CONFIRMED.value,
        choices=[m.value for m in ReconciliationMethod],
    )
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("paths", help="show where profile, state and artifacts are stored")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_paths)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code: int = args.func(args)
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
