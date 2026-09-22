"""Adapters to the concrete candidate package (C2P) and I1 runner.

Both are imported lazily so the presentation layer and its tests do not depend on
their availability. ``LocalCandidateGateway`` and ``runner_factory`` are the only
places that name their APIs; if a published signature differs, only this module
changes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from interviewmaxxing_core import CandidateIdentity, CandidateProfile, SavedAnswer

from .candidate import CandidateDataInvalid, CandidateSetupError, ResumeEntry
from .config import ServiceConfig
from .executor import ApplicationExecutor, ExecutorFactory, ServiceInteraction


def _entry(record: Any) -> ResumeEntry:
    uploaded = getattr(record, "uploaded_at", None) or getattr(record, "stored_at", None)
    if not isinstance(uploaded, datetime):
        uploaded = datetime.fromtimestamp(0, UTC)
    return ResumeEntry(
        id=str(record.id),
        filename=str(record.filename),
        size_bytes=int(record.size_bytes),
        uploaded_at=uploaded,
        sha256=str(record.sha256),
        media_type=str(record.media_type),
    )


class LocalCandidateGateway:
    """``CandidateGateway`` over ``interviewmaxxing_candidate.LocalCandidateStore``."""

    def __init__(self, config: ServiceConfig) -> None:
        from interviewmaxxing_candidate import LocalCandidateStore  # type: ignore[import-not-found]

        self._store: Any = LocalCandidateStore.from_paths(config.paths)

    def _errors(self) -> tuple[type[Exception], ...]:
        import interviewmaxxing_candidate as candidate

        return tuple(
            e for e in (
                getattr(candidate, "ResumeRejected", None),
                getattr(candidate, "ProfileRejected", None),
            ) if isinstance(e, type)
        )

    def load_profile(self, candidate_id: str) -> CandidateProfile | None:
        from interviewmaxxing_core import CandidateNotFound, CandidateProfileInvalid

        try:
            profile: CandidateProfile = self._store.load(candidate_id)
        except CandidateNotFound:
            return None
        except CandidateProfileInvalid as exc:
            raise CandidateDataInvalid(type(exc).__name__) from exc
        return profile

    def list_resumes(self, candidate_id: str) -> list[ResumeEntry]:
        return [_entry(r) for r in self._store.list_resumes(candidate_id)]

    def store_resume(
        self, candidate_id: str, *, filename: str, content: bytes, media_type: str
    ) -> ResumeEntry:
        try:
            record = self._store.store_resume(
                candidate_id, filename=filename, content=content, media_type=media_type
            )
        except self._errors() as exc:
            raise CandidateSetupError("resumeFile", str(exc)) from exc
        return _entry(record)

    def upsert_profile(
        self, candidate_id: str, *, identity: CandidateIdentity, resume_id: str
    ) -> CandidateProfile:
        try:
            profile: CandidateProfile = self._store.upsert_profile(
                candidate_id, identity=identity, resume_id=resume_id
            )
        except self._errors() as exc:
            raise CandidateSetupError("resumeId", str(exc)) from exc
        return profile

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
