"""The deterministic packet resolver (``PacketResolver``).

Each field of the inspected form step is resolved from, in order:

1. the user's answer to this exact question (``PacketContext.user_inputs``);
2. a saved answer that applies to the job and was given for this exact question
   wording (job-scoped answers take precedence over global ones);
3. for every type except ``EXPLICIT_ANSWER_REQUIRED``: the supplied resume, the
   verified identity, or verified candidate facts;
4. for free-text questions that are pure fact lookups ("What is your current job
   title?"): text assembled from verified facts.

Anything else is unknown. Unknown optional fields stay blank. Unknown required
fields become a ``MissingInput`` scoped to this form step and question, with a
stable id. Work authorization, sponsorship, salary, consent, attestations and
protected attributes are never inferred.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from interviewmaxxing_core import (
    EXPLICIT_ANSWER_REQUIRED,
    PROTECTED_ATTRIBUTE_TYPES,
    AnswerScope,
    AnswerSource,
    AnswerValue,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ArtifactRef,
    CandidateFact,
    CandidateIdentity,
    ControlType,
    FileValue,
    MissingInput,
    MissingReason,
    PacketAnswer,
    PacketContext,
    Provenance,
    SavedAnswer,
    SemanticType,
    UserInput,
    answer_problems,
    new_id,
    utc_now,
)

from .questions import (
    QuestionText,
    factual_template,
    parse_years_question,
    saved_answer_matches,
    years_fact_area,
)
from .values import Mapped, RawValue, Unmapped, as_number, render_scalar, translate


class PacketResolutionError(RuntimeError):
    """The resolver produced a packet its context rejects. This is a resolver bug;
    the packet is never returned."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("resolved packet is invalid: " + "; ".join(self.problems))


# --- per-field outcomes --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Answer:
    value: AnswerValue
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class _Unresolved:
    """Nothing usable answers the field. ``reason`` overrides the type's default."""

    detail: str = ""
    reason: MissingReason | None = None
    candidates: tuple[AnswerValue, ...] = field(default=())


_Outcome = _Answer | _Unresolved

_IDENTITY_ATTRIBUTES: dict[SemanticType, Callable[[CandidateIdentity], str | None]] = {
    SemanticType.FIRST_NAME: lambda i: i.first_name,
    SemanticType.LAST_NAME: lambda i: i.last_name,
    SemanticType.FULL_NAME: lambda i: i.full_name,
    SemanticType.PREFERRED_NAME: lambda i: i.preferred_name,
    SemanticType.EMAIL: lambda i: i.email,
    SemanticType.PHONE: lambda i: i.phone,
    SemanticType.ADDRESS: lambda i: i.address.street,
    SemanticType.CITY: lambda i: i.address.city,
    SemanticType.STATE: lambda i: i.address.region,
    SemanticType.ZIP: lambda i: i.address.postal_code,
    SemanticType.COUNTRY: lambda i: i.address.country,
    SemanticType.LOCATION: lambda i: ", ".join(
        p for p in (i.address.city, i.address.region, i.address.country) if p
    ),
    SemanticType.LINKEDIN: lambda i: i.linkedin_url,
    SemanticType.WEBSITE: lambda i: i.website_url,
    SemanticType.GITHUB: lambda i: i.github_url,
}

_FACT_KEYS: dict[SemanticType, tuple[str, ...]] = {
    SemanticType.CURRENT_COMPANY: ("current_company",),
    SemanticType.CURRENT_TITLE: ("current_title",),
    SemanticType.EDUCATION_LEVEL: ("education_level", "highest_education_level"),
    SemanticType.UNIVERSITY: ("university",),
    SemanticType.DEGREE: ("degree",),
}
"""Semantic types answered verbatim from verified facts with these keys."""

_TEMPLATE_TYPES = frozenset(
    {SemanticType.CUSTOM_TEXT, SemanticType.CUSTOM_LONG_TEXT, SemanticType.CUSTOM_SELECT}
)
"""Custom question types that may be answered when they are pure fact lookups."""


def _default_reason(fld: ApplicationField) -> MissingReason:
    if fld.control_type is ControlType.UNSUPPORTED:
        return MissingReason.UNSUPPORTED_CONTROL
    if fld.semantic_type is SemanticType.ATTESTATION:
        return MissingReason.UNCOVERED_ATTESTATION
    if fld.semantic_type in EXPLICIT_ANSWER_REQUIRED:
        return MissingReason.EXPLICIT_ANSWER_REQUIRED
    return MissingReason.NO_ANSWER


# --- the resolver --------------------------------------------------------------------


