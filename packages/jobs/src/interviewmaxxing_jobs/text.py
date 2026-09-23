"""Deterministic parsing of text exactly as job sources display it.

Nothing here guesses. Pay gets numeric bounds only when the text states an amount,
a currency and a period; work arrangement is set only from an explicit label.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlsplit

from interviewmaxxing_core import (
    Compensation,
    CompensationPeriod,
    WorkArrangement,
    employer_job_key,
)


def clean(value: str | None) -> str | None:
    """Collapse whitespace and unescape entities; empty becomes None."""
    if value is None:
        return None
    text = " ".join(html.unescape(value).split())
    return text or None


# --- compensation ---------------------------------------------------------------------

_PERIODS: tuple[tuple[re.Pattern[str], CompensationPeriod], ...] = (
    (re.compile(r"/\s*(?:yr|year)\b|\b(?:a|per)\s+year\b|\bannual(?:ly)?\b|\byearly\b", re.I),
     CompensationPeriod.YEAR),
    (re.compile(r"/\s*(?:mo|month)\b|\b(?:a|per)\s+month\b|\bmonthly\b", re.I),
     CompensationPeriod.MONTH),
    (re.compile(r"/\s*(?:wk|week)\b|\b(?:a|per)\s+week\b|\bweekly\b", re.I),
     CompensationPeriod.WEEK),
    (re.compile(r"/\s*day\b|\b(?:a|per)\s+day\b|\bdaily\b", re.I), CompensationPeriod.DAY),
    (re.compile(r"/\s*(?:hr|hour)\b|\b(?:an|a|per)\s+hour\b|\bhourly\b", re.I),
     CompensationPeriod.HOUR),
)
_AMOUNT = re.compile(
    r"(?P<cur>\$|USD\s*)?\s*(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<mult>[kKmM])?\b"
)
_ESTIMATE = re.compile(r"\b(?:estimated|estimate|est\.)\b", re.I)


def pay_segment(text: str | None) -> str | None:
    """The part of a card line that states pay (an amount with a period), if any."""
    value = clean(text)
    if not value:
        return None
    for segment in re.split(r"\s+[·•]\s+", value):
        if _AMOUNT.search(segment) and any(p.search(segment) for p, _ in _PERIODS):
            return segment
    return None


def parse_compensation(raw: str | None, *, dollar_currency: str | None = None) -> Compensation | None:
    """Pay as stated.

    ``dollar_currency`` is the currency a bare ``$`` denotes on this source (e.g.
    ``"USD"`` on a US site); without it, ``$`` is not an explicit currency. Text that
    is an estimate (not stated by the employer), has no period, or no explicit
    currency keeps only ``raw_text``.
    """
    text = clean(raw)
    if not text:
        return None
    # Cards join facts with " · " ("$110K/yr - $130K/yr · 3 benefits"); keep the pay part.
    segments = [s for s in re.split(r"\s+[·•]\s+", text) if s]
    pay_like = [s for s in segments if _AMOUNT.search(s) and any(p.search(s) for p, _ in _PERIODS)]
    if len(segments) > 1 and pay_like:
        text = pay_like[0]
    period = next((p for pattern, p in _PERIODS if pattern.search(text)), None)
    matches = list(_AMOUNT.finditer(text))
    amounts: list[float] = []
    currencies: set[str] = set()
    for m in matches:
        value = float(m.group("num").replace(",", ""))
        mult = (m.group("mult") or "").lower()
        value *= 1000 if mult == "k" else 1_000_000 if mult == "m" else 1
        amounts.append(value)
        cur = (m.group("cur") or "").strip().upper()
        if cur == "$":
            if dollar_currency:
                currencies.add(dollar_currency)
            else:
                currencies.add("?")
        elif cur == "USD":
            currencies.add("USD")
        else:
            currencies.add("")
    explicit = {c for c in currencies if c not in ("", "?")}
    # Every amount must carry the same explicit currency ("$110K - 130K" is still
    # explicit because the first amount states it and the rest have none).
    if (
        not amounts or period is None or _ESTIMATE.search(text) or len(explicit) != 1
        or "?" in currencies or len(amounts) > 2
    ):
        return Compensation(raw_text=text)
    currency = explicit.pop()
    lowered = text.lower()
    if len(amounts) == 2:
        low, high = amounts
    elif re.search(r"\b(?:up to|to)\b", lowered):
        low, high = None, amounts[0]
    elif re.search(r"\b(?:from|starting at|at least|min(?:imum)?)\b", lowered) or "+" in text:
        low, high = amounts[0], None
    else:
        low = high = amounts[0]
    if low is not None and high is not None and low > high:
        return Compensation(raw_text=text)
    return Compensation(raw_text=text, minimum=low, maximum=high, currency=currency, period=period)


_SCHEMA_UNITS = {
    "YEAR": CompensationPeriod.YEAR, "MONTH": CompensationPeriod.MONTH,
    "WEEK": CompensationPeriod.WEEK, "DAY": CompensationPeriod.DAY,
    "HOUR": CompensationPeriod.HOUR,
}


def compensation_from_schema(base_salary: object, raw_text: str | None = None) -> Compensation | None:
    """schema.org ``baseSalary`` (MonetaryAmount) with explicit currency and unit."""
    if not isinstance(base_salary, dict):
        return None
    currency = base_salary.get("currency")
    value = base_salary.get("value")
    if not isinstance(value, dict) or not isinstance(currency, str):
        return None
    period = _SCHEMA_UNITS.get(str(value.get("unitText", "")).upper())
    low = _number(value.get("minValue"))
    high = _number(value.get("maxValue"))
    single = _number(value.get("value"))
    if low is None and high is None and single is not None:
        low = high = single
    if period is None or (low is None and high is None):
        return None
    if low is not None and high is not None and low > high:
        return None
    text = clean(raw_text) or _schema_text(low, high, currency, period)
    return Compensation(raw_text=text, minimum=low, maximum=high, currency=currency, period=period)


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ""))
        except ValueError:
            return None
    return None


def _schema_text(low: float | None, high: float | None, currency: str, period: CompensationPeriod) -> str:
    parts = [f"{v:,.0f}" for v in (low, high) if v is not None]
    return f"{' - '.join(dict.fromkeys(parts))} {currency} per {period.value.lower()} (JobPosting baseSalary)"


# --- work arrangement ------------------------------------------------------------------

_ARRANGEMENT_WORDS: tuple[tuple[re.Pattern[str], WorkArrangement], ...] = (
    (re.compile(r"\b(?:fully\s+)?remote\b|\bwork from home\b|\btelecommute\b", re.I),
     WorkArrangement.REMOTE),
    (re.compile(r"\bhybrid\b", re.I), WorkArrangement.HYBRID),
    (re.compile(r"\bon-?\s?site\b|\bin-?\s?office\b|\bin person\b", re.I), WorkArrangement.ONSITE),
)


def parse_arrangement(label: str | None) -> WorkArrangement:
    """An explicit arrangement label. Mixed labels ("In-Office or Remote") are UNKNOWN."""
    text = clean(label)
    if not text:
        return WorkArrangement.UNKNOWN
    found = {kind for pattern, kind in _ARRANGEMENT_WORDS if pattern.search(text)}
    return found.pop() if len(found) == 1 else WorkArrangement.UNKNOWN


_CAPTION = re.compile(r"^(?P<loc>.*?)\s*\((?P<arr>On-site|Hybrid|Remote)\)\s*$", re.I)


def split_linkedin_caption(caption: str | None) -> tuple[str | None, WorkArrangement]:
    """``"Austin, TX (Hybrid)"`` -> ``("Austin, TX", HYBRID)``."""
    text = clean(caption)
    if not text:
        return None, WorkArrangement.UNKNOWN
    m = _CAPTION.match(text)
    if not m:
        return text, WorkArrangement.UNKNOWN
    return clean(m.group("loc")), parse_arrangement(m.group("arr"))


_INDEED_LOC = re.compile(r"^(?P<arr>Remote|Hybrid work|Hybrid)(?:\s+in\s+(?P<loc>.+))?$", re.I)


def split_indeed_location(text: str | None) -> tuple[str | None, WorkArrangement]:
    """``"Remote in Austin, TX"`` -> ``("Austin, TX", REMOTE)``; ``"Remote"`` -> ``(None, REMOTE)``."""
    value = clean(text)
    if not value:
        return None, WorkArrangement.UNKNOWN
    m = _INDEED_LOC.match(value)
    if not m:
        return value, WorkArrangement.UNKNOWN
    return clean(m.group("loc")), parse_arrangement(m.group("arr"))


_COUNTRY_NAMES = {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."}


def country_level_region(location: str | None) -> str | None:
    """The location text when it names only the United States, else None."""
    text = clean(location)
    if text and text.lower().rstrip(".") in {c.rstrip(".") for c in _COUNTRY_NAMES}:
        return text
    return None


# --- URLs and source ids ----------------------------------------------------------------

_LINKEDIN_VIEW = re.compile(r"/jobs/view/(?:[^/]*-)?(?P<id>\d{6,})/?")
_BUILTIN_JOB = re.compile(r"^/job/[^/]+/(?P<id>\d+)/?$")
_INDEED_JK = re.compile(r"^[0-9a-f]{16}$")


def linkedin_job_url(job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def indeed_job_url(jk: str) -> str:
    return f"https://www.indeed.com/viewjob?jk={jk}"


def decode_linkedin_redirect(url: str | None) -> str | None:
    """LinkedIn wraps external links in ``/safety/go/?url=<target>``; return the target."""
    if not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.endswith("linkedin.com") and parts.path.rstrip("/") in ("/safety/go", "/redir/redirect"):
        target = parse_qs(parts.query).get("url", [None])[0]
        return unquote(target) if target else None
    return url


def known_source_ref(url: str | None) -> tuple[str, str, str] | None:
    """``(source, source_listing_id, canonical_url)`` when ``url`` is a posting on
    LinkedIn, Indeed or Built In that carries the source's own job id."""
    if not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        m = _LINKEDIN_VIEW.search(parts.path)
        if m:
            return "linkedin", m.group("id"), linkedin_job_url(m.group("id"))
        job = parse_qs(parts.query).get("currentJobId", [None])[0]
        if job and job.isdigit():
            return "linkedin", job, linkedin_job_url(job)
    if host == "indeed.com" or host.endswith(".indeed.com"):
        jk = parse_qs(parts.query).get("jk", [None])[0] or parse_qs(parts.query).get("vjk", [None])[0]
        if jk and _INDEED_JK.fullmatch(jk) and parts.path.rstrip("/") in ("/viewjob", "/rc/clk", "/jobs", "/m/viewjob"):
            return "indeed", jk, indeed_job_url(jk)
    if host == "builtin.com" or host.endswith(".builtin.com"):
        m = _BUILTIN_JOB.match(parts.path)
        if m:
            return "builtin", m.group("id"), f"https://builtin.com{parts.path.rstrip('/')}"
    return None


