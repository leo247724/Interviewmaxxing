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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from interviewmaxxing_core import (
    CandidateFact,
    CandidateProfile,
    FactVerification,
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
MAX_TITLE_CHARS = 400
"""A story heading longer than this is a paragraph marked as a heading, not a title."""
_COMPANY_GENERIC_WORDS = frozenset({
    "inc", "llc", "ltd", "corp", "co", "company", "corporation", "consulting", "solutions",
    "group", "agency", "agencies", "law", "firm", "the", "and", "of", "marketing", "media",
    "digital", "partners", "services", "labs", "studio", "global", "international", "holdings",
    "limited", "plc", "gmbh", "team", "office", "practice", "clinic", "shop", "store",
    # Common business words that name no one employer on their own (WP12 round 5): a story
    # about "Growth Marketing" is not about "Shop Growth Solutions".
    "growth", "consultants", "consultancy", "strategies", "strategy", "creative", "creatives",
    "advertising", "ads", "ventures", "capital", "systems", "technologies", "technology", "tech",
    "brands", "brand", "network", "networks", "enterprise", "enterprises", "industries",
    "associates", "management", "analytics", "data", "commerce", "ecommerce", "online", "web",
    "interactive", "communications", "performance", "sales", "works", "collective", "lab",
    "studios", "hub", "center", "centre", "national", "united", "american", "america", "usa",
    "social", "search", "content", "design", "development", "software", "cloud", "financial",
    "insurance", "legal", "attorneys", "lawyers", "medical", "health", "healthcare", "real",
    "estate", "realty", "llp", "pllc"})
_LEGAL_SUFFIXES = frozenset({"inc", "llc", "ltd", "corp", "co", "company", "corporation", "limited",
                             "plc", "gmbh", "llp", "pllc", "lp", "pc", "sa", "ag", "bv", "pty"})
"""Trailing words a story may leave out of a company's full name ("Acme Growth" for
"Acme Growth, Inc.")."""

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
_FIRST_PERSON = re.compile(r"\b(?:I|I've|I'd|I'm|my|me|myself)\b")
"""The applicant speaking for themselves: only such sentences (or resume-style ones that
open with a verb) may become the applicant's own facts; "we", "our" and "us" describe a
team's work and stay story evidence (WP12 round 4, H5)."""
_ANY_PERSON = re.compile(r"\b(?:I|I've|I'd|I'm|my|me|myself|we|we've|our|us)\b", re.IGNORECASE)
"""First person singular or plural: what the analysis reads as the story's actions."""
_PLURAL_PERSON = re.compile(r"\b(?:we|we've|our|us)\b", re.IGNORECASE)
_YEAR_RANGE = re.compile(r"\b((?:19|20)\d{2})\s*(?:-|\u2013|\u2014|to|through|until|and)\s*((?:19|20)\d{2})\b")
_MONTH_NUMBERS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
                  "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_NAME = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
               r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_PERIOD_RANGE = re.compile(
    rf"(?<![\w/-])(?:(?P<start_name>{_MONTH_NAME})\s+|(?P<start_number>0?[1-9]|1[0-2])/)?"
    r"(?P<start_year>(?:19|20)\d{2})(?:-(?P<start_iso>0[1-9]|1[0-2])(?!\d))?"
    r"\s*(?:-|\u2013|\u2014|to|through|until|till|thru)\s*"
    rf"(?:(?:(?P<end_name>{_MONTH_NAME})\s+|(?P<end_number>0?[1-9]|1[0-2])/)?"
    r"(?P<end_year>(?:19|20)\d{2})(?:-(?P<end_iso>0[1-9]|1[0-2])(?!\d))?"
    r"|(?P<open>present|current|now|today))(?![\w/])", re.IGNORECASE)
"""A stated period: "Aug 2019 - May 2020", "Jun 2025 - Present", "08/2019 to 05/2020",
"2019-08 to 2020-05" or "2019 to 2023"; a single year or a year list is not a period."""
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
    ("paid media", r"\bpaid\s+media\b"),
    ("programmatic advertising", r"\bprogrammatic\b"),
    ("SEO", r"\bseo\b|\bsearch\s+engine\s+optimi[sz]ation\b"),
    ("PPC", r"\bppc\b|\bpay[- ]per[- ]click\b"),
    ("performance marketing", r"\bperformance\s+marketing\b"),
    ("demand generation", r"\bdemand\s+generation\b"),
    ("growth marketing", r"\bgrowth\s+marketing\b"),
    ("digital marketing", r"\bdigital\s+marketing\b"),
    ("email marketing", r"\bemail\s+marketing\b"),
    ("content marketing", r"\bcontent\s+marketing\b"),
    ("social media marketing", r"\bsocial\s+media\s+marketing\b"),
    ("project management", r"\bproject\s+manag\w+\b"),
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
    boundary: bool = False
    """A Markdown H1 or H2: it starts a story whatever its wording, numbered or not."""


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
class ResumeRole:
    """A resume experience group as the linking and dating reference for a story."""
    id: str
    company: str
    title: str
    start: str | None
    end: str | None
    current: bool
    bullets: tuple[str, ...]

    @property
    def period(self) -> str | None:
        if not self.start:
            return None
        return f"{self.start} to {self.end or ('present' if self.current else '?')}"


@dataclass(frozen=True)
class StoryRoleLink:
    """A story linked to one resume role, by employer name or by a gated decision."""
    story_id: str
    resume_role_id: str
    company: str
    title: str
    start: str | None
    end: str | None
    current: bool
    method: str
    """``employer_name`` (a distinctive company token in the story) or ``jev_match``."""
    confidence: float = 1.0
    probability: float = 1.0

    @property
    def period(self) -> str | None:
        if not self.start:
            return None
        return f"{self.start} to {self.end or ('present' if self.current else '?')}"

    def contains_year(self, year: str) -> bool:
        """Whether a stated year falls within the role's dates (an open end counts)."""
        if not self.start:
            return True
        first = int(self.start[:4])
        last = int(self.end[:4]) if self.end else 9999
        return first <= int(year) <= last


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
    resume_role_id: str | None = None

    @property
    def word_count(self) -> int:
        return len(self.text.split())


@dataclass(frozen=True)
class StoryDocument:
    file_sha256: str
    file_bytes: int
    stories: tuple[Story, ...]
    analyses: tuple[StoryAnalysis, ...]


@dataclass(frozen=True)
class StoryIndex:
    file_sha256: str
    file_bytes: int
    stories: tuple[Story, ...]
    analyses: tuple[StoryAnalysis, ...]
    chunks: tuple[StoryChunk, ...]
    facts: tuple[CandidateFact, ...]
    skipped: tuple[dict[str, Any], ...]
    links: Mapping[str, StoryRoleLink] | None = None
    """Resume role links by story id (``resolve_period`` dates the facts and headers)."""
    source_id: str = DEFAULT_STORY_SOURCE
    link_mismatches: Mapping[str, dict[str, Any]] | None = None
    """Proposed links the story's own stated period contradicts, by story id: not used,
    reported in the review (``link_period_mismatch``)."""

    def link_for(self, story_id: str) -> StoryRoleLink | None:
        return (self.links or {}).get(story_id)

    def mismatch_for(self, story_id: str) -> dict[str, Any] | None:
        return (self.link_mismatches or {}).get(story_id)


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


_MARKDOWN_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*|\*\*)?\s*(stor(?:y|ies)\s*#?\s*\d{1,3}\s*[-\u2013\u2014:.]\s*.+?)\s*(?:\*\*)?\s*$",
    re.IGNORECASE)
