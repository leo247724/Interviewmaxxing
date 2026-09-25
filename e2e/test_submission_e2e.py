"""Approved submission through the installed CLI: ``approve``, ``submit`` and
``submit-approved`` drive real headless Chromium against the separately running
localhost mock ATS, with the fictional candidate, every call a new process.

What the mock received is the authority: an approved application is submitted once
with exactly the approved values; without IMX_ALLOW_SUBMISSION=1, --yes and a valid
approval nothing is submitted; a form that changed, or answers the site rejects, stop
before anything is accepted and withdraw the approval until the application is
prepared, reviewed and approved again."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from support import PREPARED, Cli, MockServer, events

from interviewmaxxing_cli.runner import MISMATCH_MESSAGE
from interviewmaxxing_core import ApplicationStore, LocalPaths

pytestmark = pytest.mark.slow

EXIT_OK, EXIT_INCOMPLETE, EXIT_BLOCKED = 0, 3, 4
TRAVEL = "Are you willing to travel to client sites up to 25% of the time?"


def _allowed(cli: Cli) -> Cli:
    """The same CLI with submission enabled for its commands."""
    allowed = Cli(Path(cli.env["IMX_HOME"]), cli.artifacts)
    allowed.env["IMX_ALLOW_SUBMISSION"] = "1"
    return allowed


def _prepared(cli: Cli, url: str) -> str:
    result = cli("apply", url, "--headless", "--json")
    outcome = result.json()
    assert result.code == EXIT_INCOMPLETE and outcome["message"].startswith(PREPARED), outcome
    return str(outcome["application_id"])


def _approve(cli: Cli, app_id: str) -> dict[str, Any]:
    result = cli("approve", app_id, "--json")
    assert result.code == EXIT_OK, result.stderr
    return result.json()  # type: ignore[no-any-return]


def _approved_fields(home: Path, approval: dict[str, Any]) -> dict[str, Any]:
    """The values the approved packets fill, as the mock records them."""
    fields: dict[str, Any] = {}
    with ApplicationStore.open(LocalPaths.from_env({}, home=home).state_db) as store:
        for step in approval["steps"]:
            for answer in store.get_packet(step["packet_id"]).answers:
                value = answer.value.model_dump()
                if value["kind"] == "text":
                    fields[answer.field_id] = value["text"]
                elif value["kind"] == "choice":
                    fields[answer.field_id] = value["value"]
                elif value["kind"] == "multi_choice":
                    fields[answer.field_id] = [c["value"] for c in value["choices"]]
                elif value["kind"] == "boolean" and value["checked"]:
                    fields[answer.field_id] = "yes"
    return fields


def _names(cli: Cli, app_id: str) -> list[str]:
    return [e["event"] for e in events(cli, app_id)]


def test_approve_then_submit_sends_exactly_the_approved_application_once(
    cli: Cli, ats: MockServer, home: Path, profile
):
    app_id = _prepared(cli, ats.url("standard"))
    text = cli("approve", app_id)
    assert text.code == EXIT_OK, text.stderr
    assert "step 1  years_experience: 6 to 9 years (saved_answer)" in text.stdout
    assert f"IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit {app_id} --yes" in text.stdout
    approval = cli("status", app_id, "--json").json()["approval"]
    assert approval is not None and approval["approver"].startswith("cli")

    # The gates: the environment variable, then --yes. Nothing reaches the site.
    refused = cli("submit", app_id, "--yes", "--headless")
    assert refused.code == EXIT_BLOCKED and "IMX_ALLOW_SUBMISSION=1" in refused.stderr
    allowed = _allowed(cli)
    refused = allowed("submit", app_id, "--headless")
    assert refused.code == EXIT_BLOCKED and "--yes" in refused.stderr
    assert ats.submissions("standard")["accepted_count"] == 0

    result = allowed("submit", app_id, "--yes", "--headless", "--json")
    outcome = result.json()
    assert result.code == EXIT_OK and outcome["state"] == "SUBMITTED", outcome["message"]
    server = ats.submissions("standard")
    assert (server["accepted_count"], server["rejected_count"]) == (1, 0)
    [record] = server["submissions"]
    assert record["fields"] == _approved_fields(home, approval)
    assert outcome["receipt"]["confirmation_reference"] == record["confirmation_reference"]
    status = cli("status", app_id, "--json").json()
    assert [a["packet_id"] for a in status["attempts"]] == [approval["packet_id"]]
    names = _names(cli, app_id)
    assert (names.index("application.approved") < names.index("application.submission_authorized")
            < names.index("application.submitting") < names.index("application.submitted"))
    assert cli("receipt", app_id).code == EXIT_OK

    # Never twice: the application is submitted.
    again = allowed("submit", app_id, "--yes", "--headless")
    assert again.code == EXIT_BLOCKED and "never submitted again" in again.stderr
    assert ats.submissions("standard")["accepted_count"] == 1


def test_a_changed_form_is_refused_until_prepared_and_approved_again(
    cli: Cli, ats: MockServer, profile
):
    allowed = _allowed(cli)
    app_id = _prepared(cli, ats.url("changed-after-prepare"))
    unapproved = allowed("submit", app_id, "--yes", "--headless")
    assert unapproved.code == EXIT_BLOCKED and "has no valid approval" in unapproved.stderr
    _approve(cli, app_id)

    result = allowed("submit", app_id, "--yes", "--headless", "--json")
    outcome = result.json()
    assert result.code == EXIT_INCOMPLETE and outcome["state"] == "NEEDS_INPUT"
    assert outcome["message"].startswith(MISMATCH_MESSAGE)
    assert f"a new required question {TRAVEL!r} appeared" in outcome["message"]
    server = ats.submissions("changed-after-prepare")
    assert (server["accepted_count"], server["rejected_count"]) == (0, 0)
    status = cli("status", app_id, "--json").json()
    assert status["approval"] is None and status["attempts"] == []
    assert cli("approve", app_id).code == EXIT_BLOCKED  # prepare again first

    # Prepared again, the new question is asked; answered, prepared, approved, submitted.
    asked = cli("resume", app_id, "--headless", "--json").json()
    assert [m["field_id"] for m in asked["missing_inputs"]] == ["travel_willingness"]
    assert cli("answer", app_id, "--set", "travel_willingness=Yes").code == EXIT_OK
    _prepared(cli, ats.url("changed-after-prepare"))
    _approve(cli, app_id)
    done = allowed("submit", app_id, "--yes", "--headless", "--json")
    assert done.code == EXIT_OK and done.json()["state"] == "SUBMITTED", done.stdout
    [record] = ats.submissions("changed-after-prepare")["submissions"]
    assert record["fields"]["travel_willingness"] == "travel_yes"


def test_answers_the_site_rejects_withdraw_the_approval_until_corrected(
    cli: Cli, ats: MockServer, profile
):
    allowed = _allowed(cli)
    app_id = _prepared(cli, ats.url("validation"))
    _approve(cli, app_id)

    result = allowed("submit", app_id, "--yes", "--headless", "--json")
    outcome = result.json()
    assert result.code == EXIT_INCOMPLETE and outcome["state"] == "NEEDS_INPUT"
    assert outcome["message"].startswith(MISMATCH_MESSAGE)
    assert "the site did not accept the approved answers" in outcome["message"]
    server = ats.submissions("validation")
    assert (server["accepted_count"], server["rejected_count"]) == (0, 1)
    status = cli("status", app_id, "--json").json()
    assert [a["outcome"] for a in status["attempts"]] == ["NOT_SUBMITTED"]
    assert status["approval"] is None

    # Preparing again asks for the rejected answer; the corrected one is submitted.
    asked = cli("resume", app_id, "--headless", "--json").json()
    assert [m["field_id"] for m in asked["missing_inputs"]] == ["phone"]
    assert cli("answer", app_id, "--set", "phone=3035550142").code == EXIT_OK
    _prepared(cli, ats.url("validation"))
    _approve(cli, app_id)
    done = allowed("submit", app_id, "--yes", "--headless", "--json")
    assert done.code == EXIT_OK and done.json()["state"] == "SUBMITTED", done.stdout
    [record] = ats.submissions("validation")["submissions"]
    assert record["fields"]["phone"] == "3035550142"


def _widen_saved_answers(profile_path: Path, ats: MockServer) -> None:
    """Cover the multistep job's job-scoped "why" question too (as test_batch_e2e)."""
    data = json.loads(profile_path.read_text())
    why = next(a for a in data["saved_answers"] if a["id"] == "sa.why")
    data["saved_answers"].append({**why, "id": "sa.why.multistep", "job_url": ats.url("multistep")})
    profile_path.write_text(json.dumps(data, indent=2))


