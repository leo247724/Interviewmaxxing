"""Round 12: the ``answer_policies`` section of the simple-answers map: validation, import as
untyped GLOBAL saved answers, export, the blank template and the script's report. The candidate
is the fictional Avery Example in a per-test IMX home; no value is ever printed."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from interviewmaxxing_candidate.simple_answers import (
    _CONTACT_KEYS,
    _REUSABLE_QUESTIONS,
    SimpleAnswers,
)
from interviewmaxxing_core import AnswerScope, CandidateProfile, SavedAnswer
from interviewmaxxing_core.answer_policies import (
    ANSWER_POLICY_QUESTIONS,
    answer_policy_key,
    policy_contradictions,
    policy_value,
    stated_answer_policies,
)

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/simple_answers.py"
KEYS = ("claims_experience_asked", "meets_experience_thresholds", "certifies_truth",
        "not_current_or_former_employee", "sanctioned_locations")
LISTED = tuple(f"answer_policies.{key}" for key in KEYS)
PERSON = {"claims_experience_asked": "Yes", "meets_experience_thresholds": "Yes",
          "certifies_truth": "Yes", "not_current_or_former_employee": "No",
          "sanctioned_locations": "No"}
"""A complete set of fictional standing answers."""
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)


def run(command: str, path: Path) -> subprocess.CompletedProcess[str]:
    """The simple-answers script against the per-test IMX home (``isolated_imx_home``)."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), command, "--file", str(path)],
        capture_output=True, text=True, timeout=30, check=False,
    )


def answers_map(**values: Any) -> SimpleAnswers:
    """A fictional map with the required contact keys and ``values``."""
    base = dict.fromkeys(_CONTACT_KEYS) | {"first_name": "Avery", "last_name": "Example",
                                           "email": "avery@example.test"}
    return SimpleAnswers.model_validate(base | values)


def rejection(section: Any) -> list[Any]:
    """The problems a map whose ``answer_policies`` is ``section`` is rejected with."""
    with pytest.raises(ValidationError) as error:
        answers_map(answer_policies=section)
    return error.value.errors(include_input=False, include_url=False)


def exported_map(tmp_path: Path, name: str = "simple-answers.json") -> tuple[Path, dict[str, Any]]:
    """Export the stored profile to a new file; the file and its parsed map."""
    target = tmp_path / name
    result = run("export", target)
    assert result.returncode == 0, result.stderr
    assert_reports_policy_keys_only(result)
    return target, json.loads(target.read_text())


def _strings(value: Any) -> Iterator[str]:
    """Every string in a parsed JSON report, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def assert_reports_policy_keys_only(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """The report lists each policy as answer_policies.<key> (never the bare section) and prints
    no policy value or statement; the parsed report."""
    report: dict[str, Any] = json.loads(result.stdout)
    listed = [*report["filled_keys"], *report["unanswered_keys"]]
    assert "answer_policies" not in listed
    assert sorted(k for k in listed if k.startswith("answer_policies")) == sorted(LISTED)
    assert not [s for s in _strings(report) if s.strip().casefold() in {"yes", "no"}]
    for statement in ANSWER_POLICY_QUESTIONS.values():
        assert statement not in result.stdout and statement not in result.stderr
    assert "Answer policy:" not in result.stdout + result.stderr
    return report


def policy_answers(profile: CandidateProfile) -> dict[str, list[SavedAnswer]]:
    """The profile's saved answers to each policy statement."""
    found: dict[str, list[SavedAnswer]] = {}
    for answer in profile.saved_answers:
        key = answer_policy_key(answer)
        if key is not None:
            found.setdefault(key, []).append(answer)
    return found


def with_answers(profile: CandidateProfile, *answers: SavedAnswer) -> CandidateProfile:
    """``profile`` with ``answers`` added to its saved answers."""
    return profile.model_copy(update={"saved_answers": [*profile.saved_answers, *answers]})