_ATX_HEADING = re.compile(r"^(#{1,6})(?:\s+(.*?))?(?:\s+#+)?\s*$")
"""A Markdown heading line: hashes, then a space ("#hashtag" is text, not a heading)."""
_SETEXT_UNDERLINE = re.compile(r"^(?:={3,}|-{3,})$")
_THEMATIC_BREAK = re.compile(r"^(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$")
TEXT_SUFFIXES = frozenset({".md", ".markdown", ".txt"})


def _heading_text(text: str) -> str:
    """A heading's words without surrounding emphasis marks ("**Title**", "_Title_")."""
    text = text.strip()
    while len(text) > 2 and text[0] == text[-1] and text[0] in "*_":
        text = text[1:-1].strip()
    return text


def read_text_document(path: Path) -> list[Paragraph]:
    """Paragraphs of a plain-text or Markdown stories file: a line reading "Stories NN -
    title" (Markdown marks allowed) or any other heading line is a heading paragraph;
    the lines up to the next blank line form one body paragraph. An H1 or H2 (``#``,
    ``##`` or a setext underline) is a story boundary: it starts a story whatever its
    wording, like a heading of any level in a .docx. The words are not changed."""
    try:
        if path.stat().st_size > MAX_DOCX_BYTES:
            raise StoryParseError("The stories file exceeds its size limit")
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise StoryParseError("The stories file is unavailable or is not UTF-8 text") from None
    paragraphs: list[Paragraph] = []
    block: list[str] = []

    def flush() -> None:
        if block:
            paragraphs.append(Paragraph((Run(" ".join(block), False),)))
            block.clear()

    def heading(text: str, *, level: int | None) -> None:
        flush()
        numbered = _MARKDOWN_HEADING.match(text)
        words = numbered.group(1) if numbered else _heading_text(text)
        if words:
            paragraphs.append(Paragraph((Run(words, bool(numbered)),), heading=True,
                                        boundary=level is not None and level <= 2))

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        atx = _ATX_HEADING.match(stripped)
        if _SETEXT_UNDERLINE.match(stripped) and len(block) == 1:
            title = block.pop()  # "Title" underlined with === (H1) or --- (H2)
            heading(title, level=1 if stripped.startswith("=") else 2)
        elif _THEMATIC_BREAK.match(stripped) or _SETEXT_UNDERLINE.match(stripped):
            flush()  # a separator line, not text
        elif atx:
            heading(atx.group(2) or "", level=len(atx.group(1)))
        elif _MARKDOWN_HEADING.match(stripped):
            heading(stripped, level=None)  # "**Stories NN - title**" or a bare numbered line
        else:
            block.append(stripped)
    flush()
    return paragraphs


def read_document(path: Path) -> list[Paragraph]:
    """A .docx, or a .md/.txt file, as paragraphs of runs."""
    if path.suffix.casefold() in TEXT_SUFFIXES:
        return read_text_document(path)
    if path.suffix.casefold() == ".docx":
        return read_docx(path)
    raise StoryParseError("The stories file must be a .docx, .md or .txt document")


def split_sentences(text: str) -> list[str]:
    """Sentences of a block of text; a missing space after a period still splits."""
    return [_normalize(part) for part in _SENTENCE_BOUNDARY.split(text) if part and part.strip()]


@dataclass(frozen=True)
class _Segment:
    text: str
    emphasis: bool
    heading: bool = False
    boundary: bool = False


def parse_stories(paragraphs: Sequence[Paragraph]) -> list[Story]:
    """Stories start at a bold or heading segment reading "Story NN - title". A
    document without such headings falls back to short bold or heading lines that end
    without a period. A Markdown H1 or H2 (``Paragraph.boundary``) always starts a story,
    numbered or not, however long its wording (up to ``MAX_TITLE_CHARS``). Each story's
    body is everything up to the next heading. A heading with no text under it that is
    followed directly by another heading is a section title (a document title over H2
    stories), not a story; any other heading without text is an error."""
    segments: list[_Segment] = []
    for paragraph in paragraphs:
        if paragraph.heading:
            text = "".join(run.text for run in paragraph.runs)
            if text.strip():
                segments.append(_Segment(text, True, heading=True, boundary=paragraph.boundary))
        else:
            for bold, group in itertools.groupby(paragraph.runs, key=lambda run: run.bold):
                text = "".join(run.text for run in group)
                if text.strip():
                    segments.append(_Segment(text, bold))
        segments.append(_Segment("\n", False))
    numbered = any(segment.emphasis and _HEADING.match(segment.text) for segment in segments)

    def is_title(index: int) -> tuple[int | None, str] | None:
        segment = segments[index]
        if not segment.emphasis:
            return None
        cleaned = _normalize(segment.text)
        match = _HEADING.match(cleaned)
        if segment.boundary:
            return (int(match.group(1)), match.group(2)) if match else (None, cleaned)
        if numbered:
            return (int(match.group(1)), match.group(2)) if match else None
        previous = next((segments[i].text for i in range(index - 1, -1, -1)
                         if segments[i].text.strip()), "")
        starts_line = (index == 0 or not previous or previous.rstrip().endswith((".", "!", "?"))
                       or segments[index - 1].text == "\n")
        if starts_line and 0 < len(cleaned) <= 120 and not cleaned.endswith((".", "!", "?", ":")):
            return None, cleaned
        return None

    stories: list[Story] = []
    title: str | None = None
    number: int | None = None
    from_heading = False
    body: list[str] = []

    def flush(*, next_title: bool) -> None:
        if title is None:
            return
        sentences = tuple(split_sentences("".join(body)))
        if not sentences:
            if from_heading and next_title:
                return  # a section title directly over the next heading
            raise StoryParseError("A story heading has no story text under it")
        if len(_normalize(title)) > MAX_TITLE_CHARS:
            raise StoryParseError("A story heading exceeds its length bound")
        # A chunk header separates its fields with "|": a title keeps the same words with
        # "/" there, so the story, its chunks and its facts share one title and story id.
        stories.append(Story(number if number is not None else len(stories) + 1,
                             _clean_field(title), sentences))

    for index, segment in enumerate(segments):
        found = is_title(index)
        if found is not None:
            flush(next_title=True)
            number, title = found
            from_heading = segment.heading
            body = []
        elif title is not None:
            body.append(segment.text)
    flush(next_title=False)
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