class _FieldResolver:
    def __init__(self, context: PacketContext) -> None:
        self.context = context
        self.candidate = context.candidate
        self.facts = context.candidate.verified_facts()
        latest: dict[str, UserInput] = {}
        for item in sorted(context.user_inputs, key=lambda u: u.provided_at):
            latest[item.field_id] = item
        self.user_inputs = latest
        self.saved_answers = context.candidate.applicable_saved_answers(context.job)

    def resolve(self, fld: ApplicationField) -> _Outcome:
        user_input = self.user_inputs.get(fld.id)
        if user_input is not None:
            return self._from_user_input(fld, user_input)
        if fld.control_type is ControlType.UNSUPPORTED:
            return _Unresolved()
        question = QuestionText.of(fld)
        saved = self._from_saved_answers(fld, question)
        if saved is not None:
            return saved
        if fld.semantic_type in EXPLICIT_ANSWER_REQUIRED:
            return _Unresolved()
        if fld.semantic_type is SemanticType.RESUME:
            return self._resume(fld)
        if fld.semantic_type in _IDENTITY_ATTRIBUTES:
            return self._identity(fld)
        if fld.semantic_type in _FACT_KEYS:
            return self._lookup(fld, _FACT_KEYS[fld.semantic_type])
        if fld.semantic_type is SemanticType.YEARS_EXPERIENCE:
            return self._years(fld, question)
        if fld.semantic_type in _TEMPLATE_TYPES:
            return self._template(fld, question)
        return _Unresolved()

    # -- sources --

    def _from_user_input(self, fld: ApplicationField, user_input: UserInput) -> _Outcome:
        problems = answer_problems(fld, user_input.value)
        if problems:
            return _Unresolved("Your earlier answer cannot be used: " + "; ".join(problems) + ".")
        return _Answer(
            user_input.value,
            Provenance(source=AnswerSource.USER_INPUT, reference_ids=[user_input.id]),
        )

    def _from_saved_answers(self, fld: ApplicationField, question: QuestionText) -> _Outcome | None:
        if fld.control_type is ControlType.FILE:
            return None
        matching = [
            a
            for a in self.saved_answers
            if (a.semantic_type is None or a.semantic_type is fld.semantic_type)
            and saved_answer_matches(a, question)
        ]
        if not matching:
            return None
        job_scoped = [a for a in matching if a.scope is AnswerScope.JOB]
        tier = job_scoped or matching
        translated = [(a, translate(fld, a.value)) for a in tier]
        values: list[AnswerValue] = []
        for _, result in translated:
            if isinstance(result, Mapped) and result.value not in values:
                values.append(result.value)
        failures = [(a, r) for a, r in translated if isinstance(r, Unmapped)]
        if len(values) == 1 and not failures:
            return _Answer(
                values[0],
                Provenance(
                    source=AnswerSource.SAVED_ANSWER,
                    reference_ids=[a.id for a in tier],
                    note=f"saved answer for {tier[0].question!r}",
                ),
            )
        conflicting = len(values) > 1 or (values and failures)
        if conflicting:
            detail = "Your saved answers to this question disagree."
        else:
            answer, failure = failures[0]
            detail = f"Your saved answer {_describe(answer)} cannot be used here: {failure.reason}."
        for _, failure in failures:
            values.extend(c for c in failure.candidates if c not in values)
        # One saved answer that does not fit (e.g. a declined required consent) is not
        # ambiguous; the field keeps its own reason and the detail explains why.
        reason = MissingReason.AMBIGUOUS if conflicting or len(values) > 1 else None
        return _Unresolved(detail, reason, tuple(values))

    def _resume(self, fld: ApplicationField) -> _Outcome:
        if fld.control_type is not ControlType.FILE:
            return _Unresolved("The resume can only be uploaded, not typed into this field.")
        resume = self.candidate.resume
        if not resume.verify():
            return _Unresolved(
                "The supplied resume file is missing or has changed since your profile was loaded."
            )
        artifact = ArtifactRef.model_validate(
            resume.model_dump(include=set(ArtifactRef.model_fields))
        )
        value = FileValue(artifact=artifact)
        problems = answer_problems(fld, value)
        if problems:
            return _Unresolved("The supplied resume cannot be used: " + "; ".join(problems) + ".")
        return _Answer(
            value,
            Provenance(
                source=AnswerSource.RESUME,
                reference_ids=[resume.id],
                note=f"supplied resume ({resume.variant})",
            ),
        )

    def _identity(self, fld: ApplicationField) -> _Outcome:
        attribute = _IDENTITY_ATTRIBUTES[fld.semantic_type]
        known = attribute(self.candidate.identity)
        if not known or not known.strip():
            return _Unresolved()
        result = translate(fld, known)
        if isinstance(result, Unmapped):
            return _Unresolved(f"Your verified profile value cannot be used: {result.reason}.",
                               MissingReason.AMBIGUOUS if result.candidates else None,
                               result.candidates)
        return _Answer(
            result.value,
            Provenance(
                source=AnswerSource.PROFILE_IDENTITY,
                note=f"verified identity: {fld.semantic_type.value.lower()}",
            ),
        )

    def _fact_value(self, keys: Sequence[str]) -> tuple[RawValue, list[CandidateFact]] | None:
        """The single value verified facts with any of ``keys`` agree on, or None
        (no such fact, or facts that disagree)."""
        facts = [f for f in self.facts if f.key in keys and _has_value(f.value)]
        if not facts or len({_fact_identity(f.value) for f in facts}) > 1:
            return None
        return _raw(facts[0].value), facts

    def _from_facts(
        self,
        fld: ApplicationField,
        raw: RawValue,
        facts: Sequence[CandidateFact],
        *,
        source: AnswerSource = AnswerSource.CANDIDATE_FACT,
        numeric_ranges: bool = False,
    ) -> _Outcome:
        result = translate(fld, raw, numeric_ranges=numeric_ranges)
        if isinstance(result, Unmapped):
            return _Unresolved(
                f"Your verified facts cannot be used: {result.reason}.",
                MissingReason.AMBIGUOUS if result.candidates else None,
                result.candidates,
            )
        keys = ", ".join(dict.fromkeys(f.key for f in facts))
        return _Answer(
            result.value,
            Provenance(source=source, reference_ids=[f.id for f in facts],
                       note=f"verified facts: {keys}"),
        )

    def _lookup(self, fld: ApplicationField, keys: Sequence[str]) -> _Outcome:
        found = self._fact_value(keys)
        if found is None:
            return _Unresolved()
        return self._from_facts(fld, *found)

    def _years(self, fld: ApplicationField, question: QuestionText) -> _Outcome:
        asked = parse_years_question(question)
        if asked is None:
            return _Unresolved()
        keys = [f.key for f in self.facts if years_fact_area(f.key) == (asked.area or "")]
        found = self._fact_value(keys)
        if found is None or as_number(found[0]) is None:
            return _Unresolved()
        return self._from_facts(fld, *found, numeric_ranges=True)

    def _template(self, fld: ApplicationField, question: QuestionText) -> _Outcome:
        if parse_years_question(question) is not None:
            return self._years(fld, question)
        template = factual_template(question)
        if template is None:
            return _Unresolved()
        values: list[str] = []
        facts: list[CandidateFact] = []
        for key in template.fact_keys:
            found = self._fact_value([key])
            if found is None or isinstance(found[0], list):
                return _Unresolved()
            values.append(render_scalar(found[0]))
            facts.extend(found[1])
        if len(template.fact_keys) == 1:
            return self._from_facts(fld, values[0], facts)
        return self._from_facts(fld, template.template.format(*values), facts,
                                source=AnswerSource.GENERATED_FROM_FACTS)


