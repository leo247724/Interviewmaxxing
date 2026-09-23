"""Durable, candidate-scoped pipeline storage (its own SQLite file).

The pipeline lives in ``pipeline.sqlite3`` beside the application store
(``LocalPaths.state_db.parent``), never in the application tables. Every operation
is one ``BEGIN IMMEDIATE`` transaction (WAL, ``synchronous=FULL``) and every query is
scoped to one candidate: another candidate's item id is simply not found.

Concurrent edits use optimistic revisions. ``update_item`` and ``move_item`` take the
revision the caller last saw and raise ``RevisionConflict`` (carrying the current
item) if someone else changed it first.

Imports are all-or-nothing. ``preview_import`` reports exactly what ``apply_import``
would do. Cards are keyed by (candidate, logical source, import key), so two files
never overwrite each other's cards. Re-importing the same source is idempotent. A
changed source value replaces a field only if the user has not edited that field
since the latest import; otherwise the user's edit is kept and reported. An import
key whose Company/Role changes within a source is rejected as ambiguous. Re-imports
never move a card. Every imported version is kept (``source_versions``); the first
imported values (``provenance.initial``) never change.

Schema 2 added source scoping and source versions. Opening a schema-1 database
migrates it in one transaction: existing imported cards keep their values, get a
``legacy-...`` source id (shown in their provenance) and a first source version.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Self

from pydantic import ValidationError

from interviewmaxxing_core import LocalPaths, PipelineEntry, new_id, utc_now

from .entry import to_pipeline_entry
from .fields import FIELD_NAMES, KEY_BY_NAME, TrackingFields
from .importer import ImportDocument, ParsedRow
from .lanes import DEFAULT_BOARD_LANES, BoardLanes, suggest_lane
from .models import (
    BoardView,
    ImportPreview,
    ImportProvenance,
    ImportReceipt,
    LaneView,
    NewPipelineItem,
    PipelineCard,
    PipelineItem,
    PipelineUpdate,
    RowIssue,
    RowPlan,
    SourceVersion,
    StageChange,
)

PIPELINE_DB_NAME: Final = "pipeline.sqlite3"
SCHEMA_VERSION: Final = 2
_CANDIDATE_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_META = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"

_ITEMS_TABLE = """
CREATE TABLE {name} (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    source_id TEXT,
    import_key TEXT,
    lane TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    body TEXT NOT NULL,
    UNIQUE (candidate_id, source_id, import_key),
    CHECK ((source_id IS NULL) = (import_key IS NULL))
);
"""

_SCHEMA = _ITEMS_TABLE.format(name="IF NOT EXISTS items") + """
CREATE INDEX IF NOT EXISTS items_by_candidate ON items (candidate_id, lane);
CREATE TABLE IF NOT EXISTS history (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS history_by_item ON history (candidate_id, item_id, sequence);
CREATE TRIGGER IF NOT EXISTS history_no_update BEFORE UPDATE ON history
    BEGIN SELECT RAISE(ABORT, 'pipeline history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS history_no_delete BEFORE DELETE ON history
    BEGIN SELECT RAISE(ABORT, 'pipeline history is append-only'); END;
CREATE TABLE IF NOT EXISTS source_versions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS source_versions_by_item
    ON source_versions (candidate_id, item_id, sequence);
CREATE TRIGGER IF NOT EXISTS source_versions_no_update BEFORE UPDATE ON source_versions
    BEGIN SELECT RAISE(ABORT, 'pipeline source versions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS source_versions_no_delete BEFORE DELETE ON source_versions
    BEGIN SELECT RAISE(ABORT, 'pipeline source versions are append-only'); END;
CREATE TABLE IF NOT EXISTS boards (
    candidate_id TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    body TEXT NOT NULL
);
"""
LEGACY_IMPORT_ID: Final = "legacy-v1"


class PipelineError(Exception):
    """Base class for pipeline errors."""


class ItemNotFound(PipelineError, LookupError):
    """No such item for this candidate."""


class RevisionConflict(PipelineError):
    """The item changed since the caller read it. ``current`` is the stored item."""

    def __init__(self, current: PipelineItem, expected: int) -> None:
        self.current = current
        self.expected = expected
        super().__init__(
            f"item {current.id} is at revision {current.revision}, not {expected}; "
            "reload it and apply the change again")


class LaneError(PipelineError, ValueError):
    """An unknown lane, or removing lanes that still hold cards."""


class ImportRejected(PipelineError, ValueError):
    """The import has issues (or changed since its preview); nothing was written."""

    def __init__(self, issues: list[RowIssue]) -> None:
        self.issues = issues
        first = "; ".join(
            f"row {i.row}" + (f" {i.column}" if i.column else "") + f": {i.message}"
            if i.row is not None else i.message
            for i in issues[:5])
        more = f" (and {len(issues) - 5} more)" if len(issues) > 5 else ""
        super().__init__(f"import rejected: {first}{more}")


def _identity(tracking: TrackingFields) -> tuple[str, str]:
    """Company/Role compared case- and space-insensitively."""
    return (" ".join((tracking.company or "").split()).casefold(),
            " ".join((tracking.role or "").split()).casefold())


def default_pipeline_db(paths: LocalPaths) -> Path:
    """``$IMX_HOME/state/pipeline.sqlite3`` (beside, not inside, the application store)."""
    return paths.state_db.parent / PIPELINE_DB_NAME


def _check_candidate(candidate_id: str) -> str:
    if not _CANDIDATE_ID.fullmatch(candidate_id):
        raise ValueError(f"invalid candidate id {candidate_id!r}")
    return candidate_id


def _ts(value: datetime) -> str:
    return value.isoformat()


def imported_item_id(candidate_id: str, source_id: str, import_key: str) -> str:
    """The stable id of the card created for ``import_key`` from ``source_id``."""
    digest = hashlib.sha256(
        f"{candidate_id}\x1f{source_id}\x1f{import_key}".encode()).hexdigest()
    return f"pipe_{digest[:24]}"


class PipelineStore:
    """Local pipeline repository. One instance per thread; see ``open``."""

    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], datetime]) -> None:
        self._conn = conn
        self._clock = clock

    # --- lifecycle ------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        path: Path | str,
        *,
        clock: Callable[[], datetime] = utc_now,
        busy_timeout: float = 30.0,
    ) -> PipelineStore:
        """Open (creating if needed) the pipeline database at ``path``. The directory is
        created owner-only and the database file is made owner-readable only."""
        db = Path(path).expanduser()
        db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = sqlite3.connect(db, timeout=busy_timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout * 1000)}")
        store = cls(conn, clock)
        conn.executescript(_META)
        store._migrate()
        conn.executescript(_SCHEMA)  # idempotent DDL; executescript cannot run inside _tx
        with store._tx() as c:
            row = c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                c.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(row["value"]) != SCHEMA_VERSION:
                raise PipelineError(f"unsupported pipeline schema version {row['value']}")
        for suffix in ("", "-wal", "-shm"):
            file = Path(f"{db}{suffix}")
            if file.exists():
                os.chmod(file, 0o600)
        return store

    def _migrate(self) -> None:
        """Upgrade a schema-1 database in one transaction (no-op otherwise)."""
        with self._tx() as c:
            row = c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None or int(row["value"]) != 1:
                return
            c.execute(_ITEMS_TABLE.format(name="items_v2"))
            c.execute("""CREATE TABLE IF NOT EXISTS source_versions (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL,
                item_id TEXT NOT NULL, imported_at TEXT NOT NULL, body TEXT NOT NULL)""")
            for old in c.execute("SELECT * FROM items ORDER BY rowid").fetchall():
                item = PipelineItem.model_validate_json(old["body"])  # upgrades provenance
                source_id = item.provenance.source_id if item.provenance else None
                import_key = item.provenance.import_key if item.provenance else None
                c.execute(
                    "INSERT INTO items_v2 (id, candidate_id, source_id, import_key, lane, revision,"
                    " created_at, updated_at, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (old["id"], old["candidate_id"], source_id, import_key, old["lane"],
                     old["revision"], old["created_at"], old["updated_at"],
                     item.model_dump_json()))
                if item.provenance is not None:
                    self._record_version(c, item, import_id=LEGACY_IMPORT_ID,
                                         values=item.provenance.initial,
                                         at=item.provenance.first_imported_at,
                                         document_sha256=item.provenance.document_sha256,
                                         source_row=item.provenance.source_row)
            c.execute("DROP TABLE items")
            c.execute("ALTER TABLE items_v2 RENAME TO items")
            c.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                      (str(SCHEMA_VERSION),))

    @classmethod
    def from_paths(
        cls, paths: LocalPaths, *, clock: Callable[[], datetime] = utc_now
    ) -> PipelineStore:
        return cls.open(default_pipeline_db(paths), clock=clock)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("the pipeline clock must return timezone-aware datetimes")
        return now

    # --- lanes ----------------------------------------------------------------------

    def lanes(self, candidate_id: str) -> BoardLanes:
        """The candidate's board lanes (``DEFAULT_BOARD_LANES`` until changed)."""
        _check_candidate(candidate_id)
        return self._lanes(self._conn, candidate_id)

    def _lanes(self, c: sqlite3.Connection, candidate_id: str) -> BoardLanes:
        row = c.execute("SELECT body FROM boards WHERE candidate_id = ?",
                        (candidate_id,)).fetchone()
        return BoardLanes.model_validate_json(row["body"]) if row else DEFAULT_BOARD_LANES

    def set_lanes(self, candidate_id: str, lanes: BoardLanes) -> BoardLanes:
        """Replace the candidate's lanes (rename, reorder, add, remove). Removing a
        lane that still holds cards raises ``LaneError``; move those cards first."""
        _check_candidate(candidate_id)
        now = self._now()
        with self._tx() as c:
            keep = set(lanes.ids())
            in_use = {
                r["lane"]: r["n"] for r in c.execute(
                    "SELECT lane, COUNT(*) AS n FROM items WHERE candidate_id = ? GROUP BY lane",
                    (candidate_id,))
                if r["lane"] not in keep
            }
            if in_use:
                raise LaneError(f"lanes still hold cards: {dict(sorted(in_use.items()))}")
            c.execute(
                "INSERT INTO boards (candidate_id, updated_at, body) VALUES (?, ?, ?)"
                " ON CONFLICT (candidate_id) DO UPDATE SET updated_at = excluded.updated_at,"
                " body = excluded.body",
                (candidate_id, _ts(now), lanes.model_dump_json()))
        return lanes

    # --- items ----------------------------------------------------------------------

    def list_items(self, candidate_id: str, *, lane: str | None = None) -> list[PipelineItem]:
        _check_candidate(candidate_id)
        sql = "SELECT body FROM items WHERE candidate_id = ?"
        params: list[Any] = [candidate_id]
        if lane is not None:
            sql += " AND lane = ?"
            params.append(lane)
        rows = self._conn.execute(sql + " ORDER BY created_at, rowid", params).fetchall()
        return [PipelineItem.model_validate_json(r["body"]) for r in rows]

    def get_item(self, candidate_id: str, item_id: str) -> PipelineItem:
        _check_candidate(candidate_id)
        return self._get(self._conn, candidate_id, item_id)

    def _get(self, c: sqlite3.Connection, candidate_id: str, item_id: str) -> PipelineItem:
        row = c.execute("SELECT body FROM items WHERE candidate_id = ? AND id = ?",
                        (candidate_id, item_id)).fetchone()
        if row is None:
            raise ItemNotFound(f"no pipeline item {item_id!r} for this candidate")
        return PipelineItem.model_validate_json(row["body"])

    def create_item(self, candidate_id: str, new: NewPipelineItem) -> PipelineItem:
        """Create a manual card. Its lane defaults to the suggestion for its stage and
        status wording; nothing else is filled in."""
        _check_candidate(candidate_id)
        now = self._now()
        with self._tx() as c:
            lanes = self._lanes(c, candidate_id)
            lane = new.lane or suggest_lane(new.tracking.stage, new.tracking.status, lanes).lane
            if lanes.get(lane) is None:
                raise LaneError(f"unknown lane {lane!r}; lanes are {lanes.ids()}")
            item = PipelineItem(
                id=new_id("pipe"), candidate_id=candidate_id, lane=lane,
                tracking=new.tracking, notes=new.notes, next_action_due=new.next_action_due,
                listing_id=new.listing_id, application_id=new.application_id,
                selection_id=new.selection_id, application_url=new.application_url,
                revision=1, created_at=now, updated_at=now)
            self._insert(c, item)
            self._history(c, item, kind="created", actor="user", to_lane=lane,
                          to_stage=item.tracking.stage, to_status=item.tracking.status, now=now)
        return item

    def update_item(
        self, candidate_id: str, item_id: str, update: PipelineUpdate, *, expected_revision: int
    ) -> PipelineItem:
        """Apply a partial edit. Only fields present in ``update`` change (``tracking``
        is partial too). Stage/status wording changes are added to the history."""
        _check_candidate(candidate_id)
        now = self._now()
        with self._tx() as c:
            item = self._current(c, candidate_id, item_id, expected_revision)
            changes: dict[str, Any] = {
                name: getattr(update, name) for name in update.model_fields_set
                if name != "tracking"
            }
            tracking = item.tracking
            if update.tracking is not None and update.tracking.model_fields_set:
                merged = tracking.model_dump()
                merged.update({n: getattr(update.tracking, n)
                               for n in update.tracking.model_fields_set})
                tracking = TrackingFields.model_validate(merged)
            if tracking == item.tracking and all(
                    getattr(item, k) == v for k, v in changes.items()):
                return item
            updated = PipelineItem.model_validate({
                **item.model_dump(), **changes, "tracking": tracking.model_dump(),
                "revision": item.revision + 1, "updated_at": now,
            })
            self._save(c, updated)
            if (tracking.stage, tracking.status) != (item.tracking.stage, item.tracking.status):
                self._history(c, updated, kind="stage_edited", actor="user",
                              from_stage=item.tracking.stage, to_stage=tracking.stage,
                              from_status=item.tracking.status, to_status=tracking.status,
                              now=now)
        return updated

    def move_item(
        self, candidate_id: str, item_id: str, lane: str, *, expected_revision: int,
        note: str | None = None,
    ) -> PipelineItem:
        """Move a card to another lane and record it. A move is the user's own
        tracking; it never submits, records or confirms an application."""
        _check_candidate(candidate_id)
        now = self._now()
        with self._tx() as c:
            item = self._current(c, candidate_id, item_id, expected_revision)
            lanes = self._lanes(c, candidate_id)
            if lanes.get(lane) is None:
                raise LaneError(f"unknown lane {lane!r}; lanes are {lanes.ids()}")
            if lane == item.lane:
                return item
            moved = item.model_copy(update={
                "lane": lane, "revision": item.revision + 1, "updated_at": now})
            self._save(c, moved)
            self._history(c, moved, kind="moved", actor="user", from_lane=item.lane,
                          to_lane=lane, note=note, now=now)
        return moved

    def entry(self, candidate_id: str, item_id: str) -> PipelineEntry:
        """The card as a core ``PipelineEntry`` (see ``entry.to_pipeline_entry``)."""
        return to_pipeline_entry(self.get_item(candidate_id, item_id), self.lanes(candidate_id))

    def entries(self, candidate_id: str) -> list[PipelineEntry]:
        """Every card as a core ``PipelineEntry``; raises ``EntryUnavailable`` for a
        card with neither a Role nor a listing."""
        lanes = self.lanes(candidate_id)
        return [to_pipeline_entry(i, lanes) for i in self.list_items(candidate_id)]

    def history(self, candidate_id: str, item_id: str) -> list[StageChange]:
        """The card's lane moves and stage/status edits, oldest first."""
        self.get_item(candidate_id, item_id)
        rows = self._conn.execute(
            "SELECT sequence, body FROM history WHERE candidate_id = ? AND item_id = ?"
            " ORDER BY sequence", (candidate_id, item_id)).fetchall()
        return [StageChange.model_validate_json(r["body"]).model_copy(
            update={"sequence": r["sequence"]}) for r in rows]

    def board(self, candidate_id: str) -> BoardView:
        """Cards grouped into the candidate's lanes, in lane order."""
        lanes = self.lanes(candidate_id)
        items = self.list_items(candidate_id)
        return BoardView(candidate_id=candidate_id, lanes=[
            LaneView(id=lane.id, label=lane.label, description=lane.description,
                     cards=[PipelineCard.of(i) for i in items if i.lane == lane.id])
            for lane in lanes.lanes
        ])

    def _current(
        self, c: sqlite3.Connection, candidate_id: str, item_id: str, expected_revision: int
    ) -> PipelineItem:
        item = self._get(c, candidate_id, item_id)
        if item.revision != expected_revision:
            raise RevisionConflict(item, expected_revision)
        return item

    def _insert(self, c: sqlite3.Connection, item: PipelineItem) -> None:
        provenance = item.provenance
        c.execute(
            "INSERT INTO items (id, candidate_id, source_id, import_key, lane, revision,"
            " created_at, updated_at, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item.id, item.candidate_id, provenance.source_id if provenance else None,
             provenance.import_key if provenance else None, item.lane, item.revision,
             _ts(item.created_at), _ts(item.updated_at), item.model_dump_json()))

    def _save(self, c: sqlite3.Connection, item: PipelineItem) -> None:
        cursor = c.execute(
            "UPDATE items SET lane = ?, revision = ?, updated_at = ?, body = ?"
            " WHERE id = ? AND candidate_id = ? AND revision = ?",
            (item.lane, item.revision, _ts(item.updated_at), item.model_dump_json(),
             item.id, item.candidate_id, item.revision - 1))
        if cursor.rowcount != 1:  # pragma: no cover - guarded by BEGIN IMMEDIATE
            raise PipelineError(f"item {item.id} changed concurrently")

    def _history(
        self, c: sqlite3.Connection, item: PipelineItem, *, kind: str, actor: str,
        now: datetime, **fields: str | None,
    ) -> None:
        entry = StageChange.model_validate({
            "sequence": 1, "item_id": item.id, "candidate_id": item.candidate_id,
            "kind": kind, "actor": actor, "changed_at": now, **fields})
        c.execute(
            "INSERT INTO history (candidate_id, item_id, changed_at, body) VALUES (?, ?, ?, ?)",
            (item.candidate_id, item.id, _ts(now), entry.model_dump_json()))

    def _record_version(
        self, c: sqlite3.Connection, item: PipelineItem, *, import_id: str,
        values: TrackingFields, at: datetime, document_sha256: str | None,
        source_row: int | None,
    ) -> None:
        assert item.provenance is not None
        version = SourceVersion(
            sequence=1, candidate_id=item.candidate_id, item_id=item.id,
            source_id=item.provenance.source_id, import_id=import_id,
            document_sha256=document_sha256, source_row=source_row, imported_at=at,
            values=values)
        c.execute("INSERT INTO source_versions (candidate_id, item_id, imported_at, body)"
                  " VALUES (?, ?, ?, ?)",
                  (item.candidate_id, item.id, _ts(at), version.model_dump_json()))

    def source_versions(self, candidate_id: str, item_id: str) -> list[SourceVersion]:
        """Every imported version of the card's source row, oldest first."""
        self.get_item(candidate_id, item_id)
        rows = self._conn.execute(
            "SELECT sequence, body FROM source_versions WHERE candidate_id = ? AND item_id = ?"
            " ORDER BY sequence", (candidate_id, item_id)).fetchall()
        return [SourceVersion.model_validate_json(r["body"]).model_copy(
            update={"sequence": r["sequence"]}) for r in rows]

    # --- import ---------------------------------------------------------------------

    def preview_import(self, candidate_id: str, document: ImportDocument) -> ImportPreview:
        """What ``apply_import`` would do with ``document``. Writes nothing."""
        _check_candidate(candidate_id)
        plans, issues, _ = self._plan(self._conn, candidate_id, document, self._now())
        return ImportPreview(candidate_id=candidate_id, source=document.source, rows=plans,
                             issues=[*document.issues, *issues],
                             skipped_blank_rows=document.skipped_blank_rows)

    def apply_import(
        self, candidate_id: str, document: ImportDocument, *,
        expected_document_sha256: str | None = None,
    ) -> ImportReceipt:
        """Apply a clean import in one transaction and store its receipt. Raises
        ``ImportRejected`` (writing nothing) if the document has any issue, or if it
        is not the document that was previewed (``expected_document_sha256``)."""
        _check_candidate(candidate_id)
        if expected_document_sha256 is not None and \
                expected_document_sha256 != document.source.document_sha256:
            raise ImportRejected([RowIssue(row=None, message=(
                "the file changed since it was previewed; preview it again"))])
        if document.issues:
            raise ImportRejected(list(document.issues))
        now = self._now()
        receipt_id = new_id("pimp")
        with self._tx() as c:
            plans, issues, writes = self._plan(c, candidate_id, document, now)
            if issues:
                raise ImportRejected(issues)
            for plan, before, after in writes:
                assert after.provenance is not None
                self._record_version(c, after, import_id=receipt_id,
                                     values=after.provenance.latest, at=now,
                                     document_sha256=document.source.document_sha256,
                                     source_row=plan.source_row)
                if before is None:
                    self._insert(c, after)
                    self._history(c, after, kind="imported", actor="import", to_lane=after.lane,
                                  to_stage=after.tracking.stage, to_status=after.tracking.status,
                                  now=now)
                else:
                    self._save(c, after)
                    if (before.tracking.stage, before.tracking.status) != (
                            after.tracking.stage, after.tracking.status):
                        self._history(c, after, kind="stage_edited", actor="import",
                                      from_stage=before.tracking.stage,
                                      to_stage=after.tracking.stage,
                                      from_status=before.tracking.status,
                                      to_status=after.tracking.status, now=now)
            receipt = ImportReceipt(id=receipt_id, candidate_id=candidate_id,
                                    imported_at=now, source=document.source, rows=plans)
            c.execute("INSERT INTO imports (id, candidate_id, imported_at, body)"
                      " VALUES (?, ?, ?, ?)",
                      (receipt.id, candidate_id, _ts(now), receipt.model_dump_json()))
        return receipt

    def list_imports(self, candidate_id: str) -> list[ImportReceipt]:
        _check_candidate(candidate_id)
        rows = self._conn.execute(
            "SELECT body FROM imports WHERE candidate_id = ? ORDER BY imported_at, rowid",
            (candidate_id,)).fetchall()
        return [ImportReceipt.model_validate_json(r["body"]) for r in rows]

    def _plan(
        self, c: sqlite3.Connection, candidate_id: str, document: ImportDocument, now: datetime
    ) -> tuple[list[RowPlan], list[RowIssue],
               list[tuple[RowPlan, PipelineItem | None, PipelineItem]]]:
        lanes = self._lanes(c, candidate_id)
        plans: list[RowPlan] = []
        issues: list[RowIssue] = []
        writes: list[tuple[RowPlan, PipelineItem | None, PipelineItem]] = []
        source_id = document.source.source_id
        if source_id is None:  # the document already carries an issue for this
            return plans, issues, writes
        for row in document.rows:
            stored = c.execute(
                "SELECT body FROM items WHERE candidate_id = ? AND source_id = ?"
                " AND import_key = ?", (candidate_id, source_id, row.import_key)).fetchone()
            if stored is None:
                plan, item = self._plan_create(candidate_id, row, document, lanes, now)
                plans.append(plan)
                writes.append((plan, None, item))
                continue
            existing = PipelineItem.model_validate_json(stored["body"])
            result = self._plan_merge(existing, row, document, now)
            if isinstance(result, RowIssue):
                issues.append(result)
                continue
            plan, updated = result
            plans.append(plan)
            if updated is not None:
                writes.append((plan, existing, updated))
        return plans, issues, writes

    def _provenance(
        self, row: ParsedRow, document: ImportDocument, *, first: datetime, now: datetime,
        initial: TrackingFields, suggested_lane: str, lane_rule: str | None,
    ) -> ImportProvenance:
        source = document.source
        assert source.source_id is not None
        return ImportProvenance(
            import_key=row.import_key, source_id=source.source_id, source_name=source.name,
            source_format=source.format, document_sha256=source.document_sha256,
            source_sha256=source.source_sha256, source_row=row.source_row,
            first_imported_at=first, last_imported_at=now, initial=initial,
            latest=row.tracking, suggested_lane=suggested_lane, lane_rule=lane_rule)

    def _plan_create(
        self, candidate_id: str, row: ParsedRow, document: ImportDocument, lanes: BoardLanes,
        now: datetime,
    ) -> tuple[RowPlan, PipelineItem]:
        suggestion = suggest_lane(row.tracking.stage, row.tracking.status, lanes)
        assert document.source.source_id is not None
        item = PipelineItem(
            id=imported_item_id(candidate_id, document.source.source_id, row.import_key),
            candidate_id=candidate_id, lane=suggestion.lane, tracking=row.tracking, revision=1,
            created_at=now, updated_at=now,
            provenance=self._provenance(row, document, first=now, now=now,
                                        initial=row.tracking, suggested_lane=suggestion.lane,
                                        lane_rule=suggestion.rule))
        plan = RowPlan(source_row=row.source_row, import_key=row.import_key, action="create",
                       item_id=item.id, lane=item.lane, lane_rule=suggestion.rule,
                       updated_fields=[KEY_BY_NAME[n] for n in FIELD_NAMES
                                       if row.tracking.value(n) is not None])
        return plan, item

    def _plan_merge(
        self, existing: PipelineItem, row: ParsedRow, document: ImportDocument, now: datetime
    ) -> tuple[RowPlan, PipelineItem | None] | RowIssue:
        provenance = existing.provenance
        assert provenance is not None  # rows with an import key are imported items
        base = RowPlan(source_row=row.source_row, import_key=row.import_key,
                       action="unchanged", item_id=existing.id, lane=existing.lane,
                       lane_rule=provenance.lane_rule)
        if row.tracking == provenance.latest:
            return base, None
        if _identity(row.tracking) != _identity(provenance.latest):
            return RowIssue(row=row.source_row, message=(
                "this import key belonged to a card with a different Company/Role in this "
                "source; give the row a new import key, or change the card first"))
        merged = existing.tracking.model_dump()
        updated: list[str] = []
        kept: list[str] = []
        for name in FIELD_NAMES:
            old, new, current = (provenance.latest.value(name), row.tracking.value(name),
                                 existing.tracking.value(name))
            if new == old or current == new:
                continue
            if current == old:
                merged[name] = new
                updated.append(KEY_BY_NAME[name])
            else:
                kept.append(KEY_BY_NAME[name])
        try:
            tracking = TrackingFields.model_validate(merged)
        except ValidationError as exc:
            return RowIssue(row=row.source_row, message=(
                "merging this row with your edits would be invalid ("
                + "; ".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())
                + "); edit the card or the source row"))
        new_provenance = self._provenance(
            row, document, first=provenance.first_imported_at, now=now,
            initial=provenance.initial, suggested_lane=provenance.suggested_lane,
            lane_rule=provenance.lane_rule)
        item = existing.model_copy(update={
            "tracking": tracking, "provenance": new_provenance,
            "revision": existing.revision + 1, "updated_at": now})
        plan = base.model_copy(update={"action": "update", "updated_fields": updated,
                                       "kept_manual_fields": kept})
        return plan, item
