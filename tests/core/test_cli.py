"""The interviewmaxxing CLI: help, state-blocked runs, answers, inspection, reconcile.

Flows that open a real browser are covered by ``e2e/`` against the mock ATS."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from interviewmaxxing_cli.main import (
    EXIT_BLOCKED,
    EXIT_INCOMPLETE,
    EXIT_OK,
    EXIT_UNCERTAIN,
    EXIT_USAGE,
    main,
)
from interviewmaxxing_core import (
    ApplicationState,
    ApplicationStore,
    LocalPaths,
    SubmissionObservation,
    SubmissionOutcome,
)

URL = "https://jobs.mock.example/mock-co/4012"
S = ApplicationState
ENTRYPOINT = Path(sys.executable).with_name("interviewmaxxing")


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _store(paths: LocalPaths) -> ApplicationStore:
    return ApplicationStore.open(paths.state_db)


def _drive_to_submitting(store: ApplicationStore, app_id: str):
    claim = store.claim(app_id, "test")
    for state in (S.INSPECTING, S.PACKET_READY, S.FILLING):
        store.transition(claim, state)
    return claim, store.begin_submission(claim)


def test_installed_entrypoint_help():
    result = subprocess.run([str(ENTRYPOINT), "--help"], capture_output=True, text=True,
                            check=True)
    for command in ("apply", "status", "events", "receipt", "reconcile", "paths"):
        assert command in result.stdout
    assert "IMX_HOME" in result.stdout
    for sub in ("apply", "status", "receipt", "reconcile"):
        subprocess.run([str(ENTRYPOINT), sub, "--help"], capture_output=True, check=True)


def test_command_is_required(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == EXIT_USAGE


def test_apply_without_a_profile_explains_and_never_opens_a_browser(capsys, isolated_imx_home):
    code, out, _ = run(capsys, "apply", URL + "?utm_source=x", "--headless")
    assert code == EXIT_INCOMPLETE
    assert "Candidate profile unavailable" in out and "profile.json" in out
    assert "SUBMITTED" not in out
    with _store(isolated_imx_home) as store:
        [app] = store.list_applications()
        assert app.state is S.REQUESTED and app.candidate_id == "default"
        assert store.pinned_resume(app.id) is None
    assert not isolated_imx_home.browser_dir.joinpath("Default").exists()


def test_apply_rejects_invalid_urls(capsys):
    code, _, err = run(capsys, "apply", "ftp://jobs.example/x")
    assert code == EXIT_USAGE and "http(s)" in err


def test_apply_refuses_when_already_submitted_or_unknown(capsys, isolated_imx_home,
                                                         accepted_observation):
    isolated_imx_home.ensure()
    with _store(isolated_imx_home) as store:
        done = store.record_request("default", URL).application
        claim, attempt = _drive_to_submitting(store, done.id)
        store.record_submission_outcome(claim, attempt.id, accepted_observation)
        unsure = store.record_request("default", URL + "/other").application
        claim, attempt = _drive_to_submitting(store, unsure.id)
        store.record_submission_outcome(
            claim, attempt.id, SubmissionObservation(outcome=SubmissionOutcome.UNKNOWN)
        )
    code, out, _ = run(capsys, "apply", URL, "--headless")
    assert code == EXIT_BLOCKED and "Already submitted" in out
    code, out, _ = run(capsys, "apply", URL + "/other", "--headless")
    assert code == EXIT_UNCERTAIN and f"reconcile {unsure.id}" in out
    code, out, _ = run(capsys, "resume", unsure.id, "--headless", "--json")
    assert code == EXIT_UNCERTAIN and json.loads(out)["state"] == "SUBMISSION_UNKNOWN"
    with _store(isolated_imx_home) as store:
        assert len(store.list_attempts(unsure.id)) == 1


def _needs_input(store: ApplicationStore, packet, app) -> None:
    """Record NEEDS_INPUT exactly as the runner does (packet + event metadata)."""
    claim = store.claim(app.id, "runner")
    store.transition(claim, S.INSPECTING)
    store.save_packet(claim, packet)
    store.transition(claim, S.NEEDS_INPUT, metadata={
        "missing_inputs": [m.model_dump(mode="json") for m in packet.missing_inputs],
        "reason": "missing answers",
    })
    store.release(claim)


def test_answer_saves_only_valid_answers_to_recorded_questions(
    capsys, isolated_imx_home, mock_packet, tmp_path
):
    isolated_imx_home.ensure()
    with _store(isolated_imx_home) as store:
        app = store.record_request("default", URL).application
        packet = mock_packet.model_copy(update={
            "application_id": app.id, "job_id": app.job_id, "candidate_id": app.candidate_id})
        _needs_input(store, packet, app)

    code, out, _ = run(capsys, "status", app.id)
    assert "gender" in out and "why_us" in out and f"answer {app.id}" in out
    code, _, err = run(capsys, "answer", app.id, "--set", "gender=Prefer not to say")
    assert code == EXIT_USAGE and "not one of the options" in err
    code, _, err = run(capsys, "answer", app.id, "--set", "favourite_colour=blue")
    assert code == EXIT_USAGE and "not a recorded question" in err
    code, _, err = run(capsys, "answer", app.id)
    assert code == EXIT_USAGE
    with _store(isolated_imx_home) as store:
        assert store.list_user_inputs(app.id) == []

    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"why_us": "Mock Co builds useful tools."}))
    code, out, _ = run(capsys, "answer", app.id, "--set", "gender=i decline to self-identify",
                       "--answers", str(answers))
    assert code == EXIT_OK and f"resume {app.id}" in out
    with _store(isolated_imx_home) as store:
        saved = {u.field_id: u for u in store.list_user_inputs(app.id)}
        assert saved["gender"].value.value == "decline"  # label matched, machine value kept
        assert saved["why_us"].question == "Why do you want to work at Mock Co?"
        assert all(u.reuse.value == "APPLICATION" for u in saved.values())


def test_answer_refuses_applications_not_waiting_for_input(capsys, isolated_imx_home):
    isolated_imx_home.ensure()
    with _store(isolated_imx_home) as store:
        app = store.record_request("default", URL).application
    code, _, err = run(capsys, "answer", app.id, "--set", "x=y")
    assert code == EXIT_BLOCKED and "not waiting for answers" in err


def test_status_events_and_receipt(capsys, isolated_imx_home, accepted_observation):
    code, out, _ = run(capsys, "status")
    assert code == EXIT_OK and "No applications" in out
    isolated_imx_home.ensure()
    with _store(isolated_imx_home) as store:
        app = store.record_request("default", URL).application
    code, out, _ = run(capsys, "receipt", app.id)
    assert code == EXIT_BLOCKED and "No receipt" in out and "REQUESTED" in out

    with _store(isolated_imx_home) as store:
        claim, attempt = _drive_to_submitting(store, app.id)
        store.record_submission_outcome(claim, attempt.id, accepted_observation)

    code, out, _ = run(capsys, "status")
    assert code == EXIT_OK and app.id in out and "SUBMITTED" in out
    code, out, _ = run(capsys, "status", app.id, "--json")
    data = json.loads(out)
    assert data["application"]["state"] == "SUBMITTED"
    assert data["attempts"][0]["outcome"] == "ACCEPTED"
    code, out, _ = run(capsys, "events", app.id, "--json")
    assert [e["event"] for e in json.loads(out)][-1] == "application.submitted"
    code, out, _ = run(capsys, "events", app.id)
    assert "application.submitting FILLING -> SUBMITTING" in out
    code, out, _ = run(capsys, "receipt", app.id)
    assert code == EXIT_OK and "MOCK-APP-000123" in out and URL in out
    code, out, _ = run(capsys, "receipt", app.id, "--json")
    assert json.loads(out)["confirmation_reference"] == "MOCK-APP-000123"


def test_status_flags_interrupted_submission(capsys, isolated_imx_home):
    isolated_imx_home.ensure()
    with _store(isolated_imx_home) as store:
        app = store.record_request("default", URL).application
        claim, _ = _drive_to_submitting(store, app.id)
        store.release(claim)  # owner vanished without an outcome
    _, out, _ = run(capsys, "status", app.id)
    assert "interrupted; will become SUBMISSION_UNKNOWN" in out


def test_reconcile_never_promotes_a_user_report(capsys, isolated_imx_home):
    """There is no CLI path that turns "I got an email" into a receipt or into permission
    to resubmit; reconcile only re-reads the site."""
    with pytest.raises(SystemExit) as exc:
        main(["reconcile", "app_x", "--accepted", "--detail", "confirmation email"])
    assert exc.value.code == EXIT_USAGE
    isolated_imx_home.ensure()
    with _store(isolated_imx_home) as store:
        app = store.record_request("default", URL).application
        claim, attempt = _drive_to_submitting(store, app.id)
        store.record_submission_outcome(
            claim, attempt.id, SubmissionObservation(outcome=SubmissionOutcome.UNKNOWN)
        )
        store.release(claim)
        requested = store.record_request("default", URL + "/requested").application
    # The job identity was never observed, so nothing on a page could be tied to it.
    code, out, _ = run(capsys, "reconcile", app.id, "--headless")
    assert code == EXIT_UNCERTAIN and "cannot be tied" in out
    code, out, _ = run(capsys, "reconcile", requested.id, "--headless")
    assert code == EXIT_BLOCKED and "Only an uncertain submission" in out
    with _store(isolated_imx_home) as store:
        assert store.get_application(app.id).state is S.SUBMISSION_UNKNOWN
        assert store.get_receipt(app.id) is None


def test_paths_follow_environment_and_home_flag(capsys, tmp_path, monkeypatch):
    _, out, _ = run(capsys, "paths", "--json")
    data = json.loads(out)
    assert data["state_db"].endswith("imx-home/state/imx.sqlite3")
    monkeypatch.setenv("IMX_STATE_DB", str(tmp_path / "custom.sqlite3"))
    _, out, _ = run(capsys, "--home", str(tmp_path / "h"), "paths", "--json")
    data = json.loads(out)
    assert data["home"] == str(tmp_path / "h")
    assert data["state_db"] == str(tmp_path / "custom.sqlite3")
    assert data["artifacts_dir"] == str(tmp_path / "h" / "artifacts")


def test_unknown_application_is_an_error(capsys, isolated_imx_home):
    run(capsys, "apply", URL)
    code, _, err = run(capsys, "status", "app_missing")
    assert code == 1 and "app_missing" in err
