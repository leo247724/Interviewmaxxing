"""The service's own small durable state: saved preferences and background tasks.

Stored in ``$IMX_HOME/state/service.sqlite3`` (owner-only), separate from the
application, pipeline, job and selection databases. Nothing here is a domain model:

* ``preferences`` holds the user's canonical D0 ``SelectionPreferences`` plus the
  search-only settings that D0 keeps on ``JobSearchQuery`` (keywords, sources,
  per-source limit).
* ``tasks`` records each search or decision the service started, *before* it is
  queued, so its id is stable and survives a restart. A task that was still queued
  or running when the process stopped is marked ``INTERRUPTED`` on the next start.
  A partial unique index allows only one active task per (kind, dedupe key), which
  is how identical expensive requests are coalesced.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

TaskKind = Literal["search", "decision"]
TaskState = Literal["QUEUED", "RUNNING", "DONE", "FAILED", "INTERRUPTED"]
ACTIVE_STATES: tuple[TaskState, ...] = ("QUEUED", "RUNNING")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS preferences (
    candidate_id TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT,
    dedupe_key TEXT NOT NULL,
    state TEXT NOT NULL,
    request TEXT NOT NULL,
    progress TEXT NOT NULL DEFAULT '{}',
    result TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_one_active
    ON tasks(kind, candidate_id, dedupe_key) WHERE state IN ('QUEUED', 'RUNNING');
CREATE INDEX IF NOT EXISTS tasks_subject ON tasks(kind, candidate_id, subject, created_at);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Task:
    id: str
    candidate_id: str
    kind: TaskKind
    subject: str | None
    dedupe_key: str
    state: TaskState
    request: dict[str, Any]
    progress: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES


class ServiceState:
    """Thread-safe access to the service database (one shared connection, one lock)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        os.chmod(path, 0o600)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = FULL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
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

    # --- preferences ------------------------------------------------------------------

    def load_preferences(self, candidate_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM preferences WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
        return json.loads(row["body"]) if row else None

    def save_preferences(self, candidate_id: str, body: dict[str, Any]) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO preferences (candidate_id, body, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(candidate_id) DO UPDATE SET body = excluded.body,"
                " updated_at = excluded.updated_at",
                (candidate_id, json.dumps(body, sort_keys=True, allow_nan=False), _now()),
            )

    # --- tasks ------------------------------------------------------------------------

    @staticmethod
    def _task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            candidate_id=row["candidate_id"],
            kind=row["kind"],
            subject=row["subject"],
            dedupe_key=row["dedupe_key"],
            state=row["state"],
            request=json.loads(row["request"]),
            progress=json.loads(row["progress"]),
            result=json.loads(row["result"]) if row["result"] else None,
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def create_or_join(
        self,
        *,
        candidate_id: str,
        kind: TaskKind,
        dedupe_key: str,
        request: dict[str, Any],
        progress: dict[str, Any] | None = None,
        subject: str | None = None,
        prefix: str,
    ) -> tuple[Task, bool]:
        """The active task with this key, or a new durable QUEUED one.
        Returns ``(task, created)``."""
        with self._tx() as c:
            row = c.execute(
                "SELECT * FROM tasks WHERE kind = ? AND candidate_id = ? AND dedupe_key = ?"
                " AND state IN ('QUEUED', 'RUNNING')",
                (kind, candidate_id, dedupe_key),
            ).fetchone()
            if row is not None:
                return self._task(row), False
            task_id = f"{prefix}_{uuid.uuid4().hex}"
            now = _now()
            c.execute(
                "INSERT INTO tasks (id, candidate_id, kind, subject, dedupe_key, state, request,"
                " progress, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?, ?)",
                (task_id, candidate_id, kind, subject, dedupe_key,
                 json.dumps(request, sort_keys=True, allow_nan=False),
                 json.dumps(progress or {}, sort_keys=True, allow_nan=False), now, now),
            )
            created = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return self._task(created), True

    def active(self, candidate_id: str, kind: TaskKind) -> list[Task]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE candidate_id = ? AND kind = ?"
                " AND state IN ('QUEUED', 'RUNNING') ORDER BY created_at",
                (candidate_id, kind),
            ).fetchall()
        return [self._task(r) for r in rows]

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task(row) if row else None

    def latest(self, candidate_id: str, kind: TaskKind, subject: str | None = None) -> Task | None:
        sql = "SELECT * FROM tasks WHERE candidate_id = ? AND kind = ?"
        args: list[Any] = [candidate_id, kind]
        if subject is not None:
            sql += " AND subject = ?"
            args.append(subject)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return self._task(row) if row else None

    def update(
        self,
        task_id: str,
        *,
        state: TaskState | None = None,
        progress: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> Task:
        sets = ["updated_at = ?"]
        args: list[Any] = [_now()]
        if state is not None:
            sets.append("state = ?")
            args.append(state)
        if progress is not None:
            sets.append("progress = ?")
            args.append(json.dumps(progress, sort_keys=True, allow_nan=False))
        if result is not None:
            sets.append("result = ?")
            args.append(json.dumps(result, sort_keys=True, allow_nan=False))
        if error is not None:
            sets.append("error = ?")
            args.append(error)
        with self._tx() as c:
            c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", (*args, task_id))
            row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task(row)

    def interrupt_active(self) -> list[str]:
        """Mark tasks left QUEUED/RUNNING by a stopped process as INTERRUPTED."""
        with self._tx() as c:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM tasks WHERE state IN ('QUEUED', 'RUNNING')"
            ).fetchall()]
            c.execute(
                "UPDATE tasks SET state = 'INTERRUPTED', updated_at = ?,"
                " error = COALESCE(error, 'the service stopped before this finished')"
                " WHERE state IN ('QUEUED', 'RUNNING')",
                (_now(),),
            )
        return ids
