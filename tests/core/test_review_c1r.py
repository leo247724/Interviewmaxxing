"""Regression checks for the C1 review (C1R): form identity, question scope, choice
validity, saved-answer scope and verified facts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from interviewmaxxing_core import (
    AnswerReuse,
    AnswerScope,
    Application,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    ArtifactRef,
    BooleanValue,
    CandidateFact,
    ChoiceValue,
    ControlType,
    FactVerification,
    FieldOption,
    FileValue,
    MissingInput,
    MissingReason,
    MultiChoiceValue,
    PacketAnswer,
    PacketContext,
    Provenance,
    SavedAnswer,
    SemanticType,
    TextValue,
    UserInput,
    VerificationStatus,
    answer_problems,
    provenance_problems,
)

NOW = datetime(2026, 9, 22, 20, 0, tzinfo=UTC)


def _application(packet) -> Application:
    return Application(
        id=packet.application_id, request_id="req_fixture", job_id=packet.job_id,
        candidate_id=packet.candidate_id, state=ApplicationState.INSPECTING, version=2,
        created_at=NOW, updated_at=NOW,
    )


def _with_answer(packet, answer):
    return packet.model_copy(update={"answers": [*packet.answers, answer]})


# --- 1. packet/form identity and field-bound validation ----------------------------


def test_fixture_packet_is_valid_for_its_context(
    mock_packet, mock_form, mock_job, fictional_candidate
):
    ctx = PacketContext(application=_application(mock_packet), job=mock_job, form=mock_form,
                        candidate=fictional_candidate)
    assert ctx.problems(mock_packet) == []


@pytest.mark.parametrize(
    "update",
    [{"url": "http://127.0.0.1:0/jobs/other/apply"}, {"step": 1}],
    ids=["other-url", "other-step"],
)
def test_packet_for_another_form_step_is_rejected(mock_packet, mock_form, update):
    problems = mock_packet.problems_against(mock_form.model_copy(update=update))
    assert problems and "not the current form" in problems[0]


def test_packet_for_an_older_inspection_of_the_same_step_is_rejected(mock_packet, mock_form):
    relabeled = mock_form.field("why_us").model_copy(update={"label": "Why us? (new question)"})
    fields = [relabeled if f.id == "why_us" else f for f in mock_form.fields]
    problems = mock_packet.problems_against(mock_form.model_copy(update={"fields": fields}))
    assert any("different inspection" in p for p in problems)
    assert any("missing input 'why_us'" in p for p in problems)


def test_tracking_parameters_do_not_change_form_identity(mock_packet, mock_form):
    same = mock_form.model_copy(update={"url": mock_form.url + "?utm_source=x"})
    assert same.fingerprint == mock_form.fingerprint
    assert mock_packet.problems_against(same) == []


def test_reviewer_case_attestation_answered_as_custom_boolean_fact(mock_packet, mock_form):
    attestation = ApplicationField(
        id="certify", label="I certify that my answers are true", selector="#certify",
        semantic_type=SemanticType.ATTESTATION, control_type=ControlType.CHECKBOX, required=True,
    )
    form = mock_form.model_copy(update={"fields": [*mock_form.fields, attestation]})
    packet = _with_answer(
        mock_packet.model_copy(update={"form_fingerprint": form.fingerprint}),
        PacketAnswer(
            field_id="certify", semantic_type=SemanticType.CUSTOM_BOOLEAN,
            value=BooleanValue(checked=True),
            provenance=Provenance(source="CANDIDATE_FACT", reference_ids=["fact.current_title"]),
        ),
    )
    problems = packet.problems_against(form)
    assert any("claims CUSTOM_BOOLEAN, field is ATTESTATION" in p for p in problems)
    assert any("needs a saved answer or user input, not CANDIDATE_FACT" in p for p in problems)


def test_semantic_type_must_match_even_with_explicit_provenance(mock_packet, mock_form):
    answers = [
        a.model_copy(update={"semantic_type": SemanticType.WORK_AUTHORIZATION})
        if a.field_id == "sponsorship" else a
        for a in mock_packet.answers
    ]
    problems = mock_packet.model_copy(update={"answers": answers}).problems_against(mock_form)
    assert problems == [
        "answer for 'sponsorship' claims WORK_AUTHORIZATION, field is SPONSORSHIP"
    ]


def test_profile_identity_only_fills_identity_fields(mock_packet, mock_form):
    missing = [m for m in mock_packet.missing_inputs if m.field_id != "why_us"]
    packet = _with_answer(
        mock_packet.model_copy(update={"missing_inputs": missing}),
        PacketAnswer(field_id="why_us", semantic_type=SemanticType.CUSTOM_LONG_TEXT,
                     value=TextValue(text="Avery Example"),
                     provenance=Provenance(source="PROFILE_IDENTITY")),
    )
    assert packet.problems_against(mock_form) == [
        "'why_us' (CUSTOM_LONG_TEXT) is not an identity field"
    ]


# --- 2. question scope ---------------------------------------------------------------


def test_missing_inputs_and_user_inputs_carry_question_scope(mock_packet, mock_form):
    gender = mock_packet.missing_inputs[0]
    assert gender.scope == mock_form.scope
    assert gender.field_fingerprint == mock_form.field("gender").fingerprint
    with pytest.raises(ValidationError, match="form_url, form_step and field_fingerprint"):
        MissingInput(field_id="gender", label="Gender", reason=MissingReason.NO_ANSWER,
                     prompt="?")


def test_user_input_is_validated_against_the_question(mock_packet):
    gender = mock_packet.missing_inputs[0]
    with pytest.raises(ValueError, match="not an option"):
        UserInput.answering(gender, ChoiceValue(value="prefer-not", label="Prefer not"))
    with pytest.raises(ValueError, match="does not match"):
        UserInput.answering(gender, ChoiceValue(value="f", label="Male"))
    action = MissingInput(field_id=None, label="Sign in", reason=MissingReason.USER_ACTION,
                          prompt="Sign in in the browser window")
    with pytest.raises(ValueError, match="USER_ACTION"):
        UserInput.answering(action, TextValue(text="done"))


def test_packet_context_rejects_inputs_for_other_steps_or_questions(
    mock_packet, mock_form, mock_job, fictional_candidate, multistep_forms, gender_input
):
    step0, step1 = multistep_forms
    app = _application(mock_packet)
    PacketContext(application=app, job=mock_job, form=mock_form,
                  candidate=fictional_candidate, user_inputs=[gender_input])
    other_step = UserInput.for_field(step1, "question_0", TextValue(text="7"))
    with pytest.raises(ValueError, match="another form step or question"):
        PacketContext(application=app, job=mock_job, form=step0,
                      candidate=fictional_candidate, user_inputs=[other_step])


def test_user_input_provenance_must_match_question_and_value(
    mock_packet, mock_form, mock_job, fictional_candidate, gender_input
):
    ctx = PacketContext(application=_application(mock_packet), job=mock_job, form=mock_form,
                        candidate=fictional_candidate, user_inputs=[gender_input])
    missing = [m for m in mock_packet.missing_inputs if m.field_id != "gender"]
    base = mock_packet.model_copy(update={"missing_inputs": missing})
    good = PacketAnswer(field_id="gender", semantic_type=SemanticType.EEO_GENDER,
                        value=gender_input.value,
                        provenance=Provenance(source="USER_INPUT", reference_ids=[gender_input.id]))
    assert ctx.problems(_with_answer(base, good)) == []
    altered = good.model_copy(update={"value": ChoiceValue(value="f", label="Female")})
    assert "'gender' does not use the value the user gave" in ctx.problems(
        _with_answer(base, altered)
    )
    unknown = good.model_copy(
        update={"provenance": Provenance(source="USER_INPUT", reference_ids=["ui_nope"])}
    )
    assert "'gender' cites unknown user input 'ui_nope'" in ctx.problems(
        _with_answer(base, unknown)
    )


# --- 3. choice validity ---------------------------------------------------------------


COUNTRY = ApplicationField(
    id="country", label="Country", control_type=ControlType.SELECT, selector="#country",
    required=True, semantic_type=SemanticType.COUNTRY,
    options=[
        FieldOption(value="", label="Select a country", disabled=True),
        FieldOption(value="US", label="United States"),
        FieldOption(value="CA", label="Canada"),
        FieldOption(value="XX", label="Unavailable", disabled=True),
    ],
)


@pytest.mark.parametrize(
    "value, label, expected",
    [
        ("US", "Canada", "does not match"),
        ("XX", "Unavailable", "disabled"),
        ("", "Select a country", "empty option"),
        ("GB", "United Kingdom", "not an option"),
    ],
)
def test_single_choice_must_be_an_enabled_matching_option(value, label, expected):
    problems = answer_problems(COUNTRY, ChoiceValue(value=value, label=label))
    assert any(expected in p for p in problems), problems


def test_single_choice_label_comparison_ignores_case_and_spacing():
    assert answer_problems(COUNTRY, ChoiceValue(value="US", label="  united   STATES ")) == []


def test_required_select_placeholder_is_not_an_answer(mock_form):
    sponsorship = mock_form.field("sponsorship")
    problems = answer_problems(sponsorship, ChoiceValue(value="", label="Select..."))
    assert any("disabled" in p for p in problems) and any("empty option" in p for p in problems)
    enabled_placeholder = sponsorship.model_copy(update={"options": [
        FieldOption(value="", label="Select..."), *(sponsorship.options or [])[1:]
    ]})
    assert any("empty option" in p for p in answer_problems(
        enabled_placeholder, ChoiceValue(value="", label="Select...")
    ))


def test_multi_choice_validity(mock_form):
    channels = mock_form.field("channels")
    search = FieldOption(value="search", label="Paid search")
    assert answer_problems(channels, MultiChoiceValue(choices=[search])) == []
    repeated = answer_problems(channels, MultiChoiceValue(choices=[search, search]))
    assert any("more than once" in p for p in repeated)
    mislabeled = FieldOption(value="search", label="Paid social")
    assert any("does not match" in p for p in answer_problems(
        channels, MultiChoiceValue(choices=[mislabeled])
    ))
    disabled_field = channels.model_copy(update={"options": [
        search, FieldOption(value="display", label="Programmatic display", disabled=True)
    ]})
    disabled = FieldOption(value="display", label="Programmatic display")
    assert any("disabled" in p for p in answer_problems(
        disabled_field, MultiChoiceValue(choices=[disabled])
    ))
    required = channels.model_copy(update={"required": True})
    assert any("no selected options" in p for p in answer_problems(
        required, MultiChoiceValue(choices=[])
    ))


def test_file_answers_respect_accept(mock_form, fictional_candidate):
    resume_field = mock_form.field("resume")
    assert answer_problems(resume_field, FileValue(artifact=fictional_candidate.resume)) == []
    image = ArtifactRef(id="img", path="/tmp/x.png", filename="x.png", media_type="image/png",
                        sha256="0" * 64, size_bytes=1)
    assert any("not accepted" in p for p in answer_problems(resume_field, FileValue(artifact=image)))


# --- 4. saved-answer scope ------------------------------------------------------------


def test_saved_answer_scope_is_explicit_and_consistent():
    base = {"id": "sa.x", "question": "Q", "value": "Yes", "confirmed_at": NOW}
    with pytest.raises(ValidationError, match="scope"):
        SavedAnswer(**base)
    with pytest.raises(ValidationError, match="JOB-scoped"):
        SavedAnswer(**base, scope=AnswerScope.JOB)
    with pytest.raises(ValidationError, match="GLOBAL"):
        SavedAnswer(**base, scope=AnswerScope.GLOBAL, job_identity_key="ats:mock:x:1")
    with pytest.raises(ValidationError, match="GLOBAL"):
        SavedAnswer(**base, scope=AnswerScope.GLOBAL, employer="Mock Co")


def test_job_answers_never_apply_to_another_job(fictional_candidate, mock_job):
    other_co = fictional_candidate.find_saved_answer("sa.other_co_salary")
    mock_co = fictional_candidate.find_saved_answer("sa.mock_co_start")
    assert not other_co.applies_to(mock_job)
    assert mock_co.applies_to(mock_job)
    unbound = mock_job.model_copy(update={"identity_key": None})
    assert not mock_co.applies_to(unbound)  # identity-keyed answers need that identity
    assert fictional_candidate.saved_answers_for(SemanticType.SALARY_EXPECTATION,
                                                 job=mock_job) == []
    by_url = SavedAnswer(id="sa.u", scope=AnswerScope.JOB, question="Q", value="v",
                         confirmed_at=NOW, job_url=mock_job.application_url + "?utm_source=x")
    assert by_url.applies_to(mock_job)
    assert not by_url.applies_to(mock_job.model_copy(update={"normalized_url": "http://x.example/"}))


def test_packet_citing_another_jobs_saved_answer_is_rejected(
    mock_packet, mock_form, mock_job, fictional_candidate
):
    salary = ApplicationField(id="salary", label="Salary expectations", selector="#salary",
                              semantic_type=SemanticType.SALARY_EXPECTATION,
                              control_type=ControlType.TEXT)
    form = mock_form.model_copy(update={"fields": [*mock_form.fields, salary]})
    packet = _with_answer(
        mock_packet.model_copy(update={"form_fingerprint": form.fingerprint}),
        PacketAnswer(field_id="salary", semantic_type=SemanticType.SALARY_EXPECTATION,
                     value=TextValue(text="150000"),
                     provenance=Provenance(source="SAVED_ANSWER",
                                           reference_ids=["sa.other_co_salary"])),
    )
    ctx = PacketContext(application=_application(packet), job=mock_job, form=form,
                        candidate=fictional_candidate)
    assert ctx.problems(packet) == [
        "'salary' cites saved answer 'sa.other_co_salary' scoped to another job"
    ]


def test_saved_answer_type_must_match_the_question(mock_packet, mock_form, mock_job,
                                                   fictional_candidate):
    answers = [
        a.model_copy(update={"provenance": Provenance(source="SAVED_ANSWER",
                                                      reference_ids=["sa.work_auth_us"])})
        if a.field_id == "sponsorship" else a
        for a in mock_packet.answers
    ]
    problems = provenance_problems(
        mock_packet.model_copy(update={"answers": answers}), form=mock_form,
        candidate=fictional_candidate, job=mock_job,
    )
    assert problems == [
        "'sponsorship' (SPONSORSHIP) cites saved answer 'sa.work_auth_us' about WORK_AUTHORIZATION"
    ]


def test_user_answers_stay_local_unless_reuse_is_chosen(gender_input, mock_job, multistep_forms):
    assert gender_input.reuse is AnswerReuse.APPLICATION
    assert gender_input.to_saved_answer(job=mock_job) is None
    job_scoped = gender_input.model_copy(update={"reuse": AnswerReuse.JOB})
    saved = job_scoped.to_saved_answer(job=mock_job)
    assert saved.scope is AnswerScope.JOB
    assert (saved.job_identity_key, saved.employer) == ("ats:mock:mock-co:4012", "Mock Co")
    assert saved.value == "I decline to self-identify"
    assert not saved.applies_to(mock_job.model_copy(update={"identity_key": "ats:mock:o:1"}))
    global_ = gender_input.model_copy(update={"reuse": AnswerReuse.GLOBAL}).to_saved_answer(
        job=mock_job
    )
    assert global_.scope is AnswerScope.GLOBAL and global_.job_identity_key is None
    step0, _ = multistep_forms
    url_only = UserInput.for_field(step0, "question_0", ChoiceValue(value="no", label="No"),
                                   reuse=AnswerReuse.JOB)
    no_identity = mock_job.model_copy(update={"identity_key": None})
    saved_by_url = url_only.to_saved_answer(job=no_identity)
    assert saved_by_url.job_url == no_identity.normalized_url and saved_by_url.applies_to(no_identity)


# --- 5. verified facts ----------------------------------------------------------------


def test_fact_verification_is_explicit_and_consistent():
    with pytest.raises(ValidationError, match="verification"):
        CandidateFact(id="f", key="k", value=1, source="resume")
    with pytest.raises(ValidationError, match="need method and verified_at"):
        FactVerification(status=VerificationStatus.VERIFIED)
    with pytest.raises(ValidationError, match="must not carry"):
        FactVerification(status=VerificationStatus.UNVERIFIED, verified_at=NOW)


def test_confidence_is_not_verification(fictional_candidate):
    team = fictional_candidate.fact("fact.team_size")
    sure = team.model_copy(update={"confidence": 1.0})
    assert not sure.is_verified
    assert "fact.team_size" not in {f.id for f in fictional_candidate.verified_facts()}
    assert "fact.team_size" not in {f.id for f in fictional_candidate.verified_only().facts}


def test_generated_text_citing_unverified_or_unknown_facts_is_rejected(
    mock_packet, mock_form, mock_job, fictional_candidate
):
    missing = [m for m in mock_packet.missing_inputs if m.field_id != "why_us"]
    packet = _with_answer(
        mock_packet.model_copy(update={"missing_inputs": missing}),
        PacketAnswer(field_id="why_us", semantic_type=SemanticType.CUSTOM_LONG_TEXT,
                     value=TextValue(text="I led a team of 12 managing $400k/month."),
                     provenance=Provenance(source="GENERATED_FROM_FACTS",
                                           reference_ids=["fact.team_size", "fact.monthly_spend",
                                                          "fact.invented"])),
    )
    ctx = PacketContext(application=_application(packet), job=mock_job, form=mock_form,
                        candidate=fictional_candidate)
    assert ctx.problems(packet) == [
        "'why_us' cites unverified fact 'fact.team_size'",
        "'why_us' cites unknown fact 'fact.invented'",
    ]


def test_resume_answer_must_be_the_supplied_file(mock_packet, mock_form, mock_job,
                                                 fictional_candidate):
    other = fictional_candidate.resume.model_copy(update={"sha256": "1" * 64})
    answers = [
        a.model_copy(update={"value": FileValue(artifact=other)}) if a.field_id == "resume" else a
        for a in mock_packet.answers
    ]
    problems = provenance_problems(
        mock_packet.model_copy(update={"answers": answers}), form=mock_form,
        candidate=fictional_candidate, job=mock_job,
    )
    assert problems == ["'resume' does not upload the supplied resume file"]


# --- C1R2: question text beyond the label is part of question identity ---------------


def _attestation_form(help_text: str, *, required: bool = True, placeholder: str | None = None):
    agree = ApplicationField(
        id="agree", label="I agree", semantic_type=SemanticType.ATTESTATION,
        control_type=ControlType.CHECKBOX, selector="#agree", required=required,
        help_text=help_text, placeholder=placeholder,
    )
    return ApplicationForm(url="http://127.0.0.1:0/jobs/mock-4012/apply", step=0, fields=[agree])


ACCURATE = "By checking this box I certify that my application is accurate and complete."
NEVER_DISMISSED = "By checking this box I certify that I have never been dismissed from any job."


def test_changed_attestation_help_text_cannot_reuse_old_authorization(
    store, fictional_candidate
):
    request = store.record_request(fictional_candidate.id, "http://127.0.0.1:0/jobs/mock-4012/apply")
    app, job = request.application, request.job
    claim = store.claim(app.id, "run")
    old_form = _attestation_form(ACCURATE)
    missing = MissingInput.for_field(old_form, old_form.field("agree"),
                                     reason=MissingReason.UNCOVERED_ATTESTATION,
                                     prompt="Do you certify this?")
    agreed = UserInput.answering(missing, BooleanValue(checked=True))
    store.save_user_inputs(claim, [agreed])
    old_packet = ApplicationPacket(
        application_id=app.id, job_id=job.id, candidate_id=fictional_candidate.id,
        form_url=old_form.url, form_step=0, form_fingerprint=old_form.fingerprint,
        answers=[PacketAnswer(
            field_id="agree", semantic_type=SemanticType.ATTESTATION, value=agreed.value,
            provenance=Provenance(source="USER_INPUT", reference_ids=[agreed.id]),
        )],
    )
    old_ctx = PacketContext(application=app, job=job, form=old_form,
                            candidate=fictional_candidate,
                            user_inputs=store.get_user_inputs(app.id, old_form))
    assert old_ctx.problems(old_packet) == []  # control: valid for the question answered

    # Same URL, step, id, label and control; only the attestation text changed.
    new_form = _attestation_form(NEVER_DISMISSED)
    assert new_form.field("agree").fingerprint != old_form.field("agree").fingerprint
    assert not agreed.matches(new_form)
    assert not missing.matches(new_form)
    assert store.get_user_inputs(app.id, new_form) == []
    with pytest.raises(ValueError, match="another form step or question"):
        PacketContext(application=app, job=job, form=new_form, candidate=fictional_candidate,
                      user_inputs=[agreed])
    new_ctx = PacketContext(application=app, job=job, form=new_form,
                            candidate=fictional_candidate,
                            user_inputs=store.get_user_inputs(app.id, new_form))
    problems = new_ctx.problems(old_packet)
    assert "packet was resolved against a different inspection of this form step" in problems
    assert f"'agree' cites unknown user input '{agreed.id}'" in problems


def test_question_identity_ignores_formatting_and_requiredness_only():
    base = _attestation_form(ACCURATE).field("agree")
    reformatted = _attestation_form("  by CHECKING this box I certify that my application\n"
                                    " is accurate and complete. ").field("agree")
    now_optional = _attestation_form(ACCURATE, required=False).field("agree")
    with_placeholder = _attestation_form(ACCURATE, placeholder="Type YES").field("agree")
    assert reformatted.fingerprint == base.fingerprint
    assert now_optional.fingerprint == base.fingerprint
    assert with_placeholder.fingerprint != base.fingerprint
