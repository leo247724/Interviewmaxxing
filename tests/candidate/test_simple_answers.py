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
    assert set(parsed.model_dump().values()) == {None}
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
