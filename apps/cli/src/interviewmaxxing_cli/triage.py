"""Triage of held and failed applications: group what stopped them so that each
distinct question is answered once, and tell where each application stands now.

``batch-report`` groups the holds and fill failures of its ledger rows here,
``prepare-batch --retry`` classifies the applications it may run again, and
``interviewmaxxing holds`` (``build_holds``) groups the open holds of every
NEEDS_INPUT application in the store.

Everything here only reads. The application store is opened as ``status`` opens it,
and saved answers are read only to tell whether a question was answered after its
application stopped: no stored answer value ever reaches the output. Question
wording is cut to ``QUESTION_LIMIT`` characters and failure details are masked
(``failure_text``).
"""

from __future__ import annotations

import re
import shlex
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import Field, ValidationError

from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_core import (
    Application,
    ApplicationEvent,
    ApplicationState,
    ApplicationStore,
    CandidateNotFound,
    CandidateProfileInvalid,
    Contract,
    JobRecord,
    LocalPaths,
    MissingInput,
    MissingReason,
    SavedAnswer,
)
from interviewmaxxing_generation import wording_key

S = ApplicationState

PROG = "interviewmaxxing"
NEEDS_INPUT_EVENT = "application.needs_input"
"""The store's event for entering NEEDS_INPUT; its ``missing_inputs`` are the holds."""
FAILED_EVENT = "application.failed_retryable"
"""The store's event for entering FAILED_RETRYABLE: ``failure_reason`` and, from runners
that record it, ``failed_fields`` (``[{"field_id", "label", "status", "detail"}]``)."""
PREPARED_EVENT = "preparation.ready"
LEDGER_LABEL_LIMIT = 120
"""Question wording as a batch ledger keeps it (``batch.LABEL_LIMIT``)."""
QUESTION_LIMIT = 80
"""Question wording in reports (``batch.REPORT_LABEL_LIMIT``)."""
DETAIL_LIMIT = 120
"""Failure details in reports."""
VALUE_PLACEHOLDER = "VALUE"
"""Stands for the answer in the ``answer`` lines; the person replaces it."""
NO_FORM_PREFIX = "Could not reach the application form"
"""How the runner reports a run that never reached a fillable form."""

RUN_STATES: frozenset[ApplicationState] = frozenset(
    {S.REQUESTED, S.INSPECTING, S.PACKET_READY, S.FILLING}
)
"""States a run passes through; an application left in one has no recorded outcome."""
CURRENT_OUTCOMES: tuple[str, ...] = (
    "prepared", "needs_input", "failed_retryable", "unknown", "closed", "duplicate", "blocked",
)
"""What ``current_outcome`` can say about a stored application."""
ACT_REASONS = frozenset({MissingReason.USER_ACTION.value, MissingReason.UNSUPPORTED_CONTROL.value})
"""Holds the person clears in a visible browser (``resume APP --act``), not with ``answer``."""
ACT_CONTROLS = frozenset({"FILE", "UNSUPPORTED"})


def truncate(text: str, limit: int) -> str:
    """Whitespace collapsed, cut to ``limit`` characters with a trailing ellipsis."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def hold_key(label: str, limit: int | None = LEDGER_LABEL_LIMIT) -> str:
    """The grouping key of a question: its ``wording_key`` (case, spacing, sentence
    punctuation and required markers ignored), after the ledger's cut to ``limit``
    characters so that ledger labels and stored labels key alike. ``limit=None`` keys
    the complete wording."""
    return wording_key(truncate(label, limit) if limit is not None else label)


# --- hold categories ------------------------------------------------------------------------

HOLD_CATEGORIES: tuple[str, ...] = (
    "custom_control", "explicit_answer", "screener_yes_no", "lookup", "narrative", "other",
)
"""What held an application, by simple rules on its reason, control and wording:

``custom_control``   UNSUPPORTED_CONTROL.
``explicit_answer``  EXPLICIT_ANSWER_REQUIRED or UNCOVERED_ATTESTATION.
``lookup``           NO_ANSWER on a TYPEAHEAD (a lookup such as a location).
``screener_yes_no``  NO_ANSWER whose wording starts with "Do you", "Have you" or "Are you".
``narrative``        NO_ANSWER whose wording asks to describe, tell, explain or why.
``other``            everything else (AMBIGUOUS, USER_ACTION, an unknown reason, ...).
"""
_SCREENER = re.compile(r"(?:do|have|are) you\b")
_NARRATIVE = re.compile(r"\b(?:describe|tell|why|explain)\b")
_LEADING = re.compile(r"^[\W_]+")


def categorize_hold(label: str, reason: str | None, control_type: str | None = None) -> str:
    """The ``HOLD_CATEGORIES`` entry for one recorded question or action."""
    if reason == "UNSUPPORTED_CONTROL":
        return "custom_control"
    if reason in ("EXPLICIT_ANSWER_REQUIRED", "UNCOVERED_ATTESTATION"):
        return "explicit_answer"
    if reason != "NO_ANSWER":
        return "other"
    if (control_type or "").upper() == "TYPEAHEAD":
        return "lookup"
    text = _LEADING.sub("", " ".join(label.casefold().split()))
    if _SCREENER.match(text):
        return "screener_yes_no"
    if _NARRATIVE.search(text):
        return "narrative"
    return "other"


# --- where an application stands --------------------------------------------------------------


def prepared_stop(events: Sequence[ApplicationEvent]) -> bool:
    """True when the application's current stop is a completed preparation. Walking back
    from the latest transition: it must enter NEEDS_INPUT, and ``preparation.ready`` must
    come before any other transition than the one into INSPECTING, so a later stop
    (questions, sign-in, a failure) is never taken for a preparation. The same rule as
    the service's ``views.prepared_event``, which the CLI cannot import."""
    stopped = False
    for event in reversed(events):
        if not stopped:
            if event.to_state is None:
                continue
            if event.to_state is not S.NEEDS_INPUT:
                return False
            stopped = True
            continue
        if event.event == PREPARED_EVENT:
            return True
        if event.to_state is not None and event.to_state is not S.INSPECTING:
            return False
    return False


def current_outcome(app: Application, events: Sequence[ApplicationEvent]) -> str:
    """Where a stored application stands now, in batch outcome terms:

    ``prepared``          NEEDS_INPUT stopped at the final review step (``prepared_stop``).
    ``needs_input``       NEEDS_INPUT for questions, sign-in, CAPTCHA or a custom control.
    ``failed_retryable``  FAILED_RETRYABLE.
    ``unknown``           REQUESTED, INSPECTING, PACKET_READY or FILLING: a run started and
                          recorded no outcome (it timed out, crashed, or is still running).
    ``closed``            FAILED_PERMANENT.
    ``duplicate``         DUPLICATE.
    ``blocked``           a submission state (never the case for preparation-only work).
    """
    state = app.state
    if state is S.NEEDS_INPUT:
        return "prepared" if prepared_stop(events) else "needs_input"
    if state is S.FAILED_RETRYABLE:
        return "failed_retryable"
    if state is S.FAILED_PERMANENT:
        return "closed"
    if state is S.DUPLICATE:
        return "duplicate"
    if state in RUN_STATES:
        return "unknown"
    return "blocked"


@dataclass(frozen=True, slots=True)
class RecordedHolds:
    """The questions and actions a NEEDS_INPUT application stopped for."""

    stopped_at: datetime | None
    """When the application entered its current NEEDS_INPUT stop."""
    recorded: tuple[MissingInput, ...]
    """Everything that stop recorded (``pending_inputs``)."""
    open: tuple[MissingInput, ...]
    """Those not answered since the stop: no answer of the person's to this exact
    question on this application, and no saved answer for its wording that applies to
    the job, both newer than the stop."""

    @property
    def answered(self) -> int:
        return len(self.recorded) - len(self.open)


NO_HOLDS = RecordedHolds(stopped_at=None, recorded=(), open=())


def _saved_phrases(answer: SavedAnswer) -> frozenset[str]:
    phrases = {wording_key(answer.question), *(wording_key(p) for p in answer.match_phrases)}
    return frozenset(p for p in phrases if p)


