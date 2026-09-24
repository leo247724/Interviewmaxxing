"""The batch harness findings of the independent review (WP9 addendum), one test each:
directory modes (M9), one state-database connection (L7), rows sharing a URL (L12),
runs stopped mid-fill (L13), unreadable ledger lines (L14), Closed moves only for
closed jobs (L15), lookups outside the worker lock (L16), timeout escalation (L17) and
escaped Markdown cells (L18). A store-backed fake ``interviewmaxxing`` stands in for the
CLI; everything is fictional and local."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import stat
import sys
import textwrap
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_cli import batch as batch_module
from interviewmaxxing_cli.batch import (
    BatchOptions,
    BatchRow,
    LedgerEntry,
    append_ledger,
    build_report,
    plan,
    read_ledger_lines,
    render_report_markdown,
    render_summary_markdown,
    run_batch,
    summarize,
)
from interviewmaxxing_cli.main import EXIT_OK, main
from interviewmaxxing_cli.retry import plan_retry
from interviewmaxxing_core import ApplicationState, ApplicationStore, LocalPaths
from interviewmaxxing_pipeline import NewPipelineItem, PipelineStore, TrackingFields

S = ApplicationState
ORIGIN = "https://jobs.fictional.example/brambleway"
CLOSED = "The job is no longer accepting applications."

FAKE_CLI = textwrap.dedent('''\
    """Fake ``interviewmaxxing apply URL`` over the real store; the URL's last path
    segment is the kind. Logs every call (with its start time) to FAKE_LOG."""
    import json, os, signal, sys, time

    from interviewmaxxing_core import ApplicationState as S, ApplicationStore, LocalPaths

    assert sys.argv[1] == "apply" and "--json" in sys.argv and "--headless" in sys.argv
    url = sys.argv[2]
    kind = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    with open(os.environ["FAKE_LOG"], "a") as fh:
        fh.write(json.dumps({"url": url, "pid": os.getpid(), "t": time.monotonic()}) + "\\n")
    if kind == "stubborn":  # ignores SIGTERM; only SIGKILL stops it
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(30)
        sys.exit(0)
    time.sleep(float(os.environ.get("FAKE_SLEEP", "0.05")))
    candidate = sys.argv[sys.argv.index("--candidate") + 1]
    with ApplicationStore.open(LocalPaths.from_env().state_db) as store:
        app = store.record_request(candidate, url).application
        claim = store.claim(app.id, "hardening-fake-cli")
        if store.get_application(app.id).state is not S.INSPECTING:
            store.transition(claim, S.INSPECTING)
        store.append_event(claim, "provider.budget", {"calls": 1, "known_cost_usd": 0.001,
                                                      "unknown_cost_calls": 0})
        if kind == "withdrawn":
            state, message = S.FAILED_PERMANENT, "The application cannot be completed."
            store.transition(claim, state, failure_reason=message)
        elif kind == "closed":
            state, message = S.FAILED_PERMANENT, @CLOSED@
            store.transition(claim, state, failure_reason=message)
        else:
            store.append_event(claim, "preparation.ready", {"submitted": False})
            state = S.NEEDS_INPUT
            message = ("Prepared to the final review step. Nothing was submitted. Submission "
                       "remains disabled when this application is resumed.")
            store.transition(claim, state, metadata={"missing_inputs": [], "reason": "prepared"})
        store.release(claim)
    print(json.dumps({"application_id": app.id, "state": state.value, "receipt": None,
                      "missing_inputs": [], "message": message}))
    sys.exit(3)
''').replace("@CLOSED@", repr(CLOSED))


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "hardening_fake_cli.py"
    script.write_text(FAKE_CLI)
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    return script


@pytest.fixture
def paths(tmp_path: Path) -> LocalPaths:
    return LocalPaths.from_env({}, home=tmp_path / "home")


def log(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "fake.log"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def row(listing: str, path: str, *, pipeline_id: str | None = None,
        application_id: str | None = None) -> BatchRow:
    return BatchRow(listing_id=listing, pipeline_id=pipeline_id, company="Brambleway",
                    title=listing, application_url=f"{ORIGIN}/{path}", backend="mock",
                    status="resolved", application_id=application_id)


def options(fake: Path, paths: LocalPaths, **settings: Any) -> BatchOptions:
    return BatchOptions(**({"paths": paths, "candidate_id": "default", "batch_id": "b1",
                            "command": [sys.executable, str(fake)]} | settings))


def run(opts: BatchOptions, rows: list[BatchRow]) -> tuple[Any, list[LedgerEntry]]:
    entries: list[LedgerEntry] = []
    summary = asyncio.run(run_batch(opts, rows, on_entry=entries.append))
    return summary, entries


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- M9: directory modes -----------------------------------------------------------------------


@pytest.mark.slow
def test_m9_prepare_batch_creates_every_directory_owner_only(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(batch_module, "default_command", lambda: [sys.executable, str(fake)])
    home = tmp_path / "fresh-home"
    inventory = tmp_path / "inv.json"
    inventory.write_text(json.dumps([{"listing_id": "l1", "source_application_url":
                                      f"{ORIGIN}/one/prepared", "status": "resolved"}]))
    assert not home.exists()
    assert main(["--home", str(home), "prepare-batch", "--inventory", str(inventory),
                 "--batch-id", "m9", "--json"]) == EXIT_OK
    capsys.readouterr()
    for directory in (home, home / "batches", home / "batches" / "m9", home / "browser-workers",
                      home / "browser-workers" / "w0", home / "state"):
        assert mode(directory) == 0o700, directory
    assert mode(home / "batches" / "m9" / "ledger.jsonl") == 0o600
    assert mode(home / "batches" / "m9" / "summary.json") == 0o600


def test_m9_private_dirs_sets_every_missing_level(tmp_path):
    target = tmp_path / "a" / "b" / "c"
    (tmp_path / "a").mkdir(mode=0o755)
    os.chmod(tmp_path / "a", 0o755)
    assert batch_module.private_dirs(target) == target
    assert (mode(tmp_path / "a"), mode(tmp_path / "a" / "b"), mode(target)) == (0o755, 0o700,
                                                                               0o700)
    assert batch_module.private_dirs(target) == target  # existing: left as they are


# --- L7: one state-database connection ----------------------------------------------------------


@pytest.mark.slow
def test_l7_a_batch_opens_the_state_database_once(fake, paths, monkeypatch):
    """Before: every finished job opened the store for its provider cost, every row for
    the existing-application check and every card for its link, each open running the
    store's schema check (a write transaction) while other jobs wrote."""
    opened: list[str] = []
    real_open = ApplicationStore.open.__func__  # type: ignore[attr-defined]

    def counting_open(cls: type[ApplicationStore], path: Any, **kwargs: Any) -> ApplicationStore:
        opened.append(str(path))
        return real_open(cls, path, **kwargs)

    with PipelineStore.from_paths(paths) as pipeline:
        cards = {n: pipeline.create_item("default", NewPipelineItem(
            tracking=TrackingFields(company="Brambleway", role=f"Role {n}"), lane="saved",
            listing_id=f"l{n}", application_url=f"{ORIGIN}/{n}/prepared")) for n in range(4)}
    monkeypatch.setattr(ApplicationStore, "open", classmethod(counting_open))
    summary, entries = run(options(fake, paths, workers=2),
                           [row(f"l{n}", f"{n}/prepared", pipeline_id=cards[n].id)
                            for n in range(4)])
    assert summary.totals == {"prepared": 4} and summary.pipeline_linked == 4
    assert all(e.provider_cost_usd == 0.001 and e.provider_calls == 1 for e in entries)
    assert opened == [str(paths.state_db)]

    # A rerun records nothing new, and a new batch over the same rows (already recorded,
    # linked again idempotently) still opens it once.
    opened.clear()
    backfill, entries = run(options(fake, paths, batch_id="b2", workers=2),
                            [row(f"l{n}", f"{n}/prepared", pipeline_id=cards[n].id)
                             for n in range(4)])
    assert backfill.totals == {"already_recorded": 4} and all(e.linked for e in entries)
    assert opened == [str(paths.state_db)]


