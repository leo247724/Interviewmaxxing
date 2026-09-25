"""The reusable local application runner (I1).

``LocalApplicationRunner`` drives one application at a time through the canonical
pieces: the candidate store (C2), the factual packet resolver (C3), the browser
runtime (C4) and the ``ApplicationStore``, which remains the only state authority.
The CLI and the local HTTP service (S1) both use it::

    runner = create_runner(LocalPaths.from_env(), headless=False, interaction=interaction)
    outcome = await runner.apply(url, candidate_id="default")
    outcome = await runner.resume(application_id)
    outcome = await runner.reconcile(application_id)

Construction opens nothing (no database, browser or terminal), so a runner can be
built on any thread. Each call opens its own store connection on the thread running
it and closes it before returning.

Guarantees:

* Preparation is the default. Nothing is submitted unless the user approved the
  prepared application and its submission was authorized (``submit``); a
  ``prepare_only=False`` run of any other application records the no-submit
  restriction and prepares (``submit_unapproved`` is for synthetic tests only).
  Questions are asked only for required answers the verified data cannot give, and
  the user is asked to act only for sign-in, CAPTCHA or custom controls.
* ``SUBMITTING`` is durably recorded before the submit click, and any interruption
  (exception, cancellation, Ctrl-C, SIGTERM) during the submit records
  ``SUBMISSION_UNKNOWN``. Only site acceptance tied to this job yields SUBMITTED.
* Missing input is durable: the packet (with scoped ``MissingInput`` items) and the
  ``application.needs_input`` event carry the exact questions, so a later process
  re-presents them (``pending_inputs``).
* The claim on the application stays alive while the run waits for the user or the
  browser (questions, a sign-in, a CAPTCHA, custom controls): a heartbeat renews it
  every ``RunLimits.claim_heartbeat_s``. If the claim is lost anyway (the process was
  suspended past the TTL, or another run took over) the wait is cancelled and the run
  ends without writing anything more; every store write is fenced by the claim token.
* Each application keeps the resume it started with: the first run pins the
  profile's resume (``ApplicationStore.pin_resume``) and every later run uses the
  pinned file, whatever the profile says now. If that file is missing or changed the
  run stops; another resume is never substituted.
* One run per browser profile (an OS file lock) and per application (store claim).
* Advancing and submitting are distinct; an ambiguous step stops the run. A run
  stops after ``RunLimits.max_steps`` pages, when the same form comes back
  ``max_same_form`` times, or when one step has gone back to the user
  ``max_input_rounds`` times, so it cannot loop.
* An answer the site rejects is asked again. Rejections are persisted as
  ``validation.rejected`` events (one per inspection of a step this run acted on), so
  a correction stored later, in this run or by ``interviewmaxxing answer`` in another
  process, is used even if the site still shows the old message; a correction the
  site rejects again is asked again. The site's message is stored redacted
  (``redact_detail``): sites echo the value that was typed.
* A lookup the site could not commit (``NEEDS_CHOICE``) gets one choice round per
  question per run: the resolver, when it is a ``SuggestionChooser``, may pick one of
  the observed suggestions, which is then typed verbatim (a ``field.suggestion_chosen``
  event records it); otherwise the user is asked to pick one. It is never reported as
  a failed fill.
* A prepare-only run records, with ``preparation.ready``, every step it filled: the
  step's packet and the questions it answered (``steps``: id, fingerprint, required
  flag, semantic type, control, options digest, short label per field).
* A submission run (``prepare_only=False``) of an application the user approved and
  authorized (``ApplicationStore.authorize_submission``) submits exactly the approved
  packets: after opening and inspecting the site again, each step is filled from its
  approved packet and nothing is resolved or generated again. Before filling, every
  question must match the approved one (fingerprint, required flag, options, semantic
  type) and the final step must still be the approved one. A new, missing or changed
  question, a ``VERIFICATION_MISMATCH``, a lookup that no longer commits or answers the
  site rejects stop the run as NEEDS_INPUT ("The form no longer matches the approved
  application") before any submit, and the approval is invalidated. A site that opens
  a draft it kept at a later page is walked back to the first approved page with the
  site's own previous-step control when the browser offers one (``StepBack``), so
  every approved page is still checked and filled; otherwise the run stops as
  NEEDS_INPUT before filling anything ("The site resumed a draft it kept"), the
  approval is withdrawn and the user submits that draft in the browser.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import re
import socket
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TypeVar, runtime_checkable

from interviewmaxxing_browser import (
    AmbiguousAction,
    ConfirmationTie,
    PlaywrightSessionFactory,
    SubmissionRefused,
    consent_gate,
    reconciliation_from,
    user_action_needs,
)
from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_core import (
    PRE_SUBMISSION_STATES,
    SUBMISSION_BLOCKING_STATES,
    TERMINAL_STATES,
    AnswerSource,
    Application,
    ApplicationBrowser,
    ApplicationEvent,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    ApplyOutcome,
    BooleanValue,
    BrowserOptions,
    BrowserSessionFactory,
    CandidateLoader,
    CandidateNotFound,
    CandidateProfile,
    CandidateProfileInvalid,
    Claim,
    ClaimLost,
    ClaimUnavailable,
    ControlType,
    FieldFillStatus,
    FieldOption,
    FillResult,
    IdentityConflict,
    LocalPaths,
    MissingInput,
    MissingReason,
    NotFound,
    PacketAnswer,
    PacketContext,
    PacketResolver,
    PageInspection,
    PageKind,
    ReconciliationMethod,
    ResumeArtifact,
    SavedAnswer,
    SubmissionApproval,
    SubmissionObservation,
    SubmissionOutcome,
    TextValue,
    UserInput,
    UserInteraction,
    answer_problems,
    new_id,
    normalize_text,
    utc_now,
)
from interviewmaxxing_core.interfaces import SelectiveFill, SuggestionChooser
from interviewmaxxing_generation import FactualPacketResolver, lookup_alternatives, missing_input_id

S = ApplicationState
T = TypeVar("T")

NEEDS_INPUT_EVENT = "application.needs_input"
INPUT_EVENT = "input.received"
REJECTION_EVENT = "validation.rejected"
"""Emitted by the runner (``append_event``) when a step it acted on comes back with
field validation messages: a rejection epoch for those questions."""
PROVIDER_EVENT = "provider.budget"
"""Emitted by the runner once per run when its resolver used AI providers: calls, known
cost, unknown-cost calls and latency, in total and by purpose (``provider_usage``)."""
SUGGESTION_EVENT = "field.suggestion_chosen"
"""Emitted by the runner when the resolver chose one of a lookup's site suggestions:
the question, the chosen label and the chooser's decision metadata. (The store
reserves the ``application.`` prefix for its own events.)"""
CONSENT_EVENT = "consent.accepted"
"""Emitted by the runner when it accepted a data-processing consent page in front of the
application form (round 14): the page's question ("I accept the <policy>"), its URL, and
where the answer came from (the person's saved statement or input that covers it, by id).
The page's own acceptance is the only thing it sends; no application is submitted."""
PREPARED_EVENT = "preparation.ready"
"""Emitted by a prepare-only run at the final review step: the form step and URL, the
prepared packet, ``submitted: False`` and every filled step with its questions
(``steps``), which an approval pins."""
MISMATCH_MESSAGE = "The form no longer matches the approved application"
"""How a submission run reports a form that differs from the approved one: it stops as
NEEDS_INPUT before any submit and the approval is invalidated."""
KEPT_DRAFT_MESSAGE = "The site resumed a draft it kept"
"""How a submission run reports a site that opened the application's kept draft at a
later page than an approved one, when the browser cannot go back to it (``StepBack``):
the earlier approved pages can be neither checked nor filled, so the run stops as
NEEDS_INPUT before filling anything and the approval is invalidated (with this reason).
Preparing again reopens the same draft; the user submits it in the browser."""
KEPT_DRAFT_REASON = "site resumed a kept draft"
"""The ``reason`` of that NEEDS_INPUT stop (``application.needs_input`` metadata)."""
NOT_AUTHORIZED_MESSAGE = "Not authorized for submission"
"""How ``submit`` reports an application without a current authorization; nothing is
opened."""
BUSY_MESSAGE = "another run is using the browser profile"
"""The outcome message when another run holds the browser profile; nothing was run."""
CLAIMED_MESSAGE = "Another run is working on this application."
"""The outcome message when another run holds the application's claim; nothing was run."""
ROUTING_EVENT = "routing.trace"
"""Emitted by the runner once per resolved step when its resolver routes through AI:
the full-form route decisions for every field (route, source scope, semantic type and
their probabilities) and a projection of the decision traces added since the previous
step, so a held or failed field can be diagnosed from the store without running the
site again. Values never enter it: see ``project_trace``."""

_TRACE_VALUE_KEYS = frozenset({
    "answer", "content", "draft", "evidence", "facts", "missing_information", "prompt",
    "rendered", "response", "review_feedback", "review_issues", "sentences", "text", "typed",
    "typed_value", "value", "values",
})
"""Trace keys that carry fact values, generated prose, review text or typed text."""
_TRACE_TEXT_LIMIT = 300
_WRITER_STAGES = frozenset({"draft", "corrective_rewrite", "strong_review", "humanize", "narrative",
                            "writer", "motivation_narrative"})
"""Stages whose free-text reasons quote the writer's or reviewer's own words."""
_RECORDED_MARK = "_recorded_by_runner"
_QUOTED = re.compile(r"""(['"]).*?\1""")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"https?://\S+")
_DIGITS = re.compile(r"\+?\d[\d\s().-]{2,}\d")
"""Four or more characters of digits and phone punctuation (a number, a date, an id)."""


def project_trace(trace: dict[str, Any]) -> dict[str, Any]:
    """The diagnostic part of a resolver trace: stage, field ids, statuses, scores,
    choices, rule names and page wording. Keys that carry the candidate's values,
    drafted prose or review text are dropped at every depth, and remaining strings are
    cut to ``_TRACE_TEXT_LIMIT`` characters. Non-JSON values become their string form."""
    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): clean(v) for k, v in value.items() if str(k) not in _TRACE_VALUE_KEYS}
        if isinstance(value, list | tuple):
            return [clean(v) for v in value]
        if isinstance(value, bool | int | float) or value is None:
            return value
        text = value if isinstance(value, str) else str(value)
        return text if len(text) <= _TRACE_TEXT_LIMIT else text[:_TRACE_TEXT_LIMIT] + "…"
    cleaned = clean(trace)
    if not isinstance(cleaned, dict):
        return {}
    cleaned.pop(_RECORDED_MARK, None)
    if cleaned.get("stage") in _WRITER_STAGES:
        cleaned.pop("reason", None)  # writer-side reasons quote model text; statuses suffice
    return cleaned


def redact_detail(detail: str | None) -> str | None:
    """A fill result's detail with every quoted value replaced by an ellipsis, so the
    shape ("reads back ['…']") is kept and the value read from the page is not. Unquoted
    email addresses, URLs and runs of four or more digits (phone numbers, dates, ids) are
    replaced too."""
    if not detail:
        return None
    redacted = _DIGITS.sub("…", _URL.sub("…", _EMAIL.sub("…@…", _QUOTED.sub("'…'", detail))))
    return redacted if len(redacted) <= _TRACE_TEXT_LIMIT else redacted[:_TRACE_TEXT_LIMIT] + "…"


def _decision_projection(decision: Any) -> dict[str, Any]:
    """A route decision without its boilerplate requirement text; the reason is cut."""
    data: dict[str, Any] = decision.model_dump(mode="json")
    data.pop("source_requirement", None)
    reason = data.get("reason")
    if isinstance(reason, str) and len(reason) > 200:
        data["reason"] = reason[:200] + "…"
    return data


