"""``GET /review``: the Prepared queue, from one read of the canonical store.

The queue lists the candidate's applications that wait at their final review step
(prepared: filled, nothing submitted) and those held only by a step the person does in
the browser (a sign-in, a CAPTCHA, a custom control or a file), newest stop first. Like
``summaries``, it reads the core tables in one read transaction on a connection that
cannot write, with a fixed number of statements however many applications there are:

* the NEEDS_INPUT applications with their request, their job and their current stop
  (the latest transition, whose metadata records what the stop waits for);
* the prepared stops (``StoreSnapshot.prepared_stops``, the rule of
  ``views.prepared_event``);
* per application the latest ``preparation.ready``, ``application.approved``,
  ``application.approval_invalidated`` and ``input.received``, which decide whether the
  preparation is approved exactly as ``ApplicationStore._approval_row`` does: the latest
  approval names the latest preparation, no invalidation came after it and no answer
  was saved after that preparation;
* the provider usage of every ``provider.budget`` event.

It reads the columns ``summaries`` reads. ``tests/service/test_review_queue.py`` checks
the approval flag against ``ApplicationStore.submission_approval``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from interviewmaxxing_core import ApplicationState, ControlType, MissingInput, MissingReason

from .models import HoldView, ProviderCostView, ReviewQueueItemView
from .summaries import StoreSnapshot
from .views import _interaction_kind, is_answerable, iso, job_identity_view

S = ApplicationState

APPROVED_EVENT = "application.approved"
APPROVAL_INVALIDATED_EVENT = "application.approval_invalidated"
ANSWERED_EVENT = "input.received"
PREPARED_EVENT = "preparation.ready"
PROVIDER_EVENT = "provider.budget"

_STOPS = f"""
SELECT a.id, a.state, r.application_url, j.title, j.company, j.ats_type,
       stop.seq AS stop_seq, stop.timestamp AS stop_at, stop.to_state AS stop_state,
       stop.metadata AS stop_metadata
FROM applications a
JOIN requests r ON r.id = COALESCE(
    (SELECT q.id FROM requests q WHERE q.id = a.request_id AND q.application_id = a.id),
    (SELECT q.id FROM requests q WHERE q.application_id = a.id
     ORDER BY q.requested_at, q.rowid LIMIT 1))
JOIN jobs j ON j.id = a.job_id
JOIN events stop ON stop.seq = (
    SELECT e.seq FROM events e WHERE e.application_id = a.id AND e.to_state IS NOT NULL
    ORDER BY e.seq DESC LIMIT 1)
WHERE a.candidate_id = ? AND a.state = '{S.NEEDS_INPUT.value}'
"""
"""Each NEEDS_INPUT application with its request, its job and its current stop."""

_LATEST = f"""
SELECT e.application_id, e.event, e.seq, e.id, e.metadata
FROM events e
WHERE e.application_id IN (SELECT value FROM json_each(?))
  AND e.event IN ('{PREPARED_EVENT}', '{APPROVED_EVENT}', '{APPROVAL_INVALIDATED_EVENT}',
                  '{ANSWERED_EVENT}')
  AND e.seq = (SELECT MAX(x.seq) FROM events x
               WHERE x.application_id = e.application_id AND x.event = e.event)
"""
"""The latest event of each kind an approval depends on, per application."""

_PROVIDER = f"""
SELECT application_id,
       SUM(COALESCE(json_extract(metadata, '$.known_cost_usd'), 0)) AS known_usd,
       SUM(COALESCE(json_extract(metadata, '$.calls'), 0)) AS calls,
       SUM(COALESCE(json_extract(metadata, '$.unknown_cost_calls'), 0)) AS unknown_calls
FROM events
WHERE application_id IN (SELECT value FROM json_each(?)) AND event = '{PROVIDER_EVENT}'
GROUP BY application_id
"""

_BROWSER_REASONS = frozenset({MissingReason.USER_ACTION, MissingReason.UNSUPPORTED_CONTROL})
_BROWSER_CONTROLS = frozenset({ControlType.FILE, ControlType.UNSUPPORTED})
CAPTCHA_SUFFIX = " · CAPTCHA to solve when submitting"
_LABEL_LIMIT = 60


@dataclass(frozen=True)
class _Latest:
    seq: int
    id: str
    metadata: dict[str, Any]


def _metadata(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _missing(meta: Mapping[str, Any]) -> list[MissingInput] | None:
    """The stop's recorded questions and actions; None when it recorded none."""
    raw = meta.get("missing_inputs")
    if not isinstance(raw, list):
        return None
    items = []
    for item in raw:
        try:
            items.append(MissingInput.model_validate(item))
        except ValidationError:
            continue
    return items


def needs_browser(item: MissingInput) -> bool:
    """A step the person does in the browser: a sign-in, a CAPTCHA, a custom control or
    a file (``triage.HoldItem.needs_browser``)."""
    return item.reason in _BROWSER_REASONS or item.control_type in _BROWSER_CONTROLS


def _short(text: str) -> str:
    line = " ".join(text.split("\n", 1)[0].split())
    return line if len(line) <= _LABEL_LIMIT else line[: _LABEL_LIMIT - 1] + "…"


