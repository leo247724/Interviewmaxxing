"""Private local storage of selection decisions and their evidence.

One SQLite file (default ``$IMX_HOME/selection/selection.sqlite3``, directory ``0700``,
file ``0600``). Each row keeps the decision record plus the exact job/candidate
evidence snapshots, the requests sent and the raw provider responses, so a decision
can be audited and reproduced. Never contains the API key.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from interviewmaxxing_core import LocalPaths

from .decision import SelectionOutcome

_SCHEMA = """
CREATE TABLE IF NOT EXISTS selection_decisions (
    id TEXT PRIMARY KEY,
    listing_id TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS selection_by_listing ON selection_decisions (listing_id, decided_at);
CREATE INDEX IF NOT EXISTS selection_by_cache ON selection_decisions (listing_id, cache_key);
"""


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
        self._conn.executescript(_SCHEMA)

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
        with self._tx() as c:
            c.execute(
                "INSERT INTO selection_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    outcome.selection.id,
                    outcome.selection.listing_id,
                    outcome.cache_key,
                    int(outcome.cacheable),
                    outcome.selection.effective_choice.value,
                    outcome.selection.decided_at.isoformat(),
                    outcome.model_dump_json(),
                    _dump(job_snapshot),
                    _dump(candidate_snapshot),
                    _dump(preferences),
                    _dump(requests),
                    _dump(responses),
                ),
            )

    def get(self, selection_id: str) -> SelectionOutcome | None:
        row = self._conn.execute(
            "SELECT record_json FROM selection_decisions WHERE id = ?", (selection_id,)
        ).fetchone()
        return SelectionOutcome.model_validate_json(row[0]) if row else None

    def audit(self, selection_id: str) -> DecisionAudit | None:
        row = self._conn.execute(
            "SELECT * FROM selection_decisions WHERE id = ?", (selection_id,)
        ).fetchone()
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

    def latest(self, listing_id: str) -> SelectionOutcome | None:
        history = self.history(listing_id)
        return history[-1] if history else None

    def history(self, listing_id: str) -> list[SelectionOutcome]:
        rows = self._conn.execute(
            "SELECT record_json FROM selection_decisions WHERE listing_id = ? "
            "ORDER BY decided_at, rowid",
            (listing_id,),
        ).fetchall()
        return [SelectionOutcome.model_validate_json(r[0]) for r in rows]

    def find_cached(self, listing_id: str, cache_key: str) -> SelectionOutcome | None:
        """The most recent complete Jev decision for this listing on exactly these inputs."""
        row = self._conn.execute(
            "SELECT record_json FROM selection_decisions "
            "WHERE listing_id = ? AND cache_key = ? AND cacheable = 1 "
            "ORDER BY decided_at DESC, rowid DESC LIMIT 1",
            (listing_id, cache_key),
        ).fetchone()
        return SelectionOutcome.model_validate_json(row[0]) if row else None
