"""Application packets: per-field answers with provenance, and missing inputs.

A packet answers one inspected ``ApplicationForm`` step. Every answer records where
it came from. Anything that cannot be answered from verified data is reported as a
``MissingInput`` rather than guessed.

Validation happens against the *actual* inspected field, never against what an
answer claims about itself:

* ``answer_problems(field, value)`` — value kind, enabled option, label/value
  agreement, placeholders, duplicates, length, required, file type.
* ``ApplicationPacket.problems_against(form)`` — the packet belongs to this form
  step (scope and fingerprint), each answer's semantic type equals the field's, and
  fields whose actual type needs an explicit answer are only answered from a saved
  answer or user input.
* ``provenance_problems(packet, ...)`` — every referenced fact is verified, every
  saved answer is in scope for the job, user inputs belong to this form step and
  question, and the resume reference matches the supplied file.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ._base import Confidence, Contract, NonEmptyStr, UtcDatetime, new_id, utc_now
from .artifacts import ArtifactRef
from .authorization import is_pay_period_choice
from .candidate import AnswerScope, CandidateProfile, SavedAnswer, SavedAnswerValue
from .forms import (
    ADDRESS_DERIVED_TYPES,
    EXPLICIT_ANSWER_REQUIRED,
    MULTI_CHOICE_CONTROLS,
    PROFILE_IDENTITY_TYPES,
    ApplicationField,
    ApplicationForm,
    ControlType,
    FieldOption,
    FormScope,
    SemanticType,
    normalize_text,
)
from .jobs import JobRecord

_SHA256 = r"^[0-9a-f]{64}$"

# --- answer values ---------------------------------------------------------------


class TextValue(Contract):
    kind: Literal["text"] = "text"
    text: str


class ChoiceValue(Contract):
    """A single option: machine ``value`` plus the visible ``label`` it must match."""

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
    ControlType.TYPEAHEAD: (TextValue,),
    ControlType.UNSUPPORTED: (),
}


def _option_problems(field: ApplicationField, value: str, label: str) -> list[str]:
    option = field.option_for_value(value)
    if option is None:
        return [f"{value!r} is not an option of {field.id!r}"]
    problems = []
    if option.disabled:
        problems.append(f"option {value!r} of {field.id!r} is disabled")
    if not option.value.strip():
        problems.append(f"{field.id!r}: the empty option {option.label!r} is not an answer")
    if normalize_text(label) != normalize_text(option.label):
        problems.append(
            f"{field.id!r}: label {label!r} does not match option {value!r} ({option.label!r})"
        )
    return problems


def _accepts(accept: list[str], artifact: ArtifactRef) -> bool:
    name = artifact.filename.lower()
    media = artifact.media_type.lower()
    for raw in accept:
        rule = raw.strip().lower()
        if rule.startswith("."):
            if name.endswith(rule):
                return True
        elif rule.endswith("/*"):
            if media.startswith(rule[:-1]):
                return True
        elif media == rule:
            return True
    return False


_SINGLE_LINE_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
"""Control characters, including newline: typed key by key into a single-line input, a
newline is an Enter key and submits the form (implicit submission)."""
_MULTILINE_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
"""Control characters other than newline, carriage return and tab, which a text area
legitimately holds."""


def text_control_problems(field: ApplicationField, text: str) -> list[str]:
    """Control characters that must never reach ``field`` (empty when the text is clean).

    Everything except a TEXTAREA is a single-line control: a newline typed into it presses
    Enter inside the application form, which can submit or advance it outside the
    submission guards. Text areas keep newlines, carriage returns and tabs only."""
    pattern = (_MULTILINE_CONTROL_CHARACTERS if field.control_type is ControlType.TEXTAREA
               else _SINGLE_LINE_CONTROL_CHARACTERS)
    match = pattern.search(text)
    if match is None:
        return []
    code = f"U+{ord(match.group(0)):04X}"
    if field.control_type is ControlType.TEXTAREA:
        return [f"text for {field.id!r} contains the control character {code}"]
    return [f"text for {field.id!r} contains the control character {code}; a newline or "
            "other control key cannot be typed into a single-line field"]


def answer_problems(field: ApplicationField, value: AnswerValue) -> list[str]:
    """Why ``value`` cannot be applied to ``field`` (empty when compatible)."""
    accepted = _ACCEPTED_VALUES[field.control_type]
    if not isinstance(value, accepted):
        return [f"{field.control_type} field {field.id!r} cannot take a {value.kind} value"]
    problems: list[str] = []
    if isinstance(value, ChoiceValue):
        problems.extend(_option_problems(field, value.value, value.label))
    elif isinstance(value, MultiChoiceValue):
        values = [c.value for c in value.choices]
        repeated = sorted({v for v in values if values.count(v) > 1})
        if repeated:
            problems.append(f"{field.id!r}: options selected more than once: {repeated}")
        for choice in value.choices:
            problems.extend(_option_problems(field, choice.value, choice.label))
        if field.required and not value.choices:
            problems.append(f"required field {field.id!r} has no selected options")
    elif isinstance(value, TextValue):
        if field.max_length and len(value.text) > field.max_length:
            problems.append(f"text for {field.id!r} exceeds max_length {field.max_length}")
        if field.required and not value.text.strip():
            problems.append(f"required field {field.id!r} has an empty answer")
        problems.extend(text_control_problems(field, value.text))
    elif isinstance(value, BooleanValue):
        if field.required and not value.checked:
            problems.append(f"required checkbox {field.id!r} is left unchecked")
    elif isinstance(value, FileValue) and field.accept and not _accepts(field.accept, value.artifact):
        problems.append(
            f"{value.artifact.filename!r} ({value.artifact.media_type}) is not accepted by "
            f"{field.id!r} ({', '.join(field.accept)})"
        )
    return problems


# --- provenance ------------------------------------------------------------------


class AnswerSource(StrEnum):
    PROFILE_IDENTITY = "PROFILE_IDENTITY"
    """Verified contact details (``CandidateIdentity``); only ``PROFILE_IDENTITY_TYPES``."""
    CANDIDATE_FACT = "CANDIDATE_FACT"
    SAVED_ANSWER = "SAVED_ANSWER"
    RESUME = "RESUME"
    """The supplied resume document itself (file uploads)."""
    USER_INPUT = "USER_INPUT"
    """Answered by the user for this application (missing-input resume)."""
    GENERATED_FROM_FACTS = "GENERATED_FROM_FACTS"
    """Text drafted from verified candidate facts; ``reference_ids`` lists them."""
    GENERATED_FROM_QUESTION = "GENERATED_FROM_QUESTION"
    """Text computed from the data the question itself shows (a case-study question: its
    wording and the section context recorded with it); ``reference_ids`` is exactly
    ``[question_content_ref(field)]``. It cites no candidate fact and makes no personal claim."""


def question_content_ref(field: ApplicationField) -> str:
    """The id of a question's own recorded content, ``form:<sha256>`` of its full wording
    and section context: an answer computed from that content references it, and any change
    to the recording makes it a different reference."""
    payload = json.dumps({"question": field.question_text, "section_context": list(field.section_context)},
                         sort_keys=True, ensure_ascii=False)
    return "form:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
        if self.source is not AnswerSource.PROFILE_IDENTITY and not self.reference_ids:
            raise ValueError(f"{self.source} provenance must reference its source ids")
        return self


class PacketAnswer(Contract):
    field_id: NonEmptyStr
    semantic_type: SemanticType
    """Must equal the inspected field's semantic type (checked by ``problems_against``)."""
    value: AnswerValue
    provenance: Provenance
    confidence: Confidence = 1.0

    @model_validator(mode="after")
    def _explicit_when_sensitive(self) -> Self:
        # First line of defence; problems_against repeats this against the actual field.
        if (
            self.semantic_type in EXPLICIT_ANSWER_REQUIRED
            and self.provenance.source not in EXPLICIT_SOURCES
        ):
            raise ValueError(
                f"{self.semantic_type} answers must come from a saved answer or user input, "
                f"not {self.provenance.source}"
            )
        return self


