"""Races between real processes/threads sharing one SQLite database, and a real crash."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from interviewmaxxing_core import (
    ApplicationState,
    ApplicationStore,
    ClaimUnavailable,
    RequestDisposition,
    utc_now,
)

WORKER = Path(__file__).with_name("_store_worker.py")
URL = "https://jobs.mock.example/mock-co/4012"
CAND = "cand_avery_example"
S = ApplicationState

pytestmark = pytest.mark.slow


def _spawn(*args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(WORKER), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _collect(procs: list[subprocess.Popen[str]]) -> list[str]:
    outputs = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        outputs.append(out.strip())
    return outputs


def _start_at(delay: float = 1.5) -> str:
    return str(time.time() + delay)


def test_concurrent_first_requests_create_one_application(store_path):
    # The database does not exist yet: creation and migration race too.
    start = _start_at()
    procs = [_spawn("request", str(store_path), CAND, URL + f"?utm_source=w{i}", start)
             for i in range(8)]
    results = [json.loads(o) for o in _collect(procs)]
    assert len({r["app"] for r in results}) == 1
    assert sorted(r["disposition"] for r in results) == ["NEW"] + ["RESUMABLE"] * 7
    with ApplicationStore.open(store_path) as store:
        [app] = store.list_applications()
        assert len(store.list_requests(app.id)) == 8
        events = [e.event for e in store.list_events(app.id)]
        assert events.count("application.requested") == 1
        assert events.count("application.request_repeated") == 7


def test_concurrent_claims_have_exactly_one_winner(store_path):
    with ApplicationStore.open(store_path) as store:
        app = store.record_request(CAND, URL).application
    start = _start_at()
    procs = [_spawn("claim", str(store_path), app.id, f"w{i}", start) for i in range(8)]
    outputs = _collect(procs)
    assert outputs.count("CLAIMED") == 1
    assert outputs.count("UNAVAILABLE") == 7


def test_concurrent_submitters_produce_one_attempt(store_path):
    with ApplicationStore.open(store_path) as store:
        app = store.record_request(CAND, URL).application
        claim = store.claim(app.id, "setup")
        for state in (S.INSPECTING, S.PACKET_READY, S.FILLING):
            store.transition(claim, state)
        store.release(claim)
    start = _start_at()
    procs = [_spawn("submit", str(store_path), app.id, f"w{i}", start) for i in range(6)]
    outputs = _collect(procs)
    assert sum(o.startswith("SUBMITTED") for o in outputs) == 1, outputs
    assert all(o.startswith(("SUBMITTED", "BLOCKED")) for o in outputs), outputs
    with ApplicationStore.open(store_path) as store:
        assert len(store.list_attempts(app.id)) == 1
        assert store.get_application(app.id).state is S.SUBMITTED
        events = [e.event for e in store.list_events(app.id)]
        assert events.count("application.submitting") == 1
        assert events.count("application.submitted") == 1


def test_threads_with_separate_connections_serialize(store_path):
    ApplicationStore.open(store_path).close()
    results: list[RequestDisposition] = []
    claimed: list[str] = []
    barrier = threading.Barrier(12)
    lock = threading.Lock()

    def worker(i: int) -> None:
        with ApplicationStore.open(store_path) as store:
            barrier.wait()
            result = store.record_request(CAND, URL)
            try:
                store.claim(result.application.id, f"t{i}")
                ok = True
            except ClaimUnavailable:
                ok = False
            with lock:
                results.append(result.disposition)
                if ok:
                    claimed.append(f"t{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(RequestDisposition.NEW) == 1
    assert len(claimed) == 1


def test_killed_submitter_leaves_uncertain_state_that_blocks_retry(store_path):
    with ApplicationStore.open(store_path) as store:
        app = store.record_request(CAND, URL).application
    proc = _spawn("crash-during-submit", str(store_path), app.id, "doomed", _start_at(0))
    line = proc.stdout.readline().strip()  # type: ignore[union-attr]
    assert line.startswith("SUBMITTING"), proc.stderr.read()  # type: ignore[union-attr]
    attempt_id = line.split()[1]
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=10)
    assert proc.returncode == -signal.SIGKILL

    # SUBMITTING was durable before the "click"; a fresh process sees it.
    with ApplicationStore.open(store_path) as store:
        assert store.get_application(app.id).state is S.SUBMITTING
        with pytest.raises(ClaimUnavailable):
            store.claim(app.id, "impatient")
        assert store.record_request(CAND, URL).disposition is (
            RequestDisposition.SUBMISSION_IN_PROGRESS
        )

    # Once the dead owner's lease lapses, the next claim marks the outcome unknown.
    later = ApplicationStore.open(store_path, clock=lambda: utc_now() + timedelta(minutes=11))
    with later:
        later.claim(app.id, "next-run")
        after = later.get_application(app.id)
        assert after.state is S.SUBMISSION_UNKNOWN
        [attempt] = later.list_attempts(app.id)
        assert (attempt.id, attempt.outcome) == (attempt_id, "INTERRUPTED")
        assert later.record_request(CAND, URL).disposition is RequestDisposition.SUBMISSION_UNKNOWN
        assert later.get_receipt(app.id) is None
