"""Durable work-queue prototype (Phase 1 of the design), SQLite, standard library.

A scheduler around the canonical stores, never a second state machine:

* one active item per ``(kind, key)`` (partial unique index) — idempotent enqueue;
* exclusive, expiring leases with owner + token, checked by every mutation (same
  shape as the core ``Claim``);
* lanes with ``max_active`` slots (browser profile, ATS tenant, source session, Jev)
  and a ``high_water`` READY depth for backpressure;
* ``available_at`` for retry spacing, ``attempts``/``max_attempts`` with a dead-letter
  state, ``input_version`` for cache-style invalidation, ``cursor`` for per-source
  progress;
* an append-only event log and an idempotent *effect ledger* that stands in for the
  store's ``begin_submission``/``UNIQUE`` guarantees in the chaos test.

One ``DurableQueue`` per thread (SQLite connection affinity), like ``ApplicationStore``.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

READY, RUNNING, DONE, FAILED, DEAD = "READY", "RUNNING", "DONE", "FAILED", "DEAD"
ACTIVE = (READY, RUNNING)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    lanes TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    available_at REAL NOT NULL,
    input_version TEXT,
    cursor TEXT,
    lease_owner TEXT,
    lease_token TEXT,
    lease_expires_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_error TEXT,
    result TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_item ON work_items (kind, key)
    WHERE state IN ('READY', 'RUNNING');
CREATE INDEX IF NOT EXISTS ready_order ON work_items (state, available_at, priority, created_at);
CREATE TABLE IF NOT EXISTS item_lanes (
    item_id TEXT NOT NULL REFERENCES work_items(id),
    lane TEXT NOT NULL,
    PRIMARY KEY (item_id, lane)
);
CREATE INDEX IF NOT EXISTS lanes_by_lane ON item_lanes (lane);
CREATE TABLE IF NOT EXISTS lane_limits (
    lane TEXT PRIMARY KEY,
    max_active INTEGER NOT NULL,
    high_water INTEGER,
    spacing_s REAL NOT NULL DEFAULT 0,
    last_completed_at REAL
);
CREATE TABLE IF NOT EXISTS budgets (
    name TEXT PRIMARY KEY,
    cap REAL NOT NULL,
    spent REAL NOT NULL DEFAULT 0,
    period_start REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS submission_intents (
    item_id TEXT PRIMARY KEY REFERENCES work_items(id),
    started_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_attempts (
    attempt_id TEXT PRIMARY KEY,
    budget TEXT NOT NULL REFERENCES budgets(name),
    reserved REAL NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'pending',
    actual REAL
);
CREATE TABLE IF NOT EXISTS cooldowns (name TEXT PRIMARY KEY, until_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS effects (
    key TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    applied_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT,
    event TEXT NOT NULL,
    at REAL NOT NULL,
    metadata TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_append_only_u BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_append_only_d BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


class QueueError(RuntimeError):
    pass


class Backpressure(QueueError):
    pass


class LeaseLost(QueueError):
    pass


class BudgetExhausted(QueueError):
    pass


@dataclass(frozen=True)
class Lease:
    item_id: str
    owner: str
    token: str
    expires_at: float


@dataclass(frozen=True)
class Item:
    id: str
    kind: str
    key: str
    lanes: tuple[str, ...]
    priority: int
    payload: dict[str, Any]
    state: str
    attempts: int
    max_attempts: int
    available_at: float
    input_version: str | None
    cursor: str | None
    lease: Lease | None
    last_error: str | None
    result: dict[str, Any] | None


def decision_key(*, candidate_id: str, candidate_evidence_version: str, job_id: str,
                 job_evidence_version: str, preferences_version: str, rubric_version: str,
                 model_version: str) -> str:
    """Execution task identity: no candidate or input-version coalescing."""
    values = locals()
    if not all(isinstance(v, str) and v for v in values.values()):
        raise ValueError("all decision identity fields are required")
    return hashlib.sha256(_json(values).encode()).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class DurableQueue:
    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time,
                 busy_timeout_s: float = 30.0) -> None:
        self.path = str(path)
        self._clock = clock
        self._conn = sqlite3.connect(self.path, timeout=busy_timeout_s, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_s * 1000)}")
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            deadline = time.monotonic() + busy_timeout_s
            while True:
                try:
                    self._conn.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) or time.monotonic() > deadline:
                        raise
                    time.sleep(0.01)
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # --- plumbing --------------------------------------------------------------------

    def now(self) -> float:
        return float(self._clock())

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def _event(self, c: sqlite3.Connection, item_id: str | None, event: str, now: float,
               metadata: dict[str, Any] | None = None) -> None:
        c.execute("INSERT INTO events (item_id, event, at, metadata) VALUES (?, ?, ?, ?)",
                  (item_id, event, now, _json(metadata or {})))

    @staticmethod
    def _item(row: sqlite3.Row) -> Item:
        lease = None
        if row["lease_token"]:
            lease = Lease(row["id"], row["lease_owner"], row["lease_token"], row["lease_expires_at"])
        return Item(
            id=row["id"], kind=row["kind"], key=row["key"], lanes=tuple(json.loads(row["lanes"])),
            priority=row["priority"], payload=json.loads(row["payload"]), state=row["state"],
            attempts=row["attempts"], max_attempts=row["max_attempts"],
            available_at=row["available_at"], input_version=row["input_version"],
            cursor=row["cursor"], lease=lease, last_error=row["last_error"],
            result=json.loads(row["result"]) if row["result"] else None,
        )

    def _row(self, c: sqlite3.Connection, item_id: str) -> sqlite3.Row:
        row = c.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise QueueError(f"no item {item_id}")
        return row

    def _check(self, c: sqlite3.Connection, lease: Lease, now: float) -> sqlite3.Row:
        row = self._row(c, lease.item_id)
        if row["lease_token"] != lease.token or row["lease_owner"] != lease.owner or row["state"] != RUNNING:
            raise LeaseLost(f"lease on {lease.item_id} is no longer held by {lease.owner}")
        if row["lease_expires_at"] is None or row["lease_expires_at"] <= now:
            raise LeaseLost(f"lease on {lease.item_id} expired")
        return row

    # --- configuration ----------------------------------------------------------------

    def set_lane(self, lane: str, *, max_active: int, high_water: int | None = None,
                 spacing_s: float = 0.0) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO lane_limits (lane, max_active, high_water, spacing_s) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(lane) DO UPDATE SET max_active = excluded.max_active,"
                " high_water = excluded.high_water, spacing_s = excluded.spacing_s",
                (lane, max_active, high_water, spacing_s),
            )

    def set_budget(self, name: str, *, cap: float) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO budgets (name, cap, spent, period_start) VALUES (?, ?, 0, ?)"
                " ON CONFLICT(name) DO UPDATE SET cap = excluded.cap",
                (name, cap, self.now()),
            )

    def spend(self, name: str, amount: float) -> float:
        """Add to a budget; raises ``BudgetExhausted`` (and records nothing) past the cap."""
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("budget amount must be finite and nonnegative")
        with self._tx() as c:
            row = c.execute("SELECT * FROM budgets WHERE name = ?", (name,)).fetchone()
            if row is None:
                raise QueueError(f"no budget {name}")
            if row["spent"] + amount > row["cap"]:
                self._event(c, None, "budget.exhausted", self.now(), {"name": name, "cap": row["cap"]})
                raise BudgetExhausted(f"budget {name} exhausted ({row['spent']:.4f}/{row['cap']:.4f})")
            c.execute("UPDATE budgets SET spent = spent + ? WHERE name = ?", (amount, name))
            return float(row["spent"] + amount)

    def reserve_provider_attempt(self, budget: str, attempt_id: str, conservative_cost: float) -> None:
        """Atomically reserve before each paid attempt; uncertain failures retain the reserve.

        Every retry gets a fresh attempt_id. This is an estimate cap, not billing proof.
        """
        if not math.isfinite(conservative_cost) or conservative_cost < 0:
            raise ValueError("invalid reserve")
        with self._tx() as c:
            if c.execute("SELECT 1 FROM provider_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone():
                raise QueueError("attempt already reserved; do not dispatch it again")
            cool = c.execute("SELECT until_at FROM cooldowns WHERE name = ?", (budget,)).fetchone()
            if cool and cool[0] > self.now():
                raise QueueError("shared provider cooldown")
            row = c.execute("SELECT * FROM budgets WHERE name = ?", (budget,)).fetchone()
            if row is None:
                raise QueueError("unknown budget")
            if row["spent"] + conservative_cost > row["cap"] + 1e-12:
                raise BudgetExhausted(budget)
            c.execute("UPDATE budgets SET spent = spent + ? WHERE name = ?", (conservative_cost, budget))
            c.execute("INSERT INTO provider_attempts (attempt_id, budget, reserved) VALUES (?, ?, ?)",
                      (attempt_id, budget, conservative_cost))
            self._event(c, None, "provider.reserved", self.now(), {"attempt_id": attempt_id})

    def settle_provider_attempt(self, attempt_id: str, *, outcome: str,
                                actual_cost: float | None = None) -> None:
        """Reconcile known billing; unknown timed-out/failed calls remain charged in full."""
        if actual_cost is not None and (not math.isfinite(actual_cost) or actual_cost < 0):
            raise ValueError("invalid actual cost")
        if outcome not in ("success", "failed", "timeout"):
            raise ValueError("invalid outcome")
        with self._tx() as c:
            row = c.execute("SELECT * FROM provider_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            if row is None or row["outcome"] != "pending":
                raise QueueError("attempt missing or already settled")
            if actual_cost is not None:
                # Unexpected overage is recorded honestly and blocks further reservations.
                c.execute("UPDATE budgets SET spent = spent + ? WHERE name = ?",
                          (actual_cost - row["reserved"], row["budget"]))
            c.execute("UPDATE provider_attempts SET outcome = ?, actual = ? WHERE attempt_id = ?",
                      (outcome, actual_cost, attempt_id))

    def cooldown(self, budget: str, retry_after_s: float) -> None:
        if not math.isfinite(retry_after_s) or retry_after_s < 0:
            raise ValueError("invalid Retry-After")
        with self._tx() as c:
            c.execute("INSERT INTO cooldowns VALUES (?, ?) ON CONFLICT(name) DO UPDATE"
                      " SET until_at = MAX(until_at, excluded.until_at)",
                      (budget, self.now() + retry_after_s))

    def begin_submission(self, lease: Lease) -> None:
        """Durable intent BEFORE the fictional dispatch; a crash must reconcile even with no effect."""
        with self._tx() as c:
            row = self._check(c, lease, self.now())
            if row["kind"] not in ("apply", "resume"):
                raise QueueError("reconciliation must never submit")
            c.execute("INSERT INTO submission_intents VALUES (?, ?)", (lease.item_id, self.now()))
            self._event(c, lease.item_id, "submission.intent", self.now())

    def _uncertain(self, c: sqlite3.Connection, row: sqlite3.Row, now: float) -> bool:
        if row["kind"] not in ("apply", "resume") or not c.execute(
            "SELECT 1 FROM submission_intents WHERE item_id = ?", (row["id"],)
        ).fetchone():
            return False
        c.execute("UPDATE work_items SET kind = 'reconcile', state = 'READY', attempts = 0,"
                  " available_at = ?, lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,"
                  " updated_at = ?, last_error = 'SUBMISSION_UNKNOWN: reconcile only' WHERE id = ?",
                  (now, now, row["id"]))
        self._event(c, row["id"], "submission.unknown", now)
        return True

    # --- producers ----------------------------------------------------------------------

    def admit(self, lane: str) -> bool:
        """Backpressure: False when the lane's READY depth is at or above its high-water mark."""
        row = self._conn.execute("SELECT high_water FROM lane_limits WHERE lane = ?", (lane,)).fetchone()
        if row is None or row["high_water"] is None:
            return True
        return self.depth(lane) < row["high_water"]

    def enqueue(self, kind: str, key: str, *, lanes: Sequence[str] = (), priority: int = 0,
                payload: dict[str, Any] | None = None, max_attempts: int = 5,
                available_at: float | None = None, input_version: str | None = None) -> tuple[Item, bool]:
        """Idempotent: returns the active item for ``(kind, key)`` and whether it was created."""
        now = self.now()
        with self._tx() as c:
            if kind in ("apply", "resume", "reconcile"):
                operation = c.execute(
                    "SELECT * FROM work_items WHERE key = ? AND kind IN ('apply','resume','reconcile')"
                    " AND state IN ('READY','RUNNING') ORDER BY created_at LIMIT 1", (key,)
                ).fetchone()
                if operation is not None:
                    return self._item(operation), False
            if kind in ("apply", "resume"):
                uncertain = c.execute("SELECT w.* FROM work_items w JOIN submission_intents s ON s.item_id = w.id"
                                      " WHERE w.key = ? ORDER BY w.created_at DESC LIMIT 1", (key,)).fetchone()
                if uncertain is not None:
                    return self._item(uncertain), False
            row = c.execute(
                "SELECT * FROM work_items WHERE kind = ? AND key = ? AND state IN ('READY', 'RUNNING')",
                (kind, key),
            ).fetchone()
            if row is not None:
                return self._item(row), False
            for lane in lanes:
                if not self.admit(lane):
                    raise Backpressure(lane)
            item_id = f"wi_{uuid.uuid4().hex}"
            c.execute(
                "INSERT INTO work_items (id, kind, key, lanes, priority, payload, state, attempts,"
                " max_attempts, available_at, input_version, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'READY', 0, ?, ?, ?, ?, ?)",
                (item_id, kind, key, _json(list(lanes)), priority, _json(payload or {}),
                 max_attempts, available_at if available_at is not None else now, input_version, now, now),
            )
            for lane in lanes:
                c.execute("INSERT INTO item_lanes (item_id, lane) VALUES (?, ?)", (item_id, lane))
            self._event(c, item_id, "item.enqueued", now, {"kind": kind, "key": key, "lanes": list(lanes)})
            return self._item(self._row(c, item_id)), True

    # --- workers -------------------------------------------------------------------------

    def _lane_active(self, c: sqlite3.Connection, lane: str) -> int:
        return int(c.execute(
            "SELECT COUNT(*) FROM item_lanes il JOIN work_items w ON w.id = il.item_id"
            " WHERE il.lane = ? AND w.state = 'RUNNING'", (lane,),
        ).fetchone()[0])

    def _recover(self, c: sqlite3.Connection, now: float) -> list[str]:
        recovered = []
        for row in c.execute(
            "SELECT * FROM work_items WHERE state = 'RUNNING' AND lease_expires_at <= ?", (now,),
        ).fetchall():
            if self._uncertain(c, row, now):
                recovered.append(row["id"])
                continue
            attempts = row["attempts"]
            if attempts >= row["max_attempts"]:
                c.execute(
                    "UPDATE work_items SET state = 'DEAD', lease_owner = NULL, lease_token = NULL,"
                    " lease_expires_at = NULL, updated_at = ?, last_error = ? WHERE id = ?",
                    (now, "lease expired after max attempts", row["id"]),
                )
                self._event(c, row["id"], "item.dead", now, {"attempts": attempts, "reason": "lease expired"})
            else:
                c.execute(
                    "UPDATE work_items SET state = 'READY', lease_owner = NULL, lease_token = NULL,"
                    " lease_expires_at = NULL, updated_at = ?, last_error = ? WHERE id = ?",
                    (now, f"lease of {row['lease_owner']} expired", row["id"]),
                )
                self._event(c, row["id"], "lease.expired", now, {"owner": row["lease_owner"], "attempts": attempts})
            recovered.append(row["id"])
        return recovered

    def recover_expired(self) -> list[str]:
        with self._tx() as c:
            return self._recover(c, self.now())

    def claim_next(self, owner: str, *, ttl_s: float, lanes: Sequence[str] | None = None,
                   kinds: Sequence[str] | None = None) -> tuple[Item, Lease] | None:
        """Take the best READY item whose lanes all have a free slot, or None."""
        now = self.now()
        with self._tx() as c:
            self._recover(c, now)
            sql = "SELECT * FROM work_items WHERE state = 'READY' AND available_at <= ?"
            args: list[Any] = [now]
            if kinds:
                sql += f" AND kind IN ({','.join('?' * len(kinds))})"
                args += list(kinds)
            sql += " ORDER BY CASE WHEN kind = 'reconcile' AND available_at <= ? THEN 0 ELSE 1 END, priority DESC, created_at, rowid"
            args.append(now - 900.0)  # aged uncertainty wins the next eligible slot
            limits = {r["lane"]: r for r in c.execute("SELECT * FROM lane_limits").fetchall()}
            counts: dict[str, int] = {}
            for row in c.execute(sql, args).fetchall():
                item_lanes = json.loads(row["lanes"])
                if lanes is not None and not set(item_lanes) <= set(lanes):
                    continue
                ok = True
                for lane in item_lanes:
                    limit = limits.get(lane)
                    if limit is None:
                        continue
                    if lane not in counts:
                        counts[lane] = self._lane_active(c, lane)
                    if counts[lane] >= limit["max_active"]:
                        ok = False
                        break
                    last = limit["last_completed_at"]
                    if limit["spacing_s"] and last is not None and now - last < limit["spacing_s"]:
                        ok = False
                        break
                if not ok:
                    continue
                token = uuid.uuid4().hex
                expires = now + ttl_s
                c.execute(
                    "UPDATE work_items SET state = 'RUNNING', attempts = attempts + 1, lease_owner = ?,"
                    " lease_token = ?, lease_expires_at = ?, updated_at = ? WHERE id = ?",
                    (owner, token, expires, now, row["id"]),
                )
                self._event(c, row["id"], "lease.granted", now, {"owner": owner, "attempt": row["attempts"] + 1})
                return self._item(self._row(c, row["id"])), Lease(row["id"], owner, token, expires)
        return None

    def renew(self, lease: Lease, *, ttl_s: float) -> Lease:
        now = self.now()
        with self._tx() as c:
            self._check(c, lease, now)
            expires = now + ttl_s
            c.execute("UPDATE work_items SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                      (expires, now, lease.item_id))
        return Lease(lease.item_id, lease.owner, lease.token, expires)

    def progress(self, lease: Lease, *, cursor: str | None = None,
                 metadata: dict[str, Any] | None = None) -> None:
        """Durable per-item progress (e.g. a per-source cursor) under the lease."""
        now = self.now()
        with self._tx() as c:
            self._check(c, lease, now)
            if cursor is not None:
                c.execute("UPDATE work_items SET cursor = ?, updated_at = ? WHERE id = ?",
                          (cursor, now, lease.item_id))
            self._event(c, lease.item_id, "item.progress", now, {"cursor": cursor, **(metadata or {})})

    def apply_effect(self, lease: Lease, effect_key: str) -> bool:
        """Record a side effect exactly once per key. True if this call applied it; False
        if it was already applied (by anyone). Mirrors the store's one-open-attempt and
        UNIQUE(candidate, job) protections for the chaos test."""
        now = self.now()
        with self._tx() as c:
            row = self._check(c, lease, now)
            if row["kind"] == "reconcile":
                raise QueueError("reconciliation cannot dispatch an effect")
            cur = c.execute("INSERT OR IGNORE INTO effects (key, item_id, owner, applied_at) VALUES (?, ?, ?, ?)",
                            (effect_key, lease.item_id, lease.owner, now))
            applied = cur.rowcount == 1
            self._event(c, lease.item_id, "effect.applied" if applied else "effect.already_applied", now,
                        {"key": effect_key})
            return applied

    def effect_applied(self, effect_key: str) -> bool:
        return self._conn.execute("SELECT 1 FROM effects WHERE key = ?", (effect_key,)).fetchone() is not None

    def complete(self, lease: Lease, result: dict[str, Any] | None = None) -> Item:
        now = self.now()
        with self._tx() as c:
            row = self._check(c, lease, now)
            c.execute(
                "UPDATE work_items SET state = 'DONE', result = ?, lease_owner = NULL, lease_token = NULL,"
                " lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                (_json(result or {}), now, lease.item_id),
            )
            for lane in json.loads(row["lanes"]):
                c.execute("UPDATE lane_limits SET last_completed_at = ? WHERE lane = ?", (now, lane))
            self._event(c, lease.item_id, "item.done", now, {"owner": lease.owner})
            return self._item(self._row(c, lease.item_id))

    def fail(self, lease: Lease, error: str, *, retry_in_s: float | None = None,
             permanent: bool = False) -> Item:
        now = self.now()
        with self._tx() as c:
            row = self._check(c, lease, now)
            if self._uncertain(c, row, now):
                return self._item(self._row(c, lease.item_id))
            dead = permanent or row["attempts"] >= row["max_attempts"]
            state = DEAD if dead else READY
            c.execute(
                "UPDATE work_items SET state = ?, available_at = ?, last_error = ?, lease_owner = NULL,"
                " lease_token = NULL, lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                (state, now + (retry_in_s or 0.0), error[:500], now, lease.item_id),
            )
            self._event(c, lease.item_id, "item.dead" if dead else "item.failed", now,
                        {"error": error[:200], "retry_in_s": retry_in_s, "attempts": row["attempts"]})
            return self._item(self._row(c, lease.item_id))

    def release(self, lease: Lease) -> None:
        """Give the item back without counting a failure (e.g. a transient ClaimUnavailable)."""
        now = self.now()
        with self._tx() as c:
            row = self._check(c, lease, now)
            if self._uncertain(c, row, now):
                return
            c.execute(
                "UPDATE work_items SET state = 'READY', attempts = attempts - 1, lease_owner = NULL,"
                " lease_token = NULL, lease_expires_at = NULL, updated_at = ?"
                " WHERE id = ? AND lease_token = ? AND state = 'RUNNING'",
                (now, lease.item_id, lease.token),
            )
            self._event(c, lease.item_id, "lease.released", now, {"owner": lease.owner})

    # --- queries --------------------------------------------------------------------------

    def get(self, item_id: str) -> Item:
        return self._item(self._row(self._conn, item_id))

    def depth(self, lane: str | None = None, state: str = READY) -> int:
        if lane is None:
            return int(self._conn.execute("SELECT COUNT(*) FROM work_items WHERE state = ?", (state,)).fetchone()[0])
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM item_lanes il JOIN work_items w ON w.id = il.item_id"
            " WHERE il.lane = ? AND w.state = ?", (lane, state),
        ).fetchone()[0])

    def stats(self) -> dict[str, Any]:
        states = {r["state"]: r["n"] for r in self._conn.execute(
            "SELECT state, COUNT(*) AS n FROM work_items GROUP BY state").fetchall()}
        effects = int(self._conn.execute("SELECT COUNT(*) FROM effects").fetchone()[0])
        events = {r["event"]: r["n"] for r in self._conn.execute(
            "SELECT event, COUNT(*) AS n FROM events GROUP BY event").fetchall()}
        live = int(self._conn.execute(
            "SELECT COUNT(*) FROM work_items WHERE state = 'RUNNING' AND lease_expires_at > ?",
            (self.now(),)).fetchone()[0])
        return {"states": states, "effects": effects, "events": events, "live_leases": live}

    def items(self, state: str | None = None) -> list[Item]:
        if state is None:
            rows = self._conn.execute("SELECT * FROM work_items ORDER BY created_at, rowid").fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM work_items WHERE state = ? ORDER BY created_at, rowid",
                                      (state,)).fetchall()
        return [self._item(r) for r in rows]


class Heartbeat:
    """Renews a lease on a background thread while long work runs (the cure for a wait
    longer than the lease TTL, cf. the I1 claim-renewal finding)."""

    def __init__(self, queue_factory: Callable[[], DurableQueue], lease: Lease, *, ttl_s: float,
                 every_s: float) -> None:
        self._factory = queue_factory
        self.lease = lease
        self.ttl_s = ttl_s
        self.every_s = every_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.lost = False

    def __enter__(self) -> Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        q = self._factory()
        try:
            while not self._stop.wait(self.every_s):
                try:
                    self.lease = q.renew(self.lease, ttl_s=self.ttl_s)
                except LeaseLost:
                    self.lost = True
                    return
        finally:
            q.close()
