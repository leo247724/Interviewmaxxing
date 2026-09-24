"""Canonical store state -> frontend presentation views.

Everything here is a pure function of a ``Snapshot`` read from ``ApplicationStore``.
Nothing is inferred beyond what the store recorded:

* ``receipt`` only for SUBMITTED with a stored ``Receipt``. Evidence the user reported
  (``EvidenceKind.USER_STATEMENT``, or a receipt reconciled by ``USER_CONFIRMED``) is
  shown with ``source: "user"``; only site observations are ``source: "site"``.
* ``uncertain`` for SUBMISSION_UNKNOWN, ``failure`` for FAILED_*, ``prior`` for a
  DUPLICATE whose surviving application was confirmed.
* ``needs`` for NEEDS_INPUT: the latest packet's missing inputs, with the site's own
  wording and options, or a browser interaction (sign-in, CAPTCHA).

Question ids are derived from the question's identity (form scope, field id and
fingerprint), so they are stable across re-resolution of the same question and change
when the site changes the question. An answer to an id that is not among the latest
missing inputs is stale and rejected (see ``answers.py``).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from interviewmaxxing_core import (
    Application,
    ApplicationEvent,
    ApplicationPacket,
    ApplicationRequest,
    ApplicationState,
    ControlType,
    EvidenceKind,
    EvidenceRef,
    FileValue,
    JobRecord,
    MissingInput,
    MissingReason,
    Receipt,
    ReconciliationMethod,
    SemanticType,
    SubmissionAttempt,
    UserInput,
)
from interviewmaxxing_core.packets import BooleanValue, ChoiceValue, MultiChoiceValue, TextValue

from .models import (
    ApplicationEventView,
    ApplicationView,
    AttestationView,
    ConfirmationAuthority,
    ConfirmationMethod,
    EventTone,
    EvidenceView,
    FailureView,
    InteractionNeed,
    JobIdentityView,
    PriorSubmissionView,
    ProgressView,
    QuestionControl,
    QuestionOption,
    QuestionsNeed,
    RequiredQuestionView,
    SubmissionReceiptView,
    UncertainSubmissionView,
)

S = ApplicationState
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def iso(value: datetime) -> str:
    """UTC ISO-8601 with millisecond precision and a ``Z`` suffix."""
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- snapshot ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriorRecord:
    application: Application
    application_url: str
    receipt: Receipt | None


@dataclass(frozen=True)
class RunStatus:
    """What the service's executor is doing for this application right now."""

    kind: str
    """``apply``, ``resume`` or ``reconcile``."""
    started_at: datetime


@dataclass(frozen=True)
class Snapshot:
    application: Application
    job: JobRecord
    request: ApplicationRequest
    events: Sequence[ApplicationEvent]
    attempts: Sequence[SubmissionAttempt] = ()
    evidence: Sequence[EvidenceRef] = ()
    receipt: Receipt | None = None
    packet: ApplicationPacket | None = None
    user_inputs: Sequence[UserInput] = ()
    prior: PriorRecord | None = None
    fallback_resume_name: str | None = None
    pinned_resume_name: str | None = None
    """File name of the resume pinned to this application (``pin_resume``)."""
    running: RunStatus | None = None
    answer_errors: Mapping[str, str] = field(default_factory=dict)


# --- questions --------------------------------------------------------------------------


def question_id(missing: MissingInput) -> str:
    """Stable id of one question: its form scope, field id and fingerprint."""
    scope = missing.scope.key if missing.scope else ""
    raw = f"{scope}|{missing.field_id}|{missing.field_fingerprint}"
    return "q_" + hashlib.sha256(raw.encode()).hexdigest()[:32]


_ANSWERABLE_CONTROLS = {
    ControlType.TEXT,
    ControlType.TEXTAREA,
    ControlType.TYPEAHEAD,
    ControlType.SELECT,
    ControlType.RADIO,
    ControlType.MULTISELECT,
    ControlType.CHECKBOX_GROUP,
    ControlType.CHECKBOX,
}


def is_answerable(missing: MissingInput) -> bool:
    """A field question the user can answer through the frontend."""
    return (
        missing.field_id is not None
        and missing.reason not in (MissingReason.USER_ACTION, MissingReason.UNSUPPORTED_CONTROL)
        and missing.control_type in _ANSWERABLE_CONTROLS
    )


