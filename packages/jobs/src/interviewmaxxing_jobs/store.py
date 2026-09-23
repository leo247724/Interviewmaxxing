"""Private local storage for search runs, observed listings and their raw evidence.

One SQLite file, owner-only (directory 0700, file 0600), default
``$IMX_HOME/jobs/jobs.sqlite3`` (override with ``IMX_JOBS_DB``). It is separate from
the application store: listings here are observations, not application records, and
nothing here holds an application state. User pipeline stages will be stored beside
these tables using the D0 ``PipelineEntry`` contract.

Dedupe follows D0R2 (``JobListing.identity_keys``): observations are one posting only
when they share a source's own posting id/URL (``posting_key``) or a proven
``employer_job_key``. Search/result page URLs and application URLs are provenance, not
identity, and matching titles, companies or locations never merge. When a merge changes which record is
primary (a direct source is preferred over a Google aggregation), the old listing id
stays resolvable as an alias.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from interviewmaxxing_core import (
    JobListing,
    JobSearchRun,
    ListingStatus,
    LocalPaths,
    utc_now,
)

from .ranking import LocationPreferences, rank_listings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listing_keys (
    key TEXT PRIMARY KEY,
    listing_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listing_aliases (
    alias_id TEXT PRIMARY KEY,
    listing_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_listing_id TEXT,
    run_id TEXT,
    query_id TEXT,
    observed_at TEXT NOT NULL,
    raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_listing ON observations(listing_id);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    data TEXT NOT NULL
);
"""


def default_db_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    explicit = env.get("IMX_JOBS_DB")
    if explicit:
        return Path(explicit).expanduser()
    return LocalPaths.from_env(env).home / "jobs" / "jobs.sqlite3"


def _dump(model: JobListing | JobSearchRun) -> str:
    return model.model_dump_json()


