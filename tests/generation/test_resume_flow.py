"""Missing-input resume: stable question keys, user inputs through the real local
store, re-inspection and multi-step forms."""

from __future__ import annotations

from datetime import UTC, datetime

from interviewmaxxing_core import (
    AnswerReuse,
    AnswerSource,
    ApplicationState,
    ChoiceValue,
    FieldOption,
    MissingReason,
    MultiChoiceValue,
    PacketContext,
    TextValue,
    UserInput,
)
from interviewmaxxing_generation import missing_input_id

LATER = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


def _reinspect(form, **field_updates):
    """The same page inspected again later: new timestamp and selectors, unless a
    field's question is explicitly changed via ``field_updates``."""
    fields = []
    for f in form.fields:
        update = {"selector": f.selector + "-v2", **field_updates.get(f.id, {})}
        fields.append(f.model_copy(update=update))
    return form.model_copy(update={"fields": fields, "inspected_at": LATER})


def test_missing_input_ids_are_stable_across_reinspection(
    mock_form, fictional_candidate, make_context, resolve
):
    first = resolve(make_context(mock_form, fictional_candidate))
    again = resolve(make_context(_reinspect(mock_form), fictional_candidate))
    # Becoming optional/required does not change the question either.
    toggled = _reinspect(mock_form, phone={"required": True})
    third = resolve(make_context(toggled, fictional_candidate))

    assert first.id != again.id
    assert [m.id for m in first.missing_inputs] == [m.id for m in again.missing_inputs]
    assert {m.id for m in first.missing_inputs} <= {m.id for m in third.missing_inputs}
    assert [a.field_id for a in first.answers] == [a.field_id for a in again.answers]
    for item in first.missing_inputs:
        assert item.id == missing_input_id(mock_form, mock_form.field(item.field_id))


def test_user_answers_complete_the_packet_through_the_store(
    store, mock_form, fictional_candidate, mock_identity, resolve
):
    request = store.record_request(fictional_candidate.id, mock_form.url)
    app = request.application
    claim = store.claim(app.id, "generation-test")
    store.transition(claim, ApplicationState.INSPECTING)
    job = store.bind_job_identity(claim, mock_identity).job
    app = store.get_application(app.id)

    context = PacketContext(application=app, job=job, form=mock_form,
                            candidate=fictional_candidate,
                            user_inputs=store.get_user_inputs(app.id, mock_form))
    first = resolve(context)
    assert context.problems(first) == []
    store.save_packet(claim, first)
    store.transition(claim, ApplicationState.NEEDS_INPUT)
    assert set(first.unresolved_fields) == {"sponsorship", "gender", "why_us"}

    answers = {
        "sponsorship": ChoiceValue(value="no", label="No"),
        "gender": ChoiceValue(value="decline", label="I decline to self-identify"),
        "why_us": TextValue(text="Fictional answer supplied by the test user."),
    }
    inputs = [UserInput.answering(m, answers[m.field_id]) for m in first.missing_inputs]
    store.save_user_inputs(claim, inputs)
    store.transition(claim, ApplicationState.INSPECTING)

    page = _reinspect(mock_form)
    context = PacketContext(application=store.get_application(app.id), job=job, form=page,
                            candidate=fictional_candidate,
                            user_inputs=store.get_user_inputs(app.id, page))
    second = resolve(context)

    assert context.problems(second) == []
    assert second.is_complete and second.missing_inputs == []
    assert second.form_fingerprint == page.fingerprint == mock_form.fingerprint
    by_id = {u.field_id: u for u in inputs}
    for field_id, value in answers.items():
        answer = second.answer_for(field_id)
        assert answer.value == value
        assert answer.provenance.source is AnswerSource.USER_INPUT
        assert answer.provenance.reference_ids == [by_id[field_id].id]
    # Everything answered before is answered the same way again.
    for answer in first.answers:
        assert second.answer_for(answer.field_id) == answer
    store.save_packet(claim, second)


def test_changed_question_is_asked_again_with_a_new_key(
    store, mock_form, fictional_candidate, make_context, resolve
):
    first = resolve(make_context(mock_form, fictional_candidate))
    app = store.record_request(fictional_candidate.id, mock_form.url).application
    claim = store.claim(app.id, "generation-test")
    why = next(m for m in first.missing_inputs if m.field_id == "why_us")
    gender = next(m for m in first.missing_inputs if m.field_id == "gender")
    store.save_user_inputs(claim, [
        UserInput.answering(why, TextValue(text="Fictional motivation.")),
        UserInput.answering(gender, ChoiceValue(value="decline",
                                                label="I decline to self-identify")),
    ])

    changed = _reinspect(mock_form, why_us={"help_text": "Mention a product you use daily."})
    inputs = store.get_user_inputs(app.id, changed)
    assert [u.field_id for u in inputs] == ["gender"]
    packet = resolve(make_context(changed, fictional_candidate, user_inputs=inputs))

    item = next(m for m in packet.missing_inputs if m.field_id == "why_us")
    assert item.id != why.id and item.field_fingerprint != why.field_fingerprint
    assert "Mention a product you use daily." in item.prompt
    assert packet.answer_for("gender").provenance.source is AnswerSource.USER_INPUT


