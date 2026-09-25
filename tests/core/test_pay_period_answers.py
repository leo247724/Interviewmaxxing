"""Round 8: a salary answer may give its pay period to a single choice of another type,
and only to one whose options are all pay periods (fictional data only)."""
from __future__ import annotations

import pytest

from interviewmaxxing_core import (
    AnswerSource,
    ApplicationField,
    ApplicationForm,
    ChoiceValue,
    ControlType,
    FieldOption,
    PacketAnswer,
    Provenance,
    SavedAnswer,
    SemanticType,
    is_pay_period_choice,
    pay_period_of,
)
from interviewmaxxing_core.packets import provenance_problems


def _select(*labels: str, control: ControlType = ControlType.SELECT) -> ApplicationField:
    return ApplicationField(id="period", selector="#period", label="",
                            semantic_type=SemanticType.CUSTOM_SELECT, control_type=control,
                            required=True,
                            options=[FieldOption(value=f"v{i}", label=label)
                                     for i, label in enumerate(labels)])


@pytest.mark.parametrize("labels,period", [
    (("Hourly", "Weekly", "Monthly", "Yearly"), True),
    (("Hourly", "Annual"), True),
    (("Per hour", "Per year"), True),
    (("Yearly",), False),  # one option is not a period choice
    (("Hourly", "Full-time"), False),
    (("Morning", "Evening"), False),
])
def test_a_pay_period_choice_has_only_pay_period_options(labels, period):
    assert is_pay_period_choice(_select(*labels)) is period
    assert pay_period_of("Yearly") == "year" and pay_period_of("Full-time") is None


@pytest.mark.parametrize("labels,allowed", [
    (("Hourly", "Weekly", "Monthly", "Yearly"), True),
    (("Full-time", "Part-time"), False),
])
def test_only_a_pay_period_choice_may_cite_a_salary_answer_of_another_type(
    fictional_candidate, mock_job, mock_packet, labels, allowed
):
    field = _select(*labels)
    form = ApplicationForm(url=mock_packet.form_url, fields=[field])
    salary = SavedAnswer(id="sa.salary", scope="GLOBAL",
                         semantic_type=SemanticType.SALARY_EXPECTATION,
                         question="What is your desired salary?", value="USD 95,000 per year",
                         confirmed_at="2026-09-01T12:00:00Z")
    candidate = fictional_candidate.model_copy(update={"saved_answers": [salary]})
    answer = PacketAnswer(field_id="period", semantic_type=SemanticType.CUSTOM_SELECT,
                          value=ChoiceValue(value="v0", label=labels[0]),
                          provenance=Provenance(source=AnswerSource.SAVED_ANSWER,
                                                reference_ids=["sa.salary"]))
    packet = mock_packet.model_copy(update={"answers": [answer], "missing_inputs": [],
                                            "form_fingerprint": form.fingerprint})
    problems = provenance_problems(packet, form=form, candidate=candidate, job=mock_job)
    assert (problems == []) is allowed
    if not allowed:
        assert any("SALARY_EXPECTATION" in problem for problem in problems)
