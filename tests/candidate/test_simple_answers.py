"""Contact-map import uses the canonical profile and keeps other answers intact."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/simple_answers.py"


def run(command, path):
    return subprocess.run(
        [sys.executable, str(SCRIPT), command, "--file", str(path)],
        capture_output=True, text=True, timeout=15, check=False,
    )


def test_export_edit_import_preserves_other_profile_data_and_hides_values(
    write_candidate, candidate_store, tmp_path
):
    directory = write_candidate(answers=[])
    before = json.loads((directory / "profile.json").read_text())
    answer_bytes = (directory / "answers.json").read_bytes()
    target = tmp_path / "simple-answers.json"
    result = run("export", target)
    assert result.returncode == 0, result.stderr
    assert before["identity"]["email"] not in result.stdout
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    exported = target.read_bytes()
    result = run("export", target)
    assert result.returncode == 1 and target.read_bytes() == exported
    data = json.loads(exported)
    data.update(email="updated@example.test", linkedin_url="https://www.linkedin.com/in/example",
                phone="+1 555 010 0020", postal_code="00123", website_url="   ", country=None)
    target.write_text(json.dumps(data))
    original_profile = (directory / "profile.json").read_bytes()
    assert run("validate", target).returncode == 0
    assert (directory / "profile.json").read_bytes() == original_profile
    result = run("import", target)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["profile_updated"] is True
    assert data["email"] not in result.stdout
    after = json.loads((directory / "profile.json").read_text())
    for key in before.keys() - {"identity"}:
        assert after[key] == before[key]
    assert (directory / "answers.json").read_bytes() == answer_bytes
    profile = candidate_store.load("default")
    assert profile.identity.email == data["email"]
    assert profile.identity.linkedin_url == data["linkedin_url"]
    assert profile.identity.address.postal_code == "00123"
    assert profile.identity.address.country is None
    assert profile.identity.website_url is None
    assert profile.identity.full_name == "Avery Example"
    assert profile.resume.verify()
    # Repeated import changes neither the file nor its verification timestamp.
    imported = (directory / "profile.json").read_bytes()
    result = run("import", target)
    assert result.returncode == 0
    assert json.loads(result.stdout)["profile_updated"] is False
    assert (directory / "profile.json").read_bytes() == imported


@pytest.mark.parametrize("mutation", [
    {"first_name": None},
    {"email": "not-an-email"},
    {"phone": 15550100020},
    {"postal_code": False},
    {"work_authorization": "Yes"},
    {"gender": False},
    {"salary_expectation": "100000"},
    {"authorized_to_work_us": "sometimes"},
    {"above_age_18": "perhaps"},
    {"hispanic_latino": "unsure"},
    {"education_start_date": "2017-13"},
    {"education_start_date": "2022-05", "education_end_date": "2017-08"},
])
def test_invalid_or_out_of_scope_input_writes_nothing(write_candidate, tmp_path, mutation):
    directory = write_candidate()
    target = tmp_path / "simple-answers.json"
    assert run("export", target).returncode == 0
    data = json.loads(target.read_text())
    data.update(mutation)
    target.write_text(json.dumps(data))
    before = (directory / "profile.json").read_bytes()
    result = run("import", target)
    assert result.returncode == 1
    assert (directory / "profile.json").read_bytes() == before
    assert "updated@example.test" not in result.stderr


def test_missing_keys_and_duplicate_keys_are_rejected(write_candidate, tmp_path):
    directory = write_candidate()
    target = tmp_path / "simple-answers.json"
    assert run("export", target).returncode == 0
    data = json.loads(target.read_text())
    before = (directory / "profile.json").read_bytes()
    del data["country"]
    target.write_text(json.dumps(data))
    result = run("import", target)
    assert result.returncode == 1 and "country" in result.stderr
    target.write_text('{"email":"first@example.test","email":"second@example.test"}')
    result = run("import", target)
    assert result.returncode == 1 and "duplicate key" in result.stderr
    assert (directory / "profile.json").read_bytes() == before


def test_blank_example_is_a_valid_draft_but_cannot_be_imported(tmp_path):
    from interviewmaxxing_candidate.simple_answers import SimpleAnswers

    example = REPO / "examples/simple-answers.example.json"
    parsed = SimpleAnswers.model_validate(json.loads(example.read_text()))
    values = parsed.model_dump()
    policies = values.pop("answer_policies")  # round 12: a section of its own, all null
    assert set(values.values()) == {None} and set(policies.values()) == {None}
    result = run("validate", example)
    assert result.returncode == 1
    assert "first_name, last_name, email" in result.stderr


def test_user_added_defaults_and_legacy_keys_import_and_export(write_candidate, candidate_store, tmp_path):
    directory = write_candidate()
    target = tmp_path / "simple-answers.json"
    assert run("export", target).returncode == 0
    data = json.loads(target.read_text())
    del data["requires_visa_sponsorship"]
    del data["referral_source"]
    del data["referred_by_current_employee"]
    for key in ("above_age_18", "authorized_to_work_us", "school", "degree"):
        del data[key]
    data.update({
        "gender": "Male",
        "where_are_you_based": "Springfield, OR",
        "will_you_now_or_in_the_future_require _visa_sponsorship_for_employment": "no",
        "Where_did_you_first_hear_about_company": "Company career page",
        "Were_you_referred_to_this_position_by_a_current_employee": "no",
        "are_you_above_the_age_of_18": "yes",
        "Are_you_currently_authorized_to_work_in_the_US": "yes",
        "School": "Example State University",
        "Degree": "Bachelor's Degree",
        "hispanic_latino": "no",
        "veteran_status": "I am not a protected veteran",
        "education_discipline": "Business Administration",
        "education_start_date": "August 2017",
        "education_end_date": "May 2022",
    })
    target.write_text(json.dumps(data))
    profile_before = (directory / "profile.json").read_bytes()
    result = run("import", target)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["saved_answers_updated"] == 18
    assert json.loads(result.stdout)["profile_updated"] is False
    assert (directory / "profile.json").read_bytes() == profile_before
    answers_before = (directory / "answers.json").read_bytes()
    profile = candidate_store.load("default")
    added = [a for a in profile.saved_answers if a.id.startswith("simple_answer_")]
    assert len(added) == 18
    assert all(a.scope.value == "GLOBAL" and a.job_url is None for a in added)
    sponsor = next(a for a in added if a.semantic_type and a.semantic_type.value == "SPONSORSHIP")
    assert sponsor.value == "No"
    result = run("import", target)
    assert result.returncode == 0
    assert json.loads(result.stdout)["saved_answers_updated"] == 0
    assert (directory / "answers.json").read_bytes() == answers_before
    new_export = tmp_path / "export.json"
    assert run("export", new_export).returncode == 0
    values = json.loads(new_export.read_text())
    assert values["requires_visa_sponsorship"] == "No"
    assert values["referral_source"] == "Company career page"
    assert values["gender"] == "Male"
    assert values["where_are_you_based"] == "Springfield, OR"
    assert values["referred_by_current_employee"] == "No"
    assert values["above_age_18"] == "Yes"
    assert values["authorized_to_work_us"] == "Yes"
    assert values["school"] == "Example State University"
    assert values["degree"] == "Bachelor's Degree"
    assert values["hispanic_latino"] == "No"
    assert values["veteran_status"] == "I am not a protected veteran"
    assert values["education_discipline"] == "Business Administration"
    assert values["education_start_date"] == "2017-08"
    assert values["education_end_date"] == "2022-05"


def test_invalid_sponsorship_prevents_contact_or_answer_writes(write_candidate, tmp_path):
    directory = write_candidate()
    target = tmp_path / "simple-answers.json"
    assert run("export", target).returncode == 0
    data = json.loads(target.read_text())
    data.update(email="new@example.test", requires_visa_sponsorship="maybe")
    target.write_text(json.dumps(data))
    original = (directory / "profile.json").read_bytes()
    result = run("import", target)
    assert result.returncode == 1 and '"Yes", "No", or null' in result.stderr
    assert (directory / "profile.json").read_bytes() == original
    assert not (directory / "answers.json").exists()


# --- round 3: more reusable defaults, null until the person fills them ----------------------

NEW_YES_NO_KEYS = (
    "previously_employed_here", "previously_interviewed_here", "related_to_employee",
    "willing_to_relocate", "open_to_other_positions", "willing_to_provide_references",
)
NEW_TEXT_VALUES = {
    "desired_salary": "$150,000",
    "english_proficiency": "Native",
    "available_time_zones": "US Central, US Eastern",
    "travel_willingness": "Up to 25%",
    "earliest_start_date": "Two weeks after an offer",
}
NEW_KEYS = (*NEW_YES_NO_KEYS, *NEW_TEXT_VALUES)
NEW_SEMANTICS = {"willing_to_relocate": "RELOCATION", "desired_salary": "SALARY_EXPECTATION",
                 "earliest_start_date": "START_DATE"}


def _canonical_question(key):
    from interviewmaxxing_candidate.simple_answers import _REUSABLE_QUESTIONS

    return _REUSABLE_QUESTIONS[key][1]


def test_new_defaults_round_trip_through_import_and_export(write_candidate, candidate_store, tmp_path):
    directory = write_candidate()
    target = tmp_path / "simple-answers.json"
    assert run("export", target).returncode == 0
    data = json.loads(target.read_text())
    assert all(data[key] is None for key in NEW_KEYS)  # nothing is invented on export
    # Mixed case on purpose: yes/no answers are normalized to "Yes"/"No".
    data.update({key: ("Yes" if i % 2 == 0 else "no") for i, key in enumerate(NEW_YES_NO_KEYS)})
    data.update(NEW_TEXT_VALUES)
    target.write_text(json.dumps(data))
    profile_before = (directory / "profile.json").read_bytes()
    result = run("import", target)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["saved_answers_updated"] == len(NEW_KEYS)
    assert json.loads(result.stdout)["profile_updated"] is False
    assert (directory / "profile.json").read_bytes() == profile_before
    for value in NEW_TEXT_VALUES.values():
        assert value not in result.stdout  # the import report lists keys, never values
    profile = candidate_store.load("default")
    added = {a.question: a for a in profile.saved_answers if a.id.startswith("simple_answer_")}
    assert len(added) == len(NEW_KEYS)
    for key in NEW_KEYS:
        answer = added[_canonical_question(key)]
        assert answer.scope.value == "GLOBAL"
        assert answer.job_identity_key is None and answer.job_url is None and answer.employer is None
        expected_semantic = NEW_SEMANTICS.get(key)
        assert (answer.semantic_type.value if answer.semantic_type else None) == expected_semantic
        assert answer.match_phrases  # every new default carries its observed variants
    assert added[_canonical_question("willing_to_relocate")].value == "No"
    assert added[_canonical_question("previously_employed_here")].value == "Yes"
    assert added[_canonical_question("desired_salary")].value == "$150,000"
    # A repeated import of the same map writes nothing.
    answers_before = (directory / "answers.json").read_bytes()
    result = run("import", target)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["saved_answers_updated"] == 0
    assert (directory / "answers.json").read_bytes() == answers_before
    exported = tmp_path / "exported.json"
    assert run("export", exported).returncode == 0
    values = json.loads(exported.read_text())
    for i, key in enumerate(NEW_YES_NO_KEYS):
        assert values[key] == ("Yes" if i % 2 == 0 else "No")
    for key, value in NEW_TEXT_VALUES.items():
        assert values[key] == value


@pytest.mark.parametrize("key", NEW_YES_NO_KEYS)
def test_new_yes_no_defaults_reject_other_values_and_write_nothing(write_candidate, tmp_path, key):
    directory = write_candidate()
    target = tmp_path / "simple-answers.json"
    assert run("export", target).returncode == 0
    data = json.loads(target.read_text())
    data.update({key: "maybe", "email": "changed@example.test", "desired_salary": "$150,000"})
    target.write_text(json.dumps(data))
    before = (directory / "profile.json").read_bytes()
    result = run("import", target)
    assert result.returncode == 1
    assert '"Yes", "No", or null' in result.stderr and key in result.stderr
    assert "$150,000" not in result.stderr and "changed@example.test" not in result.stderr
    assert (directory / "profile.json").read_bytes() == before
    assert not (directory / "answers.json").exists()


def test_the_blank_example_lists_every_key_including_the_new_defaults():
    from interviewmaxxing_candidate.simple_answers import SimpleAnswers

    example = json.loads((REPO / "examples/simple-answers.example.json").read_text())
    assert set(example) == set(SimpleAnswers.model_fields)
    assert set(NEW_KEYS) <= set(example)
    assert all(example[key] is None for key in NEW_KEYS)


def test_a_null_new_default_adds_nothing_and_never_erases_a_confirmed_one(
    write_candidate, candidate_store, tmp_path
):
    directory = write_candidate()
    target = tmp_path / "simple-answers.json"
    assert run("export", target).returncode == 0
    data = json.loads(target.read_text())
    data["willing_to_relocate"] = "Yes"
    target.write_text(json.dumps(data))
    result = run("import", target)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["saved_answers_updated"] == 1
    answers_before = (directory / "answers.json").read_bytes()
    data["willing_to_relocate"] = None  # clearing the map entry is not an answer
    target.write_text(json.dumps(data))
    result = run("import", target)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["saved_answers_updated"] == 0
    assert (directory / "answers.json").read_bytes() == answers_before
    profile = candidate_store.load("default")
    [kept] = [a for a in profile.saved_answers if a.question == _canonical_question("willing_to_relocate")]
    assert kept.value == "Yes" and kept.scope.value == "GLOBAL"
    assert not [a for a in profile.saved_answers
                if a.id.startswith("simple_answer_") and a.question != kept.question]
    exported = tmp_path / "exported.json"
    assert run("export", exported).returncode == 0
    assert json.loads(exported.read_text())["willing_to_relocate"] == "Yes"


def test_new_defaults_carry_distinct_wordings_and_phrases():
    from interviewmaxxing_candidate.simple_answers import _REUSABLE_PHRASES, _REUSABLE_QUESTIONS
    from interviewmaxxing_generation import wording_key

    for key in NEW_KEYS:
        assert _REUSABLE_PHRASES.get(key), f"{key} has no observed variants"
    canonical = [wording_key(question) for _, question in _REUSABLE_QUESTIONS.values()]
    assert len(set(canonical)) == len(canonical)
    owners: dict[str, set[str]] = {}
    for key, (_, question) in _REUSABLE_QUESTIONS.items():
        for wording in (question, *_REUSABLE_PHRASES.get(key, [])):
            owners.setdefault(wording_key(wording), set()).add(key)
    for key in NEW_KEYS:
        for wording in (_canonical_question(key), *_REUSABLE_PHRASES[key]):
            assert owners[wording_key(wording)] == {key}, wording


# --- round 7: statements, one-time keys, race and disability, typed imports -------------------

ROUND7_YES_NO = ["family_government_official", "non_compete_agreement", "uses_ai_tools",
                 "acknowledge_privacy_notice", "certify_information_true", "consent_to_contact",
                 "consent_reference_checks", "consent_background_check"]


def _round7_answers(**values):
    from interviewmaxxing_candidate.simple_answers import _CONTACT_KEYS, SimpleAnswers

    base = dict.fromkeys(_CONTACT_KEYS) | {"first_name": "Avery", "last_name": "Example",
                                           "email": "avery@example.test"}
    return SimpleAnswers.model_validate(base | values)


@pytest.mark.parametrize("key", ROUND7_YES_NO)
def test_round7_yes_no_keys_accept_only_yes_or_no(key):
    from pydantic import ValidationError

    assert getattr(_round7_answers(**{key: "yes"}), key) == "Yes"
    with pytest.raises(ValidationError, match='"Yes", "No", or null'):
        _round7_answers(**{key: "maybe"})


def test_round7_keys_import_as_typed_global_answers_where_a_type_exists():
    from datetime import UTC, datetime

    from interviewmaxxing_candidate.simple_answers import _REUSABLE_QUESTIONS, STATEMENT_KEYS
    from interviewmaxxing_core import AnswerScope, SemanticType

    answers = _round7_answers(
        race_ethnicity="Two or more races", disability_status="No, I do not have a disability",
        pronouns="they/them", county="Fictional County", familiar_with_company="Somewhat familiar",
        acknowledge_privacy_notice="Yes", certify_information_true="Yes", consent_to_contact="Yes",
        consent_reference_checks="No", consent_background_check="Yes", uses_ai_tools="No")
    updates = answers.saved_answer_updates(confirmed_at=datetime(2026, 9, 24, tzinfo=UTC))
    by_question = {a.question: a for a in updates}
    expected = {
        "race_ethnicity": SemanticType.EEO_RACE_ETHNICITY,
        "disability_status": SemanticType.EEO_DISABILITY_STATUS,
        "pronouns": SemanticType.PRONOUNS,
        "acknowledge_privacy_notice": SemanticType.CONSENT,
        "certify_information_true": SemanticType.ATTESTATION,
        "consent_to_contact": SemanticType.CONSENT,
        "consent_reference_checks": SemanticType.CONSENT,
        "consent_background_check": SemanticType.CONSENT,
        "county": None, "familiar_with_company": None, "uses_ai_tools": None,
    }
    for key, semantic in expected.items():
        answer = by_question[_REUSABLE_QUESTIONS[key][1]]
        assert answer.semantic_type is semantic and answer.scope is AnswerScope.GLOBAL
    assert {_REUSABLE_QUESTIONS[key][0] for key in STATEMENT_KEYS} == {
        SemanticType.CONSENT, SemanticType.ATTESTATION}
    # Education discipline stays untyped: sites type "Discipline" as a custom question.
    assert _REUSABLE_QUESTIONS["education_discipline"][0] is None


# --- round 8: the stated work authorization status -------------------------------------------

@pytest.mark.parametrize("given,code", [
    ("us_citizen", "us_citizen"), ("US-Citizen", "us_citizen"),
    ("us permanent resident", "us_permanent_resident"), ("ead_opt", "ead_opt"), ("h1b", "h1b"),
    ("tn", "tn"), ("other_visa", "other_visa"), ("not_authorized", "not_authorized"),
])
def test_the_status_takes_the_closed_vocabulary(given, code):
    assert _round7_answers(work_authorization_status=given).work_authorization_status == code


def test_a_status_outside_the_vocabulary_is_rejected_with_the_choices():
    from pydantic import ValidationError

    with pytest.raises(ValidationError,
                       match="us_citizen, us_permanent_resident, asylee, refugee, daca, tps"):
        _round7_answers(work_authorization_status="green card")


@pytest.mark.parametrize("status,changes,keys", [
    ("us_citizen", {"requires_visa_sponsorship": "Yes"}, ["requires_visa_sponsorship"]),
    ("us_permanent_resident", {"authorized_to_work_us": "No"}, ["authorized_to_work_us"]),
    ("not_authorized", {"authorized_to_work_us": "Yes"}, ["authorized_to_work_us"]),
    ("not_authorized", {"requires_visa_sponsorship": "No"}, ["requires_visa_sponsorship"]),
])
def test_a_status_that_contradicts_a_stated_answer_names_both_keys(status, changes, keys):
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as error:
        _round7_answers(work_authorization_status=status, **changes)
    message = str(error.value)
    assert "work_authorization_status" in message and all(key in message for key in keys)


@pytest.mark.parametrize("status,changes", [
    ("us_citizen", {"requires_visa_sponsorship": "No", "authorized_to_work_us": "Yes"}),
    ("h1b", {"requires_visa_sponsorship": "Yes", "authorized_to_work_us": "Yes"}),
    ("ead_opt", {"requires_visa_sponsorship": "Yes"}),
])
def test_consistent_statuses_import_as_one_untyped_global_answer(status, changes):
    from datetime import UTC, datetime

    from interviewmaxxing_core import WORK_AUTHORIZATION_STATUS_QUESTION, AnswerScope

    answers = _round7_answers(work_authorization_status=status, **changes)
    updates = answers.saved_answer_updates(confirmed_at=datetime(2026, 9, 24, tzinfo=UTC))
    [saved] = [a for a in updates if a.question == WORK_AUTHORIZATION_STATUS_QUESTION]
    assert (saved.value, saved.semantic_type, saved.scope) == (status, None, AnswerScope.GLOBAL)


# --- round 9: the extended status vocabulary ---

ROUND9_CODES = (
    "us_citizen", "us_permanent_resident", "asylee", "refugee", "daca", "tps",
    "pending_adjustment", "dependent_ead", "ead_opt", "h1b", "tn", "other_visa", "not_authorized",
)
ROUND9_NEW_CODES = ("asylee", "refugee", "daca", "tps", "pending_adjustment", "dependent_ead")
ROUND9_RULED_OUT: dict[str, dict[str, str]] = {
    **{code: {"requires_visa_sponsorship": "Yes", "authorized_to_work_us": "No"}
       for code in ("us_citizen", "us_permanent_resident", "asylee", "refugee")},
    **{code: {"authorized_to_work_us": "No"}
       for code in ("daca", "tps", "pending_adjustment", "dependent_ead")},
    "ead_opt": {"requires_visa_sponsorship": "No", "authorized_to_work_us": "No"},
    "h1b": {"requires_visa_sponsorship": "No"},
    "not_authorized": {"authorized_to_work_us": "Yes", "requires_visa_sponsorship": "No"},
    "tn": {},
    "other_visa": {},
}
"""The round 9 contract: the legal answers each stated status rules out."""
ROUND9_LEGAL_KEYS = ("requires_visa_sponsorship", "authorized_to_work_us")
ROUND9_STATUS_PHRASES = [
    "Work authorization status", "What is your work authorization status?",
    "What is your current U.S. work authorization?",
]
ROUND9_CONFIRMED = "2026-09-24T09:00:00Z"


def _round9_rejection(status, **legal):
    """The one model-level message importing ``status`` beside ``legal`` gives, or None."""
    from pydantic import ValidationError

    try:
        _round7_answers(work_authorization_status=status, **legal)
    except ValidationError as error:
        [problem] = error.errors(include_input=False, include_url=False)
        assert problem["loc"] == ()
        return problem["msg"]
    return None


@pytest.mark.parametrize("given,code", [
    ("DACA", "daca"), ("Pending-Adjustment", "pending_adjustment"),
    ("dependent ead", "dependent_ead"), ("Asylee", "asylee"), ("REFUGEE", "refugee"),
    ("Tps", "tps"), ("pending adjustment", "pending_adjustment"),
    ("Dependent-EAD", "dependent_ead"), ("  daca  ", "daca"), ("asylee", "asylee"),
])
def test_round9_the_new_codes_are_accepted_with_normalization(given, code):
    assert _round7_answers(work_authorization_status=given).work_authorization_status == code


@pytest.mark.parametrize("code", ROUND9_CODES)
def test_round9_every_code_is_accepted_in_any_case_with_dashes_or_spaces(code):
    for given in (code, code.upper(), code.replace("_", " ").title(),
                  code.replace("_", "-").upper()):
        assert _round7_answers(work_authorization_status=given).work_authorization_status == code


@pytest.mark.parametrize("given", ["green card", "asylum", "DACA recipient", "U.S. citizen",
                                   "dependent_ead_h4", "opt"])
def test_round9_a_status_outside_the_vocabulary_lists_all_thirteen_codes(given):
    import re

    from pydantic import ValidationError

    from interviewmaxxing_core import WORK_AUTHORIZATION_STATUSES

    with pytest.raises(ValidationError) as error:
        _round7_answers(work_authorization_status=given)
    [problem] = error.value.errors(include_input=False, include_url=False)
    assert problem["loc"] == ("work_authorization_status",)
    listed = re.fullmatch(r"Value error, Use one of (.+), or null\.", problem["msg"])
    assert listed is not None, problem["msg"]
    codes = listed.group(1).split(", ")
    assert len(codes) == 13 and set(codes) == set(ROUND9_CODES)
    assert codes == list(WORK_AUTHORIZATION_STATUSES)  # listed once each, in vocabulary order


@pytest.mark.parametrize("status,key,wrong", [
    (status, key, wrong) for status, ruled in ROUND9_RULED_OUT.items()
    for key, wrong in ruled.items()
])
def test_round9_each_contradiction_is_rejected_naming_both_keys(status, key, wrong):
    # The status and the legal answer are given in another spelling; both are normalized
    # before the check and named in their normalized form.
    message = _round9_rejection(status.replace("_", " ").upper(), **{key: wrong.lower()})
    assert message is not None, f"{status} beside {key} {wrong} was imported"
    assert f"work_authorization_status {status!r} contradicts {key} {wrong!r}" in message
    [other] = set(ROUND9_LEGAL_KEYS) - {key}
    assert other not in message


@pytest.mark.parametrize("status", [s for s, ruled in ROUND9_RULED_OUT.items() if len(ruled) == 2])
def test_round9_a_status_contradicting_both_answers_names_all_three_keys(status):
    ruled = ROUND9_RULED_OUT[status]
    message = _round9_rejection(status, **ruled)
    assert message is not None
    assert f"work_authorization_status {status!r} contradicts " in message
    for key, wrong in ruled.items():
        assert f"{key} {wrong!r}" in message


@pytest.mark.parametrize("status", ROUND9_CODES)
def test_round9_every_status_rules_out_exactly_its_contradictions(status):
    ruled = ROUND9_RULED_OUT[status]
    wrong = []
    for sponsorship in (None, "Yes", "No"):
        for authorized in (None, "Yes", "No"):
            legal = {"requires_visa_sponsorship": sponsorship, "authorized_to_work_us": authorized}
            expected = [key for key, value in legal.items()
                        if value is not None and ruled.get(key) == value]
            message = _round9_rejection(status, **legal)
            if not expected:
                if message is not None:
                    wrong.append((legal, message))
                continue
            named = message is not None and all(
                f"{key} {legal[key]!r}" in message for key in expected
            ) and not any(key in message for key in set(ROUND9_LEGAL_KEYS) - set(expected))
            if not named:
                wrong.append((legal, message))
    assert wrong == []


def test_round9_a_legacy_alias_is_checked_against_the_status():
    message = _round9_rejection(
        "TPS", Are_you_currently_authorized_to_work_in_the_US="no")
    assert message is not None
    assert "work_authorization_status 'tps' contradicts authorized_to_work_us 'No'" in message


@pytest.mark.parametrize("status,legal", [
    ("ead_opt", {"requires_visa_sponsorship": "Yes"}),
    ("ead_opt", {"requires_visa_sponsorship": "Yes", "authorized_to_work_us": "Yes"}),
    ("daca", {"requires_visa_sponsorship": "Yes"}),
    ("daca", {"requires_visa_sponsorship": "No"}),
    ("daca", {"requires_visa_sponsorship": "No", "authorized_to_work_us": "Yes"}),
    ("tps", {"requires_visa_sponsorship": "Yes", "authorized_to_work_us": "Yes"}),
    ("pending_adjustment", {"requires_visa_sponsorship": "No", "authorized_to_work_us": "Yes"}),
    ("pending_adjustment", {"requires_visa_sponsorship": "Yes"}),
    ("dependent_ead", {"requires_visa_sponsorship": "Yes", "authorized_to_work_us": "Yes"}),
    ("dependent_ead", {"requires_visa_sponsorship": "No"}),
    ("tn", {"requires_visa_sponsorship": "No"}),
    ("tn", {"requires_visa_sponsorship": "Yes"}),
    ("tn", {"requires_visa_sponsorship": "No", "authorized_to_work_us": "No"}),
    ("other_visa", {"requires_visa_sponsorship": "Yes", "authorized_to_work_us": "Yes"}),
    ("other_visa", {"requires_visa_sponsorship": "Yes", "authorized_to_work_us": "No"}),
    ("other_visa", {"requires_visa_sponsorship": "No", "authorized_to_work_us": "Yes"}),
    ("other_visa", {"requires_visa_sponsorship": "No", "authorized_to_work_us": "No"}),
    ("asylee", {"requires_visa_sponsorship": "No", "authorized_to_work_us": "Yes"}),
    ("refugee", {"requires_visa_sponsorship": "No", "authorized_to_work_us": "Yes"}),
    ("h1b", {"requires_visa_sponsorship": "Yes", "authorized_to_work_us": "No"}),
    ("not_authorized", {"requires_visa_sponsorship": "Yes", "authorized_to_work_us": "No"}),
    *[(code, {}) for code in ROUND9_CODES],
])
def test_round9_consistent_combinations_import_as_one_untyped_global_answer(status, legal):
    from datetime import UTC, datetime

    from interviewmaxxing_core import (
        WORK_AUTHORIZATION_STATUS_QUESTION,
        AnswerScope,
        SemanticType,
        stated_status,
    )

    answers = _round7_answers(work_authorization_status=status, **legal)
    confirmed = datetime(2026, 9, 24, tzinfo=UTC)
    updates = answers.saved_answer_updates(confirmed_at=confirmed)
    [saved] = [a for a in updates if a.question == WORK_AUTHORIZATION_STATUS_QUESTION]
    assert (saved.value, saved.semantic_type, saved.scope) == (status, None, AnswerScope.GLOBAL)
    assert (saved.job_identity_key, saved.job_url, saved.employer) == (None, None, None)
    assert saved.match_phrases == ROUND9_STATUS_PHRASES
    assert stated_status(saved) == status
    # One answer states the status; the two legal answers keep their own types.
    assert [a for a in updates if a.value == status] == [saved]
    types = {"requires_visa_sponsorship": SemanticType.SPONSORSHIP,
             "authorized_to_work_us": SemanticType.WORK_AUTHORIZATION}
    assert {a.semantic_type: a.value for a in updates if a is not saved} == {
        types[key]: value for key, value in legal.items()}
    # A repeated import of the same map adds nothing.
    assert answers.saved_answer_updates(confirmed_at=confirmed, current=updates) == []


@pytest.mark.parametrize("code", ROUND9_NEW_CODES)
def test_round9_a_new_status_exports_as_its_code(code, fictional_candidate):
    from interviewmaxxing_candidate.simple_answers import SimpleAnswers
    from interviewmaxxing_core import WORK_AUTHORIZATION_STATUS_QUESTION, AnswerScope, SavedAnswer

    status = SavedAnswer(id="sa.status", scope=AnswerScope.GLOBAL,
                         question=WORK_AUTHORIZATION_STATUS_QUESTION, value=code,
                         match_phrases=ROUND9_STATUS_PHRASES, confirmed_at=ROUND9_CONFIRMED)
    profile = fictional_candidate.model_copy(update={"saved_answers": [status]})
    assert SimpleAnswers.from_profile(profile).work_authorization_status == code


def test_round9_a_new_status_imports_through_the_script_and_exports_as_its_code(
    write_candidate, candidate_store, tmp_path
):
    from interviewmaxxing_core import WORK_AUTHORIZATION_STATUS_QUESTION, AnswerScope

    directory = write_candidate()
    target = tmp_path / "simple-answers.json"
    assert run("export", target).returncode == 0
    data = json.loads(target.read_text())
    assert data["work_authorization_status"] is None
    data.update(work_authorization_status="Pending-Adjustment", authorized_to_work_us="yes",
                requires_visa_sponsorship="no")
    target.write_text(json.dumps(data))
    profile_before = (directory / "profile.json").read_bytes()
    result = run("import", target)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["saved_answers_updated"] == 3
    assert (directory / "profile.json").read_bytes() == profile_before
    profile = candidate_store.load("default")
    [status] = [a for a in profile.saved_answers if a.question == WORK_AUTHORIZATION_STATUS_QUESTION]
    assert (status.value, status.semantic_type, status.scope) == (
        "pending_adjustment", None, AnswerScope.GLOBAL)
    assert status.match_phrases == ROUND9_STATUS_PHRASES
    exported = tmp_path / "exported.json"
    assert run("export", exported).returncode == 0
    values = json.loads(exported.read_text())
    assert values["work_authorization_status"] == "pending_adjustment"
    assert (values["authorized_to_work_us"], values["requires_visa_sponsorship"]) == ("Yes", "No")


def test_round9_a_contradicting_new_status_writes_nothing_through_the_script(
    write_candidate, tmp_path
):
    directory = write_candidate()
    target = tmp_path / "simple-answers.json"
    assert run("export", target).returncode == 0
    data = json.loads(target.read_text())
    data.update(email="changed@example.test", work_authorization_status="DACA",
                authorized_to_work_us="No")
    target.write_text(json.dumps(data))
    before = (directory / "profile.json").read_bytes()
    result = run("import", target)
    assert result.returncode == 1
    assert "work_authorization_status 'daca' contradicts authorized_to_work_us 'No'" in result.stderr
    assert "changed@example.test" not in result.stderr
    assert (directory / "profile.json").read_bytes() == before
    assert not (directory / "answers.json").exists()


@pytest.mark.parametrize("given", ["H-1B", "h-1b", "H1B"])
def test_h1b_is_accepted_as_people_write_it(given):
    assert _round7_answers(work_authorization_status=given).work_authorization_status == "h1b"


def test_the_importer_and_the_derivation_share_one_contradiction_table():
    from interviewmaxxing_candidate.simple_answers import _REUSABLE_QUESTIONS
    from interviewmaxxing_core import (
        STATED_ANSWER_QUESTIONS,
        STATUS_CONTRADICTIONS,
        WORK_AUTHORIZATION_STATUSES,
    )

    assert set(STATUS_CONTRADICTIONS) <= set(WORK_AUTHORIZATION_STATUSES)
    assert {key for rules in STATUS_CONTRADICTIONS.values() for key in rules} <= set(
        STATED_ANSWER_QUESTIONS)
    for key, question in STATED_ANSWER_QUESTIONS.items():
        assert _REUSABLE_QUESTIONS[key][1] == question
# --- WP12 round 3: the one-time career_motivation statement -----------------------------------

CAREER_MOTIVATION = ("I look for roles where paid media budgets are tied to measured outcomes and "
                     "where I can build the tracking that shows what worked.")


def test_career_motivation_imports_as_a_verified_fact_and_exports_from_it(
    write_candidate, candidate_store, tmp_path
):
    directory = write_candidate()
    target = tmp_path / "simple-answers.json"
    assert run("export", target).returncode == 0
    data = json.loads(target.read_text())
    assert data["career_motivation"] is None
    data["career_motivation"] = CAREER_MOTIVATION
    target.write_text(json.dumps(data))
    result = run("import", target)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["facts_updated"] == ["career_motivation"] and report["saved_answers_updated"] == 0
    assert "measured outcomes" not in result.stdout  # keys are reported, never the statement
    profile = candidate_store.load("default")
    statement = profile.find_fact("career_motivation")
    assert statement is not None and statement.is_verified
    assert (statement.key, statement.value, statement.source) == (
        "career_motivation", CAREER_MOTIVATION, "user:simple-answers")
    assert statement.verification.method.value == "USER_STATED"
    assert not any("career" in a.question.lower() for a in profile.saved_answers)  # a fact, not a saved answer
    verified_at = statement.verification.verified_at
    # A repeated import writes nothing; a null keeps the statement and its verification time.
    result = run("import", target)
    assert result.returncode == 0 and json.loads(result.stdout)["facts_updated"] == []
    data["career_motivation"] = None
    target.write_text(json.dumps(data))
    assert run("import", target).returncode == 0
    profile = candidate_store.load("default")
    kept = profile.find_fact("career_motivation")
    assert kept is not None and kept.value == CAREER_MOTIVATION
    assert kept.verification.verified_at == verified_at
    exported = tmp_path / "exported.json"
    assert run("export", exported).returncode == 0
    assert json.loads(exported.read_text())["career_motivation"] == CAREER_MOTIVATION
    assert not (directory / "answers.json").exists()


def test_the_career_motivation_fact_is_null_safe_and_idempotent():
    from datetime import UTC, datetime

    from interviewmaxxing_candidate.simple_answers import SimpleAnswers

    when = datetime(2026, 9, 24, tzinfo=UTC)
    assert _round7_answers().career_motivation_fact(confirmed_at=when) is None
    assert _round7_answers(career_motivation="   ").career_motivation is None
    answers = _round7_answers(career_motivation=CAREER_MOTIVATION)
    statement = answers.career_motivation_fact(confirmed_at=when)
    assert statement is not None and statement.id == "career_motivation"
    assert statement.verification.verified_at == when and statement.is_verified
    assert statement.evidence == ["Written by the applicant in the simple answers map: what they look for in a role"]
    assert answers.saved_answer_updates(confirmed_at=when) == []  # never a saved answer
    assert "career_motivation" in SimpleAnswers.model_fields
    assert json.loads((REPO / "examples/simple-answers.example.json").read_text())["career_motivation"] is None


# --- round 10: the work-arrangement preference ---------------------------------------------------

ROUND10_PREFERENCE_PHRASES = [
    "Location Preference", "Work location preference", "Preferred work location",
    "Preferred work arrangement", "What is your preferred work arrangement?",
    "Which work setting do you prefer?", "Remote, hybrid or on-site?",
    "What is your work location preference?",
]


def test_round10_the_work_arrangement_preference_is_a_closed_vocabulary_and_untyped():
    from datetime import UTC, datetime

    from pydantic import ValidationError

    from interviewmaxxing_candidate.simple_answers import _REUSABLE_PHRASES, _REUSABLE_QUESTIONS
    from interviewmaxxing_core import WORK_ARRANGEMENT_PREFERENCE_QUESTION, AnswerScope

    assert _REUSABLE_QUESTIONS["work_arrangement_preference"] == (None, WORK_ARRANGEMENT_PREFERENCE_QUESTION)
    assert _REUSABLE_PHRASES["work_arrangement_preference"] == ROUND10_PREFERENCE_PHRASES
    assert _round7_answers().work_arrangement_preference is None
    assert _round7_answers(work_arrangement_preference="  ").work_arrangement_preference is None
    for given, code in (("Remote", "remote"), ("Fully remote", "remote"), ("WFH", "remote"),
                        ("hybrid", "hybrid"), ("On-site", "on-site"), ("Onsite", "on-site"),
                        ("In office", "on-site")):
        assert _round7_answers(work_arrangement_preference=given).work_arrangement_preference == code
    for given in ("Remote or hybrid", "Anywhere", "Flexible"):
        with pytest.raises(ValidationError, match='"remote", "hybrid", "on-site", or null'):
            _round7_answers(work_arrangement_preference=given)
    answers = _round7_answers(work_arrangement_preference="Remote")
    [saved] = answers.saved_answer_updates(confirmed_at=datetime(2026, 9, 25, tzinfo=UTC))
    assert (saved.question, saved.value, saved.semantic_type, saved.scope) == (
        WORK_ARRANGEMENT_PREFERENCE_QUESTION, "remote", None, AnswerScope.GLOBAL)
    assert saved.match_phrases == ROUND10_PREFERENCE_PHRASES
    assert answers.saved_answer_updates(confirmed_at=datetime(2026, 9, 26, tzinfo=UTC),
                                        current=[saved]) == []  # a repeated import is a no-op


def test_round10_the_preference_round_trips_through_the_script(write_candidate, candidate_store, tmp_path):
    from interviewmaxxing_core import WORK_ARRANGEMENT_PREFERENCE_QUESTION

    directory = write_candidate()
    target = tmp_path / "simple-answers.json"
    assert run("export", target).returncode == 0
    data = json.loads(target.read_text())
    assert data["work_arrangement_preference"] is None
    data["work_arrangement_preference"] = "Remote"  # stored as its code
    target.write_text(json.dumps(data))
    profile_before = (directory / "profile.json").read_bytes()
    result = run("import", target)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["saved_answers_updated"] == 1
    assert "Remote" not in result.stdout
    assert (directory / "profile.json").read_bytes() == profile_before
    profile = candidate_store.load("default")
    [saved] = [a for a in profile.saved_answers if a.question == WORK_ARRANGEMENT_PREFERENCE_QUESTION]
    assert (saved.value, saved.semantic_type) == ("remote", None)
    exported = tmp_path / "exported.json"
    assert run("export", exported).returncode == 0
    assert json.loads(exported.read_text())["work_arrangement_preference"] == "remote"


def test_round10_the_blank_template_and_the_docs_list_every_key():
    from interviewmaxxing_candidate.simple_answers import (
        _CONTACT_KEYS,
        _REUSABLE_QUESTIONS,
        SimpleAnswers,
    )

    example = json.loads((REPO / "examples/simple-answers.example.json").read_text())
    assert set(example) == set(SimpleAnswers.model_fields) == (
        _CONTACT_KEYS | set(_REUSABLE_QUESTIONS) | {"career_motivation", "answer_policies"})
    assert len(_REUSABLE_QUESTIONS) == 45  # round 11: SMS, accommodations; round 13: metro_area;
    # round 15: middle_name, time_zone
    docs = (REPO / "docs/simple-answers.md").read_text()
    for key in _REUSABLE_QUESTIONS:
        assert f"`{key}`" in docs, key
    assert "forty-five explicit reusable answers" in docs


# --- round 11: SMS consent, interview accommodations, Yes/No sentences ------------------------

def test_sms_consent_is_a_reusable_consent_statement_answered_yes_or_no():
    from datetime import UTC, datetime

    from pydantic import ValidationError

    from interviewmaxxing_candidate.simple_answers import _REUSABLE_QUESTIONS, STATEMENT_KEYS
    from interviewmaxxing_core import SemanticType

    semantic, statement = _REUSABLE_QUESTIONS["consent_sms_messages"]
    assert "consent_sms_messages" in STATEMENT_KEYS and semantic is SemanticType.CONSENT
    assert all(word in statement for word in ("text messages", "rates may apply", "STOP", "HELP"))
    with pytest.raises(ValidationError, match='Use "Yes", "No", or null'):
        _round7_answers(consent_sms_messages="maybe")
    updates = _round7_answers(consent_sms_messages="yes").saved_answer_updates(
        confirmed_at=datetime(2026, 9, 25, tzinfo=UTC))
    [saved] = [a for a in updates if a.question == statement]
    assert (saved.value, saved.semantic_type, saved.scope) == ("Yes", SemanticType.CONSENT, "GLOBAL")


def test_interview_accommodations_is_untyped_free_text_with_its_observed_wordings():
    from datetime import UTC, datetime

    from interviewmaxxing_candidate.simple_answers import _REUSABLE_QUESTIONS

    semantic, question = _REUSABLE_QUESTIONS["interview_accommodations"]
    assert semantic is None
    assert question == "Are there any accommodations we can make throughout the interview process?"
    updates = _round7_answers(interview_accommodations="  None needed  ").saved_answer_updates(
        confirmed_at=datetime(2026, 9, 25, tzinfo=UTC))
    [saved] = [a for a in updates if a.question == question]
    assert (saved.value, saved.semantic_type) == ("None needed", None)
    assert any("accommodations" in phrase for phrase in saved.match_phrases)


def test_every_untyped_yes_no_key_has_its_sentences_in_core():
    from pydantic import ValidationError

    from interviewmaxxing_candidate.simple_answers import _REUSABLE_QUESTIONS, STATEMENT_KEYS
    from interviewmaxxing_core import yes_no_sentence

    yes_no = []
    for key, (semantic, question) in _REUSABLE_QUESTIONS.items():
        if semantic is not None or key in STATEMENT_KEYS:
            continue
        try:
            _round7_answers(**{key: "maybe"})
        except ValidationError as error:
            if 'Use "Yes", "No", or null' in str(error):
                yes_no.append((key, question))
    assert {"non_compete_agreement", "family_government_official", "uses_ai_tools"} <= {
        key for key, _ in yes_no}
    for key, question in yes_no:
        assert yes_no_sentence(question, True) and yes_no_sentence(question, False), key