def test_user_input_takes_precedence_over_a_saved_answer(
    mock_form, fictional_candidate, make_context, resolve
):
    mine = UserInput.for_field(mock_form, "work_auth", ChoiceValue(value="0", label="No"))
    packet = resolve(make_context(mock_form, fictional_candidate, user_inputs=[mine]))
    answer = packet.answer_for("work_auth")
    assert answer.value == ChoiceValue(value="0", label="No")
    assert answer.provenance.reference_ids == [mine.id]


def test_latest_user_input_for_a_question_wins(
    mock_form, fictional_candidate, make_context, resolve
):
    draft = UserInput.for_field(mock_form, "why_us", TextValue(text="Draft"))
    final = UserInput.for_field(mock_form, "why_us", TextValue(text="Final")).model_copy(
        update={"provided_at": LATER})
    packet = resolve(make_context(mock_form, fictional_candidate, user_inputs=[final, draft]))
    assert packet.answer_for("why_us").value == TextValue(text="Final")


def test_earlier_answer_invalid_for_the_current_field_is_asked_again(
    mock_form, fictional_candidate, make_context, resolve
):
    none_chosen = UserInput.for_field(mock_form, "channels", MultiChoiceValue(choices=[]))
    now_required = _reinspect(mock_form, channels={"required": True})
    packet = resolve(make_context(now_required, fictional_candidate, user_inputs=[none_chosen]))
    item = next(m for m in packet.missing_inputs if m.field_id == "channels")
    assert item.reason is MissingReason.NO_ANSWER
    assert "earlier answer cannot be used" in item.prompt


def test_multistep_answers_stay_on_their_step(
    store, multistep_forms, fictional_candidate, make_context, resolve
):
    step0, step1 = multistep_forms
    first0 = resolve(make_context(step0, fictional_candidate))
    first1 = resolve(make_context(step1, fictional_candidate))
    # Relocation has no saved answer; "paid search" years are not "paid media" years.
    assert first0.unresolved_fields == ["question_0"]
    assert first1.unresolved_fields == ["question_0"]
    assert first0.missing_inputs[0].id != first1.missing_inputs[0].id

    app = store.record_request(fictional_candidate.id, step0.url).application
    claim = store.claim(app.id, "generation-test")
    store.save_user_inputs(claim, [
        UserInput.answering(first0.missing_inputs[0], ChoiceValue(value="no", label="No"))])

    step0_inputs = store.get_user_inputs(app.id, step0)
    assert store.get_user_inputs(app.id, step1) == []
    answered0 = resolve(make_context(step0, fictional_candidate, user_inputs=step0_inputs))
    still1 = resolve(make_context(step1, fictional_candidate))
    assert answered0.is_complete
    assert still1.unresolved_fields == ["question_0"]
    assert still1.missing_inputs[0].id == first1.missing_inputs[0].id


def test_answer_options_mirror_the_field(mock_form, fictional_candidate, make_context, resolve):
    packet = resolve(make_context(mock_form, fictional_candidate))
    gender = next(m for m in packet.missing_inputs if m.field_id == "gender")
    assert gender.options == mock_form.field("gender").options
    assert FieldOption(value="decline", label="I decline to self-identify") in gender.options


def test_answer_saved_for_reuse_answers_the_same_question_on_another_job(
    mock_form, fictional_candidate, mock_job, make_context, resolve
):
    first = resolve(make_context(mock_form, fictional_candidate))
    item = next(m for m in first.missing_inputs if m.field_id == "sponsorship")
    given = UserInput.answering(item, ChoiceValue(value="no", label="No"),
                                reuse=AnswerReuse.GLOBAL)
    saved = given.to_saved_answer(job=mock_job, answer_id="sa.from_user")
    candidate = fictional_candidate.model_copy(
        update={"saved_answers": [*fictional_candidate.saved_answers, saved]})

    other_job = mock_job.model_copy(update={"id": "job_other", "identity_key": "ats:mock:x:1"})
    other_form = mock_form.model_copy(update={"url": "http://127.0.0.1:0/jobs/other/apply"})
    packet = resolve(make_context(other_form, candidate, job=other_job))
    answer = packet.answer_for("sponsorship")
    assert answer.value == ChoiceValue(value="no", label="No")
    assert answer.provenance.reference_ids == ["sa.from_user"]

    # APPLICATION reuse (the default) is never saved, so it is asked again elsewhere.
    assert UserInput.answering(item, ChoiceValue(value="no", label="No")).to_saved_answer(
        job=mock_job) is None
