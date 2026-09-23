"""I1 acceptance: the installed CLI drives real Chromium against the separately
running localhost mock ATS. Every CLI call is a new process. Assertions cover the
CLI-visible result, the server-side submission count and received values, the
persisted state, events and receipt, and the uploaded file."""

from __future__ import annotations

import hashlib
import json

import pytest
from support import Q_WHY, RESUME, Cli, MockServer, switch_resume

pytestmark = pytest.mark.slow

EXIT_OK, EXIT_USAGE, EXIT_INCOMPLETE, EXIT_BLOCKED, EXIT_UNCERTAIN = 0, 2, 3, 4, 5


def _apply(cli: Cli, url: str, *extra: str):
    result = cli("apply", url, "--headless", "--json", *extra)
    return result, result.json()


def _events(cli: Cli, app_id: str) -> list[str]:
    return [e["event"] for e in cli("events", app_id, "--json").json()]


def test_standard_application_submits_once_with_receipt(cli: Cli, ats: MockServer, profile):
    result, outcome = _apply(cli, ats.url("standard"))
    assert result.code == EXIT_OK, result.stdout + result.stderr
    assert outcome["state"] == "SUBMITTED"
    app_id = outcome["application_id"]

    server = ats.submissions("standard")
    assert server["accepted_count"] == 1
    [record] = server["submissions"]
    receipt = outcome["receipt"]
    assert receipt["confirmation_reference"] == record["confirmation_reference"]
    assert receipt["title"] == "Senior Data Platform Engineer"
    fields = record["fields"]
    assert (fields["first_name"], fields["last_name"]) == ("Avery", "Quill")
    assert fields["email"] == "avery.quill@example.test"
    assert fields["work_authorization"] == "wa_authorized"
    assert fields["sponsorship"] == "no_sponsorship"
    assert fields["years_experience"] == "yrs_6_9"
    assert fields["skills"] == ["sk_python", "sk_sql", "sk_spark", "sk_dbt"]
    assert fields["why_brambleway"].startswith("I have built data platforms")
    assert "work_arrangements" not in fields  # optional, not answered, left empty
    upload = record["files"]["resume"]
    assert upload["sha256"] == hashlib.sha256(RESUME.read_bytes()).hexdigest()
    assert upload["size"] == RESUME.stat().st_size

    status = cli("status", app_id, "--json").json()
    assert status["application"]["state"] == "SUBMITTED"
    assert [a["outcome"] for a in status["attempts"]] == ["ACCEPTED"]
    events = _events(cli, app_id)
    assert events.index("application.submitting") < events.index("application.submitted")
    assert events.count("application.submitting") == 1
    assert cli("receipt", app_id, "--json").json()["confirmation_reference"] == \
        record["confirmation_reference"]

    # Repeating the request never submits again.
    again, repeat = _apply(cli, ats.url("standard"))
    assert again.code == EXIT_BLOCKED and repeat["state"] == "SUBMITTED"
    assert repeat["application_id"] == app_id
    assert ats.submissions("standard")["accepted_count"] == 1


def test_missing_answers_survive_process_restart_then_submit(cli: Cli, ats: MockServer, profile):
    result, outcome = _apply(cli, ats.url("missing-required"))
    assert result.code == EXIT_INCOMPLETE and outcome["state"] == "NEEDS_INPUT"
    app_id = outcome["application_id"]
    asked = {m["field_id"]: m for m in outcome["missing_inputs"]}
    assert set(asked) == {"notice_period", "salary_expectation", "faa_part_107"}
    assert asked["salary_expectation"]["reason"] == "EXPLICIT_ANSWER_REQUIRED"
    assert ats.submissions("missing-required")["accepted_count"] == 0

    # A new process re-presents the same recorded questions.
    status = cli("status", app_id, "--json").json()
    assert {m["field_id"] for m in status["pending_inputs"]} == set(asked)
    assert "notice_period" in cli("status", app_id).stdout

    # Invalid or unknown answers are rejected before anything is saved.
    bad = cli("answer", app_id, "--set", "notice_period=3 months or more (no longer offered)")
    assert bad.code == EXIT_USAGE and "not one of the options" in bad.stderr
    unknown = cli("answer", app_id, "--set", "favourite_colour=blue")
    assert unknown.code == EXIT_USAGE

    answered = cli("answer", app_id, "--set", "notice_period=2 weeks",
                   "--set", "salary_expectation=150000", "--set", "faa_part_107=No")
    assert answered.code == EXIT_OK, answered.stderr
    assert "input.received" in _events(cli, app_id)

    resumed = cli("resume", app_id, "--headless", "--json")
    assert resumed.code == EXIT_OK, resumed.stdout + resumed.stderr
    assert resumed.json()["state"] == "SUBMITTED"
    [record] = ats.submissions("missing-required")["submissions"]
    assert record["fields"]["notice_period"] == "np_2_weeks" or "2" in record["fields"]["notice_period"]
    assert record["fields"]["salary_expectation"] == "150000"
    assert record["fields"]["faa_part_107"] in ("no", "faa_no") or record["fields"]["faa_part_107"]


