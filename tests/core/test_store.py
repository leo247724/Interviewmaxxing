"""ApplicationStore behavior: requests, dedup, transitions, events, submission safety."""

from __future__ import annotations

import itertools
import sqlite3
from datetime import timedelta

import pytest

from interviewmaxxing_core import (
    TERMINAL_STATES,
    TRANSITIONS,
    ApplicationState,
    ApplicationStore,
    ChoiceValue,
    ClaimLost,
    ClaimUnavailable,
    EvidenceKind,
    EvidenceRef,
    FieldOption,
    IdentityConflict,
    InvalidTransition,
    ReconciliationMethod,
    RequestDisposition,
    SubmissionBlocked,
    SubmissionObservation,
    SubmissionOutcome,
    SubmissionReconciliation,
    TextValue,
    UserInput,
    can_transition,
)

S = ApplicationState
URL = "https://jobs.mock.example/mock-co/4012?utm_source=newsletter"
URL_ALIAS = "https://mock-co.example/careers/senior-paid-media?gh_jid=4012"
CAND = "cand_avery_example"


def _events(store, app_id):
    return [e.event for e in store.list_events(app_id)]


def _to_filling(store, app_id, owner="w1", packet=None):
    claim = store.claim(app_id, owner)
    store.transition(claim, S.INSPECTING)
    if packet is not None:
        store.save_packet(claim, packet)
    store.transition(claim, S.PACKET_READY)
    store.transition(claim, S.FILLING)
    return claim


def _packet_for(mock_packet, app):
    return mock_packet.model_copy(
        update={"application_id": app.id, "job_id": app.job_id, "candidate_id": app.candidate_id}
    )


# --- state machine table -----------------------------------------------------------


def test_transition_table_covers_every_state():
    assert set(TRANSITIONS) == set(ApplicationState)
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset()


def test_submission_states_cannot_flow_back_into_work():
    work = {S.INSPECTING, S.PACKET_READY, S.FILLING, S.SUBMITTING, S.NEEDS_INPUT}
    for src in (S.SUBMITTED, S.SUBMITTING, S.SUBMISSION_UNKNOWN):
        assert not (TRANSITIONS[src] & (work - {S.FILLING, S.NEEDS_INPUT}))
    # SUBMITTING may only return to work on a proven non-submission (FILLING/NEEDS_INPUT);
    # SUBMISSION_UNKNOWN never returns to work directly.
    assert TRANSITIONS[S.SUBMISSION_UNKNOWN] == {S.SUBMITTED, S.FAILED_RETRYABLE}
    reaching_submitted = {src for src, dsts in TRANSITIONS.items() if S.SUBMITTED in dsts}
    assert reaching_submitted == {S.SUBMITTING, S.SUBMISSION_UNKNOWN}
    reaching_submitting = {src for src, dsts in TRANSITIONS.items() if S.SUBMITTING in dsts}
    assert reaching_submitting == {S.FILLING}


def test_happy_path_is_valid():
    path = [S.REQUESTED, S.INSPECTING, S.PACKET_READY, S.FILLING, S.INSPECTING, S.PACKET_READY,
            S.FILLING, S.SUBMITTING, S.SUBMITTED]
    assert all(can_transition(a, b) for a, b in itertools.pairwise(path))


# --- requests and deduplication ----------------------------------------------------


def test_first_request_creates_requested_application_and_event(store):
    result = store.record_request(CAND, URL)
    assert result.disposition is RequestDisposition.NEW and result.may_proceed
    app = result.application
    assert app.state is S.REQUESTED and app.version == 1
    assert result.request.application_url == URL
    assert result.request.selection_source == "USER_PROVIDED"
    assert result.job.normalized_url == "https://jobs.mock.example/mock-co/4012"
    [event] = store.list_events(app.id)
    assert (event.event, event.to_state) == ("application.requested", S.REQUESTED)
    assert event.metadata["request_id"] == result.request.id


