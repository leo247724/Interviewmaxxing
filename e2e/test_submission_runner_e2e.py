"""The runner's approved-submission path end to end: ``LocalApplicationRunner`` with real
headless Chromium, the real factual resolver and the real store against the separately
running localhost mock ATS, with the fictional candidate. Approval and authorization go
through the store exactly as ``interviewmaxxing approve`` / ``submit`` call them.

Asserted against what the mock actually received: an approved application is submitted
once, with exactly the approved values; everything else submits nothing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from support import PREPARED, MockServer

from interviewmaxxing_cli.runner import (
    MISMATCH_MESSAGE,
    NOT_AUTHORIZED_MESSAGE,
    LocalApplicationRunner,
    NoninteractiveInteraction,
)
from interviewmaxxing_core import (
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    ApplyOutcome,
    BooleanValue,
    ChoiceValue,
    FileValue,
    LocalPaths,
    MultiChoiceValue,
    TextValue,
)

pytestmark = pytest.mark.slow

S = ApplicationState
TRAVEL = "Are you willing to travel to client sites up to 25% of the time?"


def _paths(home: Path) -> LocalPaths:
    return LocalPaths.from_env({}, home=home)


def _runner(paths: LocalPaths, *, prepare_only: bool) -> LocalApplicationRunner:
    return LocalApplicationRunner(paths=paths, interaction=NoninteractiveInteraction(),
                                  headless=True, prepare_only=prepare_only)


def _prepare(paths: LocalPaths, url: str) -> ApplyOutcome:
    outcome = asyncio.run(_runner(paths, prepare_only=True).apply(url, candidate_id="default"))
    assert outcome.state is S.NEEDS_INPUT and outcome.message.startswith(PREPARED), outcome.message
    return outcome


def _approve(paths: LocalPaths, app_id: str, *, authorize: bool = True) -> str:
    with ApplicationStore.open(paths.state_db) as store:
        claim = store.claim(app_id, "cli:e2e")
        packet_id = store.prepared_packet(app_id)
        assert packet_id is not None
        store.approve_submission(claim, packet_id=packet_id, approver="cli:e2e")
        if authorize:
            store.authorize_submission(claim)
        store.release(claim)
    return packet_id


def _submit(paths: LocalPaths, app_id: str) -> ApplyOutcome:
    return asyncio.run(_runner(paths, prepare_only=False).submit(app_id))


def received(packets: list[ApplicationPacket]) -> dict[str, Any]:
    """What the mock records for these answers (``fields``: machine values; a checked
    box is "yes"; files are recorded separately)."""
    fields: dict[str, Any] = {}
    for packet in packets:
        for answer in packet.answers:
            value = answer.value
            if isinstance(value, TextValue):
                fields[answer.field_id] = value.text
            elif isinstance(value, ChoiceValue):
                fields[answer.field_id] = value.value
            elif isinstance(value, MultiChoiceValue):
                fields[answer.field_id] = [c.value for c in value.choices]
            elif isinstance(value, BooleanValue) and value.checked:
                fields[answer.field_id] = "yes"
    return fields


def _files(packets: list[ApplicationPacket]) -> dict[str, str]:
    return {a.field_id: a.value.artifact.sha256 for p in packets for a in p.answers
            if isinstance(a.value, FileValue)}


def test_an_approved_application_is_submitted_once_with_exactly_the_approved_values(
    ats: MockServer, home: Path, profile: Path
):
    paths = _paths(home)
    app_id = _prepare(paths, ats.url("standard")).application_id
    packet_id = _approve(paths, app_id)
    with ApplicationStore.open(paths.state_db) as store:
        approved = store.get_packet(packet_id)
    # The saved answers change after the approval; what was approved is what is sent.
    data = json.loads(profile.read_text())
    for saved in data["saved_answers"]:
        if saved["id"] == "sa.years":
            saved["value"] = "10 or more years"
        if saved["id"] == "sa.why":
            saved["value"] = "A different motivation written after the approval."
    profile.write_text(json.dumps(data, indent=2))

    result = _submit(paths, app_id)

    assert result.state is S.SUBMITTED and result.receipt is not None, result.message
    server = ats.submissions("standard")
    assert (server["accepted_count"], server["rejected_count"]) == (1, 0)
    [record] = server["submissions"]
    assert record["fields"] == received([approved])
    assert record["fields"]["years_experience"] == "yrs_6_9"
    assert record["fields"]["why_brambleway"].startswith("I have built data platforms")
    assert {k: v["sha256"] for k, v in record["files"].items()} == _files([approved])
    assert result.receipt.confirmation_reference == record["confirmation_reference"]
    with ApplicationStore.open(paths.state_db) as store:
        [attempt] = store.list_attempts(app_id)
        assert attempt.packet_id == packet_id
        names = [e.event for e in store.list_events(app_id)]
    assert "packet.saved" not in names[names.index("application.submission_authorized"):]


def test_an_unapproved_application_is_never_submitted(ats: MockServer, home: Path, profile: Path):
    paths = _paths(home)
    app_id = _prepare(paths, ats.url("standard")).application_id

    refused = _submit(paths, app_id)
    assert refused.state is S.NEEDS_INPUT and refused.message.startswith(NOT_AUTHORIZED_MESSAGE)
    # A submission-capable runner resuming it keeps the stored restriction: it prepares
    # again and never submits.
    resumed = asyncio.run(_runner(paths, prepare_only=False).resume(app_id))
    assert resumed.state is S.NEEDS_INPUT and resumed.message.startswith(PREPARED)
    assert ats.submissions("standard")["accepted_count"] == 0
    assert ats.submissions("standard")["rejected_count"] == 0
    with ApplicationStore.open(paths.state_db) as store:
        assert store.list_attempts(app_id) == []


def test_a_form_that_changed_since_approval_is_not_submitted(ats: MockServer, home: Path, profile: Path):
    paths = _paths(home)
    app_id = _prepare(paths, ats.url("changed-after-prepare")).application_id
    packet_id = _approve(paths, app_id)

    result = _submit(paths, app_id)  # the form's second load asks a new required question

    assert result.state is S.NEEDS_INPUT and result.message.startswith(MISMATCH_MESSAGE), result.message
    assert f"a new required question {TRAVEL!r} appeared" in result.message
    server = ats.submissions("changed-after-prepare")
    assert (server["accepted_count"], server["rejected_count"]) == (0, 0)  # nothing was posted
    with ApplicationStore.open(paths.state_db) as store:
        assert store.list_attempts(app_id) == []
        assert store.approved_packet(app_id) is None and store.is_preparation_only(app_id)
        [withdrawn] = [e for e in store.list_events(app_id)
                       if e.event == "application.approval_invalidated"]
    assert withdrawn.metadata["packet_id"] == packet_id


def test_a_prepare_only_run_after_an_approval_never_submits(ats: MockServer, home: Path, profile: Path):
    paths = _paths(home)
    app_id = _prepare(paths, ats.url("standard")).application_id
    first = _approve(paths, app_id)

    again = asyncio.run(_runner(paths, prepare_only=True).resume(app_id))

    assert again.state is S.NEEDS_INPUT and again.message.startswith(PREPARED)
    assert ats.submissions("standard")["accepted_count"] == 0
    with ApplicationStore.open(paths.state_db) as store:
        assert store.is_preparation_only(app_id)
        assert store.approved_packet(app_id) is None
        assert store.prepared_packet(app_id) not in (None, first)
    assert _submit(paths, app_id).message.startswith(NOT_AUTHORIZED_MESSAGE)
    assert ats.submissions("standard")["accepted_count"] == 0
