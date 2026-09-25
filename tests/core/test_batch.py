"""The bulk preparation harness, offline: a fake ``apply`` command stands in for the
CLI (no browser), so classification, bounded concurrency, per-slot browser
directories, the resumable ledger, timeouts, the ``max_prepared`` bound, the
already-recorded skip and the summary are exercised without Playwright."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sqlite3
import stat
import sys
import textwrap
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_cli import batch as batch_module
from interviewmaxxing_cli.batch import (
    BatchOptions,
    BatchRow,
    LedgerEntry,
    classify,
    format_entry,
    load_inventory,
    read_inventory,
    read_ledger,
    render_summary_markdown,
    run_batch,
    summarize,
)
from interviewmaxxing_cli.main import EXIT_ERROR, EXIT_OK, EXIT_USAGE, build_parser, main
from interviewmaxxing_core import (
    Application,
    ApplicationState,
    ApplicationStore,
    ApplyOutcome,
    IdentityEvidenceKind,
    JobIdentityObservation,
    LocalPaths,
)
from interviewmaxxing_pipeline import (
    BoardLane,
    BoardLanes,
    NewPipelineItem,
    PipelineItem,
    PipelineStore,
    PipelineUpdate,
    RevisionConflict,
    TrackingFields,
    default_pipeline_db,
)

S = ApplicationState
ORIGIN = "https://jobs.fictional.example/brambleway"

FAKE_CLI = textwrap.dedent('''\
    """Fake ``interviewmaxxing apply``: answers by URL path, logs its environment."""
    import json, os, sys, time
    from pathlib import Path

    assert sys.argv[1] == "apply" and "--json" in sys.argv and "--headless" in sys.argv
    url = sys.argv[2]
    kind = url.rsplit("/", 1)[-1].split("?")[0]
    app_id = "app_" + kind
    log = Path(os.environ["FAKE_LOG"])
    counter = Path(os.environ["FAKE_COUNTER"])

    def note(phase):
        with log.open("a") as fh:
            fh.write(json.dumps({"phase": phase, "t": time.monotonic(), "pid": os.getpid(),
                                 "kind": kind, "url": url, "argv": sys.argv[1:],
                                 "env": {k: v for k, v in os.environ.items() if k.startswith("IMX_")}})
                     + "\\n")

    def outcome(state, message, missing=()):
        return json.dumps({"application_id": app_id, "state": state, "receipt": None,
                           "missing_inputs": list(missing), "message": message})

    NEEDS = {"field_id": "notice_period", "form_url": url, "form_step": 0, "field_fingerprint": "ab" * 32,
             "label": "What is your notice period?", "reason": "NO_ANSWER", "prompt": "Please answer.",
             "required": True}
    ACTION = {"field_id": None, "label": "Sign in", "reason": "USER_ACTION", "prompt": "Sign in first."}

    note("start")
    time.sleep(float(os.environ.get("FAKE_SLEEP", "0.3")))
    if kind == "hang":
        time.sleep(3600)
    if kind == "garbage":
        note("end")
        print("this is not json")
        print("the CLI exploded (no private data here)", file=sys.stderr)
        sys.exit(1)
    if kind == "retry":
        calls = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(calls + 1))
        if calls == 0:
            note("end")
            print(outcome("FAILED_RETRYABLE", "Stopped by a browser error (Error: boom). Nothing was submitted; resume to retry."))
            sys.exit(3)
        kind = "prepared"
    note("end")
    if kind == "prepared":
        print(outcome("NEEDS_INPUT", "Prepared to the final review step. Nothing was submitted. Submission remains disabled when this application is resumed."))
        sys.exit(3)
    if kind == "needs":
        print(outcome("NEEDS_INPUT", "1 required question(s) need your answer.", [NEEDS]))
        sys.exit(3)
    if kind == "signin":
        print(outcome("NEEDS_INPUT", "Sign in", [ACTION]))
        sys.exit(3)
    if kind == "closed":
        print(outcome("FAILED_PERMANENT", "The job is no longer accepting applications."))
        sys.exit(3)
    if kind == "dup":
        print(outcome("DUPLICATE", "This job duplicates an application that already exists."))
        sys.exit(4)
    raise SystemExit("unknown kind " + kind)
''')


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI)
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    monkeypatch.setenv("FAKE_COUNTER", str(tmp_path / "retry.count"))
    monkeypatch.setenv("IMX_BROWSER_DIR", str(tmp_path / "parent-browser"))  # must not leak
    return script


@pytest.fixture
def paths(tmp_path: Path) -> LocalPaths:
    return LocalPaths.from_env({}, home=tmp_path / "home")


def log_lines(tmp_path: Path) -> list[dict]:
    log = tmp_path / "fake.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def row(kind: str, *, backend: str = "mock", listing: str | None = None) -> BatchRow:
    """A row whose URL ends in ``kind``; a named listing gets a job (URL) of its own."""
    url = f"{ORIGIN}/{listing}/{kind}" if listing else f"{ORIGIN}/{kind}"
    return BatchRow(listing_id=listing or f"lst_{kind}", pipeline_id=None, company="Brambleway",
                    title=kind.title(), application_url=url, backend=backend,
                    status="resolved")


def options(fake: Path, paths: LocalPaths, **kwargs: object) -> BatchOptions:
    settings: dict = {"paths": paths, "candidate_id": "default", "batch_id": "b1",
                      "command": [sys.executable, str(fake)]}
    settings.update(kwargs)
    return BatchOptions(**settings)


def run(opts: BatchOptions, rows: list[BatchRow], **kwargs: object):
    entries: list[LedgerEntry] = []
    summary = asyncio.run(run_batch(opts, rows, on_entry=entries.append, **kwargs))
    return summary, entries


# --- classification ------------------------------------------------------------------------


def _outcome(state: S, message: str = "") -> ApplyOutcome:
    return ApplyOutcome(application_id="app_x", state=state, message=message)


@pytest.mark.parametrize("state, message, expected", [
    (S.NEEDS_INPUT, "Prepared to the final review step. Nothing was submitted.", "prepared"),
    (S.NEEDS_INPUT, "2 required question(s) need your answer.", "needs_input"),
    (S.FAILED_RETRYABLE, "Stopped by a browser error", "failed_retryable"),
    (S.FAILED_PERMANENT, "The job is no longer accepting applications.", "closed"),
    (S.DUPLICATE, "", "duplicate"),
    (S.INSPECTING, "This job already has application app_1; not applying twice.", "duplicate"),
    (S.SUBMITTED, "Already submitted", "blocked"),
    (S.SUBMISSION_UNKNOWN, "", "blocked"),
    (S.SUBMITTING, "", "blocked"),
    (S.WITHDRAWN, "", "blocked"),
    (S.REQUESTED, "another run is using the browser profile", "failed_retryable"),
])
def test_classify(state, message, expected):
    assert classify(_outcome(state, message)) == expected


# --- options ----------------------------------------------------------------------------------


def test_opencli_needs_a_single_worker(fake, paths):
    with pytest.raises(ValueError, match="one owned Chrome session"):
        options(fake, paths, browser="opencli", opencli_profile="p", workers=2)
    assert options(fake, paths, browser="opencli", opencli_profile="p", workers=1).workers == 1


def test_dynamic_flags_are_validated_and_translated(fake, paths):
    with pytest.raises(ValueError, match="--ai-routing"):
        options(fake, paths, env_file=Path("/tmp/x.env"))
    opts = options(fake, paths, browser="opencli", opencli_profile="prof", ai_routing=True,
                   env_file=Path("/private/x.env"), writer_model="anthropic/claude-opus-5.5",
                   rag_connection_file=Path("/private/conn.json"))
    argv = opts.argv("https://jobs.fictional.example/x")
    assert argv[:5] == [sys.executable, str(fake), "apply", "https://jobs.fictional.example/x",
                        "--json"]
    assert argv[5:] == ["--candidate", "default", "--headless", "--browser", "opencli",
                        "--opencli-profile", "prof", "--ai-routing", "--env-file", "/private/x.env",
                        "--writer-model", "anthropic/claude-opus-5.5",
                        "--rag-connection-file", "/private/conn.json"]
    assert "--submit" not in " ".join(argv)
    for bad in ({"workers": 0}, {"workers": 9}, {"retry_retryable": 3}, {"max_prepared": 0},
                {"per_job_timeout_s": 0}, {"batch_id": "../x"}):
        with pytest.raises(ValueError):
            opts = options(fake, paths, **bad)
            asyncio.run(run_batch(opts, []))


def test_environment_strips_parent_imx_and_pins_paths(fake, paths, monkeypatch):
    monkeypatch.setenv("IMX_HOME", "/somewhere/else")
    monkeypatch.setenv("IMX_STATE_DB", "/somewhere/else/db")
    monkeypatch.setenv("KEEP_ME", "1")
    env = options(fake, paths, workers=3).environment(2)
    assert env["IMX_HOME"] == str(paths.home)
    assert env["IMX_STATE_DB"] == str(paths.state_db)
    assert env["IMX_PROFILE_DIR"] == str(paths.profile_dir)
    assert env["IMX_ARTIFACTS_DIR"] == str(paths.artifacts_dir)
    assert env["IMX_CANDIDATE_ID"] == "default"
    assert env["IMX_BROWSER_DIR"] == str(paths.home / "browser-workers" / "w2")
    assert env["KEEP_ME"] == "1"
    assert {k for k in env if k.startswith("IMX_")} == {
        "IMX_HOME", "IMX_STATE_DB", "IMX_PROFILE_DIR", "IMX_ARTIFACTS_DIR", "IMX_CANDIDATE_ID",
        "IMX_BROWSER_DIR"}


# --- inventory ----------------------------------------------------------------------------------


def _inventory(tmp_path: Path) -> Path:
    rows = [
        {"listing_id": "l1", "pipeline_id": "p1", "company": "A", "title": "T1",
         "source_application_url": f"{ORIGIN}/one", "backend": "lever", "status": "resolved",
         "evidence_url": "https://x.example", "notes": "extra keys are ignored"},
        {"listing_id": "l2", "company": "B", "title": "T2",
         "source_application_url": f"{ORIGIN}/two", "backend": "greenhouse", "status": "resolved"},
        {"listing_id": "l3", "company": "C", "title": "T3",
         "source_application_url": "", "backend": "greenhouse", "status": "resolved"},
        {"listing_id": "l4", "company": "D", "title": "T4",
         "source_application_url": "not a url", "backend": "lever", "status": "resolved"},
        {"listing_id": "l5", "company": "E", "title": "T5",
         "source_application_url": f"{ORIGIN}/five", "backend": "workday", "status": "blocked"},
        {"listing_id": "l6", "company": "F", "title": "T6",
         "source_application_url": f"{ORIGIN}/six?utm_source=x", "backend": "greenhouse",
         "status": "resolved"},
        {"listing_id": 7, "source_application_url": f"{ORIGIN}/seven", "backend": "lever",
         "status": "resolved"},
        "not an object",
    ]
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(rows))
    return path


def test_load_inventory_filters_orders_and_skips_invalid_urls(tmp_path):
    path = _inventory(tmp_path)
    rows, invalid = read_inventory(path)
    assert [r.listing_id for r in rows] == ["l2", "l6", "l1", "7"]  # backend, then file order
    assert invalid == 2  # empty and invalid URLs, both with status resolved
    assert rows[2].pipeline_id == "p1" and rows[2].company == "A" and rows[3].title == ""
    assert rows[1].application_url == f"{ORIGIN}/six?utm_source=x"  # kept verbatim
    assert [r.listing_id for r in load_inventory(path, backends={"lever"})] == ["l1", "7"]
    assert [r.listing_id for r in load_inventory(path, statuses={"blocked"})] == ["l5"]
    assert [r.listing_id for r in load_inventory(path, statuses={"resolved", "blocked"})] == \
        ["l2", "l6", "l1", "7", "l5"]
    assert [r.listing_id for r in load_inventory(path, limit=2)] == ["l2", "l6"]
    assert load_inventory(path, backends={"nope"}) == []
    (tmp_path / "bad.json").write_text('{"rows": "no"}')
    with pytest.raises(ValueError):
        load_inventory(tmp_path / "bad.json")
    (tmp_path / "nolisting.json").write_text(json.dumps(
        [{"source_application_url": f"{ORIGIN}/x/", "status": "resolved"}]))
    assert load_inventory(tmp_path / "nolisting.json")[0].listing_id == f"url:{ORIGIN}/x"


# --- runs ----------------------------------------------------------------------------------------


@pytest.mark.slow
def test_outcomes_ledger_and_summary(fake, paths, tmp_path):
    rows = [row("prepared"), row("needs", backend="greenhouse"), row("signin"), row("closed"),
            row("dup"), row("garbage", backend="greenhouse")]
    summary, entries = run(options(fake, paths, workers=2), rows, skipped_invalid_url=3)
    by_id = {e.listing_id: e for e in entries}
    assert {k: v.outcome for k, v in by_id.items()} == {
        "lst_prepared": "prepared", "lst_needs": "needs_input", "lst_signin": "needs_input",
        "lst_closed": "closed", "lst_dup": "duplicate", "lst_garbage": "error"}
    assert by_id["lst_prepared"].application_id == "app_prepared"
    assert by_id["lst_prepared"].state is S.NEEDS_INPUT and by_id["lst_prepared"].exit_code == 3
    assert by_id["lst_needs"].missing_reasons == ["NO_ANSWER"]
    assert by_id["lst_needs"].missing_labels == ["What is your notice period?"]
    assert by_id["lst_signin"].missing_reasons == ["USER_ACTION"]
    assert by_id["lst_garbage"].application_id is None and by_id["lst_garbage"].exit_code == 1
    assert by_id["lst_garbage"].message == "the CLI exploded (no private data here)"
    assert all(e.batch_id == "b1" and e.attempt == 1 and e.duration_s > 0 for e in entries)
    assert {e.worker_slot for e in entries} <= {0, 1}

    batch_dir = paths.home / "batches" / "b1"
    assert stat.S_IMODE(batch_dir.stat().st_mode) == 0o700
    ledger = batch_dir / "ledger.jsonl"
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
    assert [e.listing_id for e in read_ledger(ledger)] == [e.listing_id for e in entries]
    assert len(ledger.read_text().splitlines()) == 6

    assert summary.batch_id == "b1" and summary.rows == 6 and summary.launched == 6
    assert summary.skipped_settled == 0 and summary.skipped_invalid_url == 3
    assert summary.totals == {"prepared": 1, "needs_input": 2, "closed": 1, "duplicate": 1,
                              "error": 1}
    assert summary.by_backend == {"greenhouse": {"needs_input": 1, "error": 1},
                                  "mock": {"prepared": 1, "needs_input": 1, "closed": 1,
                                           "duplicate": 1}}
    assert summary.application_ids["prepared"] == ["app_prepared"]
    assert "error" not in summary.application_ids
    assert summary.duration_median_s is not None and summary.duration_p95_s is not None
    assert summary.duration_p95_s >= summary.duration_median_s > 0
    assert [(m.label, m.count) for m in summary.top_missing_reasons] == \
        [("NO_ANSWER", 1), ("USER_ACTION", 1)]
    assert summary.top_missing_labels[0].label == "What is your notice period?"
    assert not summary.stopped_at_max_prepared
    written = json.loads((batch_dir / "summary.json").read_text())
    assert written["totals"] == summary.totals
    assert stat.S_IMODE((batch_dir / "summary.json").stat().st_mode) == 0o600

    text = render_summary_markdown(summary)
    assert "# Batch b1" in text and "| prepared | 1 |" in text and "| needs_input | 2 |" in text
    assert "| **all** | 6 |" in text and "greenhouse" in text and "NO_ANSWER (1)" in text
    assert "nothing was submitted" in text and str(ledger) in text
    assert "exploded" not in text  # messages never reach the summary


@pytest.mark.slow
def test_concurrency_is_bounded_and_each_slot_has_its_own_browser_dir(fake, paths, tmp_path,
                                                                        monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "0.5")
    rows = [row("prepared", listing=f"l{i}") for i in range(5)]
    summary, _ = run(options(fake, paths, workers=2), rows)
    assert summary.totals == {"prepared": 5}
    lines = log_lines(tmp_path)
    starts = sorted(x["t"] for x in lines if x["phase"] == "start")
    ends = sorted(x["t"] for x in lines if x["phase"] == "end")
    assert len(starts) == len(ends) == 5
    overlap = max(sum(1 for s, e in zip(starts, ends, strict=True) if s <= t < e)
                  for t in starts)
    concurrent = max(sum(1 for s, e in zip(starts, ends, strict=True) if s <= t < e)
                     for t in starts)
    assert 2 == overlap == concurrent
    dirs = {x["env"]["IMX_BROWSER_DIR"] for x in lines}
    assert dirs == {str(paths.home / "browser-workers" / "w0"),
                    str(paths.home / "browser-workers" / "w1")}
    for directory in dirs:
        assert stat.S_IMODE(Path(directory).stat().st_mode) == 0o700
    # No two overlapping jobs shared a slot directory.
    by_pid = {}
    for x in lines:
        by_pid.setdefault(x["pid"], {}).update({x["phase"]: x["t"], "dir": x["env"]["IMX_BROWSER_DIR"]})
    jobs = list(by_pid.values())
    for a in jobs:
        for b in jobs:
            if a is not b and a["dir"] == b["dir"]:
                assert a["end"] <= b["start"] or b["end"] <= a["start"]
    assert all(x["env"]["IMX_HOME"] == str(paths.home) for x in lines)
    assert all("parent-browser" not in x["env"]["IMX_BROWSER_DIR"] for x in lines)


@pytest.mark.slow
def test_ledger_resumes_and_retries_once(fake, paths, tmp_path):
    rows = [row("prepared"), row("needs"), row("retry"), row("closed"), row("garbage")]
    opts = options(fake, paths, workers=2, retry_retryable=1)
    first, entries = run(opts, rows)
    assert {e.listing_id: e.outcome for e in entries} == {
        "lst_prepared": "prepared", "lst_needs": "needs_input", "lst_retry": "failed_retryable",
        "lst_closed": "closed", "lst_garbage": "error"}
    assert first.launched == 5 and first.skipped_settled == 0

    second, entries = run(opts, rows)
    assert {(e.listing_id, e.attempt, e.outcome) for e in entries} == {
        ("lst_retry", 2, "prepared"), ("lst_garbage", 2, "error")}
    assert second.launched == 2 and second.skipped_settled == 3
    assert second.totals == {"prepared": 2, "needs_input": 1, "closed": 1, "error": 1}
    assert second.application_ids["prepared"] == ["app_prepared", "app_retry"]

    third, entries = run(opts, rows)
    assert entries == [] and third.launched == 0 and third.skipped_settled == 5
    assert third.totals == second.totals
    assert len(read_ledger(paths.home / "batches" / "b1" / "ledger.jsonl")) == 7
    assert sum(1 for x in log_lines(tmp_path) if x["phase"] == "start") == 7

    # A truncated trailing line (crash mid-write) does not poison the ledger.
    ledger = paths.home / "batches" / "b1" / "ledger.jsonl"
    with ledger.open("a") as fh:
        fh.write('{"batch_id": "b1", "listing_id": "lst_')
    assert len(read_ledger(ledger)) == 7

    # No retries at all: the failed rows stay as they are.
    _, entries = run(options(fake, paths, batch_id="b2", retry_retryable=0),
                        [row("garbage")])
    assert [e.outcome for e in entries] == ["error"]
    again, entries = run(options(fake, paths, batch_id="b2", retry_retryable=0),
                         [row("garbage")])
    assert entries == [] and again.skipped_settled == 1


@pytest.mark.slow
def test_timeout_kills_the_job_and_records_an_error(fake, paths, tmp_path):
    summary, [entry] = run(options(fake, paths, per_job_timeout_s=1.0), [row("hang")])
    assert entry.outcome == "error" and entry.application_id is None
    assert entry.message.startswith("timed out after 1 s")
    assert entry.duration_s >= 1.0
    [start] = log_lines(tmp_path)
    with pytest.raises(ProcessLookupError):
        os.kill(start["pid"], 0)
    assert summary.totals == {"error": 1}


@pytest.mark.slow
def test_max_prepared_stops_launching(fake, paths, tmp_path):
    rows = [row("prepared", listing=f"l{i}") for i in range(4)]
    summary, _ = run(options(fake, paths, workers=1, max_prepared=2), rows)
    assert summary.launched == 2 and summary.totals == {"prepared": 2}
    assert summary.stopped_at_max_prepared
    assert sum(1 for x in log_lines(tmp_path) if x["phase"] == "start") == 2
    # Resuming with a higher bound continues with the remaining rows.
    # Resuming with a higher bound continues; a job is launched only while the prepared
    # count plus the jobs still running stays below the bound (two workers, one launch).
    more, _ = run(options(fake, paths, workers=2, max_prepared=3), rows)
    assert more.launched == 1 and more.totals == {"prepared": 3} and more.stopped_at_max_prepared
    assert sum(1 for x in log_lines(tmp_path) if x["phase"] == "start") == 3


@pytest.mark.slow
def test_existing_applications_are_skipped_unless_included(fake, paths, tmp_path):
    paths.ensure()
    url = f"{ORIGIN}/prepared"
    with ApplicationStore.open(paths.state_db) as store:
        waiting = store.record_request("default", url).application
        claim = store.claim(waiting.id, "test")
        store.transition(claim, S.INSPECTING)
        store.transition(claim, S.NEEDS_INPUT, metadata={"missing_inputs": [], "reason": "x"})
        store.release(claim)
        fresh = store.record_request("default", f"{ORIGIN}/needs").application  # REQUESTED
    rows = [row("prepared"), row("needs")]
    summary, entries = run(options(fake, paths), rows)
    assert {e.listing_id: e.outcome for e in entries} == {"lst_prepared": "already_recorded",
                                                          "lst_needs": "needs_input"}
    skipped = entries[0]
    assert skipped.application_id == waiting.id and skipped.state is S.NEEDS_INPUT
    assert skipped.worker_slot is None and skipped.exit_code is None
    assert [x["kind"] for x in log_lines(tmp_path) if x["phase"] == "start"] == ["needs"]
    assert summary.launched == 1 and summary.totals == {"already_recorded": 1, "needs_input": 1}
    assert summary.application_ids == {"needs_input": ["app_needs"],
                                       "already_recorded": [waiting.id]}
    assert fresh.state is S.REQUESTED

    included, entries = run(options(fake, paths, batch_id="b2", include_existing=True), rows)
    assert {e.listing_id: e.outcome for e in entries} == {"lst_prepared": "prepared",
                                                          "lst_needs": "needs_input"}
    assert included.launched == 2


# --- CLI ------------------------------------------------------------------------------------------


def test_prepare_batch_parser(capsys):
    parser = build_parser()
    args = parser.parse_args(["prepare-batch", "--inventory", "inv.json", "--workers", "3",
                              "--backends", "lever,greenhouse", "--limit", "10",
                              "--max-prepared", "5", "--retry-retryable", "0",
                              "--per-job-timeout", "120", "--batch-id", "night", "--json"])
    assert (args.workers, args.limit, args.max_prepared, args.retry_retryable) == (3, 10, 5, 0)
    assert args.backends == "lever,greenhouse" and args.statuses == "resolved"
    assert args.per_job_timeout == 120.0 and args.batch_id == "night" and args.json
    assert args.browser == "playwright" and not args.ai_routing and not args.include_existing
    assert args.func.__name__ == "cmd_prepare_batch"
    with pytest.raises(SystemExit) as exc:
        main(["prepare-batch", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "nothing is submitted" in out and "--workers" in out and "--submit" not in out
    for bad in (["--workers", "0"], ["--workers", "9"], ["--workers", "two"],
                ["--retry-retryable", "3"], ["--max-prepared", "0"], ["--submit"],
                ["--headless"], ["--act"]):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["prepare-batch", "--inventory", "inv.json", *bad])
        assert exc.value.code == EXIT_USAGE
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["prepare-batch"])
    assert exc.value.code == EXIT_USAGE


def test_prepare_batch_usage_errors_touch_no_state(tmp_path, capsys):
    home = tmp_path / "never"
    inventory = tmp_path / "inv.json"
    inventory.write_text(json.dumps([{"listing_id": "l1", "status": "resolved",
                                      "source_application_url": f"{ORIGIN}/prepared"}]))
    assert main(["--home", str(home), "prepare-batch", "--inventory",
                 str(tmp_path / "missing.json")]) == EXIT_USAGE
    assert main(["--home", str(home), "prepare-batch", "--inventory", str(inventory),
                 "--backends", "nope"]) == EXIT_USAGE
    assert "no inventory rows match" in capsys.readouterr().err
    assert main(["--home", str(home), "prepare-batch", "--inventory", str(inventory),
                 "--browser", "opencli", "--workers", "2"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "use --workers 1" in err and "pydantic" not in err
    with pytest.raises(SystemExit) as exc:  # dynamic flags are checked by the parser itself
        main(["--home", str(home), "prepare-batch", "--inventory", str(inventory),
              "--env-file", "/private/x.env"])
    assert exc.value.code == EXIT_USAGE
    assert not home.exists()


@pytest.mark.slow
def test_prepare_batch_command_runs_the_fake_cli(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(batch_module, "default_command", lambda: [sys.executable, str(fake)])
    home = tmp_path / "home"
    inventory = tmp_path / "inv.json"
    inventory.write_text(json.dumps([
        {"listing_id": "l1", "company": "Brambleway", "title": "Prepared",
         "source_application_url": f"{ORIGIN}/prepared", "backend": "mock", "status": "resolved"},
        {"listing_id": "l2", "company": "Brambleway", "title": "Needs",
         "source_application_url": f"{ORIGIN}/needs", "backend": "mock", "status": "resolved"},
        {"listing_id": "l3", "company": "Brambleway", "title": "Bad",
         "source_application_url": "nope", "backend": "mock", "status": "resolved"},
    ]))
    code = main(["--home", str(home), "prepare-batch", "--inventory", str(inventory),
                 "--workers", "2", "--batch-id", "cli", "--json"])
    out, err = capsys.readouterr()
    assert code == EXIT_OK, err
    summary = json.loads(out)
    assert summary["totals"] == {"prepared": 1, "needs_input": 1}
    assert summary["skipped_invalid_url"] == 1 and summary["launched"] == 2
    assert "prepared" in err and "Brambleway — Prepared" in err and "[app_prepared]" in err
    assert "notice period" not in err  # progress lines never show questions
    assert (home / "batches" / "cli" / "ledger.jsonl").exists()
    assert {x["env"]["IMX_HOME"] for x in log_lines(tmp_path)} == {str(home)}

    code = main(["--home", str(home), "prepare-batch", "--inventory", str(inventory),
                 "--batch-id", "cli"])
    out, err = capsys.readouterr()
    assert code == EXIT_OK and "# Batch cli" in out and "| **all** | 2 |" in out
    assert "already settled: 2" in out and f"batch directory: {home / 'batches' / 'cli'}" in out


# --- pipeline cards ---------------------------------------------------------------------------
# ``sync_pipeline_card`` links a row's application to the row's Saved card and moves the card of
# a closed job to Closed. It is looked up on the module when called (``synced``).

CARD_URL = f"{ORIGIN}/card"
CLOSED_REASON = "The job is no longer accepting applications."
FINISHED = datetime(2026, 9, 23, 21, 15, tzinfo=UTC)
LINK_FIELDS = ("linked", "link_reason", "linked_application_id", "closed_synced",
               "closed_sync_reason")
LONG_QUESTION = "Explain in your own words why this role fits you. " * 4
UPDATE_ITEM = PipelineStore.update_item  # the real methods, for edits made during a race
MOVE_ITEM = PipelineStore.move_item

Edit = Callable[[PipelineStore, str, int], PipelineItem]


@pytest.fixture
def pipeline(paths: LocalPaths) -> Iterator[PipelineStore]:
    with PipelineStore.from_paths(paths) as store:
        yield store


def stored_application(paths: LocalPaths, url: str = CARD_URL,
                       state: ApplicationState = S.NEEDS_INPUT, *, candidate: str = "default",
                       failure_reason: str = CLOSED_REASON) -> Application:
    """An application for ``url`` left NEEDS_INPUT or FAILED_PERMANENT, as a runner leaves it."""
    assert state in (S.NEEDS_INPUT, S.FAILED_PERMANENT)
    paths.ensure()
    with ApplicationStore.open(paths.state_db) as store:
        app = store.record_request(candidate, url).application
        claim = store.claim(app.id, "test")
        store.transition(claim, S.INSPECTING)
        if state is S.FAILED_PERMANENT:
            store.transition(claim, state, failure_reason=failure_reason)
        else:
            store.transition(claim, state, metadata={"missing_inputs": [], "reason": "fictional"})
        store.release(claim)
        return store.get_application(app.id)


def saved_card(pipeline: PipelineStore, listing_id: str | None = "lst_card", url: str = CARD_URL,
               *, candidate: str = "default", lane: str = "saved",
               tracking: TrackingFields | None = None, **extra: Any) -> PipelineItem:
    return pipeline.create_item(candidate, NewPipelineItem(
        tracking=tracking or TrackingFields(company="Brambleway", role="Fictional Analyst"),
        lane=lane, listing_id=listing_id, application_url=url, **extra))


def card_entry(pipeline_id: str | None, application_id: str | None, **fields: Any) -> LedgerEntry:
    """A finished ledger entry for row ``lst_card`` (``CARD_URL``); ``fields`` override."""
    finished = fields.pop("finished_at", FINISHED)
    values: dict[str, Any] = {
        "batch_id": "b1", "listing_id": "lst_card", "pipeline_id": pipeline_id,
        "company": "Brambleway", "title": "Fictional Analyst", "application_url": CARD_URL,
        "backend": "mock", "status": "resolved", "attempt": 1, "worker_slot": 0,
        "application_id": application_id, "state": S.NEEDS_INPUT, "outcome": "needs_input",
        "message": "1 required question(s) need your answer.", "exit_code": 3,
        "started_at": finished - timedelta(seconds=4), "finished_at": finished, "duration_s": 4.0,
    }
    return LedgerEntry(**(values | fields))


def closed_entry(pipeline_id: str | None, application_id: str | None,
                 **fields: Any) -> LedgerEntry:
    closed = {"outcome": "closed", "state": S.FAILED_PERMANENT, "message": CLOSED_REASON}
    return card_entry(pipeline_id, application_id, **(closed | fields))


def link_fields(entry: LedgerEntry) -> tuple[Any, ...]:
    """(linked, link_reason, linked_application_id, closed_synced, closed_sync_reason)"""
    return tuple(getattr(entry, name) for name in LINK_FIELDS)


def synced(paths: LocalPaths, entry: LedgerEntry, **kwargs: Any) -> LedgerEntry:
    """``sync_pipeline_card`` for the default candidate; only the link fields may change."""
    result = batch_module.sync_pipeline_card(paths, "default", entry, **kwargs)
    assert result.model_dump(exclude=set(LINK_FIELDS)) == \
        entry.model_dump(exclude=set(LINK_FIELDS))
    return result


def lane_moves(pipeline: PipelineStore, card_id: str) -> list[tuple[str | None, str | None]]:
    return [(h.from_lane, h.to_lane) for h in pipeline.history("default", card_id)
            if h.kind == "moved"]


def edit_card(**changes: Any) -> Edit:
    """Someone else's edit of a default-candidate card: ``lane=`` moves it, else an update."""
    def apply(store: PipelineStore, item_id: str, revision: int) -> PipelineItem:
        if "lane" in changes:
            return MOVE_ITEM(store, "default", item_id, changes["lane"],
                             expected_revision=revision)
        return UPDATE_ITEM(store, "default", item_id, PipelineUpdate(**changes),
                           expected_revision=revision)
    return apply


