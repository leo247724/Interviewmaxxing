"""Adapters to the concrete candidate package (C2P) and the I1 runner.

These are the only places that name their APIs, so a signature change touches only
this module. Both packages are imported lazily; the presentation layer and its tests
do not need them.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from interviewmaxxing_core import (
    CandidateIdentity,
    CandidateNotFound,
    CandidateProfile,
    CandidateProfileInvalid,
    JobListing,
    JobSearchQuery,
    JobSelection,
    LocalPaths,
    SavedAnswer,
    SelectionPreferences,
    SourceSearchResult,
)

from .candidate import CandidateDataInvalid, CandidateSetupError, CandidateSetupState, ResumeEntry
from .config import ServiceConfig
from .executor import ApplicationExecutor, ExecutorFactory, ServiceInteraction
from .jobs_api import ApplicationLookup, DecisionRecord, Rank


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

    def profile(self, candidate_id: str) -> CandidateProfile | None:
        """The complete canonical profile, or None before setup or when it does not
        load (selection then records a MISSING_PROFILE hold, never an APPLY)."""
        try:
            profile: CandidateProfile = self._store.load(candidate_id)
        except (CandidateNotFound, CandidateProfileInvalid):
            return None
        return profile


def _runner_module() -> Any:
    return importlib.import_module("interviewmaxxing_cli.runner")


def runner_problem() -> str | None:
    """Why the I1 runner cannot be used, or None. Checked before any application
    request is recorded, so an unavailable runner fails truthfully and does nothing."""
    try:
        module = _runner_module()
    except ImportError:
        return "The application runner isn't installed in this service yet. Nothing was sent."
    if not hasattr(module, "create_runner") and not hasattr(module, "LocalApplicationRunner"):
        return "The installed application runner has no usable entry point. Nothing was sent."
    return None


def runner_factory(config: ServiceConfig) -> ExecutorFactory:
    """Build I1 runners for background runs (one per run, created on the executor
    thread so its store connection belongs to that thread).

    Uses ``interviewmaxxing_cli.runner.create_runner(paths, *, headless, interaction)``
    when published, else ``LocalApplicationRunner(paths=, interaction=, headless=)``."""

    def make(interaction: ServiceInteraction) -> ApplicationExecutor:
        module = _runner_module()
        runner: ApplicationExecutor
        if hasattr(module, "create_runner"):
            runner = module.create_runner(
                config.paths, headless=config.headless, interaction=interaction
            )
        else:
            runner = module.LocalApplicationRunner(
                paths=config.paths, interaction=interaction, headless=config.headless
            )
        return runner

    return make


# --- J1 jobs --------------------------------------------------------------------------------


class LocalJobsBackend:
    """``ListingRepository`` + ``SearchBackend`` over ``interviewmaxxing_jobs``.

    Searches one source per call with J1's ``JobSearchService`` (OpenCLI transport,
    owned ``imx-jobs-<source>`` sessions), so the service can report per-source
    progress. ``transport``/``adapters`` are injectable for fixture tests."""

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        transport: Any = None,
        adapters: Any = None,
        detail_limit: int | None = None,
    ) -> None:
        jobs = importlib.import_module("interviewmaxxing_jobs")  # J1, optional
        self._jobs = jobs
        self.store = jobs.JobStore(db_path or jobs.default_db_path())
        self._transport = transport
        self._adapters = adapters
        self._detail_limit = detail_limit

    def get_listing(self, listing_id: str) -> JobListing | None:
        listing: JobListing | None = self.store.get_listing(listing_id)
        return listing

    def list_listings(self, *, limit: int | None = None) -> list[JobListing]:
        listings: list[JobListing] = self.store.list_listings(limit=limit)
        return listings

    def search_source(self, query: JobSearchQuery, source: str) -> SourceSearchResult:
        transport = self._transport or self._jobs.OpenCliTransport()
        service = self._jobs.JobSearchService(self.store, transport, adapters=self._adapters)
        run = service.run(
            query.model_copy(update={"sources": [source]}), detail_limit=self._detail_limit
        )
        result: SourceSearchResult = run.results[0]
        return result


# --- J2 selection ---------------------------------------------------------------------------

_UNRESOLVED_COMPENSATION = {
    "UNKNOWN": "Pay isn't stated.",
    "NONCOMPARABLE": "Pay isn't stated in a form comparable with your minimum.",
}
_UNRESOLVED_LOCATION = {
    "UNKNOWN": "The work arrangement or location isn't stated.",
    "REMOTE_NEEDS_ELIGIBILITY": "Where remote workers may live isn't stated as the United States.",
}


class LocalSelectionBackend:
    """``DecisionBackend`` over ``interviewmaxxing_selection``.

    The OpenRouter key is read by J2's ``load_api_key`` from the server environment or
    the explicit ``IMX_OPENROUTER_ENV_FILE`` path, on each decision, never logged or
    returned. A missing key is a recorded ``PROVIDER_ERROR`` hold (NOT_CONFIGURED),
    never an APPLY. Stores are opened per call (SQLite thread affinity). Only J2's
    minimal ``CandidateEvidence.from_profile`` projection is sent to Jev."""

    def __init__(
        self,
        paths: LocalPaths,
        *,
        client_factory: Any = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._sel = importlib.import_module("interviewmaxxing_selection")  # J2, optional
        self._paths = paths
        self._client_factory = client_factory
        self._env = env

    def _client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        try:
            key = self._sel.load_api_key(environ=self._env)
        except self._sel.CredentialError:
            return None
        return self._sel.JevClient(key)

    def _service(self, store: Any, *, candidate_id: str | None = None,
                 lookup: Any = None, client: Any = None) -> Any:
        return self._sel.SelectionService(
            client=client, store=store, application_lookup=lookup, candidate_id=candidate_id
        )

    def _store(self) -> Any:
        return self._sel.SelectionStore(self._sel.default_store_path(self._paths))

    def _evidence(self, profile: CandidateProfile | None) -> Any:
        return self._sel.CandidateEvidence.from_profile(profile) if profile else None

    def _record(self, outcome: Any) -> DecisionRecord:
        unresolved: list[str] = []
        pay = _UNRESOLVED_COMPENSATION.get(str(getattr(outcome.compensation, "value", "")))
        if pay:
            unresolved.append(pay)
        where = _UNRESOLVED_LOCATION.get(str(getattr(outcome.location, "value", "")))
        if where:
            unresolved.append(where)
        for hold in outcome.selection.holds:
            if hold.code.value in ("MISSING_PROFILE", "INSUFFICIENT_EVIDENCE"):
                unresolved.append(hold.detail)
        return DecisionRecord(selection=outcome.selection, unresolved=unresolved)

    def decide(
        self,
        listing: JobListing,
        preferences: SelectionPreferences,
        profile: CandidateProfile | None,
        *,
        candidate_id: str,
        application_lookup: ApplicationLookup,
    ) -> DecisionRecord:
        store = self._store()
        try:
            service = self._service(
                store, candidate_id=candidate_id, lookup=application_lookup,
                client=self._client(),
            )
            outcome = service.select(listing, preferences, self._evidence(profile))
        finally:
            store.close()
        return self._record(outcome)

    def latest(self, listing_id: str) -> DecisionRecord | None:
        store = self._store()
        try:
            outcome = store.latest(listing_id)
        finally:
            store.close()
        return self._record(outcome) if outcome else None

    def get(self, selection_id: str) -> DecisionRecord | None:
        store = self._store()
        try:
            outcome = store.get(selection_id)
        finally:
            store.close()
        return self._record(outcome) if outcome else None

    def is_current(
        self,
        selection: JobSelection,
        listing: JobListing,
        preferences: SelectionPreferences,
        profile: CandidateProfile | None,
    ) -> bool:
        current: bool = self._service(None).is_current(
            selection, listing, preferences, self._evidence(profile)
        )
        return current

    def rank(self, listing: JobListing, preferences: SelectionPreferences) -> Rank:
        location = self._sel.check_location(listing, preferences)
        tier = self._sel.location_tier(location, preferences.location_priority)
        return Rank(
            tier=tier.value,
            reason=self._sel.location_priority_reason(tier, preferences.location_priority, location),
        )
