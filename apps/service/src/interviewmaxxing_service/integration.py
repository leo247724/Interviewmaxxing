"""Adapters to the concrete candidate package (C2P) and the I1 runner.

These are the only places that name their APIs, so a signature change touches only
this module. Both packages are imported lazily; the presentation layer and its tests
do not need them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from interviewmaxxing_core import (
    CandidateIdentity,
    CandidateNotFound,
    CandidateProfile,
    CandidateProfileInvalid,
    SavedAnswer,
)

from .candidate import CandidateDataInvalid, CandidateSetupError, CandidateSetupState, ResumeEntry
from .config import ServiceConfig
from .executor import ApplicationExecutor, ExecutorFactory, ServiceInteraction


def _modified_at(path: str) -> datetime | None:
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime, UTC)
    except OSError:
        return None


def _entry(stored: Any) -> ResumeEntry:
    """``interviewmaxxing_candidate.StoredResume`` -> ``ResumeEntry``."""
    artifact = stored.artifact
    return ResumeEntry(
        id=artifact.id,
        filename=artifact.filename,
        size_bytes=artifact.size_bytes,
        uploaded_at=stored.uploaded_at,
        file_modified_at=None if stored.uploaded_at else _modified_at(artifact.path),
    )


def _sentence(message: str) -> str:
    message = message.strip()
    return (message[:1].upper() + message[1:]).rstrip(".") + "." if message else message


class LocalCandidateGateway:
    """``CandidateGateway`` over ``interviewmaxxing_candidate.LocalCandidateStore``."""

    def __init__(self, config: ServiceConfig) -> None:
        from interviewmaxxing_candidate import LocalCandidateStore

        self._store = LocalCandidateStore.from_paths(
            config.paths, max_resume_bytes=config.max_upload_bytes
        )

    def setup(self, candidate_id: str) -> CandidateSetupState:
        try:
            state = self._store.candidate_setup(candidate_id)
        except CandidateNotFound as exc:  # the configured id itself is unusable
            raise CandidateDataInvalid("the configured candidate id is not usable") from exc
        return CandidateSetupState(
            identity=state.identity,
            resumes=[_entry(r) for r in state.resumes],
            selected_resume_id=state.selected_resume_id,
            complete=state.complete,
        )

    def store_resume(self, candidate_id: str, *, filename: str, content: bytes) -> ResumeEntry:
        from interviewmaxxing_candidate import ResumeRejected

        try:
            stored = self._store.store_resume(candidate_id, filename=filename, content=content)
        except ResumeRejected as exc:
            raise CandidateSetupError("resumeFile", _sentence(str(exc))) from exc
        return _entry(stored)

    def upsert_profile(
        self, candidate_id: str, *, identity: CandidateIdentity, resume_id: str
    ) -> CandidateProfile:
        from interviewmaxxing_candidate import ResumeNotFound

        try:
            return self._store.upsert_profile(candidate_id, identity=identity, resume_id=resume_id)
        except ResumeNotFound as exc:
            raise CandidateSetupError(
                "resumeId", "Choose one of your saved resumes or upload one."
            ) from exc
        except CandidateProfileInvalid as exc:
            raise CandidateDataInvalid("the saved profile does not validate") from exc

    def save_answer(self, candidate_id: str, answer: SavedAnswer) -> None:
        self._store.save_answer(candidate_id, answer)


def runner_factory(config: ServiceConfig) -> ExecutorFactory:
    """Build I1 runners for background runs (one per run, created on the executor
    thread so its store connection belongs to that thread)."""

    def make(interaction: ServiceInteraction) -> ApplicationExecutor:
        from interviewmaxxing_cli.runner import create_runner  # type: ignore[import-not-found]

        runner: ApplicationExecutor = create_runner(
            config.paths, headless=config.headless, interaction=interaction
        )
        return runner

    return make
