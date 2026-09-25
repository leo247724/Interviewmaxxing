"""``prepare-batch --retry`` offline. A store-backed fake ``interviewmaxxing`` stands in
for ``apply`` and ``resume`` (no browser), so selection by where each application
stands now, the explicit-only skip, holds answered after the stop, the retry ledger,
holds cleared, the inherited run options and the CLI all run without Playwright.
Every company, URL, answer and id here is fictional."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_cli import batch as batch_module
from interviewmaxxing_cli.batch import (
    BatchOptions,
    BatchRow,
    BatchRunOptions,
    LedgerEntry,
    MissingItem,
    append_ledger,
    classify,
    read_ledger,
    read_summary,
    render_retry_markdown,
    render_summary_markdown,
    run_batch,
)
from interviewmaxxing_cli.main import EXIT_ERROR, EXIT_OK, EXIT_USAGE, build_parser, main
from interviewmaxxing_cli.retry import (
    RETRY_OUTCOMES,
    default_retry_id,
    holds_cleared,
    plan_retry,
    retry_options,
    run_retry,
)
from interviewmaxxing_cli.triage import hold_key
from interviewmaxxing_core import (
    AnswerScope,
    ApplicationState,
    ApplicationStore,
    ApplyOutcome,
    ControlType,
    LocalPaths,
    MissingInput,
    MissingReason,
    SavedAnswer,
    SemanticType,
    TextValue,
    UserInput,
)

S = ApplicationState
ORIGIN = "https://jobs.fictional.example/brambleway"
RESUME = Path(__file__).resolve().parents[1] / "fixtures" / "core" / "resume-avery-example.pdf"
KINDS = ["prepared", "held", "explicit", "flaky", "closed", "crash", "garbage", "noform"]
NOTICE = "What is your notice period?"
SPONSOR = "Will you now or in the future require visa sponsorship?"
SALARY = "Desired annual base salary (USD)"
START = "Earliest start date"
WRITER = "anthropic/claude-opus-5.5"
CLI_DEFAULTS: dict[str, Any] = {
    "candidate_id": "default", "workers": 1, "per_job_timeout_s": 900.0, "retry_retryable": 1,
    "sync_closed": True, "browser": "playwright", "opencli_profile": None, "ai_routing": False,
    "env_file": None, "writer_model": None, "rag_connection_file": None, "writer_effort": None,
}
"""What ``prepare-batch`` gives when no run flag is passed."""

RETRY_FAKE_CLI = textwrap.dedent('''\
    """Fake ``interviewmaxxing apply URL`` / ``resume APP`` over the real store. The URL's
    last path segment is the job's kind; ``resume`` ends as FAKE_RESUME (JSON kind ->
    result) says, else as ``apply`` did. Every call is logged to FAKE_LOG."""
    import json, os, sys
    from pathlib import Path

    from interviewmaxxing_core import ApplicationState as S, ApplicationStore, LocalPaths

    command = sys.argv[1]
    assert command in ("apply", "resume") and "--json" in sys.argv and "--headless" in sys.argv
    with open(os.environ["FAKE_LOG"], "a") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "pid": os.getpid(), "env": {
            k: v for k, v in os.environ.items() if k.startswith("IMX_")}}) + "\\n")

    PREPARED = ("Prepared to the final review step. Nothing was submitted. Submission "
                "remains disabled when this application is resumed.")
    FAILED_FIELDS = [{"field_id": "notice_period", "label": @NOTICE@,
                      "status": "VERIFICATION_MISMATCH",
                      "detail": "read back 'One month' instead of 'Two weeks'"}]

    def question(url, field_id, fingerprint, label, reason, control, semantic="UNKNOWN"):
        return {"field_id": field_id, "form_url": url, "form_step": 0,
                "field_fingerprint": fingerprint * 32, "label": label, "reason": reason,
                "prompt": "Please answer.", "required": True, "control_type": control,
                "semantic_type": semantic}

    def holds(url, result):
        notice = question(url, "notice_period", "ab", @NOTICE@, "NO_ANSWER", "SELECT")
        sponsor = question(url, "sponsorship", "cd", @SPONSOR@, "EXPLICIT_ANSWER_REQUIRED",
                           "RADIO", "SPONSORSHIP")
        salary = question(url, "salary", "ef", @SALARY@, "EXPLICIT_ANSWER_REQUIRED", "TEXT",
                          "SALARY_EXPECTATION")
        start = question(url, "start_date", "12", @START@, "NO_ANSWER", "TEXT")
        return {"held": [notice, sponsor], "explicit": [sponsor, salary], "start": [start],
                "notice": [notice]}.get(result)

    def finish(store, claim, app_id, url, result):
        if store.get_application(app_id).state is not S.INSPECTING:
            store.transition(claim, S.INSPECTING)
        missing, state = [], S.NEEDS_INPUT
        if result == "prepared":
            store.append_event(claim, "preparation.ready", {"submitted": False})
            store.transition(claim, S.NEEDS_INPUT, metadata={
                "missing_inputs": [], "reason": "prepared for final review; submission disabled"})
            message = PREPARED
        elif holds(url, result) is not None:
            missing = holds(url, result)
            message = f"{len(missing)} required question(s) need your answer."
            store.transition(claim, S.NEEDS_INPUT, metadata={"missing_inputs": missing,
                                                             "reason": "missing answers"})
        elif result == "flaky":
            state, message = S.FAILED_RETRYABLE, "Could not fill notice_period reliably; nothing was submitted."
            store.transition(claim, state, failure_reason=message,
                             metadata={"failed_fields": FAILED_FIELDS})
        elif result == "noform":
            state = S.FAILED_RETRYABLE
            message = ("Could not reach the application form: the page is UNKNOWN, not an "
                       "application form.")
            store.transition(claim, state, failure_reason=message)
        elif result == "closed":
            state, message = S.FAILED_PERMANENT, "The job is no longer accepting applications."
            store.transition(claim, state, failure_reason=message)
        else:
            raise SystemExit("unknown result " + result)
        return {"application_id": app_id, "state": state.value, "receipt": None,
                "missing_inputs": missing, "message": message}

    def kind_of(url):
        return url.rsplit("/", 1)[-1].split("?")[0]

    with ApplicationStore.open(LocalPaths.from_env().state_db) as store:
        if command == "apply":
            url = sys.argv[2]
            result = kind_of(url)
            if result == "garbage":
                counter = Path(os.environ["FAKE_DIR"]) / "garbage.count"
                calls = int(counter.read_text()) if counter.exists() else 0
                counter.write_text(str(calls + 1))
                if calls == 0:  # crashes before recording anything
                    print("this is not json")
                    print("the fake CLI exploded (fictional)", file=sys.stderr)
                    sys.exit(1)
                result = "prepared"
            candidate = sys.argv[sys.argv.index("--candidate") + 1]
            app_id = store.record_request(candidate, url).application.id
        else:
            app_id = sys.argv[2]
            url = store.list_requests(app_id)[0].application_url
            result = json.loads(os.environ.get("FAKE_RESUME", "{}")).get(kind_of(url), kind_of(url))
        claim = store.claim(app_id, "retry-fake-cli")
        if result == "crash":  # the run started and recorded no outcome (like a timeout)
            if store.get_application(app_id).state is not S.INSPECTING:
                store.transition(claim, S.INSPECTING)
            store.release(claim)
            print("the fake CLI crashed mid-run (fictional)", file=sys.stderr)
            sys.exit(1)
        outcome = finish(store, claim, app_id, url, result)
        store.release(claim)
    print(json.dumps(outcome))
    sys.exit(3)
''')
for _name, _value in (("NOTICE", NOTICE), ("SPONSOR", SPONSOR), ("SALARY", SALARY),
                      ("START", START)):
    RETRY_FAKE_CLI = RETRY_FAKE_CLI.replace(f"@{_name}@", repr(_value))


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "retry_fake_cli.py"
    script.write_text(RETRY_FAKE_CLI)
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    monkeypatch.setenv("FAKE_DIR", str(tmp_path))
    monkeypatch.delenv("FAKE_RESUME", raising=False)
    return script


@pytest.fixture
def paths(tmp_path: Path) -> LocalPaths:
    return LocalPaths.from_env({}, home=tmp_path / "home")


def calls(tmp_path: Path) -> list[dict[str, Any]]:
    log = tmp_path / "fake.log"
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def row(kind: str, backend: str = "mock") -> BatchRow:
    return BatchRow(listing_id=f"lst_{kind}", company="Brambleway", title=kind.title(),
                    application_url=f"{ORIGIN}/{kind}", backend=backend, status="resolved")


def first_batch(fake: Path, paths: LocalPaths, kinds: list[str] = KINDS,
                **settings: Any) -> dict[str, LedgerEntry]:
    options = BatchOptions(paths=paths, candidate_id="default", batch_id="b1",
                           command=[sys.executable, str(fake)], **settings)
    entries: list[LedgerEntry] = []
    asyncio.run(run_batch(options, [row(k) for k in kinds], on_entry=entries.append))
    return {e.listing_id: e for e in entries}


def retry(fake: Path, paths: LocalPaths, batch_id: str = "b1-retry", **plan: Any
          ) -> tuple[Any, list[LedgerEntry]]:
    summary = read_summary(paths, "b1")
    assert summary is not None
    options = retry_options(paths, retry_of="b1", batch_id=batch_id,
                            recorded=summary.run_options, cli=CLI_DEFAULTS, explicit=set(),
                            command=[sys.executable, str(fake)])
    entries: list[LedgerEntry] = []
    result = asyncio.run(run_retry(options, plan_retry(paths, "b1", candidate_id="default",
                                                       **plan), on_entry=entries.append))
    return result, entries


# --- the retry run -----------------------------------------------------------------------------


@pytest.mark.slow
def test_retry_runs_held_and_failed_applications_again(fake, paths, tmp_path, monkeypatch):
    first = first_batch(fake, paths, workers=2, per_job_timeout_s=60, ai_routing=True,
                        env_file=Path("/private/fictional.env"), writer_model=WRITER)
    assert {k: e.outcome for k, e in first.items()} == {
        "lst_prepared": "prepared", "lst_held": "needs_input", "lst_explicit": "needs_input",
        "lst_flaky": "failed_retryable", "lst_closed": "closed", "lst_crash": "error",
        "lst_garbage": "error", "lst_noform": "failed_retryable"}
    assert first["lst_crash"].application_id is None  # the store has it, INSPECTING
    ledger = paths.home / "batches" / "b1" / "ledger.jsonl"
    before = ledger.read_bytes()
    recorded = read_summary(paths, "b1")
    assert recorded is not None and recorded.run_options == BatchRunOptions(
        candidate_id="default", workers=2, per_job_timeout_s=60.0, retry_retryable=1,
        sync_closed=True, browser="playwright", ai_routing=True,
        env_file="/private/fictional.env", writer_model=WRITER)

    # By default a held application runs again only once something was answered for it;
    # failed, unknown and error ones always do.
    default = plan_retry(paths, "b1", candidate_id="default")
    assert {i.row.listing_id for i in default.items} == {
        "lst_flaky", "lst_crash", "lst_garbage", "lst_noform"}
    assert default.stats.skipped == {"prepared": 1, "closed": 1,
                                     "nothing answered since the stop": 2}
    plan = plan_retry(paths, "b1", candidate_id="default", rerun_all=True)  # after a fix
    assert {i.row.listing_id: (i.previous_outcome, i.row.application_id is not None,
                               len(i.holds_before)) for i in plan.items} == {
        "lst_held": ("needs_input", True, 2), "lst_flaky": ("failed_retryable", True, 0),
        "lst_crash": ("unknown", True, 0), "lst_garbage": ("error", False, 0),
        "lst_noform": ("failed_retryable", True, 0)}
    assert (plan.stats.considered, plan.stats.selected) == (8, 5)
    assert plan.stats.skipped == {"prepared": 1, "closed": 1, "explicit answers only": 1}

    monkeypatch.setenv("FAKE_RESUME", json.dumps(
        {"held": "prepared", "flaky": "prepared", "crash": "start"}))
    summary, entries = retry(fake, paths, rerun_all=True)
    by_id = {e.listing_id: e for e in entries}
    assert {k: (e.previous_outcome, e.outcome, e.holds_before, e.holds_cleared)
            for k, e in by_id.items()} == {
        "lst_held": ("needs_input", "prepared", 2, 2),
        "lst_flaky": ("failed_retryable", "prepared", 0, 0),
        "lst_crash": ("unknown", "needs_input", 0, 0),
        "lst_garbage": ("error", "prepared", 0, 0),
        "lst_noform": ("failed_retryable", "failed_retryable", 0, None)}
    assert all(e.retry_of == "b1" and e.batch_id == "b1-retry" and e.attempt == 1
               for e in entries)
    for listing in ("lst_held", "lst_flaky", "lst_noform"):  # the same application, resumed
        assert by_id[listing].application_id == first[listing].application_id
    assert by_id["lst_crash"].application_id and by_id["lst_garbage"].application_id
    assert ledger.read_bytes() == before  # the retried ledger is only read
    assert [e.listing_id for e in read_ledger(paths.home / "batches" / "b1-retry" /
                                              "ledger.jsonl")] == [e.listing_id for e in entries]

    # Every job ran with the original batch's harness settings: resume for a stored
    # application, apply for the row that never recorded one; two worker slots.
    retried = calls(tmp_path)[len(KINDS):]  # after the first batch's jobs
    assert len(retried) == 5
    flags = ["--headless", "--ai-routing", "--env-file", "/private/fictional.env",
             "--writer-model", WRITER]
    resumed = {c["argv"][1]: c["argv"] for c in retried if c["argv"][0] == "resume"}
    assert set(resumed) == {by_id[k].application_id for k in
                            ("lst_held", "lst_flaky", "lst_crash", "lst_noform")}
    assert all(argv[2:] == ["--json", *flags] for argv in resumed.values())
    [applied] = [c["argv"] for c in retried if c["argv"][0] == "apply"]
    assert applied == ["apply", f"{ORIGIN}/garbage", "--json", "--candidate", "default", *flags]
    workers = paths.home / "browser-workers"
    assert {c["env"]["IMX_BROWSER_DIR"] for c in retried} <= {str(workers / "w0"),
                                                              str(workers / "w1")}

    stats = summary.retry
    assert stats is not None and stats.retry_of == "b1"
    assert stats.model_dump() == {
        "retry_of": "b1", "outcomes": list(RETRY_OUTCOMES), "include_explicit": False,
        "rerun_all": True, "user_actions": False, "only_apps": [], "considered": 8,
        "selected": 5,
        "selected_by": {"failed_retryable": 2, "--all": 1, "unknown": 1, "error": 1},
        "skipped": {"prepared": 1, "closed": 1, "explicit answers only": 1},
        "retried": 5, "prepared": 3, "holds_before": 2, "holds_cleared": 2, "holds_open": 1,
        "transitions": {"needs_input": {"prepared": 1},
                        "failed_retryable": {"prepared": 1, "failed_retryable": 1},
                        "unknown": {"needs_input": 1}, "error": {"prepared": 1}},
        "ledger_lines_ignored": 0}
    assert summary.totals == {"prepared": 3, "needs_input": 1, "failed_retryable": 1}
    assert summary.run_options is not None and summary.run_options.workers == 2
    written = read_summary(paths, "b1-retry")
    assert written is not None and written.retry == stats
    text = render_summary_markdown(summary)
    assert "## Retry of b1" in text and "- holds cleared: 2 of 2; open now: 1" in text
    assert "- retried: 5; now prepared: 3" in text
    assert "| was | prepared | needs_input | failed_retryable |" in text

    # Retrying again finds the prepared ones settled; what still fails runs, and what is
    # held with nothing answered since does not (unless --all).
    again = plan_retry(paths, "b1", candidate_id="default")
    assert {i.row.listing_id: i.previous_outcome for i in again.items} == {
        "lst_noform": "failed_retryable"}
    assert again.stats.skipped == {"prepared": 4, "closed": 1,
                                   "nothing answered since the stop": 2}
    forced = plan_retry(paths, "b1", candidate_id="default", rerun_all=True)
    assert {i.row.listing_id for i in forced.items} == {"lst_crash", "lst_noform"}


@pytest.mark.slow
def test_holds_cleared_follow_the_question_wording(fake, paths, monkeypatch):
    first_batch(fake, paths, ["held", "prepared"])
    monkeypatch.setenv("FAKE_RESUME", json.dumps({"held": "notice"}))
    summary, [entry] = retry(fake, paths, rerun_all=True)
    assert (entry.previous_outcome, entry.outcome, entry.holds_before, entry.holds_cleared) == \
        ("needs_input", "needs_input", 2, 1)  # sponsorship cleared, notice period still asked
    assert [m.label for m in entry.missing_items] == [NOTICE]
    assert summary.retry is not None
    assert (summary.retry.holds_before, summary.retry.holds_cleared,
            summary.retry.holds_open) == (2, 1, 1)
    # Running the retry batch again continues it: the held row is settled there.
    rerun, entries = retry(fake, paths, rerun_all=True)
    assert entries == [] and rerun.launched == 0 and rerun.skipped_settled == 1
    assert rerun.retry is not None and rerun.retry.holds_cleared == 1


def entry_with(outcome: str, labels: list[str]) -> LedgerEntry:
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    return LedgerEntry(batch_id="r", listing_id="l", application_url=f"{ORIGIN}/x", attempt=1,
                       outcome=outcome, missing_items=[MissingItem(label=x, reason="NO_ANSWER")
                                                       for x in labels],
                       started_at=now, finished_at=now, duration_s=0.0)


def test_holds_cleared_counts():
    before = (hold_key(NOTICE), hold_key(SPONSOR))
    assert holds_cleared(before, entry_with("prepared", [])) == 2
    assert holds_cleared(before, entry_with("needs_input", ["what is your NOTICE period"])) == 1
    assert holds_cleared(before, entry_with("needs_input", [START])) == 2  # a new question
    assert holds_cleared(before, entry_with("needs_input", [NOTICE, SPONSOR, START])) == 0
    assert holds_cleared(before * 2, entry_with("needs_input", [NOTICE])) == 3  # counted each
    for outcome in ("failed_retryable", "error", "closed", "duplicate"):
        assert holds_cleared(before, entry_with(outcome, [])) is None
    assert holds_cleared((), entry_with("prepared", [])) == 0


# --- selection by where each application stands now ----------------------------------------


def question(url: str, field_id: str, label: str, reason: MissingReason,
             semantic: SemanticType = SemanticType.UNKNOWN,
             control: ControlType = ControlType.TEXT) -> MissingInput:
    return MissingInput(field_id=field_id, form_url=url, form_step=0,
                        field_fingerprint=(field_id.encode().hex() * 64)[:64], label=label,
                        reason=reason, prompt="Please answer.", semantic_type=semantic,
                        control_type=control)


def explicit_holds(url: str) -> list[MissingInput]:
    return [question(url, "sponsorship", SPONSOR, MissingReason.EXPLICIT_ANSWER_REQUIRED,
                     SemanticType.SPONSORSHIP, ControlType.RADIO),
            question(url, "salary", SALARY, MissingReason.EXPLICIT_ANSWER_REQUIRED,
                     SemanticType.SALARY_EXPECTATION)]


def stored(paths: LocalPaths, path: str, stop: str, missing: list[MissingInput] | None = None,
           *, candidate: str = "default") -> str:
    """An application for ``ORIGIN/path`` left as a run leaves it: ``prepared``, ``held``
    (with ``missing``), ``failed``, ``closed`` or ``inspecting`` (no outcome recorded)."""
    paths.ensure()
    url = f"{ORIGIN}/{path}"
    with ApplicationStore.open(paths.state_db) as store:
        app = store.record_request(candidate, url).application
        claim = store.claim(app.id, "test")
        store.transition(claim, S.INSPECTING)
        if stop == "prepared":
            store.append_event(claim, "preparation.ready", {"submitted": False})
            store.transition(claim, S.NEEDS_INPUT, metadata={"missing_inputs": [],
                                                             "reason": "prepared"})
        elif stop == "held":
            store.transition(claim, S.NEEDS_INPUT, metadata={
                "missing_inputs": [m.model_dump(mode="json") for m in missing or []],
                "reason": "missing answers"})
        elif stop == "failed":
            store.transition(claim, S.FAILED_RETRYABLE, failure_reason="Stopped (fictional).")
        elif stop == "closed":
            store.transition(claim, S.FAILED_PERMANENT,
                             failure_reason="The job is no longer accepting applications.")
        else:
            assert stop == "inspecting"
        store.release(claim)
        return app.id


def line(listing: str, outcome: str, path: str, app: str | None, *, backend: str = "mock",
         minute: int = 0, **fields: Any) -> LedgerEntry:
    finished = datetime(2026, 9, 24, 9, 0, tzinfo=UTC) + timedelta(minutes=minute)
    fields.setdefault("state", {"prepared": S.NEEDS_INPUT, "needs_input": S.NEEDS_INPUT,
                                "failed_retryable": S.FAILED_RETRYABLE,
                                "closed": S.FAILED_PERMANENT}.get(outcome))
    return LedgerEntry(batch_id="b1", listing_id=listing, application_url=f"{ORIGIN}/{path}",
                       backend=backend, attempt=1, application_id=app, outcome=outcome,
                       started_at=finished, finished_at=finished, duration_s=1.0, **fields)


def write_ledger(paths: LocalPaths, *entries: LedgerEntry) -> None:
    for entry in entries:
        append_ledger(paths.home / "batches" / "b1" / "ledger.jsonl", entry)


def test_selection_reads_where_each_application_stands_now(paths):
    url = f"{ORIGIN}/held-now"
    now_prepared = stored(paths, "now-prepared", "prepared")
    held_again = stored(paths, "held-again", "held", [question(url, "q", NOTICE,
                                                               MissingReason.NO_ANSWER)])
    now_closed = stored(paths, "now-closed", "closed")
    failing = stored(paths, "failing", "failed")
    recorded = stored(paths, "recorded", "held", [question(url, "q", START,
                                                           MissingReason.NO_ANSWER)])
    halfway = stored(paths, "halfway", "inspecting")
    lever = stored(paths, "lever", "held", [question(url, "q", START, MissingReason.NO_ANSWER)])
    write_ledger(
        paths,
        line("l-now-prepared", "needs_input", "now-prepared", now_prepared),
        line("l-ledger-prepared", "prepared", "held-again", held_again),  # held since
        line("l-now-closed", "needs_input", "now-closed", now_closed),
        line("l-failing", "failed_retryable", "failing", failing),
        line("l-recorded", "already_recorded", "recorded", recorded),
        line("l-unknown-app", "needs_input", "gone", "app_fictional_gone"),
        line("l-duplicate", "duplicate", "dup", "app_fictional_dup", state=S.DUPLICATE),
        line("l-halfway", "error", "halfway", None),  # timed out; the store knows the app
        line("l-lever", "needs_input", "lever", lever, backend="lever"),
        line("l-failing", "failed_retryable", "failing", failing, minute=1),  # latest line
    )
    default = plan_retry(paths, "b1", candidate_id="default")
    assert [i.row.listing_id for i in default.items] == ["l-failing", "l-halfway"]
    assert default.stats.skipped["nothing answered since the stop"] == 2
    plan = plan_retry(paths, "b1", candidate_id="default", rerun_all=True)
    assert {i.row.listing_id: (i.previous_outcome, i.row.application_id)
            for i in plan.items} == {
        "l-failing": ("failed_retryable", failing), "l-recorded": ("needs_input", recorded),
        "l-halfway": ("unknown", halfway), "l-lever": ("needs_input", lever)}
    assert plan.stats.skipped == {"prepared": 2, "closed": 1, "application not found": 1,
                                  "duplicate": 1}
    assert (plan.stats.considered, plan.stats.selected) == (9, 4)

    only = plan_retry(paths, "b1", candidate_id="default", outcomes=["failed_retryable"])
    assert [i.row.listing_id for i in only.items] == ["l-failing"]
    assert only.stats.skipped["not selected (needs_input)"] == 2
    assert only.stats.skipped["not selected (unknown)"] == 1
    assert only.stats.outcomes == ["failed_retryable"]

    lever_only = plan_retry(paths, "b1", candidate_id="default", backends={"lever"},
                            rerun_all=True)
    assert [i.row.listing_id for i in lever_only.items] == ["l-lever"]
    assert lever_only.stats.considered == 1 and lever_only.stats.skipped == {}

    capped = plan_retry(paths, "b1", candidate_id="default", limit=1, rerun_all=True)
    assert len(capped.items) == 1 and capped.stats.skipped["over --limit"] == 3

    # Another candidate has none of these applications: only the row that never recorded
    # one can run (``apply`` records it for them).
    other = plan_retry(paths, "b1", candidate_id="someone-else")
    assert [(i.row.listing_id, i.row.application_id) for i in other.items] == [
        ("l-halfway", None)]
    assert other.stats.skipped["application not found"] == 6


def test_selection_skips_a_second_listing_of_the_same_application(paths):
    app = stored(paths, "shared", "held", [question(f"{ORIGIN}/shared", "q", START,
                                                    MissingReason.NO_ANSWER)])
    write_ledger(paths, line("l-first", "needs_input", "shared", app),
                 line("l-alias", "already_recorded", "shared", app, minute=1))
    plan = plan_retry(paths, "b1", candidate_id="default", rerun_all=True)
    assert [i.row.listing_id for i in plan.items] == ["l-first"]
    assert plan.stats.skipped == {"same application as another listing": 1}


def test_plan_errors(paths):
    with pytest.raises(FileNotFoundError):
        plan_retry(paths, "nope", candidate_id="default")
    for bad in ("", "..", "a/b"):
        with pytest.raises(ValueError):
            plan_retry(paths, bad, candidate_id="default")
    write_ledger(paths, line("l1", "error", "x", None))
    for never in ("prepared", "closed", "bogus"):
        with pytest.raises(ValueError):
            plan_retry(paths, "b1", candidate_id="default", outcomes=[never])
    assert not paths.state_db.exists()  # selection only reads
    # Without a state database only rows that never recorded an application can run.
    plan = plan_retry(paths, "b1", candidate_id="default")
    assert [(i.row.listing_id, i.row.application_id) for i in plan.items] == [("l1", None)]


def write_profile(paths: LocalPaths, candidate: str = "default") -> LocalCandidateStore:
    directory = paths.profile_dir / candidate
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RESUME, directory / "resume.pdf")
    (directory / "profile.json").write_text(json.dumps({
        "id": candidate,
        "identity": {"first_name": "Avery", "last_name": "Example",
                     "email": "avery@example.test", "verified_at": "2026-09-01T12:00:00Z"},
        "resume": {"id": "resume_supplied", "path": "resume.pdf"}}))
    return LocalCandidateStore.from_paths(paths)


def saved(question_text: str, *, when: datetime, value: str = "No",
          semantic: SemanticType | None = None, answer_id: str = "sa_fictional") -> SavedAnswer:
    return SavedAnswer(id=answer_id, scope=AnswerScope.GLOBAL, semantic_type=semantic,
                       question=question_text, value=value, confirmed_at=when)


def test_explicit_only_holds_are_skipped_until_answered_after_the_stop(paths):
    candidates = write_profile(paths)
    url = f"{ORIGIN}/explicit"
    app = stored(paths, "explicit", "held", explicit_holds(url))
    write_ledger(paths, line("l-explicit", "needs_input", "explicit", app))

    def selected(**kwargs: Any) -> list[str]:
        return [i.row.listing_id for i in plan_retry(paths, "b1", candidate_id="default",
                                                      **kwargs).items]

    assert selected() == [] and selected(include_explicit=True) == []  # nothing answered
    assert plan_retry(paths, "b1", candidate_id="default").stats.skipped == {
        "nothing answered since the stop": 1}
    assert plan_retry(paths, "b1", candidate_id="default", rerun_all=True).stats.skipped == {
        "explicit answers only": 1}
    assert selected(include_explicit=True, rerun_all=True) == ["l-explicit"]

    # An answer that already existed when the application stopped did not help it then.
    candidates.save_answer("default", saved(SPONSOR, when=datetime(2026, 1, 1, tzinfo=UTC)))
    assert selected() == [] and selected(rerun_all=True) == []
    # The person answers the salary on this application: the sponsorship is still open.
    with ApplicationStore.open(paths.state_db) as store:
        claim = store.claim(app, "test")
        store.save_user_inputs(claim, [UserInput.answering(explicit_holds(url)[1],
                                                           TextValue(text="fictional"))])
        store.release(claim)
    assert selected() == []  # something answered, but only explicit questions are open
    assert plan_retry(paths, "b1", candidate_id="default").stats.skipped == {
        "explicit answers only": 1}
    # ... and saves a global answer for the sponsorship wording (any spelling of it).
    later = datetime.now(UTC) + timedelta(minutes=1)
    candidates.save_answer("default", saved(SPONSOR.upper().rstrip("?"), when=later,
                                            answer_id="sa_fictional_2"))
    plan = plan_retry(paths, "b1", candidate_id="default")
    assert [(i.row.listing_id, len(i.holds_before)) for i in plan.items] == [("l-explicit", 2)]


def test_a_mixed_hold_runs_again_with_all_or_once_something_is_answered(paths):
    url = f"{ORIGIN}/mixed"
    app = stored(paths, "mixed", "held", [*explicit_holds(url),
                                          question(url, "start", START, MissingReason.NO_ANSWER)])
    write_ledger(paths, line("l-mixed", "needs_input", "mixed", app))
    assert plan_retry(paths, "b1", candidate_id="default").items == ()
    forced = plan_retry(paths, "b1", candidate_id="default", rerun_all=True)
    assert [i.row.listing_id for i in forced.items] == ["l-mixed"] and forced.stats.rerun_all
    with ApplicationStore.open(paths.state_db) as store:  # the person answers the salary
        claim = store.claim(app, "test")
        store.save_user_inputs(claim, [UserInput.answering(explicit_holds(url)[1],
                                                           TextValue(text="fictional"))])
        store.release(claim)
    [again] = plan_retry(paths, "b1", candidate_id="default").items  # still a mixed hold
    assert (again.row.listing_id, len(again.holds_before)) == ("l-mixed", 3)


def test_holds_only_a_browser_can_clear_are_skipped_as_such_unless_user_actions(paths):
    """Sign-in, CAPTCHA, custom controls and files cannot be answered, so they never count
    as answered: a held application with only those open is skipped by default under its
    own reason (a headless retry meets them again) and runs with ``--user-actions``."""
    url = f"{ORIGIN}/browser"
    sign_in = MissingInput(field_id=None, label="Sign in to continue", prompt="Sign in in the browser.",
                           reason=MissingReason.USER_ACTION)
    captcha = MissingInput(field_id=None, label="Solve the CAPTCHA", prompt="Solve it in the browser.",
                           reason=MissingReason.USER_ACTION)
    pronouns = question(url, "pronouns", "Pronouns", MissingReason.UNSUPPORTED_CONTROL,
                        control=ControlType.UNSUPPORTED)
    cover = question(url, "cover", "Cover letter", MissingReason.NO_ANSWER, control=ControlType.FILE)
    notice = question(url, "q", NOTICE, MissingReason.NO_ANSWER)
    apps = {"signin": stored(paths, "signin", "held", [sign_in]),
            "captcha": stored(paths, "captcha", "held", [captcha]),
            "custom": stored(paths, "custom", "held", [pronouns, cover]),
            "mixed": stored(paths, "mixed", "held", [pronouns, notice])}
    write_ledger(paths, *(line(f"l-{name}", "needs_input", name, app) for name, app in apps.items()))

    default = plan_retry(paths, "b1", candidate_id="default")
    assert default.items == () and not default.stats.user_actions
    assert default.stats.skipped == {"browser actions only": 3, "nothing answered since the stop": 1}
    text = "\n".join(render_retry_markdown(default.stats))
    assert "- skipped: browser actions only (3), nothing answered since the stop (1)" in text
    assert "`interviewmaxxing resume APP --act`" in text and "with --user-actions" in text

    acting = plan_retry(paths, "b1", candidate_id="default", user_actions=True)
    assert [i.row.listing_id for i in acting.items] == ["l-signin", "l-captcha", "l-custom"]
    assert [len(i.holds_before) for i in acting.items] == [1, 1, 2]
    assert acting.stats.skipped == {"nothing answered since the stop": 1} and acting.stats.user_actions
    text = "\n".join(render_retry_markdown(acting.stats))
    assert "browser-action holds included (--user-actions)" in text and "resume APP --act" not in text
    # --all runs every held one anyway, the mixed hold included.
    assert len(plan_retry(paths, "b1", candidate_id="default", rerun_all=True).items) == 4


def test_the_selection_reasons_combine_user_actions_only_app_and_all(paths):
    """Each selected held application counts under the most specific reason: answered
    since the stop, named by --only-app, ``user actions`` (only browser actions open,
    --user-actions), then --all; nothing applies: its own skip reason."""
    url = f"{ORIGIN}/reasons"
    sign_in = MissingInput(field_id=None, label="Sign in to continue", prompt="Sign in.",
                           reason=MissingReason.USER_ACTION)
    upload = question(url, "cover", "Cover letter", MissingReason.NO_ANSWER,
                      control=ControlType.FILE)
    notice = question(url, "q", NOTICE, MissingReason.NO_ANSWER)
    apps = {"signin": stored(paths, "signin", "held", [sign_in]),
            "upload": stored(paths, "upload", "held", [upload]),
            "plain": stored(paths, "plain", "held", [notice]),
            "failing": stored(paths, "failing", "failed")}
    write_ledger(paths, *(line(f"l-{name}", "needs_input" if name != "failing"
                               else "failed_retryable", name, app)
                          for name, app in apps.items()))

    def plan(**kwargs: Any) -> Any:
        return plan_retry(paths, "b1", candidate_id="default", **kwargs)

    default = plan()
    assert (default.stats.selected_by, default.stats.skipped) == (
        {"failed_retryable": 1},
        {"browser actions only": 2, "nothing answered since the stop": 1})
    acting = plan(user_actions=True)
    assert [i.row.listing_id for i in acting.items] == ["l-signin", "l-upload", "l-failing"]
    assert acting.stats.selected_by == {"user actions": 2, "failed_retryable": 1}
    assert acting.stats.skipped == {"nothing answered since the stop": 1}
    everything = plan(user_actions=True, rerun_all=True)
    assert everything.stats.selected_by == {"user actions": 2, "--all": 1, "failed_retryable": 1}
    assert plan(rerun_all=True).stats.selected_by == {"--all": 3, "failed_retryable": 1}
    # --only-app runs a named application held only on browser actions without
    # --user-actions, and counts it as named even with --user-actions.
    named = plan(only_apps=[apps["signin"], apps["plain"]])
    assert [i.row.listing_id for i in named.items] == ["l-signin", "l-plain"]
    assert named.stats.selected_by == {"--only-app": 2} and named.stats.considered == 2
    both = plan(only_apps=[apps["upload"]], user_actions=True)
    assert (both.stats.selected_by, both.stats.user_actions, both.stats.only_apps) == (
        {"--only-app": 1}, True, [apps["upload"]])
    text = "\n".join(render_retry_markdown(both.stats))
    assert (f"(outcomes: needs_input, failed_retryable, unknown, error; browser-action holds "
            f"included (--user-actions); only {apps['upload']})") in text
    assert "- selected by: --only-app (1)" in text
    # Something answered since the stop outranks every flag.
    with ApplicationStore.open(paths.state_db) as store:
        claim = store.claim(apps["plain"], "test")
        store.save_user_inputs(claim, [UserInput.answering(notice, TextValue(text="fictional"))])
        store.release(claim)
    answered = plan(only_apps=[apps["plain"]], user_actions=True, rerun_all=True)
    assert answered.stats.selected_by == {"answered since the stop": 1}


# --- options and ids ---------------------------------------------------------------------------


def test_retry_options_take_the_recorded_run_options_unless_given(paths):
    recorded = BatchRunOptions(candidate_id="c1", workers=3, per_job_timeout_s=120,
                               retry_retryable=2, sync_closed=False, ai_routing=True,
                               env_file="/private/x.env", writer_model=WRITER)
    options = retry_options(paths, retry_of="b1", batch_id="r1", recorded=recorded,
                            cli=CLI_DEFAULTS, explicit=set())
    assert (options.candidate_id, options.workers, options.per_job_timeout_s,
            options.retry_retryable, options.sync_closed, options.ai_routing,
            options.env_file, options.writer_model) == (
        "c1", 3, 120.0, 2, False, True, Path("/private/x.env"), WRITER)
    assert (options.retry_of, options.batch_id, options.max_prepared, options.headless) == \
        ("b1", "r1", None, True)
    given = retry_options(paths, retry_of="b1", batch_id="r1", recorded=recorded,
                          cli=CLI_DEFAULTS | {"workers": 1}, explicit={"workers"},
                          max_prepared=5)
    assert (given.workers, given.per_job_timeout_s, given.max_prepared) == (1, 120.0, 5)
    old = retry_options(paths, retry_of="b1", batch_id="r1", recorded=None,
                        cli=CLI_DEFAULTS | {"workers": 2}, explicit=set())
    assert (old.workers, old.ai_routing, old.candidate_id) == (2, False, "default")
    with pytest.raises(ValueError, match="batch of its own"):
        retry_options(paths, retry_of="b1", batch_id="b1", recorded=recorded,
                      cli=CLI_DEFAULTS, explicit=set())
    with pytest.raises(ValueError, match="one owned Chrome session"):
        retry_options(paths, retry_of="b1", batch_id="r1", recorded=None,
                      cli=CLI_DEFAULTS | {"browser": "opencli", "workers": 2}, explicit=set())


def test_default_retry_id():
    now = datetime(2026, 9, 24, 21, 5, 7, tzinfo=UTC)
    assert default_retry_id("batch-20260924T090000Z", now) == \
        "batch-20260924T090000Z-retry-20260924T210507Z"
    assert default_retry_id("batch-20260924T090000Z-retry-20260924T100000Z", now) == \
        "batch-20260924T090000Z-retry-20260924T210507Z"
    assert default_retry_id("night", now) == "night-retry-20260924T210507Z"


def test_resume_argv_and_busy_outcomes(paths):
    options = BatchOptions(paths=paths, candidate_id="default", batch_id="r", command=["imx"],
                           retry_of="b1")
    assert options.resume_argv("app_1") == ["imx", "resume", "app_1", "--json", "--headless"]
    resume_row = BatchRow(listing_id="l", application_url=f"{ORIGIN}/x", application_id="app_1")
    assert options.job_argv(resume_row) == options.resume_argv("app_1")
    assert options.job_argv(resume_row.model_copy(update={"application_id": None})) == \
        options.argv(f"{ORIGIN}/x")
    # A job that did not run because another run held the application is retryable,
    # whatever stored state it reports (a timed-out run's claim lapses in minutes).
    for state in (S.NEEDS_INPUT, S.FAILED_RETRYABLE, S.INSPECTING):
        for message in ("Another run is working on this application.",
                        "another run is using the browser profile"):
            outcome = ApplyOutcome(application_id="app_x", state=state, message=message)
            assert classify(outcome) == "failed_retryable"


# --- CLI ---------------------------------------------------------------------------------------


def test_prepare_batch_retry_parser(tmp_path, capsys):
    parser = build_parser()
    args = parser.parse_args(["prepare-batch", "--retry", "b1", "--outcomes",
                              "needs_input,error,needs_input", "--include-explicit"])
    assert (args.retry, args.inventory, args.outcomes, args.include_explicit) == \
        ("b1", None, ["needs_input", "error"], True)
    assert args.user_actions is False
    assert parser.parse_args(["prepare-batch", "--retry", "b1", "--user-actions"]).user_actions
    # --user-actions selects among a retried batch's holds; an inventory run has none.
    assert main(["--home", str(tmp_path / "home"), "prepare-batch", "--inventory",
                 str(tmp_path / "none.json"), "--user-actions"]) == EXIT_USAGE
    assert "--user-actions apply to --retry" in capsys.readouterr().err
    for bad in (["--retry", "b1", "--inventory", "x.json"], ["--retry", "b1", "--outcomes", "prepared"],
                ["--retry", "b1", "--outcomes", "closed"], ["--retry", "b1", "--outcomes", "bogus"],
                ["--retry", "b1", "--outcomes", ","], ["--retry", "b1", "--outcomes",
                                                       "already_recorded"], []):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["prepare-batch", *bad])
        assert exc.value.code == EXIT_USAGE


@pytest.mark.slow
def test_prepare_batch_retry_command(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(batch_module, "default_command", lambda: [sys.executable, str(fake)])
    home = tmp_path / "home"
    local = LocalPaths.from_env({}, home=home)
    inventory = tmp_path / "inv.json"
    inventory.write_text(json.dumps([
        {"listing_id": f"lst_{kind}", "company": "Brambleway", "title": kind,
         "source_application_url": f"{ORIGIN}/{kind}", "backend": "mock", "status": "resolved"}
        for kind in ("held", "flaky", "prepared", "explicit")]))
    base = ["--home", str(home), "prepare-batch"]
    assert main([*base, "--inventory", str(inventory), "--workers", "2", "--batch-id", "b1",
                 "--json"]) == EXIT_OK
    capsys.readouterr()
    log_start = len(calls(tmp_path))

    monkeypatch.setenv("FAKE_RESUME", json.dumps({"held": "notice", "flaky": "prepared"}))
    assert main([*base, "--retry", "b1", "--batch-id", "r1", "--all", "--json"]) == EXIT_OK
    out, err = capsys.readouterr()
    summary = json.loads(out)
    assert summary["batch_id"] == "r1" and summary["totals"] == {"prepared": 1,
                                                                 "needs_input": 1}
    retry_stats = summary["retry"]
    assert (retry_stats["retry_of"], retry_stats["selected"], retry_stats["holds_before"],
            retry_stats["holds_cleared"], retry_stats["holds_open"]) == ("b1", 2, 2, 1, 1)
    assert retry_stats["skipped"] == {"prepared": 1, "explicit answers only": 1}
    assert summary["run_options"]["workers"] == 2 and retry_stats["rerun_all"]  # from b1
    assert "retry of b1, 2 of 4 listing(s) selected" in err and "notice period" not in err
    workers = home / "browser-workers"
    assert {c["env"]["IMX_BROWSER_DIR"] for c in calls(tmp_path)[log_start:]} <= {
        str(workers / "w0"), str(workers / "w1")}
    assert all(c["argv"][0] == "resume" for c in calls(tmp_path)[log_start:])

    # A flag given again replaces the recorded one (--workers 1 here), even at its default.
    log_start = len(calls(tmp_path))
    assert main([*base, "--retry", "b1", "--batch-id", "r2", "--workers", "1",
                 "--include-explicit", "--all"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "# Batch r2" in out and "## Retry of b1" in out
    assert {c["env"]["IMX_BROWSER_DIR"] for c in calls(tmp_path)[log_start:]} == {
        str(workers / "w0")}
    r2 = read_summary(local, "r2")
    assert r2 is not None and r2.run_options is not None and r2.run_options.workers == 1
    assert r2.retry is not None and r2.retry.selected == 2 and r2.retry.include_explicit

    # Nothing left to select is not an error, and the default id is derived from the batch.
    assert main([*base, "--retry", "b1", "--outcomes", "failed_retryable", "--json"]) == EXIT_OK
    nothing = json.loads(capsys.readouterr().out)
    assert nothing["launched"] == 0 and nothing["batch_id"].startswith("b1-retry-")
    assert nothing["retry"]["skipped"] == {"prepared": 2, "not selected (needs_input)": 2}
    # Without --all, held applications with nothing answered since they stopped wait.
    assert main([*base, "--retry", "b1", "--batch-id", "r3", "--json"]) == EXIT_OK
    idle = json.loads(capsys.readouterr().out)
    assert idle["launched"] == 0 and idle["retry"]["skipped"] == {
        "prepared": 2, "nothing answered since the stop": 2}

    # Usage and missing batches.
    assert main([*base, "--retry", "nope"]) == EXIT_ERROR
    assert "no ledger for batch 'nope'" in capsys.readouterr().err
    for bad in (["--retry", "../x"], ["--retry", "b1", "--batch-id", "b1"],
                ["--retry", "b1", "--statuses", "blocked"],
                ["--retry", "b1", "--include-existing"],
                ["--inventory", str(inventory), "--outcomes", "needs_input"],
                ["--inventory", str(inventory), "--include-explicit"],
                ["--inventory", str(inventory), "--all"]):
        assert main([*base, *bad]) == EXIT_USAGE, bad
        assert capsys.readouterr().err.strip()


@pytest.mark.slow
def test_retry_of_a_batch_without_recorded_run_options_uses_the_given_flags(
        fake, paths, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(batch_module, "default_command", lambda: [sys.executable, str(fake)])
    first_batch(fake, paths, ["held", "flaky"])
    summary_path = paths.home / "batches" / "b1" / "summary.json"
    data = json.loads(summary_path.read_text())
    del data["run_options"]  # as written before run options were recorded
    summary_path.write_text(json.dumps(data))
    assert (read_summary(paths, "b1") or pytest.fail("unreadable")).run_options is None
    log_start = len(calls(tmp_path))
    assert main(["--home", str(paths.home), "prepare-batch", "--retry", "b1", "--workers", "2",
                 "--batch-id", "r1", "--all", "--json"]) == EXIT_OK
    out, err = capsys.readouterr()
    assert "recorded no run options" in err
    assert json.loads(out)["run_options"]["workers"] == 2
    assert len(calls(tmp_path)) - log_start == 2


# --- round 4: sheet answers count as answered; --only-app --------------------------------------


def test_sheet_answers_count_as_answered_for_the_default_retry(paths):
    from interviewmaxxing_cli.retry import batch_applications
    from interviewmaxxing_cli.sheet import apply_sheet, build_sheet, read_sheet, write_sheet

    candidates = write_profile(paths)

    def asks(name: str, *labels: str) -> str:
        return stored(paths, name, "held", [
            question(f"{ORIGIN}/{name}", f"q{n}", label, MissingReason.NO_ANSWER)
            for n, label in enumerate(labels)])

    held = {"one": asks("one", NOTICE, START), "two": asks("two", NOTICE, START),
            "three": asks("three", NOTICE, START), "four": asks("four", START)}
    later = asks("later", NOTICE)
    write_ledger(paths, *(line(f"l-{name}", "needs_input", name, app)
                          for name, app in held.items()))
    append_ledger(paths.home / "batches" / "b2" / "ledger.jsonl",
                  line("l-later", "needs_input", "later", later).model_copy(
                      update={"batch_id": "b2"}))
    assert plan_retry(paths, "b1", candidate_id="default").items == ()  # nothing answered yet

    # The sheet of batch b1 answers the notice period (reuse global) for "one" and "two";
    # the person took "three" out of the entry; the start date stays open everywhere.
    sheet_path = paths.home / "sheet.json"
    write_sheet(build_sheet(paths, "default", batches=["b1"],
                            applications=batch_applications(paths, ["b1"], candidate_id="default")),
                sheet_path)
    data = json.loads(sheet_path.read_text())
    [entry] = [q for q in data["questions"] if q["question"] == NOTICE]
    assert set(entry["fields"]) == {held["one"], held["two"], held["three"]}  # not b2's "later"
    del entry["fields"][held["three"]]
    entry["answer"] = "Two weeks"
    sheet_path.write_text(json.dumps(data))
    result = apply_sheet(paths, read_sheet(sheet_path), owner="test")
    assert (result.applied, result.applications) == (2, 2)
    assert (NOTICE, AnswerScope.GLOBAL) in {(s.question, s.scope)
                                            for s in candidates.load("default").saved_answers}

    # Without --all: "one" and "two" (their own answers) and "three" (the global answer for
    # its wording, saved after it stopped) count as answered; "four" does not.
    plan = plan_retry(paths, "b1", candidate_id="default")
    assert [i.row.listing_id for i in plan.items] == ["l-one", "l-two", "l-three"]
    assert plan.stats.selected_by == {"answered since the stop": 3}
    assert plan.stats.skipped == {"nothing answered since the stop": 1}
    # The application of another batch that asks the same wording counts as answered too.
    later_plan = plan_retry(paths, "b2", candidate_id="default")
    assert [i.row.listing_id for i in later_plan.items] == ["l-later"]
    assert later_plan.stats.selected_by == {"answered since the stop": 1}
    text = render_summary_markdown(batch_module.summarize(
        "r", [], started_at=datetime.now(UTC), finished_at=datetime.now(UTC), rows=0, launched=0,
        skipped_settled=0, skipped_invalid_url=0, ledger_path=Path("ledger.jsonl"),
        retry=plan.stats))
    assert "- selected by: answered since the stop (3)" in text


def test_only_app_selects_just_the_named_applications(paths):
    url = f"{ORIGIN}/x"
    x = stored(paths, "x", "held", [question(url, "q", START, MissingReason.NO_ANSWER)])
    y = stored(paths, "y", "held", [question(url, "q", START, MissingReason.NO_ANSWER)])
    failing = stored(paths, "failing", "failed")
    ready = stored(paths, "ready", "prepared")
    explicit = stored(paths, "explicit", "held", explicit_holds(f"{ORIGIN}/explicit"))
    halfway = stored(paths, "halfway", "inspecting")
    write_ledger(paths, line("l-x", "needs_input", "x", x), line("l-y", "needs_input", "y", y),
                 line("l-failing", "failed_retryable", "failing", failing),
                 line("l-ready", "prepared", "ready", ready),
                 line("l-explicit", "needs_input", "explicit", explicit),
                 line("l-halfway", "error", "halfway", None))  # the store has it by URL

    def plan(*apps: str, **kwargs: Any) -> Any:
        return plan_retry(paths, "b1", candidate_id="default", only_apps=list(apps), **kwargs)

    one = plan(x)
    assert [i.row.listing_id for i in one.items] == ["l-x"]  # nothing answered: named instead
    assert (one.stats.only_apps, one.stats.considered, one.stats.selected_by, one.stats.skipped) \
        == ([x], 1, {"--only-app": 1}, {})
    two = plan(x, failing, halfway, x)
    assert [i.row.listing_id for i in two.items] == ["l-x", "l-failing", "l-halfway"]
    assert two.stats.selected_by == {"--only-app": 1, "failed_retryable": 1, "unknown": 1}
    assert two.stats.only_apps == [x, failing, halfway]
    assert plan(ready).stats.skipped == {"prepared": 1} and plan(ready).items == ()
    assert plan(explicit).stats.skipped == {"explicit answers only": 1}
    assert [i.row.listing_id for i in plan(explicit, include_explicit=True).items] == ["l-explicit"]
    assert plan(x, outcomes=["failed_retryable"]).stats.skipped == {
        "not selected (needs_input)": 1}
    assert plan(x, rerun_all=True).stats.selected_by == {"--only-app": 1}  # the named reason
    with pytest.raises(ValueError, match=r"not an application of batch 'b1'.*app_fictional_nope"):
        plan(x, "app_fictional_nope")
    # The ledger names it, but it is not this candidate's: listed as not found, as without
    # --only-app.
    theirs = plan_retry(paths, "b1", candidate_id="someone-else", only_apps=[x])
    assert theirs.items == () and theirs.stats.skipped == {"application not found": 1}


@pytest.mark.slow
def test_prepare_batch_retry_only_app_command(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(batch_module, "default_command", lambda: [sys.executable, str(fake)])
    home = tmp_path / "home"
    local = LocalPaths.from_env({}, home=home)
    first_batch(fake, local, ["held", "flaky", "prepared", "notice"])
    held_app = read_ledger(home / "batches" / "b1" / "ledger.jsonl")[0].application_id
    assert held_app is not None
    log_start = len(calls(tmp_path))
    monkeypatch.setenv("FAKE_RESUME", json.dumps({"held": "prepared"}))
    base = ["--home", str(home), "prepare-batch"]
    assert main([*base, "--retry", "b1", "--only-app", held_app, "--batch-id", "r1",
                 "--json"]) == EXIT_OK
    out, err = capsys.readouterr()
    summary = json.loads(out)
    assert summary["totals"] == {"prepared": 1}  # the failed one was not named: not run
    assert summary["retry"]["only_apps"] == [held_app]
    assert summary["retry"]["selected_by"] == {"--only-app": 1}
    assert "1 of 1 listing(s) selected (by: --only-app (1))" in err
    assert [c["argv"][:2] for c in calls(tmp_path)[log_start:]] == [["resume", held_app]]
    for bad in (["--retry", "b1", "--only-app", "app_fictional_nope"],
                ["--inventory", str(tmp_path / "inv.json"), "--only-app", held_app],
                ["--retry", "b1", "--exclude-batches", "b1"]):
        assert main([*base, *bad]) == EXIT_USAGE, bad
        assert capsys.readouterr().err.strip()
    with pytest.raises(SystemExit) as exc:
        main([*base, "--retry", "b1", "--only-app", ","])
    assert exc.value.code == EXIT_USAGE