def is_attestation(missing: MissingInput) -> bool:
    """A single checkbox statement the user makes (consent, "I certify...")."""
    return missing.control_type is ControlType.CHECKBOX and (
        missing.reason is MissingReason.UNCOVERED_ATTESTATION
        or missing.semantic_type in (SemanticType.ATTESTATION, SemanticType.CONSENT)
    )


def answerable_options(missing: MissingInput) -> list[QuestionOption]:
    """The site's own options, verbatim, minus placeholders and disabled choices,
    which are never valid answers."""
    return [
        QuestionOption(value=o.value, label=o.label)
        for o in missing.options or []
        if o.value.strip() and not o.disabled
    ]


_CONTROLS: dict[ControlType, QuestionControl] = {
    ControlType.TEXTAREA: "long_text",
    ControlType.SELECT: "single_select",
    ControlType.RADIO: "single_select",
    ControlType.MULTISELECT: "multi_select",
    ControlType.CHECKBOX_GROUP: "multi_select",
    ControlType.CHECKBOX: "boolean",
}


def _control(missing: MissingInput) -> QuestionControl:
    if missing.control_type is ControlType.TYPEAHEAD:
        # A lookup with the site's suggestions is picked like a select; without
        # suggestions the person types the place or entity to look up.
        return "single_select" if missing.options else "text"
    return _CONTROLS.get(missing.control_type or ControlType.TEXT, "text")


_REASONS = {
    MissingReason.NO_ANSWER: "Your saved details don't answer this question.",
    MissingReason.EXPLICIT_ANSWER_REQUIRED: (
        "Only you can answer this. It is never filled in from other details."
    ),
    MissingReason.UNCOVERED_ATTESTATION: "This is a statement only you can make.",
    MissingReason.AMBIGUOUS: (
        "Your saved answers disagree or could mean more than one of the site's options."
    ),
}


def view_value(value: Any) -> str | list[str] | bool | None:
    if isinstance(value, TextValue):
        return value.text
    if isinstance(value, ChoiceValue):
        return value.value
    if isinstance(value, MultiChoiceValue):
        return [c.value for c in value.choices]
    if isinstance(value, BooleanValue):
        return value.checked
    return None


def _split_question(text: str) -> tuple[str, str | None]:
    label, _, rest = text.partition("\n")
    return label, (rest.replace("\n", " ") or None)


def saved_input_for(missing: MissingInput, inputs: Sequence[UserInput]) -> UserInput | None:
    for item in inputs:
        if (
            item.field_id == missing.field_id
            and missing.scope is not None
            and item.scope == missing.scope
            and item.field_fingerprint == missing.field_fingerprint
        ):
            return item
    return None


def awaited_inputs(
    events: Sequence[ApplicationEvent], packet: ApplicationPacket | None, state: ApplicationState
) -> list[MissingInput]:
    """What a NEEDS_INPUT application is waiting for, exactly as recorded.

    The I1 runner records the questions and browser actions in the
    ``application.needs_input`` event (``metadata.missing_inputs``; I1's
    ``pending_inputs``), which also covers sign-in/CAPTCHA stops that have no packet.
    Older records without that metadata fall back to the latest packet."""
    if state is not S.NEEDS_INPUT:
        return []
    entered = _last_event(events, S.NEEDS_INPUT)
    recorded = entered.metadata.get("missing_inputs") if entered else None
    if isinstance(recorded, list):
        return [MissingInput.model_validate(m) for m in recorded]
    return list(packet.missing_inputs) if packet is not None else []


