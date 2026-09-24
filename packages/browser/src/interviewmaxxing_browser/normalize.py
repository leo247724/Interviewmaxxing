"""Turn a raw ``DomSnapshot`` into canonical contracts. Pure and deterministic.

Field identity (CONTRACTS.md section 4): ``ApplicationField.id`` comes from the
control's ``name`` (radio/checkbox groups share one), else its DOM ``id``, else a slug
of its label. All question text a user sees for a field is captured so it is part of
the field fingerprint: the label (legend for groups), ``aria-describedby`` text,
descriptions adjacent to the control inside its own container (for example the terms
next to an "I agree" checkbox) and, for a single control inside a fieldset, the
legend. Validation messages go to ``validation_error`` and never into the question.

Only enabled, operable controls become fields. Hidden inputs, honeypots (aria-hidden
or off-screen), disabled controls and CAPTCHA answer boxes are excluded; a CAPTCHA
is reported through the page kind instead. Non-native widgets become ``UNSUPPORTED``
fields for the user to operate, except unambiguous selection-only ARIA comboboxes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from interviewmaxxing_core import (
    ApplicationField,
    ApplicationForm,
    ControlType,
    EvidenceRef,
    FieldOption,
    IdentityEvidenceKind,
    JobIdentityObservation,
    PageInspection,
    PageKind,
    SemanticType,
)

from .semantics import classify
from .signals import (
    ALREADY_APPLIED,
    APPLY_LINK,
    CAPTCHA_TEXT,
    ERROR_HEADING,
    JOB_CLOSED,
    ButtonIntent,
    affirmative_acceptance,
    button_intent,
    job_ids,
)
from .snapshot import DomButton, DomControl, DomSnapshot

TEXT_INPUT_TYPES = frozenset(
    {"text", "email", "tel", "url", "number", "date", "search", "month", "week", "time",
     "datetime-local"}
)

_KNOWN_ATS = (
    ("greenhouse", re.compile(r"(^|\.)greenhouse\.io$")),
    ("lever", re.compile(r"(^|\.)lever\.co$")),
    ("ashby", re.compile(r"(^|\.)ashbyhq\.com$")),
    ("workday", re.compile(r"(^|\.)(?:myworkdayjobs|myworkdaysite|workday)\.com$")),
    ("smartrecruiters", re.compile(r"(^|\.)smartrecruiters\.com$")),
    ("workable", re.compile(r"(^|\.)workable\.com$")),
    ("linkedin_easy_apply", re.compile(r"(^|\.)linkedin\.com$")),
    ("icims", re.compile(r"(^|\.)icims\.com$")),
    ("bamboohr", re.compile(r"(^|\.)bamboohr\.com$")),
    ("jobvite", re.compile(r"(^|\.)jobvite\.com$")),
    ("recruitee", re.compile(r"(^|\.)recruitee\.com$")),
    ("successfactors", re.compile(r"(^|\.)successfactors\.(?:com|eu)$")),
    ("taleo", re.compile(r"(^|\.)taleo\.net$")),
    ("rippling", re.compile(r"(^|\.)rippling\.com$|(^|\.)rippling-ats\.com$")),
)


def detect_ats(url: str) -> str:
    """Informational ATS family from the host; ``generic`` when unknown."""
    host = (urlsplit(url).hostname or "").lower()
    for name, pattern in _KNOWN_ATS:
        if pattern.search(host):
            return name
    return "generic"


@dataclass(frozen=True)
class FieldBinding:
    """How the runtime operates one field. Internal to the browser package."""

    field_id: str
    control_type: ControlType
    selector: str
    option_selectors: Mapping[str, str] = field(default_factory=dict)
    """Option value -> its own input (radio and checkbox groups)."""
    label_selectors: Mapping[str, str] = field(default_factory=dict)
    """Option value (or "" for a single control) -> its <label>, for click fallback."""
    checked_values: frozenset[str] = frozenset()
    value: str = ""
    aria: dict[str, Any] | None = None
    """Document-current ARIA selection binding, never a provider-authored selector."""
    user_completed: bool = False
    """An UNSUPPORTED control the user has already operated."""


@dataclass(frozen=True)
class ClassifiedButton:
    button: DomButton
    intent: ButtonIntent
    apply_control: bool = False
    """An "Apply" control that leads to the application rather than submitting one
    (it has no real form behind it), so it is followed like a link, never clicked as
    a submit or next control."""


@dataclass(frozen=True)
class CaptchaState:
    present: bool = False
    solved: bool = False
    detail: str = ""


@dataclass(frozen=True)
class PageModel:
    """A normalized page: the canonical inspection plus what is needed to act on it."""

    snapshot: DomSnapshot
    inspection: PageInspection
    form_index: int | None
    form_selector: str | None
    form_method: str | None
    bindings: Mapping[str, FieldBinding]
    buttons: list[ClassifiedButton]
    captcha: CaptchaState
    title: str | None
    unsupported_pending: list[str]
    """Required custom fields without a verified value (including ARIA selects)."""
    candidate_fields: list[ApplicationField] = field(default_factory=list)
    """Fields of the chosen form even when it is not an application form (e.g. a
    status lookup), for read-only helpers such as reconciliation."""
    apply_controls: list[DomButton] = field(default_factory=list)
    """Page-wide controls that lead to the application and never submit one."""
    fillable_counts: Mapping[int, int] = field(default_factory=dict)
    """Fillable (non-utility) fields per form index; ``-1`` is outside any form."""

    @property
    def form(self) -> ApplicationForm | None:
        return self.inspection.form


# --- text helpers ---------------------------------------------------------------------


def _squash(text: str) -> str:
    return " ".join(text.split())


_REQUIRED_MARKER = r"(?:[*\u2731\uff0a]|\(required\)|[-\u2013\u2014]\s*required)"
"""``*``, ``\u2731``, ``\uff0a``, ``(required)``, ``- required`` at the end of question text."""
_TRAILING_MARKERS = re.compile(rf"(?:\s*(?:{_REQUIRED_MARKER}|:))+\s*$", re.IGNORECASE)
_LEADING_MARKERS = re.compile(r"^(?:\s*[*\u2731\uff0a])+\s*")
_HAS_REQUIRED_MARKER = re.compile(
    rf"(?:{_REQUIRED_MARKER}\s*:?\s*$)|(?:^\s*[*\u2731\uff0a])", re.IGNORECASE
)
_QUESTION_THEN_MORE = re.compile(
    rf"^(?P<question>.*?{_REQUIRED_MARKER})\s*:?\s+(?P<rest>\S.*)$", re.IGNORECASE | re.DOTALL
)
_IDENTIFIER = re.compile(r"[\[\]_\-]|\d|(?<=[a-z])[A-Z]")


def clean_label(text: str) -> str:
    """Visible question text without required markers (``*``, ``\u2731``, ``\uff0a`` before
    or after it, ``(required)``, ``- required`` after it) or a trailing colon."""
    return _TRAILING_MARKERS.sub("", _LEADING_MARKERS.sub("", _squash(text))).strip()


def _take_question(text: str) -> tuple[str, str | None]:
    """Split visible text at a required marker followed by more text ("Question \u2731
    hint"): the question, and the rest for help text."""
    text = _squash(text)
    lead = _LEADING_MARKERS.match(text)
    prefix, body = (text[:lead.end()], text[lead.end():]) if lead else ("", text)
    split = _QUESTION_THEN_MORE.match(body)
    if split:
        return prefix + split.group("question"), split.group("rest")
    return text, None


def _has_required_marker(text: str) -> bool:
    return bool(_HAS_REQUIRED_MARKER.search(_squash(text)))


def _looks_like_identifier(text: str) -> bool:
    """A machine name (``cards[\u2026][field3]``, ``first_name``, ``q2``, ``startDate``)
    rather than a word a person would read as the question."""
    text = text.strip()
    if not text:
        return False
    return bool(_IDENTIFIER.search(text)) or (" " not in text and not text.isalpha())


def _fallback_label(control: DomControl) -> tuple[str, list[str], str | None]:
    """Label text for a control without a label, placeholder or accessible name.

    The inspector can only name such a control after its ``name``/``id``. When that is
    a machine identifier and visible question text sits next to the control (a
    ``<div>Question \u2731</div>`` before an unlabeled Lever input), that text is the
    question; any text after its required marker stays help text. Returns the label
    text, the adjacent parts left for help text and the described text consumed."""
    adjacent = [_squash(t) for t in control.adjacent if _squash(t)]
    identifier = control.name or control.id
    if not _looks_like_identifier(identifier):
        return identifier, adjacent, None
    if adjacent:
        question, rest = _take_question(adjacent[0])
        return question, [*([rest] if rest else []), *adjacent[1:]], None
    described = next((_squash(d.text) for d in control.described if not d.error and _squash(d.text)), None)
    if described:
        return described, adjacent, described
    if _squash(control.preceding):
        return _take_question(control.preceding)[0], adjacent, None
    return identifier, adjacent, None


def _member_values(members: list[DomControl]) -> list[str]:
    """Option values of a radio/checkbox group. Missing or duplicate ``value``
    attributes (some ATSs post the choice separately) get a stable synthetic value,
    the member's id, else ``<name>#<index>``, so every member stays selectable and
    the binding maps it back to the member's own input."""
    values = [m.value for m in members]
    if len(members) == 1:
        return [values[0] or "on"]
    if all(values) and len(set(values)) == len(values):
        return values
    ids = [m.id for m in members]
    if all(ids) and len(set(ids)) == len(ids):
        return ids
    return [f"{m.name or 'option'}#{i}" for i, m in enumerate(members)]


_COUNTER = re.compile(
    r"^\(?\d+\s*(?:/|of)\s*\d+\)?(?:\s*characters?)?$|^\d+\s*characters?\s*(?:remaining|left)$",
    re.IGNORECASE,
)


def _join_unique(parts: Iterable[str | None]) -> str | None:
    """Join description parts, dropping duplicates and live character counters (which
    change while typing and must not change the question's identity)."""
    out: list[str] = []
    for part in parts:
        text = _squash(part or "")
        if text and not _COUNTER.match(text) and text not in out:
            out.append(text)
    return " ".join(out) or None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "field"


def _section_context(parts: Iterable[str], label: str) -> list[str]:
    """Headings and group labels preceding the control, outermost first, without
    duplicates or the field's own label. Context for models only: it never enters the
    question text or the fingerprint, so saved answers match on the bare wording."""
    out: list[str] = []
    for part in parts:
        text = _squash(part)
        if text and clean_label(text) != label and text not in out:
            out.append(text)
    return out


# --- fields -------------------------------------------------------------------------


def _is_captcha(control: DomControl) -> bool:
    if control.kind != "native" or control.type not in TEXT_INPUT_TYPES:
        return False
    text = " ".join(
        [control.label, control.legend or "", *(d.text for d in control.described),
         *(d.text for d in control.legend_described), *control.image_alts]
    )
    return bool(CAPTCHA_TEXT.search(text))


def _operable(control: DomControl) -> bool:
    if control.disabled:
        return False
    if control.kind == "native" and control.type == "file":
        # Styled uploads often hide the input behind a button; it stays operable
        # unless its label is hidden too and it is not visible itself.
        return control.visible or control.label_visible or bool(control.label)
    return control.visible or control.label_visible


def _control_type(control: DomControl, group_size: int) -> ControlType:
    if control.aria is not None:
        # Wording can reveal multi-select ambiguity missing from ARIA metadata.
        question = " ".join([control.label, *control.adjacent, *(d.text for d in control.described)])
        if re.search(r"(?:mark|select|choose|check)\s+all|multiple\s+(?:answers|options|choices)", question, re.I):
            return ControlType.UNSUPPORTED
        return ControlType.SELECT
    if control.kind == "custom":
        return ControlType.UNSUPPORTED
    if control.tag == "select":
        return ControlType.MULTISELECT if control.multiple else ControlType.SELECT
    if control.tag == "textarea":
        return ControlType.TEXTAREA
    if control.type == "file":
        return ControlType.FILE
    if control.type == "radio":
        return ControlType.RADIO
    if control.type == "checkbox":
        return ControlType.CHECKBOX_GROUP if group_size > 1 else ControlType.CHECKBOX
    if control.type in TEXT_INPUT_TYPES:
        if control.role == "combobox" or control.autocomplete_list:
            return ControlType.UNSUPPORTED  # typeahead: typing alone does not choose a value
        return ControlType.TEXT
    return ControlType.UNSUPPORTED


def _non_errors(descriptions: Iterable[Any]) -> list[str]:
    return [d.text for d in descriptions if not d.error]


_ERROR_PREFIX = re.compile(r"^(?:error|warning)\s*[:\-\u2013]\s*", re.IGNORECASE)


def _errors(descriptions: Iterable[Any]) -> list[str]:
    return [d.text for d in descriptions if d.error]


def _error_text(parts: Iterable[str | None]) -> str | None:
    """Validation messages without a screen-reader "Error:" prefix."""
    return _join_unique(_ERROR_PREFIX.sub("", p or "") for p in parts)


@dataclass
class _Group:
    key: tuple[int, str, str]
    members: list[DomControl]


def _groups(controls: list[DomControl]) -> list[_Group]:
    groups: dict[tuple[int, str, str], _Group] = {}
    ordered: list[_Group] = []
    for i, control in enumerate(controls):
        if control.kind == "native" and control.type in ("radio", "checkbox") and control.name:
            key = (control.form_index, control.type, control.name)
        else:
            key = (control.form_index, "#", str(i))
        if key not in groups:
            groups[key] = _Group(key, [])
            ordered.append(groups[key])
        groups[key].members.append(control)
    return ordered


def _build_field(group: _Group) -> tuple[ApplicationField, FieldBinding]:
    first = group.members[0]
    control_type = _control_type(first, len(group.members))
    is_group = control_type in (ControlType.RADIO, ControlType.CHECKBOX_GROUP)

    values = _member_values(group.members)
    if is_group:
        option_labels = {clean_label(m.label).lower() for m in group.members if m.label}
        label_text = first.legend or first.group_label or first.label or first.name
        adjacent = [_squash(t) for t in first.adjacent if _squash(t)]
        described = [t for m in group.members for t in _non_errors(m.described)]
        consumed: str | None = None
        derived = clean_label(label_text)
        if not derived or derived.lower() in option_labels or _looks_like_identifier(label_text):
            # The group has no question of its own: its "label" is an option's text
            # (a yes/no group) or a machine name. The visible question shown next to
            # or before the group is the question.
            for candidate in (*adjacent, *_non_errors(first.legend_described),
                              *_non_errors(first.group_described), *described, first.preceding):
                cleaned = clean_label(candidate)
                if cleaned and cleaned.lower() not in option_labels:
                    label_text, consumed = candidate, _squash(candidate)
                    break
        question, rest = _take_question(label_text) if consumed else (label_text, None)
        label = clean_label(question)
        help_text = _join_unique(
            [*(t for t in _non_errors(first.legend_described) if _squash(t) != consumed),
             *(t for t in _non_errors(first.group_described) if _squash(t) != consumed),
             *(t for t in described if _squash(t) != consumed),
             rest,
             *(clean_label(t) for t in adjacent if t != consumed)]
        )
        errors = _error_text(
            [*_errors(first.legend_described), *_errors(first.group_described),
             *(t for m in group.members for t in _errors(m.described)),
             *(m.error_message for m in group.members), *first.adjacent_errors]
        )
        selector = first.legend_selector or first.selector
        options = [
            FieldOption(value=value, label=clean_label(m.label) or value,
                        selector=m.selector, disabled=m.disabled)
            for m, value in zip(group.members, values, strict=True)
        ]
        required = any(m.required for m in group.members) or _has_required_marker(question)
    else:
        if first.label or first.placeholder:
            label_text, adjacent, consumed = first.label or first.placeholder, list(first.adjacent), None
        else:
            label_text, adjacent, consumed = _fallback_label(first)
        label = clean_label(label_text)
        legend = first.legend if first.legend and clean_label(first.legend) != label else None
        help_text = _join_unique(
            [legend, first.group_label, *_non_errors(first.group_described),
             *_non_errors(first.legend_described),
             *(t for t in _non_errors(first.described) if _squash(t) != consumed),
             *(clean_label(t) for t in adjacent)]
        )
        errors = _error_text(
            [*_errors(first.described), first.error_message, *first.adjacent_errors]
        )
        selector = first.selector
        options = None
        if control_type in (ControlType.SELECT, ControlType.MULTISELECT):
            options = [
                FieldOption(value=o.value, label=o.label, disabled=o.disabled)
                for o in first.options
            ]
        if control_type is ControlType.SELECT and first.aria:
            options = [FieldOption(value=o["value"], label=o["label"], disabled=o["disabled"])
                       for o in first.aria["options"]]
        # A visible required marker counts when the control itself carries no attribute.
        required = first.required or _has_required_marker(label_text)

    user_completed = control_type is ControlType.UNSUPPORTED and first.has_value
    if user_completed or (control_type is ControlType.UNSUPPORTED and first.kind == "native"
                          and first.value.strip()):
        user_completed = True
        required = False  # already operated by the user; the runtime never touches it

    field_id = first.name or first.id or f"field-{_slug(label)}"
    input_type = first.type if control_type is ControlType.TEXT else None
    accept = (
        [a.strip() for a in first.accept.split(",") if a.strip()] or None
        if control_type is ControlType.FILE
        else None
    )
    max_length = (
        first.max_length if control_type in (ControlType.TEXT, ControlType.TEXTAREA) else None
    )
    semantic = classify(
        label=label,
        help_text=help_text or "",
        name=first.name,
        element_id=first.id,
        autocomplete=first.autocomplete,
        input_type=input_type,
        control_type=control_type,
    )
    app_field = ApplicationField(
        id=field_id,
        label=label,
        semantic_type=semantic,
        control_type=control_type,
        selector=selector,
        required=required,
        input_type=input_type,
        options=options if control_type is not ControlType.UNSUPPORTED else None,
        accept=accept,
        max_length=max_length,
        placeholder=first.placeholder or None,
        help_text=help_text,
        validation_error=errors,
        section_context=_section_context(first.section_context, label),
    )
    binding = FieldBinding(
        field_id=field_id,
        control_type=control_type,
        selector=first.selector,
        option_selectors=(
            {value: m.selector for m, value in zip(group.members, values, strict=True)}
            if is_group else {}
        ),
        label_selectors={
            (value if is_group else ""): m.label_selector
            for m, value in zip(group.members, values, strict=True)
            if m.label_selector
        },
        checked_values=frozenset(
            value for m, value in zip(group.members, values, strict=True) if m.checked
        ) if is_group else (frozenset({"on"}) if first.checked else frozenset()),
        value=first.aria["value"] if first.aria else first.value,
        aria=first.aria if control_type is ControlType.SELECT else None,
        user_completed=user_completed,
    )
    return app_field, binding


def _dedupe_ids(pairs: list[tuple[ApplicationField, FieldBinding]]) -> list[tuple[ApplicationField, FieldBinding]]:
    seen: dict[str, int] = {}
    out = []
    for app_field, binding in pairs:
        n = seen.get(app_field.id, 0) + 1
        seen[app_field.id] = n
        if n > 1:
            new_id = f"{app_field.id}~{n}"
            app_field = app_field.model_copy(update={"id": new_id})
            binding = FieldBinding(**{**binding.__dict__, "field_id": new_id})
        out.append((app_field, binding))
    return out


# --- identity -----------------------------------------------------------------------


def _job_postings(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "JobPosting" in kinds:
                found.append(node)
            if "@graph" in node:
                walk(node["@graph"])

    walk(data)
    return found


def _identifier(posting: Mapping[str, Any]) -> str | None:
    ident = posting.get("identifier")
    if isinstance(ident, dict):
        ident = ident.get("value")
    if isinstance(ident, (str, int)) and str(ident).strip():
        return str(ident).strip()
    return None


def _text(value: Any) -> str | None:
    return _squash(value) if isinstance(value, str) and value.strip() else None


def extract_job_identity(snapshot: DomSnapshot) -> JobIdentityObservation | None:
    """A job identity the page itself shows: schema.org ``JobPosting`` identifier, or
    a single "Job ID ..." token in the page text. Never derived from the URL."""
    url = snapshot.url
    tenant = (urlsplit(url).netloc or "").lower() or "unknown"
    ats = detect_ats(url)
    h1 = next((h.text for h in snapshot.headings if h.level == 1), None)
    for raw in snapshot.ld_json:
        for posting in _job_postings(raw):
            ident = _identifier(posting)
            if not ident:
                continue
            org = posting.get("hiringOrganization")
            location = posting.get("jobLocation")
            locality = None
            if isinstance(location, dict) and isinstance(location.get("address"), dict):
                locality = _text(location["address"].get("addressLocality"))
            return JobIdentityObservation(
                ats_type=ats,
                ats_tenant=tenant,
                external_job_id=ident,
                evidence_kind=IdentityEvidenceKind.STRUCTURED_DATA,
                evidence=f"schema.org JobPosting identifier {ident!r} on {url}",
                observed_url=url,
                company=_text(org.get("name")) if isinstance(org, dict) else None,
                title=_text(posting.get("title")) or h1,
                location=locality,
            )
    visible_text = " ".join([*(h.text for h in snapshot.headings), snapshot.body_text[:4000]])
    ids = job_ids(visible_text)
    if len(ids) == 1:
        return JobIdentityObservation(
            ats_type=ats,
            ats_tenant=tenant,
            external_job_id=ids[0],
            evidence_kind=IdentityEvidenceKind.ATS_JOB_ID_ON_PAGE,
            evidence=f"job id {ids[0]!r} shown in the page text of {url}",
            observed_url=url,
            company=_text(snapshot.meta.og_site_name),
            title=h1,
        )
    return None


# --- page ---------------------------------------------------------------------------


def _captcha_state(snapshot: DomSnapshot, captcha_controls: list[DomControl], has_fields: bool) -> CaptchaState:
    frames = [f for f in snapshot.captcha_frames if f.visible]
    tokens_filled = any(t.filled for t in snapshot.captcha_tokens)
    if frames or snapshot.captcha_widget:
        return CaptchaState(True, tokens_filled, "CAPTCHA widget on the page")
    if captcha_controls:
        solved = all(c.value.strip() for c in captcha_controls)
        return CaptchaState(True, solved, f"CAPTCHA challenge: {captcha_controls[0].label}")
    page_text = " ".join([snapshot.title, *(h.text for h in snapshot.headings)])
    if not has_fields and CAPTCHA_TEXT.search(page_text):
        return CaptchaState(True, False, "The page asks to verify you are human")
    return CaptchaState()


_UTILITY_CONTROL = re.compile(r"\b(?:lang(?:uage)?|locale|share|search)\b", re.IGNORECASE)
"""Page utilities that are not application questions: a language/locale switch, a
share or search box. They never make a page an application form on their own."""


def _fillable(fields: list[ApplicationField]) -> list[ApplicationField]:
    return [f for f in fields if not _UTILITY_CONTROL.search(f"{f.id} {f.label}")]


def _classify_buttons(
    buttons: list[DomButton], fillable_counts: Mapping[int, int]
) -> list[ClassifiedButton]:
    """``fillable_counts``: fillable fields per form index (-1: controls outside any
    form). An "Apply"/"Apply now" control whose form has fewer than two of them does
    not submit an application; it leads to one, and is never a submit/next intent."""
    out: list[ClassifiedButton] = []
    for b in buttons:
        intent = button_intent(b.text, submits_form=b.submits_form)
        apply_control = bool(APPLY_LINK.search(b.text)) and fillable_counts.get(b.form_index, 0) < 2
        out.append(ClassifiedButton(b, ButtonIntent.OTHER if apply_control else intent, apply_control))
    return out


def _qualifies_as_application(
    fields: list[ApplicationField], buttons: list[ClassifiedButton],
    final: bool | None, has_step: bool,
) -> bool:
    """At least two fillable fields; one fillable field only with a genuine submit or
    next control (never an apply control); no fields only for a review step of a
    multi-step form."""
    fillable = _fillable(fields)
    genuine = any(b.intent in (ButtonIntent.SUBMIT, ButtonIntent.NEXT) for b in buttons)
    return (
        len(fillable) >= 2
        or (len(fillable) == 1 and genuine)
        or (not fields and final is not None and has_step)
    )


def _primary_action(buttons: list[ClassifiedButton]) -> tuple[bool | None, str | None, str | None]:
    actionable = [b for b in buttons if b.intent is not ButtonIntent.OTHER]
    intents = {b.intent for b in actionable}
    if intents == {ButtonIntent.SUBMIT}:
        chosen = next((b for b in actionable if not b.button.disabled), actionable[0])
        return True, chosen.button.selector, None
    if intents == {ButtonIntent.NEXT}:
        chosen = next((b for b in actionable if not b.button.disabled), actionable[0])
        return False, None, chosen.button.selector
    return None, None, None


def build_page(
    snapshot: DomSnapshot,
    *,
    fallback_step: int = 0,
    http_status: int | None = None,
    evidence: list[EvidenceRef] | None = None,
) -> PageModel:
    """Normalize one snapshot. ``fallback_step`` is used when the page shows no step
    indicator (the session's own count of steps advanced)."""
    captcha_controls: list[DomControl] = []
    operable: list[DomControl] = []
    for control in snapshot.controls:
        if control.kind == "native" and control.type == "password":
            continue
        if not _operable(control):
            continue
        if _is_captcha(control):
            captcha_controls.append(control)
            continue
        operable.append(control)

    per_form: dict[int, list[tuple[ApplicationField, FieldBinding]]] = {}
    for group in _groups(operable):
        pair = _build_field(group)
        per_form.setdefault(group.key[0], []).append(pair)

    fillable_counts = {index: len(_fillable([f for f, _ in pairs])) for index, pairs in per_form.items()}
    all_buttons = _classify_buttons(snapshot.buttons, fillable_counts)

    def score(index: int) -> int:
        pairs = per_form.get(index, [])
        has_file = any(f.control_type is ControlType.FILE for f, _ in pairs)
        acts = [b for b in all_buttons if b.button.form_index == index
                and b.intent in (ButtonIntent.SUBMIT, ButtonIntent.NEXT)]
        return len(pairs) + (5 if has_file else 0) + (3 if acts else 0)

    def plausible_application(index: int) -> bool:
        candidate_fields = [f for f, _ in per_form.get(index, [])]
        candidate_buttons = [b for b in all_buttons if b.button.form_index == index]
        final, _, _ = _primary_action(candidate_buttons)
        candidate_form = next((f for f in snapshot.forms if f.index == index), None)
        lookup = (candidate_form is not None and candidate_form.method == "get"
                  and len(candidate_fields) <= 2 and not any(
                      b.intent in (ButtonIntent.SUBMIT, ButtonIntent.NEXT)
                      for b in candidate_buttons))
        return not lookup and _qualifies_as_application(
            candidate_fields, candidate_buttons, final, snapshot.step is not None)

    candidates = set(per_form) | {b.button.form_index for b in all_buttons
                                  if b.intent in (ButtonIntent.SUBMIT, ButtonIntent.NEXT)}
    plausible = [index for index in candidates if plausible_application(index)]
    ambiguous_forms = False
    if len(plausible) > 1:
        # Field count and DOM order cannot distinguish an alert subscription from
        # an application. Only retain the established dominant-document case:
        # exactly one candidate asks for a resume/cover letter, has more fields,
        # and outranks every competitor. This is a bounded heuristic, not general form-purpose
        # understanding; other competing plausible forms require user review.
        document_forms = [index for index in plausible if any(
            f.control_type is ControlType.FILE
            and f.semantic_type in (SemanticType.RESUME, SemanticType.COVER_LETTER)
            for f, _ in per_form.get(index, []))]
        dominant = (document_forms[0] if len(document_forms) == 1
                    and all(len(per_form.get(document_forms[0], [])) > len(per_form.get(other, []))
                            and score(document_forms[0]) > score(other)
                            for other in plausible if other != document_forms[0]) else None)
        form_index = dominant
        ambiguous_forms = dominant is None
    elif plausible:
        form_index = plausible[0]
    else:
        # Non-application lookup helpers have no application data-entry authority.
        form_index = max(candidates, key=lambda index: (score(index), -index)) if candidates else None
    pairs = _dedupe_ids(per_form.get(form_index, [])) if form_index is not None else []
    buttons = [b for b in all_buttons if b.button.form_index == form_index] if form_index is not None else []
    is_final, submit_selector, next_selector = _primary_action(buttons)
    dom_form = next((f for f in snapshot.forms if f.index == form_index), None)

    step = fallback_step
    if snapshot.step is not None and snapshot.step.current >= 1:
        step = snapshot.step.current - 1

    fields = [f for f, _ in pairs]
    has_action = is_final is not None or any(
        b.intent in (ButtonIntent.SUBMIT, ButtonIntent.NEXT, ButtonIntent.AMBIGUOUS) for b in buttons
    )
    lookup_only = (
        dom_form is not None and dom_form.method == "get" and len(fields) <= 2
        and not any(b.intent in (ButtonIntent.SUBMIT, ButtonIntent.NEXT) for b in buttons)
    )
    is_application_form = not lookup_only and _qualifies_as_application(
        fields, buttons, is_final, snapshot.step is not None)

    captcha = _captcha_state(snapshot, captcha_controls, bool(fields))
    # An embedded widget (badge, checkbox, token field) on an otherwise usable form only
    # matters when the form is submitted, so filling and preparation go ahead and the
    # inspection reports it as pending. A text challenge control, or a widget with no
    # fillable form behind it (an interstitial), still needs the user first.
    captcha_pending = (
        captcha.present and not captcha.solved and is_application_form
        and not captcha_controls
        and (any(f.visible for f in snapshot.captcha_frames) or snapshot.captcha_widget)
    )
    identity = extract_job_identity(snapshot)
    alerts = [r.text for r in snapshot.regions if r.role == "alert"]
    h1 = next((h.text for h in snapshot.headings if h.level == 1), None)
    form: ApplicationForm | None = None
    message: str | None = None

    if snapshot.password_visible:
        kind = PageKind.SIGN_IN_REQUIRED
        message = "Sign in (or create an account) in the browser to continue."
    elif captcha.present and not captcha.solved and not captcha_pending:
        kind = PageKind.CAPTCHA
        message = f"{captcha.detail}. Solve it in the browser to continue."
    elif ambiguous_forms:
        kind = PageKind.UNKNOWN
        message = ("Multiple plausible application forms are visible; their purpose is ambiguous. "
                   "No form was selected for filling. Open or identify the intended application form.")
    elif is_application_form:
        kind = PageKind.APPLICATION_FORM
        form = ApplicationForm(
            url=snapshot.url,
            ats_type=detect_ats(snapshot.url),
            step=step,
            fields=fields,
            is_final_step=is_final,
            submit_selector=submit_selector,
            next_selector=next_selector,
            page_errors=alerts,
        )
        if is_final is None and has_action:
            message = "The step's primary action is ambiguous; it will not be clicked automatically."
    elif ALREADY_APPLIED.search(snapshot.body_text):
        kind = PageKind.ALREADY_APPLIED
        message = "The site says an application already exists."
    elif any(
        affirmative_acceptance(t)
        for t in (snapshot.title, *(h.text for h in snapshot.headings),
                  *(r.text for r in snapshot.regions), snapshot.body_text[:3000])
    ):
        kind = PageKind.CONFIRMATION
    elif JOB_CLOSED.search(snapshot.body_text):
        kind = PageKind.JOB_CLOSED
    elif any(APPLY_LINK.search(link.text) for link in snapshot.links) or any(
        b.apply_control for b in all_buttons
    ):
        kind = PageKind.JOB_DESCRIPTION
    elif (http_status is not None and http_status >= 400) or any(
        ERROR_HEADING.search(h.text) for h in snapshot.headings if h.level <= 2
    ):
        kind = PageKind.ERROR
        message = f"HTTP {http_status}" if http_status and http_status >= 400 else h1
    else:
        kind = PageKind.UNKNOWN

    bindings = {b.field_id: b for _, b in pairs}
    pending = [
        f.id for f in fields
        if f.required and (f.control_type is ControlType.UNSUPPORTED
                           or (bindings[f.id].aria is not None and not bindings[f.id].value))
    ] if form is not None else []
    inspection = PageInspection(
        kind=kind,
        observed_url=snapshot.url,
        form=form,
        job_identity=identity,
        message=message,
        evidence=evidence or [],
        captcha_pending=captcha_pending and form is not None,
    )
    return PageModel(
        snapshot=snapshot,
        inspection=inspection,
        form_index=form_index,
        form_selector=dom_form.selector if dom_form else None,
        form_method=dom_form.method if dom_form else None,
        bindings=bindings,
        buttons=buttons,
        captcha=captcha,
        title=(identity.title if identity and identity.title else h1),
        unsupported_pending=pending,
        candidate_fields=fields,
        apply_controls=[b.button for b in all_buttons if b.apply_control],
        fillable_counts=fillable_counts,
    )