_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ATS_PATHS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("greenhouse", "boards.greenhouse.io", re.compile(r"^/(?P<tenant>[\w-]+)/jobs/(?P<id>\d+)/?$")),
    ("greenhouse", "job-boards.greenhouse.io", re.compile(r"^/(?P<tenant>[\w-]+)/jobs/(?P<id>\d+)/?$")),
    ("lever", "jobs.lever.co", re.compile(rf"^/(?P<tenant>[\w.-]+)/(?P<id>{_UUID})(?:/apply)?/?$", re.I)),
    ("ashby", "jobs.ashbyhq.com", re.compile(rf"^/(?P<tenant>[^/]+)/(?P<id>{_UUID})(?:/application)?/?$", re.I)),
    ("smartrecruiters", "jobs.smartrecruiters.com", re.compile(r"^/(?P<tenant>[\w-]+)/(?P<id>\d{6,})(?:-[^/]*)?/?$")),
)
_WORKDAY = re.compile(r"^(?P<tenant>[\w-]+)\.wd\d+\.myworkdayjobs\.com$")
_WORKDAY_JOB = re.compile(r"/job/(?:[^/]+/)*[^/]*_(?P<id>[A-Za-z0-9-]+)/?(?:apply(?:/[^/]*)?)?/?$")


def employer_key_from_url(url: str | None) -> str | None:
    """The D0 ``employer_job_key`` when ``url`` is a job-specific ATS posting whose
    path carries the job's own id (Greenhouse, Lever, Ashby, SmartRecruiters,
    Workday). Generic apply endpoints, careers pages and search pages give None."""
    if not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == "boards.greenhouse.io" and parts.path.rstrip("/") == "/embed/job_app":
        query = parse_qs(parts.query)
        tenant, token = query.get("for", [None])[0], query.get("token", [None])[0]
        if tenant and token and token.isdigit():
            return employer_job_key("greenhouse", tenant, token)
        return None
    for ats, ats_host, pattern in _ATS_PATHS:
        if host == ats_host:
            m = pattern.match(parts.path)
            return employer_job_key(ats, m.group("tenant"), m.group("id")) if m else None
    wd = _WORKDAY.match(host)
    if wd:
        m = _WORKDAY_JOB.search(parts.path)
        if m:
            return employer_job_key("workday", wd.group("tenant"), m.group("id"))
    return None


def is_host(url: str | None, *domains: str) -> bool:
    host = (urlsplit(url or "").hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


# --- HTML to text -----------------------------------------------------------------------

_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr",
               "section", "article", "table"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS and tag != "li":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str | None) -> str | None:
    """Readable text from a JobPosting ``description`` (HTML)."""
    if not markup or not markup.strip():
        return None
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    text = "\n".join(out).strip()
    return text or None
