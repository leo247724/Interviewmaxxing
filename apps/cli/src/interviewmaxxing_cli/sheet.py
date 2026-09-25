"""The answer sheet: every distinct open question of the held applications in one
private JSON file, answered in one sitting.

``interviewmaxxing holds --sheet FILE`` (``build_sheet``, ``write_sheet``) writes one
entry per distinct open question (grouped as ``holds`` groups them): the complete
wording, semantic and control type, the recorded options, the backends, the field id on
each application, a ``reuse`` scope (``global`` by default) and ``answer: null``. When the
projected routing trace of one of its applications shows a candidate the resolver
found but did not place because it scored below its gate (a fact screener's best
choice, a saved answer that scored below the gate, a status derivation below the
gate), the entry also carries a ``proposal`` with its ``proposal_basis``. A proposal is
unconfirmed: nothing uses it unless the person copies it into ``answer``. Holds that
need the browser (sign-in, CAPTCHA, an unsupported control, a file) are listed under
``actions`` with their ``resume APP --act`` lines.

``interviewmaxxing answer --sheet FILE`` (``read_sheet``, ``apply_sheet``) applies every
entry whose ``answer`` is not null to each of its applications through the same path as
``interviewmaxxing answer APP``: the value is validated against the question as it was
recorded on that application (an option's value or label; several separated by ``;``
for a multi-select; yes or no for a checkbox), stored as the application's own user
input, and saved for reuse with the entry's scope. An entry that fails validation is
reported by its wording and skipped. A hold answered since its application stopped is
left alone, so applying the same sheet twice changes nothing the second time, and a
sheet generated afterwards no longer lists it.

The sheet holds the person's values, so it is written owner-only (``0600``) and its
contents never reach the terminal: the commands print counts and wordings only.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError

from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_core import (
    AnswerReuse,
    ApplicationState,
    ApplicationStore,
    ClaimUnavailable,
    Contract,
    LocalPaths,
    MissingInput,
    NotFound,
    SavedAnswer,
    UserInput,
    normalize_text,
)

from .answers import AnswerError, user_input_for
from .batch import LEDGER_NAME, list_batches, read_summary
from .triage import (
    HoldOccurrence,
    act_line,
    candidate_saved_answers,
    command_line,
    hold_key,
    prepared_stop,
    recorded_holds,
)

S = ApplicationState

PROG = "interviewmaxxing"
SHEET_VERSION = 1
ROUTING_EVENT = "routing.trace"
"""The runner's route-decision and trace projection event (``runner.ROUTING_EVENT``)."""
ReuseScope = Literal["global", "job", "application"]
SheetValue = str | bool | list[str]
"""What ``answer`` (and a proposal) may hold: text, an option's value or label, several
options separated by ``;`` or as a list, or yes/no (a bool) for a checkbox."""

BELOW_GATE_STAGES: tuple[str, ...] = (
    "question_equivalence", "status_derivation", "option_equivalence", "fact_screener",
    "experience_screener",
)
"""Trace stages whose latest trace for the field may name a below-gate candidate, in the
order they are offered as a proposal: the person's own saved answer first (reworded
wording, then the stated status, then an exact answer mapped onto the options), then
what verified facts suggested."""
_BELOW_GATE_STATUSES: dict[str, frozenset[str]] = {
    "question_equivalence": frozenset({"BELOW_GATE"}),
    "status_derivation": frozenset({"BELOW_GATE"}),
    "option_equivalence": frozenset({"BELOW_GATE"}),
    "fact_screener": frozenset({"UNKNOWN", "RANGE_MISMATCH"}),
    "experience_screener": frozenset({"UNKNOWN"}),
}
_NON_CHOICES = frozenset({"NONE", "UNKNOWN", "NOT_EXPERIENCE", "SUPPORTED"})


# --- the sheet ---------------------------------------------------------------------------------


class SheetOption(Contract):
    value: str
    label: str