def _traces_since(traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The traces not yet recorded by this runner, marked once they are read. A mark on the
    resolver's own dict outlives list truncation and cannot be confused by a reused object
    address, unlike matching by identity."""
    fresh = [trace for trace in traces if not trace.get(_RECORDED_MARK)]
    for trace in fresh:
        trace[_RECORDED_MARK] = True
    return fresh
RUN_LOCK_NAME = ".interviewmaxxing-run.lock"
LATE_COST_WAIT_S = 10.0
"""How long a cancelled run waits for resolver work still running before its final cost."""
LABEL_LIMIT = 80
ATTEMPT_STARTS: frozenset[ApplicationState] = frozenset(
    {S.REQUESTED, S.FAILED_RETRYABLE, S.FAILED_PERMANENT, S.DUPLICATE})
"""Transitions after which a form is filled from its first page again. A question stop
(NEEDS_INPUT) is not one: the resumed run carries on in the draft the site kept, so the
pages filled before it belong to the same attempt (as the review lane's
``views.preparing_attempt``)."""


def attempt_events(events: Sequence[ApplicationEvent]) -> list[ApplicationEvent]:
    """The events of the current attempt: those after the last transition into one of
    ``ATTEMPT_STARTS``."""
    for index in range(len(events) - 1, -1, -1):
        if events[index].to_state in ATTEMPT_STARTS:
            return list(events[index + 1:])
    return list(events)


class SavedAnswerStore(CandidateLoader, Protocol):
    def save_answer(self, candidate_id: str, answer: SavedAnswer) -> None: ...


@runtime_checkable
class StepBack(Protocol):
    """Optional ``ApplicationBrowser`` capability: go back one page of a multi-step form,
    in the same draft, with the site's own previous-step (Back) control, and inspect the
    page it shows. Raises ``AmbiguousAction`` when the page offers no single unambiguous
    way back. A submission run uses it to walk a kept draft back to its first approved
    page; nothing is filled on the way back."""

    async def previous_step(self) -> PageInspection: ...


@dataclass(frozen=True, slots=True)
class RunLimits:
    max_steps: int = 12
    """Pages inspected in one run before it stops (FAILED_RETRYABLE)."""
    max_same_form: int = 3
    """Times the same form (fingerprint) may be shown in one run before it stops."""
    max_input_rounds: int = 5
    """Times one form step may go back to the user (questions or a browser action)
    before the run stops as NEEDS_INPUT with the open questions recorded."""
    user_action_timeout_s: float = 600.0
    """How long ``wait_for_user`` waits for sign-in, CAPTCHA or custom controls."""
    claim_ttl_s: float = 300.0
    """Lease on the application; renewed at every step and by the heartbeat."""
    claim_heartbeat_s: float | None = None
    """How often the claim is renewed while the run waits for the user or the
    browser. Default: a third of ``claim_ttl_s``."""

    @property
    def heartbeat_s(self) -> float:
        if self.claim_heartbeat_s is not None:
            return self.claim_heartbeat_s
        return self.claim_ttl_s / 3


class RunnerBusy(RuntimeError):
    """Another run holds the browser profile."""


class _Stop(Exception):
    """Internal: end the run with this outcome."""

    def __init__(self, outcome: ApplyOutcome) -> None:
        super().__init__(outcome.message)
        self.outcome = outcome


@contextlib.contextmanager
def browser_profile_lock(browser_dir: Path) -> Iterator[None]:
    """Exclusive, non-blocking OS lock for the persistent browser profile. The lock
    disappears with the process, so a crash never leaves it stale."""
    browser_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(browser_dir / RUN_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunnerBusy(BUSY_MESSAGE) from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def pending_inputs(store: ApplicationStore, application_id: str) -> list[MissingInput]:
    """The questions and actions a NEEDS_INPUT application is waiting for, exactly as
    last recorded (from the latest ``application.needs_input`` event). Empty for any
    other state."""
    app = store.get_application(application_id)
    if app.state is not S.NEEDS_INPUT:
        return []
    for event in reversed(store.list_events(application_id)):
        if event.event == NEEDS_INPUT_EVENT:
            return [MissingInput.model_validate(m) for m in event.metadata.get("missing_inputs", [])]
    return []


def rejection_epochs(
    store: ApplicationStore, application_id: str
) -> tuple[dict[tuple[int, str, str], int], dict[str, int]]:
    """The persisted rejection epochs of an application.

    Returns the latest ``validation.rejected`` event sequence per question
    ``(form step, field id, field fingerprint)`` and the ``input.received`` event
    sequence of every stored user input. A rejected answer is asked again only while
    its rejection is newer than the user's answer to that question."""
    rejections, received = _rejection_history(store, application_id)
    return {key: epoch for key, (epoch, _) in rejections.items()}, received


def _rejection_history(
    store: ApplicationStore, application_id: str
) -> tuple[dict[tuple[int, str, str], tuple[int, str]], dict[str, int]]:
    """Keep each rejection's message with its epoch even after the DOM clears it. The
    message is redacted again on reading: rejections recorded before messages were
    stored redacted must not bring a typed value into a new question's prompt."""
    rejections: dict[tuple[int, str, str], tuple[int, str]] = {}
    received: dict[str, int] = {}
    for event in store.list_events(application_id):
        if event.event == REJECTION_EVENT:
            step = event.metadata.get("form_step")
            if step is None:
                continue
            for item in event.metadata.get("fields", []):
                rejections[(int(step), item["field_id"], item["field_fingerprint"])] = (
                    event.sequence, redact_detail(item["message"]) or "")
        elif event.event == INPUT_EVENT:
            for item in event.metadata.get("inputs", []):
                received[item["id"]] = event.sequence
    return rejections, received


def _outcome(store: ApplicationStore, application_id: str, message: str,
             missing: Sequence[MissingInput] = ()) -> ApplyOutcome:
    app = store.get_application(application_id)
    if app.state is S.NEEDS_INPUT and not missing:
        missing = pending_inputs(store, application_id)
    return ApplyOutcome(
        application_id=application_id,
        state=app.state,
        receipt=store.get_receipt(application_id),
        missing_inputs=list(missing),
        message=message,
    )


def _detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}".splitlines()[0][:300]


def _chosen_answer(answer: PacketAnswer, label: str) -> PacketAnswer:
    """The same answer, typed as the exact site suggestion chosen for its value. The
    source (identity or saved answer) and its references are unchanged; the note names
    the value the suggestion was chosen for."""
    typed = answer.value.text if isinstance(answer.value, TextValue) else ""
    note = "; ".join(p for p in (answer.provenance.note,
                                 f"site suggestion chosen for the typed value {typed!r}") if p)
    return answer.model_copy(update={
        "value": TextValue(text=label),
        "provenance": answer.provenance.model_copy(update={"note": note}),
    })


def _lookup_question(form: ApplicationForm, field: ApplicationField, typed: str,
                     suggestions: Sequence[str]) -> MissingInput:
    """Ask the user to pick one of a lookup's observed suggestions; the picked label is
    typed verbatim. The suggestions travel as the item's ``options`` (value = label)."""
    labels = list(dict.fromkeys(s for s in suggestions if s.strip()))
    shown = repr(typed) if typed.strip() else "the value"
    if labels:
        prompt = (f"The site did not accept {shown} as typed; choose the suggestion it offered "
                  "that is right for you: " + "; ".join(labels) + ". You can also enter a "
                  "different value to look up.")
    else:
        prompt = (f"The site offered no suggestion for {shown}. Enter the value to look up the "
                  "way the site spells it.")
    item = MissingInput.for_field(form, field, reason=MissingReason.NO_ANSWER, prompt=prompt)
    update: dict[str, Any] = {"id": missing_input_id(form, field)}
    if field.control_type is ControlType.TYPEAHEAD:  # other controls keep their own options
        update["options"] = [FieldOption(value=label, label=label) for label in labels] or None
    return item.model_copy(update=update)


def _merged(first: FillResult, refill: FillResult) -> FillResult:
    """``first`` with the fields ``refill`` operated again replaced by their new results."""
    redone = {r.field_id: r for r in refill.fields}
    fields = [redone.pop(r.field_id, r) for r in first.fields]
    return FillResult(form_step=first.form_step, fields=[*fields, *redone.values()],
                      page_errors=refill.page_errors, evidence=[*first.evidence, *refill.evidence])


def _fail_retryable(store: ApplicationStore, claim: Claim, app: Application, message: str) -> None:
    """Record the current failure, re-entering INSPECTING when retrying a stopped run."""
    if app.state in (S.NEEDS_INPUT, S.FAILED_RETRYABLE):
        store.transition(claim, S.INSPECTING)
    if store.get_application(app.id).state is not S.FAILED_RETRYABLE:
        store.transition(claim, S.FAILED_RETRYABLE, failure_reason=message)


# --- approved submission ----------------------------------------------------------------


def _short(text: str, limit: int = LABEL_LIMIT) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _steps_text(numbers: Sequence[int]) -> str:
    """``step 1``, ``steps 1 and 2``, ``steps 1, 2 and 3``."""
    shown = [str(n) for n in numbers]
    if len(shown) == 1:
        return f"step {shown[0]}"
    return f"steps {', '.join(shown[:-1])} and {shown[-1]}"


def options_digest(field: ApplicationField) -> str | None:
    """The field's options alone (value and visible label, order-free), so a changed
    option set can be told apart from changed wording. None without options."""
    if not field.options:
        return None
    items = sorted([o.value, normalize_text(o.label)] for o in field.options)
    raw = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def field_record(field: ApplicationField) -> dict[str, Any]:
    """What ``preparation.ready`` pins about one question for a later approval: its
    fingerprint (wording, control and options), required flag, semantic type, control,
    options digest and a short label for messages."""
    return {"id": field.id, "fingerprint": field.fingerprint, "required": field.required,
            "semantic_type": field.semantic_type.value, "control_type": field.control_type.value,
            "options": options_digest(field), "label": _short(field.question_text) or field.id}


def _step_record(form: ApplicationForm, packet: ApplicationPacket) -> dict[str, Any]:
    """One filled step as an approval pins it: its packet and the questions it answered."""
    return {"form_step": form.step, "packet_id": packet.id, "form_url": form.url,
            "form_fingerprint": form.fingerprint,
            "fields": [field_record(f) for f in form.fields]}


@dataclass(frozen=True)
class _ApprovedStep:
    packet: ApplicationPacket
    fields: dict[str, dict[str, Any]] | None
    """The approved questions by field id, or None for a preparation recorded before
    questions were pinned (the packet's own form fingerprint is compared instead)."""


@dataclass(frozen=True)
class _Approved:
    approval: SubmissionApproval
    steps: dict[int, _ApprovedStep]
    final_step: int


def _approved_application(store: ApplicationStore, approval: SubmissionApproval) -> _Approved:
    """The approved packets and the questions pinned for each step (NotFound when a
    packet is gone)."""
    prepared = next((e for e in store.list_events(approval.application_id)
                     if e.id == approval.preparation_event_id), None)
    recorded: dict[tuple[int, str], dict[str, dict[str, Any]]] = {}
    for item in (prepared.metadata.get("steps") if prepared is not None else None) or []:
        if (isinstance(item, dict) and isinstance(item.get("form_step"), int)
                and isinstance(item.get("packet_id"), str) and isinstance(item.get("fields"), list)):
            recorded[(item["form_step"], item["packet_id"])] = {
                f["id"]: f for f in item["fields"] if isinstance(f, dict) and isinstance(f.get("id"), str)}
    steps = {s.form_step: _ApprovedStep(packet=store.get_packet(s.packet_id),
                                        fields=recorded.get((s.form_step, s.packet_id)))
             for s in approval.steps}
    if not steps:
        steps = {approval.form_step or 0: _ApprovedStep(packet=store.get_packet(approval.packet_id),
                                                        fields=None)}
    final = approval.form_step if approval.form_step is not None else max(steps)
    return _Approved(approval=approval, steps=steps, final_step=final)


