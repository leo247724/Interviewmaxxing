"""Round 9 (L5): a question asking for the stated work authorization status itself gets the
status in the applicant's words, never its code: the deterministic resolver
(``FactualPacketResolver`` / ``resolve_packet``), its missing-input prompts and
``stored_value`` all read the status through ``saved_value`` (fictional data only)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from interviewmaxxing_core import (
    WORK_AUTHORIZATION_STATUS_QUESTION,
    WORK_AUTHORIZATION_STATUSES,
    AnswerScope,
    AnswerSource,
    ApplicationField,
    ApplicationForm,
    CandidateProfile,
    ChoiceValue,
    ControlType,
    FieldOption,
    MissingReason,
    SavedAnswer,
    SemanticType,
    TextValue,
)
from interviewmaxxing_generation import stored_value
from interviewmaxxing_generation.resolver import saved_value

URL = "http://127.0.0.1:9/jobs/status/apply"
CONFIRMED = "2026-09-02T12:00:00Z"
LABEL = "Work authorization status"
IMPORTER_PHRASES = [
    "Work authorization status", "What is your work authorization status?",
    "What is your current U.S. work authorization?",
]
CITIZEN = "U.S. citizen"
EXPLICIT_AND_CUSTOM = [SemanticType.CUSTOM_TEXT, SemanticType.WORK_AUTHORIZATION]
UNFIT_OPTIONS = ("Citizen", "Permanent resident", "Visa holder", "Other")


def _status(code: str = "us_citizen", answer_id: str = "sa.status") -> SavedAnswer:
    """The status as the simple-answers importer saves it: untyped, GLOBAL, the code."""
    return SavedAnswer(id=answer_id, scope=AnswerScope.GLOBAL, semantic_type=None,
                       question=WORK_AUTHORIZATION_STATUS_QUESTION, value=code,
                       match_phrases=IMPORTER_PHRASES, confirmed_at=CONFIRMED)


def _saved(answer_id: str, question: str, value: object, *,
           semantic: SemanticType | None = None) -> SavedAnswer:
    return SavedAnswer(id=answer_id, scope=AnswerScope.GLOBAL, semantic_type=semantic,
                       question=question, value=value, confirmed_at=CONFIRMED)


def _with(candidate: CandidateProfile, *answers: SavedAnswer) -> CandidateProfile:
    return candidate.model_copy(update={"saved_answers": [*candidate.saved_answers, *answers]})


def _form(*fields: ApplicationField) -> ApplicationForm:
    return ApplicationForm(url=URL, fields=list(fields), is_final_step=True)


def _text(semantic: SemanticType, *, label: str = LABEL, control: ControlType = ControlType.TEXT,
          max_length: int | None = None) -> ApplicationField:
    return ApplicationField(id="status", label=label, selector="#status", semantic_type=semantic,
                            control_type=control, required=True, max_length=max_length)


def _choice(semantic: SemanticType, *labels: str, label: str = LABEL,
            control: ControlType = ControlType.SELECT) -> ApplicationField:
    options = [FieldOption(value="", label="Select...")] if control is ControlType.SELECT else []
    options += [FieldOption(value=f"opt{i}", label=text) for i, text in enumerate(labels)]
    return ApplicationField(id="status", label=label, selector="#status", semantic_type=semantic,
                            control_type=control, required=True, options=options)


def _answered(packet, context):
    assert context.problems(packet) == []
    assert packet.missing_inputs == []
    [answer] = packet.answers
    assert answer.provenance.source is AnswerSource.SAVED_ANSWER
    return answer


def _held(packet, context):
    assert context.problems(packet) == []
    assert packet.answers == []
    [missing] = packet.missing_inputs
    return missing


# --- a text question about the status -----------------------------------------------------------


@pytest.mark.parametrize("semantic", EXPLICIT_AND_CUSTOM)
@pytest.mark.parametrize("control", [ControlType.TEXT, ControlType.TEXTAREA])
def test_a_text_question_about_the_status_gets_its_meaning_never_the_code(
    fictional_candidate, make_context, resolve, semantic, control
):
    context = make_context(_form(_text(semantic, control=control)),
                           _with(fictional_candidate, _status()))
    answer = _answered(resolve(context), context)
    assert answer.value == TextValue(text=CITIZEN)
    assert "us_citizen" not in answer.value.text
    assert answer.provenance.reference_ids == ["sa.status"]
    assert "us_citizen" not in (answer.provenance.note or "")


@pytest.mark.parametrize("label", [WORK_AUTHORIZATION_STATUS_QUESTION, *IMPORTER_PHRASES])
def test_every_wording_the_importer_saves_gets_the_meaning(fictional_candidate, make_context,
                                                             resolve, label):
    context = make_context(_form(_text(SemanticType.CUSTOM_TEXT, label=label)),
                           _with(fictional_candidate, _status()))
    assert _answered(resolve(context), context).value == TextValue(text=CITIZEN)


@pytest.mark.parametrize("code", list(WORK_AUTHORIZATION_STATUSES))
def test_every_status_is_typed_as_its_meaning(fictional_candidate, make_context, resolve, code):
    context = make_context(_form(_text(SemanticType.WORK_AUTHORIZATION)),
                           _with(fictional_candidate, _status(code)))
    text = _answered(resolve(context), context).value.text
    assert text == WORK_AUTHORIZATION_STATUSES[code]
    assert text != code and "_" not in text


@pytest.mark.parametrize("code", ["asylee", "refugee", "daca", "tps", "pending_adjustment",
                                  "dependent_ead"])
def test_the_importers_own_status_answer_is_typed_as_its_meaning(
    fictional_candidate, make_context, resolve, code
):
    from interviewmaxxing_candidate.simple_answers import SimpleAnswers

    data = SimpleAnswers.from_identity(fictional_candidate.identity).model_dump()
    data["work_authorization_status"] = code.replace("_", " ").upper()
    imported = SimpleAnswers.model_validate(data).saved_answer_updates(
        confirmed_at=datetime(2026, 9, 24, tzinfo=UTC))
    context = make_context(_form(_text(SemanticType.CUSTOM_TEXT,
                                       label="What is your work authorization status?")),
                           _with(fictional_candidate, *imported))
    answer = _answered(resolve(context), context)
    assert answer.value == TextValue(text=WORK_AUTHORIZATION_STATUSES[code])
    assert answer.provenance.reference_ids == [a.id for a in imported]


# --- a choice ------------------------------------------------------------------------------------


@pytest.mark.parametrize("semantic", [SemanticType.CUSTOM_SELECT, SemanticType.WORK_AUTHORIZATION])
@pytest.mark.parametrize("control", [ControlType.SELECT, ControlType.RADIO])
def test_a_choice_with_the_meaning_as_an_option_gets_that_option(
    fictional_candidate, make_context, resolve, semantic, control
):
    field = _choice(semantic, CITIZEN, WORK_AUTHORIZATION_STATUSES["us_permanent_resident"],
                    "H-1B visa holder", "Other", control=control)
    context = make_context(_form(field), _with(fictional_candidate, _status()))
    answer = _answered(resolve(context), context)
    assert answer.value == ChoiceValue(value="opt0", label=CITIZEN)
    assert answer.provenance.reference_ids == ["sa.status"]


@pytest.mark.parametrize("code", list(WORK_AUTHORIZATION_STATUSES))
def test_a_select_listing_every_meaning_and_every_code_picks_the_meaning(
    fictional_candidate, make_context, resolve, code
):
    meanings = list(WORK_AUTHORIZATION_STATUSES.values())
    field = _choice(SemanticType.WORK_AUTHORIZATION, *meanings, *WORK_AUTHORIZATION_STATUSES,
                    label="What is your current U.S. work authorization?")
    context = make_context(_form(field), _with(fictional_candidate, _status(code)))
    answer = _answered(resolve(context), context)
    assert answer.value.label == WORK_AUTHORIZATION_STATUSES[code]
    assert answer.value.value == f"opt{meanings.index(WORK_AUTHORIZATION_STATUSES[code])}"


@pytest.mark.parametrize("semantic,reason", [
    (SemanticType.CUSTOM_SELECT, MissingReason.NO_ANSWER),
    (SemanticType.WORK_AUTHORIZATION, MissingReason.EXPLICIT_ANSWER_REQUIRED),
])
@pytest.mark.parametrize("code", ["us_citizen", "us_permanent_resident", "pending_adjustment",
                                  "dependent_ead", "ead_opt"])
def test_a_select_that_cannot_take_the_meaning_quotes_it_never_the_code(
    fictional_candidate, make_context, resolve, semantic, reason, code
):
    context = make_context(_form(_choice(semantic, *UNFIT_OPTIONS)),
                           _with(fictional_candidate, _status(code)))
    missing = _held(resolve(context), context)
    meaning = WORK_AUTHORIZATION_STATUSES[code]
    assert missing.reason is reason and missing.candidates == []
    assert f"Your saved answer {meaning!r} cannot be used here" in missing.prompt
    assert f"{meaning!r} is not one of the options" in missing.prompt
    assert code not in missing.prompt
    assert "Choose one: " + "; ".join(UNFIT_OPTIONS) + "." in missing.prompt


def test_two_options_labelled_with_the_meaning_are_offered_without_the_code(
    fictional_candidate, make_context, resolve
):
    field = _choice(SemanticType.CUSTOM_SELECT, CITIZEN, "Permanent resident", f"{CITIZEN}.")
    context = make_context(_form(field), _with(fictional_candidate, _status()))
    missing = _held(resolve(context), context)
    assert missing.reason is MissingReason.AMBIGUOUS
    assert missing.candidates == [ChoiceValue(value="opt0", label=CITIZEN),
                                  ChoiceValue(value="opt2", label=f"{CITIZEN}.")]
    assert f"Your saved answer {CITIZEN!r} cannot be used here" in missing.prompt
    assert "us_citizen" not in missing.prompt


def test_a_text_field_too_short_for_the_meaning_holds_rather_than_typing_the_code(
    fictional_candidate, make_context, resolve
):
    # The code (21 characters) would fit; the meaning (50) does not, and is never shortened.
    code = "us_permanent_resident"
    meaning = WORK_AUTHORIZATION_STATUSES[code]
    assert len(code) <= 30 < len(meaning)
    context = make_context(_form(_text(SemanticType.WORK_AUTHORIZATION, max_length=30)),
                           _with(fictional_candidate, _status(code)))
    missing = _held(resolve(context), context)
    assert missing.reason is MissingReason.EXPLICIT_ANSWER_REQUIRED
    assert f"Your saved answer {meaning!r} cannot be used here" in missing.prompt
    assert code not in missing.prompt


# --- stored_value --------------------------------------------------------------------------------


@pytest.mark.parametrize("field", [
    _text(SemanticType.CUSTOM_TEXT),
    _text(SemanticType.WORK_AUTHORIZATION),
    _choice(SemanticType.WORK_AUTHORIZATION, *UNFIT_OPTIONS),
    _choice(SemanticType.CUSTOM_SELECT, CITIZEN, "Other", control=ControlType.RADIO),
], ids=["custom-text", "typed-text", "unfit-select", "radio"])
def test_stored_value_is_the_meaning(fictional_candidate, make_context, field):
    stored = stored_value(make_context(_form(field), _with(fictional_candidate, _status())), field)
    assert stored is not None and stored.value == CITIZEN
    assert stored.provenance.source is AnswerSource.SAVED_ANSWER
    assert stored.provenance.reference_ids == ["sa.status"]


@pytest.mark.parametrize("code", list(WORK_AUTHORIZATION_STATUSES))
def test_stored_value_of_every_status_is_its_meaning(fictional_candidate, make_context, code):
    field = _text(SemanticType.CUSTOM_TEXT)
    stored = stored_value(make_context(_form(field), _with(fictional_candidate, _status(code))),
                          field)
    assert stored is not None and stored.value == WORK_AUTHORIZATION_STATUSES[code]


def test_an_answer_in_the_persons_words_agrees_with_the_status(fictional_candidate, make_context,
                                                               resolve):
    own_words = _saved("sa.own_words", LABEL, CITIZEN)
    candidate = _with(fictional_candidate, _status(), own_words)
    field = _text(SemanticType.CUSTOM_TEXT)
    context = make_context(_form(field), candidate)
    answer = _answered(resolve(context), context)
    assert answer.value == TextValue(text=CITIZEN)
    assert sorted(answer.provenance.reference_ids) == ["sa.own_words", "sa.status"]
    stored = stored_value(context, field)
    assert stored is not None and stored.value == CITIZEN
    assert sorted(stored.provenance.reference_ids) == ["sa.own_words", "sa.status"]


def test_a_different_answer_to_the_same_wording_disagrees_with_the_status(
    fictional_candidate, make_context, resolve
):
    other = _saved("sa.other_words", LABEL, "H-1B visa holder")
    field = _text(SemanticType.CUSTOM_TEXT)
    context = make_context(_form(field), _with(fictional_candidate, _status(), other))
    missing = _held(resolve(context), context)
    assert missing.reason is MissingReason.AMBIGUOUS
    assert {c.text for c in missing.candidates} == {CITIZEN, "H-1B visa holder"}
    assert "us_citizen" not in missing.prompt
    assert stored_value(context, field) is None


# --- answers that are not the status -------------------------------------------------------------


@pytest.mark.parametrize("answer,raw", [
    (_saved("sa.code", "Visa type code", "h1b"), "h1b"),
    (_saved("sa.typed", WORK_AUTHORIZATION_STATUS_QUESTION, "us_citizen",
            semantic=SemanticType.WORK_AUTHORIZATION), "us_citizen"),
    (_saved("sa.words", WORK_AUTHORIZATION_STATUS_QUESTION, "Green card holder"),
     "Green card holder"),
    (_saved("sa.flag", WORK_AUTHORIZATION_STATUS_QUESTION, True), True),
    (_saved("sa.count", "Years in the U.S.", 7), 7),
    (_saved("sa.languages", "Languages", ["English", "Spanish"]), ["English", "Spanish"]),
], ids=["another-question", "typed", "own-words", "boolean", "number", "list"])
def test_a_saved_answer_that_states_no_status_keeps_its_raw_value(answer, raw):
    value = saved_value(answer)
    assert value == raw and type(value) is type(raw)


def test_the_fixture_answers_keep_their_raw_values(fictional_candidate):
    for answer in fictional_candidate.saved_answers:
        assert saved_value(answer) == answer.value
    assert saved_value(_status("tn")) == WORK_AUTHORIZATION_STATUSES["tn"]


def test_a_code_saved_for_another_question_is_typed_as_given(fictional_candidate, make_context,
                                                             resolve):
    field = _text(SemanticType.CUSTOM_TEXT, label="Visa type code")
    candidate = _with(fictional_candidate, _status(), _saved("sa.code", "Visa type code", "h1b"))
    context = make_context(_form(field), candidate)
    answer = _answered(resolve(context), context)
    assert answer.value == TextValue(text="h1b")
    assert answer.provenance.reference_ids == ["sa.code"]
    stored = stored_value(context, field)
    assert stored is not None and stored.value == "h1b"


def test_a_non_status_answer_that_does_not_fit_is_quoted_as_saved(fictional_candidate,
                                                                   make_context, resolve):
    field = _choice(SemanticType.CUSTOM_SELECT, *UNFIT_OPTIONS, label="Visa type code")
    candidate = _with(fictional_candidate, _saved("sa.code", "Visa type code", "h1b"))
    context = make_context(_form(field), candidate)
    missing = _held(resolve(context), context)
    assert "Your saved answer 'h1b' cannot be used here" in missing.prompt
    assert "H-1B visa holder" not in missing.prompt