def recorded_holds(store: ApplicationStore, app: Application,
                   events: Sequence[ApplicationEvent], saved_answers: Sequence[SavedAnswer],
                   job: JobRecord) -> RecordedHolds:
    """The holds of ``app``'s current NEEDS_INPUT stop, and which of them are still open.

    A hold counts as answered when, after the stop, the person answered exactly this
    question for this application (``interviewmaxxing answer``: same step, field and
    wording fingerprint), or a saved answer appeared whose question is this wording
    (compared as the resolver compares it, ``wording_key``), whose semantic type is
    unset or the hold's, and which applies to the job (global, or saved for this job).
    Answers that existed before the stop were already available to the run that
    stopped, so they do not count. Values are never read."""
    if app.state is not S.NEEDS_INPUT:
        return NO_HOLDS
    stop = next((e for e in reversed(events) if e.event == NEEDS_INPUT_EVENT), None)
    if stop is None:
        return NO_HOLDS
    recorded: list[MissingInput] = []
    raw = stop.metadata.get("missing_inputs")
    for value in raw if isinstance(raw, list) else []:
        try:
            recorded.append(MissingInput.model_validate(value))
        except ValidationError:
            continue
    answered = {(u.form_step, u.field_id, u.field_fingerprint)
                for u in store.list_user_inputs(app.id) if u.provided_at > stop.timestamp}
    fresh = [(a, _saved_phrases(a)) for a in saved_answers
             if a.confirmed_at > stop.timestamp and a.applies_to(job)]
    still: list[MissingInput] = []
    for item in recorded:
        if item.field_id is not None:
            if (item.form_step, item.field_id, item.field_fingerprint) in answered:
                continue
            key = wording_key(item.label)
            if key and any(key in phrases and (a.semantic_type is None
                                               or a.semantic_type is item.semantic_type)
                           for a, phrases in fresh):
                continue
        still.append(item)
    return RecordedHolds(stopped_at=stop.timestamp, recorded=tuple(recorded), open=tuple(still))


def candidate_saved_answers(paths: LocalPaths, candidate_id: str) -> list[SavedAnswer]:
    """The candidate's saved answers (profile and ``answers.json``), or none when the
    profile cannot be loaded; only their questions, scope and dates are used."""
    try:
        return list(LocalCandidateStore.from_paths(paths).load(candidate_id).saved_answers)
    except (CandidateNotFound, CandidateProfileInvalid, OSError, ValueError):
        return []


# --- grouping holds by question ---------------------------------------------------------------


def command_line(cli: Sequence[str], *args: str) -> str:
    """A shell-ready command: every part quoted where the shell needs it (field ids
    such as ``cards[a][field0]`` or a ``--home`` path with spaces)."""
    return " ".join(shlex.quote(part) for part in (*cli, *args))


def answer_line(cli: Sequence[str], application_id: str, field_id: str) -> str:
    """``interviewmaxxing answer APP --set FIELD=VALUE --reuse global``: answering this
    once saves a global answer that every application with the same wording reuses."""
    return command_line(cli, "answer", application_id, "--set",
                        f"{field_id}={VALUE_PLACEHOLDER}", "--reuse", "global")


def act_line(cli: Sequence[str], application_id: str) -> str:
    """``interviewmaxxing resume APP --act``: the person acts in a visible browser."""
    return command_line(cli, "resume", application_id, "--act")


@dataclass(frozen=True, slots=True)
class HoldOccurrence:
    """One recorded question or action of one application."""

    label: str
    reason: str | None
    control_type: str | None = None
    semantic_type: str | None = None
    field_id: str | None = None
    application_id: str | None = None
    backend: str = ""

    @property
    def needs_browser(self) -> bool:
        """A browser action, a custom control or a file: the person completes it in a
        visible window (``resume APP --act``)."""
        return self.reason in ACT_REASONS or (self.control_type or "").upper() in ACT_CONTROLS

    @property
    def answerable(self) -> bool:
        """``interviewmaxxing answer`` can answer it: a field question (its field id is
        known) that does not need the browser."""
        return bool(self.field_id) and not self.needs_browser