def lose_races(monkeypatch: pytest.MonkeyPatch, method: str, *, edit: Edit | None = None,
               times: int | None = 1) -> list[dict[str, Any]]:
    """Patch ``PipelineStore.<method>`` so that its first ``times`` calls (every call when
    None) lose a race: ``edit`` (when given) changes the card first and the call raises
    ``RevisionConflict``. Later calls go through. Returns every call's arguments."""
    original = getattr(PipelineStore, method)
    signature = inspect.signature(original)
    calls: list[dict[str, Any]] = []

    def racing(*args: Any, **kwargs: Any) -> PipelineItem:
        call = signature.bind(*args, **kwargs).arguments
        calls.append(call)
        if times is None or len(calls) <= times:
            store, item_id, revision = call["self"], call["item_id"], call["expected_revision"]
            current = edit(store, item_id, revision) if edit else \
                store.get_item(call["candidate_id"], item_id)
            raise RevisionConflict(current, revision)
        return original(*args, **kwargs)

    monkeypatch.setattr(PipelineStore, method, racing)
    return calls


def test_sync_links_the_rows_application_to_its_saved_card(paths, pipeline):
    app = stored_application(paths)
    card = saved_card(pipeline)
    result = synced(paths, card_entry(card.id, app.id))
    assert link_fields(result) == (True, None, app.id, None, None)
    linked = pipeline.get_item("default", card.id)
    assert (linked.lane, linked.application_id, linked.revision) == \
        ("saved", app.id, card.revision + 1)
    changed = {"application_id", "revision", "updated_at"}
    assert linked.model_dump(exclude=changed) == card.model_dump(exclude=changed)
    assert lane_moves(pipeline, card.id) == []

    # Linking again is idempotent: no write and no revision bump.
    again = synced(paths, card_entry(card.id, app.id))
    assert link_fields(again) == (True, None, app.id, None, None)
    assert pipeline.get_item("default", card.id) == linked


