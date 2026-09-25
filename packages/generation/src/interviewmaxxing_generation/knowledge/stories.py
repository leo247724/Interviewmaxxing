"""Professional stories as retrieval evidence: docx parsing, analysis, chunks and facts.

Stories are the candidate's own account of their work, deeper than the resume. This
module reads the document with the standard library only, splits it into stories at its
headings, analyses each story deterministically (employer, role, period, situation,
actions, quantified outcomes, tools, skills and the themes it evidences), cuts
self-contained retrieval chunks with a machine-readable header line, and turns concrete
first-person sentences into atomic ``CandidateFact`` records the user authored. Nothing
here contacts a provider or a database, invents or rounds a number, or writes a file.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from interviewmaxxing_core import (
    CandidateFact,
    FactVerification,
    VerificationMethod,
    VerificationStatus,
)

MIN_CHUNK_WORDS = 120
MAX_CHUNK_WORDS = 300
MAX_CHUNK_CHARS = 1750
"""Below the store's 1800-character chunk bound, header included."""
MAX_FACT_CHARS = 600
MAX_DOCX_BYTES = 4_000_000
MAX_DOCUMENT_XML_BYTES = 16_000_000
MAX_STORIES = 64
DEFAULT_STORY_SOURCE = "candidate-stories"
"""The one current stories source of a candidate; a new document version replaces it."""
STORY_CHUNK_ID = re.compile(r"^story:[0-9a-f]{64}$")

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_HEADING = re.compile(r"^\s*(?:stor(?:y|ies))\s*#?\s*(\d{1,3})\s*[-\u2013\u2014:.]\s*(.+?)\s*$",
                      re.IGNORECASE)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=\S)|\n+|(?<=[a-z0-9)%]\.)(?=[A-Z])")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_DURATION = re.compile(
    r"\b(?:(?:over|almost|nearly|about|around|under|more than|less than|a little (?:less|more) than)\s+)?"
    r"\d+(?:\.\d+)?\+?\s+(?:years?|months?|weeks?)\b", re.IGNORECASE)
_ORG_NOUNS = (r"(?:law firm|consulting firm|firm|agency|agencies|company|startup|start-up|brand|"
              r"business|dealership|dealerships|organization|organisation|nonprofit|non-profit|"
              r"retailer|marketplace|practice|hospital|clinic|bank|school|university|studio|"
              r"publisher|network|manufacturer|fund|chain|chains|shop|shops|store|stores|"
              r"restaurant|restaurants|hotel|hotels)")
_EMPLOYER = re.compile(
    rf"\b(?:for|at|with|of|joined|joining)\s+(?:a|an|the|my|this)\s+"
    rf"((?:[a-z0-9&'/-]+\s+){{0,5}}?{_ORG_NOUNS})\b", re.IGNORECASE)
_EMPLOYER_STOPWORDS = frozenset({
    "to", "that", "which", "who", "and", "or", "of", "in", "is", "was", "were", "be", "has",
    "have", "had", "it", "its", "entire", "whole", "same", "other", "another", "new", "own"})
_BUDGET = re.compile(r"\bbudgets?\b|\bad\s+spend\b|\bspend\s+allocation\b|\bmedia\s+spend\b",
                     re.IGNORECASE)
_CERTIFICATION = re.compile(r"\bcertif(?:ied|ication|icate)s?\b", re.IGNORECASE)
_TITLE_WORDS = (r"(?:manager|director|lead|head|specialist|consultant|founder|co-founder|cofounder|"
                r"engineer|developer|analyst|strategist|coordinator|officer|owner|president|"
                r"vice president|vp|associate|intern|freelancer|contractor|media buyer|buyer|"
                r"marketer|designer|architect|partner|principal|executive|advisor|adviser)")
_ROLE_POSITION = re.compile(
    rf"\bmy\s+((?:[a-z0-9&/-]+\s+){{0,4}}?{_TITLE_WORDS})\s+(?:position|role|job|title)\b",
    re.IGNORECASE)
_ROLE = re.compile(
    rf"(?<!such )\b(?:as|i was|i worked as|i served as|hired as|promoted to|role as|title of|"
    rf"position as)\s+(?:the\s+|a\s+|an\s+)?((?:[a-z0-9&/-]+\s+){{0,4}}?{_TITLE_WORDS})\b",
    re.IGNORECASE)
_PROJECT = re.compile(
    r"\bI\s+(?:built|founded|launched|created|started|co-founded)\s+(?:a|an|my|the)\s+"
    r"[^,.;]{0,80}?,\s*([A-Z][\w.-]+),")
_SITUATION = re.compile(
    r"\b(?:challenge|broken|problem|bottleneck|issue|wasn'?t|weren'?t|not tracking|lacked|"
    r"difficult|found that|wasted|losing money|barely profitable|no |not enough)\b", re.IGNORECASE)
_ACTION = re.compile(
    r"\b(?:built|build|developed|managed|worked|shifted|leveraged|used|interviewed|implemented|"
    r"set ?up|debugged|identified|restructured|created|launched|ran|directed|owned|generated|"
    r"increased|reduced|lowered|improved|tested|wrote|designed|analy[sz]ed|negotiated|pitched|"
    r"convinced|approved|migrated|integrated|connected|automated|hired|trained|led|scaled|"
    r"optimi[sz]ed|forecast(?:ed)?|planned|presented|reported|took over|got .* approved)\b",
    re.IGNORECASE)