class HoldGroup(Contract):
    """Holds that share one question wording (``hold_key``), with a way to clear them."""

    question: str
    """The wording as first seen, cut to ``QUESTION_LIMIT`` characters."""
    holds: int = Field(ge=1)
    applications: int = Field(ge=0)
    """Distinct applications held by this question."""
    backends: list[str] = Field(default_factory=list)
    """Where it was asked, sorted; ``(none)`` when unknown."""
    reason: str | None = None
    """The most frequent ``MissingReason``."""
    reasons: dict[str, int] = Field(default_factory=dict)
    """Every recorded reason, counted, most frequent first."""
    semantic_type: str | None = None
    """The most frequent known ``SemanticType`` (None when none is known)."""
    control_type: str | None = None
    """The most frequent ``ControlType``."""
    category: str
    """``HOLD_CATEGORIES`` entry of the wording, reason and control."""
    sample_application_id: str | None = None
    """One application held by it; the lines below name it."""
    field_id: str | None = None
    """The sample's field id for this question."""
    answer: str | None = None
    """The exact ``interviewmaxxing answer SAMPLE --set FIELD=VALUE --reuse global`` line
    (replace ``VALUE``); None when ``answer`` cannot answer it, or when no field id was
    recorded for it (a ledger line older than field ids, read without the state
    database: ``interviewmaxxing status SAMPLE`` shows it)."""
    act: str | None = None
    """``interviewmaxxing resume SAMPLE --act`` for a browser action, a custom control
    or a file, which the person completes in a visible window."""
    application_ids: list[str] = Field(default_factory=list)


def _most_common(values: Iterable[str | None]) -> str | None:
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else None


def _backends(values: Iterable[str]) -> list[str]:
    return sorted({v or "(none)" for v in values})


def _hold_group(items: Sequence[HoldOccurrence], cli: Sequence[str]) -> HoldGroup:
    first = items[0]
    reasons = Counter(i.reason for i in items if i.reason)
    reason = reasons.most_common(1)[0][0] if reasons else None
    control = _most_common(i.control_type for i in items)
    with_app = [i for i in items if i.application_id]
    answerable = [i for i in with_app if i.answerable]
    # A sample without a semantic type saves an untyped answer, which the resolver
    # offers to every field with this wording whatever its type.
    untyped = [i for i in answerable if (i.semantic_type or "UNKNOWN") == "UNKNOWN"]
    candidates = untyped or answerable or with_app
    sample = candidates[0] if candidates else None
    answer = act = None
    if sample is not None and sample.application_id:
        if sample.answerable and sample.field_id:
            answer = answer_line(cli, sample.application_id, sample.field_id)
        elif sample.needs_browser:
            act = act_line(cli, sample.application_id)
    return HoldGroup(
        question=truncate(first.label, QUESTION_LIMIT) or "(no wording)",
        holds=len(items),
        applications=len({i.application_id for i in with_app}),
        backends=_backends(i.backend for i in items),
        reason=reason,
        reasons=dict(reasons.most_common()),
        semantic_type=_most_common(i.semantic_type for i in items
                                   if i.semantic_type != "UNKNOWN"),
        control_type=control,
        category=categorize_hold(first.label, reason, control),
        sample_application_id=sample.application_id if sample else None,
        field_id=sample.field_id if sample else None,
        answer=answer,
        act=act,
        application_ids=list(dict.fromkeys(i.application_id for i in with_app
                                           if i.application_id)),
    )