class ProposalBasis(Contract):
    """Where a proposal came from: one below-gate trace of one application."""

    stage: str
    """The trace stage (``BELOW_GATE_STAGES``)."""
    score: float | None = None
    """The decision's probability for its choice, as the trace records it."""
    confidence: float | None = None
    status: str | None = None
    """The trace status (``BELOW_GATE``, ``UNKNOWN``, ...)."""
    application_id: str
    """The application whose trace named the candidate."""
    reference_ids: list[str] = Field(default_factory=list)
    """For a saved answer: its ids in the profile."""
    confirmed: Literal[False] = False
    """Always false: a proposal is never applied unless copied into ``answer``."""


class SheetQuestion(Contract):
    """One distinct open question, with a place for its answer."""

    question: str
    """The complete wording as first recorded."""
    semantic_type: str | None = None
    """The most frequent known ``SemanticType`` of its holds."""
    control_type: str | None = None
    options: list[SheetOption] = Field(default_factory=list)
    """The usable options as recorded on the sample application (value and label)."""
    options_by_application: dict[str, list[SheetOption]] = Field(default_factory=dict)
    """For applications whose recorded options differ from ``options``, theirs."""
    backends: list[str] = Field(default_factory=list)
    reason: str | None = None
    applications: int = Field(ge=0)
    fields: dict[str, str] = Field(default_factory=dict)
    """Application id -> the field id of this question on it."""
    reuse: ReuseScope = "global"
    """How far the answer is reused once saved: ``global`` (any job asking this wording),
    ``job`` (each application's job) or ``application`` (these applications only)."""
    answer: SheetValue | None = None
    """Filled in by the person; null leaves the question open."""
    proposal: SheetValue | None = None
    """A below-gate candidate the resolver found (unconfirmed; see ``proposal_basis``)."""
    proposal_basis: ProposalBasis | None = None


class SheetAction(Contract):
    """Holds the person clears in a visible browser window."""

    question: str
    reason: str | None = None
    control_type: str | None = None
    applications: int = Field(ge=0)
    application_ids: list[str] = Field(default_factory=list)
    resume: list[str] = Field(default_factory=list)
    """``interviewmaxxing resume APP --act`` for each application."""


class AnswerSheet(Contract):
    version: int = SHEET_VERSION
    candidate_id: str
    generated_at: datetime
    note: str = ("Fill in `answer` (an option's value or label; several separated by ';'; yes "
                 "or no for a checkbox); a `proposal` is unconfirmed and is used only when "
                 "copied into `answer`. Then: interviewmaxxing answer --sheet FILE.")
    held: int = Field(default=0, ge=0)
    """Applications with at least one open hold."""
    open_holds: int = Field(default=0, ge=0)
    questions: list[SheetQuestion] = Field(default_factory=list)
    actions: list[SheetAction] = Field(default_factory=list)


# --- building it --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Hold:
    """One open hold with everything the sheet needs (the ``HoldOccurrence`` plus the
    recorded question and the application's projected traces for it)."""

    occurrence: HoldOccurrence
    item: MissingInput
    traces: tuple[dict[str, Any], ...]
    """The application's projected routing traces for this field, oldest first."""


def _usable(item: MissingInput) -> list[SheetOption]:
    return [SheetOption(value=o.value, label=o.label) for o in item.options or []
            if o.value.strip() and not o.disabled]


def _field_traces(events: Iterable[Any], item: MissingInput) -> tuple[dict[str, Any], ...]:
    """The projected traces recorded for this question, in order. A trace is matched by
    the field fingerprint, or by the field id when it carries none."""
    found: list[dict[str, Any]] = []
    for event in events:
        if event.event != ROUTING_EVENT:
            continue
        raw = event.metadata.get("traces")
        for trace in raw if isinstance(raw, list) else []:
            if not isinstance(trace, dict):
                continue
            fingerprint = trace.get("field_fingerprint")
            if fingerprint is not None:
                if fingerprint == item.field_fingerprint:
                    found.append(trace)
            elif trace.get("field_id") == item.field_id:
                found.append(trace)
    return tuple(found)


