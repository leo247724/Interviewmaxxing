"""Round 12: the person's standing answer policies in core (``interviewmaxxing_core.
answer_policies``): the five policy keys and their saved statements, the policy a saved answer
states, and the person's own answers that contradict ``not_current_or_former_employee``.
Every answer here is fictional."""
from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from interviewmaxxing_candidate.simple_answers import _REUSABLE_PHRASES, _REUSABLE_QUESTIONS
from interviewmaxxing_core import AnswerScope, SavedAnswer, SemanticType, normalize_text
from interviewmaxxing_core.answer_policies import (
    ANSWER_POLICY_KEYS,
    ANSWER_POLICY_QUESTIONS,
    POLICY_CONTRADICTIONS,
    answer_policy_key,
    policy_contradictions,
    policy_value,
    stated_answer_policies,
)
from interviewmaxxing_generation import wording_key

KEYS = ("claims_experience_asked", "meets_experience_thresholds", "certifies_truth",
        "not_current_or_former_employee", "sanctioned_locations")
NOT_EMPLOYEE = "not_current_or_former_employee"
EMPLOYED = "Have you previously been employed by this company?"
INTERVIEWED = "Have you previously interviewed with this company?"
EARLIER = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
LATER = EARLIER + timedelta(hours=2)
_IDS = itertools.count(1)


def saved(question: str, value: Any, when: datetime = EARLIER, *,
          scope: AnswerScope = AnswerScope.GLOBAL,
          semantic: SemanticType | None = None) -> SavedAnswer:
    """A fictional saved answer; a JOB answer belongs to the fictional Mock Co job."""
    target: dict[str, str] = (
        {"job_identity_key": "ats:mock:mock-co:4012", "employer": "Mock Co"}
        if scope is AnswerScope.JOB else {})
    return SavedAnswer(id=f"sa.policy.{next(_IDS)}", scope=scope, semantic_type=semantic,
                       question=question, value=value, confirmed_at=when, **target)


def policy(key: str, value: Any, when: datetime = EARLIER, **kwargs: Any) -> SavedAnswer:
    """A saved answer to policy ``key``'s own statement."""
    return saved(ANSWER_POLICY_QUESTIONS[key], value, when, **kwargs)


# --- the keys and their statements ------------------------------------------------------------


def test_the_policy_keys_are_the_five_classes_in_order() -> None:
    """ANSWER_POLICY_KEYS lists the five classes in order, each with its own distinct statement."""
    assert ANSWER_POLICY_KEYS == KEYS
    assert tuple(ANSWER_POLICY_QUESTIONS) == KEYS
    assert len({normalize_text(q) for q in ANSWER_POLICY_QUESTIONS.values()}) == len(KEYS)


def test_no_policy_statement_is_a_reusable_question_or_phrase() -> None:
    """No policy statement equals a simple-answers question or phrase (as text or wording)."""
    statements = {normalize_text(q) for q in ANSWER_POLICY_QUESTIONS.values()}
    wordings = {wording_key(q) for q in ANSWER_POLICY_QUESTIONS.values()}
    for key, (_, question) in _REUSABLE_QUESTIONS.items():
        for wording in (question, *_REUSABLE_PHRASES.get(key, [])):
            assert normalize_text(wording) not in statements, (key, wording)
            assert wording_key(wording) not in wordings, (key, wording)
            assert answer_policy_key(saved(wording, "Yes")) is None, (key, wording)


# --- answer_policy_key and policy_value ------------------------------------------------------------


def test_answer_policy_key_reads_each_statement_in_any_case_and_spacing() -> None:
    """answer_policy_key names the policy whose statement the question is after normalize_text."""
    for key, question in ANSWER_POLICY_QUESTIONS.items():
        for wording in (question, question.upper(), question.lower(),
                        question.replace(" ", " \n\t ")):
            assert answer_policy_key(saved(wording, "Yes")) == key, wording


def test_answer_policy_key_holds_whatever_the_scope_type_and_value() -> None:
    """The key comes from the question alone: a JOB, typed or odd-valued answer still names it."""
    question = ANSWER_POLICY_QUESTIONS["certifies_truth"]
    for answer in (saved(question, "No", scope=AnswerScope.JOB),
                   saved(question, "Yes", semantic=SemanticType.ATTESTATION),
                   saved(question, True), saved(question, ["Yes", "No"])):
        assert answer_policy_key(answer) == "certifies_truth", answer


