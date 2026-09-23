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
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<mult>[kKmM])?\b"
)
# Estimates are not employer-stated pay: "Estimated", "Est.", "approx.", "~$120K".
_ESTIMATE = re.compile(r"(?<![A-Za-z])(?:estimated|estimate|est\.?|approx(?:imately|\.)?)(?![A-Za-z])|~\s*[$€£]?\d", re.I)
_CURRENCY_CODES = ("USD", "CAD", "EUR", "GBP", "AUD", "NZD", "CHF", "MXN", "INR", "JPY", "SGD",
                   "HKD", "BRL", "SEK", "NOK", "DKK", "PLN", "ZAR")
_CURRENCY_CODE = re.compile(r"(?<![A-Za-z])(" + "|".join(_CURRENCY_CODES) + r")(?![A-Za-z])", re.I)
_CURRENCY_SYMBOLS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<![A-Za-z])(?:C|CA)\$"), "CAD"),
    (re.compile(r"(?<![A-Za-z])(?:A|AU)\$"), "AUD"),
    (re.compile(r"(?<![A-Za-z])(?:NZ)\$"), "NZD"),
    (re.compile(r"(?<![A-Za-z])(?:US)\$"), "USD"),
    (re.compile(r"(?<![A-Za-z])(?:MX)\$"), "MXN"),
    (re.compile(r"€"), "EUR"),
    (re.compile(r"£"), "GBP"),
)
_BARE_DOLLAR = re.compile(r"(?<![A-Za-z])\$")


def explicit_currencies(text: str) -> set[str]:
    """ISO codes stated in ``text`` by code (``CAD``, ``USD``) or symbol (``C$``, ``€``,
    ``£``). A bare ``$`` is not explicit."""
    found = {m.group(1).upper() for m in _CURRENCY_CODE.finditer(text)}
    found |= {code for pattern, code in _CURRENCY_SYMBOLS if pattern.search(text)}
    return found


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

    Numeric bounds are set only when the pay text states one currency and a period
    and is not marked as an estimate. An explicit currency (``CAD $110,000``,
    ``€60,000``, ``110,000 USD``) always wins; ``dollar_currency`` is only what a bare
    ``$`` denotes on this source (``"USD"`` on a US site). Otherwise only ``raw_text``
    is kept.
    """
    text = clean(raw)
    if not text:
        return None
    # Cards join facts with " · " ("$110K/yr - $130K/yr · 3 benefits"); keep the pay part.
    segment = pay_segment(text)
    if segment is not None and len(re.split(r"\s+[·•]\s+", text)) > 1:
        text = segment
    period = next((p for pattern, p in _PERIODS if pattern.search(text)), None)
    explicit = explicit_currencies(text)
    if len(explicit) == 1:
        currency: str | None = explicit.pop()
    elif explicit:
        currency = None  # two currencies stated: not comparable
    elif _BARE_DOLLAR.search(text):
        currency = dollar_currency
    else:
        currency = None
    # Amounts, ignoring the currency tokens themselves.
    stripped = _CURRENCY_CODE.sub(" ", text)
    for pattern, _ in _CURRENCY_SYMBOLS:
        stripped = pattern.sub(" ", stripped)
    amounts: list[float] = []
    for m in _AMOUNT.finditer(stripped):
        value = float(m.group("num").replace(",", ""))
        mult = (m.group("mult") or "").lower()
        amounts.append(value * (1000 if mult == "k" else 1_000_000 if mult == "m" else 1))
    if not amounts or period is None or currency is None or _ESTIMATE.search(text) or len(amounts) > 2:
        return Compensation(raw_text=text)
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
US_STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


def country_level_region(location: str | None) -> str | None:
    """The location text when it names only the United States, else None."""
    text = clean(location)
    if text and text.lower().rstrip(".") in {c.rstrip(".") for c in _COUNTRY_NAMES}:
        return text
    return None


def stated_remote_region(location: str | None) -> str | None:
    """Preserve an explicitly named country/state on a remote card without
    expanding it to the search country. City-only locations remain unverified."""
    text = clean(location)
    if text is None:
        return None
    label = re.sub(r"(?:[- ]only)$", "", text, flags=re.I).strip().casefold().rstrip(".")
    regions = {name.casefold() for name in US_STATES.values()} | {
        state.casefold() for state in US_STATES
    } | {c.rstrip(".") for c in _COUNTRY_NAMES} | {"canada", "united kingdom", "uk"}
    return text if label in regions else None


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
