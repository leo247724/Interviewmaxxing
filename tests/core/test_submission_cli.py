"""``interviewmaxxing approve`` and ``submit``: the gates (IMX_ALLOW_SUBMISSION, --yes,
an approval), what approving records and prints, and how ``submit`` authorizes the
approved packet before handing it to the submission runner. The runner is a stub here;
real submissions against the mock ATS are in ``e2e/test_submission_e2e.py``.

All data is fictional; a prepare-only run is simulated with the store's operations."""

from __future__ import annotations

import json
from typing import Any

import pytest

from interviewmaxxing_cli import main as cli_main
from interviewmaxxing_cli.main import EXIT_BLOCKED, EXIT_INCOMPLETE, EXIT_OK, EXIT_UNCERTAIN, main
from interviewmaxxing_cli.runner import NOT_AUTHORIZED_MESSAGE
from interviewmaxxing_core import (
    AnswerSource,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    ApplyOutcome,
    ChoiceValue,
    ControlType,
    FieldOption,
    LocalPaths,
    PacketAnswer,
    Provenance,
    SemanticType,
    SubmissionObservation,
    SubmissionOutcome,
    TextValue,
)

S = ApplicationState
URL = "https://jobs.mock.example/mock-co/4012/apply"


def _form() -> ApplicationForm:
    return ApplicationForm(url=URL, step=0, is_final_step=True, submit_selector="#submit", fields=[
        ApplicationField(id="first_name", label="First name", selector="#first_name",
                         semantic_type=SemanticType.FIRST_NAME, control_type=ControlType.TEXT,
                         required=True),
        ApplicationField(id="work_authorization", label="Are you authorized to work in the US?",
                         selector="#wa", semantic_type=SemanticType.WORK_AUTHORIZATION,
                         control_type=ControlType.SELECT, required=True,
                         options=[FieldOption(value="wa_yes", label="Yes"),
                                  FieldOption(value="wa_no", label="No")]),
    ])


def _prepare(paths: LocalPaths, url: str = URL) -> tuple[str, str]:
    """A prepare-only run that stopped at the final review step: (application, packet)."""
    paths.ensure()
    form = _form().model_copy(update={"url": url})
    with ApplicationStore.open(paths.state_db) as store:
        app = store.record_request("default", url).application
        claim = store.claim(app.id, "runner")
        store.require_preparation_only(claim)
        store.transition(claim, S.INSPECTING)
        packet = ApplicationPacket(
            application_id=app.id, job_id=app.job_id, candidate_id=app.candidate_id,
            form_url=form.url, form_step=0, form_fingerprint=form.fingerprint, answers=[
                PacketAnswer(field_id="first_name", semantic_type=SemanticType.FIRST_NAME,
                             value=TextValue(text="Avery"),
                             provenance=Provenance(source=AnswerSource.PROFILE_IDENTITY)),
                PacketAnswer(field_id="work_authorization",
                             semantic_type=SemanticType.WORK_AUTHORIZATION,
                             value=ChoiceValue(value="wa_yes", label="Yes"),
                             provenance=Provenance(source=AnswerSource.SAVED_ANSWER,
                                                   reference_ids=["sa.work_auth"])),
            ])
        store.save_packet(claim, packet)
        store.transition(claim, S.PACKET_READY)
        store.transition(claim, S.FILLING)
        store.append_event(claim, "preparation.ready", {
            "form_url": form.url, "form_step": 0, "form_fingerprint": form.fingerprint,
            "packet_id": packet.id, "submitted": False, "browser_location": None,
            "captcha_pending": False})
        store.transition(claim, S.INSPECTING)
        store.transition(claim, S.NEEDS_INPUT, metadata={"missing_inputs": [], "reason": "prepared"})
        store.release(claim)
    return app.id, packet.id


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _events(paths: LocalPaths, app_id: str) -> list[str]:
    with ApplicationStore.open(paths.state_db) as store:
        return [e.event for e in store.list_events(app_id)]


