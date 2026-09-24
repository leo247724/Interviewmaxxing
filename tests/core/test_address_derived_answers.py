"""Address-derived relocation answers (round 6, C).

A ``PROFILE_IDENTITY`` answer may fill a non-identity field in exactly one case: a single
choice (``ChoiceValue``) on a ``RELOCATION`` field, derived from the verified current address
("Do you live in, or will you relocate to, Texas?" is Yes for an applicant who lives there).
Text, multi-choice and checkbox answers of that type, and identity answers on every other
non-identity type, are still refused. Validation follows the inspected field, never what an
answer claims about itself. Core contracts only; fictional data only.
"""

from __future__ import annotations

import pytest

import interviewmaxxing_core
from interviewmaxxing_core import (
    ADDRESS_DERIVED_TYPES,
    EXPLICIT_ANSWER_REQUIRED,
    PROFILE_IDENTITY_TYPES,
    AnswerSource,
    AnswerValue,
    Application,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    BooleanValue,
    CandidateProfile,
    ChoiceValue,
    ControlType,
    FieldOption,
    JobRecord,
    MultiChoiceValue,
    PacketAnswer,
    PacketContext,
    Provenance,
    SemanticType,
    TextValue,
)
from interviewmaxxing_core import forms as forms_mod

URL = "http://127.0.0.1:9/jobs/fictional-relocation/apply"
QUESTION = "Do you currently live in, or are you willing to relocate to, Texas?"
YES_NO = [FieldOption(value="yes", label="Yes"), FieldOption(value="no", label="No")]
STATES = [FieldOption(value="tx", label="Texas"), FieldOption(value="ca", label="California"),
          FieldOption(value="ny", label="New York")]
ADDRESS_NOTE = ("verified identity address: the applicant already lives in the named place; "
                "relocation question answered by Jev")
CHOICE_CONTROLS = (ControlType.SELECT, ControlType.RADIO, ControlType.MULTISELECT,
                   ControlType.CHECKBOX_GROUP)
NOT_IDENTITY = "'relocation' (RELOCATION) is not an identity field"
OTHER_NON_IDENTITY_TYPES = sorted(set(SemanticType) - PROFILE_IDENTITY_TYPES
                                  - ADDRESS_DERIVED_TYPES - EXPLICIT_ANSWER_REQUIRED)
"""Every type an identity answer may not fill, other than the address-derived ones and the
types that need an explicit answer (a ``PacketAnswer`` of those cannot carry identity)."""


def _field(control: ControlType, *, semantic: SemanticType = SemanticType.RELOCATION,
           options: list[FieldOption] | None = None, field_id: str = "relocation",
           label: str = QUESTION) -> ApplicationField:
    return ApplicationField(id=field_id, label=label, selector=f"#{field_id}",
                            semantic_type=semantic, control_type=control, required=True,
                            options=(options or YES_NO) if control in CHOICE_CONTROLS else None)


def _form(*fields: ApplicationField) -> ApplicationForm:
    return ApplicationForm(url=URL, fields=list(fields), is_final_step=True)


def _answer(field: ApplicationField, value: AnswerValue, *,
            source: AnswerSource = AnswerSource.PROFILE_IDENTITY,
            claims: SemanticType | None = None) -> PacketAnswer:
    """``value`` for ``field`` from ``source``; ``claims`` is the type the answer says it has."""
    identity = source is AnswerSource.PROFILE_IDENTITY
    return PacketAnswer(field_id=field.id, semantic_type=claims or field.semantic_type,
                        value=value, provenance=Provenance(
                            source=source, reference_ids=[] if identity else ["ref.fictional"],
                            note=ADDRESS_NOTE if identity else None))


def _packet(form: ApplicationForm, *answers: PacketAnswer, application_id: str = "app-relocation",
            job_id: str = "job-relocation", candidate_id: str = "cand-relocation") -> ApplicationPacket:
    return ApplicationPacket(application_id=application_id, job_id=job_id,
                             candidate_id=candidate_id, form_url=form.url, form_step=form.step,
                             form_fingerprint=form.fingerprint, answers=list(answers))


