"""The person's standing answer policies (simple answers ``answer_policies``, round 12).

The person does not answer screener questions one by one. They state one standing answer
per class of question, and each new question takes the answer of the class it belongs to
by its meaning: one Jev decision classifies it (``DynamicPacketResolver``), never its
wording. Each policy is stored as an untyped GLOBAL saved answer whose question is the
policy's own statement below, so an answer cites it as ``SAVED_ANSWER`` like any saved
answer and it backs a field of any type. A policy never takes part in wording
equivalence: its saved question is a rule, not a form question."""

from __future__ import annotations

from collections.abc import Iterable

from .candidate import AnswerScope, SavedAnswer
from .forms import normalize_text

ANSWER_POLICY_SOURCE = "user:simple-answers"
"""Where the policies come from: the person's own simple-answers map, imported as
user-confirmed GLOBAL saved answers (recorded in the answer's note and the trace)."""

ANSWER_POLICY_QUESTIONS: dict[str, str] = {
    "claims_experience_asked": (
        "Answer policy: a question asking whether I have, or have done, led, worked with or "
        "managed an experience, skill, platform or kind of work"),
    "meets_experience_thresholds": (
        "Answer policy: a question asking whether I have at least a number of years of "
        "experience in marketing or one of its areas (N+ years, at least N years, N or more "
        "years), answered from my stated years"),
    "certifies_truth": (
        "Answer policy: a statement certifying that the information I provide is true, "
        "accurate and complete"),
    "not_current_or_former_employee": (
        "Answer policy: a question asking whether I am or was an employee of the company, have "
        "worked at or with it or its affiliates, or have interviewed with it before"),
    "sanctioned_locations": (
        "Answer policy: a question asking whether I am located in, resident in or a national "
        "of a sanctioned country or region (Cuba, Iran, North Korea, Syria, Crimea …)"),
}
"""Each policy key and the saved question its answer is stored under."""

ANSWER_POLICY_KEYS: tuple[str, ...] = tuple(ANSWER_POLICY_QUESTIONS)
ANSWER_POLICY_VALUES: tuple[str, ...] = ("Yes", "No")
"""What a policy may answer; null in the map means no policy for that class."""

POLICY_CONTRADICTIONS: dict[str, dict[str, str]] = {
    "not_current_or_former_employee": {
        "Have you previously been employed by this company?": "Yes",
        "Have you previously interviewed with this company?": "Yes",
    },
}
"""The person's own saved answers that contradict a policy (the simple-answers questions of
``previously_employed_here`` and ``previously_interviewed_here``): when the newest GLOBAL
answer to one of these questions, or the newest one saved for this job, is the value
listed, the policy is not applied and the question waits for the person."""

_BY_QUESTION = {normalize_text(question): key for key, question in ANSWER_POLICY_QUESTIONS.items()}


def answer_policy_key(answer: SavedAnswer) -> str | None:
    """The policy a saved answer states (its question is one of ``ANSWER_POLICY_QUESTIONS``),
    whatever its scope and value, else None."""
    return _BY_QUESTION.get(normalize_text(answer.question))


def policy_value(answer: SavedAnswer) -> str | None:
    """"Yes" or "No" (any case in the saved value), else None."""
    if not isinstance(answer.value, str):
        return None
    return {"yes": "Yes", "no": "No"}.get(normalize_text(answer.value))


def stated_answer_policies(answers: Iterable[SavedAnswer]) -> dict[str, SavedAnswer]:
    """The person's standing policies: for each policy, its newest untyped GLOBAL answer
    when that answer is Yes or No. A policy whose newest answers disagree is left out (the
    person resolves the conflict), and so is a job-scoped or typed one."""
    newest: dict[str, list[SavedAnswer]] = {}
    for answer in answers:
        key = answer_policy_key(answer)
        if key is None or answer.scope is not AnswerScope.GLOBAL or answer.semantic_type is not None:
            continue
        group = newest.setdefault(key, [])
        if group and answer.confirmed_at < group[0].confirmed_at:
            continue
        if group and answer.confirmed_at > group[0].confirmed_at:
            group.clear()
        group.append(answer)
    stated: dict[str, SavedAnswer] = {}
    for key, group in newest.items():
        values = {policy_value(a) for a in group}
        if len(values) == 1 and None not in values:
            stated[key] = group[0]
    return stated


def policy_contradictions(key: str, answers: Iterable[SavedAnswer]) -> list[SavedAnswer]:
    """The person's own answers that contradict policy ``key`` (``POLICY_CONTRADICTIONS``):
    per question, the newest GLOBAL answers and the newest job-scoped ones that state the
    listed value. Pass the answers applicable to the job, so the job-scoped ones are this
    job's ("Yes, I worked here" for this very job). Empty when nothing does."""
    wrong = {normalize_text(question): value
             for question, value in POLICY_CONTRADICTIONS.get(key, {}).items()}
    newest: dict[tuple[str, AnswerScope], list[SavedAnswer]] = {}
    for answer in answers:
        question = normalize_text(answer.question)
        if question not in wrong:
            continue
        group = newest.setdefault((question, answer.scope), [])
        if group and answer.confirmed_at < group[0].confirmed_at:
            continue
        if group and answer.confirmed_at > group[0].confirmed_at:
            group.clear()
        group.append(answer)
    return [answer for (question, _), group in newest.items() for answer in group
            if _yes_no(answer.value) == normalize_text(wrong[question])]


def _yes_no(value: object) -> str | None:
    """A saved yes/no value in comparison form ("yes", "no"; a bool as its word)."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return normalize_text(value) if isinstance(value, str) else None