def test_submit_approved_submits_a_batch_s_approved_applications_and_reports_them(
    cli: Cli, ats: MockServer, home: Path, profile, tmp_path: Path
):
    _widen_saved_answers(profile, ats)
    jobs = ["standard", "multistep", "changed-after-prepare", "missing-required"]
    rows = [{"listing_id": f"lst_{job}", "pipeline_id": None, "company": "Brambleway Analytics",
             "title": job, "source_application_url": ats.url(job), "backend": "mock",
             "status": "resolved"} for job in jobs]
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(rows))
    prepared = cli("prepare-batch", "--inventory", str(inventory), "--batch-id", "e2e-sub",
                   "--workers", "2", "--json", timeout=600)
    assert prepared.code == EXIT_OK, prepared.stderr
    summary = prepared.json()
    assert summary["totals"] == {"prepared": 3, "needs_input": 1}
    ids = {job: next(i for i in summary["application_ids"]["prepared"]
                     if cli("status", i, "--json").json()["requests"][0]["application_url"]
                     == ats.url(job)) for job in ("standard", "multistep", "changed-after-prepare")}
    for app_id in ids.values():
        _approve(cli, app_id)

    allowed = _allowed(cli)
    refused = cli("submit-approved", "--batch", "e2e-sub", "--slots", "2", "--yes")
    assert refused.code == EXIT_BLOCKED  # no IMX_ALLOW_SUBMISSION
    result = allowed("submit-approved", "--batch", "e2e-sub", "--slots", "2", "--yes", "--json",
                     timeout=600)
    assert result.code == EXIT_INCOMPLETE, result.stderr  # one form changed
    report = result.json()
    assert report["totals"] == {"submitted": 2, "needs_input": 1}
    assert sorted(report["application_ids"]["submitted"]) == sorted(
        [ids["standard"], ids["multistep"]])
    assert report["application_ids"]["needs_input"] == [ids["changed-after-prepare"]]
    for job in ("standard", "multistep"):
        assert ats.submissions(job)["accepted_count"] == 1, job
    changed = ats.submissions("changed-after-prepare")
    assert (changed["accepted_count"], changed["rejected_count"]) == (0, 0)
    assert ats.submissions("missing-required")["accepted_count"] == 0  # never approved
    # The multistep application was filled on a new draft, step by step, from its
    # approved packets, and submitted once.
    [record] = ats.submissions("multistep")["submissions"]
    approval = cli("status", ids["multistep"], "--json").json()["attempts"]
    assert len(approval) == 1
    assert len(ats.drafts("multistep")) == 2 and record["draft_id"] == ats.drafts("multistep")[-1]["draft_id"]

    ledger = [json.loads(line) for line in
              (home / "batches" / "e2e-sub" / "ledger.jsonl").read_text().splitlines()]
    submitted = [line for line in ledger if line.get("kind") == "submission"]
    assert len(submitted) == 3 and len(ledger) == len(jobs) + 3
    for line in submitted:
        if line["outcome"] == "submitted":
            assert line["receipt_id"].startswith("sub_") and line["confirmation_reference"]
            assert line["approved_packet_id"] and line["exit_code"] == 0

    text = cli("batch-report", "e2e-sub")
    assert text.code == EXIT_OK and "## Submissions" in text.stdout
    assert "| submitted | 2 |" in text.stdout and "| needs_input | 1 |" in text.stdout
    as_json = cli("batch-report", "e2e-sub", "--json").json()
    assert as_json["totals"] == {"prepared": 3, "needs_input": 1}
    assert as_json["submissions"]["totals"] == {"submitted": 2, "needs_input": 1}

    # Nothing is left to submit: the rest were submitted or are no longer approved.
    again = allowed("submit-approved", "--all-approved", "--yes")
    assert again.code == EXIT_OK and "No approved application to submit" in again.stdout
    for job in ("standard", "multistep"):
        assert ats.submissions(job)["accepted_count"] == 1, job