def questions_need(
    awaited: Sequence[MissingInput], inputs: Sequence[UserInput], errors: Mapping[str, str]
) -> QuestionsNeed:
    questions: list[RequiredQuestionView] = []
    attestations: list[AttestationView] = []
    saved_times: list[datetime] = []
    for missing in awaited:
        if not is_answerable(missing):
            continue
        qid = question_id(missing)
        saved = saved_input_for(missing, inputs)
        if saved is not None:
            saved_times.append(saved.provided_at)
        label, help_text = _split_question(missing.label)
        if is_attestation(missing):
            attestations.append(
                AttestationView(
                    id=qid,
                    statement=missing.label.replace("\n", " "),
                    required=missing.required,
                    accepted=bool(saved and isinstance(saved.value, BooleanValue)
                                  and saved.value.checked),
                )
            )
            continue
        choice = missing.control_type in (
            ControlType.SELECT, ControlType.RADIO, ControlType.MULTISELECT,
            ControlType.CHECKBOX_GROUP,
        )
        questions.append(
            RequiredQuestionView(
                id=qid,
                label=label,
                help=help_text,
                control=_control(missing),
                required=missing.required,
                options=answerable_options(missing) if choice else None,
                value=view_value(saved.value) if saved else None,
                max_length=None,
                reason=_REASONS.get(missing.reason),
            )
        )
    return QuestionsNeed(
        questions=questions,
        attestations=attestations,
        saved_at=iso(max(saved_times)) if saved_times else None,
        errors=dict(errors),
    )


# --- needs ------------------------------------------------------------------------------

_INTERACTION_TEXT = {
    "SIGN_IN": (
        "The site asks you to sign in. Choose Continue: a browser window opens on the "
        "application, sign in there and the application carries on."
    ),
    "CAPTCHA": (
        "The site shows a check that only a person can complete. Choose Continue: a "
        "browser window opens on the application, complete the check there and the "
        "application carries on."
    ),
    "VERIFICATION": (
        "The site needs something only you can do in the browser. Choose Continue to open "
        "the application in a browser window."
    ),
}


def _interaction_kind(text: str) -> str:
    lowered = text.lower()
    if "captcha" in lowered or "robot" in lowered:
        return "CAPTCHA"
    if "sign in" in lowered or "sign-in" in lowered or "log in" in lowered or "login" in lowered:
        return "SIGN_IN"
    return "VERIFICATION"


def _last_event(events: Sequence[ApplicationEvent], to_state: ApplicationState) -> ApplicationEvent | None:
    return next((e for e in reversed(events) if e.to_state is to_state), None)


def needs_view(snap: Snapshot) -> QuestionsNeed | InteractionNeed | None:
    if snap.application.state is not S.NEEDS_INPUT:
        return None
    entered = _last_event(snap.events, S.NEEDS_INPUT)
    meta = entered.metadata if entered else {}
    page_kind = str(meta.get("page_kind") or "")
    page_url = meta.get("observed_url") if isinstance(meta.get("observed_url"), str) else None
    if page_kind in ("SIGN_IN_REQUIRED", "CAPTCHA"):
        kind = "SIGN_IN" if page_kind == "SIGN_IN_REQUIRED" else "CAPTCHA"
        return InteractionNeed(
            interaction=kind, instructions=_INTERACTION_TEXT[kind], page_url=page_url
        )
    awaited = awaited_inputs(snap.events, snap.packet, snap.application.state)
    if awaited:
        actions = [m for m in awaited if m.reason is MissingReason.USER_ACTION]
        if actions:
            kind = _interaction_kind(" ".join(f"{m.label} {m.prompt}" for m in actions))
            return InteractionNeed(
                interaction=kind,
                instructions=_INTERACTION_TEXT[kind],
                page_url=page_url,
            )
        need = questions_need(awaited, snap.user_inputs, snap.answer_errors)
        if need.questions or need.attestations:
            return need
        blocked = [m for m in awaited if m.required and not is_answerable(m)]
        if blocked:
            labels = "; ".join(f"“{m.label.splitlines()[0]}”" for m in blocked)
            return InteractionNeed(
                interaction="VERIFICATION",
                instructions=(
                    f"The form asks something this tool can't fill in: {labels}. Choose "
                    "Continue to open the application in a browser window and answer it there."
                ),
                page_url=page_url,
            )
    reason = meta.get("reason")
    kind = _interaction_kind(reason if isinstance(reason, str) else "")
    return InteractionNeed(
        interaction=kind,
        instructions=_INTERACTION_TEXT[kind],
        page_url=page_url,
    )


# --- evidence ---------------------------------------------------------------------------


def evidence_href(public_base: str, application_id: str, evidence: EvidenceRef) -> str | None:
    if not evidence.path or not SAFE_ID.match(evidence.id) or not SAFE_ID.match(application_id):
        return None
    return f"{public_base}/applications/{application_id}/evidence/{evidence.id}"