def _has_value(value: object) -> bool:
    """Facts with a value of 0 or False still carry information; None and blank don't."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return True


def _fact_identity(value: object) -> object:
    return tuple(value) if isinstance(value, list) else (type(value).__name__, value)


def _raw(value: object) -> RawValue:
    if isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [str(v) for v in value]
    raise TypeError(f"unexpected fact value {value!r}")


def _describe(answer: SavedAnswer) -> str:
    shown = ", ".join(answer.value) if isinstance(answer.value, list) else render_scalar(answer.value)
    return repr(shown)


# --- prompts -------------------------------------------------------------------------

_EXPLICIT_KINDS: dict[SemanticType, str] = {
    SemanticType.WORK_AUTHORIZATION: "your work authorization",
    SemanticType.SPONSORSHIP: "whether you need visa sponsorship",
    SemanticType.SALARY_EXPECTATION: "your salary expectations",
    SemanticType.CONSENT: "your consent",
    SemanticType.ATTESTATION: "a personal attestation",
}

_REASON_TEXT: dict[MissingReason, str] = {
    MissingReason.NO_ANSWER: "No verified fact or saved answer answers this question.",
    MissingReason.UNCOVERED_ATTESTATION: (
        "This is a personal attestation you have not already made; "
        "confirm it only if it is true for you."
    ),
    MissingReason.AMBIGUOUS: "Your saved information does not map to exactly one answer.",
    MissingReason.UNSUPPORTED_CONTROL: "This control cannot be filled automatically.",
}


def _explicit_text(fld: ApplicationField) -> str:
    if fld.semantic_type in PROTECTED_ATTRIBUTE_TYPES:
        kind = "voluntary self-identification"
    else:
        kind = _EXPLICIT_KINDS.get(fld.semantic_type, "an explicit answer")
    return f"This asks for {kind}, which is only ever answered by you, never inferred."


def _instruction(fld: ApplicationField) -> str:
    control = fld.control_type
    if control in (ControlType.SELECT, ControlType.RADIO):
        labels = [o.label for o in fld.options or [] if not o.disabled and o.value.strip()]
        return "Choose one: " + "; ".join(labels) + "."
    if control in (ControlType.MULTISELECT, ControlType.CHECKBOX_GROUP):
        labels = [o.label for o in fld.options or [] if not o.disabled and o.value.strip()]
        return "Choose all that apply: " + "; ".join(labels) + "."
    if control is ControlType.CHECKBOX:
        return "Check the box only if the statement is true for you."
    if control is ControlType.FILE:
        accepted = f" ({', '.join(fld.accept)})" if fld.accept else ""
        return f"Provide a file{accepted}."
    if control is ControlType.UNSUPPORTED:
        return "Complete this field yourself in the browser."
    limit = f" (up to {fld.max_length} characters)" if fld.max_length else ""
    return f"Enter your answer{limit}."


def _prompt(fld: ApplicationField, reason: MissingReason, detail: str) -> str:
    question = fld.label.strip() or fld.id
    parts = [f'Required: "{question}".']
    if fld.help_text and fld.help_text.strip():
        parts.append(f"Details: {fld.help_text.strip()}")
    if reason is MissingReason.EXPLICIT_ANSWER_REQUIRED:
        parts.append(_explicit_text(fld))
    elif not (detail and reason is MissingReason.NO_ANSWER):
        parts.append(_REASON_TEXT.get(reason, ""))
    if detail:
        parts.append(detail)
    parts.append(_instruction(fld))
    return " ".join(p for p in parts if p)


def missing_input_id(form: ApplicationForm, fld: ApplicationField) -> str:
    """Stable id of the missing-input item for this question: the same form scope,
    field id and question fingerprint give the same id on every re-inspection."""
    raw = f"{form.scope.key}\n{fld.id}\n{fld.fingerprint}"
    return "mi_" + hashlib.sha256(raw.encode()).hexdigest()[:32]


# --- public API ----------------------------------------------------------------------


def resolve_packet(
    context: PacketContext,
    *,
    packet_id: str | None = None,
    created_at: datetime | None = None,
) -> ApplicationPacket:
    """Resolve ``context.form`` into a packet (synchronous core of the resolver).

    Raises ``PacketResolutionError`` if the result would fail ``context.problems``."""
    form = context.form
    fields = _FieldResolver(context)
    answers: list[PacketAnswer] = []
    missing: list[MissingInput] = []
    for fld in form.fields:
        outcome = fields.resolve(fld)
        if isinstance(outcome, _Answer):
            answers.append(
                PacketAnswer(
                    field_id=fld.id,
                    semantic_type=fld.semantic_type,
                    value=outcome.value,
                    provenance=outcome.provenance,
                    confidence=1.0,
                )
            )
            continue
        if not fld.required:
            continue
        reason = outcome.reason or _default_reason(fld)
        item = MissingInput.for_field(
            form,
            fld,
            reason=reason,
            prompt=_prompt(fld, reason, outcome.detail),
            candidates=[c for c in outcome.candidates if not answer_problems(fld, c)],
        )
        missing.append(item.model_copy(update={"id": missing_input_id(form, fld)}))
    packet = ApplicationPacket(
        id=packet_id or new_id("pkt"),
        application_id=context.application.id,
        job_id=context.job.id,
        candidate_id=context.candidate.id,
        form_url=form.url,
        form_step=form.step,
        form_fingerprint=form.fingerprint,
        resume_variant=context.candidate.resume.variant,
        answers=answers,
        missing_inputs=missing,
        cover_letter=None,
        created_at=created_at or utc_now(),
    )
    problems = context.problems(packet)
    if problems:
        raise PacketResolutionError(problems)
    return packet


class FactualPacketResolver:
    """``PacketResolver`` that answers only from verified data and explicit answers.

    ``FactualPacketResolver()`` needs no services or network access. ``clock``
    supplies ``ApplicationPacket.created_at`` (UTC-aware)."""

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock

    async def resolve(self, context: PacketContext) -> ApplicationPacket:
        return resolve_packet(context, created_at=self._clock())