def test_sync_never_overwrites_a_link_to_another_application(paths, pipeline):
    app = stored_application(paths)
    other = stored_application(paths, f"{ORIGIN}/other")
    card = saved_card(pipeline, application_id=other.id)
    result = synced(paths, card_entry(card.id, app.id))
    assert link_fields(result) == (False, "card links another application", None, None, None)
    assert pipeline.get_item("default", card.id) == card


def test_sync_only_uses_this_candidates_existing_card(paths, pipeline):
    app = stored_application(paths)
    theirs = saved_card(pipeline, candidate="other")
    for card_id in ("pipe_missing", theirs.id):
        result = synced(paths, card_entry(card_id, app.id))
        assert link_fields(result) == (False, "card not found", None, None, None)
    assert pipeline.list_items("default") == []  # never creates a card
    assert pipeline.list_items("other") == [theirs]


def test_sync_checks_the_cards_listing(paths, pipeline):
    app = stored_application(paths)
    card = saved_card(pipeline, "lst_elsewhere")
    result = synced(paths, card_entry(card.id, app.id))
    assert link_fields(result) == (False, "card is for another listing", None, None, None)
    assert pipeline.get_item("default", card.id) == card

    # A row keyed by its URL has no listing id to compare; a card without a listing takes
    # the row that names it.
    url = f"{ORIGIN}/keyed-by-url"
    by_url = stored_application(paths, url)
    keyed = saved_card(pipeline, "lst_elsewhere", url)
    result = synced(paths, card_entry(keyed.id, by_url.id, application_url=url,
                                      listing_id=f"url:{url}"))
    assert link_fields(result) == (True, None, by_url.id, None, None)
    url = f"{ORIGIN}/no-listing"
    plain = stored_application(paths, url)
    unlisted = saved_card(pipeline, None, url)
    result = synced(paths, card_entry(unlisted.id, plain.id, application_url=url))
    assert link_fields(result) == (True, None, plain.id, None, None)
    assert pipeline.get_item("default", unlisted.id).application_id == plain.id


