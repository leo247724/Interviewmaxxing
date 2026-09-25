"""``GET /applications`` from one read of the canonical store.

The detail view (``PresentationService._view``) reads and validates an application's
whole history. The board lists every application on each load and needs far less: the
request, the job and whether the latest stop is a prepared one. So the list reads the
canonical store's tables directly, in one read transaction on a connection that cannot
write (``PRAGMA query_only``), with a fixed number of statements however many
applications and cards there are:

* every application of the candidate with its request and job;
* the prepared stops, found exactly as ``views.prepared_event`` walks the history: per
  NEEDS_INPUT application its latest transition, the latest transition before that
  which is not the runner's INSPECTING, and a ``preparation.ready`` between the two;
  with the ``evidence.recorded`` events of the preparing run (``views.preparing_run``);
* the evidence those runs recorded;
* the applications behind the unlinked pipeline cards' URLs, by the same URL alias and
  job merges as ``ApplicationStore.find_application``.

This reads these core tables and columns, and nothing else: ``applications`` (id,
candidate_id, request_id, job_id, state, created_at, updated_at), ``requests`` (id,
application_id, application_url, requested_at), ``jobs`` (id, title, company, ats_type,
merged_into), ``job_aliases`` (alias, job_id), ``events`` (seq, id, application_id,
event, timestamp, from_state, to_state, actor, metadata) and ``evidence`` (id,
application_id, captured_at, body). ``tests/service/test_application_list.py`` checks
every row against the detail view of the same application.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from interviewmaxxing_core import (
    ApplicationEvent,
    ApplicationState,
    EvidenceRef,
    normalize_application_url,
)

from .models import ApplicationSummaryView
from .views import (
    EVIDENCE_EVENT,
    PREPARED_EVENT,
    iso,
    job_identity_view,
    prepared_view,
    recorded_evidence_ids,
)

S = ApplicationState

_APPLICATIONS = """
SELECT a.id, a.state, a.updated_at, r.application_url, r.requested_at,
       j.title, j.company, j.ats_type
FROM applications a
JOIN requests r ON r.id = COALESCE(
    (SELECT q.id FROM requests q WHERE q.id = a.request_id AND q.application_id = a.id),
    (SELECT q.id FROM requests q WHERE q.application_id = a.id
     ORDER BY q.requested_at, q.rowid LIMIT 1))
JOIN jobs j ON j.id = a.job_id
WHERE a.candidate_id = ?
ORDER BY a.created_at, a.rowid
"""
"""Each application with the request that created it (else its first), as
``ApplicationStore.list_applications`` orders them."""

_PREPARED = f"""
WITH stops AS (
    SELECT a.id AS app_id,
           (SELECT e.seq FROM events e WHERE e.application_id = a.id
              AND e.to_state IS NOT NULL ORDER BY e.seq DESC LIMIT 1) AS stop_seq
    FROM applications a
    WHERE a.candidate_id = ? AND a.state = '{S.NEEDS_INPUT.value}'
),
windows AS (
    SELECT s.app_id, s.stop_seq,
           COALESCE((SELECT e.seq FROM events e WHERE e.application_id = s.app_id
                       AND e.seq < s.stop_seq AND e.to_state IS NOT NULL
                       AND e.to_state <> '{S.INSPECTING.value}'
                     ORDER BY e.seq DESC LIMIT 1), 0) AS after_seq
    FROM stops s
    JOIN events stop ON stop.seq = s.stop_seq AND stop.to_state = '{S.NEEDS_INPUT.value}'
),
ready AS (
    SELECT w.app_id,
           (SELECT e.seq FROM events e WHERE e.application_id = w.app_id
              AND e.event = '{PREPARED_EVENT}' AND e.seq > w.after_seq AND e.seq < w.stop_seq
            ORDER BY e.seq DESC LIMIT 1) AS ready_seq
    FROM windows w
),
prepared AS (
    SELECT r.app_id, r.ready_seq,
           COALESCE((SELECT e.seq FROM events e WHERE e.application_id = r.app_id
                       AND e.seq < r.ready_seq AND e.to_state IS NOT NULL
                       AND e.to_state NOT IN ('{S.INSPECTING.value}', '{S.PACKET_READY.value}',
                                              '{S.FILLING.value}')
                     ORDER BY e.seq DESC LIMIT 1), 0) AS run_start
    FROM ready r
    WHERE r.ready_seq IS NOT NULL
)
SELECT e.seq, e.id, e.application_id, e.event, e.timestamp, e.from_state, e.to_state,
       e.actor, e.metadata
FROM prepared p JOIN events e ON e.seq = p.ready_seq
UNION ALL
SELECT e.seq, e.id, e.application_id, e.event, e.timestamp, e.from_state, e.to_state,
       e.actor, e.metadata
FROM prepared p JOIN events e ON e.application_id = p.app_id
    AND e.event = '{EVIDENCE_EVENT}' AND e.seq > p.run_start AND e.seq < p.ready_seq