def _awaiting_user(step: _ApprovedStep, form: ApplicationForm) -> list[ApplicationField]:
    """Required custom controls the approved answers do not fill because the user operated
    them in the browser while preparing (an operated control reads as not required)."""
    pending = []
    for field in form.fields:
        if (field.control_type is not ControlType.UNSUPPORTED or not field.required
                or step.packet.answer_for(field.id) is not None):
            continue
        record = step.fields.get(field.id) if step.fields is not None else None
        if step.fields is None or (record is not None and record.get("fingerprint") == field.fingerprint
                                   and record.get("required") is False):
            pending.append(field)
    return pending


def approval_mismatches(step: _ApprovedStep | None, form: ApplicationForm, *, final_step: int,
                        awaiting: Sequence[ApplicationField] = ()) -> list[str]:
    """Why ``form`` (inspected, not yet filled) is not the approved step: a step that was
    not approved, a moved final step, a new, missing or changed question (wording,
    control, options, required flag), or a required question the approved answers do
    not fill. Empty when the approved packet may be filled into it as it is. The
    semantic type is the runtime's reading, not the site's question, so it is not
    compared (``bound_packet`` follows the current reading). ``awaiting``: custom
    controls left to the user, not counted as changed."""
    if step is None:
        return [f"step {form.step + 1} was not part of the approved application"]
    problems: list[str] = []
    if (form.is_final_step is True) != (form.step == final_step):
        problems.append(f"step {form.step + 1} now submits the application" if form.is_final_step
                        else f"step {form.step + 1} no longer submits the application")
    skip = {f.id for f in awaiting}
    if step.fields is not None:
        current = {f.id: f for f in form.fields}
        for field_id, record in step.fields.items():
            label = str(record.get("label") or field_id)
            field = current.get(field_id)
            if field is None:
                problems.append(f"the question {label!r} is no longer on the form")
            elif field.fingerprint != record.get("fingerprint"):
                problems.append(f"the options of {label!r} changed"
                                if options_digest(field) != record.get("options")
                                else f"the question {label!r} changed")
            elif field.required != record.get("required") and field_id not in skip:
                problems.append(f"{label!r} is {'now' if field.required else 'no longer'} required")
        for field in form.fields:
            if field.id not in step.fields:
                kind = "required question" if field.required else "question"
                problems.append(f"a new {kind} {_short(field.question_text) or field.id!r} appeared")
    elif (step.packet.form_step != form.step or form.model_copy(
            update={"url": step.packet.form_url}).fingerprint != step.packet.form_fingerprint):
        # Pinned before questions were recorded: the form must ask exactly the questions
        # the approved packet answered (ids, wording, options), whatever its URL.
        problems.append("the questions differ from the ones the approved answers were prepared for")
    if not problems:
        unanswered = [f for f in form.required_fields()
                      if step.packet.answer_for(f.id) is None and f.id not in skip]
        problems += [f"the required question {_short(f.question_text) or f.id!r} has no approved answer"
                     for f in unanswered]
    return problems


def bound_packet(packet: ApplicationPacket, form: ApplicationForm) -> ApplicationPacket:
    """The approved packet bound to this inspection of its step, with the same id,
    values and provenance: a step URL may carry a new draft or session id, and each
    answer takes the semantic type the runtime reads for its (unchanged) question now."""
    answers = []
    for answer in packet.answers:
        field = form.find(answer.field_id)
        if field is not None and field.semantic_type is not answer.semantic_type:
            answer = answer.model_copy(update={"semantic_type": field.semantic_type})
        answers.append(answer)
    if (packet.form_url == form.url and packet.form_fingerprint == form.fingerprint
            and answers == packet.answers):
        return packet
    return packet.model_copy(update={
        "form_url": form.url,
        "form_fingerprint": form.fingerprint,
        "answers": answers,
        "missing_inputs": [m.model_copy(update={"form_url": form.url}) if m.field_id is not None else m
                           for m in packet.missing_inputs],
    })


_STATE_MESSAGES: dict[ApplicationState, str] = {
    S.SUBMITTED: "Already submitted; the site confirmed it. See the receipt.",
    S.SUBMITTING: "A submission is in progress or was interrupted; it will not be repeated.",
    S.SUBMISSION_UNKNOWN: "A previous submit may have reached the employer. It will not be "
                          "retried; reconcile it first.",
    S.DUPLICATE: "This job duplicates an application that already exists.",
    S.WITHDRAWN: "The application was withdrawn.",
    S.FAILED_PERMANENT: "The application cannot be completed.",
}


class LocalApplicationRunner:
    """Concrete ``ApplicationRunner`` over local storage and a real browser.

    Preparation is the default: complete known fields and stop before final submit.
    ``prepare_only=False`` is reserved for the explicitly authorized submission caller
    (``submit``, via ``create_submission_runner``): once the user approved a prepared
    application and its submission was authorized, it fills exactly the approved
    packets and submits them. It never overrides a stored preparation-only restriction,
    and any other run it makes (``apply`` or ``resume`` of an application without an
    authorized approval, even one never restricted) records the restriction and
    prepares, as a preparation-only runner does.

    ``submit_unapproved=True`` is for synthetic tests only: a ``prepare_only=False`` run
    may then submit an application that was never restricted with the packets it
    resolves itself. No production caller sets it.
    """

    def __init__(
        self,
        *,
        paths: LocalPaths,
        interaction: UserInteraction,
        headless: bool = False,
        browser_factory: BrowserSessionFactory | None = None,
        candidates: SavedAnswerStore | None = None,
        resolver: PacketResolver | None = None,
        prepare_only: bool = True,
        limits: RunLimits | None = None,
        owner: str | None = None,
        clock: Callable[[], datetime] = utc_now,
        submit_unapproved: bool = False,
    ) -> None:
        self.paths = paths
        self.interaction = interaction
        self.headless = headless
        self.browser_factory = browser_factory or PlaywrightSessionFactory()
        self.candidates: SavedAnswerStore = candidates or LocalCandidateStore.from_paths(paths)
        self.resolver = resolver or FactualPacketResolver()
        self.prepare_only = prepare_only
        self.submit_unapproved = submit_unapproved
        """Synthetic tests only (see the class docstring)."""
        self.limits = limits or RunLimits()
        self.owner = owner or f"runner:{socket.gethostname()}:{os.getpid()}"
        self.clock = clock
        """The store's clock (claims, events). Tests use a virtual clock."""

    # --- public API -------------------------------------------------------------------

    async def apply(self, application_url: str, *, candidate_id: str) -> ApplyOutcome:
        """Record the request (idempotent) and run it unless the stored state forbids
        it. A repeated request for a submitted, in-flight or uncertain application
        returns that state without touching the browser."""
        with self._store() as store:
            result = store.record_request(candidate_id, application_url)
            app_id = result.application.id
            if not result.may_proceed:
                return self._blocked(store, app_id)
            return await self._run(store, app_id, application_url)

    async def resume(self, application_id: str) -> ApplyOutcome:
        """Continue a stopped application (missing input answered, sign-in done, a
        retryable failure) from a fresh inspection of the site."""
        with self._store() as store:
            app = store.get_application(application_id)
            if app.state in SUBMISSION_BLOCKING_STATES or app.state in TERMINAL_STATES:
                return self._blocked(store, application_id)
            url = store.list_requests(application_id)[0].application_url
            return await self._run(store, application_id, url)

    async def submit(self, application_id: str) -> ApplyOutcome:
        """Submit exactly what the user approved. The application must be authorized
        (``ApplicationStore.authorize_submission`` after ``approve_submission``) and the
        runner built with ``prepare_only=False``; otherwise nothing is opened and the
        outcome says so. The site is opened and inspected again and each step is filled
        from its approved packet; a form that no longer matches the approval stops as
        NEEDS_INPUT before any submit, with the approval withdrawn."""
        with self._store() as store:
            app = store.get_application(application_id)
            if app.state in SUBMISSION_BLOCKING_STATES or app.state in TERMINAL_STATES:
                return self._blocked(store, application_id)
            if self.prepare_only or store.submission_authorization(application_id) is None:
                return self._not_authorized(store, application_id)
            url = store.list_requests(application_id)[0].application_url
            return await self._run(store, application_id, url, submission=True)

    def _not_authorized(self, store: ApplicationStore, application_id: str) -> ApplyOutcome:
        reason = ("this runner is preparation-only" if self.prepare_only else
                  "approve the prepared application, then authorize its submission")
        return _outcome(store, application_id,
                        f"{NOT_AUTHORIZED_MESSAGE}: {reason}. Nothing was opened or submitted.")

    async def reconcile(self, application_id: str) -> ApplyOutcome:
        """Re-read the site for a SUBMISSION_UNKNOWN application, never resubmitting.
        Only acceptance tied to this job (its id or title shown with confirmation
        wording) is recorded, as SUBMITTED with a receipt; otherwise it stays unknown."""
        with self._store() as store:
            store.recover_interrupted_submissions()
            app = store.get_application(application_id)
            if app.state is not S.SUBMISSION_UNKNOWN:
                return _outcome(store, application_id,
                                f"Only an uncertain submission can be reconciled; this one is "
                                f"{app.state.value}.")
            job = store.get_job(app.job_id)
            tie = ConfirmationTie.from_job(job)
            if not (tie.external_job_id or tie.job_title):
                return _outcome(store, application_id,
                                "The job's identity (id or title) was never observed, so a "
                                "confirmation cannot be tied to it. Check with the employer and "
                                "record the result with `interviewmaxxing reconcile`.")
            email = self._lookup_email(app.candidate_id)
            url = job.application_url
            try:
                with browser_profile_lock(self.paths.browser_dir):
                    claim = store.claim(application_id, self.owner, ttl=self._ttl)
                    try:
                        try:
                            browser = await self._start_browser(application_id)
                        except Exception as exc:
                            return _outcome(store, application_id, self._browser_start_failed(exc)
                                            + " The application stays SUBMISSION_UNKNOWN and "
                                            "nothing was resubmitted; reconcile again afterwards.")
                        try:
                            observation = await browser.reconcile(  # type: ignore[attr-defined]
                                url, tie=tie, lookup_email=email)
                        except Exception as exc:
                            with contextlib.suppress(Exception):
                                store.append_event(claim, "reconcile.failed", {"error": _detail(exc)})
                            return _outcome(store, application_id,
                                            f"Could not re-check the site ({_detail(exc)}). The "
                                            "application stays SUBMISSION_UNKNOWN and nothing was "
                                            "resubmitted; reconcile again later.")
                        finally:
                            await browser.close()
                        return self._record_reconciliation(store, claim, observation)
                    finally:
                        store.release(claim)
            except RunnerBusy as exc:
                return _outcome(store, application_id, str(exc))
            except ClaimUnavailable:
                return _outcome(store, application_id, CLAIMED_MESSAGE)

    # --- plumbing -------------------------------------------------------------------------

    @property
    def _ttl(self) -> timedelta:
        return timedelta(seconds=self.limits.claim_ttl_s)

    @contextlib.contextmanager
    def _store(self) -> Iterator[ApplicationStore]:
        self.paths.ensure()
        store = ApplicationStore.open(self.paths.state_db, clock=self.clock)
        try:
            yield store
        finally:
            store.close()

    async def _start_browser(self, application_id: str) -> ApplicationBrowser:
        with self._store() as store:
            allow_submission = not (self.prepare_only or store.is_preparation_only(application_id))
        return await self.browser_factory.start(BrowserOptions(
            artifacts_dir=self.paths.application_artifacts(application_id),
            artifacts_root=self.paths.artifacts_dir,
            profile_dir=self.paths.browser_dir,
            headless=self.headless,
            allow_submission=allow_submission,
        ))

    @staticmethod
    def _browser_start_failed(exc: BaseException) -> str:
        return (f"Could not start the browser ({_detail(exc)}). Nothing was submitted; check "
                "the browser installation (`playwright install chromium`).")

    def _lookup_email(self, candidate_id: str) -> str | None:
        try:
            return self.candidates.load(candidate_id).identity.email
        except (CandidateNotFound, CandidateProfileInvalid):
            return None

    def _blocked(self, store: ApplicationStore, app_id: str) -> ApplyOutcome:
        """The stored state forbids a run. An interrupted submit whose lease has lapsed
        is settled as SUBMISSION_UNKNOWN here (never retried) so the user is pointed
        at ``reconcile``; one whose lease is still live may belong to a running
        process and is left alone."""
        app = store.get_application(app_id)
        if app.state is S.SUBMITTING:
            store.recover_interrupted_submissions()
            app = store.get_application(app_id)
        if app.state is S.SUBMITTING:
            until = (app.claim_expires_at.strftime("%Y-%m-%d %H:%M:%SZ")
                     if app.claim_expires_at else "unknown")
            return _outcome(store, app_id,
                            f"A submit is in progress in another run ({app.claim_owner or 'unknown'}"
                            f", lease until {until}). It will not be repeated. If that run is "
                            "gone, wait for the lease to lapse, then reconcile.")
        return _outcome(store, app_id, _STATE_MESSAGES.get(app.state, "This application cannot be run."))

    def _record_reconciliation(self, store: ApplicationStore, claim: Claim,
                               observation: SubmissionObservation) -> ApplyOutcome:
        app_id = claim.application_id
        reconciliation = reconciliation_from(observation, method=ReconciliationMethod.SITE_CONFIRMATION)
        if reconciliation is not None:
            store.reconcile_submission(claim, reconciliation)
            return _outcome(store, app_id, "The site confirms this application. Receipt saved.")
        if observation.evidence:
            store.add_evidence(claim, observation.evidence)
        store.append_event(claim, "reconcile.unconfirmed", {
            "signals": observation.signals, "observed_url": observation.observed_url,
        })
        return _outcome(store, app_id, "The site does not confirm this application yet; it stays "
                                       "SUBMISSION_UNKNOWN and will not be resubmitted.")

    # --- the run --------------------------------------------------------------------------

    async def _run(self, store: ApplicationStore, app_id: str, url: str, *,
                   submission: bool = False) -> ApplyOutcome:
        try:
            with browser_profile_lock(self.paths.browser_dir):
                try:
                    claim = store.claim(app_id, self.owner, ttl=self._ttl)
                except ClaimUnavailable:
                    return _outcome(store, app_id, CLAIMED_MESSAGE)
                try:
                    app = store.get_application(app_id)
                    if app.state in SUBMISSION_BLOCKING_STATES or app.state in TERMINAL_STATES:
                        return _outcome(store, app_id, _STATE_MESSAGES.get(app.state, ""))
                    approved: _Approved | None = None
                    authorization = (None if self.prepare_only
                                     else store.submission_authorization(app_id))
                    if authorization is not None:
                        try:
                            approved = _approved_application(store, authorization)
                        except NotFound:
                            return _outcome(store, app_id, "The approved packet could not be "
                                            "read; nothing was opened or submitted. Prepare and "
                                            "approve the application again.")
                    if submission and approved is None:
                        return self._not_authorized(store, app_id)
                    if self.prepare_only or (approved is None and not self.submit_unapproved):
                        # Only an authorized approval is submitted: any other run records
                        # the no-submit restriction (again) and prepares.
                        store.require_preparation_only(claim)
                    try:
                        candidate = self.candidates.load(app.candidate_id)
                    except (CandidateNotFound, CandidateProfileInvalid) as exc:
                        # Stored failures are also shown by the HTTP service; loader
                        # errors can contain private absolute profile/resume paths.
                        detail = ("the profile could not be found" if isinstance(exc, CandidateNotFound)
                                  else "the profile or its resume could not be read or validated")
                        message = (f"Candidate profile unavailable: {detail}. "
                                   "Restore a valid profile.json and its resume, then resume.")
                        _fail_retryable(store, claim, app, message)
                        return _outcome(store, app_id, message)
                    pinned = store.pin_resume(app_id, candidate.resume)
                    if not pinned.verify():
                        return self._pinned_resume_missing(store, claim, app, pinned)
                    candidate = candidate.model_copy(update={"resume": pinned})
                    try:
                        browser = await self._start_browser(app_id)
                    except Exception as exc:
                        # A local problem (Chromium missing, profile locked by another
                        # browser): recorded, retryable, never a traceback.
                        message = self._browser_start_failed(exc) + " Then resume."
                        _fail_retryable(store, claim, app, message)
                        return _outcome(store, app_id, message)
                    run = _Run(self, store, claim, candidate, browser, url, approved=approved)
                    try:
                        return await run.execute()
                    except asyncio.CancelledError:
                        # Record what the cancelled run spent, while the claim is held.
                        await asyncio.shield(run.settle_provider_cost())
                        raise
                    finally:
                        await browser.close()
                finally:
                    with contextlib.suppress(Exception):
                        store.release(claim)
        except RunnerBusy as exc:
            return _outcome(store, app_id, str(exc))

    @staticmethod
    def _pinned_resume_missing(store: ApplicationStore, claim: Claim, app: Application,
                               pinned: ResumeArtifact) -> ApplyOutcome:
        """The application's own resume is gone or changed. Stop; never substitute the
        profile's current resume, which may belong to another application."""
        message = (f"The resume this application uses ({pinned.filename}, sha256 "
                   f"{pinned.sha256[:12]}...) is missing or changed. Restore that file and "
                   "resume; another resume is never substituted.")
        _fail_retryable(store, claim, app, message)
        return _outcome(store, app.id, message)


