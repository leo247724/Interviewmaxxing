"""Unit tests for question-wording binding and value translation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from interviewmaxxing_core import (
    AnswerScope,
    ApplicationField,
    BooleanValue,
    ChoiceValue,
    ControlType,
    FieldOption,
    MultiChoiceValue,
    SavedAnswer,
    SemanticType,
    TextValue,
)
from interviewmaxxing_generation import (
    QuestionText,
    is_neutral_hint,
    question_key,
    saved_answer_matches,
)
from interviewmaxxing_generation.questions import (
    factual_template,
    parse_years_question,
    wording_key,
    years_fact_area,
)
from interviewmaxxing_generation.values import Mapped, Unmapped, option_range, translate

AT = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _field(label: str, control: ControlType = ControlType.TEXT, *, options=None, **extra):
    opts = [FieldOption(value=v, label=lbl) for v, lbl in options] if options else None
    return ApplicationField(id="f", label=label, control_type=control, selector="#f",
                            options=opts, **extra)


def _saved(question: str, *phrases: str) -> SavedAnswer:
    return SavedAnswer(id="sa", scope=AnswerScope.GLOBAL, question=question,
                       match_phrases=list(phrases), value="Yes", confirmed_at=AT)


# --- question wording ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("  Email *", "email"),
        ("Phone (optional)", "phone"),
        ("Are you authorized?  ", "are you authorized"),
        ("Why Mock Co?!", "why mock co"),
    ],
)
def test_question_key_drops_only_trailing_markers(text, key):
    assert question_key(text) == key


def test_wording_key_ignores_punctuation_but_keeps_plus_and_hash():
    assert wording_key("I agree. I certify: this is true.") == "i agree i certify this is true"
    assert wording_key("C++ experience") != wording_key("C experience")
    assert wording_key("C# experience") == "c# experience"


@pytest.mark.parametrize("hint", [None, "", "Select...", "e.g. 5", "MM/YYYY", "Your answer"])
def test_neutral_hints(hint):
    assert is_neutral_hint(hint)


@pytest.mark.parametrize("hint", ["https://linkedin.com/in/you", "Only B2B roles", "In USD"])
def test_meaningful_placeholders_are_not_neutral(hint):
    assert not is_neutral_hint(hint)


def test_saved_answer_binds_the_complete_question():
    plain = QuestionText.of(_field("Are you legally authorized to work in the United States?"))
    assert saved_answer_matches(
        _saved("are you legally authorized to work in the united states"), plain)
    # match phrases are whole alternative phrasings, never substrings
    assert not saved_answer_matches(_saved("Other", "authorized to work in the us"), plain)
    assert saved_answer_matches(_saved("Other", "Authorized to work in the US?"),
                                QuestionText.of(_field("Authorized to work in the US")))
    assert not saved_answer_matches(
        _saved("Are you legally authorized to work in the United States?"),
        QuestionText.of(_field("Are you legally authorized to work in the United Kingdom?")))


def test_help_text_and_placeholder_are_part_of_the_question():
    field = _field("I agree", ControlType.CHECKBOX,
                   help_text="I certify that I have never been dismissed.")
    question = QuestionText.of(field)
    assert not question.label_is_complete
    assert not saved_answer_matches(_saved("I agree"), question)
    assert saved_answer_matches(
        _saved("I agree - I certify that I have never been dismissed"), question)

    hinted = QuestionText.of(_field("Expected salary", placeholder="In EUR"))
    assert not saved_answer_matches(_saved("Expected salary"), hinted)
    assert saved_answer_matches(_saved("Expected salary (in EUR)"), hinted)
    neutral = QuestionText.of(_field("Notice period", placeholder="e.g. 2"))
    assert saved_answer_matches(_saved("Notice period"), neutral)


# --- factual questions ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "area"),
    [
        ("Years of experience", None),
        ("Years of professional experience", None),
        ("How many years of work experience do you have?", None),
        ("Total years of experience", None),
        ("How many years of paid search experience do you have?", "paid search"),
        ("Years of experience with SQL", "sql"),
        ("Years of C++ experience", "c++"),
        ("Years of relevant experience", "relevant"),
    ],
)
def test_parse_years_question(label, area):
    parsed = parse_years_question(QuestionText.of(_field(label)))
    assert parsed is not None and parsed.area == area


@pytest.mark.parametrize(
    "label",
    ["Describe your experience", "Years at current company", "Experience level",
     "How many years of SQL experience with Postgres do you have?"],
)
def test_non_years_questions_are_not_parsed(label):
    assert parse_years_question(QuestionText.of(_field(label))) is None


def test_years_question_with_help_text_is_not_parsed():
    field = _field("Years of experience", help_text="Only count leadership roles.")
    assert parse_years_question(QuestionText.of(field)) is None


def test_years_fact_areas():
    assert years_fact_area("years_experience") == ""
    assert years_fact_area("years_experience.paid_media") == "paid media"
    assert years_fact_area("years_experience.c++") == "c++"
    assert years_fact_area("team_size_managed") is None


@pytest.mark.parametrize(
    ("label", "keys"),
    [
        ("What is your current job title?", ("current_title",)),
        ("Current employer", ("current_company",)),
        ("What is your current role?", ("current_title", "current_company")),
    ],
)
def test_factual_templates_match_whole_questions(label, keys):
    template = factual_template(QuestionText.of(_field(label)))
    assert template is not None and template.fact_keys == keys


@pytest.mark.parametrize(
    "label",
    ["Why do you want this role?", "What do you like about your current role?",
     "Describe your current role and responsibilities", "Tell us about yourself"],
)
def test_opinion_and_motivation_questions_have_no_template(label):
    assert factual_template(QuestionText.of(_field(label))) is None


# --- value translation ---------------------------------------------------------------

YES_NO = [("1", "Yes"), ("0", "No")]


def test_booleans_map_to_yes_no_labels_and_zero_is_text():
    radio = _field("Q", ControlType.RADIO, options=YES_NO)
    assert translate(radio, False) == Mapped(ChoiceValue(value="0", label="No"))
    assert translate(radio, "yes") == Mapped(ChoiceValue(value="1", label="Yes"))
    assert translate(_field("Q"), 0) == Mapped(TextValue(text="0"))
    assert translate(_field("Q"), 7.0) == Mapped(TextValue(text="7"))
    assert translate(_field("Q", ControlType.CHECKBOX), False) == Mapped(
        BooleanValue(checked=False))
    assert isinstance(translate(_field("Q", ControlType.CHECKBOX, required=True), False),
                      Unmapped)


def test_option_values_are_never_matched_against_user_text():
    radio = _field("Q", ControlType.RADIO, options=YES_NO)
    assert isinstance(translate(radio, "0"), Unmapped)
    assert isinstance(translate(radio, 0), Unmapped)


def test_disabled_and_placeholder_options_are_never_chosen():
    field = ApplicationField(
        id="f", label="Q", control_type=ControlType.SELECT, selector="#f",
        options=[FieldOption(value="", label="No"),
                 FieldOption(value="n", label="No", disabled=True),
                 FieldOption(value="y", label="Yes")])
    assert isinstance(translate(field, "No"), Unmapped)


def test_multi_choice_requires_every_value_to_map():
    field = _field("Q", ControlType.CHECKBOX_GROUP,
                   options=[("s", "Paid search"), ("o", "Paid social")])
    assert translate(field, ["paid search", "Paid search"]) == Mapped(
        MultiChoiceValue(choices=[FieldOption(value="s", label="Paid search")]))
    assert isinstance(translate(field, ["Paid search", "Affiliate"]), Unmapped)
    assert isinstance(translate(field, True), Unmapped)


def test_text_limits_and_number_inputs():
    assert isinstance(translate(_field("Q", max_length=3), "abcd"), Unmapped)
    assert isinstance(translate(_field("Q", input_type="number"), "Yes"), Unmapped)
    assert isinstance(translate(_field("Q"), ["a"]), Unmapped)


def test_country_and_state_equivalents_only_for_those_types():
    country = _field("Country", ControlType.SELECT, semantic_type=SemanticType.COUNTRY,
                     options=[("usa", "United States of America"), ("ca", "Canada")])
    assert translate(country, "US") == Mapped(
        ChoiceValue(value="usa", label="United States of America"))
    other = _field("Q", ControlType.SELECT, semantic_type=SemanticType.CUSTOM_SELECT,
                   options=[("usa", "United States of America")])
    assert isinstance(translate(other, "US"), Unmapped)


@pytest.mark.parametrize(
    ("label", "inside", "outside"),
    [
        ("0-2 years", [0, 2], [3]),
        ("3 to 5", [3, 5], [2.5, 6]),
        ("10+ years", [10, 25], [9]),
        ("Less than 1 year", [0, 0.5], [1]),
        ("More than 10 years", [11], [10]),
        ("5", [5], [4]),
    ],
)
def test_option_ranges(label, inside, outside):
    bounds = option_range(label)
    assert bounds is not None
    field = _field("Years", ControlType.SELECT, options=[("x", label)])
    for number in inside:
        assert isinstance(translate(field, number, numeric_ranges=True), Mapped), number
    for number in outside:
        assert isinstance(translate(field, number, numeric_ranges=True), Unmapped), number


def test_overlapping_ranges_are_ambiguous_not_guessed():
    field = _field("Years", ControlType.SELECT, options=[("a", "0-2"), ("b", "2-5")])
    result = translate(field, 2, numeric_ranges=True)
    assert isinstance(result, Unmapped) and len(result.candidates) == 2
    assert translate(field, 1, numeric_ranges=True) == Mapped(ChoiceValue(value="a", label="0-2"))