def _period(story: Story) -> str | None:
    """What the story states about its own time: the range in its heading or its body's
    one range, else its one year or adjacent or ranged years, else a duration phrase
    ("almost 2 years"). Years stated apart date nothing (round 4, L8)."""
    own = dating_period(story)
    if own is not None:
        return own.label
    span = stated_year_span(story)
    if span is not None:
        return span
    duration = _DURATION.search(story.body)
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
    actions = [s for s in story.sentences if _ANY_PERSON.search(s) and _ACTION.search(s)][:20]
    outcomes = [s for s in story.sentences if re.search(r"\d", s) and _RESULT.search(s)
                and (_ANY_PERSON.search(s) or _LEADING_VERB.match(s) or _OWN_SUBJECT.match(s))][:12]
    tools = find_tools(body)
    return StoryAnalysis(
        story_id=story.story_id, number=story.number, title=story.title, employer=employer,
        project=project, role=role, period=_period(story), situation=tuple(situation),
        actions=tuple(actions), outcomes=tuple(outcomes), tools=tuple(tools),
        skills=tuple(find_skills(body)), themes=tuple(find_themes(body, outcomes=bool(outcomes))))


# --- resume roles, links and periods -----------------------------------------------------


def resume_roles(profile: CandidateProfile) -> list[ResumeRole]:
    """The profile's experience groups with their verified bullet texts."""
    by_id = {fact.id: fact for fact in profile.verified_facts()}
    roles = []
    for group in profile.experience:
        bullets = tuple(str(by_id[fid].value) for fid in group.fact_ids
                        if fid in by_id and isinstance(by_id[fid].value, str))
        roles.append(ResumeRole(group.id, group.company, group.title, group.start, group.end,
                                group.current, bullets))
    return roles


def company_tokens(company: str) -> set[str]:
    """Distinctive words of a company name (no suffixes or generic industry words)."""
    words = {w for w in re.findall(r"[a-z0-9]+", company.casefold()) if len(w) >= 3}
    return {w for w in words if w not in _COMPANY_GENERIC_WORDS}


def company_words(company: str) -> list[str]:
    """A company's full name as words, without the trailing legal suffixes a story may omit."""
    words = re.findall(r"[a-z0-9]+", company.casefold())
    while len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return words


def _contains_words(words: Sequence[str], phrase: Sequence[str]) -> bool:
    size = len(phrase)
    return size > 0 and any(list(words[i:i + size]) == list(phrase) for i in range(len(words) - size + 1))


def names_company(texts: Sequence[str], company: str) -> bool:
    """Whether the texts name the company distinctively: every distinctive word of its
    name (``company_tokens``), or its full name as consecutive words. A single common
    word ("Growth", "Solutions", "Marketing", "Law", "Group", "Inc") never names a
    company, and a name made only of such words needs its full name."""
    pieces = [re.findall(r"[a-z0-9]+", text.casefold()) for text in texts if text]
    tokens = company_tokens(company)
    if tokens and tokens <= {word for words in pieces for word in words}:
        return True
    phrase = company_words(company)
    return any(_contains_words(words, phrase) for words in pieces)


def match_role_by_name(story: Story, analysis: StoryAnalysis,
                       roles: Sequence[ResumeRole]) -> StoryRoleLink | None:
    """The one resume role whose company the story names distinctively (``names_company``
    over its heading, text, employer phrase and product name); several or none: no link."""
    texts = [analysis.title, story.body, analysis.employer or "", analysis.project or ""]
    matches = [role for role in roles if names_company(texts, role.company)]
    if len(matches) != 1:
        return None
    role = matches[0]
    return StoryRoleLink(analysis.story_id, role.id, role.company, role.title, role.start,
                         role.end, role.current, "employer_name")


_TEAM_SIZE = re.compile(
    r"\bteam of (\d{1,3})\b|\b(\d{1,3}) (?:direct reports|reports|specialists|people|members|"
    r"marketers|analysts|coordinators|engineers|writers|designers|buyers)\b", re.IGNORECASE)


def team_sizes(text: str) -> set[int]:
    """Team sizes a text states ("team of 4", "5 SEO specialists")."""
    return {int(a or b) for a, b in _TEAM_SIZE.findall(text)}


def resume_quantity_differences(story: Story, link: StoryRoleLink | None,
                                roles: Sequence[ResumeRole]) -> list[dict[str, Any]]:
    """Figures the story states differently from the linked resume role's bullets, for
    the person to settle; nothing is resolved or changed here. Team sizes so far."""
    if link is None:
        return []
    role = next((role for role in roles if role.id == link.resume_role_id), None)
    if role is None:
        return []
    story_sizes = team_sizes(story.body)
    resume_sizes = team_sizes(" ".join(role.bullets))
    if story_sizes and resume_sizes and story_sizes.isdisjoint(resume_sizes):
        return [{"kind": "team size", "story": sorted(story_sizes), "resume": sorted(resume_sizes),
                 "resume_role_id": role.id}]
    return []


_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_DURATION_CLAIM = re.compile(
    r"\b(?:(?P<qual>over|more than|a little more than|almost|nearly|a little less than|just under|"
    r"about|around|approximately|roughly|under|less than)\s+)?(?:(?P<n>\d+(?:\.\d+)?|"
    + "|".join(_WORD_NUMBERS) + r")\+?|(?P<article>an?))\s*(?P<unit>years?|yrs?|months?)\b"
    r"(?!\s*-?\s*old\b)", re.IGNORECASE)
"""A duration the text states; an age ("a 26 year old") is not one."""