# --- missing input and user answers ------------------------------------------------


class MissingReason(StrEnum):
    NO_ANSWER = "NO_ANSWER"
    """No verified data answers this required question."""
    EXPLICIT_ANSWER_REQUIRED = "EXPLICIT_ANSWER_REQUIRED"
    """Consent, protected attribute, eligibility or salary with no applicable saved answer."""
    UNCOVERED_ATTESTATION = "UNCOVERED_ATTESTATION"
    """A personal attestation the user has not already made."""
    AMBIGUOUS = "AMBIGUOUS"
    """Saved data could map to more than one option, or conflicts."""
    UNSUPPORTED_CONTROL = "UNSUPPORTED_CONTROL"
    USER_ACTION = "USER_ACTION"
    """The page needs the user (sign-in, CAPTCHA, email verification)."""


class MissingInput(Contract):
    """A question the user must answer (or an action they must take) to continue.

    Field questions carry their form scope (``form_url``/``form_step``) and the
    question's ``field_fingerprint`` so the answer can be bound to exactly this
    question. Build them with ``MissingInput.for_field``.
    """

    id: NonEmptyStr = Field(default_factory=lambda: new_id("mi"))
    field_id: str | None
    """None only for ``USER_ACTION`` items that are not tied to a field."""
    form_url: str | None = None
    form_step: int | None = Field(default=None, ge=0)
    field_fingerprint: str | None = Field(default=None, pattern=_SHA256)
    label: str
    """The complete question wording shown to the user (``ApplicationField.question_text``:
    label, help text and placeholder, newline-separated). ``UserInput.answering`` copies it
    to ``UserInput.question``."""
    reason: MissingReason
    prompt: NonEmptyStr
    """What to ask the user, in plain language."""
    semantic_type: SemanticType = SemanticType.UNKNOWN
    control_type: ControlType | None = None
    options: list[FieldOption] | None = None
    """The field's options; for a lookup (``TYPEAHEAD``) item, the site's observed
    suggestions (value = label) that the user may pick and have typed verbatim."""
    required: bool = True
    candidates: list[AnswerValue] = Field(default_factory=list)
    """For AMBIGUOUS: the plausible answers the user can choose between."""

    @model_validator(mode="after")
    def _field_or_action(self) -> Self:
        if self.field_id is None:
            if self.reason is not MissingReason.USER_ACTION:
                raise ValueError("only USER_ACTION missing inputs may omit field_id")
        elif self.form_url is None or self.form_step is None or self.field_fingerprint is None:
            raise ValueError("field missing inputs need form_url, form_step and field_fingerprint")
        return self

    @classmethod
    def for_field(
        cls,
        form: ApplicationForm,
        field: ApplicationField,
        *,
        reason: MissingReason,
        prompt: str,
        candidates: Sequence[AnswerValue] = (),
    ) -> MissingInput:
        return cls(
            field_id=field.id,
            form_url=form.url,
            form_step=form.step,
            field_fingerprint=field.fingerprint,
            label=field.question_text,
            reason=reason,
            prompt=prompt,
            semantic_type=field.semantic_type,
            control_type=field.control_type,
            options=field.options,
            required=field.required,
            candidates=list(candidates),
        )

    @property
    def scope(self) -> FormScope | None:
        if self.form_url is None or self.form_step is None:
            return None
        return FormScope.of(self.form_url, self.form_step)

    def matches(self, form: ApplicationForm) -> bool:
        """True if this item is the same question on ``form`` (scope + fingerprint)."""
        if self.field_id is None:
            return True
        field = form.find(self.field_id)
        return (
            self.scope == form.scope
            and field is not None
            and field.fingerprint == self.field_fingerprint
        )


