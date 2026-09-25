"""Approved submission in the store: approve → authorize → submit exactly the approved
packet, and every way the no-submit restriction comes back.

All data is fictional (candidate "Avery Example", a mock employer on example hosts).
A prepare-only run is simulated with the store's own operations, the way the runner
leaves it: ``require_preparation_only``, one packet per step, ``preparation.ready`` and
the stop (FILLING -> INSPECTING -> NEEDS_INPUT)."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

import pytest

from interviewmaxxing_core import (
    AnswerSource,
    Application,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    ApprovedStep,
    ClaimLost,
    ControlType,
    MissingInput,
    MissingReason,
    PacketAnswer,
    Provenance,
    SemanticType,
    SubmissionApproval,
    SubmissionBlocked,
    SubmissionObservation,
    SubmissionOutcome,
    TextValue,
)

S = ApplicationState
CAND = "cand_avery_example"
URL = "https://jobs.mock.example/mock-co/4012/apply"
OTHER_URL = "https://jobs.mock.example/mock-co/4013/apply"


def _form(step: int = 0, *, final: bool = True, url: str = URL) -> ApplicationForm:
    return ApplicationForm(
        url=url, step=step, is_final_step=final,
        submit_selector="#submit" if final else None, next_selector=None if final else "#next",
        fields=[
            ApplicationField(id="first_name", label="First name", selector="#first_name",
                             semantic_type=SemanticType.FIRST_NAME, control_type=ControlType.TEXT,
                             required=True),
            ApplicationField(id=f"why_{step}", label="Why us?", selector=f"#why_{step}",
                             semantic_type=SemanticType.CUSTOM_LONG_TEXT,
                             control_type=ControlType.TEXTAREA, required=True),
        ],
    )


def _packet(app: Application, form: ApplicationForm, *, complete: bool = True) -> ApplicationPacket:
    answers = [PacketAnswer(field_id="first_name", semantic_type=SemanticType.FIRST_NAME,
                            value=TextValue(text="Avery"),
                            provenance=Provenance(source=AnswerSource.PROFILE_IDENTITY))]
    missing: list[MissingInput] = []
    why = form.field(f"why_{form.step}")
    if complete:
        answers.append(PacketAnswer(field_id=why.id, semantic_type=why.semantic_type,
                                    value=TextValue(text="I like forecasting."),
                                    provenance=Provenance(source=AnswerSource.USER_INPUT,
                                                          reference_ids=["ui_fictional"])))
    else:
        missing.append(MissingInput.for_field(form, why, reason=MissingReason.NO_ANSWER,
                                              prompt="Why us?"))
    return ApplicationPacket(application_id=app.id, job_id=app.job_id, candidate_id=app.candidate_id,
                             form_url=form.url, form_step=form.step,
                             form_fingerprint=form.fingerprint, answers=answers,
                             missing_inputs=missing)


def _prepare(store: ApplicationStore, app: Application, *, steps: int = 1,
             record_steps: bool = True, complete: bool = True, owner: str = "prep") -> str:
    """A prepare-only run that reaches the final review step; returns its packet id."""
    claim = store.claim(app.id, owner)
    store.require_preparation_only(claim)
    store.transition(claim, S.INSPECTING)
    recorded: list[dict[str, Any]] = []
    for step in range(steps):
        form = _form(step, final=step == steps - 1)
        if step:
            store.transition(claim, S.INSPECTING)  # the next page of the form
        packet = _packet(app, form, complete=complete)
        store.save_packet(claim, packet)
        store.transition(claim, S.PACKET_READY)
        store.transition(claim, S.FILLING)
        recorded.append({"form_step": step, "packet_id": packet.id, "fields": []})
    metadata: dict[str, Any] = {
        "form_url": form.url, "form_step": form.step, "form_fingerprint": form.fingerprint,
        "packet_id": packet.id, "submitted": False, "browser_location": None,
        "captcha_pending": False,
    }
    if record_steps:
        metadata["steps"] = recorded
    store.append_event(claim, "preparation.ready", metadata)
    store.transition(claim, S.INSPECTING)
    store.transition(claim, S.NEEDS_INPUT, metadata={"missing_inputs": [], "reason": "prepared"})
    store.release(claim)
    return packet.id


def _app(store: ApplicationStore, url: str = URL, candidate: str = CAND) -> Application:
    return store.record_request(candidate, url).application


def _events(store: ApplicationStore, app_id: str) -> list[str]:
    return [e.event for e in store.list_events(app_id)]


def _approve(store: ApplicationStore, app_id: str, packet_id: str,
             approver: str = "cli:fixture") -> SubmissionApproval:
    claim = store.claim(app_id, "approver")
    try:
        return store.approve_submission(claim, packet_id=packet_id, approver=approver)
    finally:
        store.release(claim)


def _authorize(store: ApplicationStore, app_id: str) -> SubmissionApproval:
    claim = store.claim(app_id, "submitter")
    try:
        return store.authorize_submission(claim)
    finally:
        store.release(claim)


def _to_filling(store: ApplicationStore, app_id: str):
    claim = store.claim(app_id, "runner")
    store.transition(claim, S.INSPECTING)
    store.transition(claim, S.PACKET_READY)
    store.transition(claim, S.FILLING)
    return claim


# --- approve ------------------------------------------------------------------------


def test_approval_records_the_prepared_packet_but_keeps_the_restriction(store):
    app = _app(store)
    packet_id = _prepare(store, app)
    assert store.is_preparation_only(app.id)
    assert store.prepared_packet(app.id) == packet_id
    assert store.approved_packet(app.id) is None and store.submission_approval(app.id) is None

    approval = _approve(store, app.id, packet_id)

    [prepared] = [e for e in store.list_events(app.id) if e.event == "preparation.ready"]
    [event] = [e for e in store.list_events(app.id) if e.event == "application.approved"]
    assert event.from_state is None and event.to_state is None and event.actor == "approver"
    assert event.metadata == {
        "packet_id": packet_id, "approver": "cli:fixture", "form_step": 0, "form_url": URL,
        "form_fingerprint": prepared.metadata["form_fingerprint"],
        "preparation_event_id": prepared.id,
        "steps": [{"form_step": 0, "packet_id": packet_id}], "captcha_pending": False,
    }
    assert approval == SubmissionApproval(
        application_id=app.id, event_id=event.id, packet_id=packet_id, approver="cli:fixture",
        approved_at=event.timestamp, preparation_event_id=prepared.id, form_step=0, form_url=URL,
        steps=[ApprovedStep(form_step=0, packet_id=packet_id)])
    assert store.approved_packet(app.id) == packet_id
    assert store.submission_approval(app.id) == approval
    # An approval is a review decision, not permission to submit.
    assert store.is_preparation_only(app.id)
    assert store.submission_authorization(app.id) is None
    claim = _to_filling(store, app.id)
    with pytest.raises(SubmissionBlocked, match="preparation-only"):
        store.begin_submission(claim, packet_id=packet_id)
    assert store.list_attempts(app.id) == []


def test_approving_the_approved_packet_again_records_nothing(store):
    app = _app(store)
    packet_id = _prepare(store, app)
    first = _approve(store, app.id, packet_id)
    again = _approve(store, app.id, packet_id, approver="cli:someone-else")
    assert again == first
    assert _events(store, app.id).count("application.approved") == 1


def test_approval_needs_the_prepared_stop_and_its_complete_packet(store, clock):
    app = _app(store)
    claim = store.claim(app.id, "approver")
    with pytest.raises(SubmissionBlocked, match="REQUESTED"):
        store.approve_submission(claim, packet_id="pkt_none", approver="cli:fixture")
    store.release(claim)

    packet_id = _prepare(store, app)
    claim = store.claim(app.id, "approver")
    with pytest.raises(SubmissionBlocked, match="not the prepared packet"):
        store.approve_submission(claim, packet_id="pkt_other", approver="cli:fixture")
    with pytest.raises(ValueError, match="approver"):
        store.approve_submission(claim, packet_id=packet_id, approver="  ")
    store.release(claim)

    # A claim is required and must be live.
    stale = store.claim(app.id, "approver", ttl=timedelta(seconds=1))
    clock.advance(seconds=2)
    with pytest.raises(ClaimLost):
        store.approve_submission(stale, packet_id=packet_id, approver="cli:fixture")

    # A later run that stopped for questions is not a preparation stop, even though the
    # latest preparation.ready is unchanged.
    claim = store.claim(app.id, "later-run")
    store.transition(claim, S.INSPECTING)
    store.transition(claim, S.NEEDS_INPUT, metadata={"missing_inputs": [], "reason": "questions"})
    assert store.prepared_packet(app.id) is None
    with pytest.raises(SubmissionBlocked, match="not stopped at a completed preparation"):
        store.approve_submission(claim, packet_id=packet_id, approver="cli:fixture")
    store.release(claim)
    assert "application.approved" not in _events(store, app.id)


def test_an_incomplete_prepared_packet_cannot_be_approved(store):
    app = _app(store)
    packet_id = _prepare(store, app, complete=False)
    claim = store.claim(app.id, "approver")
    with pytest.raises(SubmissionBlocked, match="open required questions"):
        store.approve_submission(claim, packet_id=packet_id, approver="cli:fixture")


def test_every_prepared_step_is_approved_with_its_packet(store):
    app = _app(store)
    packet_id = _prepare(store, app, steps=3)
    approval = _approve(store, app.id, packet_id)
    saved = [e.metadata["packet_id"] for e in store.list_events(app.id) if e.event == "packet.saved"]
    assert [s.model_dump() for s in approval.steps] == [
        {"form_step": 0, "packet_id": saved[0]}, {"form_step": 1, "packet_id": saved[1]},
        {"form_step": 2, "packet_id": packet_id}]
    assert approval.form_step == 2


def test_an_older_preparation_without_recorded_steps_uses_its_run_s_latest_packets(store):
    """Preparations recorded before the runner listed its steps: the latest packet saved
    per step after the previous stop (an earlier run's packets are not used)."""
    app = _app(store)
    _prepare(store, app, steps=2, record_steps=False, owner="first-run")
    packet_id = _prepare(store, app, steps=2, record_steps=False, owner="second-run")
    saved = [e for e in store.list_events(app.id) if e.event == "packet.saved"]
    assert len(saved) == 4
    approval = _approve(store, app.id, packet_id)
    assert [(s.form_step, s.packet_id) for s in approval.steps] == [
        (0, saved[2].metadata["packet_id"]), (1, packet_id)]


# --- authorize ---------------------------------------------------------------------------


def test_authorization_lifts_the_restriction_for_exactly_the_approved_packet(store):
    app = _app(store)
    packet_id = _prepare(store, app)
    claim = store.claim(app.id, "submitter")
    with pytest.raises(SubmissionBlocked, match="no valid approval"):
        store.authorize_submission(claim)
    store.release(claim)
    approval = _approve(store, app.id, packet_id)

    assert _authorize(store, app.id) == approval
    [event] = [e for e in store.list_events(app.id) if e.event == "application.submission_authorized"]
    assert event.metadata == {"packet_id": packet_id, "approval_event_id": approval.event_id,
                              "approver": "cli:fixture"}
    assert not store.is_preparation_only(app.id)
    assert store.submission_authorization(app.id) == approval

    claim = _to_filling(store, app.id)
    for wrong in (None, "pkt_other"):
        with pytest.raises(SubmissionBlocked, match="only the approved packet"):
            store.begin_submission(claim, packet_id=wrong)
    attempt = store.begin_submission(claim, packet_id=packet_id)
    assert attempt.packet_id == packet_id
    assert store.get_application(app.id).state is S.SUBMITTING
    [submitting] = [e for e in store.list_events(app.id) if e.event == "application.submitting"]
    assert submitting.metadata["packet_id"] == packet_id
    done = store.record_submission_outcome(claim, attempt.id, SubmissionObservation(
        outcome=SubmissionOutcome.ACCEPTED, signals=["heading 'Application received'"],
        confirmation_reference="MOCK-1"))
    assert done.state is S.SUBMITTED and store.get_receipt(app.id) is not None
    assert store.list_approved() == []  # submitted: nothing left to submit


def test_a_new_preparation_invalidates_the_approval_until_it_is_approved_again(store):
    app = _app(store)
    first = _prepare(store, app)
    _approve(store, app.id, first)
    _authorize(store, app.id)
    assert not store.is_preparation_only(app.id)

    second = _prepare(store, app, owner="prepare-again")
    assert second != first
    # The prepare-only run recorded the restriction again, and the approval named the
    # previous preparation.
    assert _events(store, app.id).count("application.preparation_only") == 2
    assert store.is_preparation_only(app.id)
    assert store.approved_packet(app.id) is None
    assert store.submission_authorization(app.id) is None
    claim = store.claim(app.id, "submitter")
    with pytest.raises(SubmissionBlocked, match="no valid approval"):
        store.authorize_submission(claim)
    with pytest.raises(SubmissionBlocked, match="not the prepared packet"):
        store.approve_submission(claim, packet_id=first, approver="cli:fixture")
    store.release(claim)

    _approve(store, app.id, second)
    assert store.is_preparation_only(app.id)  # approved, not yet authorized
    _authorize(store, app.id)
    assert not store.is_preparation_only(app.id)
    assert store.approved_packet(app.id) == second


def test_a_new_preparation_invalidates_an_approval_that_was_never_authorized(store):
    app = _app(store)
    first = _prepare(store, app)
    _approve(store, app.id, first)
    _prepare(store, app, owner="prepare-again")
    assert store.approved_packet(app.id) is None
    # Never authorized, so the restriction was never lifted and is not recorded twice.
    assert _events(store, app.id).count("application.preparation_only") == 1


def test_a_prepare_only_run_after_authorization_restores_the_restriction(store):
    app = _app(store)
    packet_id = _prepare(store, app)
    _approve(store, app.id, packet_id)
    _authorize(store, app.id)

    claim = store.claim(app.id, "prepare-only-run")
    store.require_preparation_only(claim)  # before any browser work, as the runner does
    assert store.is_preparation_only(app.id)
    store.transition(claim, S.INSPECTING)
    store.transition(claim, S.PACKET_READY)
    store.transition(claim, S.FILLING)
    with pytest.raises(SubmissionBlocked, match="preparation-only"):
        store.begin_submission(claim, packet_id=packet_id)
    store.release(claim)
    assert store.list_attempts(app.id) == []


def test_invalidating_an_authorized_approval_restores_the_restriction(store):
    app = _app(store)
    packet_id = _prepare(store, app)
    approval = _approve(store, app.id, packet_id)
    _authorize(store, app.id)

    claim = store.claim(app.id, "runner")
    store.invalidate_approval(claim, reason="the form no longer matches the approved application",
                              details=["new required question 'Travel?'"])
    store.release(claim)
    invalidated = [e for e in store.list_events(app.id) if e.event == "application.approval_invalidated"]
    assert [e.metadata for e in invalidated] == [{
        "packet_id": packet_id, "approval_event_id": approval.event_id,
        "reason": "the form no longer matches the approved application",
        "details": ["new required question 'Travel?'"]}]
    assert _events(store, app.id)[-1] == "application.preparation_only"
    assert store.is_preparation_only(app.id)
    assert store.approved_packet(app.id) is None
    assert store.submission_authorization(app.id) is None
    claim = _to_filling(store, app.id)
    with pytest.raises(SubmissionBlocked, match="preparation-only"):
        store.begin_submission(claim, packet_id=packet_id)
    store.release(claim)


def test_invalidating_without_an_approval_records_nothing(store):
    app = _app(store)
    _prepare(store, app)
    before = _events(store, app.id)
    claim = store.claim(app.id, "runner")
    store.invalidate_approval(claim, reason="nothing to withdraw")
    store.release(claim)
    assert _events(store, app.id) == before


def test_authorization_requires_a_pre_submission_state(store):
    app = _app(store)
    packet_id = _prepare(store, app)
    _approve(store, app.id, packet_id)
    _authorize(store, app.id)
    claim = _to_filling(store, app.id)
    attempt = store.begin_submission(claim, packet_id=packet_id)
    store.record_submission_outcome(claim, attempt.id, SubmissionObservation(
        outcome=SubmissionOutcome.UNKNOWN, signals=["no confirmation"]))
    store.release(claim)
    claim = store.claim(app.id, "submitter")
    with pytest.raises(SubmissionBlocked, match="SUBMISSION_UNKNOWN"):
        store.authorize_submission(claim)


# --- the SQL boundary ------------------------------------------------------------------------


def test_the_sql_trigger_admits_only_the_authorized_packet(store_path, store):
    app = _app(store)
    packet_id = _prepare(store, app)

    def insert(attempt: str, packet: str | None) -> None:
        with sqlite3.connect(store_path) as conn:
            conn.execute(
                "INSERT INTO submission_attempts (id, application_id, attempt_number, owner,"
                " packet_id, started_at) VALUES (?, ?, ?, 'legacy', ?, '2026-09-23T23:00:00Z')",
                (attempt, app.id, int(attempt[-1]), packet),
            )

    # An older runner without the Python guard cannot insert an attempt: not approved,
    # approved but not authorized, or authorized for another packet.
    with pytest.raises(sqlite3.IntegrityError, match="preparation-only"):
        insert("raw-1", packet_id)
    _approve(store, app.id, packet_id)
    with pytest.raises(sqlite3.IntegrityError, match="preparation-only"):
        insert("raw-1", packet_id)
    _authorize(store, app.id)
    for wrong in (None, "pkt_other"):
        with pytest.raises(sqlite3.IntegrityError, match="preparation-only"):
            insert("raw-1", wrong)
    insert("raw-1", packet_id)
    assert [a.packet_id for a in store.list_attempts(app.id)] == [packet_id]


def test_an_existing_database_gets_the_new_trigger(store_path):
    """A database created before approved submission has the unconditional trigger;
    opening it installs the replacement without changing the schema version."""
    with ApplicationStore.open(store_path) as store:
        app = _app(store)
        packet_id = _prepare(store, app)
    with sqlite3.connect(store_path) as conn:
        conn.execute("DROP TRIGGER preparation_blocks_submission")
        conn.execute(
            "CREATE TRIGGER preparation_blocks_submission BEFORE INSERT ON submission_attempts"
            " WHEN EXISTS (SELECT 1 FROM events WHERE application_id = NEW.application_id"
            " AND event = 'application.preparation_only')"
            " BEGIN SELECT RAISE(ABORT, 'preparation-only application cannot be submitted'); END")
    with ApplicationStore.open(store_path) as store:
        sql = store._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'preparation_blocks_submission'").fetchone()[0]
        assert "application.submission_authorized" in sql
        version = store._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        assert version == "4"
        _approve(store, app.id, packet_id)
        _authorize(store, app.id)
        claim = _to_filling(store, app.id)
        assert store.begin_submission(claim, packet_id=packet_id).packet_id == packet_id


def test_a_never_restricted_application_submits_as_before(store):
    """Synthetic callers that never prepared (tests, a future authorized caller) keep
    the original contract: no approval is involved and any packet may be recorded."""
    app = _app(store)
    claim = _to_filling(store, app.id)
    assert not store.is_preparation_only(app.id)
    assert store.submission_authorization(app.id) is None
    assert store.begin_submission(claim, packet_id=None).packet_id is None


# --- queries --------------------------------------------------------------------------------


def test_list_approved_returns_valid_approvals_not_yet_submitted(store):
    approved = _app(store)
    _approve(store, approved.id, _prepare(store, approved))
    reprepared = _app(store, OTHER_URL)
    _approve(store, reprepared.id, _prepare(store, reprepared))
    _prepare(store, reprepared, owner="prepare-again")
    unapproved = _app(store, "https://jobs.mock.example/mock-co/4014/apply")
    _prepare(store, unapproved)
    other = _app(store, "https://jobs.mock.example/mock-co/4015/apply", candidate="cand_other")
    _approve(store, other.id, _prepare(store, other))

    assert [a.id for a in store.list_approved()] == [approved.id, other.id]
    assert [a.id for a in store.list_approved(candidate_id=CAND)] == [approved.id]
