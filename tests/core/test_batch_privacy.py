"""Review pass 4, WP9 round 2: what the CLI prints of stored records. Fill-failure
details mask digit groups, e-mail addresses and URLs (M10). ``events``, ``status --json``
and ``holds --json`` print no draft tokens, no typed lookup values and no state-database
path (L12). Unreadable submission ledger lines are counted (L4). All values are fictional."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from interviewmaxxing_cli.batch import (
    LedgerEntry,
    SubmissionEntry,
    SubmitBatchOptions,
    append_ledger,
    build_report,
    read_ledger_lines,
    read_submission_lines,
    render_report_markdown,
    render_submissions_markdown,
    run_submissions,
)
from interviewmaxxing_cli.main import EXIT_OK, main
from interviewmaxxing_cli.redaction import HIDDEN, page_address, public_metadata
from interviewmaxxing_cli.triage import failure_text
from interviewmaxxing_core import (
    AnswerSource,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    ControlType,
    FieldOption,
    LocalPaths,
    MissingInput,
    MissingReason,
    PacketAnswer,
    Provenance,
    SemanticType,
    TextValue,
)

S = ApplicationState
ORIGIN = "https://jobs.fictional.example/brambleway"
TOKEN = "SECRETDRAFTTOKEN"
STEP_URL = f"https://avery:pw@jobs.fictional.example/brambleway/apply/step?draft={TOKEN}#review"
STEP_ADDRESS = f"{ORIGIN}/apply/step"
TYPED = "Austin, TX"
"""The person's typed lookup value: it must not show without --verbose."""
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def paths(isolated_imx_home: LocalPaths) -> LocalPaths:
    """The conftest's IMX_HOME, so commands run without ``--home`` (no path in their lines)."""
    return isolated_imx_home


# --- M10 ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("detail", "shown"), [
    ("reads back +1 512-555-0142 (+1) instead", "reads back +1 #-#-# (+1) instead"),
    ("Timeout 30000ms exceeded at https://x.example/a?t=123 for avery@example.test",
     "Timeout #ms exceeded at <url> for <email>"),
    ("HTTP 403 on step 2 of question_12345", "HTTP # on step 2 of question_#"),
    ("expected 'Two weeks', got \"One month\"", "expected '…', got '…'"),
    ("Could not fill notice reliably. Provider cost: USD 0.0123 for 2 call(s).",
     "Could not fill notice reliably."),
])
def test_m10_failure_text_masks_digit_groups_emails_and_urls(detail, shown):
    assert failure_text(detail) == shown


def test_m10_an_unquoted_phone_never_reaches_the_report(paths):
    paths.ensure()
    with ApplicationStore.open(paths.state_db) as store:
        app = store.record_request("default", f"{ORIGIN}/phone").application
        claim = store.claim(app.id, "test")
        store.transition(claim, S.INSPECTING)
        store.transition(claim, S.FAILED_RETRYABLE, failure_reason="Could not fill phone.",
                         metadata={"failed_fields": [{
                             "field_id": "phone", "label": "Phone", "status": "VERIFICATION_MISMATCH",
                             "detail": "reads back +1 512-555-0142 (+1)"}]})
        store.release(claim)
    append_ledger(paths.home / "batches" / "b1" / "ledger.jsonl", LedgerEntry(
        batch_id="b1", listing_id="l1", application_url=f"{ORIGIN}/phone", attempt=1,
        application_id=app.id, outcome="failed_retryable", state=S.FAILED_RETRYABLE,
        started_at=datetime.now(UTC), finished_at=datetime.now(UTC), duration_s=1.0))
    report = build_report(paths)
    [group] = report.fill_failures
    assert group.detail == "reads back +1 #-#-# (+1)"
    for output in (render_report_markdown(report), report.model_dump_json()):
        assert "512" not in output and "555" not in output and "0142" not in output


# --- L12 --------------------------------------------------------------------------------------


def lookup_hold() -> MissingInput:
    return MissingInput(
        field_id="city", form_url=STEP_URL, form_step=0, field_fingerprint="ab" * 32,
        label="Location", reason=MissingReason.NO_ANSWER, control_type=ControlType.TYPEAHEAD,
        prompt=f"The site did not accept {TYPED!r} as typed; choose the suggestion it offered: "
               f"{TYPED}, USA; Austin, MN, USA.",
        options=[FieldOption(value=f"{TYPED}, USA", label=f"{TYPED}, USA")])


def ambiguous_hold() -> MissingInput:
    return MissingInput(
        field_id="where", form_url=STEP_URL, form_step=0, field_fingerprint="cd" * 32,
        label="Where do you live?", reason=MissingReason.AMBIGUOUS,
        control_type=ControlType.TEXT, prompt="Your saved answers disagree.",
        candidates=[TextValue(text=TYPED), TextValue(text="Denver, CO")])


