"""Saved-answer identity and reconciliation.

Two saved answers answer *the same question* when they have the same scope, the same
job target (identity key and URL), the same semantic type and the same question text
after ``normalize_text``. Nothing else makes answers equivalent: an answer about one
job or employer is never related to another, and a JOB answer never merges with a
GLOBAL one.

When the same question has different answers, the one confirmed strictly later
replaces the older ones (the user re-answered). If the latest confirmations
disagree, none of that question's answers is used: they are reported as a conflict
so the question is asked again instead of guessing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from interviewmaxxing_core import SavedAnswer, normalize_text
from interviewmaxxing_core.candidate import SavedAnswerValue

QuestionKey = tuple[str, str | None, str | None, str | None, str]
ValueKey = tuple[str, object]


def question_key(answer: SavedAnswer) -> QuestionKey:
    """Identity of the question ``answer`` answers, including its scope and target."""
    return (
        answer.scope.value,
        answer.job_identity_key,
        answer.job_url,
        answer.semantic_type.value if answer.semantic_type is not None else None,
        normalize_text(answer.question),
    )


def value_key(value: SavedAnswerValue) -> ValueKey:
    """Comparable form of an answer value. Types stay distinct (``False`` is not
    ``0`` and not ``"No"``); text compares case- and whitespace-insensitively and
    label lists compare as sets."""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int | float):
        return ("number", value)
    if isinstance(value, str):
        return ("text", normalize_text(value))
    return ("labels", tuple(sorted({normalize_text(v) for v in value})))


@dataclass(frozen=True)
class SupersededAnswer:
    """A saved answer replaced by a later confirmation of the same question."""

    answer: SavedAnswer
    superseded_by: str


@dataclass(frozen=True)
class AnswerConflict:
    """Answers to one question that disagree with no later confirmation to decide."""

    answers: tuple[SavedAnswer, ...]

    @property
    def answer_ids(self) -> tuple[str, ...]:
        return tuple(a.id for a in self.answers)

    @property
    def question(self) -> str:
        return self.answers[0].question


@dataclass(frozen=True)
class AnswerReconciliation:
    kept: tuple[SavedAnswer, ...]
    superseded: tuple[SupersededAnswer, ...]
    conflicts: tuple[AnswerConflict, ...]


def reconcile_saved_answers(answers: Sequence[SavedAnswer]) -> AnswerReconciliation:
    """Drop superseded and conflicting answers; keep the rest in their original order.

    Answer ids must already be unique."""
    groups: dict[QuestionKey, list[SavedAnswer]] = {}
    for answer in answers:
        groups.setdefault(question_key(answer), []).append(answer)

    excluded: set[str] = set()
    superseded: list[SupersededAnswer] = []
    conflicts: list[AnswerConflict] = []
    for group in groups.values():
        if len({value_key(a.value) for a in group}) <= 1:
            continue
        latest = max(a.confirmed_at for a in group)
        newest = [a for a in group if a.confirmed_at == latest]
        if len({value_key(a.value) for a in newest}) > 1:
            conflicts.append(AnswerConflict(tuple(group)))
            excluded.update(a.id for a in group)
            continue
        winner = newest[0]
        for answer in group:
            if value_key(answer.value) != value_key(winner.value):
                superseded.append(SupersededAnswer(answer, superseded_by=winner.id))
                excluded.add(answer.id)

    return AnswerReconciliation(
        kept=tuple(a for a in answers if a.id not in excluded),
        superseded=tuple(superseded),
        conflicts=tuple(conflicts),
    )
