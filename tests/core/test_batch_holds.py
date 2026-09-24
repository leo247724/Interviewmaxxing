"""``interviewmaxxing holds`` offline: the open holds of every NEEDS_INPUT application in
the store, grouped by question wording with counts and the ``answer`` line that answers
each question once for all of them. Holds answered after their application stopped are
not open; stored answer values never reach the output. Everything here is fictional."""

from __future__ import annotations

import json
import shlex
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_cli.main import EXIT_OK, build_parser, main
from interviewmaxxing_cli.triage import (
    HoldsReport,
    build_holds,
    recorded_holds,
    render_holds_markdown,
)
from interviewmaxxing_core import (
    AnswerScope,
    ApplicationState,
    ApplicationStore,
    ControlType,
    FieldOption,
    IdentityEvidenceKind,
    JobIdentityObservation,
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
SPONSOR = "Will you now or in the future require visa sponsorship?"
NOTICE = "What is your notice period?"
SALARY = "Desired annual base salary (USD)"
START = "Earliest start date"
PRIVATE = "PRIVATE-FICTIONAL-ANSWER-4242"
"""A stored answer value; it must never be printed."""
SPONSOR_OPTIONS = [FieldOption(value="needs_sponsorship", label="Yes, I will require sponsorship"),
                   FieldOption(value="no_sponsorship", label="No, I will not require sponsorship")]


@pytest.fixture
def paths(tmp_path: Path) -> LocalPaths:
    return LocalPaths.from_env({}, home=tmp_path / "home with space")


def question(url: str, field_id: str, label: str, reason: MissingReason,
             semantic: SemanticType = SemanticType.UNKNOWN,
             control: ControlType | None = ControlType.TEXT,
             options: list[FieldOption] | None = None) -> MissingInput:
    return MissingInput(field_id=field_id, form_url=url, form_step=0,
                        field_fingerprint=(field_id.encode().hex() * 64)[:64], label=label,
                        reason=reason, prompt="Please answer.", semantic_type=semantic,
                        control_type=control, options=options)


def sponsor(url: str, semantic: SemanticType = SemanticType.SPONSORSHIP,
            label: str = SPONSOR) -> MissingInput:
    return question(url, "sponsorship", label, MissingReason.EXPLICIT_ANSWER_REQUIRED, semantic,
                    ControlType.RADIO, SPONSOR_OPTIONS)


def sign_in() -> MissingInput:
    return MissingInput(field_id=None, label="Sign in", reason=MissingReason.USER_ACTION,
                        prompt="Sign in first.")


def application(paths: LocalPaths, name: str, stop: str = "held",
                missing: list[MissingInput] | None = None, *, ats: str = "greenhouse",
                candidate: str = "default") -> str:
    """An application for ``ORIGIN/name`` bound to an ``ats`` job and left ``held`` (with
    ``missing``), ``prepared``, ``failed`` or ``unrecorded`` (NEEDS_INPUT, nothing asked)."""
    paths.ensure()
    url = f"{ORIGIN}/{name}"
    with ApplicationStore.open(paths.state_db) as store:
        app = store.record_request(candidate, url).application
        claim = store.claim(app.id, "test")
        store.transition(claim, S.INSPECTING)
        store.bind_job_identity(claim, JobIdentityObservation(
            ats_type=ats, ats_tenant="brambleway", external_job_id=name,
            evidence_kind=IdentityEvidenceKind.ATS_JOB_ID_ON_PAGE, evidence="fictional"))
        if stop == "prepared":
            store.append_event(claim, "preparation.ready", {"submitted": False})
        if stop == "failed":
            store.transition(claim, S.FAILED_RETRYABLE, failure_reason="Stopped (fictional).")
        else:
            store.transition(claim, S.NEEDS_INPUT, metadata={"reason": "x", "missing_inputs": [
                m.model_dump(mode="json") for m in missing or []]})
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


def saved(question_text: str, when: datetime, *, answer_id: str,
          semantic: SemanticType | None = None, job_url: str | None = None) -> SavedAnswer:
    scope = AnswerScope.JOB if job_url else AnswerScope.GLOBAL
    return SavedAnswer(id=answer_id, scope=scope, job_url=job_url, semantic_type=semantic,
                       question=question_text, value=PRIVATE, confirmed_at=when)


def table(report: HoldsReport) -> list[tuple[Any, ...]]:
    return [(q.question, q.holds, q.applications, q.backends, q.reason, q.semantic_type,
             q.control_type, q.sample_application_id, q.answer, q.act, q.application_ids)
            for q in report.questions]


def test_holds_group_the_open_questions_of_held_applications(paths):
    a = application(paths, "a", missing=[sponsor(f"{ORIGIN}/a"), question(
        f"{ORIGIN}/a", "notice", NOTICE, MissingReason.NO_ANSWER, control=ControlType.SELECT)])
    b = application(paths, "b", ats="lever", missing=[
        sponsor(f"{ORIGIN}/b", label="will you now or in the future require visa sponsorship? *"),
        question(f"{ORIGIN}/b", "salary", SALARY, MissingReason.EXPLICIT_ANSWER_REQUIRED,
                 SemanticType.SALARY_EXPECTATION)])
    c = application(paths, "c", missing=[sponsor(f"{ORIGIN}/c", SemanticType.UNKNOWN),
                                         sign_in()])
    application(paths, "d", "prepared")
    application(paths, "e", "failed")
    application(paths, "f", "unrecorded")
    theirs = application(paths, "g", missing=[sponsor(f"{ORIGIN}/g")], candidate="someone")

    report = build_holds(paths, "default")
    assert (report.held, report.answered, report.prepared, report.unrecorded,
            report.open_holds, report.answered_holds) == (3, 0, 1, 1, 6, 0)
    assert table(report) == [
        (SPONSOR, 3, 3, ["greenhouse", "lever"], "EXPLICIT_ANSWER_REQUIRED", "SPONSORSHIP",
         "RADIO", c, f"interviewmaxxing answer {c} --set sponsorship=VALUE --reuse global", None,
         [a, b, c]),
        (NOTICE, 1, 1, ["greenhouse"], "NO_ANSWER", None, "SELECT", a,
         f"interviewmaxxing answer {a} --set notice=VALUE --reuse global", None, [a]),
        (SALARY, 1, 1, ["lever"], "EXPLICIT_ANSWER_REQUIRED", "SALARY_EXPECTATION", "TEXT", b,
         f"interviewmaxxing answer {b} --set salary=VALUE --reuse global", None, [b]),
        ("Sign in", 1, 1, ["greenhouse"], "USER_ACTION", None, None, c, None,
         f"interviewmaxxing resume {c} --act", [c]),
    ]
    other = build_holds(paths, "someone")
    assert [(q.question, q.application_ids) for q in other.questions] == [(SPONSOR, [theirs])]

    text = render_holds_markdown(report)
    lines = text.splitlines()
    assert "- held applications: 3, with 6 open hold(s) in 4 distinct question(s)" in lines
    assert "- not listed: 1 prepared for final review, 1 stopped without a recorded question" \
        in lines
    assert f"1. `interviewmaxxing answer {c} --set sponsorship=VALUE --reuse global`" in lines
    assert f"4. `interviewmaxxing resume {c} --act`  (complete it in the browser window)" in lines


def test_holds_answered_after_the_stop_are_not_open(paths):
    candidates = write_profile(paths)
    candidates.save_answer("default", saved(SPONSOR, datetime(2026, 1, 1, tzinfo=UTC),
                                            answer_id="sa_before"))  # already there: no help
    a = application(paths, "a", missing=[question(f"{ORIGIN}/a", "notice", NOTICE,
                                                  MissingReason.NO_ANSWER), sponsor(f"{ORIGIN}/a")])
    application(paths, "b", missing=[sponsor(f"{ORIGIN}/b")])
    c = application(paths, "c", missing=[sponsor(f"{ORIGIN}/c", SemanticType.UNKNOWN),
                                         question(f"{ORIGIN}/c", "start", START,
                                                  MissingReason.NO_ANSWER)])
    assert build_holds(paths, "default").open_holds == 5

    # The person answers the notice period on application a (``interviewmaxxing answer``).
    with ApplicationStore.open(paths.state_db) as store:
        claim = store.claim(a, "test")
        store.save_user_inputs(claim, [UserInput.answering(
            question(f"{ORIGIN}/a", "notice", NOTICE, MissingReason.NO_ANSWER),
            TextValue(text=PRIVATE))])
        store.release(claim)
    later = datetime.now(UTC) + timedelta(minutes=1)
    # Saved answers that do not apply: for another job, or typed as another question.
    candidates.save_answer("default", saved(SPONSOR, later, answer_id="sa_job",
                                            job_url=f"{ORIGIN}/elsewhere"))
    candidates.save_answer("default", saved(SPONSOR, later, answer_id="sa_typed",
                                            semantic=SemanticType.WORK_AUTHORIZATION))
    report = build_holds(paths, "default")
    assert (report.open_holds, report.answered_holds) == (4, 1)
    assert [(q.question, q.holds) for q in report.questions] == [(SPONSOR, 3), (START, 1)]

    # A global answer to the same wording, given after the stops, answers all three.
    candidates.save_answer("default", saved(SPONSOR.upper(), later, answer_id="sa_global"))
    report = build_holds(paths, "default")
    assert (report.held, report.answered, report.open_holds, report.answered_holds) == \
        (1, 2, 1, 4)
    assert table(report) == [(START, 1, 1, ["greenhouse"], "NO_ANSWER", None, "TEXT", c,
                              f"interviewmaxxing answer {c} --set start=VALUE --reuse global",
                              None, [c])]
    assert "- answered since their application stopped: 4 hold(s); 2 application(s) have " \
           "nothing open" in render_holds_markdown(report)
    for output in (render_holds_markdown(report), report.model_dump_json()):
        assert PRIVATE not in output


def test_recorded_holds_only_reads(paths):
    app_id = application(paths, "a", missing=[sponsor(f"{ORIGIN}/a")])
    with ApplicationStore.open(paths.state_db) as store:
        app = store.get_application(app_id)
        events = store.list_events(app_id)
        holds = recorded_holds(store, app, events, [], store.get_job(app.job_id))
        assert (len(holds.recorded), len(holds.open), holds.answered) == (1, 1, 0)
        assert holds.stopped_at == events[-1].timestamp
        assert len(store.list_events(app_id)) == len(events)


def test_holds_command(paths, capsys):
    write_profile(paths).save_answer("default", saved(NOTICE, datetime(2026, 1, 1, tzinfo=UTC),
                                                      answer_id="sa_old"))
    app_id = application(paths, "a", missing=[sponsor(f"{ORIGIN}/a")])
    home = str(paths.home)
    assert main(["--home", home, "holds"]) == EXIT_OK
    out = capsys.readouterr().out
    line = (f"interviewmaxxing --home '{home}' answer {app_id} --set sponsorship=VALUE "
            "--reuse global")
    assert f"1. `{line}`" in out and "# Open holds" in out
    assert "stored answer values are never shown" in out and PRIVATE not in out
    assert main(["--home", home, "holds", "--json"]) == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert set(data) == set(HoldsReport.model_fields)
    assert data["questions"][0]["answer"] == line and PRIVATE not in json.dumps(data)
    assert main(["--home", home, "holds", "--candidate", "someone", "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["questions"] == []
    args = build_parser().parse_args(["holds", "--candidate", "x", "--json"])
    assert (args.candidate, args.json, args.func.__name__) == ("x", True, "cmd_holds")


def test_holds_without_a_state_database_creates_nothing(tmp_path, capsys):
    absent = tmp_path / "absent"
    assert main(["--home", str(absent), "holds"]) == EXIT_OK
    assert "- held applications: 0, with 0 open hold(s) in 0 distinct question(s)" in \
        capsys.readouterr().out
    assert main(["--home", str(absent), "holds", "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["questions"] == []
    assert not absent.exists()


def test_one_answer_line_answers_the_question_for_every_application(paths, capsys):
    """The workflow: run the group's line once (VALUE replaced), and the question is no
    longer open anywhere."""
    candidates = write_profile(paths)
    apps = [application(paths, name, missing=[sponsor(f"{ORIGIN}/{name}", semantic)])
            for name, semantic in (("a", SemanticType.SPONSORSHIP), ("b", SemanticType.UNKNOWN),
                                   ("c", SemanticType.SPONSORSHIP))]
    [group] = build_holds(paths, "default", cli=["interviewmaxxing", "--home",
                                                 str(paths.home)]).questions
    assert group.applications == 3 and group.answer is not None
    argv = shlex.split(group.answer.replace("VALUE", "no_sponsorship"))
    assert argv[0] == "interviewmaxxing" and argv[3:5] == ["answer", apps[1]]
    assert main(argv[1:]) == EXIT_OK
    assert "saved 1 answer(s)" in capsys.readouterr().out
    report = build_holds(paths, "default")
    assert (report.questions, report.held, report.answered, report.answered_holds) == \
        ([], 0, 3, 3)
    [answer] = [a for a in candidates.load("default").saved_answers if a.question == SPONSOR]
    assert (answer.scope, answer.semantic_type) == (AnswerScope.GLOBAL, None)
