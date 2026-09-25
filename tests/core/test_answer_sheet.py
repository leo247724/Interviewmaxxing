"""The answer sheet offline: ``holds --sheet`` writes one entry per distinct open
question (owner-only), unconfirmed proposals come from below-gate routing traces,
``answer --sheet`` applies the filled-in entries through the ``answer`` path, reports
counts and failing wordings only, and is idempotent. Everything here is fictional."""

from __future__ import annotations

import json
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_cli.batch import summarize, write_summary
from interviewmaxxing_cli.main import EXIT_ERROR, EXIT_OK, EXIT_USAGE, main
from interviewmaxxing_cli.sheet import (
    AnswerSheet,
    SheetResult,
    apply_sheet,
    build_sheet,
    newest_batch_id,
    read_sheet,
    write_sheet,
)
from interviewmaxxing_cli.triage import build_holds
from interviewmaxxing_core import (
    AnswerScope,
    ApplicationState,
    ApplicationStore,
    BooleanValue,
    ChoiceValue,
    ControlType,
    FieldOption,
    IdentityEvidenceKind,
    JobIdentityObservation,
    LocalPaths,
    MissingInput,
    MissingReason,
    MultiChoiceValue,
    SavedAnswer,
    SemanticType,
    TextValue,
)

S = ApplicationState
ORIGIN = "https://jobs.fictional.example/brambleway"
RESUME = Path(__file__).resolve().parents[1] / "fixtures" / "core" / "resume-avery-example.pdf"
SPONSOR = "Will you now or in the future require visa sponsorship?"
AI_TOOLS = "Have you used AI tools to help prepare this application?"
RESTAURANT = "What is your favourite restaurant?"
TOOLS = "Which of these tools have you used?\nSelect all that apply."
NON_COMPETE = "I have signed a non-compete agreement with a previous employer."
FAMILIAR = "Before applying, how familiar were you with this company?"
PRIVATE = "PRIVATE-FICTIONAL-ANSWER-4242"
"""A value typed into the sheet or stored in the profile; it must never be printed."""
SPONSOR_OPTIONS = [FieldOption(value="needs_sponsorship", label="Yes, I will require sponsorship"),
                   FieldOption(value="no_sponsorship", label="No, I will not require sponsorship")]
YES_NO = [FieldOption(value="", label="Select..."), FieldOption(value="y", label="Yes"),
          FieldOption(value="n", label="No")]
TOOL_OPTIONS = [FieldOption(value="t1", label="Spreadsheets"), FieldOption(value="t2", label="Dashboards"),
                FieldOption(value="t3", label="Notebooks")]
FAMILIAR_OPTIONS = [FieldOption(value="f0", label="Not at all"),
                    FieldOption(value="f1", label="Somewhat familiar"),
                    FieldOption(value="f2", label="Very familiar")]


@pytest.fixture
def paths(tmp_path: Path) -> LocalPaths:
    return LocalPaths.from_env({}, home=tmp_path / "home with space")


def question(url: str, field_id: str, label: str, reason: MissingReason = MissingReason.NO_ANSWER,
             semantic: SemanticType = SemanticType.UNKNOWN,
             control: ControlType | None = ControlType.TEXT,
             options: list[FieldOption] | None = None) -> MissingInput:
    return MissingInput(field_id=field_id, form_url=url, form_step=0,
                        field_fingerprint=(field_id.encode().hex() * 64)[:64], label=label,
                        reason=reason, prompt="Please answer.", semantic_type=semantic,
                        control_type=control, options=options)


def sponsor(url: str, semantic: SemanticType = SemanticType.SPONSORSHIP) -> MissingInput:
    return question(url, "sponsorship", SPONSOR, MissingReason.EXPLICIT_ANSWER_REQUIRED, semantic,
                    ControlType.RADIO, SPONSOR_OPTIONS)


def ai_tools(url: str) -> MissingInput:
    return question(url, "ai_tools", AI_TOOLS, control=ControlType.SELECT, options=YES_NO)


def sign_in() -> MissingInput:
    return MissingInput(field_id=None, label="Sign in", reason=MissingReason.USER_ACTION,
                        prompt="Sign in first.")


