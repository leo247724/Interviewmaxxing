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
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
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
from .aria import LookupOutcome, MenuProbe, fill_lookup, fill_phone
from .driver import DriverError, NotActionable, PageContextLost, PageDriver, file_anchor, file_shown
from .evidence import EvidenceRecorder
from .normalize import FieldBinding, PageModel, build_page, detect_ats
from .signals import (
    APPLY_LINK,
    CONFIRMATION_LINK,
    NOT_SUBMITTED_STATUS,
    PENDING,
    STATUS_LINK,
    UNCERTAIN,
    ButtonIntent,
    affirmative_acceptance,
    application_records,
    confirmation_references,
    job_ids,
    lookup_matches,
)
from .snapshot import DomButton, DomSnapshot, inspector_script


class SubmissionRefused(RuntimeError):
    """The runtime refused an action that could submit (or resubmit) an application."""


class AmbiguousAction(RuntimeError):
    """The page offers no unambiguous control for the requested action."""


_READ_CONTROL = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  if (el.tagName === 'SELECT') return {values: Array.from(el.selectedOptions).map((o) => o.value)};
  if (el.type === 'file') return {files: Array.from(el.files || []).map((f) => ({name: f.name, size: f.size}))};
  if (el.type === 'checkbox' || el.type === 'radio') return {checked: el.checked};
  return {value: el.value};
}"""

_READ_CHECKED = """(sels) => sels.map((s) => { const e = document.querySelector(s); return e ? e.checked : null; })"""

_NATIVE_VALIDITY = """({form, button}) => {
  const f = form ? document.querySelector(form) : null;
  const b = button ? document.querySelector(button) : null;
  if (!f || f.noValidate || (b && b.formNoValidate)) return [];
  const out = [];
  const CAPTCHA_TOKENS = ['g-recaptcha-response', 'h-captcha-response', 'cf-turnstile-response'];
  for (const el of f.elements) {
    if (!el.willValidate || el.validity.valid) continue;  // read-only: no 'invalid' events
    if (CAPTCHA_TOKENS.includes(el.name)) continue;  // filled by the CAPTCHA widget, reported separately
    const label = (el.labels && el.labels[0] ? el.labels[0].innerText : el.name || el.id || el.type);
    const line = label.replace(/\\s+/g, ' ').replace(/[\\s*:]+$/, '').trim() + ': ' + el.validationMessage;
    if (!out.includes(line)) out.push(line);
  }
  return out;
}"""

_EFFECTIVE_SUBMISSION = """(sel) => {
  const b = document.querySelector(sel);
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
"""Upper bound on the network/document settle inside one readiness wait."""

_COOKIE_TEXT = re.compile(r"cookie", re.IGNORECASE)
_COOKIE_DECLINE = re.compile(
    r"^(?:decline(?: all)?(?: cookies)?|reject(?: all)?(?: cookies)?|only necessary|"
    r"necessary(?: cookies)? only|essential only|use necessary cookies only)$",
    re.IGNORECASE,
)
_COOKIE_ACCEPT = re.compile(
    r"^(?:accept(?: all)?(?: cookies)?|allow all(?: cookies)?|agree|i agree|got it|"
    r"i understand|ok|okay)$",
    re.IGNORECASE,
)
"""Consent-banner buttons. A decline-group button is preferred; an accept-group button
is the fallback. Only a visible button that does not submit a form and is not inside
the selected application form is ever clicked, at most once per ``open()``."""


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


def _binding_shape(bindings: Mapping[str, FieldBinding]) -> dict[str, Any]:
    return {key: [b.selector, b.control_type.value, sorted(b.option_selectors.items()),
                  sorted(b.label_selectors.items())] for key, b in bindings.items()}


def _same_questions(approved: ApplicationForm, current: ApplicationForm) -> bool:
    return approved.scope.key == current.scope.key and (
        {f.id: f.fingerprint for f in approved.fields} == {f.id: f.fingerprint for f in current.fields})


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
        self._active_fill_signature: str | None = None
        self._steps_advanced = 0
        self._last: PageModel | None = None
        self._identity: JobIdentityObservation | None = None
        self._filled: tuple[str, str] | None = None  # (scope key, form fingerprint)
        self._context_lost = False
        """The document was replaced mid-fill; fresh inspection and refill are required."""
        self._inspected_after_loss = False
        self._fill_document: str | None = None
        self._fill_bindings: dict[str, Any] = {}
        self._uploads: dict[str, _Upload] = {}
        """Files attached in this document whose input the uploader emptied or replaced."""
        self._pending: _PendingSubmit | None = None
        self._accepted = False

    # --- inspection -----------------------------------------------------------------

    async def _snapshot(self) -> DomSnapshot:
        """One read-only inspection, with the menus probed earlier in this document
        attached (probing itself happens only in ``_model``)."""
        last_error: Exception | None = None
        for _ in range(3):
            try:
                raw = await self.driver.evaluate(inspector_script())
                return self.menus.merge(DomSnapshot.model_validate(raw))
            except DriverError:
                raise
            except Exception as exc:  # e.g. the context was destroyed by a navigation
                last_error = exc
                await self.driver.settle(self.settle_timeout_s)
        raise DriverError(f"could not inspect the page: {last_error}")

    async def _probe(self, model: PageModel) -> bool:
        """Probe the application form's closed menu controls that are not cached yet
        (bounded; see ``MenuProbe``). True when the page was touched and must be re-read
        with every menu closed again."""
        if model.form is None:
            return False
        targets = self.menus.targets(model.snapshot, model.form_index)
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
            model = build_page(snapshot, fallback_step=self._steps_advanced,
                               http_status=self.driver.last_status)
            if probe:
                probe = False
                if await self._probe(model):
                    snapshot = await self._snapshot()
                    model = build_page(snapshot, fallback_step=self._steps_advanced,
                                       http_status=self.driver.last_status)
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
            fresh = build_page(fresh_snapshot, fallback_step=self._steps_advanced,
                               http_status=self.driver.last_status)
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
        model = await self._with_uploads(model)
        refs: list[EvidenceRef] = []
        if evidence:
            refs = await self._evidence.capture(
                self.driver, evidence, description=f"{evidence} at {snapshot.url}",
                html=html, text=snapshot.body_text if html else None,
            )
        if refs:
            model = replace(model, inspection=model.inspection.model_copy(update={"evidence": refs}))
        if self.annotator is not None and model.form is not None:
            self._observations.append((model.form, document, observation_signature(model)))
            self._observations = self._observations[-32:]
        if model.inspection.job_identity is not None:
            self._identity = model.inspection.job_identity
        self._last = model
        return model

    async def _with_uploads(self, model: PageModel) -> PageModel:
        """Keep a file question this runtime answered in this document as it was approved
        while its uploader shows that file: the uploader may replace the input with the
        file's name (Greenhouse unmounts the input and its Attach and cloud buttons) or
        show the name beside it (Workable). The question is still on the page, answered;
        the name it shows is the answer, not new wording."""
        form = model.form
        if not self._uploads or form is None:
            return model
        fields = list(form.fields)
        bindings = dict(model.bindings)
        changed = False
        for field_id, upload in sorted(self._uploads.items(), key=lambda item: item[1].index):
            if upload.document != model.snapshot.document:
                continue
            present = next((i for i, f in enumerate(fields) if f.id == field_id), None)
            if present is not None and fields[present].fingerprint == upload.field.fingerprint:
                continue
            if not await file_shown(self.driver, upload.binding.selector, upload.name,
                                    anchor=upload.anchor, wait_s=0.0, emptied=False):
                continue
            changed = True
            if present is not None:
                fields[present] = upload.field
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
        model = await self._model()
        if self._context_lost:
            self._inspected_after_loss = True
        label = self._evidence_label(model.inspection.kind)
        if label:
            model = await self._model(evidence=label)
        return model.inspection

    async def _await_ready(self) -> PageModel:
        """Bounded readiness before classifying a freshly loaded document.

        A page that already classifies is used as it is. An ``UNKNOWN`` page (an SPA
        still rendering its form, a consent overlay) gets ``settle_timeout_s`` in
        total: the document and network settle first, then the page is re-read every
        ``_READY_POLL_S`` until it classifies, or until it stops changing between two
        consecutive reads while showing no loading indicator. Static pages therefore
        classify within a couple of seconds; nothing on the page is touched."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settle_timeout_s
        model = await self._model()
        if model.inspection.kind is not PageKind.UNKNOWN:
            return model
        await self.driver.settle(min(self.settle_timeout_s, _READY_SETTLE_S))
        model = await self._model()
        previous = self._readiness_key(model)
        while model.inspection.kind is PageKind.UNKNOWN and loop.time() < deadline:
            await asyncio.sleep(max(0.0, min(_READY_POLL_S, deadline - loop.time())))
            model = await self._model()
            key = self._readiness_key(model)
            if key == previous and not model.snapshot.loading_indicator:
                break
            previous = key
        return model

    @staticmethod
    def _readiness_key(model: PageModel) -> tuple[str, int, int]:
        return (observation_signature(model), len(model.snapshot.body_text),
                len(model.snapshot.controls))

    async def _dismiss_cookie_banner(self, model: PageModel) -> bool:
        """Click one consent button (decline preferred) that lies outside the
        application form, then let the page settle. Returns whether a click happened.
        Nothing about the page is logged."""
        if not _COOKIE_TEXT.search(model.snapshot.body_text):
            return False
        candidates = [
            b for b in model.snapshot.buttons
            if not b.submits_form and not b.disabled
            and (b.form_index == -1
                 or (model.form_index is not None and b.form_index != model.form_index))
        ]

        def first(pattern: re.Pattern[str]) -> DomButton | None:
            return next((b for b in candidates if pattern.match(b.text.strip())), None)

        button = first(_COOKIE_DECLINE) or first(_COOKIE_ACCEPT)
        if button is None:
            return False
        try:
            await self.driver.click(button.selector)
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
        if self._context_lost or self._changed_since_fill(form):
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
        if self._changed_since_fill(after.form):
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
        """Navigate to ``url`` exactly as supplied; follow an apply *link* (never a
        form submission) from a job posting to the application form."""
        self._steps_advanced = 0
        self._filled = None
        self._context_lost = False
        self._fill_document = None
        await self.driver.goto(url)
        model = await self._await_ready()
        consent_done = await self._dismiss_cookie_banner(model)
        if consent_done:
            model = await self._await_ready()
        posting_identity = model.inspection.job_identity
        for _ in range(2):
            if model.inspection.kind is not PageKind.JOB_DESCRIPTION:
                break
            if not await self._follow_apply(model):
                break
            model = await self._await_ready()
            if not consent_done:
                consent_done = await self._dismiss_cookie_banner(model)
                if consent_done:
                    model = await self._await_ready()
        inspection = model.inspection
        label = self._evidence_label(inspection.kind)
        if label:
            inspection = (await self._model(evidence=label)).inspection
        if inspection.job_identity is None and posting_identity is not None:
            inspection = inspection.model_copy(update={"job_identity": posting_identity})
        return inspection

    async def observe(self, url: str) -> PageInspection:
        """Open precisely this URL and classify it without following or acting on forms
        (menus are not expanded either)."""
        self._observing = True
        try:
            await self.driver.goto(url)
            await self._await_ready()
            return await self.inspect()
        finally:
            self._observing = False

    async def _follow_apply(self, model: PageModel) -> bool:
        link = next((lk for lk in model.snapshot.links if APPLY_LINK.search(lk.text)), None)
        if link is not None:
            await self.driver.goto(link.href)
            return True
        # An apply control leads to the form. One that submits a form is clicked only
        # when that form has no fillable field at all (a navigation form), never when
        # anything on the page could be sent with it.
        button = next(
            (b for b in model.apply_controls
             if not b.disabled
             and (not b.submits_form or model.fillable_counts.get(b.form_index, 0) == 0)),
            None,
        )
        if button is None:
            return False
        await self.driver.click(button.selector)
        await self.driver.settle(self.settle_timeout_s)
        return True

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
        issued = next((item for item in self._observations if item[0] is form), None)
        model = await self._model()
        current = model.form
        if self.annotator is not None:
            document = str(await self.driver.evaluate(_DOCUMENT_IDENTITY))
            if (issued is None or issued[1] != document
                    or issued[2] != observation_signature(model)):
                raise ValueError("the annotated document or field bindings changed; re-inspect and resolve")
        if current is None or current.fingerprint != form.fingerprint:
            raise ValueError(
                "the page no longer shows the inspected form step; re-inspect and resolve again"
            )
        self._fill_document = str(await self.driver.evaluate(_DOCUMENT_IDENTITY))
        errors_before = set(current.page_errors)
        results: list[FieldFillResult] = []
        lost: PageContextLost | None = None
        self._active_fill_signature = observation_signature(model)
        self._fill_bindings = _binding_shape(model.bindings)
        final_signature: str | None = None
        try:
            for app_field in form.fields:
                binding = model.bindings[app_field.id]
                answer = packet.answer_for(app_field.id)
                if only is not None and (app_field.id not in only or answer is None):
                    continue  # a selective fill never touches or reports other controls
                if lost is not None:
                    if answer is not None:
                        results.append(FieldFillResult(
                            field_id=app_field.id, status=FieldFillStatus.FAILED,
                            detail="not attempted: the page changed while filling an earlier field"))
                    continue
                try:
                    await self._assert_fill_context()
                    if answer is None:
                        results.append(await self._leave_unanswered(app_field, binding))
                    else:
                        outcome = await self._apply(app_field, binding, answer.value)
                        results.append(outcome)
                        if (app_field.control_type is ControlType.FILE
                                and outcome.status is FieldFillStatus.FILLED):
                            await self._accept_own_upload(form, app_field)
                    await self._assert_fill_context()
                except PageContextLost as exc:
                    # The document or approved questions changed. Every remaining
                    # answer is reported without writing through stale bindings.
                    lost = exc
                    self._context_lost = True
                    self._inspected_after_loss = False
                    self.menus.reset()
                    results.append(FieldFillResult(
                        field_id=app_field.id, status=FieldFillStatus.FAILED, detail=str(exc)))
                except DriverError as exc:  # per-field, page still the same: keep going
                    results.append(FieldFillResult(
                        field_id=app_field.id, status=FieldFillStatus.FAILED, detail=str(exc)))
        finally:
            final_signature = self._active_fill_signature
            self._active_fill_signature = None
        if lost is not None:
            self._filled = None
            self._context_lost = True
            evidence: list[EvidenceRef] = []
            with contextlib.suppress(DriverError):
                evidence = await self._evidence.capture(
                    self.driver, f"context-lost-step-{form.step}",
                    description="page after its document or approved questions changed mid-fill")
            return FillResult(
                form_step=form.step, fields=results, evidence=evidence,
                page_errors=[f"{lost}; the step must be inspected and resolved again"],
            )
        self._context_lost = False
        self._inspected_after_loss = False
        # The packet authorized exactly the inspected questions; that stays the authority.
        self._filled = (form.scope.key, form.fingerprint)
        self._filled_structure = final_signature or observation_signature(model)
        # Let a passing state from the last write settle before the page is read again.
        await self._settled_model(self._filled_structure)
        label = f"filled-step-{form.step}" if only is None else f"filled-step-{form.step}-again"
        after = await self._model(evidence=label)
        await self._assert_fill_context()
        new_errors = [e for e in (after.form.page_errors if after.form else []) if e not in errors_before]
        if (after.form is None or after.form.fingerprint != form.fingerprint
                or self._changed_since_fill(after.form)):
            results.extend(await self._contain_changed_questions(form, after))
            new_errors.append(
                "questions on this step changed while filling; re-inspect and resolve again "
                "before continuing"
            )
        return FillResult(
            form_step=form.step,
            fields=results,
            page_errors=new_errors,
            evidence=after.inspection.evidence,
        )

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

    async def _assert_fill_freshness(self) -> None:
        """Check trusted DOM constraints before every mutation, never reclassify.

        This also runs between individual checkbox-group writes. An earlier input
        handler may replace a later question without replacing the document.
        """
        await self._assert_fill_context()
        if self._active_fill_signature is None:
            return
        fresh = await self._settled_model(self._active_fill_signature)
        await self._assert_fill_context()
        if observation_signature(fresh) != self._active_fill_signature:
            raise PageContextLost(
                "questions, bindings, actions, or employer context changed while filling; "
                "remaining answers were not attempted; re-inspect and resolve again"
            )

    async def _fresh_model(self) -> PageModel:
        """The page as the fill guard sees it now (probed menus and own uploads kept)."""
        try:
            snapshot = await self._snapshot()
            return await self._with_uploads(build_page(
                snapshot, fallback_step=self._steps_advanced, http_status=self.driver.last_status))
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
        while observation_signature(fresh) != expected and loop.time() < end:
            await asyncio.sleep(_FLICKER_POLL_S)
            fresh = await self._fresh_model()
        return fresh

    async def _accept_own_upload(self, form: ApplicationForm, app_field: ApplicationField) -> None:
        """An uploader changes its own controls once it takes a file: its Attach and cloud
        buttons give way to the file's name and a Remove button, and the input may go.
        That change is the answer, not a changed page: when every other question and
        binding is as approved, the fill continues against the page as it now is."""
        if self._active_fill_signature is None:
            return
        upload = self._uploads.get(app_field.id)
        if upload is not None and upload.index < 0:
            index = [f.id for f in form.fields].index(app_field.id)
            self._uploads[app_field.id] = replace(upload, index=index)
        # Past any passing state (attaching fires input events too), then two reads agree.
        fresh = await self._settled_model(self._active_fill_signature)
        if observation_signature(fresh) == self._active_fill_signature:
            return
        for _ in range(int(_FLICKER_S / _FLICKER_POLL_S)):
            await asyncio.sleep(_FLICKER_POLL_S)
            again = await self._fresh_model()
            settled = observation_signature(again) == observation_signature(fresh)
            fresh = again
            if settled:
                break
        if (fresh.form is not None and _same_questions(form, fresh.form)
                and _binding_shape(fresh.bindings) == self._fill_bindings):
            self._active_fill_signature = observation_signature(fresh)

    async def _write(self, operation: Callable[[], Awaitable[None]]) -> None:
        await self._assert_fill_freshness()
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
        self, approved: ApplicationForm, after: PageModel
    ) -> list[FieldFillResult]:
        """Questions that appeared or changed while filling were not authorized by the
        packet. Never leave a pre-checked consent/attestation among them checked."""
        if after.form is None:
            return []
        known = {f.id: f.fingerprint for f in approved.fields}
        results = []
        for new in after.form.fields:
            if known.get(new.id) == new.fingerprint:
                continue
            detail = "appeared or changed while filling; not answered by this packet"
            binding = after.bindings[new.id]
            if (
                new.control_type is ControlType.CHECKBOX
                and new.semantic_type in (SemanticType.CONSENT, SemanticType.ATTESTATION)
                and binding.checked_values
            ):
                await self._write(partial(self.driver.set_checked,
                    binding.selector, False, label_selector=binding.label_selectors.get("")))
                detail += "; cleared its pre-checked box"
            results.append(FieldFillResult(field_id=new.id, status=FieldFillStatus.FAILED,
                                           detail=detail))
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
        if isinstance(value, TextValue) and ctype is ControlType.TEXT and app_field.expects_international_phone:
            # Typed as given: "+<code><number>" makes the widget's picker choose the
            # country itself. The readback compares digits, not the widget's formatting.
            phone: list[tuple[bool, str]] = []

            async def type_phone() -> None:
                phone.append(await fill_phone(self.driver, binding.selector, value.text))

            await self._write(type_phone)
            return result(*phone[0])
        if isinstance(value, TextValue) and ctype in (ControlType.TEXT, ControlType.TEXTAREA):
            await self._write(lambda: self.driver.fill(binding.selector, value.text))
            got = (await self._read(binding.selector)).get("value")
            text = value.text.replace("\r\n", "\n")
            return result(isinstance(got, str) and got.replace("\r\n", "\n") == text, got)
        if isinstance(value, TextValue) and ctype is ControlType.TYPEAHEAD and binding.aria is not None:
            outcomes: list[LookupOutcome] = []

            async def look_up() -> None:
                outcomes.append(await fill_lookup(
                    self.driver, binding.selector, value.text, dict(binding.aria or {}),
                    lookup_matches, before_action=self._assert_fill_freshness))

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
                        before_action=self._assert_fill_freshness))

                await self._write(select_accessible)
                if selected == [value.value] and (binding.aria or {}).get("probed"):
                    self.menus.confirm(binding.selector, value.value)
                return result(selected == [value.value], selected)
            await self._write(lambda: self.driver.select_values(binding.selector, [value.value]))
            got = (await self._read(binding.selector)).get("values")
            return result(got == [value.value], got)
        if isinstance(value, MultiChoiceValue) and ctype is ControlType.MULTISELECT:
            wanted = [c.value for c in value.choices]
            await self._write(lambda: self.driver.select_values(binding.selector, wanted))
            got = (await self._read(binding.selector)).get("values")
            return result(isinstance(got, list) and set(got) == set(wanted), got)
        if isinstance(value, ChoiceValue) and ctype is ControlType.RADIO:
            await self._write(lambda: self.driver.set_checked(
                binding.option_selectors[value.value], True,
                label_selector=binding.label_selectors.get(value.value)))
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
            artifact = value.artifact
            name = Path(artifact.path).name
            taken = self._uploads.get(fid)
            if (taken is not None and taken.name == name
                    and taken.document == str(await self.driver.evaluate(_DOCUMENT_IDENTITY))
                    and await file_shown(self.driver, binding.selector, name, anchor=taken.anchor, wait_s=0.0)):
                return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED,
                                       detail="the uploader already shows this file")
            if not artifact.verify():
                return FieldFillResult(field_id=fid, status=FieldFillStatus.FAILED,
                                       detail=f"{artifact.filename} is missing or changed since it was verified")
            anchor = await file_anchor(self.driver, binding.selector)
            await self._write(lambda: self.driver.set_files(binding.selector, Path(artifact.path)))
            if not artifact.verify():
                return FieldFillResult(field_id=fid, status=FieldFillStatus.FAILED,
                                       detail="the pinned file changed while attaching it")
            got = (await self._read(binding.selector)).get("files")
            expected = [{"name": artifact.filename, "size": artifact.size_bytes}]
            document = str(await self.driver.evaluate(_DOCUMENT_IDENTITY))
            if got == expected:
                # An uploader that keeps the file may still show its name beside the input.
                self._uploads[fid] = _Upload(app_field, binding, anchor, name, document)
                return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED)
            if got in ([], None) and await file_shown(self.driver, binding.selector, name, anchor=anchor):
                # The uploader took the file and emptied or replaced its input (Workable,
                # Greenhouse); it shows the file's name and no error.
                self._uploads[fid] = _Upload(app_field, binding, anchor, name, document)
                return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED,
                                       detail="the uploader shows the file; its input is empty or replaced")
            return result(False, got)
        return FieldFillResult(field_id=fid, status=FieldFillStatus.FAILED,
                               detail=f"cannot apply a {type(value).__name__} to {ctype}")

    async def _verify_group(self, fid: str, binding: FieldBinding, wanted: set[str]) -> FieldFillResult:
        values = list(binding.option_selectors)
        checked = await self.driver.evaluate(_READ_CHECKED, [binding.option_selectors[v] for v in values])
        got = {v for v, c in zip(values, checked, strict=True) if c}
        if got == wanted:
            return FieldFillResult(field_id=fid, status=FieldFillStatus.FILLED)
        return FieldFillResult(field_id=fid, status=FieldFillStatus.VERIFICATION_MISMATCH,
                               detail=f"checked options read back as {sorted(got)}")

    def _changed_since_fill(self, form: ApplicationForm) -> bool:
        return (
            self._filled is not None
            and self._filled[0] == form.scope.key
            and (self._filled[1] != form.fingerprint
                 or (self._filled_structure is not None and self._last is not None
                     and self._filled_structure != observation_signature(self._last)))
        )

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
        if self._changed_since_fill(form):
            raise ValueError("questions on this step changed after filling; re-inspect and resolve")
        invalid = await self.driver.evaluate(
            _NATIVE_VALIDITY, {"form": model.form_selector, "button": form.next_selector}
        )
        if invalid:
            # The browser itself would block the step; clicking would change nothing.
            return NavigationResult(advanced=False, inspection=model.inspection,
                                    validation_errors=list(invalid))
        await self._assert_fill_context()
        await self.driver.click(form.next_selector)
        await self.driver.settle(self.settle_timeout_s)
        self._steps_advanced += 1
        after = await self._model()
        if after.form is not None and _same_step_shown_again(form, after.form):
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
        if self._changed_since_fill(form):
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
            or bool(model.unsupported_pending)
            or (self.options.allow_submission and model.inspection.captcha_pending)
        )

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
        while True:
            # Never open a menu while the person may be operating the page.
            model = await self._model(probe=False)
            if not self._needs_user(model):
                break
            if deadline is not None and loop.time() >= deadline:
                break
            await asyncio.sleep(self.poll_interval_s)
        return (await self._model(evidence="after-user-action")).inspection

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