def duration_claims(text: str) -> list[tuple[str, int]]:
    """Durations a text states with the fewest months each could mean: "almost 2 years"
    is at least 15, "over 3 years" at least 37, "2 years" at least 21, "6 months" at
    least 5; "under a year" could be any length."""
    claims = []
    for match in _DURATION_CLAIM.finditer(text):
        raw = (match.group("n") or "1").casefold()
        unit, qual = match.group("unit").casefold(), (match.group("qual") or "").casefold()
        number = _WORD_NUMBERS.get(raw) or float(raw)
        months = number * 12 if unit.startswith(("year", "yr")) else number
        years = unit.startswith(("year", "yr"))
        if qual in ("over", "more than", "a little more than"):
            minimum = months + 1
        elif qual in ("almost", "nearly", "a little less than", "just under"):
            minimum = months - (9 if years else 2)
        elif qual in ("about", "around", "approximately", "roughly"):
            minimum = months - (3 if years else 1)
        elif qual in ("under", "less than"):
            minimum = 0
        else:
            minimum = months - (3 if years else 1)
        claims.append((match.group(0), max(int(minimum), 0)))
    return claims


def role_tenure_months(link: StoryRoleLink, today: date) -> int | None:
    """The linked resume role's length in months (inclusive), an open role to today."""
    start = re.match(r"^(\d{4})(?:-(\d{2}))?$", link.start or "")
    if not start:
        return None
    first = int(start.group(1)) * 12 + int(start.group(2) or 1) - 1
    if link.end:
        end = re.match(r"^(\d{4})(?:-(\d{2}))?$", link.end)
        if not end:
            return None
        last = int(end.group(1)) * 12 + int(end.group(2) or 12) - 1
    elif link.current:
        last = today.year * 12 + today.month - 1
    else:
        return None
    return max(last - first + 1, 0)


def tenure_conflicts(sentence: str, link: StoryRoleLink | None, today: date) -> list[dict[str, Any]]:
    """Duration statements in a sentence that exceed the linked role's tenure: the story
    cannot have spent "almost 2 years" in a role the resume dates to 12 months. Durations
    shorter than the tenure fit inside it and never conflict."""
    if link is None:
        return []
    tenure = role_tenure_months(link, today)
    if tenure is None:
        return []
    return [{"phrase": phrase, "minimum_months": minimum, "tenure_months": tenure,
             "resume_role_id": link.resume_role_id}
            for phrase, minimum in duration_claims(sentence) if minimum > tenure + 1]


def _stated_text(story: Story) -> str:
    """The heading and the body: a story often dates itself in its heading
    ("Growth Marketing Specialist, Acme (Aug 2019 - May 2020)")."""
    return story.title + "\n" + story.body


def stated_years(story: Story) -> list[str]:
    """Every year the story states, in its heading or its body."""
    return sorted(set(_YEAR.findall(_stated_text(story))))


@dataclass(frozen=True)
class StatedPeriod:
    """A period the story states for itself: a month-year or year range, "Present" open."""
    start: str
    """``YYYY-MM`` when a month is stated, else ``YYYY``."""
    end: str | None
    """``YYYY-MM`` or ``YYYY``; None when the period is open ("Present")."""
    source: str
    """``heading`` or ``body``."""

    @property
    def label(self) -> str:
        """``2019-08 to 2020-05``, ``2025-06 to present``; a years-only range keeps the
        ``2019\u20132023`` form of earlier rounds."""
        if self.end is None:
            return f"{self.start} to present"
        if "-" not in self.start and "-" not in self.end:
            return self.start if self.start == self.end else f"{self.start}\u2013{self.end}"
        return f"{self.start} to {self.end}"

    def months(self, today: date) -> tuple[int, int]:
        """Inclusive month indices; an open period runs to ``today``."""
        first = _month_index(self.start, end=False)
        last = _month_index(self.end, end=True) if self.end else today.year * 12 + today.month - 1
        assert first is not None and last is not None
        return first, last


def _month_index(value: str | None, *, end: bool) -> int | None:
    """``YYYY-MM`` or ``YYYY`` as a month index; a bare year starts in January and ends
    in December."""
    match = re.match(r"^(\d{4})(?:-(\d{2}))?$", value or "")
    if not match:
        return None
    return int(match.group(1)) * 12 + (int(match.group(2)) if match.group(2) else (12 if end else 1)) - 1


def _month_of(name: str | None, number: str | None, iso: str | None) -> int | None:
    if name:
        return _MONTH_NUMBERS[name.casefold()[:3]]
    if number or iso:
        return int(number or iso or "0")
    return None


def _period_value(year: str, month: int | None) -> str:
    return f"{year}-{month:02d}" if month else year


def stated_periods(story: Story) -> list[StatedPeriod]:
    """The periods the story states, its heading's first: month-year ranges ("Aug 2019 -
    May 2020", "Jun 2025 - Present") and year ranges ("2019 to 2023"); a heading without a
    range that names one year, or adjacent years, states that year or span ("Growth
    Marketer, Acme (2019)"). A range whose end comes before its start is not a period, and
    a year in the body is never one by itself."""
    periods: list[StatedPeriod] = []
    for source, text in (("heading", story.title), ("body", story.body)):
        found: list[StatedPeriod] = []
        for match in _PERIOD_RANGE.finditer(text):
            start = _period_value(match.group("start_year"), _month_of(
                match.group("start_name"), match.group("start_number"), match.group("start_iso")))
            end = None if match.group("open") else _period_value(match.group("end_year"), _month_of(
                match.group("end_name"), match.group("end_number"), match.group("end_iso")))
            first, last = _month_index(start, end=False), _month_index(end, end=True)
            if end is not None and (first is None or last is None or last < first):
                continue
            found.append(StatedPeriod(start, end, source))
        if source == "heading" and not found:
            years = sorted({int(year) for year in _YEAR.findall(text)})
            if years and all(later - earlier <= 1 for earlier, later in pairwise(years)):
                found.append(StatedPeriod(str(years[0]), str(years[-1]), source))
        for period in found:
            if all((p.start, p.end) != (period.start, period.end) for p in periods):
                periods.append(period)
    return periods


def story_period(story: Story) -> StatedPeriod | None:
    """The period the story states for itself: the range, year or adjacent years in its
    heading ("Growth Marketing Specialist, Acme (Aug 2019 - May 2020)"), where a story or a
    profile entry states its dates. It is the period that can reject a resume role link
    (``link_period_mismatch``); a range in the body may date only a part of the work (a
    pilot, the years before), so it never does."""
    heading = [period for period in stated_periods(story) if period.source == "heading"]
    return heading[0] if heading else None


