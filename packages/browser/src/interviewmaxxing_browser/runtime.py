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
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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

from .driver import DriverError, NotActionable, PageDriver
from .evidence import EvidenceRecorder
from .normalize import FieldBinding, PageModel, build_page
from .signals import (
    APPLY_LINK,
    NOT_SUBMITTED_STATUS,
    PENDING,
    STATUS_LINK,
    UNCERTAIN,
    ButtonIntent,
    affirmative_acceptance,
    application_records,
    confirmation_references,
    job_ids,
)
from .snapshot import DomSnapshot, inspector_script


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
  for (const el of f.elements) {
    if (!el.willValidate || el.validity.valid) continue;  // read-only: no 'invalid' events
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
    ) -> None:
        self.driver = driver
        self.options = options
        self.policy = policy or ActionPolicy()
        self.settle_timeout_s = settle_timeout_s
        self.poll_interval_s = poll_interval_s
        self._evidence = EvidenceRecorder(options.artifacts_dir, options.artifacts_root)
        self._on_close = on_close
        self._steps_advanced = 0
        self._last: PageModel | None = None
        self._identity: JobIdentityObservation | None = None
        self._filled: tuple[str, str] | None = None  # (scope key, form fingerprint)
        self._pending: _PendingSubmit | None = None
        self._accepted = False

    # --- inspection -----------------------------------------------------------------

    async def _snapshot(self) -> DomSnapshot:
        last_error: Exception | None = None
        for _ in range(3):
            try:
                raw = await self.driver.evaluate(inspector_script())
                return DomSnapshot.model_validate(raw)
            except DriverError:
                raise
            except Exception as exc:  # e.g. the context was destroyed by a navigation
                last_error = exc
                await self.driver.settle(self.settle_timeout_s)
        raise DriverError(f"could not inspect the page: {last_error}")

    async def _model(self, *, evidence: str | None = None, html: bool = False) -> PageModel:
        snapshot = await self._snapshot()
        refs: list[EvidenceRef] = []
        if evidence:
            refs = await self._evidence.capture(
                self.driver, evidence, description=f"{evidence} at {snapshot.url}",
                html=html, text=snapshot.body_text if html else None,
            )
        model = build_page(
            snapshot,
            fallback_step=self._steps_advanced,
            http_status=self.driver.last_status,
            evidence=refs,
        )
        if model.inspection.job_identity is not None:
            self._identity = model.inspection.job_identity
        self._last = model
        return model

    def _evidence_label(self, kind: PageKind) -> str | None:
        if kind in USER_ACTION_PAGES or kind in (
            PageKind.ERROR, PageKind.UNKNOWN, PageKind.ALREADY_APPLIED, PageKind.JOB_CLOSED,
            PageKind.CONFIRMATION,
        ):
            return f"page-{kind.value.lower()}"
        return None

    async def inspect(self) -> PageInspection:
        model = await self._model()
        label = self._evidence_label(model.inspection.kind)
        if label:
            model = await self._model(evidence=label)
        return model.inspection

    @property
    def last_page(self) -> PageModel | None:
        """The most recent normalized page (for adapters and diagnostics)."""
        return self._last

    async def open(self, url: str) -> PageInspection:
        """Navigate to ``url`` exactly as supplied; follow an apply *link* (never a
        form submission) from a job posting to the application form."""
        self._steps_advanced = 0
        self._filled = None
        await self.driver.goto(url)
        model = await self._model()
        posting_identity = model.inspection.job_identity
        for _ in range(2):
            if model.inspection.kind is not PageKind.JOB_DESCRIPTION:
                break
            if not await self._follow_apply(model):
                break
            model = await self._model()
        inspection = model.inspection
        label = self._evidence_label(inspection.kind)
        if label:
            inspection = (await self._model(evidence=label)).inspection
        if inspection.job_identity is None and posting_identity is not None:
            inspection = inspection.model_copy(update={"job_identity": posting_identity})
        return inspection

    async def _follow_apply(self, model: PageModel) -> bool:
        link = next((lk for lk in model.snapshot.links if APPLY_LINK.search(lk.text)), None)
        if link is not None:
            await self.driver.goto(link.href)
            return True
        button = next(
            (b for b in model.snapshot.buttons
             if APPLY_LINK.search(b.text) and not b.submits_form and not b.disabled),
            None,
        )
        if button is None:
            return False
        await self.driver.click(button.selector)
        await self.driver.settle(self.settle_timeout_s)
        return True

    # --- fill -----------------------------------------------------------------------

    async def fill(self, form: ApplicationForm, packet: ApplicationPacket) -> FillResult:
        problems = packet.problems_against(form)
        if problems:
            raise ValueError("packet does not fit this form: " + "; ".join(problems))
        model = await self._model()
        current = model.form
        if current is None or current.fingerprint != form.fingerprint:
            raise ValueError(
                "the page no longer shows the inspected form step; re-inspect and resolve again"
            )
        errors_before = set(current.page_errors)
        results: list[FieldFillResult] = []
        for app_field in form.fields:
            binding = model.bindings[app_field.id]
            answer = packet.answer_for(app_field.id)
            if answer is None:
                results.append(await self._leave_unanswered(app_field, binding))
                continue
            try:
                results.append(await self._apply(app_field, binding, answer.value))
            except DriverError as exc:  # includes unsupported driver capabilities
                results.append(FieldFillResult(
                    field_id=app_field.id, status=FieldFillStatus.FAILED, detail=str(exc)))
        # The packet authorized exactly the inspected questions; that stays the authority.
        self._filled = (form.scope.key, form.fingerprint)
        after = await self._model(evidence=f"filled-step-{form.step}")
        new_errors = [e for e in (after.form.page_errors if after.form else []) if e not in errors_before]
        if after.form is None or after.form.fingerprint != form.fingerprint:
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
                await self.driver.set_checked(binding.selector, False,
                                              label_selector=binding.label_selectors.get(""))
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
            await self.driver.set_checked(binding.selector, False,
                                          label_selector=binding.label_selectors.get(""))
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
        if isinstance(value, TextValue) and ctype in (ControlType.TEXT, ControlType.TEXTAREA):
            await self.driver.fill(binding.selector, value.text)
            got = (await self._read(binding.selector)).get("value")
            text = value.text.replace("\r\n", "\n")
            return result(isinstance(got, str) and got.replace("\r\n", "\n") == text, got)
        if isinstance(value, ChoiceValue) and ctype is ControlType.SELECT:
            await self.driver.select_values(binding.selector, [value.value])
            got = (await self._read(binding.selector)).get("values")
            return result(got == [value.value], got)
        if isinstance(value, MultiChoiceValue) and ctype is ControlType.MULTISELECT:
            wanted = [c.value for c in value.choices]
            await self.driver.select_values(binding.selector, wanted)
            got = (await self._read(binding.selector)).get("values")
            return result(isinstance(got, list) and set(got) == set(wanted), got)
        if isinstance(value, ChoiceValue) and ctype is ControlType.RADIO:
            await self.driver.set_checked(binding.option_selectors[value.value], True,
                                          label_selector=binding.label_selectors.get(value.value))
            return await self._verify_group(fid, binding, {value.value})
        if isinstance(value, MultiChoiceValue) and ctype is ControlType.CHECKBOX_GROUP:
            wanted_set = {c.value for c in value.choices}
            for option_value, selector in binding.option_selectors.items():
                want = option_value in wanted_set
                if (option_value in binding.checked_values) != want:
                    await self.driver.set_checked(
                        selector, want, label_selector=binding.label_selectors.get(option_value))
            return await self._verify_group(fid, binding, wanted_set)
        if isinstance(value, BooleanValue) and ctype is ControlType.CHECKBOX:
            await self.driver.set_checked(binding.selector, value.checked,
                                          label_selector=binding.label_selectors.get(""))
            got = (await self._read(binding.selector)).get("checked")
            return result(got is value.checked, got)
        if isinstance(value, FileValue) and ctype is ControlType.FILE:
            artifact = value.artifact
            if not artifact.verify():
                return FieldFillResult(field_id=fid, status=FieldFillStatus.FAILED,
                                       detail=f"{artifact.filename} is missing or changed since it was verified")
            await self.driver.set_files(binding.selector, Path(artifact.path))
            got = (await self._read(binding.selector)).get("files")
            expected = [{"name": artifact.filename, "size": artifact.size_bytes}]
            return result(got == expected, got)
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
            and self._filled[1] != form.fingerprint
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
        if self._changed_since_fill(form):
            raise ValueError("questions on this step changed after filling; re-inspect and resolve")
        invalid = await self.driver.evaluate(
            _NATIVE_VALIDITY, {"form": model.form_selector, "button": form.next_selector}
        )
        if invalid:
            # The browser itself would block the step; clicking would change nothing.
            return NavigationResult(advanced=False, inspection=model.inspection,
                                    validation_errors=list(invalid))
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
        if self._changed_since_fill(form):
            return not_dispatched("questions changed after filling; re-inspect and resolve",
                                  next_state=NotSubmittedNext.FILLING)
        if model.unsupported_pending:
            return not_dispatched(
                "required controls must be operated by the user first: "
                + ", ".join(model.unsupported_pending),
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
        shows a not-submitted status is ambiguous. A page without such records is one
        record. The acceptance statement must be affirmative, and a job id or title
        must tie it to this application; a reference seen for the first time counts
        only immediately after our own submit (``fresh_reference_ties``)."""
        snapshot = model.snapshot
        candidates: list[tuple[str, str]] = []  # (statement, record text)
        records = application_records(snapshot)
        if records is None:
            page = f"{snapshot.title}\n{snapshot.body_text}"
            statement = affirmative_acceptance(snapshot.title) or affirmative_acceptance(
                snapshot.body_text[:3000]
            )
            if statement:
                candidates.append((statement, page))
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
            if refs and fresh_reference_ties and records is None:
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
        return model.inspection.kind in USER_ACTION_PAGES or bool(model.unsupported_pending)

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
            model = await self._model()
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
            link = next((lk for lk in model.snapshot.links if STATUS_LINK.search(lk.text)), None)
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
