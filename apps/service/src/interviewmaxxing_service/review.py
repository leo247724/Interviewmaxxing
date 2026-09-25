"""The review page's answers: every question of the prepared form in form order, where
each answer came from, what it cites, and how it can be changed.

A prepared application's pages are the steps an approval pins
(``ApplicationStore._prepared_steps``): the latest packet saved for each step of the
preparing attempt, overridden by the ``steps`` the runner recorded with
``preparation.ready``, the final step taking the prepared packet. The runner records,
for each step, every question of the form (``steps[].fields``: id, fingerprint,
required flag, semantic and control type, and the question's first line as
``label``), so the review lists the questions in the form's own order and shows the
optional ones left blank. A preparation recorded before those records lists the
packets' answers only and offers no edits.

Provenance kinds come from the packet answers' own provenance and notes:

* ``identity``, ``resume``, ``user``: ``PROFILE_IDENTITY``, ``RESUME``, ``USER_INPUT``.
* ``derived``: a ``SAVED_ANSWER`` the resolver derived rather than copied (the salary,
  pay period, work authorization status, start date and work-arrangement derivations;
  their notes say so).
* ``saved_policy``: a ``SAVED_ANSWER`` backed by a standing rule: a policy reference,
  the standing referral answer, or answers saved for global reuse from a question
  (``answer --reuse global`` and answer sheets; ``sa_`` ids with GLOBAL scope).
* ``saved_answer``: any other ``SAVED_ANSWER``: the simple-answer keys, job-scoped
  answers, and answers whose saved record can't be read.
* ``narrative``: text the writer drafted from facts and story passages
  (``GENERATED_FROM_FACTS`` with the writer's note); it cites facts, passages and
  job-description evidence.
* ``fact_screener``: any other answer grounded in verified facts (``CANDIDATE_FACT`` and
  the other ``GENERATED_FROM_FACTS`` answers: screeners, fact copies, templates).
* ``blank``: a question of the form that no answer fills.

An edit is an answer to one question as it was prepared: its step, field id and
fingerprint (``edit_question``), saved through ``POST /applications/{id}/answers``
like any answer, so the next preparation fills it in (the resolver takes the user's
answer to that exact question first). A choice needs the form's options, which the
store keeps only for questions that were once asked (a question stop's
``missing_inputs``) or that a runner records with the step (``choices``); without them
the answer can't be changed here. Reuse beyond this application needs the question's
full wording, since a reusable answer is matched to other forms by it; contact details
change for this application only (the verified profile changes them everywhere), and a
written answer drafted for this job is not offered for every application.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from interviewmaxxing_core import (
    PROFILE_IDENTITY_TYPES,
    AnswerScope,
    AnswerSource,
    ApplicationEvent,
    ApplicationPacket,
    ControlType,
    FieldOption,
    MissingInput,
    MissingReason,
    PacketAnswer,
    SemanticType,
    UserInput,
)

from .models import (
    ProvenanceKind,
    ReuseChoice,
    ReviewCitationsView,
    ReviewControl,
    ReviewEditView,
    ReviewProvenanceView,
    ReviewRowView,
)
from .views import (
    _TYPE_NAMES,
    _control,
    _first_line,
    answerable_options,
    form_step_of,
    is_attestation,
    preparing_attempt,
    question_id,
    review_value,
    view_value,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
TRUNCATION_MARK = "…"
"""How the runner marks a question's first line it cut (``runner._short``)."""


# --- the pinned steps ---------------------------------------------------------------------


@dataclass(frozen=True)
class FieldRecord:
    """One question of a prepared step, as ``preparation.ready`` records it."""

    id: str
    fingerprint: str | None
    required: bool | None
    semantic_type: SemanticType
    control_type: ControlType | None
    label: str
    """The question's first line, cut to 80 characters (``…`` when cut)."""
    question: str | None = None
    """The question's full wording, when a runner records it (``question``)."""
    choices: tuple[FieldOption, ...] | None = None
    """The question's options, when a runner records them (``choices``)."""


