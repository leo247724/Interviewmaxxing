"""Frontend answers -> canonical ``UserInput``s for the latest missing inputs.

Every answer must address a question id among the latest packet's answerable missing
inputs (``views.question_id``). Values are translated with the question's own
options, then built with ``UserInput.answering`` so the exact wording, form scope and
fingerprint are preserved and the value is re-checked against the options. Nothing is
saved unless every submitted answer is valid.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from interviewmaxxing_core import (
    AnswerReuse,
    ControlType,
    MissingInput,
    UserInput,
)
from interviewmaxxing_core.packets import (
    AnswerValue,
    BooleanValue,
    ChoiceValue,
    MultiChoiceValue,
    TextValue,
)

from .models import AnswerInput, ReuseChoice
from .views import is_answerable, is_attestation, question_id, saved_input_for

_REUSE = {"application": AnswerReuse.APPLICATION, "job": AnswerReuse.JOB, "global": AnswerReuse.GLOBAL}


@dataclass(frozen=True)
class AnswerPlan:
    inputs: list[UserInput]
    errors: dict[str, str]
    """Per question id: why the answer was not accepted (invalid choice, wrong kind)."""
    stale: list[str]
    """Question ids that are not current questions of this application."""


def current_questions(awaited: Sequence[MissingInput]) -> dict[str, MissingInput]:
    return {question_id(m): m for m in awaited if is_answerable(m)}


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def _convert(missing: MissingInput, value: object) -> AnswerValue:
    control = missing.control_type
    options = {o.value: o for o in missing.options or []}
    if control in (ControlType.TEXT, ControlType.TEXTAREA):
        if not isinstance(value, str):
            raise ValueError("Enter text for this question.")
        return TextValue(text=value.strip())
    if control in (ControlType.SELECT, ControlType.RADIO):
        if not isinstance(value, str):
            raise ValueError("Choose one of the listed options.")
        option = options.get(value)
        if option is None or option.disabled or not option.value.strip():
            raise ValueError("Choose one of the listed options.")
        return ChoiceValue(value=option.value, label=option.label)
    if control is ControlType.TYPEAHEAD:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Choose one of the suggestions or enter the place to look up.")
        option = options.get(value)
        if option is not None and not option.disabled and option.value.strip():
            return TextValue(text=option.label)
        return TextValue(text=value.strip())
    if control in (ControlType.MULTISELECT, ControlType.CHECKBOX_GROUP):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("Choose from the listed options.")
        chosen = []
        for item in value:
            option = options.get(item)
            if option is None or option.disabled or not option.value.strip():
                raise ValueError("Choose only from the listed options.")
            chosen.append(option)
        if len({o.value for o in chosen}) != len(chosen):
            raise ValueError("Each option can be chosen once.")
        return MultiChoiceValue(choices=chosen)
    if control is ControlType.CHECKBOX:
        if not isinstance(value, bool):
            raise ValueError("Answer yes or no.")
        return BooleanValue(checked=value)
    raise ValueError("This question can't be answered here.")


SCOPE_NOT_OFFERED = "Choose one of the scopes this answer offers; it can't be kept that widely."


def plan_answers(
    awaited: Sequence[MissingInput],
    body: AnswerInput,
    *,
    allowed_reuse: Mapping[str, Collection[str]] | None = None,
) -> AnswerPlan:
    """``allowed_reuse``: per question id, the reuse scopes its answer may be saved with
    (the review lane's edits say why they are fewer); any scope for other questions."""
    questions = current_questions(awaited)
    allowed_reuse = allowed_reuse or {}
    inputs: list[UserInput] = []
    errors: dict[str, str] = {}
    stale: list[str] = []
    submitted: list[tuple[str, object, bool]] = []
    submitted += [(qid, value, False) for qid, value in body.answers.items()]
    submitted += [(qid, accepted, True) for qid, accepted in body.attestations.items()]
    for qid, value, as_attestation in submitted:
        missing = questions.get(qid)
        if missing is None or is_attestation(missing) != as_attestation:
            stale.append(qid)
            continue
        if _is_blank(value):
            continue  # saving a draft may leave questions blank
        reuse: ReuseChoice = body.reuse.get(qid, "application")
        if qid in allowed_reuse and reuse not in allowed_reuse[qid]:
            errors[qid] = SCOPE_NOT_OFFERED
            continue
        try:
            converted = _convert(missing, value)
            if as_attestation and missing.required and converted == BooleanValue(checked=False):
                raise ValueError("Accept this statement to continue, or stop the application.")
            inputs.append(UserInput.answering(missing, converted, reuse=_REUSE[reuse]))
        except ValueError as exc:
            errors[qid] = _plain(exc)
    stale.extend(q for q in body.reuse if q not in questions and q not in stale)
    return AnswerPlan(inputs=inputs, errors=errors, stale=stale)


def _plain(exc: ValueError) -> str:
    message = str(exc)
    # Pydantic/contract messages are technical; the conversions above are already plain.
    if "\n" in message or "validation error" in message:
        return "This answer doesn't fit the question."
    return message


def unanswered_required(
    awaited: Sequence[MissingInput], inputs: Sequence[UserInput]
) -> dict[str, str]:
    """Required answerable questions without a stored answer, keyed by question id."""
    out: dict[str, str] = {}
    for qid, missing in current_questions(awaited).items():
        if not missing.required:
            continue
        saved = saved_input_for(missing, inputs)
        if saved is None:
            out[qid] = (
                "Accept this statement to continue."
                if is_attestation(missing)
                else "Answer this question to continue."
            )
    return out

