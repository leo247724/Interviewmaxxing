"""Subprocess worker for concurrency and crash tests (run with the venv's python).

usage: _store_worker.py COMMAND DB ARGS... START_AT
Every command waits until the epoch time START_AT so workers collide.
"""

from __future__ import annotations

import json
import sys
import time

from interviewmaxxing_core import (
    ApplicationState,
    ApplicationStore,
    ClaimUnavailable,
    InvalidTransition,
    SubmissionBlocked,
    SubmissionObservation,
    SubmissionOutcome,
)


def _wait(start_at: str) -> None:
    delay = float(start_at) - time.time()
    if delay > 0:
        time.sleep(delay)


def main(argv: list[str]) -> int:
    command, db, *rest = argv
    if command == "request":
        candidate, url, start_at = rest
        _wait(start_at)
        with ApplicationStore.open(db) as store:
            result = store.record_request(candidate, url)
        print(json.dumps({"app": result.application.id, "disposition": result.disposition.value}))
        return 0

    if command == "claim":
        app_id, owner, start_at = rest
        _wait(start_at)
        with ApplicationStore.open(db) as store:
            try:
                store.claim(app_id, owner)
            except ClaimUnavailable:
                print("UNAVAILABLE")
            else:
                print("CLAIMED")
        return 0

    if command == "submit":
        app_id, owner, start_at = rest
        _wait(start_at)
        with ApplicationStore.open(db) as store:
            for _ in range(50):
                try:
                    claim = store.claim(app_id, owner)
                except ClaimUnavailable:
                    time.sleep(0.005)
                    continue
                try:
                    attempt = store.begin_submission(claim)
                except (SubmissionBlocked, InvalidTransition):
                    store.release(claim)
                    print("BLOCKED")
                    return 0
                time.sleep(0.02)  # the "click"
                store.record_submission_outcome(
                    claim,
                    attempt.id,
                    SubmissionObservation(
                        outcome=SubmissionOutcome.ACCEPTED, signals=[f"confirmed for {owner}"]
                    ),
                )
                print(f"SUBMITTED {attempt.id}")
                return 0
        print("GAVE_UP")
        return 0

    if command == "crash-during-submit":
        app_id, owner, start_at = rest
        _wait(start_at)
        store = ApplicationStore.open(db)
        claim = store.claim(app_id, owner)
        for state in (ApplicationState.INSPECTING, ApplicationState.PACKET_READY,
                      ApplicationState.FILLING):
            store.transition(claim, state)
        attempt = store.begin_submission(claim)
        print(f"SUBMITTING {attempt.id}", flush=True)
        time.sleep(60)  # the parent SIGKILLs us "mid-click"
        return 1

    raise SystemExit(f"unknown command {command}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