def application(paths: LocalPaths, name: str, missing: list[MissingInput], *,
                ats: str = "greenhouse", candidate: str = "default",
                traces: list[dict[str, Any]] | None = None) -> str:
    """A held application for ``ORIGIN/name`` with ``missing`` recorded, and optionally
    a projected ``routing.trace`` event as the runner records it."""
    paths.ensure()
    url = f"{ORIGIN}/{name}"
    with ApplicationStore.open(paths.state_db) as store:
        app = store.record_request(candidate, url).application
        claim = store.claim(app.id, "test")
        store.transition(claim, S.INSPECTING)
        store.bind_job_identity(claim, JobIdentityObservation(
            ats_type=ats, ats_tenant="brambleway", external_job_id=name,
            evidence_kind=IdentityEvidenceKind.ATS_JOB_ID_ON_PAGE, evidence="fictional"))
        if traces:
            store.append_event(claim, "routing.trace", {"form_step": 0, "traces": traces})
        store.transition(claim, S.NEEDS_INPUT, metadata={"reason": "x", "missing_inputs": [
            m.model_dump(mode="json") for m in missing]})
        store.release(claim)
        return app.id


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


def trace(stage: str, item: MissingInput, **extra: Any) -> dict[str, Any]:
    return {"stage": stage, "field_id": item.field_id, "field_fingerprint": item.field_fingerprint,
            **extra}


def entries(sheet: AnswerSheet) -> dict[str, Any]:
    return {q.question: q for q in sheet.questions}


def user_inputs(paths: LocalPaths, app_id: str) -> dict[str, Any]:
    with ApplicationStore.open(paths.state_db) as store:
        return {u.field_id: u for u in store.list_user_inputs(app_id)}


def batch(paths: LocalPaths, batch_id: str, *, retry_of: str | None = None,
          mtime: float | None = None) -> None:
    from interviewmaxxing_cli.batch import RetryStats

    directory = paths.home / "batches" / batch_id
    directory.mkdir(parents=True)
    ledger = directory / "ledger.jsonl"
    ledger.write_text("")
    now = datetime.now(UTC)
    summary = summarize(batch_id, [], started_at=now, finished_at=now, rows=0, launched=0,
                        skipped_settled=0, skipped_invalid_url=0, ledger_path=ledger,
                        retry=RetryStats(retry_of=retry_of) if retry_of else None)
    write_summary(summary, directory)
    if mtime is not None:
        import os

        os.utime(ledger, (mtime, mtime))


# --- holds --sheet -------------------------------------------------------------------------------


def test_sheet_lists_each_open_question_once_with_its_field_on_every_application(paths, capsys):
    a = application(paths, "a", [sponsor(f"{ORIGIN}/a"), ai_tools(f"{ORIGIN}/a"),
                                 question(f"{ORIGIN}/a", "restaurant", RESTAURANT)])
    b = application(paths, "b", [sponsor(f"{ORIGIN}/b", SemanticType.UNKNOWN),
                                 question(f"{ORIGIN}/b", "q_ai", AI_TOOLS + " *",
                                          control=ControlType.SELECT, options=YES_NO)],
                    ats="lever")
    c = application(paths, "c", [sign_in(), question(f"{ORIGIN}/c", "cv", "Upload your CV",
                                                     control=ControlType.FILE)])
    application(paths, "d", [sponsor(f"{ORIGIN}/d")], candidate="someone")
    home = str(paths.home)
    path = paths.home / "sheet.json"

    assert main(["--home", home, "holds", "--sheet", str(path)]) == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("wrote 3 question(s) (0 with an unconfirmed proposal) and 2 browser "
                          "action(s) for 3 held application(s) to ")
    assert f"answer --sheet '{path}'" in out and SPONSOR not in out
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    data = json.loads(path.read_text())
    assert set(data) == set(AnswerSheet.model_fields)
    assert (data["candidate_id"], data["held"], data["open_holds"]) == ("default", 3, 7)
    sponsor_entry, ai_entry, restaurant_entry = data["questions"]
    assert sponsor_entry == {
        "question": SPONSOR, "semantic_type": "SPONSORSHIP", "control_type": "RADIO",
        "options": [{"value": "needs_sponsorship", "label": "Yes, I will require sponsorship"},
                    {"value": "no_sponsorship", "label": "No, I will not require sponsorship"}],
        "options_by_application": {}, "backends": ["greenhouse", "lever"],
        "reason": "EXPLICIT_ANSWER_REQUIRED", "applications": 2,
        "fields": {a: "sponsorship", b: "sponsorship"}, "reuse": "global", "answer": None,
        "proposal": None, "proposal_basis": None}
    assert ai_entry["question"] == AI_TOOLS and ai_entry["fields"] == {a: "ai_tools", b: "q_ai"}
    assert ai_entry["options"] == [{"value": "y", "label": "Yes"}, {"value": "n", "label": "No"}]
    assert (ai_entry["semantic_type"], ai_entry["control_type"]) == (None, "SELECT")
    assert restaurant_entry["fields"] == {a: "restaurant"} and restaurant_entry["options"] == []
    assert data["actions"] == [
        {"question": "Sign in", "reason": "USER_ACTION", "control_type": None, "applications": 1,
         "application_ids": [c], "resume": [f"interviewmaxxing --home '{home}' resume {c} --act"]},
        {"question": "Upload your CV", "reason": "NO_ANSWER", "control_type": "FILE",
         "applications": 1, "application_ids": [c],
         "resume": [f"interviewmaxxing --home '{home}' resume {c} --act"]}]

    # An existing sheet is kept (it may hold typed answers) unless --force.
    assert main(["--home", home, "holds", "--sheet", str(path)]) == EXIT_USAGE
    assert "exists" in capsys.readouterr().err
    assert main(["--home", home, "holds", "--sheet", str(path), "--force"]) == EXIT_OK
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_sheet_records_options_that_differ_between_applications(paths):
    a = application(paths, "a", [question(f"{ORIGIN}/a", "fam", FAMILIAR, control=ControlType.SELECT,
                                          options=FAMILIAR_OPTIONS)])
    b = application(paths, "b", [question(f"{ORIGIN}/b", "fam", FAMILIAR, control=ControlType.SELECT,
                                          options=FAMILIAR_OPTIONS[:2])])
    [entry] = build_sheet(paths, "default").questions
    assert [o.label for o in entry.options] == ["Not at all", "Somewhat familiar", "Very familiar"]
    assert entry.fields == {a: "fam", b: "fam"}
    assert {app: [o.label for o in opts] for app, opts in entry.options_by_application.items()} \
        == {b: ["Not at all", "Somewhat familiar"]}