def browser_hold(meta: Mapping[str, Any]) -> HoldView | None:
    """What a stop that waits only for the browser asks for; None for any other stop."""
    page_kind = str(meta.get("page_kind") or "")
    if page_kind == "SIGN_IN_REQUIRED":
        return HoldView(kind="sign_in", summary="Sign-in needed in the browser")
    if page_kind == "CAPTCHA":
        return HoldView(kind="captcha", summary="CAPTCHA to solve in the browser")
    items = _missing(meta)
    if not items or not all(needs_browser(item) for item in items):
        return None
    actions = [i for i in items if i.reason is MissingReason.USER_ACTION]
    kind = _interaction_kind(" ".join(f"{i.label} {i.prompt}" for i in actions)) if actions else ""
    if kind == "SIGN_IN":
        return HoldView(kind="sign_in", summary="Sign-in needed in the browser")
    if kind == "CAPTCHA":
        return HoldView(kind="captcha", summary="CAPTCHA to solve in the browser")
    first = items[0]
    return HoldView(kind="browser_action",
                    summary=f"To finish in the browser: {_short(first.label) or 'a step on the page'}")


def prepared_hold(
    meta: Mapping[str, Any], *, approved: bool, changed: bool, captcha: bool
) -> HoldView:
    """The one line for a prepared stop: required questions left, answers changed since,
    approved, or ready for review. A prepared stop records the optional questions it left
    blank too; those don't hold it."""
    open_questions = [i for i in _missing(meta) or [] if i.required and is_answerable(i)]
    if open_questions:
        count = len(open_questions)
        return HoldView(
            kind="questions",
            summary=f"{count} required {'question' if count == 1 else 'questions'} still open")
    if changed:
        return HoldView(kind="edited",
                        summary="Answers changed since this preparation: prepare it again")
    suffix = CAPTCHA_SUFFIX if captcha else ""
    if approved:
        return HoldView(kind="approved", summary="Approved: waiting to be submitted" + suffix)
    return HoldView(kind="ready", summary="Ready to review and approve" + suffix)


def _approved(latest: Mapping[str, _Latest]) -> bool:
    """``ApplicationStore._approval_row`` over the latest events of one application."""
    approved, prepared = latest.get(APPROVED_EVENT), latest.get(PREPARED_EVENT)
    if approved is None or prepared is None:
        return False
    if approved.metadata.get("preparation_event_id") != prepared.id:
        return False
    answered = latest.get(ANSWERED_EVENT)
    if answered is not None and answered.seq > prepared.seq:
        return False
    invalidated = latest.get(APPROVAL_INVALIDATED_EVENT)
    return invalidated is None or invalidated.seq < approved.seq


def provider_cost(known_usd: Any, calls: Any, unknown_calls: Any) -> ProviderCostView:
    return ProviderCostView(known_usd=round(float(known_usd or 0.0), 6), calls=int(calls or 0),
                            unknown_cost_calls=int(unknown_calls or 0))


def review_queue(snapshot: StoreSnapshot, candidate_id: str) -> list[ReviewQueueItemView]:
    """The candidate's prepared and browser-held applications, newest stop first."""
    conn: sqlite3.Connection = snapshot.conn
    stops = [row for row in conn.execute(_STOPS, (candidate_id,)).fetchall()
             if row["stop_state"] == S.NEEDS_INPUT.value]
    if not stops:
        return []
    ids = json.dumps(sorted({row["id"] for row in stops}))
    ready, _evidence = snapshot.prepared_stops(candidate_id)
    latest: dict[str, dict[str, _Latest]] = {}
    for row in conn.execute(_LATEST, (ids,)):
        latest.setdefault(row["application_id"], {})[row["event"]] = _Latest(
            seq=row["seq"], id=row["id"], metadata=_metadata(row["metadata"]))
    costs = {
        row["application_id"]: provider_cost(row["known_usd"], row["calls"], row["unknown_calls"])
        for row in conn.execute(_PROVIDER, (ids,))
    }
    items: list[ReviewQueueItemView] = []
    for row in stops:
        app_id = row["id"]
        meta = _metadata(row["stop_metadata"])
        events = latest.get(app_id, {})
        approved = _approved(events)
        prepared = ready.get(app_id)
        if prepared is not None:
            captcha = prepared.metadata.get("captcha_pending") is True
            answered = events.get(ANSWERED_EVENT)
            changed = answered is not None and answered.seq > prepared.sequence
            hold = prepared_hold(meta, approved=approved, changed=changed, captcha=captcha)
            stage: str = "prepared"
        else:
            browser = browser_hold(meta)
            if browser is None:
                continue
            hold, captcha, stage = browser, False, "browser_action"
        items.append(ReviewQueueItemView(
            id=app_id,
            state=S.NEEDS_INPUT.value,
            stage="prepared" if stage == "prepared" else "browser_action",
            application_url=row["application_url"],
            job=job_identity_view(row["title"], row["company"], row["ats_type"]),
            prepared_at=iso(prepared.timestamp) if prepared is not None else None,
            stopped_at=iso(datetime.fromisoformat(row["stop_at"])),
            captcha_pending=captcha,
            provider_cost=costs.get(app_id),
            hold=hold,
            approved=approved,
        ))
    items.sort(key=lambda item: (item.stopped_at, item.id), reverse=True)
    return items
