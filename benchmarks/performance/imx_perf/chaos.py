"""Chaos test for the durable queue: kill workers randomly during a 100-item run.

Invariants checked (WT-07 acceptance criterion, run against the prototype):

* every item ends DONE (or DEAD only if it exceeded its attempt budget, which the
  default parameters make impossible);
* the simulated submit effect is applied **at most once** per item, however many
  times the item was re-claimed after a kill — including kills *after* the effect and
  before completion, which is the `SUBMISSION_UNKNOWN` shape (re-claimers must route to
  reconciliation, never re-apply);
* lane limits are never exceeded at any sampled instant;
* no live lease remains at the end.

Bounded: four worker threads, 40 items, 20 seconds per variant. Worker kills are
cooperative abandonment, not OS kills or power-loss durability tests.
"""

from __future__ import annotations

import random
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .durable_queue import DurableQueue, Heartbeat, LeaseLost


@dataclass
class ChaosConfig:
    items: int = 40
    workers: int = 4
    steps_per_item: int = 4
    step_ms: float = 4.0
    p_kill_per_step: float = 0.08
    lease_ttl_s: float = 0.15
    slots: int = 1
    tenants: int = 6
    heartbeat: bool = True
    seed: int = 3
    max_wall_s: float = 20.0


@dataclass
class ChaosResult:
    config: ChaosConfig
    wall_s: float
    kills: int
    takeovers: int
    """Times a still-working owner lost its lease to expiry (the un-renewed-wait hazard)."""
    reclaims: int
    reconcile_routes: int
    reconciled_without_effect: int
    effects: int
    double_effects: int
    states: dict[str, int]
    live_leases_at_end: int
    lane_violations: int
    max_observed_active: dict[str, int]
    heartbeat_lost: int
    passed: bool
    failures: list[str] = field(default_factory=list)


