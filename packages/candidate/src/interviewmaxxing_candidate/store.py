"""Local candidate storage: ``CandidateLoader`` and ``SavedAnswerWriter``.

Layout, under ``LocalPaths.profile_dir`` (``$IMX_HOME/profile``)::

    <candidate_id>/
        profile.json    CandidateProfile JSON, written by the user or upsert_profile
        answers.json    JSON array of SavedAnswer, written by save_answer
        resumes/        uploads from store_resume (see ``resumes``)
        <resume file>   optional; resume.path may point anywhere

The candidate id is the directory name. It is never derived from profile content,
so editing the profile never changes it, and ``profile.json``'s ``id`` must equal it.
"""

from __future__ import annotations

import fcntl
import os
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Self

from pydantic import ValidationError

from interviewmaxxing_core import (
    CandidateFact,
    CandidateIdentity,
    CandidateNotFound,
    CandidateProfile,
    CandidateProfileInvalid,
    LocalPaths,
    ResumeArtifact,
    SavedAnswer,
    sha256_file,
    utc_now,
)

from .answers import (
    AnswerConflict,
    SupersededAnswer,
    question_key,
    reconcile_saved_answers,
    value_key,
)
from .files import describe_validation_error, read_json, write_json_private
from .resumes import (
    DEFAULT_MAX_RESUME_BYTES,
    RESUMES_DIRNAME,
    UPLOAD_ID,
    DamagedResume,
    ResumeNotFound,
    ResumeOrigin,
    StoredResume,
    check_resume_content,
    read_upload,
    safe_resume_filename,
    scan_uploads,
    write_upload,
)

PROFILE_FILENAME = "profile.json"
ANSWERS_FILENAME = "answers.json"
_LOCK_FILENAME = ".answers.lock"
_CANDIDATE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

RESUME_MEDIA_TYPES: Mapping[str, str] = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".rtf": "application/rtf",
    ".txt": "text/plain",
    ".md": "text/markdown",
}
"""Media types assumed from the resume extension when ``resume.media_type`` is omitted."""


class SavedAnswerRejected(ValueError):
    """``save_answer`` refused an answer; nothing was written."""


@dataclass(frozen=True)
class CandidateLoadReport:
    """A loaded profile plus where each part came from and what was left out."""

    profile: CandidateProfile
    profile_path: Path
    resume_path: Path
    resume_digest_computed: bool
    """True when ``profile.json`` omitted ``resume.sha256`` and it was computed."""
    answers_path: Path | None
    """``answers.json`` if it exists."""
    answer_sources: Mapping[str, Path] = field(default_factory=dict)
    """File each saved answer (including excluded ones) was read from, by answer id."""
    superseded_answers: tuple[SupersededAnswer, ...] = ()
    answer_conflicts: tuple[AnswerConflict, ...] = ()

    @property
    def unverified_fact_ids(self) -> tuple[str, ...]:
        return tuple(f.id for f in self.profile.facts if not f.is_verified)

    def warnings(self) -> list[str]:
        """Human-readable notes about unverified facts and replaced or conflicting answers."""
        notes = [
            f"fact {f.id} ({f.key}) is UNVERIFIED and will not be used until you confirm it"
            for f in self.profile.facts
            if not f.is_verified
        ]
        notes += [
            f"saved answer {s.answer.id} was replaced by {', '.join(s.superseded_by)} "
            "(same question, confirmed later)"
            for s in self.superseded_answers
        ]
        notes += [
            f"saved answers {', '.join(c.answer_ids)} disagree on {c.question!r} with no later "
            "confirmation; the question stays ambiguous until you remove the wrong one"
            for c in self.answer_conflicts
        ]
        return notes


@dataclass(frozen=True)
class CandidateSetup:
    """What the setup UI shows. It is not a ``CandidateProfile``: before the user has
    supplied contact details and selected a resume, ``identity`` and
    ``selected_resume_id`` are ``None`` and nothing is invented to fill them."""

    candidate_id: str
    identity: CandidateIdentity | None
    """Contact details from ``profile.json``, if present and valid."""
    resumes: tuple[StoredResume, ...]
    """``list_resumes``: uploads oldest first, then an imported profile's own resume."""
    selected_resume_id: str | None
    """The resume ``profile.json`` references, if any."""
    complete: bool
    """True when ``load`` succeeds, i.e. the profile can be used to apply."""
    problem: str | None
    """The ``CandidateProfileInvalid`` message when the profile exists but cannot load."""
    damaged_resumes: tuple[DamagedResume, ...] = ()
    """Uploads left out of ``resumes`` because their file or metadata is damaged."""


