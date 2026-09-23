"""C3R regressions: symbols, currency and units are part of what a question asks."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from interviewmaxxing_core import (
    AnswerScope,
    ApplicationField,
    ApplicationForm,
    ChoiceValue,
    ControlType,
    FieldOption,
    MissingReason,
    SavedAnswer,
    SemanticType,
    render_question,
)
from interviewmaxxing_generation import (
    QuestionText,
    display_question,
    is_neutral_hint,
    saved_answer_matches,
    wording_key,
)

AT = datetime(2026, 9, 1, 12, tzinfo=UTC)
URL = "http://127.0.0.1:0/jobs/mock-4012/apply"
YES_NO = [FieldOption(value="y", label="Yes"), FieldOption(value="n", label="No")]


def _field(field_id: str, label: str, semantic_type: SemanticType, control: ControlType, **extra):
    options = YES_NO if control is ControlType.RADIO else None
    return ApplicationField(id=field_id, label=label, semantic_type=semantic_type,
                            control_type=control, selector=f"#{field_id}", required=True,
                            options=options, **extra)


def _saved(answer_id: str, question: str, value, semantic_type: SemanticType | None = None):
    return SavedAnswer(id=answer_id, scope=AnswerScope.GLOBAL, semantic_type=semantic_type,
                       question=question, value=value, confirmed_at=AT)


# --- direct -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Have you managed a budget > $100,000?", "Have you managed a budget < $100,000?"),
        ("Expected salary $", "Expected salary €"),
        ("Expected salary $", "Expected salary"),
        ("Minimum rate: 50%", "Minimum rate: 50"),
        ("Budget >= $1M", "Budget = $1M"),
        ("Experience in 1.5 years", "Experience in 15 years"),
        ("Salary $100,000", "Salary $100 000"),
        ("Hours/week", "Hours week"),
        ("R&D experience", "R D experience"),
    ],
)
def test_meaning_bearing_symbols_are_kept(first, second):
    assert wording_key(first) != wording_key(second)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Why Mock Co?", "why  mock co"),
        ("Are you authorized (U.S. only)?", "Are you authorized U.S only"),
        ("I agree - I certify it is true.", "I agree. I certify it is true"),
        ("I agree \u2014 I certify", "I agree I certify"),
        ("Driver\u2019s license", "Driver's license"),
        ("Email *", "email"),
    ],
)
def test_harmless_punctuation_spacing_and_case_are_ignored(first, second):
    assert wording_key(first) == wording_key(second)


@pytest.mark.parametrize(
    "hint",
    ["€", "$", "USD", "%", "< 100", "per hour", "in weeks", "1-5", "0-10", "100000",
     "e.g. 5 years", "e.g. $5,000", "Précisez"],
)
def test_units_currency_and_scales_are_not_neutral_hints(hint):
    assert not is_neutral_hint(hint)


@pytest.mark.parametrize("hint", [None, "", "Select...", "Your answer", "MM/YYYY", "e.g. 5",
                                  "DD-MM-YYYY", "Type here\u2026"])
def test_genuinely_neutral_hints(hint):
    assert is_neutral_hint(hint)


def test_currency_placeholder_is_part_of_the_question():
    euro = QuestionText.of(_field("s", "Expected salary", SemanticType.SALARY_EXPECTATION,
                                  ControlType.TEXT, placeholder="€"))
    assert not euro.label_is_complete
    assert not saved_answer_matches(_saved("sa", "Expected salary $", "90000"), euro)
    assert not saved_answer_matches(_saved("sa", "Expected salary", "90000"), euro)
    assert saved_answer_matches(_saved("sa", "Expected salary (€)", "90000"), euro)


def test_display_question_is_the_core_rendering():
    field = _field("s", "Expected salary", SemanticType.SALARY_EXPECTATION, ControlType.TEXT,
                   help_text="Annual  base pay.", placeholder="€")
    assert display_question(field) == field.question_text == render_question(
        "Expected salary", "Annual base pay.", "€")
    assert display_question(field) == "Expected salary\nAnnual base pay.\n€"
    plain = _field("n", "Notice period", SemanticType.CUSTOM_TEXT, ControlType.TEXT)
    assert display_question(plain) == "Notice period"


# --- resolver level -----------------------------------------------------------------


def _resolve(make_context, resolve, candidate, fields):
    form = ApplicationForm(url=URL, fields=fields)
    context = make_context(form, candidate)
    packet = resolve(context)
    assert context.problems(packet) == []
    return packet


def test_saved_answer_for_greater_than_does_not_answer_less_than(
    fictional_candidate, make_context, resolve
):
    candidate = fictional_candidate.model_copy(update={"saved_answers": [
        _saved("sa.budget_over", "Have you managed a budget > $100,000?", "Yes",
               SemanticType.CUSTOM_BOOLEAN)]})
    over = _field("over", "Have you managed a budget > $100,000?",
                  SemanticType.CUSTOM_BOOLEAN, ControlType.RADIO)
    under = _field("under", "Have you managed a budget < $100,000?",
                   SemanticType.CUSTOM_BOOLEAN, ControlType.RADIO)
    packet = _resolve(make_context, resolve, candidate, [over, under])

    assert packet.answer_for("over").value == ChoiceValue(value="y", label="Yes")
    assert packet.answer_for("over").provenance.reference_ids == ["sa.budget_over"]
    assert packet.answer_for("under") is None
    item = next(m for m in packet.missing_inputs if m.field_id == "under")
    assert item.reason is MissingReason.NO_ANSWER
    assert "< $100,000" in item.prompt


def test_dollar_salary_answer_does_not_fill_a_euro_salary_field(
    fictional_candidate, make_context, resolve
):
    candidate = fictional_candidate.model_copy(update={"saved_answers": [
        _saved("sa.salary_usd", "Expected salary $", "150000",
               SemanticType.SALARY_EXPECTATION)]})
    euro = _field("salary", "Expected salary", SemanticType.SALARY_EXPECTATION,
                  ControlType.TEXT, placeholder="€")
    dollars = _field("salary_usd", "Expected salary $", SemanticType.SALARY_EXPECTATION,
                     ControlType.TEXT)
    packet = _resolve(make_context, resolve, candidate, [euro, dollars])

    assert packet.answer_for("salary") is None
    assert packet.answer_for("salary_usd").provenance.reference_ids == ["sa.salary_usd"]
    item = next(m for m in packet.missing_inputs if m.field_id == "salary")
    assert item.reason is MissingReason.EXPLICIT_ANSWER_REQUIRED
    assert item.label == euro.question_text == "Expected salary\n€"
    assert "Expected salary\n€" in item.prompt


def test_unit_placeholder_blocks_label_only_reuse_and_is_prompted(
    fictional_candidate, make_context, resolve
):
    candidate = fictional_candidate.model_copy(update={"saved_answers": [
        _saved("sa.notice", "Notice period", "2", SemanticType.CUSTOM_TEXT)]})
    weeks = _field("notice", "Notice period", SemanticType.CUSTOM_TEXT, ControlType.TEXT,
                   placeholder="in weeks", help_text="Contractual notice only.")
    plain = _field("notice_plain", "Notice period", SemanticType.CUSTOM_TEXT, ControlType.TEXT,
                   placeholder="e.g. 2")
    packet = _resolve(make_context, resolve, candidate, [weeks, plain])

    assert packet.answer_for("notice") is None
    assert packet.answer_for("notice_plain").provenance.reference_ids == ["sa.notice"]
    item = next(m for m in packet.missing_inputs if m.field_id == "notice")
    assert "Notice period\nContractual notice only.\nin weeks" in item.prompt
