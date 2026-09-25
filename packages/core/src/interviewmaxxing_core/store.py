"""SQLite application store: requests, jobs, applications, events and submissions.

Guarantees (each enforced inside one ``BEGIN IMMEDIATE`` transaction):

* One application per (candidate, canonical job) — a UNIQUE constraint. Repeating a
  request returns the existing application with a ``RequestDisposition``.
* Every state change writes its event in the same transaction; events are
  append-only (database triggers reject UPDATE/DELETE).
* Work on an application requires an exclusive, expiring ``Claim``.
* ``begin_submission`` durably records ``SUBMITTING`` and an open attempt *before*
  the caller dispatches the submit action; at most one attempt can be open.
* ``SUBMITTED`` requires an ACCEPTED observation (or reconciliation) and is final
  (a trigger rejects any change away from it).
* ``SUBMITTING``/``SUBMISSION_UNKNOWN``/``SUBMITTED`` cannot be retried. An attempt
  whose owner disappears (lease expired) becomes ``SUBMISSION_UNKNOWN`` and stays so
  until ``reconcile_submission`` establishes acceptance or definite non-submission.
* Job URL aliases are merged only by binding an observed ATS identity.
* A preparation-only application (``require_preparation_only``) is submitted only
  through review → approve → authorize: ``approve_submission`` approves the packet of
  the preparation behind the current stop, ``authorize_submission`` lifts the
  restriction for exactly that packet, and ``begin_submission`` (and an SQL trigger)
  accept only an attempt for the authorized packet. A new preparation, an invalidated
  approval or a later preparation-only run restores the restriction.

A ``ApplicationStore`` wraps one connection; use one instance per thread/process.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

from pydantic import Field
from pydantic_core import to_jsonable_python

from ._base import Contract, new_id, utc_now
from .applications import (
    GUARDED_TRANSITIONS,
    PRE_SUBMISSION_STATES,
    SUBMISSION_BLOCKING_STATES,
    TERMINAL_STATES,
    Application,
    ApplicationEvent,
    ApplicationRequest,
    ApplicationState,
    Claim,
    Receipt,
    RequestDisposition,
    SubmissionAttempt,
    can_transition,
    disposition_for,
    event_name_for,
)
from .artifacts import EvidenceRef
from .candidate import ResumeArtifact
from .errors import (
    ClaimLost,
    ClaimUnavailable,
    IdentityConflict,
    InvalidTransition,
    NotFound,
    SubmissionBlocked,
)
from .execution import (
    ReconciliationMethod,
    SubmissionObservation,
    SubmissionOutcome,
    SubmissionReconciliation,
)
from .forms import ApplicationForm
from .jobs import JobIdentityObservation, JobRecord
from .packets import ApplicationPacket, UserInput
from .urls import normalize_application_url

S = ApplicationState
SCHEMA_VERSION = 4
DEFAULT_CLAIM_TTL = timedelta(minutes=5)
SUBMISSION_LEASE = timedelta(minutes=10)
RESERVED_EVENT_PREFIXES = (
    "application.", "job.", "submission.", "packet.", "input.", "document.",
)
PREPARATION_ONLY_EVENT = "application.preparation_only"
PREPARED_EVENT = "preparation.ready"
"""Recorded by the runner (``append_event``) when a prepare-only run reached the final
review step; its ``packet_id`` is the prepared packet an approval refers to."""
APPROVED_EVENT = "application.approved"
AUTHORIZED_EVENT = "application.submission_authorized"
APPROVAL_INVALIDATED_EVENT = "application.approval_invalidated"
_RUN_STATES = frozenset({S.INSPECTING, S.PACKET_READY, S.FILLING})
"""States a run passes through between two stops."""

_PREPARATION_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS preparation_blocks_submission
BEFORE INSERT ON submission_attempts
WHEN EXISTS (SELECT 1 FROM events WHERE application_id = NEW.application_id
             AND event = 'application.preparation_only')
 AND NOT EXISTS (
    SELECT 1 FROM events a
    WHERE a.application_id = NEW.application_id
      AND a.event = 'application.submission_authorized'
      AND a.seq = (SELECT MAX(seq) FROM events WHERE application_id = NEW.application_id
                   AND event = 'application.submission_authorized')
      AND a.seq > (SELECT MAX(seq) FROM events WHERE application_id = NEW.application_id
                   AND event = 'application.preparation_only')
      AND json_extract(a.metadata, '$.packet_id') = NEW.packet_id
      AND NEW.packet_id = (SELECT json_extract(p.metadata, '$.packet_id') FROM events p
                           WHERE p.application_id = NEW.application_id
                           AND p.event = 'preparation.ready' ORDER BY p.seq DESC LIMIT 1))
BEGIN SELECT RAISE(ABORT, 'preparation-only application cannot be submitted'); END;
"""
"""A preparation-only application takes a submission attempt only for the packet its
latest ``application.submission_authorized`` event names, when that event is newer than
every ``application.preparation_only`` event and the packet is the one the latest
``preparation.ready`` prepared (``ApplicationStore.is_preparation_only`` in SQL)."""
_TRIGGER_MARKER = "application.submission_authorized"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    application_url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    identity_key TEXT UNIQUE,
    ats_type TEXT,
    external_job_id TEXT,
    company TEXT,
    title TEXT,
    location TEXT,
    merged_into TEXT REFERENCES jobs(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_aliases (
    alias TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL CHECK (kind IN ('URL', 'ATS')),
    evidence TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    candidate_id TEXT NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    packet_id TEXT,
    submitted_at TEXT,
    failure_reason TEXT,
    duplicate_of TEXT REFERENCES applications(id),
    claim_owner TEXT,
    claim_token TEXT,
    claim_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (candidate_id, job_id)
);

CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    application_url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    selection_source TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    application_id TEXT NOT NULL REFERENCES applications(id)
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    application_id TEXT NOT NULL REFERENCES applications(id),
    event TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    actor TEXT,
    metadata TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_application ON events (application_id, seq);

CREATE TABLE IF NOT EXISTS submission_attempts (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    attempt_number INTEGER NOT NULL,
    owner TEXT NOT NULL,
    packet_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    detail TEXT,
    UNIQUE (application_id, attempt_number)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_attempt
    ON submission_attempts (application_id) WHERE finished_at IS NULL;

CREATE TABLE IF NOT EXISTS packets (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    form_step INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_inputs (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    field_id TEXT NOT NULL,
    provided_at TEXT NOT NULL,
    body TEXT NOT NULL,
    form_scope TEXT,
    field_fingerprint TEXT
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    attempt_id TEXT REFERENCES submission_attempts(id),
    captured_at TEXT NOT NULL,
    body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipts (
    application_id TEXT PRIMARY KEY REFERENCES applications(id),
    created_at TEXT NOT NULL,
    body TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS events_are_append_only_u BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_are_append_only_d BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
""" + _PREPARATION_TRIGGER + """
CREATE TRIGGER IF NOT EXISTS submitted_is_final BEFORE UPDATE OF state ON applications
WHEN OLD.state = 'SUBMITTED' AND NEW.state <> 'SUBMITTED'
BEGIN SELECT RAISE(ABORT, 'SUBMITTED is final'); END;
CREATE TABLE IF NOT EXISTS application_documents (
    application_id TEXT NOT NULL REFERENCES applications(id),
    role TEXT NOT NULL,
    pinned_at TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (application_id, role)
);
CREATE TRIGGER IF NOT EXISTS application_documents_are_final
BEFORE UPDATE ON application_documents
BEGIN SELECT RAISE(ABORT, 'pinned application documents are final'); END;
CREATE TABLE IF NOT EXISTS application_expected_job_identities (
    application_id TEXT PRIMARY KEY REFERENCES applications(id),
    identity_key TEXT NOT NULL,
    pinned_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS expected_job_identities_are_final_u
BEFORE UPDATE ON application_expected_job_identities
BEGIN SELECT RAISE(ABORT, 'expected job identities are final'); END;
CREATE TRIGGER IF NOT EXISTS expected_job_identities_are_final_d
BEFORE DELETE ON application_expected_job_identities
BEGIN SELECT RAISE(ABORT, 'expected job identities are final'); END;
CREATE TRIGGER IF NOT EXISTS receipts_are_final BEFORE UPDATE ON receipts
BEGIN SELECT RAISE(ABORT, 'receipts are final'); END;
"""


# --- results ---------------------------------------------------------------------


class RequestResult(Contract):
    request: ApplicationRequest
    application: Application
    job: JobRecord
    disposition: RequestDisposition

    @property
    def may_proceed(self) -> bool:
        """True when the caller may claim the application and work on it."""
        return self.disposition in (RequestDisposition.NEW, RequestDisposition.RESUMABLE)


class BindResult(Contract):
    job: JobRecord
    """The canonical job after binding."""
    application: Application
    """The claimed application, after binding (may now be DUPLICATE)."""
    merged_from_job_id: str | None = None
    """Set when the application's previous job was merged into ``job``."""
    duplicate_of: str | None = None
    """Set when the claimed application became DUPLICATE; the surviving application."""
    moved_application_ids: list[str] = Field(default_factory=list)


class ApprovedStep(Contract):
    """One form step of an approved application and the packet that fills it."""

    form_step: int = Field(ge=0)
    packet_id: str


class SubmissionApproval(Contract):
    """The user's approval of an application's prepared packet (``approve_submission``),
    while it is still valid: it names the latest preparation and no
    ``application.approval_invalidated`` event followed it."""

    application_id: str
    event_id: str
    """The ``application.approved`` event."""
    packet_id: str
    """The final step's prepared packet (``preparation.ready`` ``packet_id``)."""
    approver: str
    approved_at: datetime
    preparation_event_id: str
    """The ``preparation.ready`` event that was approved."""
    form_step: int | None = None
    form_url: str | None = None
    steps: list[ApprovedStep] = Field(default_factory=list)
    """Every step of the preparing run with its packet, in step order; the last one is
    ``packet_id``."""


# --- helpers ---------------------------------------------------------------------


def _ts(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _json(value: Any) -> str:
    return json.dumps(to_jsonable_python(value), sort_keys=True, separators=(",", ":"))


def _create_private_file(path: Path) -> None:
    """Create the database file owner-only (0600) if it does not exist yet, so the
    application history is never world-readable (SQLite would create it with the
    umask, typically 0644; its ``-wal``/``-shm`` companions copy the file's mode).
    An existing file is left exactly as it is: nothing is ever chmod-ed."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(fd)


class ApplicationStore:
    """Local durable store. Open with ``ApplicationStore.open(path)``."""

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Callable[[], datetime] = utc_now,
        busy_timeout: float = 30.0,
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _create_private_file(self.path)
        self._conn = sqlite3.connect(str(path), timeout=busy_timeout, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout * 1000)}")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._enable_wal(busy_timeout)
        self._conn.execute("PRAGMA synchronous = FULL")
        self._migrate()

    @classmethod
    def open(cls, path: Path | str, **kwargs: Any) -> ApplicationStore:
        return cls(path, **kwargs)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- plumbing ----------------------------------------------------------------

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction holding the database write lock from the start."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def _enable_wal(self, busy_timeout: float) -> None:
        # WAL is persistent in the file, so only the first open switches it. SQLite
        # reports SQLITE_BUSY for this switch without consulting the busy handler
        # when other connections race to open a new database, so retry here.
        deadline = time.monotonic() + busy_timeout
        while True:
            try:
                self._conn.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or time.monotonic() > deadline:
                    raise
                time.sleep(0.01)

    def _migrate(self) -> None:
        # executescript manages its own transaction; the schema is idempotent and the
        # IMMEDIATE lock serializes concurrent first opens.
        self._conn.executescript(
            "BEGIN IMMEDIATE;"
            + _SCHEMA
            + "INSERT OR IGNORE INTO meta (key, value) VALUES"
            + f" ('schema_version', '{SCHEMA_VERSION}');"
            + "COMMIT;"
        )
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        version = int(row["value"])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"state database schema {version} is newer than this code "
                f"({SCHEMA_VERSION}); upgrade interviewmaxxing"
            )
        with self._tx() as c:
            # v1 -> v2: user inputs gained a form scope and question fingerprint.
            # v1 rows have neither and are never returned (the question is re-asked).
            columns = {r["name"] for r in c.execute("PRAGMA table_info(user_inputs)")}
            for column in ("form_scope", "field_fingerprint"):
                if column not in columns:
                    c.execute(f"ALTER TABLE user_inputs ADD COLUMN {column} TEXT")
            c.execute(
                "CREATE INDEX IF NOT EXISTS user_inputs_by_question"
                " ON user_inputs (application_id, form_scope, field_id)"
            )
            # The preparation trigger of a database created before approved submission
            # blocked every attempt of a preparation-only application. Its replacement
            # also admits the authorized packet, so an existing database is upgraded in
            # place. The schema version is unchanged: older code keeps refusing these
            # attempts in Python and never needs the new events.
            trigger = c.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger'"
                " AND name = 'preparation_blocks_submission'"
            ).fetchone()
            if trigger is None or _TRIGGER_MARKER not in (trigger["sql"] or ""):
                c.execute("DROP TRIGGER IF EXISTS preparation_blocks_submission")
                c.execute(_PREPARATION_TRIGGER)
            c.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version' AND CAST(value AS INTEGER) < ?",
                (str(SCHEMA_VERSION), SCHEMA_VERSION),
            )

    def _event(
        self,
        c: sqlite3.Connection,
        application_id: str,
        event: str,
        *,
        now: datetime,
        from_state: ApplicationState | None = None,
        to_state: ApplicationState | None = None,
        actor: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        c.execute(
            "INSERT INTO events (id, application_id, event, timestamp, from_state, to_state,"
            " actor, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("evt"),
                application_id,
                event,
                _ts(now),
                from_state.value if from_state else None,
                to_state.value if to_state else None,
                actor,
                _json(metadata or {}),
            ),
        )

    def _app_row(self, c: sqlite3.Connection, application_id: str) -> sqlite3.Row:
        row: sqlite3.Row | None = c.execute(
            "SELECT * FROM applications WHERE id = ?", (application_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"application {application_id}")
        return row

    def _job_row(self, c: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row: sqlite3.Row | None = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise NotFound(f"job {job_id}")
        return row

    def _canonical_job_id(self, c: sqlite3.Connection, job_id: str) -> str:
        seen = set()
        while True:
            row = self._job_row(c, job_id)
            if row["merged_into"] is None:
                return job_id
            if job_id in seen:  # pragma: no cover - defensive
                raise IdentityConflict(f"job merge cycle at {job_id}")
            seen.add(job_id)
            job_id = row["merged_into"]

    def _check_claim(self, c: sqlite3.Connection, claim: Claim, now: datetime) -> sqlite3.Row:
        row = self._app_row(c, claim.application_id)
        if row["claim_token"] != claim.token:
            raise ClaimLost(f"claim on {claim.application_id} is no longer held by {claim.owner}")
        expires = _dt(row["claim_expires_at"])
        if expires is None or expires <= now:
            raise ClaimLost(f"claim on {claim.application_id} expired")
        return row

    def _set_state(
        self,
        c: sqlite3.Connection,
        row: sqlite3.Row,
        dst: ApplicationState,
        *,
        now: datetime,
        actor: str | None,
        metadata: dict[str, Any] | None = None,
        failure_reason: str | None = None,
        submitted_at: datetime | None = None,
        duplicate_of: str | None = None,
    ) -> None:
        src = S(row["state"])
        if not can_transition(src, dst):
            raise InvalidTransition(f"{row['id']}: {src} -> {dst} is not allowed")
        release = dst in TERMINAL_STATES
        c.execute(
            "UPDATE applications SET state = ?, version = version + 1, updated_at = ?,"
            " failure_reason = ?, submitted_at = COALESCE(?, submitted_at),"
            " duplicate_of = COALESCE(?, duplicate_of),"
            " claim_owner = CASE WHEN ? THEN NULL ELSE claim_owner END,"
            " claim_token = CASE WHEN ? THEN NULL ELSE claim_token END,"
            " claim_expires_at = CASE WHEN ? THEN NULL ELSE claim_expires_at END"
            " WHERE id = ?",
            (
                dst.value,
                _ts(now),
                failure_reason,
                _ts(submitted_at) if submitted_at else None,
                duplicate_of,
                release,
                release,
                release,
                row["id"],
            ),
        )
        self._event(
            c,
            row["id"],
            event_name_for(dst),
            now=now,
            from_state=src,
            to_state=dst,
            actor=actor,
            metadata=metadata,
        )

    @staticmethod
    def _to_application(row: sqlite3.Row) -> Application:
        return Application(
            id=row["id"],
            request_id=row["request_id"],
            job_id=row["job_id"],
            candidate_id=row["candidate_id"],
            state=S(row["state"]),
            version=row["version"],
            packet_id=row["packet_id"],
            submitted_at=_dt(row["submitted_at"]),
            failure_reason=row["failure_reason"],
            duplicate_of=row["duplicate_of"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
            claim_owner=row["claim_owner"],
            claim_expires_at=_dt(row["claim_expires_at"]),
        )

    @staticmethod
    def _to_job(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            application_url=row["application_url"],
            normalized_url=row["normalized_url"],
            identity_key=row["identity_key"],
            ats_type=row["ats_type"],
            external_job_id=row["external_job_id"],
            company=row["company"],
            title=row["title"],
            location=row["location"],
            merged_into=row["merged_into"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    @staticmethod
    def _to_request(row: sqlite3.Row) -> ApplicationRequest:
        return ApplicationRequest(
            id=row["id"],
            candidate_id=row["candidate_id"],
            application_url=row["application_url"],
            normalized_url=row["normalized_url"],
            requested_at=_dt(row["requested_at"]),
            job_id=row["job_id"],
            application_id=row["application_id"],
        )

    @staticmethod
    def _to_attempt(row: sqlite3.Row) -> SubmissionAttempt:
        return SubmissionAttempt(
            id=row["id"],
            application_id=row["application_id"],
            attempt_number=row["attempt_number"],
            owner=row["owner"],
            packet_id=row["packet_id"],
            started_at=_dt(row["started_at"]),
            finished_at=_dt(row["finished_at"]),
            outcome=row["outcome"],
            detail=row["detail"],
        )

    # --- requests and jobs ---------------------------------------------------------

    def record_request(
        self,
        candidate_id: str,
        application_url: str,
        *,
        requested_at: datetime | None = None,
    ) -> RequestResult:
        """Record the user's request to apply at ``application_url``.

        Creates the job (keyed by its normalized URL alias) and the application on
        first request. A repeated request, including one via a URL alias already
        bound to the same job, returns the existing application with a disposition
        telling the caller whether it may proceed.
        """
        normalized = normalize_application_url(application_url)
        now = self._now()
        requested = requested_at or now
        with self._tx() as c:
            alias = c.execute(
                "SELECT job_id FROM job_aliases WHERE alias = ?", (f"url:{normalized}",)
            ).fetchone()
            if alias is not None:
                job_id = self._canonical_job_id(c, alias["job_id"])
            else:
                job_id = new_id("job")
                c.execute(
                    "INSERT INTO jobs (id, application_url, normalized_url, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (job_id, application_url.strip(), normalized, _ts(now), _ts(now)),
                )
                c.execute(
                    "INSERT INTO job_aliases (alias, job_id, kind, evidence, created_at)"
                    " VALUES (?, ?, 'URL', 'user-supplied application URL', ?)",
                    (f"url:{normalized}", job_id, _ts(now)),
                )

            request_id = new_id("req")
            app_row = c.execute(
                "SELECT * FROM applications WHERE candidate_id = ? AND job_id = ?",
                (candidate_id, job_id),
            ).fetchone()
            created = app_row is None
            if created:
                application_id = new_id("app")
                c.execute(
                    "INSERT INTO applications (id, request_id, job_id, candidate_id, state,"
                    " version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                    (
                        application_id,
                        request_id,
                        job_id,
                        candidate_id,
                        S.REQUESTED.value,
                        _ts(now),
                        _ts(now),
                    ),
                )
                self._event(
                    c,
                    application_id,
                    event_name_for(S.REQUESTED),
                    now=now,
                    to_state=S.REQUESTED,
                    metadata={
                        "request_id": request_id,
                        "application_url": application_url,
                        "selection_source": "USER_PROVIDED",
                    },
                )
                app_row = self._app_row(c, application_id)
            else:
                disposition = disposition_for(S(app_row["state"]), created=False)
                self._event(
                    c,
                    app_row["id"],
                    "application.request_repeated",
                    now=now,
                    metadata={
                        "request_id": request_id,
                        "application_url": application_url,
                        "state": app_row["state"],
                        "disposition": disposition.value,
                    },
                )
            c.execute(
                "INSERT INTO requests (id, candidate_id, application_url, normalized_url,"
                " requested_at, selection_source, job_id, application_id)"
                " VALUES (?, ?, ?, ?, ?, 'USER_PROVIDED', ?, ?)",
                (
                    request_id,
                    candidate_id,
                    application_url.strip(),
                    normalized,
                    _ts(requested),
                    job_id,
                    app_row["id"],
                ),
            )
            request = self._to_request(
                c.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
            )
            application = self._to_application(app_row)
            job = self._to_job(self._job_row(c, job_id))
        return RequestResult(
            request=request,
            application=application,
            job=job,
            disposition=disposition_for(application.state, created=created),
        )

    def bind_job_identity(self, claim: Claim, observation: JobIdentityObservation) -> BindResult:
        """Bind an ATS identity observed on the page to the claimed application's job.

        * Unbound identity: recorded on the job.
        * Already bound to this job: metadata refreshed.
        * Bound to another job: the two jobs are the same. This job is merged into
          the canonical one, its URL aliases re-pointed, and its applications moved.
          Where the candidate already has an application on the canonical job, the
          pre-submission application here becomes ``DUPLICATE`` of it.

        Raises ``IdentityConflict`` when the job already has a different identity, or
        when a merge would affect an application whose submission may have started.
        """
        key = observation.identity_key
        now = self._now()
        with self._tx() as c:
            row = self._check_claim(c, claim, now)
            job_id = row["job_id"]
            expected = self.expected_job_identity(claim.application_id)
            if expected is not None and expected != key:
                raise IdentityConflict("the observed job does not match the selected job")
            job_row = self._job_row(c, job_id)
            if job_row["identity_key"] not in (None, key):
                raise IdentityConflict(
                    f"job {job_id} is bound to {job_row['identity_key']}, page shows {key}"
                )
            alias = c.execute(
                "SELECT job_id FROM job_aliases WHERE alias = ?", (key,)
            ).fetchone()
            target = self._canonical_job_id(c, alias["job_id"]) if alias else job_id
            meta = {
                "identity_key": key,
                "evidence_kind": observation.evidence_kind.value,
                "evidence": observation.evidence,
                "observed_url": observation.observed_url,
            }
            merged_from: str | None = None
            duplicate_of: str | None = None
            moved: list[str] = []

            if target != job_id:
                if S(row["state"]) not in PRE_SUBMISSION_STATES:
                    raise IdentityConflict(
                        f"{row['id']} is {row['state']}; cannot merge its job after submission began"
                    )
                merged_from = job_id
                for x in c.execute(
                    "SELECT * FROM applications WHERE job_id = ? ORDER BY created_at", (job_id,)
                ).fetchall():
                    y = c.execute(
                        "SELECT * FROM applications WHERE job_id = ? AND candidate_id = ?",
                        (target, x["candidate_id"]),
                    ).fetchone()
                    x_state = S(x["state"])
                    if y is None:
                        c.execute(
                            "UPDATE applications SET job_id = ?, version = version + 1,"
                            " updated_at = ? WHERE id = ?",
                            (target, _ts(now), x["id"]),
                        )
                        self._event(
                            c, x["id"], "job.merged", now=now, actor=claim.owner,
                            metadata={**meta, "from_job_id": job_id, "to_job_id": target},
                        )
                        moved.append(x["id"])
                    elif x_state in PRE_SUBMISSION_STATES:
                        self._set_state(
                            c, x, S.DUPLICATE, now=now, actor=claim.owner,
                            duplicate_of=y["id"],
                            metadata={**meta, "duplicate_of": y["id"], "canonical_job_id": target},
                        )
                        if x["id"] == row["id"]:
                            duplicate_of = y["id"]
                    elif x_state in SUBMISSION_BLOCKING_STATES:
                        raise IdentityConflict(
                            f"{x['id']} ({x_state}) and {y['id']} ({y['state']}) are the same "
                            "candidate and job; manual review required"
                        )
                    # FAILED_PERMANENT / WITHDRAWN / DUPLICATE history stays on the merged job.
                c.execute(
                    "UPDATE job_aliases SET job_id = ? WHERE job_id = ?", (target, job_id)
                )
                c.execute(
                    "UPDATE jobs SET merged_into = ?, updated_at = ? WHERE id = ?",
                    (target, _ts(now), job_id),
                )
            elif alias is None:
                c.execute(
                    "INSERT INTO job_aliases (alias, job_id, kind, evidence, created_at)"
                    " VALUES (?, ?, 'ATS', ?, ?)",
                    (key, job_id, observation.evidence, _ts(now)),
                )

            c.execute(
                "UPDATE jobs SET identity_key = ?, ats_type = ?, external_job_id = ?,"
                " company = COALESCE(?, company), title = COALESCE(?, title),"
                " location = COALESCE(?, location), updated_at = ? WHERE id = ?",
                (
                    key,
                    observation.ats_type,
                    observation.external_job_id,
                    observation.company,
                    observation.title,
                    observation.location,
                    _ts(now),
                    target,
                ),
            )
            if duplicate_of is None:
                self._event(
                    c, row["id"], "job.identity_bound", now=now, actor=claim.owner,
                    metadata={**meta, "job_id": target, "merged_from_job_id": merged_from},
                )
            job = self._to_job(self._job_row(c, target))
            application = self._to_application(self._app_row(c, row["id"]))
        return BindResult(
            job=job,
            application=application,
            merged_from_job_id=merged_from,
            duplicate_of=duplicate_of,
            moved_application_ids=moved,
        )

    # --- claims --------------------------------------------------------------------

    def claim(
        self, application_id: str, owner: str, *, ttl: timedelta = DEFAULT_CLAIM_TTL
    ) -> Claim:
        """Take the exclusive lease on an application.

        If the application is ``SUBMITTING`` without a live claim, its previous owner
        stopped mid-submission: it becomes ``SUBMISSION_UNKNOWN`` (never retried
        automatically) before the new claim is granted.
        """
        now = self._now()
        with self._tx() as c:
            row = self._app_row(c, application_id)
            expires = _dt(row["claim_expires_at"])
            if row["claim_token"] and expires is not None and expires > now:
                raise ClaimUnavailable(
                    f"{application_id} is claimed by {row['claim_owner']} until {row['claim_expires_at']}"
                )
            if S(row["state"]) is S.SUBMITTING:
                self._interrupt(c, row, now=now, actor=owner)
            token = uuid.uuid4().hex
            expires_at = now + ttl
            c.execute(
                "UPDATE applications SET claim_owner = ?, claim_token = ?, claim_expires_at = ?"
                " WHERE id = ?",
                (owner, token, _ts(expires_at), application_id),
            )
        return Claim(application_id=application_id, owner=owner, token=token, expires_at=expires_at)

    def renew(self, claim: Claim, *, ttl: timedelta = DEFAULT_CLAIM_TTL) -> Claim:
        now = self._now()
        with self._tx() as c:
            self._check_claim(c, claim, now)
            expires_at = now + ttl
            c.execute(
                "UPDATE applications SET claim_expires_at = ? WHERE id = ?",
                (_ts(expires_at), claim.application_id),
            )
        return claim.model_copy(update={"expires_at": expires_at})

    def release(self, claim: Claim) -> None:
        """Give up a claim. A no-op if it was already lost."""
        with self._tx() as c:
            c.execute(
                "UPDATE applications SET claim_owner = NULL, claim_token = NULL,"
                " claim_expires_at = NULL WHERE id = ? AND claim_token = ?",
                (claim.application_id, claim.token),
            )

    def _interrupt(
        self, c: sqlite3.Connection, row: sqlite3.Row, *, now: datetime, actor: str | None
    ) -> None:
        attempt = c.execute(
            "SELECT * FROM submission_attempts WHERE application_id = ? AND finished_at IS NULL",
            (row["id"],),
        ).fetchone()
        detail = "submission interrupted: owner lost its claim before observing an outcome"
        if attempt is not None:
            c.execute(
                "UPDATE submission_attempts SET finished_at = ?, outcome = 'INTERRUPTED',"
                " detail = ? WHERE id = ?",
                (_ts(now), detail, attempt["id"]),
            )
        self._set_state(
            c, row, S.SUBMISSION_UNKNOWN, now=now, actor=actor,
            failure_reason=detail,
            metadata={
                "reason": "interrupted",
                "attempt_id": attempt["id"] if attempt else None,
                "previous_owner": row["claim_owner"],
            },
        )

    def recover_interrupted_submissions(self) -> list[Application]:
        """Mark every ``SUBMITTING`` application without a live claim as
        ``SUBMISSION_UNKNOWN``. Returns the applications changed."""
        now = self._now()
        changed: list[str] = []
        with self._tx() as c:
            for row in c.execute(
                "SELECT * FROM applications WHERE state = 'SUBMITTING'"
                " AND (claim_expires_at IS NULL OR claim_expires_at <= ?)",
                (_ts(now),),
            ).fetchall():
                self._interrupt(c, row, now=now, actor="recovery")
                c.execute(
                    "UPDATE applications SET claim_owner = NULL, claim_token = NULL,"
                    " claim_expires_at = NULL WHERE id = ?",
                    (row["id"],),
                )
                changed.append(row["id"])
        return [self.get_application(i) for i in changed]

    # --- state changes -----------------------------------------------------------------

    def transition(
        self,
        claim: Claim,
        to_state: ApplicationState,
        *,
        reason: str | None = None,
        failure_reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Application:
        """Move a claimed application along the state machine and emit its event.

        Transitions into or out of SUBMITTING / SUBMISSION_UNKNOWN / SUBMITTED are
        refused here; use ``begin_submission``, ``record_submission_outcome`` and
        ``reconcile_submission``. Failure states require ``failure_reason``.
        """
        now = self._now()
        with self._tx() as c:
            row = self._check_claim(c, claim, now)
            src = S(row["state"])
            if (src, to_state) in GUARDED_TRANSITIONS or to_state in (
                S.SUBMITTING, S.SUBMITTED, S.SUBMISSION_UNKNOWN
            ):
                raise InvalidTransition(
                    f"{src} -> {to_state} must go through the submission operations"
                )
            if to_state in (S.FAILED_RETRYABLE, S.FAILED_PERMANENT) and not failure_reason:
                raise InvalidTransition(f"{to_state} requires a failure_reason")
            if to_state is S.DUPLICATE and not reason:
                raise InvalidTransition("DUPLICATE requires a reason")
            meta = dict(metadata or {})
            if reason:
                meta["reason"] = reason
            if failure_reason:
                meta["failure_reason"] = failure_reason
            self._set_state(
                c, row, to_state, now=now, actor=claim.owner, metadata=meta,
                failure_reason=failure_reason,
            )
            return self._to_application(self._app_row(c, claim.application_id))

    def append_event(
        self, claim: Claim, event: str, metadata: dict[str, Any] | None = None
    ) -> ApplicationEvent:
        """Record a non-transition event (e.g. ``form.discovered``, ``field.unresolved``,
        ``page.completed``, ``validation.failed``). Prefixes owned by the store
        (``application.``, ``job.``, ``submission.``, ``packet.``, ``input.``) are refused."""
        if event.startswith(RESERVED_EVENT_PREFIXES) or not event.strip():
            raise ValueError(f"event name {event!r} is reserved or empty")
        now = self._now()
        with self._tx() as c:
            row = self._check_claim(c, claim, now)
            self._event(c, row["id"], event, now=now, actor=claim.owner, metadata=metadata)
            seq = c.execute("SELECT last_insert_rowid() AS seq").fetchone()["seq"]
            return self._to_event(c.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone())

    def save_packet(self, claim: Claim, packet: ApplicationPacket) -> Application:
        if packet.application_id != claim.application_id:
            raise ValueError("packet belongs to a different application")
        now = self._now()
        with self._tx() as c:
            row = self._check_claim(c, claim, now)
            if packet.job_id != row["job_id"] or packet.candidate_id != row["candidate_id"]:
                raise ValueError("packet job/candidate do not match the application")
            c.execute(
                "INSERT INTO packets (id, application_id, form_step, created_at, body)"
                " VALUES (?, ?, ?, ?, ?)",
                (packet.id, packet.application_id, packet.form_step, _ts(now),
                 packet.model_dump_json()),
            )
            c.execute(
                "UPDATE applications SET packet_id = ?, version = version + 1, updated_at = ?"
                " WHERE id = ?",
                (packet.id, _ts(now), packet.application_id),
            )
            self._event(
                c, row["id"], "packet.saved", now=now, actor=claim.owner,
                metadata={
                    "packet_id": packet.id,
                    "form_step": packet.form_step,
                    "answered": len(packet.answers),
                    "missing_field_ids": packet.unresolved_fields,
                },
            )
            return self._to_application(self._app_row(c, row["id"]))

    def save_user_inputs(self, claim: Claim, inputs: Sequence[UserInput]) -> None:
        """Persist the user's answers to missing inputs so the run can resume. Each is
        stored under its form scope, field id and question fingerprint."""
        now = self._now()
        with self._tx() as c:
            row = self._check_claim(c, claim, now)
            for item in inputs:
                c.execute(
                    "INSERT INTO user_inputs (id, application_id, field_id, provided_at, body,"
                    " form_scope, field_fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (item.id, row["id"], item.field_id, _ts(item.provided_at),
                     item.model_dump_json(), item.scope.key, item.field_fingerprint),
                )
            self._event(
                c, row["id"], "input.received", now=now, actor=claim.owner,
                metadata={
                    "inputs": [
                        {"id": i.id, "form_scope": i.scope.key, "field_id": i.field_id,
                         "reuse": i.reuse.value}
                        for i in inputs
                    ]
                },
            )

    def add_evidence(
        self, claim: Claim, evidence: Sequence[EvidenceRef], *, attempt_id: str | None = None
    ) -> None:
        now = self._now()
        with self._tx() as c:
            row = self._check_claim(c, claim, now)
            self._insert_evidence(c, row["id"], evidence, attempt_id)
            self._event(
                c, row["id"], "submission.evidence_recorded" if attempt_id else "evidence.recorded",
                now=now, actor=claim.owner,
                metadata={"evidence_ids": [e.id for e in evidence], "attempt_id": attempt_id},
            )

    def _insert_evidence(
        self,
        c: sqlite3.Connection,
        application_id: str,
        evidence: Sequence[EvidenceRef],
        attempt_id: str | None,
    ) -> None:
        for e in evidence:
            c.execute(
                "INSERT OR IGNORE INTO evidence (id, application_id, attempt_id, captured_at, body)"
                " VALUES (?, ?, ?, ?, ?)",
                (e.id, application_id, attempt_id, _ts(e.captured_at), e.model_dump_json()),
            )

    # --- submission ------------------------------------------------------------------

    @staticmethod
    def _latest_event(c: sqlite3.Connection, application_id: str, event: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = c.execute(
            "SELECT * FROM events WHERE application_id = ? AND event = ? ORDER BY seq DESC LIMIT 1",
            (application_id, event),
        ).fetchone()
        return row

    @staticmethod
    def _meta(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        meta = json.loads(row["metadata"])
        return meta if isinstance(meta, dict) else {}

    @staticmethod
    def _stored_packet(c: sqlite3.Connection, packet_id: str) -> ApplicationPacket | None:
        row = c.execute("SELECT body FROM packets WHERE id = ?", (packet_id,)).fetchone()
        return ApplicationPacket.model_validate_json(row["body"]) if row else None

    def _authorized_packet(self, c: sqlite3.Connection, application_id: str) -> str | None:
        """The packet a preparation-only application may be submitted with: the packet of
        its latest ``application.submission_authorized`` event, when that event is newer
        than every ``application.preparation_only`` event and names the packet of the
        latest ``preparation.ready``. None otherwise (also when never restricted)."""
        restriction = self._latest_event(c, application_id, PREPARATION_ONLY_EVENT)
        authorization = self._latest_event(c, application_id, AUTHORIZED_EVENT)
        if restriction is None or authorization is None or authorization["seq"] < restriction["seq"]:
            return None
        packet_id = self._meta(authorization).get("packet_id")
        prepared = self._latest_event(c, application_id, PREPARED_EVENT)
        if not isinstance(packet_id, str) or self._meta(prepared).get("packet_id") != packet_id:
            return None
        return packet_id

    def is_preparation_only(self, application_id: str) -> bool:
        """True while the persisted no-submit restriction applies: the application has an
        ``application.preparation_only`` event and no authorization lifts it. Only
        ``authorize_submission`` lifts it, for the prepared packet the user approved, and
        only while that authorization is newer than every restriction and names the
        latest ``preparation.ready`` packet. Apply and resume never clear it; a new
        preparation or a later ``require_preparation_only`` restores it."""
        c = self._conn
        if self._latest_event(c, application_id, PREPARATION_ONLY_EVENT) is None:
            return False
        return self._authorized_packet(c, application_id) is None

    def require_preparation_only(self, claim: Claim) -> None:
        """Persist the user's no-submit boundary before any browser work.

        The restriction has no automatic expiry or resume override. Only an explicit
        ``authorize_submission`` of an approved packet lifts it, and calling this on an
        authorized application records it again (a prepare-only run after an approval
        never submits). It cannot retract a submission which has already started.
        """
        now = self._now()
        with self._tx() as c:
            row = self._check_claim(c, claim, now)
            if S(row["state"]) not in PRE_SUBMISSION_STATES:
                raise SubmissionBlocked("cannot prepare an application after submission or closure")
            if not self.is_preparation_only(row["id"]):
                self._event(c, row["id"], PREPARATION_ONLY_EVENT, now=now,
                            actor=claim.owner, metadata={"submission_authorized": False})

    # --- approval ----------------------------------------------------------------------

    def _prepared_stop(self, c: sqlite3.Connection, application_id: str) -> sqlite3.Row | None:
        """The latest ``preparation.ready`` when it is behind the application's current
        stop: after it come only the run's own re-inspection (INSPECTING) and the stop
        (NEEDS_INPUT), so a later run (questions, sign-in, a failure) never counts."""
        prepared = self._latest_event(c, application_id, PREPARED_EVENT)
        if prepared is None:
            return None
        later = [S(r["to_state"]) for r in c.execute(
            "SELECT to_state FROM events WHERE application_id = ? AND seq > ?"
            " AND to_state IS NOT NULL ORDER BY seq", (application_id, prepared["seq"]),
        )]
        if not later or later[-1] is not S.NEEDS_INPUT or any(s is not S.INSPECTING for s in later[:-1]):
            return None
        return prepared

    def _prepared_steps(self, c: sqlite3.Connection, application_id: str,
                        prepared: sqlite3.Row) -> list[ApprovedStep]:
        """Each step's packet of the preparing run: the ``steps`` the runner recorded
        with ``preparation.ready``, else (older preparations) the latest packet saved per
        step after the previous stop. The final step always takes the prepared packet."""
        meta = self._meta(prepared)
        latest: dict[int, str] = {}
        recorded = meta.get("steps")
        if isinstance(recorded, list) and recorded:
            for item in recorded:
                if (isinstance(item, dict) and isinstance(item.get("form_step"), int)
                        and isinstance(item.get("packet_id"), str)):
                    latest[item["form_step"]] = item["packet_id"]
        else:
            start = c.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM events WHERE application_id = ?"
                " AND seq < ? AND to_state IS NOT NULL AND to_state NOT IN (?, ?, ?)",
                (application_id, prepared["seq"], *(s.value for s in sorted(_RUN_STATES))),
            ).fetchone()["seq"]
            for r in c.execute(
                "SELECT metadata FROM events WHERE application_id = ? AND event = 'packet.saved'"
                " AND seq > ? AND seq < ? ORDER BY seq", (application_id, start, prepared["seq"]),
            ):
                saved = json.loads(r["metadata"])
                if isinstance(saved.get("form_step"), int) and isinstance(saved.get("packet_id"), str):
                    latest[saved["form_step"]] = saved["packet_id"]
        final_step, final_packet = meta.get("form_step"), meta.get("packet_id")
        if isinstance(final_step, int) and isinstance(final_packet, str):
            latest = {step: pid for step, pid in latest.items() if step < final_step}
            latest[final_step] = final_packet
        return [ApprovedStep(form_step=step, packet_id=pid) for step, pid in sorted(latest.items())]

    def _approval_row(self, c: sqlite3.Connection, application_id: str) -> sqlite3.Row | None:
        """The latest ``application.approved`` event while it is valid: it approved the
        latest ``preparation.ready`` and no ``application.approval_invalidated`` followed."""
        approved = self._latest_event(c, application_id, APPROVED_EVENT)
        if approved is None:
            return None
        prepared = self._latest_event(c, application_id, PREPARED_EVENT)
        if prepared is None or prepared["id"] != self._meta(approved).get("preparation_event_id"):
            return None
        if c.execute(
            "SELECT 1 FROM events WHERE application_id = ? AND event = ? AND seq > ? LIMIT 1",
            (application_id, APPROVAL_INVALIDATED_EVENT, approved["seq"]),
        ).fetchone():
            return None
        return approved

    def _to_approval(self, row: sqlite3.Row) -> SubmissionApproval:
        meta = self._meta(row)
        return SubmissionApproval(
            application_id=row["application_id"],
            event_id=row["id"],
            packet_id=meta["packet_id"],
            approver=str(meta.get("approver") or ""),
            approved_at=_dt(row["timestamp"]),
            preparation_event_id=meta["preparation_event_id"],
            form_step=meta.get("form_step"),
            form_url=meta.get("form_url"),
            steps=[ApprovedStep.model_validate(step) for step in meta.get("steps") or []],
        )

    def prepared_packet(self, application_id: str) -> str | None:
        """The packet of the preparation behind the application's current stop (what
        ``approve_submission`` approves by default), or None when the application is not
        stopped at a completed preparation."""
        c = self._conn
        if S(self._app_row(c, application_id)["state"]) is not S.NEEDS_INPUT:
            return None
        packet_id = self._meta(self._prepared_stop(c, application_id)).get("packet_id")
        return packet_id if isinstance(packet_id, str) else None

    def approve_submission(self, claim: Claim, *, packet_id: str,
                           approver: str) -> SubmissionApproval:
        """Record the user's approval of the prepared packet (``application.approved``;
        metadata: ``packet_id``, ``approver``, ``form_step``, ``form_url``,
        ``form_fingerprint``, ``preparation_event_id``, ``steps``, ``captcha_pending``).

        Allowed only when the application is NEEDS_INPUT, stopped right after its latest
        ``preparation.ready`` (``prepared_packet``), ``packet_id`` is that preparation's
        packet, and the packet of every prepared step exists and is complete. Anything
        else raises ``SubmissionBlocked``. Approving the approved packet again returns
        the existing approval. Approval alone never lifts the no-submit restriction."""
        approver = approver.strip()
        if not approver:
            raise ValueError("approver must not be empty")
        now = self._now()
        with self._tx() as c:
            row = self._check_claim(c, claim, now)
            state = S(row["state"])
            if state is not S.NEEDS_INPUT:
                raise SubmissionBlocked(
                    f"{row['id']} is {state}; only an application prepared to its final review "
                    "step (NEEDS_INPUT) can be approved")
            prepared = self._prepared_stop(c, row["id"])
            if prepared is None:
                raise SubmissionBlocked(
                    f"{row['id']} is not stopped at a completed preparation; prepare it again "
                    "and review it before approving")
            meta = self._meta(prepared)
            if meta.get("packet_id") != packet_id:
                raise SubmissionBlocked(
                    f"packet {packet_id} is not the prepared packet of {row['id']} "
                    f"({meta.get('packet_id')})")
            steps = self._prepared_steps(c, row["id"], prepared)
            for step in steps:
                packet = self._stored_packet(c, step.packet_id)
                if (packet is None or packet.application_id != row["id"]
                        or packet.form_step != step.form_step):
                    raise SubmissionBlocked(
                        f"the prepared packet of step {step.form_step} ({step.packet_id}) is missing")
                if not packet.is_complete:
                    raise SubmissionBlocked(
                        f"the prepared packet of step {step.form_step} still has open required questions")
            existing = self._approval_row(c, row["id"])
            if existing is not None and self._meta(existing).get("packet_id") == packet_id:
                return self._to_approval(existing)
            self._event(c, row["id"], APPROVED_EVENT, now=now, actor=claim.owner, metadata={
                "packet_id": packet_id,
                "approver": approver,
                "form_step": meta.get("form_step"),
                "form_url": meta.get("form_url"),
                "form_fingerprint": meta.get("form_fingerprint"),
                "preparation_event_id": prepared["id"],
                "steps": [step.model_dump() for step in steps],
                "captcha_pending": meta.get("captcha_pending") is True,
            })
            approved = self._latest_event(c, row["id"], APPROVED_EVENT)
            assert approved is not None
            return self._to_approval(approved)

    def submission_approval(self, application_id: str) -> SubmissionApproval | None:
        """The application's valid approval: its latest ``application.approved`` names
        the latest ``preparation.ready`` and was not invalidated. None otherwise (never
        approved, prepared again since, or invalidated)."""
        row = self._approval_row(self._conn, application_id)
        return self._to_approval(row) if row is not None else None

    def approved_packet(self, application_id: str) -> str | None:
        """The approved packet id (``submission_approval(...).packet_id``), or None."""
        approval = self.submission_approval(application_id)
        return approval.packet_id if approval is not None else None

    def authorize_submission(self, claim: Claim) -> SubmissionApproval:
        """Lift the no-submit restriction for exactly the approved packet: records
        ``application.submission_authorized`` (metadata: ``packet_id``,
        ``approval_event_id``, ``approver``). Requires a pre-submission state and a valid
        approval of the latest prepared packet (``SubmissionBlocked`` otherwise). The
        authorization lapses when the application is prepared again, when a
        prepare-only run records the restriction again, or when the approval is
        invalidated (``invalidate_approval``)."""
        now = self._now()
        with self._tx() as c:
            row = self._check_claim(c, claim, now)
            state = S(row["state"])
            if state not in PRE_SUBMISSION_STATES:
                raise SubmissionBlocked(f"{row['id']} is {state}; it cannot be submitted")
            approved = self._approval_row(c, row["id"])
            if approved is None:
                raise SubmissionBlocked(
                    f"{row['id']} has no valid approval of its prepared packet; review and "
                    "approve it first")
            approval = self._to_approval(approved)
            self._event(c, row["id"], AUTHORIZED_EVENT, now=now, actor=claim.owner, metadata={
                "packet_id": approval.packet_id,
                "approval_event_id": approval.event_id,
                "approver": approval.approver,
            })
            return approval

    def submission_authorization(self, application_id: str) -> SubmissionApproval | None:
        """The approval a submission run may submit now: the application is authorized
        (``is_preparation_only`` is False because of an authorization) for the packet of
        its valid approval. None for an unauthorized or never-restricted application."""
        packet_id = self._authorized_packet(self._conn, application_id)
        if packet_id is None:
            return None
        approval = self.submission_approval(application_id)
        return approval if approval is not None and approval.packet_id == packet_id else None

    def invalidate_approval(self, claim: Claim, *, reason: str,
                            details: Sequence[str] = ()) -> None:
        """Withdraw the application's approval (``application.approval_invalidated``;
        metadata: ``packet_id``, ``approval_event_id``, ``reason``, ``details``) and, when
        it was authorized, record ``application.preparation_only`` again, so the
        restriction is back until the application is prepared and approved again. A
        no-op without an approval or authorization."""
        now = self._now()
        with self._tx() as c:
            row = self._check_claim(c, claim, now)
            approved = self._approval_row(c, row["id"])
            authorized = self._authorized_packet(c, row["id"]) is not None
            if approved is None and not authorized:
                return
            self._event(c, row["id"], APPROVAL_INVALIDATED_EVENT, now=now, actor=claim.owner,
                        metadata={
                            "packet_id": self._meta(approved).get("packet_id"),
                            "approval_event_id": approved["id"] if approved is not None else None,
                            "reason": reason,
                            "details": [str(d)[:300] for d in details][:20],
                        })
            if authorized:
                self._event(c, row["id"], PREPARATION_ONLY_EVENT, now=now, actor=claim.owner,
                            metadata={"submission_authorized": False,
                                      "reason": "approval invalidated"})

    def list_approved(self, *, candidate_id: str | None = None) -> list[Application]:
        """Applications with a valid approval that nothing has been dispatched for yet
        (a pre-submission state), oldest first."""
        sql = ("SELECT * FROM applications WHERE id IN"
               " (SELECT application_id FROM events WHERE event = ?)")
        args: list[Any] = [APPROVED_EVENT]
        if candidate_id is not None:
            sql += " AND candidate_id = ?"
            args.append(candidate_id)
        sql += " ORDER BY created_at, rowid"
        return [
            app for app in (self._to_application(r) for r in self._conn.execute(sql, args).fetchall())
            if app.state in PRE_SUBMISSION_STATES and self.submission_approval(app.id) is not None
        ]

    def begin_submission(
        self, claim: Claim, *, packet_id: str | None = None, lease: timedelta = SUBMISSION_LEASE
    ) -> SubmissionAttempt:
        """Durably record SUBMITTING and an open attempt. Call immediately *before*
        dispatching the final submit action, and only if this returns.

        The claim is extended to at least ``lease`` so the outcome can be recorded.
        Raises ``SubmissionBlocked`` if a submission may already have happened, if the
        application is preparation-only, or if an authorized (once preparation-only)
        application would be submitted with another packet than the approved one.
        """
        now = self._now()
        with self._tx() as c:
            row = self._check_claim(c, claim, now)
            if self.is_preparation_only(row["id"]):
                raise SubmissionBlocked("preparation-only application cannot be submitted")
            if self._latest_event(c, row["id"], PREPARATION_ONLY_EVENT) is not None:
                approved = self._authorized_packet(c, row["id"])
                if packet_id != approved:
                    raise SubmissionBlocked(
                        f"only the approved packet {approved} may be submitted, not {packet_id}")
            state = S(row["state"])
            if state in SUBMISSION_BLOCKING_STATES:
                raise SubmissionBlocked(f"{row['id']} is {state}; it must not be submitted again")
            if state is not S.FILLING:
                raise InvalidTransition(f"submission can only start from FILLING, not {state}")
            if c.execute(
                "SELECT 1 FROM submission_attempts WHERE application_id = ? AND finished_at IS NULL",
                (row["id"],),
            ).fetchone():
                raise SubmissionBlocked(f"{row['id']} already has an open submission attempt")
            number = c.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS n FROM submission_attempts"
                " WHERE application_id = ?",
                (row["id"],),
            ).fetchone()["n"]
            attempt_id = new_id("sub")
            try:
                c.execute(
                    "INSERT INTO submission_attempts (id, application_id, attempt_number, owner,"
                    " packet_id, started_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (attempt_id, row["id"], number, claim.owner, packet_id, _ts(now)),
                )
            except sqlite3.IntegrityError as exc:
                if "preparation-only" not in str(exc):
                    raise
                raise SubmissionBlocked("preparation-only application cannot be submitted") from exc
            self._set_state(
                c, row, S.SUBMITTING, now=now, actor=claim.owner,
                metadata={"attempt_id": attempt_id, "attempt_number": number,
                          "packet_id": packet_id},
            )
            current = _dt(row["claim_expires_at"])
            if current is None or current < now + lease:
                c.execute(
                    "UPDATE applications SET claim_expires_at = ? WHERE id = ?",
                    (_ts(now + lease), row["id"]),
                )
            return self._to_attempt(
                c.execute("SELECT * FROM submission_attempts WHERE id = ?", (attempt_id,)).fetchone()
            )

    def record_submission_outcome(
        self, claim: Claim, attempt_id: str, observation: SubmissionObservation
    ) -> Application:
        """Record what was observed after the submit action.

        ACCEPTED -> SUBMITTED with a receipt; NOT_SUBMITTED -> ``observation.next_state``;
        UNKNOWN -> SUBMISSION_UNKNOWN.
        """
        now = self._now()
        with self._tx() as c:
            row = self._check_claim(c, claim, now)
            attempt = c.execute(
                "SELECT * FROM submission_attempts WHERE id = ? AND application_id = ?",
                (attempt_id, row["id"]),
            ).fetchone()
            if attempt is None:
                raise NotFound(f"submission attempt {attempt_id}")
            if attempt["finished_at"] is not None or S(row["state"]) is not S.SUBMITTING:
                raise InvalidTransition(f"attempt {attempt_id} is not in progress")
            self._insert_evidence(c, row["id"], observation.evidence, attempt_id)
            c.execute(
                "UPDATE submission_attempts SET finished_at = ?, outcome = ?, detail = ?"
                " WHERE id = ?",
                (_ts(now), observation.outcome.value, observation.detail, attempt_id),
            )
            meta = {
                "attempt_id": attempt_id,
                "outcome": observation.outcome.value,
                "signals": observation.signals,
                "validation_errors": observation.validation_errors,
                "confirmation_reference": observation.confirmation_reference,
                "observed_url": observation.observed_url,
                "evidence_ids": [e.id for e in observation.evidence],
            }
            if observation.outcome is SubmissionOutcome.ACCEPTED:
                started = _dt(attempt["started_at"])
                assert started is not None
                self._set_state(
                    c, row, S.SUBMITTED, now=now, actor=claim.owner, metadata=meta,
                    submitted_at=started,
                )
                self._write_receipt(
                    c, row, attempt_id=attempt_id, submitted_at=started,
                    confirmed_at=observation.observed_at,
                    reference=observation.confirmation_reference,
                    signals=observation.signals, method=None,
                )
            elif observation.outcome is SubmissionOutcome.NOT_SUBMITTED:
                assert observation.next_state is not None
                dst = S(observation.next_state.value)
                failure = None
                if dst in (S.FAILED_RETRYABLE, S.FAILED_PERMANENT):
                    failure = observation.detail or "; ".join(
                        observation.validation_errors or observation.signals
                    )
                self._set_state(
                    c, row, dst, now=now, actor=claim.owner, metadata=meta, failure_reason=failure
                )
            else:
                self._set_state(
                    c, row, S.SUBMISSION_UNKNOWN, now=now, actor=claim.owner, metadata=meta,
                    failure_reason=observation.detail or "submission outcome not observed",
                )
            return self._to_application(self._app_row(c, row["id"]))

    def reconcile_submission(
        self, claim: Claim, reconciliation: SubmissionReconciliation
    ) -> Application:
        """Resolve ``SUBMISSION_UNKNOWN``: ACCEPTED -> SUBMITTED (with receipt),
        NOT_SUBMITTED -> FAILED_RETRYABLE (after which a retry is allowed)."""
        now = self._now()
        with self._tx() as c:
            row = self._check_claim(c, claim, now)
            if S(row["state"]) is not S.SUBMISSION_UNKNOWN:
                raise InvalidTransition(f"{row['id']} is {row['state']}, not SUBMISSION_UNKNOWN")
            attempt = c.execute(
                "SELECT * FROM submission_attempts WHERE application_id = ?"
                " ORDER BY attempt_number DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            if attempt is None:  # pragma: no cover - SUBMISSION_UNKNOWN implies an attempt
                raise NotFound(f"no submission attempt for {row['id']}")
            self._insert_evidence(c, row["id"], reconciliation.evidence, attempt["id"])
            meta = {
                "attempt_id": attempt["id"],
                "reconciliation": reconciliation.method.value,
                "outcome": reconciliation.outcome.value,
                "detail": reconciliation.detail,
                "confirmation_reference": reconciliation.confirmation_reference,
                "evidence_ids": [e.id for e in reconciliation.evidence],
            }
            if reconciliation.outcome is SubmissionOutcome.ACCEPTED:
                started = _dt(attempt["started_at"])
                assert started is not None
                self._set_state(
                    c, row, S.SUBMITTED, now=now, actor=claim.owner, metadata=meta,
                    submitted_at=started,
                )
                self._write_receipt(
                    c, row, attempt_id=attempt["id"], submitted_at=started,
                    confirmed_at=reconciliation.reconciled_at,
                    reference=reconciliation.confirmation_reference,
                    signals=[reconciliation.detail], method=reconciliation.method,
                )
            else:
                self._set_state(
                    c, row, S.FAILED_RETRYABLE, now=now, actor=claim.owner, metadata=meta,
                    failure_reason=f"not submitted (reconciled): {reconciliation.detail}",
                )
            return self._to_application(self._app_row(c, row["id"]))

    def _write_receipt(
        self,
        c: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        attempt_id: str,
        submitted_at: datetime,
        confirmed_at: datetime,
        reference: str | None,
        signals: list[str],
        method: ReconciliationMethod | None,
    ) -> None:
        job = self._job_row(c, row["job_id"])
        request = c.execute(
            "SELECT application_url FROM requests WHERE id = ?", (row["request_id"],)
        ).fetchone()
        evidence = [
            EvidenceRef.model_validate_json(e["body"])
            for e in c.execute(
                "SELECT body FROM evidence WHERE application_id = ? ORDER BY captured_at",
                (row["id"],),
            ).fetchall()
        ]
        receipt = Receipt(
            application_id=row["id"],
            candidate_id=row["candidate_id"],
            job_id=job["id"],
            application_url=request["application_url"] if request else job["application_url"],
            company=job["company"],
            title=job["title"],
            ats_type=job["ats_type"],
            external_job_id=job["external_job_id"],
            submitted_at=submitted_at,
            confirmed_at=confirmed_at,
            confirmation_reference=reference,
            signals=signals,
            reconciliation_method=method,
            evidence=evidence,
            attempt_id=attempt_id,
        )
        c.execute(
            "INSERT INTO receipts (application_id, created_at, body) VALUES (?, ?, ?)",
            (row["id"], _ts(self._now()), receipt.model_dump_json()),
        )

    # --- queries -----------------------------------------------------------------------

    def get_application(self, application_id: str) -> Application:
        return self._to_application(self._app_row(self._conn, application_id))

    def get_job(self, job_id: str) -> JobRecord:
        return self._to_job(self._job_row(self._conn, job_id))

    def find_application(self, candidate_id: str, application_url: str) -> Application | None:
        """The candidate's application for the job this URL is an alias of, if any."""
        alias = self._conn.execute(
            "SELECT job_id FROM job_aliases WHERE alias = ?",
            (f"url:{normalize_application_url(application_url)}",),
        ).fetchone()
        if alias is None:
            return None
        job_id = self._canonical_job_id(self._conn, alias["job_id"])
        row = self._conn.execute(
            "SELECT * FROM applications WHERE candidate_id = ? AND job_id = ?",
            (candidate_id, job_id),
        ).fetchone()
        return self._to_application(row) if row else None

    def list_applications(
        self,
        *,
        candidate_id: str | None = None,
        states: Sequence[ApplicationState] | None = None,
    ) -> list[Application]:
        sql = "SELECT * FROM applications WHERE 1 = 1"
        args: list[Any] = []
        if candidate_id is not None:
            sql += " AND candidate_id = ?"
            args.append(candidate_id)
        if states:
            sql += f" AND state IN ({', '.join('?' for _ in states)})"
            args.extend(s.value for s in states)
        sql += " ORDER BY created_at, rowid"
        return [self._to_application(r) for r in self._conn.execute(sql, args).fetchall()]

    def list_requests(self, application_id: str) -> list[ApplicationRequest]:
        return [
            self._to_request(r)
            for r in self._conn.execute(
                "SELECT * FROM requests WHERE application_id = ? ORDER BY requested_at, rowid",
                (application_id,),
            ).fetchall()
        ]

    @staticmethod
    def _to_event(row: sqlite3.Row) -> ApplicationEvent:
        return ApplicationEvent(
            id=row["id"],
            sequence=row["seq"],
            application_id=row["application_id"],
            event=row["event"],
            timestamp=_dt(row["timestamp"]),
            from_state=S(row["from_state"]) if row["from_state"] else None,
            to_state=S(row["to_state"]) if row["to_state"] else None,
            actor=row["actor"],
            metadata=json.loads(row["metadata"]),
        )

    def list_events(self, application_id: str) -> list[ApplicationEvent]:
        return [
            self._to_event(r)
            for r in self._conn.execute(
                "SELECT * FROM events WHERE application_id = ? ORDER BY seq", (application_id,)
            ).fetchall()
        ]

    def list_attempts(self, application_id: str) -> list[SubmissionAttempt]:
        return [
            self._to_attempt(r)
            for r in self._conn.execute(
                "SELECT * FROM submission_attempts WHERE application_id = ?"
                " ORDER BY attempt_number",
                (application_id,),
            ).fetchall()
        ]

    def get_packet(self, packet_id: str) -> ApplicationPacket:
        row = self._conn.execute("SELECT body FROM packets WHERE id = ?", (packet_id,)).fetchone()
        if row is None:
            raise NotFound(f"packet {packet_id}")
        return ApplicationPacket.model_validate_json(row["body"])

    def latest_packet(self, application_id: str) -> ApplicationPacket | None:
        row = self._conn.execute(
            "SELECT p.body FROM packets p JOIN applications a ON a.packet_id = p.id"
            " WHERE a.id = ?",
            (application_id,),
        ).fetchone()
        return ApplicationPacket.model_validate_json(row["body"]) if row else None

    def list_user_inputs(self, application_id: str) -> list[UserInput]:
        """Effective user inputs across all form steps: the most recently saved answer
        per (form step, field id). For display; resolution uses ``get_user_inputs``."""
        latest: dict[tuple[int, str], UserInput] = {}
        for r in self._conn.execute(
            "SELECT body FROM user_inputs"
            " WHERE application_id = ? AND form_scope IS NOT NULL ORDER BY rowid",
            (application_id,),
        ).fetchall():
            item = UserInput.model_validate_json(r["body"])
            key = (item.form_step, item.field_id)
            latest.pop(key, None)
            latest[key] = item
        return list(latest.values())

    def get_user_inputs(self, application_id: str, form: ApplicationForm) -> list[UserInput]:
        """User inputs that answer questions on ``form`` as currently inspected: the
        latest answer per (step, field id) whose step and question fingerprint still
        match (``UserInput.matches``). Answers to a changed question are not returned.
        The step URL may differ (e.g. a new draft id after a restart)."""
        return [item for item in self.list_user_inputs(application_id) if item.matches(form)]

    def list_evidence(self, application_id: str) -> list[EvidenceRef]:
        return [
            EvidenceRef.model_validate_json(r["body"])
            for r in self._conn.execute(
                "SELECT body FROM evidence WHERE application_id = ? ORDER BY captured_at, rowid",
                (application_id,),
            ).fetchall()
        ]

    # --- expected job identity --------------------------------------------------------------

    def pin_expected_job_identity(self, application_id: str, identity_key: str) -> str:
        """Persist the selected listing's proven ATS key before dispatch, without
        treating it as browser evidence or binding a canonical job. The first pin is
        immutable; a different pin or conflicting observed identity raises
        ``IdentityConflict``. A new pin requires a pre-submission state and no live
        claim (``SubmissionBlocked`` / ``ClaimUnavailable`` otherwise)."""
        key = identity_key.strip().lower()
        if re.fullmatch(r"ats:[^:\s]+:[^:\s]+:\S+", key) is None:
            raise ValueError("expected job identity must be an ats:type:tenant:job key")
        now = self._now()
        with self._tx() as c:
            app = self._app_row(c, application_id)
            existing = self.expected_job_identity(application_id)
            observed = self._job_row(c, app["job_id"])["identity_key"]
            if existing not in (None, key) or observed not in (None, key):
                raise IdentityConflict("the application belongs to a different selected job")
            if existing is not None:
                return existing
            if S(app["state"]) not in PRE_SUBMISSION_STATES:
                raise SubmissionBlocked("cannot select a job after submission or completion")
            expires = _dt(app["claim_expires_at"])
            if app["claim_token"] and expires is not None and expires > now:
                raise ClaimUnavailable("cannot select a job while the application is running")
            c.execute(
                "INSERT INTO application_expected_job_identities"
                " (application_id, identity_key, pinned_at) VALUES (?, ?, ?)",
                (application_id, key, _ts(now)),
            )
            self._event(c, application_id, "application.expected_job_identity_pinned", now=now,
                        metadata={"identity_key": key})
        return key

    def expected_job_identity(self, application_id: str) -> str | None:
        """The selected listing's expected key, never evidence observed by the browser."""
        row = self._conn.execute(
            "SELECT identity_key FROM application_expected_job_identities WHERE application_id = ?",
            (application_id,),
        ).fetchone()
        return str(row["identity_key"]) if row else None

    # --- application documents -------------------------------------------------------------

    def pin_resume(self, application_id: str, resume: ResumeArtifact) -> ResumeArtifact:
        """Bind the resume this application uses, once. First writer wins and the pin
        can never change (a SQL trigger rejects updates): later calls return the
        already pinned resume unchanged, whatever the candidate profile says now.

        Needs no claim, so a service can pin the resume the user selected right after
        ``record_request``. The runner pins the profile's resume on the first run of an
        application that has none, and afterwards always uses the pinned one."""
        now = self._now()
        with self._tx() as c:
            self._app_row(c, application_id)
            inserted = c.execute(
                "INSERT OR IGNORE INTO application_documents (application_id, role, pinned_at, body)"
                " VALUES (?, 'resume', ?, ?)",
                (application_id, _ts(now), resume.model_dump_json()),
            ).rowcount
            if inserted:
                self._event(c, application_id, "document.resume_pinned", now=now, metadata={
                    "resume_id": resume.id, "filename": resume.filename, "sha256": resume.sha256,
                })
        pinned = self.pinned_resume(application_id)
        assert pinned is not None
        return pinned

    def pinned_resume(self, application_id: str) -> ResumeArtifact | None:
        row = self._conn.execute(
            "SELECT body FROM application_documents WHERE application_id = ? AND role = 'resume'",
            (application_id,),
        ).fetchone()
        return ResumeArtifact.model_validate_json(row["body"]) if row else None

    def get_receipt(self, application_id: str) -> Receipt | None:
        row = self._conn.execute(
            "SELECT body FROM receipts WHERE application_id = ?", (application_id,)
        ).fetchone()
        return Receipt.model_validate_json(row["body"]) if row else None
