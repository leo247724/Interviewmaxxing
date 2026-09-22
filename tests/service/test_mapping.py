"""Pure mapping, configuration and recovery rules."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from interviewmaxxing_core import (
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    CandidateIdentity,
    LocalPaths,
    MissingInput,
    MissingReason,
    PostalAddress,
    SubmissionObservation,
    SubmissionOutcome,
)
from interviewmaxxing_service import ConfigError, ServiceConfig
from interviewmaxxing_service.candidate import CandidateSetupError, identity_from_input
from interviewmaxxing_service.models import CandidateProfileInput
from interviewmaxxing_service.views import Snapshot, application_view, question_id

from .conftest import ORIGIN, Harness

S = ApplicationState
NOW = datetime(2026, 9, 22, 20, 0, tzinfo=UTC)


def test_config_refuses_non_loopback(isolated_imx_home: LocalPaths) -> None:
    for host in ("0.0.0.0", "192.168.1.5", "example.test", "::"):
        with pytest.raises(ConfigError):
            ServiceConfig(paths=isolated_imx_home, allowed_origin=ORIGIN, host=host)
    for origin in ("http://evil.example", "http://127.0.0.1:4317/app", "file:///x", "*"):
        with pytest.raises(ConfigError):
            ServiceConfig(paths=isolated_imx_home, allowed_origin=origin)
    ok = ServiceConfig(paths=isolated_imx_home, allowed_origin="HTTP://LOCALHOST:4317/", host="::1")
    assert ok.allowed_origin == "http://localhost:4317"
    with pytest.raises(ConfigError):
        ServiceConfig.from_env({"IMX_HOME": "/tmp/x"})


def _input(**overrides: str) -> CandidateProfileInput:
    base = {
        "firstName": "Avery", "lastName": "Example", "email": "avery@example.test",
        "phone": "", "location": "Springfield, Oregon, USA", "linkedinUrl": "", "websiteUrl": "",
    }
    base.update(overrides)
    return CandidateProfileInput.model_validate(base)


def test_identity_mapping_keeps_what_the_user_did_not_change() -> None:
    current = CandidateIdentity(
        first_name="Avery", last_name="Example", preferred_name="Ave",
        email="avery@example.test", github_url="https://github.example/avery",
        address=PostalAddress(street="1 Fictional Way", city="Springfield", region="Oregon",
                              postal_code="00000", country="USA"),
        verified_at=NOW,
    )
    same = identity_from_input(_input(), current=current, now=NOW + timedelta(days=1))
    assert same is current  # nothing changed: original confirmation time kept

    moved = identity_from_input(
        _input(phone="+1 555 0100"), current=current, now=NOW + timedelta(days=1)
    )
    assert moved.verified_at == NOW + timedelta(days=1)
    assert moved.address == current.address  # location unchanged: street/postal kept
    assert moved.preferred_name == "Ave" and moved.github_url == current.github_url

    relocated = identity_from_input(_input(location="Lyon, France"), current=current, now=NOW)
    assert relocated.address == PostalAddress(city="Lyon", country="France")
    assert identity_from_input(_input(location=""), current=None, now=NOW).address == PostalAddress()
    with pytest.raises(CandidateSetupError) as err:
        identity_from_input(_input(location="a,,b"), current=None, now=NOW)
    assert err.value.field == "location"
    with pytest.raises(CandidateSetupError) as err:
        identity_from_input(_input(firstName="  "), current=None, now=NOW)
    assert err.value.field == "firstName"


def _snapshot(store: ApplicationStore, app_id: str, **extra: object) -> Snapshot:
    app = store.get_application(app_id)
    return Snapshot(
        application=app, job=store.get_job(app.job_id), request=store.list_requests(app_id)[0],
        events=store.list_events(app_id), attempts=store.list_attempts(app_id),
        evidence=store.list_evidence(app_id), receipt=store.get_receipt(app_id),
        packet=store.latest_packet(app_id), user_inputs=store.list_user_inputs(app_id),
        **extra,  # type: ignore[arg-type]
    )


def test_question_ids_follow_question_identity(mock_form: ApplicationForm) -> None:
    field = mock_form.field("gender")
    item = MissingInput.for_field(mock_form, field, reason=MissingReason.NO_ANSWER, prompt="?")
    again = MissingInput.for_field(mock_form, field, reason=MissingReason.NO_ANSWER, prompt="?")
    assert item.id != again.id and question_id(item) == question_id(again)
    changed = mock_form.model_copy(update={"fields": [
        field.model_copy(update={"help_text": "Now a different question"})
    ]})
    other = MissingInput.for_field(
        changed, changed.field("gender"), reason=MissingReason.NO_ANSWER, prompt="?"
    )
    assert question_id(other) != question_id(item)
    step1 = mock_form.model_copy(update={"step": 1})
    assert question_id(
        MissingInput.for_field(step1, field, reason=MissingReason.NO_ANSWER, prompt="?")
    ) != question_id(item)


def test_user_action_and_unsupported_become_interactions(
    store: ApplicationStore, mock_form: ApplicationForm
) -> None:
    app = store.record_request("default", "https://jobs.example.test/f/1").application
    claim = store.claim(app.id, "t")
    store.transition(claim, S.INSPECTING)
    upload = mock_form.field("resume")
    form = mock_form.model_copy(update={"fields": [upload]})
    packet = ApplicationPacket(
        application_id=app.id, job_id=app.job_id, candidate_id="default",
        form_url=form.url, form_step=0, form_fingerprint=form.fingerprint,
        missing_inputs=[MissingInput.for_field(
            form, upload, reason=MissingReason.UNSUPPORTED_CONTROL, prompt="Upload"
        )],
    )
    store.save_packet(claim, packet)
    store.transition(claim, S.NEEDS_INPUT)
    view = application_view(_snapshot(store, app.id), public_base="/api/imx")
    assert view.needs is not None and view.needs.kind == "interaction"
    assert view.needs.interaction == "VERIFICATION"
    assert "Resume" in view.needs.instructions

    action = MissingInput(field_id=None, label="Solve the CAPTCHA", reason=MissingReason.USER_ACTION,
                          prompt="Complete the CAPTCHA in the browser")
    store.transition(claim, S.INSPECTING)
    store.save_packet(claim, packet.model_copy(update={"id": "pkt_2", "missing_inputs": [action]}))
    store.transition(claim, S.NEEDS_INPUT)
    view = application_view(_snapshot(store, app.id), public_base="/api/imx")
    assert view.needs is not None and view.needs.interaction == "CAPTCHA"
    assert view.progress is not None and view.progress.page == 1


def test_duplicate_shows_the_confirmed_prior(
    store: ApplicationStore, accepted_observation: SubmissionObservation
) -> None:
    first = store.record_request("default", "https://jobs.example.test/f/1").application
    claim = store.claim(first.id, "t")
    for state in (S.INSPECTING, S.PACKET_READY, S.FILLING):
        store.transition(claim, state)
    attempt = store.begin_submission(claim)
    store.record_submission_outcome(claim, attempt.id, accepted_observation)
    second = store.record_request("default", "https://jobs.example.test/f/1-alias").application
    c2 = store.claim(second.id, "t")
    store.transition(c2, S.DUPLICATE, reason="same job as an earlier application")
    from interviewmaxxing_service.views import PriorRecord

    dup = store.get_application(second.id)
    prior = PriorRecord(
        application=store.get_application(first.id),
        application_url="https://jobs.example.test/f/1",
        receipt=store.get_receipt(first.id),
    )
    view = application_view(_snapshot(store, second.id, prior=prior), public_base="/api/imx")
    assert dup.state is S.DUPLICATE
    assert view.prior is not None and view.prior.application_id == first.id
    assert view.prior.confirmation_reference == accepted_observation.confirmation_reference
    assert view.receipt is None and view.failure is None


def test_interrupted_submit_surfaces_as_unknown(harness: Harness) -> None:
    with harness.store() as store:
        app = store.record_request("default", "https://jobs.example.test/f/9").application
        claim = store.claim(app.id, "crashed-runner")
        for state in (S.INSPECTING, S.PACKET_READY, S.FILLING):
            store.transition(claim, state)
        store.begin_submission(claim)
    # The process died mid-submit: its lease lapses.
    past = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    conn = sqlite3.connect(harness.paths.state_db)
    conn.execute("UPDATE applications SET claim_expires_at = ? WHERE id = ?", (past, app.id))
    conn.commit()
    conn.close()
    view = harness.client.get(f"/applications/{app.id}").json
    assert view["state"] == "SUBMISSION_UNKNOWN"
    assert view["uncertain"]["reason"].startswith("Submitting was interrupted")
    assert harness.client.post(f"/applications/{app.id}/resume", {}).status == 409
    assert harness.site.accepted_posts == 0


def test_observation_unknown_is_not_a_receipt(harness: Harness) -> None:
    with harness.store() as store:
        app = store.record_request("default", "https://jobs.example.test/f/10").application
        claim = store.claim(app.id, "t")
        for state in (S.INSPECTING, S.PACKET_READY, S.FILLING):
            store.transition(claim, state)
        attempt = store.begin_submission(claim)
        store.record_submission_outcome(claim, attempt.id, SubmissionObservation(
            outcome=SubmissionOutcome.UNKNOWN, detail="timed out"))
        store.release(claim)
    view = harness.client.get(f"/applications/{app.id}").json
    assert view["receipt"] is None
    assert view["uncertain"]["reason"] == (
        "Submit was dispatched, but the site didn't show a confirmation."
    )
    assert view["events"][-1]["tone"] == "warning"
