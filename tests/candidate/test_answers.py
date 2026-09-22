"""Saved answers: explicit scope, job-specific reuse, conflicts and the writer."""

from __future__ import annotations

import json
import stat
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import interviewmaxxing_candidate.store as candidate_store_module
from interviewmaxxing_candidate import (
    LocalCandidateStore,
    SavedAnswerRejected,
    reconcile_saved_answers,
)
from interviewmaxxing_core import (
    AnswerReuse,
    AnswerScope,
    ApplicationForm,
    CandidateNotFound,
    CandidateProfileInvalid,
    ChoiceValue,
    JobRecord,
    SavedAnswer,
    SemanticType,
    UserInput,
)

WriteCandidate = Callable[..., Path]
T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)
T1 = datetime(2026, 9, 15, 12, tzinfo=UTC)


def job(url: str, identity_key: str | None = None, *, job_id: str = "job_x") -> JobRecord:
    return JobRecord(
        id=job_id,
        application_url=url,
        normalized_url=url,
        identity_key=identity_key,
        created_at=T0,
        updated_at=T0,
    )


def answer(
    answer_id: str,
    value: Any,
    *,
    question: str = "Will you require visa sponsorship?",
    semantic_type: SemanticType | None = SemanticType.SPONSORSHIP,
    confirmed_at: datetime = T0,
    **target: Any,
) -> SavedAnswer:
    scope = AnswerScope.JOB if target else AnswerScope.GLOBAL
    return SavedAnswer(
        id=answer_id,
        scope=scope,
        semantic_type=semantic_type,
        question=question,
        value=value,
        confirmed_at=confirmed_at,
        **target,
    )


def dump(*answers: SavedAnswer) -> list[dict[str, Any]]:
    return [a.model_dump(mode="json") for a in answers]


OTHER_JOB = job("http://127.0.0.1:0/jobs/other-9001/apply", "ats:mock:other-co:9001")
UNBOUND_JOB = job("http://127.0.0.1:0/jobs/unbound/apply")


# --- scope from the canonical fixture ------------------------------------------------


def test_job_answers_apply_only_to_their_job(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore, mock_job: JobRecord
) -> None:
    write_candidate("default")
    profile = candidate_store.load("default")

    mock_ids = {a.id for a in profile.applicable_saved_answers(mock_job)}
    assert "sa.mock_co_start" in mock_ids and "sa.other_co_salary" not in mock_ids
    other_ids = {a.id for a in profile.applicable_saved_answers(OTHER_JOB)}
    assert "sa.other_co_salary" in other_ids and "sa.mock_co_start" not in other_ids
    # An unbound job never matches an identity-keyed JOB answer.
    unbound_ids = {a.id for a in profile.applicable_saved_answers(UNBOUND_JOB)}
    assert unbound_ids == {"sa.work_auth_us", "sa.sponsorship", "sa.privacy_consent"}
    assert profile.saved_answers_for(SemanticType.SALARY_EXPECTATION, job=mock_job) == []
    job_scoped = {a.id for a in profile.saved_answers if a.scope is AnswerScope.JOB}
    assert job_scoped == {"sa.mock_co_start", "sa.other_co_salary"}