_RESULT = re.compile(
    r"%|\$|\bfigures?\b|\brevenue\b|\bleads?\b|\bconversion\b|\bctr\b|\bcpa\b|\bcost per\b|"
    r"\broas\b|\bquality score\b|\bfrom\s+\S+\s+to\s+\S+|\bincreas|\breduc|\blower|\bimprov|"
    r"\bsav(?:e|ed|es|ing)\b|\bgenerat|\bgrew\b|\bdoubl|\btripl|\brecord\b|\bhours?\b|\bdecreas",
    re.IGNORECASE)
_FIRST_PERSON = re.compile(r"\b(?:I|I've|I'd|I'm|my|me|myself|we|we've|our|us)\b", re.IGNORECASE)
_LEADING_VERB = re.compile(
    r"^(?:[A-Z][a-z]+ed|Led|Ran|Built|Grew|Drove|Oversaw|Won|Set|Took|Wrote|Spent|Cut|Made)\b")
_OWN_SUBJECT = re.compile(r"^(?:the|this|our)\s+(?:software|product|agent|system|tool)\b", re.IGNORECASE)
_DIGITS = re.compile(r"\d[\d,.]*")

# Generic industry tools. An ambiguous name (a common word) counts only next to other tools.
_TOOLS: tuple[tuple[str, str, bool], ...] = (
    ("Google Ads", r"\bgoogle\s?ads?\b", False),
    ("Google Ads API", r"\bgoogle\s?ads\s+api'?s?\b", False),
    ("Meta Ads", r"\bmeta\s?ads?\b", False),
    ("Meta Ads API", r"\bmeta\s?ads\s+api'?s?\b", False),
    ("Facebook Ads", r"\bfacebook\s?ads?\b", False),
    ("Instagram Ads", r"\binstagram\s?ads?\b", False),
    ("LinkedIn Ads", r"\blinked\s?in\s?ads?\b", False),
    ("LinkedIn", r"\blinked\s?in\b(?!\s?(?:ads|sales))", False),
    ("LinkedIn Sales Navigator", r"\bsales\s?navigator\b", False),
    ("TikTok Ads", r"\btik\s?tok\s?ads?\b", False),
    ("YouTube", r"\byou\s?tube\b", False),
    ("Performance Max", r"\bperformance\s?max\b|\bpmax\b", False),
    ("Demand Gen", r"\bdemand\s?gen\b", False),
    ("Google Analytics", r"\bgoogle\s?analytics\b|\bga4\b", False),
    ("Google Tag Manager", r"\bgoogle\s?tag\s?manager\b|\bgtm\s+container\b", False),
    ("Google Search Console", r"\bsearch\s?console\b", False),
    ("Looker Studio", r"\blooker\s?studio\b|\bdata\s?studio\b", False),
    ("Microsoft Ads", r"\b(?:microsoft|bing)\s?ads\b", False),
    ("Reddit Ads", r"\breddit\s?ads\b", False),
    ("HubSpot", r"\bhub\s?spot\b", False),
    ("Salesforce", r"\bsales\s?force\b", False),
    ("Marketo", r"\bmarketo\b", False),
    ("Pardot", r"\bpardot\b", False),
    ("Klaviyo", r"\bklaviyo\b", False),
    ("Mailchimp", r"\bmail\s?chimp\b", False),
    ("ActiveCampaign", r"\bactive\s?campaign\b", False),
    ("GoHighLevel", r"\bgo\s?high\s?level\b|\bghl\b", False),
    ("CallRail", r"\bcall\s?rail\b", False),
    ("RingCentral", r"\bring\s?central\b", False),
    ("Twilio", r"\btwilio\b", False),
    ("Slack", r"\bslack\b", False),
    ("Stripe", r"\bstripe\b", False),
    ("Shopify", r"\bshopify\b", False),
    ("WordPress", r"\bword\s?press\b", False),
    ("Webflow", r"\bwebflow\b", False),
    ("Unbounce", r"\bunbounce\b", False),
    ("Instapage", r"\binstapage\b", False),
    ("Clay", r"\bclay(?:\.io)?\b", True),
    ("6sense", r"\b6sense\b", False),
    ("Demandbase", r"\bdemandbase\b", False),
    ("ZoomInfo", r"\bzoom\s?info\b", False),
    ("Apollo", r"\bapollo(?:\.io)?\b", True),
    ("Instantly", r"\binstantly(?:\.ai)?\b", True),
    ("Calendly", r"\bcalendly\b", False),
    ("Google Calendar", r"\bgoogle\s?calendar\b", False),
    ("HeyGen", r"\bhey\s?gen\b", False),
    ("Higgsfield", r"\bhiggs\s?field\b", False),
    ("Runway", r"\brunway\s?ml\b|\brunway\b", True),
    ("Midjourney", r"\bmid\s?journey\b", False),
    ("ChatGPT", r"\bchat\s?gpt\b", False),
    ("OpenAI", r"\bopen\s?ai\b", False),
    ("Claude", r"\bclaude\b(?!\s+code)", False),
    ("Claude Code", r"\bclaude\s+code\b", False),
    ("Codex", r"\bcodex\b", False),
    ("Cursor", r"\bcursor\b", True),
    ("TypeScript", r"\btype\s?script\b", False),
    ("JavaScript", r"\bjava\s?script\b", False),
    ("Node.js", r"\bnode(?:\.js|js)\b", False),
    ("Python", r"\bpython\b", False),
    ("React", r"\breact\b", True),
    ("Next.js", r"\bnext\.?js\b", False),
    ("Postgres", r"\bpostgres(?:ql)?\b", False),
    ("Supabase", r"\bsupabase\b", False),
    ("Zapier", r"\bzapier\b", False),
    ("Make", r"\bmake\.com\b", False),
    ("n8n", r"\bn8n\b", False),
    ("Airtable", r"\bairtable\b", False),
    ("Notion", r"\bnotion\b", True),
    ("Figma", r"\bfigma\b", False),
    ("Canva", r"\bcanva\b", False),
    ("Mixpanel", r"\bmixpanel\b", False),
    ("Amplitude", r"\bamplitude\b", True),
    ("Hotjar", r"\bhotjar\b", False),
    ("Semrush", r"\bsemrush\b", False),
    ("Ahrefs", r"\bahrefs\b", False),
    ("Optimizely", r"\boptimizely\b", False),
)
_CHANNELS = frozenset({
    "Google Ads", "Meta Ads", "Facebook Ads", "Instagram Ads", "LinkedIn Ads", "TikTok Ads",
    "YouTube", "Performance Max", "Demand Gen", "Microsoft Ads", "Reddit Ads"})