class _Run:
    """One pass through the site for one claimed application."""

    def __init__(self, runner: LocalApplicationRunner, store: ApplicationStore, claim: Claim,
                 candidate: CandidateProfile, browser: ApplicationBrowser, url: str,
                 approved: _Approved | None = None) -> None:
        self.runner = runner
        self.store = store
        self.claim = claim
        self.candidate = candidate
        self.browser = browser
        self.url = url
        self.approved = approved
        """Set for a submission run of an authorized approval: every step is filled from
        its approved packet and nothing is resolved."""
        self.step_packets: dict[int, tuple[ApplicationForm, ApplicationPacket]] = {}
        """The latest packet saved for each step in this run and the inspection it
        answers; ``preparation.ready`` records them."""
        self.awaited: set[tuple[int, str]] = set()
        """Custom controls ``(step, field id)`` this submission run already waited for."""
        self.filled_steps: set[int] = set()
        """Steps this submission run filled from their approved packets."""
        self.limits = runner.limits
        self.interaction = runner.interaction
        self.forms_seen: Counter[str] = Counter()
        self.acted_steps: set[int] = set()
        """Steps this run filled, advanced or submitted since their last inspection.
        Validation messages on such a step are new rejections (an epoch); messages on
        any other inspection (a fresh open, a page the user operated) are not."""
        self.choice_rounds: set[tuple[int, str, str]] = set()
        """Lookup questions ``(step, field id, fingerprint)`` that already had their one
        choice round in this run."""
        self.chosen: dict[tuple[int, str, str], tuple[str, str]] = {}
        """Suggestions chosen in this run: question -> (typed text, chosen label). A
        re-resolved packet types the stored value again; the chosen label replaces it."""
        self.retyped: dict[tuple[int, str, str], str] = {}
        """Lookups typed a second way in this run (the site offered nothing for the first):
        question -> the resolver's own typed text, which a chosen label is recorded under."""
        mark = getattr(runner.resolver, "provider_mark", None)
        self.provider_mark: int | None = mark() if callable(mark) else None
        """Where this run's unrecorded provider receipts start (None: the resolver uses no
        provider)."""
        self.provider_total: dict[str, Any] = {"calls": 0, "known_cost_usd": 0.0,
                                               "unknown_cost_calls": 0}
        """The provider usage this run recorded, for its outcome message."""
        self.consent_tried = False
        """Whether this run already accepted (or tried to accept) a data-processing consent
        page; a consent page that comes back is the person's (``_accept_consent``)."""

    @property
    def app_id(self) -> str:
        return self.claim.application_id

    def app(self) -> Application:
        return self.store.get_application(self.app_id)

    # state helpers ---------------------------------------------------------------------

    def _renew(self) -> None:
        self.claim = self.store.renew(self.claim, ttl=self.runner._ttl)

    async def _fenced(self, awaitable: Awaitable[T]) -> T:
        """Await a wait on the user or the browser while a heartbeat renews the claim
        every ``RunLimits.heartbeat_s``. If the claim is lost meanwhile (expired, or
        another run took it over) the wait is cancelled and ``ClaimLost`` is raised,
        so this run never acts on what the user did after losing the application."""
        waiting: asyncio.Future[T] = asyncio.ensure_future(awaitable)
        lost: ClaimLost | None = None

        async def heartbeat() -> None:
            nonlocal lost
            while True:
                await asyncio.sleep(self.limits.heartbeat_s)
                try:
                    self._renew()
                except ClaimLost as exc:
                    lost = exc
                    waiting.cancel()
                    return

        beat = asyncio.create_task(heartbeat())
        try:
            return await waiting
        except asyncio.CancelledError:
            if lost is not None:
                raise lost from None
            raise
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
            if not waiting.done():
                waiting.cancel()
                with contextlib.suppress(BaseException):
                    await waiting

    def _to(self, state: ApplicationState, **kwargs: Any) -> None:
        if self.app().state is not state:
            self.store.transition(self.claim, state, **kwargs)

    def _save_packet(self, form: ApplicationForm, packet: ApplicationPacket) -> None:
        self.store.save_packet(self.claim, packet)
        self.step_packets[form.step] = (form, packet)

    def _provider_cost(self) -> str:
        """Record this run's provider usage not recorded yet (``provider.budget``) while the
        claim is still held, and return the run's recorded total for the outcome message
        ("" without provider calls). A run usually records one event, when it stops or before
        a submit. Two moments that can end it without holding the claim record what came
        before them in their own event: a first job-identity binding (it may be a duplicate)
        and a submit (the site may show the form again, and the run continues)."""
        usage_of = getattr(self.runner.resolver, "provider_usage", None)
        mark_of = getattr(self.runner.resolver, "provider_mark", None)
        if self.provider_mark is not None and callable(usage_of) and callable(mark_of):
            usage = usage_of(self.provider_mark)
            if usage.get("calls"):
                try:
                    self.store.append_event(self.claim, PROVIDER_EVENT, usage)
                except Exception:  # a lost claim is reported by what follows
                    pass
                else:
                    self.provider_mark = mark_of()
                    for key in self.provider_total:
                        self.provider_total[key] += usage[key]
        total = self.provider_total
        if not total["calls"]:
            return ""
        unknown = total["unknown_cost_calls"]
        return (f" Provider cost: USD {total['known_cost_usd']:.4f} for {total['calls']} call(s)"
                + (f", {unknown} without a reported cost" if unknown else "") + ".")

    async def settle_provider_cost(self) -> None:
        """A cancelled run still records its provider usage: wait (at most
        ``LATE_COST_WAIT_S``) for resolver work the cancellation left running, so calls it
        finishes are receipted, then record what was not recorded yet."""
        drain = getattr(self.runner.resolver, "drain", None)
        if callable(drain):
            with contextlib.suppress(Exception):
                await drain(timeout=LATE_COST_WAIT_S)
        with contextlib.suppress(Exception):
            self._provider_cost()

    def _stop(self, state: ApplicationState, message: str, *, reason: str | None = None,
              missing: Sequence[MissingInput] = (),
              metadata: dict[str, Any] | None = None) -> _Stop:
        cost = self._provider_cost()
        current = self.app().state
        if current is S.NEEDS_INPUT and state is not S.NEEDS_INPUT:
            self._to(S.INSPECTING)  # NEEDS_INPUT only leaves through a fresh inspection
            current = S.INSPECTING
        if state is S.NEEDS_INPUT:
            self._to(S.INSPECTING)
            stop: dict[str, Any] = {
                "missing_inputs": [m.model_dump(mode="json") for m in missing],
                "reason": reason or message,
            }
            if missing and self.step_packets:
                # The next run of this attempt may carry on in the site's draft: the pages
                # filled here are pinned by a later preparation (``_prepared_steps``).
                stop["steps"] = [_step_record(form, packet)
                                 for _, (form, packet) in sorted(self.step_packets.items())]
            self.store.transition(self.claim, S.NEEDS_INPUT, metadata=stop)
        elif state in (S.FAILED_RETRYABLE, S.FAILED_PERMANENT):
            if current is not state:
                self.store.transition(self.claim, state, failure_reason=message,
                                      metadata=metadata)
        elif state is S.DUPLICATE:
            self.store.transition(self.claim, S.DUPLICATE, reason=message)
        return _Stop(_outcome(self.store, self.app_id, message + cost, missing))

    # main loop -----------------------------------------------------------------------------

    async def execute(self) -> ApplyOutcome:
        try:
            await self.interaction.progress("Opening the application page")
            page = await self.browser.open(self.url)
            self._to(S.INSPECTING)
            for _ in range(self.limits.max_steps):
                self._renew()
                page = await self._step(page)
            raise self._stop(S.FAILED_RETRYABLE,
                             f"Stopped after {self.limits.max_steps} pages without reaching a "
                             "submission; nothing was submitted.")
        except _Stop as stop:
            return stop.outcome
        except ClaimLost:
            return _outcome(self.store, self.app_id,
                            "This run no longer holds the application (its claim lapsed or another "
                            "run took over), so it stopped without recording anything more. "
                            "Resume to continue.")
        except Exception as exc:
            # A browser or page failure before any submit: stop, keep it retryable.
            # (Failures during a submit were already recorded as SUBMISSION_UNKNOWN.)
            detail = _detail(exc)
            if self.app().state in PRE_SUBMISSION_STATES:
                return self._stop(S.FAILED_RETRYABLE, f"Stopped by a browser error ({detail}). "
                                  "Nothing was submitted; resume to retry.").outcome
            return _outcome(self.store, self.app_id,
                            f"Stopped by an error: {detail}" + self._provider_cost())

    async def _step(self, page: PageInspection) -> PageInspection:
        kind = page.kind
        if consent_gate(page) and not self.consent_tried and self.approved is None:
            # A submission run of an approval resolves nothing: the page is the person's.
            accepted = await self._accept_consent()
            if accepted is not None:
                return accepted
        if kind in (PageKind.SIGN_IN_REQUIRED, PageKind.CAPTCHA):
            return await self._user_action(page, user_action_needs(page))
        if kind is PageKind.ALREADY_APPLIED:
            raise self._stop(S.DUPLICATE, "The site says you have already applied to this job.")
        if kind is PageKind.JOB_CLOSED:
            raise self._stop(S.FAILED_PERMANENT, "The job is no longer accepting applications.")
        if kind is not PageKind.APPLICATION_FORM or page.form is None:
            detail = page.message or f"the page is {kind.value}, not an application form"
            raise self._stop(S.FAILED_RETRYABLE, f"Could not reach the application form: {detail}.")
        self._bind_identity(page)
        return await self._form_step(page, page.form)

    def _bind_identity(self, page: PageInspection) -> None:
        expected = self.store.expected_job_identity(self.app_id)
        if expected is not None:
            if page.job_identity is None:
                raise self._stop(S.FAILED_RETRYABLE, "Could not verify that the application page "
                                 "belongs to the selected job. Open its specific application page "
                                 "and resume; nothing was submitted.")
            if page.job_identity.identity_key != expected:
                raise self._stop(S.FAILED_RETRYABLE, "The application page identifies a different "
                                 "job from the one selected. Check the selected job's application "
                                 "link and resume; nothing was submitted.")
        if page.job_identity is None:
            return
        if self.store.get_job(self.app().job_id).identity_key != page.job_identity.identity_key:
            # A first binding can find the job already applied to (DUPLICATE releases the
            # claim), so the provider usage so far is recorded while the claim is held.
            self._provider_cost()
        try:
            result = self.store.bind_job_identity(self.claim, page.job_identity)
        except IdentityConflict as exc:
            raise self._stop(S.FAILED_RETRYABLE, f"Job identity conflict: {exc}") from exc
        if result.duplicate_of is not None:
            raise _Stop(_outcome(self.store, self.app_id,
                                 f"This job already has application {result.duplicate_of}; "
                                 "not applying twice." + self._provider_cost()))

    async def _verify_expected_page(self) -> None:
        """Recheck a pinned selection after waits/fills, immediately before acting.
        An earlier step's identity cannot authorize a different page after navigation."""
        if self.store.expected_job_identity(self.app_id) is None:
            return
        page = await self._fenced(self.browser.inspect())
        self._renew()
        if page.kind is not PageKind.APPLICATION_FORM or page.form is None:
            raise self._stop(S.FAILED_RETRYABLE, "The selected job's application form is no longer "
                             "visible. Open its application page and resume; nothing was submitted.")
        self._bind_identity(page)

    async def _user_action(self, page: PageInspection, needs: list[MissingInput]) -> PageInspection:
        message = "; ".join(n.prompt for n in needs) or (page.message or "Action needed in the browser")
        if not await self._fenced(self.interaction.request_action(message)):
            raise self._stop(S.NEEDS_INPUT, message, reason="user action required", missing=needs)
        self._renew()
        after = await self._fenced(
            self.browser.wait_for_user(message, self.limits.user_action_timeout_s))
        self._renew()
        if after.kind in (PageKind.SIGN_IN_REQUIRED, PageKind.CAPTCHA):
            raise self._stop(S.NEEDS_INPUT, "Still waiting for you in the browser: " + message,
                             reason="user action not completed", missing=user_action_needs(after))
        if after.kind is PageKind.APPLICATION_FORM:
            return after
        # e.g. a sign-in that returns to the posting: go back to the application URL.
        return await self.browser.open(self.url)

    async def _accept_consent(self) -> PageInspection | None:
        """Round 14: a data-processing consent page in front of the form (Jobvite's "Data
        Consent") is accepted when the person's own statement covers it. Its question ("I
        accept the <policy>", the browser's ``data_consent``) is resolved like any consent
        on a form: an exact saved answer or input, else the routing resolver's
        statement-coverage decision over the person's saved statements. Only a checked
        answer from the person's own answers lets the browser choose the policy and click
        "I Accept" (``accept_data_consent``), once per preparation run (a submission run
        of an approval resolves nothing). Returns the page it leads to, or None: the page
        is then the person's to accept, as before."""
        offer = getattr(self.browser, "data_consent", None)
        accept = getattr(self.browser, "accept_data_consent", None)
        if not callable(offer) or not callable(accept):
            return None
        self.consent_tried = True
        residence = self.candidate.identity.address.country
        question: ApplicationForm | None = await self._fenced(offer(residence))
        self._renew()
        if question is None or len(question.fields) != 1:
            return None
        (field,) = question.fields
        packet = await self._resolve(question)
        answer = packet.answer_for(field.id)
        if (answer is None or answer.value != BooleanValue(checked=True)
                or answer.provenance.source not in (AnswerSource.SAVED_ANSWER, AnswerSource.USER_INPUT)):
            return None
        await self.interaction.progress("Accepting the data-processing consent that your saved "
                                        "statement covers")
        after: PageInspection | None = await self._fenced(accept(question, residence))
        self._renew()
        if after is None:
            return None
        self.store.append_event(self.claim, CONSENT_EVENT, {
            "question": field.question_text, "form_url": question.url,
            "source": answer.provenance.source.value,
            "reference_ids": list(answer.provenance.reference_ids),
            "next_page": after.kind.value})
        return after

    def _context(self, form: ApplicationForm) -> PacketContext:
        app = self.app()
        return PacketContext(application=app, job=self.store.get_job(app.job_id), form=form,
                             candidate=self.candidate,
                             user_inputs=self.store.get_user_inputs(self.app_id, form))

    async def _resolve(self, form: ApplicationForm) -> ApplicationPacket:
        context = self._context(form)
        packet = await self.runner.resolver.resolve(context)
        self._record_routing(form)
        problems = context.problems(packet)
        if problems:
            raise self._stop(S.FAILED_RETRYABLE, "Internal error: the answers for this step are "
                             "inconsistent (" + "; ".join(problems[:3]) + ").")
        return self._with_choices(form, self._with_rejections(form, packet))

    def _record_routing(self, form: ApplicationForm) -> None:
        """Persist a diagnostic projection of the AI resolver's route decisions and of the
        traces added since the previous step (``ROUTING_EVENT``). The event log is
        append-only, so only ids, stages, statuses, scores and page wording go in: never
        fact values, generated prose, review text or anything read from the candidate
        (``project_trace``). A resolver without a router or traces records nothing."""
        resolver = self.runner.resolver
        router = getattr(resolver, "router", None)
        report_for = getattr(router, "report_for", None)
        report = report_for(form) if callable(report_for) else None
        traces = list(getattr(resolver, "narrative_traces", None) or ())
        new_traces = _traces_since(traces)
        if report is None and not new_traces:
            return
        metadata: dict[str, Any] = {"form_step": form.step}
        if report is not None:
            metadata["prompt_version"] = report.prompt_version
            metadata["fields"] = [_decision_projection(decision) for decision in report.fields]
        if new_traces:
            metadata["traces"] = [project_trace(trace) for trace in new_traces]
        self.store.append_event(self.claim, ROUTING_EVENT, metadata)

    def _with_choices(self, form: ApplicationForm, packet: ApplicationPacket) -> ApplicationPacket:
        """Re-apply the suggestions chosen earlier in this run: the resolver types the
        stored value again, and the label chosen for exactly that value commits it."""
        answers: list[PacketAnswer] = []
        for answer in packet.answers:
            field = form.find(answer.field_id)
            choice = (self.chosen.get((form.step, field.id, field.fingerprint))
                      if field is not None else None)
            if (choice is not None and answer.value == TextValue(text=choice[0])
                    and answer.provenance.source is not AnswerSource.USER_INPUT):
                answer = _chosen_answer(answer, choice[1])
            answers.append(answer)
        if answers == packet.answers:
            return packet
        return packet.model_copy(update={"answers": answers})

    def _note_rejections(self, form: ApplicationForm) -> None:
        """Persist the field validation messages of a step this run acted on as one
        ``validation.rejected`` event, the rejection epoch of those questions. A step
        counts as acted on once, until the next fill/advance/submit. Sites quote the
        value that was typed ("'…' is not a valid e-mail"), and the event log is
        append-only, so each message is stored redacted (``redact_detail``): the epoch
        needs the question and the message's shape, not the value."""
        if form.step not in self.acted_steps:
            return
        self.acted_steps.discard(form.step)
        rejected = [f for f in form.fields if f.validation_error]
        if not rejected:
            return
        self.store.append_event(self.claim, REJECTION_EVENT, {
            "form_url": form.url,
            "form_step": form.step,
            "fields": [{"field_id": f.id, "field_fingerprint": f.fingerprint,
                        "message": redact_detail(f.validation_error)} for f in rejected],
        })

    def _with_rejections(self, form: ApplicationForm, packet: ApplicationPacket) -> ApplicationPacket:
        """Turn answers the site rejected into questions for the user.

        A persisted rejection applies to the same question even if a fresh page no
        longer displays its message. Only a scoped user input stored after its epoch
        is a correction; it is used even if the page still shows the old message. If
        the site rejects the correction too, the newer epoch asks again."""
        flagged = [f for f in form.fields if packet.answer_for(f.id) is not None]
        if not flagged:
            return packet
        rejections, received = _rejection_history(self.store, self.app_id)
        rejected: list[Any] = []
        for f in flagged:
            rejection = rejections.get((form.step, f.id, f.fingerprint))
            if rejection is None:
                continue  # never rejected after one of our actions: a stale message
            epoch, message = rejection
            answer = packet.answer_for(f.id)
            assert answer is not None
            if answer.provenance.source is AnswerSource.USER_INPUT and any(
                received.get(ref, -1) > epoch for ref in answer.provenance.reference_ids
            ):
                continue  # corrected after the rejection
            rejected.append(f.model_copy(update={"validation_error": message}))
        if not rejected:
            return packet
        ids = {f.id for f in rejected}
        missing = [
            MissingInput.for_field(form, f, reason=MissingReason.NO_ANSWER,
                                   prompt=f"The site rejected the answer for this question: "
                                          f"{f.validation_error}. Please provide a corrected answer.")
            for f in rejected
        ]
        return ApplicationPacket.model_validate({
            **packet.model_dump(),
            "answers": [a.model_dump() for a in packet.answers if a.field_id not in ids],
            "missing_inputs": [*(m.model_dump() for m in packet.missing_inputs),
                               *(m.model_dump() for m in missing)],
        })

    async def _form_step(self, page: PageInspection, form: ApplicationForm) -> PageInspection:
        self.forms_seen[form.fingerprint] += 1
        if self.forms_seen[form.fingerprint] > self.limits.max_same_form:
            raise self._stop(S.FAILED_RETRYABLE, "The same form step kept coming back; stopped to "
                             "avoid a loop. Nothing was submitted.")
        self._to(S.INSPECTING)
        acted = form.step in self.acted_steps
        self._note_rejections(form)
        if self.approved is not None:
            return await self._approved_step(page, form, acted=acted)
        packet = await self._resolve(form)
        self._save_packet(form, packet)
        rounds = 0
        while True:
            while not packet.is_complete:
                required = [m for m in packet.missing_inputs if m.required]
                unsupported = [m for m in required if m.reason is MissingReason.UNSUPPORTED_CONTROL]
                questions = [m for m in required if m.reason is not MissingReason.UNSUPPORTED_CONTROL]
                rounds += 1
                if rounds > self.limits.max_input_rounds:
                    raise self._stop(S.NEEDS_INPUT, f"{len(required)} required question(s) are "
                                     f"still open after {self.limits.max_input_rounds} attempts; "
                                     "answer them and resume.", reason="input rounds exhausted",
                                     missing=packet.missing_inputs)
                if questions:
                    inputs = list(await self._fenced(self.interaction.request_inputs(questions)))
                    if not self._accept_inputs(questions, inputs):
                        raise self._stop(S.NEEDS_INPUT, f"{len(questions)} required question(s) "
                                         "need your answer.", reason="missing answers",
                                         missing=packet.missing_inputs)
                else:
                    page = await self._user_action(page, unsupported)
                    if page.form is None:
                        return page
                    form = page.form
                packet = await self._resolve(form)
                self._save_packet(form, packet)
                if not questions and self._still_unoperated(unsupported, packet):
                    raise self._stop(S.NEEDS_INPUT, "Still waiting for you in the browser: "
                                     + "; ".join(m.prompt for m in unsupported),
                                     reason="user action not completed",
                                     missing=packet.missing_inputs)
            outcome = await self._act(form, packet)
            if isinstance(outcome, PageInspection):
                return outcome
            packet = outcome  # a lookup the site could not commit now needs the user's pick

    @staticmethod
    def _still_unoperated(waited_for: list[MissingInput], packet: ApplicationPacket) -> bool:
        """True when every custom control the user was waited for is still required and
        unoperated: the wait ended (timeout, or the user did something else) without
        progress, so the run must stop instead of waiting again."""
        still = {(m.field_id, m.field_fingerprint) for m in packet.missing_inputs
                 if m.required and m.reason is MissingReason.UNSUPPORTED_CONTROL}
        waited = {(m.field_id, m.field_fingerprint) for m in waited_for}
        return bool(waited) and waited <= still

    def _accept_inputs(self, questions: list[MissingInput], inputs: list[UserInput]) -> bool:
        wanted = {(m.field_id, m.field_fingerprint) for m in questions}
        usable = [u for u in inputs if (u.field_id, u.field_fingerprint) in wanted]
        if not usable:
            return False
        self._renew()
        self.store.save_user_inputs(self.claim, usable)
        job = self.store.get_job(self.app().job_id)
        for user_input in usable:
            saved = user_input.to_saved_answer(job=job)
            if saved is not None:
                self.runner.candidates.save_answer(self.app().candidate_id, saved)
        return True

    async def _act(self, form: ApplicationForm,
                   packet: ApplicationPacket) -> PageInspection | ApplicationPacket:
        """Fill the step, then advance or submit. Returns the page to continue with, or
        the packet again (saved, still incomplete) when a lookup the site could not
        commit now needs the user's pick among its suggestions."""
        await self._verify_expected_page()
        self._to(S.PACKET_READY)
        self._to(S.FILLING)
        self.acted_steps.add(form.step)
        try:
            fill = await self.browser.fill(form, packet)
        except ValueError:
            # The page changed under us; inspect it again rather than guess.
            self._to(S.INSPECTING)
            return await self.browser.inspect()
        if fill.needs_choice() and not fill.failed_field_ids() and not fill.page_errors:
            settled = await self._settle_choices(form, packet, fill)
            if settled is None:  # the page changed before the chosen label was typed
                self._to(S.INSPECTING)
                return await self.browser.inspect()
            fill, packet = settled
        failed = fill.failed_field_ids()
        if failed:
            labels = {fld.id: fld.label for fld in form.fields}
            raise self._stop(S.FAILED_RETRYABLE, "Could not fill " + ", ".join(failed)
                             + " reliably; nothing was submitted.",
                             metadata={"failed_fields": [
                                 {"field_id": result.field_id, "label": labels.get(result.field_id),
                                  "status": result.status.value,
                                  "detail": redact_detail(result.detail)}
                                 for result in fill.fields if result.field_id in failed]})
        if fill.page_errors:
            self._to(S.INSPECTING)
            return await self.browser.inspect()
        if not packet.is_complete:
            self._to(S.INSPECTING)
            return packet
        if form.is_final_step is True:
            return await self._submit(packet)
        await self._verify_expected_page()
        try:
            nav = await self.browser.advance()
        except (SubmissionRefused, AmbiguousAction) as exc:
            raise self._stop(S.FAILED_RETRYABLE, f"Cannot tell how to continue safely: {exc}. "
                             "Nothing was submitted.") from exc
        self._to(S.INSPECTING)
        return nav.inspection

    async def _settle_choices(
        self, form: ApplicationForm, packet: ApplicationPacket, fill: FillResult
    ) -> tuple[FillResult, ApplicationPacket] | None:
        """Lookups the site could not commit (``NEEDS_CHOICE``): one choice round per
        question per run. A label the resolver (``SuggestionChooser``) picks among the
        observed suggestions replaces the typed value, is recorded, and only those
        fields are filled again. Otherwise the user is asked to pick a suggestion (an
        optional lookup is left blank). Returns the merged fill and the saved packet,
        or None when the browser refused to fill again because the page changed."""
        retyped = await self._retype_lookups(form, packet, fill)
        if retyped is None:
            return None
        fill, packet = retyped
        resolver = self.runner.resolver
        chooser = resolver if isinstance(resolver, SuggestionChooser) else None
        chosen: dict[str, PacketAnswer] = {}
        picked: dict[str, str] = {}
        open_: dict[str, tuple[str, list[str]]] = {}
        events: list[dict[str, Any]] = []
        lookups: list[tuple[ApplicationField, PacketAnswer, str, list[str],
                            tuple[int, str, str], int | None]] = []
        asks: list[tuple[ApplicationField, str, list[str]]] = []
        for result in fill.needs_choice():
            field = form.find(result.field_id)
            answer = packet.answer_for(result.field_id)
            if field is None or answer is None:
                continue
            typed = answer.value.text if isinstance(answer.value, TextValue) else ""
            key = (form.step, field.id, field.fingerprint)
            ask = None
            if (chooser is not None and key not in self.choice_rounds and typed.strip()
                    and answer.provenance.source is not AnswerSource.USER_INPUT):
                self.choice_rounds.add(key)
                ask = len(asks)
                asks.append((field, typed, list(result.suggestions)))
            lookups.append((field, answer, typed, list(result.suggestions), key, ask))
        labels = await self._choose(chooser, form, asks) if chooser is not None and asks else []
        for field, answer, typed, suggestions, key, ask in lookups:
            label = labels[ask] if ask is not None else None
            if label is None:
                self.chosen.pop(key, None)  # a label chosen earlier did not commit either
                open_[field.id] = (typed, suggestions)
                continue
            chosen[field.id], picked[field.id] = _chosen_answer(answer, label), label
            self.chosen[key] = (self.retyped.get(key, typed), label)
            decision = getattr(chooser, "suggestion_decision", None)
            events.append({
                "form_url": form.url, "form_step": form.step, "field_id": field.id,
                "field_fingerprint": field.fingerprint, "chosen_label": label,
                "suggestion_count": len(suggestions),
                "source": answer.provenance.source.value, "chooser": type(chooser).__name__,
                "decision": decision(field.id) if callable(decision) else None,
            })
        self._renew()
        packet = self._with_lookups(form, packet, chosen, open_)
        self._save_packet(form, packet)
        for metadata in events:
            self.store.append_event(self.claim, SUGGESTION_EVENT, metadata)
        if not chosen or not packet.is_complete:
            # Nothing to type again, or the user must pick first: the whole step is
            # filled again once the questions are answered.
            return fill, packet
        try:
            refill = await self._refill(form, packet, list(chosen))
        except ValueError:
            return None
        fill = _merged(fill, refill)
        again = {r.field_id: r for r in refill.needs_choice() if r.field_id in chosen}
        if again:
            # The exact label did not commit either; no second round in this run.
            for field_id in again:
                field = form.field(field_id)
                self.chosen.pop((form.step, field.id, field.fingerprint), None)
            retry = {fid: (picked[fid], list(r.suggestions)) for fid, r in again.items()}
            packet = self._with_lookups(form, packet, {}, retry)
            self._save_packet(form, packet)
        return fill, packet

    async def _retype_lookups(
        self, form: ApplicationForm, packet: ApplicationPacket, fill: FillResult
    ) -> tuple[FillResult, ApplicationPacket] | None:
        """A lookup typed from the verified identity for which the site offered no
        suggestion at all is typed once more the other way the identity allows ("City,
        Region" after the bare city); the site's suggestions for that then go through the
        normal choice round. Once per question per run. None when the browser refused to
        fill again because the page changed."""
        retype: dict[str, PacketAnswer] = {}
        for result in fill.needs_choice():
            field = form.find(result.field_id)
            answer = packet.answer_for(result.field_id)
            if (result.suggestions or field is None or answer is None
                    or answer.provenance.source is not AnswerSource.PROFILE_IDENTITY
                    or not isinstance(answer.value, TextValue)):
                continue
            key = (form.step, field.id, field.fingerprint)
            others = [text for text in lookup_alternatives(self.candidate.identity, field.semantic_type)
                      if text != answer.value.text]
            if key in self.retyped or not others:
                continue
            self.retyped[key] = answer.value.text
            note = "; ".join(p for p in (answer.provenance.note, "typed a second way: the site "
                                         "offered no suggestion for the first") if p)
            retype[field.id] = answer.model_copy(update={
                "value": TextValue(text=others[0]),
                "provenance": answer.provenance.model_copy(update={"note": note})})
        if not retype:
            return fill, packet
        packet = packet.model_copy(update={"answers": [retype.get(a.field_id, a) for a in packet.answers]})
        try:
            refill = await self._refill(form, packet, list(retype))
        except ValueError:
            return None
        return _merged(fill, refill), packet

    async def _choose(self, chooser: SuggestionChooser, form: ApplicationForm,
                      asks: list[tuple[ApplicationField, str, list[str]]]) -> list[str | None]:
        """The chooser's picks for this step's lookups, each accepted only if it is one of
        that lookup's observed suggestions verbatim and fits the field. A chooser that can
        decide several at once (``choose_suggestions``, the dynamic resolver) decides them
        concurrently; a failing chooser means the user picks."""
        context = self._context(form)
        batch = getattr(chooser, "choose_suggestions", None)
        labels: list[str | None] = []
        # The chooser gets copies: labels are checked against the observed suggestions.
        if callable(batch):
            try:
                labels = list(await batch(context, [(field, typed, list(suggestions))
                                                    for field, typed, suggestions in asks]))
            except Exception:
                labels = []
            if len(labels) != len(asks):
                labels = [None] * len(asks)
        else:
            for field, typed, suggestions in asks:
                try:
                    labels.append(await chooser.choose_suggestion(context, field, typed,
                                                                  list(suggestions)))
                except Exception:
                    labels.append(None)
        return [label if (isinstance(label, str) and label in suggestions
                          and not answer_problems(field, TextValue(text=label))) else None
                for (field, _, suggestions), label in zip(asks, labels, strict=True)]

    def _with_lookups(self, form: ApplicationForm, packet: ApplicationPacket,
                      chosen: dict[str, PacketAnswer],
                      open_: dict[str, tuple[str, list[str]]]) -> ApplicationPacket:
        """A new packet with chosen labels in place of typed values, and each lookup still
        open replaced by a question listing the site's suggestions (required fields) or
        left blank (optional fields)."""
        questions = [_lookup_question(form, form.field(fid), typed, suggestions)
                     for fid, (typed, suggestions) in open_.items() if form.field(fid).required]
        return ApplicationPacket.model_validate({
            **packet.model_dump(),
            "id": new_id("pkt"),
            "answers": [chosen.get(a.field_id, a).model_dump() for a in packet.answers
                        if a.field_id not in open_],
            "missing_inputs": [*(m.model_dump() for m in packet.missing_inputs),
                               *(q.model_dump() for q in questions)],
        })

    async def _refill(self, form: ApplicationForm, packet: ApplicationPacket,
                      field_ids: list[str]) -> FillResult:
        if isinstance(self.browser, SelectiveFill):
            return await self.browser.fill_fields(form, packet, field_ids)
        return await self.browser.fill(form, packet)

    # approved submission -------------------------------------------------------------------

    async def _approved_step(self, page: PageInspection, form: ApplicationForm, *,
                             acted: bool) -> PageInspection:
        """Fill this step from its approved packet once the form is checked to ask
        exactly the approved questions. Nothing is resolved or generated. Every approved
        page before this one must have been filled by this run first: a kept draft the
        site opened at this page is walked back to them (``_walk_back``) or stops the run
        (``_kept_draft``), and a page the site skipped on the way here is a changed
        form."""
        approved = self.approved
        assert approved is not None
        if acted and (form.page_errors or any(f.validation_error for f in form.fields)):
            shown = [*(redact_detail(e) or "" for e in form.page_errors),
                     *(f"{_short(f.question_text) or f.id}: {redact_detail(f.validation_error)}"
                       for f in form.fields if f.validation_error)]
            raise self._not_approved(["the site did not accept the approved answers ("
                                      + "; ".join(_short(m, 160) for m in shown[:3]) + ")"])
        earlier = self._unfilled_before(form.step)
        if earlier:
            if self.filled_steps:  # the site went past an approved page after this run's first
                raise self._not_approved(self._unseen(earlier))
            back = await self._walk_back(form, earlier[0])
            if back is None:
                raise self._kept_draft(form, earlier)
            return back
        step = approved.steps.get(form.step)
        awaiting = _awaiting_user(step, form) if step is not None else []
        problems = approval_mismatches(step, form, final_step=approved.final_step,
                                       awaiting=awaiting)
        if problems:
            raise self._not_approved(problems)
        assert step is not None
        if awaiting:
            return await self._approved_user_action(page, form, awaiting)
        packet = bound_packet(step.packet, form)
        rejected = [redact_detail(p) or p for answer in packet.answers
                    if (field := form.find(answer.field_id))
                    for p in answer_problems(field, answer.value)]
        if rejected:  # e.g. the approved option is now disabled
            raise self._not_approved(rejected)
        problems = packet.problems_against(form)
        if problems:
            # Same questions, read differently this time (a semantic type whose answer
            # must come from a saved answer or the user): not a change of the form.
            raise self._stop(S.FAILED_RETRYABLE, "The approved answers cannot be filled as the "
                             "page is read this time ("
                             + "; ".join(redact_detail(p) or p for p in problems[:3]) + "). Nothing "
                             "was submitted and the approval stands; submit again with the "
                             "runtime options the application was prepared with.")
        return await self._act_approved(form, packet)

    async def _approved_user_action(self, page: PageInspection, form: ApplicationForm,
                                    controls: list[ApplicationField]) -> PageInspection:
        """Custom controls the user set in the browser while preparing are not in the
        approved packet: the user sets them again in the visible window, otherwise the
        run stops as NEEDS_INPUT and the approval stands."""
        needs = [MissingInput.for_field(
            form, f, reason=MissingReason.UNSUPPORTED_CONTROL,
            prompt=f"Set {_short(f.question_text) or f.id!r} in the browser window as you did "
                   "when the application was prepared; it is not filled automatically.")
            for f in controls]
        keys = {(form.step, f.id) for f in controls}
        if keys <= self.awaited:
            raise self._stop(S.NEEDS_INPUT, "Still waiting for you in the browser: "
                             + "; ".join(n.prompt for n in needs),
                             reason="user action not completed", missing=needs)
        self.awaited |= keys
        return await self._user_action(page, needs)

    async def _act_approved(self, form: ApplicationForm,
                            packet: ApplicationPacket) -> PageInspection:
        """``_act`` for an approved packet: fill it, then advance or submit it. A value
        that does not read back or a lookup that no longer commits stops the run before
        any submit; a step that changed while filling is inspected and compared again."""
        await self._verify_expected_page()
        self._to(S.PACKET_READY)
        self._to(S.FILLING)
        self.acted_steps.add(form.step)
        try:
            fill = await self.browser.fill(form, packet)
        except ValueError:
            # The page changed under us: inspect it again and compare it once more.
            self._to(S.INSPECTING)
            return await self.browser.inspect()
        unmatched = [r for r in fill.fields if r.status in (FieldFillStatus.VERIFICATION_MISMATCH,
                                                            FieldFillStatus.NEEDS_CHOICE)]
        if unmatched:
            labels = {f.id: _short(f.question_text) or f.id for f in form.fields}
            raise self._not_approved([
                (f"the approved answer to {labels.get(r.field_id, r.field_id)!r} does not read back"
                 if r.status is FieldFillStatus.VERIFICATION_MISMATCH else
                 f"the site no longer accepts the approved answer to "
                 f"{labels.get(r.field_id, r.field_id)!r}")
                + (f" ({_short(redact_detail(r.detail) or '', 120)})" if r.detail else "")
                for r in unmatched])
        if fill.page_errors:
            self._to(S.INSPECTING)
            return await self.browser.inspect()
        failed = fill.failed_field_ids()
        if failed:
            labels = {fld.id: fld.label for fld in form.fields}
            raise self._stop(S.FAILED_RETRYABLE, "Could not fill " + ", ".join(failed)
                             + " reliably; nothing was submitted. The approval stands; submit "
                               "again to retry.",
                             metadata={"failed_fields": [
                                 {"field_id": result.field_id, "label": labels.get(result.field_id),
                                  "status": result.status.value,
                                  "detail": redact_detail(result.detail)}
                                 for result in fill.fields if result.field_id in failed]})
        self.filled_steps.add(form.step)
        if form.is_final_step is True:
            approved = self.approved
            assert approved is not None
            unseen = self._unfilled_before(approved.final_step)
            if unseen:
                # Checked when each page is reached (``_approved_step``); kept as the last
                # guard before the submit: every approved page was filled by this run.
                raise self._not_approved(self._unseen(unseen))
            return await self._submit(packet)
        await self._verify_expected_page()
        try:
            nav = await self.browser.advance()
        except (SubmissionRefused, AmbiguousAction) as exc:
            raise self._stop(S.FAILED_RETRYABLE, f"Cannot tell how to continue safely: {exc}. "
                             "Nothing was submitted.") from exc
        self._to(S.INSPECTING)
        return nav.inspection

    def _unfilled_before(self, step: int) -> list[int]:
        """The approved steps before ``step`` that this submission run has not filled."""
        approved = self.approved
        assert approved is not None
        return [s for s in sorted(approved.steps) if s < step and s not in self.filled_steps]

    @staticmethod
    def _unseen(steps: Sequence[int]) -> list[str]:
        return [f"the site did not show step {step + 1}, so its approved answers could not "
                "be checked" for step in steps]

    async def _walk_back(self, form: ApplicationForm, target: int) -> PageInspection | None:
        """The page of step ``target`` (or an earlier one), reached from ``form``, the
        later page a kept draft opened at, with the site's own previous-step control one
        page at a time (``StepBack``). Nothing is filled on the way back; the run then
        compares and fills every page from there as if the site had opened at it. None
        when the browser cannot go back (no ``StepBack``, or no unambiguous Back control)
        or a step back does not show an earlier page of the form."""
        if not isinstance(self.browser, StepBack):
            return None
        await self.interaction.progress(
            f"The site opened a draft it kept at step {form.step + 1}; going back to step "
            f"{target + 1} to check and fill every approved page from there")
        step, page = form.step, None
        while step > target:
            await self._verify_expected_page()
            try:
                page = await self.browser.previous_step()
            except AmbiguousAction:
                return None
            self._renew()
            if (page.kind is not PageKind.APPLICATION_FORM or page.form is None
                    or page.form.step >= step):
                return None
            step = page.form.step
        return page

    def _kept_draft(self, form: ApplicationForm, earlier: Sequence[int]) -> _Stop:
        """Stop before filling anything: the site opened a draft it kept at ``form``'s
        page and the browser cannot go back to the approved pages before it, so this run
        can neither check nor fill them. Preparing again would reopen the same draft and
        pin the same pages, so the message names the manual remedy instead: the user
        submits that draft in the browser. The approval is withdrawn (reason
        ``KEPT_DRAFT_MESSAGE``), so nothing submits the application later on its own."""
        pages = _steps_text([step + 1 for step in earlier])
        self.store.invalidate_approval(self.claim, reason=KEPT_DRAFT_MESSAGE, details=[
            f"the site opened its kept draft at step {form.step + 1}; {pages} could not be "
            "checked or filled"])
        return self._stop(S.NEEDS_INPUT, f"{KEPT_DRAFT_MESSAGE} at step {form.step + 1}, so "
                          f"the approved answers of {pages} could not be checked or filled. "
                          "Nothing was submitted and the approval was withdrawn. Preparing it "
                          "again reopens the same draft: submit this application in the "
                          "browser yourself, after checking each page against the answers "
                          "you approved.", reason=KEPT_DRAFT_REASON)

    def _not_approved(self, problems: Sequence[str]) -> _Stop:
        """Stop before any submit: withdraw the approval (the no-submit restriction is
        back) and record NEEDS_INPUT naming what differs."""
        details = list(dict.fromkeys(p for p in problems if p))
        self.store.invalidate_approval(self.claim, reason=MISMATCH_MESSAGE, details=details)
        shown = "; ".join(details[:3]) + (f"; and {len(details) - 3} more" if len(details) > 3 else "")
        return self._stop(S.NEEDS_INPUT, f"{MISMATCH_MESSAGE}: {shown}. Nothing was submitted and "
                          "the approval was withdrawn; prepare it again, review it and approve it "
                          "before submitting.", reason="form no longer matches the approved application")

    def _prepared_steps(self, packet: ApplicationPacket,
                        final: ApplicationForm) -> list[dict[str, Any]]:
        """Every step of this attempt up to the final one, with the packet last saved for it
        and, where recorded, the questions that packet answered: what an approval of this
        preparation pins. Pages this run filled come from this run; pages an earlier run
        of the attempt filled before a question stop (the site keeps them in its draft)
        come from that stop's ``steps``, or from the packet alone when none was recorded."""
        latest: dict[int, str] = {}
        recorded: dict[tuple[int, str], dict[str, Any]] = {}
        for event in attempt_events(self.store.list_events(self.app_id)):
            meta = event.metadata
            if event.event == "packet.saved":
                if isinstance(meta.get("form_step"), int) and isinstance(meta.get("packet_id"), str):
                    latest[meta["form_step"]] = meta["packet_id"]
            elif event.to_state is S.NEEDS_INPUT:
                for item in meta.get("steps") or []:
                    if isinstance(item, dict) and isinstance(item.get("form_step"), int):
                        recorded[(item["form_step"], str(item.get("packet_id")))] = item
        records: dict[int, dict[str, Any]] = {}
        for step, packet_id in latest.items():
            if step >= packet.form_step:
                continue
            if step in self.step_packets and self.step_packets[step][1].id == packet_id:
                records[step] = _step_record(*self.step_packets[step])
            elif (step, packet_id) in recorded:
                records[step] = dict(recorded[(step, packet_id)])
            else:
                earlier = self.store.get_packet(packet_id)
                records[step] = {"form_step": step, "packet_id": packet_id,
                                 "form_url": earlier.form_url,
                                 "form_fingerprint": earlier.form_fingerprint}
        answered = self.step_packets.get(packet.form_step)
        records[packet.form_step] = _step_record(
            *(answered if answered is not None and answered[1].id == packet.id else (final, packet)))
        return [{**record, "final": step == packet.form_step}
                for step, record in sorted(records.items())]

    async def _submit(self, packet: ApplicationPacket) -> PageInspection:
        if self.store.is_preparation_only(self.app_id):
            # Re-read after filling. A changed final step must be resolved again;
            # never record a successful preparation from a stale observation.
            prepare_review = getattr(self.browser, "prepare_review", self.browser.inspect)
            page = await prepare_review()
            if (page.form is None or page.form.is_final_step is not True
                    or packet.problems_against(page.form)):
                self._to(S.INSPECTING)
                return page
            if page.form.page_errors or any(f.validation_error for f in page.form.fields):
                raise self._stop(S.NEEDS_INPUT,
                                 "Stopped before submission: the final form has validation errors.",
                                 reason="final form needs correction")
            await self._verify_expected_page()
            if page.evidence:
                self.store.add_evidence(self.claim, page.evidence)
            keep_for_review = getattr(self.browser, "keep_for_review", None)
            location = keep_for_review() if callable(keep_for_review) else None
            self.store.append_event(self.claim, PREPARED_EVENT, {
                "form_url": page.form.url,
                "form_step": page.form.step,
                "form_fingerprint": page.form.fingerprint,
                "packet_id": packet.id,
                "submitted": False,
                "browser_location": location,
                "captcha_pending": page.captcha_pending,
                "steps": self._prepared_steps(packet, page.form),
            })
            captcha_note = (" A CAPTCHA on this form must be solved in the browser before it can "
                            "be submitted." if page.captcha_pending else "")
            raise self._stop(S.NEEDS_INPUT,
                             "Prepared to the final review step. Nothing was submitted. "
                             "Submission remains disabled when this application is resumed."
                             + captcha_note
                             + (f" Review the open page in {location}." if location else ""),
                             reason="prepared for final review; submission disabled",
                             missing=[m for m in packet.missing_inputs if not m.required])
        if self.approved is None and not self.runner.submit_unapproved:
            # ``_run`` restricts every run without an authorized approval, so this is not
            # reached; it stays as the last guard before ``begin_submission``.
            raise self._stop(S.FAILED_RETRYABLE, f"{NOT_AUTHORIZED_MESSAGE}: only an "
                             "approved application whose submission was authorized is "
                             "submitted. Nothing was submitted.")
        has_expected_job = self.store.expected_job_identity(self.app_id) is not None
        if has_expected_job:
            await self.interaction.progress("Submitting the application")
            await self._verify_expected_page()
        cost = self._provider_cost()  # a terminal submission outcome releases the claim
        attempt = self.store.begin_submission(self.claim, packet_id=packet.id)
        try:
            if not has_expected_job:
                await self.interaction.progress("Submitting the application")
            await self.browser.submit()
            observation = await self.browser.confirm()
        except BaseException as exc:
            # Interrupted or failed after SUBMITTING was recorded: the site may have the
            # application. Record it as unknown, never as a failure that permits a retry.
            with contextlib.suppress(Exception):
                self.store.record_submission_outcome(self.claim, attempt.id, SubmissionObservation(
                    outcome=SubmissionOutcome.UNKNOWN,
                    signals=[f"interrupted during submission: {type(exc).__name__}"],
                    detail="the run stopped while submitting; the outcome is not known",
                ))
            raise
        app = self.store.record_submission_outcome(self.claim, attempt.id, observation)
        if app.state is S.SUBMITTED:
            raise _Stop(_outcome(self.store, self.app_id, "Submitted; the site confirmed it. "
                                                          "Receipt saved." + cost))
        if app.state is S.SUBMISSION_UNKNOWN:
            raise _Stop(_outcome(self.store, self.app_id,
                                 "The submit may have reached the employer, but no confirmation "
                                 "tied to this job was seen. It will not be retried; reconcile it."
                                 + cost))
        if app.state in (S.FAILED_RETRYABLE, S.FAILED_PERMANENT):
            raise _Stop(_outcome(self.store, self.app_id,
                                 f"Not submitted: {app.failure_reason or observation.detail}"
                                 + cost))
        # FILLING or NEEDS_INPUT: the site showed the form again (definitely not received).
        page = await self.browser.inspect()
        if (app.state is S.NEEDS_INPUT and page.kind is PageKind.APPLICATION_FORM
                and page.captcha_pending):
            # The browser refused to dispatch because the form's embedded CAPTCHA widget
            # is unsolved. That is the user's action, recorded (or waited for) exactly
            # like a CAPTCHA page; the packet has no question for it.
            needs = [MissingInput(
                field_id=None, label="Solve the CAPTCHA", reason=MissingReason.USER_ACTION,
                prompt="Solve the CAPTCHA on the application form in the browser window; the "
                       "form cannot be submitted until it is solved.")]
            page = await self._user_action(page, needs)
            if page.kind is not PageKind.APPLICATION_FORM or page.form is None:
                return page
            if page.captcha_pending:
                raise self._stop(S.NEEDS_INPUT, "Still waiting for you in the browser: "
                                 + needs[0].prompt, reason="user action not completed",
                                 missing=needs)
        return page