def saved_policy(key: str, value: Any, when: datetime, *, job: bool = False) -> SavedAnswer:
    """A saved answer to policy ``key``'s statement, GLOBAL or for the fictional Mock Co job."""
    target = {"job_identity_key": "ats:mock:mock-co:4012", "employer": "Mock Co"} if job else {}
    return SavedAnswer(id=f"sa.{key}.{when:%H%M}.{'job' if job else 'global'}",
                       scope=AnswerScope.JOB if job else AnswerScope.GLOBAL,
                       question=ANSWER_POLICY_QUESTIONS[key], value=value, confirmed_at=when,
                       **target)


# --- validation ------------------------------------------------------------------------------


def test_policy_values_are_normalized_to_yes_or_no_in_any_case_and_spacing() -> None:
    """ "yes", " NO " and the like are kept as "Yes" / "No" under every policy key."""
    for given, expected in (("Yes", "Yes"), ("yes", "Yes"), ("YES", "Yes"), (" NO ", "No"),
                            ("no", "No"), ("\tnO\n", "No")):
        policies = answers_map(answer_policies=dict.fromkeys(KEYS, given)).answer_policies
        assert policies.model_dump() == dict.fromkeys(KEYS, expected), given


def test_a_blank_policy_value_is_null() -> None:
    """An empty or whitespace-only value is null, like an explicit null."""
    policies = answers_map(answer_policies={
        "certifies_truth": "", "sanctioned_locations": "   \n", "claims_experience_asked": None,
    }).answer_policies
    assert policies.model_dump() == dict.fromkeys(KEYS)


@pytest.mark.parametrize("key,given", [
    ("claims_experience_asked", "maybe"),
    ("meets_experience_thresholds", "5+ years"),
    ("certifies_truth", "true"),
    ("not_current_or_former_employee", "N"),
    ("sanctioned_locations", "No."),
])
def test_other_text_is_rejected_at_the_policys_location(key: str, given: str) -> None:
    """Text other than yes/no is rejected with the "Yes", "No", or null message at its key."""
    [problem] = rejection({key: given})
    assert problem["loc"] == ("answer_policies", key)
    assert '"Yes", "No", or null' in problem["msg"]


def test_a_non_text_policy_value_is_rejected_at_its_location() -> None:
    """A JSON true or false, a number or a list is rejected at answer_policies.<key>."""
    for given in (True, False, 1, ["Yes"]):
        [problem] = rejection({"sanctioned_locations": given})
        assert problem["loc"] == ("answer_policies", "sanctioned_locations"), given


def test_a_non_text_policy_value_gets_the_yes_no_or_null_message() -> None:
    """The rejection of a JSON true, a number or a list tells the person the allowed values."""
    for given in (True, 1, ["Yes"]):
        [problem] = rejection({"sanctioned_locations": given})
        assert '"Yes", "No", or null' in problem["msg"], given


def test_an_unknown_policy_key_is_rejected() -> None:
    """A key outside the five policies is rejected at answer_policies.<that key>."""
    [problem] = rejection({"certifies_truth": "Yes", "answers_every_screener": "Yes"})
    assert problem["loc"] == ("answer_policies", "answers_every_screener")


def test_a_section_that_is_not_an_object_is_rejected() -> None:
    """The section must be an object or null: text, a list, a number or true is rejected."""
    for section in ("Yes", "", ["Yes"], 1, True):
        problems = rejection(section)
        assert [p["loc"] for p in problems] == [("answer_policies",)], section


def test_an_omitted_empty_or_null_section_means_no_policy() -> None:
    """Omitting the section, {} and JSON null all leave every policy null."""
    data = answers_map().model_dump()
    del data["answer_policies"]
    for variant in (data, data | {"answer_policies": None}, data | {"answer_policies": {}}):
        policies = SimpleAnswers.model_validate(variant).answer_policies
        assert policies.model_dump() == dict.fromkeys(KEYS)


# --- import (saved_answer_updates) --------------------------------------------------------------