# Generic techniques and skills; each maps a canonical label to the wording that evidences it.
_SKILLS: tuple[tuple[str, str], ...] = (
    ("offline conversion tracking", r"\boffline\s+(?:conversion|event)s?\b"),
    ("conversion tracking", r"\bconversion\s+tracking\b"),
    ("enhanced conversions", r"\benhanced\s+conversions?\b"),
    ("dynamic number insertion", r"\bdynamic\s+number\s+insertion\b"),
    ("call tracking", r"\bcall\s+tracking\b|\bphone\s+systems?\b"),
    ("click ID capture", r"\bgclid\b|\bclick\s+ids?\b"),
    ("retargeting", r"\bre-?targeting\b|\bremarketing\b"),
    ("lead generation", r"\blead[\s-]+gen(?:eration|erating)?\b|\bgenerat\w*\s+\S*\s*leads\b"),
    ("Quality Score optimization", r"\bquality\s+score\b"),
    ("ad copy", r"\bad\s+copy\b"),
    ("landing pages", r"\blanding\s+pages?\b"),
    ("A/B testing", r"\ba/?b\s+test(?:ing|s)?\b|\bsplit\s+test"),
    ("creative testing", r"\bcreative\s+testing\b|\btesting\s+\w+\s+creative\b"),
    ("buyer personas", r"\b(?:buyer\s+)?personas?\b"),
    ("ideal customer profiles", r"\bicps?\b|\bideal\s+customer\s+profiles?\b"),
    ("revenue forecasting", r"\bforecast(?:ing|s|ed)?\b"),
    ("budget allocation", r"\bbudget\s+allocation\b|\bspend\s+allocation\b|\bshift(?:ed|ing)?\s+budget\b"),
    ("location targeting", r"\b(?:location|geo|radius)[\s-]+targeting\b|\bgeo\s?locations?\b"),
    ("cold email", r"\bcold\s+email\b"),
    ("cold outreach", r"\bcold\s+outreach\b|\boutreach\s+campaigns?\b"),
    ("go-to-market strategy", r"\bgtm\b|\bgo-to-market\b"),
    ("paid search", r"\bpaid\s+search\b"),
    ("paid social", r"\bpaid\s+social\b|\bsocial\s+media\s+ads\b"),
    ("funnel design", r"\bfunnels?\b"),
    ("CRM integration", r"\bcrms?\b"),
    ("marketing automation", r"\bautomat(?:ion|ed|e|ing)\b"),
    ("AI agents", r"\bai\s+agents?\b|\bai[\s-]native\b|\bagentic\b"),
    ("large language models", r"\blarge\s+language\s+models?\b|\bllms?\b"),
    ("video creative", r"\bvideo\s+(?:creative|ads?|hooks?)\b|\bvideography\b"),
    ("UGC", r"\bugc\b"),
    ("copywriting", r"\bcopy\s?writ(?:er|ing)\b|\bwebsite\s+copy\b"),
    ("negative keywords", r"\bnegative\s+keywords?\b"),
    ("frequency caps", r"\bfrequency\s+caps?\b"),
    ("responsive search ads", r"\bresponsive\s+search\s+ads?\b"),
    ("ad group structure", r"\bad\s+groups?\b"),
    ("pipeline stages (MQL/SQL)", r"\bmqls?\b|\bsqls?\b|\bclosed[\s-]won\b"),
    ("ROAS analysis", r"\broas\b|\bproas\b|\breturn\s+on\s+ad\s+spend\b"),
    ("CPA optimization", r"\bcost\s+per\s+acquisition\b|\bcpa\b"),
    ("CTR optimization", r"\bctr\b|\bclick[\s-]through\b"),
    ("stakeholder management", r"\bstakeholders?\b"),
    ("team leadership", r"\breported\s+to\s+me\b|\bmy\s+(?:small\s+)?team\b|\bteam\s+of\s+\d+\b|\bdirect\s+reports?\b"),
    ("direct mail", r"\bphysical\s+mail\b|\bdirect\s+mail\b|\bgift\s+baskets?\b"),
    ("API integration", r"\bapi'?s?\b"),
    ("sales alignment", r"\bsales\s+team\b|\bsales\s+reps?\b"),
    ("media buying", r"\bmedia\s+buy(?:ing|er|ers)\b"),
    ("traditional media", r"\bradio\b|\btv\b|\btelevision\b"),
    ("data analysis", r"\bdata\s+analysis\b|\banaly[sz]e[sd]?\b"),
    ("account management", r"\baccount\s+management\b|\bclient\s+accounts?\b|\b\d+\+?\s+clients\b"),
    ("intake automation", r"\bintake\b"),
)

