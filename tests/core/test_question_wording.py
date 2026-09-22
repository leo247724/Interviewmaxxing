"""C1R3: the complete question wording survives missing input -> user input -> saved answer."""

from __future__ import annotations

import pytest

from interviewmaxxing_core import (
    QUESTION_PART_SEPARATOR,
    AnswerReuse,
    AnswerScope,
    ApplicationField,
    ApplicationForm,
    BooleanValue,
    ControlType,
    JobRecord,
    MissingInput,
    MissingReason,
    SemanticType,
    TextValue,
    UserInput,
    normalize_text,
    render_question,
)

URL = "http://127.0.0.1:0/jobs/mock-4012/apply"
ACCURATE = "I certify that all application information is accurate."
NEVER_DISMISSED = "I certify that I have never been dismissed from any job."


def _form(**field: object) -> ApplicationForm:
    defaults: dict[str, object] = {
        "id": "q", "label": "I agree", "selector": "#q", "required": True,
        "semantic_type": SemanticType.ATTESTATION, "control_type": ControlType.CHECKBOX,
    }
    return ApplicationForm(url=URL, step=0, fields=[ApplicationField(**{**defaults, **field})])


def _job(identity_key: str | None = "ats:mock:mock-co:4012") -> JobRecord:
    return JobRecord(id="job_q", application_url=URL, normalized_url=URL,
                     identity_key=identity_key, company="Mock Co",
                     created_at="2026-09-22T20:00:00Z", updated_at="2026-09-22T20:00:00Z")


def _answer_and_save(form: ApplicationForm, value, *, reuse=AnswerReuse.GLOBAL):
    """Run the real path: MissingInput.for_field -> UserInput.answering -> to_saved_answer."""
    field = form.fields[0]
    missing = MissingInput.for_field(form, field, reason=MissingReason.UNCOVERED_ATTESTATION,
                                     prompt="Please answer")
    user = UserInput.answering(missing, value, reuse=reuse)
    return missing, user, user.to_saved_answer(job=_job())


# --- renderer ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "parts, expected",
    [
        (("I agree", None, None), "I agree"),
        (("I agree", ACCURATE, None), f"I agree\n{ACCURATE}"),
        (("Expected salary", "Annual base pay", "€"), "Expected salary\nAnnual base pay\n€"),
        (("Expected salary", None, "$"), "Expected salary\n$"),
        (("Expected salary", "   ", ""), "Expected salary"),
        (("", "Help only", None), "Help only"),
        (("  Budget   managed\n> $100,000? ", None, "USD, e.g. 250000"),
         "Budget managed > $100,000?\nUSD, e.g. 250000"),
    ],
)
def test_render_question_order_whitespace_and_empty_parts(parts, expected):
    assert render_question(*parts) == expected
    assert QUESTION_PART_SEPARATOR == "\n"


def test_render_question_preserves_meaning_bearing_symbols_and_case():
    text = render_question("Have you managed a budget < $100,000?", "Amounts in €, 10% tolerance")
    for token in ("<", "$", "€", "%", "Have", "?"):
        assert token in text
    assert render_question("Budget > $100,000?") != render_question("Budget < $100,000?")


def test_question_text_covers_exactly_what_the_fingerprint_covers():
    base = _form(help_text=ACCURATE, placeholder="Type YES")
    field = base.fields[0]
    assert field.question_text == f"I agree\n{ACCURATE}\nType YES"
    for change in ({"label": "I accept"}, {"help_text": NEVER_DISMISSED}, {"placeholder": "Y/N"}):
        other = field.model_copy(update=change)
        assert other.question_text != field.question_text
        assert other.fingerprint != field.fingerprint
    # Requiredness and selector are outside both.
    same = field.model_copy(update={"required": False, "selector": "#other"})
    assert (same.question_text, same.fingerprint) == (field.question_text, field.fingerprint)


# --- carriers: MissingInput.label -> UserInput.question -> SavedAnswer.question ------


def test_reviewer_case_attestation_help_text_survives_answer_and_save():
    form = _form(help_text=ACCURATE)
    missing, user, saved = _answer_and_save(form, BooleanValue(checked=True))
    full = f"I agree\n{ACCURATE}"
    assert missing.label == full
    assert user.question == full
    assert saved.question == full
    assert saved.scope is AnswerScope.GLOBAL and saved.semantic_type is SemanticType.ATTESTATION
    # The identical form asks the identical wording, so generation can match it exactly.
    same_again = _form(help_text=ACCURATE).fields[0]
    assert normalize_text(saved.question) == normalize_text(same_again.question_text)
    assert user.matches(_form(help_text=ACCURATE))


def test_same_label_with_changed_help_text_saves_a_different_question():
    _, _, accurate = _answer_and_save(_form(help_text=ACCURATE), BooleanValue(checked=True))
    _, _, dismissed = _answer_and_save(_form(help_text=NEVER_DISMISSED),
                                       BooleanValue(checked=True))
    assert accurate.question != dismissed.question
    other_field = _form(help_text=NEVER_DISMISSED).fields[0]
    assert normalize_text(accurate.question) != normalize_text(other_field.question_text)


def test_meaningful_currency_placeholder_is_kept_through_save():
    euro = _form(id="salary", label="Expected salary", placeholder="€",
                 semantic_type=SemanticType.SALARY_EXPECTATION, control_type=ControlType.TEXT)
    dollar = _form(id="salary", label="Expected salary", placeholder="$",
                   semantic_type=SemanticType.SALARY_EXPECTATION, control_type=ControlType.TEXT)
    missing, user, saved = _answer_and_save(euro, TextValue(text="90000"), reuse=AnswerReuse.JOB)
    assert missing.label == user.question == saved.question == "Expected salary\n€"
    assert saved.scope is AnswerScope.JOB and saved.job_identity_key == "ats:mock:mock-co:4012"
    assert normalize_text(saved.question) != normalize_text(dollar.fields[0].question_text)
    assert normalize_text(saved.question) != normalize_text("Expected salary")


def test_label_help_and_placeholder_all_reach_the_saved_answer():
    form = _form(id="budget", label="Largest budget managed", help_text="Annual, in USD",
                 placeholder="> $100,000", semantic_type=SemanticType.CUSTOM_TEXT,
                 control_type=ControlType.TEXT, required=False)
    field = form.fields[0]
    via_for_field = UserInput.for_field(form, "budget", TextValue(text="400000"),
                                        reuse=AnswerReuse.GLOBAL)
    _, via_answering, saved = _answer_and_save(form, TextValue(text="400000"))
    expected = "Largest budget managed\nAnnual, in USD\n> $100,000"
    assert field.question_text == expected
    assert via_for_field.question == via_answering.question == saved.question == expected


def test_label_only_fields_are_unchanged(mock_form, mock_packet):
    for field in mock_form.fields:
        assert field.question_text == field.label
    assert [m.label for m in mock_packet.missing_inputs] == [
        mock_form.field(m.field_id).label for m in mock_packet.missing_inputs
    ]
