"""Round 10: a single choice among work modes (remote, hybrid, on-site) is the
work-location preference whatever the field's own type (fictional data only)."""
from __future__ import annotations

import pytest

from interviewmaxxing_core import (
    WORK_LOCATION_PREFERENCE_QUESTION,
    ApplicationField,
    ControlType,
    FieldOption,
    SemanticType,
    is_work_mode_choice,
    names_work_mode,
    stated_work_location_preference,
)


def _select(*labels: str, control: ControlType = ControlType.SELECT,
            semantic: SemanticType = SemanticType.LOCATION) -> ApplicationField:
    return ApplicationField(id="pref", selector="#pref", label="Location Preference",
                            semantic_type=semantic, control_type=control, required=True,
                            options=[FieldOption(value=f"v{i}", label=label)
                                     for i, label in enumerate(labels)])


@pytest.mark.parametrize("labels,expected", [
    (("Remote", "Hybrid", "On-site"), True),  # live Upstart
    (("Fully remote", "Hybrid (2-3 days in office)", "In-office", "No preference"), True),
    (("Remote (US only)", "Hybrid - 3 days a week", "Onsite"), True),
    (("Work from home", "In person", "Either"), True),
    (("Remote", "Prefer not to say"), False),  # one mode is no choice among modes
    (("Remote", "Austin, TX"), False),  # a place
    (("Remote", "Hybrid", "Full-time"), False),  # an employment type
    (("Yes", "No"), False),
    (("Remote",), False),
])
def test_a_work_mode_choice_has_only_work_mode_or_neutral_options(labels, expected):
    assert is_work_mode_choice(_select(*labels)) is expected
    assert is_work_mode_choice(_select(*labels, control=ControlType.RADIO)) is expected
    assert is_work_mode_choice(_select(*labels, semantic=SemanticType.CUSTOM_SELECT)) is expected


def test_a_multi_select_or_a_disabled_option_is_read_accordingly():
    assert not is_work_mode_choice(_select("Remote", "Hybrid", control=ControlType.MULTISELECT))
    field = _select("Remote", "Hybrid", "Austin, TX")
    disabled = field.model_copy(update={"options": [
        *field.options[:2], field.options[2].model_copy(update={"disabled": True})]})
    assert is_work_mode_choice(disabled)


@pytest.mark.parametrize("label,expected", [
    ("Remote", True), ("Hybrid", True), ("On-site", True), ("Onsite", True), ("In-office", True),
    ("In person", True), ("WFH", True), ("Telecommuting", True), ("No preference", False),
    ("Austin, TX", False), ("Yes", False),
])
def test_names_work_mode(label, expected):
    assert names_work_mode(label) is expected


def test_the_preference_question_is_recognised_whatever_its_spacing_and_case():
    assert stated_work_location_preference(WORK_LOCATION_PREFERENCE_QUESTION)
    assert stated_work_location_preference("  which work arrangement do you prefer:  remote, hybrid or on-site? ")
    assert not stated_work_location_preference("Where are you based?")
