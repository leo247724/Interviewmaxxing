"""The dashboard offers lookup suggestions as a select and accepts free text too."""
from __future__ import annotations

import pytest

from interviewmaxxing_core import (
    ApplicationField,
    ApplicationForm,
    ControlType,
    FieldOption,
    MissingInput,
    MissingReason,
    TextValue,
)
from interviewmaxxing_service.answers import _convert
from interviewmaxxing_service.views import _ANSWERABLE_CONTROLS, _control


def _lookup(options: list[str]) -> MissingInput:
    field = ApplicationField(id="location", label="Location", control_type=ControlType.TYPEAHEAD,
                             selector="#location", required=True)
    form = ApplicationForm(url="https://example.test/apply", fields=[field])
    item = MissingInput.for_field(form, field, reason=MissingReason.NO_ANSWER, prompt="Pick one.")
    return item.model_copy(update={"options": [FieldOption(value=o, label=o) for o in options]})


def test_lookup_is_answerable_and_rendered_as_a_select_when_suggestions_exist() -> None:
    assert ControlType.TYPEAHEAD in _ANSWERABLE_CONTROLS
    assert _control(_lookup(["Austin, TX, USA"])) == "single_select"
    assert _control(_lookup([])) == "text"


def test_lookup_conversion_accepts_a_suggestion_or_free_text() -> None:
    item = _lookup(["Austin, TX, USA", "Austin, MN, USA"])
    assert _convert(item, "Austin, TX, USA") == TextValue(text="Austin, TX, USA")
    assert _convert(item, " Round Rock, TX ") == TextValue(text="Round Rock, TX")
    with pytest.raises(ValueError):
        _convert(item, "")
    with pytest.raises(ValueError):
        _convert(item, ["Austin, TX, USA"])
