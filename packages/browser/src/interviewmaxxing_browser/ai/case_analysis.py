"""Case-study questions answered from the data the question itself shows.

"Calculate CPA and ROAS for each channel. Based on this information, respond to the above
question" asks for a computation over data the form shows with the question (a table,
figures, text), not for the applicant's history. The resolver recognizes the wording
(``case_analysis_question``), checks that the data was recorded with the field (its wording
and ``section_context``, where the inspector records the content that precedes a question
in its block; ``data_present``), and hands that data to the writer as the only evidence
(``data_evidence``, cited by its ``form:<sha256>`` id; the answer's provenance is
``GENERATED_FROM_QUESTION``). The answer shows its working, and ``check_working`` verifies it
in code: every ``a / b = c`` is computed correctly from inputs the data states (or earlier
results), and no other number appears that the data does not state or the working does not
compute. Nothing here calls a provider (WP12 round 5, addendum item 7).
"""
from __future__ import annotations

import ast
import hashlib
import re
from decimal import Decimal, DivisionByZero, InvalidOperation
from itertools import pairwise
from typing import Any

from interviewmaxxing_core import ApplicationField, question_content_ref

MAX_DATA_CHARS = 12000
"""The question and its recorded context together; a longer recording is not answered."""
MIN_DATA_NUMBERS = 4
"""A computation needs data: fewer numbers than this in the recording means the table or
figures the question refers to were not recorded with it."""
SMALL_COUNT = 12
"""Small whole numbers ("three channels", "2 options") need no source in the data."""

_CASE_WORDING = re.compile(
    r"\b(?:calculate|compute|work\s+out)\b[^.?!]{0,100}?\b(?:for\s+(?:each|every|all)|per\s+(?:channel|campaign|"
    r"ad\s+set|platform|month|week)|by\s+(?:channel|campaign|platform)|of\s+(?:each|every))\b"
    r"|\b(?:based\s+on|using|given|from|with|according\s+to)\s+(?:the|this|these|that|those)\s+(?:above\s+|following\s+|"
    r"attached\s+|provided\s+|given\s+|below\s+)?(?:information|data|table|tables|numbers|figures|results|metrics|chart|"
    r"charts|graph|dataset|data\s*set|scenario|case|report|spreadsheet|screenshot)\b"
    r"|\brespond\s+to\s+the\s+(?:above|following|previous)\b"
    r"|\b(?:analy[sz]e|interpret|review|read)\s+(?:the|this|these)\s+(?:above\s+|following\s+|attached\s+)?(?:data|table|"
    r"numbers|figures|results|metrics|chart|dataset|campaign\s+data)\b"
    r"|\bcase\s+stud(?:y|ies)\b|\bdata\s+(?:reading|interpretation)\b",
    re.IGNORECASE)
_NUMBER = re.compile(r"(?<![\w.])[$€£]?\d[\d,]*(?:\.\d+)?%?")
_TOKEN = re.compile(r"(?<![\w.])[$€£]?\d[\d,]*(?:\.\d+)?%?|[()+*/\u00f7\u00d7\u2212-]|(?<=\s)x(?=\s)")


def case_analysis_question(text: str) -> bool:
    """A question asking to calculate, analyse or respond to data given with it."""
    return _CASE_WORDING.search(text) is not None


def case_data(field: ApplicationField) -> str:
    """What the question shows: its recorded section context (headings and, once the
    inspector records it, the tables and text that precede it) and its full wording."""
    return "\n".join([*field.section_context, field.question_text]).strip()


def _value(raw: str) -> tuple[Decimal, int, bool]:
    """A written number as its value, its decimal places and whether it is a percentage."""
    text = raw.strip().lstrip("$€£").replace(",", "")
    percent = text.endswith("%")
    text = text.rstrip("%")
    places = len(text.split(".", 1)[1]) if "." in text else 0
    return Decimal(text), places, percent


def data_numbers(text: str) -> list[Decimal]:
    return [_value(match.group(0))[0] for match in _NUMBER.finditer(text)]


def data_present(field: ApplicationField) -> bool:
    """Whether the recording carries data to compute from (at least ``MIN_DATA_NUMBERS``
    numbers) and fits the bound."""
    data = case_data(field)
    return len(data) <= MAX_DATA_CHARS and len(data_numbers(data)) >= MIN_DATA_NUMBERS


