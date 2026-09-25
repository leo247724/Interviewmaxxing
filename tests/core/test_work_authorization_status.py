"""Round 9: the extended work authorization status vocabulary, what each status settles,
and ``stated_status`` (fictional data only)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from interviewmaxxing_core import (
    PERMANENT_STATUSES,
    SPONSORSHIP_UNSETTLED_STATUSES,
    WORK_AUTHORIZATION_IMPLICATIONS,
    WORK_AUTHORIZATION_STATUS_QUESTION,
    WORK_AUTHORIZATION_STATUSES,
    AnswerScope,
    SavedAnswer,
    SemanticType,
    stated_status,
)

CODES = (
    "us_citizen", "us_permanent_resident", "asylee", "refugee", "daca", "tps",
    "pending_adjustment", "dependent_ead", "ead_opt", "h1b", "tn", "other_visa", "not_authorized",
)
FUTURE_UNSETTLED = ("daca", "tps", "pending_adjustment", "dependent_ead")
IMPORTER_PHRASES = [
    "Work authorization status", "What is your work authorization status?",
    "What is your current U.S. work authorization?",
]
CONFIRMED = datetime(2026, 9, 2, 12, tzinfo=UTC)


def _answer(value: object, *, question: str = WORK_AUTHORIZATION_STATUS_QUESTION,
            semantic: SemanticType | None = None) -> SavedAnswer:
    return SavedAnswer(id="sa.status", scope=AnswerScope.GLOBAL, semantic_type=semantic,
                       question=question, value=value, match_phrases=IMPORTER_PHRASES,
                       confirmed_at=CONFIRMED)


# --- the vocabulary ------------------------------------------------------------------------------


def test_the_vocabulary_has_exactly_the_thirteen_codes():
    assert len(WORK_AUTHORIZATION_STATUSES) == 13
    assert set(WORK_AUTHORIZATION_STATUSES) == set(CODES)


def test_the_implications_cover_exactly_the_same_codes():
    assert WORK_AUTHORIZATION_IMPLICATIONS.keys() == WORK_AUTHORIZATION_STATUSES.keys()
    assert all(text.strip() for text in WORK_AUTHORIZATION_IMPLICATIONS.values())


def test_the_permanent_and_unsettled_statuses_are_codes_of_the_vocabulary():
    assert PERMANENT_STATUSES.issubset(WORK_AUTHORIZATION_STATUSES)
    assert SPONSORSHIP_UNSETTLED_STATUSES.issubset(WORK_AUTHORIZATION_STATUSES)
    assert {"us_citizen", "us_permanent_resident"} <= PERMANENT_STATUSES
    assert "other_visa" in SPONSORSHIP_UNSETTLED_STATUSES
    assert not PERMANENT_STATUSES & SPONSORSHIP_UNSETTLED_STATUSES


def test_opt_will_need_sponsorship_in_the_future():
    text = WORK_AUTHORIZATION_IMPLICATIONS["ead_opt"].casefold()
    assert re.search(r"will need [^.;]*sponsorship[^.;]* in the future", text), text
    assert "does not settle" not in text


@pytest.mark.parametrize("code", FUTURE_UNSETTLED)
def test_an_ead_status_leaves_the_future_unsettled(code):
    text = WORK_AUTHORIZATION_IMPLICATIONS[code].casefold()
    assert re.search(r"does not settle whether sponsorship [^.;]*future", text), text


@pytest.mark.parametrize("code", ["asylee", "refugee"])
def test_an_asylee_or_refugee_never_needs_sponsorship(code):
    text = WORK_AUTHORIZATION_IMPLICATIONS[code].casefold()
    assert "never needs an employer's sponsorship, now or in the future" in text
    assert "does not settle" not in text


@pytest.mark.parametrize("code", CODES)
def test_the_meaning_is_the_applicants_own_words_never_a_code(code):
    meaning = WORK_AUTHORIZATION_STATUSES[code]
    assert meaning == meaning.strip() and meaning and "\n" not in meaning
    assert "_" not in meaning
    for other in CODES:
        if "_" in other:
            assert other not in meaning.casefold(), (code, other)
    assert meaning.casefold() != code


def test_every_meaning_is_distinct():
    meanings = [" ".join(m.casefold().rstrip(".").split())
                for m in WORK_AUTHORIZATION_STATUSES.values()]
    assert len(set(meanings)) == len(meanings)


def test_a_citizen_is_stated_in_plain_words():
    assert WORK_AUTHORIZATION_STATUSES["us_citizen"] == "U.S. citizen"


# --- stated_status -------------------------------------------------------------------------------


@pytest.mark.parametrize("code", CODES)
def test_the_untyped_answer_to_the_status_question_states_its_code(code):
    assert stated_status(_answer(code)) == code


@pytest.mark.parametrize("question", [
    "what is your u.s. work authorization status?",
    "WHAT IS YOUR U.S. WORK AUTHORIZATION STATUS?",
    "  What is your U.S.   work authorization\nstatus?  ",
    "What is your U.S.\twork authorization status?",
    "What is your U.S.\N{NO-BREAK SPACE}work authorization status?",
])
def test_the_status_question_is_compared_ignoring_case_and_whitespace(question):
    assert stated_status(_answer("daca", question=question)) == "daca"


@pytest.mark.parametrize("semantic", [SemanticType.WORK_AUTHORIZATION, SemanticType.SPONSORSHIP,
                                      SemanticType.CUSTOM_TEXT, SemanticType.CUSTOM_SELECT])
def test_a_typed_answer_states_no_status(semantic):
    assert stated_status(_answer("us_citizen", semantic=semantic)) is None


@pytest.mark.parametrize("question", [
    "Work authorization status",
    "What is your work authorization status?",
    "What is your current U.S. work authorization?",
    "Are you currently authorized to work in the US?",
    "Will you now or in the future require visa sponsorship for employment?",
])
def test_an_answer_to_another_question_states_no_status(question):
    assert stated_status(_answer("us_citizen", question=question)) is None


@pytest.mark.parametrize("value", [
    "U.S. citizen", "green card", "asylum", "H-1B visa holder", "", True, False, 1, 2.5,
    ["us_citizen"],
])
def test_a_value_outside_the_vocabulary_states_no_status(value):
    assert stated_status(_answer(value)) is None