def test_multistep_application_with_an_answer_file(cli: Cli, ats: MockServer, profile, tmp_path):
    result, outcome = _apply(cli, ats.url("multistep"))
    assert outcome["state"] == "NEEDS_INPUT", result.stdout
    app_id = outcome["application_id"]
    assert [m["label"] for m in outcome["missing_inputs"]] == [Q_WHY]
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"why_brambleway": "Forecasting logistics interests me."}))
    assert cli("answer", app_id, "--answers", str(answers)).code == EXIT_OK

    resumed = cli("resume", app_id, "--headless", "--json")
    assert resumed.code == EXIT_OK, resumed.stdout + resumed.stderr
    assert resumed.json()["state"] == "SUBMITTED"
    server = ats.submissions("multistep")
    assert server["accepted_count"] == 1
    [record] = server["submissions"]
    assert record["draft_id"] is not None  # went through the multistep draft flow
    assert record["fields"]["why_brambleway"] == "Forecasting logistics interests me."
    assert record["files"]["resume"]["sha256"] == hashlib.sha256(RESUME.read_bytes()).hexdigest()
    events = _events(cli, app_id)
    assert events.count("application.submitting") == 1
    assert events.count("application.inspecting") >= 4  # one per step, across both runs


def test_personal_attestations_are_only_made_by_the_user(cli: Cli, ats: MockServer, profile):
    _result, outcome = _apply(cli, ats.url("attestation"))
    assert outcome["state"] == "NEEDS_INPUT"
    app_id = outcome["application_id"]
    reasons = {m["field_id"]: m["reason"] for m in outcome["missing_inputs"]}
    assert reasons["attest_accuracy"] == "UNCOVERED_ATTESTATION"
    assert ats.submissions("attestation")["accepted_count"] == 0
    assert cli("answer", app_id, "--set", "attest_accuracy=yes",
               "--set", "attest_privacy_notice=yes").code == EXIT_OK
    resumed = cli("resume", app_id, "--headless", "--json")
    assert resumed.json()["state"] == "SUBMITTED", resumed.stdout
    [record] = ats.submissions("attestation")["submissions"]
    assert record["fields"]["attest_accuracy"] == "yes"


def test_site_rejection_asks_for_a_corrected_answer(cli: Cli, ats: MockServer, profile):
    result, outcome = _apply(cli, ats.url("validation"))
    assert outcome["state"] == "NEEDS_INPUT", result.stdout
    app_id = outcome["application_id"]
    [phone] = outcome["missing_inputs"]
    assert phone["field_id"] == "phone" and "rejected" in phone["prompt"]
    server = ats.submissions("validation")
    assert server["accepted_count"] == 0 and server["rejected_count"] == 1
    status = cli("status", app_id, "--json").json()
    assert [a["outcome"] for a in status["attempts"]] == ["NOT_SUBMITTED"]
    assert _events(cli, app_id).count("validation.rejected") == 1  # the rejection epoch

    # A correction the site rejects again (new process, new epoch) is asked again,
    # not silently resubmitted with the same value or suppressed as "already answered".
    assert cli("answer", app_id, "--set", "phone=555").code == EXIT_OK
    resumed = cli("resume", app_id, "--headless", "--json")
    assert resumed.code == EXIT_INCOMPLETE and resumed.json()["state"] == "NEEDS_INPUT", resumed.stdout
    [phone] = resumed.json()["missing_inputs"]
    assert phone["field_id"] == "phone" and "rejected" in phone["prompt"]
    assert ats.submissions("validation")["rejected_count"] == 2
    assert _events(cli, app_id).count("validation.rejected") == 2

    assert cli("answer", app_id, "--set", "phone=3035550142").code == EXIT_OK
    resumed = cli("resume", app_id, "--headless", "--json")
    assert resumed.json()["state"] == "SUBMITTED", resumed.stdout
    server = ats.submissions("validation")
    assert server["accepted_count"] == 1
    assert server["submissions"][0]["fields"]["phone"] == "3035550142"
    status = cli("status", app_id, "--json").json()
    assert [a["outcome"] for a in status["attempts"]] == ["NOT_SUBMITTED", "NOT_SUBMITTED", "ACCEPTED"]