def test_sheet_without_a_state_database_is_empty_and_creates_nothing(tmp_path, capsys):
    absent = tmp_path / "absent"
    path = tmp_path / "sheet.json"
    assert main(["--home", str(absent), "holds", "--sheet", str(path)]) == EXIT_OK
    assert "wrote 0 question(s)" in capsys.readouterr().out
    assert json.loads(path.read_text())["questions"] == [] and not absent.exists()


# --- proposals ---------------------------------------------------------------------------------


def test_proposals_come_from_below_gate_traces_and_are_marked_unconfirmed(paths):
    candidates = write_profile(paths)
    candidates.save_answer("default", SavedAnswer(
        id="sa_familiar", scope=AnswerScope.GLOBAL, question="How well do you know us?",
        value=PRIVATE, confirmed_at=datetime(2026, 1, 1, tzinfo=UTC)))
    url = f"{ORIGIN}/a"
    familiar = question(url, "fam", FAMILIAR, control=ControlType.SELECT, options=FAMILIAR_OPTIONS)
    sponsorship = sponsor(url)
    tools = question(url, "tools", "How many years have you used spreadsheets?",
                     control=ControlType.SELECT, options=[
                         FieldOption(value="", label="Choose"), FieldOption(value="a", label="0-2"),
                         FieldOption(value="b", label="3-5")])
    used_ai = ai_tools(url)
    non_compete = question(url, "nc", NON_COMPETE, control=ControlType.CHECKBOX)
    a = application(paths, "a", [familiar, sponsorship, tools, used_ai, non_compete], traces=[
        # A reworded saved answer scored below the gate: the older trace is superseded.
        trace("question_equivalence", familiar, candidate_ids=[["sa_other"]], choice="q0",
              confidence=0.5, probability=0.5, status="BELOW_GATE"),
        trace("question_equivalence", familiar, candidate_ids=[["sa_familiar"], ["sa_other"]],
              choice="q0", confidence=0.88, probability=0.9, value_probability=0.91,
              gate="standard", status="BELOW_GATE"),
        # The stated status, derived below the gate.
        trace("status_derivation", sponsorship, option_count=2, via="jev", choice="o1",
              confidence=0.7, probability=0.8, status="BELOW_GATE"),
        # A fact screener's best choice among the usable options (placeholder excluded).
        trace("fact_screener", tools, kind="choice", fact_ids=["fact_1"], choice="o1",
              confidence=0.93, probability=0.6, status="UNKNOWN"),
        # A yes/no screener that could not decide, leaning NO.
        trace("experience_screener", used_ai, fact_ids=[], decision=None, choice="NO",
              confidence=0.9, probability=0.7, status="UNKNOWN"),
        # A trace that passed its gate proposes nothing (the field was answered elsewhere).
        trace("status_derivation", non_compete, via="table", choice="o0", status="ANSWERED"),
    ])
    # A second application with the same sponsorship question but no trace: the sample
    # with the trace still supplies the proposal.
    b = application(paths, "b", [sponsor(f"{ORIGIN}/b")])

    sheet = build_sheet(paths, "default")
    by_question = entries(sheet)
    fam = by_question[FAMILIAR]
    assert (fam.proposal, fam.proposal_basis.model_dump()) == (PRIVATE, {
        "stage": "question_equivalence", "score": 0.91, "confidence": 0.88,
        "status": "BELOW_GATE", "application_id": a, "reference_ids": ["sa_familiar"],
        "confirmed": False})
    spons = by_question[SPONSOR]
    assert spons.fields == {a: "sponsorship", b: "sponsorship"}
    assert (spons.proposal, spons.proposal_basis.stage, spons.proposal_basis.score) == \
        ("No, I will not require sponsorship", "status_derivation", 0.8)
    assert by_question[tools.label].proposal == "3-5"
    assert by_question[tools.label].proposal_basis.stage == "fact_screener"
    assert by_question[AI_TOOLS].proposal == "No"
    assert by_question[AI_TOOLS].proposal_basis.model_dump() == {
        "stage": "experience_screener", "score": 0.7, "confidence": 0.9, "status": "UNKNOWN",
        "application_id": a, "reference_ids": [], "confirmed": False}
    assert (by_question[NON_COMPETE].proposal, by_question[NON_COMPETE].proposal_basis) == \
        (None, None)
    assert all(q.answer is None for q in sheet.questions)

    # Proposals are never applied: the sheet as written changes nothing.
    result = apply_sheet(paths, sheet, owner="test")
    assert (result.answered, result.applied) == (0, 0)
    assert user_inputs(paths, a) == {} and user_inputs(paths, b) == {}
    assert build_holds(paths, "default").open_holds == 6