def create_runner(
    paths: LocalPaths,
    *,
    headless: bool,
    interaction: UserInteraction,
    limits: RunLimits | None = None,
    dynamic_options: Any = None,
) -> LocalApplicationRunner:
    """Production preparation-only runner with an explicitly selected runtime."""
    factory = None
    resolver = None
    if dynamic_options is not None:
        from .dynamic import runtime_components

        factory, resolver = runtime_components(dynamic_options)
    return LocalApplicationRunner(paths=paths, interaction=interaction, headless=headless,
                                  limits=limits, browser_factory=factory, resolver=resolver,
                                  prepare_only=True)


ALLOW_SUBMISSION_ENV = "IMX_ALLOW_SUBMISSION"
"""The CLI submits only while this variable is ``1`` (and ``--yes`` is given, and the
application is approved). ``apply``, ``resume`` and ``prepare-batch`` never read it."""


def create_submission_runner(
    paths: LocalPaths,
    *,
    headless: bool,
    interaction: UserInteraction,
    limits: RunLimits | None = None,
    dynamic_options: Any = None,
) -> LocalApplicationRunner:
    """The runner behind ``interviewmaxxing submit``: ``prepare_only=False``, so an
    application whose approval was authorized is submitted with exactly its approved
    packets (``LocalApplicationRunner.submit``). Any other run it makes (``apply`` or
    ``resume`` of an application without an authorized approval, restricted or never
    restricted) records the no-submit restriction and only prepares; it never sets
    ``submit_unapproved``. Only an explicit, gated caller may build it."""
    factory = None
    if dynamic_options is not None:
        from .dynamic import runtime_components

        factory, _ = runtime_components(dynamic_options)
    return LocalApplicationRunner(paths=paths, interaction=interaction, headless=headless,
                                  limits=limits, browser_factory=factory, prepare_only=False)


