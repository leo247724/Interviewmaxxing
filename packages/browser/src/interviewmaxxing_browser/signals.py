"""Pure text rules: page wording, confirmation references, job ids, button intent.

These only *recognize* wording; decisions about acceptance live in
:mod:`interviewmaxxing_browser.runtime`, which also requires the wording to be tied
to the application being submitted.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Sequence
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
    r"\bno longer (?:accepting applications|available|active|open)\b|"
    r"\b(?:job|position|posting|role|opening|requisition)(?: posting)? (?:is|has been|was) "
    r"(?:closed|filled|removed|no longer active|not currently active)\b|"
    r"\bthis job is closed\b|"
    r"\b(?:job|position|posting|role|opening|requisition)(?: posting)? has expired\b|"
    r"\bdoes not exist or is not currently active\b|\bis not currently active\b|"
    r"\b(?:job|position|posting|role|opening|requisition)(?: posting)? "
    r"(?:not found|does not exist|doesn't exist|(?:could|can)not be found|can't be found)\b|"
    r"\b(?:job|position|posting|role|opening|requisition) you (?:requested|were looking for|are looking for) "
    r"(?:was not|wasn't|is not|isn't|could not be|cannot be|can't be) found\b"
)
"""Wording that says the job itself is gone (closed, filled, expired, inactive, not
found). Tied to job words where the verb alone is ambiguous: a session, not a job, "has
expired"; a page, not a job, is "not found"."""
PENDING = _rx(r"still processing|being processed|cannot confirm|can't confirm|pending review")
ERROR_HEADING = _rx(
    r"something went wrong|error|not found|unavailable|try again later|bad gateway|timed? ?out"
)
DATA_CONSENT_GATE = _rx(
    r"^\s*(?:data(?: privacy| protection| processing)? consent|"
    r"(?:candidate|applicant) (?:data|privacy) consent|"
    r"consent to (?:the )?(?:processing|use|collection) of (?:your )?(?:personal )?(?:data|information))\s*$"
)
"""The heading of a data-processing consent page in front of an application (Jobvite's
"Data Consent"): the person chooses a policy (a location of residence and language) and
accepts it before the site shows the form."""
CONSENT_GATE_ACTION = "accept its data-processing consent"
"""Wording in the message of a consent gate's inspection (``SIGN_IN_REQUIRED``), by which
``user_action_needs`` names the user's action."""
CAPTCHA_TEXT = _rx(
    r"captcha|verify (?:that )?you(?:'re| are) (?:a )?human|are you a robot|not a robot|"
    r"characters (?:shown|in the image)|security check|checking your browser"
)
APPLY_LINK = _rx(
    r"^(?!.*\bsubmit\b)(?!.*\bapply (?:with|using|via)\b)(?!.*\buse my\b)\s*(?:"
    r"(?:easy |quick )?apply(?: now| here| online| today| manually)?"
    r"|apply (?:for|to) (?:this |the )?(?:job|role|position|opening)"
    r"|apply (?:without (?:an? )?account|as (?:an? )?guest)"
    r"|continue as (?:an? )?guest"
    r"|(?:start|begin) (?:your |my )?application"
    r"|continue to (?:the |your )?application"
    r"|i'?m interested)\s*[\u00bb\u203a>\u2192]?\s*$"
)
"""Text of a control that leads to the application form (a posting's apply link or
button, LinkedIn's "Easy Apply", a guest-apply choice). Third-party flows ("Apply with
LinkedIn", "Use my Indeed resume") and anything mentioning "submit" are excluded."""

APPLY_ENTRY = _rx(
    r"^\s*(?:(?:easy|quick) apply|i'?m interested|apply (?:without (?:an? )?account|as (?:an? )?guest)"
    r"|continue as (?:an? )?guest|(?:start|begin) (?:your |my )?application"
    r"|continue to (?:the |your )?application)\b"
)
"""Apply wording that only ever opens an application ("Easy Apply", "I'm interested",
"Start your application"), never submits one, whatever form it sits in."""
MANUAL_APPLY = _rx(r"^\s*(?:apply manually|manual(?:ly)? appl(?:y|ication))\s*[\u00bb\u203a>\u2192]?\s*$")
"""The manual route of an apply-options chooser (Workday's "Start Your Application":
"Autofill with Resume", "Apply Manually", "Use My Last Application"). It is preferred
over every other apply control; autofill uploads and parses the resume before any
question is shown, and "Use My Last Application" copies another application."""
ACCOUNT_CREATION = _rx(
    r"\bcreate (?:an |a |a new |your |my )?(?:candidate )?account\b|\bsign ?up\b|\bregister\b"
)
"""Wording of a sign-in page that also offers a new account ("Create Account")."""
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
    r"autofill|import|choose file|select file|\bsave\b|\bdismiss\b|\bdiscard\b|\bskip\b|download|"
    r"\bpaste\b|\bshow(?: \d+)? (?:more|less)\b|\bsee(?: \d+)? (?:more|less)\b"
)


