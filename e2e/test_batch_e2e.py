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

    # The person answers one shared question with the exact line ``holds`` gave, and the
    # other through the answer sheet (one entry per distinct question, filled in once).
    answered = cli(*shlex.split(work_auth["answer"].replace("VALUE", "wa_authorized"))[1:])
    assert answered.code == 0, answered.stderr
    sheet_path = tmp_path / "sheet.json"
    written = cli("holds", "--sheet", str(sheet_path), "--batch-id", "loop")
    assert written.code == 0, written.stderr
    assert stat.S_IMODE(sheet_path.stat().st_mode) == 0o600
    assert Q_SPONSORSHIP not in written.stdout and "held application(s) of loop to" in written.stdout
    sheet = json.loads(sheet_path.read_text())
    assert sheet["batches"] == ["loop"]  # the applications this batch holds, nothing older
    assert all(set(a["fields"]) == set(a["application_ids"]) for a in sheet["actions"])
    by_wording = {" ".join(q["question"].split()): q for q in sheet["questions"]}
    assert " ".join(Q_WORK_AUTH.split()) not in by_wording  # answered above: no longer open
    entry = by_wording[Q_SPONSORSHIP]
    assert (entry["applications"], entry["reuse"], entry["answer"]) == (3, "global", None)
    assert len(entry["fields"]) == 3 and entry["control_type"] in ("SELECT", "RADIO")
    assert {o["value"] for o in entry["options"]} >= {"no_sponsorship"}
    entry["answer"] = "no_sponsorship"
    sheet_path.write_text(json.dumps(sheet))

    # A dry run checks every application and saves nothing.
    dry = cli("answer", "--sheet", str(sheet_path), "--dry-run")
    assert dry.code == 0, dry.stderr
    assert "(dry run, nothing saved)" in dry.stdout
    assert "would save 3 answer(s) for 3 application(s)" in dry.stdout
    assert ("- 3 of 3 application(s) would receive it; options changed on 0: "
            + Q_SPONSORSHIP) in dry.stdout.splitlines()
    assert cli("holds", "--json").json()["answered"] == 0  # nothing saved by the dry run
    applied = cli("answer", "--sheet", str(sheet_path))
    assert applied.code == 0, applied.stderr
    assert "saved 3 answer(s) for 3 application(s)" in applied.stdout
    assert applied.stdout.rstrip().endswith("prepare-batch --retry loop")  # the sheet's batch
    assert "no_sponsorship" not in applied.stdout
    again = cli("answer", "--sheet", str(sheet_path), "--batch-id", "loop")
    assert again.code == 0 and "saved 0 answer(s) for 0 application(s)" in again.stdout
    assert "already answered on 3" in again.stdout
    after = cli("holds", "--json")
    assert (after.json()["answered"], after.json()["held"]) == (2, 1)
    assert set(_groups(after)) == {Q_NOTICE, *(q for q in groups if q not in (
        " ".join(Q_WORK_AUTH.split()), Q_SPONSORSHIP))}  # only those two were answered

    # Without --all: the sheet's answers count as answered, so all three run again.
    final = _summary(cli("prepare-batch", "--retry", "loop", "--batch-id", "loop-r2", "--json",
                         timeout=600))
    assert final["retry"]["selected"] == 3 and final["retry"]["skipped"] == {}
    assert final["retry"]["selected_by"] == {"answered since the stop": 3}
    assert final["totals"] == {"prepared": 2, "needs_input": 1}
    assert (final["retry"]["prepared"], final["retry"]["holds_cleared"]) == (2, 6)
    ledger = [json.loads(line) for line in
              (home / "batches" / "loop-r2" / "ledger.jsonl").read_text().splitlines()]
    assert {line["listing_id"]: (line["retry_of"], line["previous_outcome"], line["outcome"])
            for line in ledger} == {
        "lst_validation": ("loop", "needs_input", "prepared"),
        "lst_standard": ("loop", "needs_input", "prepared"),
        "lst_missing-required": ("loop", "needs_input", "needs_input")}

    # One application again after a (pretend) fix, without --all.
    held_app = next(line["application_id"] for line in ledger
                    if line["listing_id"] == "lst_missing-required")
    only = _summary(cli("prepare-batch", "--retry", "loop", "--only-app", held_app,
                        "--batch-id", "loop-r3", "--json", timeout=600))
    assert (only["launched"], only["retry"]["selected_by"]) == (1, {"--only-app": 1})
    assert only["totals"] == {"needs_input": 1}

    combined = _summary(cli("batch-report", "loop", "loop-r2", "--json"))
    assert combined["rows"] == 3 and combined["totals"] == {"prepared": 2, "needs_input": 1}
    family = _summary(cli("batch-report", "--family", "loop", "--json"))
    [runs] = [f["runs"] for f in family["families"]]
    assert [(r["batch_id"], r["ran"], r["prepared_total"], r["held"]) for r in runs] == [
        ("loop", 3, 0, 3), ("loop-r1", 1, 0, 3), ("loop-r2", 3, 2, 1), ("loop-r3", 1, 2, 1)]
    ready = {r["listing_id"]: r for r in family["ready"]}
    assert set(ready) == {"lst_validation", "lst_standard"}
    for row in ready.values():
        assert row["approve"] == f"interviewmaxxing approve {row['application_id']}"
        assert row["submit"] == (f"IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit "
                                 f"{row['application_id']} --yes")
    text = cli("batch-report", "--family", "loop")
    assert text.code == 0 and "## Yield over retries" in text.stdout
    assert "## At the final review step" in text.stdout

    # The mass run over a whole inventory leaves out what this batch prepared or held.
    mass_inventory = tmp_path / "mass-inventory.json"
    mass_inventory.write_text(json.dumps([
        {"listing_id": f"lst_{job}", "company": catalog[job]["company"],
         "title": catalog[job]["title"], "source_application_url": ats.url(job),
         "backend": "mock", "status": "resolved"} for job in [*RETRY_JOBS, "attestation"]]))
    mass = _summary(cli("prepare-batch", "--inventory", str(mass_inventory), "--exclude-batches",
                        "loop", "--batch-id", "mass", "--json", timeout=600))
    assert mass["skipped_excluded"] == 3 and mass["launched"] == 1
    assert mass["excluded_batches"] == ["loop", "loop-r1", "loop-r2", "loop-r3"]
    assert [json.loads(line)["listing_id"] for line in
            (home / "batches" / "mass" / "ledger.jsonl").read_text().splitlines()] == [
        "lst_attestation"]
    assert ats.submissions("attestation")["accepted_count"] == 0
    for job in RETRY_JOBS:
        assert ats.submissions(job)["accepted_count"] == 0, job