def run(config: ChaosConfig | None = None) -> ChaosResult:
    cfg = config or ChaosConfig()
    rng = random.Random(cfg.seed)
    tmp = tempfile.TemporaryDirectory(prefix="imx-perf-chaos-")
    path = Path(tmp.name) / "queue.sqlite3"
    q = DurableQueue(path)
    for s in range(cfg.slots):
        q.set_lane(f"browser_profile:slot-{s}", max_active=1)
    for t in range(cfg.tenants):
        q.set_lane(f"ats_tenant:tenant-{t}.example", max_active=1)
    for i in range(cfg.items):
        lanes = [f"browser_profile:slot-{i % cfg.slots}", f"ats_tenant:tenant-{i % cfg.tenants}.example"]
        q.enqueue("apply", f"app_{i:04d}", lanes=lanes, priority=rng.randint(0, 3),
                  payload={"application_id": f"app_{i:04d}"}, max_attempts=50)

    kills = 0
    takeovers = 0
    reclaims = 0
    reconcile_routes = 0
    reconciled_without_effect = 0
    heartbeat_lost = 0
    lane_violations = 0
    max_active: dict[str, int] = {}
    lock = threading.Lock()
    stop = threading.Event()
    failures: list[str] = []

    def observe_lanes(qq: DurableQueue) -> None:
        nonlocal lane_violations
        rows = qq._conn.execute(
            "SELECT il.lane AS lane, COUNT(*) AS n FROM item_lanes il JOIN work_items w ON w.id = il.item_id"
            " WHERE w.state = 'RUNNING' AND w.lease_expires_at > ? GROUP BY il.lane", (qq.now(),),
        ).fetchall()
        with lock:
            for r in rows:
                max_active[r["lane"]] = max(max_active.get(r["lane"], 0), r["n"])
                if r["n"] > 1:
                    lane_violations += 1

    def worker(index: int) -> None:
        nonlocal kills, takeovers, reclaims, reconcile_routes, reconciled_without_effect, heartbeat_lost
        wrng = random.Random(cfg.seed * 100 + index)
        incarnation = 0
        qq = DurableQueue(path)
        try:
            while not stop.is_set():
                owner = f"worker-{index}-{incarnation}"
                claimed = qq.claim_next(owner, ttl_s=cfg.lease_ttl_s)
                if claimed is None:
                    if qq.depth() == 0 and qq.depth(state="RUNNING") == 0:
                        return
                    time.sleep(0.003)
                    continue
                item, lease = claimed
                if item.attempts > 1:
                    with lock:
                        reclaims += 1
                app = item.payload["application_id"]
                effect_key = f"submit:{app}"
                if item.kind == "reconcile":
                    # A previous owner dispatched the submit and died: never resubmit.
                    qq.complete(lease, {"outcome": "routed_to_reconcile"})
                    with lock:
                        reconcile_routes += 1
                        reconciled_without_effect += not qq.effect_applied(effect_key)
                    continue
                died = False
                lost = False
                hb = None
                if cfg.heartbeat:
                    hb = Heartbeat(lambda: DurableQueue(path), lease, ttl_s=cfg.lease_ttl_s,
                                   every_s=cfg.lease_ttl_s / 3)
                    hb.__enter__()
                try:
                    for step in range(cfg.steps_per_item):
                        time.sleep(cfg.step_ms / 1000.0 * wrng.uniform(0.5, 1.5))
                        observe_lanes(qq)
                        if wrng.random() < cfg.p_kill_per_step:
                            died = True
                            break
                        if step == cfg.steps_per_item - 3:
                            current = hb.lease if hb is not None else lease
                            try:
                                qq.begin_submission(current)
                            except LeaseLost:
                                lost = True
                                break
                        if step == cfg.steps_per_item - 2:
                            current = hb.lease if hb is not None else lease
                            try:
                                qq.apply_effect(current, effect_key)
                            except LeaseLost:
                                lost = True  # took too long; the item was reclaimed
                                break
                    if not died and not lost:
                        current = hb.lease if hb is not None else lease
                        try:
                            qq.complete(current, {"outcome": "submitted"})
                        except LeaseLost:
                            lost = True
                finally:
                    if hb is not None:
                        hb.__exit__(None, None, None)
                        if hb.lost:
                            with lock:
                                heartbeat_lost += 1
                if lost:
                    with lock:
                        takeovers += 1
                if died:
                    # Abandon the item without release: the lease must expire and be reclaimed.
                    with lock:
                        kills += 1
                    incarnation += 1
                    time.sleep(cfg.lease_ttl_s * 0.5)
        except Exception as exc:  # pragma: no cover - surfaced in the result
            with lock:
                failures.append(f"worker {index}: {type(exc).__name__}: {exc}")
        finally:
            qq.close()

    started = time.monotonic()
    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(cfg.workers)]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        if time.monotonic() - started > cfg.max_wall_s:
            stop.set()
            failures.append("timed out")
            break
        time.sleep(0.01)
    for t in threads:
        t.join(timeout=1.0)
    q.recover_expired()
    stats = q.stats()
    wall = time.monotonic() - started
    effects = stats["effects"]
    double = int(q._conn.execute(
        "SELECT COUNT(*) FROM (SELECT key FROM effects GROUP BY key HAVING COUNT(*) > 1)").fetchone()[0])
    states = stats["states"]
    passed = (
        states.get("DONE", 0) == cfg.items
        and effects + reconciled_without_effect == cfg.items
        and double == 0
        and stats["live_leases"] == 0
        and lane_violations == 0
        and not failures
    )
    result = ChaosResult(
        config=cfg, wall_s=round(wall, 3), kills=kills, takeovers=takeovers, reclaims=reclaims,
        reconcile_routes=reconcile_routes, reconciled_without_effect=reconciled_without_effect, effects=effects, double_effects=double, states=states,
        live_leases_at_end=stats["live_leases"], lane_violations=lane_violations,
        max_observed_active=dict(sorted(max_active.items())), heartbeat_lost=heartbeat_lost,
        passed=passed, failures=failures,
    )
    q.close()
    tmp.cleanup()
    return result


def as_dict(result: ChaosResult) -> dict[str, Any]:
    d = dict(result.__dict__)
    d["config"] = dict(result.config.__dict__)
    return d