def group_holds(occurrences: Iterable[HoldOccurrence], *, cli: Sequence[str] = (PROG,),
                label_limit: int | None = LEDGER_LABEL_LIMIT) -> list[HoldGroup]:
    """Holds grouped by question wording (``hold_key``), most holds first, then most
    applications; ties keep the order in which the questions were first seen."""
    groups: dict[str, list[HoldOccurrence]] = {}
    for item in occurrences:
        groups.setdefault(hold_key(item.label, label_limit), []).append(item)
    result = [_hold_group(items, cli) for items in groups.values()]
    result.sort(key=lambda g: (-g.holds, -g.applications))
    return result


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def render_hold_groups(groups: Sequence[HoldGroup], *, top: int | None = None) -> list[str]:
    """Markdown lines: a table of the question groups, then one numbered line per group
    that clears it (``answer`` or ``resume --act``). At most ``top`` groups."""
    shown = list(groups if top is None else groups[:top])
    if not shown:
        return []
    lines = ["| # | question | holds | apps | reason | control | type | backends |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {n} | {_cell(g.question)} | {g.holds} | {g.applications} | "
              f"{g.reason or '-'} | {g.control_type or '-'} | {g.semantic_type or '-'} | "
              f"{_cell(', '.join(g.backends))} |" for n, g in enumerate(shown, 1)]
    lines += ["", f"Answer each question once (replace {VALUE_PLACEHOLDER}; a choice takes an "
                  "option value or label, see `interviewmaxxing status APP`):", ""]
    for n, group in enumerate(shown, 1):
        if group.answer:
            lines.append(f"{n}. `{group.answer}`")
        elif group.act:
            lines.append(f"{n}. `{group.act}`  (complete it in the browser window)")
        elif group.sample_application_id:
            lines.append(f"{n}. no field id recorded; `status {group.sample_application_id}` "
                         "shows the question and its field id")
        else:
            lines.append(f"{n}. no application id recorded; see `interviewmaxxing status`")
    if len(groups) > len(shown):
        lines += ["", f"... and {len(groups) - len(shown)} more question(s) (all are in --json)"]
    return lines


# --- fill failures -------------------------------------------------------------------------------

_COST_SUFFIX = re.compile(r"\s*Provider cost: USD .*$")
_URL = re.compile(r"\bhttps?://\S+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_QUOTED = re.compile("(?<!\\w)'[^']*'(?!\\w)|\"[^\"]*\"|\u201c[^\u201d]*\u201d|\u2018[^\u2019]*\u2019")
_LONG_NUMBER = re.compile(r"\d{4,}")


def without_cost_note(text: str) -> str:
    """``text`` without the runner's trailing provider cost note ("Provider cost: USD ...")."""
    return _COST_SUFFIX.sub("", text)


def failure_text(text: str | None) -> str:
    """A failure detail as reports show and group it: whitespace collapsed, the
    runner's provider cost note dropped, URLs, e-mail addresses, quoted values and
    numbers of four or more digits masked (so the same failure on different values,
    pages or timeouts groups together and typed values never show), cut to
    ``DETAIL_LIMIT`` characters."""
    value = without_cost_note(" ".join((text or "").split()))
    value = _URL.sub("<url>", value)
    value = _EMAIL.sub("<email>", value)
    value = _QUOTED.sub("'…'", value)
    value = _LONG_NUMBER.sub("#", value)
    return truncate(value, DETAIL_LIMIT) or "(no detail)"


@dataclass(frozen=True, slots=True)
class FieldFailure:
    """One entry of ``failed_fields`` (all optional: read defensively)."""

    field_id: str | None
    label: str | None
    status: str | None
    detail: str | None


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return None


def failed_fields(raw: Any) -> list[FieldFailure]:
    """``failed_fields`` event metadata as the runner records it
    (``[{"field_id", "label", "status", "detail"}]``); anything else is skipped."""
    items: list[FieldFailure] = []
    for value in raw if isinstance(raw, list) else []:
        if not isinstance(value, dict):
            continue
        item = FieldFailure(field_id=_text(value.get("field_id")), label=_text(value.get("label")),
                            status=_text(value.get("status")), detail=_text(value.get("detail")))
        if item.field_id or item.label or item.detail:
            items.append(item)
    return items


