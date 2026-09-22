"""Fixture-to-packet tests for ``FactualPacketResolver`` (fictional candidates only)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from interviewmaxxing_core import (
    EXPLICIT_ANSWER_REQUIRED,
    PROFILE_IDENTITY_TYPES,
    AnswerSource,
    ApplicationField,
    ApplicationForm,
    CandidateFact,
    ChoiceValue,
    ControlType,
    FactVerification,
    FieldOption,
    MissingReason,
    MultiChoiceValue,
    PacketResolver,
    SemanticType,
    TextValue,
    UserInput,
    VerificationMethod,
    VerificationStatus,
)
from interviewmaxxing_generation import FactualPacketResolver, QuestionText, saved_answer_matches

VERIFIED = FactVerification(
    status=VerificationStatus.VERIFIED,
    method=VerificationMethod.USER_STATED,
    verified_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
)


def test_implements_the_core_protocol():
    assert isinstance(FactualPacketResolver(), PacketResolver)


def test_core_fixture_form_resolves_to_the_expected_packet(
    mock_form, fictional_candidate, make_context, resolve, projection, generation_fixture
):
    context = make_context(mock_form, fictional_candidate)
    packet = resolve(context)

    assert context.problems(packet) == []
    assert projection(packet) == generation_fixture("expected_mock_form_packet.json")
    assert packet.form_fingerprint == mock_form.fingerprint
    assert (packet.form_url, packet.form_step) == (mock_form.url, mock_form.step)
    assert packet.application_id == context.application.id
    assert not packet.is_complete
    assert packet.cover_letter is None


def test_screening_form_resolves_to_the_expected_packet(
    screening_form, extended_candidate, make_context, resolve, projection, generation_fixture
):
    context = make_context(screening_form, extended_candidate)
    packet = resolve(context)

    assert context.problems(packet) == []
    assert projection(packet) == generation_fixture("expected_screening_packet.json")


def test_every_answer_has_truthful_provenance(
    screening_form, extended_candidate, mock_job, make_context, resolve
):
    packet = resolve(make_context(screening_form, extended_candidate))
    for answer in packet.answers:
        field = screening_form.field(answer.field_id)
        source, refs = answer.provenance.source, answer.provenance.reference_ids
        if source is AnswerSource.PROFILE_IDENTITY:
            assert field.semantic_type in PROFILE_IDENTITY_TYPES
        elif source in (AnswerSource.CANDIDATE_FACT, AnswerSource.GENERATED_FROM_FACTS):
            facts = [extended_candidate.fact(r) for r in refs]
            assert facts and all(f.is_verified for f in facts)
            if isinstance(answer.value, TextValue):
                for fact in facts:
                    assert str(fact.value) in answer.value.text
        elif source is AnswerSource.SAVED_ANSWER:
            question = QuestionText.of(field)
            for ref in refs:
                saved = extended_candidate.find_saved_answer(ref)
                assert saved is not None and saved.applies_to(mock_job)
                assert saved_answer_matches(saved, question)
        elif source is AnswerSource.RESUME:
            assert refs == [extended_candidate.resume.id]
        else:  # pragma: no cover - no user inputs in this context
            pytest.fail(f"unexpected source {source}")
        if field.semantic_type in EXPLICIT_ANSWER_REQUIRED:
            assert source is AnswerSource.SAVED_ANSWER

    cited = {r for a in packet.answers for r in a.provenance.reference_ids}
    unverified = {f.id for f in extended_candidate.facts if not f.is_verified}
    assert unverified and not cited & unverified


def test_missing_inputs_are_scoped_to_the_question(
    screening_form, extended_candidate, make_context, resolve
):
    packet = resolve(make_context(screening_form, extended_candidate))
    assert packet.missing_inputs
    for item in packet.missing_inputs:
        field = screening_form.field(item.field_id)
        assert item.matches(screening_form)
        assert (item.form_step, item.field_fingerprint) == (screening_form.step, field.fingerprint)
        assert item.required and field.required
        assert (item.control_type, item.options, item.semantic_type) == (
            field.control_type, field.options, field.semantic_type)
        assert field.label in item.prompt


def test_optional_unknown_fields_are_left_blank(
    screening_form, extended_candidate, make_context, resolve
):
    packet = resolve(make_context(screening_form, extended_candidate))
    accounted = {a.field_id for a in packet.answers} | set(packet.unresolved_fields)
    blank = {f.id for f in screening_form.fields if f.id not in accounted}
    # no website, no saved gender, no written campaign story, an unsupported optional
    # widget, and no cover letter: all optional, so nothing is asked or invented.
    assert blank == {"website", "gender", "achievement", "scheduler", "cover_letter"}
    assert all(not screening_form.field(f).required for f in blank)
    assert packet.is_complete is False


def test_required_fields_are_always_answered_or_missing(
    screening_form, extended_candidate, make_context, resolve
):
    packet = resolve(make_context(screening_form, extended_candidate))
    accounted = {a.field_id for a in packet.answers} | set(packet.unresolved_fields)
    assert {f.id for f in screening_form.required_fields()} <= accounted


def _fact(fact_id: str, key: str, value: object) -> CandidateFact:
    return CandidateFact(id=fact_id, key=key, value=value, source="user", verification=VERIFIED)


def test_explicit_types_are_never_inferred_from_facts_or_identity(
    fictional_candidate, make_context, resolve
):
    """Verified facts that *look* like eligibility, pay or protected data never answer
    those questions; only an explicit saved answer or user input may."""
    candidate = fictional_candidate.model_copy(update={
        "facts": [
            _fact("fact.auth", "work_authorization", True),
            _fact("fact.sponsor", "requires_sponsorship", False),
            _fact("fact.salary", "salary_expectation", 150000),
            _fact("fact.gender", "gender", "Female"),
            _fact("fact.consent", "consent", True),
        ],
        "saved_answers": [],
        "experience": [],
    })
    yes_no = [FieldOption(value="y", label="Yes"), FieldOption(value="n", label="No")]
    fields = [
        ApplicationField(id="auth", label="Work authorization", selector="#a", required=True,
                         semantic_type=SemanticType.WORK_AUTHORIZATION,
                         control_type=ControlType.RADIO, options=yes_no),
        ApplicationField(id="sponsor", label="Sponsorship", selector="#b", required=True,
                         semantic_type=SemanticType.SPONSORSHIP,
                         control_type=ControlType.RADIO, options=yes_no),
        ApplicationField(id="salary", label="Salary expectation", selector="#c", required=True,
                         semantic_type=SemanticType.SALARY_EXPECTATION,
                         control_type=ControlType.TEXT),
        ApplicationField(id="gender", label="Gender", selector="#d", required=True,
                         semantic_type=SemanticType.EEO_GENDER, control_type=ControlType.SELECT,
                         options=[FieldOption(value="f", label="Female"),
                                  FieldOption(value="m", label="Male")]),
        ApplicationField(id="pronouns", label="Pronouns", selector="#e", required=True,
                         semantic_type=SemanticType.PRONOUNS, control_type=ControlType.TEXT),
        ApplicationField(id="consent", label="I consent", selector="#f", required=True,
                         semantic_type=SemanticType.CONSENT, control_type=ControlType.CHECKBOX),
        ApplicationField(id="attest", label="I certify the above", selector="#g", required=True,
                         semantic_type=SemanticType.ATTESTATION,
                         control_type=ControlType.CHECKBOX),
        ApplicationField(id="optional_race", label="Race", selector="#h", required=False,
                         semantic_type=SemanticType.EEO_RACE_ETHNICITY,
                         control_type=ControlType.TEXT),
        # A custom question whose words resemble a fact key is still not answered.
        ApplicationField(id="custom_auth", label="Work authorization", selector="#i",
                         required=True, semantic_type=SemanticType.CUSTOM_TEXT,
                         control_type=ControlType.TEXT),
    ]
    form = ApplicationForm(url="http://127.0.0.1:0/jobs/mock-4012/apply", fields=fields)
    context = make_context(form, candidate)
    packet = resolve(context)

    assert context.problems(packet) == []
    assert packet.answers == []
    reasons = {m.field_id: m.reason for m in packet.missing_inputs}
    assert reasons == {
        "auth": MissingReason.EXPLICIT_ANSWER_REQUIRED,
        "sponsor": MissingReason.EXPLICIT_ANSWER_REQUIRED,
        "salary": MissingReason.EXPLICIT_ANSWER_REQUIRED,
        "gender": MissingReason.EXPLICIT_ANSWER_REQUIRED,
        "pronouns": MissingReason.EXPLICIT_ANSWER_REQUIRED,
        "consent": MissingReason.EXPLICIT_ANSWER_REQUIRED,
        "attest": MissingReason.UNCOVERED_ATTESTATION,
        "custom_auth": MissingReason.NO_ANSWER,
    }
    assert "never inferred" in packet.missing_inputs[0].prompt


def test_job_scoped_saved_answers_apply_only_to_their_job(
    screening_form, extended_candidate, mock_job, make_context, resolve
):
    mock_packet = resolve(make_context(screening_form, extended_candidate))
    # The Other Co salary answer has the same wording but belongs to another job.
    assert "salary" in mock_packet.unresolved_fields
    assert mock_packet.answer_for("start_date").provenance.reference_ids == ["sa.mock_co_start"]

    other_job = mock_job.model_copy(update={
        "id": "job_other", "identity_key": "ats:mock:other-co:9001", "company": "Other Co"})
    other_packet = resolve(make_context(screening_form, extended_candidate, job=other_job))
    salary = other_packet.answer_for("salary")
    assert salary is not None and salary.value == TextValue(text="150000")
    assert salary.provenance.reference_ids == ["sa.other_co_salary"]
    assert "start_date" in other_packet.unresolved_fields

    unbound = mock_job.model_copy(update={"identity_key": None})
    unbound_packet = resolve(make_context(screening_form, extended_candidate, job=unbound))
    assert "start_date" in unbound_packet.unresolved_fields


def test_similar_but_different_questions_do_not_reuse_answers(
    mock_form, fictional_candidate, screening_form, extended_candidate, make_context, resolve
):
    core = resolve(make_context(mock_form, fictional_candidate))
    # Saved for "... require visa sponsorship?"; the form asks "... require sponsorship?".
    assert "sponsorship" in core.unresolved_fields

    packet = resolve(make_context(screening_form, extended_candidate))
    for field_id in ("relocate_denver", "work_auth_ca", "dismissal", "years_search", "years_b2b"):
        assert field_id in packet.unresolved_fields, field_id
    # Same label "I agree": the help text decides which attestation it is.
    assert packet.answer_for("accuracy").provenance.reference_ids == ["sa.accuracy_attestation"]


def test_changed_help_text_makes_an_exact_saved_answer_inapplicable(
    screening_form, extended_candidate, make_context, resolve
):
    accuracy = screening_form.field("accuracy").model_copy(
        update={"help_text": "I certify that I hold every license this role requires."})
    form = screening_form.model_copy(update={
        "fields": [accuracy if f.id == "accuracy" else f for f in screening_form.fields]})
    packet = resolve(make_context(form, extended_candidate))
    assert packet.answer_for("accuracy") is None
    missing = next(m for m in packet.missing_inputs if m.field_id == "accuracy")
    assert missing.reason is MissingReason.UNCOVERED_ATTESTATION
    assert "hold every license" in missing.prompt


def test_boolean_and_zero_values_are_kept(
    screening_form, extended_candidate, make_context, resolve
):
    packet = resolve(make_context(screening_form, extended_candidate))
    assert packet.answer_for("years_python").value == TextValue(text="0")
    assert packet.answer_for("direct_reports").value == TextValue(text="0")
    assert packet.answer_for("relocate").value == ChoiceValue(value="0", label="No")
    sponsorship = packet.answer_for("sponsorship")
    # "No" and False from two saved answers to the same question agree.
    assert sponsorship.value == ChoiceValue(value="n", label="No")
    assert sponsorship.provenance.reference_ids == ["sa.sponsorship", "sa.visa_sponsorship"]
    # A saved "No" to a required consent checkbox is respected, never flipped.
    background = next(m for m in packet.missing_inputs if m.field_id == "background")
    assert background.reason is MissingReason.EXPLICIT_ANSWER_REQUIRED
    assert "left unchecked" in background.prompt


def test_choices_are_actual_options_translated_exactly(
    screening_form, extended_candidate, make_context, resolve
):
    packet = resolve(make_context(screening_form, extended_candidate))
    for answer in packet.answers:
        field = screening_form.field(answer.field_id)
        chosen = (
            [answer.value] if isinstance(answer.value, ChoiceValue)
            else answer.value.choices if isinstance(answer.value, MultiChoiceValue) else []
        )
        for choice in chosen:
            option = field.option_for_value(choice.value)
            assert option is not None and not option.disabled and option.value
            assert option.label == choice.label

    assert packet.answer_for("country").value == ChoiceValue(
        value="USA", label="United States of America")
    assert packet.answer_for("state").value == ChoiceValue(value="OR", label="Oregon")
    assert packet.answer_for("years_total").value == ChoiceValue(value="c", label="6-10 years")
    # "Yes" is not "Yes, a full license": the user is asked instead.
    license_item = next(m for m in packet.missing_inputs if m.field_id == "license")
    assert "not one of the options" in license_item.prompt


def test_ambiguous_saved_answers_offer_valid_candidates(
    screening_form, extended_candidate, make_context, resolve
):
    packet = resolve(make_context(screening_form, extended_candidate))
    heard = next(m for m in packet.missing_inputs if m.field_id == "heard_from")
    assert heard.reason is MissingReason.AMBIGUOUS
    assert heard.candidates == [ChoiceValue(value="board", label="Job board"),
                                ChoiceValue(value="referral", label="Employee referral")]
    answer = UserInput.answering(heard, heard.candidates[1])
    assert answer.matches(screening_form)


def test_free_text_is_only_assembled_when_facts_answer_the_question(
    screening_form, extended_candidate, make_context, resolve
):
    packet = resolve(make_context(screening_form, extended_candidate))
    role = packet.answer_for("role")
    assert role.provenance.source is AnswerSource.GENERATED_FROM_FACTS
    assert role.value == TextValue(text="Paid Media Lead at Fictional Widgets Co")
    assert role.provenance.reference_ids == ["fact.current_title", "fact.current_company"]
    assert packet.answer_for("why") is None
    why = next(m for m in packet.missing_inputs if m.field_id == "why")
    assert why.reason is MissingReason.NO_ANSWER
    assert "1500 characters" in why.prompt


def test_facts_that_disagree_are_not_chosen_between(
    fictional_candidate, screening_form, make_context, resolve
):
    candidate = fictional_candidate.model_copy(update={
        "facts": [*fictional_candidate.facts, _fact("fact.title_2", "current_title", "Director")]})
    packet = resolve(make_context(screening_form, candidate))
    assert "current_title" in packet.unresolved_fields
    assert packet.answer_for("role") is None


def test_changed_resume_file_is_not_uploaded(
    tmp_path, mock_form, fictional_candidate, make_context, resolve
):
    copy = tmp_path / "resume.pdf"
    copy.write_bytes(b"%PDF-1.4 a different fictional file")
    resume = fictional_candidate.resume.model_copy(update={"path": str(copy)})
    packet = resolve(make_context(mock_form, fictional_candidate.model_copy(
        update={"resume": resume})))
    item = next(m for m in packet.missing_inputs if m.field_id == "resume")
    assert "changed" in item.prompt
