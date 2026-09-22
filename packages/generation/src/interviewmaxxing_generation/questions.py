"""Question wording: when an answer given for one question may answer another.

Reuse is bound to the complete question the user sees, never to semantic
similarity. A field's question is its label, help text and placeholder
(``ApplicationField.fingerprint`` covers the same text). Comparison (``wording_key``)
ignores only case, whitespace, sentence punctuation, free-standing dashes and
trailing required/optional markers. Symbols that carry meaning (``< > $ € %`` ...)
and punctuation inside numbers are kept.

A saved answer's ``question`` (or one of its ``match_phrases``) matches a field when
it equals either

* the field's full question: label, help text and placeholder joined by spaces, or
* the field's label alone, when the field has no help text and its placeholder is a
  neutral format hint (``"Select..."``, ``"e.g. 5"``) that adds no meaning. Units,
  currency symbols and scales in a placeholder are never neutral.

"Will you require visa sponsorship?" therefore does not answer "Will you require
sponsorship?", and an "I agree" answer does not carry over to an "I agree" checkbox
whose help text states a different attestation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from interviewmaxxing_core import ApplicationField, SavedAnswer, normalize_text

_TRAILING_MARKERS = (
    re.compile(r"\s*\*+$"),
    re.compile(r"\s*\((?:required|optional)\)$"),
    re.compile(r"[\s?.:!]+$"),
)

_NEUTRAL_HINT_WORDS = frozenset(
    {
        "a", "an", "answer", "choose", "dd", "e", "eg", "enter", "ex", "example", "g",
        "here", "mm", "number", "one", "option", "please", "response", "select",
        "type", "value", "yy", "yyyy", "your",
    }
)
"""Words a placeholder may contain and still be a pure format hint. Units (years,
hours, USD, per ...) are deliberately absent: they change what is being asked."""

_HINT_FORMAT_SYMBOLS = frozenset("./-:\u2026")
"""Symbols allowed in a neutral hint ("Select...", "MM/YYYY", "e.g. 5")."""

_EXAMPLE_MARKER = re.compile(r"\b(?:e\.?\s?g|eg|ex|example)\b")

_UNIFORM_DASHES = str.maketrans(dict.fromkeys(
    "\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"))
"""Hyphen, figure/en/em dashes and minus sign all read as ``-``."""
_UNIFORM_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})

_SEPARATOR_PUNCTUATION = re.compile("[;!?'\"`()\\[\\]{}\u00bf\u00a1\u2026]")
"""Sentence punctuation that never changes what a question asks."""
_NUMBER_PUNCTUATION = re.compile(r"(?<!\d)[.,:]|[.,:](?!\d)")
"""``.``/``,``/``:`` except between digits, where 1.5, 100,000 and 10:30 keep them."""
_FREE_DASH = re.compile(r"(?<!\S)-+(?!\S)")
"""A dash standing alone between spaces is a separator ("I agree - I certify")."""


def question_key(text: str | None) -> str:
    """Comparison form of question or option text: ``normalize_text`` without
    trailing punctuation or required/optional markers. Nothing else is dropped."""
    key = normalize_text(text or "")
    previous = None
    while previous != key:
        previous = key
        for pattern in _TRAILING_MARKERS:
            key = pattern.sub("", key)
    return key


def wording_key(text: str | None) -> str:
    """Comparison form of question wording.

    Case, whitespace, sentence punctuation (``. , ; : ! ? ' " ( ) [ ]``), dashes that
    stand alone between spaces and trailing required markers are ignored. Everything
    that can carry meaning is kept: comparison and currency symbols (``< > = $ € £``),
    ``% + # & / @``, attached hyphens, and punctuation inside numbers (``1.5``,
    ``100,000``). "budget > $100,000" is therefore not "budget < $100,000", and
    "C++" is not "C"."""
    key = question_key(text).translate(_UNIFORM_DASHES).translate(_UNIFORM_QUOTES)
    key = _SEPARATOR_PUNCTUATION.sub(" ", key)
    key = _NUMBER_PUNCTUATION.sub(" ", key)
    key = _FREE_DASH.sub(" ", key)
    return " ".join(key.split())


def is_neutral_hint(text: str | None) -> bool:
    """True when a placeholder only hints at format and so cannot change what a
    question asks: empty, ``"Select..."``, ``"Your answer"``, ``"MM/YYYY"`` or an
    example number such as ``"e.g. 5"``.

    Any other symbol (``$``, ``€``, ``%``, ``<``, ``+`` ...), any unit or other word,
    and bare numbers or scales (``"1-5"``) make the placeholder part of the
    question."""
    hint = normalize_text(text or "").translate(_UNIFORM_DASHES)
    tokens = re.findall(r"[a-z]+|\d+|\S", hint)
    words = [t for t in tokens if t.isascii() and t.isalpha()]
    symbols = [t for t in tokens if not t.isalnum()]
    has_digits = any(t.isdigit() for t in tokens)
    if len(words) + len(symbols) + sum(t.isdigit() for t in tokens) != len(tokens):
        return False  # non-ASCII letters: a word we cannot vouch for
    if any(s not in _HINT_FORMAT_SYMBOLS for s in symbols):
        return False
    if any(w not in _NEUTRAL_HINT_WORDS for w in words):
        return False
    return not has_digits or bool(_EXAMPLE_MARKER.search(hint))


def display_question(field: ApplicationField) -> str:
    """The complete question as the user sees it, for prompts: the label, then the
    help text, then the placeholder when it carries meaning (a neutral format hint
    such as "Select..." is left out). Empty parts are omitted.

    This is the seam for core's shared full-question renderer (C1R3); matching does
    not depend on this text."""
    parts = [field.label.strip() or field.id]
    if field.help_text and field.help_text.strip():
        parts.append(field.help_text.strip())
    if field.placeholder and field.placeholder.strip() and not is_neutral_hint(field.placeholder):
        parts.append(f"[{field.placeholder.strip()}]")
    return " ".join(parts)


@dataclass(frozen=True, slots=True)
class QuestionText:
    """The complete question a field asks, in comparison form."""

    label: str
    help_text: str
    placeholder: str
    label_is_complete: bool
    """True when the label alone is the whole question (no help text, neutral placeholder)."""

    @classmethod
    def of(cls, field: ApplicationField) -> QuestionText:
        return cls(
            label=wording_key(field.label),
            help_text=wording_key(field.help_text),
            placeholder=wording_key(field.placeholder),
            label_is_complete=not wording_key(field.help_text)
            and is_neutral_hint(field.placeholder),
        )

    @property
    def full(self) -> str:
        return " ".join(p for p in (self.label, self.help_text, self.placeholder) if p)

    @property
    def keys(self) -> frozenset[str]:
        """Question texts a saved answer must equal to answer this field."""
        keys = {self.full}
        if self.label_is_complete:
            keys.add(self.label)
        return frozenset(k for k in keys if k)


def saved_answer_phrases(answer: SavedAnswer) -> frozenset[str]:
    phrases = {wording_key(answer.question), *(wording_key(p) for p in answer.match_phrases)}
    return frozenset(p for p in phrases if p)


def saved_answer_matches(answer: SavedAnswer, question: QuestionText) -> bool:
    """True when the saved answer was given for exactly this question wording."""
    return bool(saved_answer_phrases(answer) & question.keys)


# --- factual questions ---------------------------------------------------------------

_GENERIC_EXPERIENCE_QUALIFIERS = frozenset({"professional", "work", "total", "industry"})

_YEARS_QUESTION = re.compile(
    r"^(?:how many )?(?:total )?years (?:of )?(?:(?P<pre>[^?]+?) )?experience"
    r"(?: (?:in|with) (?P<post>[^?]+?))?(?: do you have)?$"
)


@dataclass(frozen=True, slots=True)
class YearsQuestion:
    """A question asking only for a number of years of experience."""

    area: str | None
    """The qualified area (``"paid search"``), or None for total/professional experience."""


def parse_years_question(question: QuestionText) -> YearsQuestion | None:
    """Recognize "How many years of <area> experience do you have?" and similar.

    Only a label that is the complete question qualifies; help text could narrow
    or change what is asked. Returns None for anything else."""
    if not question.label_is_complete:
        return None
    match = _YEARS_QUESTION.fullmatch(question.label)
    if match is None:
        return None
    pre, post = match.group("pre"), match.group("post")
    if pre and post:
        return None
    area = pre or post
    if area in _GENERIC_EXPERIENCE_QUALIFIERS:
        area = None
    return YearsQuestion(area=area)


GENERIC_YEARS_KEYS = frozenset(
    {"years_experience", "years_experience.total", "years_professional_experience"}
)


def years_fact_area(key: str) -> str | None:
    """The area a years-of-experience fact key covers: ``""`` for total experience,
    ``"paid media"`` for ``years_experience.paid_media``, None if not such a key."""
    if key in GENERIC_YEARS_KEYS:
        return ""
    prefix = "years_experience."
    if key.startswith(prefix) and len(key) > len(prefix):
        return wording_key(key[len(prefix):].replace("_", " "))
    return None


@dataclass(frozen=True, slots=True)
class FactualTemplate:
    """A free-text question that is a direct lookup of verified facts."""

    pattern: re.Pattern[str]
    fact_keys: tuple[str, ...]
    """One verified fact per key is required; ``template`` receives their values in order."""
    template: str


FACTUAL_TEMPLATES: tuple[FactualTemplate, ...] = (
    FactualTemplate(
        re.compile(r"(?:what is )?(?:your )?current (?:job )?title"),
        ("current_title",),
        "{0}",
    ),
    FactualTemplate(
        re.compile(r"(?:what is )?(?:the name of )?(?:your )?current (?:company|employer)(?: name)?"),
        ("current_company",),
        "{0}",
    ),
    FactualTemplate(
        re.compile(r"(?:what is )?(?:your )?current (?:role|position)"),
        ("current_title", "current_company"),
        "{0} at {1}",
    ),
)


def factual_template(question: QuestionText) -> FactualTemplate | None:
    """The template whose pattern is the whole question, if any. Motivation,
    qualification and opinion questions never match."""
    if not question.label_is_complete:
        return None
    return next((t for t in FACTUAL_TEMPLATES if t.pattern.fullmatch(question.label)), None)
