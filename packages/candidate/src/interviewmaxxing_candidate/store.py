"""Local candidate storage: ``CandidateLoader`` and ``SavedAnswerWriter``.

Layout, under ``LocalPaths.profile_dir`` (``$IMX_HOME/profile``)::

    <candidate_id>/
        profile.json    CandidateProfile JSON, written by the user
        answers.json    JSON array of SavedAnswer, written by save_answer
        <resume file>   optional; resume.path may point anywhere

The candidate id is the directory name. It is never derived from profile content,
so editing the profile never changes it, and ``profile.json``'s ``id`` must equal it.
"""

from __future__ import annotations

import fcntl
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from pydantic import ValidationError

from interviewmaxxing_core import (
    CandidateNotFound,
    CandidateProfile,
    CandidateProfileInvalid,
    LocalPaths,
    SavedAnswer,
    sha256_file,
)

from .answers import (
    AnswerConflict,
    SupersededAnswer,
    question_key,
    reconcile_saved_answers,
    value_key,
)
from .files import describe_validation_error, read_json, write_json_private

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


class LocalCandidateStore:
    """Reads candidate profiles and persists saved answers under ``profile_dir``.

    Implements ``CandidateLoader`` and ``SavedAnswerWriter``. A relative
    ``profile_dir`` is resolved against the working directory at construction."""

    def __init__(self, profile_dir: Path | str) -> None:
        self.profile_dir = Path(profile_dir).expanduser().resolve()

    @classmethod
    def from_paths(cls, paths: LocalPaths) -> Self:
        return cls(paths.profile_dir)

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

        resume_raw = raw.get("resume")
        if not isinstance(resume_raw, dict):
            raise CandidateProfileInvalid(
                f'{profile_path}: "resume" is required: an object with at least "id" and '
                f'"path" (the resume file, absolute or relative to {profile_path.parent})'
            )
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

    # --- internals -----------------------------------------------------------------

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
    if not resolved.exists():
        raise CandidateProfileInvalid(
            f"{profile_path}: resume file not found: {resolved} from {where}"
        )
    if not resolved.is_file():
        raise CandidateProfileInvalid(f"{profile_path}: resume is not a regular file: {resolved}")
    size = resolved.stat().st_size
    if size == 0:
        raise CandidateProfileInvalid(f"{profile_path}: resume file is empty: {resolved}")

    digest = sha256_file(resolved)
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


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
