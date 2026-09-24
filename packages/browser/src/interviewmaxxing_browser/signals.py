"""Pure text rules: page wording, confirmation references, job ids, button intent.

These only *recognize* wording; decisions about acceptance live in
:mod:`interviewmaxxing_browser.runtime`, which also requires the wording to be tied
to the application being submitted.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


ACCEPTANCE = _rx(
    r"\b(?:application (?:has been |was |is )?(?:successfully )?(?:submitted|received|complete)|"
    r"thank(?:s| you) for (?:applying|your application|submitting)|"
    r"we(?:'ve| have) received your application|"
    r"your application (?:has been|was|is) (?:successfully )?(?:submitted|received|complete)|"
    r"successfully applied|you(?:'ve| have) (?:successfully )?applied)\b"
)
"""Acceptance *wording*. Use :func:`affirmative_acceptance`, which also rejects
negated, conditional, instructional and questioning uses of the same words."""

# Words before the phrase (in the same clause) that negate it or make it
# conditional, future or an instruction: "No application received", "If your
# application was submitted", "Once your application is received", "Click submit to
# have your application received".
_NEGATING_PREFIX = _rx(
    r"\b(?:no|not|never|none|nothing|cannot|unable|failed|without|if|unless|until|once|"
    r"when|whether|before|after|click|press|please|to have|will|would|should|must|may|"
    r"might|could)\b|n't\b"
)
# What directly follows the phrase: "Application submitted: no", "... not yet".
_NEGATING_SUFFIX = _rx(
    r"^\W*(?:not\b|no\b|none\b|never\b|pending|failed|false|incomplete|unconfirmed|"
    r"unknown|n/?a\b)"
)
_CLAUSE = re.compile(r"[^.!?;\n]+[.!?;]?")


def affirmative_acceptance(text: str) -> str | None:
    """The first affirmative acceptance statement in ``text`` ("Application
    submitted"), or None. Statements that are negated, conditional, future,
    instructional or questions do not count."""
    clauses: list[str] = _CLAUSE.findall(text)
    for clause in clauses:
        stripped = clause.strip()
        if not stripped or stripped.endswith("?"):
            continue
        for match in ACCEPTANCE.finditer(stripped):
            before, after = stripped[: match.start()], stripped[match.end() :]
            if _NEGATING_PREFIX.search(before) or _NEGATING_SUFFIX.search(after):
                continue
            return stripped
    return None


APPLICATION_STATUS = _rx(
    r"\b(?:draft|submitted|received|in review|under review|reviewing|not submitted|incomplete|"
    r"rejected|declined|withdrawn|applied|pending|processing|interview(?:ing)?|offer(?:ed)?|"
    r"hired|in progress|started)\b"
)
"""Words that describe an application's state in a status list or portal."""

NOT_SUBMITTED_STATUS = _rx(
    r"\bdraft\b|not (?:yet )?submitted|unsubmitted|\bincomplete\b|\bwithdrawn\b|"
    r"\bcancel+ed\b|\bin progress\b|\bstarted\b"
)
"""A record showing one of these cannot also prove that the application was submitted."""


UNCERTAIN = _rx(
    r"cannot confirm|can't confirm|could not confirm|couldn't confirm|unable to confirm|"
    r"not sure whether|whether (?:or not )?your application|may (?:not )?have been|"
    r"timed? ?out|try again later|server error|something went wrong|temporarily unavailable|"
    r"bad gateway|service unavailable|network error|connection (?:was )?(?:lost|reset|closed)"
)
"""Wording that says the outcome itself is unknown (transport or server trouble)."""
ALREADY_APPLIED = _rx(
    r"you(?:'ve| have)? already applied|already submitted an application|"
    r"you already have an application|application (?:for this (?:job|role|position) )?already exists"
)
JOB_CLOSED = _rx(
    r"\bno longer (?:accepting applications|available|active)\b|"
    r"\b(?:job|position|posting|role|opening|requisition)(?: posting)? (?:is|has been|was) "
    r"(?:closed|filled|removed|no longer active|not currently active)\b|"
    r"\bthis job is closed\b|"
    r"\b(?:job|position|posting|role|opening|requisition)(?: posting)? has expired\b|"
    r"\bdoes not exist or is not currently active\b|\bis not currently active\b"
)
"""Wording that says the job itself is gone (closed, filled, expired, inactive). Tied
to job words where the verb alone is ambiguous: a session, not a job, "has expired"."""
PENDING = _rx(r"still processing|being processed|cannot confirm|can't confirm|pending review")
ERROR_HEADING = _rx(
    r"something went wrong|error|not found|unavailable|try again later|bad gateway|timed? ?out"
)
CAPTCHA_TEXT = _rx(
    r"captcha|verify (?:that )?you(?:'re| are) (?:a )?human|are you a robot|not a robot|"
    r"characters (?:shown|in the image)|security check|checking your browser"
)
APPLY_LINK = _rx(
    r"^(?!.*\bsubmit\b)(?!.*\bapply (?:with|using|via)\b)(?!.*\buse my\b)\s*(?:"
    r"apply(?: now| here| online| today)?"
    r"|apply (?:for|to) (?:this |the )?(?:job|role|position|opening)"
    r"|(?:start|begin) (?:your |my )?application"
    r"|continue to (?:the |your )?application"
    r"|i'?m interested)\s*[\u00bb\u203a>\u2192]?\s*$"
)
"""Text of a control that leads to the application form (a posting's apply link or
button). Third-party flows ("Apply with LinkedIn", "Use my Indeed resume") and
anything mentioning "submit" are excluded."""
STATUS_LINK = _rx(
    r"application status|check (?:your )?status|already applied|my applications|candidate (?:home|portal)"
)

CONFIRMATION_LINK = _rx(r"^\s*view (?:your )?(?:confirmation|receipt)\s*$")
"""An explicit read-only receipt link; reconciliation also requires the same origin."""

_REFERENCE = _rx(
    r"\b(?:confirmation|reference|application|submission)"
    r"(?:\s+(?:number|no\.?|code|id|reference|#))?\s*[:#]\s*([A-Z0-9][A-Z0-9-]{2,38}[A-Z0-9])\b"
)
_JOB_ID = _rx(
    r"\b(?:job|req(?:uisition)?|posting|position)\s*(?:id|#|no\.?|number|code)\s*[:#]?\s*"
    r"([A-Z0-9][A-Z0-9._/-]{0,62}[A-Z0-9])"
)


def confirmation_references(text: str) -> list[str]:
    """Reference-like tokens ("Confirmation reference: BWA-000123"), in order.
    A token must contain a digit so words are never taken for references."""
    seen: list[str] = []
    for match in _REFERENCE.finditer(text):
        token = match.group(1)
        if any(ch.isdigit() for ch in token) and token not in seen:
            seen.append(token)
    return seen


def job_ids(text: str) -> list[str]:
    """Job/requisition ids shown as text ("Job ID BWA-ENG-101")."""
    seen: list[str] = []
    for match in _JOB_ID.finditer(text):
        token = match.group(1)
        if any(ch.isdigit() for ch in token) and token not in seen:
            seen.append(token)
    return seen


class ButtonIntent(StrEnum):
    SUBMIT = "SUBMIT"
    """Final submission of the application."""
    NEXT = "NEXT"
    """Forward navigation to another step."""
    AMBIGUOUS = "AMBIGUOUS"
    """Could be either; never clicked automatically."""
    OTHER = "OTHER"
    """Back, cancel, upload, save draft, sign in, ...: not a step action."""


_SUBMIT = _rx(
    r"\bsubmit\b|send (?:my )?application|\bapply\b|finish|complete (?:my )?application|"
    r"confirm and send"
)
_NEXT = _rx(r"\bnext\b|continue|proceed|\breview\b|go to step|save (?:and|&) (?:next|continue)")
_OTHER = _rx(
    r"\bback\b|previous|cancel|save (?:as )?draft|save for later|sign ?in|log ?in|upload|attach|"
    r"browse|\badd\b|remove|delete|clear|reset|check (?:your )?status|search|\bedit\b|close|"
    r"autofill|import|choose file|select file"
)


def button_intent(text: str, *, submits_form: bool) -> ButtonIntent:
    """Intent of one visible button from its own text. An unlabelled or unfamiliar
    button that would submit the form is AMBIGUOUS, never assumed to be "next"."""
    submit = bool(_SUBMIT.search(text))
    forward = bool(_NEXT.search(text))
    if submit and forward:
        return ButtonIntent.AMBIGUOUS
    if submit:
        return ButtonIntent.SUBMIT
    if forward:
        return ButtonIntent.NEXT
    if _OTHER.search(text):
        return ButtonIntent.OTHER
    return ButtonIntent.AMBIGUOUS if submits_form else ButtonIntent.OTHER


def _record_like(text: str) -> bool:
    return bool(APPLICATION_STATUS.search(text) or ACCEPTANCE.search(text) or job_ids(text))


def application_records(snapshot: Any) -> list[str] | None:
    """The texts of the outermost application records on the page, or None when the
    no repeated group has two or more status/identity-bearing members. None does
    not prove a single record: flat pages need separate local scope evidence."""
    members = snapshot.record_members
    counts: dict[int, int] = {}
    for member in members:
        if _record_like(member.text):
            counts[member.group] = counts.get(member.group, 0) + 1
    qualifying = {group for group, n in counts.items() if n >= 2}
    if not qualifying:
        return None
    records: list[str] = []
    for member in members:
        if member.group not in qualifying:
            continue
        if any(members[a].group in qualifying for a in member.ancestors):
            continue  # judged as part of its enclosing record
        records.append(member.text)
    return records