def _saved_value(answer: SavedAnswer) -> SheetValue:
    value = answer.value
    if isinstance(value, bool | list):
        return value
    return str(value)


def _yes_no_option(options: Sequence[SheetOption], word: str) -> SheetValue | None:
    """The option whose label starts with ``word`` (yes or no), or the word itself when
    there are no options (a text question phrased as yes/no)."""
    if not options:
        return word.capitalize()
    matches = [o for o in options if normalize_text(o.label) == word
               or normalize_text(o.label).startswith(word + " ")]
    return matches[0].label if len(matches) == 1 else None


def _option_choice(options: Sequence[SheetOption], choice: Any) -> SheetValue | None:
    """The label of the option the trace names by its opaque key (``o3``)."""
    if not isinstance(choice, str) or not choice.startswith("o") or not choice[1:].isdigit():
        return None
    index = int(choice[1:])
    return options[index].label if index < len(options) else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _proposal_from(trace: dict[str, Any], options: Sequence[SheetOption],
                   saved: dict[str, SavedAnswer]) -> tuple[SheetValue, list[str]] | None:
    """The candidate a below-gate trace names, as a sheet value, with the saved answer
    ids it came from (empty for a screener or a derivation)."""
    stage, choice = trace.get("stage"), trace.get("choice")
    if trace.get("status") not in _BELOW_GATE_STATUSES.get(str(stage), ()):
        return None
    if choice in _NON_CHOICES or choice is None:
        return None
    if stage == "question_equivalence":
        if not (isinstance(choice, str) and choice.startswith("q") and choice[1:].isdigit()):
            return None
        groups = trace.get("candidate_ids")
        index = int(choice[1:])
        if not isinstance(groups, list) or index >= len(groups):
            return None
        ids = [i for i in (groups[index] if isinstance(groups[index], list) else [])
               if isinstance(i, str)]
        answers = [saved[i] for i in ids if i in saved]
        return (_saved_value(answers[0]), ids) if answers else None
    if stage in ("status_derivation", "option_equivalence", "fact_screener"):
        if stage == "fact_screener" and trace.get("kind") not in (None, "choice"):
            return None  # a number or a select-all names facts, whose values are not recorded
        if stage == "option_equivalence":
            decisions = trace.get("decisions")
            if not isinstance(decisions, dict) or len(decisions) != 1:
                return None
            (decision,) = decisions.values()
            choice = decision.get("choice") if isinstance(decision, dict) else None
        value = _option_choice(options, choice)
        return (value, []) if value is not None else None
    if stage == "experience_screener" and choice in ("YES", "NO"):
        value = _yes_no_option(options, choice.lower())
        return (value, []) if value is not None else None
    return None


def _decision_scores(trace: dict[str, Any]) -> tuple[float | None, float | None]:
    if trace.get("stage") == "option_equivalence":
        decisions = trace.get("decisions")
        if isinstance(decisions, dict) and len(decisions) == 1:
            (decision,) = decisions.values()
            if isinstance(decision, dict):
                return _number(decision.get("probability")), _number(decision.get("confidence"))
        return None, None
    score = trace.get("value_probability", trace.get("probability"))
    return _number(score), _number(trace.get("confidence"))


def propose(holds: Sequence[_Hold], options: Sequence[SheetOption],
            saved: dict[str, SavedAnswer]) -> tuple[SheetValue | None, ProposalBasis | None]:
    """The first below-gate candidate among the holds' latest traces, stage by stage in
    ``BELOW_GATE_STAGES`` order, then application by application. Only the latest trace
    of each stage for the field counts (the latest run's view)."""
    for stage in BELOW_GATE_STAGES:
        for hold in holds:
            latest = next((t for t in reversed(hold.traces) if t.get("stage") == stage), None)
            if latest is None:
                continue
            found = _proposal_from(latest, options, saved)
            if found is None:
                continue
            value, ids = found
            score, confidence = _decision_scores(latest)
            application_id = hold.occurrence.application_id or ""
            return value, ProposalBasis(stage=stage, score=score, confidence=confidence,
                                        status=str(latest.get("status") or "") or None,
                                        application_id=application_id, reference_ids=ids)
    return None, None