THIRD_PARTY_ASSIST = _rx(
    r"auto-?fill|\bapply (?:with|using|via)\b|\bimport (?:from|my|your)\b|"
    r"\buse my (?:linkedin|indeed|resume|r\u00e9sum\u00e9|cv|profile)\b|"
    r"\b(?:connect|continue|sign in|log ?in) with (?:linkedin|indeed|seek|xing)\b|"
    r"\blinked ?in\b|\bindeed\b|\bmygreenhouse\b|\bparse (?:my |your )?(?:resume|cv)\b"
)
"""A helper that fills the application from somewhere else ("Apply with LinkedIn",
"Autofill my application", "Import from Indeed"). Such controls are never clicked,
are never a step's submit or next action, and are not part of a form's structure."""
LOADING_STATE = _rx(r"^\W*(?:loading|please wait|one moment)\b")
"""A control that shows only that it is still loading ("Loading..."); what it will
become is not known yet (Lever's "Apply with LinkedIn" reads "Loading..." first)."""


def button_intent(text: str, *, submits_form: bool) -> ButtonIntent:
    """Intent of one visible button from its own text. An unlabelled or unfamiliar
    button that would submit the form is AMBIGUOUS, never assumed to be "next".
    Third-party autofill and "Apply with ..." helpers are OTHER."""
    if THIRD_PARTY_ASSIST.search(text):
        return ButtonIntent.OTHER
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


# --- autofill prompts ---------------------------------------------------------------------

AUTOFILL_OFFER = _rx(
    r"auto-?fill|pre-?fill|\bimport (?:your |my )?(?:details|profile|information|resume|cv|data)\b|"
    r"\b(?:from|with) (?:your )?(?:linkedin|indeed|resume|r\u00e9sum\u00e9|cv)\b|"
    r"\bparse (?:your |my )?(?:resume|cv)\b|\bfill (?:in|out) (?:this|the|your) (?:form|application)\b"
)
"""A dialog offering to fill the application from a profile or a parsed resume."""
DECLINE_OFFER = _rx(
    r"^(?:no|no,? thanks?|no,? thank you|not now|not yet|maybe later|later|skip(?: for now)?|"
    r"dismiss|close(?: dialog| this dialog)?|cancel|x|\u00d7|\u2715|\u2716|"
    r"continue without(?: autofill(?:ing)?| importing)?|"
    r"(?:fill|enter|complete)(?: it| in| out| the form| my application)* manually|apply manually|"
    r"i'?ll fill (?:it )?(?:in|out) myself)[.!]?$"
)
"""The control that declines such an offer ("No thanks", "Not now", "Close", "\u00d7")."""


def autofill_decline(text: str, buttons: Sequence[tuple[str, str]]) -> str | None:
    """The selector of the control that declines an offer to autofill the application,
    or None. ``buttons`` are the dialog's (text, selector) pairs in document order. The
    dialog must offer autofill; the first decline control is chosen, and a control that
    names the offer itself ("Autofill", "Import from LinkedIn") never is."""
    if not AUTOFILL_OFFER.search(text):
        return None
    return next((selector for label, selector in buttons
                 if DECLINE_OFFER.match(label.strip()) and not THIRD_PARTY_ASSIST.search(label)),
                None)


# --- dates and phone numbers -------------------------------------------------------------

_MONTHS = {name: i for i, names in enumerate(
    (("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",),
     ("june", "jun"), ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"),
     ("october", "oct"), ("november", "nov"), ("december", "dec")), start=1) for name in names}


