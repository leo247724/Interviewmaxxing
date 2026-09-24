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
