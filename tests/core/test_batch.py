"""The bulk preparation harness, offline: a fake ``apply`` command stands in for the
CLI (no browser), so classification, bounded concurrency, per-slot browser
directories, the resumable ledger, timeouts, the ``max_prepared`` bound, the
already-recorded skip and the summary are exercised without Playwright."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from interviewmaxxing_cli import batch as batch_module
from interviewmaxxing_cli.batch import (
    BatchOptions,
    BatchRow,
    LedgerEntry,
    classify,
    load_inventory,
    read_inventory,
    read_ledger,
    render_summary_markdown,
    run_batch,
)
from interviewmaxxing_cli.main import EXIT_OK, EXIT_USAGE, build_parser, main
from interviewmaxxing_core import ApplicationState, ApplicationStore, ApplyOutcome, LocalPaths

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
    return BatchRow(listing_id=listing or f"lst_{kind}", pipeline_id=None, company="Brambleway",
                    title=kind.title(), application_url=f"{ORIGIN}/{kind}", backend=backend,
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