def test_repeated_request_reuses_application(store):
    first = store.record_request(CAND, URL)
    again = store.record_request(CAND, "https://JOBS.mock.example/mock-co/4012/?fbclid=zz")
    assert again.application.id == first.application.id
    assert again.disposition is RequestDisposition.RESUMABLE
    assert again.request.id != first.request.id
    assert [r.id for r in store.list_requests(first.application.id)] == [
        first.request.id, again.request.id
    ]
    assert _events(store, first.application.id) == [
        "application.requested", "application.request_repeated"
    ]
    assert len(store.list_applications()) == 1


def test_meaningful_query_parameters_are_different_jobs(store):
    a = store.record_request(CAND, "https://mock-co.example/careers?gh_jid=1")
    b = store.record_request(CAND, "https://mock-co.example/careers?gh_jid=2")
    assert a.job.id != b.job.id and a.application.id != b.application.id


def test_candidates_are_independent(store):
    a = store.record_request(CAND, URL)
    b = store.record_request("cand_other", URL)
    assert a.job.id == b.job.id
    assert a.application.id != b.application.id
    assert b.disposition is RequestDisposition.NEW


def test_uniqueness_is_enforced_by_the_database(store, store_path):
    app = store.record_request(CAND, URL).application
    raw = sqlite3.connect(store_path)
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            "INSERT INTO applications (id, request_id, job_id, candidate_id, state, version,"
            " created_at, updated_at) VALUES ('app_x', 'req_x', ?, ?, 'REQUESTED', 1, 'now', 'now')",
            (app.job_id, CAND),
        )
    raw.close()


# --- transitions -------------------------------------------------------------------


def test_invalid_transitions_are_rejected_without_side_effects(store):
    app = store.record_request(CAND, URL).application
    claim = store.claim(app.id, "w1")
    for bad in (S.FILLING, S.PACKET_READY, S.SUBMITTED, S.SUBMITTING, S.SUBMISSION_UNKNOWN):
        with pytest.raises(InvalidTransition):
            store.transition(claim, bad)
    with pytest.raises(InvalidTransition, match="failure_reason"):
        store.transition(claim, S.FAILED_RETRYABLE)
    with pytest.raises(InvalidTransition, match="reason"):
        store.transition(claim, S.DUPLICATE)
    after = store.get_application(app.id)
    assert after.state is S.REQUESTED and after.version == app.version
    assert _events(store, app.id) == ["application.requested"]


def test_every_transition_emits_one_event_with_states(store):
    app = store.record_request(CAND, URL).application
    claim = store.claim(app.id, "w1")
    store.transition(claim, S.INSPECTING)
    store.transition(claim, S.PACKET_READY, metadata={"form_step": 0})
    events = store.list_events(app.id)
    assert [(e.from_state, e.to_state) for e in events] == [
        (None, S.REQUESTED), (S.REQUESTED, S.INSPECTING), (S.INSPECTING, S.PACKET_READY)
    ]
    assert events[-1].metadata == {"form_step": 0}
    assert events[-1].actor == "w1"
    assert [e.sequence for e in events] == sorted(e.sequence for e in events)


def test_clear_failure_then_retry_and_permanent_failure(store):
    app = store.record_request(CAND, URL).application
    claim = store.claim(app.id, "w1")
    store.transition(claim, S.INSPECTING)
    failed = store.transition(claim, S.FAILED_RETRYABLE, failure_reason="page failed to load")
    assert failed.failure_reason == "page failed to load"
    again = store.record_request(CAND, URL)
    assert again.disposition is RequestDisposition.RESUMABLE
    retry = store.transition(claim, S.INSPECTING)
    assert retry.failure_reason is None
    done = store.transition(claim, S.FAILED_PERMANENT, failure_reason="job closed")
    assert done.claim_owner is None  # terminal states release the claim
    assert store.record_request(CAND, URL).disposition is RequestDisposition.CLOSED
    claim = store.claim(app.id, "w2")
    with pytest.raises(InvalidTransition):
        store.transition(claim, S.INSPECTING)


# --- claims ------------------------------------------------------------------------