class LocalCandidateStore:
    """Reads candidate profiles and persists saved answers under ``profile_dir``.

    Implements ``CandidateLoader`` and ``SavedAnswerWriter``, and the setup
    operations ``store_resume``/``list_resumes``/``get_resume``/``upsert_profile``/
    ``candidate_setup``. A relative ``profile_dir`` is resolved against the working
    directory at construction."""

    def __init__(
        self,
        profile_dir: Path | str,
        *,
        max_resume_bytes: int = DEFAULT_MAX_RESUME_BYTES,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.profile_dir = Path(profile_dir).expanduser().resolve()
        self.max_resume_bytes = max_resume_bytes
        self._clock = clock

    @classmethod
    def from_paths(
        cls,
        paths: LocalPaths,
        *,
        max_resume_bytes: int = DEFAULT_MAX_RESUME_BYTES,
        clock: Callable[[], datetime] = utc_now,
    ) -> Self:
        return cls(paths.profile_dir, max_resume_bytes=max_resume_bytes, clock=clock)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        """Use ``IMX_PROFILE_DIR`` (default ``$IMX_HOME/profile``)."""
        return cls.from_paths(LocalPaths.from_env(env))

    # --- paths ---------------------------------------------------------------------

    def candidate_dir(self, candidate_id: str) -> Path:
        if not _CANDIDATE_ID.fullmatch(candidate_id):
            raise CandidateNotFound(
                f"invalid candidate id {candidate_id!r}: use 1-128 letters, digits, '.', '_' "
                "or '-', starting with a letter or digit"
            )
        return self.profile_dir / candidate_id

    def profile_path(self, candidate_id: str) -> Path:
        return self.candidate_dir(candidate_id) / PROFILE_FILENAME

    def answers_path(self, candidate_id: str) -> Path:
        return self.candidate_dir(candidate_id) / ANSWERS_FILENAME

    def resumes_dir(self, candidate_id: str) -> Path:
        return self.candidate_dir(candidate_id) / RESUMES_DIRNAME

    def exists(self, candidate_id: str) -> bool:
        try:
            return self.profile_path(candidate_id).is_file()
        except CandidateNotFound:
            return False

    # --- CandidateLoader -----------------------------------------------------------

    def load(self, candidate_id: str) -> CandidateProfile:
        """The profile with its resume checked and saved answers merged.

        Unverified facts are returned unchanged (flagged by their verification).
        Superseded saved answers are left out; conflicting ones are kept and listed in
        ``load_report().answer_conflicts``. Raises
        ``CandidateNotFound`` or ``CandidateProfileInvalid``."""
        return self.load_report(candidate_id).profile

    def load_report(self, candidate_id: str) -> CandidateLoadReport:
        """Like ``load``, with sources, superseded answers and conflicts."""
        profile_path = self._existing_profile_path(candidate_id)
        raw = self._read_profile_object(profile_path, candidate_id)
        return self._report_from_raw(candidate_id, profile_path, raw)

    def _report_from_raw(
        self, candidate_id: str, profile_path: Path, raw: dict[str, Any]
    ) -> CandidateLoadReport:
        resume_raw = raw.get("resume")
        if not isinstance(resume_raw, dict):
            raise CandidateProfileInvalid(
                f'{profile_path}: "resume" is required: an object with at least "id" and '
                f'"path" (the resume file, absolute or relative to {profile_path.parent})'
            )
        self._private_upload_for(candidate_id, profile_path, resume_raw)
        resume, resume_path, computed = _resolve_resume(profile_path, resume_raw)

        try:
            profile = CandidateProfile.model_validate({**raw, "resume": resume})
        except ValidationError as exc:
            raise CandidateProfileInvalid(describe_validation_error(profile_path, exc)) from None

        sources: dict[str, Path] = {a.id: profile_path for a in profile.saved_answers}
        answers_path = self.answers_path(candidate_id)
        stored = _read_answers(answers_path)
        for answer in stored:
            if answer.id in sources:
                raise CandidateProfileInvalid(
                    f"saved answer id {answer.id!r} appears in both {profile_path} and "
                    f"{answers_path}; remove or rename one of them"
                )
            sources[answer.id] = answers_path

        result = reconcile_saved_answers([*profile.saved_answers, *stored])
        return CandidateLoadReport(
            profile=profile.model_copy(update={"saved_answers": list(result.kept)}),
            profile_path=profile_path,
            resume_path=resume_path,
            resume_digest_computed=computed,
            answers_path=answers_path if answers_path.exists() else None,
            answer_sources=sources,
            superseded_answers=result.superseded,
            answer_conflicts=result.conflicts,
        )

    # --- SavedAnswerWriter ---------------------------------------------------------

    def save_answer(self, candidate_id: str, answer: SavedAnswer) -> None:
        """Persist ``answer`` to ``answers.json`` exactly as given (scope included).

        Stored answers to the same question (same scope, job target, semantic type
        and question text) are compared by ``confirmed_at`` under the file lock:

        * older ones are replaced;
        * if one is newer, ``answer`` is stale and nothing is written;
        * one confirmed at the same time with the same value makes this a retry
          (no-op); one with a different value is kept alongside ``answer``, and
          loading reports the pair as a conflict.

        Answers with another scope or target are untouched. Saving an identical
        answer again is a no-op. Raises ``SavedAnswerRejected`` if the id is already
        used by a different answer."""
        if not isinstance(answer, SavedAnswer):
            raise TypeError(f"expected SavedAnswer, got {type(answer).__name__}")
        profile_path = self._existing_profile_path(candidate_id)
        raw = self._read_profile_object(profile_path, candidate_id)
        profile_answer_ids = {
            item.get("id") for item in raw.get("saved_answers") or [] if isinstance(item, dict)
        }
        if answer.id in profile_answer_ids:
            raise SavedAnswerRejected(
                f"saved answer id {answer.id!r} is already used in {profile_path}; "
                "save the new answer under a new id"
            )

        answers_path = self.answers_path(candidate_id)
        with _exclusive_lock(profile_path.parent / _LOCK_FILENAME):
            stored = _read_answers(answers_path)
            same_id = next((a for a in stored if a.id == answer.id), None)
            if same_id is not None:
                if same_id == answer:
                    return
                raise SavedAnswerRejected(
                    f"saved answer id {answer.id!r} already holds a different answer in "
                    f"{answers_path}; save the new answer under a new id"
                )
            key = question_key(answer)
            same_question = [a for a in stored if question_key(a) == key]
            if any(a.confirmed_at > answer.confirmed_at for a in same_question):
                return
            if any(
                a.confirmed_at == answer.confirmed_at
                and value_key(a.value) == value_key(answer.value)
                for a in same_question
            ):
                return
            kept = [
                a for a in stored if question_key(a) != key or a.confirmed_at == answer.confirmed_at
            ]
            write_json_private(answers_path, [a.model_dump(mode="json") for a in [*kept, answer]])

    # --- setup: resumes and contact profile ------------------------------------------

    def store_resume(
        self,
        candidate_id: str,
        *,
        filename: str,
        content: bytes,
        media_type: str | None = None,
    ) -> StoredResume:
        """Store an uploaded resume under a new generated id; no profile is needed.

        ``filename`` is reduced to a safe name with an accepted extension
        (``RESUME_UPLOAD_TYPES``). ``media_type`` may be omitted or
        ``application/octet-stream``; otherwise it must match the extension. The
        content must be non-empty, at most ``max_resume_bytes``, and look like its
        type. Raises ``ResumeRejected`` (nothing stored) or ``CandidateNotFound``
        for an invalid candidate id. The bytes are stored unchanged and never read
        for facts."""
        directory = self.candidate_dir(candidate_id)
        safe_name = safe_resume_filename(filename)
        stored_type = check_resume_content(
            safe_name, content, media_type, max_bytes=self.max_resume_bytes
        )
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.mkdir(exist_ok=True, mode=0o700)
        return write_upload(
            directory / RESUMES_DIRNAME,
            filename=safe_name,
            content=content,
            media_type=stored_type,
            uploaded_at=self._clock(),
        )

    def list_resumes(self, candidate_id: str) -> list[StoredResume]:
        """Usable uploaded resumes (oldest first), then the resume an existing
        ``profile.json`` references if it is not an upload and its file checks out
        (origin PROFILE). Damaged uploads are skipped individually (see
        ``candidate_setup().damaged_resumes``). Arbitrary files are never copied or
        listed."""
        return self._scan_resumes(candidate_id)[0]

    def _scan_resumes(self, candidate_id: str) -> tuple[list[StoredResume], list[DamagedResume]]:
        resumes, damaged = scan_uploads(self.resumes_dir(candidate_id))
        profile_resume = self._profile_resume(candidate_id)
        if profile_resume is not None and profile_resume.id not in {r.id for r in resumes}:
            resumes.append(profile_resume)
        return resumes, damaged

    def get_resume(self, candidate_id: str, resume_id: str) -> StoredResume:
        """One resume from ``list_resumes``. Strict: raises ``ResumeNotFound`` for an
        unknown id (or an upload without metadata) and ``CandidateProfileInvalid`` for
        a damaged upload."""
        uploads = self.resumes_dir(candidate_id)
        if UPLOAD_ID.fullmatch(resume_id) and os.path.lexists(uploads / resume_id):
            # A private upload is only ever served with its metadata and digest intact;
            # it never falls back to the profile's copy of the reference.
            return read_upload(uploads, resume_id)
        profile_resume = self._profile_resume(candidate_id)
        if profile_resume is not None and profile_resume.id == resume_id:
            return profile_resume
        raise ResumeNotFound(f"candidate {candidate_id!r} has no resume {resume_id!r}")

    def upsert_profile(
        self, candidate_id: str, *, identity: CandidateIdentity, resume_id: str
    ) -> CandidateProfile:
        """Create or update ``profile.json`` with contact details and a selected resume.

        ``identity`` is stored as given, including its ``verified_at`` (the time the
        user confirmed these details). ``resume_id`` must be one of ``list_resumes``:
        an upload (referenced as ``resumes/<id>/<file>``), or the profile's current
        resume, which is then left exactly as it is. Every other key of an existing
        ``profile.json`` (facts, experience, education, embedded saved answers) is
        kept verbatim; ``answers.json`` is not touched. A new profile starts with no
        facts, answers, experience or education. The result is validated like
        ``load`` before anything is written, under the candidate lock. Returns the
        loaded profile. Raises ``ResumeNotFound``, ``CandidateProfileInvalid`` (e.g.
        an existing profile that cannot be parsed or validated; nothing is written)
        or ``CandidateNotFound`` for an invalid id."""
        if not isinstance(identity, CandidateIdentity):
            raise TypeError(f"expected CandidateIdentity, got {type(identity).__name__}")
        directory = self.candidate_dir(candidate_id)
        profile_path = directory / PROFILE_FILENAME
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.mkdir(exist_ok=True, mode=0o700)
        with _exclusive_lock(directory / _LOCK_FILENAME):
            if profile_path.exists():
                raw = self._read_profile_object(
                    self._existing_profile_path(candidate_id), candidate_id
                )
            else:
                raw = {
                    "id": candidate_id,
                    "identity": None,
                    "resume": None,
                    "facts": [],
                    "saved_answers": [],
                    "experience": [],
                    "education": [],
                }
            current = raw.get("resume")
            keep_current = isinstance(current, dict) and current.get("id") == resume_id
            updated = {**raw, "identity": identity.model_dump(mode="json")}
            if not keep_current:
                updated["resume"] = _upload_reference(
                    self.get_resume(candidate_id, resume_id), directory
                )
            report = self._report_from_raw(candidate_id, profile_path, updated)
            write_json_private(profile_path, updated)
        return report.profile

    def upsert_facts(
        self, candidate_id: str, facts: list[CandidateFact]
    ) -> CandidateProfile:
        """Merge explicitly supplied facts by id under the existing profile lock.

        Verification is accepted as supplied, never inferred by this store. This
        also allows a caller to revoke an obsolete fact by supplying UNVERIFIED.
        Existing identity, resume, experience and saved answers remain intact.
        """
        if any(not isinstance(fact, CandidateFact) for fact in facts):
            raise TypeError("facts must be CandidateFact instances")
        if len({fact.id for fact in facts}) != len(facts):
            raise ValueError("duplicate incoming fact ids")
        directory = self.candidate_dir(candidate_id)
        with _exclusive_lock(directory / _LOCK_FILENAME):
            path = self._existing_profile_path(candidate_id)
            raw = self._read_profile_object(path, candidate_id)
            self._report_from_raw(candidate_id, path, raw)
            merged = {fact["id"]: fact for fact in raw.get("facts", [])}
            merged.update({fact.id: fact.model_dump(mode="json") for fact in facts})
            updated = {**raw, "facts": list(merged.values())}
            report = self._report_from_raw(candidate_id, path, updated)
            write_json_private(path, updated)
        return report.profile

    def remove_facts(self, candidate_id: str, fact_ids: list[str]) -> CandidateProfile:
        """Delete the facts with these ids under the profile lock (unknown ids are
        ignored), dropping them from any experience or education group. For a fact that
        an import replaced: a revoked fact stays as UNVERIFIED with ``upsert_facts``, a
        removed one is gone. Identity, resume and saved answers remain intact."""
        if any(not isinstance(fact_id, str) or not fact_id for fact_id in fact_ids):
            raise TypeError("fact_ids must be nonempty strings")
        doomed = set(fact_ids)
        directory = self.candidate_dir(candidate_id)
        with _exclusive_lock(directory / _LOCK_FILENAME):
            path = self._existing_profile_path(candidate_id)
            raw = self._read_profile_object(path, candidate_id)
            self._report_from_raw(candidate_id, path, raw)
            updated = {
                **raw,
                "facts": [fact for fact in raw.get("facts", []) if fact.get("id") not in doomed],
                "experience": [{**group, "fact_ids": [i for i in group.get("fact_ids", []) if i not in doomed]}
                               for group in raw.get("experience", [])],
                "education": [{**group, "fact_ids": [i for i in group.get("fact_ids", []) if i not in doomed]}
                              for group in raw.get("education", [])],
            }
            report = self._report_from_raw(candidate_id, path, updated)
            write_json_private(path, updated)
        return report.profile

    def candidate_setup(self, candidate_id: str) -> CandidateSetup:
        """Contact details, resumes and completeness for the setup UI. Works before
        any profile exists (nothing is invented) and for imported profiles.

        The resumes and the profile are read as one snapshot under a shared candidate
        lock, so a concurrent ``upsert_profile`` (exclusive lock) happens entirely
        before or after it: a selected upload is always among ``resumes`` unless it
        is damaged. Damaged uploads are reported in ``damaged_resumes`` without
        hiding the usable ones."""
        directory = self.candidate_dir(candidate_id)
        if not directory.is_dir():
            return CandidateSetup(candidate_id, None, (), None, False, None)
        with _candidate_lock(directory / _LOCK_FILENAME, shared=True):
            listed, damaged_list = self._scan_resumes(candidate_id)
            resumes, damaged = tuple(listed), tuple(damaged_list)
            if not self.profile_path(candidate_id).exists():
                return CandidateSetup(candidate_id, None, resumes, None, False, None, damaged)
            try:
                profile = self.load(candidate_id)
            except CandidateProfileInvalid as exc:
                identity, selected = self._partial_profile(candidate_id)
                return CandidateSetup(
                    candidate_id, identity, resumes, selected, False, str(exc), damaged
                )
        return CandidateSetup(
            candidate_id, profile.identity, resumes, profile.resume.id, True, None, damaged
        )

    # --- internals -----------------------------------------------------------------

    def _profile_resume(self, candidate_id: str) -> StoredResume | None:
        """The existing profile's resume if it resolves and checks out."""
        try:
            profile_path = self._existing_profile_path(candidate_id)
            raw = self._read_profile_object(profile_path, candidate_id)
            resume_raw = raw.get("resume")
            if not isinstance(resume_raw, dict):
                return None
            upload = self._private_upload_for(candidate_id, profile_path, resume_raw)
            resume, _, _ = _resolve_resume(profile_path, resume_raw)
            if upload is not None:
                return upload
            artifact = ResumeArtifact.model_validate(resume)
        except (CandidateNotFound, CandidateProfileInvalid, ValidationError):
            return None
        return StoredResume(artifact=artifact, origin=ResumeOrigin.PROFILE, uploaded_at=None)

    def _private_upload_for(
        self, candidate_id: str, profile_path: Path, resume_raw: dict[str, Any]
    ) -> StoredResume | None:
        """The intact upload a profile resume entry refers to, or ``None`` for an
        external (imported or hand-placed) resume.

        An entry is a private upload when its id has the generated upload form and
        its path lies in this candidate's ``resumes/`` directory or an upload
        directory with that id exists. Such an entry must pass the full upload
        check (metadata, digest, readable file) and point at the upload's own file;
        otherwise ``CandidateProfileInvalid`` is raised. It is never accepted on
        the strength of the profile's copy of the reference alone."""
        resume_id = resume_raw.get("id")
        if not isinstance(resume_id, str) or not UPLOAD_ID.fullmatch(resume_id):
            return None
        uploads = self.resumes_dir(candidate_id)
        path_value = resume_raw.get("path")
        target: Path | None = None
        if isinstance(path_value, str) and path_value.strip():
            given = Path(path_value).expanduser()
            target = (given if given.is_absolute() else profile_path.parent / given).resolve()
        if not (
            (target is not None and target.is_relative_to(uploads))
            or os.path.lexists(uploads / resume_id)
        ):
            return None
        try:
            upload = read_upload(uploads, resume_id)
        except ResumeNotFound:
            raise CandidateProfileInvalid(
                f"{profile_path}: the selected uploaded resume {resume_id} has no metadata "
                f"in {uploads / resume_id}; upload the resume again and select it"
            ) from None
        if target is not None and Path(upload.artifact.path) != target:
            raise CandidateProfileInvalid(
                f"{profile_path}: resume {resume_id} does not point at its uploaded file "
                f"{upload.artifact.path}"
            )
        return upload

    def _partial_profile(self, candidate_id: str) -> tuple[CandidateIdentity | None, str | None]:
        """Identity and resume id of a profile that does not fully load."""
        try:
            raw = self._read_profile_object(self.profile_path(candidate_id), candidate_id)
        except CandidateProfileInvalid:
            return None, None
        try:
            identity: CandidateIdentity | None = CandidateIdentity.model_validate(
                raw.get("identity")
            )
        except ValidationError:
            identity = None
        resume = raw.get("resume")
        selected = resume.get("id") if isinstance(resume, dict) else None
        return identity, selected if isinstance(selected, str) else None

    def _existing_profile_path(self, candidate_id: str) -> Path:
        path = self.profile_path(candidate_id)
        if path.is_file():
            return path
        if path.exists():
            raise CandidateProfileInvalid(f"{path} exists but is not a regular file")
        raise CandidateNotFound(
            f"no profile for candidate {candidate_id!r}: expected {path} "
            f'(a CandidateProfile JSON with "id": "{candidate_id}"; see '
            "examples/candidate.example.json)"
        )

    @staticmethod
    def _read_profile_object(path: Path, candidate_id: str) -> dict[str, Any]:
        raw = read_json(path)
        if not isinstance(raw, dict):
            raise CandidateProfileInvalid(f"{path}: expected a JSON object (a CandidateProfile)")
        if raw.get("id") != candidate_id:
            raise CandidateProfileInvalid(
                f'{path}: "id" is {raw.get("id")!r} but this is candidate {candidate_id!r}. '
                "The candidate id is the directory name and stays fixed across edits; "
                f'set "id": "{candidate_id}"'
            )
        return raw


def _resolve_resume(
    profile_path: Path, resume_raw: dict[str, Any]
) -> tuple[dict[str, Any], Path, bool]:
    """Resolve ``resume.path`` against the profile's directory and check the file.

    ``sha256``, ``size_bytes``, ``filename`` and ``media_type`` are taken from the
    file when omitted and must match it when given."""
    base = profile_path.parent
    path_value = resume_raw.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        raise CandidateProfileInvalid(
            f"{profile_path}: resume.path is required: the resume file, absolute or "
            f"relative to {base}"
        )
    given = Path(path_value).expanduser()
    resolved = (given if given.is_absolute() else base / given).resolve()
    where = f"resume.path {path_value!r}" + (
        "" if given.is_absolute() else f" (relative to {base})"
    )
    try:
        if not resolved.exists():
            raise CandidateProfileInvalid(
                f"{profile_path}: resume file not found: {resolved} from {where}"
            )
        if not resolved.is_file():
            raise CandidateProfileInvalid(
                f"{profile_path}: resume is not a regular file: {resolved}"
            )
        size = resolved.stat().st_size
        if size == 0:
            raise CandidateProfileInvalid(f"{profile_path}: resume file is empty: {resolved}")
        digest = sha256_file(resolved)
    except OSError as exc:
        raise CandidateProfileInvalid(
            f"{profile_path}: cannot read resume file {resolved} ({exc.strerror or exc})"
        ) from None
    declared = resume_raw.get("sha256")
    if isinstance(declared, str) and declared != digest:
        raise CandidateProfileInvalid(
            f"{profile_path}: resume {resolved} does not match resume.sha256 "
            f"(profile {declared}, file {digest}). The file changed after the profile was "
            "written; if the current file is the right resume, update or remove resume.sha256"
        )
    declared_size = resume_raw.get("size_bytes")
    if isinstance(declared_size, int) and declared_size != size:
        raise CandidateProfileInvalid(
            f"{profile_path}: resume {resolved} is {size} bytes but resume.size_bytes is "
            f"{declared_size}; update or remove resume.size_bytes"
        )

    resume = {**resume_raw, "path": str(resolved)}
    resume.setdefault("sha256", digest)
    resume.setdefault("size_bytes", size)
    resume.setdefault("filename", resolved.name)
    if "media_type" not in resume:
        media_type = RESUME_MEDIA_TYPES.get(resolved.suffix.lower())
        if media_type is None:
            raise CandidateProfileInvalid(
                f"{profile_path}: cannot tell the media type of {resolved.name}; set "
                'resume.media_type (e.g. "application/pdf")'
            )
        resume["media_type"] = media_type
    return resume, resolved, "sha256" not in resume_raw


def _upload_reference(resume: StoredResume, candidate_dir: Path) -> dict[str, Any]:
    """``profile.json`` resume entry for an upload, relative to the candidate dir."""
    artifact = resume.artifact
    relative = Path(artifact.path).relative_to(candidate_dir)
    return {
        "id": artifact.id,
        "path": relative.as_posix(),
        "filename": artifact.filename,
        "media_type": artifact.media_type,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "variant": "supplied",
        "extracted_text": None,
    }


def _read_answers(path: Path) -> list[SavedAnswer]:
    if not path.exists():
        return []
    if not path.is_file():
        raise CandidateProfileInvalid(f"{path} exists but is not a regular file")
    raw = read_json(path)
    if not isinstance(raw, list):
        raise CandidateProfileInvalid(f"{path}: expected a JSON array of saved answers")
    answers: list[SavedAnswer] = []
    for index, item in enumerate(raw):
        try:
            answers.append(SavedAnswer.model_validate(item))
        except ValidationError as exc:
            raise CandidateProfileInvalid(
                describe_validation_error(path, exc, prefix=f"[{index}]")
            ) from None
    ids = [a.id for a in answers]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise CandidateProfileInvalid(f"{path}: duplicate saved answer ids {duplicates}")
    return answers


def _exclusive_lock(path: Path) -> AbstractContextManager[None]:
    return _candidate_lock(path, shared=False)


@contextmanager
def _candidate_lock(path: Path, *, shared: bool) -> Iterator[None]:
    """Per-candidate ``flock``: writers exclusive, snapshot readers shared."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
