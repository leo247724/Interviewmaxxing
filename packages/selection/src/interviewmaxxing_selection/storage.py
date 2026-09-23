"""Private local storage of selection decisions and their evidence.

One SQLite file (default ``$IMX_HOME/selection/selection.sqlite3``, directory ``0700``,
file ``0600``). Each row keeps the decision record plus the exact job/candidate
evidence snapshots, the requests sent and the raw provider responses, so a decision
can be audited and reproduced. Never contains the API key.

Every row belongs to one candidate. Lookups that could otherwise cross candidates
(cache reuse, latest, history) take the candidate id; ``get``/``audit`` by selection id
also require the owner.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from interviewmaxxing_core import LocalPaths

from .decision import SelectionOutcome

_TABLE = """
CREATE TABLE IF NOT EXISTS selection_decisions (
    id TEXT PRIMARY KEY,
    listing_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    cacheable INTEGER NOT NULL,
    effective_decision TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    record_json TEXT NOT NULL,
    job_snapshot_json TEXT NOT NULL,
    candidate_snapshot_json TEXT NOT NULL,
    preferences_json TEXT NOT NULL,
    requests_json TEXT NOT NULL,
    responses_json TEXT NOT NULL
);
"""
_INDEXES = """
DROP INDEX IF EXISTS selection_by_listing;
DROP INDEX IF EXISTS selection_by_cache;
CREATE INDEX IF NOT EXISTS selection_by_candidate_listing
    ON selection_decisions (candidate_id, listing_id, decided_at);
CREATE INDEX IF NOT EXISTS selection_by_candidate_cache
    ON selection_decisions (candidate_id, listing_id, cache_key, cacheable);