def _question_entry(holds: Sequence[_Hold], saved: dict[str, SavedAnswer]) -> SheetQuestion:
    first = holds[0]
    reasons = Counter(h.occurrence.reason for h in holds if h.occurrence.reason)
    controls = Counter(h.occurrence.control_type for h in holds if h.occurrence.control_type)
    types = Counter(h.occurrence.semantic_type for h in holds
                    if h.occurrence.semantic_type and h.occurrence.semantic_type != "UNKNOWN")
    options = _usable(first.item)
    fields: dict[str, str] = {}
    variants: dict[str, list[SheetOption]] = {}
    for hold in holds:
        app_id = hold.occurrence.application_id
        if not app_id or app_id in fields or hold.item.field_id is None:
            continue
        fields[app_id] = hold.item.field_id
        theirs = _usable(hold.item)
        if theirs != options:
            variants[app_id] = theirs
    proposal, basis = propose(holds, options, saved)
    return SheetQuestion(
        question=first.item.label, semantic_type=types.most_common(1)[0][0] if types else None,
        control_type=controls.most_common(1)[0][0] if controls else None,
        options=options, options_by_application=variants,
        backends=sorted({h.occurrence.backend or "(none)" for h in holds}),
        reason=reasons.most_common(1)[0][0] if reasons else None,
        applications=len(fields), fields=fields, proposal=proposal, proposal_basis=basis)


def _action_entry(holds: Sequence[_Hold], cli: Sequence[str]) -> SheetAction:
    first = holds[0]
    reasons = Counter(h.occurrence.reason for h in holds if h.occurrence.reason)
    ids = list(dict.fromkeys(h.occurrence.application_id for h in holds
                             if h.occurrence.application_id))
    return SheetAction(question=first.item.label,
                       reason=reasons.most_common(1)[0][0] if reasons else None,
                       control_type=first.occurrence.control_type, applications=len(ids),
                       application_ids=ids, resume=[act_line(cli, app_id) for app_id in ids])


def build_sheet(paths: LocalPaths, candidate_id: str, *,
                cli: Sequence[str] = (PROG,)) -> AnswerSheet:
    """The answer sheet of every NEEDS_INPUT application of ``candidate_id``: its open
    holds grouped by complete wording (as ``holds`` groups them). Reads only; without a
    state database the sheet is empty."""
    now = datetime.now(UTC)
    sheet = AnswerSheet(candidate_id=candidate_id, generated_at=now)
    if not paths.state_db.is_file():
        return sheet
    saved_answers = candidate_saved_answers(paths, candidate_id)
    saved = {a.id: a for a in saved_answers}
    groups: dict[str, list[_Hold]] = {}
    held = open_holds = 0
    with ApplicationStore.open(paths.state_db) as store:
        for app in store.list_applications(candidate_id=candidate_id, states=[S.NEEDS_INPUT]):
            events = store.list_events(app.id)
            if prepared_stop(events):
                continue
            job = store.get_job(app.job_id)
            holds = recorded_holds(store, app, events, saved_answers, job)
            if holds.open:
                held += 1
            for item in holds.open:
                open_holds += 1
                occurrence = HoldOccurrence(
                    label=item.label, reason=item.reason.value,
                    control_type=item.control_type.value if item.control_type else None,
                    semantic_type=item.semantic_type.value, field_id=item.field_id,
                    application_id=app.id, backend=job.ats_type or "")
                groups.setdefault(hold_key(item.label, None), []).append(
                    _Hold(occurrence, item, _field_traces(events, item)))
    questions: list[SheetQuestion] = []
    actions: list[SheetAction] = []
    for members in groups.values():
        if members[0].occurrence.answerable:
            questions.append(_question_entry(members, saved))
        else:
            actions.append(_action_entry(members, cli))
    questions.sort(key=lambda q: -q.applications)
    actions.sort(key=lambda a: -a.applications)
    return sheet.model_copy(update={"held": held, "open_holds": open_holds,
                                    "questions": questions, "actions": actions})