_THEMES: tuple[tuple[str, str], ...] = (
    ("leadership", r"\breported\s+to\s+me\b|\bmy\s+(?:small\s+)?team\b|\bteam\s+of\s+\d+\b|"
                   r"\bdirect\s+reports?\b|\bmanaged\s+(?:a|an|my)\s+(?:team|copy\s?writer)\b|"
                   r"\bled\s+(?:a|the|my)\s+team\b|\bdepartment\s+lead\b"),
    ("budgets", r"\$\s?[\d,.]+|\bbudget\b|\bad\s+spend\b|\bspend\s+allocation\b|\bper\s+month\s+in\b"),
    ("channels", r"\bgoogle\s?ads\b|\bmeta\s?ads\b|\blinked\s?in\s?ads\b|\byou\s?tube\b|"
                 r"\bperformance\s?max\b|\bdemand\s?gen\b|\bpaid\s+search\b|\bpaid\s+social\b|"
                 r"\bradio\b|\btv\b|\bphysical\s+mail\b|\bcold\s+email\b"),
    ("results", r"%|\bfigures?\b|\brevenue\b|\bfrom\s+\S+\s+to\s+\S+|\bincreas|\breduc|\blower|"
                r"\bimprov|\bsav(?:e|ed|es)\b|\brecord\b"),
    ("measurement", r"\bconversion\s+tracking\b|\boffline\s+(?:conversion|event)s?\b|\battribution\b|"
                    r"\bgclid\b|\benhanced\s+conversions?\b|\bcall\s+tracking\b|"
                    r"\bdynamic\s+number\s+insertion\b|\broas\b|\bproas\b|\bcost\s+per\s+acquisition\b|"
                    r"\bcpa\b|\bctr\b|\bquality\s+score\b"),
    ("stakeholders", r"\bceo\b|\bcmo\b|\bchief\s+marketing\s+officer\b|\bdirector\s+of\s+marketing\b|"
                     r"\bpartners?\b|\bvp\s+of\b|\bstakeholders?\b|\bsenior\s+attorneys?\b|"
                     r"\bupper\s+management\b|\bowners?\b|\bpresidents?\b"),
    ("sales alignment", r"\bsales\s+team\b|\bmqls?\b|\bsqls?\b|\bclosed[\s-]won\b|\blead\s+quality\b|"
                        r"\bpipeline\b|\bsales\s+reps?\b"),
    ("automation and AI", r"\bai\b|\bautomat\w*\b|\bagents?\b|\blarge\s+language\s+models?\b|"
                          r"\bllms?\b|\bgenerative\b"),
    ("creative", r"\bcreative\b|\bad\s+copy\b|\bvideo\b|\bugc\b|\bcopy\s?writer\b|\bhooks?\b"),
    ("strategy", r"\bstrategy\b|\bforecast\w*\b|\bbuyer\s+personas?\b|\bicps?\b|"
                 r"\bvalue\s+propositions?\b|\bfunnel\b"),
    ("failures learned from", r"\blearned\b|\btaught\s+me\b|\bmistakes?\b|\bfailed\b|\bwasn'?t\s+working\b|"
                              r"\bbroken\b|\blosing\s+money\b|\bbarely\s+profitable\b|\bbottleneck\b|"
                              r"\bdifficult\b|\bpushback\b|\bwasted\b"),
    ("persuasion", r"\bconvinc\w*\b|\bapproved?\b|\bpitch\w*\b|\bargument\b|\bpersuad\w*\b|"
                   r"\breluctant\b|\bshow(?:ed|ing)\b.*\bdata\b"),
    ("clients and agencies", r"\bclients?\b|\bagenc(?:y|ies)\b|\baccounts?\b"),
    ("product and engineering", r"\bbuilt\s+(?:my|the)\s+software\b|\btype\s?script\b|\bnode\.?js\b|"
                                r"\bapis?\b|\bcoding\s+agents?\b|\bcodex\b|\bclaude\s+code\b|\bsoftware\b"),
    ("outreach", r"\bcold\s+email\b|\bcold\s+outreach\b|\bgtm\b|\bsales\s?navigator\b|\boutreach\b"),
    ("geo targeting", r"\blocation\s+targeting\b|\bradius\b|\bgeo\b"),
)


class StoryParseError(ValueError):
    """The stories document could not be read into stories; carries no document text."""


@dataclass(frozen=True)
class Run:
    text: str
    bold: bool


@dataclass(frozen=True)
class Paragraph:
    runs: tuple[Run, ...]
    heading: bool = False


@dataclass(frozen=True)
class Story:
    number: int
    title: str
    sentences: tuple[str, ...]

    @property
    def body(self) -> str:
        return " ".join(self.sentences)

    @property
    def story_id(self) -> str:
        return hashlib.sha256(_normalize(self.title).casefold().encode("utf-8")).hexdigest()[:16]

    @property
    def word_count(self) -> int:
        return len(self.body.split())


