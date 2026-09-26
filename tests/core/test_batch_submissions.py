"""``submit-approved`` offline: a fake ``interviewmaxxing submit`` acts on the real
store (authorize, begin, record the outcome) the way the real command would, so the
harness's target selection, slots, per-slot browser directories, the submission
ledger, store-based classification (a kill mid-submit is uncertain, never an error),
reruns, the runner's per-profile lock (a submission slot never shares a prepare worker's
profile), moving a confirmed submission's pipeline card to Applied and
``batch-report``'s submissions table are exercised without a browser.

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
from interviewmaxxing_cli.runner import (
    BUSY_MESSAGE,
    KEPT_DRAFT_MESSAGE,
    RUN_LOCK_NAME,
    browser_profile_lock,
)
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
from interviewmaxxing_pipeline import (
    NewPipelineItem,
    PipelineStore,
    TrackingFields,
    default_pipeline_db,
)

S = ApplicationState
ORIGIN = "https://jobs.fictional.example/brambleway"

FAKE_SUBMIT = textwrap.dedent('''\
    """Fake ``interviewmaxxing submit APP --yes --json``: acts on the real store by plan."""
    import fcntl, json, os, sys, time
    from pathlib import Path

    from interviewmaxxing_core import (ApplicationState as S, ApplicationStore, NotSubmittedNext,
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

    profile = os.open(Path(os.environ["IMX_BROWSER_DIR"]) / os.environ["FAKE_LOCK_NAME"],
                      os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(profile, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the runner's per-profile lock
    except BlockingIOError:  # `submit` authorized the approval; the runner found the profile busy
        claim = store.claim(app_id, "fake-submit")
        store.authorize_submission(claim)
        store.release(claim)
        outcome("NEEDS_INPUT", "another run is using the browser profile", 4)

    def begin():
        claim = store.claim(app_id, "fake-submit")
        store.authorize_submission(claim)
        for state in (S.INSPECTING, S.PACKET_READY, S.FILLING):
            store.transition(claim, state)
        return claim, store.begin_submission(claim, packet_id=store.approved_packet(app_id))

    time.sleep(0.2)
    if kind == "rejected_unless_opencli":
        if "--browser" in sys.argv and sys.argv[sys.argv.index("--browser") + 1] == "opencli":
            kind = "submitted"
        else:  # what the runner records when the site shows its refusal banner
            claim, attempt = begin()
            refusal = ("We couldn't submit your application. Your application submission was "
                       "flagged as possible spam.")
            store.record_submission_outcome(claim, attempt.id, SubmissionObservation(
                outcome=SubmissionOutcome.NOT_SUBMITTED,
                signals=["the site refused the submission: " + repr(refusal)],
                next_state=NotSubmittedNext.FAILED_RETRYABLE,
                detail="the site refused the submission: " + refusal))
            store.release(claim)
            outcome("FAILED_RETRYABLE", "Rejected by the site: " + repr(refusal) + ". Nothing was "
                    "received; the approval stands.", 3)
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
    if kind == "kept_draft":
        outcome("NEEDS_INPUT", "The site resumed a draft it kept at step 2, so the approved "
                "answers of step 1 could not be checked or filled.", 3)
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
    monkeypatch.setenv("FAKE_LOCK_NAME", RUN_LOCK_NAME)
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
    assert env["IMX_ALLOW_SUBMISSION"] == "1" and env["IMX_SUBMIT_CARDS_BY_BATCH"] == "1"
    assert "IMX_SUBMIT_CARDS_BY_BATCH" not in prepare.environment(1)
    assert env["IMX_BROWSER_DIR"] == str(paths.home / "browser-workers" / "s1")
    assert prepare.environment(1)["IMX_BROWSER_DIR"] == str(paths.home / "browser-workers" / "w1")
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
        assert line["env"]["IMX_BROWSER_DIR"] in {str(paths.home / "browser-workers" / f"s{s}")
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
                                            ("changed", EXIT_INCOMPLETE),
                                            ("rejected_unless_opencli", EXIT_INCOMPLETE)])
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


# --- review pass 5: a ledger shared with prepare-batch; kept drafts ---------------------------


def test_a_shared_ledger_counts_only_submission_lines_that_cannot_be_read(paths, fake, tmp_path):
    """``submit-approved --batch B`` appends to B's own ledger. A prepare line cut short by
    a crash there is prepare-batch's unreadable line, not a submission line; the first
    submission line is not joined to it; a cut line after a submission line may have been
    either kind and counts."""
    ids = _approved_batch(paths, ["one", "two"])
    ledger = paths.home / "batches" / "b1" / "ledger.jsonl"
    with ledger.open("a") as fh:  # prepare-batch stopped mid-write: no newline
        fh.write('{"application_id": "app_fictional_cut", "application_url": "https://jobs.fict')
    _plan(tmp_path, {ids["one"]: "submitted", ids["two"]: "changed"})

    summary = asyncio.run(run_submissions(_options(paths, fake), approved_targets(
        paths, "default", source_batch="b1"), source_batch="b1"))

    lines = read_submissions(ledger)
    assert [e.application_id for e in lines] == [ids["one"], ids["two"]]  # nothing joined the cut line
    assert read_submission_lines(ledger) == (lines, 0)
    assert summary.ledger_lines_ignored == 0
    assert "unreadable" not in render_submissions_markdown(summary)
    assert read_ledger_lines(ledger)[1] == 1  # prepare-batch's reader counts its own cut line
    report = build_report(paths, ["b1"])
    assert report.ledger_lines_ignored == 1
    assert report.submissions is not None and report.submissions.lines_ignored == 0

    with ledger.open("a") as fh:  # submit-approved stopped mid-write, after a submission line
        fh.write('{"application_id": "app_fictional_cut_2", "application_u\n')
    assert read_submission_lines(ledger)[1] == 1
    assert read_submission_lines(ledger, count_unparsed=False)[1] == 0

    # A ledger of submission lines only (``--batch-id`` of its own): every cut line counts,
    # the first one included.
    own = paths.home / "batches" / "approved-own" / "ledger.jsonl"
    own.parent.mkdir(parents=True)
    own.write_text('{"application_id": "app_fictional_cut_3", "appl\n'
                   + ledger.read_text().splitlines()[-2] + "\n")
    assert [e.application_id for e in read_submission_lines(own)[0]] == [ids["two"]]
    assert read_submission_lines(own)[1] == 1


def test_a_kept_draft_is_listed_with_its_manual_remedy(paths, fake, tmp_path):
    ids = _approved_batch(paths, ["done", "changed", "kept"])
    _plan(tmp_path, {ids["done"]: "submitted", ids["changed"]: "changed", ids["kept"]: "kept_draft"})

    summary = asyncio.run(run_submissions(_options(paths, fake), approved_targets(
        paths, "default", source_batch="b1"), source_batch="b1"))

    assert summary.totals == {"submitted": 1, "needs_input": 2}
    assert summary.kept_drafts == [ids["kept"]]
    [kept] = [e for e in read_submissions(paths.home / "batches" / "b1" / "ledger.jsonl")
              if e.application_id == ids["kept"]]
    assert kept.outcome == "needs_input" and kept.message.startswith(KEPT_DRAFT_MESSAGE)
    text = render_submissions_markdown(summary)
    assert (f"needs_input (interviewmaxxing status APP; prepare, review and approve again if the "
            f"form changed): {ids['changed']}\n") in text
    assert ("needs_input, the site resumed a draft it kept (submit it in the browser yourself; "
            f"preparing it again reopens the same draft): {ids['kept']}\n") in text
    report = build_report(paths, ["b1"])
    assert report.submissions is not None and report.submissions.kept_drafts == [ids["kept"]]
    assert (f"the site resumed a draft it kept (submit each in the browser yourself; preparing it "
            f"again reopens the same draft): {ids['kept']}") in render_report_markdown(report)


def test_a_rejected_submission_is_submitted_again_by_the_next_run_from_another_browser(
    paths, fake, tmp_path
):
    ids = _approved_batch(paths, ["spam"])
    _plan(tmp_path, {ids["spam"]: "rejected_unless_opencli"})
    ledger = paths.home / "batches" / "b1" / "ledger.jsonl"

    first = asyncio.run(run_submissions(_options(paths, fake), approved_targets(
        paths, "default", source_batch="b1"), source_batch="b1"))

    [entry] = read_submissions(ledger)
    assert (entry.outcome, entry.state) == ("rejected", S.FAILED_RETRYABLE)
    assert "flagged as possible spam" in entry.message
    assert first.totals == {"rejected": 1}
    assert "rejected (the site refused them and received nothing" in render_submissions_markdown(first)
    targets = approved_targets(paths, "default", source_batch="b1")  # still approved
    assert [t.application_id for t in targets] == [ids["spam"]]

    second = asyncio.run(run_submissions(_options(paths, fake, browser="opencli",
                                                  opencli_profile="fictional-profile"),
                                         targets, source_batch="b1"))

    assert second.totals == {"submitted": 1} and second.launched == 1
    first_argv, second_argv = (line["argv"] for line in _log(tmp_path))
    assert "--browser" not in first_argv
    assert second_argv[-4:] == ["--browser", "opencli", "--opencli-profile", "fictional-profile"]
    assert [e.attempt for e in read_submissions(ledger)] == [1, 2]
    rejected = ApplyOutcome(application_id="app_x", state=S.FAILED_RETRYABLE,
                            message="Rejected by the site: 'flagged as possible spam'.")
    assert classify_submission(rejected, 3) == "rejected"


# --- browser profiles -----------------------------------------------------------------------------


def test_submission_slots_never_wait_on_a_running_prepare_batch(paths, fake, tmp_path):
    """A prepare batch's worker holds the runner's lock on its ``w0`` profile. Submission
    slot 0 is ``s0``, so the submission runs instead of stopping as busy."""
    ids = _approved_batch(paths, ["one"])
    _plan(tmp_path, {ids["one"]: "submitted"})
    workers = paths.home / "browser-workers"
    prepare = BatchOptions(paths=paths, candidate_id="default", batch_id="p1")
    assert prepare.worker_browser_dir(0) == workers / "w0"
    with browser_profile_lock(prepare.worker_browser_dir(0)):
        summary = asyncio.run(run_submissions(
            _options(paths, fake), approved_targets(paths, "default", source_batch="b1"),
            source_batch="b1"))
    assert summary.totals == {"submitted": 1}
    [line] = _log(tmp_path)
    assert line["env"]["IMX_BROWSER_DIR"] == str(workers / "s0")
    assert stat.S_IMODE((workers / "s0").stat().st_mode) == 0o700


def test_a_submission_blocked_by_a_busy_profile_is_submitted_by_the_next_run_once(
    paths, fake, tmp_path, monkeypatch, capsys
):
    """The profile was in use (exit 4, nothing ran): the application keeps its approval,
    the next ``submit-approved --batch`` submits it and the one after launches nothing."""
    ids = _approved_batch(paths, ["one"])
    _plan(tmp_path, {ids["one"]: "submitted"})
    monkeypatch.setattr(batch_module, "default_command", lambda: [sys.executable, str(fake)])
    monkeypatch.setenv("IMX_ALLOW_SUBMISSION", "1")
    argv = ["--home", str(paths.home), "submit-approved", "--batch", "b1", "--yes"]
    ledger = paths.home / "batches" / "b1" / "ledger.jsonl"
    with browser_profile_lock(paths.home / "browser-workers" / "s0"):  # e.g. a `submit` by hand
        assert main(argv) == EXIT_INCOMPLETE
    [blocked] = read_submissions(ledger)
    assert blocked.outcome == "blocked" and BUSY_MESSAGE in blocked.message
    with ApplicationStore.open(paths.state_db) as store:
        assert store.approved_packet(ids["one"]) is not None
        assert store.get_application(ids["one"]).state is S.NEEDS_INPUT

    assert main(argv) == EXIT_OK
    assert [(e.outcome, e.attempt) for e in read_submissions(ledger)] == [
        ("blocked", 1), ("submitted", 2)]
    capsys.readouterr()
    assert main(argv) == EXIT_OK
    assert "No approved application to submit" in capsys.readouterr().out
    assert [line["app"] for line in _log(tmp_path)] == [ids["one"]] * 2  # busy, then submitted
    assert len(read_submissions(ledger)) == 2
    with ApplicationStore.open(paths.state_db) as store:
        assert store.get_application(ids["one"]).state is S.SUBMITTED


# --- pipeline cards -------------------------------------------------------------------------------


def _card(paths: LocalPaths, app_id: str | None, name: str, lane: str = "saved") -> str:
    """A fictional pipeline card in ``lane`` linked to ``app_id``; its id."""
    with PipelineStore.from_paths(paths) as pipeline:
        return pipeline.create_item("default", NewPipelineItem(
            tracking=TrackingFields(company="Brambleway Analytics", role=name.title()),
            lane=lane, listing_id=f"lst_{name}", application_url=f"{ORIGIN}/{name}",
            application_id=app_id)).id


def _lanes(paths: LocalPaths, cards: dict[str, str]) -> dict[str, str]:
    with PipelineStore.from_paths(paths) as pipeline:
        return {name: pipeline.get_item("default", card).lane for name, card in cards.items()}


def test_only_a_confirmed_submission_moves_its_saved_card_to_applied(paths, fake, tmp_path):
    kinds = ["submitted", "uncertain", "changed", "closed", "refused", "retryable", "garbage"]
    ids = _approved_batch(paths, kinds)
    cards = {k: _card(paths, ids[k], k) for k in kinds}
    _plan(tmp_path, {ids[k]: k for k in kinds})

    summary = asyncio.run(run_submissions(
        _options(paths, fake, workers=2), approved_targets(paths, "default", source_batch="b1"),
        source_batch="b1"))

    entries = {e.application_id: e for e in read_submissions(paths.home / "batches" / "b1" /
                                                             "ledger.jsonl")}
    by_kind = {k: entries[ids[k]] for k in kinds}
    assert {k: e.outcome for k, e in by_kind.items()} == {
        "submitted": "submitted", "uncertain": "uncertain", "changed": "needs_input",
        "closed": "blocked", "refused": "blocked", "retryable": "error", "garbage": "error"}
    assert _lanes(paths, cards) == {k: "applied" if k == "submitted" else "saved" for k in kinds}
    done = by_kind["submitted"]
    assert (done.applied_synced, done.applied_sync_reason) == (True, None)
    assert all((e.applied_synced, e.applied_sync_reason) == (None, None)
               for k, e in by_kind.items() if k != "submitted")
    with PipelineStore.from_paths(paths) as pipeline:
        [move] = [h for h in pipeline.history("default", cards["submitted"]) if h.kind == "moved"]
        assert pipeline.get_item("default", cards["submitted"]).application_id == ids["submitted"]
    assert (move.from_lane, move.to_lane) == ("saved", "applied")
    assert move.note == (
        f"Submitted on {done.finished_at:%Y-%m-%d} (UTC) by submit-approved b1; the site "
        f"confirmed it (application {ids['submitted']}, receipt {done.receipt_id}, "
        "confirmation BWA-000777)")
    assert (summary.cards_applied, summary.card_problems) == (1, {})
    assert "- pipeline cards moved to Applied: 1" in render_submissions_markdown(summary)
    assert batch_module.format_submission(done).endswith("[card moved to Applied]")

    # A rerun launches the rest again; the moved card is not touched twice.
    asyncio.run(run_submissions(_options(paths, fake, workers=2),
                                approved_targets(paths, "default", source_batch="b1"),
                                source_batch="b1"))
    with PipelineStore.from_paths(paths) as pipeline:
        assert len([h for h in pipeline.history("default", cards["submitted"])
                    if h.kind == "moved"]) == 1
    assert _lanes(paths, cards) == {k: "applied" if k == "submitted" else "saved" for k in kinds}


def test_a_card_already_applied_or_closed_is_left_alone(paths, tmp_path):
    cards = {name: _card(paths, f"app_{name}", name, lane)
             for name, lane in (("applied", "applied"), ("closed", "closed"),
                                ("later", "interviewing"), ("bystander", "saved"))}
    confirmed = {"receipt_id": "sub_fictional", "reference": None, "by": "submit",
                 "submitted_at": datetime(2026, 9, 25, 9, 30, tzinfo=UTC)}

    def move(app_id: str, where: LocalPaths = paths) -> tuple[bool | None, str | None]:
        return batch_module.move_submitted_card(where, "default", app_id, **confirmed)

    assert move("app_applied") == move("app_closed") == (None, None)
    assert move("app_later") == (False, "card not in Saved (in interviewing)")
    assert move("app_nobody") == (None, None)  # no card links it
    assert _lanes(paths, cards) == {"applied": "applied", "closed": "closed",
                                    "later": "interviewing", "bystander": "saved"}
    with PipelineStore.from_paths(paths) as pipeline:
        assert not any(h.kind == "moved" for c in cards.values()
                       for h in pipeline.history("default", c))

    bare = LocalPaths.from_env({}, home=tmp_path / "bare")
    bare.ensure()
    assert move("app_applied", bare) == (None, None)
    assert not default_pipeline_db(bare).exists()  # never created


def test_a_card_that_cannot_be_moved_is_counted_with_its_reason(paths, fake, tmp_path):
    ids = _approved_batch(paths, ["one"])
    card = _card(paths, ids["one"], "one", lane="follow-up")
    _plan(tmp_path, {ids["one"]: "submitted"})
    summary = asyncio.run(run_submissions(
        _options(paths, fake), approved_targets(paths, "default", source_batch="b1"),
        source_batch="b1"))
    [entry] = read_submissions(paths.home / "batches" / "b1" / "ledger.jsonl")
    assert (entry.outcome, entry.applied_synced) == ("submitted", False)
    assert entry.applied_sync_reason == "card not in Saved (in follow-up)"
    assert (summary.cards_applied, summary.card_problems) == (
        0, {"card not in Saved (in follow-up)": 1})
    assert ("- pipeline cards moved to Applied: 0; not moved: card not in Saved (in follow-up)"
            in render_submissions_markdown(summary))
    assert batch_module.format_submission(entry).endswith("[card not moved to Applied]")
    assert _lanes(paths, {"one": card}) == {"one": "follow-up"}