class JobStore:
    """Local persistence API for J1 (and the S1 service built on it)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fresh = not self.path.exists()
        if fresh:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            os.close(fd)
        os.chmod(self.path, 0o600)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> JobStore:
        return cls(default_db_path(env))

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    # --- listings ------------------------------------------------------------------

    def upsert(self, listing: JobListing, *, raw: Mapping[str, Any] | None = None,
               run_id: str | None = None) -> JobListing:
        """Store an observation, merging it into any provably identical listing.
        Returns the stored (possibly merged) listing."""
        with self._tx() as db:
            matched = self._matching_ids(db, listing)
            existing = [self._load(db, i) for i in matched]
            stored, absorbed = combine(listing, [e for e in existing if e is not None])
            matched = [i for i in matched if i in absorbed]
            now = utc_now().isoformat()
            first_seen = [
                row[0] for row in db.execute(
                    f"SELECT first_seen_at FROM listings WHERE id IN ({_marks(matched)})", matched)
            ]
            db.execute(
                "INSERT INTO listings(id, source, status, data, first_seen_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET source=excluded.source, "
                "status=excluded.status, data=excluded.data, updated_at=excluded.updated_at",
                (stored.id, stored.source, stored.status.value, _dump(stored),
                 min(first_seen, default=now), now),
            )
            for old in matched:
                if old != stored.id:
                    db.execute("DELETE FROM listings WHERE id = ?", (old,))
                    db.execute("INSERT OR REPLACE INTO listing_aliases(alias_id, listing_id) "
                               "VALUES (?, ?)", (old, stored.id))
                    db.execute("UPDATE listing_aliases SET listing_id = ? WHERE listing_id = ?",
                               (stored.id, old))
                    db.execute("UPDATE listing_keys SET listing_id = ? WHERE listing_id = ?",
                               (stored.id, old))
                    db.execute("UPDATE observations SET listing_id = ? WHERE listing_id = ?",
                               (stored.id, old))
            if listing.id != stored.id:
                db.execute("INSERT OR REPLACE INTO listing_aliases(alias_id, listing_id) "
                           "VALUES (?, ?)", (listing.id, stored.id))
            for key in stored.identity_keys:
                db.execute("INSERT OR IGNORE INTO listing_keys(key, listing_id) VALUES (?, ?)",
                           (key, stored.id))
            first = listing.provenance[0]
            db.execute(
                "INSERT INTO observations(listing_id, source, source_listing_id, run_id, query_id, "
                "observed_at, raw) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (stored.id, first.source, first.source_listing_id, run_id, first.query_id,
                 first.observed_at.isoformat(), json.dumps(dict(raw or {}), ensure_ascii=False)),
            )
            return stored

    def get_listing(self, listing_id: str) -> JobListing | None:
        """A listing by id; ids replaced by a merge resolve to the surviving listing."""
        with self._lock:
            return self._load(self._conn, self._resolve(self._conn, listing_id))

    def list_listings(self, *, source: str | None = None, status: ListingStatus | None = None,
                      ids: Sequence[str] | None = None, limit: int | None = None,
                      rank_for: LocationPreferences | None = None) -> list[JobListing]:
        """Stored listings, newest first. ``rank_for`` (a JobSearchQuery or
        SelectionPreferences) orders them by its ``location_priority`` before the
        limit is applied, so Austin onsite/hybrid roles are not crowded out by remote
        ones; nothing is filtered out by location."""
        sql = "SELECT data FROM listings"
        where: list[str] = []
        args: list[Any] = []
        if status:
            where.append("status = ?")
            args.append(status.value)
        if ids is not None:
            resolved = list(dict.fromkeys(self._resolve(self._conn, i) for i in ids))
            where.append(f"id IN ({_marks(resolved)})")
            args += resolved
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, id"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        listings = [JobListing.model_validate_json(r[0]) for r in rows]
        if source:
            # Any provenance counts: a LinkedIn posting also found through Google is
            # listed under both sources.
            listings = [x for x in listings if any(p.source == source for p in x.provenance)]
        if rank_for is not None:
            listings = rank_listings(listings, rank_for)
        return listings if limit is None else listings[: max(0, limit)]

    def observations(self, listing_id: str) -> list[dict[str, Any]]:
        """Raw per-source evidence recorded for a listing, oldest first."""
        with self._lock:
            target = self._resolve(self._conn, listing_id)
            rows = self._conn.execute(
                "SELECT source, source_listing_id, run_id, query_id, observed_at, raw "
                "FROM observations WHERE listing_id = ? ORDER BY seq", (target,)).fetchall()
        return [{"source": r[0], "source_listing_id": r[1], "run_id": r[2], "query_id": r[3],
                 "observed_at": r[4], "raw": json.loads(r[5])} for r in rows]

    # --- runs --------------------------------------------------------------------------

    def save_run(self, run: JobSearchRun) -> None:
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO runs(id, started_at, data) VALUES (?, ?, ?)",
                       (run.id, run.started_at.isoformat(), _dump(run)))

    def get_run(self, run_id: str) -> JobSearchRun | None:
        with self._lock:
            row = self._conn.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
        return JobSearchRun.model_validate_json(row[0]) if row else None

    def latest_run(self) -> JobSearchRun | None:
        runs = self.list_runs(1)
        return runs[0] if runs else None

    def list_runs(self, limit: int = 20) -> list[JobSearchRun]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [JobSearchRun.model_validate_json(r[0]) for r in rows]

    # --- internals ---------------------------------------------------------------------

    def _resolve(self, db: sqlite3.Connection, listing_id: str) -> str:
        seen = {listing_id}
        current = listing_id
        while True:
            row = db.execute("SELECT listing_id FROM listing_aliases WHERE alias_id = ?",
                             (current,)).fetchone()
            if not row or row[0] in seen:
                return current
            current = row[0]
            seen.add(current)

    def _matching_ids(self, db: sqlite3.Connection, listing: JobListing) -> list[str]:
        found: list[str] = []
        own = self._resolve(db, listing.id)
        if db.execute("SELECT 1 FROM listings WHERE id = ?", (own,)).fetchone():
            found.append(own)
        for key in sorted(listing.identity_keys):
            row = db.execute("SELECT listing_id FROM listing_keys WHERE key = ?", (key,)).fetchone()
            if row:
                target = self._resolve(db, row[0])
                if target not in found and db.execute(
                        "SELECT 1 FROM listings WHERE id = ?", (target,)).fetchone():
                    found.append(target)
        return found

    def _load(self, db: sqlite3.Connection, listing_id: str) -> JobListing | None:
        row = db.execute("SELECT data FROM listings WHERE id = ?", (listing_id,)).fetchone()
        return JobListing.model_validate_json(row[0]) if row else None


def _marks(values: Sequence[object]) -> str:
    return ",".join("?" * len(values)) or "NULL"


_AGGREGATORS = {"google"}


def combine(new: JobListing, existing: Sequence[JobListing]) -> tuple[JobListing, list[str]]:
    """Merge a new observation with the stored listings that share an identity key.

    Returns the listing to store and the stored ids it absorbed. Under D0R2 a shared
    key is the same source's own posting id/URL or a proven ``employer_job_key``;
    records that contradict (different ids from one source, different employer keys)
    are never merged. The same posting re-observed on its source refreshes its fields
    and keeps any enriched employer key and evidence (``merged_with``). A direct
    source stays primary over an aggregator (Google). Missing values are filled from
    the other records, stated pay with numeric bounds is not replaced by text-only
    pay, the fuller description wins, and a CLOSED observation stays closed.
    """
    if not existing:
        return new, []
    absorbed: list[str] = []
    same = next((e for e in existing if e.id == new.id), None)
    if same is not None:
        merged = _prefer_bounds(new.merged_with(same), same) if new.is_same_posting(same) else new
        absorbed.append(same.id)
        pending = [e for e in existing if e is not same]
    else:
        # Stored records first, so an existing direct listing keeps its id.
        candidates = [*existing, new]
        direct = [c for c in candidates if c.source not in _AGGREGATORS]
        merged = direct[0] if direct else existing[0]
        if merged is not new:
            absorbed.append(merged.id)
        pending = [c for c in candidates if c is not merged]
    progress = True
    while pending and progress:
        progress = False
        for other in list(pending):
            if merged.is_same_posting(other):
                merged = _prefer_bounds(merged.merged_with(other), other)
                pending.remove(other)
                if other is not new:
                    absorbed.append(other.id)
                progress = True
    if any(p is new for p in pending):
        # The observation contradicts the stored record it shares a key with: keep it
        # as its own listing and leave the stored ones untouched.
        return new, []
    return merged, absorbed


def _prefer_bounds(merged: JobListing, other: JobListing) -> JobListing:
    have = merged.compensation is not None and merged.compensation.is_comparable
    if not have and other.compensation is not None and other.compensation.is_comparable:
        data = merged.model_dump()
        data["compensation"] = other.compensation.model_dump()
        return JobListing.model_validate(data)
    return merged
