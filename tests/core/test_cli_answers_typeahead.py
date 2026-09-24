"""Lookup (TYPEAHEAD) questions can be answered from the terminal and the answer file."""
from __future__ import annotations

import pytest

from interviewmaxxing_cli.answers import AnswerError, value_for
from interviewmaxxing_core import (
    ApplicationField,
    ApplicationForm,
    ControlType,
    FieldOption,
    MissingInput,
    MissingReason,
    TextValue,
)


def _lookup(options: list[str]) -> MissingInput:
    field = ApplicationField(id="location", label="Location (City)", control_type=ControlType.TYPEAHEAD,
                             selector="#location", required=True)
    form = ApplicationForm(url="https://example.test/apply", fields=[field])
    item = MissingInput.for_field(form, field, reason=MissingReason.NO_ANSWER,
                                  prompt="Choose the suggestion that is your location.")
    return item.model_copy(update={"options": [FieldOption(value=o, label=o) for o in options]})


def test_lookup_answer_takes_a_listed_suggestion_by_label_or_free_text() -> None:
    item = _lookup(["Austin, Texas, United States", "Austin, Minnesota, United States"])
    assert value_for(item, "austin, texas, united states") == TextValue(text="Austin, Texas, United States")
    assert value_for(item, "Round Rock, Texas") == TextValue(text="Round Rock, Texas")
    assert value_for(_lookup([]), "Austin, TX") == TextValue(text="Austin, TX")


def test_lookup_answer_needs_text() -> None:
    with pytest.raises(AnswerError):
        value_for(_lookup([]), "   ")
    with pytest.raises(AnswerError):
        value_for(_lookup([]), True)
