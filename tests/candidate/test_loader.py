"""LocalCandidateStore.load: layout, resume resolution, validation and provenance."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_core import (
    AnswerScope,
    CandidateLoader,
    CandidateNotFound,
    CandidateProfile,
    CandidateProfileInvalid,
    LocalPaths,
    SavedAnswerWriter,
    VerificationStatus,
    sha256_file,
)

REPO = Path(__file__).resolve().parents[2]
CORE_FIXTURES = REPO / "tests" / "fixtures" / "core"
EXAMPLES = REPO / "examples"
FICTIONAL_RESUME = CORE_FIXTURES / "resume-avery-example.pdf"
WriteCandidate = Callable[..., Path]


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2))


def test_implements_core_interfaces(candidate_store: LocalCandidateStore) -> None:
    assert isinstance(candidate_store, CandidateLoader)
    assert isinstance(candidate_store, SavedAnswerWriter)


def test_loads_canonical_profile(
    write_candidate: WriteCandidate,
    candidate_store: LocalCandidateStore,
    fictional_candidate: CandidateProfile,
) -> None:
    directory = write_candidate("default")
    profile = candidate_store.load("default")

    assert isinstance(profile, CandidateProfile)
    assert profile.id == "default"
    assert profile.resume.path == str((directory / FICTIONAL_RESUME.name).resolve())
    assert profile.resume.verify()
    expected = fictional_candidate.model_copy(
        update={"id": "default", "resume": profile.resume}
    ).model_dump()
    assert profile.model_dump() == expected


def test_from_env_uses_profile_dir(
    write_candidate: WriteCandidate, paths: LocalPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_candidate("default")
    assert LocalCandidateStore.from_env().load("default").id == "default"

    other = paths.home / "elsewhere"
    shutil.copytree(paths.profile_dir, other)
    monkeypatch.setenv("IMX_PROFILE_DIR", str(other))
    store = LocalCandidateStore.from_env()
    assert store.profile_dir == other.resolve()
    assert store.load("default").resume.path.startswith(str(other.resolve()))


# --- relative paths -----------------------------------------------------------------


def test_relative_resume_path_is_relative_to_profile_file_not_cwd(
    write_candidate: WriteCandidate,
    candidate_store: LocalCandidateStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def nested(data: dict[str, Any]) -> None:
        data["resume"]["path"] = "docs/../docs/cv.pdf"

    directory = write_candidate("default", edit=nested)
    (directory / "docs").mkdir()
    shutil.copyfile(FICTIONAL_RESUME, directory / "docs" / "cv.pdf")
    # A same-named file in the working directory must not be picked up.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "cv.pdf").write_bytes(b"not the resume")

    profile = candidate_store.load("default")
    assert profile.resume.path == str((directory / "docs" / "cv.pdf").resolve())
    assert profile.resume.filename == "resume-avery-example.pdf"  # declared in the profile


def test_relative_profile_dir_resolves_at_construction(
    write_candidate: WriteCandidate, paths: LocalPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_candidate("default")
    monkeypatch.chdir(paths.home)
    store = LocalCandidateStore("profile")
    monkeypatch.chdir(paths.home.parent)
    assert store.profile_dir == paths.profile_dir.resolve()
    assert store.load("default").id == "default"


def test_absolute_and_home_relative_resume_paths(
    write_candidate: WriteCandidate,
    candidate_store: LocalCandidateStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "home-dir" / "Documents"
    outside.mkdir(parents=True)
    shutil.copyfile(FICTIONAL_RESUME, outside / "resume.pdf")

    write_candidate("abs", edit=lambda d: d["resume"].update(path=str(outside / "resume.pdf")))
    assert candidate_store.load("abs").resume.path == str((outside / "resume.pdf").resolve())

    monkeypatch.setenv("HOME", str(tmp_path / "home-dir"))
    write_candidate("tilde", edit=lambda d: d["resume"].update(path="~/Documents/resume.pdf"))
    assert candidate_store.load("tilde").resume.path == str((outside / "resume.pdf").resolve())


# --- resume checks ------------------------------------------------------------------


def test_omitted_resume_digest_and_media_type_are_computed(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    def minimal(data: dict[str, Any]) -> None:
        data["resume"] = {"id": "resume_supplied", "path": "resume-avery-example.pdf"}

    directory = write_candidate("default", edit=minimal)
    before = (directory / "profile.json").read_bytes()
    report = candidate_store.load_report("default")

    resume = report.profile.resume
    assert report.resume_digest_computed
    assert resume.sha256 == sha256_file(FICTIONAL_RESUME)
    assert resume.size_bytes == FICTIONAL_RESUME.stat().st_size
    assert resume.media_type == "application/pdf"
    assert resume.filename == "resume-avery-example.pdf"
    assert resume.extracted_text is None  # never extracted by the loader
    assert (directory / "profile.json").read_bytes() == before  # load never writes


def test_unknown_resume_type_needs_media_type(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    def odd(data: dict[str, Any]) -> None:
        data["resume"] = {"id": "resume_supplied", "path": "resume.xyz"}

    directory = write_candidate("default", edit=odd)
    shutil.copyfile(FICTIONAL_RESUME, directory / "resume.xyz")
    with pytest.raises(CandidateProfileInvalid, match=r"resume\.media_type"):
        candidate_store.load("default")


def test_changed_resume_is_rejected(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    directory = write_candidate("default")
    (directory / FICTIONAL_RESUME.name).write_bytes(b"%PDF-1.4 a different resume")
    with pytest.raises(CandidateProfileInvalid, match=r"does not match resume\.sha256"):
        candidate_store.load("default")


def test_missing_resume_names_resolved_path(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    directory = write_candidate("default")
    (directory / FICTIONAL_RESUME.name).unlink()
    with pytest.raises(CandidateProfileInvalid) as exc:
        candidate_store.load("default")
    message = str(exc.value)
    assert "resume file not found" in message
    assert str((directory / FICTIONAL_RESUME.name).resolve()) in message
    assert "relative to" in message


@pytest.mark.parametrize(
    ("resume", "needle"),
    [
        (None, '"resume" is required'),
        ({"id": "resume_supplied"}, "resume.path is required"),
        ({"id": "resume_supplied", "path": "  "}, "resume.path is required"),
        ({"id": "resume_supplied", "path": "."}, "not a regular file"),
    ],
)
def test_resume_shape_errors(
    write_candidate: WriteCandidate,
    candidate_store: LocalCandidateStore,
    resume: Any,
    needle: str,
) -> None:
    def edit(data: dict[str, Any]) -> None:
        if resume is None:
            del data["resume"]
        else:
            data["resume"] = resume

    write_candidate("default", edit=edit)
    with pytest.raises(CandidateProfileInvalid, match=needle):
        candidate_store.load("default")


def test_empty_resume_is_rejected(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    def empty(data: dict[str, Any]) -> None:
        data["resume"] = {"id": "resume_supplied", "path": "empty.pdf"}

    directory = write_candidate("default", edit=empty)
    (directory / "empty.pdf").write_bytes(b"")
    with pytest.raises(CandidateProfileInvalid, match="resume file is empty"):
        candidate_store.load("default")


def test_wrong_declared_size_is_rejected(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    def size(data: dict[str, Any]) -> None:
        del data["resume"]["sha256"]
        data["resume"]["size_bytes"] = 1

    write_candidate("default", edit=size)
    with pytest.raises(CandidateProfileInvalid, match=r"resume\.size_bytes"):
        candidate_store.load("default")


# --- missing and malformed profiles --------------------------------------------------


def test_missing_profile_raises_not_found_with_expected_path(
    candidate_store: LocalCandidateStore, paths: LocalPaths
) -> None:
    with pytest.raises(CandidateNotFound) as exc:
        candidate_store.load("default")
    assert str(paths.profile_dir.resolve() / "default" / "profile.json") in str(exc.value)
    assert not candidate_store.exists("default")


@pytest.mark.parametrize("candidate_id", ["", "..", "../default", "a/b", ".hidden", "x" * 129])
def test_unsafe_candidate_ids_are_not_found(
    candidate_store: LocalCandidateStore, candidate_id: str
) -> None:
    with pytest.raises(CandidateNotFound, match="invalid candidate id"):
        candidate_store.load(candidate_id)
    assert not candidate_store.exists(candidate_id)


def test_profile_path_that_is_a_directory(
    candidate_store: LocalCandidateStore, paths: LocalPaths
) -> None:
    (paths.profile_dir / "default" / "profile.json").mkdir(parents=True)
    with pytest.raises(CandidateProfileInvalid, match="not a regular file"):
        candidate_store.load("default")


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ('{"id": "default",', "invalid JSON at line 1 column"),
        ('{"id": "default", "id": "default"}', "duplicate key 'id'"),
        ('{"id": "default", "x": NaN}', "NaN is not allowed"),
        ("[]", "expected a JSON object"),
        (b"\xff\xfe".decode("latin-1"), "not UTF-8"),
    ],
)
def test_malformed_json(
    candidate_store: LocalCandidateStore, paths: LocalPaths, text: str, needle: str
) -> None:
    path = paths.profile_dir / "default" / "profile.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(text.encode("latin-1"))
    with pytest.raises(CandidateProfileInvalid, match=needle) as exc:
        candidate_store.load("default")
    assert str(path) in str(exc.value)


def test_candidate_id_must_match_directory(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    write_candidate("default", edit=lambda d: d.update(id="cand_avery_example"))
    with pytest.raises(CandidateProfileInvalid, match="stays fixed across edits"):
        candidate_store.load("default")


def test_editing_profile_keeps_candidate_id(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    directory = write_candidate("default")
    first = candidate_store.load("default")
    data = json.loads((directory / "profile.json").read_text())
    data["identity"]["email"] = "avery.new@example.test"
    data["identity"]["first_name"] = "Avery Jo"
    write_json(directory / "profile.json", data)
    second = candidate_store.load("default")
    assert second.id == first.id == "default"
    assert second.identity.email == "avery.new@example.test"


@pytest.mark.parametrize(
    ("edit", "needle"),
    [
        (lambda d: d["facts"][0].pop("verification"), "facts[0].verification: Field required"),
        (lambda d: d["facts"][1].pop("value"), "facts[1].value: Field required"),
        (lambda d: d["identity"].pop("verified_at"), "identity.verified_at: Field required"),
        (lambda d: d["identity"].update(email="not-an-email"), "identity.email"),
        (lambda d: d.update(nickname="Ave"), "nickname: Extra inputs are not permitted"),
        (
            lambda d: d["facts"][0]["verification"].update(method=None),
            "VERIFIED facts need method and verified_at",
        ),
        (
            lambda d: d["facts"][4]["verification"].update(verified_at="2026-09-01T12:00:00Z"),
            "UNVERIFIED facts must not carry",
        ),
        (
            lambda d: d["facts"][0]["verification"].update(verified_at="2026-09-01T12:00:00"),
            "timezone",
        ),
        (lambda d: d["experience"][0]["fact_ids"].append("fact.nope"), "unknown facts"),
        (lambda d: d["facts"].append(dict(d["facts"][0])), "duplicate candidate fact ids"),
        (
            lambda d: d["saved_answers"][3].update(job_identity_key=None),
            "JOB-scoped answers need job_identity_key or job_url",
        ),
    ],
)
def test_schema_errors_name_file_and_location(
    write_candidate: WriteCandidate,
    candidate_store: LocalCandidateStore,
    edit: Any,
    needle: str,
) -> None:
    directory = write_candidate("default", edit=edit)
    with pytest.raises(CandidateProfileInvalid) as exc:
        candidate_store.load("default")
    assert str(directory / "profile.json") in str(exc.value)
    assert needle in str(exc.value)


# --- verification and value preservation ---------------------------------------------


def test_unverified_facts_stay_unverified(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    def certain_but_unverified(data: dict[str, Any]) -> None:
        data["facts"][4]["confidence"] = 1.0

    write_candidate("default", edit=certain_but_unverified)
    report = candidate_store.load_report("default")
    fact = report.profile.fact("fact.team_size")

    assert fact.verification.status is VerificationStatus.UNVERIFIED
    assert fact.verification.verified_at is None and fact.verification.method is None
    assert fact.confidence == 1.0 and not fact.is_verified
    assert report.unverified_fact_ids == ("fact.team_size",)
    assert "fact.team_size" not in {f.id for f in report.profile.verified_facts()}
    assert "fact.team_size" not in {f.id for f in report.profile.verified_only().facts}
    assert any("fact.team_size" in w and "UNVERIFIED" in w for w in report.warnings())


def test_false_zero_empty_and_null_values_survive(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    verified = {
        "status": "VERIFIED",
        "method": "USER_STATED",
        "verified_at": "2026-09-01T12:00:00Z",
    }
    values: dict[str, Any] = {
        "fact.false": False,
        "fact.zero": 0,
        "fact.zero_float": 0.0,
        "fact.empty_text": "",
        "fact.empty_list": [],
        "fact.null": None,
    }

    def add(data: dict[str, Any]) -> None:
        for fact_id, value in values.items():
            data["facts"].append(
                {
                    "id": fact_id,
                    "key": fact_id.removeprefix("fact."),
                    "value": value,
                    "source": "user",
                    "verification": verified,
                }
            )

    write_candidate("default", edit=add)
    profile = candidate_store.load("default")
    for fact_id, value in values.items():
        loaded = profile.fact(fact_id).value
        assert loaded == value and type(loaded) is type(value), fact_id
    assert profile.fact("fact.false").is_verified


def test_loads_examples(candidate_store: LocalCandidateStore, paths: LocalPaths) -> None:
    directory = paths.profile_dir / "default"
    directory.mkdir(parents=True)
    shutil.copyfile(EXAMPLES / "candidate.example.json", directory / "profile.json")
    shutil.copyfile(EXAMPLES / "answers.example.json", directory / "answers.json")
    shutil.copyfile(FICTIONAL_RESUME, directory / "resume.pdf")

    report = candidate_store.load_report("default")
    profile = report.profile
    assert profile.identity.full_name == "Jordan Sample"
    assert report.resume_digest_computed
    assert profile.fact("fact.direct_reports").value == 0
    assert report.unverified_fact_ids == ("fact.budget_managed",)
    sponsorship = profile.find_saved_answer("sa.sponsorship")
    assert sponsorship is not None and sponsorship.value is False
    start = profile.find_saved_answer("sa.sample_co_start")
    assert start is not None and start.scope is AnswerScope.JOB
    assert set(report.answer_sources.values()) == {directory / "answers.json"}