def data_evidence(field: ApplicationField, form_url: str) -> dict[str, str]:
    """The question's data as the writer's only evidence entry, cited by its content id."""
    data = case_data(field)
    return {"id": question_content_ref(field), "text": data, "source_url": form_url,
            "source_version": hashlib.sha256(data.encode("utf-8")).hexdigest()}


def _evaluate(node: ast.AST) -> Decimal:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return Decimal(str(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_evaluate(node.operand)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        left, right = _evaluate(node.left), _evaluate(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        return left / right
    raise ValueError("not arithmetic")


def _arithmetic(tokens: list[str]) -> tuple[Decimal, list[str]] | None:
    """The value of tokens that form arithmetic over at least two numbers with at least one
    operator, and the numbers used; None when they do not."""
    numbers = [token for token in tokens if token[0].isdigit() or token[0] in "$€£"]
    if len(numbers) < 2 or not any(token in ("+", "-", "*", "/", "\u00f7", "\u00d7", "x") for token in tokens):
        return None
    expression = []
    for token in tokens:
        if token == "\u00f7":
            expression.append("/")
        elif token in ("\u00d7", "x"):
            expression.append("*")
        elif token in ("+", "-", "*", "/", "(", ")"):
            expression.append(token)
        else:
            value, _, percent = _value(token)
            expression.append(str(value / 100 if percent else value))
    try:
        return _evaluate(ast.parse(" ".join(expression), mode="eval")), numbers
    except (SyntaxError, ValueError, ZeroDivisionError, DivisionByZero, InvalidOperation):
        return None


def _matches(value: Decimal, shown: Decimal, places: int, percent: bool) -> bool:
    """Whether a computed value is what a number shown with ``places`` decimals states
    (a percentage may show a ratio times 100)."""
    tolerance = Decimal(5) / (Decimal(10) ** (places + 1)) + Decimal("1e-9")
    candidates = [value * 100, value] if percent else [value]
    return any(abs(candidate - shown) <= tolerance for candidate in candidates)


def _known(shown: Decimal, places: int, percent: bool, known: list[Decimal]) -> bool:
    return any(_matches(value, shown, places, percent) or _matches(value, shown, places, False)
               for value in known)


def check_working(text: str, data: str) -> str | None:
    """Why the shown working does not check out, or None. Every ``… = c`` whose left side
    is arithmetic over numbers must compute ``c`` from numbers the data states or earlier
    results; every other number must be stated in the data, be such a result (as shown or
    rounded), or be a small count."""
    known = data_numbers(data)
    stated = list(known)
    for clause in re.split(r"(?<=[.;!?])\s+|\n+", text.replace("\u2212", "-")):
        parts = clause.split("=")
        for left, right in pairwise(parts):
            tokens = [match.group(0) for match in _TOKEN.finditer(left)]
            result = _NUMBER.search(right)
            if result is None:
                continue
            # The computation is the longest run of tokens ending the left side that reads as
            # arithmetic: in "c1 = a + b = c2, so (d + e) / f = c3" the third side starts at "(".
            parsed = next((found for start in range(len(tokens)) if (found := _arithmetic(tokens[start:]))), None)
            if parsed is None:
                continue  # not a computation this check can read; its numbers are checked below
            computed, inputs = parsed
            unknown = next((token for token in inputs if not _known(*_value(token)[:2], False, known)), None)
            if unknown is not None:
                return f"The working uses {unknown.strip()}, which the question's data does not state"
            shown, places, percent = _value(result.group(0))
            if not _matches(computed, shown, places, percent):
                return (f"The working '{left.strip()} = {result.group(0).strip()}' is wrong: it computes "
                        f"{computed.quantize(Decimal('0.01'))}")
            known.append(computed)
    for match in _NUMBER.finditer(text):
        shown, places, percent = _value(match.group(0))
        if percent is False and places == 0 and 0 <= shown <= SMALL_COUNT:
            continue
        if not _known(shown, places, percent, known):
            return f"The answer states {match.group(0).strip()}, which the data does not state and the working does not compute"
    return None if stated else "The question's data states no numbers"


def case_trace(field: ApplicationField) -> dict[str, Any]:
    """Counts only, for the trace: the recording's size and how many numbers it states."""
    data = case_data(field)
    return {"data_chars": len(data), "data_numbers": len(data_numbers(data)),
            "section_context_parts": len(field.section_context)}


__all__ = [
    "MAX_DATA_CHARS", "MIN_DATA_NUMBERS", "case_analysis_question", "case_data", "case_trace",
    "check_working", "data_evidence", "data_numbers", "data_present",
]