def stored_failure(events: Sequence[ApplicationEvent],
                   app: Application | None) -> tuple[list[FieldFailure], str | None]:
    """The failed fields and failure reason of the application's latest FAILED_RETRYABLE
    stop (``application.failed_retryable``), else its stored ``failure_reason``."""
    fallback = app.failure_reason if app is not None else None
    for event in reversed(events):
        if event.event == FAILED_EVENT:
            reason = _text(event.metadata.get("failure_reason"))
            return failed_fields(event.metadata.get("failed_fields")), reason or fallback
    return [], fallback


@dataclass(frozen=True, slots=True)
class FailureOccurrence:
    kind: str
    """``field`` (one failed field), ``run`` (a run's failure reason) or ``error`` (the
    job printed no outcome or timed out)."""
    detail: str
    status: str | None = None
    label: str | None = None
    application_id: str | None = None
    backend: str = ""


class FailureGroup(Contract):
    """Failures that share a kind, a status and a masked detail."""

    kind: str
    """``field``: one field the browser could not fill or read back (``failed_fields``);
    ``run``: a run that stopped FAILED_RETRYABLE without per-field detail (its failure
    reason); ``error``: a job that printed no outcome, timed out or could not start."""
    status: str | None = None
    """For ``field``: the fill status (``FAILED``, ``VERIFICATION_MISMATCH``, ...)."""
    detail: str
    """``failure_text`` of the detail or reason."""
    failures: int = Field(ge=1)
    applications: int = Field(ge=0)
    backends: list[str] = Field(default_factory=list)
    sample_application_id: str | None = None
    labels: list[str] = Field(default_factory=list)
    """For ``field``: up to five of the failed fields' question wordings, cut to
    ``QUESTION_LIMIT`` characters."""
    application_ids: list[str] = Field(default_factory=list)


def group_failures(occurrences: Iterable[FailureOccurrence]) -> list[FailureGroup]:
    """Failures grouped by kind, status and masked detail; most failures first, then most
    applications, ties in first-seen order."""
    groups: dict[tuple[str, str, str], list[FailureOccurrence]] = {}
    for item in occurrences:
        key = (item.kind, item.status or "", failure_text(item.detail))
        groups.setdefault(key, []).append(item)
    result: list[FailureGroup] = []
    for (kind, status, detail), items in groups.items():
        apps = list(dict.fromkeys(i.application_id for i in items if i.application_id))
        labels = list(dict.fromkeys(truncate(i.label, QUESTION_LIMIT) for i in items if i.label))
        result.append(FailureGroup(
            kind=kind, status=status or None, detail=detail, failures=len(items),
            applications=len(apps), backends=_backends(i.backend for i in items),
            sample_application_id=apps[0] if apps else None, labels=labels[:5],
            application_ids=apps))
    result.sort(key=lambda g: (-g.failures, -g.applications))
    return result