def test_claims_are_exclusive_and_expire(store, clock):
    app = store.record_request(CAND, URL).application
    c1 = store.claim(app.id, "w1", ttl=timedelta(minutes=1))
    with pytest.raises(ClaimUnavailable):
        store.claim(app.id, "w2")
    c1 = store.renew(c1, ttl=timedelta(minutes=2))
    clock.advance(seconds=90)
    with pytest.raises(ClaimUnavailable):
        store.claim(app.id, "w2")
    clock.advance(seconds=31)
    c2 = store.claim(app.id, "w2")
    with pytest.raises(ClaimLost):
        store.transition(c1, S.INSPECTING)
    with pytest.raises(ClaimLost):
        store.renew(c1)
    store.release(c1)  # stale release is a no-op
    assert store.get_application(app.id).claim_owner == "w2"
    store.release(c2)
    store.claim(app.id, "w3")


# --- normal submission -------------------------------------------------------------


def test_confirmed_submission_writes_receipt_and_blocks_resubmission(
    store, mock_packet, mock_identity, accepted_observation
):
    app = store.record_request(CAND, URL).application
    claim = store.claim(app.id, "w1")
    store.transition(claim, S.INSPECTING)
    store.bind_job_identity(claim, mock_identity)
    packet = _packet_for(mock_packet, app)
    store.save_packet(claim, packet)
    store.transition(claim, S.PACKET_READY)
    store.transition(claim, S.FILLING)
    attempt = store.begin_submission(claim, packet_id=packet.id)
    assert store.get_application(app.id).state is S.SUBMITTING
    assert attempt.attempt_number == 1 and attempt.finished_at is None

    done = store.record_submission_outcome(claim, attempt.id, accepted_observation)
    assert done.state is S.SUBMITTED
    assert done.submitted_at == attempt.started_at
    assert done.claim_owner is None

    receipt = store.get_receipt(app.id)
    assert receipt is not None
    assert receipt.application_url == URL
    assert (receipt.company, receipt.title) == ("Mock Co", "Senior Paid Media Manager")
    assert receipt.confirmation_reference == "MOCK-APP-000123"
    assert receipt.signals == accepted_observation.signals
    assert {e.id for e in receipt.evidence} == {"ev_confirm_png", "ev_confirm_txt"}
    assert receipt.attempt_id == attempt.id

    assert _events(store, app.id) == [
        "application.requested",
        "application.inspecting",
        "job.identity_bound",
        "packet.saved",
        "application.packet_ready",
        "application.filling",
        "application.submitting",
        "application.submitted",
    ]
    again = store.record_request(CAND, URL_ALIAS.replace("4012", "4012"))
    assert again.application.id != app.id  # unbound alias is a new job until identity is seen
    assert store.record_request(CAND, URL).disposition is RequestDisposition.ALREADY_SUBMITTED

    claim = store.claim(app.id, "w2")
    with pytest.raises(SubmissionBlocked):
        store.begin_submission(claim)
    with pytest.raises(InvalidTransition):
        store.transition(claim, S.INSPECTING)
    with pytest.raises(InvalidTransition):
        store.record_submission_outcome(claim, attempt.id, accepted_observation)