def test_l7_the_reader_never_creates_the_database(paths):
    reader = batch_module.StateReader(paths)
    try:
        assert reader.call(lambda store: store) is None
        assert asyncio.run(reader.run(lambda store: store is None)) is True
    finally:
        reader.close()
    assert not paths.state_db.exists() and reader.opens == 0


# --- L12: rows that share a URL ------------------------------------------------------------------


@pytest.mark.slow
def test_l12_rows_with_one_normalized_url_do_not_run_together(fake, paths, tmp_path):
    rows = [row("l-a", "same/prepared"), row("l-b", "same/prepared/"),
            row("l-c", "other/prepared")]
    first, entries = run(options(fake, paths, workers=3), rows)
    assert [e.listing_id for e in entries if e.listing_id != "l-c"] == ["l-a"]
    assert (first.launched, first.skipped_same_url) == (2, 1)
    assert "same URL as another row: 1" in render_summary_markdown(first)
    assert sum(1 for x in log(tmp_path) if "/same/" in x["url"]) == 1

    # The next run of the batch records the other listing against the same application.
    again, entries = run(options(fake, paths, workers=3), rows)
    assert [(e.listing_id, e.outcome) for e in entries] == [("l-b", "already_recorded")]
    assert again.skipped_same_url == 0 and again.totals == {"prepared": 2,
                                                            "already_recorded": 1}