@dataclass(frozen=True)
class StoryAnalysis:
    story_id: str
    number: int
    title: str
    employer: str | None
    project: str | None
    role: str | None
    period: str | None
    situation: tuple[str, ...]
    actions: tuple[str, ...]
    outcomes: tuple[str, ...]
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    themes: tuple[str, ...]

    @property
    def context_label(self) -> str | None:
        """What the story is about, for chunk headers and fact context."""
        return self.employer or self.project


@dataclass(frozen=True)
class StoryChunk:
    id: str
    story_id: str
    number: int
    kind: str
    title: str
    employer: str | None
    period: str | None
    themes: tuple[str, ...]
    text: str

    @property
    def word_count(self) -> int:
        return len(self.text.split())


@dataclass(frozen=True)
class StoryIndex:
    file_sha256: str
    file_bytes: int
    stories: tuple[Story, ...]
    analyses: tuple[StoryAnalysis, ...]
    chunks: tuple[StoryChunk, ...]
    facts: tuple[CandidateFact, ...]
    skipped: tuple[dict[str, Any], ...]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- document reading --------------------------------------------------------------------


def read_docx(path: Path) -> list[Paragraph]:
    """Paragraphs of text runs (with bold and heading style) from a .docx, stdlib only."""
    try:
        size = path.stat().st_size
    except OSError:
        raise StoryParseError("The stories file is unavailable") from None
    if size > MAX_DOCX_BYTES:
        raise StoryParseError("The stories file exceeds its size limit")
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_DOCUMENT_XML_BYTES:
                raise StoryParseError("The stories document exceeds its size limit")
            xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError, OSError):
        raise StoryParseError("The stories file is not a readable .docx document") from None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise StoryParseError("The stories document XML is invalid") from None
    body = root.find(_W + "body")
    if body is None:
        raise StoryParseError("The stories document has no body")
    paragraphs: list[Paragraph] = []
    for paragraph in body.iter(_W + "p"):
        style = paragraph.find(f"{_W}pPr/{_W}pStyle")
        name = (style.get(_W + "val") or "") if style is not None else ""
        heading = name.casefold().startswith(("heading", "title"))
        runs: list[Run] = []
        for run in paragraph.iter(_W + "r"):
            properties = run.find(_W + "rPr")
            bold = False
            if properties is not None:
                flag = properties.find(_W + "b")
                bold = flag is not None and (flag.get(_W + "val") or "true").casefold() not in (
                    "0", "false", "off")
            pieces: list[str] = []
            for child in run:
                if child.tag == _W + "t":
                    pieces.append(child.text or "")
                elif child.tag == _W + "tab":
                    pieces.append("\t")
                elif child.tag in (_W + "br", _W + "cr"):
                    pieces.append("\n")
            text = "".join(pieces)
            if text:
                runs.append(Run(text, bold))
        paragraphs.append(Paragraph(tuple(runs), heading))
    return paragraphs


def split_sentences(text: str) -> list[str]:
    """Sentences of a block of text; a missing space after a period still splits."""
    return [_normalize(part) for part in _SENTENCE_BOUNDARY.split(text) if part and part.strip()]


def parse_stories(paragraphs: Sequence[Paragraph]) -> list[Story]:
    """Stories start at a bold or heading segment reading "Story NN - title". A
    document without such headings falls back to short bold or heading lines that end
    without a period. Each story's body is everything up to the next heading."""
    segments: list[tuple[str, bool]] = []
    for paragraph in paragraphs:
        if paragraph.heading:
            text = "".join(run.text for run in paragraph.runs)
            if text.strip():
                segments.append((text, True))
        else:
            for bold, group in itertools.groupby(paragraph.runs, key=lambda run: run.bold):
                text = "".join(run.text for run in group)
                if text.strip():
                    segments.append((text, bold))
        segments.append(("\n", False))
    numbered = any(emphasis and _HEADING.match(text) for text, emphasis in segments)

    def is_title(index: int) -> tuple[int | None, str] | None:
        text, emphasis = segments[index]
        if not emphasis:
            return None
        if numbered:
            match = _HEADING.match(_normalize(text))
            return (int(match.group(1)), match.group(2)) if match else None
        cleaned = _normalize(text)
        previous = next((segments[i][0] for i in range(index - 1, -1, -1)
                         if segments[i][0].strip()), "")
        starts_line = (index == 0 or not previous or previous.rstrip().endswith((".", "!", "?"))
                       or segments[index - 1][0] == "\n")
        if starts_line and 0 < len(cleaned) <= 120 and not cleaned.endswith((".", "!", "?", ":")):
            return None, cleaned
        return None

    stories: list[Story] = []
    title: str | None = None
    number: int | None = None
    body: list[str] = []

    def flush() -> None:
        if title is None:
            return
        sentences = tuple(split_sentences("".join(body)))
        if not sentences:
            raise StoryParseError("A story heading has no story text under it")
        stories.append(Story(number if number is not None else len(stories) + 1,
                             _normalize(title), sentences))

    for index, (text, _) in enumerate(segments):
        found = is_title(index)
        if found is not None:
            flush()
            number, title = found
            body = []
        elif title is not None:
            body.append(text)
    flush()
    if not stories:
        raise StoryParseError("No story headings were found in the document")
    if len(stories) > MAX_STORIES:
        raise StoryParseError("The document exceeds the story limit")
    return stories


# --- analysis -----------------------------------------------------------------------------