def test_answer_policy_key_is_none_for_any_other_question() -> None:
    """Other questions, a fragment of a statement or a statement with added text name no policy."""
    certifies = ANSWER_POLICY_QUESTIONS["certifies_truth"]
    for question in ("Are you above the age of 18?", EMPLOYED, "Answer policy:", certifies + ".",
                     "Do you certify that the information you provide is true, accurate and "
                     "complete?"):
        assert answer_policy_key(saved(question, "Yes")) is None, question
    phrase_only = SavedAnswer(id="sa.phrase", scope=AnswerScope.GLOBAL, question="Certification",
                              match_phrases=[certifies], value="Yes", confirmed_at=EARLIER)
    assert answer_policy_key(phrase_only) is None  # only the saved question counts


def test_policy_value_reads_yes_or_no_in_any_case() -> None:
    """policy_value gives "Yes" or "No" for the two words in any case."""
    for value, expected in (("Yes", "Yes"), ("yes", "Yes"), ("YES", "Yes"), ("yEs", "Yes"),
                            ("No", "No"), ("no", "No"), ("NO", "No"), ("nO", "No")):
        assert policy_value(saved("Certification", value)) == expected, value


def test_policy_value_is_none_for_bools_lists_numbers_and_other_text() -> None:
    """policy_value gives None for a bool, a list, a number or any other text."""
    for value in (True, False, ["Yes"], ["No"], 1, 0, 1.5, "Y", "N", "true", "Yes please",
                  "No.", "Maybe"):
        assert policy_value(saved("Certification", value)) is None, value


# --- stated_answer_policies ---------------------------------------------------------------------


def test_each_policy_is_stated_by_its_newest_global_untyped_yes_or_no() -> None:
    """Each policy with a Yes/No answer is stated by that answer; others and non-policies are absent."""
    claims = policy("claims_experience_asked", "Yes")
    sanctions = policy("sanctioned_locations", "no", LATER)
    other = saved(EMPLOYED, "No")
    assert stated_answer_policies([other, claims, sanctions]) == {
        "claims_experience_asked": claims, "sanctioned_locations": sanctions}
    assert stated_answer_policies([]) == {}
    assert stated_answer_policies([other]) == {}


def test_a_newer_answer_replaces_an_older_one_in_any_order() -> None:
    """Older answers never count once a newer one exists, whatever the input order or iterable."""
    older, newer = policy("certifies_truth", "Yes"), policy("certifies_truth", "No", LATER)
    assert stated_answer_policies([older, newer]) == {"certifies_truth": newer}
    assert stated_answer_policies([newer, older]) == {"certifies_truth": newer}
    assert stated_answer_policies(iter([newer, older])) == {"certifies_truth": newer}


def test_newest_answers_that_disagree_leave_the_policy_out() -> None:
    """Newest answers that disagree state nothing, even beside an older agreeing answer."""
    sanctions = policy("sanctioned_locations", "No")
    answers = [policy("certifies_truth", "Yes"), policy("certifies_truth", "Yes", LATER),
               policy("certifies_truth", "No", LATER), sanctions]
    assert stated_answer_policies(answers) == {"sanctioned_locations": sanctions}


def test_newest_answers_that_agree_in_another_case_state_the_policy() -> None:
    """Two newest answers "Yes" and "yes" agree: the policy is stated as Yes."""
    first = policy("meets_experience_thresholds", "Yes")
    second = policy("meets_experience_thresholds", "yes")
    stated = stated_answer_policies([first, second])
    assert set(stated) == {"meets_experience_thresholds"}
    assert stated["meets_experience_thresholds"] in (first, second)
    assert policy_value(stated["meets_experience_thresholds"]) == "Yes"


def test_job_scoped_and_typed_answers_are_ignored() -> None:
    """JOB-scoped or typed answers never state a policy, nor hide an older untyped GLOBAL one."""
    job = policy(NOT_EMPLOYEE, "Yes", LATER, scope=AnswerScope.JOB)
    typed = policy(NOT_EMPLOYEE, "Yes", LATER, semantic=SemanticType.CUSTOM_BOOLEAN)
    assert stated_answer_policies([job, typed]) == {}
    person = policy(NOT_EMPLOYEE, "No")
    assert stated_answer_policies([job, person, typed]) == {NOT_EMPLOYEE: person}


def test_an_unrecognized_newest_value_leaves_the_policy_out() -> None:
    """A newest answer that is not Yes/No states nothing: the older Yes never stands in for it."""
    for newest_value in ("maybe", True, ["Yes"]):
        answers = [policy("certifies_truth", "Yes"),
                   policy("certifies_truth", newest_value, LATER)]
        assert stated_answer_policies(answers) == {}, newest_value


# --- policy_contradictions -----------------------------------------------------------------------


def test_the_contradictions_are_the_simple_answers_employment_questions() -> None:
    """Only not_current_or_former_employee has contradictions: a Yes to the two map questions."""
    assert set(POLICY_CONTRADICTIONS) == {NOT_EMPLOYEE}
    assert POLICY_CONTRADICTIONS[NOT_EMPLOYEE] == {EMPLOYED: "Yes", INTERVIEWED: "Yes"}
    # The simple-answers questions they read, so the two can never drift apart.
    assert _REUSABLE_QUESTIONS["previously_employed_here"][1] == EMPLOYED
    assert _REUSABLE_QUESTIONS["previously_interviewed_here"][1] == INTERVIEWED


