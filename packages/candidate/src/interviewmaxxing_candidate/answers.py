"""Saved-answer identity and reconciliation.

Two saved answers answer *the same question* when they have the same scope, the same
job target (identity key and URL), the same semantic type and the same question text
after ``normalize_text``. Nothing else makes answers equivalent: an answer about one
job or employer is never related to another, and a JOB answer never merges with a
GLOBAL one.

When the same question has different answers, only its newest confirmation counts:
an older answer whose value differs from every newest one is superseded (the user
re-answered) and dropped. If the newest confirmations themselves disagree, they are
all kept and reported as a conflict. Keeping them lets the resolver see the
disagreement and report the question as ambiguous. Dropping them would let a less
specific answer (e.g. a GLOBAL one) fill the field instead.
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
    superseded_by: tuple[str, ...]
    """Ids of the newest confirmations (more than one if they conflict)."""


@dataclass(frozen=True)
class AnswerConflict:
    """Kept answers to one question that disagree, with no later confirmation to decide.

    They stay in the profile; a resolver must treat the question as ambiguous."""

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


def newest_confirmations(group: Sequence[SavedAnswer]) -> list[SavedAnswer]:
    """The answers in ``group`` with the latest ``confirmed_at``."""
    latest = max(a.confirmed_at for a in group)
    return [a for a in group if a.confirmed_at == latest]


def reconcile_saved_answers(answers: Sequence[SavedAnswer]) -> AnswerReconciliation:
    """Drop superseded answers and report conflicts; keep the rest in original order.

    Conflicting answers are kept (see the module docstring). Answer ids must
    already be unique."""
    groups: dict[QuestionKey, list[SavedAnswer]] = {}
    for answer in answers:
        groups.setdefault(question_key(answer), []).append(answer)

    excluded: set[str] = set()
    superseded: list[SupersededAnswer] = []
    conflicts: list[AnswerConflict] = []
    for group in groups.values():
        if len({value_key(a.value) for a in group}) <= 1:
            continue
        newest = newest_confirmations(group)
        newest_ids = tuple(a.id for a in newest)
        newest_values = {value_key(a.value) for a in newest}
        for answer in group:
            if value_key(answer.value) not in newest_values:
                superseded.append(SupersededAnswer(answer, superseded_by=newest_ids))
                excluded.add(answer.id)
        if len(newest_values) > 1:
            conflicts.append(AnswerConflict(tuple(a for a in group if a.id not in excluded)))

    return AnswerReconciliation(
        kept=tuple(a for a in answers if a.id not in excluded),
        superseded=tuple(superseded),
        conflicts=tuple(conflicts),
    )
