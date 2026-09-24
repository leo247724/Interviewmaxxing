"""Modal wizards, embedded application frames and resume pickers: pure rules.

Shared by normalization and the runtime; nothing here touches a page.

* **Progress.** A wizard that shows only a percentage bar (LinkedIn's Easy Apply shows
  0%, 33%, 67%, 100%) identifies each step by that percentage, so a step keeps the
  same identity on every run and a Next click that did not move the bar did not
  advance. "Step N of M" wording is used first when a page shows it.
* **Embedded application frames.** A careers page may show no form but embed the ATS
  application page in an iframe (Greenhouse's ``iframe#grnhse_iframe`` pointing at
  ``job-boards.greenhouse.io/embed/job_app?...``, iCIMS' ``in_iframe=1`` pages). Only
  an application page on a known ATS host (or the ATS's own embed frame id on the
  page's own origin) is ever followed, never an arbitrary frame.
* **Resume pickers.** Sites that keep uploaded resumes offer them as selectable cards
  (radio inputs labelled like "Select resume Avery_Quill_Resume.pdf"). The card named
  like the pinned resume file is chosen, or the only card when there is just one;
  anything else is left to the person.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from .snapshot import DomFrame, DomProgress

# --- progress ------------------------------------------------------------------------


def progress_step(bars: Sequence[DomProgress]) -> int | None:
    """The step identity one determinate progress bar gives: its percentage (0 to 100).
    None without a bar, or when several bars disagree."""
    percents = {max(0, min(100, round(100 * bar.value / bar.max))) for bar in bars if bar.max > 0}
    return percents.pop() if len(percents) == 1 else None


# --- embedded application frames -----------------------------------------------------

_FRAME_RULES: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...] = (
    ("greenhouse", re.compile(r"(^|\.)greenhouse\.io$"),
     re.compile(r"^/embed/job_app/?$|^/[^/]+/jobs/\d+")),
    ("lever", re.compile(r"(^|\.)lever\.co$"),
     re.compile(r"^/[^/]+/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:/apply)?/?$")),
    ("workday", re.compile(r"(^|\.)(?:myworkdayjobs|myworkdaysite)\.com$"), re.compile(r"/job/|/apply")),
    ("icims", re.compile(r"(^|\.)icims\.com$"), re.compile(r"^/jobs/\d+")),
    ("jobvite", re.compile(r"(^|\.)jobvite\.com$"), re.compile(r"/job/|/apply")),
)
"""(ATS, host, path) of application pages that careers sites embed in an iframe."""

_EMBED_FRAME_IDS: dict[str, re.Pattern[str]] = {
    "grnhse_iframe": re.compile(r"^/embed/job_app/?$"),
    "icims_content_iframe": re.compile(r"^/jobs/\d+"),
}
"""Frame ids the ATS embed scripts themselves set, with their application paths. They
count on the page's own origin too (a careers site serving the board itself)."""


@dataclass(frozen=True)
class ApplicationFrame:
    src: str
    ats: str
    visible: bool


def _frame_ats(frame: DomFrame, page_url: str) -> str | None:
    parts = urlsplit(frame.src)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    for name, host_rule, path_rule in _FRAME_RULES:
        if host_rule.search(host) and path_rule.search(parts.path):
            return name
    rule = _EMBED_FRAME_IDS.get(frame.id)
    page = urlsplit(page_url)
    if rule is not None and rule.search(parts.path) and (parts.scheme, parts.netloc) == (page.scheme, page.netloc):
        return "embedded"
    return None


def application_frame(frames: Sequence[DomFrame], page_url: str) -> ApplicationFrame | None:
    """The one embedded application page on this page, visible or not (a careers page
    may keep it in a hidden "Application" tab), or None when there is none or several."""
    found: dict[str, ApplicationFrame] = {}
    for frame in frames:
        ats = _frame_ats(frame, page_url)
        if ats is not None:
            known = found.get(frame.src)
            found[frame.src] = ApplicationFrame(frame.src, ats, frame.visible or bool(known and known.visible))
    return next(iter(found.values())) if len(found) == 1 else None


# --- resume pickers --------------------------------------------------------------------

_DOCUMENT = re.compile(
    r"([^\s/\\:*?\"<>|][^/\\:*?\"<>|]*?\.(?:pdf|docx?|rtf|txt|odt|pages))(?=$|[\s,;:·|)\]])",
    re.IGNORECASE,
)
_CHOICE_VERB = re.compile(
    r"^\s*(?:(?:de)?select(?:ed)?|choose|chosen|use|current(?:ly)?)\b\s*(?:this\s+)?"
    r"(?:(?:resume|cv|document|file)\b(?![.\w-]))?\s*[:\-]?\s*",
    re.IGNORECASE,
)


def document_name(text: str) -> str | None:
    """The file name a resume card or its label shows ("Select resume Avery_Quill.pdf",
    "Avery Quill CV.docx 290 KB · Last used on 8/12/2026"), or None."""
    match = _DOCUMENT.search(_CHOICE_VERB.sub("", " ".join(text.split())))
    return match.group(1).strip() if match else None


@dataclass(frozen=True)
class ResumeChoice:
    """One previously uploaded resume the site offers as a choice."""

    name: str
    selector: str
    """The choice's own input (a radio), checked exactly when it is selected."""
    label_selector: str | None = None
    checked: bool = False


def _same_name(shown: str, pinned: str) -> bool:
    a, b = shown.strip().casefold(), pinned.strip().casefold()
    if a == b:
        return True
    stem = re.compile(r"\.[a-z0-9]{2,5}$")
    # A site may drop the extension; the name itself must still be the same.
    return bool(stem.search(a)) != bool(stem.search(b)) and stem.sub("", a) == stem.sub("", b)


def choose_resume(choices: Sequence[ResumeChoice], pinned: str) -> tuple[ResumeChoice | None, str]:
    """The card to use for the pinned resume file and why: the one named like it, else
    the only card. (None, reason) when no card or several cards and none named like it."""
    named = [c for c in choices if _same_name(c.name, pinned)]
    if len(named) == 1:
        return named[0], f"the resume {named[0].name!r} already on the site is the pinned file"
    if len(named) > 1:
        return None, f"several resumes on the site are named {pinned!r}"
    if len(choices) == 1:
        return choices[0], f"the only resume on the site, {choices[0].name!r}"
    if not choices:
        return None, "the site shows no previously uploaded resume"
    return None, (f"none of the {len(choices)} resumes on the site "
                  f"({', '.join(repr(c.name) for c in choices)}) is the pinned {pinned!r}")


def named_choice(choices: Sequence[ResumeChoice], pinned: str) -> ResumeChoice | None:
    """The one card named like the pinned file, if any."""
    named = [c for c in choices if _same_name(c.name, pinned)]
    return named[0] if len(named) == 1 else None


# --- wording ---------------------------------------------------------------------------

_SIGN_IN = re.compile(
    r"\bsign ?in\b|\blog ?in\b|create (?:an? )?account|\bregister\b|\bforgot (?:your )?password\b",
    re.IGNORECASE,
)
_APPLICATION = re.compile(r"\bappl(?:y|ying|ication)\b|\bresume\b|\bcv\b|\bcandidate\b", re.IGNORECASE)


def sign_in_wording(text: str) -> bool:
    """A dialog about signing in or creating an account, never an application."""
    return bool(_SIGN_IN.search(text))


def application_wording(text: str) -> bool:
    return bool(_APPLICATION.search(text))