def test_each_stated_policy_imports_as_one_untyped_global_answer() -> None:
    """Every non-null policy is one GLOBAL answer to its statement: untyped, no job, no phrases,
    "Yes"/"No", confirmed at the import time."""
    answers = answers_map(answer_policies={key: value.lower() for key, value in PERSON.items()})
    updates = answers.saved_answer_updates(confirmed_at=NOW)
    by_question = {a.question: a for a in updates}
    assert len(updates) == len(by_question) == len(KEYS)
    for key, value in PERSON.items():
        saved = by_question[ANSWER_POLICY_QUESTIONS[key]]
        assert (saved.scope, saved.semantic_type, saved.value) == (AnswerScope.GLOBAL, None, value)
        assert (saved.job_identity_key, saved.job_url, saved.employer) == (None, None, None)
        assert saved.match_phrases == [] and saved.confirmed_at == NOW
    assert {key: policy_value(a) for key, a in stated_answer_policies(updates).items()} == PERSON


def test_policy_ids_name_their_key_and_every_other_key_keeps_the_simple_answer_prefix() -> None:
    """Each policy's id is answer_policy_<key>_<uuid hex>; every other simple-answers key
    (the derived education month and year included) keeps simple_answer_<uuid hex>."""
    # Any valid value will do (the test is about ids): yes/no keys take No, text keys any text.
    valid = {"authorized_to_work_us": "Yes", "work_authorization_status": "us_citizen",
             "work_arrangement_preference": "remote", "education_start_date": "2017-08",
             "education_end_date": "2022-05"}
    others = {key: valid.get(key, "No") for key in _REUSABLE_QUESTIONS}
    updates = answers_map(answer_policies=PERSON, **others).saved_answer_updates(confirmed_at=NOW)
    assert len({a.id for a in updates}) == len(updates)
    policies = {answer_policy_key(a): a for a in updates if answer_policy_key(a) is not None}
    assert set(policies) == set(KEYS)
    for key, answer in policies.items():
        assert re.fullmatch(rf"answer_policy_{key}_[0-9a-f]{{32}}", answer.id), answer.id
    rest = [a for a in updates if answer_policy_key(a) is None]
    assert len(rest) == len(_REUSABLE_QUESTIONS) + 4  # education start/end month and year
    for answer in rest:
        assert re.fullmatch(r"simple_answer_[0-9a-f]{32}", answer.id), (answer.question, answer.id)


def test_a_null_policy_adds_nothing() -> None:
    """Null policies add no answer, alone or beside an earlier saved policy."""
    assert answers_map().saved_answer_updates(confirmed_at=NOW) == []
    earlier = answers_map(answer_policies={"certifies_truth": "Yes"}).saved_answer_updates(
        confirmed_at=NOW)
    cleared = answers_map(answer_policies={"certifies_truth": None})
    assert cleared.saved_answer_updates(confirmed_at=LATER, current=earlier) == []


def test_re_importing_the_same_policies_adds_nothing() -> None:
    """A repeated import of the same values, in any case, is a no-op."""
    first = answers_map(answer_policies=PERSON).saved_answer_updates(confirmed_at=NOW)
    again = answers_map(answer_policies={key: value.upper() for key, value in PERSON.items()})
    assert again.saved_answer_updates(confirmed_at=LATER, current=first) == []


def test_a_changed_policy_adds_one_newer_answer() -> None:
    """Changing one policy adds exactly one newer answer with the new value; the rest add nothing."""
    first = answers_map(answer_policies=PERSON).saved_answer_updates(confirmed_at=NOW)
    changed = answers_map(answer_policies=PERSON | {"not_current_or_former_employee": "Yes"})
    [update] = changed.saved_answer_updates(confirmed_at=LATER, current=first)
    assert update.question == ANSWER_POLICY_QUESTIONS["not_current_or_former_employee"]
    assert (update.value, update.confirmed_at) == ("Yes", LATER)
    assert (update.scope, update.semantic_type) == (AnswerScope.GLOBAL, None)
    assert stated_answer_policies([*first, update])["not_current_or_former_employee"] == update


