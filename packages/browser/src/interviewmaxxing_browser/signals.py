"""Pure text rules: page wording, confirmation references, job ids, button intent.

These only *recognize* wording; decisions about acceptance live in
:mod:`interviewmaxxing_browser.runtime`, which also requires the wording to be tied
to the application being submitted.
"""

from __future__ import annotations

import re
from enum import StrEnum


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


ACCEPTANCE = _rx(
    r"application (?:has been |was |is )?(?:successfully )?(?:submitted|received|complete)|"
    r"thank(?:s| you) for (?:applying|your application|submitting)|"
    r"we(?:'ve| have) received your application|"
    r"your application (?:has been|was|is) (?:submitted|received|complete)|"
    r"successfully applied|you(?:'ve| have) (?:successfully )?applied"
)
ALREADY_APPLIED = _rx(
    r"you(?:'ve| have)? already applied|already submitted an application|"
    r"you already have an application|application (?:for this (?:job|role|position) )?already exists"
)
JOB_CLOSED = _rx(
    r"no longer accepting applications|(?:job|position|posting|role) (?:is|has been) "
    r"(?:closed|filled|removed)|this job is closed|no longer available"
)
PENDING = _rx(r"still processing|being processed|cannot confirm|can't confirm|pending review")
ERROR_HEADING = _rx(
    r"something went wrong|error|not found|unavailable|try again later|bad gateway|timed? ?out"
)
CAPTCHA_TEXT = _rx(
    r"captcha|verify (?:that )?you(?:'re| are) (?:a )?human|are you a robot|not a robot|"
    r"characters (?:shown|in the image)|security check|checking your browser"
)
APPLY_LINK = _rx(r"^\s*(?:apply(?: now| for this (?:job|role|position))?|start (?:your )?application|i'?m interested)\s*$")
STATUS_LINK = _rx(
    r"application status|check (?:your )?status|already applied|my applications|candidate (?:home|portal)"
)

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