def test_sync_needs_the_rows_own_application(paths, pipeline):
    app = stored_application(paths)
    theirs = stored_application(paths, f"{ORIGIN}/theirs", candidate="other")
    stored_application(paths, f"{ORIGIN}/other")
    card = saved_card(pipeline)
    for entry, reason in (
        (card_entry(card.id, None, outcome="error", state=None,
                    message="the CLI printed nothing (exit 1)"), "no application id"),
        (card_entry(card.id, "app_missing"), "application not found"),
        (card_entry(card.id, theirs.id, application_url=f"{ORIGIN}/theirs"),
         "application not found"),
        (card_entry(card.id, app.id, application_url=f"{ORIGIN}/unknown"),
         "application does not match the row's URL"),
        (card_entry(card.id, app.id, application_url=f"{ORIGIN}/other"),
         "application does not match the row's URL"),
    ):
        assert link_fields(synced(paths, entry)) == (False, reason, None, None, None)
    assert pipeline.get_item("default", card.id) == card


def test_sync_without_a_state_database_finds_no_application(paths, pipeline):
    card = saved_card(pipeline)
    assert not paths.state_db.exists()
    result = synced(paths, card_entry(card.id, "app_fictional"))
    assert link_fields(result) == (False, "application not found", None, None, None)
    assert pipeline.get_item("default", card.id) == card
    assert not paths.state_db.exists()  # looking is read-only: no empty store appears


def test_sync_links_the_surviving_application_of_a_duplicate(paths, pipeline):
    paths.ensure()
    seen = JobIdentityObservation(
        ats_type="mock", ats_tenant="brambleway", external_job_id="job-7",
        evidence_kind=IdentityEvidenceKind.ATS_JOB_ID_ON_PAGE,
        evidence="Job 7 on the fictional form")
    with ApplicationStore.open(paths.state_db) as store:
        survivor = store.record_request("default", f"{ORIGIN}/first").application
        claim = store.claim(survivor.id, "test")
        store.transition(claim, S.INSPECTING)
        store.bind_job_identity(claim, seen)
        store.release(claim)
        duplicate = store.record_request("default", f"{ORIGIN}/alias").application
        claim = store.claim(duplicate.id, "test")
        store.transition(claim, S.INSPECTING)
        assert store.bind_job_identity(claim, seen).duplicate_of == survivor.id
        store.release(claim)
        assert store.get_application(duplicate.id).state is S.DUPLICATE
        assert store.find_application("default", f"{ORIGIN}/alias").id == survivor.id
    stored_application(paths, f"{ORIGIN}/unrelated")
    card = saved_card(pipeline, url=f"{ORIGIN}/alias")
    entry = card_entry(card.id, duplicate.id, application_url=f"{ORIGIN}/alias",
                       outcome="duplicate", state=S.DUPLICATE,
                       message="This job duplicates an application that already exists.")
    assert link_fields(synced(paths, entry)) == (True, None, survivor.id, None, None)
    assert pipeline.get_item("default", card.id).application_id == survivor.id

    # A duplicate stands in only for the application it duplicates.
    other = saved_card(pipeline, url=f"{ORIGIN}/unrelated")
    elsewhere = entry.model_copy(update={"pipeline_id": other.id,
                                         "application_url": f"{ORIGIN}/unrelated"})
    assert link_fields(synced(paths, elsewhere)) == \
        (False, "application does not match the row's URL", None, None, None)
    assert pipeline.get_item("default", other.id) == other


def test_sync_without_a_pipeline_database_creates_none(paths):
    app = stored_application(paths, state=S.FAILED_PERMANENT)
    database = default_pipeline_db(paths)
    result = synced(paths, closed_entry("pipe_fictional", app.id))
    assert link_fields(result) == (False, "no pipeline database", None, False, "card not linked")
    assert not list(database.parent.glob(database.name + "*"))


def test_sync_without_the_pipeline_package(paths, pipeline, monkeypatch):
    app = stored_application(paths, state=S.FAILED_PERMANENT)
    card = saved_card(pipeline)
    monkeypatch.setitem(sys.modules, "interviewmaxxing_pipeline", None)  # imports now fail
    result = synced(paths, closed_entry(card.id, app.id))
    assert link_fields(result) == \
        (False, "pipeline package not installed", None, False, "card not linked")
    assert pipeline.get_item("default", card.id) == card


def test_sync_leaves_a_row_without_a_card_alone(paths, pipeline):
    app = stored_application(paths, state=S.FAILED_PERMANENT)
    card = saved_card(pipeline)  # the same job, but the row does not name the card
    entry = closed_entry(None, app.id)
    assert synced(paths, entry) == entry
    assert link_fields(entry) == (None, None, None, None, None)
    assert pipeline.list_items("default") == [card]


def test_closed_job_moves_its_saved_card_to_closed_with_a_dated_note(paths, pipeline):
    app = stored_application(paths, state=S.FAILED_PERMANENT)
    card = saved_card(pipeline, tracking=TrackingFields(
        company="Brambleway", role="Fictional Analyst", stage="Saved", status="Interested",
        priority="High"), notes="Referred by a fictional friend", selection_id="sel_fictional",
        next_action_due=date(2026, 10, 1))
    # 02:15 on the 24th at UTC+5 is still the 23rd in UTC.
    finished = datetime(2026, 9, 24, 2, 15, tzinfo=timezone(timedelta(hours=5)))
    entry = closed_entry(card.id, app.id, finished_at=finished)
    result = synced(paths, entry)
    assert link_fields(result) == (True, None, app.id, True, None)
    closed = pipeline.get_item("default", card.id)
    assert (closed.lane, closed.application_id) == ("closed", app.id)
    assert closed.revision == card.revision + 2  # the link, then the move
    changed = {"lane", "application_id", "revision", "updated_at"}
    assert closed.model_dump(exclude=changed) == card.model_dump(exclude=changed)
    history = pipeline.history("default", card.id)
    assert [(h.kind, h.from_lane, h.to_lane) for h in history] == [
        ("created", None, "saved"), ("moved", "saved", "closed")]
    assert history[-1].note == (f"Observed closed on 2026-09-23 (UTC) by prepare-batch b1: "
                                f"{CLOSED_REASON} (application {app.id})")

    # Syncing again finds the card already in Closed: nothing happens and nothing is wrong.
    assert link_fields(synced(paths, entry)) == (True, None, app.id, None, None)
    assert pipeline.get_item("default", card.id) == closed
    assert len(pipeline.history("default", card.id)) == 2


