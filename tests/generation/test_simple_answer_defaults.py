"""Explicit map defaults reach the existing resolver with their declared scope."""

from datetime import UTC, datetime

import pytest

from interviewmaxxing_candidate.simple_answers import (
    _REUSABLE_PHRASES,
    _REUSABLE_QUESTIONS,
    SimpleAnswers,
)
from interviewmaxxing_core import (
    AnswerScope,
    AnswerSource,
    ApplicationField,
    ControlType,
    FieldOption,
    MissingReason,
    SemanticType,
)

NOW = datetime(2026, 9, 24, 1, tzinfo=UTC)


@pytest.mark.parametrize("question", [
    "Where did you hear about us?",
    "How did you hear about us?",
    "Where did you first hear about the company?",
])
def test_referral_default_answers_the_question_for_different_jobs(
    question, fictional_candidate, mock_form, mock_job, make_context, resolve
):
    data = SimpleAnswers.from_identity(fictional_candidate.identity).model_dump()
    data["referral_source"] = "Company career page"
    answers = SimpleAnswers.model_validate(data).saved_answer_updates(confirmed_at=NOW)
    candidate = fictional_candidate.model_copy(update={"saved_answers": answers})
    form = mock_form.model_copy(update={"fields": [ApplicationField(
        id="referral", selector="#referral", label=question,
        semantic_type=SemanticType.REFERRAL_SOURCE,
        control_type=ControlType.SELECT, required=True,
        options=[FieldOption(value="careers", label="Company career page"),
                 FieldOption(value="friend", label="Employee referral")],
    )]})
    for company in ("First Fictional Employer", "Second Fictional Employer"):
        job = mock_job.model_copy(update={"company": company, "identity_key": company})
        assert answers[0].scope is AnswerScope.GLOBAL and answers[0].applies_to(job)
        context = make_context(form, candidate, job=job)
        packet = resolve(context)
        assert context.problems(packet) == []
        assert packet.answers[0].value.value == "careers"
        assert packet.answers[0].provenance.source is AnswerSource.SAVED_ANSWER


def test_referral_default_does_not_answer_different_referrer_question(
    fictional_candidate, mock_form, make_context, resolve
):
    data = SimpleAnswers.from_identity(fictional_candidate.identity).model_dump()
    data["referral_source"] = "Company career page"
    candidate = fictional_candidate.model_copy(update={"saved_answers":
        SimpleAnswers.model_validate(data).saved_answer_updates(confirmed_at=NOW)})
    form = mock_form.model_copy(update={"fields": [ApplicationField(
        id="referral", selector="#referral", label="Who referred you? Please enter their name.",
        semantic_type=SemanticType.REFERRAL_SOURCE, control_type=ControlType.TEXT, required=True,
    )]})
    packet = resolve(make_context(form, candidate))
    assert packet.answers == []
    assert packet.missing_inputs


def test_employee_referral_no_is_separate_from_referral_source(
    fictional_candidate, mock_form, make_context, resolve
):
    data = SimpleAnswers.from_identity(fictional_candidate.identity).model_dump()
    data.update(referral_source="Company career page", referred_by_current_employee="No")
    candidate = fictional_candidate.model_copy(update={"saved_answers":
        SimpleAnswers.model_validate(data).saved_answer_updates(confirmed_at=NOW)})
    form = mock_form.model_copy(update={"fields": [ApplicationField(
        id="employee_referral", selector="#employee_referral",
        label="Were you referred to this position by a current employee?",
        semantic_type=SemanticType.CUSTOM_BOOLEAN, control_type=ControlType.RADIO, required=True,
        options=[FieldOption(value="yes", label="Yes"), FieldOption(value="no", label="No")],
    )]})
    context = make_context(form, candidate)
    packet = resolve(context)
    assert context.problems(packet) == []
    assert packet.answers[0].value.value == "no"
    assert packet.answers[0].provenance.source is AnswerSource.SAVED_ANSWER


