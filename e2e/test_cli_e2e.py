"""I1 acceptance of the prepare-only CLI: the installed ``interviewmaxxing`` entry point
drives real headless Chromium against the separately running localhost mock ATS.
Every CLI call is a new process. Assertions cover the CLI-visible result, the
persisted state and events, what the mock ATS actually received, and the boundary
that nothing is ever submitted.

``apply``/``resume`` are prepare-only (docs/application-preparation.md): a complete
form stops at the final review step as NEEDS_INPUT (exit 3) with a
``preparation.ready`` event, no submission attempt and no receipt. The previous
version of this file was written against a submitting CLI; it maps as follows:

  old test                                                     -> new test
  test_standard_application_submits_once_with_receipt
      -> test_standard_application_is_prepared_and_never_submitted
         + test_repeated_apply_of_a_prepared_application_reuses_it
  test_missing_answers_survive_process_restart_then_submit
      -> test_missing_answers_survive_process_restart_then_prepare
  test_multistep_application_with_an_answer_file
      -> test_multistep_application_with_an_answer_file_stops_at_review
  test_personal_attestations_are_only_made_by_the_user
      -> same name, parametrized over the attestation and agreement jobs
  test_site_rejection_asks_for_a_corrected_answer
      -> test_server_side_validation_cannot_fire_before_submission
  test_local_state_is_created_private                          -> unchanged
  test_uncertain_submission_is_never_retried_and_reconciles_from_the_site   }
  test_generic_thank_you_is_not_acceptance_until_the_status_page_confirms   }
  test_reconcile_from_an_apply_only_url_stays_unknown                       }
      -> test_reconcile_refuses_an_application_that_was_never_submitted
  test_sign_in_and_captcha_stop_for_the_user
      -> unchanged, plus test_captcha_widget_is_prepared_with_the_captcha_pending
  test_custom_controls_are_left_to_the_user
      -> same name; the ARIA combobox is now inspected as an accessible SELECT
  test_each_application_keeps_its_own_resume_across_profile_change_and_restart
      -> same name; application A is the multistep form so the mock's draft records
         which resume was actually uploaded before the run stopped
  test_a_missing_pinned_resume_stops_instead_of_substituting   -> unchanged

Dropped because they cannot exist without a dispatched submit: the SUBMITTED state
with a receipt, the site rejecting a posted value (validation epochs and corrected
re-submits), SUBMISSION_UNKNOWN after an uncertain or vague confirmation, and
reconciliation from the public status page. Those paths stay covered at runner level
with a synthetic browser (tests/core/test_runner.py) until submission is
re-authorized; their end-to-end coverage returns here when it is.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from support import (
    PREPARED,
    Q_WHY,
    RESUME,
    Cli,
    MockServer,
    event_names,
    events,
    preparation_ready,
    switch_resume,
)

pytestmark = pytest.mark.slow

EXIT_OK, EXIT_USAGE, EXIT_INCOMPLETE, EXIT_BLOCKED = 0, 2, 3, 4

SUBMISSION_EVENTS = {"application.submitting", "application.submitted"}
"""Never recorded by a prepare-only run."""


def _apply(cli: Cli, url: str, *extra: str):
    result = cli("apply", url, "--headless", "--json", *extra)
    return result, result.json()


def _resume(cli: Cli, app_id: str):
    result = cli("resume", app_id, "--headless", "--json")
    return result, result.json()


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _assert_prepared(cli: Cli, ats: MockServer, job: str, result, outcome, *,
                     runs: int = 1) -> dict:
    """The run ended at the final review step and nothing was submitted: the CLI
    result, the persisted record and the mock's counters all agree. Returns the
    metadata of the latest ``preparation.ready`` event (``runs`` = how many runs
    reached the review step so far)."""
    assert result.code == EXIT_INCOMPLETE, result.stdout + result.stderr
    assert outcome["state"] == "NEEDS_INPUT" and outcome["missing_inputs"] == []
    assert outcome["message"].startswith(PREPARED), outcome["message"]
    assert outcome["receipt"] is None
    app_id = outcome["application_id"]

    names = event_names(cli, app_id)
    assert names.index("application.preparation_only") < names.index("application.inspecting")
    assert not SUBMISSION_EVENTS & set(names)
    ready = preparation_ready(cli, app_id)
    assert len(ready) == runs and all(r["submitted"] is False for r in ready)

    server = ats.submissions(job)
    assert (server["accepted_count"], server["rejected_count"]) == (0, 0)
    status = cli("status", app_id, "--json").json()
    assert status["application"]["state"] == "NEEDS_INPUT"
    assert status["pending_inputs"] == [] and status["attempts"] == []
    return ready[-1]


def test_standard_application_is_prepared_and_never_submitted(cli: Cli, ats: MockServer, profile):
    result, outcome = _apply(cli, ats.url("standard"))
    ready = _assert_prepared(cli, ats, "standard", result, outcome)
    app_id = outcome["application_id"]
    assert ready["captcha_pending"] is False
    assert (ready["form_url"], ready["form_step"]) == (ats.url("standard"), 0)
    assert ready["form_fingerprint"]

    # The filled values live in the packet the review step was prepared from; the
    # mock never received a POST.
    status = cli("status", app_id, "--json").json()
    assert status["application"]["packet_id"] == ready["packet_id"]
    packet = status["packet"]
    assert packet["missing_inputs"] == []
    values = {a["field_id"]: a["value"] for a in packet["answers"]}
    assert (values["first_name"]["text"], values["last_name"]["text"]) == ("Avery", "Quill")
    assert values["email"]["text"] == "avery.quill@example.test"
    assert values["work_authorization"]["value"] == "wa_authorized"
    assert values["sponsorship"]["value"] == "no_sponsorship"
    assert values["years_experience"]["value"] == "yrs_6_9"
    assert [c["value"] for c in values["skills"]["choices"]] == [
        "sk_python", "sk_sql", "sk_spark", "sk_dbt"]
    assert values["why_brambleway"]["text"].startswith("I have built data platforms")
    assert values["resume"]["kind"] == "file"
    assert "work_arrangements" not in values  # optional, not answered, left empty
    assert ats.submissions("standard")["submissions"] == []

    text = cli("status", app_id)
    assert text.code == EXIT_OK and "state:        NEEDS_INPUT" in text.stdout
    assert "needed from you" not in text.stdout  # nothing to answer: the user reviews
    assert f"next:         interviewmaxxing resume {app_id}" in text.stdout

    # No receipt exists and none is invented, with or without --json.
    for extra in ((), ("--json",)):
        receipt = cli("receipt", app_id, *extra)
        assert receipt.code == EXIT_BLOCKED
        assert receipt.stdout.strip() == (
            f"No receipt: application {app_id} is NEEDS_INPUT; "
            "no confirmed submission has been recorded.")

    # Resuming a prepared application (new process) prepares it again and still
    # does not submit: the restriction is stored with the application.
    resumed, again = _resume(cli, app_id)
    assert again["application_id"] == app_id
    _assert_prepared(cli, ats, "standard", resumed, again, runs=2)
    assert ats.submissions("standard")["accepted_count"] == 0


def test_repeated_apply_of_a_prepared_application_reuses_it(cli: Cli, ats: MockServer, profile):
    """A second request for the same URL is the same application (no duplicate) and
    ends prepared again. The stored state is RESUMABLE, so the CLI re-runs the
    browser and records a fresh ``preparation.ready`` rather than replaying the
    old one; it never submits."""
    result, outcome = _apply(cli, ats.url("standard"))
    _assert_prepared(cli, ats, "standard", result, outcome)
    app_id = outcome["application_id"]

    again, repeat = _apply(cli, ats.url("standard"))
    assert repeat["application_id"] == app_id
    _assert_prepared(cli, ats, "standard", again, repeat, runs=2)
    repeated = [e for e in events(cli, app_id) if e["event"] == "application.request_repeated"]
    assert [(e["metadata"]["state"], e["metadata"]["disposition"]) for e in repeated] == [
        ("NEEDS_INPUT", "RESUMABLE")]
    apps = cli("status", "--json").json()
    assert [a["state"] for a in apps] == ["NEEDS_INPUT"]  # one application, not two
    assert ats.submissions("standard")["accepted_count"] == 0


def test_missing_answers_survive_process_restart_then_prepare(cli: Cli, ats: MockServer, profile):
    result, outcome = _apply(cli, ats.url("missing-required"))
    assert result.code == EXIT_INCOMPLETE and outcome["state"] == "NEEDS_INPUT"
    app_id = outcome["application_id"]
    asked = {m["field_id"]: m for m in outcome["missing_inputs"]}
    assert set(asked) == {"notice_period", "salary_expectation", "faa_part_107"}
    assert asked["salary_expectation"]["reason"] == "EXPLICIT_ANSWER_REQUIRED"
    assert asked["notice_period"]["reason"] == asked["faa_part_107"]["reason"] == "NO_ANSWER"
    assert preparation_ready(cli, app_id) == []  # stopped before the review step
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
    assert "input.received" not in event_names(cli, app_id)

    answered = cli("answer", app_id, "--set", "notice_period=2 weeks",
                   "--set", "salary_expectation=150000", "--set", "faa_part_107=No")
    assert answered.code == EXIT_OK, answered.stderr
    assert "input.received" in event_names(cli, app_id)

    # Answered in one process and resumed in another, the form is prepared, not sent.
    resumed, outcome = _resume(cli, app_id)
    assert outcome["application_id"] == app_id
    _assert_prepared(cli, ats, "missing-required", resumed, outcome)
    values = {a["field_id"]: a["value"] for a in cli("status", app_id, "--json").json()["packet"]["answers"]}
    assert values["notice_period"]["label"] == "2 weeks"
    assert values["salary_expectation"]["text"] == "150000"
    assert values["faa_part_107"]["label"] == "No"


def test_multistep_application_with_an_answer_file_stops_at_review(
    cli: Cli, ats: MockServer, profile, tmp_path
):
    result, outcome = _apply(cli, ats.url("multistep"))
    assert result.code == EXIT_INCOMPLETE and outcome["state"] == "NEEDS_INPUT", result.stdout
    app_id = outcome["application_id"]
    assert [(m["label"], m["form_step"]) for m in outcome["missing_inputs"]] == [(Q_WHY, 2)]
    # Steps 1 and 2 were filled and advanced (the mock saved them as a draft, with the
    # resume) before the run stopped at step 3's unanswered question.
    [draft] = ats.drafts("multistep")
    assert sorted(draft["steps"]) == ["1", "2"]
    assert (draft["steps"]["1"]["first_name"], draft["steps"]["1"]["email"]) == (
        "Avery", "avery.quill@example.test")
    assert draft["steps"]["2"]["work_authorization"] == "wa_authorized"
    assert draft["files"]["resume"]["sha256"] == _sha(RESUME.read_bytes())

    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"why_brambleway": "Forecasting logistics interests me."}))
    assert cli("answer", app_id, "--answers", str(answers)).code == EXIT_OK

    resumed, outcome = _resume(cli, app_id)
    ready = _assert_prepared(cli, ats, "multistep", resumed, outcome)
    assert ready["form_step"] == 3 and ready["form_url"].endswith("/review")
    # The resumed run walked every step again with the file answer and stopped at
    # the review page: the mock has the whole draft, but no submission.
    complete = [d for d in ats.drafts("multistep") if "3" in d["steps"]]
    assert len(complete) == 1 and ready["form_url"] == (
        f"{ats.url('multistep')}/{complete[0]['draft_id']}/review")
    assert complete[0]["steps"]["3"]["why_brambleway"] == "Forecasting logistics interests me."
    assert complete[0]["files"]["resume"]["sha256"] == _sha(RESUME.read_bytes())
    assert ats.submissions("multistep")["submissions"] == []
    assert event_names(cli, app_id).count("application.inspecting") >= 4  # per step, both runs


@pytest.mark.parametrize("job, attestation, consent", [
    ("attestation", "attest_accuracy", "attest_privacy_notice"),
    ("agreement", "agree_declaration", "agree_retention"),
])
def test_personal_attestations_are_only_made_by_the_user(
    cli: Cli, ats: MockServer, profile, job, attestation, consent
):
    result, outcome = _apply(cli, ats.url(job))
    assert result.code == EXIT_INCOMPLETE and outcome["state"] == "NEEDS_INPUT"
    app_id = outcome["application_id"]
    reasons = {m["field_id"]: m["reason"] for m in outcome["missing_inputs"]}
    assert reasons == {attestation: "UNCOVERED_ATTESTATION", consent: "EXPLICIT_ANSWER_REQUIRED"}
    assert all(m["control_type"] == "CHECKBOX" for m in outcome["missing_inputs"])
    assert preparation_ready(cli, app_id) == []
    assert ats.submissions(job)["accepted_count"] == 0

    assert cli("answer", app_id, "--set", f"{attestation}=yes",
               "--set", f"{consent}=yes").code == EXIT_OK
    resumed, outcome = _resume(cli, app_id)
    _assert_prepared(cli, ats, job, resumed, outcome)
    values = {a["field_id"]: a["value"] for a in cli("status", app_id, "--json").json()["packet"]["answers"]}
    assert values[attestation]["checked"] is True and values[consent]["checked"] is True


def test_server_side_validation_cannot_fire_before_submission(cli: Cli, ats: MockServer, profile):
    """The validation job rejects the fixture phone (not 10 digits) only when the form
    is posted, and the HTML carries no ``pattern``. Preparation never posts it, so
    the form is prepared like the standard one and no rejection epoch exists. The
    rejection path (validation.rejected, a corrected answer, re-submit) is covered
    with a synthetic browser in tests/core/test_runner.py."""
    result, outcome = _apply(cli, ats.url("validation"))
    _assert_prepared(cli, ats, "validation", result, outcome)
    app_id = outcome["application_id"]
    assert "validation.rejected" not in event_names(cli, app_id)
    values = {a["field_id"]: a["value"] for a in cli("status", app_id, "--json").json()["packet"]["answers"]}
    assert values["phone"]["text"] == "+1 (303) 555-0142"  # would be rejected on submit
    assert ats.submissions("validation")["rejections"] == []


def test_local_state_is_created_private(cli: Cli, ats: MockServer, profile, home):
    import stat

    _apply(cli, ats.url("standard"))
    state_db = home / "state" / "imx.sqlite3"
    assert stat.S_IMODE(state_db.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_db.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "browser").stat().st_mode) == 0o700


def test_reconcile_refuses_an_application_that_was_never_submitted(
    cli: Cli, ats: MockServer, profile
):
    """The uncertain job answers a POST with an error page and no reference; only a
    dispatched submit could make it SUBMISSION_UNKNOWN. Prepared, it is an ordinary
    NEEDS_INPUT application: ``reconcile`` refuses without opening the browser and
    nothing was ever received by the site."""
    result, outcome = _apply(cli, ats.url("uncertain"))
    _assert_prepared(cli, ats, "uncertain", result, outcome)
    app_id = outcome["application_id"]

    refused = cli("reconcile", app_id, "--headless", "--json")
    assert refused.code == EXIT_BLOCKED, refused.stdout + refused.stderr
    body = refused.json()
    assert body["state"] == "NEEDS_INPUT" and body["receipt"] is None
    assert body["message"] == (
        "Only an uncertain submission can be reconciled; this one is NEEDS_INPUT.")
    plain = cli("reconcile", app_id)
    assert plain.code == EXIT_BLOCKED and body["message"] in plain.stdout
    assert f"interviewmaxxing resume {app_id}" in plain.stdout

    names = event_names(cli, app_id)
    assert not SUBMISSION_EVENTS & set(names)
    assert not any(n.startswith("reconcile.") for n in names)
    assert cli("status", app_id, "--json").json()["application"]["state"] == "NEEDS_INPUT"
    assert ats.submissions("uncertain")["accepted_count"] == 0
    assert cli("receipt", app_id).code == EXIT_BLOCKED


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


def test_captcha_widget_is_prepared_with_the_captcha_pending(cli: Cli, ats: MockServer, profile):
    """An invisible CAPTCHA widget is checked by the site only on submit, so the form
    is filled and prepared as usual; the stop message and the event say the CAPTCHA
    is still to be solved. (The fixture's saved "why" answer is scoped to the
    standard job's URL, so this job first asks for it.)"""
    result, outcome = _apply(cli, ats.url("captcha-widget"))
    assert result.code == EXIT_INCOMPLETE and outcome["state"] == "NEEDS_INPUT"
    app_id = outcome["application_id"]
    assert [(m["field_id"], m["reason"]) for m in outcome["missing_inputs"]] == [
        ("why_brambleway", "NO_ANSWER")]
    assert cli("answer", app_id, "--set",
               "why_brambleway=Platform work on a fictional team interests me.").code == EXIT_OK

    resumed, outcome = _resume(cli, app_id)
    ready = _assert_prepared(cli, ats, "captcha-widget", resumed, outcome)
    assert ready["captcha_pending"] is True
    assert "A CAPTCHA on this form must be solved in the browser before it can be submitted." \
        in outcome["message"]
    assert "Solve the CAPTCHA" not in outcome["message"]  # no user action was requested


def test_custom_controls_are_left_to_the_user(cli: Cli, ats: MockServer, profile):
    """The "Preferred office" ARIA combobox is inspected as an accessible SELECT with
    its listbox options (tests/browser/test_inspection.py), so it is no longer an
    UNSUPPORTED_CONTROL: it stops the run as an unanswered required question, is
    never inferred, and the disabled and honeypot fields are not asked at all."""
    result, outcome = _apply(cli, ats.url("custom-control"))
    assert result.code == EXIT_INCOMPLETE and outcome["state"] == "NEEDS_INPUT"
    [office] = outcome["missing_inputs"]
    assert (office["field_id"], office["reason"], office["label"]) == (
        "preferred_office", "NO_ANSWER", "Preferred office")
    assert office["control_type"] == "SELECT" and office["required"]
    assert [o["value"] for o in office["options"]] == ["office_den", "office_bou"]
    assert preparation_ready(cli, outcome["application_id"]) == []
    assert ats.submissions("custom-control")["accepted_count"] == 0


RESUME_B = RESUME.read_bytes() + b"\n% fictional variant B of the resume\n"


def test_each_application_keeps_its_own_resume_across_profile_change_and_restart(
    cli: Cli, ats: MockServer, profile
):
    # Application A (multistep) starts with resume A: it uploads it at step 2, which
    # the mock keeps in the draft, then stops for the step 3 answer.
    _, a = _apply(cli, ats.url("multistep"))
    assert a["state"] == "NEEDS_INPUT"
    pinned = [e for e in events(cli, a["application_id"]) if e["event"] == "document.resume_pinned"]
    assert [e["metadata"]["sha256"] for e in pinned] == [_sha(RESUME.read_bytes())]
    [draft] = ats.drafts("multistep")
    assert draft["files"]["resume"]["sha256"] == _sha(RESUME.read_bytes())

    # The user switches the profile to resume B and applies to job B with it.
    switch_resume(profile, RESUME_B, name="resume-b.pdf", resume_id="resume_b")
    result, b = _apply(cli, ats.url("standard"))
    _assert_prepared(cli, ats, "standard", result, b)
    pinned_b = [e["metadata"] for e in events(cli, b["application_id"])
                if e["event"] == "document.resume_pinned"]
    assert [(p["resume_id"], p["sha256"]) for p in pinned_b] == [("resume_b", _sha(RESUME_B))]

    # Application A, answered and resumed in new processes, still uploads resume A.
    assert cli("answer", a["application_id"], "--set",
               "why_brambleway=Forecasting logistics interests me.").code == EXIT_OK
    resumed, outcome = _resume(cli, a["application_id"])
    _assert_prepared(cli, ats, "multistep", resumed, outcome)
    pinned = [e for e in events(cli, a["application_id"]) if e["event"] == "document.resume_pinned"]
    assert [e["metadata"]["sha256"] for e in pinned] == [_sha(RESUME.read_bytes())]  # pin unchanged
    uploaded = [d["files"]["resume"]["sha256"] for d in ats.drafts("multistep")]
    assert len(uploaded) >= 2 and set(uploaded) == {_sha(RESUME.read_bytes())}
    assert ats.submissions("multistep")["accepted_count"] == 0


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
    assert preparation_ready(cli, app_id) == []
    assert ats.submissions("missing-required")["accepted_count"] == 0
