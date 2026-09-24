"""Contract validation and serialization."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from interviewmaxxing_core import (
    EXPLICIT_ANSWER_REQUIRED,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    BooleanValue,
    CandidateProfile,
    ChoiceValue,
    ControlType,
    EvidenceKind,
    EvidenceRef,
    FieldOption,
    IdentityEvidenceKind,
    JobIdentityObservation,
    MissingInput,
    MissingReason,
    MultiChoiceValue,
    PacketAnswer,
    PageInspection,
    PageKind,
    Provenance,
    Receipt,
    ReconciliationMethod,
    SemanticType,
    SubmissionObservation,
    SubmissionOutcome,
    SubmissionReconciliation,
    TextValue,
    UserInput,
    answer_problems,
)
from interviewmaxxing_core import applications as applications_mod
from interviewmaxxing_core import candidate as candidate_mod
from interviewmaxxing_core import execution as execution_mod
from interviewmaxxing_core import forms as forms_mod
from interviewmaxxing_core import jobs as jobs_mod
from interviewmaxxing_core import packets as packets_mod
from interviewmaxxing_core._base import Contract

FIXTURE_MODELS = {
    "candidate_profile.json": CandidateProfile,
    "application_form.json": ApplicationForm,
    "application_packet.json": ApplicationPacket,
    "job_identity_observation.json": JobIdentityObservation,
    "submission_observation_accepted.json": SubmissionObservation,
    "submission_observation_unknown.json": SubmissionObservation,
    "page_inspection_sign_in.json": PageInspection,
    "multistep_form_step0.json": ApplicationForm,
    "multistep_form_step1.json": ApplicationForm,
    "user_input_gender.json": UserInput,
}


@pytest.mark.parametrize("name", sorted(FIXTURE_MODELS))
def test_fixture_round_trips_through_json(name, core_fixture):
    model_cls = FIXTURE_MODELS[name]
    model = model_cls.model_validate(core_fixture(name))
    again = model_cls.model_validate_json(model.model_dump_json())
    assert again == model
    assert json.loads(again.model_dump_json()) == json.loads(model.model_dump_json())


def _public_contracts() -> list[type[BaseModel]]:
    found: dict[str, type[BaseModel]] = {}
    for mod in (applications_mod, candidate_mod, execution_mod, forms_mod, jobs_mod, packets_mod):
        for name, obj in vars(mod).items():
            if isinstance(obj, type) and issubclass(obj, Contract) and obj is not Contract:
                found[name] = obj
    return list(found.values())


@pytest.mark.parametrize("model_cls", _public_contracts(), ids=lambda m: m.__name__)
def test_every_contract_publishes_a_json_schema(model_cls):
    schema = model_cls.model_json_schema()
    assert schema["type"] == "object"


def test_contracts_are_immutable_and_reject_unknown_fields(mock_form):
    with pytest.raises(ValidationError):
        mock_form.step = 3  # type: ignore[misc]
    data = mock_form.model_dump()
    data["unexpected"] = 1
    with pytest.raises(ValidationError, match="unexpected"):
        ApplicationForm.model_validate(data)


def test_naive_datetimes_rejected_and_aware_normalized_to_utc(mock_identity):
    data = mock_identity.model_dump()
    data["observed_at"] = datetime(2026, 9, 22, 20, 0)
    with pytest.raises(ValidationError):
        JobIdentityObservation.model_validate(data)
    data["observed_at"] = datetime(2026, 9, 22, 13, 0, tzinfo=timezone(timedelta(hours=-7)))
    parsed = JobIdentityObservation.model_validate(data)
    assert parsed.observed_at.utcoffset() == timedelta(0)
    assert parsed.observed_at.hour == 20


# --- forms ------------------------------------------------------------------------


def test_mock_form_covers_every_operable_control(mock_form):
    controls = {f.control_type for f in mock_form.fields}
    # TYPEAHEAD (site-suggestion lookups) is exercised by the browser suites' lookup
    # scenarios; the standard mock form has no such control.
    assert controls == set(ControlType) - {ControlType.UNSUPPORTED, ControlType.TYPEAHEAD}


def test_option_value_is_distinct_from_label(mock_form):
    work_auth = mock_form.field("work_auth")
    assert work_auth.option_values() == ["1", "0"]
    assert [o.label for o in work_auth.options or []] == ["Yes", "No"]
    # Selecting by label instead of machine value is detected.
    assert answer_problems(work_auth, ChoiceValue(value="Yes", label="Yes"))
    assert answer_problems(work_auth, ChoiceValue(value="1", label="Yes")) == []


@pytest.mark.parametrize(
    "control", [ControlType.SELECT, ControlType.RADIO, ControlType.MULTISELECT,
                ControlType.CHECKBOX_GROUP]
)
def test_choice_controls_need_options(control):
    with pytest.raises(ValidationError, match="needs options"):
        ApplicationField(id="q", label="Q", control_type=control, selector="#q")


def test_duplicate_option_values_and_misplaced_options_rejected():
    opts = [FieldOption(value="a", label="A"), FieldOption(value="a", label="Also A")]
    with pytest.raises(ValidationError, match="duplicate option values"):
        ApplicationField(id="q", label="Q", control_type=ControlType.SELECT, selector="#q",
                         options=opts)
    with pytest.raises(ValidationError, match="must not have options"):
        ApplicationField(id="q", label="Q", control_type=ControlType.TEXT, selector="#q",
                         options=opts[:1])
    with pytest.raises(ValidationError, match="accept"):
        ApplicationField(id="q", label="Q", control_type=ControlType.TEXT, selector="#q",
                         accept=[".pdf"])


def test_duplicate_field_ids_rejected(mock_form):
    data = mock_form.model_dump()
    data["fields"].append(data["fields"][0])
    with pytest.raises(ValidationError, match="duplicate field ids"):
        ApplicationForm.model_validate(data)


def test_answer_value_kinds_must_match_controls(mock_form):
    assert answer_problems(mock_form.field("email"), BooleanValue(checked=True))
    assert answer_problems(mock_form.field("privacy"), BooleanValue(checked=False))  # required
    assert answer_problems(mock_form.field("privacy"), BooleanValue(checked=True)) == []
    channels = mock_form.field("channels")
    ok = MultiChoiceValue(choices=[FieldOption(value="search", label="Paid search")])
    bad = MultiChoiceValue(choices=[FieldOption(value="tv", label="TV")])
    assert answer_problems(channels, ok) == []
    assert answer_problems(channels, bad)
    long = TextValue(text="x" * 2001)
    assert answer_problems(mock_form.field("why_us"), long)
    assert answer_problems(mock_form.field("why_us"), TextValue(text="   "))


# --- packets ----------------------------------------------------------------------


def test_fixture_packet_is_consistent_with_form_and_reports_missing(mock_packet, mock_form):
    assert mock_packet.problems_against(mock_form) == []
    assert not mock_packet.is_complete
    assert mock_packet.unresolved_fields == ["gender", "why_us"]
    assert mock_packet.answer_for("work_auth").value.value == "1"  # type: ignore[union-attr]


def test_required_field_neither_answered_nor_missing_is_a_problem(mock_packet, mock_form):
    incomplete = mock_packet.model_copy(update={"missing_inputs": mock_packet.missing_inputs[:1]})
    assert any("why_us" in p for p in incomplete.problems_against(mock_form))


@pytest.mark.parametrize("semantic_type", sorted(EXPLICIT_ANSWER_REQUIRED))
@pytest.mark.parametrize(
    "source", ["CANDIDATE_FACT", "GENERATED_FROM_FACTS", "PROFILE_IDENTITY", "RESUME"]
)
def test_sensitive_answers_cannot_be_inferred(semantic_type, source):
    with pytest.raises(ValidationError, match="saved answer or user input"):
        PacketAnswer(
            field_id="f",
            semantic_type=semantic_type,
            value=TextValue(text="Yes"),
            provenance=Provenance(source=source, reference_ids=["fact.x"]),
        )


@pytest.mark.parametrize("source", ["SAVED_ANSWER", "USER_INPUT"])
def test_sensitive_answers_allowed_from_explicit_sources(source):
    answer = PacketAnswer(
        field_id="f",
        semantic_type=SemanticType.EEO_GENDER,
        value=ChoiceValue(value="decline", label="I decline to self-identify"),
        provenance=Provenance(source=source, reference_ids=["x"]),
    )
    assert answer.provenance.source == source


def test_provenance_must_reference_its_sources():
    with pytest.raises(ValidationError, match="reference"):
        Provenance(source="CANDIDATE_FACT")
    Provenance(source="PROFILE_IDENTITY")


def test_packet_answers_each_field_once_and_not_also_missing(mock_packet):
    data = mock_packet.model_dump()
    data["answers"].append(data["answers"][0])
    with pytest.raises(ValidationError, match="at most once"):
        ApplicationPacket.model_validate(data)
    data = mock_packet.model_dump()
    data["missing_inputs"][0]["field_id"] = "first_name"
    with pytest.raises(ValidationError, match="both answered and missing"):
        ApplicationPacket.model_validate(data)


def test_missing_input_needs_field_unless_user_action():
    with pytest.raises(ValidationError):
        MissingInput(field_id=None, label="x", reason=MissingReason.NO_ANSWER, prompt="?")
    item = MissingInput(field_id=None, label="Sign in", reason=MissingReason.USER_ACTION,
                        prompt="Sign in to the employer's site in the browser window.")
    assert item.required


# --- candidate --------------------------------------------------------------------


def test_fictional_candidate_resume_is_verifiable(fictional_candidate, mock_job):
    assert Path(fictional_candidate.resume.path).is_absolute()
    assert fictional_candidate.resume.verify()
    assert fictional_candidate.identity.full_name == "Avery Example"
    assert [a.id for a in fictional_candidate.saved_answers_for(SemanticType.SPONSORSHIP,
                                                                     job=mock_job)] == [
        "sa.sponsorship"
    ]


def test_candidate_references_must_resolve(fictional_candidate):
    data = fictional_candidate.model_dump()
    data["experience"][0]["fact_ids"].append("fact.invented")
    with pytest.raises(ValidationError, match="unknown facts"):
        CandidateProfile.model_validate(data)


def test_candidate_identity_requires_verification(fictional_candidate):
    data = fictional_candidate.model_dump()
    del data["identity"]["verified_at"]
    with pytest.raises(ValidationError, match="verified_at"):
        CandidateProfile.model_validate(data)


# --- jobs, evidence, submission ---------------------------------------------------


def test_identity_key_is_normalized_and_redirects_are_not_evidence(mock_identity):
    assert mock_identity.identity_key == "ats:mock:mock-co:4012"
    shouted = mock_identity.model_copy(update={"ats_type": "MOCK", "ats_tenant": " Mock-Co "})
    assert shouted.identity_key == mock_identity.identity_key
    assert "REDIRECT" not in IdentityEvidenceKind.__members__


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.png", "a/../../b", "C:\\x.png"])
def test_evidence_paths_stay_inside_artifacts_dir(path):
    with pytest.raises(ValidationError):
        EvidenceRef(kind=EvidenceKind.SCREENSHOT, path=path)


def test_evidence_needs_some_content():
    with pytest.raises(ValidationError):
        EvidenceRef(kind=EvidenceKind.OTHER)
    ok = EvidenceRef(kind=EvidenceKind.SCREENSHOT, path="app_1/confirm.png")
    assert ok.resolve(Path("/tmp/artifacts")) == Path("/tmp/artifacts/app_1/confirm.png")


def test_acceptance_requires_observed_signals():
    with pytest.raises(ValidationError, match="acceptance signal"):
        SubmissionObservation(outcome=SubmissionOutcome.ACCEPTED)
    with pytest.raises(ValidationError, match="proving"):
        SubmissionObservation(outcome=SubmissionOutcome.NOT_SUBMITTED, next_state="FILLING")
    with pytest.raises(ValidationError, match="next_state"):
        SubmissionObservation(outcome=SubmissionOutcome.NOT_SUBMITTED,
                              validation_errors=["Email is required"])
    with pytest.raises(ValidationError, match="only meaningful"):
        SubmissionObservation(outcome=SubmissionOutcome.UNKNOWN, next_state="FILLING")
    SubmissionObservation(outcome=SubmissionOutcome.UNKNOWN)


def test_reconciliation_must_be_definite():
    with pytest.raises(ValidationError):
        SubmissionReconciliation(outcome=SubmissionOutcome.UNKNOWN,
                                 method=ReconciliationMethod.USER_CONFIRMED, detail="unsure")
    with pytest.raises(ValidationError):
        SubmissionReconciliation(outcome=SubmissionOutcome.ACCEPTED,
                                 method=ReconciliationMethod.USER_CONFIRMED, detail="  ")


def test_page_inspection_form_presence_matches_kind(mock_form):
    PageInspection(kind=PageKind.APPLICATION_FORM, observed_url=mock_form.url, form=mock_form)
    with pytest.raises(ValidationError):
        PageInspection(kind=PageKind.APPLICATION_FORM, observed_url=mock_form.url)
    with pytest.raises(ValidationError):
        PageInspection(kind=PageKind.CAPTCHA, observed_url=mock_form.url, form=mock_form)


def test_receipt_schema_has_required_proof_fields():
    required = set(Receipt.model_json_schema()["required"])
    assert {"application_url", "submitted_at", "confirmed_at", "attempt_id"} <= required


def test_typeahead_and_needs_choice_contract_seed() -> None:
    from interviewmaxxing_core import (
        ApplicationField,
        ControlType,
        FieldFillResult,
        FieldFillStatus,
        FillResult,
        TextValue,
    )
    from interviewmaxxing_core.packets import answer_problems

    field = ApplicationField(id="loc", label="Location (City)", control_type=ControlType.TYPEAHEAD,
                             selector="#loc", required=True)
    assert field.options is None and answer_problems(field, TextValue(text="Austin, Texas")) == []
    assert field.fingerprint == field.model_copy(update={"expects_international_phone": True}).fingerprint
    with pytest.raises(ValueError):
        ApplicationField(id="loc", label="Location", control_type=ControlType.TYPEAHEAD, selector="#loc",
                         options=[{"value": "a", "label": "a"}])  # type: ignore[list-item]
    result = FillResult(form_step=0, fields=[
        FieldFillResult(field_id="loc", status=FieldFillStatus.NEEDS_CHOICE,
                        suggestions=["Austin, Texas, United States", "Austin, Minnesota, United States"]),
        FieldFillResult(field_id="email", status=FieldFillStatus.FILLED),
        FieldFillResult(field_id="phone", status=FieldFillStatus.FAILED, detail="x"),
    ])
    assert not result.ok
    assert result.failed_field_ids() == ["phone"]
    assert [f.field_id for f in result.needs_choice()] == ["loc"]


def test_a_lookup_question_lists_suggestions_and_takes_the_picked_label_as_text() -> None:
    from interviewmaxxing_core import (
        ApplicationField,
        ApplicationForm,
        ChoiceValue,
        ControlType,
        FieldOption,
        MissingInput,
        MissingReason,
        TextValue,
        UserInput,
    )

    field = ApplicationField(id="loc", label="Location (City)", control_type=ControlType.TYPEAHEAD,
                             selector="#loc", required=True)
    form = ApplicationForm(url="http://127.0.0.1:9/apply", fields=[field])
    suggestions = ["Austin, Texas, United States", "Austin, Minnesota, United States"]
    item = MissingInput.for_field(form, field, reason=MissingReason.NO_ANSWER,
                                  prompt="Choose the suggestion that is right for you.").model_copy(
        update={"options": [FieldOption(value=s, label=s) for s in suggestions]})
    picked = UserInput.answering(item, TextValue(text=suggestions[0]))
    assert picked.value == TextValue(text=suggestions[0]) and picked.matches(form)
    # Another value to look up is still text; a choice value is not.
    assert UserInput.answering(item, TextValue(text="Austin, TX")).matches(form)
    with pytest.raises(ValueError, match="cannot take a choice value"):
        UserInput.answering(item, ChoiceValue(value=suggestions[0], label=suggestions[0]))
    with pytest.raises(ValueError, match="empty answer"):
        UserInput.answering(item, TextValue(text=" "))
    # Real choice controls still check their own options.
    select = item.model_copy(update={"control_type": ControlType.SELECT})
    with pytest.raises(ValueError):
        UserInput.answering(select, ChoiceValue(value="elsewhere", label="Elsewhere"))


def test_suggestion_chooser_and_selective_fill_are_optional_runtime_protocols() -> None:
    from interviewmaxxing_core.interfaces import (
        ApplicationBrowser,
        PacketResolver,
        SelectiveFill,
        SuggestionChooser,
    )

    class Resolver:
        async def resolve(self, context):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    class Chooser(Resolver):
        async def choose_suggestion(self, context, field, typed_value, suggestions):  # type: ignore[no-untyped-def]
            return None

    assert isinstance(Resolver(), PacketResolver) and not isinstance(Resolver(), SuggestionChooser)
    assert isinstance(Chooser(), SuggestionChooser)

    class Refill:
        async def fill_fields(self, form, packet, field_ids):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    assert isinstance(Refill(), SelectiveFill) and not isinstance(Refill(), ApplicationBrowser)
