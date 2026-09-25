"""Bulk preparation end to end: ``run_batch`` drives the installed CLI (one process per
job) and real headless Chromium against the separately running localhost mock ATS,
three workers at a time, with the fictional candidate. Nothing is ever submitted."""

from __future__ import annotations

import asyncio
import json
import shlex
import stat
from pathlib import Path
from typing import Any

import pytest
from support import Q_SPONSORSHIP, Q_WORK_AUTH, Cli, MockServer

from interviewmaxxing_cli.batch import BatchOptions, LedgerEntry, load_inventory, run_batch
from interviewmaxxing_core import ApplicationState, ApplicationStore, LocalPaths

pytestmark = pytest.mark.slow

JOBS = ["standard", "multistep", "missing-required", "attestation", "agreement", "validation"]


def _widen_saved_answers(profile_path: Path, ats: MockServer) -> None:
    """Cover the multistep job's job-scoped "why" question too. Saved answers match
    the bare question wording (section headings are model context, not wording), so
    no other change is needed. Values are unchanged; nothing new is answered."""
    profile = json.loads(profile_path.read_text())
    why = next(a for a in profile["saved_answers"] if a["id"] == "sa.why")
    profile["saved_answers"].append({**why, "id": "sa.why.multistep",
                                     "job_url": ats.url("multistep")})
    profile_path.write_text(json.dumps(profile, indent=2))


def _inventory(tmp_path: Path, ats: MockServer) -> Path:
    catalog = {job["job_id"]: job for job in ats.get("/__test__/jobs")["jobs"]}
    rows = [{"listing_id": f"lst_{job}", "pipeline_id": None,
             "company": catalog[job]["company"], "title": catalog[job]["title"],
             "source_application_url": ats.url(job), "backend": "mock", "status": "resolved",
             "evidence_url": ats.posting(job)} for job in JOBS]
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(rows, indent=2))
    return path