class StubRunner:
    """Stands in for the submission runner: records what the store said when it ran."""

    def __init__(self, paths: LocalPaths, outcome: Any) -> None:
        self.paths = paths
        self.outcome = outcome
        self.seen: list[dict[str, Any]] = []

    async def submit(self, application_id: str) -> ApplyOutcome:
        with ApplicationStore.open(self.paths.state_db) as store:
            self.seen.append({
                "application_id": application_id,
                "preparation_only": store.is_preparation_only(application_id),
                "authorized": store.submission_authorization(application_id),
            })
        return self.outcome(application_id) if callable(self.outcome) else self.outcome


@pytest.fixture
def no_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the submission runner must not be built")

    monkeypatch.setattr(cli_main, "create_submission_runner", refuse)


@pytest.fixture
def allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMX_ALLOW_SUBMISSION", "1")


# --- the gates --------------------------------------------------------------------------------


def test_submit_without_the_environment_variable_explains_how_to_enable_it(
    capsys, isolated_imx_home, monkeypatch, no_runner
):
    monkeypatch.delenv("IMX_ALLOW_SUBMISSION", raising=False)
    app_id, _ = _prepare(isolated_imx_home)
    for value in (None, "0", "yes", "true"):
        if value is not None:
            monkeypatch.setenv("IMX_ALLOW_SUBMISSION", value)
        code, out, err = run(capsys, "submit", app_id, "--yes")
        assert code == EXIT_BLOCKED and out == ""
        assert "Submission is disabled; nothing was submitted." in err
        assert f"IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit {app_id} --yes" in err
    assert "application.submission_authorized" not in _events(isolated_imx_home, app_id)


def test_submit_without_yes_refuses(capsys, isolated_imx_home, allowed, no_runner):
    app_id, _ = _prepare(isolated_imx_home)
    code, _, err = run(capsys, "approve", app_id)
    assert code == EXIT_OK
    code, out, err = run(capsys, "submit", app_id)
    assert code == EXIT_BLOCKED and out == ""
    assert "Refusing to submit without --yes" in err
    assert "application.submission_authorized" not in _events(isolated_imx_home, app_id)


def test_submit_refuses_an_application_without_an_approval(capsys, isolated_imx_home, allowed,
                                                           no_runner):
    app_id, _ = _prepare(isolated_imx_home)
    code, out, err = run(capsys, "submit", app_id, "--yes")
    assert code == EXIT_BLOCKED and out == ""
    assert "has no valid approval" in err and f"interviewmaxxing approve {app_id}" in err
    code, out, _ = run(capsys, "submit", app_id, "--yes", "--json")
    assert code == EXIT_BLOCKED
    outcome = ApplyOutcome.model_validate_json(out)
    assert outcome.state is S.NEEDS_INPUT and "has no valid approval" in outcome.message
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert store.is_preparation_only(app_id) and store.list_attempts(app_id) == []


def test_submit_never_touches_a_submitted_or_uncertain_application(capsys, isolated_imx_home,
                                                                  allowed, no_runner):
    app_id, packet_id = _prepare(isolated_imx_home)
    assert run(capsys, "approve", app_id)[0] == EXIT_OK
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        claim = store.claim(app_id, "earlier-submit")
        store.authorize_submission(claim)
        for state in (S.INSPECTING, S.PACKET_READY, S.FILLING):
            store.transition(claim, state)
        attempt = store.begin_submission(claim, packet_id=packet_id)
        store.record_submission_outcome(claim, attempt.id, SubmissionObservation(
            outcome=SubmissionOutcome.UNKNOWN, signals=["no confirmation"]))
        store.release(claim)
    code, _, err = run(capsys, "submit", app_id, "--yes")
    assert code == EXIT_UNCERTAIN and "is never submitted again" in err
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        assert len(store.list_attempts(app_id)) == 1


# --- approve --------------------------------------------------------------------------------------