def _problems(field: ApplicationField, value: AnswerValue, *,
              source: AnswerSource = AnswerSource.PROFILE_IDENTITY,
              claims: SemanticType | None = None) -> list[str]:
    """What ``problems_against`` finds with a one-field form answered by ``value``."""
    form = _form(field)
    return _packet(form, _answer(field, value, source=source, claims=claims)).problems_against(form)


# --- the type set -----------------------------------------------------------------------------


def test_relocation_is_the_only_address_derived_type_and_it_is_exported():
    assert set(ADDRESS_DERIVED_TYPES) == {SemanticType.RELOCATION}
    assert isinstance(ADDRESS_DERIVED_TYPES, frozenset)
    assert forms_mod.ADDRESS_DERIVED_TYPES is ADDRESS_DERIVED_TYPES
    assert "ADDRESS_DERIVED_TYPES" in interviewmaxxing_core.__all__
    # Not an identity type (where any value kind may copy identity), and not a type that
    # needs the user's explicit answer (where identity may never be copied).
    assert not ADDRESS_DERIVED_TYPES & PROFILE_IDENTITY_TYPES
    assert not ADDRESS_DERIVED_TYPES & EXPLICIT_ANSWER_REQUIRED
    assert SemanticType.RELOCATION not in OTHER_NON_IDENTITY_TYPES


# --- a single choice on a relocation field ----------------------------------------------------


@pytest.mark.parametrize(("control", "options", "chosen"), [
    (ControlType.RADIO, YES_NO, YES_NO[0]),
    (ControlType.SELECT, YES_NO, YES_NO[0]),
    (ControlType.SELECT, STATES, STATES[0]),
    (ControlType.RADIO, STATES, STATES[0]),
], ids=["yes-no-radio", "yes-no-select", "state-select", "state-radio"])
def test_a_single_choice_relocation_answer_may_come_from_the_verified_address(
    control, options, chosen
):
    field = _field(control, options=options)
    assert _problems(field, ChoiceValue(value=chosen.value, label=chosen.label)) == []


@pytest.mark.parametrize(("control", "value"), [
    (ControlType.TEXT, TextValue(text="Yes")),
    (ControlType.TEXTAREA, TextValue(text="I already live in Austin, Texas.")),
    (ControlType.TYPEAHEAD, TextValue(text="Austin, TX")),
    (ControlType.CHECKBOX_GROUP, MultiChoiceValue(choices=[STATES[0]])),
    (ControlType.MULTISELECT, MultiChoiceValue(choices=[STATES[0], STATES[1]])),
    (ControlType.CHECKBOX, BooleanValue(checked=True)),
], ids=["text", "textarea", "lookup", "checkbox-group", "multiselect", "checkbox"])
def test_a_relocation_answer_that_is_not_a_single_choice_never_copies_identity(control, value):
    # Each value fits its control, so the source is the only problem.
    field = _field(control, options=STATES)
    assert _problems(field, value) == [NOT_IDENTITY]


def test_a_choice_on_a_text_relocation_control_is_refused_by_the_control():
    # The exemption is about the value kind; it never makes a value fit a control.
    field = _field(ControlType.TEXT)
    assert _problems(field, ChoiceValue(value="yes", label="Yes")) == [
        "TEXT field 'relocation' cannot take a choice value"]


@pytest.mark.parametrize(("options", "value", "problem"), [
    (STATES, ChoiceValue(value="wa", label="Washington"), "'wa' is not an option of 'relocation'"),
    ([YES_NO[0], FieldOption(value="no", label="No", disabled=True)],
     ChoiceValue(value="no", label="No"), "option 'no' of 'relocation' is disabled"),
    (YES_NO, ChoiceValue(value="yes", label="No"),
     "'relocation': label 'No' does not match option 'yes' ('Yes')"),
], ids=["not-an-option", "disabled-option", "label-mismatch"])
def test_an_address_derived_choice_must_still_be_an_enabled_option_of_the_field(
    options, value, problem
):
    assert _problems(_field(ControlType.RADIO, options=options), value) == [problem]


# --- every other type ---------------------------------------------------------------------------