@dataclass(frozen=True)
class ReviewStep:
    """One page of the prepared form and the packet that fills it."""

    form_step: int
    packet_id: str
    form_url: str | None = None
    fields: tuple[FieldRecord, ...] | None = None
    """The page's questions in form order; None when the preparation didn't record them."""


def _semantic_type(raw: object) -> SemanticType:
    try:
        return SemanticType(raw) if isinstance(raw, str) else SemanticType.UNKNOWN
    except ValueError:
        return SemanticType.UNKNOWN


def _control_type(raw: object) -> ControlType | None:
    try:
        return ControlType(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def _field_record(raw: object) -> FieldRecord | None:
    if not isinstance(raw, dict):
        return None
    field_id = raw.get("id")
    if not isinstance(field_id, str) or not field_id:
        return None
    fingerprint = raw.get("fingerprint")
    required = raw.get("required")
    label = raw.get("label")
    question = raw.get("question")
    choices: tuple[FieldOption, ...] | None = None
    if isinstance(raw.get("choices"), list):
        parsed = []
        for item in raw["choices"]:
            try:
                parsed.append(FieldOption.model_validate(item))
            except ValidationError:
                continue
        choices = tuple(parsed)
    return FieldRecord(
        id=field_id,
        fingerprint=fingerprint if isinstance(fingerprint, str) and _SHA256.match(fingerprint) else None,
        required=required if isinstance(required, bool) else None,
        semantic_type=_semantic_type(raw.get("semantic_type")),
        control_type=_control_type(raw.get("control_type")),
        label=label.strip() if isinstance(label, str) and label.strip() else field_id,
        question=question if isinstance(question, str) and question.strip() else None,
        choices=choices,
    )


def prepared_steps(
    events: Sequence[ApplicationEvent], prepared: ApplicationEvent
) -> list[ReviewStep]:
    """The pages an approval of ``prepared`` pins, in step order, as
    ``ApplicationStore._prepared_steps`` chooses them, with the questions the runner
    recorded for each page."""
    latest: dict[int, ReviewStep] = {}
    for event in preparing_attempt(events, prepared):
        if event.event != "packet.saved":
            continue
        step, packet_id = event.metadata.get("form_step"), event.metadata.get("packet_id")
        if isinstance(step, int) and not isinstance(step, bool) and isinstance(packet_id, str):
            latest[step] = ReviewStep(form_step=step, packet_id=packet_id)
    recorded = prepared.metadata.get("steps")
    for item in recorded if isinstance(recorded, list) else []:
        if not isinstance(item, dict):
            continue
        step, packet_id = item.get("form_step"), item.get("packet_id")
        if not (isinstance(step, int) and not isinstance(step, bool) and isinstance(packet_id, str)):
            continue
        fields = item.get("fields")
        records = (
            tuple(r for r in (_field_record(f) for f in fields) if r is not None)
            if isinstance(fields, list) else None
        )
        url = item.get("form_url")
        latest[step] = ReviewStep(form_step=step, packet_id=packet_id,
                                  form_url=url if isinstance(url, str) else None, fields=records)
    final_step, final_packet = form_step_of(prepared), prepared.metadata.get("packet_id")
    if final_step is not None and isinstance(final_packet, str):
        final = latest.get(final_step)
        url = prepared.metadata.get("form_url")
        kept = {step: s for step, s in latest.items() if step < final_step}
        kept[final_step] = (
            final if final is not None and final.packet_id == final_packet
            else ReviewStep(form_step=final_step, packet_id=final_packet,
                            form_url=url if isinstance(url, str) else None)
        )
        latest = kept
    return [latest[step] for step in sorted(latest)]


# --- provenance and citations ----------------------------------------------------------------

PROVENANCE_LABELS: dict[ProvenanceKind, str] = {
    "identity": "Your details",
    "resume": "Your resume",
    "saved_answer": "Saved answer",
    "saved_policy": "Saved policy",
    "derived": "Derived",
    "fact_screener": "Fact-grounded screener",
    "narrative": "RAG narrative",
    "user": "Your answer",
    "blank": "Left blank",
}
"""The badges' words, the review lane's own vocabulary (the dashboard shows them as sent)."""

_DERIVED_NOTES = (
    "derived from",
    "pay period stated in",
    "the saved earliest start date",
    "availability bucket containing",
    "saved work-arrangement preference",
    "saved relocation answer",
)
"""Notes of saved answers the resolver derived an answer from, rather than copied: the
salary, pay period, work authorization status, start date, work arrangement and
relocation derivations (``interviewmaxxing_browser.ai.routing``)."""
_POLICY_NOTES = ("standing ", "policy")
_POLICY_REFERENCES = ("policy:", "policy_", "pol_", "answer_policy")
"""Reference ids of standing answer rules (answer policies), however a runner names them."""
_NARRATIVE_NOTE = "opus draft"
_STORY_MARK = "story evidence"
_JOB_MARK = "job context"


@dataclass(frozen=True)
class SavedAnswerRef:
    """What the review needs of one saved answer: its wording and scope, never its value."""

    id: str
    question: str
    scope: AnswerScope


def _saved_kind(
    refs: Sequence[str], note: str, saved: Mapping[str, SavedAnswerRef]
) -> ProvenanceKind:
    lowered = note.lower()
    if lowered.startswith(_DERIVED_NOTES):
        return "derived"
    if any(r.lower().startswith(_POLICY_REFERENCES) for r in refs) or lowered.startswith(_POLICY_NOTES):
        return "saved_policy"
    known = [ref for ref in (saved.get(r) for r in refs) if ref is not None]
    if known and all(
        ref.scope is AnswerScope.GLOBAL and not ref.id.startswith("simple_answer")
        for ref in known
    ):
        return "saved_policy"
    return "saved_answer"


def provenance_kind(
    answer: PacketAnswer, saved: Mapping[str, SavedAnswerRef]
) -> ProvenanceKind:
    source = answer.provenance.source
    note = (answer.provenance.note or "").strip()
    if source is AnswerSource.PROFILE_IDENTITY:
        return "identity"
    if source is AnswerSource.RESUME:
        return "resume"
    if source is AnswerSource.USER_INPUT:
        return "user"
    if source is AnswerSource.SAVED_ANSWER:
        return _saved_kind(answer.provenance.reference_ids, note, saved)
    if source is AnswerSource.GENERATED_FROM_FACTS and note.lower().startswith(_NARRATIVE_NOTE):
        return "narrative"
    return "fact_screener"


def _cited_ids(note: str, mark: str) -> tuple[list[str], int, int]:
    """The ``id`` of each item of the JSON list the note records after ``mark``, with
    where that part of the note starts and ends."""
    start = note.find(mark)
    if start < 0:
        return [], -1, -1
    bracket = note.find("[", start)
    if bracket < 0:
        return [], -1, -1
    try:
        items, end = json.JSONDecoder().raw_decode(note, bracket)
    except ValueError:
        return [], -1, -1
    ids = [str(i["id"]) for i in items if isinstance(i, dict) and isinstance(i.get("id"), str)] \
        if isinstance(items, list) else []
    return ids, start, end


def _note_parts(note: str) -> tuple[str, list[str], list[str]]:
    """The note without its citation lists, and the story passage and job evidence ids
    those lists name."""
    passages, story_start, story_end = _cited_ids(note, _STORY_MARK)
    job, job_start, job_end = _cited_ids(note, _JOB_MARK)
    spans = sorted((s, e) for s, e in ((story_start, story_end), (job_start, job_end)) if s >= 0)
    text, cursor = "", 0
    for start, end in spans:
        text += note[cursor:start]
        cursor = end
    text += note[cursor:]
    cleaned = "; ".join(part.strip() for part in text.split(";") if part.strip())
    return cleaned, passages, job


def provenance_view(
    answer: PacketAnswer | None, saved: Mapping[str, SavedAnswerRef]
) -> tuple[ReviewProvenanceView, ReviewCitationsView | None]:
    if answer is None:
        return ReviewProvenanceView(kind="blank", label=PROVENANCE_LABELS["blank"], detail=None), None
    kind = provenance_kind(answer, saved)
    note, passages, job = _note_parts((answer.provenance.note or "").strip())
    citations = None
    if kind in ("narrative", "fact_screener"):
        citations = ReviewCitationsView(
            facts=list(dict.fromkeys(answer.provenance.reference_ids)),
            passages=list(dict.fromkeys(passages)),
            job_evidence=list(dict.fromkeys(job)),
        )
    return ReviewProvenanceView(kind=kind, label=PROVENANCE_LABELS[kind], detail=note or None), citations


# --- rows ------------------------------------------------------------------------------------

_CHOICE_CONTROLS = frozenset(
    {ControlType.SELECT, ControlType.RADIO, ControlType.MULTISELECT, ControlType.CHECKBOX_GROUP}
)
_ROW_CONTROLS: dict[ControlType, ReviewControl] = {
    ControlType.TEXTAREA: "long_text",
    ControlType.SELECT: "single_select",
    ControlType.RADIO: "single_select",
    ControlType.MULTISELECT: "multi_select",
    ControlType.CHECKBOX_GROUP: "multi_select",
    ControlType.CHECKBOX: "boolean",
    ControlType.FILE: "file",
}
ALL_SCOPES: list[ReuseChoice] = ["application", "job", "global"]

NO_RECORDS = (
    "This preparation didn't record the form's questions, so its answers can't be changed "
    "here. Prepare it again to change them."
)
NO_OPTIONS = (
    "The form's options for this question weren't recorded when it was filled, so it can't "
    "be changed here."
)
NO_FILES = "Files are attached as prepared: the resume is fixed for this application."
NO_CONTROL = "This kind of control can't be set from the dashboard."
APPLICATION_ONLY = (
    "Kept for this application only: the question's full wording wasn't recorded, so a "
    "reusable answer couldn't be matched to it on other forms."
)
CONTACT_DETAILS_ONLY = (
    "Kept for this application only: your contact details come from your verified profile, "
    "so change them there to change them everywhere."
)
NARRATIVE_NOT_GLOBAL = (
    "A written answer is drafted for this job, so it can be kept for this application or "
    "this job, not for every application."
)


@dataclass(frozen=True)
class ReviewInputs:
    """What the rows are built from, besides the packets."""

    user_inputs: Sequence[UserInput]
    recorded: Mapping[tuple[int, str], MissingInput]
    """Questions recorded in question stops, by (form step, field id): ``recorded_questions``."""
    saved: Mapping[str, SavedAnswerRef]
    """Saved answers by id; a mapping that reads the profile on first use."""


@dataclass(frozen=True)
class ReviewAnswers:
    rows: list[ReviewRowView]
    questions: dict[str, MissingInput]
    """The editable questions by question id (``question_id``), for ``plan_answers``."""
    reuse: dict[str, list[ReuseChoice]]
    """The reuse scopes each editable question offers (``ReviewEditView.reuse``)."""


def _recorded_for(
    step: int, field_id: str, fingerprint: str | None, inputs: ReviewInputs
) -> MissingInput | None:
    """The question a stop recorded for this field, only while it is the same question."""
    recorded = inputs.recorded.get((step, field_id))
    if recorded is None or (fingerprint is not None and recorded.field_fingerprint != fingerprint):
        return None
    return recorded


def _user_input_for(step: int, record: FieldRecord, inputs: ReviewInputs) -> UserInput | None:
    for item in reversed(inputs.user_inputs):
        if (item.form_step == step and item.field_id == record.id
                and item.field_fingerprint == record.fingerprint):
            return item
    return None


def _question(
    step: int,
    record: FieldRecord | None,
    answer: PacketAnswer | None,
    recorded: MissingInput | None,
    inputs: ReviewInputs,
) -> tuple[str, bool]:
    """The wording shown for a row: the user's own question, a recorded question, the
    form's first line, a saved answer's question, else a plain name for its kind."""
    if answer is not None and answer.provenance.source is AnswerSource.USER_INPUT:
        refs = set(answer.provenance.reference_ids)
        given = next((i for i in inputs.user_inputs if i.id in refs), None)
        if given is not None and given.question.strip():
            return _first_line(given.question), True
    if recorded is not None and recorded.label.strip():
        return _first_line(recorded.label), True
    if record is not None and record.label.strip() and record.label != record.id:
        return record.label, True
    if answer is not None and answer.provenance.source is AnswerSource.SAVED_ANSWER:
        for ref in answer.provenance.reference_ids:
            saved = inputs.saved.get(ref)
            if saved is not None and saved.question.strip():
                return _first_line(saved.question), True
    semantic = answer.semantic_type if answer is not None else (
        record.semantic_type if record is not None else SemanticType.UNKNOWN)
    return _TYPE_NAMES.get(semantic, "Question on the form"), False


def _wording(
    step: int, record: FieldRecord, recorded: MissingInput | None, inputs: ReviewInputs
) -> tuple[str, bool]:
    """The question's wording for an edit, and whether it is the full wording."""
    if recorded is not None and recorded.label.strip():
        return recorded.label, True
    if record.question:
        return record.question, True
    given = _user_input_for(step, record, inputs)
    if given is not None and given.question.strip():
        return given.question, True
    return record.label, not record.label.endswith(TRUNCATION_MARK)


def edit_question(
    step: ReviewStep, record: FieldRecord | None, packet: ApplicationPacket,
    inputs: ReviewInputs,
) -> tuple[MissingInput | None, str | None, str | None]:
    """The question an edit of this row answers, or why there is none; and, when the
    answer may be kept for this application only, why (``APPLICATION_ONLY`` without the
    full wording, ``CONTACT_DETAILS_ONLY`` for contact details, which a reusable answer
    would shadow on every form while the verified profile says otherwise)."""
    if record is None or record.fingerprint is None:
        return None, NO_RECORDS, None
    control = record.control_type
    if control is ControlType.FILE:
        return None, NO_FILES, None
    if control is None or control is ControlType.UNSUPPORTED:
        return None, NO_CONTROL, None
    recorded = _recorded_for(step.form_step, record.id, record.fingerprint, inputs)
    options: list[FieldOption] | None = None
    if control in _CHOICE_CONTROLS:
        options = list(recorded.options or []) if recorded is not None else list(record.choices or [])
        if not options:
            return None, NO_OPTIONS, None
    elif control is ControlType.TYPEAHEAD and recorded is not None:
        options = list(recorded.options or []) or None
    wording, full = _wording(step.form_step, record, recorded, inputs)
    form_url = step.form_url or (recorded.form_url if recorded is not None else None) or packet.form_url
    question = MissingInput(
        field_id=record.id,
        form_url=form_url,
        form_step=step.form_step,
        field_fingerprint=record.fingerprint,
        label=wording,
        reason=MissingReason.NO_ANSWER,
        prompt="Change this answer.",
        semantic_type=record.semantic_type,
        control_type=control,
        options=options,
        required=bool(record.required),
    )
    if record.semantic_type in PROFILE_IDENTITY_TYPES:
        return question, None, CONTACT_DETAILS_ONLY
    return question, None, None if full else APPLICATION_ONLY


def _edit_view(
    question: MissingInput, answer: PacketAnswer | None, reuse: list[ReuseChoice],
    note: str | None,
) -> ReviewEditView:
    lookup = question.control_type is ControlType.TYPEAHEAD
    choice = question.control_type in _CHOICE_CONTROLS or (lookup and bool(question.options))
    return ReviewEditView(
        control=_control(question),
        options=answerable_options(question) if choice else None,
        lookup=lookup,
        attestation=is_attestation(question),
        required=question.required,
        value=view_value(answer.value) if answer is not None else None,
        reuse=reuse,
        note=note,
    )


def _scopes(only_here: str | None, kind: ProvenanceKind) -> tuple[list[ReuseChoice], str | None]:
    """The reuse scopes an edit offers, and the note that says why when they are fewer."""
    if only_here:
        return ["application"], only_here
    if kind == "narrative":
        return ["application", "job"], NARRATIVE_NOT_GLOBAL
    return list(ALL_SCOPES), None


def _row(
    step: ReviewStep,
    packet: ApplicationPacket,
    record: FieldRecord | None,
    answer: PacketAnswer | None,
    inputs: ReviewInputs,
    questions: dict[str, MissingInput],
    scopes: dict[str, list[ReuseChoice]],
) -> ReviewRowView | None:
    field_id = record.id if record is not None else answer.field_id if answer is not None else ""
    recorded = _recorded_for(step.form_step, field_id,
                             record.fingerprint if record is not None else None, inputs)
    value: str | list[str] | None = None
    if answer is not None:
        textarea = (
            (record is not None and record.control_type is ControlType.TEXTAREA)
            or (recorded is not None and recorded.control_type is ControlType.TEXTAREA)
        )
        rendered = review_value(answer, textarea=textarea)
        if rendered is None:
            return None
        control, value = rendered
    elif record is not None and record.control_type is not None:
        control = _ROW_CONTROLS.get(record.control_type, "text")
    else:
        control = "text"
    question, wording_recorded = _question(step.form_step, record, answer, recorded, inputs)
    provenance, citations = provenance_view(answer, inputs.saved)
    target, no_edit, only_here = edit_question(step, record, packet, inputs)
    qid = question_id(target) if target is not None else None
    reuse, note = _scopes(only_here, provenance.kind)
    if target is not None and qid is not None:
        questions[qid] = target
        scopes[qid] = reuse
    return ReviewRowView(
        question_id=qid,
        question=question,
        wording_recorded=wording_recorded,
        page=step.form_step + 1,
        control=control,
        value=value,
        required=record.required if record is not None else None,
        provenance=provenance,
        citations=citations,
        confidence=float(answer.confidence) if answer is not None else None,
        edit=_edit_view(target, answer, reuse, note) if target is not None else None,
        no_edit_reason=no_edit,
    )


def review_answers(
    steps: Sequence[ReviewStep],
    packets: Mapping[str, ApplicationPacket],
    inputs: ReviewInputs,
) -> ReviewAnswers:
    """Every question of the pinned pages in form order with its answer (or a blank),
    and the questions an edit may answer."""
    rows: list[ReviewRowView] = []
    questions: dict[str, MissingInput] = {}
    scopes: dict[str, list[ReuseChoice]] = {}
    for step in sorted(steps, key=lambda s: s.form_step):
        packet = packets.get(step.packet_id)
        if packet is None:
            continue
        answered: dict[str, PacketAnswer] = {a.field_id: a for a in packet.answers}
        pairs: list[tuple[FieldRecord | None, PacketAnswer | None]] = []
        if step.fields is not None:
            pairs.extend((field, answered.pop(field.id, None)) for field in step.fields)
        pairs.extend((None, answer) for answer in answered.values())
        for record, answer in pairs:
            row = _row(step, packet, record, answer, inputs, questions, scopes)
            if row is not None:
                rows.append(row)
    return ReviewAnswers(rows=rows, questions=questions, reuse=scopes)


def packet_rows(packets: Sequence[ApplicationPacket], inputs: ReviewInputs) -> list[ReviewRowView]:
    """The answers of packets outside a prepared stop (a stop for a browser action, a
    failure), in form order; nothing can be edited from them."""
    steps = [ReviewStep(form_step=p.form_step, packet_id=p.id) for p in packets]
    rows = review_answers(steps, {p.id: p for p in packets}, inputs).rows
    return [row.model_copy(update={"question_id": None, "edit": None, "no_edit_reason": None})
            for row in rows]