def test_age_work_authorization_and_education_use_explicit_answers(
    fictional_candidate, mock_form, make_context, resolve
):
    data = SimpleAnswers.from_identity(fictional_candidate.identity).model_dump()
    data.update(above_age_18="Yes", authorized_to_work_us="Yes",
                school="Example State University", degree="Bachelor's Degree")
    candidate = fictional_candidate.model_copy(update={"saved_answers":
        SimpleAnswers.model_validate(data).saved_answer_updates(confirmed_at=NOW)})
    fields = [
        ApplicationField(
            id="age", selector="#age", label="Are you at least 18 years old?",
            semantic_type=SemanticType.CUSTOM_BOOLEAN, control_type=ControlType.RADIO,
            required=True, options=[FieldOption(value="yes", label="Yes"),
                                    FieldOption(value="no", label="No")],
        ),
        ApplicationField(
            id="work", selector="#work", label="Are you legally authorized to work in the United States?",
            semantic_type=SemanticType.WORK_AUTHORIZATION, control_type=ControlType.RADIO,
            required=True, options=[FieldOption(value="yes", label="Yes"),
                                    FieldOption(value="no", label="No")],
        ),
        ApplicationField(
            id="school", selector="#school", label="School",
            semantic_type=SemanticType.UNIVERSITY, control_type=ControlType.TEXT, required=True,
        ),
        ApplicationField(
            id="degree", selector="#degree", label="Degree",
            semantic_type=SemanticType.DEGREE, control_type=ControlType.TEXT, required=True,
        ),
    ]
    form = mock_form.model_copy(update={"fields": fields})
    context = make_context(form, candidate)
    packet = resolve(context)
    assert context.problems(packet) == []
    values = {a.field_id: a.value for a in packet.answers}
    assert values["age"].value == values["work"].value == "yes"
    assert values["school"].text == "Example State University"
    assert values["degree"].text == "Bachelor's Degree"
    assert all(a.provenance.source is AnswerSource.SAVED_ANSWER for a in packet.answers)
    assert not packet.missing_inputs


@pytest.mark.parametrize("semantic,question", [
    (SemanticType.CUSTOM_BOOLEAN, "Are you at least 21 years old?"),
    (SemanticType.WORK_AUTHORIZATION, "Are you currently authorized to work in Canada?"),
])
def test_eligibility_answers_do_not_change_threshold_or_country(
    semantic, question, fictional_candidate, mock_form, make_context, resolve
):
    data = SimpleAnswers.from_identity(fictional_candidate.identity).model_dump()
    data.update(above_age_18="Yes", authorized_to_work_us="Yes")
    candidate = fictional_candidate.model_copy(update={"saved_answers":
        SimpleAnswers.model_validate(data).saved_answer_updates(confirmed_at=NOW)})
    form = mock_form.model_copy(update={"fields": [ApplicationField(
        id="eligibility", selector="#eligibility", label=question,
        semantic_type=semantic, control_type=ControlType.TEXT, required=True,
    )]})
    packet = resolve(make_context(form, candidate))
    assert packet.answers == []
    assert packet.missing_inputs


def test_demographics_and_education_dates_fill_their_matching_questions(
    fictional_candidate, mock_form, make_context, resolve
):
    data = SimpleAnswers.from_identity(fictional_candidate.identity).model_dump()
    data.update(hispanic_latino="No", veteran_status="I am not a protected veteran",
                education_discipline="Business Administration",
                education_start_date="August 2017", education_end_date="May 2022")
    candidate = fictional_candidate.model_copy(update={"saved_answers":
        SimpleAnswers.model_validate(data).saved_answer_updates(confirmed_at=NOW)})
    fields = [
        ApplicationField(
            id="hispanic", selector="#hispanic", label="Are you Hispanic/Latino?",
            semantic_type=SemanticType.EEO_RACE_ETHNICITY, control_type=ControlType.RADIO,
            required=True, options=[FieldOption(value="yes", label="Yes"),
                                    FieldOption(value="no", label="No")],
        ),
        ApplicationField(
            id="veteran", selector="#veteran", label="Veteran Status",
            semantic_type=SemanticType.EEO_VETERAN_STATUS, control_type=ControlType.SELECT,
            required=True, options=[FieldOption(value="not_protected", label="I am not a protected veteran"),
                                    FieldOption(value="decline", label="Decline to answer")],
        ),
        ApplicationField(
            id="discipline", selector="#discipline", label="Discipline", help_text="Education",
            semantic_type=SemanticType.CUSTOM_SELECT, control_type=ControlType.SELECT, required=True,
            options=[FieldOption(value="business_admin", label="Business Administration")],
        ),
        ApplicationField(
            id="start", selector="#start", label="Start date", help_text="Education",
            semantic_type=SemanticType.CUSTOM_TEXT, control_type=ControlType.TEXT, required=True,
        ),
        ApplicationField(
            id="end_month", selector="#end_month", label="End date month", help_text="Education",
            semantic_type=SemanticType.CUSTOM_SELECT, control_type=ControlType.SELECT, required=True,
            options=[FieldOption(value="5", label="May"), FieldOption(value="6", label="June")],
        ),
        ApplicationField(
            id="end_year", selector="#end_year", label="End date year", help_text="Education",
            semantic_type=SemanticType.CUSTOM_TEXT, control_type=ControlType.TEXT, required=True,
        ),
    ]
    context = make_context(mock_form.model_copy(update={"fields": fields}), candidate)
    packet = resolve(context)
    assert context.problems(packet) == []
    values = {a.field_id: a.value for a in packet.answers}
    assert values["hispanic"].value == "no"
    assert values["veteran"].value == "not_protected"
    assert values["discipline"].value == "business_admin"
    assert values["start"].text == "2017-08"
    assert values["end_month"].value == "5"
    assert values["end_year"].text == "2022"
    assert all(a.provenance.source is AnswerSource.SAVED_ANSWER for a in packet.answers)
    assert not packet.missing_inputs