def test_proposal_from_a_trace_without_a_fingerprint_matches_by_field_id(paths):
    url = f"{ORIGIN}/a"
    item = ai_tools(url)
    application(paths, "a", [item], traces=[
        {"stage": "experience_screener", "field_id": "ai_tools", "choice": "YES",
         "confidence": 0.9, "probability": 0.5, "status": "UNKNOWN"},
        {"stage": "experience_screener", "field_id": "other", "choice": "NO",
         "confidence": 0.9, "probability": 0.5, "status": "UNKNOWN"}])
    [entry] = build_sheet(paths, "default").questions
    assert (entry.proposal, entry.proposal_basis.score) == ("Yes", 0.5)


# --- answer --sheet ------------------------------------------------------------------------------


def _filled(paths: LocalPaths) -> tuple[Path, dict[str, str]]:
    """Three held applications and a sheet with every kind of answer filled in."""
    a = application(paths, "a", [sponsor(f"{ORIGIN}/a"), ai_tools(f"{ORIGIN}/a"),
                                 question(f"{ORIGIN}/a", "restaurant", RESTAURANT),
                                 question(f"{ORIGIN}/a", "tools", TOOLS,
                                          control=ControlType.MULTISELECT, options=TOOL_OPTIONS),
                                 question(f"{ORIGIN}/a", "nc", NON_COMPETE,
                                          control=ControlType.CHECKBOX)])
    b = application(paths, "b", [sponsor(f"{ORIGIN}/b", SemanticType.UNKNOWN),
                                 question(f"{ORIGIN}/b", "q_ai", AI_TOOLS,
                                          control=ControlType.SELECT, options=YES_NO)], ats="lever")
    c = application(paths, "c", [question(f"{ORIGIN}/c", "fam", FAMILIAR,
                                          control=ControlType.SELECT, options=FAMILIAR_OPTIONS),
                                 question(f"{ORIGIN}/c", "tools", TOOLS,
                                          control=ControlType.MULTISELECT, options=TOOL_OPTIONS)])
    path = paths.home / "sheet.json"
    write_sheet(build_sheet(paths, "default"), path)
    data = json.loads(path.read_text())
    answers = {SPONSOR: "no_sponsorship",                # an option value
               AI_TOOLS: "yes",                          # an option label, case ignored
               RESTAURANT: PRIVATE,                      # free text
               TOOLS: "Spreadsheets; t3",                # several, label or value
               NON_COMPETE: "yes",                       # a checkbox (required: yes)
               FAMILIAR: "Extremely familiar"}           # not one of the options
    for entry in data["questions"]:
        entry["answer"] = answers[entry["question"]]
        if entry["question"] == RESTAURANT:
            entry["reuse"] = "application"
        if entry["question"] == TOOLS:
            entry["reuse"] = "job"
    path.write_text(json.dumps(data))
    return path, {"a": a, "b": b, "c": c}