def test_closed_note_keeps_the_runner_wording_without_the_cost_and_truncates_it(paths, pipeline):
    app = stored_application(paths, state=S.FAILED_PERMANENT)
    card = saved_card(pipeline)
    costed = CLOSED_REASON + " Provider cost: USD 0.0100 for 2 call(s)."
    assert synced(paths, closed_entry(card.id, app.id, message=costed)).closed_synced is True
    assert pipeline.history("default", card.id)[-1].note == (
        f"Observed closed on 2026-09-23 (UTC) by prepare-batch b1: {CLOSED_REASON} "
        f"(application {app.id})")

    url = f"{ORIGIN}/long"
    app = stored_application(paths, url, state=S.FAILED_PERMANENT)
    card = saved_card(pipeline, url=url)
    message = CLOSED_REASON + " " + "Brambleway closed this fictional posting after a hiring freeze. " * 5
    entry = closed_entry(card.id, app.id, application_url=url, message=message)
    assert synced(paths, entry).closed_synced is True
    note = pipeline.history("default", card.id)[-1].note
    prefix = "Observed closed on 2026-09-23 (UTC) by prepare-batch b1: "
    suffix = f" (application {app.id})"
    assert note.startswith(prefix) and note.endswith(suffix)
    reason = note[len(prefix):-len(suffix)]
    assert len(reason) <= 200 and reason[:150] == message[:150]


@pytest.mark.parametrize("message", [
    "",
    "The application cannot be completed.",
    "Brambleway rejected this fictional application permanently.",
    "Stopped: " + CLOSED_REASON,  # the wording must open the reason
])
def test_a_permanent_failure_that_is_not_a_closed_job_leaves_the_card(paths, pipeline, message):
    """L15: only the runner's closed wording moves a card; any other FAILED_PERMANENT
    (outcome ``closed`` all the same) is linked but stays in Saved, with no note."""
    app = stored_application(paths, state=S.FAILED_PERMANENT, failure_reason=message or "x")
    card = saved_card(pipeline)
    result = synced(paths, closed_entry(card.id, app.id, message=message))
    assert link_fields(result) == (True, None, app.id, None, None)
    after = pipeline.get_item("default", card.id)
    assert (after.lane, after.application_id) == ("saved", app.id)
    assert lane_moves(pipeline, card.id) == [] and not any(
        h.note for h in pipeline.history("default", card.id))


def test_closed_sync_leaves_a_card_the_user_moved_on(paths, pipeline):
    app = stored_application(paths, state=S.FAILED_PERMANENT)
    card = saved_card(pipeline)
    pipeline.move_item("default", card.id, "applied", expected_revision=card.revision)
    result = synced(paths, closed_entry(card.id, app.id))
    assert link_fields(result) == (True, None, app.id, False, "card not in Saved (in applied)")
    after = pipeline.get_item("default", card.id)
    assert (after.lane, after.application_id) == ("applied", app.id)
    assert lane_moves(pipeline, card.id) == [("saved", "applied")]  # only the user's own move


def test_closed_sync_can_be_turned_off(paths, pipeline):
    app = stored_application(paths, state=S.FAILED_PERMANENT)
    card = saved_card(pipeline)
    result = synced(paths, closed_entry(card.id, app.id), sync_closed=False)
    assert link_fields(result) == (True, None, app.id, None, None)
    after = pipeline.get_item("default", card.id)
    assert (after.lane, after.application_id) == ("saved", app.id)
    assert lane_moves(pipeline, card.id) == []


def test_closed_sync_needs_the_link(paths, pipeline):
    app = stored_application(paths, state=S.FAILED_PERMANENT)
    card = saved_card(pipeline, application_id="app_fictional_other")
    result = synced(paths, closed_entry(card.id, app.id))
    assert link_fields(result) == \
        (False, "card links another application", None, False, "card not linked")
    result = synced(paths, closed_entry("pipe_missing", app.id))
    assert link_fields(result) == (False, "card not found", None, False, "card not linked")
    assert pipeline.list_items("default") == [card]


def test_closed_sync_needs_a_closed_lane(paths, pipeline):
    pipeline.set_lanes("default", BoardLanes(lanes=[BoardLane(id="saved", label="Saved"),
                                                    BoardLane(id="applied", label="Applied")]))
    app = stored_application(paths, state=S.FAILED_PERMANENT)
    card = saved_card(pipeline)
    result = synced(paths, closed_entry(card.id, app.id))
    assert link_fields(result) == (True, None, app.id, False, "board has no Closed lane")
    assert pipeline.get_item("default", card.id).lane == "saved"


def test_closed_sync_finds_the_saved_and_closed_lanes_by_label(paths, pipeline):
    pipeline.set_lanes("default", BoardLanes(lanes=[
        BoardLane(id="inbox", label="SAVED"), BoardLane(id="applied", label="Applied"),
        BoardLane(id="ended", label="closed")]))
    app = stored_application(paths, state=S.FAILED_PERMANENT)
    card = saved_card(pipeline, lane="inbox")
    entry = closed_entry(card.id, app.id)
    assert link_fields(synced(paths, entry)) == (True, None, app.id, True, None)
    assert pipeline.get_item("default", card.id).lane == "ended"
    assert lane_moves(pipeline, card.id) == [("inbox", "ended")]
    assert link_fields(synced(paths, entry)) == (True, None, app.id, None, None)  # already closed
    assert lane_moves(pipeline, card.id) == [("inbox", "ended")]


def test_already_recorded_closed_application_moves_its_card(paths, pipeline):
    reason = f"{CLOSED_REASON} Brambleway took the fictional posting down."
    app = stored_application(paths, state=S.FAILED_PERMANENT, failure_reason=reason)
    card = saved_card(pipeline)
    entry = card_entry(card.id, app.id, outcome="already_recorded", state=S.FAILED_PERMANENT,
                       message="an application already exists (FAILED_PERMANENT)",
                       batch_id="b2", worker_slot=None, exit_code=None,
                       finished_at=datetime(2030, 1, 2, 3, 4, tzinfo=UTC))
    assert link_fields(synced(paths, entry)) == (True, None, app.id, True, None)
    assert pipeline.get_item("default", card.id).lane == "closed"
    assert pipeline.history("default", card.id)[-1].note == (
        f"Observed closed on {app.updated_at.astimezone(UTC):%Y-%m-%d} (UTC) by prepare-batch "
        f"b2: {reason} (application {app.id})")

    # L15: a stored permanent failure for another reason is not a closed job.
    url = f"{ORIGIN}/withdrawn"
    other = stored_application(paths, url, state=S.FAILED_PERMANENT,
                               failure_reason="Brambleway took the fictional posting down.")
    kept = saved_card(pipeline, url=url)
    entry = entry.model_copy(update={"pipeline_id": kept.id, "application_id": other.id,
                                     "application_url": url})
    assert link_fields(synced(paths, entry)) == (True, None, other.id, None, None)
    assert pipeline.get_item("default", kept.id).lane == "saved"


@pytest.mark.parametrize("outcome, state", [
    ("prepared", S.NEEDS_INPUT),
    ("needs_input", S.NEEDS_INPUT),
    ("failed_retryable", S.FAILED_RETRYABLE),
    ("already_recorded", S.NEEDS_INPUT),
])
def test_only_closed_jobs_move_cards(paths, pipeline, outcome, state):
    app = stored_application(paths)
    card = saved_card(pipeline)
    result = synced(paths, card_entry(card.id, app.id, outcome=outcome, state=state))
    assert link_fields(result) == (True, None, app.id, None, None)
    after = pipeline.get_item("default", card.id)
    assert (after.lane, after.application_id) == ("saved", app.id)
    assert lane_moves(pipeline, card.id) == []  # never Applied, never anywhere


def test_link_retries_after_losing_a_race(paths, pipeline, monkeypatch):
    app = stored_application(paths)
    card = saved_card(pipeline)
    calls = lose_races(monkeypatch, "update_item", edit=edit_card(notes="Edited meanwhile"))
    assert link_fields(synced(paths, card_entry(card.id, app.id))) == \
        (True, None, app.id, None, None)
    after = pipeline.get_item("default", card.id)
    assert (after.application_id, after.notes) == (app.id, "Edited meanwhile")
    assert [call["expected_revision"] for call in calls] == [card.revision, card.revision + 1]


def test_link_never_overwrites_a_link_made_during_the_race(paths, pipeline, monkeypatch):
    app = stored_application(paths)
    card = saved_card(pipeline)
    lose_races(monkeypatch, "update_item", edit=edit_card(application_id="app_fictional_other"))
    result = synced(paths, card_entry(card.id, app.id))
    assert link_fields(result) == (False, "card links another application", None, None, None)
    assert pipeline.get_item("default", card.id).application_id == "app_fictional_other"


def test_link_gives_up_when_the_card_keeps_changing(paths, pipeline, monkeypatch):
    app = stored_application(paths, state=S.FAILED_PERMANENT)
    card = saved_card(pipeline)
    calls = lose_races(monkeypatch, "update_item", times=None)
    result = synced(paths, closed_entry(card.id, app.id))
    assert link_fields(result) == (False, "card kept changing", None, False, "card not linked")
    assert len(calls) == batch_module.LINK_ATTEMPTS == 3
    assert pipeline.list_items("default") == [card]


@pytest.mark.parametrize("changes, moved, reason, lane", [
    ({"notes": "Edited meanwhile"}, True, None, "closed"),
    ({"lane": "applied"}, False, "card not in Saved (in applied)", "applied"),
    ({"lane": "closed"}, None, None, "closed"),  # the user closed it first
    ({"application_id": None}, False, "card not linked", "saved"),
    ({"application_id": "app_fictional_other"}, False, "card not linked", "saved"),
])
def test_closed_move_rechecks_the_card_after_losing_a_race(paths, pipeline, monkeypatch,
                                                           changes, moved, reason, lane):
    app = stored_application(paths, state=S.FAILED_PERMANENT)
    card = saved_card(pipeline)
    calls = lose_races(monkeypatch, "move_item", edit=edit_card(**changes))
    result = synced(paths, closed_entry(card.id, app.id))
    assert link_fields(result) == (True, None, app.id, moved, reason)
    after = pipeline.get_item("default", card.id)
    assert (after.lane, after.notes) == (lane, changes.get("notes"))
    assert after.application_id == changes.get("application_id", app.id)
    assert {call["lane"] for call in calls} == {"closed"}  # the only move it ever asks for
    assert lane_moves(pipeline, card.id) == ([] if lane == "saved" else [("saved", lane)])
    # Only the sync's own move carries a note (the concurrent edits have none).
    assert len([h for h in pipeline.history("default", card.id) if h.note]) == int(moved is True)


def test_closed_move_gives_up_when_the_card_keeps_changing(paths, pipeline, monkeypatch):
    app = stored_application(paths, state=S.FAILED_PERMANENT)
    card = saved_card(pipeline)
    calls = lose_races(monkeypatch, "move_item", times=None)
    result = synced(paths, closed_entry(card.id, app.id))
    assert link_fields(result) == (True, None, app.id, False, "card kept changing")
    assert len(calls) == batch_module.LINK_ATTEMPTS
    after = pipeline.get_item("default", card.id)
    assert (after.lane, after.application_id) == ("saved", app.id)