def render_failure_groups(groups: Sequence[FailureGroup], *, top: int | None = None) -> list[str]:
    shown = list(groups if top is None else groups[:top])
    if not shown:
        return []
    lines = ["| kind | status | detail | failures | apps | backends | sample |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for group in shown:
        detail = group.detail
        if group.labels:
            detail += " (fields: " + "; ".join(group.labels[:3]) + ")"
        lines.append(f"| {group.kind} | {group.status or '-'} | {_cell(detail)} | "
                     f"{group.failures} | {group.applications} | {_cell(', '.join(group.backends))} | "
                     f"{group.sample_application_id or '-'} |")
    if len(groups) > len(shown):
        lines += ["", f"... and {len(groups) - len(shown)} more failure group(s) (all are in "
                      "--json)"]
    return lines


# --- interviewmaxxing holds ----------------------------------------------------------------------


class HoldsReport(Contract):
    """``interviewmaxxing holds``: the open holds of the candidate's NEEDS_INPUT
    applications, grouped by question wording."""

    candidate_id: str
    state_db: str
    held: int = Field(default=0, ge=0)
    """Applications with at least one open hold."""
    answered: int = Field(default=0, ge=0)
    """Applications whose every recorded hold was answered after they stopped: waiting
    for ``resume`` or ``prepare-batch --retry``."""
    prepared: int = Field(default=0, ge=0)
    """Applications stopped at their final review step (not listed)."""
    unrecorded: int = Field(default=0, ge=0)
    """NEEDS_INPUT applications that recorded no question or action (for example a
    final step with validation errors); ``resume`` inspects them again."""
    open_holds: int = Field(default=0, ge=0)
    answered_holds: int = Field(default=0, ge=0)
    """Recorded holds answered after their application stopped."""
    questions: list[HoldGroup] = Field(default_factory=list)


def build_holds(paths: LocalPaths, candidate_id: str, *,
                cli: Sequence[str] = (PROG,)) -> HoldsReport:
    """Group the open holds of every NEEDS_INPUT application of ``candidate_id`` by their
    complete question wording. Creates nothing (no state database: an empty report)."""
    report: dict[str, Any] = {"candidate_id": candidate_id, "state_db": str(paths.state_db)}
    if not paths.state_db.is_file():
        return HoldsReport(**report)
    saved = candidate_saved_answers(paths, candidate_id)
    counts: Counter[str] = Counter()
    occurrences: list[HoldOccurrence] = []
    with ApplicationStore.open(paths.state_db) as store:
        for app in store.list_applications(candidate_id=candidate_id, states=[S.NEEDS_INPUT]):
            events = store.list_events(app.id)
            if prepared_stop(events):
                counts["prepared"] += 1
                continue
            job = store.get_job(app.job_id)
            holds = recorded_holds(store, app, events, saved, job)
            counts["answered_holds"] += holds.answered
            if holds.open:
                counts["held"] += 1
            elif holds.recorded:
                counts["answered"] += 1
            else:
                counts["unrecorded"] += 1
            occurrences += [HoldOccurrence(
                label=m.label, reason=m.reason.value,
                control_type=m.control_type.value if m.control_type else None,
                semantic_type=m.semantic_type.value, field_id=m.field_id,
                application_id=app.id, backend=job.ats_type or "") for m in holds.open]
    return HoldsReport(**report, **counts, open_holds=len(occurrences),
                       questions=group_holds(occurrences, cli=cli, label_limit=None))


def render_holds_markdown(report: HoldsReport) -> str:
    lines = ["# Open holds", "",
             f"- candidate: {report.candidate_id}",
             f"- held applications: {report.held}, with {report.open_holds} open hold(s) in "
             f"{len(report.questions)} distinct question(s)"]
    if report.answered_holds:
        lines.append(f"- answered since their application stopped: {report.answered_holds} "
                     f"hold(s); {report.answered} application(s) have nothing open and wait "
                     "for `resume` or `prepare-batch --retry`")
    if report.prepared or report.unrecorded:
        lines.append(f"- not listed: {report.prepared} prepared for final review, "
                     f"{report.unrecorded} stopped without a recorded question")
    lines.append("- nothing was submitted; stored answer values are never shown")
    if report.questions:
        lines += ["", "## Questions", "", *render_hold_groups(report.questions), "",
                  "Then continue: `interviewmaxxing prepare-batch --retry BATCH_ID` (every "
                  "held application of a batch) or `interviewmaxxing resume APP`."]
    return "\n".join(lines) + "\n"


__all__ = [
    "ACT_REASONS",
    "CURRENT_OUTCOMES",
    "DETAIL_LIMIT",
    "HOLD_CATEGORIES",
    "LEDGER_LABEL_LIMIT",
    "NO_FORM_PREFIX",
    "QUESTION_LIMIT",
    "VALUE_PLACEHOLDER",
    "FailureGroup",
    "FailureOccurrence",
    "FieldFailure",
    "HoldGroup",
    "HoldOccurrence",
    "HoldsReport",
    "RecordedHolds",
    "act_line",
    "answer_line",
    "build_holds",
    "candidate_saved_answers",
    "categorize_hold",
    "command_line",
    "current_outcome",
    "failed_fields",
    "failure_text",
    "group_failures",
    "group_holds",
    "hold_key",
    "prepared_stop",
    "recorded_holds",
    "render_failure_groups",
    "render_hold_groups",
    "render_holds_markdown",
    "stored_failure",
    "truncate",
    "without_cost_note",
]