@pytest.mark.parametrize("semantic", OTHER_NON_IDENTITY_TYPES)
def test_an_identity_choice_on_any_other_non_identity_field_is_still_refused(semantic):
    field = _field(ControlType.RADIO, semantic=semantic, field_id="question",
                   label="Fictional question?")
    assert _problems(field, ChoiceValue(value="yes", label="Yes")) == [
        f"'question' ({semantic.value}) is not an identity field"]


@pytest.mark.parametrize("semantic", sorted(EXPLICIT_ANSWER_REQUIRED))
def test_an_answer_claiming_relocation_cannot_carry_identity_onto_an_explicit_field(semantic):
    field = _field(ControlType.RADIO, semantic=semantic, field_id="question",
                   label="Fictional eligibility question?")
    assert _problems(field, ChoiceValue(value="yes", label="Yes"),
                     claims=SemanticType.RELOCATION) == [
        f"answer for 'question' claims RELOCATION, field is {semantic.value}",
        f"'question' is {semantic.value}; it needs a saved answer or user input, "
        "not PROFILE_IDENTITY",
        f"'question' ({semantic.value}) is not an identity field",
    ]


def test_the_exemption_follows_the_inspected_field_not_the_answers_claim():
    yes = ChoiceValue(value="yes", label="Yes")
    # An answer calling itself RELOCATION does not carry identity onto a custom choice...
    custom = _field(ControlType.RADIO, semantic=SemanticType.CUSTOM_SELECT)
    assert _problems(custom, yes, claims=SemanticType.RELOCATION) == [
        "answer for 'relocation' claims RELOCATION, field is CUSTOM_SELECT",
        "'relocation' (CUSTOM_SELECT) is not an identity field",
    ]
    # ...while on a real relocation field only the mislabelled type is wrong.
    relocation = _field(ControlType.RADIO)
    assert _problems(relocation, yes, claims=SemanticType.CUSTOM_SELECT) == [
        "answer for 'relocation' claims CUSTOM_SELECT, field is RELOCATION"]


@pytest.mark.parametrize(("source", "control", "value"), [
    (AnswerSource.SAVED_ANSWER, ControlType.TEXT, TextValue(text="Yes")),
    (AnswerSource.USER_INPUT, ControlType.CHECKBOX_GROUP, MultiChoiceValue(choices=[STATES[0]])),
    (AnswerSource.SAVED_ANSWER, ControlType.RADIO, ChoiceValue(value="tx", label="Texas")),
    (AnswerSource.CANDIDATE_FACT, ControlType.TEXT, TextValue(text="Willing to relocate")),
], ids=["saved-text", "user-multi-choice", "saved-choice", "fact-text"])
def test_relocation_answers_from_other_sources_are_unaffected(source, control, value):
    assert _problems(_field(control, options=STATES), value, source=source) == []


# --- the whole canonical check --------------------------------------------------------------------


def test_the_canonical_context_accepts_only_the_single_choice_relocation_identity(
    fictional_candidate: CandidateProfile, mock_job: JobRecord
):
    city = ApplicationField(id="city", label="City", selector="#city",
                            semantic_type=SemanticType.CITY, control_type=ControlType.TEXT,
                            required=True)
    application = Application(id="app-relocation", request_id="request-relocation",
                              job_id=mock_job.id, candidate_id=fictional_candidate.id,
                              state=ApplicationState.INSPECTING, version=1,
                              created_at="2026-09-24T00:00:00Z", updated_at="2026-09-24T00:00:00Z")
    ids = {"application_id": application.id, "job_id": mock_job.id,
           "candidate_id": fictional_candidate.id}
    city_answer = TextValue(text=fictional_candidate.identity.address.city or "")
    for relocation, value, problems in (
        (_field(ControlType.RADIO), ChoiceValue(value="yes", label="Yes"), []),
        (_field(ControlType.TEXT), TextValue(text="Yes"), [NOT_IDENTITY]),
    ):
        form = _form(city, relocation)
        context = PacketContext(application=application, job=mock_job, form=form,
                                candidate=fictional_candidate)
        packet = _packet(form, _answer(city, city_answer), _answer(relocation, value), **ids)
        assert context.problems(packet) == problems