def find_tools(text: str) -> list[str]:
    """Canonical tool names the text mentions; an ambiguous name needs two other tools."""
    found: list[tuple[int, str, bool]] = []
    for name, pattern, ambiguous in _TOOLS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            found.append((match.start(), name, ambiguous))
    certain = sum(1 for _, _, ambiguous in found if not ambiguous)
    company = certain >= 2 or (certain >= 1 and len(found) >= 3)
    names = [name for _, name, ambiguous in sorted(found) if not ambiguous or company]
    return list(dict.fromkeys(names))


def find_skills(text: str) -> list[str]:
    return [name for name, pattern in _SKILLS if re.search(pattern, text, re.IGNORECASE)]


def find_themes(text: str, *, outcomes: bool) -> list[str]:
    themes = [name for name, pattern in _THEMES if re.search(pattern, text, re.IGNORECASE)]
    if not outcomes and "results" in themes:
        themes.remove("results")
    return themes


def _period(text: str) -> str | None:
    years = sorted(set(_YEAR.findall(text)))
    if years:
        return years[0] if len(years) == 1 else f"{years[0]}\u2013{years[-1]}"
    duration = _DURATION.search(text)
    return _normalize(duration.group(0)) if duration else None


def _employer(sentences: Sequence[str]) -> str | None:
    """An organization the opening names as the work's context ("for a law firm"), the
    first sentence preferred; a phrase with a verb or connective in it is not one."""
    for window in (sentences[:1], sentences[:3]):
        for match in _EMPLOYER.finditer(" ".join(window)):
            phrase = _normalize(match.group(1))
            words = phrase.casefold().split()
            if len(words) <= 5 and not any(word in _EMPLOYER_STOPWORDS for word in words):
                return phrase
    return None


def analyse_story(story: Story) -> StoryAnalysis:
    body = story.body
    employer = _employer(story.sentences)
    project_match = _PROJECT.search(body)
    project = project_match.group(1).rstrip(".") if project_match else None
    role_match = _ROLE_POSITION.search(body) or _ROLE.search(body)
    role = _normalize(role_match.group(1)).casefold() if role_match else None
    situation = [story.sentences[0], *(s for s in story.sentences[1:8] if _SITUATION.search(s))][:3]
    actions = [s for s in story.sentences if _FIRST_PERSON.search(s) and _ACTION.search(s)][:20]
    outcomes = [s for s in story.sentences if re.search(r"\d", s) and _RESULT.search(s)
                and (_FIRST_PERSON.search(s) or _LEADING_VERB.match(s) or _OWN_SUBJECT.match(s))][:12]
    tools = find_tools(body)
    return StoryAnalysis(
        story_id=story.story_id, number=story.number, title=story.title, employer=employer,
        project=project, role=role, period=_period(body), situation=tuple(situation),
        actions=tuple(actions), outcomes=tuple(outcomes), tools=tuple(tools),
        skills=tuple(find_skills(body)), themes=tuple(find_themes(body, outcomes=bool(outcomes))))


# --- chunks -------------------------------------------------------------------------------


def _clean_field(value: str) -> str:
    return _normalize(value).replace("|", "/")


def chunk_header(analysis: StoryAnalysis) -> str:
    parts = [f"Story {analysis.number:02d}: {_clean_field(analysis.title)}"]
    if analysis.employer:
        parts.append("employer: " + _clean_field(analysis.employer))
    elif analysis.project:
        parts.append("project: " + _clean_field(analysis.project))
    if analysis.role:
        parts.append("role: " + _clean_field(analysis.role))
    if analysis.period:
        parts.append("period: " + _clean_field(analysis.period))
    if analysis.themes:
        parts.append("themes: " + ", ".join(analysis.themes))
    return " | ".join(parts)


_HEADER = re.compile(r"^Story (\d{1,3}): (?P<title>[^|\n]*?)(?P<rest>(?: \| [a-z]+: [^|\n]*)*)$")


def parse_chunk_header(text: str) -> dict[str, Any] | None:
    """The metadata a chunk carries in its first line, or None for a foreign body."""
    first = text.split("\n", 1)[0]
    match = _HEADER.match(first)
    if not match:
        return None
    fields: dict[str, Any] = {
        "number": int(match.group(1)), "title": match.group("title").strip(),
        "employer": None, "project": None, "role": None, "period": None, "themes": []}
    for item in match.group("rest").split(" | ")[1:]:
        name, _, value = item.partition(": ")
        if name == "themes":
            fields["themes"] = [theme.strip() for theme in value.split(",") if theme.strip()]
        elif name in fields:
            fields[name] = value.strip() or None
    fields["story_id"] = hashlib.sha256(
        _normalize(str(fields["title"])).casefold().encode("utf-8")).hexdigest()[:16]
    return fields


def _hard_split(sentence: str, limit: int) -> list[str]:
    words = sentence.split()
    pieces: list[str] = []
    current: list[str] = []
    for word in words:
        if current and len(" ".join([*current, word])) > limit:
            pieces.append(" ".join(current))
            current = []
        current.append(word)
    if current:
        pieces.append(" ".join(current))
    return pieces


def _pack(sentences: Iterable[str], *, max_words: int, max_chars: int) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    words = chars = 0
    for sentence in sentences:
        for piece in ([sentence] if len(sentence) <= max_chars else _hard_split(sentence, max_chars)):
            count, size = len(piece.split()), len(piece) + 1
            if current and (words + count > max_words or chars + size > max_chars):
                groups.append(current)
                current, words, chars = [], 0, 0
            current.append(piece)
            words += count
            chars += size
    if current:
        groups.append(current)
    if len(groups) >= 2:
        tail, previous = groups[-1], groups[-2]
        if (sum(len(s.split()) for s in tail) < MIN_CHUNK_WORDS
                and sum(len(s.split()) for s in previous + tail) <= max_words
                and len(" ".join(previous + tail)) <= max_chars):
            groups[-2:] = [previous + tail]
    return groups