def dating_period(story: Story) -> StatedPeriod | None:
    """The period an unlinked story's facts and headers carry when it states one: its
    heading's range (``story_period``), else the one range its body states. Several
    different ranges in the body date parts of the story, not the whole of it: none."""
    own = story_period(story)
    if own is not None:
        return own
    periods = stated_periods(story)
    return periods[0] if len(periods) == 1 else None


def stated_year_span(story: Story) -> str | None:
    """The years an unlinked story's facts may carry: its one stated year; the span of
    its stated years when they are adjacent (2022, 2023, 2024) or the story states an
    explicit range ("2019 to 2023", "2019\u20132023"); otherwise none, because years
    stated apart ("in 2019 ... by 2023") do not date every sentence in between. The
    heading counts like the body."""
    years = stated_years(story)
    if not years:
        return None
    if len(years) == 1:
        return years[0]
    ordered = [int(year) for year in years]
    adjacent = all(later - earlier <= 1 for earlier, later in pairwise(ordered))
    explicit = any(int(match.group(1)) <= int(match.group(2))
                   for match in _YEAR_RANGE.finditer(_stated_text(story)))
    if adjacent or explicit:
        return f"{years[0]}\u2013{years[-1]}"
    return None


def period_overlaps_role(period: StatedPeriod, link: StoryRoleLink, today: date) -> bool | None:
    """Whether a stated period shares at least one month with the linked resume role's
    dates; None when the role is undated. An open role runs to today, a role with an
    unknown end is never ruled out by its end."""
    role_first = _month_index(link.start, end=False)
    if role_first is None:
        return None
    if link.end:
        role_last = _month_index(link.end, end=True)
        if role_last is None:
            return None
    else:
        role_last = today.year * 12 + today.month - 1 if link.current else 10 ** 6
    first, last = period.months(today)
    return first <= role_last and role_first <= last


def link_period_mismatch(story: Story, link: StoryRoleLink | None, today: date) -> dict[str, Any] | None:
    """A link the story's own stated period contradicts: the story's heading states its
    period (``story_period``) and it shares no month with the linked role's dates. Such a
    story is not that role's account; it stays unlinked and keeps its stated period. A
    year the body mentions is not a period, and a range in the body may date only a part
    of the work: neither rejects a link (round 2: the resume dates win and a sentence
    stating a year outside them yields no fact)."""
    if link is None:
        return None
    period = story_period(story)
    if period is None or period_overlaps_role(period, link, today) is not False:
        return None
    return {"story_id": link.story_id, "resume_role_id": link.resume_role_id, "method": link.method,
            "company": link.company, "title": link.title, "role_period": link.period,
            "stated_period": period.label, "stated_in": period.source,
            "reason": "stated_period_outside_resume_role"}


def resolve_period(story: Story, link: StoryRoleLink | None) -> tuple[str | None, str, bool]:
    """The period a story's facts and headers carry, where it comes from, and whether
    the story states a year outside the linked role's dates.

    A linked story carries the resume role's dates (the canonical profile is
    authoritative); an unlinked story carries the period it states (``dating_period``: its
    heading's range, else its body's one range), else its one stated year or the span of
    adjacent or explicitly ranged years (``stated_year_span``); otherwise none. Never a
    default year. ``build_story_index`` never passes a link whose role the story's own
    period contradicts (``link_period_mismatch``)."""
    years = stated_years(story)
    if link is not None and link.period:
        discrepancy = any(not link.contains_year(year) for year in years)
        return link.period, "resume_role", discrepancy
    own = dating_period(story)
    if own is not None:
        return own.label, "story", False
    span = stated_year_span(story)
    if span is not None:
        return span, "story", False
    return None, "none", False


# --- chunks -------------------------------------------------------------------------------


def _clean_field(value: str) -> str:
    return _normalize(value).replace("|", "/")


def chunk_header(analysis: StoryAnalysis, link: StoryRoleLink | None = None,
                 period: str | None = None) -> str:
    """The first line of every chunk: title, employer or project, resume role when
    linked, the resolved period (``period``; the story's own when None) and themes."""
    parts = [f"Story {analysis.number:02d}: {_clean_field(analysis.title)}"]
    if analysis.employer:
        parts.append("employer: " + _clean_field(analysis.employer))
    elif analysis.project:
        parts.append("project: " + _clean_field(analysis.project))
    if link is not None:
        parts.append("resume role: " + _clean_field(f"{link.title}, {link.company}"))
    if analysis.role:
        parts.append("role: " + _clean_field(analysis.role))
    resolved = period if period is not None else analysis.period
    if resolved:
        parts.append("period: " + _clean_field(resolved))
    if analysis.themes:
        parts.append("themes: " + ", ".join(analysis.themes))
    return " | ".join(parts)


_HEADER = re.compile(r"^Story (\d{1,3}): (?P<title>[^|\n]*?)(?P<rest>(?: \| [a-z ]+: [^|\n]*)*)$")


def parse_chunk_header(text: str) -> dict[str, Any] | None:
    """The metadata a chunk carries in its first line, or None for a foreign body."""
    first = text.split("\n", 1)[0]
    match = _HEADER.match(first)
    if not match:
        return None
    fields: dict[str, Any] = {
        "number": int(match.group(1)), "title": match.group("title").strip(),
        "employer": None, "project": None, "resume_role": None, "role": None, "period": None,
        "themes": []}
    for item in match.group("rest").split(" | ")[1:]:
        name, _, value = item.partition(": ")
        name = name.replace(" ", "_")
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


def _chunk(analysis: StoryAnalysis, number: int, kind: str, text: str, *,
           period: str | None, link: StoryRoleLink | None) -> StoryChunk:
    text = text.strip()
    if len(text) > MAX_CHUNK_CHARS + 50:
        raise StoryParseError("A story chunk exceeds the store's size bound")
    return StoryChunk(id="story:" + _sha256(text), story_id=analysis.story_id, number=number,
                      kind=kind, title=analysis.title, employer=analysis.context_label,
                      period=period, themes=analysis.themes, text=text,
                      resume_role_id=link.resume_role_id if link else None)