def test_l12_plan_keeps_one_row_per_url_and_application(paths):
    opts = BatchOptions(paths=paths, candidate_id="default", batch_id="b1")
    rows = [row("l1", "x/one?utm_source=a"), row("l2", "x/one/"),
            row("l3", "two", application_id="app_same"),
            row("l4", "alias-of-two", application_id="app_same"), row("l5", "three")]
    launch, settled, same = plan(opts, rows, [])
    assert [r.listing_id for r, _ in launch] == ["l1", "l3", "l5"]
    assert (settled, same) == (0, 2)


# --- L13: runs stopped mid-fill ---------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("left_in", [S.PACKET_READY, S.FILLING])
def test_l13_an_application_stopped_mid_fill_is_run_again(fake, paths, left_in):
    paths.ensure()
    url = f"{ORIGIN}/midway/prepared"
    with ApplicationStore.open(paths.state_db) as store:
        app = store.record_request("default", url).application
        claim = store.claim(app.id, "test")
        store.transition(claim, S.INSPECTING)
        store.transition(claim, S.PACKET_READY)
        if left_in is S.FILLING:
            store.transition(claim, S.FILLING)
        store.release(claim)
    summary, [entry] = run(options(fake, paths), [row("l-mid", "midway/prepared")])
    assert (entry.outcome, entry.application_id) == ("prepared", app.id)
    assert summary.launched == 1 and "already_recorded" not in summary.totals
    assert {S.PACKET_READY, S.FILLING} <= batch_module._LAUNCH_STATES


# --- L14: unreadable ledger lines ---------------------------------------------------------------


def ok_line(listing: str, outcome: str = "prepared") -> LedgerEntry:
    now = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
    return LedgerEntry(batch_id="b1", listing_id=listing, application_url=f"{ORIGIN}/{listing}",
                       attempt=1, outcome=outcome, state=S.NEEDS_INPUT, started_at=now,
                       finished_at=now, duration_s=1.0)