def _chunk(analysis: StoryAnalysis, number: int, kind: str, text: str) -> StoryChunk:
    text = text.strip()
    if len(text) > MAX_CHUNK_CHARS + 50:
        raise StoryParseError("A story chunk exceeds the store's size bound")
    return StoryChunk(id="story:" + _sha256(text), story_id=analysis.story_id, number=number,
                      kind=kind, title=analysis.title, employer=analysis.context_label,
                      period=analysis.period, themes=analysis.themes, text=text)


def chunk_story(story: Story, analysis: StoryAnalysis) -> list[StoryChunk]:
    """One summary chunk, then sections of about 120-300 words, each prefixed with the
    story's header line so it stands alone. Chunk ids are content hashes."""
    header = chunk_header(analysis)
    header_words, header_chars = len(header.split()), len(header) + 1
    max_words = max(MAX_CHUNK_WORDS - header_words, 60)
    max_chars = MAX_CHUNK_CHARS - header_chars
    summary_parts = [f"Summary of story {analysis.number:02d}."]
    if analysis.role and analysis.context_label:
        summary_parts.append(f"Role: {analysis.role} at {analysis.context_label}"
                             + (f" ({analysis.period})." if analysis.period else "."))
    elif analysis.context_label:
        summary_parts.append(f"Context: {analysis.context_label}"
                             + (f" ({analysis.period})." if analysis.period else "."))
    if analysis.situation:
        summary_parts.append("Situation: " + analysis.situation[0])
    outcomes = list(analysis.outcomes[:3])
    tools = "Tools: " + ", ".join(analysis.tools) + "." if analysis.tools else ""
    themes = "Themes: " + ", ".join(analysis.themes) + "." if analysis.themes else ""
    while True:
        summary = " ".join(part for part in [*summary_parts, *(["Outcomes: " + " ".join(outcomes)]
                                                                if outcomes else []), tools, themes] if part)
        if (len(summary.split()) <= max_words and len(summary) <= max_chars) or not outcomes:
            break
        outcomes.pop()
    if len(summary) > max_chars:
        summary = " ".join(_hard_split(summary, max_chars)[:1])
    chunks = [_chunk(analysis, 0, "summary", header + "\n" + summary)]
    for index, group in enumerate(_pack(story.sentences, max_words=max_words, max_chars=max_chars), 1):
        chunks.append(_chunk(analysis, index, "section", header + "\n" + " ".join(group)))
    return chunks


# --- facts --------------------------------------------------------------------------------


def numbers_in(text: str) -> set[str]:
    """Every number as written (digits with their separators), for grounding checks."""
    return {match.rstrip(".,") for match in _DIGITS.findall(text)}


def _fact_key(sentence: str, tools: list[str]) -> str | None:
    if not (_FIRST_PERSON.search(sentence) or _LEADING_VERB.match(sentence) or _OWN_SUBJECT.match(sentence)):
        return None
    quantified = bool(re.search(r"\d", sentence))
    if _CERTIFICATION.search(sentence):
        return "education"
    if quantified and _RESULT.search(sentence):
        return "achievement"
    if tools:
        return "skills"
    if quantified or _BUDGET.search(sentence):
        return "experience"
    if _PROJECT.search(sentence):
        return "project"
    return None


def extract_story_facts(story: Story, analysis: StoryAnalysis, chunks: Sequence[StoryChunk], *,
                        verified_at: datetime) -> tuple[list[CandidateFact], list[dict[str, Any]]]:
    """Atomic, user-authored facts from a story's concrete first-person sentences, each
    with ``story:<chunk id>`` provenance. Values are the sentences as written (numbers
    are never changed); a vague sentence yields nothing. Returns the facts and the
    sentences skipped for length."""
    facts: list[CandidateFact] = []
    skipped: list[dict[str, Any]] = []
    body = story.body
    body_numbers = numbers_in(body)
    context = analysis.employer
    if context and analysis.period:
        context += ", " + analysis.period
    verification = FactVerification(status=VerificationStatus.VERIFIED,
                                    method=VerificationMethod.USER_STATED, verified_at=verified_at)
    sections = [chunk for chunk in chunks if chunk.kind == "section"]

    def provenance(sentence: str) -> str:
        return next((chunk.id for chunk in sections if sentence in chunk.text), chunks[0].id)

    def add(key: str, value: str, source: str, evidence: list[str]) -> None:
        if not numbers_in(value) <= body_numbers | (numbers_in(context) if context else set()):
            return  # a number the story does not state; never invented here
        digest = _sha256(key + "\n" + value)[:12]
        identifier = f"sf_{analysis.story_id}_{digest}"
        if any(fact.id == identifier for fact in facts):
            return
        facts.append(CandidateFact(id=identifier, key=key, value=value, source=source,
                                   verification=verification, evidence=evidence))

    label = f"Story {analysis.number:02d}: {analysis.title}"
    if analysis.role and analysis.employer:
        role_sentence = next((s for s in story.sentences
                              if re.search(re.escape(analysis.role), s, re.IGNORECASE)), story.sentences[0])
        value = f"{analysis.role[0].upper()}{analysis.role[1:]}, {analysis.employer}"
        value += f" ({analysis.period})" if analysis.period else ""
        add("employment", value, provenance(role_sentence), [label, role_sentence])
    for sentence in story.sentences:
        tools = find_tools(sentence)
        key = _fact_key(sentence, tools)
        if key is None:
            continue
        if len(sentence) > MAX_FACT_CHARS:
            skipped.append({"story": analysis.number, "reason": "sentence_too_long",
                            "chars": len(sentence), "key": key})
            continue
        value = sentence if not context else f"{sentence} ({context})"
        evidence = [label] + (["Tools: " + ", ".join(tools)] if tools else [])
        add(key, value, provenance(sentence), evidence)
    return facts, skipped


