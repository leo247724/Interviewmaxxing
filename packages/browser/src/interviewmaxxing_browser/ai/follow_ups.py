"""Conditional follow-up questions (round 11).

A question that only applies when an earlier answer was Yes ("If yes to the above question,
what role and what governmental organization?", "If you were referred by a team member,
please provide their name.", "Please confirm whether the government official mentioned
above …", "Please provide the details and expiration date here if your specific visa or work
authorization …") is a follow-up. Its governing question is the nearest preceding yes/no
question its wording refers to, found here deterministically; the resolver reads that
question's answer: No makes the follow-up not applicable ("N/A" when required, blank when
optional), Yes keeps today's hold."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from interviewmaxxing_core import ApplicationField, FieldOption, normalize_text

_CONDITION = re.compile(
    r"\bif\s+(?:yes|so)\b|\bif\s+you\s+(?:were|are|have|had|answered|selected|responded|checked|"
    r"do|did|hold|need|require|currently)\b|\bif\s+your\b|\bif\s+(?:the\s+)?answer\s+(?:is|was)\s+"
    r"yes\b", re.IGNORECASE)
_CLAUSE = re.compile(r"\bif\b(?P<clause>[^,.;:?!\n]*)", re.IGNORECASE)
_AFFIRMATIVE = re.compile(r"^\s*(?:yes|so|the\s+answer\s+(?:is|was)\s+yes|you\s+answered\s+yes)\b",
                          re.IGNORECASE)
_NEGATED = re.compile(r"\b(?:not|no|never|none|without)\b|n't\b", re.IGNORECASE)
_ABOVE = re.compile(
    r"\b(?:above|previous|preceding|prior)\s+(?:question|answer|response)\b|\b(?:mentioned|stated|"
    r"listed|indicated|noted|referenced|named|identified)\s+above\b|\bquestion\s+above\b",
    re.IGNORECASE)
_VISA = re.compile(r"\bvisa\b|\bwork\s+authori[sz]ation\b|\bwork\s+permit\b|\bsponsor\w*|"
                   r"\bimmigration\b|\bexpir\w*|\bead\b", re.IGNORECASE)
_WORD = re.compile(r"[a-z][a-z'-]{3,}")
_STOP = frozenset({
    "please", "provide", "question", "questions", "answer", "answered", "above", "detail",
    "details", "this", "that", "these", "those", "with", "have", "what", "which", "their",
    "there", "they", "them", "name", "names", "confirm", "whether", "here", "list", "describe",
    "explain", "specify", "position", "role", "company", "employer", "current", "currently",
    "your", "yours", "were", "been", "being", "does", "will", "would", "could", "should",
    "from", "into", "about", "also", "each", "other", "more", "most", "such", "only", "same",
    "than", "then", "when", "where", "while", "applicable", "include", "including", "below",
    "following", "field", "information", "enter", "type", "select", "any", "anyone"})
"""Words a follow-up and its governing question share by accident."""
_WINDOW = 6
"""How many preceding fields a follow-up may refer back to."""
NOT_APPLICABLE_LABELS = frozenset({"n/a", "na", "not applicable", "none", "does not apply",
                                   "not relevant"})
"""Option labels (``normalize_text``) that say a follow-up does not apply."""


def is_follow_up(field: ApplicationField) -> bool:
    """The question applies only after an earlier Yes: a condition ("If yes", "If you were
    referred …", "… if your visa …") or a reference to the question above. A negated
    condition ("If you do not have …", "If you answered no …") applies after a No and is
    never one."""
    text = field.question_text
    if _CONDITION.search(text) is None:
        return _ABOVE.search(text) is not None
    return not any(_NEGATED.search(match.group("clause")) for match in _CLAUSE.finditer(text))


def _condition_clause(field: ApplicationField) -> str | None:
    """The words of the first "if …" clause ("you were referred by a team member"), or
    None when there is none or it only says "if yes"."""
    for match in _CLAUSE.finditer(field.question_text):
        clause = match.group("clause")
        if _CONDITION.search("if" + clause) is None:
            continue
        return None if _AFFIRMATIVE.match(clause) else clause
    return None


def refers_to_visa(field: ApplicationField) -> bool:
    """A follow-up about a visa or work authorization the applicant may not hold ("details
    and expiration date … if your specific visa or work authorization …")."""
    return _CONDITION.search(field.question_text) is not None and _VISA.search(field.question_text) is not None


def _stems(text: str) -> set[str]:
    return {word[:6] for word in _WORD.findall(normalize_text(text)) if word not in _STOP}


def governing_index(fields: Sequence[ApplicationField], index: int,
                    yes_no: Callable[[ApplicationField], bool]) -> int | None:
    """The preceding yes/no question (within ``_WINDOW`` fields) sharing the most words with
    the follow-up's condition ("referred" for "If you were referred by …"; the whole wording
    for "the government official mentioned above"), the nearest on a tie; else, for "If yes"
    or "the above question", the nearest preceding yes/no question when no other question
    sits between them. None when the wording refers to nothing it can be tied to (a
    condition such as "If you are selected" that no question states)."""
    field = fields[index]
    clause = _condition_clause(field)
    affirmative = clause is None and (_ABOVE.search(field.question_text) is not None or re.search(
        r"\bif\s+(?:yes|so)\b", field.question_text, re.IGNORECASE) is not None)
    words = _stems(clause if clause is not None else field.question_text)
    candidates = [i for i in range(index - 1, max(-1, index - 1 - _WINDOW), -1) if yes_no(fields[i])]
    shared = {i: len(words & _stems(fields[i].question_text)) for i in candidates}
    best = max(shared.values(), default=0)
    if best:
        return next(i for i in candidates if shared[i] == best)
    if not affirmative:
        return None
    if candidates and candidates[0] == index - 1 and (
            _ABOVE.search(field.question_text) or re.search(r"\bif\s+(?:yes|so)\b", field.question_text,
                                                           re.IGNORECASE)):
        return candidates[0]
    return None


def not_applicable_option(options: Sequence[FieldOption]) -> FieldOption | None:
    """The one enabled option that says the question does not apply, if there is one."""
    found = [o for o in options if not o.disabled and o.value.strip()
             and normalize_text(o.label).rstrip(".") in NOT_APPLICABLE_LABELS]
    return found[0] if len(found) == 1 else None