def test_the_saved_listings_location_reaches_every_batch_job(ats: MockServer, cli: Cli,
                                                               home: Path, profile: Path,
                                                               tmp_path: Path):
    """WP9 round 5: prepare-batch looks each row's listing up in the jobs store and passes
    its location to the run (``apply --job-location``). The job keeps it (these apply pages
    state no place), the ledger records it, and a retry passes it again. Nothing is
    submitted."""
    from datetime import UTC, datetime

    from interviewmaxxing_core.store import LOCATION_EVENT
    from interviewmaxxing_jobs.sources.base import make_listing
    from interviewmaxxing_jobs.store import JobStore

    places = {"standard": "Round Rock, TX (Hybrid)", "missing-required": "Remote (US)"}
    jobs = JobStore(home / "jobs" / "jobs.sqlite3")  # the fictional saved listings
    catalog = {job["job_id"]: job for job in ats.get("/__test__/jobs")["jobs"]}
    rows = []
    for n, (job, place) in enumerate(places.items()):
        posting = f"https://jobs.linkedin.test/view/49000000{n}"
        saved = jobs.upsert(make_listing(
            source="linkedin", source_listing_id=f"49000000{n}", posting_url=posting,
            source_url=posting, title=catalog[job]["title"], company=catalog[job]["company"],
            location=place, observed_at=datetime(2026, 9, 25, tzinfo=UTC), query_id="qry_e2e",
            evidence="fictional listing"))
        rows.append({"listing_id": saved.id, "company": catalog[job]["company"],
                     "title": catalog[job]["title"], "source_application_url": ats.url(job),
                     "backend": "mock", "status": "resolved"})
    jobs.close()
    inventory = tmp_path / "located-inventory.json"
    inventory.write_text(json.dumps(rows))

    first = cli("prepare-batch", "--inventory", str(inventory), "--workers", "2",
                "--batch-id", "located", "--json", timeout=600)
    assert first.code == 0, first.stderr
    assert "2 with their saved listing's location" in first.stderr
    assert first.json()["totals"] == {"prepared": 1, "needs_input": 1}
    lines = [json.loads(x) for x in (home / "batches" / "located" / "ledger.jsonl")
             .read_text().splitlines()]
    by_job = {line["application_url"].split("/jobs/")[1].split("/")[0]: line for line in lines}
    assert {job: line["location"] for job, line in by_job.items()} == places
    with ApplicationStore.open(home / "state" / "imx.sqlite3") as store:
        for job, line in by_job.items():
            app = store.get_application(line["application_id"])
            assert store.get_job(app.job_id).location == places[job], job
            assert [(e.metadata["source"], e.metadata["location"])
                    for e in store.list_events(app.id) if e.event == LOCATION_EVENT] == [
                ("listing", places[job])]

    retried = cli("prepare-batch", "--retry", "located", "--batch-id", "located-r1", "--all",
                  "--json", timeout=600)
    assert retried.code == 0, retried.stderr
    assert retried.json()["retry"]["selected"] == 1  # the held one; the prepared one never
    [again] = [json.loads(x) for x in (home / "batches" / "located-r1" / "ledger.jsonl")
               .read_text().splitlines()]
    assert (again["location"], again["retry_of"]) == ("Remote (US)", "located")
    for job in places:
        assert ats.submissions(job)["accepted_count"] == 0, job
