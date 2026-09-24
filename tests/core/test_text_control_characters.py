"""A typed newline is an Enter key inside the form: the packet contract rejects control
characters in single-line text values, and keeps only newline, carriage return and tab
in text areas."""

import pytest

from interviewmaxxing_core.forms import ApplicationField, ControlType
from interviewmaxxing_core.packets import TextValue, answer_problems, text_control_problems


def _field(control: ControlType, *, required: bool = True) -> ApplicationField:
    return ApplicationField(id="q", label="Question", control_type=control, selector="#q",
                            required=required)


@pytest.mark.parametrize("control", [ControlType.TEXT, ControlType.TYPEAHEAD])
@pytest.mark.parametrize("text", ["Austin, TX\n", "Austin\r\nTX", "a\tb", "+1 512 555 0100\x7f",
                                  "\x1bAustin"])
def test_single_line_controls_reject_every_control_character(control, text):
    problems = answer_problems(_field(control), TextValue(text=text))
    assert len(problems) == 1
    assert "control character" in problems[0]
    assert "single-line" in problems[0]


@pytest.mark.parametrize("control", [ControlType.TEXT, ControlType.TYPEAHEAD])
def test_single_line_controls_accept_ordinary_text(control):
    assert answer_problems(_field(control), TextValue(text="Austin, TX — Travis County")) == []


def test_text_areas_keep_newlines_and_tabs_but_reject_other_control_characters():
    area = _field(ControlType.TEXTAREA)
    assert answer_problems(area, TextValue(text="Dear team,\n\nFirst line.\r\n\tIndented.")) == []
    problems = answer_problems(area, TextValue(text="Dear team,\x0cnext page"))
    assert problems == ["text for 'q' contains the control character U+000C"]


def test_text_control_problems_names_the_first_offending_character():
    assert text_control_problems(_field(ControlType.TEXT), "clean") == []
    (problem,) = text_control_problems(_field(ControlType.TEXT), "one\ntwo\x00")
    assert "U+000A" in problem