@pytest.mark.slow
def test_l14_unreadable_ledger_lines_are_counted_and_shown(fake, paths):
    ledger = paths.home / "batches" / "b1" / "ledger.jsonl"
    append_ledger(ledger, ok_line("l-ok"))
    newer = ok_line("l-newer").model_dump(mode="json") | {"field_from_the_future": 1}
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(newer) + "\n")
        fh.write('{"batch_id": "b1", "listing_id": "l-cut')  # a crash mid-write
    entries, ignored = read_ledger_lines(ledger)
    assert [e.listing_id for e in entries] == ["l-ok"] and ignored == 2

    summary, ran = run(options(fake, paths), [row("l-ok", "l-ok"), row("l-newer", "newer/prepared")])
    assert [e.listing_id for e in ran] == ["l-newer"]  # its line was unreadable: run again
    assert (summary.ledger_lines_ignored, summary.skipped_settled) == (2, 1)
    assert "- unreadable ledger lines ignored: 2" in render_summary_markdown(summary)
    # The run's own line did not join the cut one: it is readable after it.
    entries, ignored = read_ledger_lines(ledger)
    assert [e.listing_id for e in entries] == ["l-ok", "l-newer"] and ignored == 2
    report = build_report(paths, ["b1"])
    assert report.ledger_lines_ignored == 2 and report.rows == 2
    assert "- unreadable ledger lines ignored: 2" in render_report_markdown(report)
    assert plan_retry(paths, "b1", candidate_id="default").stats.ledger_lines_ignored == 2


# --- L15: Closed moves only for closed jobs -----------------------------------------------------


@pytest.mark.slow
def test_l15_only_the_closed_wording_moves_a_card(fake, paths):
    with PipelineStore.from_paths(paths) as pipeline:
        withdrawn = pipeline.create_item("default", NewPipelineItem(
            tracking=TrackingFields(company="Brambleway", role="Withdrawn"), lane="saved",
            listing_id="l-w", application_url=f"{ORIGIN}/w/withdrawn"))
        closed = pipeline.create_item("default", NewPipelineItem(
            tracking=TrackingFields(company="Brambleway", role="Closed"), lane="saved",
            listing_id="l-c", application_url=f"{ORIGIN}/c/closed"))
    summary, entries = run(options(fake, paths), [
        row("l-w", "w/withdrawn", pipeline_id=withdrawn.id),
        row("l-c", "c/closed", pipeline_id=closed.id)])
    by_id = {e.listing_id: e for e in entries}
    assert by_id["l-w"].outcome == by_id["l-c"].outcome == "closed"  # both FAILED_PERMANENT
    assert (by_id["l-w"].linked, by_id["l-w"].closed_synced) == (True, None)
    assert (by_id["l-c"].linked, by_id["l-c"].closed_synced) == (True, True)
    with PipelineStore.from_paths(paths) as pipeline:
        assert pipeline.get_item("default", withdrawn.id).lane == "saved"
        assert pipeline.get_item("default", closed.id).lane == "closed"
    assert summary.pipeline_closed == 1 and summary.pipeline_problems == []


# --- L16: lookups outside the worker lock ----------------------------------------------------------


@pytest.mark.slow
def test_l16_slow_card_bookkeeping_does_not_stall_other_launches(fake, paths, tmp_path,
                                                                 monkeypatch):
    """A card sync that waits on a locked pipeline database (here 1.5 s) for an already
    recorded row used to run under the workers' lock, so no other job could start."""
    paths.ensure()
    with ApplicationStore.open(paths.state_db) as store:  # l-slow is already recorded
        app = store.record_request("default", f"{ORIGIN}/slow/prepared").application
        claim = store.claim(app.id, "test")
        store.transition(claim, S.INSPECTING)
        store.transition(claim, S.NEEDS_INPUT, metadata={"missing_inputs": [], "reason": "x"})
        store.release(claim)
    real_sync = batch_module.sync_pipeline_card

    def slow_sync(paths_: LocalPaths, candidate: str, entry: LedgerEntry,
                  **kwargs: Any) -> LedgerEntry:
        if entry.pipeline_id == "pipe_locked":
            time.sleep(1.5)
            return entry.model_copy(update={"linked": False, "link_reason": "link error: busy"})
        return real_sync(paths_, candidate, entry, **kwargs)

    monkeypatch.setattr(batch_module, "sync_pipeline_card", slow_sync)
    started = time.monotonic()
    summary, entries = run(options(fake, paths, workers=2), [
        row("l-slow", "slow/prepared", pipeline_id="pipe_locked"),
        row("l-b", "b/prepared"), row("l-c", "c/prepared")])
    assert [e.listing_id for e in entries][-1] == "l-slow"
    assert {e.listing_id: e.outcome for e in entries} == {
        "l-slow": "already_recorded", "l-b": "prepared", "l-c": "prepared"}
    first_job = min(x["t"] for x in log(tmp_path))
    assert first_job - started < 1.2  # launched while the slow sync was still waiting
    assert summary.totals == {"already_recorded": 1, "prepared": 2}