class AnswerReuse(StrEnum):
    """How far the user allowed an answer to be reused. Default: this application."""

    APPLICATION = "APPLICATION"
    JOB = "JOB"
    GLOBAL = "GLOBAL"


class UserInput(Contract):
    """The user's answer to one question on one form step, stored for resume.

    Identity within its application is (form step, ``field_id``,
    ``field_fingerprint``): an input applies only to a form on the same step whose
    field with that id asks the same question (``matches``). ``form_url`` is recorded
    but not compared, because step URLs can carry per-session draft ids. Build with ``UserInput.answering(missing, ...)``
    or ``UserInput.for_field(form, field_id, ...)``.
    """

    id: NonEmptyStr = Field(default_factory=lambda: new_id("ui"))
    form_url: NonEmptyStr
    form_step: int = Field(ge=0)
    field_id: NonEmptyStr
    field_fingerprint: str = Field(pattern=_SHA256)
    question: str
    """The complete question wording the user answered (``render_question`` of label,
    help text and placeholder). ``to_saved_answer`` copies it to ``SavedAnswer.question``."""
    semantic_type: SemanticType = SemanticType.UNKNOWN
    value: AnswerValue
    reuse: AnswerReuse = AnswerReuse.APPLICATION
    """APPLICATION keeps the answer local; JOB/GLOBAL ask the candidate package to save
    it as a ``SavedAnswer`` with that scope (``to_saved_answer``)."""
    provided_at: UtcDatetime = Field(default_factory=utc_now)

    @property
    def scope(self) -> FormScope:
        return FormScope.of(self.form_url, self.form_step)

    @property
    def question_key(self) -> tuple[int, str, str]:
        """``(form_step, field_id, field_fingerprint)``: the question this answers,
        within its application."""
        return (self.form_step, self.field_id, self.field_fingerprint)

    def matches(self, form: ApplicationForm) -> bool:
        """True if ``form`` asks this exact question: the same step index, a field with
        this id, and the same question fingerprint (wording and options).

        The step's URL is recorded (``form_url``) but not compared: multistep and
        session-based forms put draft or session ids in step URLs, so the same
        question on the same step has a new URL after a restart. User inputs belong
        to one application, so they never apply to another application's form."""
        field = form.find(self.field_id)
        return (
            self.form_step == form.step
            and field is not None
            and field.fingerprint == self.field_fingerprint
        )

    @classmethod
    def answering(
        cls,
        missing: MissingInput,
        value: AnswerValue,
        *,
        reuse: AnswerReuse = AnswerReuse.APPLICATION,
    ) -> UserInput:
        """Answer a ``MissingInput``; the value is checked against its options.

        A lookup (``TYPEAHEAD``) item may list the site's suggestions as ``options``
        for the user to pick from; they are not field options, so the answer is a
        ``TextValue`` checked like any text (the chosen label is typed verbatim)."""
        if (
            missing.field_id is None
            or missing.form_url is None
            or missing.form_step is None
            or missing.field_fingerprint is None
        ):
            raise ValueError("USER_ACTION items are not answered with a UserInput")
        if missing.control_type is not None:
            lookup = missing.control_type is ControlType.TYPEAHEAD
            probe = ApplicationField(
                id=missing.field_id,
                label=missing.label,
                control_type=missing.control_type,
                selector="-",
                required=missing.required,
                options=None if lookup else missing.options,
            )
            problems = answer_problems(probe, value)
            if problems:
                raise ValueError("; ".join(problems))
        return cls(
            form_url=missing.form_url,
            form_step=missing.form_step,
            field_id=missing.field_id,
            field_fingerprint=missing.field_fingerprint,
            question=missing.label,
            semantic_type=missing.semantic_type,
            value=value,
            reuse=reuse,
        )

    @classmethod
    def for_field(
        cls,
        form: ApplicationForm,
        field_id: str,
        value: AnswerValue,
        *,
        reuse: AnswerReuse = AnswerReuse.APPLICATION,
    ) -> UserInput:
        field = form.field(field_id)
        problems = answer_problems(field, value)
        if problems:
            raise ValueError("; ".join(problems))
        return cls(
            form_url=form.url,
            form_step=form.step,
            field_id=field.id,
            field_fingerprint=field.fingerprint,
            question=field.question_text,
            semantic_type=field.semantic_type,
            value=value,
            reuse=reuse,
        )

    def to_saved_answer(
        self, *, job: JobRecord, answer_id: str | None = None
    ) -> SavedAnswer | None:
        """The ``SavedAnswer`` the user asked for, or None for APPLICATION reuse.
        JOB answers are bound to ``job`` (identity key when known, else its URL)."""
        if self.reuse is AnswerReuse.APPLICATION:
            return None
        value: SavedAnswerValue
        if isinstance(self.value, TextValue):
            value = self.value.text
        elif isinstance(self.value, ChoiceValue):
            value = self.value.label
        elif isinstance(self.value, MultiChoiceValue):
            value = [c.label for c in self.value.choices]
        elif isinstance(self.value, BooleanValue):
            value = self.value.checked
        else:
            raise ValueError("file answers are not saved for reuse")
        job_scoped = self.reuse is AnswerReuse.JOB
        return SavedAnswer(
            id=answer_id or new_id("sa"),
            scope=AnswerScope.JOB if job_scoped else AnswerScope.GLOBAL,
            job_identity_key=job.identity_key if job_scoped else None,
            job_url=job.normalized_url if job_scoped and not job.identity_key else None,
            employer=job.company if job_scoped else None,
            semantic_type=None if self.semantic_type is SemanticType.UNKNOWN else self.semantic_type,
            question=self.question or self.field_id,
            value=value,
            confirmed_at=self.provided_at,
        )