def test_policies_create_no_facts_and_other_keys_import_alongside() -> None:
    """Policies never become facts; the map's other keys import beside them unchanged."""
    answers = answers_map(answer_policies=PERSON, previously_employed_here="no",
                          previously_interviewed_here="No")
    assert answers.career_motivation_fact(confirmed_at=NOW) is None
    updates = answers.saved_answer_updates(confirmed_at=NOW)
    assert len({a.question for a in updates}) == len(updates) == len(KEYS) + 2
    employed = next(a for a in updates
                    if a.question == _REUSABLE_QUESTIONS["previously_employed_here"][1])
    assert employed.value == "No" and employed.match_phrases
    assert answer_policy_key(employed) is None
    assert set(stated_answer_policies(updates)) == set(KEYS)
    assert policy_contradictions("not_current_or_former_employee", updates) == []


def test_a_saved_yes_to_previously_employed_here_contradicts_the_imported_policy() -> None:
    """The map takes previously_employed_here Yes beside the No policy (the policy is then not
    applied): the imported Yes is exactly what policy_contradictions returns."""
    answers = answers_map(answer_policies={"not_current_or_former_employee": "No"},
                          previously_employed_here="yes", previously_interviewed_here="no")
    updates = answers.saved_answer_updates(confirmed_at=NOW)
    [employed] = [a for a in updates
                  if a.question == _REUSABLE_QUESTIONS["previously_employed_here"][1]]
    assert policy_contradictions("not_current_or_former_employee", updates) == [employed]
    assert set(stated_answer_policies(updates)) == {"not_current_or_former_employee"}


# --- export (from_profile) ------------------------------------------------------------------------


def test_export_gives_each_policys_newest_global_answer_and_null_otherwise(
    fictional_candidate: CandidateProfile,
) -> None:
    """from_profile exports each policy's newest GLOBAL answer as "Yes"/"No", null when there is
    none; a JOB-scoped answer is never exported."""
    assert SimpleAnswers.from_profile(fictional_candidate).answer_policies.model_dump() == (
        dict.fromkeys(KEYS))
    profile = with_answers(
        fictional_candidate,
        saved_policy("claims_experience_asked", "Yes", NOW),
        saved_policy("claims_experience_asked", "No", LATER),
        saved_policy("certifies_truth", "yes", NOW),
        saved_policy("certifies_truth", "No", LATER, job=True),
        saved_policy("sanctioned_locations", "No", LATER, job=True),
    )
    assert SimpleAnswers.from_profile(profile).answer_policies.model_dump() == {
        "claims_experience_asked": "No", "meets_experience_thresholds": None,
        "certifies_truth": "Yes", "not_current_or_former_employee": None,
        "sanctioned_locations": None,
    }


def test_export_refuses_newest_policy_answers_that_disagree(
    fictional_candidate: CandidateProfile,
) -> None:
    """Newest answers that disagree stop the export with a message naming answer_policies.<key>."""
    profile = with_answers(
        fictional_candidate,
        saved_policy("sanctioned_locations", "No", NOW),
        SavedAnswer(id="sa.sanctions.conflict", scope=AnswerScope.GLOBAL,
                    question=ANSWER_POLICY_QUESTIONS["sanctioned_locations"], value="Yes",
                    confirmed_at=NOW),
    )
    with pytest.raises(ValueError, match=r"answer_policies\.sanctioned_locations"):
        SimpleAnswers.from_profile(profile)


# --- template and docs ------------------------------------------------------------------------------


def test_the_template_has_the_section_with_all_five_keys_null() -> None:
    """examples/simple-answers.example.json has answer_policies with the five keys, all null,
    and parses."""
    example = json.loads((REPO / "examples/simple-answers.example.json").read_text())
    assert example["answer_policies"] == dict.fromkeys(KEYS)
    parsed = SimpleAnswers.model_validate(example)
    assert parsed.answer_policies.model_dump() == dict.fromkeys(KEYS)


def test_the_docs_name_the_section_and_every_policy_key_in_backticks() -> None:
    """docs/simple-answers.md names `answer_policies` and every policy key in backticks."""
    docs = (REPO / "docs/simple-answers.md").read_text()
    for key in ("answer_policies", *KEYS):
        assert f"`{key}`" in docs, key


