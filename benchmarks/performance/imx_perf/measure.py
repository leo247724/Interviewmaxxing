"""Optional measured mode: time the real packages' local operations on fictional data.

Runs only when the packages import (for example with a sibling worktree's task venv:
``<worktree>/.venv-task/bin/python -m imx_perf measure``). Everything happens in a
temporary ``IMX_HOME``; no browser, no network, no provider key (a fictional key and a
fake transport are used). It never reads ``env.local`` or the private profile.

Measured:

* ``ApplicationStore`` operations a run performs, per operation (fsync'd transactions);
* ``JobStore.upsert`` throughput and ``list_listings(limit=50)`` cost at 1k/5k listings
  (Astra finding 3: the limit is applied after decoding every row);
* ``SelectionService.select`` on the hard-constraint, cache-hit and two-call paths with a
  fake transport, plus the exact request body sizes of both calls;
* ``SelectionStore.latest`` with a growing history;
* ``JobListing.model_validate`` and ``snapshot_hash`` cost.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Assumptions
from .fixtures import CANDIDATE_EVIDENCE, Population, generate

FAKE_KEY = "sk-or-v1-fictional-benchmark-key-000000000000"


def _timeit(fn: Callable[[], Any], n: int) -> dict[str, float]:
    samples = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t) * 1000.0)
    samples.sort()
    return {
        "n": n,
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(samples[min(n - 1, int(0.95 * (n - 1)))], 3),
        "max_ms": round(samples[-1], 3),
        "mean_ms": round(statistics.fmean(samples), 3),
    }


def _importable(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def environment() -> dict[str, Any]:
    from .report import git_head
    package_names = ("interviewmaxxing_core", "interviewmaxxing_jobs", "interviewmaxxing_selection")
    sources = {}
    for name in package_names:
        if _importable(name):
            module = sys.modules[name]
            path = Path(module.__file__).resolve()
            sources[name] = {"module": str(path), "checkout_head": git_head(path.parent)}
    return {
        "package_sources": sources,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "packages": {n: _importable(n) for n in (
            "interviewmaxxing_core", "interviewmaxxing_jobs", "interviewmaxxing_selection",
            "interviewmaxxing_generation", "interviewmaxxing_candidate", "pydantic",
        )},
    }


def measure_store(tmp: Path, n: int = 100) -> dict[str, Any]:
    from interviewmaxxing_core import (
        ApplicationState as S,
    )
    from interviewmaxxing_core import (
        ApplicationStore,
        SubmissionObservation,
        SubmissionOutcome,
    )

    store = ApplicationStore.open(tmp / "state" / "imx.sqlite3")
    out: dict[str, Any] = {}
    urls = [f"https://boards.greenhouse.example/fictionalco/jobs/{i}" for i in range(n)]
    results = []

    def record(i: int) -> None:
        results.append(store.record_request("cand_fictional", urls[i]))

    out["record_request"] = _timeit(lambda: record(len(results)), n)
    claims = []

    def claim(i: int) -> None:
        claims.append(store.claim(results[i].application.id, "bench"))

    out["claim"] = _timeit(lambda: claim(len(claims)), n)
    idx = [0]

    def transition() -> None:
        c = claims[idx[0]]
        store.transition(c, S.INSPECTING)
        store.transition(c, S.PACKET_READY)
        store.transition(c, S.FILLING)
        idx[0] += 1

    out["transition_x3"] = _timeit(transition, n)
    attempts = []

    def begin(i: int) -> None:
        attempts.append(store.begin_submission(claims[i]))

    out["begin_submission"] = _timeit(lambda: begin(len(attempts)), n)
    obs = SubmissionObservation(
        outcome=SubmissionOutcome.ACCEPTED,
        signals=["Thank you, your application for Fictional Role was received"],
        confirmation_reference="FICT-0001",
        detail="fictional acceptance for benchmarking",
    )
    j = [0]

    def outcome() -> None:
        store.record_submission_outcome(claims[j[0]], attempts[j[0]].id, obs)
        j[0] += 1

    out["record_submission_outcome_accepted"] = _timeit(outcome, n)
    k = [0]

    def release() -> None:
        store.release(claims[k[0]])
        k[0] += 1

    out["release"] = _timeit(release, n)
    out["get_receipt"] = _timeit(lambda: store.get_receipt(results[0].application.id), n)
    out["repeat_record_request_existing"] = _timeit(lambda: store.record_request("cand_fictional", urls[0]), n)
    out["list_applications"] = _timeit(lambda: store.list_applications(candidate_id="cand_fictional"), 20)
    total = sum(out[k]["median_ms"] for k in ("record_request", "claim", "transition_x3", "begin_submission",
                                                 "record_submission_outcome_accepted", "release"))
    out["per_application_store_path_median_ms"] = round(total, 3)
    out["note"] = "synchronous=FULL, WAL, BEGIN IMMEDIATE per operation (store.py:283,307)"
    store.close()
    return out


def measure_jobs(tmp: Path, a: Assumptions, sizes: tuple[int, ...] = (1000, 5000)) -> dict[str, Any]:
    from interviewmaxxing_jobs import JobStore

    from interviewmaxxing_core import JobListing, SelectionPreferences, snapshot_hash

    out: dict[str, Any] = {}
    pop = Population(a, seed=42)
    sample = [JobListing.model_validate(f.listing) for f in [pop.one() for _ in range(200)]]
    raw = [s.model_dump_json() for s in sample]
    out["model_validate_json"] = _timeit(lambda: JobListing.model_validate_json(raw[0]), 500)
    out["snapshot_hash_listing"] = _timeit(lambda: snapshot_hash(sample[0]), 500)
    prefs = SelectionPreferences()
    for size in sizes:
        store = JobStore(tmp / f"jobs-{size}" / "jobs.sqlite3")
        fixtures = generate(a, size, seed=100 + size)
        listings = [JobListing.model_validate(f.listing) for f in fixtures]
        t = time.perf_counter()
        for i, listing in enumerate(listings):
            store.upsert(listing, raw={"kind": "bench"}, run_id=f"run_{i % 10}")
        elapsed = time.perf_counter() - t
        entry: dict[str, Any] = {
            "upsert_total_s": round(elapsed, 3),
            "upsert_per_listing_ms": round(elapsed / size * 1000, 3),
            "upserts_per_s": round(size / elapsed, 1),
        }
        stored = int(store._conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0])
        entry["stored_after_dedupe"] = stored
        entry["list_limit_50"] = _timeit(lambda store=store: store.list_listings(limit=50), 5)
        entry["list_limit_50_ranked"] = _timeit(lambda store=store: store.list_listings(limit=50, rank_for=prefs), 5)
        entry["get_listing"] = _timeit(lambda store=store, listing_id=listings[0].id: store.get_listing(listing_id), 200)
        out[f"listings_{size}"] = entry
        store.close()
    out["note"] = ("list_listings decodes every stored row before applying limit (store.py:182-204);"
                   " Astra measured medians 14/81/159 ms at 1k/5k/10k in the service read path")
    return out


class _Bot:
    """Fake Decisions API transport (same shape as tests/selection/conftest.py JevBot)."""

    def __init__(self) -> None:
        self.bodies: list[int] = []
        self.calls = 0

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float):  # type: ignore[no-untyped-def]
        from interviewmaxxing_selection import HttpResponse

        self.calls += 1
        self.bodies.append(len(body))
        request = json.loads(body)
        good = {"role_match": "match", "seniority_match": "at_target_level", "qualification_match": "meets",
                "location_eligibility": "eligible", "preference_match": "aligned",
                "listing_consistency": "consistent", "selection": "APPLY"}
        answers = {}
        for name, q in request["questions"].items():
            options = list(q["criteria"])
            chosen = good.get(name, options[0])
            rest = [o for o in options if o != chosen]
            probs = {o: 0.05 / len(rest) for o in rest}
            probs[chosen] = 0.95
            answers[name] = {"type": "choice", "choice": chosen, "probabilities": probs, "confidence": 0.95}
        payload = {"model": "typesafe/jev-1.13-fictional", "answers": answers,
                   "usage": {"input_tokens": len(body) // 4, "output_tokens": 40, "cost": len(body) / 4 / 1e6 * 0.042},
                   "id": f"gen-dec-bench-{self.calls}", "provider": "TypeSafe"}
        return HttpResponse(200, {}, json.dumps(payload).encode())


def measure_selection(tmp: Path, a: Assumptions, n: int = 60) -> dict[str, Any]:
    from interviewmaxxing_selection import (
        ApiKey,
        CandidateEvidence,
        JevClient,
        SelectionService,
        SelectionStore,
    )

    from interviewmaxxing_core import JobListing, SelectionPreferences
    try:
        from interviewmaxxing_selection.rubric import RUBRIC_VERSION
    except Exception:  # pragma: no cover
        RUBRIC_VERSION = "unknown"

    bot = _Bot()
    client = JevClient(ApiKey(FAKE_KEY, source="benchmark"), transport=bot, sleep=lambda _s: None)
    store = SelectionStore(tmp / "selection" / "selection.sqlite3")
    service = SelectionService(client=client, store=store, application_lookup=lambda _l: None,
                               candidate_id="cand_fictional")
    prefs = SelectionPreferences()
    candidate = CandidateEvidence(**CANDIDATE_EVIDENCE)
    pop = Population(a, seed=77)
    full = []
    closed = []
    while len(full) < n or len(closed) < n:
        fx = pop.one(enriched=True)
        listing = fx.listing
        if listing["status"] == "CLOSED" and len(closed) < n:
            closed.append(JobListing.model_validate(listing))
        elif (listing["description_completeness"] == "FULL" and listing["status"] != "CLOSED"
              and len(full) < n):
            full.append(JobListing.model_validate(listing))
    out: dict[str, Any] = {"rubric_version": RUBRIC_VERSION}
    i = [0]

    def hard() -> None:
        service.select(closed[i[0] % len(closed)], prefs, candidate)
        i[0] += 1

    out["select_hard_constraint_no_call"] = _timeit(hard, n)
    j = [0]
    before = bot.calls

    def two_call() -> None:
        service.select(full[j[0]], prefs, candidate)
        j[0] += 1

    out["select_mixed_full_evidence_fake_transport"] = _timeit(two_call, n)
    out["calls_for_mixed_full_evidence_path"] = bot.calls - before
    out["full_evidence_note"] = "60 full-description inputs; some hard/code gates use no calls. This timing is a mixed local path, not provider latency."
    bodies = bot.bodies[-(2 * n):]
    focused = bodies[0::2]
    final = bodies[1::2]
    out["request_bytes"] = {
        "focused_median": int(statistics.median(focused)), "final_median": int(statistics.median(final)),
        "two_call_total_median": int(statistics.median(focused) + statistics.median(final)),
        "est_tokens_two_call_at_4_chars": round((statistics.median(focused) + statistics.median(final)) / 4),
        "est_usd_per_listing_two_call": round((statistics.median(focused) + statistics.median(final)) / 4 / 1e6
                                              * a.jev_usd_per_million_input_tokens, 6),
    }
    k = [0]
    before = bot.calls

    def cache_hit() -> None:
        service.select(full[k[0] % len(full)], prefs, candidate)
        k[0] += 1

    out["select_repeat_cache_or_code_gate"] = _timeit(cache_hit, n)
    out["calls_for_repeated_inputs"] = bot.calls - before
    out["is_current"] = _timeit(lambda: service.is_current(store.latest(full[0].id, candidate_id="cand_fictional").selection, full[0], prefs, candidate), 100)
    for repeats in (1, 5, 20):
        while len(store.history(full[1].id, candidate_id="cand_fictional")) < repeats:
            service.select(full[1], prefs, candidate, use_cache=False)
        out[f"store_latest_history_{repeats}"] = _timeit(lambda: store.latest(full[1].id, candidate_id="cand_fictional"), 50)
    store.close()
    return out


def run(a: Assumptions | None = None) -> dict[str, Any]:
    a = a or Assumptions()
    env = environment()
    result: dict[str, Any] = {"environment": env}
    if not env["packages"]["interviewmaxxing_core"]:
        result["skipped"] = ("interviewmaxxing_core is not importable in this interpreter; run with a"
                             " sibling worktree's task venv, e.g. build/queue-runtime/.venv-task/bin/python")
        return result
    with tempfile.TemporaryDirectory(prefix="imx-perf-measure-") as d:
        tmp = Path(d)
        os.environ["IMX_HOME"] = str(tmp / "home")
        os.environ.pop("IMX_OPENROUTER_ENV_FILE", None)
        os.environ.pop("OPENROUTER_API_KEY", None)
        try:
            result["application_store"] = measure_store(tmp)
        except Exception as exc:
            result["application_store"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        if env["packages"]["interviewmaxxing_jobs"]:
            try:
                result["job_store"] = measure_jobs(tmp, a)
            except Exception as exc:
                result["job_store"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        if env["packages"]["interviewmaxxing_selection"]:
            try:
                result["selection"] = measure_selection(tmp, a)
            except Exception as exc:
                result["selection"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return result