# --- packet ----------------------------------------------------------------------


class ApplicationPacket(Contract):
    id: NonEmptyStr = Field(default_factory=lambda: new_id("pkt"))
    application_id: NonEmptyStr
    job_id: NonEmptyStr
    candidate_id: NonEmptyStr
    form_url: NonEmptyStr
    form_step: int = Field(ge=0)
    form_fingerprint: str = Field(pattern=_SHA256)
    """``ApplicationForm.fingerprint`` of the inspection this packet answers."""
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
    def scope(self) -> FormScope:
        return FormScope.of(self.form_url, self.form_step)

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
        """Problems applying this packet to ``form`` as currently inspected. Empty
        means the packet belongs to this exact form step and every answer is
        compatible with, and permitted for, the actual field it targets."""
        if self.scope != form.scope:
            return [f"packet is for {self.scope.key}, not the current form {form.scope.key}"]
        problems: list[str] = []
        if self.form_fingerprint != form.fingerprint:
            problems.append("packet was resolved against a different inspection of this form step")
        for answer in self.answers:
            field = form.find(answer.field_id)
            if field is None:
                problems.append(f"answer for unknown field {answer.field_id!r}")
                continue
            if answer.semantic_type is not field.semantic_type:
                problems.append(
                    f"answer for {field.id!r} claims {answer.semantic_type}, "
                    f"field is {field.semantic_type}"
                )
            if (
                field.semantic_type in EXPLICIT_ANSWER_REQUIRED
                and answer.provenance.source not in EXPLICIT_SOURCES
            ):
                problems.append(
                    f"{field.id!r} is {field.semantic_type}; it needs a saved answer or user "
                    f"input, not {answer.provenance.source}"
                )
            if (
                answer.provenance.source is AnswerSource.PROFILE_IDENTITY
                and field.semantic_type not in PROFILE_IDENTITY_TYPES
                and not (field.semantic_type in ADDRESS_DERIVED_TYPES
                         and isinstance(answer.value, ChoiceValue))
                and not _profile_link(field, answer)
            ):
                problems.append(f"{field.id!r} ({field.semantic_type}) is not an identity field")
            problems.extend(answer_problems(field, answer.value))
        for missing in self.missing_inputs:
            if missing.field_id is not None and not missing.matches(form):
                problems.append(f"missing input {missing.field_id!r} is not a question on this form")
        accounted = {a.field_id for a in self.answers} | set(self.unresolved_fields)
        for field in form.required_fields():
            if field.id not in accounted:
                problems.append(f"required field {field.id!r} is neither answered nor missing")
        return problems


