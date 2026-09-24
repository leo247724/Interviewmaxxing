"""Normalized application forms: field ontology, controls and options.

The browser package produces these from a live page; the packet package answers
them. Answers target ``ApplicationField.id`` and option ``value``s, never raw DOM
selectors or visible labels (ARCHITECTURE.md section 7).

Identity. A form step is identified by its ``FormScope`` (normalized URL + step) and
its ``ApplicationForm.fingerprint``. A question is identified by its scope, its
``field_id`` and ``ApplicationField.fingerprint`` (normalized label, help text,
placeholder, control type and options). A field id reused on another step, or for a
changed question, therefore never matches an answer given for the original question.

Wording. ``render_question`` / ``ApplicationField.question_text`` render the same
label, help text and placeholder as raw readable text. It is the one source for the
wording carried by ``MissingInput.label``, ``UserInput.question`` and
``SavedAnswer.question``.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Self

from pydantic import Field, model_validator

from ._base import Contract, NonEmptyStr, UtcDatetime, utc_now
from .urls import InvalidApplicationUrl, normalize_application_url


def normalize_text(value: str) -> str:
    """Case-folded text with collapsed whitespace, for comparing visible labels."""
    return " ".join(value.split()).casefold()


QUESTION_PART_SEPARATOR = "\n"


def render_question(
    label: str, help_text: str | None = None, placeholder: str | None = None
) -> str:
    """The complete question wording a user sees, as raw readable text.

    Components appear in this fixed order: label, help text, placeholder. Each is
    trimmed and has internal whitespace runs collapsed to one space; empty
    components are omitted; the rest are joined with ``QUESTION_PART_SEPARATOR``
    (a newline). Nothing else is changed: case, punctuation, comparison signs,
    currency symbols and units (``<``, ``>``, ``$``, ``€``, ``%``) are kept verbatim,
    and no decoration such as "placeholder:" is added. Comparing with
    ``normalize_text`` therefore treats the separator as a single space.
    """
    parts = (" ".join((part or "").split()) for part in (label, help_text, placeholder))
    return QUESTION_PART_SEPARATOR.join(part for part in parts if part)


def _digest(data: Any) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


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


PROFILE_IDENTITY_TYPES: frozenset[SemanticType] = frozenset(
    {
        SemanticType.FIRST_NAME,
        SemanticType.LAST_NAME,
        SemanticType.FULL_NAME,
        SemanticType.PREFERRED_NAME,
        SemanticType.EMAIL,
        SemanticType.PHONE,
        SemanticType.ADDRESS,
        SemanticType.CITY,
        SemanticType.STATE,
        SemanticType.ZIP,
        SemanticType.COUNTRY,
        SemanticType.LOCATION,
        SemanticType.LINKEDIN,
        SemanticType.WEBSITE,
        SemanticType.GITHUB,
    }
)
"""The only semantic types a ``PROFILE_IDENTITY``-sourced answer may fill."""


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
    section_context: list[str] = Field(default_factory=list)
    """Headings and group labels of the sections that precede this control, outermost
    first. Model context for subject and timeframe only: it is not part of the
    question the user sees, so ``fingerprint`` and ``question_text`` exclude it and
    saved answers keep matching on the bare wording."""

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

    @property
    def fingerprint(self) -> str:
        """Identity of the question the user sees: the complete normalized question
        text (label, help text, placeholder), control type and options (value and
        label). Any wording change makes it a different question. Selector,
        requiredness, validation messages and input type are excluded."""
        options = sorted(
            [o.value, normalize_text(o.label)] for o in self.options or []
        )
        return _digest(
            {"label": normalize_text(self.label),
             "help_text": normalize_text(self.help_text or ""),
             "placeholder": normalize_text(self.placeholder or ""),
             "control": self.control_type.value,
             "options": options}
        )

    @property
    def question_text(self) -> str:
        """``render_question(label, help_text, placeholder)``: the full wording shown to
        the user, used for ``MissingInput.label``, ``UserInput.question`` and hence
        ``SavedAnswer.question``. Covers the same text as ``fingerprint``."""
        return render_question(self.label, self.help_text, self.placeholder)

    def option_values(self) -> list[str]:
        return [o.value for o in self.options or []]

    def option_for_value(self, value: str) -> FieldOption | None:
        return next((o for o in self.options or [] if o.value == value), None)


class FormScope(Contract):
    """Which form step something belongs to: the normalized form URL and step index."""

    url: NonEmptyStr
    step: int = Field(ge=0)

    @classmethod
    def of(cls, url: str, step: int) -> FormScope:
        try:
            normalized = normalize_application_url(url)
        except InvalidApplicationUrl:
            normalized = url.strip()
        return cls(url=normalized, step=step)

    @property
    def key(self) -> str:
        return f"{self.url}|step={self.step}"


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

    @property
    def scope(self) -> FormScope:
        return FormScope.of(self.url, self.step)

    @property
    def fingerprint(self) -> str:
        """Identity of this inspected step: scope plus every field id and question
        fingerprint. A packet is valid only for the form it was resolved against."""
        return _digest(
            {"scope": self.scope.key,
             "fields": sorted([f.id, f.fingerprint] for f in self.fields)}
        )

    def find(self, field_id: str) -> ApplicationField | None:
        return next((f for f in self.fields if f.id == field_id), None)

    def field(self, field_id: str) -> ApplicationField:
        for f in self.fields:
            if f.id == field_id:
                return f
        raise KeyError(field_id)

    def required_fields(self) -> list[ApplicationField]:
        return [f for f in self.fields if f.required]
