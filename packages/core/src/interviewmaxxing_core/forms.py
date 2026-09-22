"""Normalized application forms: field ontology, controls and options.

The browser package produces these from a live page; the packet package answers
them. Answers target ``ApplicationField.id`` and option ``value``s, never raw DOM
selectors or visible labels (ARCHITECTURE.md section 7).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from ._base import Contract, NonEmptyStr, UtcDatetime, utc_now


class SemanticType(StrEnum):
    """What a field asks for, independent of how the page renders it."""

    FIRST_NAME = "FIRST_NAME"
    LAST_NAME = "LAST_NAME"
    FULL_NAME = "FULL_NAME"
    PREFERRED_NAME = "PREFERRED_NAME"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    ADDRESS = "ADDRESS"
    CITY = "CITY"
    STATE = "STATE"
    ZIP = "ZIP"
    COUNTRY = "COUNTRY"
    LOCATION = "LOCATION"

    RESUME = "RESUME"
    COVER_LETTER = "COVER_LETTER"

    LINKEDIN = "LINKEDIN"
    WEBSITE = "WEBSITE"
    GITHUB = "GITHUB"

    CURRENT_COMPANY = "CURRENT_COMPANY"
    CURRENT_TITLE = "CURRENT_TITLE"

    WORK_AUTHORIZATION = "WORK_AUTHORIZATION"
    SPONSORSHIP = "SPONSORSHIP"
    SALARY_EXPECTATION = "SALARY_EXPECTATION"
    START_DATE = "START_DATE"
    RELOCATION = "RELOCATION"

    EDUCATION_LEVEL = "EDUCATION_LEVEL"
    UNIVERSITY = "UNIVERSITY"
    DEGREE = "DEGREE"

    YEARS_EXPERIENCE = "YEARS_EXPERIENCE"
    REFERRAL_SOURCE = "REFERRAL_SOURCE"

    # Voluntary self-identification. Never inferred; only an explicit saved answer
    # or direct user input may fill these.
    EEO_GENDER = "EEO_GENDER"
    EEO_RACE_ETHNICITY = "EEO_RACE_ETHNICITY"
    EEO_VETERAN_STATUS = "EEO_VETERAN_STATUS"
    EEO_DISABILITY_STATUS = "EEO_DISABILITY_STATUS"
    PRONOUNS = "PRONOUNS"

    # Personal consent / attestation ("I certify...", privacy-policy consent).
    CONSENT = "CONSENT"
    ATTESTATION = "ATTESTATION"

    CUSTOM_TEXT = "CUSTOM_TEXT"
    CUSTOM_LONG_TEXT = "CUSTOM_LONG_TEXT"
    CUSTOM_BOOLEAN = "CUSTOM_BOOLEAN"
    CUSTOM_SELECT = "CUSTOM_SELECT"
    CUSTOM_MULTISELECT = "CUSTOM_MULTISELECT"

    UNKNOWN = "UNKNOWN"


PROTECTED_ATTRIBUTE_TYPES: frozenset[SemanticType] = frozenset(
    {
        SemanticType.EEO_GENDER,
        SemanticType.EEO_RACE_ETHNICITY,
        SemanticType.EEO_VETERAN_STATUS,
        SemanticType.EEO_DISABILITY_STATUS,
        SemanticType.PRONOUNS,
    }
)

EXPLICIT_ANSWER_REQUIRED: frozenset[SemanticType] = PROTECTED_ATTRIBUTE_TYPES | frozenset(
    {
        SemanticType.WORK_AUTHORIZATION,
        SemanticType.SPONSORSHIP,
        SemanticType.SALARY_EXPECTATION,
        SemanticType.CONSENT,
        SemanticType.ATTESTATION,
    }
)
"""Semantic types that may be answered only from an explicit saved answer or user
input: consent, protected attributes, eligibility and salary. Packet answers for
these types are rejected if their provenance is anything else (see
``interviewmaxxing_core.packets.PacketAnswer``)."""


class ControlType(StrEnum):
    """How the page renders a field, which determines how the browser operates it."""

    TEXT = "TEXT"
    """Single-line input (``input_type`` gives the HTML type: text, email, tel, url, number, date)."""
    TEXTAREA = "TEXTAREA"
    SELECT = "SELECT"
    """Single-choice select or combobox."""
    MULTISELECT = "MULTISELECT"
    """Multi-choice select or tag picker."""
    RADIO = "RADIO"
    """Radio group; exactly one option."""
    CHECKBOX = "CHECKBOX"
    """A single boolean checkbox (e.g. consent)."""
    CHECKBOX_GROUP = "CHECKBOX_GROUP"
    """Several checkboxes answering one question; zero or more options."""
    FILE = "FILE"
    UNSUPPORTED = "UNSUPPORTED"
    """Present on the page but not operable by the runtime; route to the user."""


CHOICE_CONTROLS: frozenset[ControlType] = frozenset(
    {ControlType.SELECT, ControlType.RADIO, ControlType.MULTISELECT, ControlType.CHECKBOX_GROUP}
)
MULTI_CHOICE_CONTROLS: frozenset[ControlType] = frozenset(
    {ControlType.MULTISELECT, ControlType.CHECKBOX_GROUP}
)


class FieldOption(Contract):
    """One choice. ``value`` is the machine value the page submits; ``label`` is the
    user-visible text. Packets select by ``value``; humans are shown ``label``."""

    value: str
    label: str
    selector: str | None = None
    """Per-option element for radio/checkbox groups, when it differs from the field's."""
    disabled: bool = False