def provenance_problems(
    packet: ApplicationPacket,
    *,
    form: ApplicationForm,
    candidate: CandidateProfile,
    job: JobRecord,
    user_inputs: Sequence[UserInput] = (),
) -> list[str]:
    """Problems with where the packet's answers came from: unknown or unverified
    facts, out-of-scope or mistyped saved answers, user inputs for another question,
    or a resume that is not the supplied one."""
    problems: list[str] = []
    if packet.candidate_id != candidate.id:
        problems.append("packet is for a different candidate")
    if packet.job_id != job.id:
        problems.append("packet is for a different job")
    inputs = {u.id: u for u in user_inputs}
    for answer in packet.answers:
        src, refs, fid = answer.provenance.source, answer.provenance.reference_ids, answer.field_id
        if src in (AnswerSource.CANDIDATE_FACT, AnswerSource.GENERATED_FROM_FACTS):
            for ref in refs:
                fact = candidate.find_fact(ref)
                if fact is None:
                    problems.append(f"{fid!r} cites unknown fact {ref!r}")
                elif not fact.is_verified:
                    problems.append(f"{fid!r} cites unverified fact {ref!r}")
        elif src is AnswerSource.SAVED_ANSWER:
            for ref in refs:
                saved = candidate.find_saved_answer(ref)
                if saved is None:
                    problems.append(f"{fid!r} cites unknown saved answer {ref!r}")
                    continue
                if not saved.applies_to(job):
                    problems.append(f"{fid!r} cites saved answer {ref!r} scoped to another job")
                field = form.find(fid)
                # The pay period of a salary is the one answer a salary may give a field of
                # another type: a single choice whose options are all pay periods.
                period = (saved.semantic_type is SemanticType.SALARY_EXPECTATION
                          and field is not None and is_pay_period_choice(field))
                if (saved.semantic_type is not None and saved.semantic_type is not answer.semantic_type
                        and not period):
                    problems.append(
                        f"{fid!r} ({answer.semantic_type}) cites saved answer {ref!r} "
                        f"about {saved.semantic_type}"
                    )
        elif src is AnswerSource.GENERATED_FROM_QUESTION:
            field = form.find(fid)
            if field is None or refs != [question_content_ref(field)]:
                problems.append(f"{fid!r} must reference the question's own recorded content")
        elif src is AnswerSource.RESUME:
            if refs != [candidate.resume.id]:
                problems.append(f"{fid!r} must reference the supplied resume {candidate.resume.id!r}")
            if not (
                isinstance(answer.value, FileValue)
                and answer.value.artifact.sha256 == candidate.resume.sha256
            ):
                problems.append(f"{fid!r} does not upload the supplied resume file")
        elif src is AnswerSource.PROFILE_IDENTITY:
            field = form.find(fid)
            if field is not None and _profile_link(field, answer):
                identity = candidate.identity
                urls = {identity.linkedin_url, identity.website_url, identity.github_url} - {None}
                if not isinstance(answer.value, TextValue) or answer.value.text not in urls:
                    problems.append(f"{fid!r} copies a link that is not the applicant's own profile URL")
        elif src is AnswerSource.USER_INPUT:
            for ref in refs:
                user = inputs.get(ref)
                if user is None:
                    problems.append(f"{fid!r} cites unknown user input {ref!r}")
                elif user.field_id != fid or not user.matches(form):
                    problems.append(f"{fid!r} cites user input {ref!r} for a different question")
                elif user.value != answer.value:
                    problems.append(f"{fid!r} does not use the value the user gave")
    return problems


PROFILE_LINK_TYPES = frozenset({SemanticType.UNKNOWN, SemanticType.CUSTOM_TEXT,
                                SemanticType.CUSTOM_LONG_TEXT})
"""Untyped text questions that may take the applicant's own profile URL from the verified
identity (round 11: "Professional profile link (LinkedIn, portfolio, or personal site)"):
the value must be one of the identity's own URLs (``provenance_problems``)."""


def _profile_link(field: ApplicationField, answer: PacketAnswer) -> bool:
    """A profile URL copied onto an untyped text question (see ``PROFILE_LINK_TYPES``)."""
    return (field.semantic_type in PROFILE_LINK_TYPES
            and field.control_type in (ControlType.TEXT, ControlType.TEXTAREA)
            and isinstance(answer.value, TextValue)
            and answer.value.text.startswith(("https://", "http://")))


def is_multi_choice(field: ApplicationField) -> bool:
    return field.control_type in MULTI_CHOICE_CONTROLS