ORDER BY 1
"""
"""The ``preparation.ready`` event behind each prepared stop, and the evidence events
of its run. A stop is prepared when its latest transition enters NEEDS_INPUT and a
``preparation.ready`` comes after every earlier transition other than INSPECTING
(``views.prepared_event``); the run starts after the last transition before it into a
state a run does not pass through (``views.preparing_run``)."""

_EVIDENCE = """
SELECT application_id, id, body FROM evidence
WHERE id IN (SELECT value FROM json_each(?))
ORDER BY captured_at, rowid
"""

_BY_ALIAS = """
WITH RECURSIVE chain(alias, job_id, depth) AS (
    SELECT ja.alias, ja.job_id, 0
    FROM job_aliases ja WHERE ja.alias IN (SELECT value FROM json_each(?))
    UNION ALL
    SELECT c.alias, j.merged_into, c.depth + 1
    FROM chain c JOIN jobs j ON j.id = c.job_id
    WHERE j.merged_into IS NOT NULL AND c.depth < 64
)
SELECT c.alias, a.id
FROM chain c
JOIN jobs j ON j.id = c.job_id AND j.merged_into IS NULL
JOIN applications a ON a.candidate_id = ? AND a.job_id = c.job_id
"""
"""A URL alias's job, followed through merges to the canonical job, and the
candidate's application for it (``ApplicationStore.find_application``)."""


def url_alias(url: str) -> str:
    """The store's alias key for an application URL (``job_aliases.alias``). Raises
    ``InvalidApplicationUrl`` for a URL the store would refuse."""
    return f"url:{normalize_application_url(url)}"


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _event(row: sqlite3.Row) -> ApplicationEvent:
    return ApplicationEvent(
        id=row["id"], sequence=row["seq"], application_id=row["application_id"],
        event=row["event"], timestamp=_timestamp(row["timestamp"]),
        from_state=S(row["from_state"]) if row["from_state"] else None,
        to_state=S(row["to_state"]) if row["to_state"] else None,
        actor=row["actor"], metadata=json.loads(row["metadata"]),
    )


class StoreSnapshot:
    """One read transaction over the canonical store's database."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def applications_by_alias(self, candidate_id: str, aliases: Collection[str]) -> dict[str, str]:
        """``url:<normalized URL>`` alias -> the candidate's application for its job."""
        if not aliases:
            return {}
        rows = self.conn.execute(_BY_ALIAS, (json.dumps(sorted(aliases)), candidate_id))
        return {alias: app_id for alias, app_id in rows}

    def prepared_stops(
        self, candidate_id: str
    ) -> tuple[dict[str, ApplicationEvent], dict[str, list[EvidenceRef]]]:
        """Per prepared application of the candidate (its current stop is a prepared
        one): the ``preparation.ready`` event behind the stop, and the evidence its run
        recorded."""
        return self._prepared(candidate_id)

    def _prepared(
        self, candidate_id: str
    ) -> tuple[dict[str, ApplicationEvent], dict[str, list[EvidenceRef]]]:
        ready: dict[str, ApplicationEvent] = {}
        recorded: dict[str, set[str]] = {}
        for row in self.conn.execute(_PREPARED, (candidate_id,)):
            app_id = row["application_id"]
            if row["event"] == PREPARED_EVENT:
                ready[app_id] = _event(row)
            else:
                metadata = json.loads(row["metadata"])
                if isinstance(metadata, dict):
                    recorded.setdefault(app_id, set()).update(recorded_evidence_ids(metadata))
        evidence: dict[str, list[EvidenceRef]] = {}
        wanted = sorted({i for ids in recorded.values() for i in ids})
        if wanted:
            for app_id, evidence_id, body in self.conn.execute(_EVIDENCE, (json.dumps(wanted),)):
                if evidence_id in recorded.get(app_id, ()):
                    ref = EvidenceRef.model_validate_json(body)
                    if ref.id in recorded[app_id]:
                        evidence.setdefault(app_id, []).append(ref)
        return ready, evidence

    def summaries(
        self,
        candidate_id: str,
        *,
        public_base: str,
        cards: Mapping[str, Sequence[str]],
    ) -> list[ApplicationSummaryView]:
        """The candidate's applications in creation order, each with its preparation and
        ``cards[application id]`` as its pipeline entries."""
        ready, evidence = self._prepared(candidate_id)
        rows = []
        for row in self.conn.execute(_APPLICATIONS, (candidate_id,)).fetchall():
            app_id, state = row["id"], S(row["state"])
            prepared = ready.get(app_id) if state is S.NEEDS_INPUT else None
            rows.append(ApplicationSummaryView(
                id=app_id,
                state=state.value,
                application_url=row["application_url"],
                job=job_identity_view(row["title"], row["company"], row["ats_type"]),
                requested_at=iso(_timestamp(row["requested_at"])),
                updated_at=iso(_timestamp(row["updated_at"])),
                preparation=(
                    prepared_view(prepared, evidence.get(app_id, []), application_id=app_id,
                                  public_base=public_base)
                    if prepared is not None
                    else None
                ),
                pipeline_entry_ids=list(cards.get(app_id, ())),
            ))
        return rows


@contextmanager
def store_snapshot(state_db: Path) -> Iterator[StoreSnapshot]:
    """A consistent, read-only view of the store at ``state_db`` for the block. The
    database must exist (open it with ``ApplicationStore`` first); it is never created
    here, so it never gets a file mode other than the store's own."""
    conn = sqlite3.connect(
        f"{state_db.resolve().as_uri()}?mode=rw", uri=True, timeout=30.0, isolation_level=None
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        try:
            yield StoreSnapshot(conn)
        finally:
            conn.execute("ROLLBACK")
    finally:
        conn.close()