@pytest.mark.parametrize("value", ["Yes", "yes", "YES", True])
def test_a_newest_global_yes_to_either_question_contradicts(value: Any) -> None:
    """A newest GLOBAL Yes (any case, or True) to either question is returned."""
    employed, interviewed = saved(EMPLOYED, value), saved(INTERVIEWED, value, LATER)
    found = policy_contradictions(NOT_EMPLOYEE, [employed, interviewed])
    assert sorted(a.id for a in found) == sorted([employed.id, interviewed.id])


def test_a_no_and_a_superseded_global_yes_do_not_contradict() -> None:
    """A No or False, and an older GLOBAL Yes superseded by a newer GLOBAL No, are not returned."""
    answers = [saved(EMPLOYED, "Yes"), saved(EMPLOYED, "No", LATER), saved(INTERVIEWED, False)]
    assert policy_contradictions(NOT_EMPLOYEE, answers) == []
    assert policy_contradictions(NOT_EMPLOYEE, [saved(EMPLOYED, "no")]) == []
    assert policy_contradictions(NOT_EMPLOYEE, []) == []


def test_a_job_scoped_yes_contradicts_even_beside_a_newer_global_no() -> None:
    """Every JOB-scoped Yes among the answers (this job's) is returned, also when a newer GLOBAL
    No exists; a JOB-scoped No is not."""
    employed = saved(EMPLOYED, "Yes", scope=AnswerScope.JOB)
    interviewed = saved(INTERVIEWED, True, scope=AnswerScope.JOB)
    newer_no = [saved(EMPLOYED, "No", LATER), saved(INTERVIEWED, "no", LATER)]
    for answers in ([employed, interviewed], [employed, *newer_no, interviewed]):
        found = policy_contradictions(NOT_EMPLOYEE, answers)
        assert sorted(a.id for a in found) == sorted([employed.id, interviewed.id])
    job_no = saved(EMPLOYED, "No", scope=AnswerScope.JOB)
    assert policy_contradictions(NOT_EMPLOYEE, [job_no, *newer_no]) == []


def test_a_job_scoped_yes_superseded_by_a_newer_job_no_does_not_contradict() -> None:
    """Job-scoped answers follow the newest-answer rule too: a newer job No replaces an older
    job Yes for the same question, and a newer job Yes replaces an older job No."""
    older_yes = saved(EMPLOYED, "Yes", scope=AnswerScope.JOB)
    newer_no = saved(EMPLOYED, "No", LATER, scope=AnswerScope.JOB)
    assert policy_contradictions(NOT_EMPLOYEE, [older_yes, newer_no]) == []
    assert policy_contradictions(NOT_EMPLOYEE, [newer_no, older_yes]) == []
    older_no = saved(INTERVIEWED, "No", scope=AnswerScope.JOB)
    newer_yes = saved(INTERVIEWED, "Yes", LATER, scope=AnswerScope.JOB)
    assert policy_contradictions(NOT_EMPLOYEE, [newer_yes, older_no]) == [newer_yes]


def test_a_newer_yes_a_tied_yes_and_a_yes_beside_a_job_no_contradict() -> None:
    """A Yes newer than a No, a Yes tied with a No, and a GLOBAL Yes beside a newer JOB No count."""
    newer_yes = saved(EMPLOYED, "Yes", LATER)
    assert policy_contradictions(NOT_EMPLOYEE, [saved(EMPLOYED, "No"), newer_yes]) == [newer_yes]
    tied_yes = saved(INTERVIEWED, "yes")
    assert policy_contradictions(NOT_EMPLOYEE, [tied_yes, saved(INTERVIEWED, "No")]) == [tied_yes]
    person_yes = saved(INTERVIEWED, True)
    job_no = saved(INTERVIEWED, "No", LATER, scope=AnswerScope.JOB)
    assert policy_contradictions(NOT_EMPLOYEE, [person_yes, job_no]) == [person_yes]


def test_other_policies_have_no_contradictions() -> None:
    """Every other policy key has no contradicting answers, even beside GLOBAL and JOB Yes."""
    answers = [saved(EMPLOYED, "Yes"), saved(INTERVIEWED, True),
               saved(INTERVIEWED, "Yes", scope=AnswerScope.JOB)]
    assert len(policy_contradictions(NOT_EMPLOYEE, answers)) == 3
    for key in ANSWER_POLICY_KEYS:
        if key != NOT_EMPLOYEE:
            assert policy_contradictions(key, answers) == [], key