def held_then_prepared_and_approved(paths: LocalPaths) -> str:
    """A run held on a lookup, then a run prepared to the review step and approved; every
    step URL carries a draft token and user info, as live sites' do."""
    paths.ensure()
    form = ApplicationForm(url=STEP_URL, step=0, is_final_step=True, submit_selector="#submit",
                           fields=[ApplicationField(
                               id="first_name", label="First name", selector="#first_name",
                               semantic_type=SemanticType.FIRST_NAME,
                               control_type=ControlType.TEXT, required=True)])
    with ApplicationStore.open(paths.state_db) as store:
        app = store.record_request("default", f"{ORIGIN}/apply").application
        claim = store.claim(app.id, "runner")
        store.require_preparation_only(claim)
        store.transition(claim, S.INSPECTING)
        store.append_event(claim, "routing.trace", {
            "form_step": 0, "prompt_version": "fictional-v1",
            "fields": [{"field_id": "city", "reason": f"typed {TYPED}"}],
            "traces": [{"stage": "lookup", "note": TYPED}]})
        store.append_event(claim, "field.suggestion_chosen", {
            "form_url": STEP_URL, "form_step": 0, "field_id": "city",
            "chosen_label": f"{TYPED}, USA", "suggestion_count": 2,
            "decision": {"reason": f"closest to {TYPED}"}})
        store.append_event(claim, "validation.rejected", {
            "form_url": STEP_URL, "form_step": 0, "fields": [
                {"field_id": "phone", "field_fingerprint": "ef" * 32, "message": "Invalid"}]})
        store.transition(claim, S.NEEDS_INPUT, metadata={"reason": "missing answers",
                                                         "missing_inputs": [
            lookup_hold().model_dump(mode="json"), ambiguous_hold().model_dump(mode="json")]})
        store.transition(claim, S.INSPECTING)
        packet = ApplicationPacket(
            application_id=app.id, job_id=app.job_id, candidate_id=app.candidate_id,
            form_url=STEP_URL, form_step=0, form_fingerprint=form.fingerprint,
            answers=[PacketAnswer(field_id="first_name", semantic_type=SemanticType.FIRST_NAME,
                                  value=TextValue(text="Avery"),
                                  provenance=Provenance(source=AnswerSource.PROFILE_IDENTITY))])
        store.save_packet(claim, packet)
        store.transition(claim, S.PACKET_READY)
        store.transition(claim, S.FILLING)
        store.append_event(claim, "preparation.ready", {
            "form_url": STEP_URL, "form_step": 0, "form_fingerprint": form.fingerprint,
            "packet_id": packet.id, "submitted": False, "browser_location": None,
            "captcha_pending": False})
        store.transition(claim, S.INSPECTING)
        store.transition(claim, S.NEEDS_INPUT, metadata={"missing_inputs": [],
                                                         "reason": "prepared"})
        store.release(claim)
        claim = store.claim(app.id, "cli:approve")
        store.approve_submission(claim, packet_id=packet.id, approver="cli:test")
        store.release(claim)
        return app.id


def test_page_address_and_public_metadata():
    assert page_address(STEP_URL) == STEP_ADDRESS
    assert page_address("ftp://x.example/a") is None and page_address("not a url") is None
    data = public_metadata("application.needs_input", {
        "note": f"see {STEP_URL} now", "missing_inputs": [lookup_hold().model_dump(mode="json")]})
    assert data["note"] == f"see {STEP_ADDRESS} now"
    [item] = data["missing_inputs"]
    assert (item["prompt"], item["options"], item["form_url"]) == (HIDDEN, HIDDEN, STEP_ADDRESS)
    verbose = public_metadata("application.needs_input", {
        "missing_inputs": [lookup_hold().model_dump(mode="json")]}, verbose=True)
    assert TYPED in verbose["missing_inputs"][0]["prompt"]
    assert verbose["missing_inputs"][0]["form_url"] == STEP_ADDRESS


def test_l12_events_print_page_addresses_and_hide_typed_values(paths, capsys):
    app_id = held_then_prepared_and_approved(paths)
    assert main(["events", app_id]) == EXIT_OK
    text = capsys.readouterr().out
    assert main(["events", app_id, "--json"]) == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    for output in (text, json.dumps(data)):
        assert TOKEN not in output and "avery:pw" not in output
        assert TYPED not in output and "Austin" not in output and "Denver" not in output
        assert STEP_ADDRESS in output and HIDDEN in output
    by_event = {e["event"]: e["metadata"] for e in data}
    assert by_event["application.approved"]["form_url"] == STEP_ADDRESS
    assert by_event["preparation.ready"]["form_url"] == STEP_ADDRESS
    assert by_event["validation.rejected"]["form_url"] == STEP_ADDRESS
    assert (by_event["routing.trace"]["fields"], by_event["routing.trace"]["traces"]) == \
        (HIDDEN, HIDDEN)
    assert by_event["field.suggestion_chosen"]["chosen_label"] == HIDDEN
    assert by_event["field.suggestion_chosen"]["suggestion_count"] == 2
    [lookup, ambiguous] = next(e["metadata"]["missing_inputs"] for e in data
                               if e["event"] == "application.needs_input"
                               and e["metadata"]["missing_inputs"])
    assert (lookup["prompt"], lookup["options"], ambiguous["candidates"]) == (HIDDEN,) * 3
    assert ambiguous["label"] == "Where do you live?"  # the question itself stays

    assert main(["events", app_id, "--verbose", "--json"]) == EXIT_OK
    verbose = capsys.readouterr().out
    assert TYPED in verbose and HIDDEN not in verbose
    assert TOKEN not in verbose and "avery:pw" not in verbose  # URLs are never printed raw
    with ApplicationStore.open(paths.state_db) as store:  # the record keeps them exactly
        assert any(TOKEN in json.dumps(e.metadata) for e in store.list_events(app_id))