@pytest.mark.parametrize("semantic,label,help_text", [
    (SemanticType.START_DATE, "Start date", None),
    (SemanticType.START_DATE, "When can you start?", None),
    (SemanticType.CUSTOM_TEXT, "Start date", "Employment history"),
    (SemanticType.CUSTOM_TEXT, "Month", None),
    (SemanticType.EEO_RACE_ETHNICITY, "Race", None),
    (SemanticType.CUSTOM_BOOLEAN, "Have you ever served in the military?", None),
])
def test_education_and_demographic_answers_do_not_leak_into_other_questions(
    semantic, label, help_text, fictional_candidate, mock_form, make_context, resolve
):
    data = SimpleAnswers.from_identity(fictional_candidate.identity).model_dump()
    data.update(hispanic_latino="No", veteran_status="I am not a protected veteran",
                education_start_date="2017-08", education_end_date="2022-05")
    candidate = fictional_candidate.model_copy(update={"saved_answers":
        SimpleAnswers.model_validate(data).saved_answer_updates(confirmed_at=NOW)})
    form = mock_form.model_copy(update={"fields": [ApplicationField(
        id="unrelated", selector="#unrelated", label=label, help_text=help_text,
        semantic_type=semantic, control_type=ControlType.TEXT, required=True,
    )]})
    packet = resolve(make_context(form, candidate))
    assert packet.answers == []
    assert packet.missing_inputs


# --- round 3: more reusable defaults answer their canonical wording and listed variants ------

NEW_DEFAULTS = {
    "previously_employed_here": "Yes",
    "previously_interviewed_here": "No",
    "related_to_employee": "No",
    "willing_to_relocate": "Yes",
    "open_to_other_positions": "Yes",
    "willing_to_provide_references": "Yes",
    "desired_salary": "$150,000",
    "english_proficiency": "Native",
    "available_time_zones": "US Central, US Eastern",
    "travel_willingness": "Up to 25%",
    "earliest_start_date": "Two weeks after an offer",
}
YES_NO_OPTIONS = [FieldOption(value="yes", label="Yes"), FieldOption(value="no", label="No")]


def _defaults_candidate(fictional_candidate):
    data = SimpleAnswers.from_identity(fictional_candidate.identity).model_dump()
    data.update(NEW_DEFAULTS)
    answers = SimpleAnswers.model_validate(data).saved_answer_updates(confirmed_at=NOW)
    return fictional_candidate.model_copy(update={"saved_answers": answers})


def _default_field(key, label):
    semantic, _ = _REUSABLE_QUESTIONS[key]
    yes_no = NEW_DEFAULTS[key] in ("Yes", "No")
    return ApplicationField(
        id=key, selector=f"#{key}", label=label,
        semantic_type=semantic or (SemanticType.CUSTOM_BOOLEAN if yes_no else SemanticType.CUSTOM_TEXT),
        control_type=ControlType.RADIO if yes_no else ControlType.TEXT, required=True,
        options=YES_NO_OPTIONS if yes_no else None,
    )


WORDINGS = [(key, wording) for key in NEW_DEFAULTS
            for wording in (_REUSABLE_QUESTIONS[key][1], *_REUSABLE_PHRASES[key])]