def write_sheet(sheet: AnswerSheet, path: Path, *, force: bool = False) -> None:
    """Write the sheet owner-only (``0600``). An existing file is kept unless ``force``
    (``FileExistsError``): a sheet may hold answers typed since."""
    if path.exists() and not force:
        raise FileExistsError(f"{path} exists; --force replaces it (answers typed into it "
                              "would be lost)")
    text = json.dumps(sheet.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def read_sheet(path: Path) -> AnswerSheet:
    """The sheet as the person left it. Raises ``ValueError`` naming the entry and field
    (never quoting a value) when it does not validate, ``OSError`` when unreadable."""
    try:
        return AnswerSheet.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        problems = "; ".join(".".join(str(part) for part in e["loc"]) + ": " + str(e["msg"])
                             for e in exc.errors())
        raise ValueError(f"{path} is not an answer sheet ({problems})") from None


# --- applying it --------------------------------------------------------------------------------

SKIP_KINDS: tuple[str, ...] = (
    "invalid", "already answered", "not open", "not waiting", "not found", "busy",
)
"""Why an entry was not applied to an application: ``invalid`` (the answer does not fit
the question as recorded there: not one of its options, or the wrong shape), ``already
answered`` (a hold answered since the stop: nothing to do), ``not open`` (the field id is
not a recorded question of the application's current stop), ``not waiting`` (the
application is no longer NEEDS_INPUT), ``not found`` (no such application for the
candidate) and ``busy`` (another run holds the application)."""


class SheetFailure(Contract):
    question: str
    kind: str
    applications: int = Field(ge=1)


class SheetResult(Contract):
    """What ``answer --sheet`` did; counts and wordings only."""

    entries: int = Field(ge=0)
    """Entries of the sheet."""
    answered: int = Field(ge=0)
    """Entries with an ``answer``."""
    applied: int = Field(ge=0)
    """Answers saved (one per application and entry)."""
    applications: int = Field(ge=0)
    """Distinct applications that received an answer."""
    already_answered: int = Field(default=0, ge=0)
    """Answers not saved again because the hold was answered since the stop."""
    skipped: dict[str, int] = Field(default_factory=dict)
    """Answers not saved, counted by ``SKIP_KINDS``."""
    failures: list[SheetFailure] = Field(default_factory=list)
    """Each entry not applied somewhere, with the wording, the kind and how many
    applications it concerns."""


@dataclass
class _Tally:
    applied: int = 0
    applications: set[str] = field(default_factory=set)
    skipped: Counter[str] = field(default_factory=Counter)
    failures: list[SheetFailure] = field(default_factory=list)


def _apply_one(store: ApplicationStore, candidates: LocalCandidateStore, owner: str,
               candidate_id: str, saved_answers: Sequence[SavedAnswer], app_id: str,
               field_id: str, value: SheetValue, reuse: AnswerReuse) -> str | None:
    """Save ``value`` for the recorded question ``field_id`` of ``app_id`` exactly as
    ``interviewmaxxing answer`` would; the skip kind when it was not saved."""
    try:
        app = store.get_application(app_id)
    except NotFound:
        return "not found"
    if app.candidate_id != candidate_id:
        return "not found"
    if app.state is not S.NEEDS_INPUT:
        return "not waiting"
    events = store.list_events(app.id)
    job = store.get_job(app.job_id)
    holds = recorded_holds(store, app, events, saved_answers, job)
    item = next((m for m in holds.recorded if m.field_id == field_id), None)
    if item is None:
        return "not open"
    if item not in holds.open:
        return "already answered"
    try:
        user_input: UserInput = user_input_for(item, value, reuse=reuse)
    except AnswerError:
        return "invalid"
    try:
        claim = store.claim(app.id, owner)
    except ClaimUnavailable:
        return "busy"
    try:
        store.save_user_inputs(claim, [user_input])
    finally:
        store.release(claim)
    saved = user_input.to_saved_answer(job=job)
    if saved is not None:
        candidates.save_answer(app.candidate_id, saved)
    return None


def apply_sheet(paths: LocalPaths, sheet: AnswerSheet, *, owner: str) -> SheetResult:
    """Apply every answered entry of ``sheet`` to each of its applications (see the
    module docstring). Raises ``FileNotFoundError`` without a state database."""
    if not paths.state_db.is_file():
        raise FileNotFoundError("No state database.")
    answered = [q for q in sheet.questions if q.answer is not None]
    tally = _Tally()
    saved_answers = candidate_saved_answers(paths, sheet.candidate_id)
    candidates = LocalCandidateStore.from_paths(paths)
    with ApplicationStore.open(paths.state_db) as store:
        for entry in answered:
            assert entry.answer is not None
            reuse = AnswerReuse(entry.reuse.upper())
            kinds: Counter[str] = Counter()
            for app_id, field_id in entry.fields.items():
                kind = _apply_one(store, candidates, owner, sheet.candidate_id, saved_answers,
                                  app_id, field_id, entry.answer, reuse)
                if kind is None:
                    tally.applied += 1
                    tally.applications.add(app_id)
                else:
                    kinds[kind] += 1
            tally.skipped.update(kinds)
            tally.failures += [SheetFailure(question=entry.question, kind=kind, applications=n)
                               for kind, n in kinds.items() if kind != "already answered"]
    return SheetResult(entries=len(sheet.questions), answered=len(answered),
                       applied=tally.applied, applications=len(tally.applications),
                       already_answered=tally.skipped.get("already answered", 0),
                       skipped={k: tally.skipped[k] for k in SKIP_KINDS if tally.skipped[k]},
                       failures=tally.failures)


def newest_batch_id(paths: LocalPaths) -> str | None:
    """The batch to retry next: the one under ``$IMX_HOME/batches`` whose ledger was
    written last; when that is itself a retry, the batch it retried (its ledger lists
    every application of the run, a retry's only those it selected). None without a
    batch."""
    batches = list_batches(paths)
    if not batches:
        return None
    newest = max(batches, key=lambda b: (paths.home / "batches" / b / LEDGER_NAME).stat().st_mtime)
    summary = read_summary(paths, newest)
    if summary is not None and summary.retry is not None and summary.retry.retry_of in batches:
        return summary.retry.retry_of
    return newest


def retry_line(cli: Sequence[str], batch_id: str) -> str:
    return command_line(cli, "prepare-batch", "--retry", batch_id)


def render_result(result: SheetResult, path: Path) -> list[str]:
    """The lines ``answer --sheet`` prints: counts and the wordings not applied; never
    an answer."""
    lines = [f"answer sheet {path}: {result.answered} of {result.entries} entries answered; "
             f"saved {result.applied} answer(s) for {result.applications} application(s)"]
    if result.already_answered:
        lines.append(f"already answered since the stop (left as they are): "
                     f"{result.already_answered}")
    for failure in result.failures:
        lines.append(f"not applied ({failure.kind}, {failure.applications} application(s)): "
                     f"{' '.join(failure.question.split())}")
    return lines


__all__ = [
    "BELOW_GATE_STAGES",
    "SHEET_VERSION",
    "SKIP_KINDS",
    "AnswerSheet",
    "ProposalBasis",
    "SheetAction",
    "SheetFailure",
    "SheetOption",
    "SheetQuestion",
    "SheetResult",
    "apply_sheet",
    "build_sheet",
    "newest_batch_id",
    "propose",
    "read_sheet",
    "render_result",
    "retry_line",
    "write_sheet",
]