def test_url_scoped_answer_is_normalized_and_matches_only_that_url(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    url_answer = answer(
        "sa.url_start",
        "2026-12-01",
        question="Earliest start date?",
        semantic_type=SemanticType.START_DATE,
        job_url="HTTP://127.0.0.1:8765/jobs/sample-17/apply/?utm_source=x",
    )
    write_candidate("default", answers=dump(url_answer))
    loaded = candidate_store.load("default").find_saved_answer("sa.url_start")
    assert loaded is not None
    assert loaded.job_url == "http://127.0.0.1:8765/jobs/sample-17/apply"
    assert loaded.applies_to(job("http://127.0.0.1:8765/jobs/sample-17/apply"))
    assert not loaded.applies_to(job("http://127.0.0.1:8765/jobs/sample-18/apply"))


def test_global_answer_naming_a_job_or_employer_is_rejected(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    salary = answer("sa.salary", "150000", semantic_type=SemanticType.SALARY_EXPECTATION)
    widened = salary.model_dump(mode="json") | {"employer": "Other Co"}
    directory = write_candidate("default", answers=[widened])
    with pytest.raises(CandidateProfileInvalid) as exc:
        candidate_store.load("default")
    assert "GLOBAL answers must not name a job or employer" in str(exc.value)
    assert str(directory / "answers.json") in str(exc.value)
    assert "[0]" in str(exc.value)


def test_answer_without_scope_is_rejected(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    unscoped = dump(answer("sa.x", "Yes"))[0]
    del unscoped["scope"]
    write_candidate("default", answers=[unscoped])
    with pytest.raises(CandidateProfileInvalid, match=r"\[0\]\.scope: Field required"):
        candidate_store.load("default")


def test_saved_attestation_loads_without_extra_approval(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore, mock_job: JobRecord
) -> None:
    attestation = answer(
        "sa.accurate",
        True,
        question="I certify the information in this application is accurate.",
        semantic_type=SemanticType.ATTESTATION,
    )
    write_candidate("default", answers=dump(attestation))
    profile = candidate_store.load("default")
    assert profile.saved_answers_for(SemanticType.ATTESTATION, job=mock_job) == [attestation]
    assert profile.saved_answers_for(SemanticType.ATTESTATION, job=OTHER_JOB) == [attestation]


@pytest.mark.parametrize("value", [False, 0, 0.0, "", []])
def test_false_zero_and_empty_answers_survive(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore, value: Any
) -> None:
    write_candidate("default")
    saved = answer("sa.falsy", value)
    candidate_store.save_answer("default", saved)
    loaded = candidate_store.load("default").find_saved_answer("sa.falsy")
    assert loaded is not None
    assert loaded.value == value and type(loaded.value) is type(value)


# --- conflicts -----------------------------------------------------------------------


def test_tied_conflicting_answers_are_kept_and_reported(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    yes = answer("sa.a", "Yes")
    no = answer("sa.b", "No", question="  will you REQUIRE visa   sponsorship? ")
    write_candidate("default", answers=dump(yes, no))
    report = candidate_store.load_report("default")

    assert report.profile.find_saved_answer("sa.a") == yes
    assert report.profile.find_saved_answer("sa.b") == no
    assert [c.answer_ids for c in report.answer_conflicts] == [("sa.a", "sa.b")]
    assert report.superseded_answers == ()
    assert any("sa.a, sa.b" in w and "ambiguous" in w for w in report.warnings())
    assert report.answer_sources["sa.a"] == report.answers_path


def test_tied_job_conflict_is_not_hidden_behind_global_answer(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore, mock_job: JobRecord
) -> None:
    # Review C2R-1: dropping both JOB answers let the GLOBAL 100000 answer the question.
    salary = SemanticType.SALARY_EXPECTATION
    question = "What are your salary expectations?"
    key = "ats:mock:mock-co:4012"
    global_salary = answer("sa.salary_global", "100000", question=question, semantic_type=salary)
    job_a = answer(
        "sa.salary_a",
        "150000",
        question=question,
        semantic_type=salary,
        confirmed_at=T1,
        job_identity_key=key,
    )
    job_b = answer(
        "sa.salary_b",
        "175000",
        question=question,
        semantic_type=salary,
        confirmed_at=T1,
        job_identity_key=key,
    )
    write_candidate("default", answers=dump(global_salary, job_a, job_b))
    report = candidate_store.load_report("default")

    applicable = report.profile.saved_answers_for(salary, job=mock_job)
    assert applicable == [global_salary, job_a, job_b]
    assert [c.answer_ids for c in report.answer_conflicts] == [("sa.salary_a", "sa.salary_b")]
    assert report.superseded_answers == ()


def test_older_answers_are_superseded_by_a_tied_conflict() -> None:
    old_a = answer("sa.old_a", "A")
    old_b = answer("sa.old_b", "B")
    new_b = answer("sa.new_b", "B", confirmed_at=T1)
    new_c = answer("sa.new_c", "C", confirmed_at=T1)
    result = reconcile_saved_answers([old_a, old_b, new_b, new_c])

    assert result.kept == (old_b, new_b, new_c)
    assert [(s.answer.id, s.superseded_by) for s in result.superseded] == [
        ("sa.old_a", ("sa.new_b", "sa.new_c"))
    ]
    assert [c.answer_ids for c in result.conflicts] == [("sa.old_b", "sa.new_b", "sa.new_c")]


def test_later_confirmation_replaces_earlier_answer(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    def old_in_profile(data: dict[str, Any]) -> None:
        data["saved_answers"][1]["value"] = "Yes"  # sa.sponsorship, confirmed T0

    newer = answer(
        "sa.sponsorship_2",
        "No",
        question="Will you now or in the future require visa sponsorship?",
        confirmed_at=T1,
    )
    directory = write_candidate("default", edit=old_in_profile, answers=dump(newer))
    report = candidate_store.load_report("default")

    assert report.profile.find_saved_answer("sa.sponsorship") is None
    assert report.profile.find_saved_answer("sa.sponsorship_2") == newer
    assert [(s.answer.id, s.superseded_by) for s in report.superseded_answers] == [
        ("sa.sponsorship", ("sa.sponsorship_2",))
    ]
    assert report.answer_sources["sa.sponsorship"] == directory / "profile.json"
    assert report.answer_conflicts == ()


@pytest.mark.parametrize(
    ("first", "second", "conflict"),
    [
        (True, "Yes", True),
        (False, 0, True),
        (0, 0.0, False),
        ("Yes", " yes ", False),
        (["Search", "Social"], ["social", "search"], False),
        (["Search"], ["Search", "Social"], True),
    ],
)
def test_value_comparison_keeps_types_distinct(first: Any, second: Any, conflict: bool) -> None:
    result = reconcile_saved_answers([answer("sa.a", first), answer("sa.b", second)])
    assert bool(result.conflicts) is conflict
    assert len(result.kept) == 2  # conflicting answers are reported, never dropped


def test_different_scopes_or_targets_never_conflict() -> None:
    global_no = answer("sa.global", "No")
    job_yes = answer("sa.job", "Yes", job_identity_key="ats:mock:mock-co:4012")
    other_job_maybe = answer("sa.job2", "Maybe", job_identity_key="ats:mock:other-co:9001")
    other_type = answer("sa.type", "Yes", semantic_type=SemanticType.CUSTOM_BOOLEAN)
    result = reconcile_saved_answers([global_no, job_yes, other_job_maybe, other_type])
    assert result.kept == (global_no, job_yes, other_job_maybe, other_type)
    assert result.conflicts == () and result.superseded == ()


def test_duplicate_ids_across_files_are_rejected(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    write_candidate("default", answers=dump(answer("sa.sponsorship", "No")))
    with pytest.raises(CandidateProfileInvalid, match="appears in both"):
        candidate_store.load("default")


@pytest.mark.parametrize(
    ("content", "needle"),
    [
        ("{}", "expected a JSON array"),
        ("[", "invalid JSON"),
        (json.dumps(dump(answer("sa.x", "Yes"), answer("sa.x", "Yes"))), "duplicate saved answer"),
    ],
)
def test_malformed_answers_file(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore, content: str, needle: str
) -> None:
    directory = write_candidate("default")
    (directory / "answers.json").write_text(content)
    with pytest.raises(CandidateProfileInvalid, match=needle):
        candidate_store.load("default")


# --- writer --------------------------------------------------------------------------


def _sponsorship_input(form: ApplicationForm, reuse: AnswerReuse) -> UserInput:
    field = form.find("sponsorship")
    assert field is not None
    option = next(o for o in field.options or [] if o.value == "no")
    return UserInput.for_field(
        form, "sponsorship", ChoiceValue(value=option.value, label=option.label), reuse=reuse
    )


def test_job_reuse_is_persisted_as_job_scope(
    write_candidate: WriteCandidate,
    candidate_store: LocalCandidateStore,
    mock_form: ApplicationForm,
    mock_job: JobRecord,
) -> None:
    directory = write_candidate("default")
    profile_before = (directory / "profile.json").read_bytes()
    saved = _sponsorship_input(mock_form, AnswerReuse.JOB).to_saved_answer(job=mock_job)
    assert saved is not None and saved.scope is AnswerScope.JOB

    candidate_store.save_answer("default", saved)

    assert (directory / "profile.json").read_bytes() == profile_before
    answers_file = directory / "answers.json"
    assert stat.S_IMODE(answers_file.stat().st_mode) == 0o600
    assert json.loads(answers_file.read_text()) == [saved.model_dump(mode="json")]

    profile = candidate_store.load("default")
    loaded = profile.find_saved_answer(saved.id)
    assert loaded == saved
    assert loaded.scope is AnswerScope.JOB
    assert loaded.job_identity_key == "ats:mock:mock-co:4012"
    assert loaded.applies_to(mock_job)
    assert not loaded.applies_to(OTHER_JOB) and not loaded.applies_to(UNBOUND_JOB)


def test_application_reuse_produces_nothing_to_save(
    mock_form: ApplicationForm, mock_job: JobRecord
) -> None:
    assert (
        _sponsorship_input(mock_form, AnswerReuse.APPLICATION).to_saved_answer(job=mock_job) is None
    )


def test_resaving_same_question_replaces_it_within_scope(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    directory = write_candidate("default")
    key = "ats:mock:mock-co:4012"
    first = answer("sa.j1", "Yes", question="Sponsorship?", job_identity_key=key)
    global_answer = answer("sa.g1", "Yes", question="Sponsorship?")
    candidate_store.save_answer("default", first)
    candidate_store.save_answer("default", global_answer)
    second = answer("sa.j2", "No", question="sponsorship?", confirmed_at=T1, job_identity_key=key)
    candidate_store.save_answer("default", second)

    stored = json.loads((directory / "answers.json").read_text())
    assert [a["id"] for a in stored] == ["sa.g1", "sa.j2"]
    profile = candidate_store.load("default")
    assert profile.find_saved_answer("sa.g1") == global_answer  # GLOBAL untouched
    assert profile.find_saved_answer("sa.j2") == second


def test_saving_same_answer_twice_is_idempotent(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    directory = write_candidate("default")
    saved = answer("sa.once", "No")
    candidate_store.save_answer("default", saved)
    candidate_store.save_answer("default", saved)
    assert len(json.loads((directory / "answers.json").read_text())) == 1


def test_reusing_an_id_for_a_different_answer_is_rejected(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    directory = write_candidate("default")
    job_answer = answer("sa.same", "No", job_identity_key="ats:mock:mock-co:4012")
    candidate_store.save_answer("default", job_answer)
    before = (directory / "answers.json").read_bytes()

    widened = answer("sa.same", "No")  # same id, now GLOBAL
    with pytest.raises(SavedAnswerRejected, match="already holds a different answer"):
        candidate_store.save_answer("default", widened)
    with pytest.raises(SavedAnswerRejected, match="already used in"):
        candidate_store.save_answer("default", answer("sa.sponsorship", "No"))
    assert (directory / "answers.json").read_bytes() == before
    loaded = candidate_store.load("default").find_saved_answer("sa.same")
    assert loaded is not None and loaded.scope is AnswerScope.JOB


def test_writer_errors(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    with pytest.raises(CandidateNotFound):
        candidate_store.save_answer("default", answer("sa.x", "No"))
    with pytest.raises(TypeError):
        candidate_store.save_answer("default", {"id": "sa.x"})  # type: ignore[arg-type]

    directory = write_candidate("default")
    (directory / "answers.json").write_text("[{]")
    with pytest.raises(CandidateProfileInvalid, match="invalid JSON"):
        candidate_store.save_answer("default", answer("sa.x", "No"))
    assert (directory / "answers.json").read_text() == "[{]"  # never overwritten


def test_scope_cannot_be_dropped_from_saved_answer() -> None:
    with pytest.raises(ValidationError):
        SavedAnswer(  # type: ignore[call-arg]
            id="sa.x", question="Q?", value="Yes", confirmed_at=T0
        )


def _stored(directory: Path) -> list[tuple[str, Any]]:
    return [(a["id"], a["value"]) for a in json.loads((directory / "answers.json").read_text())]


def test_stale_retry_never_replaces_newer_answer(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    # Review C2R-2: save old T0, new T1, retry old T0 must keep the new answer.
    directory = write_candidate("default")
    old = answer("sa.old", "Yes")
    new = answer("sa.new", "No", confirmed_at=T1)
    candidate_store.save_answer("default", old)
    candidate_store.save_answer("default", new)
    before = (directory / "answers.json").read_bytes()

    candidate_store.save_answer("default", old)
    candidate_store.save_answer("default", answer("sa.old_retry", "Yes"))  # fresh id, same T0

    assert (directory / "answers.json").read_bytes() == before
    assert _stored(directory) == [("sa.new", "No")]
    report = candidate_store.load_report("default")
    assert report.profile.find_saved_answer("sa.new") == new
    assert report.answer_conflicts == () and report.superseded_answers == ()


def test_equal_time_disagreement_is_preserved_not_last_writer_wins(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    directory = write_candidate("default")
    first = answer("sa.first", "Yes")
    second = answer("sa.second", "No")
    candidate_store.save_answer("default", first)
    candidate_store.save_answer("default", second)

    assert _stored(directory) == [("sa.first", "Yes"), ("sa.second", "No")]
    report = candidate_store.load_report("default")
    assert report.profile.find_saved_answer("sa.first") == first
    assert report.profile.find_saved_answer("sa.second") == second
    assert [c.answer_ids for c in report.answer_conflicts] == [("sa.first", "sa.second")]

    # A later confirmation then resolves the conflict and replaces both.
    candidate_store.save_answer("default", answer("sa.third", "No", confirmed_at=T1))
    assert _stored(directory) == [("sa.third", "No")]
    assert candidate_store.load_report("default").answer_conflicts == ()


def test_retried_user_input_is_stored_once(
    write_candidate: WriteCandidate,
    candidate_store: LocalCandidateStore,
    mock_form: ApplicationForm,
    mock_job: JobRecord,
) -> None:
    directory = write_candidate("default")
    user_input = _sponsorship_input(mock_form, AnswerReuse.GLOBAL)
    first = user_input.to_saved_answer(job=mock_job)
    retry = user_input.to_saved_answer(job=mock_job)
    assert first is not None and retry is not None and first.id != retry.id

    candidate_store.save_answer("default", first)
    candidate_store.save_answer("default", retry)

    assert _stored(directory) == [(first.id, "No")]
    assert candidate_store.load_report("default").answer_conflicts == ()


def test_explicit_false_and_zero_under_freshness_rules(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    directory = write_candidate("default")
    candidate_store.save_answer("default", answer("sa.false", False))
    candidate_store.save_answer("default", answer("sa.false_retry", False))  # retry: no-op
    assert _stored(directory) == [("sa.false", False)]

    candidate_store.save_answer("default", answer("sa.zero", 0))  # same time, 0 is not False
    stored = json.loads((directory / "answers.json").read_text())
    assert [(a["id"], a["value"], type(a["value"])) for a in stored] == [
        ("sa.false", False, bool),
        ("sa.zero", 0, int),
    ]
    report = candidate_store.load_report("default")
    assert [c.answer_ids for c in report.answer_conflicts] == [("sa.false", "sa.zero")]

    candidate_store.save_answer("default", answer("sa.zero_later", 0, confirmed_at=T1))
    candidate_store.save_answer("default", answer("sa.false_again", False))  # stale
    stored = json.loads((directory / "answers.json").read_text())
    assert [(a["id"], a["value"], type(a["value"])) for a in stored] == [("sa.zero_later", 0, int)]
    loaded = candidate_store.load("default").find_saved_answer("sa.zero_later")
    assert loaded is not None and loaded.value == 0 and type(loaded.value) is int


def test_concurrent_writers_keep_every_answer(
    write_candidate: WriteCandidate,
    candidate_store: LocalCandidateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_candidate("default")
    read_answers = candidate_store_module._read_answers

    def slow_read(path: Path) -> list[SavedAnswer]:
        answers = read_answers(path)
        time.sleep(0.005)  # widen the read-modify-write window
        return answers

    monkeypatch.setattr(candidate_store_module, "_read_answers", slow_read)
    errors: list[BaseException] = []

    def save(i: int) -> None:
        store = LocalCandidateStore(candidate_store.profile_dir)
        try:
            store.save_answer("default", answer(f"sa.q{i}", str(i), question=f"Question {i}?"))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    ids = {a.id for a in candidate_store.load("default").saved_answers}
    assert {f"sa.q{i}" for i in range(16)} <= ids
