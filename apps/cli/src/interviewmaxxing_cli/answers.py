"""Turn what a user typed into a validated ``UserInput`` for a recorded question.

Used by ``interviewmaxxing answer`` and by the interactive terminal prompts. The
question comes from the recorded ``MissingInput`` (exact wording, options and
fingerprint); a value that does not fit it is rejected before anything is saved.
"""

from __future__ import annotations

from collections.abc import Sequence

from interviewmaxxing_core import (
    AnswerReuse,
    AnswerValue,
    BooleanValue,
    ChoiceValue,
    ControlType,
    FieldOption,
    MissingInput,
    MultiChoiceValue,
    TextValue,
    UserInput,
    normalize_text,
)

TRUE_WORDS = frozenset({"yes", "y", "true", "1", "checked", "on", "agree", "i agree"})
FALSE_WORDS = frozenset({"no", "n", "false", "0", "unchecked", "off"})
MULTI_SEPARATOR = ";"


class AnswerError(ValueError):
    """The typed value does not answer the question."""


def _option(item: MissingInput, raw: str) -> FieldOption:
    wanted = raw.strip()
    options = [o for o in item.options or [] if o.value.strip() and not o.disabled]
    for option in options:
        if option.value == wanted:
            return option
    matches = [o for o in options if normalize_text(o.label) == normalize_text(wanted)]
    if len(matches) == 1:
        return matches[0]
    shown = ", ".join(f"{o.label!r} ({o.value})" for o in options)
    raise AnswerError(f"{raw!r} is not one of the options for {item.field_id!r}: {shown}")


def value_for(item: MissingInput, raw: str | bool | Sequence[str]) -> AnswerValue:
    """The answer value for ``item`` from a typed string (or a bool/list from JSON).
    Choices match an option's value exactly or its label ignoring case and spacing;
    several choices are separated by ``;``."""
    control = item.control_type
    if control in (ControlType.TEXT, ControlType.TEXTAREA, None):
        if not isinstance(raw, str):
            raise AnswerError(f"{item.field_id!r} needs text")
        return TextValue(text=raw)
    if control in (ControlType.SELECT, ControlType.RADIO):
        if not isinstance(raw, str):
            raise AnswerError(f"{item.field_id!r} needs one option")
        option = _option(item, raw)
        return ChoiceValue(value=option.value, label=option.label)
    if control is ControlType.TYPEAHEAD:
        # A lookup: the site's own suggestions are offered as options; one of them, or
        # free text the site will search for, is typed verbatim by the browser.
        if not isinstance(raw, str) or not raw.strip():
            raise AnswerError(f"{item.field_id!r} needs the place or entity to look up")
        if item.options:
            try:
                return TextValue(text=_option(item, raw).label)
            except AnswerError:
                pass
        return TextValue(text=raw.strip())
    if control in (ControlType.MULTISELECT, ControlType.CHECKBOX_GROUP):
        parts = [raw] if isinstance(raw, bool) else (
            [p for p in raw.split(MULTI_SEPARATOR)] if isinstance(raw, str) else list(raw))
        chosen = [_option(item, str(p)) for p in parts if str(p).strip()]
        return MultiChoiceValue(choices=[FieldOption(value=o.value, label=o.label) for o in chosen])
    if control is ControlType.CHECKBOX:
        if isinstance(raw, bool):
            return BooleanValue(checked=raw)
        word = normalize_text(str(raw))
        if word in TRUE_WORDS:
            return BooleanValue(checked=True)
        if word in FALSE_WORDS:
            return BooleanValue(checked=False)
        raise AnswerError(f"{item.field_id!r} needs yes or no")
    raise AnswerError(f"{item.field_id!r} ({control}) cannot be answered here; "
                      "set it yourself in the browser window")


def user_input_for(item: MissingInput, raw: str | bool | Sequence[str], *,
                   reuse: AnswerReuse = AnswerReuse.APPLICATION) -> UserInput:
    """A validated ``UserInput`` answering exactly this recorded question."""
    try:
        return UserInput.answering(item, value_for(item, raw), reuse=reuse)
    except AnswerError:
        raise
    except ValueError as exc:
        raise AnswerError(f"{item.field_id!r}: {exc}") from exc


def describe(item: MissingInput) -> str:
    """Multi-line text presenting one question, its options and how to answer."""
    lines = [item.label, f"  field: {item.field_id}   ({item.reason.value})", f"  {item.prompt}"]
    usable = [o for o in item.options or [] if o.value.strip() and not o.disabled]
    if usable:
        lines.append("  options: " + "; ".join(f"{o.label} [{o.value}]" for o in usable))
    if item.control_type is ControlType.CHECKBOX:
        lines.append("  answer: yes or no")
    if item.control_type in (ControlType.MULTISELECT, ControlType.CHECKBOX_GROUP):
        lines.append("  answer: one or more options separated by ';'")
    return "\n".join(lines)