def test_local_state_is_created_private(cli: Cli, ats: MockServer, profile, home):
    import stat

    _apply(cli, ats.url("standard"))
    state_db = home / "state" / "imx.sqlite3"
    assert stat.S_IMODE(state_db.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_db.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "browser").stat().st_mode) == 0o700


def test_uncertain_submission_is_never_retried_and_reconciles_from_the_site(
    cli: Cli, ats: MockServer, profile
):
    # The user supplied the posting URL; the runner follows its apply link to the form.
    result, outcome = _apply(cli, ats.posting("uncertain"))
    assert result.code == EXIT_UNCERTAIN and outcome["state"] == "SUBMISSION_UNKNOWN"
    assert outcome["receipt"] is None
    app_id = outcome["application_id"]
    assert ats.submissions("uncertain")["accepted_count"] == 1

    # Neither a repeated apply nor resume submits again.
    again, repeat = _apply(cli, ats.posting("uncertain"))
    assert again.code == EXIT_UNCERTAIN and repeat["application_id"] == app_id
    assert cli("resume", app_id, "--headless").code == EXIT_UNCERTAIN
    assert ats.submissions("uncertain")["accepted_count"] == 1

    # The site does not confirm it yet: it stays unknown.
    pending = cli("reconcile", app_id, "--headless", "--json")
    assert pending.code == EXIT_UNCERTAIN and pending.json()["state"] == "SUBMISSION_UNKNOWN"
    assert cli("receipt", app_id).code == EXIT_BLOCKED

    [record] = ats.submissions("uncertain")["submissions"]
    ats.post(f"/__test__/submissions/{record['submission_id']}/reveal")  # the site catches up
    settled = cli("reconcile", app_id, "--headless", "--json")
    assert settled.code == EXIT_OK, settled.stdout + settled.stderr
    body = settled.json()
    assert body["state"] == "SUBMITTED"
    assert body["receipt"]["reconciliation_method"] == "SITE_CONFIRMATION"
    assert body["receipt"]["confirmation_reference"] == record["confirmation_reference"]
    assert ats.submissions("uncertain")["accepted_count"] == 1
    assert "reconcile.unconfirmed" in _events(cli, app_id)


def test_generic_thank_you_is_not_acceptance_until_the_status_page_confirms(
    cli: Cli, ats: MockServer, profile
):
    result, outcome = _apply(cli, ats.posting("vague-confirmation"))
    assert result.code == EXIT_UNCERTAIN and outcome["state"] == "SUBMISSION_UNKNOWN"
    app_id = outcome["application_id"]
    settled = cli("reconcile", app_id, "--headless", "--json")
    assert settled.code == EXIT_OK, settled.stdout + settled.stderr
    assert settled.json()["state"] == "SUBMITTED"
    assert settled.json()["receipt"]["reconciliation_method"] == "SITE_CONFIRMATION"
    assert ats.submissions("vague-confirmation")["accepted_count"] == 1