# --- whole document -----------------------------------------------------------------------


def build_story_index(path: Path, *, verified_at: datetime) -> StoryIndex:
    """Stories, analyses, chunks and facts of one document; no text leaves in receipts."""
    data = path.read_bytes()
    paragraphs = read_docx(path)
    stories = parse_stories(paragraphs)
    analyses: list[StoryAnalysis] = []
    chunks: list[StoryChunk] = []
    facts: list[CandidateFact] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for story in stories:
        analysis = analyse_story(story)
        story_chunks = chunk_story(story, analysis)
        story_facts, story_skipped = extract_story_facts(story, analysis, story_chunks,
                                                         verified_at=verified_at)
        analyses.append(analysis)
        chunks.extend(chunk for chunk in story_chunks if chunk.id not in seen)
        seen.update(chunk.id for chunk in story_chunks)
        facts.extend(fact for fact in story_facts if fact.id not in {f.id for f in facts})
        skipped.extend(story_skipped)
    return StoryIndex(hashlib.sha256(data).hexdigest(), len(data), tuple(stories),
                      tuple(analyses), tuple(chunks), tuple(facts), tuple(skipped))


def story_index_receipt(index: StoryIndex) -> dict[str, Any]:
    """Counts, ids and hashes only; never titles, chunk text or fact values."""
    by_key: dict[str, int] = {}
    for fact in index.facts:
        by_key[fact.key] = by_key.get(fact.key, 0) + 1
    return {
        "file_sha256": index.file_sha256, "file_bytes": index.file_bytes,
        "story_count": len(index.stories),
        "stories": [{
            "story_id": analysis.story_id, "number": analysis.number,
            "title_sha256": _sha256(story.title), "word_count": story.word_count,
            "sentence_count": len(story.sentences),
            "chunk_count": sum(1 for c in index.chunks if c.story_id == analysis.story_id),
            "fact_count": sum(1 for f in index.facts if f.id.startswith(f"sf_{analysis.story_id}_")),
            "has_employer": analysis.employer is not None, "has_project": analysis.project is not None,
            "has_role": analysis.role is not None, "has_period": analysis.period is not None,
            "situation_count": len(analysis.situation), "action_count": len(analysis.actions),
            "outcome_count": len(analysis.outcomes), "tool_count": len(analysis.tools),
            "skill_count": len(analysis.skills), "themes": list(analysis.themes),
        } for story, analysis in zip(index.stories, index.analyses, strict=True)],
        "chunk_count": len(index.chunks),
        "chunk_ids": [chunk.id for chunk in index.chunks],
        "chunk_words": [chunk.word_count for chunk in index.chunks],
        "fact_count": len(index.facts), "facts_by_key": dict(sorted(by_key.items())),
        "fact_ids": [fact.id for fact in index.facts],
        "skipped": list(index.skipped),
    }


def facts_review(index: StoryIndex, *, candidate_id: str) -> dict[str, Any]:
    """The full fact list for the user's private review (contains story text)."""
    return {
        "candidate_sha256": _sha256(candidate_id), "file_sha256": index.file_sha256,
        "facts": [{"id": fact.id, "key": fact.key, "value": fact.value, "source": fact.source,
                   "evidence": list(fact.evidence),
                   "verification": fact.verification.model_dump(mode="json")}
                  for fact in index.facts],
        "stories": [{"number": a.number, "story_id": a.story_id, "title": a.title,
                     "employer": a.employer, "project": a.project, "role": a.role,
                     "period": a.period, "tools": list(a.tools), "skills": list(a.skills),
                     "themes": list(a.themes), "outcomes": list(a.outcomes)}
                    for a in index.analyses],
        "skipped": list(index.skipped),
    }


def chunk_metadata(chunk: StoryChunk) -> dict[str, Any]:
    return {"id": chunk.id, "story_id": chunk.story_id, "number": chunk.number, "kind": chunk.kind,
            "words": chunk.word_count, "chars": len(chunk.text)}


def dumps_receipt(receipt: dict[str, Any]) -> str:
    return json.dumps(receipt, sort_keys=True)


__all__ = [
    "DEFAULT_STORY_SOURCE", "MAX_CHUNK_CHARS", "MAX_CHUNK_WORDS", "MIN_CHUNK_WORDS",
    "STORY_CHUNK_ID", "Paragraph", "Run", "Story", "StoryAnalysis", "StoryChunk", "StoryIndex",
    "StoryParseError", "analyse_story", "build_story_index", "chunk_header", "chunk_metadata",
    "chunk_story", "extract_story_facts", "facts_review", "find_skills", "find_themes",
    "find_tools", "numbers_in", "parse_chunk_header", "parse_stories", "read_docx",
    "split_sentences", "story_index_receipt",
]