def test_batch_prepares_without_submitting(ats: MockServer, home: Path, profile: Path,
                                           tmp_path: Path):
    _widen_saved_answers(profile, ats)
    rows = load_inventory(_inventory(tmp_path, ats))
    assert [r.listing_id for r in rows] == [f"lst_{job}" for job in JOBS]
    paths = LocalPaths.from_env({}, home=home)
    options = BatchOptions(paths=paths, candidate_id="default", batch_id="e2e", workers=3)
    assert options.headless and options.browser == "playwright" and not options.ai_routing

    entries: list[LedgerEntry] = []
    summary = asyncio.run(run_batch(options, rows, on_entry=entries.append))

    for job in JOBS:
        assert ats.submissions(job)["accepted_count"] == 0, job
    by_job = {e.listing_id.removeprefix("lst_"): e for e in entries}
    assert set(by_job) == set(JOBS) and len(entries) == len(JOBS)
    # validation: the fixture phone is rejected by the server only on submit, which
    # preparation never dispatches, so the form is prepared like the standard one.
    for job in ("standard", "multistep", "validation"):
        assert by_job[job].outcome == "prepared", (job, by_job[job].message)
        assert by_job[job].state is ApplicationState.NEEDS_INPUT
        assert by_job[job].missing_reasons == []
    # agreement: both "I agree" checkboxes are personal attestations/consent the
    # candidate never saved, so they stop the run as questions, like attestation.
    for job in ("missing-required", "attestation", "agreement"):
        assert by_job[job].outcome == "needs_input", (job, by_job[job].message)
        assert by_job[job].state is ApplicationState.NEEDS_INPUT
    assert "UNCOVERED_ATTESTATION" in by_job["attestation"].missing_reasons
    assert "UNCOVERED_ATTESTATION" in by_job["agreement"].missing_reasons
    assert {"NO_ANSWER", "EXPLICIT_ANSWER_REQUIRED"} <= set(by_job["missing-required"].missing_reasons)
    assert all(e.exit_code == 3 and e.attempt == 1 and e.application_id for e in entries)
    assert {e.worker_slot for e in entries} <= {0, 1, 2}

    with ApplicationStore.open(paths.state_db) as store:
        for job, entry in by_job.items():
            app = store.get_application(entry.application_id)
            assert app.state is ApplicationState.NEEDS_INPUT, job
            assert store.list_attempts(app.id) == []
            events = [e.event for e in store.list_events(app.id)]
            assert "application.submitting" not in events
            assert ("preparation.ready" in events) == (entry.outcome == "prepared"), job
            if entry.outcome == "prepared":
                ready = [e for e in store.list_events(app.id) if e.event == "preparation.ready"]
                assert ready[-1].metadata["submitted"] is False
                assert any(paths.application_artifacts(app.id).iterdir())

    batch_dir = home / "batches" / "e2e"
    ledger = batch_dir / "ledger.jsonl"
    assert len(ledger.read_text().splitlines()) == len(JOBS)
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
    assert summary.totals == {"prepared": 3, "needs_input": 3}
    assert summary.by_backend == {"mock": {"prepared": 3, "needs_input": 3}}
    assert summary.launched == 6 and summary.skipped_settled == 0
    assert sorted(summary.application_ids["prepared"]) == sorted(
        by_job[j].application_id for j in ("standard", "multistep", "validation"))
    written = json.loads((batch_dir / "summary.json").read_text())
    assert written["totals"] == summary.totals
    workers = home / "browser-workers"
    used = {workers / f"w{slot}" for slot in {e.worker_slot for e in entries}}
    assert used and all(d.is_dir() and stat.S_IMODE(d.stat().st_mode) == 0o700 for d in used)
    assert not (home / "browser").exists() or not any((home / "browser").iterdir())

    # Running the same batch again launches nothing and still submits nothing.
    again = asyncio.run(run_batch(options, rows, on_entry=entries.append))
    assert again.launched == 0 and again.skipped_settled == 6 and again.totals == summary.totals
    assert len(entries) == len(JOBS)
    for job in JOBS:
        assert ats.submissions(job)["accepted_count"] == 0


RETRY_JOBS = ["validation", "standard", "missing-required"]
Q_NOTICE = "What is your notice period?"


def _without_saved_answers(profile_path: Path, *answer_ids: str) -> None:
    """The fictional candidate never saved these answers: the jobs hold on them."""
    profile = json.loads(profile_path.read_text())
    profile["saved_answers"] = [a for a in profile["saved_answers"] if a["id"] not in answer_ids]
    profile_path.write_text(json.dumps(profile, indent=2))


def _groups(result: Any) -> dict[str, dict[str, Any]]:
    assert result.code == 0, result.stderr
    return {" ".join(g["question"].split()): g for g in result.json()["questions"]}


def _summary(result: Any) -> dict[str, Any]:
    assert result.code == 0, result.stderr
    return result.json()  # type: ignore[no-any-return]