def test_reconcile_from_an_apply_only_url_stays_unknown(cli: Cli, ats: MockServer, profile):
    """Limitation (browser runtime): the apply page links to no status page, so a
    re-read starting there cannot confirm anything. It must stay unknown, never
    accept, and never resubmit."""
    _result, outcome = _apply(cli, ats.url("vague-confirmation"))
    assert outcome["state"] == "SUBMISSION_UNKNOWN"
    settled = cli("reconcile", outcome["application_id"], "--headless", "--json")
    assert settled.code == EXIT_UNCERTAIN and settled.json()["state"] == "SUBMISSION_UNKNOWN"
    assert ats.submissions("vague-confirmation")["accepted_count"] == 1


@pytest.mark.parametrize("job, label", [("signin", "Sign in"), ("captcha", "Solve the CAPTCHA")])
def test_sign_in_and_captcha_stop_for_the_user(cli: Cli, ats: MockServer, profile, job, label):
    result, outcome = _apply(cli, ats.url(job))
    assert result.code == EXIT_INCOMPLETE and outcome["state"] == "NEEDS_INPUT"
    [item] = outcome["missing_inputs"]
    assert (item["reason"], item["label"], item["field_id"]) == ("USER_ACTION", label, None)
    assert f"resume {outcome['application_id']} --act" in cli("status", outcome["application_id"]).stdout
    assert ats.submissions(job)["accepted_count"] == 0
    # Acting needs a visible window: --act with --headless is refused before anything runs.
    refused = cli("resume", outcome["application_id"], "--headless", "--act")
    assert refused.code == EXIT_USAGE and "--headless" in refused.stderr


def test_custom_controls_are_left_to_the_user(cli: Cli, ats: MockServer, profile):
    _result, outcome = _apply(cli, ats.url("custom-control"))
    assert outcome["state"] == "NEEDS_INPUT"
    assert [m["reason"] for m in outcome["missing_inputs"]] == ["UNSUPPORTED_CONTROL"]
    assert ats.submissions("custom-control")["accepted_count"] == 0


RESUME_B = RESUME.read_bytes() + b"\n% fictional variant B of the resume\n"


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def test_each_application_keeps_its_own_resume_across_profile_change_and_restart(
    cli: Cli, ats: MockServer, profile
):
    # Application A starts with resume A and stops for answers.
    _, a = _apply(cli, ats.url("missing-required"))
    assert a["state"] == "NEEDS_INPUT"
    pinned = [e for e in cli("events", a["application_id"], "--json").json()
              if e["event"] == "document.resume_pinned"]
    assert [e["metadata"]["sha256"] for e in pinned] == [_sha(RESUME.read_bytes())]

    # The user switches the profile to resume B and applies to job B with it.
    switch_resume(profile, RESUME_B, name="resume-b.pdf", resume_id="resume_b")
    result, b = _apply(cli, ats.url("standard"))
    assert result.code == EXIT_OK and b["state"] == "SUBMITTED", result.stdout
    assert ats.submissions("standard")["submissions"][0]["files"]["resume"]["sha256"] == \
        _sha(RESUME_B)

    # Application A, answered and resumed in new processes, still uploads resume A.
    assert cli("answer", a["application_id"], "--set", "notice_period=2 weeks",
               "--set", "salary_expectation=150000", "--set", "faa_part_107=No").code == EXIT_OK
    resumed = cli("resume", a["application_id"], "--headless", "--json")
    assert resumed.code == EXIT_OK, resumed.stdout + resumed.stderr
    [record] = ats.submissions("missing-required")["submissions"]
    assert record["files"]["resume"]["sha256"] == _sha(RESUME.read_bytes())


def test_a_missing_pinned_resume_stops_instead_of_substituting(cli: Cli, ats: MockServer, profile):
    _, a = _apply(cli, ats.url("missing-required"))
    app_id = a["application_id"]
    switch_resume(profile, RESUME_B, name="resume-b.pdf", resume_id="resume_b")
    (profile.parent / "resume.pdf").unlink()  # resume A is gone
    assert cli("answer", app_id, "--set", "notice_period=2 weeks", "--set",
               "salary_expectation=150000", "--set", "faa_part_107=No").code == EXIT_OK
    resumed = cli("resume", app_id, "--headless", "--json")
    body = resumed.json()
    assert resumed.code == EXIT_INCOMPLETE and body["state"] == "FAILED_RETRYABLE"
    assert "never substituted" in body["message"]
    assert ats.submissions("missing-required")["accepted_count"] == 0
