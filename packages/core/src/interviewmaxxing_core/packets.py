"""Application packets: per-field answers with provenance, and missing inputs.

A packet answers one ``ApplicationForm`` step. Every answer records where it came
from. Anything that cannot be answered from verified data is reported as a
``MissingInput`` rather than guessed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ._base import Confidence, Contract, NonEmptyStr, UtcDatetime, new_id, utc_now
from .artifacts import ArtifactRef
from .forms import (
    EXPLICIT_ANSWER_REQUIRED,
    MULTI_CHOICE_CONTROLS,
    ApplicationField,
    ApplicationForm,
    ControlType,
    FieldOption,
    SemanticType,
)

# --- answer values ---------------------------------------------------------------


class TextValue(Contract):
    kind: Literal["text"] = "text"
    text: str


class ChoiceValue(Contract):
    """A single option, by machine ``value``; ``label`` is kept for display and
    for verifying that the page still shows the expected text."""

    kind: Literal["choice"] = "choice"
    value: str
    label: str


class MultiChoiceValue(Contract):
    kind: Literal["multi_choice"] = "multi_choice"
    choices: list[FieldOption]


class BooleanValue(Contract):
    kind: Literal["boolean"] = "boolean"
    checked: bool


class FileValue(Contract):
    kind: Literal["file"] = "file"
    artifact: ArtifactRef


AnswerValue = Annotated[
    TextValue | ChoiceValue | MultiChoiceValue | BooleanValue | FileValue,
    Field(discriminator="kind"),
]

_ACCEPTED_VALUES: dict[ControlType, tuple[type[Contract], ...]] = {
    ControlType.TEXT: (TextValue,),
    ControlType.TEXTAREA: (TextValue,),
    ControlType.SELECT: (ChoiceValue,),
    ControlType.RADIO: (ChoiceValue,),
    ControlType.MULTISELECT: (MultiChoiceValue,),
    ControlType.CHECKBOX_GROUP: (MultiChoiceValue,),
    ControlType.CHECKBOX: (BooleanValue,),
    ControlType.FILE: (FileValue,),
    ControlType.UNSUPPORTED: (),
}


def answer_problems(field: ApplicationField, value: AnswerValue) -> list[str]:
    """Why ``value`` cannot be applied to ``field`` (empty when compatible)."""
    accepted = _ACCEPTED_VALUES[field.control_type]
    if not isinstance(value, accepted):
        return [f"{field.control_type} field {field.id!r} cannot take a {value.kind} value"]
    problems: list[str] = []
    known = set(field.option_values())
    if isinstance(value, ChoiceValue) and value.value not in known:
        problems.append(f"{value.value!r} is not an option of {field.id!r}")
    if isinstance(value, MultiChoiceValue):
        unknown = [c.value for c in value.choices if c.value not in known]
        if unknown:
            problems.append(f"{unknown} are not options of {field.id!r}")
    if isinstance(value, TextValue) and field.max_length and len(value.text) > field.max_length:
        problems.append(f"text for {field.id!r} exceeds max_length {field.max_length}")
    if field.required and isinstance(value, TextValue) and not value.text.strip():
        problems.append(f"required field {field.id!r} has an empty answer")
    if field.required and isinstance(value, MultiChoiceValue) and not value.choices:
        problems.append(f"required field {field.id!r} has no selected options")
    if field.required and isinstance(value, BooleanValue) and not value.checked:
        problems.append(f"required checkbox {field.id!r} is left unchecked")
    return problems


# --- provenance ------------------------------------------------------------------


class AnswerSource(StrEnum):
    PROFILE_IDENTITY = "PROFILE_IDENTITY"
    """Verified contact details (``CandidateIdentity``)."""
    CANDIDATE_FACT = "CANDIDATE_FACT"
    SAVED_ANSWER = "SAVED_ANSWER"
    RESUME = "RESUME"
    """The supplied resume document itself (file uploads)."""
    USER_INPUT = "USER_INPUT"
    """Answered by the user for this application (missing-input resume)."""
    GENERATED_FROM_FACTS = "GENERATED_FROM_FACTS"
    """Text drafted from candidate facts; ``reference_ids`` must list those facts."""


EXPLICIT_SOURCES: frozenset[AnswerSource] = frozenset(
    {AnswerSource.SAVED_ANSWER, AnswerSource.USER_INPUT}
)


class Provenance(Contract):
    source: AnswerSource
    reference_ids: list[str] = Field(default_factory=list)
    """Fact ids, saved-answer ids, artifact id or user-input id backing the answer."""
    note: str | None = None

    @model_validator(mode="after")
    def _references_required(self) -> Self:
        needs_refs = {
            AnswerSource.CANDIDATE_FACT,
            AnswerSource.SAVED_ANSWER,
            AnswerSource.GENERATED_FROM_FACTS,
            AnswerSource.RESUME,
            AnswerSource.USER_INPUT,
        }
        if self.source in needs_refs and not self.reference_ids:
            raise ValueError(f"{self.source} provenance must reference its source ids")
        return self


class PacketAnswer(Contract):
    field_id: NonEmptyStr
    semantic_type: SemanticType
    value: AnswerValue
    provenance: Provenance
    confidence: Confidence = 1.0

    @model_validator(mode="after")
    def _explicit_when_sensitive(self) -> Self:
        if (
            self.semantic_type in EXPLICIT_ANSWER_REQUIRED
            and self.provenance.source not in EXPLICIT_SOURCES
        ):
            raise ValueError(
                f"{self.semantic_type} answers must come from a saved answer or user input, "
                f"not {self.provenance.source}"
            )
        return self


# --- missing input ---------------------------------------------------------------


class MissingReason(StrEnum):
    NO_ANSWER = "NO_ANSWER"
    """No verified data answers this required question."""
    EXPLICIT_ANSWER_REQUIRED = "EXPLICIT_ANSWER_REQUIRED"
    """Consent, protected attribute, eligibility or salary with no saved answer."""
    UNCOVERED_ATTESTATION = "UNCOVERED_ATTESTATION"
    """A personal attestation the user has not already made."""
    AMBIGUOUS = "AMBIGUOUS"
    """Saved data could map to more than one option, or conflicts."""
    UNSUPPORTED_CONTROL = "UNSUPPORTED_CONTROL"
    USER_ACTION = "USER_ACTION"
    """The page needs the user (sign-in, CAPTCHA, email verification)."""


class MissingInput(Contract):
    """A question the user must answer (or an action they must take) to continue."""

    id: NonEmptyStr = Field(default_factory=lambda: new_id("mi"))
    field_id: str | None
    """None only for ``USER_ACTION`` items that are not tied to a field."""
    label: str
    reason: MissingReason
    prompt: NonEmptyStr
    """What to ask the user, in plain language."""
    semantic_type: SemanticType = SemanticType.UNKNOWN
    control_type: ControlType | None = None
    options: list[FieldOption] | None = None
    required: bool = True
    candidates: list[AnswerValue] = Field(default_factory=list)
    """For AMBIGUOUS: the plausible answers the user can choose between."""

    @model_validator(mode="after")
    def _field_or_action(self) -> Self:
        if self.field_id is None and self.reason is not MissingReason.USER_ACTION:
            raise ValueError("only USER_ACTION missing inputs may omit field_id")
        return self


class UserInput(Contract):
    """The user's answer to a ``MissingInput``, stored for resume."""

    id: NonEmptyStr = Field(default_factory=lambda: new_id("ui"))
    field_id: NonEmptyStr
    question: str
    """The field label the user saw when answering."""
    semantic_type: SemanticType = SemanticType.UNKNOWN
    value: AnswerValue
    save_for_reuse: bool = False
    """The user asked to keep this as a saved answer for future applications."""
    provided_at: UtcDatetime = Field(default_factory=utc_now)