# --- L17: timeout escalation ---------------------------------------------------------------------


@pytest.mark.slow
def test_l17_a_job_that_ignores_sigterm_is_killed_and_the_ledger_says_so(fake, paths,
                                                                         monkeypatch):
    monkeypatch.setattr(batch_module, "TERM_GRACE_S", 0.3)
    _, [entry] = run(options(fake, paths, per_job_timeout_s=1.0), [row("l-s", "s/stubborn")])
    assert entry.outcome == "error"
    assert entry.message == ("timed out after 1 s; the run ignored SIGTERM for 0.3 s and was "
                             "killed (SIGKILL); nothing was submitted")
    assert entry.exit_code == -signal.SIGKILL


def test_l17_a_job_that_outlives_both_signals_is_recorded_as_possibly_running(monkeypatch):
    monkeypatch.setattr(batch_module, "TERM_GRACE_S", 0.2)
    real_killpg = os.killpg
    sent: list[int] = []

    async def scenario() -> tuple[str, int]:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(30)", start_new_session=True)
        monkeypatch.setattr(os, "killpg", lambda pid, sig: sent.append(sig))  # lost signals
        try:
            return await batch_module._terminate(proc), proc.pid
        finally:
            monkeypatch.setattr(os, "killpg", real_killpg)
            real_killpg(proc.pid, signal.SIGKILL)
            await proc.wait()

    ending, pid = asyncio.run(scenario())
    assert ending == "running" and sent == [signal.SIGTERM, signal.SIGKILL]
    assert batch_module._timeout_message(900, ending, pid) == (
        f"timed out after 900 s; the run did not exit after SIGTERM and SIGKILL (process group "
        f"{pid} may still be running); nothing was submitted")
    assert batch_module._timeout_message(5, "stopped", pid) == (
        "timed out after 5 s; the run was stopped (SIGTERM); nothing was submitted")


# --- L18: Markdown cells ---------------------------------------------------------------------------

_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def test_l18_table_cells_escape_pipes(paths, tmp_path):
    now = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
    question = "Salary | bonus expectations (base|variable)?"
    entry = LedgerEntry(
        batch_id="b1", listing_id="l1", application_url=f"{ORIGIN}/x", backend="odd|backend",
        attempt=1, outcome="needs_input", state=S.NEEDS_INPUT, missing_reasons=["NO_ANSWER"],
        missing_labels=[question], started_at=now - timedelta(seconds=5), finished_at=now,
        duration_s=5.0)
    summary = summarize("b1", [entry], started_at=now, finished_at=now, rows=1, launched=1,
                        skipped_settled=0, skipped_invalid_url=0,
                        ledger_path=tmp_path / "ledger.jsonl")
    text = render_summary_markdown(summary)
    assert r"| Salary \| bonus expectations (base\|variable)? | 1 |" in text
    assert r"| odd\|backend | 1 |" in text
    for line in text.splitlines():
        if line.startswith("| Salary") or line.startswith("| odd"):
            assert len(_UNESCAPED_PIPE.findall(line)) == 3, line

    append_ledger(paths.home / "batches" / "b1" / "ledger.jsonl", entry)
    report = render_report_markdown(build_report(paths, ["b1"]))
    table_rows = [x for x in report.splitlines() if "odd" in x and x.startswith("|")]
    assert len(table_rows) >= 3 and all(r"odd\|backend" in x for x in table_rows)
    assert r"Salary \| bonus expectations (base\|variable)?" in report
    for text_ in (text, report):  # every row of every table has its header's column count
        tables = re.split(r"\n(?!\|)", text_)
        for table in (t for t in tables if t.startswith("|")):
            counts = {len(_UNESCAPED_PIPE.findall(x)) for x in table.splitlines()}
            assert len(counts) == 1, table