def evidence_view(public_base: str, application_id: str, evidence: EvidenceRef) -> EvidenceView:
    kind = evidence.kind
    user = kind is EvidenceKind.USER_STATEMENT
    label = {
        EvidenceKind.SCREENSHOT: "Screenshot of the page",
        EvidenceKind.HTML_SNAPSHOT: "Saved copy of the page",
        EvidenceKind.PAGE_TEXT: "Text on the page",
        EvidenceKind.CONFIRMATION_URL: "Confirmation page address",
        EvidenceKind.CONFIRMATION_EMAIL: "Confirmation email",
        EvidenceKind.USER_STATEMENT: "Your report",
        EvidenceKind.OTHER: "Observation",
    }[kind]
    view_kind = {
        EvidenceKind.SCREENSHOT: "screenshot",
        EvidenceKind.HTML_SNAPSHOT: "page_text",
        EvidenceKind.PAGE_TEXT: "page_text",
        EvidenceKind.CONFIRMATION_URL: "page_url",
        EvidenceKind.CONFIRMATION_EMAIL: "email",
        EvidenceKind.USER_STATEMENT: "user_report",
        EvidenceKind.OTHER: "page_text",
    }[kind]
    value = evidence.description.strip() or None
    if kind is EvidenceKind.CONFIRMATION_URL and evidence.uri:
        value = evidence.uri
    return EvidenceView(
        kind=view_kind,
        label=label,
        value=value,
        href=evidence_href(public_base, application_id, evidence),
        observed_at=iso(evidence.captured_at),
        source="user" if user else "site",
    )


def confirmation_of(receipt: Receipt) -> tuple[ConfirmationMethod, ConfirmationAuthority]:
    """How and by whom acceptance was established. ``user`` exactly for
    ``USER_CONFIRMED``, whatever site artifacts the receipt also lists."""
    if receipt.reconciliation_method is None:
        return "SUBMISSION_OBSERVED", "site"
    method: ConfirmationMethod = receipt.reconciliation_method.value
    by_user = receipt.reconciliation_method is ReconciliationMethod.USER_CONFIRMED
    return method, "user" if by_user else "site"


def _receipt_view(public_base: str, receipt: Receipt) -> SubmissionReceiptView:
    by_user = receipt.reconciliation_method is ReconciliationMethod.USER_CONFIRMED
    items = [evidence_view(public_base, receipt.application_id, e) for e in receipt.evidence]
    for signal in receipt.signals:
        items.append(
            EvidenceView(
                kind="user_report" if by_user else "page_text",
                label="Your report" if by_user else "What the site showed",
                value=signal,
                href=None,
                observed_at=iso(receipt.confirmed_at),
                source="user" if by_user else "site",
            )
        )
    method, authority = confirmation_of(receipt)
    return SubmissionReceiptView(
        receipt_id=f"rcpt_{receipt.attempt_id}",
        submitted_at=iso(receipt.submitted_at),
        confirmation_reference=receipt.confirmation_reference,
        evidence=items,
        confirmation_method=method,
        confirmation_authority=authority,
    )


# --- events -----------------------------------------------------------------------------


