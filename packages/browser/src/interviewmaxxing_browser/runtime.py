"""The generic application browser: inspect, fill, navigate, submit, confirm.

:class:`GenericApplicationBrowser` implements ``interviewmaxxing_core.ApplicationBrowser``
on top of any :class:`~interviewmaxxing_browser.driver.PageDriver`. It observes and
acts; it never decides that an application was submitted and never writes state.

Safety rules enforced here:

* Every action re-inspects the live page first. ``fill`` refuses a packet that has
  problems against the form (``packet.problems_against``) or a form that is no longer
  on the page. Values come only from the packet; page text is never interpreted as
  instructions.
* ``advance`` clicks only an unambiguous forward control and refuses on a step
  whose primary action submits. Ambiguous controls are never clicked.
* ``submit`` clicks once, only an unambiguous final submit control, only if the
  browser's own constraint validation would let the form through. After a dispatched
  submit whose outcome was not established, this session refuses to submit again.
* ``confirm`` reports ACCEPTED only with acceptance wording that is tied to this
  application (the job id or title shown on the result page, or a confirmation
  reference that appeared after the click). A click, a navigation, a vanished form,
  a generic "Thank you" or a timeout is never acceptance; it is UNKNOWN.
* ``reconcile`` re-reads the site later (status links and GET lookup forms only)
  to establish an uncertain outcome without ever resubmitting.
* Custom menu controls of the selected application form are probed once per
  document during inspection (opened, read through their own listbox, closed and
  verified unchanged; never typed into or chosen) so their options become canonical
  ``SELECT`` fields; ``observe`` and waits for the user never probe.
* Uploads first. ``fill`` attaches answered files before anything else, directly to the
  file input (hidden behind an "Attach" button or a drop zone included), never twice,
  waits (bounded) for the upload and reads it back from the input, a file chip or an
  upload notice; then it lets the page settle and re-reads it, so an autofill the upload
  triggers lands before our values are typed and read back. Only the attached control's
  own description and uploader, and where page actions sit, may change meanwhile (the
  submit control must keep its text and form); any other change stops the fill.
* Overlays. Loading overlays ("Loading...", ``aria-busy``, progress bars) are waited out
  (bounded) before acting; an offer to autofill the application is declined once with
  its own "No thanks"-style control; third-party autofill and "Apply with ..." controls
  are never clicked. ``observe`` does neither.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from interviewmaxxing_core import (
    USER_ACTION_PAGES,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ArtifactRef,
    BooleanValue,
    BrowserOptions,
    ChoiceValue,
    ControlType,
    EvidenceKind,
    EvidenceRef,
    FieldFillResult,
    FieldFillStatus,
    FileValue,
    FillResult,
    JobIdentityObservation,
    JobRecord,
    MultiChoiceValue,
    NavigationResult,
    NotSubmittedNext,
    PageInspection,
    PageKind,
    ReconciliationMethod,
    SemanticType,
    SubmissionObservation,
    SubmissionOutcome,
    SubmissionReconciliation,
    SubmitActionResult,
    TextValue,
    normalize_text,
    utc_now,
)

from .annotations import FormAnnotator, SchemaHintLoader, observation_signature, semantic_only
from .aria import (
    ARIA_HELPERS,
    LookupOutcome,
    MenuProbe,
    dial_code,
    fill_input_select,
    fill_lookup,
    fill_phone,
)
from .captcha import CAPTCHA_DETECT, CaptchaAttempt, CaptchaSolver, parse_detection
from .driver import (
    _FILE_DIGEST,
    DEEP_QUERY,
    CapabilityUnsupported,
    DriverError,
    NotActionable,
    PageContextLost,
    PageDriver,
    file_anchor,
    file_shown,
    shown_names,
)
from .evidence import EvidenceRecorder
from .normalize import TEXT_INPUT_TYPES, FieldBinding, PageModel, build_page, detect_ats
from .signals import (
    APPLY_LINK,
    CONFIRMATION_LINK,
    LOADING_STATE,
    MANUAL_APPLY,
    NOT_SUBMITTED_STATUS,
    PENDING,
    STATUS_LINK,
    THIRD_PARTY_ASSIST,
    UNCERTAIN,
    ButtonIntent,
    affirmative_acceptance,
    application_records,
    autofill_decline,
    confirmation_references,
    date_segment_values,
    job_ids,
    lookup_matches,
    national_number,
)
from .snapshot import DomButton, DomLink, DomPrompt, DomSnapshot, inspector_script
from .uploads import UPLOAD_STATE, UploadState
from .wizard import ResumeChoice, choose_resume


class SubmissionRefused(RuntimeError):
    """The runtime refused an action that could submit (or resubmit) an application."""


class AmbiguousAction(RuntimeError):
    """The page offers no unambiguous control for the requested action."""


class _Rebound(Exception):
    """Right before a write the form read as the same questions with regenerated
    selectors (a re-render). The fill's structure was re-read and nothing was written;
    the field is operated again through its re-resolved binding."""


class _ConditionalReveal(PageContextLost):
    """A choice this fill made changed a question it has not written yet: that question
    became required or optional, or its help text changed (BambooHR marks the sponsorship
    question required once work authorization is answered). Its approved answer is not
    written; the fill stops and the step is inspected and resolved again with every answer
    written so far, the choice included, in place."""


def _short_label(label: str, limit: int = 60) -> str:
    """A question's wording as one line, cut for a page error."""
    text = " ".join(label.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _question_shape(question: ApplicationField) -> tuple[Any, ...]:
    """What a question is apart from its id, help text and requiredness: its wording,
    placeholder, control and options (value and label)."""
    return (normalize_text(question.label), normalize_text(question.placeholder or ""),
            question.control_type.value,
            tuple((o.value, normalize_text(o.label)) for o in question.options or []))


def _renamed_questions(approved: Sequence[ApplicationField],
                       fresh: Sequence[ApplicationField]) -> dict[str, str]:
    """Fresh id -> approved id for the questions a re-render gave another id (a generated
    DOM id: BambooHR's "Date Available" is ``FabricTextField-68``, then ``-355`` once a
    Yes/No is answered). Such a question is matched by id on neither side and is the same
    occurrence of the same shape (``_question_shape``) after the same question matched by
    id (or from the start): the same place, wording, control and options."""
    old_ids = {q.id for q in approved}
    new_ids = {q.id for q in fresh}

    def places(questions: Sequence[ApplicationField], others: set[str]) -> dict[tuple[Any, ...], str]:
        out: dict[tuple[Any, ...], str] = {}
        anchor: str | None = None
        seen: Counter[tuple[Any, ...]] = Counter()
        for question in questions:
            if question.id in others:
                anchor, seen = question.id, Counter()
                continue
            shape = _question_shape(question)
            seen[shape] += 1
            out[(anchor, shape, seen[shape])] = question.id
        return out

    old = places(approved, new_ids)
    return {new_id: old[key] for key, new_id in places(fresh, old_ids).items() if key in old}


def _rekeyed_form(form: ApplicationForm, renames: Mapping[str, str]) -> ApplicationForm:
    if not renames:
        return form
    return form.model_copy(update={"fields": [
        q.model_copy(update={"id": renames.get(q.id, q.id)}) for q in form.fields]})


def _rekeyed(model: PageModel, renames: Mapping[str, str]) -> PageModel:
    """``model`` with renamed questions under the ids the fill knows them by."""
    if not renames or model.form is None:
        return model
    return replace(
        model,
        inspection=model.inspection.model_copy(update={"form": _rekeyed_form(model.form, renames)}),
        bindings={renames.get(k, k): b for k, b in model.bindings.items()},
        unsupported_pending=[renames.get(k, k) for k in model.unsupported_pending],
        candidate_fields=[q.model_copy(update={"id": renames.get(q.id, q.id)}) for q in model.candidate_fields],
    )


def _only_questions(model: PageModel, ids: Collection[str]) -> PageModel:
    """``model`` reduced to the questions ``ids`` (fields and bindings)."""
    if model.form is None:
        return model
    return replace(
        model,
        inspection=model.inspection.model_copy(update={"form": model.form.model_copy(update={
            "fields": [q for q in model.form.fields if q.id in ids]})}),
        bindings={k: b for k, b in model.bindings.items() if k in ids},
    )


def _own_selectors(binding: FieldBinding) -> set[str]:
    """Every control a question's binding operates."""
    return {binding.selector, *binding.option_selectors.values(),
            *(selector for _, selector in binding.date_segments)}


@dataclass(frozen=True)
class _QuestionDelta:
    """How the questions of a form step differ from another reading of it, question by
    question (ids already matched, renamed questions under their known ids)."""

    added: tuple[ApplicationField, ...] = ()
    removed: tuple[ApplicationField, ...] = ()
    reworded: tuple[ApplicationField, ...] = ()
    """Another wording, placeholder, control or option set: another question."""
    toggled: tuple[ApplicationField, ...] = ()
    """Became required or optional."""
    helped: tuple[ApplicationField, ...] = ()
    """Only its help text changed."""
    moved: bool = False

    @property
    def follow_ups(self) -> bool:
        """Only changes the step can be inspected and resolved again for: questions
        appeared, became required or optional, or changed their help text."""
        return (not (self.removed or self.reworded or self.moved)
                and bool(self.added or self.toggled or self.helped))

    def describe(self) -> str:
        """The follow-up changes, the questions named by their wording (page text, never
        an answer)."""
        def named(questions: Sequence[ApplicationField]) -> str:
            return "; ".join(_short_label(q.label) or q.id for q in questions)

        parts = []
        if self.added:
            parts.append(f"{len(self.added)} question(s) appeared ({named(self.added)})")
        if self.toggled:
            now = [q for q in self.toggled if q.required]
            parts.append(f"{len(self.toggled)} question(s) ({named(self.toggled)}) became "
                         + ("required" if len(now) == len(self.toggled) else "optional" if not now
                            else "required or optional"))
        if self.helped:
            parts.append(f"{len(self.helped)} question(s) ({named(self.helped)}) changed their help text")
        return " and ".join(parts)


def _question_delta(before: ApplicationForm, after: ApplicationForm) -> _QuestionDelta:
    known = {q.id: q for q in before.fields}
    now_ids = {q.id for q in after.fields}
    kept = [q for q in after.fields if q.id in known]
    return _QuestionDelta(
        added=tuple(q for q in after.fields if q.id not in known),
        removed=tuple(q for q in before.fields if q.id not in now_ids),
        reworded=tuple(q for q in kept if _question_shape(q) != _question_shape(known[q.id])),
        toggled=tuple(q for q in kept if q.required != known[q.id].required),
        helped=tuple(q for q in kept if _question_shape(q) == _question_shape(known[q.id])
                     and normalize_text(q.help_text or "") != normalize_text(known[q.id].help_text or "")),
        moved=[q.id for q in kept] != [q.id for q in before.fields if q.id in now_ids],
    )


def _phone_digits(text: str) -> str:
    """A phone number's digits as a site's formatting keeps them: no spaces, dashes,
    parentheses or "+", and no leading country code 1 on an 11-digit number."""
    digits = re.sub(r"\D", "", text)
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def _reads_as_typed(app_field: ApplicationField, got: object, text: str) -> bool:
    """A text readback equals what was typed. A phone number only needs the same digits:
    sites format it as it is typed ("+1 303 555 0142" shows as "(303) 555-0142")."""
    if not isinstance(got, str):
        return False
    want = text.replace("\r\n", "\n")
    if got.replace("\r\n", "\n") == want:
        return True
    return (app_field.semantic_type is SemanticType.PHONE
            and _phone_digits(got) != "" and _phone_digits(got) == _phone_digits(want))


_CHOICE_CONTROLS = frozenset({
    ControlType.RADIO, ControlType.SELECT, ControlType.MULTISELECT, ControlType.CHECKBOX,
    ControlType.CHECKBOX_GROUP, ControlType.TYPEAHEAD,
})
"""Controls whose answer a site may condition follow-up questions on."""


_READ_CONTROL = """(sel) => {""" + DEEP_QUERY + """
  const el = deepOne(sel);
  if (!el) return null;
  if (el.tagName === 'SELECT') return {values: Array.from(el.selectedOptions).map((o) => o.value)};
  if (el.type === 'file') return {files: Array.from(el.files || []).map((f) => ({name: f.name, size: f.size}))};
  if (el.type === 'checkbox' || el.type === 'radio') return {checked: el.checked};
  return {value: el.value};
}"""

_READ_CHECKED = """(sels) => { """ + DEEP_QUERY + """
  return sels.map((s) => {
    const e = deepOne(s);
    if (!e) return null;
    // A toggle-button option (Ashby's yes/no) is chosen when it is pressed.
    if (e.tagName === 'BUTTON' && e.hasAttribute('aria-pressed')) return e.getAttribute('aria-pressed') === 'true';
    return e.checked;
  });
}"""

_NATIVE_VALIDITY = """({form, button}) => {""" + DEEP_QUERY + ARIA_HELPERS + """
  const f = form ? deepOne(form) : null;
  const b = button ? deepOne(button) : null;
  const isForm = !!f && f.tagName === 'FORM';
  if (!f || (isForm && f.noValidate) || (b && b.formNoValidate)) return [];
  const out = [];
  const CAPTCHA_TOKENS = ['g-recaptcha-response', 'h-captcha-response', 'cf-turnstile-response'];
  // A dialog wizard is the application form without being a <form>: its own controls.
  const controls = isForm ? Array.from(f.elements) : Array.from(f.querySelectorAll('input, select, textarea'))
    .filter((el) => !(el.form && el.form.noValidate));
  for (const el of controls) {
    if (!el.willValidate || el.validity.valid) continue;  // read-only: no 'invalid' events
    if (CAPTCHA_TOKENS.includes(el.name)) continue;  // filled by the CAPTCHA widget, reported separately
    // An input-select (Paylocity's Country and State) keeps its choice in its value element;
    // its required input stays empty, and the site's own script validates the choice.
    const widget = inputSelect(el);
    if (widget && !widget.placeholder) continue;
    let label = (el.labels && el.labels[0] ? el.labels[0].innerText : el.name || el.id || el.type);
    if (widget) label = label.replace(widget.shown.innerText || '', '');  // "Select a state" is not the question
    const line = label.replace(/\\s+/g, ' ').replace(/[\\s*:]+$/, '').trim() + ': ' + el.validationMessage;
    if (!out.includes(line)) out.push(line);
  }
  return out;
}"""

_EFFECTIVE_SUBMISSION = """(sel) => {""" + DEEP_QUERY + """
  const b = deepOne(sel);
  if (!b || !b.form) return null;
  const f = b.form;
  return {
    method: (b.hasAttribute('formmethod') ? b.formMethod : (f.method || 'get')).toLowerCase(),
    action: b.hasAttribute('formaction') ? b.formAction : f.action,
    target: (b.hasAttribute('formtarget') ? b.formTarget : f.target) || '',
    hasFile: !!f.querySelector('input[type=file]'),
    hasPassword: !!f.querySelector('input[type=password]'),
  };
}"""

_DOCUMENT_IDENTITY = "() => String(performance.timeOrigin) + ' ' + location.href"
"""Read-only identity of the loaded document; a navigation produces a new timeOrigin."""

_READY_POLL_S = 0.5
"""Interval between page readiness polls (an SPA rendering its form)."""
_FLICKER_S = 1.0
"""How long a page may show a passing state after a write (Greenhouse disables its
"Autofill my application" button while it handles a keystroke) before a difference from
the approved observation counts as a changed page."""
_FLICKER_POLL_S = 0.1
_READY_SETTLE_S = 5.0
_CLOSED_CONFIRM_S = 2.0
_TYPED_CONTROLS = re.compile(r"[\r\n\t]")
"""Characters a retype enters with ``fill``, never as key presses."""
"""How long closed wording must persist, without HTTP 404/410, to count as closed."""
"""Upper bound on the network/document settle inside one readiness wait."""
_OVERLAY_WAIT_S = 8.0
"""Upper bound on waiting out loading overlays (a "Loading..." autofill button, a busy
region) before acting on a page."""
_UPLOAD_WAIT_S = 20.0
"""Upper bound on waiting for one attached file's upload and for the page to settle
after it (a spinner, "Analyzing resume...", an autofill the upload triggers)."""
_UPLOAD_PROGRESS_S = 60.0
"""Upper bound on waiting while an uploader shows its own progress for the attached file
(a spinner, "Uploading resume..." in the upload's own field): Lever's resume upload."""
_UPLOAD_GRACE_S = 2.0
"""How long an upload is read again after its progress ended before the readback
decides (the file's name may show a moment later)."""
_QUIET_POLL_S = 0.3
"""Interval between reads while waiting for the page to settle."""
_RERENDER_WAIT_S = 3.0
"""Upper bound on waiting for a transient re-render (controls briefly missing, a busy
marker) to end before a write, or for a re-rendered control to come back."""
_PROMPT_WAIT_S = 3.0
"""Upper bound on waiting for a declined autofill offer to go away."""
_RETYPE_MAX_CHARS = 200
"""Longest text typed key by key again when a re-rendered control dropped it (longer
text is entered again as one input event)."""
_STEP_WAIT_S = 3.0
"""How long a clicked Next may take to replace a step rendered in place (a dialog)."""
_CAPTCHA_PASS_S = 10.0
"""How long a CAPTCHA page whose callback was called may take to lead on (a callback
that first posts the token and then navigates)."""
_KEPT_TEXT = "already shows this value; left as it is"
"""A pre-filled text that says the answer already: nothing is written, so the sweep
after the fill does not write it again either."""

_COOKIE_TEXT = re.compile(r"cookie", re.IGNORECASE)
_COOKIE_DECLINE = re.compile(
    r"^(?:(?:i )?decline(?: all| optional)?(?: cookies)?|reject(?: all| optional| non-essential)?(?: cookies)?|"
    r"(?:i )?(?:do not|don['\u2019]t) accept(?: all)?(?: cookies)?|(?:refuse|deny)(?: all)?(?: cookies)?|"
    r"continue without accepting|"
    r"only (?:necessary|essential)(?: cookies)?|(?:necessary|essential)(?: cookies)? only|"
    r"use necessary cookies only|accept (?:only )?(?:necessary|essential)(?: cookies)?(?: only)?)$",
    re.IGNORECASE,
)
"""Consent-banner buttons that decline non-essential cookies (OneTrust's "Reject All",
"Necessary cookies only", Upstart's "I do not accept"). Nothing else on a banner is ever
clicked: marketing cookies are never accepted, and a notice's "Got it"/"OK" is often the
accept button itself (OneTrust's ``#onetrust-accept-btn-handler``). A banner without a
decline stays for the person. Only a visible button that does not submit a form, navigate
or sit inside the selected application form is clicked, each at most once per document."""


@dataclass(frozen=True)
class ConfirmationTie:
    """What ties an acceptance page to *this* application."""

    external_job_id: str | None = None
    job_title: str | None = None
    known_references: frozenset[str] = frozenset()
    """Reference-like tokens already visible before the submit (not new evidence)."""

    @classmethod
    def from_job(cls, job: JobRecord) -> ConfirmationTie:
        return cls(external_job_id=job.external_job_id, job_title=job.title)

    @classmethod
    def from_identity(cls, identity: JobIdentityObservation | None, title: str | None) -> ConfirmationTie:
        return cls(
            external_job_id=identity.external_job_id if identity else None,
            job_title=(identity.title if identity and identity.title else title),
        )


@dataclass(frozen=True)
class ActionPolicy:
    """Who may operate the page in this session. A user-present session (e.g. the
    OpenCLI path) can keep navigation or submission for the user; the runtime then
    reports ``dispatched=False`` instead of acting."""

    automation_may_navigate: bool = True
    automation_may_submit: bool = True


@dataclass
class _PendingSubmit:
    dispatched: bool
    detail: str
    form: ApplicationForm | None = None
    tie: ConfirmationTie = field(default_factory=ConfirmationTie)
    marker: str | None = None
    invalid: list[str] = field(default_factory=list)
    next_state: NotSubmittedNext = NotSubmittedNext.FAILED_RETRYABLE
    evidence: list[EvidenceRef] = field(default_factory=list)
    resolved: bool = False


def _still_on(before: ApplicationForm, after: PageModel, by_progress: bool) -> bool:
    """The step just left is shown again: for a wizard identified by its progress bar,
    the bar has not moved (same scope); otherwise most of the step's fields are back."""
    if after.form is None:
        return False
    if by_progress and after.step_source == "progress":
        return after.form.scope.key == before.scope.key
    return _same_step_shown_again(before, after.form)


def _left_behind(before: PageModel, form: ApplicationForm, after: PageModel, by_progress: bool) -> bool:
    """The step ``form`` (read in ``before``) is still, or again, shown after Next. The
    page's own step indicator showing another step settles it: a wizard may reuse field
    ids on its next page (Workday's "Application Questions 1 of 2")."""
    step, now = before.snapshot.step, after.snapshot.step
    if step is not None and now is not None and now.current != step.current:
        return False
    return _still_on(form, after, by_progress)


def _validation_errors(form: ApplicationForm) -> list[str]:
    errors = [f"{f.label}: {f.validation_error}" for f in form.fields if f.validation_error]
    return errors + [e for e in form.page_errors if e not in errors]


def _field_signature(form: ApplicationForm) -> set[str]:
    return {f.id for f in form.fields}


def _same_step_shown_again(
    before: ApplicationForm, after: ApplicationForm | None, *, any_url: bool = False
) -> bool:
    """The same step is displayed again (typically re-rendered with errors). Field
    fingerprints may differ (a retained upload adds help text), ids do not. After a
    submit the re-rendered form often lives at the form's action URL (``any_url``)."""
    if after is None or (not any_url and after.scope.url != before.scope.url):
        return False
    ids_before, ids_after = _field_signature(before), _field_signature(after)
    if not ids_before:
        return not ids_after and after.is_final_step == before.is_final_step
    return len(ids_before & ids_after) * 2 >= len(ids_before)


@dataclass(frozen=True)
class _Upload:
    """A file this runtime attached whose uploader then emptied or replaced its input."""

    field: ApplicationField
    binding: FieldBinding
    anchor: str | None
    """Selector of the uploader's own container, taken before attaching (see FILE_ANCHOR)."""
    name: str
    document: str
    index: int = -1
    """Position of the question in the approved form."""
    step: int | None = None
    """The step it was approved on: a single-page wizard keeps one document for every
    step, and the file belongs to this step's uploader only."""


def _dial_code_chosen(form: ApplicationForm, model: PageModel, packet: ApplicationPacket,
                      field_id: str) -> str | None:
    """The dial code ("1") a separate country-code picker holds for this fill: the
    packet's answer for it, else what it showed when the step was inspected (Workday
    shows "United States of America (+1)" there already)."""
    picker = form.find(field_id)
    if picker is None:
        return None
    answer = packet.answer_for(field_id)
    label = ""
    if answer is not None and isinstance(answer.value, ChoiceValue):
        option = picker.option_for_value(answer.value.value)
        label = option.label if option is not None else answer.value.label or ""
    elif answer is not None and isinstance(answer.value, TextValue):
        label = answer.value.text
    code = dial_code(label)
    if code is None:
        binding = model.bindings.get(field_id)
        current = binding.value if binding is not None else ""
        option = picker.option_for_value(current) if picker.options else None
        code = dial_code(option.label if option is not None else current)
    return code


def _binding_shape(bindings: Mapping[str, FieldBinding]) -> dict[str, Any]:
    return {key: [b.selector, b.control_type.value, sorted(b.option_selectors.items()),
                  sorted(b.label_selectors.items())] for key, b in bindings.items()}


def _button_identity(button: DomButton) -> str:
    """A button as what it does (text, kind, form, request), not where it sits."""
    return json.dumps([button.text, button.type, button.form_index, button.submits_form,
                       button.form_no_validate, button.effective_method, button.effective_action])


def _guard_signature(model: PageModel, *, skip_buttons: Collection[str] = (),
                     skip_controls: Collection[str] = (), stable: bool = False,
                     ignore_required: bool = False, ignore_preceding: bool = False) -> str:
    """``observation_signature`` as the fill guard compares it. Buttons and the submit and
    next actions count by identity (text, kind, form, request), not by a positional
    selector that shifts when sibling blocks are replaced, and not by whether they are
    enabled (a submit enabled once required fields are valid). Validity, validation
    messages and what menus show are already left out. ``skip_*`` leave out the given
    buttons and controls (an uploader's own). New or removed questions, options, labels,
    bindings, actions and navigation still count. ``stable`` also leaves out selectors a
    re-render may regenerate (see ``observation_signature``); ``ignore_required`` also
    leaves out which controls are required (a requiredness another answer decides), and
    ``ignore_preceding`` the text before each control (``DomControl.preceding``, which a
    question inserted before it replaces; a label drawn from it is compared as the
    question's wording instead)."""
    identity = {b.selector: _button_identity(b) for b in model.snapshot.buttons}
    buttons = [b.model_copy(update={"disabled": False, "selector": identity[b.selector]})
               for b in model.snapshot.buttons if b.selector not in skip_buttons]
    controls = [c for c in model.snapshot.controls if c.selector not in skip_controls]
    # A toggle-button option's pressed state is the answer, like a checkbox's checked (our
    # own write), never structure.
    controls = [c.model_copy(update={"pressed_options": [o.model_copy(update={"pressed": False})
                                                         for o in c.pressed_options]})
                if c.pressed_options else c for c in controls]
    # What an input-select shows (its placeholder, then our choice) is its answer too.
    controls = [c.model_copy(update={"input_select": {}}) if c.input_select is not None else c
                for c in controls]
    if ignore_required:
        controls = [c.model_copy(update={"required": False}) if c.required else c for c in controls]
    if ignore_preceding:
        controls = [c.model_copy(update={"preceding": ""}) if c.preceding else c for c in controls]
    if stable:
        # Element paths this inspector reports beyond the ones observation_signature
        # leaves out: an option group's box, toggle-button options, an uploader's box.
        controls = [c.model_copy(update={
            "choice_group": "", "upload_anchor": "",
            "pressed_options": [o.model_copy(update={"selector": ""}) for o in c.pressed_options]})
            for c in controls]
    snapshot = model.snapshot.model_copy(update={"buttons": buttons, "controls": controls})
    inspection = model.inspection
    if model.form is not None:
        form = model.form
        inspection = inspection.model_copy(update={"form": form.model_copy(update={
            "submit_selector": identity.get(form.submit_selector or "", form.submit_selector),
            "next_selector": identity.get(form.next_selector or "", form.next_selector)})})
    return observation_signature(replace(model, snapshot=snapshot, inspection=inspection),
                                 stable=stable)


# Read-only: which of the given buttons lie inside the given uploader containers.
_BUTTONS_WITHIN = """(arg) => {
  const boxes = [];
  for (const anchor of arg.anchors) {
    let found = [];
    try { found = document.querySelectorAll(anchor); } catch (e) { found = []; }
    if (found.length === 1) boxes.push(found[0]);
  }
  return arg.selectors.map((s) => {
    let button = null;
    try { button = document.querySelector(s); } catch (e) { button = null; }
    return !!button && boxes.some((box) => box.contains(button));
  });
}"""


# Read-only: which of the given elements lie inside the popups the widget ``owner`` owns
# now (what its aria-controls/aria-owns name, or a menu button's data-menu-id), each taken
# up to its outermost ancestor that does not contain the owner: a portal of its own.
_IN_OWN_POPUP = """(arg) => {""" + DEEP_QUERY + """
  let owner = null;
  try { owner = deepOne(arg.owner); } catch (e) { owner = null; }
  const roots = [];
  if (owner) {
    // Ids resolve in the owner's own tree first (an open shadow root), then the document.
    const tree = owner.getRootNode();
    const ids = ['aria-controls', 'aria-owns'].flatMap((a) => (owner.getAttribute(a) || '').split(/\\s+/)).filter(Boolean);
    if (!ids.length && owner.getAttribute('data-menu-id')) ids.push(owner.getAttribute('data-menu-id'));
    for (const id of ids) {
      const popup = (tree.getElementById ? tree.getElementById(id) : null) || document.getElementById(id);
      if (!popup || popup.contains(owner)) continue;
      let root = popup;
      while (root.parentElement && root.parentElement !== document.body && !root.parentElement.contains(owner)) {
        root = root.parentElement;
      }
      roots.push(root);
    }
  }
  return arg.selectors.map((s) => {
    let el = null;
    try { el = deepOne(s); } catch (e) { el = null; }
    return !!el && roots.some((root) => root.contains(el));
  });
}"""


def _same_questions(approved: ApplicationForm, current: ApplicationForm) -> bool:
    return approved.scope.key == current.scope.key and (
        {f.id: f.fingerprint for f in approved.fields} == {f.id: f.fingerprint for f in current.fields})


def _question_change(before: PageModel, after: PageModel, attached: str) -> str | None:
    """Why the questions and bindings after our own upload to ``attached`` are no longer
    the ones the fill writes against, or None. The attached control's own description
    and binding may change (a file chip, a status line, a replaced input kept as approved,
    see ``_with_uploads``); every other question, option set and binding may not, and no
    question may appear or disappear."""
    old, new = before.form, after.form
    if old is None or new is None:
        return "the application form is no longer shown after the upload"
    if new.scope.key != old.scope.key:
        return "the form step changed after the upload"
    if [f.id for f in new.fields] != [f.id for f in old.fields]:
        return "questions appeared or disappeared after the upload"
    for was, now in zip(old.fields, new.fields, strict=True):
        ignore = {"semantic_type", "validation_error", "selector"}
        if was.id == attached:
            ignore |= {"help_text", "placeholder"}
        if was.model_dump(mode="json", exclude=ignore) != now.model_dump(mode="json", exclude=ignore):
            return f"the question {was.id!r} changed after the upload"
    shape = _binding_shape(after.bindings)
    for fid, was_shape in _binding_shape(before.bindings).items():
        now_shape = shape.get(fid)
        if fid == attached:
            if now_shape is None or now_shape[1] != was_shape[1]:
                return f"the control of {fid!r} changed after the upload"
        elif now_shape != was_shape:
            return f"the control of {fid!r} changed after the upload"
    return None


def _holds_application(model: PageModel, prompt: DomPrompt) -> bool:
    """Whether a shown dialog is the application itself: the dialog the form lives in
    (or one around it), or one holding a question the form binds."""
    if prompt.dialog_index < 0:
        return False
    parents = {d.index: d.parent for d in model.snapshot.dialogs}

    def within(index: int | None) -> bool:
        seen: set[int] = set()
        while index is not None and index >= 0 and index not in seen:
            if index == prompt.dialog_index:
                return True
            seen.add(index)
            index = parents.get(index)
        return False

    if within(model.dialog_index):
        return True
    bound = {b.selector for b in model.bindings.values()}
    return any(c.selector in bound and within(c.dialog_index) for c in model.snapshot.controls)


def _same_document_link(link: DomLink, page_url: str) -> bool:
    """A link that does not load another document ("#", "#apply", javascript:)."""
    target, page = urlsplit(link.href), urlsplit(page_url)
    if target.scheme == "javascript":
        return True
    return bool(link.href.endswith("#") or target.fragment) and (
        target.scheme, target.netloc, target.path, target.query) == (page.scheme, page.netloc, page.path, page.query)


def _entry_key(url: str) -> str:
    """An apply link's identity for "already taken": without query and fragment, since
    boards add per-load tracking parameters (LinkedIn's ``trackingId``)."""
    parts = urlsplit(url)
    return parts._replace(query="", fragment="").geturl()


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _shows_same(app_field: ApplicationField, shown: str, wanted: str) -> bool:
    """Whether a pre-filled text (a site filling in the person's profile) already says
    ``wanted``: exactly, an email in another case, or a phone number with the same
    digits or only the national part of ours (the site takes the country code in a
    field of its own). Never a value the site marks invalid."""
    if app_field.validation_error:
        return False
    if shown.replace("\r\n", "\n") == wanted.replace("\r\n", "\n"):
        return True
    if app_field.semantic_type is SemanticType.EMAIL or app_field.input_type == "email":
        return shown.strip().casefold() == wanted.strip().casefold()
    if app_field.semantic_type is SemanticType.PHONE or app_field.input_type == "tel":
        national, ours = _digits(shown), _digits(wanted)
        return national == ours or (len(national) >= 7 and ours.endswith(national)
                                    and 1 <= len(ours) - len(national) <= 3)
    return False


class GenericApplicationBrowser:
    """``ApplicationBrowser`` for accessible, native HTML forms (the generic adapter)."""

    def __init__(
        self,
        driver: PageDriver,
        options: BrowserOptions,
        *,
        settle_timeout_s: float = 15.0,
        poll_interval_s: float = 0.25,
        policy: ActionPolicy | None = None,
        on_close: Callable[[], Awaitable[None]] | None = None,
        annotator: FormAnnotator | None = None,
        schema_hint_loader: SchemaHintLoader | None = None,
        menus: MenuProbe | None = None,
    ) -> None:
        self.driver = driver
        self.options = options
        self.menus = menus or MenuProbe()
        """Probed menu controls of the current document (and the probing budget)."""
        self.probe_menus = True
        self._observing = False
        requested_policy = policy or ActionPolicy()
        self.policy = ActionPolicy(
            automation_may_navigate=requested_policy.automation_may_navigate,
            automation_may_submit=(requested_policy.automation_may_submit
                                   and options.allow_submission),
        )
        self.settle_timeout_s = settle_timeout_s
        self.poll_interval_s = poll_interval_s
        self._evidence = EvidenceRecorder(options.artifacts_dir, options.artifacts_root)
        self._on_close = on_close
        self.annotator = annotator
        self.schema_hint_loader = schema_hint_loader
        self.annotation_document_id: str | None = None
        self._observations: list[tuple[ApplicationForm, str, str]] = []
        self._filled_structure: str | None = None
        self._filled_stable: str | None = None
        """``_guard_signature(stable=True)`` of the step as the last fill left it: a later
        re-render that regenerated only selectors or generated ids is not a change."""
        self._active_fill_signature: str | None = None
        self._steps_advanced = 0
        self._last: PageModel | None = None
        self._identity: JobIdentityObservation | None = None
        self._posting_identity: JobIdentityObservation | None = None
        """What the page ``open()`` loaded first showed (a posting's identity), see
        ``_continuing_posting``."""
        self._filled: tuple[str, str] | None = None  # (scope key, form fingerprint)
        self._context_lost = False
        """The document was replaced mid-fill; fresh inspection and refill are required."""
        self._inspected_after_loss = False
        self._fill_document: str | None = None
        self._fill_bindings: dict[str, Any] = {}
        self._fill_form: ApplicationForm | None = None
        """The form the current (then the last) fill is authorized against."""
        self._uploader_buttons: frozenset[str] = frozenset()
        """Buttons of ``_fill_model`` inside the containers of this runtime's uploads."""
        self._uploader_helpers: frozenset[str] = frozenset()
        """Hidden controls of ``_fill_model`` inside those containers (see ``_helpers_within``)."""
        self._uploads: dict[str, _Upload] = {}
        """Files attached in this document whose input the uploader emptied or replaced."""
        self._pending: _PendingSubmit | None = None
        self._accepted = False
        self._declined: set[tuple[str, str]] = set()
        """(document origin, dialog text) of autofill offers already declined once."""
        self._stuck: set[tuple[str, str]] = set()
        """(document origin, busy indicator) still shown when a bounded wait ended: not
        waited for again in that document."""
        self._fill_model: PageModel | None = None
        """The page structure the current (then the last) fill writes against: the approved
        observation, re-read after this runtime's own uploads and re-renders."""
        self._stable_fill_signature: str | None = None
        self._reveal_source: ApplicationField | None = None
        """The choice control this fill wrote last (see ``_follow_ups``)."""
        self._follow_up_after: list[str] = []
        """Questions whose answers were followed by follow-up changes this fill took in
        (see ``_follow_ups``), for the page error that asks for a fresh inspection."""
        self._written_ids: set[str] = set()
        """Questions this fill has written its answer to (see ``_follow_ups``)."""
        self._attach_needed: dict[tuple[str, str], str] = {}
        """(document, resume field id) -> the pinned file the person has to attach there,
        because none of the resumes the site offers can be used and this session cannot
        attach files."""
        self._apply_clicked: set[tuple[str, str]] = set()
        """(document, selector) of apply controls clicked: never clicked twice."""
        self._consent_clicked: set[tuple[str, str]] = set()
        """(document, selector) of the cookie-banner buttons clicked: each at most once."""
        self._followed: set[str] = set()
        """Apply links and embedded application pages already opened by ``open``."""

    # --- inspection -----------------------------------------------------------------

    @property
    def attaches_files(self) -> bool:
        """Whether this session can attach files (OpenCLI's Browser Bridge cannot)."""
        return bool(getattr(self.driver, "attaches_files", True))

    def _build(self, snapshot: DomSnapshot) -> PageModel:
        needed = {field_id: name for (document, field_id), name in self._attach_needed.items()
                  if document == snapshot.document}
        return build_page(snapshot, fallback_step=self._steps_advanced,
                          http_status=self.driver.last_status,
                          attach_files=self.attaches_files, attach_needed=needed)

    async def _snapshot(self) -> DomSnapshot:
        """One read-only inspection, with the menus probed earlier in this document
        attached (probing itself happens only in ``_model``)."""
        last_error: Exception | None = None
        for _ in range(3):
            try:
                raw = await self.driver.evaluate(inspector_script())
                return self.menus.merge(DomSnapshot.model_validate(raw))
            except PageContextLost as exc:
                # A navigation replaced the document mid-read (the person signing in while
                # the runtime waits, a step loading): read the new document once it has
                # settled. Writers still notice the change through the document identity.
                last_error = exc
                await self.driver.settle(self.settle_timeout_s)
            except DriverError:
                raise
            except Exception as exc:  # e.g. the context was destroyed by a navigation
                last_error = exc
                await self.driver.settle(self.settle_timeout_s)
        if isinstance(last_error, PageContextLost):
            raise last_error
        raise DriverError(f"could not inspect the page: {last_error}")

    async def _probe(self, model: PageModel) -> bool:
        """Probe the application form's closed menu controls that are not cached yet
        (bounded; see ``MenuProbe``). True when the page was touched and must be re-read
        with every menu closed again."""
        if model.form is None:
            return False
        targets = self.menus.targets(model.snapshot, model.form_index, model.dialog_index)
        return bool(targets) and await self.menus.probe(self.driver, targets)

    async def _model(self, *, evidence: str | None = None, html: bool = False,
                     probe: bool = True) -> PageModel:
        # Menus are probed before semantic annotation, so every observation signature
        # below is computed with the menus closed again and probing is never taken for
        # a form change.
        probe = probe and self.probe_menus and not self._observing
        # Read identity on both sides of inspection and provider work. A semantic
        # answer is never applied to a document or binding the provider did not see.
        for _ in range(3):
            document = str(await self.driver.evaluate(_DOCUMENT_IDENTITY)) if self.annotator else ""
            snapshot = await self._snapshot()
            model = self._build(snapshot)
            if probe:
                probe = False
                if await self._probe(model):
                    snapshot = await self._snapshot()
                    model = self._build(snapshot)
            # This runtime's uploads are restored before annotation, so the provider
            # annotates, and later resolves reuse, the form this runtime returns.
            model = await self._with_uploads(model)
            if self.annotator is None or model.form is None:
                break
            if document != str(await self.driver.evaluate(_DOCUMENT_IDENTITY)):
                continue
            hints = (self.schema_hint_loader(detect_ats(snapshot.url), snapshot.url)
                     if self.schema_hint_loader else None)
            original = model.form.model_copy(deep=True)
            annotation_document = document + ":" + observation_signature(model)
            annotated = await asyncio.to_thread(
                self.annotator.annotate, model.form.model_copy(deep=True),
                document_id=annotation_document, schema_hints=hints,
            )
            form = semantic_only(original, annotated)
            fresh_snapshot = await self._snapshot()
            fresh = await self._with_uploads(self._build(fresh_snapshot))
            if (document != str(await self.driver.evaluate(_DOCUMENT_IDENTITY))
                    or observation_signature(model, include_values=True)
                    != observation_signature(fresh, include_values=True)):
                continue
            snapshot = fresh_snapshot
            model = replace(fresh, inspection=fresh.inspection.model_copy(update={"form": form}))
            self.annotation_document_id = annotation_document
            break
        else:
            raise PageContextLost("form changed repeatedly during semantic annotation; inspect again")
        refs: list[EvidenceRef] = []
        if evidence:
            refs = await self._evidence.capture(
                self.driver, evidence, description=f"{evidence} at {snapshot.url}",
                html=html, text=snapshot.body_text if html else None,
            )
        if refs:
            model = replace(model, inspection=model.inspection.model_copy(update={"evidence": refs}))
        if self.annotator is not None and model.form is not None:
            self._observations.append((model.form, document, _guard_signature(model)))
            self._observations = self._observations[-32:]
        if model.inspection.job_identity is not None:
            self._identity = model.inspection.job_identity
        self._last = model
        return model

    async def _with_uploads(self, model: PageModel) -> PageModel:
        """Keep a file question this runtime answered in this document as it was approved
        while its uploader shows that file: the uploader may replace the input with the
        file's name (Greenhouse unmounts the input and its Attach and cloud buttons), show
        the name beside it (Workable), or mount a fresh input that takes over the question
        (Teamtailor's Dropzone: the same question, bound to another element). The
        question is still on the page, answered; the name it shows is the answer, not new
        wording, and the new input is not a new control to attach to again."""
        form = model.form
        if not self._uploads or form is None:
            return model
        fields = list(form.fields)
        bindings = dict(model.bindings)
        changed = False
        for field_id, upload in sorted(self._uploads.items(), key=lambda item: item[1].index):
            if upload.document != model.snapshot.document or (
                    upload.step is not None and upload.step != form.step):
                continue
            present = next((i for i, f in enumerate(fields) if f.id == field_id), None)
            if (present is not None and fields[present].fingerprint == upload.field.fingerprint
                    and bindings.get(field_id) == upload.binding):
                continue
            if not await file_shown(self.driver, upload.binding.selector, upload.name,
                                    anchor=upload.anchor, wait_s=0.0, emptied=False):
                continue
            changed = True
            if present is not None:
                fields[present] = upload.field
                bindings[field_id] = upload.binding
                continue
            fields.insert(upload.index if 0 <= upload.index <= len(fields) else len(fields), upload.field)
            bindings[field_id] = upload.binding
        if not changed:
            return model
        restored = form.model_copy(update={"fields": fields})
        return replace(model, inspection=model.inspection.model_copy(update={"form": restored}),
                       bindings=bindings)

    def _evidence_label(self, kind: PageKind) -> str | None:
        if kind in USER_ACTION_PAGES or kind in (
            PageKind.ERROR, PageKind.UNKNOWN, PageKind.ALREADY_APPLIED, PageKind.JOB_CLOSED,
            PageKind.CONFIRMATION,
        ):
            return f"page-{kind.value.lower()}"
        return None

    async def inspect(self) -> PageInspection:
        if not self._observing:
            await self._clear_overlays()
        model = await self._model()
        if self._context_lost:
            self._inspected_after_loss = True
        label = self._evidence_label(model.inspection.kind)
        if label:
            model = await self._model(evidence=label)
        return model.inspection

    async def _await_ready(self, model: PageModel | None = None) -> PageModel:
        """Bounded readiness before classifying a freshly loaded document or step.

        A page that already classifies is used as it is. An ``UNKNOWN`` page (an SPA
        still rendering its form, a consent overlay) gets ``settle_timeout_s`` in
        total: the document and network settle first, then the page is re-read every
        ``_READY_POLL_S`` until it classifies, or until it stops changing between two
        consecutive reads while showing no loading indicator. Static pages therefore
        classify within a couple of seconds; nothing on the page is touched. So does a
        step that shows no question yet and only a forward action: Workday draws its
        progress list and "Next" before the step's questions."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settle_timeout_s
        if model is None:
            model = await self._model()
        if model.inspection.kind is PageKind.JOB_CLOSED:
            model = await self._confirm_closed(model, deadline)
        if not self._unsettled(model):
            return model
        await self.driver.settle(min(self.settle_timeout_s, _READY_SETTLE_S))
        model = await self._model()
        previous = self._readiness_key(model)
        while self._unsettled(model) and loop.time() < deadline:
            await asyncio.sleep(max(0.0, min(_READY_POLL_S, deadline - loop.time())))
            model = await self._model()
            key = self._readiness_key(model)
            if key == previous and not model.snapshot.loading_indicator:
                break
            previous = key
        return model

    async def _confirm_closed(self, model: PageModel, deadline: float) -> PageModel:
        """Closed wording is final only when the server says so (HTTP 404/410) or when it
        persists: an SPA may render "Job not found" before its data arrives, and an
        error fallback may use that wording. The page settles and is read again for
        ``_CLOSED_CONFIRM_S``; a later reading that is no longer closed wins."""
        if self.driver.last_status in (404, 410):
            return model
        loop = asyncio.get_running_loop()
        await self.driver.settle(max(0.0, min(_READY_SETTLE_S, deadline - loop.time())))
        end = min(deadline, loop.time() + _CLOSED_CONFIRM_S)
        while True:
            model = await self._model()
            if model.inspection.kind is not PageKind.JOB_CLOSED:
                return model
            if loop.time() >= end and not model.snapshot.loading_indicator:
                return model
            if loop.time() >= deadline:
                return model
            await asyncio.sleep(_READY_POLL_S)

    @staticmethod
    def _unsettled(model: PageModel) -> bool:
        """Still rendering, as far as the page shows: unclassified, or a step without a
        question whose primary action is not the final one (only a review step is empty)."""
        form = model.form
        return model.inspection.kind is PageKind.UNKNOWN or (
            form is not None and not form.fields and form.is_final_step is not True)

    @staticmethod
    def _readiness_key(model: PageModel) -> tuple[str, int, int]:
        return (observation_signature(model), len(model.snapshot.body_text),
                len(model.snapshot.controls))

    async def _raw_model(self) -> PageModel:
        """A structural read of the page: no menu probing, no semantic annotation (menus
        probed earlier and this runtime's own uploads kept, see ``_with_uploads``)."""
        snapshot = await self._snapshot()
        return await self._with_uploads(self._build(snapshot))

    async def _decline_autofill(self, model: PageModel) -> bool:
        """Decline one visible offer to autofill the application (a dialog saying
        "Autofill your application?") with its own decline control ("No thanks", "Not
        now", "Close"), at most once per offer and document, then wait (bounded) for it
        to go away. The offer's accept control is never clicked; ``observe`` never
        declines. A dialog that is (or holds) the application is never an offer, whatever
        it says ("Import from LinkedIn or fill out this form"): its Skip, Continue and
        Dismiss are the form's own, and only ``advance`` moves it on. Returns whether a
        click happened."""
        if self._observing or not model.snapshot.prompts:
            return False
        origin = model.snapshot.document.split(" ")[0]
        for prompt in model.snapshot.prompts:
            key = (origin, normalize_text(prompt.text)[:300])
            if key in self._declined or _holds_application(model, prompt):
                continue
            # A control that submits a form or loads another document declines nothing.
            selector = autofill_decline(prompt.text, [(b.text, b.selector) for b in prompt.buttons
                                                      if not (b.submits or b.navigates)])
            if selector is None:
                continue
            self._declined.add(key)
            try:
                await self.driver.click(selector)
            except DriverError:
                return False
            loop = asyncio.get_running_loop()
            deadline = loop.time() + min(_PROMPT_WAIT_S, self.settle_timeout_s)
            while loop.time() < deadline:
                await asyncio.sleep(0.1)
                snapshot = await self._snapshot()
                if all(normalize_text(p.text)[:300] != key[1] for p in snapshot.prompts):
                    break
            return True
        return False

    def _busy(self, model: PageModel) -> list[str]:
        """Busy indicators shown now, except those that outlasted a whole bounded wait in
        this document (a widget that never finishes loading is waited for only once)."""
        origin = model.snapshot.document.split(" ")[0]
        return [b for b in model.snapshot.busy if (origin, b) not in self._stuck]

    async def _await_quiet(self, timeout_s: float, *, values: bool) -> PageModel:
        """Read the page until nothing on it is busy and two consecutive reads agree
        (values included when ``values``: an autofill still writing), declining an
        autofill offer once if one appears. Bounded by ``timeout_s``; the last read is
        returned either way."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        previous: str | None = None
        while True:
            model = await self._raw_model()
            declined = await self._decline_autofill(model)
            busy = self._busy(model)
            quiet = not declined and not busy
            key = observation_signature(model, include_values=values)
            if quiet and key == previous:
                return model
            if loop.time() >= deadline:
                origin = model.snapshot.document.split(" ")[0]
                self._stuck.update((origin, b) for b in busy)
                return model
            previous = key if quiet else None
            await asyncio.sleep(max(0.0, min(_QUIET_POLL_S, deadline - loop.time())))

    async def _clear_overlays(self) -> bool:
        """Before acting on a page: decline an autofill offer once and, on an application
        form, wait (bounded) while loading overlays are shown (a page that is not a form
        yet is readiness's to wait for). One read when there is nothing to do. Returns
        whether the page was waited for or touched."""
        model = await self._raw_model()
        # A cookie banner that slid in after the page opened (OneTrust) covers controls:
        # its non-essential cookies are declined first.
        consent = await self._dismiss_cookie_banner(model)
        if consent:
            model = await self._raw_model()
        declined = await self._decline_autofill(model)
        if not declined and not (self._busy(model)
                                 and model.inspection.kind is PageKind.APPLICATION_FORM):
            return consent
        await self._await_quiet(min(self.settle_timeout_s, _OVERLAY_WAIT_S), values=False)
        return True

    async def _dismiss_cookie_banner(self, model: PageModel) -> bool:
        """Click one consent banner's decline button that lies outside the application
        form, then let the page settle. Returns whether a click happened. Nothing about
        the page is logged."""
        if not _COOKIE_TEXT.search(model.snapshot.body_text):
            return False
        candidates = [
            (b.text, b.selector) for b in model.snapshot.buttons
            if not b.submits_form and not b.disabled
            and (b.form_index == -1
                 or (model.form_index is not None and b.form_index != model.form_index))
        ]
        # A consent dialog's own controls (a link styled as a button among them), when the
        # dialog is about cookies and the control neither submits nor navigates.
        candidates += [(b.text, b.selector) for prompt in model.snapshot.prompts
                       if _COOKIE_TEXT.search(prompt.text)
                       for b in prompt.buttons if not b.submits and not b.navigates]
        button = next((selector for text, selector in candidates if _COOKIE_DECLINE.match(text.strip())), None)
        if button is None:
            return False
        key = (model.snapshot.document, button)
        if key in self._consent_clicked:
            return False  # a banner still shown after its button was clicked is left alone
        self._consent_clicked.add(key)
        try:
            await self.driver.click(button)
        except DriverError:
            return False
        await self.driver.settle(min(self.settle_timeout_s, _READY_SETTLE_S))
        return True

    async def prepare_review(self) -> PageInspection:
        """Read final-page evidence and native validity without clicking anything."""
        model = await self._model(evidence="prepared-review")
        form = model.form
        if form is None or form.is_final_step is not True:
            return model.inspection
        if self._context_lost or await self._changed_since_fill(form):
            raise ValueError("the final form changed; inspect and resolve it again")
        await self._assert_fill_context()
        invalid = await self.driver.evaluate(
            _NATIVE_VALIDITY, {"form": model.form_selector, "button": form.submit_selector}
        )
        invalid = [*invalid, *(f"{field_id}: required control is not completed"
                                for field_id in model.unsupported_pending)]
        await self._assert_fill_context()
        after = await self._model()
        if after.form is None or after.form.fingerprint != form.fingerprint:
            return after.inspection
        if await self._changed_since_fill(after.form):
            raise ValueError("the final form constraints changed; inspect and resolve it again")
        # Preserve final-review evidence while using the latest form observation.
        inspection = after.inspection.model_copy(update={"evidence": model.inspection.evidence})
        form = after.form
        if invalid:
            form = form.model_copy(update={"page_errors": [*form.page_errors, *invalid]})
            return inspection.model_copy(update={"form": form})
        return inspection

    @property
    def last_page(self) -> PageModel | None:
        """The most recent normalized page (for adapters and diagnostics)."""
        return self._last

    async def open(self, url: str) -> PageInspection:
        """Navigate to ``url`` exactly as supplied and follow the posting's own way to
        the application form: an embedded application page on a known ATS host (opened
        like a link), an apply link, or an apply control (a button that opens a dialog
        wizard or navigates, clicked at most once per document, never one that could
        send anything of the candidate's). How the form was reached is recorded in the
        inspection message."""
        self._steps_advanced = 0
        self._filled = None
        self._context_lost = False
        self._fill_document = None
        self._attach_needed = {}
        self._apply_clicked = set()
        self._followed = set()
        await self.driver.goto(url)
        model = await self._await_ready()
        consent_done = await self._dismiss_cookie_banner(model)
        if consent_done:
            model = await self._await_ready()
        posting_identity = self._posting_identity = model.inspection.job_identity
        notes: list[str] = []
        for _ in range(3):
            if model.inspection.kind is not PageKind.JOB_DESCRIPTION:
                break
            model = await self._settle_posting(model)
            posting = model
            followed = await self._follow_apply(model)
            if followed is None:
                break
            how, note = followed
            notes.append(note)
            model = await (self._await_ready() if how == "frame" else self._await_opened(posting))
            if how == "frame" and model.inspection.kind is not PageKind.APPLICATION_FORM:
                framed = await self._open_in_frame(posting)
                if framed is not None:
                    model = framed
                    notes.append("it does not load on its own, so it is operated inside the frame")
            if not consent_done:
                consent_done = await self._dismiss_cookie_banner(model)
                if consent_done:
                    model = await self._await_ready()
        if (model.inspection.kind in (PageKind.APPLICATION_FORM, PageKind.UNKNOWN)
                and await self._clear_overlays()):
            model = await self._await_ready()
        inspection = model.inspection
        label = self._evidence_label(inspection.kind)
        if label:
            inspection = (await self._model(evidence=label)).inspection
        if inspection.job_identity is None and posting_identity is not None:
            inspection = inspection.model_copy(update={"job_identity": posting_identity})
        if notes and inspection.kind not in USER_ACTION_PAGES:
            # How the form (or the page instead of it) was reached; a page that asks the
            # person to act (sign in, a consent, a CAPTCHA) keeps the site's own message.
            lead = ("Reached the form: " if inspection.kind is PageKind.APPLICATION_FORM
                    else "Taken from the posting (no application form yet): ")
            reached = lead + "; then ".join(notes) + "."
            inspection = inspection.model_copy(update={
                "message": " ".join(m for m in (inspection.message, reached) if m)})
        return inspection

    async def observe(self, url: str) -> PageInspection:
        """Open precisely this URL and classify it without following or acting on forms
        (menus are not expanded either)."""
        self._observing = True
        try:
            await self.driver.goto(url)
            await self._settle_posting(await self._await_ready())
            return await self.inspect()
        finally:
            self._observing = False

    async def _settle_posting(self, model: PageModel) -> PageModel:
        """A posting whose only ways onwards are clicks gets one bounded settle first:
        careers pages inject the ATS's application frame after load (Greenhouse's embed
        script), and that frame is followed instead of clicking anything."""
        if (model.inspection.kind is not PageKind.JOB_DESCRIPTION or model.application_frame is not None
                or any(kind in ("frame", "link") for kind, _ in self._entries(model))):
            return model
        await self.driver.settle(min(self.settle_timeout_s, _READY_SETTLE_S))
        return await self._model()

    def _entries(self, model: PageModel) -> list[tuple[str, Any]]:
        """The ways onwards this posting offers that ``open`` has not taken yet, best
        first: its embedded application page, apply links, then apply controls."""
        document = model.snapshot.document
        entries: list[tuple[str, Any]] = []
        frame = model.application_frame
        if frame is not None and _entry_key(frame.src) not in self._followed:
            entries.append(("frame", frame))
        for link in model.snapshot.links:
            if not APPLY_LINK.search(link.text):
                continue
            if _same_document_link(link, model.snapshot.url):
                if (document, link.selector) not in self._apply_clicked:
                    entries.append(("script-link", link))
            elif _entry_key(link.href) not in self._followed:
                entries.append(("link", link))
        entries += [("button", b) for b in model.apply_controls
                    if not b.disabled and (document, b.selector) not in self._apply_clicked]
        # An apply-options chooser (Workday: "Autofill with Resume", "Apply Manually", "Use
        # My Last Application") is answered with its manual route, link or control, before
        # any other: autofill uploads and parses the resume before any question is shown,
        # and the resume is attached later on its own question.
        manual = [entry for entry in entries
                  if entry[0] != "frame" and MANUAL_APPLY.search(entry[1].text)]
        return ([entry for entry in entries if entry[0] == "frame"] + manual
                + [entry for entry in entries if entry[0] != "frame" and entry not in manual])

    async def _follow_apply(self, model: PageModel) -> tuple[str, str] | None:
        """Take one step from a posting towards its application form. Returns how
        (``frame`` or ``link``: a navigation; ``click``: an apply control was clicked)
        and a note for the inspection message, or None when there is nothing to take."""
        document = model.snapshot.document
        for kind, entry in self._entries(model):
            if kind == "frame":
                # Before anything is clicked: the form lives in an embedded ATS page (often
                # in a hidden "Application" tab) that the top document cannot see into.
                self._followed.add(_entry_key(entry.src))
                await self.driver.goto(entry.src)
                return "frame", f"opened the application page embedded in {model.snapshot.url} ({entry.src})"
            if kind == "link":
                self._followed.add(_entry_key(entry.href))
                await self.driver.goto(entry.href)
                return "link", f"followed the apply link {entry.text!r}"
            # A script link ("#", javascript:) acts only when clicked. A control that
            # submits a form is clicked only when the submission sends nothing of the
            # candidate's (see ``_navigation_form``).
            if kind == "button" and entry.submits_form and not await self._navigation_form(model, entry):
                continue
            self._apply_clicked.add((document, entry.selector))
            await self.driver.click(entry.selector)
            await self.driver.settle(self.settle_timeout_s)
            return "click", f"clicked {entry.text!r}"
        return None

    async def _navigation_form(self, model: PageModel, button: DomButton) -> bool:
        """A form whose apply button may be clicked (a posting page wrapped in one form,
        as legacy portals do): it has at most one fillable question, every typed field
        in it is empty and optional (a job-alert email box, a search box), and it has no
        file or password input and would not open elsewhere. Submitting it then sends
        nothing of the candidate's; it only navigates."""
        if model.fillable_counts.get(button.form_index, 0) > 1:
            return False
        typed = [c for c in model.snapshot.controls
                 if c.form_index == button.form_index and not c.disabled and (c.visible or c.label_visible)
                 and (c.tag == "textarea" or (c.kind == "native" and c.type in TEXT_INPUT_TYPES)
                      or c.kind == "custom")]
        if any(c.required or c.value.strip() or c.has_value for c in typed):
            return False
        effective = await self.driver.evaluate(_EFFECTIVE_SUBMISSION, button.selector)
        return (isinstance(effective, dict) and not effective.get("hasFile")
                and not effective.get("hasPassword") and effective.get("target") in ("", "_self"))

    async def _await_opened(self, posting: PageModel) -> PageModel:
        """After an apply link or control was taken: wait, within ``settle_timeout_s``,
        while the page is still a posting with no new way onwards (a dialog wizard still
        opening, LinkedIn's apply URL opening its dialog after load, a client-side route
        that lands 6 to 10 s after the click as on Dayforce), then let it get ready. A
        page still showing only the taken entries at the deadline is returned as it is."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settle_timeout_s
        model = await self._model()
        while model.inspection.kind is PageKind.JOB_DESCRIPTION and loop.time() < deadline:
            entries = self._entries(model)
            if any(kind == "frame" for kind, _ in entries):
                break  # an embedded application page appeared: it is followed, not waited for
            if model.snapshot.document != posting.snapshot.document and entries:
                break  # another page, with a way onwards of its own
            await asyncio.sleep(max(0.0, min(_READY_POLL_S, deadline - loop.time())))
            model = await self._model()
        return await self._await_ready(model) if self._unsettled(model) else model

    async def _open_in_frame(self, posting: PageModel) -> PageModel | None:
        """The embedded application page did not show a form when opened on its own
        (it requires its careers page): open the careers page again and operate the page
        inside its frame, when the driver can (Playwright). None otherwise."""
        frame = posting.application_frame
        enter = getattr(self.driver, "enter_frame", None)
        if frame is None or enter is None:
            return None
        await self.driver.goto(posting.snapshot.url)
        model = await self._await_ready()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settle_timeout_s
        revealed = False
        while True:
            shown = model.application_frame
            if shown is not None and shown.src == frame.src and shown.visible:
                break
            if not revealed and shown is not None:
                # The frame sits in a hidden "Application" panel: its apply control shows it.
                revealed = True
                button = next((b for b in model.apply_controls if not b.disabled and not b.submits_form), None)
                if button is not None:
                    await self.driver.click(button.selector)
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(_READY_POLL_S)
            model = await self._model()
        try:
            await enter(frame.src)
        except DriverError:
            return None
        self.menus.reset()
        model = await self._await_ready()
        return model if model.inspection.kind is PageKind.APPLICATION_FORM else None

    # --- fill -----------------------------------------------------------------------

    async def fill(self, form: ApplicationForm, packet: ApplicationPacket) -> FillResult:
        return await self._fill(form, packet, None)

    async def fill_fields(
        self, form: ApplicationForm, packet: ApplicationPacket, field_ids: Sequence[str]
    ) -> FillResult:
        """``SelectiveFill``: the checks of ``fill``, but only the listed fields that the
        packet answers are operated and read back (after a lookup choice); every other
        control is left exactly as it is and only those fields are reported."""
        unknown = [field_id for field_id in field_ids if form.find(field_id) is None]
        if unknown:
            raise ValueError("fields are not on this form: " + ", ".join(unknown))
        return await self._fill(form, packet, frozenset(field_ids))

    async def _fill(self, form: ApplicationForm, packet: ApplicationPacket,
                    only: frozenset[str] | None) -> FillResult:
        if self._context_lost and not self._inspected_after_loss:
            raise ValueError("the page changed while filling; inspect it again first")
        problems = packet.problems_against(form)
        if problems:
            raise ValueError("packet does not fit this form: " + "; ".join(problems))
        # A loading overlay (an autofill button reading "Loading...") or an offer to
        # autofill the application is dealt with before the page is compared with the
        # inspection the packet answered.
        await self._clear_overlays()
        issued = next((item for item in self._observations if item[0] is form), None)
        model = await self._model()
        current = model.form
        if self.annotator is not None:
            document = str(await self.driver.evaluate(_DOCUMENT_IDENTITY))
            if (issued is None or issued[1] != document
                    or issued[2] != _guard_signature(model)):
                raise ValueError("the annotated document or field bindings changed; re-inspect and resolve")
        if current is None or current.fingerprint != form.fingerprint:
            raise ValueError(
                "the page no longer shows the inspected form step; re-inspect and resolve again"
            )
        held = self._unusable_resumes(form, packet, model, only)
        if held:
            # Nothing was written: the next inspection hands these fields to the person.
            raise ValueError("the resume must be attached in the browser window ("
                             + "; ".join(held) + "); re-inspect the step")
        self._fill_document = str(await self.driver.evaluate(_DOCUMENT_IDENTITY))
        errors_before = set(current.page_errors)
        results: dict[str, FieldFillResult] = {}
        written: dict[str, str] = {}
        lost: PageContextLost | None = None
        halted: str | None = None
        halt_label = "changed-after-upload"
        halt_description = "page after an upload changed its questions"
        accepted = current
        await self._set_fill_model(model, form)
        self._reveal_source = None
        self._follow_up_after = []
        self._written_ids = set()
        try:
            for app_field in self._fill_order(form, packet):
                answer = packet.answer_for(app_field.id)
                if only is not None and (app_field.id not in only or answer is None):
                    continue  # a selective fill never touches or reports other controls
                if halted is not None:
                    continue  # re-inspected and resolved again before anything else is written
                if lost is not None:
                    if answer is not None:
                        results[app_field.id] = FieldFillResult(
                            field_id=app_field.id, status=FieldFillStatus.FAILED,
                            detail="not attempted: the page changed while filling an earlier field")
                    continue
                try:
                    await self._assert_fill_context()
                    value = answer.value if answer is not None else None
                    if isinstance(value, TextValue):
                        value = self._national_number(form, packet, app_field.id, value)
                    result, touched = await self._operate(app_field, value)
                    results[app_field.id] = result
                    if answer is not None:
                        self._written_ids.add(app_field.id)
                    # A choice just written may reveal follow-up questions (named in the page
                    # error that asks for a fresh inspection once the approved answers are in).
                    self._reveal_source = (
                        app_field if answer is not None and app_field.control_type in _CHOICE_CONTROLS
                        and result.status is FieldFillStatus.FILLED else None)
                    if touched:
                        halted, accepted = await self._settle_after_upload(app_field.id, accepted)
                    if (result.status is FieldFillStatus.FILLED
                            and result.detail != _KEPT_TEXT
                            and isinstance(value, TextValue) and self._plain_text(app_field)):
                        written[app_field.id] = value.text
                    await self._assert_fill_context()
                except _ConditionalReveal as exc:
                    # A choice changed a question not written yet (it became required, or
                    # its help text changed): that answer waits for a fresh inspection; the
                    # answers written so far, the choice included, stay in place.
                    halted = str(exc)
                    halt_label, halt_description = "questions-changed", (
                        "page after a choice changed a later question")
                except PageContextLost as exc:
                    # The document or approved questions changed. Every remaining
                    # answer is reported without writing through stale bindings.
                    lost = exc
                    self._context_lost = True
                    self._inspected_after_loss = False
                    self.menus.reset()
                    results[app_field.id] = FieldFillResult(
                        field_id=app_field.id, status=FieldFillStatus.FAILED, detail=str(exc))
                except DriverError as exc:  # per-field, page still the same: keep going
                    results[app_field.id] = FieldFillResult(
                        field_id=app_field.id, status=FieldFillStatus.FAILED, detail=str(exc))
            if lost is None and halted is None and written:
                try:
                    await self._sweep(accepted, written, results)
                except _ConditionalReveal as exc:
                    halted = str(exc)
                    halt_label, halt_description = "questions-changed", (
                        "page after a choice changed a later question")
                except PageContextLost as exc:
                    lost = exc
                    self._context_lost = True
                    self._inspected_after_loss = False
                    self.menus.reset()
            structure = self._active_fill_signature or _guard_signature(model)
            stable_structure = self._stable_fill_signature or _guard_signature(model, stable=True)
        finally:
            self._active_fill_signature = None
            self._stable_fill_signature = None
        ordered = [results[f.id] for f in form.fields if f.id in results]
        if lost is not None:
            self._filled = None
            self._context_lost = True
            evidence: list[EvidenceRef] = []
            with contextlib.suppress(DriverError):
                evidence = await self._evidence.capture(
                    self.driver, f"context-lost-step-{form.step}",
                    description="page after its document or approved questions changed mid-fill")
            return FillResult(
                form_step=form.step, fields=ordered, evidence=evidence,
                page_errors=[f"{lost}; the step must be inspected and resolved again"],
            )
        if halted is not None:
            # Our own upload changed the step (new questions, other constraints), or a choice
            # changed a question not written yet. The file stays attached (it is not attached
            # again) and the choice stays answered; the rest waits for a fresh inspection and
            # packet.
            self._filled = None
            self._context_lost = True
            self._inspected_after_loss = False
            evidence = []
            with contextlib.suppress(DriverError):
                evidence = await self._evidence.capture(
                    self.driver, f"{halt_label}-step-{form.step}", description=halt_description)
            return FillResult(
                form_step=form.step, fields=ordered, evidence=evidence,
                page_errors=[f"{halted}; inspect this step and resolve it again before continuing"],
            )
        self._context_lost = False
        self._inspected_after_loss = False
        # The packet authorized exactly the inspected questions; that stays the authority
        # (an attached control's own description may have changed with its file chip).
        self._filled = (form.scope.key, accepted.fingerprint)
        self._filled_structure = structure
        self._filled_stable = stable_structure
        # Let a passing state from the last write settle before the page is read again.
        await self._settled_model(self._filled_structure)
        label = f"filled-step-{form.step}" if only is None else f"filled-step-{form.step}-again"
        after = self._as_fill_ids(await self._model(evidence=label))
        await self._assert_fill_context()
        new_errors = [e for e in (after.form.page_errors if after.form else []) if e not in errors_before]
        if (after.form is None or after.form.fingerprint != accepted.fingerprint
                or await self._changed_since_fill(after.form)):
            base = self._fill_model
            delta = _question_delta(accepted, after.form) if after.form is not None else None
            as_base = base is not None and (
                _guard_signature(after, stable=True) == _guard_signature(base, stable=True)
                or await self._only_uploads_changed(after))
            since = None if as_base or base is None else self._follow_ups(after, base)
            follow_ups = delta is not None and delta.follow_ups and (as_base or since is not None)
            ordered.extend(await self._contain_changed_questions(accepted, after, follow_ups=follow_ups))
            if follow_ups and delta is not None:
                # Follow-up questions appeared, became required or changed their help text
                # (after a choice, or rendered late): the approved answers are written; the
                # step is inspected and resolved again with every answer in place.
                self._filled = None
                self._context_lost = True
                self._inspected_after_loss = False
                if since is not None and self._reveal_source is not None:
                    self._follow_up_after.append(self._reveal_source.label)
                when = (f"after the answer to {_short_label(self._follow_up_after[0])!r}"
                        if self._follow_up_after else "while filling")
                new_errors.append(f"{delta.describe()} {when}; inspect this step and resolve it again "
                                  "before continuing")
            else:
                new_errors.append(
                    f"questions on this step changed while filling ({self._change_summary(after)}); "
                    "re-inspect and resolve again before continuing"
                )
        return FillResult(
            form_step=form.step,
            fields=ordered,
            page_errors=new_errors,
            evidence=after.inspection.evidence,
        )

    @staticmethod
    def _fill_order(form: ApplicationForm, packet: ApplicationPacket) -> list[ApplicationField]:
        """Answered file controls first (their uploads may make the site autofill other
        fields), then every other field in document order."""
        files = [f for f in form.fields if f.control_type is ControlType.FILE
                 and (a := packet.answer_for(f.id)) is not None and isinstance(a.value, FileValue)]
        return [*files, *(f for f in form.fields if f not in files)]

    def _plain_text(self, app_field: ApplicationField) -> bool:
        """Text typed and read back exactly as written. A segmented date is not: its
        boxes read back in the widget's own shape ("09/24/2026")."""
        model = self._fill_model
        binding = model.bindings.get(app_field.id) if model is not None else None
        return (app_field.control_type in (ControlType.TEXT, ControlType.TEXTAREA)
                and not app_field.expects_international_phone
                and not (binding is not None and binding.date_segments))

    def _national_number(self, form: ApplicationForm, packet: ApplicationPacket,
                         field_id: str, value: TextValue) -> TextValue:
        """A phone number whose country code is chosen in its own picker (Workday's
        "Country Phone Code") goes into the number box as the national number."""
        model = self._fill_model
        binding = model.bindings.get(field_id) if model is not None else None
        if model is None or binding is None or not binding.dial_code_field:
            return value
        code = _dial_code_chosen(form, model, packet, binding.dial_code_field)
        national = national_number(value.text, code)
        return TextValue(text=national) if national is not None else value

    async def _set_fill_model(self, model: PageModel, form: ApplicationForm | None = None) -> None:
        """The structure the fill writes against from now on: its bindings, the guard
        signatures and the buttons its own uploaders hold (``form``: the form it is
        authorized against, when that changes too)."""
        self._fill_model = model
        if form is not None:
            self._fill_form = form
        self._fill_bindings = _binding_shape(model.bindings)
        self._active_fill_signature = _guard_signature(model)
        self._stable_fill_signature = _guard_signature(model, stable=True)
        self._uploader_buttons = await self._buttons_within(model, self._upload_anchors(model))
        self._uploader_helpers = await self._helpers_within(model, self._upload_anchors(model))

    def _current_binding(self, field_id: str) -> FieldBinding:
        model = self._fill_model
        assert model is not None
        binding = model.bindings.get(field_id)
        if binding is None:
            raise PageContextLost(f"{field_id} is no longer on the page; re-inspect before continuing")
        return binding

    async def _operate(self, app_field: ApplicationField,
                       value: Any | None) -> tuple[FieldFillResult, bool]:
        """Operate one field with ``value`` (None: leave it unanswered) through its
        current binding, again (at most twice more) when a re-render regenerated the
        selectors right before a write. Returns the result and whether an upload
        touched the page."""
        for _ in range(3):
            binding = self._current_binding(app_field.id)
            try:
                if value is None:
                    return await self._leave_unanswered(app_field, binding), False
                if isinstance(value, FileValue) and app_field.control_type is ControlType.FILE:
                    return await self._attach(app_field, binding, value)
                return await self._apply(app_field, binding, value), False
            except _Rebound:
                continue
        raise PageContextLost("the form kept re-rendering while filling; re-inspect before continuing")

    async def _assert_fill_context(self) -> None:
        if self._fill_document is None:
            return
        try:
            current = str(await self.driver.evaluate(_DOCUMENT_IDENTITY))
        except DriverError as exc:
            self._context_lost = True
            self._inspected_after_loss = False
            self.menus.reset()
            raise PageContextLost("cannot verify the filling document; re-inspect before continuing") from exc
        if current != self._fill_document:
            self._context_lost = True
            self._inspected_after_loss = False
            self.menus.reset()
            raise PageContextLost("the page changed while filling; re-inspect before continuing")

    async def _assert_fill_freshness(self, *, rebind: bool = False, widget: str | None = None) -> bool:
        """Check trusted DOM constraints before every mutation, never reclassify.

        This also runs between individual checkbox-group writes. An earlier input
        handler may replace a later question without replacing the document.

        A difference is first given ``_FLICKER_S`` to pass (a passing state after a
        keystroke). A transient state is then waited out (bounded) instead of aborting: a
        re-render that briefly removes the form's controls or marks it busy, and an offer
        to autofill the application (declined once). With ``rebind`` (the check before a
        write), a
        re-render that only regenerated selectors (the same questions, constraints,
        options, actions and context) is re-read and True is returned: the write must
        then go through the control re-resolved by its field id, never a stale selector.
        With ``widget`` (the check between the steps of operating that control), a
        difference confined to the popup the widget owns (its menu or suggestion list and
        that list's own container) is tolerated. Selectors that the mounted popup shifts
        are tolerated as well; the approved observation stays as it is.

        A question a re-render gave another generated id is the same question (see
        ``_renamed_questions``; the page is always read with it under its known id). Follow-up
        changes (``_follow_ups``: questions that appeared anywhere, questions that became
        required or optional, a question's changed help text) become the structure the fill
        writes against, and the fill goes on with the approved answers; the readback of the
        step then asks for it to be inspected and resolved again.
        Anything else aborts the fill.
        """
        await self._assert_fill_context()
        if self._active_fill_signature is None:
            return False
        fresh = await self._settled_model(self._active_fill_signature)
        await self._assert_fill_context()
        if _guard_signature(fresh) == self._active_fill_signature:
            return False
        if await self._dismiss_cookie_banner(fresh):
            # A cookie banner slid in mid-fill (OneTrust, over the bottom of the page): its
            # non-essential cookies are declined and the page read again.
            fresh = await self._settled_model(self._active_fill_signature)
            await self._assert_fill_context()
            if _guard_signature(fresh) == self._active_fill_signature:
                return False
        if await self._only_uploads_changed(fresh):
            # Our own upload re-rendered its uploader later (Greenhouse, seconds after
            # the attach): the page as it now is becomes the approved one.
            await self._set_fill_model(fresh)
            return False
        fresh = await self._outlast_transient(fresh)
        if _guard_signature(fresh) == self._active_fill_signature:
            return False
        if await self._only_uploads_changed(fresh):
            await self._set_fill_model(fresh)
            return False
        if rebind and _guard_signature(fresh, stable=True) == self._stable_fill_signature:
            await self._set_fill_model(fresh)
            return True
        if widget is not None and await self._only_own_popup_changed(fresh, widget):
            # Ashby mounts a lookup's suggestion list in a portal of its own while it is
            # operated: the page returns to the approved observation once it closes.
            return False
        base = self._fill_model
        follow_ups = self._follow_ups(fresh, base) if base is not None else None
        if follow_ups is not None and base is not None:
            changed_later = [q for q in (*follow_ups.toggled, *follow_ups.helped) if q.id not in self._written_ids]
            if changed_later and self._reveal_source is not None:
                # BambooHR marks the sponsorship question required once work authorization is
                # answered: its answer waits for a fresh inspection of the step.
                raise _ConditionalReveal(
                    f"{follow_ups.describe()} after the answer to {_short_label(self._reveal_source.label)!r}")
            if widget is None or self._still_bound(widget, base, fresh):
                # BambooHR and Greenhouse show follow-up questions once a Yes/No is chosen,
                # and Teamtailor renders a question late: the approved questions are all still
                # there, so their answers are written; the step is inspected and resolved
                # again once they are.
                if self._reveal_source is not None:
                    self._follow_up_after.append(self._reveal_source.label)
                await self._set_fill_model(fresh)
                return rebind
        raise PageContextLost(
            "questions, bindings, actions, or employer context changed while filling "
            f"({self._change_summary(fresh)}); remaining answers were not attempted; "
            "re-inspect and resolve again"
        )

    @staticmethod
    def _still_bound(widget: str, base: PageModel, fresh: PageModel) -> bool:
        """The control being operated is still bound where it was, to the same question."""
        field_id = next((k for k, b in base.bindings.items() if b.selector == widget), None)
        binding = fresh.bindings.get(field_id) if field_id is not None else None
        return binding is not None and binding.selector == widget

    async def _check_before_action(self, widget: str | None = None) -> None:
        """The freshness check between the steps of one widget operation (opening a
        menu, choosing its option). Apart from the widget's own popup, nothing may
        change there."""
        await self._assert_fill_freshness(widget=widget)

    async def _only_own_popup_changed(self, fresh: PageModel, widget: str) -> bool:
        """Whether ``fresh`` differs from the approved observation only inside the popup
        that ``widget`` owns now (``_IN_OWN_POPUP``: a suggestion list, its portal) and in
        the selectors that popup shifts while it is mounted. The widget itself must still
        be bound where it was."""
        if self._stable_fill_signature is None or widget not in {c.selector for c in fresh.snapshot.controls}:
            return False
        controls = [c.selector for c in fresh.snapshot.controls]
        buttons = [b.selector for b in fresh.snapshot.buttons]
        try:
            inside = await self.driver.evaluate(
                _IN_OWN_POPUP, {"owner": widget, "selectors": [*controls, *buttons]})
        except PageContextLost:
            raise
        except DriverError:
            return False  # unknown: nothing is tolerated
        if not isinstance(inside, list) or len(inside) != len(controls) + len(buttons):
            return False
        own_controls = {s for s, flag in zip(controls, inside[:len(controls)], strict=True) if flag}
        own_buttons = {s for s, flag in zip(buttons, inside[len(controls):], strict=True) if flag}
        return (_guard_signature(fresh, skip_controls=own_controls, skip_buttons=own_buttons, stable=True)
                == self._stable_fill_signature)

    def _transient(self, model: PageModel) -> bool:
        """The page looks mid re-render: the form or some of the fill's controls are
        missing, or something is busy."""
        base = self._fill_model
        return (model.form is None or bool(self._busy(model))
                or (base is not None and any(fid not in model.bindings for fid in base.bindings)))

    async def _outlast_transient(self, fresh: PageModel) -> PageModel:
        """Wait (bounded) while ``fresh`` looks transient or shows an autofill offer,
        until the page reads as the fill's structure again (or stops looking transient)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(self.settle_timeout_s, _RERENDER_WAIT_S)
        while True:
            declined = await self._decline_autofill(fresh)
            if not declined and not self._transient(fresh):
                return fresh
            if loop.time() >= deadline:
                return fresh
            await asyncio.sleep(0.15)
            await self._assert_fill_context()
            fresh = await self._fresh_model()
            if _guard_signature(fresh) == self._active_fill_signature:
                return fresh

    def _as_fill_ids(self, model: PageModel) -> PageModel:
        """``model`` with the questions a re-render gave another generated id under the
        ids the fill (then the last fill) knows them by (``_renamed_questions``)."""
        base = self._fill_model
        if (base is None or base.form is None or model.form is None
                or model.form.scope.key != base.form.scope.key):
            return model
        return _rekeyed(model, _renamed_questions(base.form.fields, model.form.fields))

    async def _fresh_model(self) -> PageModel:
        """The page as the fill guard sees it now (probed menus and own uploads kept,
        renamed questions under their known ids)."""
        try:
            return self._as_fill_ids(await self._raw_model())
        except DriverError as exc:
            raise PageContextLost(
                "cannot re-observe field constraints before writing; re-inspect before continuing"
            ) from exc

    async def _settled_model(self, expected: str) -> PageModel:
        """The page once it shows the ``expected`` observation again, or after
        ``_FLICKER_S``: input handlers may change page state for a moment. Only a
        difference that persists is a change."""
        loop = asyncio.get_running_loop()
        end = loop.time() + _FLICKER_S
        fresh = await self._fresh_model()
        while _guard_signature(fresh) != expected and loop.time() < end:
            await asyncio.sleep(_FLICKER_POLL_S)
            fresh = await self._fresh_model()
        return fresh

    def _upload_anchors(self, model: PageModel) -> list[str]:
        return [u.anchor for u in self._uploads.values()
                if u.anchor and u.document == model.snapshot.document]

    async def _buttons_within(self, model: PageModel, anchors: Sequence[str]) -> frozenset[str]:
        """Selectors of ``model``'s buttons that lie inside the given uploader containers
        now (read-only; only meaningful while the page still matches ``model``)."""
        return await self._within(anchors, [b.selector for b in model.snapshot.buttons])

    async def _helpers_within(self, model: PageModel, anchors: Sequence[str]) -> frozenset[str]:
        """Selectors of ``model``'s hidden controls (never shown, so never questions) inside
        the given uploader containers now: an uploader's own inputs, such as the fresh
        input Dropzone mounts after taking a file and a preview's field for the stored
        file's URL (Teamtailor)."""
        return await self._within(anchors, [c.selector for c in model.snapshot.controls if not c.visible])

    async def _within(self, anchors: Sequence[str], selectors: list[str]) -> frozenset[str]:
        """Which of ``selectors`` lie inside the given uploader containers now (read-only)."""
        if not anchors or not selectors:
            return frozenset()
        try:
            inside = await self.driver.evaluate(_BUTTONS_WITHIN, {"anchors": list(anchors), "selectors": selectors})
        except PageContextLost:
            raise
        except DriverError:
            return frozenset()  # unknown: nothing is tolerated
        if not isinstance(inside, list) or len(inside) != len(selectors):
            return frozenset()
        return frozenset(s for s, flag in zip(selectors, inside, strict=True) if flag)

    async def _guard_equal_but_uploads(self, approved: PageModel, fresh: PageModel) -> bool:
        """``approved`` and ``fresh`` read the same to the fill guard once the buttons and
        controls of this runtime's own uploaders are left out: the Submit control by what
        it is (text, kind, form, request), not by where it sits or whether it is enabled."""
        uploaded = {u.binding.selector for u in self._uploads.values()
                    if u.document == fresh.snapshot.document}
        anchors = self._upload_anchors(fresh)
        inside = await self._buttons_within(fresh, anchors)
        helpers = await self._helpers_within(fresh, anchors)
        return (_guard_signature(approved, skip_buttons=self._uploader_buttons,
                                 skip_controls=uploaded | self._uploader_helpers)
                == _guard_signature(fresh, skip_buttons=inside, skip_controls=uploaded | helpers))

    async def _only_uploads_changed(self, fresh: PageModel) -> bool:
        """Whether ``fresh`` differs from the approved observation only inside the
        containers of this runtime's uploads (their Attach and cloud buttons replaced by
        the file's name and a Remove button, the file input gone) and in where page
        actions sit, while every question, option set and control binding is as
        approved. An upload's widget may re-render seconds after the attach. The questions
        are those the fill writes against now (follow-up questions it took in included)."""
        approved = self._fill_model
        anchors = self._upload_anchors(fresh)
        if approved is None or approved.form is None or not anchors or fresh.form is None:
            return False
        if (not _same_questions(approved.form, fresh.form)
                or _binding_shape(fresh.bindings) != self._fill_bindings):
            return False
        uploaded = {u.binding.selector for u in self._uploads.values()
                    if u.document == fresh.snapshot.document}
        inside = await self._buttons_within(fresh, anchors)
        helpers = await self._helpers_within(fresh, anchors)
        return (_guard_signature(approved, skip_buttons=self._uploader_buttons,
                                 skip_controls=uploaded | self._uploader_helpers)
                == _guard_signature(fresh, skip_buttons=inside, skip_controls=uploaded | helpers))

    def _follow_ups(self, fresh: PageModel, base: PageModel) -> _QuestionDelta | None:
        """How ``fresh`` (renamed questions under their known ids) differs from ``base``
        when it is only follow-up changes (``_QuestionDelta.follow_ups``), or None:

        - questions appeared anywhere, unanswered (BambooHR's and Greenhouse's conditional
          follow-ups; Teamtailor's question rendered late); one that appeared with a box
          already checked (a pre-checked attestation) is a changed page;
        - right after a choice this fill made (``_reveal_source``) and only then, questions
          became required or optional or a question's help text changed (Greenhouse's
          Hispanic/Latino question once the race question shows); after a typed answer that
          is a changed question.

        Every question of ``base`` is still shown, in its order, with the same wording,
        placeholder, control and options, and the actions, the employer context and every
        other control read as in ``base`` once the changed questions' own controls are left
        out (requiredness aside). A reworded, removed or moved question, changed actions or
        context are never follow-up changes."""
        if fresh.form is None or base.form is None or fresh.form.scope.key != base.form.scope.key:
            return None
        delta = _question_delta(base.form, fresh.form)
        if not delta.follow_ups:
            return None
        if any((b := fresh.bindings.get(q.id)) is None or b.checked_values for q in delta.added):
            # A question that appeared with a box already checked (a pre-checked attestation)
            # was answered by the page, not by the person: a changed page.
            return None
        if (delta.toggled or delta.helped) and self._reveal_source is None:
            # Requiredness and help text change only in answer to a choice this fill made;
            # after a typed answer they are a changed question.
            return None
        own_fresh: set[str] = set()
        for question in (*delta.added, *delta.helped):
            binding = fresh.bindings.get(question.id)
            if binding is None:
                return None
            own_fresh |= _own_selectors(binding)
        own_base: set[str] = set()
        for question in delta.helped:
            binding = base.bindings.get(question.id)
            if binding is not None:
                own_base |= _own_selectors(binding)
        keep = {q.id for q in base.form.fields} - {q.id for q in delta.helped}
        if (_guard_signature(_only_questions(fresh, keep), skip_controls=own_fresh, stable=True,
                             ignore_required=True, ignore_preceding=True)
                != _guard_signature(_only_questions(base, keep), skip_controls=own_base, stable=True,
                                    ignore_required=True, ignore_preceding=True)):
            return None
        return delta

    def _change_summary(self, fresh: PageModel) -> str:
        """What differs between the step as the fill writes against it and ``fresh``, for a
        failure's detail: questions by position and field id with the kind of change (an
        appeared question also by its wording), then whether the actions or the employer
        context changed. No answer or value is included."""
        approved = self._fill_model
        if approved is None or approved.form is None:
            return ""
        if fresh.form is None:
            return "no application form is shown"
        fresh = self._as_fill_ids(fresh)
        assert fresh.form is not None
        form = approved.form
        before = {f.id: f for f in form.fields}
        position = {f.id: n for n, f in enumerate(fresh.form.fields, start=1)}
        old_position = {f.id: n for n, f in enumerate(form.fields, start=1)}
        parts: list[str] = []

        def shape(model: PageModel, field_id: str) -> Any:
            binding = model.bindings.get(field_id)
            return None if binding is None else _binding_shape({field_id: binding})

        added = [f"#{position[f.id]} {f.id} ({_short_label(f.label)})" if f.label.strip() else f"#{position[f.id]} {f.id}"
                 for f in fresh.form.fields if f.id not in before]
        after_ids = {f.id for f in fresh.form.fields}
        removed = [f"#{old_position[f.id]} {f.id}" for f in form.fields if f.id not in after_ids]
        changed = []
        for f in fresh.form.fields:
            old = before.get(f.id)
            if old is None:
                continue
            kinds = [kind for kind, differs in (
                ("wording", normalize_text(old.label) != normalize_text(f.label)),
                ("help text", normalize_text(old.help_text or "") != normalize_text(f.help_text or "")),
                ("placeholder", normalize_text(old.placeholder or "") != normalize_text(f.placeholder or "")),
                ("control", old.control_type is not f.control_type),
                ("options", [(o.value, normalize_text(o.label)) for o in old.options or []]
                            != [(o.value, normalize_text(o.label)) for o in f.options or []]),
                ("required", old.required != f.required),
                ("binding", shape(approved, f.id) != shape(fresh, f.id)),
            ) if differs]
            if kinds:
                changed.append(f"#{position[f.id]} {f.id} ({', '.join(kinds)})")
        if [f.id for f in fresh.form.fields if f.id in before] != [f.id for f in form.fields if f.id in after_ids]:
            parts.append("question order")
        for how, items in (("appeared", added), ("removed", removed), ("changed", changed)):
            if items:
                parts.append(f"{how}: " + "; ".join(items))

        def actions(model: PageModel) -> list[str]:
            return sorted(_button_identity(b) for b in model.snapshot.buttons
                          if not (THIRD_PARTY_ASSIST.search(b.text) or LOADING_STATE.match(b.text)))

        if actions(approved) != actions(fresh) or (
                approved.form is not None and approved.form.is_final_step != fresh.form.is_final_step):
            parts.append("actions")
        context = [(m.snapshot.url, m.snapshot.title, [h.text for h in m.snapshot.headings],
                    m.inspection.job_identity.model_dump(mode="json", exclude={"observed_at"})
                    if m.inspection.job_identity else None) for m in (approved, fresh)]
        if context[0] != context[1]:
            parts.append("employer context")
        summary = "; ".join(parts) or "controls outside the questions or their selectors"
        return summary if len(summary) <= 400 else summary[:399] + "…"

    async def _write(self, operation: Callable[[], Awaitable[None]]) -> None:
        if await self._assert_fill_freshness(rebind=True):
            raise _Rebound
        try:
            await operation()
        except PageContextLost:
            self._context_lost = True
            self._inspected_after_loss = False
            self.menus.reset()
            raise
        except DriverError:
            # A per-field failure is recoverable only while the same document remains.
            await self._assert_fill_context()
            raise
        await self._assert_fill_context()

    async def _contain_changed_questions(
        self, approved: ApplicationForm, after: PageModel, *, follow_ups: bool = False
    ) -> list[FieldFillResult]:
        """Questions that appeared or changed while filling were not authorized by the
        packet. Never leave a pre-checked consent/attestation among them checked. With
        ``follow_ups`` (see ``_follow_ups``) only the questions that appeared are reported,
        as not yet attempted (``SKIPPED``) rather than failed: the next resolution answers
        them. Each is named by its wording (page text), so a failure says which question
        appeared (Teamtailor's "Linkedin profile" has no label in the packet's form)."""
        if after.form is None:
            return []
        known = {f.id: f.fingerprint for f in approved.fields}
        results = []
        for new in after.form.fields:
            if known.get(new.id) == new.fingerprint or (follow_ups and new.id in known):
                continue
            question = _short_label(new.label) or new.id
            if follow_ups:
                status = FieldFillStatus.SKIPPED
                detail = f"appeared while filling ({question}); answered once this step is resolved again"
            else:
                status = FieldFillStatus.FAILED
                detail = f"appeared or changed while filling ({question}); not answered by this packet"
            binding = after.bindings[new.id]
            if (
                new.control_type is ControlType.CHECKBOX
                and new.semantic_type in (SemanticType.CONSENT, SemanticType.ATTESTATION)
                and binding.checked_values
            ):
                await self._write(partial(self.driver.set_checked,
                    binding.selector, False, label_selector=binding.label_selectors.get("")))
                detail += "; cleared its pre-checked box"
            results.append(FieldFillResult(field_id=new.id, status=status, detail=detail))
        return results

    async def _leave_unanswered(self, app_field: ApplicationField, binding: FieldBinding) -> FieldFillResult:
        if app_field.control_type is ControlType.UNSUPPORTED:
            return FieldFillResult(field_id=app_field.id, status=FieldFillStatus.SKIPPED,
                                   detail="custom control; operated only by the user")
        if (
            app_field.control_type is ControlType.CHECKBOX
            and app_field.semantic_type in (SemanticType.CONSENT, SemanticType.ATTESTATION)
            and binding.checked_values
        ):
            # A pre-checked consent or attestation is not the user's answer.
            await self._write(lambda: self.driver.set_checked(
                binding.selector, False, label_selector=binding.label_selectors.get("")))
            return FieldFillResult(field_id=app_field.id, status=FieldFillStatus.SKIPPED,
                                   detail="cleared a pre-checked box the user has not agreed to")
        return FieldFillResult(field_id=app_field.id, status=FieldFillStatus.SKIPPED)

    def _unusable_resumes(self, form: ApplicationForm, packet: ApplicationPacket, model: PageModel,
                          only: frozenset[str] | None) -> list[str]:
        """Resume uploads this session cannot fill: none of the resumes the site offers
        is the pinned file (or the only one) and files cannot be attached here. Each is
        recorded for this document, so re-inspection makes it the person's to attach."""
        if self.attaches_files:
            return []
        held = []
        for app_field in form.fields:
            binding = model.bindings.get(app_field.id)
            answer = packet.answer_for(app_field.id)
            if (binding is None or answer is None or app_field.control_type is not ControlType.FILE
                    or not binding.resume_choices or not isinstance(answer.value, FileValue)
                    or (only is not None and app_field.id not in only)):
                continue
            pinned = answer.value.artifact.filename
            choice, reason = choose_resume(binding.resume_choices, pinned)
            if choice is None:
                self._attach_needed[(model.snapshot.document, app_field.id)] = pinned
                held.append(f"{app_field.label}: {reason}")
        return held

    async def _use_resume(self, fid: str, binding: FieldBinding, choice: ResumeChoice,
                          reason: str) -> FieldFillResult:
        """Select one of the resumes the site already has and read the selection back:
        exactly that choice is checked."""
        selectors = [c.selector for c in binding.resume_choices]
        before = await self.driver.evaluate(_READ_CHECKED, selectors)
        if not (isinstance(before, list) and before[selectors.index(choice.selector)] is True):
            label = choice.label_selector
            # A card's radio usually sits under its styled label, which is what takes the
            # click; the radio itself only when there is no label.
            await self._write(lambda: self.driver.click(label) if label else self.driver.set_checked(
                choice.selector, True))
        after = await self.driver.evaluate(_READ_CHECKED, selectors)
        chosen = [c.name for c, checked in zip(binding.resume_choices, after or [], strict=False) if checked]
        if chosen == [choice.name]:
            return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED, detail=f"chose {reason}")
        return FieldFillResult(field_id=fid, status=FieldFillStatus.VERIFICATION_MISMATCH,
                               detail=f"selected resumes read back as {chosen!r}, not {choice.name!r}")

    async def _read(self, selector: str) -> dict[str, Any]:
        value = await self.driver.evaluate(_READ_CONTROL, selector)
        return value if isinstance(value, dict) else {}

    async def _apply(self, app_field: ApplicationField, binding: FieldBinding, value: Any) -> FieldFillResult:
        fid = app_field.id

        def result(ok: bool, observed: object) -> FieldFillResult:
            if ok:
                return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED)
            return FieldFillResult(field_id=fid, status=FieldFillStatus.VERIFICATION_MISMATCH,
                                   detail=f"reads back {observed!r}")

        ctype = app_field.control_type
        if isinstance(value, TextValue) and ctype is ControlType.TEXT and binding.date_segments:
            return await self._apply_date(fid, binding, value.text)
        if isinstance(value, TextValue) and ctype is ControlType.TEXT and app_field.expects_international_phone:
            # Typed as given: "+<code><number>" makes the widget's picker choose the
            # country itself. The readback compares digits, not the widget's formatting.
            phone: list[tuple[bool, str]] = []

            async def type_phone() -> None:
                phone.append(await fill_phone(self.driver, binding.selector, value.text))

            await self._write(type_phone)
            return result(*phone[0])
        if isinstance(value, TextValue) and ctype in (ControlType.TEXT, ControlType.TEXTAREA):
            shown = (await self._read(binding.selector)).get("value")
            if isinstance(shown, str) and shown.strip() and _shows_same(app_field, shown, value.text):
                # A value the site filled in itself (from the person's profile) that says
                # the same is kept as it is; one that differs is overwritten below.
                return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED, detail=_KEPT_TEXT)
            # Typed with real input events (a trusted insertText), never by assigning the
            # value, so a React-controlled input updates its state.
            await self._write(lambda: self.driver.fill(binding.selector, value.text))
            if binding.suggests:
                # A street address input lists suggestions for what was typed (Paylocity's
                # Address Line 1): the typed text is the answer; the list is dismissed,
                # never chosen from.
                with contextlib.suppress(CapabilityUnsupported):
                    await self.driver.press(binding.selector, "Escape")
            return await self._verify_text(app_field, binding.selector, value.text)
        if isinstance(value, TextValue) and ctype is ControlType.TYPEAHEAD and binding.input_select:
            chosen: list[LookupOutcome] = []

            async def choose() -> None:
                chosen.append(await fill_input_select(self.driver, binding.selector, value.text))

            await self._write(choose)
            picked = chosen[0]
            if picked.chosen is None:
                return FieldFillResult(field_id=fid, status=FieldFillStatus.NEEDS_CHOICE,
                                       detail=picked.detail, suggestions=list(picked.suggestions))
            if picked.verified:
                return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED,
                                       detail=picked.detail or None)
            return FieldFillResult(field_id=fid, status=FieldFillStatus.VERIFICATION_MISMATCH,
                                   detail=picked.detail)
        if isinstance(value, TextValue) and ctype is ControlType.TYPEAHEAD and binding.aria is not None:
            outcomes: list[LookupOutcome] = []

            async def look_up() -> None:
                outcomes.append(await fill_lookup(
                    self.driver, binding.selector, value.text, dict(binding.aria or {}),
                    lookup_matches, before_action=partial(self._check_before_action, binding.selector)))

            await self._write(look_up)
            outcome = outcomes[0]
            if outcome.chosen is None:
                # Nothing committed and the input is empty again: one of the observed
                # suggestions has to be chosen (and is then typed verbatim).
                return FieldFillResult(field_id=fid, status=FieldFillStatus.NEEDS_CHOICE,
                                       detail=outcome.detail, suggestions=list(outcome.suggestions))
            if outcome.verified:
                return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED)
            return FieldFillResult(field_id=fid, status=FieldFillStatus.VERIFICATION_MISMATCH,
                                   detail=outcome.detail)
        if isinstance(value, ChoiceValue) and ctype is ControlType.SELECT:
            if binding.aria is not None:
                selected: list[str] = []

                async def select_accessible() -> None:
                    selected.extend(await self.driver.select_accessible(
                        binding.selector, [value.value], dict(binding.aria or {}),
                        before_action=partial(self._check_before_action, binding.selector)))

                await self._write(select_accessible)
                if selected == [value.value] and (binding.aria or {}).get("probed"):
                    self.menus.confirm(binding.selector, value.value)
                return result(selected == [value.value], selected)
            if (await self._read(binding.selector)).get("values") == [value.value]:
                return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED,
                                       detail="already selected; left as it is")
            await self._write(lambda: self.driver.select_values(binding.selector, [value.value]))
            got = (await self._read(binding.selector)).get("values")
            return result(got == [value.value], got)
        if isinstance(value, MultiChoiceValue) and ctype is ControlType.MULTISELECT:
            wanted = [c.value for c in value.choices]
            await self._write(lambda: self.driver.select_values(binding.selector, wanted))
            got = (await self._read(binding.selector)).get("values")
            return result(isinstance(got, list) and set(got) == set(wanted), got)
        if isinstance(value, ChoiceValue) and ctype is ControlType.RADIO:
            option = binding.option_selectors[value.value]
            if binding.pressed:
                # A toggle-button option is clicked once unless it is pressed already
                # (clicking it again could release it).
                async def press() -> None:
                    if not (await self.driver.evaluate(_READ_CHECKED, [option]))[0]:
                        await self.driver.click(option, trial=True)
                        await self.driver.click(option)

                await self._write(press)
            else:
                await self._write(lambda: self.driver.set_checked(
                    option, True, label_selector=binding.label_selectors.get(value.value)))
            return await self._verify_group(fid, binding, {value.value})
        if isinstance(value, MultiChoiceValue) and ctype is ControlType.CHECKBOX_GROUP:
            wanted_set = {c.value for c in value.choices}
            for option_value, selector in binding.option_selectors.items():
                want = option_value in wanted_set
                if (option_value in binding.checked_values) != want:
                    await self._write(partial(self.driver.set_checked,
                        selector, want, label_selector=binding.label_selectors.get(option_value)))
            return await self._verify_group(fid, binding, wanted_set)
        if isinstance(value, BooleanValue) and ctype is ControlType.CHECKBOX:
            await self._write(lambda: self.driver.set_checked(
                binding.selector, value.checked, label_selector=binding.label_selectors.get("")))
            got = (await self._read(binding.selector)).get("checked")
            return result(got is value.checked, got)
        if isinstance(value, FileValue) and ctype is ControlType.FILE:
            return (await self._attach(app_field, binding, value))[0]
        return FieldFillResult(field_id=fid, status=FieldFillStatus.FAILED,
                               detail=f"cannot apply a {type(value).__name__} to {ctype}")

    # --- text readback ----------------------------------------------------------------

    async def _verify_text(self, app_field: ApplicationField, selector: str, text: str) -> FieldFillResult:
        """Read a typed value back. A mismatch is read once more after a settle with the
        control re-resolved by its field id (a React commit or hydration may have replaced
        it); if the re-rendered control lost the value it is typed once more with real
        key events and read back again. Only then is it a mismatch."""
        fid = app_field.id

        def same(got: object) -> bool:
            return _reads_as_typed(app_field, got, text)

        got = (await self._read(selector)).get("value")
        if same(got):
            return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED,
                                   detail=self._formatted_detail(got, text))
        fresh = await self._reresolve(fid)
        if fresh is not None:
            got = (await self._read(fresh)).get("value")
            if same(got):
                return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED,
                                       detail=self._formatted_detail(got, text))
            multiline = app_field.control_type is ControlType.TEXTAREA
            await self._write(lambda: self._retype(fresh, text, multiline=multiline))
            got = (await self._read(fresh)).get("value")
            if not same(got):
                again = await self._reresolve(fid)
                if again is not None:
                    got = (await self._read(again)).get("value")
            if same(got):
                return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED,
                                       detail="typed again after the page re-rendered it")
        return FieldFillResult(field_id=fid, status=FieldFillStatus.VERIFICATION_MISMATCH,
                               detail=f"reads back {got!r}")

    @staticmethod
    def _formatted_detail(got: object, text: str) -> str | None:
        """For a phone number the site formatted as it was typed: what it shows."""
        if isinstance(got, str) and got.replace("\r\n", "\n") != text.replace("\r\n", "\n"):
            return f"the site formats it as {got!r}"
        return None

    async def _reresolve(self, field_id: str) -> str | None:
        """After a short settle, the current selector of this fill's question
        ``field_id`` (the same question on a possibly re-rendered control), waiting
        (bounded) for it to come back; None when it does not."""
        await self.driver.settle(min(self.settle_timeout_s, 0.5))
        base = self._fill_model
        question = base.form.find(field_id) if base is not None and base.form else None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(self.settle_timeout_s, _RERENDER_WAIT_S)
        while True:
            await self._assert_fill_context()
            model = await self._raw_model()
            binding = model.bindings.get(field_id)
            found = model.form.find(field_id) if model.form else None
            if (binding is not None and found is not None and not self._busy(model)
                    and (question is None or found.fingerprint == question.fingerprint)):
                return binding.selector
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(0.15)

    async def _retype(self, selector: str, text: str, *, multiline: bool = False) -> None:
        """Empty the control the way a person does and type ``text`` key by key. A
        TEXTAREA, text with a newline, carriage return or tab, and long text are entered
        as one input event instead: ``type_text`` refuses control characters (a newline
        typed is the Enter key), and ``fill`` inserts them without pressing a key."""
        await self.driver.clear_text(selector)
        if multiline or _TYPED_CONTROLS.search(text) or len(text) > _RETYPE_MAX_CHARS:
            await self.driver.fill(selector, text)
        else:
            await self.driver.type_text(selector, text, delay_s=0.01)

    async def _sweep(self, accepted: ApplicationForm, written: Mapping[str, str],
                     results: dict[str, FieldFillResult]) -> None:
        """Text this fill wrote and verified that the page changed afterwards (an
        autofill landing late, a controlled input reverting on a re-render) is written
        once more and verified again."""
        model = await self._raw_model()
        for fid, text in written.items():
            binding = model.bindings.get(fid)
            app_field = accepted.find(fid)
            if app_field is None:
                continue
            if binding is not None and _reads_as_typed(app_field, binding.value, text):
                continue
            outcome = (await self._operate(app_field, TextValue(text=text)))[0]
            if outcome.status is FieldFillStatus.FILLED:
                outcome = outcome.model_copy(update={"detail": "written again after the page changed it"})
            results[fid] = outcome

    # --- uploads ------------------------------------------------------------------------

    async def _upload_state(self, selector: str, names: Sequence[str],
                            anchor: str | None = None) -> UploadState:
        """The upload's state (``anchor``: the uploader's own container, read when the
        input is gone); a driver that cannot run the upload read gets the file input's own
        files only (never a chip or notice)."""
        model = self._fill_model
        origin = model.snapshot.document.split(" ")[0] if model is not None else None
        stuck = sorted(marker for where, marker in self._stuck if where == origin)
        try:
            return UploadState.from_raw(await self.driver.evaluate(
                UPLOAD_STATE, {"selector": selector, "names": list(names), "anchor": anchor,
                               "stuck": stuck}))
        except PageContextLost:
            raise
        except DriverError:
            files = (await self._read(selector)).get("files")
            return UploadState(connected=files is not None,
                               files=files if isinstance(files, list) else [])

    async def _holds_pinned(self, selector: str, artifact: ArtifactRef) -> bool:
        """The control holds exactly the pinned file: name, size and the bytes' digest
        (never accepted when the page cannot hash them)."""
        digest = await self.driver.evaluate(_FILE_DIGEST, selector)
        return isinstance(digest, dict) and digest.get("sha256") == artifact.sha256

    def _question_index(self, field_id: str) -> int:
        """Position of a question in the form the fill writes against (-1 if unknown)."""
        form = self._fill_model.form if self._fill_model is not None else None
        ids = [f.id for f in form.fields] if form is not None else []
        return ids.index(field_id) if field_id in ids else -1

    async def _attach(self, app_field: ApplicationField, binding: FieldBinding,
                      value: FileValue) -> tuple[FieldFillResult, bool]:
        """Attach the pinned file to the upload control, once, and read it back. Returns
        the result and whether the page was touched.

        The file is set on the ``input[type=file]`` directly, also when it is hidden
        behind an "Attach" button or a drop zone. It is not attached again when this
        session attached it in this document and the uploader, having emptied or replaced
        its input, shows it, or when the control already holds the pinned file (digest
        checked). After attaching, a spinner or "Uploading..." is waited out (bounded);
        the readback accepts the file in the input (name and size), or, once the uploader
        emptied or replaced its input, a visible chip naming it or an upload notice
        without an error. The driver verified the attached bytes when it could read them.
        A resume the site already keeps (``binding.resume_choices``) is chosen instead
        when one is the pinned file or the only one."""
        fid = app_field.id
        artifact = value.artifact
        name = Path(artifact.path).name
        taken = self._uploads.get(fid)
        if (taken is not None and taken.name == name
                and taken.document == str(await self.driver.evaluate(_DOCUMENT_IDENTITY))):
            # Attached by this session in this document: not attached again (each attach is
            # a fresh upload) while nothing shows an error or progress and the uploader
            # still holds it, shows it (possibly as a shortened name, "resume-avery-…pdf",
            # or "1 file"), or has replaced its input.
            held = await self._upload_state(binding.selector, shown_names(name), taken.anchor)
            if held.error is None and not held.busy:
                detail = ("already attached; not attached again" if held.holds(name, artifact.size_bytes)
                          else "the uploader already shows this file" if held.shown
                          else "the uploader already holds this file" if not held.connected else None)
                if detail is not None:
                    return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED, detail=detail), False
        if not artifact.verify():
            return FieldFillResult(
                field_id=fid, status=FieldFillStatus.FAILED,
                detail=f"{artifact.filename!r} is missing or changed since it was verified"), False
        if binding.resume_choices:
            # The site keeps uploaded resumes: use the pinned file's own card (or the
            # only one) instead of uploading it again.
            choice, reason = choose_resume(binding.resume_choices, artifact.filename)
            if choice is not None:
                return await self._use_resume(fid, binding, choice, reason), False
            if not self.attaches_files:
                return FieldFillResult(field_id=fid, status=FieldFillStatus.FAILED,
                                       detail=f"{reason}; attach your resume in the browser window"), False
        before = await self._upload_state(binding.selector, [name])
        if (before.holds(name, artifact.size_bytes)
                and await self._holds_pinned(binding.selector, artifact)):
            return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED,
                                   detail="already attached; not attached again"), False
        # An upload popup's input shows the file by its button in the form, not in the popup.
        anchor = binding.upload_anchor or await file_anchor(self.driver, binding.selector)
        if anchor and self._fill_model is not None:
            # The approved buttons and hidden inputs this uploader owns (Attach, cloud
            # pickers): it may replace them once it takes the file, now or seconds later.
            self._uploader_buttons |= await self._buttons_within(self._fill_model, [anchor])
            self._uploader_helpers |= await self._helpers_within(self._fill_model, [anchor])
        verified: list[bool | None] = []

        async def attach() -> None:
            verified.append(await self.driver.set_files(binding.selector, Path(artifact.path)))

        await self._write(attach)
        if not artifact.verify():
            return FieldFillResult(field_id=fid, status=FieldFillStatus.FAILED,
                                   detail="the pinned file changed while attaching it"), True
        state = await self._await_upload(binding.selector, name, artifact.size_bytes, anchor)
        if state.error:
            return FieldFillResult(field_id=fid, status=FieldFillStatus.VERIFICATION_MISMATCH,
                                   detail=f"the site reports: {state.error!r}"), True
        if state.busy:
            return FieldFillResult(field_id=fid, status=FieldFillStatus.VERIFICATION_MISMATCH,
                                   detail="the upload was still in progress when the wait ended"), True
        fill_form = self._fill_model.form if self._fill_model is not None else None
        upload = _Upload(app_field, binding, anchor, name,
                         str(await self.driver.evaluate(_DOCUMENT_IDENTITY)), self._question_index(fid),
                         fill_form.step if fill_form is not None else None)
        if state.holds(name, artifact.size_bytes):
            # An uploader that keeps the file may still show its name beside the input.
            self._uploads[fid] = upload
            return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED), True
        if not state.files and (state.shown or await file_shown(
                self.driver, binding.selector, name, anchor=anchor)):
            # The uploader took the file and emptied or replaced its input (Workable,
            # Greenhouse, a drop zone); it shows the file's name or an upload notice.
            self._uploads[fid] = upload
            detail = "the uploader shows the file; its input is empty or replaced"
            if verified and verified[0] is False:
                detail += "; the attached bytes were not verified (the page kept no readable copy)"
            return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED, detail=detail), True
        return FieldFillResult(
            field_id=fid, status=FieldFillStatus.VERIFICATION_MISMATCH,
            detail=f"reads back {state.files!r} and the page shows no chip or notice for {name!r}"), True

    async def _await_upload(self, selector: str, name: str, size: int,
                            anchor: str | None) -> UploadState:
        """Read the upload until it is confirmed and nothing is busy in its own field, or
        an error shows. While the uploader shows its own progress this waits up to
        ``_UPLOAD_PROGRESS_S`` (Lever's "Uploading resume..."); an upload that shows
        neither progress nor the file is read for at most ``_UPLOAD_WAIT_S`` (and the
        settle timeout), and ``_UPLOAD_GRACE_S`` more after its progress ended. Only a
        state still in progress at the bound is reported as such."""
        loop = asyncio.get_running_loop()
        start = loop.time()
        confirm_by = start + min(self.settle_timeout_s, _UPLOAD_WAIT_S)
        progress_ended = start
        while True:
            state = await self._upload_state(selector, shown_names(name), anchor)
            if state.error is not None or (not state.busy and state.confirms(name, size)):
                return state
            now = loop.time()
            if state.busy:
                progress_ended = now + _UPLOAD_GRACE_S
                if now - start >= _UPLOAD_PROGRESS_S:
                    return state
            elif now >= max(confirm_by, progress_ended):
                return state
            await asyncio.sleep(0.2)

    async def _settle_after_upload(
        self, field_id: str, accepted: ApplicationForm
    ) -> tuple[str | None, ApplicationForm]:
        """After our upload: let the page settle (bounded; an autofill the upload
        triggers lands, an autofill offer is declined once) and re-read it. When only the
        attached control's own description and controls, values, the buttons inside its
        uploader's container and where page actions sit changed (the submit control keeps
        its text and form: ``_guard_signature``), the fill continues against the re-read
        page. Returns why it must stop instead (or None) and the form the fill is now
        authorized against."""
        model = await self._await_quiet(min(self.settle_timeout_s, _UPLOAD_WAIT_S), values=True)
        await self._assert_fill_context()
        base = self._fill_model
        assert base is not None
        change = _question_change(base, model, field_id)
        if change is None and not await self._guard_equal_but_uploads(base, model):
            change = "the page's actions or employer context changed after the upload"
        if change is not None:
            return change, accepted
        assert model.form is not None
        await self._set_fill_model(model, model.form)
        return None, model.form

    async def _apply_date(self, fid: str, binding: FieldBinding, text: str) -> FieldFillResult:
        """A segmented date (Month / Day / Year boxes): the date is typed into the first
        segment in the widget's own order ("09/24/2026"), as a person does, and the widget
        moves on by itself; a widget that does not is typed segment by segment. Each
        segment is read back as a number."""
        kinds = [kind for kind, _ in binding.date_segments]
        selectors = [selector for _, selector in binding.date_segments]
        parts = date_segment_values(text, kinds)
        if parts is None:
            shape = "/".join({"month": "MM", "day": "DD", "year": "YYYY"}.get(k, k) for k in kinds)
            return FieldFillResult(field_id=fid, status=FieldFillStatus.FAILED,
                                   detail=f"{text!r} is not a date this {shape} field can take")

        async def read_back() -> list[str]:
            return [str((await self._read(selector)).get("value") or "") for selector in selectors]

        def same(got: list[str]) -> bool:
            digits = [re.sub(r"\D", "", g) for g in got]
            return all(d and int(d) == int(p) for d, p in zip(digits, parts, strict=True))

        async def type_whole() -> None:
            for selector in selectors:
                await self.driver.clear_text(selector)
            await self.driver.type_text(selectors[0], "/".join(parts))

        async def type_segments() -> None:
            for selector, part in zip(selectors, parts, strict=True):
                await self.driver.clear_text(selector)
                await self.driver.type_text(selector, part)

        await self._write(type_whole)
        got = await read_back()
        if not same(got):
            await self._write(type_segments)
            got = await read_back()
        if same(got):
            return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED)
        return FieldFillResult(field_id=fid, status=FieldFillStatus.VERIFICATION_MISMATCH,
                               detail=f"reads back {'/'.join(got)!r}")

    async def _verify_group(self, fid: str, binding: FieldBinding, wanted: set[str]) -> FieldFillResult:
        values = list(binding.option_selectors)
        checked = await self.driver.evaluate(_READ_CHECKED, [binding.option_selectors[v] for v in values])
        got = {v for v, c in zip(values, checked, strict=True) if c}
        if got == wanted:
            return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED)
        return FieldFillResult(field_id=fid, status=FieldFillStatus.VERIFICATION_MISMATCH,
                               detail=f"checked options read back as {sorted(got)}")

    async def _changed_since_fill(self, form: ApplicationForm) -> bool:
        """Whether the step changed since the last fill of it. A question a re-render gave
        another generated id counts under its known id, and a re-render that regenerated
        only selectors (the same questions, constraints, actions and context) is no change
        (BambooHR re-mounts its text fields with new ids once a Yes/No is answered)."""
        if self._filled is None or self._filled[0] != form.scope.key:
            return False
        base = self._fill_model
        renames = (_renamed_questions(base.form.fields, form.fields)
                   if base is not None and base.form is not None else {})
        if self._filled[1] != _rekeyed_form(form, renames).fingerprint:
            return True
        if self._filled_structure is None or self._last is None:
            return False
        if self._filled_structure == _guard_signature(self._last):
            return False
        if (self._filled_stable is not None
                and self._filled_stable == _guard_signature(self._as_fill_ids(self._last), stable=True)):
            return False
        if await self._only_uploads_changed(self._last):
            # An upload of this fill re-rendered its uploader after the fill.
            self._filled_structure = _guard_signature(self._last)
            return False
        return True

    # --- navigation -----------------------------------------------------------------

    async def advance(self) -> NavigationResult:
        if not self.policy.automation_may_navigate:
            raise SubmissionRefused("navigation belongs to the user in this session")
        model = await self._model()
        form = model.form
        if form is None:
            raise AmbiguousAction(f"no application form step to advance from ({model.inspection.kind})")
        if form.is_final_step is True:
            raise SubmissionRefused(
                "this step's primary action submits the application; use submit() after "
                "begin_submission"
            )
        if form.is_final_step is None or not form.next_selector:
            raise AmbiguousAction("the step has no unambiguous next-step control")
        if self._context_lost:
            raise ValueError("the page changed while filling; inspect and resolve it again first")
        if await self._changed_since_fill(form):
            raise ValueError("questions on this step changed after filling; re-inspect and resolve")
        invalid = await self.driver.evaluate(
            _NATIVE_VALIDITY, {"form": model.form_selector, "button": form.next_selector}
        )
        if invalid:
            # The browser itself would block the step; clicking would change nothing.
            return NavigationResult(advanced=False, inspection=model.inspection,
                                    validation_errors=list(invalid))
        await self._assert_fill_context()
        by_progress = model.step_source == "progress"
        await self.driver.click(form.next_selector)
        await self.driver.settle(self.settle_timeout_s)
        self._steps_advanced += 1
        after = await self._model()
        # A dialog wizard renders its next step in place, sometimes after a request of
        # its own: while the step just left is still shown without errors, wait a moment.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(self.settle_timeout_s, _STEP_WAIT_S)
        while (_left_behind(model, form, after, by_progress) and after.form is not None
               and not _validation_errors(after.form) and loop.time() < deadline):
            await asyncio.sleep(_READY_POLL_S)
            after = await self._model()
        # A step drawn before its questions (Workday: the progress list and "Next"
        # first) is waited for like a freshly loaded page.
        after = await self._await_ready(after)
        if after.form is not None and _left_behind(model, form, after, by_progress):
            self._steps_advanced -= 1
            after = await self._model(evidence=f"step-{form.step}-rejected")
            assert after.form is not None
            return NavigationResult(advanced=False, inspection=after.inspection,
                                    validation_errors=_validation_errors(after.form))
        self._filled = None
        self._fill_document = None
        return NavigationResult(advanced=True, inspection=after.inspection)

    # --- submission -----------------------------------------------------------------

    def _refuse_resubmission(self) -> None:
        if self._accepted:
            raise SubmissionRefused("this session already observed an accepted submission")
        if self._pending is not None and self._pending.dispatched and not self._pending.resolved:
            raise SubmissionRefused(
                "a submit was already dispatched and its outcome is not established; "
                "reconcile before any retry"
            )

    async def submit(self) -> SubmitActionResult:
        """Dispatch the final submit once. Call only after
        ``ApplicationStore.begin_submission`` has durably recorded SUBMITTING."""
        self._refuse_resubmission()
        model = await self._model()
        form = model.form

        def not_dispatched(detail: str, *, invalid: list[str] | None = None,
                           next_state: NotSubmittedNext = NotSubmittedNext.FAILED_RETRYABLE) -> SubmitActionResult:
            self._pending = _PendingSubmit(dispatched=False, detail=detail, form=form,
                                           invalid=invalid or [], next_state=next_state)
            return SubmitActionResult(dispatched=False, detail=detail)

        if not self.policy.automation_may_submit:
            return not_dispatched("submission belongs to the user in this session")
        if form is None:
            return not_dispatched(f"no application form on the page ({model.inspection.kind})")
        if form.is_final_step is not True or not form.submit_selector:
            return not_dispatched("the step has no unambiguous final submit control")
        if self._context_lost:
            return not_dispatched("the page changed while filling; inspect and resolve it again",
                                  next_state=NotSubmittedNext.FILLING)
        if await self._changed_since_fill(form):
            return not_dispatched("questions changed after filling; re-inspect and resolve",
                                  next_state=NotSubmittedNext.FILLING)
        if model.unsupported_pending:
            return not_dispatched(
                "required controls must be operated by the user first: "
                + ", ".join(model.unsupported_pending),
                next_state=NotSubmittedNext.NEEDS_INPUT,
            )
        if model.captcha.present and not model.captcha.solved:
            return not_dispatched(
                "a CAPTCHA on this form must be solved by the user before submitting",
                next_state=NotSubmittedNext.NEEDS_INPUT,
            )
        invalid = await self.driver.evaluate(
            _NATIVE_VALIDITY, {"form": model.form_selector, "button": form.submit_selector}
        )
        if invalid:
            return not_dispatched("the browser's own validation would block the form",
                                  invalid=list(invalid), next_state=NotSubmittedNext.FILLING)
        try:
            await self.driver.click(form.submit_selector, trial=True)
        except NotActionable as exc:
            return not_dispatched(f"submit control is not actionable: {exc}")

        tie = ConfirmationTie.from_identity(self._identity, model.title)
        tie = ConfirmationTie(
            external_job_id=tie.external_job_id,
            job_title=tie.job_title,
            known_references=frozenset(confirmation_references(model.snapshot.body_text)),
        )
        before = await self._evidence.capture(self.driver, f"before-submit-step-{form.step}",
                                              description="completed form before submit")
        marker = str(await self.driver.evaluate(_DOCUMENT_IDENTITY))
        try:
            await self._assert_fill_context()
        except PageContextLost as exc:
            return not_dispatched(str(exc), next_state=NotSubmittedNext.FILLING)
        text = next((b.button.text for b in model.buttons if b.button.selector == form.submit_selector),
                    form.submit_selector)
        dispatched_at = utc_now()
        detail = f"clicked {text!r} once"
        try:
            await self.driver.click(form.submit_selector)
        except DriverError as exc:
            # The trial click passed; a failure now may have happened mid-dispatch.
            detail = f"click raised after it was dispatched ({exc}); outcome uncertain"
        self._pending = _PendingSubmit(dispatched=True, detail=detail, form=form, tie=tie,
                                       marker=marker, evidence=before)
        return SubmitActionResult(dispatched=True, dispatched_at=dispatched_at, detail=detail)

    async def confirm(self) -> SubmissionObservation:
        pending = self._pending
        if pending is None:
            raise RuntimeError("confirm() needs a preceding submit()")
        if not pending.dispatched:
            pending.resolved = True
            return SubmissionObservation(
                outcome=SubmissionOutcome.NOT_SUBMITTED,
                signals=[f"submit was not dispatched: {pending.detail}"],
                validation_errors=pending.invalid,
                observed_url=self.driver.url,
                next_state=pending.next_state,
                detail=pending.detail,
            )
        await self.driver.settle(self.settle_timeout_s)
        model = await self._model(evidence="after-submit", html=True)
        same_document = str(await self.driver.evaluate(_DOCUMENT_IDENTITY)).split(" ")[0] == (
            (pending.marker or "").split(" ")[0]
        )
        observation = self._judge(pending, model, same_document)
        if observation.outcome is SubmissionOutcome.ACCEPTED:
            self._accepted = True
        if observation.outcome is not SubmissionOutcome.UNKNOWN:
            pending.resolved = True
        return observation

    def _acceptance(
        self, model: PageModel, tie: ConfirmationTie, *, fresh_reference_ties: bool
    ) -> tuple[list[str], str | None]:
        """Signals proving acceptance of *this* application, or [] if not proven.

        A page listing several applications (repeated cards, articles, list items or
        rows carrying status or job identity) is read one record at a time: a status
        counts only with identity inside the same *outermost* record, never with text
        from another record or from the page around them, and a record that also
        shows a not-submitted status is ambiguous. Without record wrappers, only
        heading-delimited receipt/job sections or a single leaf statement can tie facts. The acceptance statement must be affirmative, and a job id or title
        must tie it to this application; a reference seen for the first time counts
        only immediately after our own submit (``fresh_reference_ties``)."""
        snapshot = model.snapshot
        candidates: list[tuple[str, str]] = []  # (statement, record text)
        records = application_records(snapshot)
        single_receipt = False
        if records is None:
            # No known repeated group is NOT evidence that the entire page is one
            # application. Use explicit local boundaries, never title + whole body.
            scopes = snapshot.confirmation_scopes
            for scope in scopes:
                heading = scope.heading
                if heading is not None:
                    receipt = affirmative_acceptance(heading)
                    names_job = (
                        bool(tie.job_title and normalize_text(tie.job_title) == normalize_text(heading))
                        or bool(tie.external_job_id and tie.external_job_id.lower() in
                                {i.lower() for i in job_ids(heading)})
                    )
                    status_heading = normalize_text(heading) in {
                        "status", "application status", "submission status", "confirmation", "thank you",
                    }
                    if not (receipt or names_job or status_heading):
                        continue  # e.g. "My applications" is a collection, not a job record
                statement = affirmative_acceptance(scope.text)
                ambiguous = (NOT_SUBMITTED_STATUS.search(scope.text)
                             or len({i.lower() for i in job_ids(scope.text)}) > 1)
                if statement and not ambiguous:
                    # An ungrouped paragraph is not automatically one record either:
                    # its identity and reference must be in the acceptance clause.
                    candidates.append((statement, scope.text if heading is not None else statement))
                    single_receipt = bool(
                        len(scopes) == 1 and heading and affirmative_acceptance(heading)
                    )
        else:
            for record in records:
                statement = affirmative_acceptance(record)
                if statement and not NOT_SUBMITTED_STATUS.search(record):
                    candidates.append((statement, record))
        for statement, record in candidates:
            ties: list[str] = []
            if tie.external_job_id:
                shown = job_ids(record)
                if shown and all(i.lower() != tie.external_job_id.lower() for i in shown):
                    continue  # this record names a different job
                if tie.external_job_id.lower() in record.lower():
                    ties.append(f"job id {tie.external_job_id!r} shown with it")
            if tie.job_title and normalize_text(tie.job_title) in normalize_text(record):
                ties.append(f"job title {tie.job_title!r} shown with it")
            refs = [r for r in confirmation_references(record) if r not in tie.known_references]
            # A new reference ties only a single-record result page, never one of several.
            if refs and fresh_reference_ties and single_receipt:
                ties.append(f"confirmation reference {refs[0]!r} appeared after the submit")
            if ties:
                return [f"acceptance text {statement!r}", *ties], (refs[0] if refs else None)
        return [], None

    def _judge(self, pending: _PendingSubmit, model: PageModel, same_document: bool) -> SubmissionObservation:
        snapshot = model.snapshot
        url = snapshot.url
        evidence = [*pending.evidence, *model.inspection.evidence]
        before = pending.form
        after = model.form
        if before is not None and _same_step_shown_again(before, after, any_url=True):
            assert after is not None
            errors = _validation_errors(after)
            status = self.driver.last_status
            messages = " ".join([*errors, *(r.text for r in snapshot.regions),
                                 *(h.text for h in snapshot.headings), snapshot.title])
            field_errors = [f for f in after.fields if f.validation_error]
            uncertain = UNCERTAIN.search(messages)
            server_error = status is not None and status >= 500
            if field_errors and not uncertain and not server_error:
                # Definite: the site re-displayed this step marking specific answers invalid.
                return SubmissionObservation(
                    outcome=SubmissionOutcome.NOT_SUBMITTED,
                    signals=["the site showed the form again marking fields invalid: "
                             + ", ".join(f.id for f in field_errors)],
                    validation_errors=errors,
                    observed_url=url,
                    evidence=evidence,
                    next_state=NotSubmittedNext.NEEDS_INPUT,
                    detail="rejected by the site's field validation",
                )
            observed = ["observed: the form is shown again"]
            if status is not None:
                observed.append(f"observed: HTTP {status}")
            if uncertain:
                observed.append(f"observed: the site says {uncertain.group(0)!r}")
            observed += [f"observed: message {e!r}" for e in errors]
            return SubmissionObservation(
                outcome=SubmissionOutcome.UNKNOWN,
                signals=observed,
                observed_url=url, evidence=evidence,
                detail="the form is shown again without a definite field-level rejection; the "
                       "application may have been received, so do not retry before reconciling",
            )
        signals, reference = self._acceptance(model, pending.tie, fresh_reference_ties=True)
        if signals and model.inspection.kind is not PageKind.APPLICATION_FORM:
            if not same_document:
                signals.append(f"a new page loaded after the submit ({url})")
            evidence.append(EvidenceRef(kind=EvidenceKind.CONFIRMATION_URL, uri=url,
                                        description="page showing the confirmation"))
            return SubmissionObservation(
                outcome=SubmissionOutcome.ACCEPTED,
                signals=signals,
                confirmation_reference=reference,
                observed_url=url,
                evidence=evidence,
                detail="site confirmation tied to this application",
            )
        heading = next((h.text for h in snapshot.headings), snapshot.title or "no heading")
        status = self.driver.last_status
        observed = [f"observed: page kind {model.inspection.kind.value}", f"observed: heading {heading!r}"]
        if status is not None:
            observed.append(f"observed: HTTP {status}")
        if not same_document:
            observed.append("observed: a new page loaded (not proof of acceptance)")
        return SubmissionObservation(
            outcome=SubmissionOutcome.UNKNOWN,
            signals=observed,
            observed_url=url,
            evidence=evidence,
            detail="no confirmation tied to this application was observed; do not retry "
                   "until the outcome is reconciled",
        )

    # --- user interaction and reconciliation ------------------------------------------

    def _needs_user(self, model: PageModel) -> bool:
        # A pending CAPTCHA widget on a form is the user's to solve only when this
        # session may submit; preparation never needs it and never waits for it.
        return (
            model.inspection.kind in USER_ACTION_PAGES
            or bool(self._pending_for_user(model))
            or (self.options.allow_submission and model.inspection.captcha_pending)
        )

    def _pending_for_user(self, model: PageModel) -> list[str]:
        """Required custom controls the person still has to operate. A menu control this
        page's probing has not observed yet (waits never probe) is the runtime's to
        probe and answer, not the person's: a Workday step reached by signing in shows
        its dropdowns unprobed."""
        if not model.unsupported_pending or not self.probe_menus:
            return list(model.unsupported_pending)
        unprobed = {c.selector for c in model.snapshot.controls
                    if self.menus.candidate(c, model.form_index if model.form_index is not None else -1)
                    and self.menus.key(c) not in self.menus.observations}
        return [field_id for field_id in model.unsupported_pending
                if model.bindings[field_id].selector not in unprobed]

    async def wait_for_user(self, reason: str, timeout_s: float | None = None) -> PageInspection:
        """Poll the live page until the user has finished (signed in, solved the
        CAPTCHA, operated custom controls) or ``timeout_s`` elapses, then return a
        fresh inspection. The runtime does nothing to the page meanwhile."""
        if not self.options.headless:
            # OpenCLI cannot focus windows; the user is told where the tab is instead.
            with contextlib.suppress(DriverError):
                await self.driver.bring_to_front()
        loop = asyncio.get_running_loop()
        deadline = None if timeout_s is None else loop.time() + timeout_s
        # The page the person was asked to act on: the one this runtime reported last (they
        # may have finished before the wait began), else the wait's first read.
        began_on: PageKind | None = self._last.inspection.kind if self._last is not None else None
        gone = 0
        while True:
            # Never open a menu while the person may be operating the page.
            model = await self._model(probe=False)
            if began_on is None:
                began_on = model.inspection.kind
            if began_on in USER_ACTION_PAGES:
                # A sign-in (Workday's account step) or CAPTCHA is done once its page has
                # gone for two reads in a row. The page it leads to is inspected afresh,
                # menus probed, and its own questions follow (unprobed menus look like
                # custom controls here, and are not what the person was asked to do).
                gone = gone + 1 if model.inspection.kind not in USER_ACTION_PAGES else 0
                if gone >= 2:
                    break
            elif not self._needs_user(model):
                break
            if deadline is not None and loop.time() >= deadline:
                break
            await asyncio.sleep(self.poll_interval_s)
        if began_on in USER_ACTION_PAGES and gone >= 2:
            # The person finished a sign-in, CAPTCHA or consent and that page is gone: the
            # page it led to is a new page, read as ``open`` reads one, once ready (Workday
            # draws a step's progress list before its questions) and with its menus probed.
            await self._await_ready()
            inspection = (await self._model(evidence="after-user-action")).inspection
        else:
            # A wait for the person to operate the form never opens a menu, not even here.
            inspection = (await self._model(evidence="after-user-action", probe=False)).inspection
        return self._continuing_posting(inspection)

    async def solve_captcha(self, solver: CaptchaSolver, *, call_callback: bool
                            ) -> tuple[CaptchaAttempt, str, PageInspection | None]:
        """Solve the CAPTCHA widget on the page through ``solver`` (2Captcha, within its
        spend cap) and put the token into the page. Returns the attempt (never the token),
        the page URL it was solved for, and, when the page no longer asks for it, the page
        as it now reads (a CAPTCHA page in front of the form leads to the form, read as
        ``open`` reads a page).

        ``call_callback`` also calls the widget's callback: only for a CAPTCHA page in front
        of the form, and only when that page has nothing to fill (a CAPTCHA page that also
        holds a form is left to the person). With an application form on the page the token
        only goes into the widget's response field, because a callback may send the form;
        the form is then sent, if ever, by the gated submit. Nothing is clicked. A session
        that cannot write to the page (OpenCLI) or a widget whose token only its callback
        hands over (an invisible reCAPTCHA bound to the submit button) is never solved:
        2Captcha is not asked and nothing is spent."""
        try:  # read-only (on OpenCLI's allowlist too)
            page_url, widgets = parse_detection(await self.driver.evaluate(CAPTCHA_DETECT))
        except DriverError:
            return (CaptchaAttempt("unsupported", detail="the page could not be read for a CAPTCHA widget"),
                    self.driver.url, None)
        target = next((w for w in widgets if not w.answered), None)
        if target is None:
            return (CaptchaAttempt("unsupported", detail="no unsolved reCAPTCHA, hCaptcha or Turnstile "
                                   "widget with a site key on the page"), page_url, None)
        if not getattr(self.driver, "injects_captcha_tokens", False):
            return (CaptchaAttempt("unsupported", target.kind, detail="this browser session cannot put a "
                                   "CAPTCHA token into the page"), page_url, None)
        if target.submit_bound and not call_callback:
            return (CaptchaAttempt("unsupported", target.kind,
                                   detail="an invisible reCAPTCHA bound to the submit button hands its token "
                                          "over only through its callback, which sends the form"), page_url, None)
        if call_callback:
            # A callback may send a form on its page: it is called only where the page has
            # nothing to fill. Without it the token would not be handed over, so nothing is
            # spent on such a page.
            here = await self._model(probe=False)
            if here.inspection.form is not None or here.candidate_fields or any(here.fillable_counts.values()):
                return (CaptchaAttempt("unsupported", target.kind,
                                       detail="the CAPTCHA page also holds a form that the widget's callback "
                                              "could send"), page_url, None)
        attempt, token = await solver.token(target, page_url or self.driver.url)
        if token is None:
            return attempt, page_url, None
        inject = getattr(self.driver, "inject_captcha_token", None)
        try:
            put = await inject(target.kind, token, callback=target.callback,
                               call_callback=call_callback) if callable(inject) else {}
        except DriverError:
            return replace(attempt, outcome="not_accepted", detail="the token could not be put into the page"), \
                page_url, None
        await self.driver.settle(min(self.settle_timeout_s, _READY_SETTLE_S))
        if put.get("navigated") or call_callback:
            # The page a CAPTCHA page leads to, once ready: a callback may post the token
            # first and navigate only after the site's answer.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _CAPTCHA_PASS_S
            model = await self._await_ready()
            while self._captcha_ahead(model) and loop.time() < deadline:
                await asyncio.sleep(_READY_POLL_S)
                await self.driver.settle(min(self.settle_timeout_s, _READY_SETTLE_S))
                model = await self._await_ready()
            refused = self._captcha_ahead(model)
        else:
            model = await self._model(probe=False)
            refused = model.inspection.kind is PageKind.CAPTCHA or (
                model.captcha.present and not model.captcha.solved)
        if refused:
            return replace(attempt, outcome="not_accepted", detail="the page still asks for the CAPTCHA"), \
                page_url, None
        return attempt, page_url, self._continuing_posting(model.inspection)

    @staticmethod
    def _captcha_ahead(model: PageModel) -> bool:
        """A CAPTCHA page that has not led on: still one, or (its widget now holding our
        token) a page of no kind that still shows the widget."""
        kind = model.inspection.kind
        return kind is PageKind.CAPTCHA or (kind is PageKind.UNKNOWN and model.captcha.present)

    def _continuing_posting(self, inspection: PageInspection) -> PageInspection:
        """A form the user reached by passing a page in front of it (a sign-in wall,
        Jobvite's data consent) that shows no job identity of its own gets the identity of
        the posting ``open()`` loaded, as ``open()`` gives it to the form an apply link
        leads to, but only while the form continues that posting: same origin, and its
        path is the posting's or one segment below it (".../job/<id>/apply")."""
        posting = self._posting_identity
        if (posting is None or not posting.observed_url or inspection.job_identity is not None
                or inspection.kind is not PageKind.APPLICATION_FORM):
            return inspection
        base, here = urlsplit(posting.observed_url), urlsplit(inspection.observed_url)
        root, path = base.path.rstrip("/"), here.path.rstrip("/")
        below = path[len(root):] if path.startswith(root) else None
        if ((here.scheme, here.netloc) != (base.scheme, base.netloc) or below is None
                or (below and not (below.startswith("/") and below.count("/") == 1))):
            return inspection
        return inspection.model_copy(update={"job_identity": posting})

    async def reconcile(
        self,
        url: str,
        *,
        tie: ConfirmationTie,
        lookup_email: str | None = None,
        max_hops: int = 3,
    ) -> SubmissionObservation:
        """Re-read the site to establish an uncertain outcome, without resubmitting.

        Opens ``url`` (normally the application URL), follows application-status links
        and submits only GET lookup forms (an email field), then looks for acceptance
        wording tied to ``tie``. Returns ACCEPTED with signals, or UNKNOWN."""
        await self.driver.goto(url)
        looked_up = False
        notes: list[str] = []
        evidence: list[EvidenceRef] = []
        for hop in range(max_hops + 1):
            model = await self._model(evidence=f"reconcile-{hop}", html=True)
            evidence.extend(model.inspection.evidence)
            if model.inspection.kind is not PageKind.APPLICATION_FORM:
                signals, reference = self._acceptance(model, tie, fresh_reference_ties=False)
                if signals:
                    return SubmissionObservation(
                        outcome=SubmissionOutcome.ACCEPTED,
                        signals=[*signals, f"re-read at {model.snapshot.url}"],
                        confirmation_reference=reference,
                        observed_url=model.snapshot.url,
                        evidence=[*evidence, EvidenceRef(kind=EvidenceKind.CONFIRMATION_URL,
                                                         uri=model.snapshot.url,
                                                         description="status page confirming the application")],
                        detail="the site confirms this application on a later visit",
                    )
            pending = PENDING.search(model.snapshot.body_text)
            if pending:
                notes.append(f"site says {pending.group(0)!r}")
            if hop == max_hops:
                break
            origin = urlsplit(model.snapshot.url)
            link = next((lk for lk in model.snapshot.links if (
                STATUS_LINK.search(lk.text) or (
                    CONFIRMATION_LINK.search(lk.text)
                    and (urlsplit(lk.href).scheme, urlsplit(lk.href).netloc) == (origin.scheme, origin.netloc)
                )
            )), None)
            if link is not None and link.href.split("#")[0] != model.snapshot.url.split("#")[0]:
                await self.driver.goto(link.href)
                continue
            if not looked_up and lookup_email and await self._lookup(model, lookup_email):
                looked_up = True
                continue
            break
        return SubmissionObservation(
            outcome=SubmissionOutcome.UNKNOWN,
            signals=[f"observed: {n}" for n in notes],
            observed_url=self.driver.url,
            evidence=evidence,
            detail="the site does not (yet) confirm this application",
        )

    async def _lookup(self, model: PageModel, email: str) -> bool:
        """Submit a read-only status lookup: a GET form with one email field."""
        if model.form_method != "get" or model.inspection.kind is PageKind.APPLICATION_FORM:
            return False
        fields = model.candidate_fields
        emails = [f for f in fields if f.semantic_type is SemanticType.EMAIL]
        if len(fields) > 2 or len(emails) != 1:
            return False
        button = next((b for b in model.buttons if b.button.submits_form and not b.button.disabled
                       and b.intent is not ButtonIntent.SUBMIT
                       and b.button.effective_method == "get"), None)
        if button is None:
            return False
        await self.driver.fill(model.bindings[emails[0].id].selector, email)
        # Re-check the request the click would really send (formmethod/formaction/
        # formtarget overrides included) immediately before dispatching it.
        effective = await self.driver.evaluate(_EFFECTIVE_SUBMISSION, button.button.selector)
        if not self._safe_lookup(effective, model.snapshot.url):
            return False
        await self.driver.click(button.button.selector)
        await self.driver.settle(self.settle_timeout_s)
        return True

    @staticmethod
    def _safe_lookup(effective: Any, page_url: str) -> bool:
        """A lookup may only send a same-origin GET in this tab from a form without
        file or password inputs; anything else could act on an application."""
        if not isinstance(effective, dict):
            return False
        action = urlsplit(str(effective.get("action", "")))
        page = urlsplit(page_url)
        return (
            effective.get("method") == "get"
            and effective.get("target") in ("", "_self")
            and not effective.get("hasFile")
            and not effective.get("hasPassword")
            and (action.scheme, action.netloc) == (page.scheme, page.netloc)
        )

    async def close(self) -> None:
        if self._on_close is not None:
            await self._on_close()


def reconciliation_from(
    observation: SubmissionObservation,
    *,
    method: ReconciliationMethod = ReconciliationMethod.SITE_CONFIRMATION,
) -> SubmissionReconciliation | None:
    """The ``SubmissionReconciliation`` an ACCEPTED re-read establishes, else None
    (an UNKNOWN re-read settles nothing)."""
    if observation.outcome is not SubmissionOutcome.ACCEPTED:
        return None
    return SubmissionReconciliation(
        outcome=SubmissionOutcome.ACCEPTED,
        method=method,
        detail="; ".join(observation.signals),
        confirmation_reference=observation.confirmation_reference,
        evidence=observation.evidence,
    )