@pytest.mark.parametrize("key,wording", WORDINGS)
def test_a_new_default_answers_its_canonical_wording_and_listed_variants(
    key, wording, fictional_candidate, mock_form, make_context, resolve
):
    candidate = _defaults_candidate(fictional_candidate)
    form = mock_form.model_copy(update={"fields": [_default_field(key, wording)]})
    context = make_context(form, candidate)
    packet = resolve(context)
    assert context.problems(packet) == []
    assert packet.missing_inputs == []
    [answer] = packet.answers
    assert answer.provenance.source is AnswerSource.SAVED_ANSWER
    [saved] = [a for a in candidate.saved_answers if a.id in answer.provenance.reference_ids]
    assert saved.scope is AnswerScope.GLOBAL and saved.question == _REUSABLE_QUESTIONS[key][1]
    expected = NEW_DEFAULTS[key]
    if expected in ("Yes", "No"):
        assert (answer.value.value, answer.value.label) == (expected.lower(), expected)
    else:
        assert answer.value.text == expected


def test_relocation_salary_and_start_date_examples_from_the_brief(
    fictional_candidate, mock_form, make_context, resolve
):
    candidate = _defaults_candidate(fictional_candidate)
    fields = [
        ApplicationField(id="relocate", selector="#relocate", label="Are you open to relocation?",
                         semantic_type=SemanticType.RELOCATION, control_type=ControlType.RADIO,
                         required=True, options=YES_NO_OPTIONS),
        ApplicationField(id="salary", selector="#salary", label="What are your salary expectations?",
                         semantic_type=SemanticType.SALARY_EXPECTATION,
                         control_type=ControlType.TEXT, required=True),
        ApplicationField(id="start", selector="#start", label="When can you start?",
                         semantic_type=SemanticType.START_DATE, control_type=ControlType.TEXT,
                         required=True),
    ]
    context = make_context(mock_form.model_copy(update={"fields": fields}), candidate)
    packet = resolve(context)
    assert context.problems(packet) == [] and packet.is_complete
    values = {a.field_id: a.value for a in packet.answers}
    assert values["relocate"].label == "Yes"
    assert values["salary"].text == "$150,000"
    assert values["start"].text == "Two weeks after an offer"


@pytest.mark.parametrize("semantic,control,label,reason", [
    (SemanticType.RELOCATION, ControlType.RADIO, "Would you consider moving to another city for this job?",
     MissingReason.NO_ANSWER),
    (SemanticType.SALARY_EXPECTATION, ControlType.TEXT, "What compensation are you looking for in this role?",
     MissingReason.EXPLICIT_ANSWER_REQUIRED),
    (SemanticType.START_DATE, ControlType.TEXT, "How soon could you join our team?", MissingReason.NO_ANSWER),
    (SemanticType.CUSTOM_TEXT, ControlType.TEXT, "Which languages do you speak fluently?",
     MissingReason.NO_ANSWER),
])
def test_an_unlisted_rewording_is_left_to_the_jev_wording_path(
    semantic, control, label, reason, fictional_candidate, mock_form, make_context, resolve
):
    candidate = _defaults_candidate(fictional_candidate)
    form = mock_form.model_copy(update={"fields": [ApplicationField(
        id="reworded", selector="#reworded", label=label, semantic_type=semantic,
        control_type=control, required=True,
        options=YES_NO_OPTIONS if control is ControlType.RADIO else None,
    )]})
    packet = resolve(make_context(form, candidate))
    assert packet.answers == []
    assert [m.reason for m in packet.missing_inputs] == [reason]


def test_null_new_defaults_answer_nothing(fictional_candidate, mock_form, make_context, resolve):
    data = SimpleAnswers.from_identity(fictional_candidate.identity).model_dump()
    answers = SimpleAnswers.model_validate(data).saved_answer_updates(confirmed_at=NOW)
    assert answers == []  # nothing is invented for keys the person left null
    candidate = fictional_candidate.model_copy(update={"saved_answers": answers})
    form = mock_form.model_copy(update={"fields": [
        _default_field("willing_to_relocate", "Are you willing to relocate?"),
        _default_field("desired_salary", "What is your desired salary?"),
    ]})
    packet = resolve(make_context(form, candidate))
    assert packet.answers == []
    assert {m.field_id for m in packet.missing_inputs} == {"willing_to_relocate", "desired_salary"}