def test_l12_status_json_prints_page_addresses_and_no_database_path(paths, tmp_path, capsys):
    app_id = held_then_prepared_and_approved(paths)
    assert main(["status", app_id, "--json"]) == EXIT_OK
    out = capsys.readouterr().out
    assert TOKEN not in out and "avery:pw" not in out
    data = json.loads(out)
    assert list(data) == ["application", "job", "attempts", "pending_inputs", "packet",
                          "requests", "approval"]
    assert data["approval"]["form_url"] == STEP_ADDRESS
    assert data["packet"]["form_url"] == STEP_ADDRESS
    assert data["job"]["application_url"] == f"{ORIGIN}/apply"  # the person's own URL
    assert data["requests"][0]["application_url"] == f"{ORIGIN}/apply"
    assert data["application"]["id"] == app_id

    absent = tmp_path / "absent"
    assert main(["--home", str(absent), "status"]) == EXIT_OK
    message = capsys.readouterr().out
    assert "No applications yet" in message and str(absent) not in message
    assert not absent.exists()


def test_l12_holds_json_has_no_database_path(paths, capsys):
    paths.ensure()
    with ApplicationStore.open(paths.state_db) as store:
        app = store.record_request("default", f"{ORIGIN}/held").application
        claim = store.claim(app.id, "test")
        store.transition(claim, S.INSPECTING)
        store.transition(claim, S.NEEDS_INPUT, metadata={"reason": "x", "missing_inputs": [
            lookup_hold().model_dump(mode="json")]})
        store.release(claim)
    assert main(["holds", "--json"]) == EXIT_OK
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "state_db" not in data and str(paths.home) not in out
    assert data["questions"][0]["answer"] == \
        f"interviewmaxxing answer {app.id} --set city=VALUE --reuse global"
    assert TOKEN not in out and TYPED not in out  # the grouping shows wording, not prompts


# --- L4 ---------------------------------------------------------------------------------------


def submission(app_id: str) -> SubmissionEntry:
    return SubmissionEntry(kind="submission", batch_id="approved-x", application_id=app_id,
                           attempt=1, outcome="submitted", started_at=NOW, finished_at=NOW,
                           duration_s=1.0)


def test_l4_unreadable_submission_lines_are_counted_and_shown(paths):
    ledger = paths.home / "batches" / "approved-x" / "ledger.jsonl"
    prepare = LedgerEntry(batch_id="approved-x", listing_id="l1", application_url=f"{ORIGIN}/x",
                          attempt=1, outcome="prepared", state=S.NEEDS_INPUT, started_at=NOW,
                          finished_at=NOW, duration_s=1.0)
    append_ledger(ledger, prepare)
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(submission("app_ok").model_dump_json() + "\n")
        fh.write(json.dumps({"kind": "submission", "batch_id": "approved-x"}) + "\n")  # broken
        fh.write("[1, 2]\n")  # JSON, but not an object
        fh.write('{"kind": "submission", "application_id": "app_c')  # cut by a crash

    entries, ignored = read_submission_lines(ledger)
    assert [e.application_id for e in entries] == ["app_ok"] and ignored == 3
    assert read_submission_lines(ledger, count_unparsed=False)[1] == 1
    prepares, prepare_ignored = read_ledger_lines(ledger)
    assert [e.listing_id for e in prepares] == ["l1"] and prepare_ignored == 2

    # The report counts each unreadable line once: not JSON objects with the prepare lines,
    # broken submission lines with the submissions.
    report = build_report(paths, ["approved-x"])
    assert report.ledger_lines_ignored == 2
    assert report.submissions is not None and report.submissions.lines_ignored == 1
    text = render_report_markdown(report)
    assert "- unreadable ledger lines ignored: 2" in text
    assert "- unreadable submission lines ignored: 1" in text

    # A submission run reading this ledger says so in its summary.
    options = SubmitBatchOptions(paths=paths, candidate_id="default", batch_id="approved-x")
    summary = asyncio.run(run_submissions(options, []))
    assert summary.ledger_lines_ignored == 3 and summary.launched == 0
    assert "- unreadable ledger lines ignored: 3" in render_submissions_markdown(summary)
    written = json.loads((ledger.parent / "submission-summary.json").read_text())
    assert written["ledger_lines_ignored"] == 3


def test_l4_a_clean_ledger_reports_nothing_ignored(tmp_path: Path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(submission("app_a").model_dump_json() + "\n")
    assert read_submission_lines(ledger) == ([submission("app_a")], 0)
    assert read_submission_lines(tmp_path / "missing.jsonl") == ([], 0)