def chunk_story(story: Story, analysis: StoryAnalysis,
                link: StoryRoleLink | None = None) -> list[StoryChunk]:
    """One summary chunk, then sections of about 120-300 words, each prefixed with the
    story's header line so it stands alone. Chunk ids are content hashes. A linked story
    carries the resume role and its dates in the header and the summary."""
    period, _, _ = resolve_period(story, link)
    header = chunk_header(analysis, link, period)
    header_words, header_chars = len(header.split()), len(header) + 1
    max_words = max(MAX_CHUNK_WORDS - header_words, 60)
    max_chars = MAX_CHUNK_CHARS - header_chars
    summary_parts = [f"Summary of story {analysis.number:02d}."]
    where = analysis.context_label
    if link is not None:
        where = f"{where} ({link.company})" if where else link.company
    dated = f" ({period})." if period else "."
    if analysis.role and where:
        summary_parts.append(f"Role: {analysis.role} at {where}" + dated)
    elif where:
        summary_parts.append(f"Context: {where}" + dated)
    if link is not None:
        summary_parts.append(f"The resume lists this role as {link.title} at {link.company}"
                             + (f", {link.period}." if link.period else "."))
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
    chunks = [_chunk(analysis, 0, "summary", header + "\n" + summary, period=period, link=link)]
    for index, group in enumerate(_pack(story.sentences, max_words=max_words, max_chars=max_chars), 1):
        chunks.append(_chunk(analysis, index, "section", header + "\n" + " ".join(group),
                             period=period, link=link))
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


def fact_context(analysis: StoryAnalysis, period: str | None,
                 link: StoryRoleLink | None) -> str | None:
    """What a story fact's value ends with, in parentheses: the story's employer phrase,
    the linked resume role and the resolved period; nothing invented."""
    parts: list[str] = []
    if analysis.employer:
        parts.append(analysis.employer)
    if link is not None:
        parts.append(f"resume: {link.company}" + (f", {period}" if period else ""))
    elif period:
        if parts:
            parts[-1] += ", " + period
        else:
            parts.append(period)
    return "; ".join(parts) if parts else None


def extract_story_facts(story: Story, analysis: StoryAnalysis, chunks: Sequence[StoryChunk], *,
                        verified_at: datetime, link: StoryRoleLink | None = None,
                        source_id: str = DEFAULT_STORY_SOURCE) -> tuple[list[CandidateFact], list[dict[str, Any]]]:
    """Atomic, user-authored facts from a story's concrete first-person singular
    sentences, each with ``story:<chunk id>`` provenance and UNVERIFIED until the person
    confirms it through the facts import. Values are the sentences as written (numbers
    are never changed) plus the story's employer and resolved period; a vague sentence
    or one about "we"/"our" work yields nothing (the chunks keep it as story evidence).
    A linked story's facts carry the resume role's dates and record ``resume_role_id`` in
    their evidence; an unlinked story's facts carry a year only when the story states one
    (or an adjacent or explicit span). ``verified_at`` is the run time, used for the
    tenure checks. Returns the facts and the sentences skipped, with the reason."""
    facts: list[CandidateFact] = []
    skipped: list[dict[str, Any]] = []
    body = story.body
    body_numbers = numbers_in(body)
    period, period_source, discrepancy = resolve_period(story, link)
    conflicting_years = ([year for year in stated_years(story) if link is not None and not link.contains_year(year)]
                         if discrepancy else [])
    context = fact_context(analysis, period, link)
    provenance_lines = [f"period_source: {period_source}", f"story_source: {source_id}"]
    if link is not None:
        provenance_lines.append(f"resume_role_id: {link.resume_role_id}")
    # Extracted, not confirmed: the person confirms a story fact through the facts import
    # (CONTRACTS 3: VERIFIED means the person stated or confirmed it, never a run clock).
    verification = FactVerification(status=VerificationStatus.UNVERIFIED)
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
        if link is not None:
            value += f" ({link.company}" + (f", {period})" if period else ")")
        elif period:
            value += f" ({period})"
        add("employment", value, provenance(role_sentence), [label, *provenance_lines, role_sentence])
    for sentence in story.sentences:
        tools = find_tools(sentence)
        key = _fact_key(sentence, tools)
        if key is None:
            if _PLURAL_PERSON.search(sentence) and (
                    _ACTION.search(sentence) or tools
                    or (re.search(r"\d", sentence) and _RESULT.search(sentence))):
                # "We grew ARR 3x": the team's work, story evidence in the chunks only;
                # counted so the receipt shows what the singular rule left out.
                skipped.append({"story": analysis.number, "reason": "plural_subject_only",
                                "chars": len(sentence), "key": None})
            continue
        if len(sentence) > MAX_FACT_CHARS:
            skipped.append({"story": analysis.number, "reason": "sentence_too_long",
                            "chars": len(sentence), "key": key})
            continue
        if any(re.search(rf"\b{year}\b", sentence) for year in conflicting_years):
            # The sentence dates the work to a year the linked resume role contradicts: a
            # fact quoting it next to the resume dates would contradict itself. The story
            # chunk keeps the sentence; the review names it for the person to correct.
            skipped.append({"story": analysis.number, "reason": "stated_year_conflicts_with_resume_role",
                            "chars": len(sentence), "key": key})
            continue
        if tenure_conflicts(sentence, link, verified_at.date()):
            # The sentence claims a tenure or duration longer than the resume dates the
            # role: the resume is canonical, so the sentence yields no fact and the review
            # names it for the person.
            skipped.append({"story": analysis.number, "reason": "stated_duration_conflicts_with_resume_role",
                            "chars": len(sentence), "key": key})
            continue
        value = sentence if not context else f"{sentence} ({context})"
        evidence = [label, *provenance_lines] + (["Tools: " + ", ".join(tools)] if tools else [])
        add(key, value, provenance(sentence), evidence)
    return facts, skipped


# --- whole document -----------------------------------------------------------------------


def read_stories(path: Path) -> StoryDocument:
    """The document's stories and their analyses: the first phase, before any linking."""
    stories = parse_stories(read_document(path))
    data = path.read_bytes()
    return StoryDocument(hashlib.sha256(data).hexdigest(), len(data), tuple(stories),
                         tuple(analyse_story(story) for story in stories))