def _event_text(event: ApplicationEvent) -> tuple[str, EventTone]:
    meta = event.metadata
    name = event.event
    if name == "application.requested":
        return "Application request recorded.", "info"
    if name == "application.request_repeated":
        disposition = meta.get("disposition")
        repeated: dict[str, tuple[str, EventTone]] = {
            "ALREADY_SUBMITTED": (
                "You asked to apply again. The earlier submission stands; nothing was sent.",
                "info",
            ),
            "SUBMISSION_IN_PROGRESS": ("You asked again while it was submitting.", "info"),
            "SUBMISSION_UNKNOWN": (
                "You asked to apply again. The earlier submission still needs settling, so "
                "nothing was sent.",
                "warning",
            ),
            "CLOSED": ("You asked to apply again. This application is closed.", "info"),
        }
        return repeated.get(str(disposition), ("You asked to apply again; carrying on.", "info"))
    if name == "application.submitted":
        method = meta.get("reconciliation")
        if method == ReconciliationMethod.USER_CONFIRMED.value:
            return "Marked as submitted on your report of a confirmation. Receipt saved.", "success"
        if method:
            return "Confirmation found on a later check. Receipt saved.", "success"
        return "The site confirmed the submission. Receipt saved.", "success"
    if name == "application.submission_unknown":
        if meta.get("reason") == "interrupted":
            return (
                "Submitting was interrupted before the site's answer was seen. Applying again "
                "is locked until the outcome is settled.",
                "warning",
            )
        return (
            "The site didn't confirm the submission. Applying again is locked until the "
            "outcome is settled.",
            "warning",
        )
    if name == "application.failed_retryable":
        return f"Stopped: {meta.get('failure_reason') or 'the application could not continue'}.", "warning"
    if name == "application.failed_permanent":
        return f"Stopped: {meta.get('failure_reason') or 'the application cannot continue'}.", "error"
    fixed: dict[str, tuple[str, EventTone]] = {
        "application.preparation_only": ("Preparation only: submission is disabled.", "info"),
        "preparation.ready": ("Ready for final review. Nothing was submitted.", "attention"),
        "application.inspecting": ("Reading the application page.", "progress"),
        "application.packet_ready": ("Answers prepared from your saved details.", "progress"),
        "application.filling": ("Filling in the form.", "progress"),
        "application.needs_input": ("Waiting for you.", "attention"),
        "application.submitting": ("Submitting the application.", "progress"),
        "application.duplicate": ("Already applied to this job. Nothing was sent.", "info"),
        "application.withdrawn": ("Withdrawn.", "info"),
        "job.merged": ("This link is the same job as an earlier one.", "info"),
        "input.received": ("Your answers were saved.", "info"),
        "evidence.recorded": ("Evidence saved.", "info"),
        "submission.evidence_recorded": ("Evidence saved.", "info"),
        "form.discovered": ("Found the application form.", "progress"),
        "field.unresolved": ("A question needs your answer.", "attention"),
        "validation.failed": ("The site rejected some entries.", "warning"),
        "reconcile.user_reported_not_received": (
            "You reported that no confirmation arrived. Applying again stays locked until "
            "the site itself shows the application was not received.",
            "attention",
        ),
        "service.run_stopped": ("The run stopped unexpectedly.", "error"),
    }
    if name in fixed:
        return fixed[name]
    if name == "job.identity_bound":
        title, company = meta.get("title"), meta.get("company")
        if title and company:
            return f"Identified the job: {title} at {company}.", "info"
        return "Identified the job.", "info"
    if name == "packet.saved":
        step = meta.get("form_step")
        missing = meta.get("missing_field_ids") or []
        page = f" for page {int(step) + 1}" if isinstance(step, int) else ""
        if missing:
            return f"Prepared answers{page}; {len(missing)} need you.", "progress"
        return f"Prepared answers{page}.", "progress"
    if name == "document.resume_pinned":
        filename = meta.get("filename")
        return (f"Resume for this application: {filename}." if filename
                else "Resume chosen for this application."), "info"
    if name == "reconcile.unconfirmed":
        return (
            "Checked the site again. No confirmation tied to this job yet; applying again "
            "stays locked.",
            "info",
        )
    if name == "page.completed":
        return "Finished a page of the form.", "progress"
    if name == "reconcile.checked":
        detail = meta.get("detail")
        return (f"Checked the site again. {detail}" if detail else "Checked the site again."), "info"
    return name.replace(".", " ").replace("_", " ").capitalize() + ".", "info"


def _is_request(event: ApplicationEvent) -> bool:
    return event.event in ("application.requested", "application.request_repeated")


def events_view(events: Sequence[ApplicationEvent]) -> list[ApplicationEventView]:
    """Events oldest first. A repeated request directly after another request for the
    same URL (the runner re-recording the request the service just recorded) is
    folded into it."""
    out: list[ApplicationEventView] = []
    previous: ApplicationEvent | None = None
    for event in events:
        if (
            event.event == "application.request_repeated"
            and previous is not None
            and _is_request(previous)
            and previous.metadata.get("application_url") == event.metadata.get("application_url")
        ):
            previous = event
            continue
        message, tone = _event_text(event)
        if event.event.startswith("document."):
            # A pin recorded between the service's request and the runner's own
            # re-record does not break the fold.
            out.append(ApplicationEventView(
                id=event.id, type=event.event, at=iso(event.timestamp), message=message,
                tone=tone,
            ))
            continue
        out.append(
            ApplicationEventView(
                id=event.id, type=event.event, at=iso(event.timestamp), message=message, tone=tone
            )
        )
        previous = event
    return out


