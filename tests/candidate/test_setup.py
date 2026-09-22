"""Candidate setup: resume uploads, contact profile upserts and the setup view."""

from __future__ import annotations

import hashlib
import json
import stat
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_candidate import (
    CandidateSetup,
    DamagedResume,
    LocalCandidateStore,
    ResumeNotFound,
    ResumeOrigin,
    ResumeRejected,
    StoredResume,
    safe_resume_filename,
)
from interviewmaxxing_core import (
    AnswerScope,
    CandidateIdentity,
    CandidateNotFound,
    CandidateProfileInvalid,
    LocalPaths,
    SavedAnswer,
    SemanticType,
)

REPO = Path(__file__).resolve().parents[2]
FICTIONAL_RESUME = REPO / "tests" / "fixtures" / "core" / "resume-avery-example.pdf"
PDF = FICTIONAL_RESUME.read_bytes()
WriteCandidate = Callable[..., Path]
CONFIRMED = datetime(2026, 9, 22, 18, 30, tzinfo=UTC)
T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)


def identity(**changes: Any) -> CandidateIdentity:
    base: dict[str, Any] = {
        "first_name": "Riley",
        "last_name": "Fixture",
        "email": "riley.fixture@example.test",
        "phone": "+1 555 010 0123",
        "verified_at": CONFIRMED,
    }
    return CandidateIdentity.model_validate(base | changes)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def saved(answer_id: str, value: Any, *, question: str = "Sponsorship?") -> SavedAnswer:
    return SavedAnswer(
        id=answer_id,
        scope=AnswerScope.GLOBAL,
        semantic_type=SemanticType.SPONSORSHIP,
        question=question,
        value=value,
        confirmed_at=T0,
    )


# --- initial upload and setup ---------------------------------------------------------


def test_upload_then_create_profile(
    candidate_store: LocalCandidateStore, paths: LocalPaths
) -> None:
    empty = candidate_store.candidate_setup("default")
    assert empty == CandidateSetup("default", None, (), None, False, None)

    upload = candidate_store.store_resume(
        "default", filename="Riley Fixture CV.pdf", content=PDF, media_type="application/pdf"
    )
    assert upload.origin is ResumeOrigin.UPLOADED
    assert upload.id.startswith("resume_") and len(upload.id) == len("resume_") + 32
    assert upload.artifact.sha256 == hashlib.sha256(PDF).hexdigest()
    assert upload.artifact.size_bytes == len(PDF)
    assert upload.artifact.filename == "Riley Fixture CV.pdf"
    assert upload.artifact.media_type == "application/pdf"
    assert upload.uploaded_at is not None and upload.uploaded_at.tzinfo is not None
    stored = Path(upload.artifact.path)
    assert stored.read_bytes() == PDF
    assert mode(stored) == 0o400 and mode(stored.parent / "meta.json") == 0o400
    assert mode(stored.parent) == 0o700 and mode(stored.parent.parent) == 0o700
    assert mode(paths.profile_dir / "default") == 0o700

    # An upload alone is not a profile and selects nothing.
    assert not candidate_store.exists("default")
    before = candidate_store.candidate_setup("default")
    assert before.identity is None and before.selected_resume_id is None
    assert [r.id for r in before.resumes] == [upload.id]
    with pytest.raises(CandidateNotFound):
        candidate_store.load("default")

    profile = candidate_store.upsert_profile("default", identity=identity(), resume_id=upload.id)
    assert profile.id == "default"
    assert profile.identity == identity()
    assert profile.identity.verified_at == CONFIRMED
    assert profile.resume.id == upload.id and profile.resume.path == upload.artifact.path
    assert profile.facts == [] and profile.saved_answers == []

    profile_file = paths.profile_dir / "default" / "profile.json"
    assert mode(profile_file) == 0o600
    raw = json.loads(profile_file.read_text())
    assert raw["resume"]["path"] == f"resumes/{upload.id}/Riley Fixture CV.pdf"
    assert list(raw)[:3] == ["id", "identity", "resume"]

    after = LocalCandidateStore.from_paths(paths).candidate_setup("default")
    assert after.complete and after.problem is None
    assert after.identity == identity() and after.selected_resume_id == upload.id