def test_retry_after_answering_each_shared_question_once(ats: MockServer, cli: Cli, home: Path,
                                                         profile: Path, tmp_path: Path):
    """The mass-preparation loop against the mock ATS: a batch holds three applications on
    the same explicit questions; ``holds`` groups them with one answer line each; a retry
    skips the applications held only on explicit questions; answering each question once
    with --reuse global lets the next retry prepare them. Nothing is submitted."""
    _without_saved_answers(profile, "sa.work_auth", "sa.sponsorship")
    catalog = {job["job_id"]: job for job in ats.get("/__test__/jobs")["jobs"]}
    inventory = tmp_path / "retry-inventory.json"
    inventory.write_text(json.dumps([
        {"listing_id": f"lst_{job}", "company": catalog[job]["company"],
         "title": catalog[job]["title"], "source_application_url": ats.url(job),
         "backend": "mock", "status": "resolved"} for job in RETRY_JOBS]))

    first = _summary(cli("prepare-batch", "--inventory", str(inventory), "--workers", "3",
                         "--batch-id", "loop", "--json", timeout=600))
    assert first["totals"] == {"needs_input": 3}
    assert first["run_options"]["workers"] == 3

    groups = _groups(cli("holds", "--json"))
    work_auth, sponsorship = groups[" ".join(Q_WORK_AUTH.split())], groups[Q_SPONSORSHIP]
    for group in (work_auth, sponsorship):
        assert (group["holds"], group["applications"], group["reason"]) == \
            (3, 3, "EXPLICIT_ANSWER_REQUIRED")
        assert group["answer"].startswith("interviewmaxxing answer app_")
        assert group["answer"].endswith("=VALUE --reuse global")
    assert groups[Q_NOTICE]["applications"] == 1

    report = _summary(cli("batch-report", "loop", "--json"))
    assert {q["question"]: q["holds"] for q in report["questions"]}[Q_SPONSORSHIP] == 3
    assert report["backends"] == [{
        "backend": "mock", "applications": 3, "prepared": 0, "needs_input": 3, "failed": 0,
        "closed": 0, "no_form": 0, "other": 0, "prepared_rate": 0.0,
        "median_duration_s": report["backends"][0]["median_duration_s"],
        "provider_cost_usd": None}]

    # Nothing is answered yet, so a default retry runs nothing. After a fix, --all runs the
    # held applications again, except the two held on explicit questions alone.
    idle = _summary(cli("prepare-batch", "--retry", "loop", "--batch-id", "loop-r0", "--json",
                        timeout=600))
    assert idle["launched"] == 0
    assert idle["retry"]["skipped"] == {"nothing answered since the stop": 3}
    early = _summary(cli("prepare-batch", "--retry", "loop", "--batch-id", "loop-r1", "--all",
                         "--json", timeout=600))
    assert early["retry"]["skipped"] == {"explicit answers only": 2}
    assert early["retry"]["selected"] == 1 and early["totals"] == {"needs_input": 1}
    assert early["retry"]["holds_cleared"] == 0

    # The person answers each shared question once, with the exact lines ``holds`` gave.
    for group, value in ((work_auth, "wa_authorized"), (sponsorship, "no_sponsorship")):
        answered = cli(*shlex.split(group["answer"].replace("VALUE", value))[1:])
        assert answered.code == 0, answered.stderr
    after = cli("holds", "--json")
    assert (after.json()["answered"], after.json()["held"]) == (2, 1)
    assert set(_groups(after)) == {Q_NOTICE, *(q for q in groups if q not in (
        " ".join(Q_WORK_AUTH.split()), Q_SPONSORSHIP))}  # only those two were answered

    final = _summary(cli("prepare-batch", "--retry", "loop", "--batch-id", "loop-r2", "--json",
                         timeout=600))
    assert final["retry"]["selected"] == 3 and final["retry"]["skipped"] == {}
    assert final["totals"] == {"prepared": 2, "needs_input": 1}
    assert (final["retry"]["prepared"], final["retry"]["holds_cleared"]) == (2, 6)
    ledger = [json.loads(line) for line in
              (home / "batches" / "loop-r2" / "ledger.jsonl").read_text().splitlines()]
    assert {line["listing_id"]: (line["retry_of"], line["previous_outcome"], line["outcome"])
            for line in ledger} == {
        "lst_validation": ("loop", "needs_input", "prepared"),
        "lst_standard": ("loop", "needs_input", "prepared"),
        "lst_missing-required": ("loop", "needs_input", "needs_input")}

    combined = _summary(cli("batch-report", "loop", "loop-r2", "--json"))
    assert combined["rows"] == 3 and combined["totals"] == {"prepared": 2, "needs_input": 1}
    for job in RETRY_JOBS:
        assert ats.submissions(job)["accepted_count"] == 0, job
