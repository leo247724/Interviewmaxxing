"""C3R2: answers the user saves for reuse come back for the identical question, and
only for it; integrated C2 conflict preservation stays unresolved.

Everything goes through the real path: resolve -> MissingInput ->
``UserInput.answering`` -> ``to_saved_answer`` -> (candidate store) -> resolve.
Fictional data only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_core import (
    AnswerReuse,
    AnswerScope,
    AnswerSource,
    ApplicationField,
    ApplicationForm,
    BooleanValue,
    ControlType,
    MissingReason,
    SavedAnswer,
    SemanticType,
    TextValue,
    UserInput,
)

URL = "http://127.0.0.1:0/jobs/mock-4012/apply"
ATTESTATION_HELP = "I certify that all application information is accurate."


def _attestation(help_text: str = ATTESTATION_HELP) -> ApplicationField:
    return ApplicationField(
        id="certify", label="I agree", help_text=help_text, selector="#certify",
        semantic_type=SemanticType.ATTESTATION, control_type=ControlType.CHECKBOX,
        required=True)


def _salary(placeholder: str = "€") -> ApplicationField:
    return ApplicationField(
        id="salary", label="Expected salary", placeholder=placeholder, selector="#salary",
        semantic_type=SemanticType.SALARY_EXPECTATION, control_type=ControlType.TEXT,
        required=True)


def _form(*fields: ApplicationField, url: str = URL) -> ApplicationForm:
    return ApplicationForm(url=url, fields=list(fields))


def _save_answers(first, answers, reuse, job):
    """What the runner does: answer each missing item, then persist to_saved_answer."""
    saved = []
    for item in first.missing_inputs:
        user_input = UserInput.answering(item, answers[item.field_id], reuse=reuse)
        answer = user_input.to_saved_answer(job=job, answer_id=f"sa.{item.field_id}")
        assert answer is not None
        saved.append(answer)
    return saved


@pytest.mark.parametrize("reuse", [AnswerReuse.GLOBAL, AnswerReuse.JOB])
def test_saved_attestation_and_currency_answers_roundtrip_on_the_identical_form(
    reuse, fictional_candidate, mock_job, make_context, resolve
):
    candidate = fictional_candidate.model_copy(update={"saved_answers": []})
    form = _form(_attestation(), _salary())
    first = resolve(make_context(form, candidate))
    assert set(first.unresolved_fields) == {"certify", "salary"}
    labels = {m.field_id: m.label for m in first.missing_inputs}
    assert labels == {"certify": f"I agree\n{ATTESTATION_HELP}", "salary": "Expected salary\n€"}

    saved = _save_answers(
        first, {"certify": BooleanValue(checked=True), "salary": TextValue(text="90000")},
        reuse, mock_job)
    assert {a.question for a in saved} == set(labels.values())
    expected_scope = AnswerScope.GLOBAL if reuse is AnswerReuse.GLOBAL else AnswerScope.JOB
    assert {a.scope for a in saved} == {expected_scope}

    with_saved = candidate.model_copy(update={"saved_answers": saved})
    reinspected = form.model_copy(update={"inspected_at": datetime(2026, 9, 23, tzinfo=UTC)})
    context = make_context(reinspected, with_saved)
    second = resolve(context)

    assert context.problems(second) == []
    assert second.is_complete and second.missing_inputs == []
    certify, salary = second.answer_for("certify"), second.answer_for("salary")
    assert certify.value == BooleanValue(checked=True)
    assert salary.value == TextValue(text="90000")
    for answer in (certify, salary):
        assert answer.provenance.source is AnswerSource.SAVED_ANSWER
        assert answer.provenance.reference_ids == [f"sa.{answer.field_id}"]


def test_saved_answers_do_not_carry_over_to_changed_help_or_currency(
    fictional_candidate, mock_job, make_context, resolve
):
    candidate = fictional_candidate.model_copy(update={"saved_answers": []})
    first = resolve(make_context(_form(_attestation(), _salary()), candidate))
    saved = _save_answers(
        first, {"certify": BooleanValue(checked=True), "salary": TextValue(text="90000")},
        AnswerReuse.GLOBAL, mock_job)
    with_saved = candidate.model_copy(update={"saved_answers": saved})

    changed = _form(
        _attestation("I certify that I have never been dismissed from employment."),
        _salary("$"))
    packet = resolve(make_context(changed, with_saved))
    reasons = {m.field_id: m.reason for m in packet.missing_inputs}
    assert reasons == {"certify": MissingReason.UNCOVERED_ATTESTATION,
                       "salary": MissingReason.EXPLICIT_ANSWER_REQUIRED}
    assert packet.answers == []


def test_job_reuse_stays_with_its_job(fictional_candidate, mock_job, make_context, resolve):
    candidate = fictional_candidate.model_copy(update={"saved_answers": []})
    form = _form(_attestation(), _salary())
    first = resolve(make_context(form, candidate))
    saved = _save_answers(
        first, {"certify": BooleanValue(checked=True), "salary": TextValue(text="90000")},
        AnswerReuse.JOB, mock_job)
    with_saved = candidate.model_copy(update={"saved_answers": saved})

    other_job = mock_job.model_copy(update={"id": "job_other", "identity_key": "ats:mock:x:1"})
    packet = resolve(make_context(form, with_saved, job=other_job))
    assert set(packet.unresolved_fields) == {"certify", "salary"}


# --- integrated C2: tied conflicts are preserved and stay unresolved ----------------------

T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)
T1 = datetime(2026, 9, 10, 12, tzinfo=UTC)
SALARY_QUESTION = "Expected salary\n€"


def _salary_answer(answer_id: str, value: str, *, job: str | None, at: datetime) -> SavedAnswer:
    return SavedAnswer(
        id=answer_id, scope=AnswerScope.JOB if job else AnswerScope.GLOBAL,
        job_identity_key=job, semantic_type=SemanticType.SALARY_EXPECTATION,
        question=SALARY_QUESTION, value=value, confirmed_at=at)


@pytest.fixture
def loaded_conflicts(tmp_path, fictional_candidate, mock_job):
    """A fictional profile on disk with a GLOBAL salary and two tied, conflicting JOB
    salaries for the Mock Co job, loaded through the real candidate store."""
    candidate_dir = tmp_path / "profile" / fictional_candidate.id
    candidate_dir.mkdir(parents=True)
    profile = fictional_candidate.model_copy(update={"saved_answers": []})
    (candidate_dir / "profile.json").write_text(profile.model_dump_json(indent=2))
    answers = [
        _salary_answer("sa.salary_global", "100000", job=None, at=T0),
        _salary_answer("sa.salary_job_a", "150000", job=mock_job.identity_key, at=T1),
        _salary_answer("sa.salary_job_b", "175000", job=mock_job.identity_key, at=T1),
    ]
    (candidate_dir / "answers.json").write_text(
        json.dumps([a.model_dump(mode="json") for a in answers], indent=2))
    store = LocalCandidateStore(tmp_path / "profile")
    return store.load(fictional_candidate.id), store.load_report(fictional_candidate.id)


def test_loaded_tied_job_conflict_is_ambiguous_not_the_global_fallback(
    loaded_conflicts, mock_job, make_context, resolve
):
    candidate, report = loaded_conflicts
    kept = {a.id for a in candidate.saved_answers}
    assert {"sa.salary_global", "sa.salary_job_a", "sa.salary_job_b"} <= kept
    assert report.answer_conflicts

    context = make_context(_form(_salary()), candidate, job=mock_job)
    packet = resolve(context)

    assert context.problems(packet) == []
    assert packet.answer_for("salary") is None
    item = packet.missing_inputs[0]
    assert item.reason is MissingReason.AMBIGUOUS
    assert item.candidates == [TextValue(text="150000"), TextValue(text="175000")]
    assert TextValue(text="100000") not in item.candidates


def test_loaded_global_answer_still_applies_to_other_jobs(
    loaded_conflicts, mock_job, make_context, resolve
):
    candidate, _ = loaded_conflicts
    other_job = mock_job.model_copy(update={"id": "job_other", "identity_key": "ats:mock:x:1"})
    packet = resolve(make_context(_form(_salary()), candidate, job=other_job))
    answer = packet.answer_for("salary")
    assert answer.value == TextValue(text="100000")
    assert answer.provenance.reference_ids == ["sa.salary_global"]