class ApplicationField(Contract):
    id: NonEmptyStr
    """Stable within the form (derived from name/id attributes), used as the answer key."""
    label: str
    semantic_type: SemanticType = SemanticType.UNKNOWN
    control_type: ControlType
    selector: NonEmptyStr
    required: bool = False
    input_type: str | None = None
    options: list[FieldOption] | None = None
    accept: list[str] | None = None
    """For FILE controls: accepted extensions or media types, e.g. [".pdf", ".docx"]."""
    max_length: int | None = Field(default=None, ge=1)
    placeholder: str | None = None
    help_text: str | None = None
    validation_error: str | None = None
    """Validation message the page currently shows for this field, if any."""

    @model_validator(mode="after")
    def _options_match_control(self) -> Self:
        if self.control_type in CHOICE_CONTROLS:
            if not self.options:
                raise ValueError(f"{self.control_type} field {self.id!r} needs options")
            values = [o.value for o in self.options]
            if len(set(values)) != len(values):
                raise ValueError(f"field {self.id!r} has duplicate option values")
        elif self.options:
            raise ValueError(f"{self.control_type} field {self.id!r} must not have options")
        if self.accept is not None and self.control_type is not ControlType.FILE:
            raise ValueError("accept is only valid for FILE controls")
        return self

    def option_values(self) -> list[str]:
        return [o.value for o in self.options or []]

    def option_for_value(self, value: str) -> FieldOption | None:
        return next((o for o in self.options or [] if o.value == value), None)


class ApplicationForm(Contract):
    """The fields visible on one step of an application."""

    url: NonEmptyStr
    ats_type: str = "generic"
    step: int = Field(default=0, ge=0)
    """Zero-based index of this step within a multi-page application."""
    fields: list[ApplicationField]
    is_final_step: bool | None = None
    """True when the step's primary action submits; None if the page does not say."""
    submit_selector: str | None = None
    next_selector: str | None = None
    page_errors: list[str] = Field(default_factory=list)
    """Form-level validation messages currently shown."""
    inspected_at: UtcDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _unique_field_ids(self) -> Self:
        ids = [f.id for f in self.fields]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate field ids: {dupes}")
        return self

    def field(self, field_id: str) -> ApplicationField:
        for f in self.fields:
            if f.id == field_id:
                return f
        raise KeyError(field_id)

    def required_fields(self) -> list[ApplicationField]:
        return [f for f in self.fields if f.required]