# --- packet ----------------------------------------------------------------------


class ApplicationPacket(Contract):
    id: NonEmptyStr = Field(default_factory=lambda: new_id("pkt"))
    application_id: NonEmptyStr
    job_id: NonEmptyStr
    candidate_id: NonEmptyStr
    form_url: NonEmptyStr
    form_step: int = Field(ge=0)
    resume_variant: NonEmptyStr = "supplied"
    answers: list[PacketAnswer] = Field(default_factory=list)
    missing_inputs: list[MissingInput] = Field(default_factory=list)
    cover_letter: str | None = None
    created_at: UtcDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _one_answer_per_field(self) -> Self:
        ids = [a.field_id for a in self.answers]
        if len(set(ids)) != len(ids):
            raise ValueError("a packet may answer each field at most once")
        answered = set(ids)
        both = [m.field_id for m in self.missing_inputs if m.field_id in answered]
        if both:
            raise ValueError(f"fields both answered and missing: {both}")
        return self

    @property
    def unresolved_fields(self) -> list[str]:
        return [m.field_id for m in self.missing_inputs if m.field_id is not None]

    @property
    def is_complete(self) -> bool:
        """True when nothing required is missing."""
        return not any(m.required for m in self.missing_inputs)

    def answer_for(self, field_id: str) -> PacketAnswer | None:
        return next((a for a in self.answers if a.field_id == field_id), None)

    def problems_against(self, form: ApplicationForm) -> list[str]:
        """Consistency problems between this packet and the form it answers:
        unknown fields, incompatible values, and required fields neither answered
        nor reported missing."""
        problems: list[str] = []
        by_id = {f.id: f for f in form.fields}
        for answer in self.answers:
            field = by_id.get(answer.field_id)
            if field is None:
                problems.append(f"answer for unknown field {answer.field_id!r}")
                continue
            problems.extend(answer_problems(field, answer.value))
        accounted = {a.field_id for a in self.answers} | set(self.unresolved_fields)
        for field in form.required_fields():
            if field.id not in accounted:
                problems.append(f"required field {field.id!r} is neither answered nor missing")
        return problems


def is_multi_choice(field: ApplicationField) -> bool:
    return field.control_type in MULTI_CHOICE_CONTROLS