# --- the script -------------------------------------------------------------------------------------


def test_validate_and_import_list_each_policy_and_save_it_at_the_import_time(
    write_candidate: Any, candidate_store: Any, tmp_path: Path,
) -> None:
    """validate writes nothing; import saves one untyped GLOBAL answer per stated policy at the
    import time, leaves the profile file alone, and both list answer_policies.<key> entries
    without printing a policy value or statement."""
    directory = write_candidate()
    target, data = exported_map(tmp_path)
    data["answer_policies"] = {"certifies_truth": "yes", "sanctioned_locations": " NO "}
    target.write_text(json.dumps(data))
    profile_before = (directory / "profile.json").read_bytes()
    filled = {"answer_policies.certifies_truth", "answer_policies.sanctioned_locations"}

    result = run("validate", target)
    assert result.returncode == 0, result.stderr
    report = assert_reports_policy_keys_only(result)
    assert filled <= set(report["filled_keys"])
    assert set(LISTED) - filled <= set(report["unanswered_keys"])
    assert not (directory / "answers.json").exists()

    before = datetime.now(UTC)
    result = run("import", target)
    after = datetime.now(UTC)
    assert result.returncode == 0, result.stderr
    report = assert_reports_policy_keys_only(result)
    assert filled <= set(report["filled_keys"])
    assert set(LISTED) - filled <= set(report["unanswered_keys"])
    assert report["saved_answers_updated"] == 2 and report["profile_updated"] is False
    assert report["facts_updated"] == []
    assert (directory / "profile.json").read_bytes() == profile_before
    policies = policy_answers(candidate_store.load("default"))
    assert set(policies) == {"certifies_truth", "sanctioned_locations"}
    for key, value in (("certifies_truth", "Yes"), ("sanctioned_locations", "No")):
        [saved] = policies[key]
        assert (saved.scope, saved.semantic_type, saved.value) == (AnswerScope.GLOBAL, None, value)
        assert saved.match_phrases == [] and before <= saved.confirmed_at <= after
        assert saved.id.startswith(f"answer_policy_{key}_"), saved.id


def test_re_import_changes_nothing_and_a_changed_policy_replaces_the_older_one(
    write_candidate: Any, candidate_store: Any, tmp_path: Path,
) -> None:
    """Re-importing the same policies adds nothing and leaves answers.json unchanged; a changed
    value adds one newer answer that the store keeps instead of the older one."""
    directory = write_candidate()
    target, data = exported_map(tmp_path)
    data.update(answer_policies=PERSON, previously_employed_here="No")  # another key alongside
    target.write_text(json.dumps(data))
    result = run("import", target)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["saved_answers_updated"] == len(KEYS) + 1
    answers_before = (directory / "answers.json").read_bytes()

    result = run("import", target)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["saved_answers_updated"] == 0
    assert (directory / "answers.json").read_bytes() == answers_before

    data["answer_policies"] = PERSON | {"certifies_truth": "No"}
    target.write_text(json.dumps(data))
    result = run("import", target)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["saved_answers_updated"] == 1
    stored = json.loads((directory / "answers.json").read_text())
    statement = ANSWER_POLICY_QUESTIONS["certifies_truth"]
    assert [a["value"] for a in stored if a["question"] == statement] == ["No"]
    profile = candidate_store.load("default")
    [kept] = policy_answers(profile)["certifies_truth"]
    assert kept.value == "No"
    assert {key: policy_value(a) for key, a in stated_answer_policies(
        profile.saved_answers).items()} == PERSON | {"certifies_truth": "No"}
    [employed] = [a for a in profile.saved_answers
                  if a.question == _REUSABLE_QUESTIONS["previously_employed_here"][1]]
    assert employed.value == "No" and employed.id.startswith("simple_answer_")
    assert all(a.id.startswith(f"answer_policy_{key}_")
               for key, group in policy_answers(profile).items() for a in group)