def test_answer_sheet_applies_every_filled_entry_through_the_answer_path(paths, capsys):
    candidates = write_profile(paths)
    path, apps = _filled(paths)
    a, b, c = apps["a"], apps["b"], apps["c"]
    batch(paths, "big1")
    home = str(paths.home)

    assert main(["--home", home, "answer", "--sheet", str(path)]) == EXIT_OK
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert lines[0] == (f"answer sheet {path}: 6 of 6 entries answered; saved 8 answer(s) "
                        "for 3 application(s)")
    assert lines[1] == "not applied (invalid, 1 application(s)): " + FAMILIAR
    assert lines[2] == f"next: interviewmaxxing --home '{home}' prepare-batch --retry big1"
    assert PRIVATE not in out + err and "Extremely" not in out + err and "no_sponsorship" not in out

    saved_a, saved_b, saved_c = user_inputs(paths, a), user_inputs(paths, b), user_inputs(paths, c)
    assert saved_a["sponsorship"].value == ChoiceValue(value="no_sponsorship",
                                                       label="No, I will not require sponsorship")
    assert saved_a["sponsorship"].reuse.value == "GLOBAL"
    assert saved_b["sponsorship"].value == saved_a["sponsorship"].value
    assert saved_a["ai_tools"].value == ChoiceValue(value="y", label="Yes")
    assert saved_b["q_ai"].value == ChoiceValue(value="y", label="Yes")
    assert saved_a["restaurant"].value == TextValue(text=PRIVATE)
    assert saved_a["restaurant"].reuse.value == "APPLICATION"
    assert saved_a["tools"].value == MultiChoiceValue(choices=[
        FieldOption(value="t1", label="Spreadsheets"), FieldOption(value="t3", label="Notebooks")])
    assert saved_a["tools"].reuse.value == "JOB" and saved_c["tools"].reuse.value == "JOB"
    assert saved_a["nc"].value == BooleanValue(checked=True)
    assert "fam" not in saved_c

    saved = {(s.question, s.scope) for s in candidates.load("default").saved_answers}
    assert saved == {(SPONSOR, AnswerScope.GLOBAL), (AI_TOOLS, AnswerScope.GLOBAL),
                     (TOOLS, AnswerScope.JOB), (NON_COMPETE, AnswerScope.GLOBAL)}
    jobs = {s.job_identity_key for s in candidates.load("default").saved_answers
            if s.question == TOOLS}
    assert len(jobs) == 2  # one job-scoped answer per application's job

    # A sheet generated now lists only what is still open.
    report = build_holds(paths, "default")
    assert (report.held, report.answered, report.answered_holds) == (1, 2, 8)
    again = build_sheet(paths, "default")
    assert [(q.question, q.fields) for q in again.questions] == [(FAMILIAR, {c: "fam"})]