def test_clock_sets_upload_time(paths: LocalPaths) -> None:
    moment = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)
    store = LocalCandidateStore.from_paths(paths, clock=lambda: moment)
    upload = store.store_resume("default", filename="cv.pdf", content=PDF)
    assert upload.uploaded_at == moment
    assert store.get_resume("default", upload.id).uploaded_at == moment


def test_resume_selection_is_stable_across_reload(
    candidate_store: LocalCandidateStore, paths: LocalPaths
) -> None:
    first = candidate_store.store_resume("default", filename="first.pdf", content=PDF)
    second = candidate_store.store_resume("default", filename="second.txt", content=b"Riley\n")
    candidate_store.upsert_profile("default", identity=identity(), resume_id=first.id)
    candidate_store.upsert_profile("default", identity=identity(), resume_id=second.id)

    reloaded = LocalCandidateStore.from_paths(paths)
    setup = reloaded.candidate_setup("default")
    assert setup.selected_resume_id == second.id
    assert [r.id for r in setup.resumes] == [first.id, second.id]
    assert reloaded.load("default").resume.media_type == "text/plain"
    assert reloaded.get_resume("default", first.id).artifact.filename == "first.pdf"

    reloaded.upsert_profile("default", identity=identity(), resume_id=first.id)
    assert LocalCandidateStore.from_paths(paths).load("default").resume.id == first.id


# --- updates preserve everything else --------------------------------------------------