def test_the_script_export_stops_on_disagreeing_policy_answers(
    write_candidate: Any, tmp_path: Path,
) -> None:
    """Export names answer_policies.<key> when the newest policy answers disagree, writes no
    file and prints no statement."""
    statement = ANSWER_POLICY_QUESTIONS["certifies_truth"]
    write_candidate(answers=[
        {"id": f"answer_policy_certifies_truth_{value.lower()}", "scope": "GLOBAL",
         "question": statement, "value": value, "confirmed_at": "2026-09-25T12:00:00Z"}
        for value in ("Yes", "No")
    ])
    target = tmp_path / "simple-answers.json"
    result = run("export", target)
    assert result.returncode == 1
    assert "answer_policies.certifies_truth" in result.stderr
    assert statement not in result.stderr + result.stdout
    assert not target.exists()


def test_a_null_policy_never_erases_a_saved_one(
    write_candidate: Any, candidate_store: Any, tmp_path: Path,
) -> None:
    """A policy set back to null, or a null section, imports nothing and keeps the saved policy,
    which the next export still shows."""
    directory = write_candidate()
    target, data = exported_map(tmp_path)
    data["answer_policies"] = {"not_current_or_former_employee": "No"}
    target.write_text(json.dumps(data))
    result = run("import", target)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["saved_answers_updated"] == 1
    answers_before = (directory / "answers.json").read_bytes()
    for section in ({"not_current_or_former_employee": None}, None):
        data["answer_policies"] = section
        target.write_text(json.dumps(data))
        result = run("import", target)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["saved_answers_updated"] == 0
        assert (directory / "answers.json").read_bytes() == answers_before
    [kept] = policy_answers(candidate_store.load("default"))["not_current_or_former_employee"]
    assert kept.value == "No"
    _, exported = exported_map(tmp_path, "exported.json")
    assert exported["answer_policies"] == dict.fromkeys(KEYS) | {
        "not_current_or_former_employee": "No"}


def test_export_import_export_round_trip_is_stable(write_candidate: Any, tmp_path: Path) -> None:
    """A map exported after an import re-imports as a no-op and exports the same map again."""
    write_candidate()
    target, data = exported_map(tmp_path)
    assert data["answer_policies"] == dict.fromkeys(KEYS)
    stated = PERSON | {"meets_experience_thresholds": None}
    data["answer_policies"] = {key: v.lower() if v else v for key, v in stated.items()}
    target.write_text(json.dumps(data))
    assert run("import", target).returncode == 0
    first, first_map = exported_map(tmp_path, "first.json")
    assert first_map["answer_policies"] == stated
    result = run("import", first)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["saved_answers_updated"] == 0
    _, second_map = exported_map(tmp_path, "second.json")
    assert second_map == first_map


@pytest.mark.parametrize("section,location,message", [
    ({"certifies_truth": "Only-Sometimes"}, "answer_policies.certifies_truth",
     '"Yes", "No", or null'),
    ({"certifies_truth": "Yes", "Only-Sometimes": "Yes"}, "answer_policies.Only-Sometimes", None),
])
def test_an_invalid_section_writes_nothing_and_never_echoes_values(
    write_candidate: Any, tmp_path: Path, section: dict[str, str], location: str,
    message: str | None,
) -> None:
    """An invalid policy value or an unknown policy key stops validate and import before any
    write; stderr names the location, never the value, contact details or other answers."""
    directory = write_candidate()
    target, data = exported_map(tmp_path)
    data.update(email="changed@example.test", desired_salary="$150,000", answer_policies=section)
    target.write_text(json.dumps(data))
    profile_before = (directory / "profile.json").read_bytes()
    for command in ("validate", "import"):
        result = run(command, target)
        assert result.returncode == 1, command
        assert location in result.stderr
        assert message is None or message in result.stderr
        assert "Only-Sometimes" not in result.stderr.replace(location, "")
        assert "changed@example.test" not in result.stderr + result.stdout
        assert "$150,000" not in result.stderr + result.stdout
    assert (directory / "profile.json").read_bytes() == profile_before
    assert not (directory / "answers.json").exists()