def test_database_refuses_to_undo_submitted_or_edit_history(store, store_path, accepted_observation):
    app = store.record_request(CAND, URL).application
    claim = _to_filling(store, app.id)
    attempt = store.begin_submission(claim)
    store.record_submission_outcome(claim, attempt.id, accepted_observation)
    raw = sqlite3.connect(store_path)
    with pytest.raises(sqlite3.IntegrityError, match="SUBMITTED is final"):
        raw.execute("UPDATE applications SET state = 'FILLING' WHERE id = ?", (app.id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw.execute("DELETE FROM events WHERE application_id = ?", (app.id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw.execute("UPDATE events SET event = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="final"):
        raw.execute("UPDATE receipts SET body = '{}'")
    raw.close()


def test_submission_only_from_filling_and_one_open_attempt(store):
    app = store.record_request(CAND, URL).application
    claim = store.claim(app.id, "w1")
    with pytest.raises(InvalidTransition, match="FILLING"):
        store.begin_submission(claim)
    store.transition(claim, S.INSPECTING)
    store.transition(claim, S.PACKET_READY)
    store.transition(claim, S.FILLING)
    store.begin_submission(claim)
    with pytest.raises(SubmissionBlocked):
        store.begin_submission(claim)
    assert len(store.list_attempts(app.id)) == 1


def test_definite_non_submission_returns_to_filling(store, accepted_observation):
    app = store.record_request(CAND, URL).application
    claim = _to_filling(store, app.id)
    first = store.begin_submission(claim)
    rejected = SubmissionObservation(
        outcome=SubmissionOutcome.NOT_SUBMITTED,
        validation_errors=["Phone number is invalid"],
        next_state="FILLING",
    )
    assert store.record_submission_outcome(claim, first.id, rejected).state is S.FILLING
    second = store.begin_submission(claim)
    assert second.attempt_number == 2
    store.record_submission_outcome(claim, second.id, accepted_observation)
    attempts = store.list_attempts(app.id)
    assert [a.outcome for a in attempts] == [SubmissionOutcome.NOT_SUBMITTED,
                                            SubmissionOutcome.ACCEPTED]
    assert store.get_receipt(app.id).attempt_id == second.id


def test_non_submission_can_route_to_needs_input_or_failure(store):
    app = store.record_request(CAND, URL).application
    claim = _to_filling(store, app.id)
    attempt = store.begin_submission(claim)
    closed = SubmissionObservation(
        outcome=SubmissionOutcome.NOT_SUBMITTED,
        signals=["page says 'This job is no longer accepting applications'; form disabled"],
        next_state="FAILED_PERMANENT",
    )
    out = store.record_submission_outcome(claim, attempt.id, closed)
    assert out.state is S.FAILED_PERMANENT
    assert "no longer accepting" in (out.failure_reason or "")


# --- uncertain submission and crash handling ------------------------------------------


def test_unknown_outcome_blocks_retry_until_reconciled(store):
    app = store.record_request(CAND, URL).application
    claim = _to_filling(store, app.id)
    attempt = store.begin_submission(claim)
    unknown = SubmissionObservation(outcome=SubmissionOutcome.UNKNOWN, detail="timeout")
    assert store.record_submission_outcome(claim, attempt.id, unknown).state is S.SUBMISSION_UNKNOWN

    result = store.record_request(CAND, URL)
    assert result.disposition is RequestDisposition.SUBMISSION_UNKNOWN and not result.may_proceed
    for bad in (S.INSPECTING, S.FILLING, S.FAILED_RETRYABLE, S.WITHDRAWN):
        with pytest.raises(InvalidTransition):
            store.transition(claim, bad)
    with pytest.raises(SubmissionBlocked):
        store.begin_submission(claim)

    out = store.reconcile_submission(
        claim,
        SubmissionReconciliation(
            outcome=SubmissionOutcome.NOT_SUBMITTED,
            method=ReconciliationMethod.ATS_CANDIDATE_PORTAL,
            detail="candidate portal lists no application for this job",
        ),
    )
    assert out.state is S.FAILED_RETRYABLE
    assert store.transition(claim, S.INSPECTING).state is S.INSPECTING
    assert store.get_receipt(app.id) is None


def test_crash_during_submission_becomes_unknown_then_reconciles_accepted(store, clock):
    app = store.record_request(CAND, URL).application
    claim = _to_filling(store, app.id, owner="crashing-worker")
    attempt = store.begin_submission(claim)
    # The worker dies here (no outcome recorded). Its lease still protects the attempt.
    with pytest.raises(ClaimUnavailable):
        store.claim(app.id, "second-worker")
    clock.advance(minutes=11)  # past SUBMISSION_LEASE
    claim2 = store.claim(app.id, "second-worker")
    app2 = store.get_application(app.id)
    assert app2.state is S.SUBMISSION_UNKNOWN
    [closed] = store.list_attempts(app.id)
    assert closed.id == attempt.id and closed.outcome == "INTERRUPTED"
    last = store.list_events(app.id)[-1]
    assert (last.event, last.from_state, last.metadata["reason"]) == (
        "application.submission_unknown", S.SUBMITTING, "interrupted"
    )
    # The crashed worker cannot come back and claim anything.
    with pytest.raises(ClaimLost):
        store.record_submission_outcome(
            claim, attempt.id,
            SubmissionObservation(outcome=SubmissionOutcome.ACCEPTED, signals=["late"]),
        )
    with pytest.raises(SubmissionBlocked):
        store.begin_submission(claim2)

    out = store.reconcile_submission(
        claim2,
        SubmissionReconciliation(
            outcome=SubmissionOutcome.ACCEPTED,
            method=ReconciliationMethod.CONFIRMATION_EMAIL,
            detail="confirmation email 'We received your application' at 20:03",
            confirmation_reference="MOCK-APP-777",
            evidence=[EvidenceRef(kind=EvidenceKind.CONFIRMATION_EMAIL,
                                  description="email subject: Application received")],
        ),
    )
    assert out.state is S.SUBMITTED and out.submitted_at == attempt.started_at
    receipt = store.get_receipt(app.id)
    assert receipt.reconciliation_method is ReconciliationMethod.CONFIRMATION_EMAIL
    assert receipt.confirmation_reference == "MOCK-APP-777"


def test_recovery_sweep_marks_orphaned_submissions(store, clock):
    live = store.record_request(CAND, URL).application
    orphan = store.record_request(CAND, URL_ALIAS).application
    c_orphan = _to_filling(store, orphan.id, owner="orphan")
    store.begin_submission(c_orphan)
    clock.advance(minutes=11)  # the orphan's submission lease has lapsed
    store.begin_submission(_to_filling(store, live.id, owner="live"))
    changed = store.recover_interrupted_submissions()
    assert [a.id for a in changed] == [orphan.id]
    assert store.get_application(orphan.id).state is S.SUBMISSION_UNKNOWN
    assert store.get_application(live.id).state is S.SUBMITTING


# --- missing input -----------------------------------------------------------------


def test_missing_input_pause_and_resume(store, clock, mock_packet, mock_form):
    app = store.record_request(CAND, URL).application
    claim = store.claim(app.id, "run-1")
    store.transition(claim, S.INSPECTING)
    packet = _packet_for(mock_packet, app)
    store.save_packet(claim, packet)
    paused = store.transition(claim, S.NEEDS_INPUT,
                              metadata={"missing_field_ids": packet.unresolved_fields})
    assert paused.state is S.NEEDS_INPUT
    store.release(claim)

    # Later, a new process resumes.
    assert store.record_request(CAND, URL).disposition is RequestDisposition.RESUMABLE
    saved = store.latest_packet(app.id)
    assert saved == packet and saved.unresolved_fields == ["gender", "why_us"]
    gender, why_us = saved.missing_inputs
    claim = store.claim(app.id, "run-2")
    store.save_user_inputs(claim, [
        UserInput.answering(gender, ChoiceValue(value="decline",
                                                label="I decline to self-identify")),
        UserInput.answering(why_us, TextValue(text="Draft")),
    ])
    clock.advance(seconds=5)
    store.save_user_inputs(claim, [UserInput.answering(why_us, TextValue(text="Final"))])
    inputs = {i.field_id: i.value for i in store.get_user_inputs(app.id, mock_form)}
    assert inputs["why_us"] == TextValue(text="Final")
    assert inputs["gender"].value == "decline"  # type: ignore[union-attr]
    assert store.transition(claim, S.INSPECTING).state is S.INSPECTING
    received = [e for e in store.list_events(app.id) if e.event == "input.received"]
    assert received[0].metadata["inputs"][0]["form_scope"] == mock_form.scope.key


def test_same_field_id_on_two_steps_keeps_both_answers(store, multistep_forms):
    step0, step1 = multistep_forms
    assert step0.field("question_0").fingerprint != step1.field("question_0").fingerprint
    app = store.record_request(CAND, URL).application
    claim = store.claim(app.id, "run")
    relocate = UserInput.for_field(step0, "question_0", ChoiceValue(value="no", label="No"))
    years = UserInput.for_field(step1, "question_0", TextValue(text="7"))
    store.save_user_inputs(claim, [relocate])
    store.save_user_inputs(claim, [years])  # later, same field id, different step

    assert store.get_user_inputs(app.id, step0) == [relocate]
    assert store.get_user_inputs(app.id, step1) == [years]
    assert {u.id for u in store.list_user_inputs(app.id)} == {relocate.id, years.id}


def test_changed_question_under_a_reused_field_id_is_not_answered(store, multistep_forms):
    step0, _ = multistep_forms
    app = store.record_request(CAND, URL).application
    claim = store.claim(app.id, "run")
    store.save_user_inputs(
        claim, [UserInput.for_field(step0, "question_0", ChoiceValue(value="yes", label="Yes"))]
    )
    # Re-inspection finds the same id and step asking something else.
    changed_field = step0.field("question_0").model_copy(
        update={"label": "Are you willing to relocate to Denver, CO?"}
    )
    reinspected = step0.model_copy(update={"fields": [changed_field]})
    assert store.get_user_inputs(app.id, reinspected) == []
    # Different options under the same label also count as a different question.
    reoptioned = step0.model_copy(update={"fields": [step0.field("question_0").model_copy(
        update={"options": [FieldOption(value="yes", label="Yes, with support"),
                            FieldOption(value="no", label="No")]})]})
    assert store.get_user_inputs(app.id, reoptioned) == []
    # The same question re-inspected (new selector, new timestamp) still matches.
    moved = step0.model_copy(update={"fields": [step0.field("question_0").model_copy(
        update={"selector": "#new-id"})]})
    assert len(store.get_user_inputs(app.id, moved)) == 1


def test_v1_database_is_migrated_and_unscoped_inputs_are_ignored(store_path, clock):
    store_path.parent.mkdir(parents=True)
    raw = sqlite3.connect(store_path)
    raw.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('schema_version', '1');"
        "CREATE TABLE user_inputs (id TEXT PRIMARY KEY, application_id TEXT NOT NULL,"
        " field_id TEXT NOT NULL, provided_at TEXT NOT NULL, body TEXT NOT NULL);"
        "INSERT INTO user_inputs VALUES ('ui_old', 'app_old', 'question_0', 'x', '{}');"
    )
    raw.close()
    with ApplicationStore.open(store_path, clock=clock) as s:
        assert s.list_user_inputs("app_old") == []
    raw = sqlite3.connect(store_path)
    assert raw.execute("SELECT value FROM meta").fetchone() == ("2",)
    columns = {r[1] for r in raw.execute("PRAGMA table_info(user_inputs)")}
    assert {"form_scope", "field_fingerprint"} <= columns
    raw.close()


def test_packet_must_belong_to_claimed_application(store, mock_packet):
    app = store.record_request(CAND, URL).application
    claim = store.claim(app.id, "w1")
    with pytest.raises(ValueError):
        store.save_packet(claim, mock_packet)  # fixture ids are app_fixture/job_fixture


def test_custom_events_cannot_spoof_store_events(store):
    app = store.record_request(CAND, URL).application
    claim = store.claim(app.id, "browser")
    with pytest.raises(ValueError):
        store.append_event(claim, "application.submitted", {})
    event = store.append_event(claim, "form.discovered", {"fields": 13})
    assert event.event == "form.discovered" and event.metadata == {"fields": 13}


# --- identity binding / aliases ------------------------------------------------------


def test_alias_urls_merge_when_the_same_ats_job_is_observed(store, mock_identity):
    a = store.record_request(CAND, URL).application
    b = store.record_request(CAND, URL_ALIAS).application
    assert a.job_id != b.job_id

    ca = store.claim(a.id, "wa")
    store.transition(ca, S.INSPECTING)
    first = store.bind_job_identity(ca, mock_identity)
    assert first.merged_from_job_id is None and first.duplicate_of is None
    assert first.job.identity_key == "ats:mock:mock-co:4012"

    cb = store.claim(b.id, "wb")
    store.transition(cb, S.INSPECTING)
    second = store.bind_job_identity(cb, mock_identity)
    assert second.job.id == a.job_id
    assert second.merged_from_job_id == b.job_id
    assert second.duplicate_of == a.id
    assert second.application.state is S.DUPLICATE
    assert second.application.duplicate_of == a.id
    assert store.get_job(b.job_id).merged_into == a.job_id
    with pytest.raises(ClaimLost):  # DUPLICATE is terminal and released the claim
        store.transition(cb, S.PACKET_READY)

    # The alias URL now resolves to the canonical application.
    again = store.record_request(CAND, URL_ALIAS)
    assert again.application.id == a.id and again.job.id == a.job_id
    assert store.find_application(CAND, URL_ALIAS).id == a.id


def test_alias_of_a_submitted_application_is_detected(store, mock_identity, accepted_observation):
    a = store.record_request(CAND, URL).application
    ca = _to_filling(store, a.id)
    store.bind_job_identity(ca, mock_identity)
    store.record_submission_outcome(ca, store.begin_submission(ca).id, accepted_observation)

    b = store.record_request(CAND, URL_ALIAS)
    assert b.disposition is RequestDisposition.NEW  # not yet known to be the same job
    cb = store.claim(b.application.id, "wb")
    store.transition(cb, S.INSPECTING)
    bound = store.bind_job_identity(cb, mock_identity)
    assert bound.duplicate_of == a.id
    assert store.record_request(CAND, URL_ALIAS).disposition is RequestDisposition.ALREADY_SUBMITTED


def test_binding_moves_other_candidates_instead_of_duplicating(store, mock_identity):
    a = store.record_request(CAND, URL).application
    other = store.record_request("cand_other", URL_ALIAS).application
    ca = store.claim(a.id, "wa")
    store.bind_job_identity(ca, mock_identity)
    co = store.claim(other.id, "wo")
    bound = store.bind_job_identity(co, mock_identity)
    assert bound.duplicate_of is None
    assert bound.application.state is S.REQUESTED
    assert bound.application.job_id == a.job_id
    assert bound.moved_application_ids == [other.id]


def test_observed_or_redirected_url_is_not_bound_as_an_alias(store, mock_identity):
    a = store.record_request(CAND, URL).application
    ca = store.claim(a.id, "wa")
    landed = "https://redirected.example/after-redirect/4012"
    store.bind_job_identity(ca, mock_identity.model_copy(update={"observed_url": landed}))
    other = store.record_request(CAND, landed)
    assert other.disposition is RequestDisposition.NEW
    assert other.application.id != a.id


def test_binding_a_different_identity_to_a_bound_job_conflicts(store, mock_identity):
    a = store.record_request(CAND, URL).application
    ca = store.claim(a.id, "wa")
    store.bind_job_identity(ca, mock_identity)
    store.bind_job_identity(ca, mock_identity)  # idempotent
    with pytest.raises(IdentityConflict):
        store.bind_job_identity(ca, mock_identity.model_copy(update={"external_job_id": "9999"}))
    assert store.get_job(a.job_id).identity_key == mock_identity.identity_key


def test_merge_never_rewrites_a_started_submission(store, mock_identity):
    a = store.record_request(CAND, URL).application
    b = store.record_request(CAND, URL_ALIAS).application
    cb = store.claim(b.id, "wb")
    store.bind_job_identity(cb, mock_identity)  # b's job owns the identity
    ca = _to_filling(store, a.id)
    store.begin_submission(ca)
    with pytest.raises(IdentityConflict):
        store.bind_job_identity(ca, mock_identity)
    assert store.get_application(a.id).state is S.SUBMITTING
    assert store.get_job(a.job_id).merged_into is None


# --- durability ----------------------------------------------------------------------


def test_events_and_state_survive_reopen(store_path, clock, mock_identity):
    with ApplicationStore.open(store_path, clock=clock) as s1:
        app = s1.record_request(CAND, URL).application
        claim = s1.claim(app.id, "w1")
        s1.transition(claim, S.INSPECTING)
        s1.bind_job_identity(claim, mock_identity)
        before = s1.list_events(app.id)
    with ApplicationStore.open(store_path, clock=clock) as s2:
        assert s2.list_events(app.id) == before
        assert s2.get_application(app.id).state is S.INSPECTING
        assert s2.get_job(app.job_id).identity_key == mock_identity.identity_key


def test_failed_operation_rolls_back_completely(store, mock_identity):
    a = store.record_request(CAND, URL).application
    b = store.record_request(CAND, URL_ALIAS).application
    cb = store.claim(b.id, "wb")
    store.bind_job_identity(cb, mock_identity)
    ca = _to_filling(store, a.id)
    store.begin_submission(ca)
    events_before = store.list_events(a.id)
    with pytest.raises(IdentityConflict):
        store.bind_job_identity(ca, mock_identity)
    assert store.list_events(a.id) == events_before
    assert store.record_request(CAND, URL).job.id == a.job_id