def test_answer_sheet_is_idempotent(paths, capsys):
    candidates = write_profile(paths)
    path, apps = _filled(paths)
    first = apply_sheet(paths, read_sheet(path), owner="test")
    assert (first.applied, first.applications, first.already_answered) == (8, 3, 0)
    assert first.skipped == {"invalid": 1}
    inputs = {name: user_inputs(paths, app_id) for name, app_id in apps.items()}
    answers_file = candidates.answers_path("default").read_text()
    with ApplicationStore.open(paths.state_db) as store:
        event_counts = {app_id: len(store.list_events(app_id)) for app_id in apps.values()}

    second = apply_sheet(paths, read_sheet(path), owner="test")
    assert (second.applied, second.applications, second.already_answered) == (0, 0, 8)
    assert second.skipped == {"invalid": 1, "already answered": 8}
    assert [f.model_dump() for f in second.failures] == [
        {"question": FAMILIAR, "kind": "invalid", "applications": 1}]
    assert {name: user_inputs(paths, app_id) for name, app_id in apps.items()} == inputs
    assert candidates.answers_path("default").read_text() == answers_file
    with ApplicationStore.open(paths.state_db) as store:
        assert {app_id: len(store.list_events(app_id)) for app_id in apps.values()} == event_counts
    assert main(["--home", str(paths.home), "answer", "--sheet", str(path)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "saved 0 answer(s) for 0 application(s)" in out
    assert "already answered since the stop (left as they are): 8" in out
    assert PRIVATE not in out


def test_answer_sheet_skips_applications_that_moved_on(paths):
    write_profile(paths)
    path, apps = _filled(paths)
    with ApplicationStore.open(paths.state_db) as store:
        claim = store.claim(apps["b"], "test")
        store.transition(claim, S.INSPECTING)
        store.transition(claim, S.FAILED_RETRYABLE, failure_reason="Stopped (fictional).")
        store.release(claim)
    data = json.loads(path.read_text())
    [entry] = [q for q in data["questions"] if q["question"] == SPONSOR]
    entry["fields"]["app_missing"] = "sponsorship"
    [entry] = [q for q in data["questions"] if q["question"] == AI_TOOLS]
    entry["fields"][apps["a"]] = "not_a_field"
    path.write_text(json.dumps(data))
    result = apply_sheet(paths, read_sheet(path), owner="test")
    assert result.skipped == {"invalid": 1, "not open": 1, "not waiting": 2, "not found": 1}
    assert {(f.question, f.kind, f.applications) for f in result.failures} == {
        (FAMILIAR, "invalid", 1), (SPONSOR, "not waiting", 1), (SPONSOR, "not found", 1),
        (AI_TOOLS, "not waiting", 1), (AI_TOOLS, "not open", 1)}
    assert result.applied == 5 and set(SheetResult.model_fields) == set(result.model_dump())


def test_answer_sheet_names_the_batch_to_retry(paths, capsys):
    write_profile(paths)
    path, _ = _filled(paths)
    home = str(paths.home)
    assert main(["--home", home, "answer", "--sheet", str(path)]) == EXIT_OK
    assert "next: interviewmaxxing resume APP (no batch under" in capsys.readouterr().out
    assert newest_batch_id(paths) is None

    batch(paths, "old", mtime=1_000_000)
    batch(paths, "big1", mtime=2_000_000)
    assert newest_batch_id(paths) == "big1"
    batch(paths, "big1-retry-20260924T210507Z", retry_of="big1", mtime=3_000_000)
    assert newest_batch_id(paths) == "big1"  # a retry names the batch it retried
    batch(paths, "orphan-retry-20260924T210507Z", retry_of="gone", mtime=4_000_000)
    assert newest_batch_id(paths) == "orphan-retry-20260924T210507Z"

    assert main(["--home", home, "answer", "--sheet", str(path)]) == EXIT_OK
    assert capsys.readouterr().out.splitlines()[-1] == \
        f"next: interviewmaxxing --home '{home}' prepare-batch --retry orphan-retry-20260924T210507Z"
    assert main(["--home", home, "answer", "--sheet", str(path), "--batch-id", "big1"]) == EXIT_OK
    assert capsys.readouterr().out.splitlines()[-1] == \
        f"next: interviewmaxxing --home '{home}' prepare-batch --retry big1"


def test_answer_sheet_usage_errors_never_quote_the_sheet(paths, tmp_path, capsys):
    home = str(paths.home)
    path = tmp_path / "sheet.json"
    assert main(["--home", home, "answer", "app_x", "--sheet", str(path)]) == EXIT_USAGE
    assert "--sheet takes no APPLICATION_ID" in capsys.readouterr().err
    assert main(["--home", home, "answer", "--sheet", str(path), "--set", "a=b"]) == EXIT_USAGE
    capsys.readouterr()
    assert main(["--home", home, "answer"]) == EXIT_USAGE
    assert "give APPLICATION_ID, or --sheet FILE" in capsys.readouterr().err
    assert main(["--home", home, "answer", "--sheet", str(path)]) == EXIT_USAGE
    assert "sheet.json" in capsys.readouterr().err
    path.write_text(json.dumps({"version": 1, "candidate_id": "default",
                                "generated_at": "2026-09-25T00:00:00Z",
                                "questions": [{"question": SPONSOR, "applications": 1,
                                               "reuse": "everyone", "answer": PRIVATE}]}))
    assert main(["--home", home, "answer", "--sheet", str(path)]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "questions.0.reuse" in err and PRIVATE not in err
    path.write_text(json.dumps({"version": 1, "candidate_id": "default",
                                "generated_at": "2026-09-25T00:00:00Z", "questions": []}))
    assert main(["--home", home, "answer", "--sheet", str(path)]) == EXIT_ERROR
    assert "No state database" in capsys.readouterr().err