def test_approve_records_and_lists_the_prepared_answers(capsys, isolated_imx_home, monkeypatch):
    monkeypatch.setattr(cli_main.getpass, "getuser", lambda: "avery")
    app_id, packet_id = _prepare(isolated_imx_home)
    code, out, err = run(capsys, "approve", app_id)
    assert code == EXIT_OK, err
    assert f"approved:    {app_id}" in out
    assert f"packet:      {packet_id} (2 answer(s) on 1 step(s))" in out
    assert "step 1  first_name: Avery (profile_identity)" in out
    assert "step 1  work_authorization: Yes (saved_answer)" in out
    assert "Nothing was submitted." in out
    assert f"next:        IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit {app_id} --yes" in out
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        [event] = [e for e in store.list_events(app_id) if e.event == "application.approved"]
        assert event.metadata["approver"] == "cli:avery"
        assert event.metadata["packet_id"] == packet_id
        assert store.is_preparation_only(app_id)  # approving submits and authorizes nothing

    code, out, _ = run(capsys, "approve", app_id, "--json")
    assert code == EXIT_OK and json.loads(out)["packet_id"] == packet_id
    assert _events(isolated_imx_home, app_id).count("application.approved") == 1

    code, out, _ = run(capsys, "status", app_id)
    assert f"approved:     packet {packet_id} by cli:avery" in out
    status = json.loads(run(capsys, "status", app_id, "--json")[1])
    assert status["approval"]["packet_id"] == packet_id


def test_approve_refuses_what_is_not_the_prepared_packet(capsys, isolated_imx_home):
    app_id, _ = _prepare(isolated_imx_home)
    code, _, err = run(capsys, "approve", app_id, "--packet", "pkt_someone_else")
    assert code == EXIT_BLOCKED and "Not approved: packet pkt_someone_else is not the prepared" in err
    with ApplicationStore.open(isolated_imx_home.state_db) as store:
        other = store.record_request("default", URL + "?job=2").application
    code, _, err = run(capsys, "approve", other.id)
    assert code == EXIT_BLOCKED and "nothing to approve" in err
    assert "application.approved" not in _events(isolated_imx_home, app_id)


# --- submit ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("state", "message", "code"), [
    (S.SUBMITTED, "Submitted; the site confirmed it. Receipt saved.", EXIT_OK),
    (S.NEEDS_INPUT, "The form no longer matches the approved application: x.", EXIT_INCOMPLETE),
    (S.SUBMISSION_UNKNOWN, "The submit may have reached the employer.", EXIT_UNCERTAIN),
    (S.NEEDS_INPUT, f"{NOT_AUTHORIZED_MESSAGE}: approve it. Nothing was opened.", EXIT_BLOCKED),
])
def test_submit_authorizes_the_approval_then_runs_the_submission(
    capsys, isolated_imx_home, allowed, monkeypatch, state, message, code
):
    app_id, packet_id = _prepare(isolated_imx_home)
    assert run(capsys, "approve", app_id)[0] == EXIT_OK
    built: dict[str, Any] = {}

    def factory(paths: LocalPaths, **kwargs: Any) -> StubRunner:
        built.update(kwargs)
        built["runner"] = StubRunner(paths, lambda app: ApplyOutcome(
            application_id=app, state=state, message=message))
        return built["runner"]

    monkeypatch.setattr(cli_main, "create_submission_runner", factory)
    result, out, _ = run(capsys, "submit", app_id, "--yes", "--headless", "--json")
    assert result == code
    assert ApplyOutcome.model_validate_json(out).message == message
    [seen] = built["runner"].seen
    assert seen["preparation_only"] is False  # authorized before the run started
    assert seen["authorized"].packet_id == packet_id
    assert built["headless"] is True and built["interaction"].allow_browser_action is False
    names = _events(isolated_imx_home, app_id)
    assert names.index("application.approved") < names.index("application.submission_authorized")
