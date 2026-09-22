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
fields for the user to operate.
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
    ("workday", re.compile(r"(^|\.)myworkdayjobs\.com$|(^|\.)workday\.com$")),
    ("smartrecruiters", re.compile(r"(^|\.)smartrecruiters\.com$")),
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
    user_completed: bool = False
    """An UNSUPPORTED control the user has already operated."""


@dataclass(frozen=True)
class ClassifiedButton:
    button: DomButton
    intent: ButtonIntent


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
    """Required UNSUPPORTED fields the user has not operated yet."""
    candidate_fields: list[ApplicationField] = field(default_factory=list)
    """Fields of the chosen form even when it is not an application form (e.g. a
    status lookup), for read-only helpers such as reconciliation."""

    @property
    def form(self) -> ApplicationForm | None:
        return self.inspection.form


# --- text helpers ---------------------------------------------------------------------


def _squash(text: str) -> str:
    return " ".join(text.split())


def clean_label(text: str) -> str:
    """Visible label text without a trailing required marker or colon."""
    text = _squash(text)
    return re.sub(r"[\s*:]+$", "", text).strip()


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

    if is_group:
        label = clean_label(first.legend or first.group_label or first.label or first.name)
        help_text = _join_unique(
            [*_non_errors(first.legend_described), *_non_errors(first.group_described),
             *(t for m in group.members for t in _non_errors(m.described)), *first.adjacent]
        )
        errors = _error_text(
            [*_errors(first.legend_described), *_errors(first.group_described),
             *(t for m in group.members for t in _errors(m.described)),
             *(m.error_message for m in group.members), *first.adjacent_errors]
        )
        selector = first.legend_selector or first.selector
        options = [
            FieldOption(value=m.value or "on", label=clean_label(m.label) or m.value,
                        selector=m.selector, disabled=m.disabled)
            for m in group.members
        ]
        required = any(m.required for m in group.members)
    else:
        label = clean_label(first.label or first.placeholder or first.name or first.id)
        legend = first.legend if first.legend and clean_label(first.legend) != label else None
        help_text = _join_unique(
            [legend, *_non_errors(first.legend_described), *_non_errors(first.described),
             *first.adjacent]
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
        required = first.required

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
    )
    binding = FieldBinding(
        field_id=field_id,
        control_type=control_type,
        selector=first.selector,
        option_selectors={m.value or "on": m.selector for m in group.members} if is_group else {},
        label_selectors={
            (m.value or "on") if is_group else "": m.label_selector
            for m in group.members
            if m.label_selector
        },
        checked_values=frozenset(
            (m.value or "on") for m in group.members if m.checked
        ) if is_group else (frozenset({"on"}) if first.checked else frozenset()),
        value=first.value,
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


def _classify_buttons(buttons: list[DomButton]) -> list[ClassifiedButton]:
    return [ClassifiedButton(b, button_intent(b.text, submits_form=b.submits_form)) for b in buttons]


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

    all_buttons = _classify_buttons(snapshot.buttons)

    def score(index: int) -> tuple[int, int]:
        pairs = per_form.get(index, [])
        has_file = any(f.control_type is ControlType.FILE for f, _ in pairs)
        acts = [b for b in all_buttons if b.button.form_index == index
                and b.intent in (ButtonIntent.SUBMIT, ButtonIntent.NEXT)]
        return (len(pairs) + (5 if has_file else 0) + (3 if acts else 0), -index)

    candidates = set(per_form) | {b.button.form_index for b in all_buttons
                                  if b.intent in (ButtonIntent.SUBMIT, ButtonIntent.NEXT)}
    form_index = max(candidates, key=score) if candidates else None
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
    is_application_form = not lookup_only and (
        (len(fields) >= 2)
        or (len(fields) == 1 and has_action)
        or (not fields and is_final is not None and snapshot.step is not None)
    )

    captcha = _captcha_state(snapshot, captcha_controls, bool(fields))
    identity = extract_job_identity(snapshot)
    alerts = [r.text for r in snapshot.regions if r.role == "alert"]
    h1 = next((h.text for h in snapshot.headings if h.level == 1), None)
    form: ApplicationForm | None = None
    message: str | None = None

    if snapshot.password_visible:
        kind = PageKind.SIGN_IN_REQUIRED
        message = "Sign in (or create an account) in the browser to continue."
    elif captcha.present and not captcha.solved:
        kind = PageKind.CAPTCHA
        message = f"{captcha.detail}. Solve it in the browser to continue."
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
        APPLY_LINK.search(b.button.text) for b in all_buttons
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
        if f.control_type is ControlType.UNSUPPORTED and f.required
    ] if form is not None else []
    inspection = PageInspection(
        kind=kind,
        observed_url=snapshot.url,
        form=form,
        job_identity=identity,
        message=message,
        evidence=evidence or [],
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
    )