def test_update_preserves_facts_answers_and_conflicts(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    def embed_conflict(data: dict[str, Any]) -> None:
        data["saved_answers"] += [
            saved("sa.tie_a", "Yes").model_dump(mode="json"),
            saved("sa.tie_b", "No").model_dump(mode="json"),
        ]

    stored = [saved("sa.stored_zero", 0, question="Years managing people?").model_dump(mode="json")]
    directory = write_candidate("default", edit=embed_conflict, answers=stored)
    raw_before = json.loads((directory / "profile.json").read_text())
    answers_before = (directory / "answers.json").read_bytes()
    report_before = candidate_store.load_report("default")

    upload = candidate_store.store_resume("default", filename="new.pdf", content=PDF)
    new_identity = identity(first_name="Avery", last_name="Example", phone=None)
    candidate_store.upsert_profile("default", identity=new_identity, resume_id=upload.id)

    raw_after = json.loads((directory / "profile.json").read_text())
    assert list(raw_after) == list(raw_before)
    for key in raw_before.keys() - {"identity", "resume"}:
        assert raw_after[key] == raw_before[key], key
    assert raw_after["identity"] == new_identity.model_dump(mode="json")
    assert (directory / "answers.json").read_bytes() == answers_before

    report_after = candidate_store.load_report("default")
    profile = report_after.profile
    assert profile.id == "default"
    assert profile.facts == report_before.profile.facts
    assert profile.experience == report_before.profile.experience
    assert profile.education == report_before.profile.education
    assert profile.saved_answers == report_before.profile.saved_answers
    assert [c.answer_ids for c in report_after.answer_conflicts] == [("sa.tie_a", "sa.tie_b")]
    stored_zero = profile.find_saved_answer("sa.stored_zero")
    assert stored_zero is not None and stored_zero.value == 0 and type(stored_zero.value) is int
    assert profile.fact("fact.team_size").is_verified is False
    assert profile.resume.id == upload.id


def test_imported_profile_resume_is_listed_and_kept(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    directory = write_candidate("default")
    original_resume = json.loads((directory / "profile.json").read_text())["resume"]

    setup = candidate_store.candidate_setup("default")
    assert setup.complete and setup.selected_resume_id == "resume_supplied"
    [imported] = setup.resumes
    assert imported.origin is ResumeOrigin.PROFILE and imported.uploaded_at is None
    assert imported.artifact.path == str((directory / FICTIONAL_RESUME.name).resolve())
    assert candidate_store.get_resume("default", "resume_supplied") == imported

    # Updating contact details while keeping the imported resume leaves its entry as is.
    candidate_store.upsert_profile(
        "default", identity=identity(email="avery.new@example.test"), resume_id="resume_supplied"
    )
    raw = json.loads((directory / "profile.json").read_text())
    assert raw["resume"] == original_resume
    assert not (directory / "resumes").exists()  # nothing copied

    # After switching to an upload, the imported file is no longer referenced or listed.
    upload = candidate_store.store_resume("default", filename="cv.pdf", content=PDF)
    candidate_store.upsert_profile("default", identity=identity(), resume_id=upload.id)
    assert [r.id for r in candidate_store.list_resumes("default")] == [upload.id]
    with pytest.raises(ResumeNotFound):
        candidate_store.get_resume("default", "resume_supplied")


def test_upsert_rejects_unknown_resume_and_invalid_input(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    directory = write_candidate("default")
    before = (directory / "profile.json").read_bytes()
    for resume_id in ["resume_" + "0" * 32, "../resume-avery-example.pdf", "", "resume_supplied2"]:
        with pytest.raises(ResumeNotFound):
            candidate_store.upsert_profile("default", identity=identity(), resume_id=resume_id)
    with pytest.raises(TypeError):
        candidate_store.upsert_profile(
            "default",
            identity={"first_name": "x"},  # type: ignore[arg-type]
            resume_id="resume_supplied",
        )
    assert (directory / "profile.json").read_bytes() == before


def test_upsert_never_overwrites_unreadable_or_invalid_profile(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    upload = candidate_store.store_resume("default", filename="cv.pdf", content=PDF)
    profile_file = candidate_store.profile_path("default")
    profile_file.write_text('{"id": "default", ')
    with pytest.raises(CandidateProfileInvalid, match="invalid JSON"):
        candidate_store.upsert_profile("default", identity=identity(), resume_id=upload.id)
    assert profile_file.read_text() == '{"id": "default", '

    directory = write_candidate("other", edit=lambda d: d["facts"][0].pop("verification"))
    other_upload = candidate_store.store_resume("other", filename="cv.pdf", content=PDF)
    before = (directory / "profile.json").read_bytes()
    with pytest.raises(CandidateProfileInvalid, match="verification"):
        candidate_store.upsert_profile("other", identity=identity(), resume_id=other_upload.id)
    assert (directory / "profile.json").read_bytes() == before

    write_candidate("third", edit=lambda d: d.update(id="someone-else"))
    third_upload = candidate_store.store_resume("third", filename="cv.pdf", content=PDF)
    with pytest.raises(CandidateProfileInvalid, match="stays fixed across edits"):
        candidate_store.upsert_profile("third", identity=identity(), resume_id=third_upload.id)


def test_setup_reports_profile_that_cannot_load(
    write_candidate: WriteCandidate, candidate_store: LocalCandidateStore
) -> None:
    directory = write_candidate("default")
    (directory / FICTIONAL_RESUME.name).unlink()
    setup = candidate_store.candidate_setup("default")
    assert not setup.complete
    assert setup.problem is not None and "resume file not found" in setup.problem
    assert setup.identity is not None and setup.identity.first_name == "Avery"
    assert setup.selected_resume_id == "resume_supplied"
    assert setup.resumes == ()  # a missing file is not offered for selection

    # Selecting a new upload repairs it without touching anything else.
    upload = candidate_store.store_resume("default", filename="cv.pdf", content=PDF)
    candidate_store.upsert_profile("default", identity=setup.identity, resume_id=upload.id)
    assert candidate_store.candidate_setup("default").complete


def test_upload_ids_are_per_candidate(candidate_store: LocalCandidateStore) -> None:
    upload = candidate_store.store_resume("alpha", filename="cv.pdf", content=PDF)
    with pytest.raises(ResumeNotFound):
        candidate_store.get_resume("beta", upload.id)
    with pytest.raises(ResumeNotFound):
        candidate_store.upsert_profile("beta", identity=identity(), resume_id=upload.id)
    assert not candidate_store.exists("beta")


# --- upload validation ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("..\\..\\Windows\\evil.PDF", "evil.pdf"),
        (".hidden.pdf", "hidden.pdf"),
        ("résumé final.pdf", "resume final.pdf"),
        ("a/b/.pdf", "resume.pdf"),
        ("name\x00\n.pdf", "name.pdf"),
        ("  report  2026 .PDF ", "report 2026.pdf"),
        ("cv; rm -rf ~.docx", "cv_ rm -rf.docx"),
        ("x" * 200 + ".md", "x" * 100 + ".md"),
    ],
)
def test_filenames_are_made_safe(given: str, stored: str) -> None:
    assert safe_resume_filename(given) == stored


@pytest.mark.parametrize(
    "filename", ["resume", "resume.exe", "resume.pdf.exe", "", "..", "/", "x" * 300 + ".pdf"]
)
def test_unusable_filenames_are_rejected(
    candidate_store: LocalCandidateStore, paths: LocalPaths, filename: str
) -> None:
    with pytest.raises(ResumeRejected):
        candidate_store.store_resume("default", filename=filename, content=PDF)
    assert not (paths.profile_dir / "default").exists()


@pytest.mark.parametrize(
    ("filename", "content", "media_type", "needle"),
    [
        ("cv.pdf", b"", None, "empty"),
        ("cv.pdf", b"%PDF-" + b"0" * 64, None, "larger than"),
        ("cv.pdf", b"not a pdf", None, "not a .pdf"),
        ("cv.pdf", b"%PDF-1.4 tiny", "text/html", "does not match"),
        ("cv.docx", b"%PDF-1.4", None, "not a .docx"),
        ("cv.txt", b"\xff\xfe\x00", None, "UTF-8"),
        ("cv.txt", b"a\x00b", None, "must be text"),
        ("cv.pdf", "text, not bytes", None, "must be bytes"),
    ],
)
def test_bad_uploads_are_rejected_and_nothing_is_stored(
    paths: LocalPaths, filename: str, content: Any, media_type: str | None, needle: str
) -> None:
    store = LocalCandidateStore.from_paths(paths, max_resume_bytes=32)
    small = b"%PDF-1.4 tiny"
    assert store.store_resume("default", filename="ok.pdf", content=small).artifact.size_bytes
    with pytest.raises(ResumeRejected, match=needle):
        store.store_resume("default", filename=filename, content=content, media_type=media_type)
    entries = sorted(p.name for p in store.resumes_dir("default").iterdir())
    assert len(entries) == 1 and entries[0].startswith("resume_")  # no staging leftovers


@pytest.mark.parametrize("media_type", [None, "", "application/octet-stream", "application/pdf"])
def test_generic_or_matching_media_types_are_accepted(
    candidate_store: LocalCandidateStore, media_type: str | None
) -> None:
    upload = candidate_store.store_resume(
        "default", filename="cv.pdf", content=PDF, media_type=media_type
    )
    assert upload.artifact.media_type == "application/pdf"


@pytest.mark.parametrize("candidate_id", ["", "..", "../default", "a/b", ".hidden"])
def test_invalid_candidate_ids(
    candidate_store: LocalCandidateStore, paths: LocalPaths, candidate_id: str
) -> None:
    with pytest.raises(CandidateNotFound):
        candidate_store.store_resume(candidate_id, filename="cv.pdf", content=PDF)
    with pytest.raises(CandidateNotFound):
        candidate_store.upsert_profile(candidate_id, identity=identity(), resume_id="resume_x")
    with pytest.raises(CandidateNotFound):
        candidate_store.list_resumes(candidate_id)
    assert not paths.profile_dir.exists() or list(paths.profile_dir.iterdir()) == []


@pytest.mark.parametrize(
    "resume_id", ["../default", "resume_" + "g" * 32, "resume_" + "0" * 32, "meta.json", ""]
)
def test_get_unknown_or_traversal_resume_id(
    candidate_store: LocalCandidateStore, resume_id: str
) -> None:
    candidate_store.store_resume("default", filename="cv.pdf", content=PDF)
    with pytest.raises(ResumeNotFound):
        candidate_store.get_resume("default", resume_id)


def test_tampered_upload_is_detected(candidate_store: LocalCandidateStore) -> None:
    upload = candidate_store.store_resume("default", filename="cv.pdf", content=PDF)
    candidate_store.upsert_profile("default", identity=identity(), resume_id=upload.id)
    stored = Path(upload.artifact.path)
    stored.chmod(0o600)
    stored.write_bytes(PDF + b"tampered")
    with pytest.raises(CandidateProfileInvalid, match="sha256"):
        candidate_store.get_resume("default", upload.id)
    with pytest.raises(CandidateProfileInvalid, match="sha256"):
        candidate_store.load("default")


# --- concurrency ------------------------------------------------------------------------


def _run_threads(targets: list[Callable[[], object]]) -> list[BaseException]:
    errors: list[BaseException] = []

    def wrap(target: Callable[[], object]) -> None:
        try:
            target()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=wrap, args=(t,)) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_concurrent_uploads_get_distinct_ids(paths: LocalPaths) -> None:
    def upload(i: int) -> Callable[[], object]:
        return lambda: LocalCandidateStore.from_paths(paths).store_resume(
            "default", filename="cv.pdf", content=PDF + str(i).encode()
        )

    assert _run_threads([upload(i) for i in range(12)]) == []
    resumes = LocalCandidateStore.from_paths(paths).list_resumes("default")
    assert len({r.id for r in resumes}) == 12
    assert {r.artifact.sha256 for r in resumes} == {
        hashlib.sha256(PDF + str(i).encode()).hexdigest() for i in range(12)
    }


def test_concurrent_profile_updates_and_answer_saves(
    write_candidate: WriteCandidate, paths: LocalPaths
) -> None:
    write_candidate("default")
    store = LocalCandidateStore.from_paths(paths)
    uploads = [store.store_resume("default", filename=f"cv{i}.pdf", content=PDF) for i in range(3)]
    emails = [f"riley{i}@example.test" for i in range(6)]

    def upsert(i: int) -> Callable[[], object]:
        return lambda: LocalCandidateStore.from_paths(paths).upsert_profile(
            "default", identity=identity(email=emails[i]), resume_id=uploads[i % 3].id
        )

    def save(i: int) -> Callable[[], object]:
        return lambda: LocalCandidateStore.from_paths(paths).save_answer(
            "default", saved(f"sa.q{i}", i, question=f"Question {i}?")
        )

    targets = [f(i) for i in range(6) for f in (upsert, save)]
    assert _run_threads(targets) == []

    profile = store.load("default")
    assert profile.identity.email in emails
    assert profile.resume.id in {u.id for u in uploads}
    assert {f"sa.q{i}" for i in range(6)} <= {a.id for a in profile.saved_answers}
    assert profile.fact("fact.current_title").value == "Paid Media Lead"


# --- damaged uploads (C2P1) -------------------------------------------------------------


def _break_missing_data(upload: StoredResume) -> None:
    Path(upload.artifact.path).unlink()


def _break_digest(upload: StoredResume) -> None:
    stored = Path(upload.artifact.path)
    stored.chmod(0o600)
    stored.write_text("Changed after upload\n")


def _break_missing_meta(upload: StoredResume) -> None:
    (Path(upload.artifact.path).parent / "meta.json").unlink()


def _break_malformed_meta(upload: StoredResume) -> None:
    meta = Path(upload.artifact.path).parent / "meta.json"
    meta.chmod(0o600)
    meta.write_text('{"id": ')


@pytest.mark.parametrize(
    ("damage", "strict_error"),
    [
        (_break_missing_data, CandidateProfileInvalid),
        (_break_digest, CandidateProfileInvalid),
        (_break_missing_meta, ResumeNotFound),
        (_break_malformed_meta, CandidateProfileInvalid),
    ],
)
def test_damaged_unselected_upload_does_not_break_setup(
    candidate_store: LocalCandidateStore,
    damage: Callable[[StoredResume], None],
    strict_error: type[Exception],
) -> None:
    old = candidate_store.store_resume("default", filename="old.txt", content=b"Old CV\n")
    current = candidate_store.store_resume("default", filename="current.txt", content=b"CV\n")
    candidate_store.upsert_profile("default", identity=identity(), resume_id=current.id)
    damage(old)

    assert candidate_store.load("default").resume.id == current.id
    setup = candidate_store.candidate_setup("default")
    assert setup.complete and setup.problem is None
    assert [r.id for r in setup.resumes] == [current.id]  # valid choices retained
    assert setup.selected_resume_id == current.id
    assert [d.resume_id for d in setup.damaged_resumes] == [old.id]
    assert setup.damaged_resumes[0].problem
    assert [r.id for r in candidate_store.list_resumes("default")] == [current.id]
    with pytest.raises(strict_error):
        candidate_store.get_resume("default", old.id)  # direct access stays strict

    # Uploading and selecting a replacement still works.
    replacement = candidate_store.store_resume("default", filename="new.pdf", content=PDF)
    candidate_store.upsert_profile("default", identity=identity(), resume_id=replacement.id)
    after = candidate_store.candidate_setup("default")
    assert after.complete and after.selected_resume_id == replacement.id
    assert [r.id for r in after.resumes] == [current.id, replacement.id]
    assert [d.resume_id for d in after.damaged_resumes] == [old.id]


def test_damaged_selected_upload_is_reported_and_recoverable(
    candidate_store: LocalCandidateStore,
) -> None:
    other = candidate_store.store_resume("default", filename="other.txt", content=b"Other\n")
    current = candidate_store.store_resume("default", filename="current.txt", content=b"CV\n")
    candidate_store.upsert_profile("default", identity=identity(), resume_id=current.id)
    _break_digest(current)

    setup = candidate_store.candidate_setup("default")
    assert not setup.complete
    assert setup.problem is not None and "sha256" in setup.problem
    assert setup.identity == identity() and setup.selected_resume_id == current.id
    assert [r.id for r in setup.resumes] == [other.id]
    assert [d.resume_id for d in setup.damaged_resumes] == [current.id]

    candidate_store.upsert_profile("default", identity=identity(), resume_id=other.id)
    recovered = candidate_store.candidate_setup("default")
    assert recovered.complete and recovered.selected_resume_id == other.id


# --- consistent setup snapshot (C2P1) ---------------------------------------------------


def test_setup_snapshot_is_consistent_with_concurrent_selection(
    candidate_store: LocalCandidateStore, paths: LocalPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = candidate_store.store_resume("default", filename="first.pdf", content=PDF)
    candidate_store.upsert_profile("default", identity=identity(), resume_id=first.id)

    reader = LocalCandidateStore.from_paths(paths)
    writer = LocalCandidateStore.from_paths(paths)
    enumerated = threading.Event()
    proceed = threading.Event()
    scan = reader._scan_resumes

    def paused_scan(candidate_id: str) -> tuple[list[StoredResume], list[DamagedResume]]:
        result = scan(candidate_id)
        enumerated.set()
        assert proceed.wait(timeout=10)
        return result

    monkeypatch.setattr(reader, "_scan_resumes", paused_scan)
    results: list[CandidateSetup] = []
    reader_thread = threading.Thread(
        target=lambda: results.append(reader.candidate_setup("default"))
    )
    reader_thread.start()
    assert enumerated.wait(timeout=10)

    # While the reader is paused between enumeration and profile read, another
    # store uploads a new resume and tries to select it.
    second = writer.store_resume("default", filename="second.pdf", content=PDF)
    selected = threading.Event()

    def select_second() -> None:
        writer.upsert_profile("default", identity=identity(), resume_id=second.id)
        selected.set()

    writer_thread = threading.Thread(target=select_second)
    writer_thread.start()
    assert not selected.wait(timeout=0.3)  # the selection waits for the snapshot

    proceed.set()
    reader_thread.join(timeout=10)
    writer_thread.join(timeout=10)
    assert not reader_thread.is_alive() and not writer_thread.is_alive()
    assert selected.is_set()

    [snapshot] = results
    assert snapshot.complete and snapshot.selected_resume_id == first.id
    assert snapshot.selected_resume_id in {r.id for r in snapshot.resumes}

    later = writer.candidate_setup("default")
    assert later.selected_resume_id == second.id
    assert [r.id for r in later.resumes] == [first.id, second.id]
