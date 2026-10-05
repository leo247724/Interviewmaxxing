"""Cross-process company cooldown, including fail-closed ambiguous dispatches.

Reserve before filling. Release only after a known no-dispatch result; a crash,
unknown outcome or outstanding dispatch must retain its reservation. This ledger
is independent of provider/browser implementation and never expires reservations.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class ReservationResult:
    allowed: bool
    token: str | None
    company_key: str
    reason: str | None = None
    blocked_until: datetime | None = None


def normalize_company(company: str) -> str:
    """Normalize spelling, but never infer parent companies or remove business words."""
    value = unicodedata.normalize("NFKC", company).casefold().strip()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE).replace("_", " ")
    words = value.split()
    # Deliberately exclude 'company', 'group', 'power', etc. that identify brands.
    while len(words) > 1 and words[-1] in {
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "plc",
    }:
        words.pop()
    key = " ".join(words)
    if not key:
        raise ValueError("Company must have a nonempty identity")
    return key


def _timestamp(value: datetime | str) -> float:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Application timestamps must include a timezone")
    return value.timestamp()


class CompanyCooldownGuard:
    """Shared SQLite ledger scoped by candidate profile, with a rolling cooldown."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        profile_id: str = "default",
        cooldown: timedelta = timedelta(days=7),
        aliases: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not profile_id.strip() or cooldown.total_seconds() <= 0:
            raise ValueError("A profile and positive cooldown are required")
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.profile_id = profile_id
        self.cooldown = cooldown
        self.clock = clock or (lambda: datetime.now(UTC))
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS company_aliases (
                alias TEXT PRIMARY KEY, canonical TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS company_cooldown (
                token TEXT PRIMARY KEY, profile TEXT NOT NULL, company TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','consumed')),
                applied_at REAL, source_id TEXT,
                UNIQUE(profile, source_id))""")
            db.execute("""CREATE INDEX IF NOT EXISTS company_cooldown_lookup
                ON company_cooldown(profile, company)""")
            for alias, canonical in (aliases or {}).items():
                alias_key = normalize_company(alias)
                canonical_key = self._canonical(db, normalize_company(canonical))
                if alias_key == canonical_key:
                    continue
                existing = db.execute(
                    "SELECT canonical FROM company_aliases WHERE alias=?", (alias_key,)
                ).fetchone()
                if existing and existing[0] != canonical_key:
                    raise ValueError(f"Conflicting company alias: {alias}")
                db.execute(
                    "INSERT OR IGNORE INTO company_aliases VALUES (?, ?)",
                    (alias_key, canonical_key),
                )
                db.execute(
                    "UPDATE company_aliases SET canonical=? WHERE canonical=?",
                    (canonical_key, alias_key),
                )
                db.execute(
                    "UPDATE company_cooldown SET company=? WHERE company=?",
                    (canonical_key, alias_key),
                )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _canonical(db: sqlite3.Connection, key: str) -> str:
        row = db.execute("SELECT canonical FROM company_aliases WHERE alias=?", (key,)).fetchone()
        return str(row[0]) if row else key

    def reserve(self, company: str) -> ReservationResult:
        now = _timestamp(self.clock())
        with self._transaction() as db:
            key = self._canonical(db, normalize_company(company))
            rows = db.execute(
                "SELECT status, applied_at FROM company_cooldown WHERE profile=? AND company=?",
                (self.profile_id, key),
            ).fetchall()
            if any(status == "pending" for status, _ in rows):
                return ReservationResult(False, None, key, "pending_or_ambiguous_dispatch")
            if any(at is None for _, at in rows):
                return ReservationResult(False, None, key, "unknown_historical_date")
            latest = max((float(at) for _, at in rows), default=float("-inf"))
            until = latest + self.cooldown.total_seconds()
            if until > now:
                return ReservationResult(
                    False, None, key, "company_cooldown", datetime.fromtimestamp(until, UTC)
                )
            token = uuid.uuid4().hex
            db.execute(
                "INSERT INTO company_cooldown VALUES (?, ?, ?, 'pending', NULL, NULL)",
                (token, self.profile_id, key),
            )
            return ReservationResult(True, token, key)

    def release(self, token: str) -> bool:
        """Release ONLY a verified no-dispatch reservation; never a consumed event."""
        with self._transaction() as db:
            return (
                db.execute(
                    "DELETE FROM company_cooldown WHERE token=? AND profile=? AND status='pending'",
                    (token, self.profile_id),
                ).rowcount
                == 1
            )

    def commit(self, token: str, applied_at: datetime | str | None = None) -> bool:
        """Consume a reservation once. Retries cannot extend the cooldown."""
        at = _timestamp(applied_at if applied_at is not None else self.clock())
        with self._transaction() as db:
            return (
                db.execute(
                    """UPDATE company_cooldown SET status='consumed', applied_at=?
                   WHERE token=? AND profile=? AND status='pending'""",
                    (at, token, self.profile_id),
                ).rowcount
                == 1
            )

    def record_history(
        self,
        company: str,
        applied_at: datetime | str | None,
        source_id: str,
    ) -> bool:
        """Import an application idempotently; unknown dates block indefinitely.

        ``source_id`` must identify a stable, namespaced source record. Reimporting
        an existing record does not reset its date or remove its uncertainty.
        """
        if not source_id.strip():
            raise ValueError("History needs a stable source_id")
        at = None if applied_at is None else _timestamp(applied_at)
        with self._transaction() as db:
            key = self._canonical(db, normalize_company(company))
            return (
                db.execute(
                    "INSERT OR IGNORE INTO company_cooldown VALUES (?, ?, ?, 'consumed', ?, ?)",
                    (uuid.uuid4().hex, self.profile_id, key, at, source_id),
                ).rowcount
                == 1
            )
