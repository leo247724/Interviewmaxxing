"""A work-history entry's dates and "I currently work here" box, from the profile's roles.

Round 14 (WP1): Paylocity's application asks for the most recent role as a work-history
entry (company, title, "Start Date MM/YYYY", "End Date MM/YYYY", "I currently work here").
The company and title come from the verified facts as before; the dates and the box come
from ``CandidateProfile.experience`` (the resume's roles, ``start``/``end`` as ``YYYY-MM``):
entry 0 is the most recent role (a current one first, then by start). A date is written only
in a format the question states (``MM/YYYY``, ``YYYY-MM``, a month input); the end date of a
role the person still holds is left blank when the entry asks whether they currently work
there (its box is checked instead), and is otherwise not answered. Every answer cites the
role's verified facts; a role without any is not used."""

from __future__ import annotations

import re
from dataclasses import dataclass

from interviewmaxxing_core import (
    AnswerValue,
    ApplicationField,
    ApplicationForm,
    BooleanValue,
    CandidateProfile,
    ControlType,
    Experience,
    TextValue,
)

_BLOCK = re.compile(
    r"work[-_ ]?history|employment[-_ ]?history|work[-_ ]?experience|job[-_ ]?history|"
    r"employment[-_ ]?record|previous[-_ ]?employ",
    re.IGNORECASE,
)
"""A work-history entry's field path ("txt-workHistory-startDate-0") or section heading."""
_INDEX = re.compile(r"(?:^|[-_.\[])(\d{1,2})\]?$")
"""The entry's index at the end of its field path ("-0", ".0", "[0]")."""
_START = re.compile(r"start|\bfrom\b|begin", re.IGNORECASE)
_END = re.compile(r"end(?:[-_ ]?date)?\b|enddate|\buntil\b|\bto\b", re.IGNORECASE)
_CURRENT = re.compile(
    r"current(?:ly)?[-_ ]?(?:work|employ)|currently\s+work|i\s+(?:currently|still)\s+work|"
    r"still\s+(?:work|employed)",
    re.IGNORECASE,
)
_MM_YYYY = re.compile(r"\bmm\s*/\s*yyyy\b", re.IGNORECASE)
_YYYY_MM = re.compile(r"\byyyy\s*-\s*mm\b", re.IGNORECASE)
_YEAR_MONTH = re.compile(r"(\d{4})-(\d{2})")


@dataclass(frozen=True)
class EntryPart:
    """Which part of which work-history entry a question is."""

    index: int
    part: str
    """``start``, ``end`` or ``current``."""


def entry_part(fld: ApplicationField) -> EntryPart | None:
    """The work-history entry and part ``fld`` asks for, or None: its field path or its
    section names a work history; a checkbox about currently working there, or a text box
    whose field path (else its label) says start or end."""
    if not (_BLOCK.search(fld.id) or any(_BLOCK.search(c) for c in fld.section_context)):
        return None
    match = _INDEX.search(fld.id)
    index = int(match.group(1)) if match else 0
    if fld.control_type is ControlType.CHECKBOX and _CURRENT.search(f"{fld.id} {fld.label}"):
        return EntryPart(index, "current")
    if fld.control_type is not ControlType.TEXT:
        return None
    path = _INDEX.sub("", fld.id)
    for words in (path, fld.label):
        if _START.search(words):
            return EntryPart(index, "start")
        if _END.search(words):
            return EntryPart(index, "end")
    return None


def roles_by_recency(profile: CandidateProfile) -> list[Experience]:
    """The profile's roles, most recent first: current ones, then by start."""
    return sorted(profile.experience, key=lambda role: (role.current, role.start or ""), reverse=True)


def _formatted(value: str | None, fld: ApplicationField) -> str | None:
    """``YYYY-MM`` in the format the question states, else None (never guessed)."""
    match = _YEAR_MONTH.fullmatch(value or "")
    if match is None:
        return None
    year, month = match.groups()
    hint = " ".join([fld.label, fld.help_text or "", fld.placeholder or ""])
    if _MM_YYYY.search(hint):
        return f"{month}/{year}"
    if _YYYY_MM.search(hint) or fld.input_type == "month":
        return f"{year}-{month}"
    return None


def _role(profile: CandidateProfile, part: EntryPart) -> tuple[Experience, list[str]] | None:
    roles = roles_by_recency(profile)
    if part.index >= len(roles):
        return None
    role = roles[part.index]
    facts = [fid for fid in role.fact_ids
             if (fact := profile.find_fact(fid)) is not None and fact.is_verified]
    return (role, facts) if facts else None


def work_history_value(profile: CandidateProfile, fld: ApplicationField
                       ) -> tuple[AnswerValue, list[str]] | None:
    """The answer to a work-history entry's date or "currently work here" box, and the
    verified facts of the role it comes from; None when the question is not one, the role
    or its facts are missing, the month is unknown, or it is a current role's end date."""
    part = entry_part(fld)
    found = _role(profile, part) if part is not None else None
    if part is None or found is None:
        return None
    role, facts = found
    if part.part == "current":
        return BooleanValue(checked=role.current), facts
    text = _formatted(role.start, fld) if part.part == "start" else (
        None if role.current else _formatted(role.end, fld))
    return (TextValue(text=text), facts) if text else None


def blank_while_current(profile: CandidateProfile, form: ApplicationForm, fld: ApplicationField) -> bool:
    """The end date of an entry for a role the person still holds, in an entry that asks
    whether they currently work there: left blank, its box checked instead, so it is not
    asked of the person."""
    part = entry_part(fld)
    if part is None or part.part != "end":
        return False
    found = _role(profile, part)
    if found is None or not found[0].current:
        return False
    return any((other := entry_part(f)) is not None and other.part == "current" and other.index == part.index
               for f in form.fields)
