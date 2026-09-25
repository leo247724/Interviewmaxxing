"""``submit-approved`` offline: a fake ``interviewmaxxing submit`` acts on the real
store (authorize, begin, record the outcome) the way the real command would, so the
harness's target selection, slots, per-slot browser directories, the submission
ledger, store-based classification (a kill mid-submit is uncertain, never an error),
reruns and ``batch-report``'s submissions table are exercised without a browser.

All data is fictional; nothing is sent anywhere."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_cli import batch as batch_module
from interviewmaxxing_cli.batch import (
    BatchOptions,
    LedgerEntry,
    SubmissionEntry,
    SubmitBatchOptions,
    append_ledger,
    approved_targets,
    build_report,
    classify_submission,
    read_ledger,
    read_ledger_lines,
    read_submission_lines,
    read_submissions,
    render_report_markdown,
    render_submissions_markdown,
    run_submissions,
)
from interviewmaxxing_cli.main import EXIT_BLOCKED, EXIT_INCOMPLETE, EXIT_OK, EXIT_UNCERTAIN, main
from interviewmaxxing_core import (
    AnswerSource,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    ApplyOutcome,
    ControlType,
    LocalPaths,
    PacketAnswer,
    Provenance,
    SemanticType,
    TextValue,
)

S = ApplicationState
ORIGIN = "https://jobs.fictional.example/brambleway"

FAKE_SUBMIT = textwrap.dedent('''\
    """Fake ``interviewmaxxing submit APP --yes --json``: acts on the real store by plan."""
    import json, os, sys, time
    from pathlib import Path

    from interviewmaxxing_core import (ApplicationState as S, ApplicationStore,
                                       SubmissionObservation, SubmissionOutcome)

    assert sys.argv[1] == "submit" and "--yes" in sys.argv and "--json" in sys.argv
    app_id = sys.argv[2]
    kind = json.loads(Path(os.environ["FAKE_PLAN"]).read_text())[app_id]
    with Path(os.environ["FAKE_LOG"]).open("a") as fh:
        fh.write(json.dumps({"app": app_id, "kind": kind, "argv": sys.argv[1:], "t": time.monotonic(),
                             "env": {k: v for k, v in os.environ.items() if k.startswith("IMX_")}})
                 + "\\n")
    store = ApplicationStore.open(os.environ["IMX_STATE_DB"])

    def outcome(state, message, code, receipt=None):
        print(json.dumps({"application_id": app_id, "state": state, "receipt": receipt,
                          "missing_inputs": [], "message": message}))
        sys.exit(code)

    def begin():
        claim = store.claim(app_id, "fake-submit")
        store.authorize_submission(claim)
        for state in (S.INSPECTING, S.PACKET_READY, S.FILLING):
            store.transition(claim, state)
        return claim, store.begin_submission(claim, packet_id=store.approved_packet(app_id))

    time.sleep(0.2)
    if kind == "submitted":
        claim, attempt = begin()
        store.record_submission_outcome(claim, attempt.id, SubmissionObservation(
            outcome=SubmissionOutcome.ACCEPTED, signals=["heading 'Application submitted'"],
            confirmation_reference="BWA-000777"))
        outcome("SUBMITTED", "Submitted; the site confirmed it. Receipt saved.", 0,
                store.get_receipt(app_id).model_dump(mode="json"))
    if kind == "uncertain":
        claim, attempt = begin()
        store.record_submission_outcome(claim, attempt.id, SubmissionObservation(
            outcome=SubmissionOutcome.UNKNOWN, signals=["no confirmation"]))
        outcome("SUBMISSION_UNKNOWN", "The submit may have reached the employer.", 5)
    if kind == "hang_mid_submit":
        begin()
        time.sleep(3600)
    if kind == "changed":
        outcome("NEEDS_INPUT", "The form no longer matches the approved application: a new "
                "required question appeared.", 3)
    if kind == "closed":
        outcome("FAILED_PERMANENT", "The job is no longer accepting applications.", 3)
    if kind == "retryable":
        outcome("FAILED_RETRYABLE", "Stopped by a browser error. Nothing was submitted.", 3)
    if kind == "refused":
        print("Not submitted: no valid approval.", file=sys.stderr)
        sys.exit(4)
    if kind == "garbage":
        print("this is not json")
        print("the CLI exploded (no private data here)", file=sys.stderr)
        sys.exit(1)
    raise SystemExit("unknown kind " + kind)
''')


def _form(url: str) -> ApplicationForm:
    return ApplicationForm(url=url, step=0, is_final_step=True, submit_selector="#submit", fields=[
        ApplicationField(id="first_name", label="First name", selector="#first_name",
                         semantic_type=SemanticType.FIRST_NAME, control_type=ControlType.TEXT,
                         required=True)])


def _prepare(store: ApplicationStore, url: str, candidate: str = "default") -> tuple[str, str]:
    """A prepare-only run that stopped at the final review step."""
    form = _form(url)
    app = store.record_request(candidate, url).application
    claim = store.claim(app.id, "runner")
    store.require_preparation_only(claim)
    store.transition(claim, S.INSPECTING)
    packet = ApplicationPacket(
        application_id=app.id, job_id=app.job_id, candidate_id=app.candidate_id,
        form_url=url, form_step=0, form_fingerprint=form.fingerprint,
        answers=[PacketAnswer(field_id="first_name", semantic_type=SemanticType.FIRST_NAME,
                              value=TextValue(text="Avery"),
                              provenance=Provenance(source=AnswerSource.PROFILE_IDENTITY))])
    store.save_packet(claim, packet)
    store.transition(claim, S.PACKET_READY)
    store.transition(claim, S.FILLING)
    store.append_event(claim, "preparation.ready", {
        "form_url": url, "form_step": 0, "form_fingerprint": form.fingerprint,
        "packet_id": packet.id, "submitted": False, "browser_location": None,
        "captcha_pending": False})
    store.transition(claim, S.INSPECTING)
    store.transition(claim, S.NEEDS_INPUT, metadata={"missing_inputs": [], "reason": "prepared"})
    store.release(claim)
    return app.id, packet.id


def _approve(store: ApplicationStore, app_id: str, packet_id: str) -> None:
    claim = store.claim(app_id, "cli:approve")
    store.approve_submission(claim, packet_id=packet_id, approver="cli:test")
    store.release(claim)


def _ledger_line(batch_id: str, name: str, app_id: str | None, outcome: str = "prepared") -> LedgerEntry:
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    return LedgerEntry(batch_id=batch_id, listing_id=f"lst_{name}", pipeline_id=None,
                       company="Brambleway Analytics", title=name.title(),
                       application_url=f"{ORIGIN}/{name}", backend="mock", status="resolved",
                       attempt=1, application_id=app_id,
                       state=S.NEEDS_INPUT if app_id else None, outcome=outcome,  # type: ignore[arg-type]
                       started_at=now, finished_at=now, duration_s=1.0)


@pytest.fixture
def paths(tmp_path: Path) -> LocalPaths:
    paths = LocalPaths.from_env({}, home=tmp_path / "home")
    paths.ensure()
    return paths


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "fake_submit.py"
    script.write_text(FAKE_SUBMIT)
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    monkeypatch.setenv("FAKE_PLAN", str(tmp_path / "plan.json"))
    monkeypatch.setenv("IMX_BROWSER_DIR", str(tmp_path / "parent-browser"))  # must not leak
    return script


def _plan(tmp_path: Path, plan: dict[str, str]) -> None:
    (tmp_path / "plan.json").write_text(json.dumps(plan))


def _log(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "fake.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _options(paths: LocalPaths, fake: Path, **kwargs: Any) -> SubmitBatchOptions:
    return SubmitBatchOptions(paths=paths, candidate_id="default", batch_id="b1",
                              command=[sys.executable, str(fake)], **kwargs)


def _approved_batch(paths: LocalPaths, names: list[str]) -> dict[str, str]:
    """Prepare and approve one application per name; record them in batch ``b1``."""
    ids: dict[str, str] = {}
    with ApplicationStore.open(paths.state_db) as store:
        for name in names:
            app_id, packet_id = _prepare(store, f"{ORIGIN}/{name}")
            _approve(store, app_id, packet_id)
            ids[name] = app_id
    ledger = paths.home / "batches" / "b1" / "ledger.jsonl"
    for name, app_id in ids.items():
        append_ledger(ledger, _ledger_line("b1", name, app_id))
    return ids


# --- selection ----------------------------------------------------------------------------------


def test_targets_are_the_approved_applications_nothing_was_dispatched_for(paths):
    with ApplicationStore.open(paths.state_db) as store:
        approved, packet = _prepare(store, f"{ORIGIN}/approved")
        _approve(store, approved, packet)
        unapproved, _ = _prepare(store, f"{ORIGIN}/unapproved")
        reprepared, first = _prepare(store, f"{ORIGIN}/reprepared")
        _approve(store, reprepared, first)
        _prepare(store, f"{ORIGIN}/reprepared")  # a new preparation withdraws the approval
        outside, packet = _prepare(store, f"{ORIGIN}/outside")  # approved, not in the batch
        _approve(store, outside, packet)
        stranger, packet = _prepare(store, f"{ORIGIN}/stranger", candidate="someone-else")
        _approve(store, stranger, packet)
    ledger = paths.home / "batches" / "b1" / "ledger.jsonl"
    for name, app_id in (("approved", approved), ("unapproved", unapproved),
                         ("reprepared", reprepared), ("crashed", None)):
        append_ledger(ledger, _ledger_line("b1", name, app_id, "prepared" if app_id else "error"))

    targets = approved_targets(paths, "default", source_batch="b1")
    assert [t.application_id for t in targets] == [approved]
    [target] = targets
    assert (target.listing_id, target.application_url) == ("lst_approved", f"{ORIGIN}/approved")
    assert [t.application_id for t in approved_targets(paths, "default")] == [approved, outside]
    assert [t.application_id for t in approved_targets(paths, "someone-else")] == [stranger]
    with pytest.raises(FileNotFoundError, match="no ledger for batch 'missing'"):
        approved_targets(paths, "default", source_batch="missing")
    with pytest.raises(ValueError, match="plain name"):
        approved_targets(paths, "default", source_batch="../b1")


def test_the_submission_environment_is_explicit_and_prepare_batch_never_gets_it(
    paths, fake, monkeypatch
):
    monkeypatch.setenv("IMX_ALLOW_SUBMISSION", "1")
    prepare = BatchOptions(paths=paths, candidate_id="default", batch_id="p1", workers=2)
    assert "IMX_ALLOW_SUBMISSION" not in prepare.environment(0)
    assert prepare.argv("https://x.example/apply")[1:3] == ["apply", "https://x.example/apply"]
    with pytest.raises(ValueError, match="use --slots 1"):
        _options(paths, fake, workers=2, browser="opencli")
    submit = _options(paths, fake, workers=2)
    env = submit.submit_environment(1)
    assert env["IMX_ALLOW_SUBMISSION"] == "1"
    assert env["IMX_BROWSER_DIR"] == str(paths.home / "browser-workers" / "w1")
    assert submit.submit_argv("app_1")[2:] == ["submit", "app_1", "--yes", "--json", "--headless"]


@pytest.mark.parametrize(("state", "code", "expected"), [
    (S.SUBMITTED, 0, "submitted"),
    (S.SUBMITTED, 4, "blocked"),  # submitted earlier, not by this run
    (S.SUBMISSION_UNKNOWN, 5, "uncertain"),
    (S.SUBMITTING, 4, "uncertain"),
    (S.NEEDS_INPUT, 3, "needs_input"),
    (S.NEEDS_INPUT, 4, "blocked"),
    (S.FAILED_PERMANENT, 3, "blocked"),
    (S.DUPLICATE, 4, "blocked"),
    (S.FAILED_RETRYABLE, 3, "error"),
])
def test_classify_submission(state, code, expected):
    outcome = ApplyOutcome(application_id="app_x", state=state, message="m")
    assert classify_submission(outcome, code) == expected


# --- running --------------------------------------------------------------------------------------


def test_each_submission_is_recorded_from_the_store(paths, fake, tmp_path):
    kinds = ["submitted", "uncertain", "changed", "closed", "retryable", "refused", "garbage"]
    ids = _approved_batch(paths, kinds)
    _plan(tmp_path, {ids[k]: k for k in kinds})
    targets = approved_targets(paths, "default", source_batch="b1")
    assert len(targets) == len(kinds)
    seen: list[SubmissionEntry] = []

    summary = asyncio.run(run_submissions(_options(paths, fake, workers=2), targets,
                                          source_batch="b1", on_entry=seen.append))

    by_kind = {k: next(e for e in seen if e.application_id == ids[k]) for k in kinds}
    assert {k: e.outcome for k, e in by_kind.items()} == {
        "submitted": "submitted", "uncertain": "uncertain", "changed": "needs_input",
        "closed": "blocked", "retryable": "error", "refused": "blocked", "garbage": "error"}
    with ApplicationStore.open(paths.state_db) as store:
        receipt = store.get_receipt(ids["submitted"])
    assert receipt is not None
    done = by_kind["submitted"]
    assert (done.receipt_id, done.confirmation_reference) == (receipt.attempt_id, "BWA-000777")
    assert done.state is S.SUBMITTED and done.exit_code == 0 and done.attempt == 1
    assert by_kind["uncertain"].state is S.SUBMISSION_UNKNOWN
    assert by_kind["garbage"].message == "the CLI exploded (no private data here)"
    assert all(e.kind == "submission" and e.batch_id == "b1" and e.source_batch_id == "b1"
               and e.worker_slot in (0, 1) for e in seen)

    log = _log(tmp_path)
    assert len(log) == len(kinds)
    for line in log:
        assert line["env"]["IMX_ALLOW_SUBMISSION"] == "1"
        assert line["env"]["IMX_BROWSER_DIR"] in {str(paths.home / "browser-workers" / f"w{s}")
                                                  for s in (0, 1)}
        assert line["env"]["IMX_STATE_DB"] == str(paths.state_db)
    ledger = paths.home / "batches" / "b1" / "ledger.jsonl"
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
    workers = paths.home / "browser-workers"  # created by this run, owner-only at every level
    for directory in (workers, *workers.iterdir()):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
    assert [e.application_id for e in read_submissions(ledger)] == [e.application_id for e in seen]
    assert len(read_ledger(ledger)) == len(kinds)  # the prepare lines, unchanged
    # Submission lines are not unreadable prepare lines.
    assert read_ledger_lines(ledger) == (read_ledger(ledger), 0)
    assert build_report(paths, ["b1"]).ledger_lines_ignored == 0
    assert summary.totals == {"submitted": 1, "uncertain": 1, "blocked": 2, "needs_input": 1,
                              "error": 2}
    assert summary.launched == len(kinds) and summary.skipped_settled == 0
    assert [r.application_id for r in summary.receipts] == [ids["submitted"]]
    written = json.loads((ledger.parent / "submission-summary.json").read_text())
    assert written["totals"] == summary.totals
    assert not (ledger.parent / "summary.json").exists()  # prepare-batch's summary is not replaced

    # A rerun never launches a submitted or uncertain application again.
    again = asyncio.run(run_submissions(_options(paths, fake, workers=2), targets, source_batch="b1"))
    assert again.skipped_settled == 2 and again.launched == len(kinds) - 2
    relaunched = {line["app"] for line in _log(tmp_path)[len(kinds):]}
    assert relaunched == {ids[k] for k in kinds} - {ids["submitted"], ids["uncertain"]}
    retried = [e for e in read_submissions(ledger) if e.application_id == ids["changed"]]
    assert [e.attempt for e in retried] == [1, 2]


def test_a_submission_stopped_mid_submit_is_uncertain_never_an_error(paths, fake, tmp_path):
    ids = _approved_batch(paths, ["hang"])
    _plan(tmp_path, {ids["hang"]: "hang_mid_submit"})
    targets = approved_targets(paths, "default", source_batch="b1")
    summary = asyncio.run(run_submissions(_options(paths, fake, per_job_timeout_s=3.0), targets,
                                          source_batch="b1"))
    [entry] = read_submissions(paths.home / "batches" / "b1" / "ledger.jsonl")
    assert entry.outcome == "uncertain" and entry.state is S.SUBMITTING
    assert "timed out after 3 s" in entry.message
    assert summary.totals == {"uncertain": 1}


def test_batch_report_gains_a_submissions_table(paths, fake, tmp_path):
    ids = _approved_batch(paths, ["submitted", "changed"])
    _plan(tmp_path, {ids["submitted"]: "submitted", ids["changed"]: "changed"})
    before = render_report_markdown(build_report(paths, ["b1"]))
    assert "nothing was submitted (preparation only)" in before and "## Submissions" not in before
    assert build_report(paths, ["b1"]).submissions is None

    asyncio.run(run_submissions(_options(paths, fake), approved_targets(paths, "default",
                                                                        source_batch="b1"),
                                source_batch="b1"))

    report = build_report(paths, ["b1"])
    assert report.totals == {"prepared": 2}  # the prepare rows are unchanged
    assert report.submissions is not None
    assert report.submissions.totals == {"submitted": 1, "needs_input": 1}
    assert report.submissions.application_ids == {"submitted": [ids["submitted"]],
                                                  "needs_input": [ids["changed"]]}
    [row] = report.submissions.receipts
    assert (row.application_id, row.confirmation_reference) == (ids["submitted"], "BWA-000777")
    text = render_report_markdown(report)
    assert "nothing was submitted (preparation only)" not in text
    assert "- 1 approved application(s) submitted or possibly submitted" in text
    lines = text.splitlines()
    assert "## Submissions" in lines and "| submitted | 1 |" in lines
    assert any(line.startswith(f"| {ids['submitted']} | sub_") and "BWA-000777" in line
               for line in lines)
    assert json.loads(report.model_dump_json())["submissions"]["totals"]["submitted"] == 1


# --- the command ----------------------------------------------------------------------------------


def test_submit_approved_runs_the_batch_through_submit(paths, fake, tmp_path, monkeypatch, capsys):
    ids = _approved_batch(paths, ["one", "two"])
    _plan(tmp_path, {ids["one"]: "submitted", ids["two"]: "submitted"})
    monkeypatch.setattr(batch_module, "default_command", lambda: [sys.executable, str(fake)])
    home = ["--home", str(paths.home)]
    monkeypatch.delenv("IMX_ALLOW_SUBMISSION", raising=False)
    assert main([*home, "submit-approved", "--batch", "b1", "--yes"]) == EXIT_BLOCKED
    monkeypatch.setenv("IMX_ALLOW_SUBMISSION", "1")
    assert main([*home, "submit-approved", "--batch", "b1"]) == EXIT_BLOCKED  # no --yes
    assert _log(tmp_path) == []
    capsys.readouterr()

    code = main([*home, "submit-approved", "--batch", "b1", "--slots", "2", "--yes", "--json"])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    summary = json.loads(out)
    assert summary["totals"] == {"submitted": 2} and summary["source_batch_id"] == "b1"
    assert sorted(line["app"] for line in _log(tmp_path)) == sorted(ids.values())

    # Nothing approved is left: nothing is launched and the command succeeds.
    assert main([*home, "submit-approved", "--all-approved", "--yes"]) == EXIT_OK
    assert "No approved application to submit" in capsys.readouterr().out
    assert len(_log(tmp_path)) == 2


@pytest.mark.parametrize(("kind", "code"), [("uncertain", EXIT_UNCERTAIN),
                                            ("changed", EXIT_INCOMPLETE)])
def test_submit_approved_exit_status(paths, fake, tmp_path, monkeypatch, capsys, kind, code):
    ids = _approved_batch(paths, ["one"])
    _plan(tmp_path, {ids["one"]: kind})
    monkeypatch.setattr(batch_module, "default_command", lambda: [sys.executable, str(fake)])
    monkeypatch.setenv("IMX_ALLOW_SUBMISSION", "1")
    assert main(["--home", str(paths.home), "submit-approved", "--all-approved", "--yes",
                 "--batch-id", "run-1"]) == code
    out = capsys.readouterr().out
    assert "# Submissions run-1" in out
    ledger = paths.home / "batches" / "run-1" / "ledger.jsonl"
    [entry] = read_submissions(ledger)
    assert entry.source_batch_id is None and os.path.exists(ledger)


def test_a_retry_leaves_approved_applications_to_submit_approved(paths):
    """``prepare-batch --retry`` re-prepares held and failed applications. One the batch
    held, that was prepared and approved later and whose submission run then stopped,
    keeps its approval: ``submit-approved`` submits it, the retry leaves it alone."""
    from interviewmaxxing_cli.retry import plan_retry

    with ApplicationStore.open(paths.state_db) as store:
        approved, packet_id = _prepare(store, f"{ORIGIN}/approved")
        _approve(store, approved, packet_id)
        plain, _ = _prepare(store, f"{ORIGIN}/plain")
        for app_id in (approved, plain):  # a later run that failed
            claim = store.claim(app_id, "runner")
            store.transition(claim, S.INSPECTING)
            store.transition(claim, S.FAILED_RETRYABLE, failure_reason="Stopped by a browser error.")
            store.release(claim)
        assert store.approved_packet(approved) is not None
    ledger = paths.home / "batches" / "b1" / "ledger.jsonl"
    for name, app_id in (("approved", approved), ("plain", plain)):  # both held in the batch
        append_ledger(ledger, _ledger_line("b1", name, app_id, "needs_input"))

    plan = plan_retry(paths, "b1", candidate_id="default")

    assert [item.row.application_id for item in plan.items] == [plain]
    assert plan.stats.skipped == {"approved (left to submit-approved)": 1}
    assert [t.application_id for t in approved_targets(paths, "default", source_batch="b1")] == [
        approved]


def test_unreadable_submission_lines_are_counted_and_shown(paths, fake, tmp_path):
    ids = _approved_batch(paths, ["one"])
    _plan(tmp_path, {ids["one"]: "changed"})
    ledger = paths.home / "batches" / "b1" / "ledger.jsonl"
    with ledger.open("a") as fh:  # a hand edit and a truncated write
        fh.write(json.dumps({"kind": "submission", "batch_id": "b1", "application_id": "app_x"}) + "\n")
        fh.write('{"kind": "submission", "batch_i\n')

    summary = asyncio.run(run_submissions(_options(paths, fake), approved_targets(
        paths, "default", source_batch="b1"), source_batch="b1"))

    # The run counts both: the unreadable submission line and the truncated line, which
    # may have been a submission too. The report splits them (below).
    assert read_submission_lines(ledger)[1] == 2
    assert read_submission_lines(ledger, count_unparsed=False)[1] == 1
    assert read_ledger_lines(ledger)[1] == 1  # the line that is not JSON at all
    assert summary.ledger_lines_ignored == 2
    assert "- unreadable ledger lines ignored: 2" in render_submissions_markdown(summary)
    report = build_report(paths, ["b1"])
    assert report.submissions is not None and report.submissions.lines_ignored == 1
    assert report.ledger_lines_ignored == 1
    assert "- unreadable submission lines ignored: 1" in render_report_markdown(report)