class NoninteractiveInteraction:
    """Asks nothing: missing questions are recorded as NEEDS_INPUT and browser actions
    are declined unless ``allow_browser_action`` is set (the user said they will act
    in the visible window)."""

    def __init__(self, *, allow_browser_action: bool = False) -> None:
        self.allow_browser_action = allow_browser_action
        self.messages: list[str] = []

    async def request_inputs(self, missing: Sequence[MissingInput]) -> Sequence[UserInput]:
        return []

    async def request_action(self, message: str) -> bool:
        return self.allow_browser_action

    async def progress(self, message: str) -> None:
        self.messages.append(message)


__all__ = [
    "ALLOW_SUBMISSION_ENV",
    "BUSY_MESSAGE",
    "CLAIMED_MESSAGE",
    "KEPT_DRAFT_MESSAGE",
    "KEPT_DRAFT_REASON",
    "MISMATCH_MESSAGE",
    "NEEDS_INPUT_EVENT",
    "NOT_AUTHORIZED_MESSAGE",
    "PREPARED_EVENT",
    "PROVIDER_EVENT",
    "REJECTION_EVENT",
    "ROUTING_EVENT",
    "SUGGESTION_EVENT",
    "LocalApplicationRunner",
    "NoninteractiveInteraction",
    "RunLimits",
    "RunnerBusy",
    "StepBack",
    "approval_mismatches",
    "bound_packet",
    "browser_profile_lock",
    "create_runner",
    "create_submission_runner",
    "field_record",
    "options_digest",
    "pending_inputs",
    "project_trace",
    "redact_detail",
    "rejection_epochs",
]