"""
_COLUMNS = (
    "id",
    "listing_id",
    "candidate_id",
    "cache_key",
    "cacheable",
    "effective_decision",
    "decided_at",
    "record_json",
    "job_snapshot_json",
    "candidate_snapshot_json",
    "preferences_json",
    "requests_json",
    "responses_json",
)


@dataclass(frozen=True, slots=True)
class DecisionAudit:
    outcome: SelectionOutcome
    job_snapshot: dict[str, Any]
    candidate_snapshot: dict[str, Any] | None
    preferences: dict[str, Any]
    requests: list[dict[str, Any]]
    responses: list[dict[str, Any]]


def default_store_path(paths: LocalPaths | None = None) -> Path:
    paths = paths or LocalPaths.from_env()
    return paths.home / "selection" / "selection.sqlite3"


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


class SelectionStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        os.chmod(self.path, 0o600)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_TABLE)
        self._migrate()
        self._conn.executescript(_INDEXES)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _migrate(self) -> None:
        """Rows written before candidate scoping carry their candidate id only inside
        ``record_json``; copy it into the new column so scoped lookups see them."""
        # Inspect under the write lock: another thread-local store may be doing
        # its first open against the same legacy database at the same time.
        with self._tx() as c:
            columns = {row["name"] for row in c.execute("PRAGMA table_info(selection_decisions)")}
            if "candidate_id" in columns:
                return
            c.execute(
                "ALTER TABLE selection_decisions ADD COLUMN candidate_id TEXT NOT NULL DEFAULT ''"
            )
            rows = c.execute("SELECT id, record_json FROM selection_decisions").fetchall()
            for row in rows:
                owner = json.loads(row["record_json"])["selection"]["candidate_id"]
                c.execute(
                    "UPDATE selection_decisions SET candidate_id = ? WHERE id = ?",
                    (owner, row["id"]),
                )

    def save(
        self,
        outcome: SelectionOutcome,
        *,
        job_snapshot: dict[str, Any],
        candidate_snapshot: dict[str, Any] | None,
        preferences: dict[str, Any],
        requests: list[dict[str, Any]],
        responses: list[dict[str, Any]],
    ) -> None:
        selection = outcome.selection
        with self._tx() as c:
            c.execute(
                f"INSERT INTO selection_decisions ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(_COLUMNS))})",
                (
                    selection.id,
                    selection.listing_id,
                    selection.candidate_id,
                    outcome.cache_key,
                    int(outcome.cacheable),
                    selection.effective_choice.value,
                    selection.decided_at.isoformat(),
                    outcome.model_dump_json(),
                    _dump(job_snapshot),
                    _dump(candidate_snapshot),
                    _dump(preferences),
                    _dump(requests),
                    _dump(responses),
                ),
            )

    def _row(self, selection_id: str, candidate_id: str) -> sqlite3.Row | None:
        return self._conn.execute(  # type: ignore[no-any-return]
            "SELECT * FROM selection_decisions WHERE id = ? AND candidate_id = ?",
            (selection_id, candidate_id),
        ).fetchone()

    def get(self, selection_id: str, *, candidate_id: str) -> SelectionOutcome | None:
        """The owner's outcome by id, or None if absent or owned by another candidate."""
        row = self._row(selection_id, candidate_id)
        return SelectionOutcome.model_validate_json(row["record_json"]) if row else None

    def audit(self, selection_id: str, *, candidate_id: str) -> DecisionAudit | None:
        row = self._row(selection_id, candidate_id)
        if row is None:
            return None
        return DecisionAudit(
            outcome=SelectionOutcome.model_validate_json(row["record_json"]),
            job_snapshot=json.loads(row["job_snapshot_json"]),
            candidate_snapshot=json.loads(row["candidate_snapshot_json"]),
            preferences=json.loads(row["preferences_json"]),
            requests=json.loads(row["requests_json"]),
            responses=json.loads(row["responses_json"]),
        )

    def latest(self, listing_id: str, *, candidate_id: str) -> SelectionOutcome | None:
        """This candidate's latest decision, loading only one record."""
        row = self._conn.execute(
            "SELECT record_json FROM selection_decisions "
            "WHERE candidate_id = ? AND listing_id = ? "
            "ORDER BY decided_at DESC, rowid DESC LIMIT 1",
            (candidate_id, listing_id),
        ).fetchone()
        return SelectionOutcome.model_validate_json(row[0]) if row else None

    def latest_many(
        self, listing_ids: Iterable[str], *, candidate_id: str
    ) -> dict[str, SelectionOutcome]:
        """Latest outcomes keyed by listing id; absent ids are omitted.

        Chunked to 400 unique ids per query. Each indexed correlated lookup reads
        only the latest row, rather than materializing a candidate's whole history.
        """
        ids = list(dict.fromkeys(listing_ids))
        outcomes: dict[str, SelectionOutcome] = {}
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            values = ",".join("(?)" for _ in chunk)
            rows = self._conn.execute(
                f"WITH requested(listing_id) AS (VALUES {values}) "
                "SELECT d.listing_id, d.record_json FROM requested r "
                "JOIN selection_decisions d ON d.rowid = ("
                "SELECT s.rowid FROM selection_decisions s "
                "WHERE s.candidate_id = ? AND s.listing_id = r.listing_id "
                "ORDER BY s.decided_at DESC, s.rowid DESC LIMIT 1)",
                (*chunk, candidate_id),
            ).fetchall()
            outcomes.update(
                (row[0], SelectionOutcome.model_validate_json(row[1])) for row in rows
            )
        return outcomes

    def history(self, listing_id: str, *, candidate_id: str) -> list[SelectionOutcome]:
        """This candidate's decisions about the listing, oldest first."""
        rows = self._conn.execute(
            "SELECT record_json FROM selection_decisions "
            "WHERE listing_id = ? AND candidate_id = ? ORDER BY decided_at, rowid",
            (listing_id, candidate_id),
        ).fetchall()
        return [SelectionOutcome.model_validate_json(r[0]) for r in rows]

    def find_cached(
        self, listing_id: str, candidate_id: str, cache_key: str
    ) -> SelectionOutcome | None:
        """This candidate's most recent complete Jev decision for the listing on exactly
        these inputs. Never returns another candidate's decision."""
        row = self._conn.execute(
            "SELECT record_json FROM selection_decisions "
            "WHERE listing_id = ? AND candidate_id = ? AND cache_key = ? AND cacheable = 1 "
            "ORDER BY decided_at DESC, rowid DESC LIMIT 1",
            (listing_id, candidate_id, cache_key),
        ).fetchone()
        return SelectionOutcome.model_validate_json(row[0]) if row else None