def date_segment_values(text: str, kinds: Sequence[str]) -> list[str] | None:
    """What to type into each segment of a segmented date (``kinds`` in page order:
    "month", "day", "year") for an answer written as an ISO date ("2026-09-24",
    "2026-09"), with the year first ("2026/09/24"), in the widget's own order ("09/24/2026"
    or "9/24/2026" for Month/Day/Year) or with a month name ("September 24, 2026").
    Two-digit months and days, four-digit years. None when it is not such a date."""
    words = re.findall(r"[a-z]+|\d+", text.casefold())
    if not words:
        return None
    named = [w for w in words if not w.isdigit()]
    numbers = [w for w in words if w.isdigit()]
    month: int | None = None
    day: int | None = None
    year: int | None = None
    if named:
        if len(named) != 1 or named[0] not in _MONTHS:
            return None
        month = _MONTHS[named[0]]
        years = [n for n in numbers if len(n) == 4]
        others = [n for n in numbers if len(n) != 4]
        if len(years) != 1 or len(others) > 1:
            return None
        year = int(years[0])
        day = int(others[0]) if others else None
    elif len(numbers) >= 2 and len(numbers[0]) == 4:
        if len(numbers) > 3:
            return None
        year, month = int(numbers[0]), int(numbers[1])
        day = int(numbers[2]) if len(numbers) == 3 else None
    else:
        wanted = [k for k in kinds if k in ("month", "day", "year")]
        if len(numbers) != len(wanted) or "year" not in wanted:
            return None
        if len(numbers[wanted.index("year")]) != 4:
            return None
        values = dict(zip(wanted, (int(n) for n in numbers), strict=True))
        month, day, year = values.get("month"), values.get("day"), values.get("year")
    if month is None or year is None or not 1 <= month <= 12 or not 1000 <= year <= 9999:
        return None
    if "day" in kinds:
        if day is None:
            return None
        try:
            datetime.date(year, month, day)
        except ValueError:
            return None
    elif day is not None:
        return None
    parts = {"month": f"{month:02d}", "day": f"{day or 0:02d}", "year": f"{year:04d}"}
    return [parts[k] for k in kinds]


def national_number(phone: str, dial_code: str | None) -> str | None:
    """``phone`` without its leading ``+<dial_code>`` when the country code is chosen
    separately ("+1 (303) 555-0142" with "1" -> "(303) 555-0142"), or None when it does
    not start with that code."""
    if not dial_code:
        return None
    match = re.match(rf"^\s*\+\s*{re.escape(dial_code)}[\s.\-]*", phone)
    if match is None:
        return None
    rest = phone[match.end():].strip()
    return rest or None


# --- lookup suggestions ------------------------------------------------------------------

US_STATES: dict[str, str] = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah", "vt": "vermont",
    "va": "virginia", "wa": "washington", "wv": "west virginia", "wi": "wisconsin",
    "wy": "wyoming", "dc": "district of columbia",
}
_UNITED_STATES = frozenset({"us", "usa", "u s", "u s a", "united states", "united states of america"})


_DIAL_SUFFIX = re.compile(r"\s*(?:\(\s*\+\s*\d{1,4}\s*\)|\+\s*\d{1,4})\s*$")
"""A trailing dial code ("United States of America (+1)", Workday's Country Phone Code)."""


def _place_segments(text: str) -> list[str]:
    """Comma segments, case- and punctuation-folded, with US state abbreviations and
    United States synonyms spelled out ("Austin, TX, USA" -> austin|texas|united states)
    and a trailing dial code left out ("United States of America (+1)" -> united states)."""
    segments = []
    for raw in _DIAL_SUFFIX.sub("", text).split(","):
        segment = " ".join(re.sub(r"[^\w\s]", " ", raw).casefold().split())
        if not segment:
            continue
        if segment in _UNITED_STATES:
            segment = "united states"
        segments.append(US_STATES.get(segment, segment))
    return segments


def lookup_matches(typed: str, suggestions: Sequence[str]) -> list[int]:
    """Indexes of the site suggestions that say what was typed.

    A suggestion equal to the typed text always wins (that is how a chosen suggestion
    is committed later). Otherwise the typed place (the first comma segment) must equal
    a whole segment of the suggestion, so "Austin" never matches "Austintown", and every
    other typed word must be a word of the suggestion or begin one ("Tex" for Texas),
    after spelling out US state abbreviations and United States synonyms."""
    squashed = " ".join(typed.split())
    exact = [i for i, s in enumerate(suggestions) if " ".join(s.split()) == squashed]
    if exact:
        return exact
    wanted = _place_segments(typed)
    if not wanted:
        return []
    place, others = wanted[0], [w for segment in wanted[1:] for w in segment.split()]
    matches = []
    for i, suggestion in enumerate(suggestions):
        segments = _place_segments(suggestion)
        words = [w for segment in segments for w in segment.split()]
        if place in segments and all(
            any(w == o or (len(o) >= 2 and w.startswith(o)) for w in words) for o in others
        ):
            matches.append(i)
    return matches


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