def build_story_index(source: Path | StoryDocument, *, verified_at: datetime,
                      links: Mapping[str, StoryRoleLink] | None = None,
                      source_id: str = DEFAULT_STORY_SOURCE) -> StoryIndex:
    """Stories, analyses, chunks and facts of one document; no text leaves in receipts.
    ``links`` (by story id) date the linked stories' chunks and facts with the resume,
    except a link the story's own stated period contradicts: that story stays unlinked,
    keeps its stated period and the mismatch is recorded for the review
    (``link_period_mismatch``; ``verified_at`` is today for an open period or role).
    ``source_id`` names the stories source the facts belong to (``story_source``)."""
    document = read_stories(source) if isinstance(source, Path) else source
    chunks: list[StoryChunk] = []
    facts: list[CandidateFact] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()
    kept: dict[str, StoryRoleLink] = {}
    mismatches: dict[str, dict[str, Any]] = {}
    for story, analysis in zip(document.stories, document.analyses, strict=True):
        link = (links or {}).get(analysis.story_id)
        mismatch = link_period_mismatch(story, link, verified_at.date())
        if mismatch is not None:
            mismatches[analysis.story_id] = mismatch
            link = None
        elif link is not None:
            kept[analysis.story_id] = link
        story_chunks = chunk_story(story, analysis, link)
        story_facts, story_skipped = extract_story_facts(story, analysis, story_chunks,
                                                         verified_at=verified_at, link=link,
                                                         source_id=source_id)
        chunks.extend(chunk for chunk in story_chunks if chunk.id not in seen)
        seen.update(chunk.id for chunk in story_chunks)
        facts.extend(fact for fact in story_facts if fact.id not in {f.id for f in facts})
        skipped.extend(story_skipped)
    return StoryIndex(document.file_sha256, document.file_bytes, document.stories,
                      document.analyses, tuple(chunks), tuple(facts), tuple(skipped),
                      kept, source_id, mismatches)


def story_index_receipt(index: StoryIndex) -> dict[str, Any]:
    """Counts, ids and hashes only; never titles, chunk text or fact values."""
    by_key: dict[str, int] = {}
    for fact in index.facts:
        by_key[fact.key] = by_key.get(fact.key, 0) + 1
    periods: dict[str, int] = {}
    discrepancies = 0
    story_rows = []
    for story, analysis in zip(index.stories, index.analyses, strict=True):
        link = index.link_for(analysis.story_id)
        mismatch = index.mismatch_for(analysis.story_id)
        period, period_source, discrepancy = resolve_period(story, link)
        discrepancies += int(discrepancy)
        own = dating_period(story)
        fact_count = sum(1 for f in index.facts if f.id.startswith(f"sf_{analysis.story_id}_"))
        periods[period_source] = periods.get(period_source, 0) + fact_count
        story_rows.append({
            "story_id": analysis.story_id, "number": analysis.number,
            "title_sha256": _sha256(story.title), "word_count": story.word_count,
            "sentence_count": len(story.sentences),
            "chunk_count": sum(1 for c in index.chunks if c.story_id == analysis.story_id),
            "fact_count": fact_count,
            "has_employer": analysis.employer is not None, "has_project": analysis.project is not None,
            "has_role": analysis.role is not None,
            "stated_year_count": len(stated_years(story)),
            "stated_period_in": own.source if own else None, "period_source": period_source,
            "period_set": period is not None, "stated_year_outside_resume_role": discrepancy,
            "link": ({"method": link.method, "resume_role_id": link.resume_role_id,
                      "confidence": link.confidence, "probability": link.probability}
                     if link else None),
            "link_rejected": ({"method": mismatch["method"], "resume_role_id": mismatch["resume_role_id"],
                               "reason": mismatch["reason"]} if mismatch else None),
            "situation_count": len(analysis.situation), "action_count": len(analysis.actions),
            "outcome_count": len(analysis.outcomes), "tool_count": len(analysis.tools),
            "skill_count": len(analysis.skills), "themes": list(analysis.themes),
        })
    return {
        "file_sha256": index.file_sha256, "file_bytes": index.file_bytes,
        "story_count": len(index.stories),
        "stories": story_rows,
        "facts_by_period_source": dict(sorted(periods.items())),
        "stated_year_discrepancies": discrepancies,
        "linked_stories": sum(1 for row in story_rows if row["link"]),
        "link_period_mismatches": sum(1 for row in story_rows if row["link_rejected"]),
        "chunk_count": len(index.chunks),
        "chunk_ids": [chunk.id for chunk in index.chunks],
        "chunk_words": [chunk.word_count for chunk in index.chunks],
        "fact_count": len(index.facts), "facts_by_key": dict(sorted(by_key.items())),
        "fact_ids": [fact.id for fact in index.facts],
        "skipped": list(index.skipped),
    }


def story_source_of(fact: CandidateFact) -> str | None:
    """The stories source a story fact belongs to (its ``story_source`` evidence line);
    the default source for a story fact that predates the line; None for other facts."""
    if not fact.source.startswith("story:"):
        return None
    for line in fact.evidence:
        if line.startswith("story_source: "):
            return line[len("story_source: "):]
    return DEFAULT_STORY_SOURCE


def facts_review(index: StoryIndex, *, candidate_id: str,
                 roles: Sequence[ResumeRole] = (), today: date | None = None) -> dict[str, Any]:
    """The full fact list for the user's private review (contains story text)."""
    stories = []
    for story, a in zip(index.stories, index.analyses, strict=True):
        link = index.link_for(a.story_id)
        mismatch = index.mismatch_for(a.story_id)
        period, period_source, discrepancy = resolve_period(story, link)
        own = dating_period(story)
        differences = resume_quantity_differences(story, link, roles)
        durations = [conflict for sentence in story.sentences
                     for conflict in tenure_conflicts(sentence, link, today or date.today())]
        notes = []
        if mismatch is not None:
            notes.append(f"The story states {mismatch['stated_period']} (in its {mismatch['stated_in']}), "
                         f"which does not overlap the resume role it was matched to "
                         f"({mismatch['title']} at {mismatch['company']}, {mismatch['role_period']}, "
                         f"by {mismatch['method']}); it was not linked and carries its stated period. "
                         "Correct the story or the resume if one is wrong.")
        for conflict in durations:
            notes.append(f"The story says \"{conflict['phrase']}\" (at least {conflict['minimum_months']} months) "
                         f"but the linked resume role lasted {conflict['tenure_months']} months; the sentence "
                         "yields no fact. Correct the story or the resume if one is wrong.")
        if discrepancy:
            notes.append("The story states a year outside the linked resume role's dates; the resume "
                         "dates were used and the sentence stating the year yields no fact. Correct "
                         "the story or the resume if one is wrong.")
        for difference in differences:
            notes.append(f"The story states a {difference['kind']} of {difference['story']} but the "
                         f"resume's linked role states {difference['resume']}; not resolved here, "
                         "settle it in the story or the resume.")
        stories.append({
            "number": a.number, "story_id": a.story_id, "title": a.title,
            "employer": a.employer, "project": a.project, "role": a.role,
            "stated_years": stated_years(story),
            "stated_period": ({"label": own.label, "start": own.start, "end": own.end, "in": own.source}
                              if own else None),
            "period": period, "period_source": period_source,
            "resume_role": ({"id": link.resume_role_id, "company": link.company, "title": link.title,
                             "start": link.start, "end": link.end, "current": link.current,
                             "method": link.method, "confidence": link.confidence,
                             "probability": link.probability} if link else None),
            "link_rejected": dict(mismatch) if mismatch else None,
            "stated_year_outside_resume_role": discrepancy,
            "resume_differences": differences,
            "duration_conflicts": durations,
            "note": " ".join(notes) if notes else None,
            "tools": list(a.tools), "skills": list(a.skills),
            "themes": list(a.themes), "outcomes": list(a.outcomes)})
    return {
        "candidate_sha256": _sha256(candidate_id), "file_sha256": index.file_sha256,
        "source_id": index.source_id,
        "facts": [{"id": fact.id, "key": fact.key, "value": fact.value, "source": fact.source,
                   "evidence": list(fact.evidence),
                   "verification": fact.verification.model_dump(mode="json")}
                  for fact in index.facts],
        "stories": stories,
        "skipped": list(index.skipped),
    }