def test_sync_reports_unexpected_errors_instead_of_raising(paths, pipeline, monkeypatch):
    app = stored_application(paths, state=S.FAILED_PERMANENT)
    card = saved_card(pipeline)

    def locked(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(PipelineStore, "move_item", locked)
    result = synced(paths, closed_entry(card.id, app.id))
    assert link_fields(result) == (True, None, app.id, False, "move error: OperationalError")
    monkeypatch.setattr(PipelineStore, "get_item", locked)
    result = synced(paths, card_entry(card.id, app.id))
    assert link_fields(result) == (False, "link error: OperationalError", None, None, None)
    [after] = pipeline.list_items("default")  # never creates a card
    assert (after.id, after.lane, after.application_id) == (card.id, "saved", app.id)


def test_summary_and_progress_lines_count_pipeline_cards(tmp_path):
    entries = [
        card_entry("pipe_1", "app_1", listing_id="l1", outcome="prepared", linked=True,
                   linked_application_id="app_1"),
        card_entry("pipe_2", "app_2", listing_id="l2", linked=False,
                   link_reason="card kept changing"),
        card_entry("pipe_2", "app_2", listing_id="l2", linked=False,
                   link_reason="card not found"),  # the row's latest entry wins
        card_entry("pipe_3", "app_3", listing_id="l3", outcome="closed",
                   state=S.FAILED_PERMANENT, linked=True, linked_application_id="app_3",
                   closed_synced=True),
        card_entry("pipe_4", "app_4", listing_id="l4", outcome="already_recorded",
                   state=S.FAILED_PERMANENT, linked=True, linked_application_id="app_4",
                   closed_synced=False, closed_sync_reason="card not in Saved (in applied)"),
        card_entry("pipe_5", "app_5", listing_id="l5", outcome="closed",
                   state=S.FAILED_PERMANENT, linked=False, link_reason="card not found",
                   closed_synced=False, closed_sync_reason="card not linked"),
        card_entry(None, "app_6", listing_id="l6", outcome="prepared"),
    ]

    def summary_of(selected: list[LedgerEntry]) -> Any:
        return summarize("b1", selected, started_at=FINISHED, finished_at=FINISHED,
                         rows=len(selected), launched=len(selected), skipped_settled=0,
                         skipped_invalid_url=0, ledger_path=tmp_path / "ledger.jsonl")

    summary = summary_of(entries)
    assert (summary.pipeline_linked, summary.pipeline_not_linked, summary.pipeline_closed) == \
        (3, 2, 1)
    # A closed row whose link failed counts once, under the link reason.
    assert {p.label: p.count for p in summary.pipeline_problems} == \
        {"card not found": 2, "card not in Saved (in applied)": 1}
    text = render_summary_markdown(summary)
    assert "- pipeline cards: 3 linked, 2 not linked, 1 moved to Closed" in text
    [problems] = [line for line in text.splitlines() if line.startswith("- pipeline problems: ")]
    assert "card not found (2)" in problems and "card not in Saved (in applied) (1)" in problems
    assert "card kept changing" not in text and "card not linked" not in text

    lines = [format_entry(e) for e in entries]
    assert lines[0].endswith(" [card linked]") and lines[2].endswith(" [card not linked]")
    assert lines[3].endswith(" [card linked, moved to Closed]")
    assert lines[4].endswith(" [card linked]") and lines[5].endswith(" [card not linked]")
    assert "[card" not in lines[6]

    plain = summary_of(entries[-1:])  # no row names a card
    assert (plain.pipeline_linked, plain.pipeline_not_linked, plain.pipeline_closed) == (0, 0, 0)
    assert plain.pipeline_problems == []
    assert "pipeline cards" not in render_summary_markdown(plain)


def test_old_ledger_lines_still_read(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps({
        "batch_id": "b0", "listing_id": "l1", "pipeline_id": "pipe_1",
        "application_url": f"{ORIGIN}/old", "attempt": 1, "application_id": "app_1",
        "state": "NEEDS_INPUT", "outcome": "needs_input", "missing_reasons": ["NO_ANSWER"],
        "missing_labels": ["What is your notice period?"],
        "started_at": "2026-09-01T10:00:00.000000Z", "finished_at": "2026-09-01T10:01:00.000000Z",
        "duration_s": 60.0}) + "\n")
    [entry] = read_ledger(ledger)
    assert entry.missing_items == [] and entry.missing_labels == ["What is your notice period?"]
    assert link_fields(entry) == (None, None, None, None, None)
    assert format_entry(entry).endswith("[app_1]")


STORE_FAKE_CLI = textwrap.dedent('''\
    """Fake ``interviewmaxxing apply`` backed by the real store: records the request, claims
    the application and leaves it the way the runner would for the URL's last path segment
    (prepared / needs / closed), then prints the outcome with the real application id."""
    import json, os, sys, time

    from interviewmaxxing_core import ApplicationState as S, ApplicationStore, LocalPaths

    assert sys.argv[1] == "apply" and "--json" in sys.argv and "--headless" in sys.argv
    url = sys.argv[2]
    candidate = sys.argv[sys.argv.index("--candidate") + 1]
    kind = url.rsplit("/", 1)[-1].split("?")[0]
    paths = LocalPaths.from_env()
    assert paths.candidate_id == candidate
    time.sleep(float(os.environ.get("FAKE_SLEEP", "0.05")))

    LOOKUP = {"field_id": "city", "form_url": url, "form_step": 0, "field_fingerprint": "ab" * 32,
              "label": "Current location\\nStart typing your city", "reason": "NO_ANSWER",
              "prompt": "Please answer.", "required": True, "control_type": "TYPEAHEAD"}
    LONG = {"field_id": "why", "form_url": url, "form_step": 0, "field_fingerprint": "cd" * 32,
            "label": @LONG_QUESTION@, "reason": "EXPLICIT_ANSWER_REQUIRED",
            "prompt": "Please answer.", "required": True}

    with ApplicationStore.open(paths.state_db) as store:
        app = store.record_request(candidate, url).application
        claim = store.claim(app.id, "store-fake-cli")
        store.transition(claim, S.INSPECTING)
        missing = []
        if kind == "prepared":
            state = S.NEEDS_INPUT
            message = ("Prepared to the final review step. Nothing was submitted. Submission "
                       "remains disabled when this application is resumed.")
            store.transition(claim, state, metadata={"missing_inputs": [], "reason": message})
            store.append_event(claim, "preparation.ready", {"submitted": False})
        elif kind == "needs":
            state, missing = S.NEEDS_INPUT, [LOOKUP, LONG]
            message = "2 required question(s) need your answer."
            store.transition(claim, state, metadata={"missing_inputs": missing, "reason": message})
        elif kind == "closed":
            state, message = S.FAILED_PERMANENT, @CLOSED_REASON@
            store.transition(claim, state, failure_reason=message)
        else:
            raise SystemExit("unknown kind " + kind)
        store.release(claim)
    print(json.dumps({"application_id": app.id, "state": state.value, "receipt": None,
                      "missing_inputs": missing, "message": message}))
    sys.exit(3)
''').replace("@LONG_QUESTION@", repr(LONG_QUESTION)).replace("@CLOSED_REASON@",
                                                              repr(CLOSED_REASON))


@pytest.fixture
def store_fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "store_fake_cli.py"
    script.write_text(STORE_FAKE_CLI)
    monkeypatch.setenv("FAKE_SLEEP", "0.05")
    return script


def store_row(listing_id: str, path: str, pipeline_id: str | None) -> BatchRow:
    return BatchRow(listing_id=listing_id, pipeline_id=pipeline_id, company="Brambleway",
                    title=listing_id, application_url=f"{ORIGIN}/{path}", backend="mock",
                    status="resolved")


@pytest.mark.slow
def test_batch_links_cards_moves_closed_ones_and_backfills_later_batches(store_fake, paths):
    carded = {"lst_prepared": "one/prepared", "lst_needs": "two/needs",
              "lst_closed": "three/closed"}
    with PipelineStore.from_paths(paths) as pipeline:
        cards = {listing: saved_card(pipeline, listing, f"{ORIGIN}/{path}")
                 for listing, path in carded.items()}
    rows = [*(store_row(listing, path, cards[listing].id) for listing, path in carded.items()),
            store_row("lst_ghost", "four/prepared", "pipe_missing"),
            store_row("lst_nocard", "five/needs", None)]
    opts = options(store_fake, paths, workers=2)
    summary, entries = run(opts, rows)

    ledger = paths.home / "batches" / "b1" / "ledger.jsonl"
    recorded = read_ledger(ledger)
    # Every ledger line carries its link fields, as reported to on_entry.
    assert [(e.listing_id, link_fields(e)) for e in recorded] == \
        [(e.listing_id, link_fields(e)) for e in entries]
    by_id = {e.listing_id: e for e in recorded}
    assert {k: e.outcome for k, e in by_id.items()} == {
        "lst_prepared": "prepared", "lst_needs": "needs_input", "lst_closed": "closed",
        "lst_ghost": "prepared", "lst_nocard": "needs_input"}
    app = {k: e.application_id for k, e in by_id.items()}
    assert len(set(app.values())) == 5 and all(str(a).startswith("app_") for a in app.values())
    assert {k: link_fields(e) for k, e in by_id.items()} == {
        "lst_prepared": (True, None, app["lst_prepared"], None, None),
        "lst_needs": (True, None, app["lst_needs"], None, None),
        "lst_closed": (True, None, app["lst_closed"], True, None),
        "lst_ghost": (False, "card not found", None, None, None),
        "lst_nocard": (None, None, None, None, None)}
    collapsed = " ".join(LONG_QUESTION.split())
    needs = by_id["lst_needs"]
    assert [item.model_dump() for item in needs.missing_items] == [
        {"label": "Current location Start typing your city", "reason": "NO_ANSWER",
         "control_type": "TYPEAHEAD", "field_id": "city", "semantic_type": "UNKNOWN"},
        {"label": collapsed[:119] + "…", "reason": "EXPLICIT_ANSWER_REQUIRED",
         "control_type": None, "field_id": "why", "semantic_type": "UNKNOWN"}]
    assert needs.missing_labels == [item.label for item in needs.missing_items]
    assert needs.missing_reasons == ["EXPLICIT_ANSWER_REQUIRED", "NO_ANSWER"]
    assert by_id["lst_prepared"].missing_items == by_id["lst_closed"].missing_items == []

    with PipelineStore.from_paths(paths) as pipeline:
        items = pipeline.list_items("default")
        assert {i.listing_id: (i.lane, i.application_id) for i in items} == {
            "lst_prepared": ("saved", app["lst_prepared"]),
            "lst_needs": ("saved", app["lst_needs"]),
            "lst_closed": ("closed", app["lst_closed"])}  # and no card was created
        [move] = [h for h in pipeline.history("default", cards["lst_closed"].id)
                  if h.kind == "moved"]
        assert not any(h.to_lane == "applied" for card in cards.values()
                       for h in pipeline.history("default", card.id))
    closed_on = by_id["lst_closed"].finished_at.astimezone(UTC).strftime("%Y-%m-%d")
    assert (move.from_lane, move.to_lane) == ("saved", "closed")
    assert move.note == (f"Observed closed on {closed_on} (UTC) by prepare-batch b1: "
                         f"{CLOSED_REASON} (application {app['lst_closed']})")

    assert summary.totals == {"prepared": 2, "needs_input": 2, "closed": 1}
    assert (summary.pipeline_linked, summary.pipeline_not_linked, summary.pipeline_closed) == \
        (3, 1, 1)
    assert [(p.label, p.count) for p in summary.pipeline_problems] == [("card not found", 1)]
    text = render_summary_markdown(summary)
    assert "- pipeline cards: 3 linked, 1 not linked, 1 moved to Closed" in text
    assert "- pipeline problems: card not found (1)" in text
    assert format_entry(by_id["lst_prepared"]).endswith(" [card linked]")
    assert format_entry(by_id["lst_closed"]).endswith(" [card linked, moved to Closed]")
    assert format_entry(by_id["lst_ghost"]).endswith(" [card not linked]")
    assert "[card" not in format_entry(by_id["lst_nocard"])
    written = json.loads((paths.home / "batches" / "b1" / "summary.json").read_text())
    assert (written["pipeline_linked"], written["pipeline_closed"]) == (3, 1)

    # Rerunning the batch launches nothing and touches no card.
    again, entries = run(opts, rows)
    assert entries == [] and again.launched == 0 and again.skipped_settled == 5
    assert (again.pipeline_linked, again.pipeline_not_linked, again.pipeline_closed) == (3, 1, 1)
    assert len(read_ledger(ledger)) == 5
    with PipelineStore.from_paths(paths) as pipeline:
        assert pipeline.list_items("default") == items
        late = saved_card(pipeline, "lst_nocard", f"{ORIGIN}/five/needs")  # saved afterwards

    # A new batch over the same rows records them as already recorded and links them
    # (idempotently): only the card saved since then is written.
    rows = [*rows[:4], store_row("lst_nocard", "five/needs", late.id)]
    backfill, entries = run(options(store_fake, paths, batch_id="b2", workers=2), rows)
    assert backfill.launched == 0 and backfill.totals == {"already_recorded": 5}
    by_id = {e.listing_id: e for e in read_ledger(paths.home / "batches" / "b2" / "ledger.jsonl")}
    assert {k: e.application_id for k, e in by_id.items()} == app
    assert by_id["lst_closed"].state is S.FAILED_PERMANENT
    assert {k: link_fields(e) for k, e in by_id.items()} == {
        "lst_prepared": (True, None, app["lst_prepared"], None, None),
        "lst_needs": (True, None, app["lst_needs"], None, None),
        "lst_closed": (True, None, app["lst_closed"], None, None),  # already in Closed
        "lst_ghost": (False, "card not found", None, None, None),
        "lst_nocard": (True, None, app["lst_nocard"], None, None)}
    assert (backfill.pipeline_linked, backfill.pipeline_not_linked,
            backfill.pipeline_closed) == (4, 1, 0)
    assert [(p.label, p.count) for p in backfill.pipeline_problems] == [("card not found", 1)]
    with PipelineStore.from_paths(paths) as pipeline:
        after = {i.id: i for i in pipeline.list_items("default")}
    assert [after[i.id] for i in items] == items
    assert (after[late.id].application_id, after[late.id].revision) == \
        (app["lst_nocard"], late.revision + 1)
    assert len(after) == 4


def test_prepare_batch_parser_sync_closed_flag(capsys):
    parser = build_parser()
    base = ["prepare-batch", "--inventory", "inv.json"]
    assert parser.parse_args(base).sync_closed is True
    assert parser.parse_args([*base, "--sync-closed"]).sync_closed is True
    assert parser.parse_args([*base, "--no-sync-closed"]).sync_closed is False
    with pytest.raises(SystemExit) as exc:
        main(["prepare-batch", "--help"])
    assert exc.value.code == 0 and "--no-sync-closed" in capsys.readouterr().out


@pytest.mark.slow
def test_prepare_batch_no_sync_closed_links_but_keeps_the_card_in_saved(store_fake, tmp_path,
                                                                       monkeypatch, capsys):
    monkeypatch.setattr(batch_module, "default_command", lambda: [sys.executable, str(store_fake)])
    home = tmp_path / "home"
    local = LocalPaths.from_env({}, home=home)
    url = f"{ORIGIN}/cli/closed"
    with PipelineStore.from_paths(local) as pipeline:
        card = saved_card(pipeline, "l-closed", url)
    inventory = tmp_path / "inv.json"
    inventory.write_text(json.dumps([
        {"listing_id": "l-closed", "pipeline_id": card.id, "company": "Brambleway",
         "title": "Closed", "source_application_url": url, "backend": "mock",
         "status": "resolved"}]))
    argv = ["--home", str(home), "prepare-batch", "--inventory", str(inventory), "--json"]
    code = main([*argv, "--batch-id", "cli", "--no-sync-closed"])
    out, err = capsys.readouterr()
    assert code == EXIT_OK, err
    summary = json.loads(out)
    assert summary["totals"] == {"closed": 1}
    assert (summary["pipeline_linked"], summary["pipeline_closed"]) == (1, 0)
    [entry] = read_ledger(home / "batches" / "cli" / "ledger.jsonl")
    assert entry.outcome == "closed" and entry.application_id
    assert link_fields(entry) == (True, None, entry.application_id, None, None)
    assert "[card linked]" in err
    with PipelineStore.from_paths(local) as pipeline:
        kept = pipeline.get_item("default", card.id)
    assert (kept.lane, kept.application_id) == ("saved", entry.application_id)

    # By default a later batch moves it: the application is recorded as closed.
    code = main([*argv, "--batch-id", "cli-2"])
    out, err = capsys.readouterr()
    assert code == EXIT_OK, err
    assert json.loads(out)["pipeline_closed"] == 1
    [later] = read_ledger(home / "batches" / "cli-2" / "ledger.jsonl")
    assert (later.outcome, later.state) == ("already_recorded", S.FAILED_PERMANENT)
    assert link_fields(later) == (True, None, entry.application_id, True, None)
    assert "[card linked, moved to Closed]" in err
    with PipelineStore.from_paths(local) as pipeline:
        assert pipeline.get_item("default", card.id).lane == "closed"


# --- per-application provider cost (deliverable 3) -----------------------------------------

PREPARED_MESSAGE = ("Prepared to the final review step. Nothing was submitted. Submission "
                    "remains disabled when this application is resumed.")


def usage(calls: int, cost: float, unknown: int = 0) -> dict[str, Any]:
    """A ``provider.budget`` event's metadata as the runner records it (fictional numbers)."""
    bucket = {"calls": calls, "known_cost_usd": cost, "unknown_cost_calls": unknown,
              "latency_seconds": 0.25 * calls}
    return bucket | {"by_purpose": {"full_form_routes": dict(bucket)}}


def application_with_runs(paths: LocalPaths, url: str, *runs: dict[str, Any]) -> str:
    """An application for ``url`` whose runs each recorded one ``provider.budget`` event
    under their own claim, beside an unrelated event with cost-like metadata."""
    paths.ensure()
    with ApplicationStore.open(paths.state_db) as store:
        app = store.record_request("default", url).application
        for metadata in runs:
            claim = store.claim(app.id, "test")
            store.append_event(claim, "form.discovered", {"calls": 9, "known_cost_usd": 9.0})
            store.append_event(claim, "provider.budget", metadata)
            store.release(claim)
        return app.id


def test_provider_cost_sums_every_run_of_one_application(paths):
    app_id = application_with_runs(paths, f"{ORIGIN}/costly", usage(2, 0.0123),
                                   usage(3, 0.0456, unknown=1))
    other = application_with_runs(paths, f"{ORIGIN}/other", usage(7, 0.5))
    assert batch_module.provider_cost(paths, app_id) == (0.0579, 5)
    assert batch_module.provider_cost(paths, other) == (0.5, 7)


def test_provider_cost_without_provider_events_is_none(paths):
    assert batch_module.provider_cost(paths, "app_fictional") == (None, None)
    assert not paths.state_db.exists()  # reading never creates the state database
    free = stored_application(paths, f"{ORIGIN}/free")
    assert batch_module.provider_cost(paths, free.id) == (None, None)
    assert batch_module.provider_cost(paths, "app_unknown") == (None, None)
    assert batch_module.provider_cost(paths, None) == (None, None)


def test_ledger_lines_without_provider_cost_still_read(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps({
        "batch_id": "b0", "listing_id": "l1", "pipeline_id": None,
        "application_url": f"{ORIGIN}/old", "attempt": 1, "application_id": "app_1",
        "state": "NEEDS_INPUT", "outcome": "prepared", "missing_reasons": [],
        "missing_labels": [], "started_at": "2026-09-01T10:00:00.000000Z",
        "finished_at": "2026-09-01T10:01:00.000000Z", "duration_s": 60.0}) + "\n")
    [old] = read_ledger(ledger)
    assert (old.provider_cost_usd, old.provider_calls) == (None, None)
    costed = old.model_copy(update={"listing_id": "l2", "provider_cost_usd": 0.0579,
                                    "provider_calls": 5})
    batch_module.append_ledger(ledger, costed)
    assert [(e.listing_id, e.provider_cost_usd, e.provider_calls) for e in read_ledger(ledger)] \
        == [("l1", None, None), ("l2", 0.0579, 5)]
    written = json.loads(ledger.read_text().splitlines()[-1])
    assert (written["provider_cost_usd"], written["provider_calls"]) == (0.0579, 5)


COST_FAKE_CLI = textwrap.dedent('''\
    """Fake ``interviewmaxxing apply`` backed by the real store that records AI provider
    usage like the runner: one ``provider.budget`` event per run under the run's claim, and
    the cost at the end of the outcome message. ``costly`` prepares after 2 calls, ``free``
    prepares without a provider, ``flaky`` fails retryably after 1 call and prepares after
    3 more on its next run."""
    import json, sys

    from interviewmaxxing_core import ApplicationState as S, ApplicationStore, LocalPaths

    assert sys.argv[1] == "apply" and "--json" in sys.argv and "--headless" in sys.argv
    url = sys.argv[2]
    candidate = sys.argv[sys.argv.index("--candidate") + 1]
    kind = url.rsplit("/", 1)[-1].split("?")[0]

    def usage(calls, cost):
        return {"calls": calls, "known_cost_usd": cost, "unknown_cost_calls": 0,
                "latency_seconds": 0.1 * calls, "by_purpose": {}}

    with ApplicationStore.open(LocalPaths.from_env().state_db) as store:
        app = store.record_request(candidate, url).application
        claim = store.claim(app.id, "cost-fake-cli")
        earlier = sum(e.event == "provider.budget" for e in store.list_events(app.id))
        store.transition(claim, S.INSPECTING)
        run = {"costly": usage(2, 0.0123), "free": None,
               "flaky": usage(3, 0.02) if earlier else usage(1, 0.004)}[kind]
        cost = ""
        if run is not None:
            store.append_event(claim, "provider.budget", run)
            cost = f" Provider cost: USD {run['known_cost_usd']:.4f} for {run['calls']} call(s)."
        if kind == "flaky" and not earlier:
            state = S.FAILED_RETRYABLE
            message = "Stopped by a browser error (fictional). Nothing was submitted; resume to retry."
            store.transition(claim, state, failure_reason=message)
        else:
            state, message = S.NEEDS_INPUT, @PREPARED@
            store.transition(claim, state, metadata={"missing_inputs": [], "reason": "fictional"})
        store.release(claim)
    print(json.dumps({"application_id": app.id, "state": state.value, "receipt": None,
                      "missing_inputs": [], "message": message + cost}))
    sys.exit(3)
''').replace("@PREPARED@", repr(PREPARED_MESSAGE))


@pytest.fixture
def cost_fake(tmp_path: Path) -> Path:
    script = tmp_path / "cost_fake_cli.py"
    script.write_text(COST_FAKE_CLI)
    return script


@pytest.mark.slow
def test_batch_ledger_lines_carry_the_applications_provider_cost(cost_fake, paths):
    rows = [row("costly"), row("free"), row("flaky")]
    opts = options(cost_fake, paths, workers=2, retry_retryable=1)
    _, entries = run(opts, rows)
    assert {e.listing_id: (e.outcome, e.provider_cost_usd, e.provider_calls)
            for e in entries} == {
        "lst_costly": ("prepared", 0.0123, 2), "lst_free": ("prepared", None, None),
        "lst_flaky": ("failed_retryable", 0.004, 1)}

    # The retry's line carries the application's cost so far: both of its runs.
    _, entries = run(opts, rows)
    assert [(e.listing_id, e.attempt, e.outcome, e.provider_cost_usd, e.provider_calls)
            for e in entries] == [("lst_flaky", 2, "prepared", 0.024, 4)]
    ledger = read_ledger(paths.home / "batches" / "b1" / "ledger.jsonl")
    assert {(e.listing_id, e.attempt): (e.provider_cost_usd, e.provider_calls)
            for e in ledger} == {
        ("lst_costly", 1): (0.0123, 2), ("lst_free", 1): (None, None),
        ("lst_flaky", 1): (0.004, 1), ("lst_flaky", 2): (0.024, 4)}

    report = batch_module.build_report(paths, ["b1"])
    assert report.rows == 3 and report.totals == {"prepared": 3}
    assert (report.provider_cost_usd, report.provider_calls, report.provider_cost_rows) == \
        (0.0363, 6, 2)
    assert report.cost_per_prepared_usd == 0.0121


# --- round 4: batch families and --exclude-batches -----------------------------------------------


def _pilot_line(batch_id: str, listing: str, kind: str, outcome: str, *, app: str | None = None,
                retry_of: str | None = None, day: int = 24,
                state: S | None = None) -> LedgerEntry:
    finished = datetime(2026, 9, day, 9, 0, tzinfo=UTC)
    return LedgerEntry(batch_id=batch_id, listing_id=listing,
                       application_url=f"{ORIGIN}/{listing}/{kind}", backend="mock", attempt=1,
                       application_id=app, outcome=outcome, retry_of=retry_of, state=state,
                       started_at=finished, finished_at=finished, duration_s=1.0)


def _write_pilot(paths: LocalPaths) -> None:
    """A pilot batch and a retry of it (ledgers only), and an unrelated batch."""
    from interviewmaxxing_cli.batch import append_ledger

    def write(*lines: LedgerEntry) -> None:
        for line in lines:
            append_ledger(paths.home / "batches" / line.batch_id / "ledger.jsonl", line)

    write(_pilot_line("pilot", "lst_a", "prepared", "prepared", app="app_a"),
          _pilot_line("pilot", "lst_b", "needs", "needs_input", app="app_b"),
          _pilot_line("pilot", "lst_c", "prepared", "failed_retryable", app="app_c"),
          _pilot_line("pilot", "lst_d", "closed", "closed", app="app_d"),
          _pilot_line("pilot", "lst_e", "prepared", "error"),
          _pilot_line("pilot", "lst_f", "needs", "already_recorded", app="app_f",
                      state=S.NEEDS_INPUT))
    write(_pilot_line("pilot-r1", "lst_c", "prepared", "needs_input", app="app_c",
                      retry_of="pilot", day=25),
          _pilot_line("pilot-r1", "lst_d", "closed", "failed_retryable", app="app_d",
                      retry_of="pilot", day=25))
    write(_pilot_line("unrelated", "lst_g", "needs", "needs_input", app="app_g", day=25))


def test_batch_tree_families_and_what_they_prepared_or_held(paths):
    from interviewmaxxing_cli.batch import BatchTree, batches_since, prepared_or_held

    _write_pilot(paths)
    tree = BatchTree.read(paths)  # no summary yet: the ledger lines' retry_of
    assert tree.batches == ("pilot", "pilot-r1", "unrelated")
    assert dict(tree.parents) == {"pilot-r1": "pilot"}
    assert (tree.root("pilot-r1"), tree.root("pilot"), tree.root("unrelated")) == \
        ("pilot", "pilot", "unrelated")
    assert tree.family("pilot-r1") == tree.family("pilot") == ["pilot", "pilot-r1"]
    assert batches_since(paths, datetime(2026, 9, 25)) == ["pilot-r1", "unrelated"]

    exclusion = prepared_or_held(paths, ["pilot-r1"])  # a retry names its whole family
    assert exclusion.batches == ("pilot", "pilot-r1")
    assert exclusion.listing_ids == {"lst_a", "lst_b", "lst_c", "lst_f"}  # not d, e, g
    alias = BatchRow(listing_id="lst_other", application_url=f"{ORIGIN}/lst_b/needs?utm_source=x")
    assert exclusion.excludes(alias) and not exclusion.excludes(row("prepared"))
    with pytest.raises(FileNotFoundError, match="no ledger for batch 'nope'"):
        prepared_or_held(paths, ["pilot", "nope"])
    with pytest.raises(ValueError):
        prepared_or_held(paths, ["../pilot"])
    assert not paths.state_db.exists()  # reads the ledgers only


@pytest.mark.slow
def test_exclude_batches_leaves_out_what_earlier_batches_prepared_or_held(fake, tmp_path,
                                                                          monkeypatch, capsys):
    monkeypatch.setattr(batch_module, "default_command", lambda: [sys.executable, str(fake)])
    home = tmp_path / "home"
    paths = LocalPaths.from_env({}, home=home)
    _write_pilot(paths)
    rows = [("lst_a", "prepared"), ("lst_b", "needs"), ("lst_c", "prepared"), ("lst_d", "closed"),
            ("lst_e", "prepared"), ("lst_f", "needs"), ("lst_g", "needs"), ("lst_i", "prepared")]
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(
        [{"listing_id": listing, "company": "Brambleway", "title": kind,
          "source_application_url": f"{ORIGIN}/{listing}/{kind}", "backend": "mock",
          "status": "resolved"} for listing, kind in rows]
        + [{"listing_id": "lst_h", "company": "Brambleway", "title": "alias of b",
            "source_application_url": f"{ORIGIN}/lst_b/needs/?utm_source=board", "backend": "mock",
            "status": "resolved"}]))
    base = ["--home", str(home), "prepare-batch", "--inventory", str(inventory)]

    assert main([*base, "--exclude-batches", "pilot", "--batch-id", "mass", "--json"]) == EXIT_OK
    out, err = capsys.readouterr()
    summary = json.loads(out)
    assert (summary["skipped_excluded"], summary["excluded_batches"]) == (5, ["pilot", "pilot-r1"])
    assert summary["rows"] == 4 and summary["launched"] == 4
    launched = sorted(x["url"].split("/")[-2] for x in log_lines(tmp_path) if x["phase"] == "start")
    assert launched == ["lst_d", "lst_e", "lst_g", "lst_i"]  # closed, errored, other batches, new
    assert summary["totals"] == {"prepared": 2, "closed": 1, "needs_input": 1}
    assert "5 row(s) left out as prepared or held by pilot, pilot-r1" in err
    ledger = read_ledger(home / "batches" / "mass" / "ledger.jsonl")
    assert sorted(e.listing_id for e in ledger) == ["lst_d", "lst_e", "lst_g", "lst_i"]
    text = render_summary_markdown(summarize(
        "mass", ledger, started_at=FINISHED, finished_at=FINISHED, rows=4, launched=4,
        skipped_settled=0, skipped_invalid_url=0, ledger_path=Path("ledger.jsonl"),
        skipped_excluded=5, excluded_batches=["pilot", "pilot-r1"]))
    assert "- left out as prepared or held by earlier batches: 5 (--exclude-batches: pilot, " \
           "pilot-r1)" in text

    # --limit counts the rows left after the exclusion; several batches, repeated or not.
    assert main([*base, "--exclude-batches", "pilot,unrelated", "--exclude-batches", "pilot",
                 "--limit", "1", "--batch-id", "mass2", "--json"]) == EXIT_OK
    second = json.loads(capsys.readouterr().out)
    assert second["excluded_batches"] == ["pilot", "pilot-r1", "unrelated"]
    assert (second["skipped_excluded"], second["rows"]) == (6, 1)

    # Usage: an unknown batch, a batch id that is not a plain name, nothing left to run.
    assert main([*base, "--exclude-batches", "nope"]) == EXIT_ERROR
    assert "no ledger for batch 'nope'" in capsys.readouterr().err
    assert main([*base, "--exclude-batches", "../x"]) == EXIT_USAGE
    capsys.readouterr()
    only_pilot = tmp_path / "only-pilot.json"
    only_pilot.write_text(json.dumps([
        {"listing_id": "lst_a", "source_application_url": f"{ORIGIN}/lst_a/prepared",
         "status": "resolved"}]))
    assert main(["--home", str(home), "prepare-batch", "--inventory", str(only_pilot),
                 "--exclude-batches", "pilot"]) == EXIT_USAGE
    assert "1 prepared or held by the excluded batches" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        main([*base, "--exclude-batches", ","])
    assert exc.value.code == EXIT_USAGE