# --- application ------------------------------------------------------------------------

_ATS_NAMES = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workday": "Workday"}


def _ats_name(ats_type: str | None) -> str | None:
    if not ats_type or ats_type.lower() in ("generic", "unknown"):
        return None
    return _ATS_NAMES.get(ats_type.lower(), ats_type)


def _resume_name(snap: Snapshot) -> str | None:
    if snap.pinned_resume_name:
        return snap.pinned_resume_name
    if snap.packet is not None:
        for answer in snap.packet.answers:
            if answer.semantic_type is SemanticType.RESUME and isinstance(answer.value, FileValue):
                return answer.value.artifact.filename
    return snap.fallback_resume_name


def _uncertain_view(snap: Snapshot, public_base: str) -> UncertainSubmissionView | None:
    app = snap.application
    if app.state is not S.SUBMISSION_UNKNOWN:
        return None
    attempt = snap.attempts[-1] if snap.attempts else None
    interrupted = attempt is not None and attempt.outcome == "INTERRUPTED"
    reason = (
        "Submitting was interrupted before the site's answer was seen."
        if interrupted
        else "Submit was dispatched, but the site didn't show a confirmation."
    )
    checks = [
        e for e in snap.events
        if e.event in ("reconcile.checked", "reconcile.unconfirmed",
                       "reconcile.user_reported_not_received")
    ]
    last = checks[-1] if checks else None
    result = _event_text(last)[0] if last else None
    if snap.running is not None and snap.running.kind == "reconcile":
        result = "Checking the site now. This can take a moment."
    return UncertainSubmissionView(
        attempted_at=iso(attempt.started_at if attempt else app.updated_at),
        reason=reason,
        evidence=[evidence_view(public_base, app.id, e) for e in snap.evidence],
        last_checked_at=iso(last.timestamp) if last else None,
        last_check_result=result,
    )


def _failure_view(snap: Snapshot, public_base: str) -> FailureView | None:
    app = snap.application
    if app.state not in (S.FAILED_RETRYABLE, S.FAILED_PERMANENT):
        return None
    retryable = app.state is S.FAILED_RETRYABLE
    return FailureView(
        reason=app.failure_reason or "The application could not continue.",
        detail=(
            "Nothing was submitted. You can try again."
            if retryable
            else "Nothing was submitted, and this application can't continue."
        ),
        retryable=retryable,
        evidence=[evidence_view(public_base, app.id, e) for e in snap.evidence],
    )


def _prior_view(snap: Snapshot) -> PriorSubmissionView | None:
    prior = snap.prior
    if snap.application.state is not S.DUPLICATE or prior is None or prior.receipt is None:
        return None
    return PriorSubmissionView(
        application_id=prior.application.id,
        application_url=prior.application_url,
        submitted_at=iso(prior.receipt.submitted_at),
        confirmation_reference=prior.receipt.confirmation_reference,
    )


def application_view(snap: Snapshot, *, public_base: str) -> ApplicationView:
    app = snap.application
    receipt = (
        _receipt_view(public_base, snap.receipt)
        if app.state is S.SUBMITTED and snap.receipt is not None
        else None
    )
    progress = (
        ProgressView(page=snap.packet.form_step + 1, page_count=None)
        if snap.packet is not None
        else None
    )
    return ApplicationView(
        id=app.id,
        state=app.state.value,
        application_url=snap.request.application_url,
        job=JobIdentityView(
            title=snap.job.title, company=snap.job.company, ats=_ats_name(snap.job.ats_type)
        ),
        requested_at=iso(snap.request.requested_at),
        updated_at=iso(app.updated_at),
        progress=progress,
        resume_file_name=_resume_name(snap),
        needs=needs_view(snap),
        receipt=receipt,
        prior=_prior_view(snap),
        failure=_failure_view(snap, public_base),
        uncertain=_uncertain_view(snap, public_base),
        events=events_view(snap.events),
    )