CONFIRM_FILE_NOTE = ("Delete the facts you do not confirm; import the rest with "
                     "scripts/rag_answers.py import-facts --file <this file>, then index-profile")


def confirmable_facts(facts: Sequence[CandidateFact]) -> list[dict[str, Any]]:
    """The facts a review offers for confirmation, in the facts-import format (id, key,
    value, evidence; the import records the person's confirmation and its own
    provenance). Only unverified facts are listed; confirmed ones need nothing."""
    return [{"id": fact.id, "key": fact.key, "value": fact.value, "evidence": list(fact.evidence)}
            for fact in facts if not fact.is_verified]


def facts_review_markdown(review: Mapping[str, Any],
                          derived: Sequence[CandidateFact] = ()) -> str:
    """A readable version of the review for the person: stories with their link and
    period, then every fact, then the derived years facts."""
    lines = ["# Story facts for review", "", f"Source: `{review.get('source_id', DEFAULT_STORY_SOURCE)}`", ""]
    for story in review["stories"]:
        role = story["resume_role"]
        lines.append(f"## Story {story['number']:02d}: {story['title']}")
        lines.append(f"- employer/project: {story['employer'] or story['project'] or 'not stated'}"
                     f"; role: {story['role'] or 'not stated'}; years the story states: "
                     f"{', '.join(story['stated_years']) or 'none'}")
        if story.get("stated_period"):
            lines.append(f"- period the story states: {story['stated_period']['label']} "
                         f"(in its {story['stated_period']['in']})")
        if story.get("link_rejected"):
            rejected = story["link_rejected"]
            lines.append(f"- not linked: {rejected['title']} at {rejected['company']}, "
                         f"{rejected['role_period']} (proposed by {rejected['method']}) does not overlap "
                         f"{rejected['stated_period']}")
        if role:
            lines.append(f"- resume role: {role['title']} at {role['company']}, "
                         f"{role['start'] or '?'} to {role['end'] or ('present' if role['current'] else '?')}"
                         f" (link by {role['method']}, confidence {role['confidence']:.2f}, "
                         f"probability {role['probability']:.2f})")
        lines.append(f"- period used: {story['period'] or 'none'} ({story['period_source']})")
        if story.get("note"):
            lines.append(f"- **check:** {story['note']}")
        lines.append("")
    lines += ["## Facts", "",
              "Every story fact is UNVERIFIED until you confirm it: keep the rows you confirm in the "
              "`.confirm.json` written beside this file, delete the rest, then run "
              "`uv run --no-sync python scripts/rag_answers.py import-facts --file <that file>` and "
              "`index-profile`. Unconfirmed facts are never used to answer a form.", "",
              "| id | key | value | provenance | status |", "|---|---|---|---|---|"]
    for fact in review["facts"]:
        value = str(fact["value"]).replace("|", "/")
        status = fact.get("verification", {}).get("status", "UNVERIFIED")
        lines.append(f"| `{fact['id']}` | {fact['key']} | {value} | `{fact['source']}` | {status} |")
    if derived:
        lines += ["", "## Derived years of experience (resume timeline)", "",
                  "The total across dated roles is verified (it only restates the confirmed role "
                  "dates); each per-area fact is UNVERIFIED until you confirm it the same way.", "",
                  "| id | key | years | status | basis |", "|---|---|---|---|---|"]
        for fact in derived:
            basis = "; ".join(fact.evidence).replace("|", "/")
            lines.append(f"| `{fact.id}` | {fact.key} | {fact.value} | {fact.verification.status.value} | {basis} |")
    lines.append("")
    return "\n".join(lines)


def chunk_metadata(chunk: StoryChunk) -> dict[str, Any]:
    return {"id": chunk.id, "story_id": chunk.story_id, "number": chunk.number, "kind": chunk.kind,
            "words": chunk.word_count, "chars": len(chunk.text)}


def dumps_receipt(receipt: dict[str, Any]) -> str:
    return json.dumps(receipt, sort_keys=True)


__all__ = [
    "CONFIRM_FILE_NOTE",
    "DEFAULT_STORY_SOURCE",
    "MAX_CHUNK_CHARS",
    "MAX_CHUNK_WORDS",
    "MAX_TITLE_CHARS",
    "MIN_CHUNK_WORDS",
    "STORY_CHUNK_ID",
    "Paragraph",
    "ResumeRole",
    "Run",
    "StatedPeriod",
    "Story",
    "StoryAnalysis",
    "StoryChunk",
    "StoryDocument",
    "StoryIndex",
    "StoryParseError",
    "StoryRoleLink",
    "analyse_story",
    "build_story_index",
    "chunk_header",
    "chunk_metadata",
    "chunk_story",
    "company_tokens",
    "company_words",
    "confirmable_facts",
    "dating_period",
    "duration_claims",
    "extract_story_facts",
    "fact_context",
    "facts_review",
    "facts_review_markdown",
    "find_skills",
    "find_themes",
    "find_tools",
    "link_period_mismatch",
    "match_role_by_name",
    "names_company",
    "numbers_in",
    "parse_chunk_header",
    "parse_stories",
    "period_overlaps_role",
    "read_document",
    "read_docx",
    "read_stories",
    "read_text_document",
    "resolve_period",
    "resume_quantity_differences",
    "resume_roles",
    "role_tenure_months",
    "split_sentences",
    "stated_periods",
    "stated_year_span",
    "stated_years",
    "story_index_receipt",
    "story_period",
    "story_source_of",
    "team_sizes",
    "tenure_conflicts",
]
