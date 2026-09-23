"""Jobs, preferences and Jev selection routes.

Domain data is canonical D0 (``JobSearchQuery``, ``JobListing``,
``SourceSearchResult``, ``SelectionPreferences``, ``JobSelection``) produced by the J1
jobs package and the J2 selection package. The service adds only orchestration:

* **Preferences** are stored as canonical ``SelectionPreferences`` plus the
  search-only settings (keywords, sources, per-source limit) in the service state
  database. ``location_priority`` is part of the canonical preferences, so it is in
  their fingerprint, Jev's evidence and its decision cache.
* **Search and decisions run in the background**, each on its own single-worker
  thread, independent of the application browser. The task id is written to the
  service database before anything is queued. An identical request that is already
  queued or running is joined instead of repeated; a different search while one runs
  is refused.
* **Nothing applies.** Searching, deciding and tracking never start an application;
  an effective APPLY only makes a listing eligible for the normal application request.
* **Ranking** orders listings by the canonical location priority (J2's
  ``location_tier``: Austin onsite/hybrid above eligible US-wide remote by default),
  then stated pay against the floor, then recency. Remote roles are ranked, never
  excluded.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import ValidationError

from interviewmaxxing_core import (
    ApplicationStore,
    CandidateProfile,
    CompensationFloor,
    CompensationPeriod,
    JobListing,
    JobSearchQuery,
    JobSelection,
    ListingStatus,
    LocationPriority,
    OnsiteTarget,
    RemoteTarget,
    SelectionPreferences,
    SourceSearchResult,
    SourceSearchState,
    UnknownCompensationPolicy,
    WorkArrangement,
    meets_floor,
    snapshot_hash,
)
from interviewmaxxing_pipeline import NewPipelineItem, TrackingFields

from . import errors
from .discovery_models import (
    CompensationFloorView,
    CompensationView,
    DecisionTaskView,
    LinkedSelectionView,
    ListingSourceView,
    ListingsView,
    ListingView,
    OnsiteTargetView,
    PolicyHoldView,
    ProviderErrorView,
    RemoteTargetView,
    SearchPreferencesInput,
    SearchPreferencesView,
    SearchRunView,
    SelectionView,
    SourceResultView,
)
from .pipeline_api import PipelineApi
from .state import ServiceState, Task
from .views import SAFE_ID, iso

log = logging.getLogger("interviewmaxxing.service.jobs")

MAX_ACTIVE_DECISIONS = 10
_TASK_ERRORS = {
    "INTERRUPTED": "The service stopped before this decision finished. Ask again.",
}
DECISION_WAIT_S = 20.0
LISTING_LIMIT = 500
SOURCE_LABELS = {"linkedin": "LinkedIn Jobs", "builtin": "Built In", "indeed": "Indeed",
                 "google": "Google Jobs"}


# --- package seams ----------------------------------------------------------------------


class ListingRepository(Protocol):
    """J1 listing storage (``interviewmaxxing_jobs.JobStore``)."""

    def get_listing(self, listing_id: str) -> JobListing | None: ...

    def list_listings(
        self, *, limit: int | None = None, rank_for: SelectionPreferences | None = None
    ) -> list[JobListing]:
        """Newest first; with ``rank_for``, ordered by its location priority (open
        before closed, then location tier) *before* ``limit`` is applied (J1)."""
        ...


class SearchBackend(Protocol):
    """J1 search for one source. Blocking; persists what it observed."""

    def search_source(self, query: JobSearchQuery, source: str) -> SourceSearchResult: ...


@dataclass(frozen=True)
class DecisionRecord:
    selection: JobSelection
    unresolved: list[str] = field(default_factory=list)
    """Facts the decision needed but could not establish, in plain language."""


@dataclass(frozen=True)
class Rank:
    tier: str
    """``PREFERRED``, ``EQUAL``, ``SECONDARY`` or ``UNRANKED`` (J2 ``LocationTier``)."""
    reason: str
    location: str = "UNRESOLVED"
    """How the listing's stated location relates to the targets, in the frontend's
    ``LocationTier`` terms: ``ONSITE_HYBRID_TARGET``, ``REMOTE_ELIGIBLE``,
    ``REMOTE_UNCONFIRMED``, ``OUTSIDE_TARGET`` or ``UNRESOLVED``."""


ApplicationLookup = Callable[[JobListing], str | None]


class DecisionBackend(Protocol):
    """J2 selection (``interviewmaxxing_selection.SelectionService``)."""

    def decide(
        self,
        listing: JobListing,
        preferences: SelectionPreferences,
        profile: CandidateProfile | None,
        *,
        candidate_id: str,
        application_lookup: ApplicationLookup,
    ) -> DecisionRecord: ...

    def latest_for(
        self, listing_ids: Sequence[str], *, candidate_id: str
    ) -> dict[str, DecisionRecord]:
        """The latest decision per listing made *for this candidate*, in one pass. A
        decision recorded for another candidate is never returned."""
        ...

    def get_many(
        self, selection_ids: Sequence[str], *, candidate_id: str
    ) -> dict[str, DecisionRecord]:
        """Decisions by id, only those that belong to this candidate."""
        ...

    def is_current(
        self,
        selection: JobSelection,
        listing: JobListing,
        preferences: SelectionPreferences,
        profile: CandidateProfile | None,
    ) -> bool: ...

    def rank(self, listing: JobListing, preferences: SelectionPreferences) -> Rank: ...


ProfileLoader = Callable[[], CandidateProfile | None]


# --- preferences ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchSettings:
    keywords: list[str]
    sources: list[str]
    max_results_per_source: int


def _defaults() -> tuple[SelectionPreferences, SearchSettings]:
    query = JobSearchQuery()
    return SelectionPreferences(), SearchSettings(
        keywords=list(query.keywords), sources=list(query.sources),
        max_results_per_source=query.max_results_per_source,
    )


_PREF_FIELDS = {
    "target_titles": "titlePhrases", "onsite": "onsite", "remote": "remote",
    "minimum_compensation": "minimumCompensation", "excluded_keywords": "excludedKeywords",
    "excluded_companies": "excludedCompanies", "location_priority": "locationPriority",
    "role_focus": "roleFocus",
    "title_phrases": "titlePhrases", "keywords": "keywords", "sources": "sources",
    "max_results_per_source": "maxResultsPerSource",
}


def _field_errors(exc: ValidationError) -> dict[str, str]:
    out: dict[str, str] = {}
    for err in exc.errors():
        loc = [str(p) for p in err["loc"] if not isinstance(p, int)]
        key = _PREF_FIELDS.get(loc[0], loc[0]) if loc else "preferences"
        message = str(err.get("msg", "Check this value.")).removeprefix("Value error, ")
        out.setdefault(key, message[:1].upper() + message[1:])
    return out


def preferences_from_input(
    body: SearchPreferencesInput, *, current: SelectionPreferences
) -> tuple[SelectionPreferences, SearchSettings, JobSearchQuery]:
    """Canonical preferences, search settings and the query they describe."""
    try:
        prefs = SelectionPreferences(
            target_titles=body.title_phrases,
            onsite=[OnsiteTarget(location=o.location, arrangements=o.arrangements)
                    for o in body.onsite],
            remote=RemoteTarget(eligible_region=body.remote.eligible_region)
            if body.remote else None,
            location_priority=LocationPriority(body.location_priority)
            if body.location_priority else current.location_priority,
            role_focus=body.role_focus if body.role_focus is not None else current.role_focus,
            minimum_compensation=CompensationFloor(
                amount=float(body.minimum_compensation.amount),
                currency=body.minimum_compensation.currency,
                period=CompensationPeriod(body.minimum_compensation.period),
            ) if body.minimum_compensation else None,
            unknown_compensation=UnknownCompensationPolicy(body.unknown_compensation),
            excluded_keywords=body.excluded_keywords,
            excluded_companies=body.excluded_companies,
            notes=current.notes,
        )
        query = JobSearchQuery.from_preferences(
            prefs, keywords=body.keywords, sources=body.sources,
            max_results_per_source=body.max_results_per_source,
        )
    except ValidationError as exc:
        raise errors.invalid("Some preferences are not valid.", _field_errors(exc)) from exc
    settings = SearchSettings(
        keywords=list(query.keywords), sources=list(query.sources),
        max_results_per_source=query.max_results_per_source,
    )
    return prefs, settings, query


def preferences_view(prefs: SelectionPreferences, settings: SearchSettings) -> SearchPreferencesView:
    floor = prefs.minimum_compensation
    return SearchPreferencesView(
        title_phrases=list(prefs.target_titles),
        keywords=settings.keywords,
        excluded_keywords=list(prefs.excluded_keywords),
        excluded_companies=list(prefs.excluded_companies),
        onsite=[OnsiteTargetView(location=o.location,
                                 arrangements=[a.value for a in o.arrangements])
                for o in prefs.onsite],
        remote=RemoteTargetView(eligible_region=prefs.remote.eligible_region)
        if prefs.remote else None,
        location_priority=prefs.location_priority.value,
        role_focus=prefs.role_focus,
        minimum_compensation=CompensationFloorView(
            amount=floor.amount, currency=floor.currency, period=floor.period.value,
        ) if floor else None,
        unknown_compensation=prefs.unknown_compensation.value,
        sources=settings.sources,
        max_results_per_source=settings.max_results_per_source,
        fingerprint=prefs.fingerprint,
    )


def _query_key(query: JobSearchQuery) -> str:
    return snapshot_hash(query.model_dump(mode="json", exclude={"id", "created_at"}))


# --- views ----------------------------------------------------------------------------------


def _source_result(entry: dict[str, Any], source: str) -> SourceResultView:
    return SourceResultView(
        source=source,
        state=entry.get("state", "QUEUED"),
        result_count=int(entry.get("result_count", 0)),
        message=entry.get("message"),
        user_action=entry.get("user_action"),
        session_name=entry.get("session_name"),
        started_at=entry.get("started_at"),
        finished_at=entry.get("finished_at"),
    )


def search_run_view(task: Task) -> SearchRunView:
    sources: dict[str, dict[str, Any]] = task.progress.get("sources", {})
    order: list[str] = task.request.get("query", {}).get("sources", list(sources))
    results = []
    for source in order:
        entry = dict(sources.get(source, {}))
        if task.state in ("INTERRUPTED", "FAILED") and entry.get("state") in (None, "QUEUED", "RUNNING"):
            entry["state"] = "ERROR"
            entry["message"] = (
                "The service stopped before this source finished. Search again."
                if task.state == "INTERRUPTED" else "The search stopped before this source finished."
            )
        results.append(_source_result(entry, source))
    finished = task.state in ("DONE", "FAILED", "INTERRUPTED")
    return SearchRunView(
        id=task.id,
        started_at=iso(task.created_at),
        finished_at=iso(task.updated_at) if finished else None,
        results=results,
    )


def _compensation(listing: JobListing) -> CompensationView | None:
    pay = listing.compensation
    if pay is None:
        return None
    return CompensationView(
        raw_text=pay.raw_text, minimum=pay.minimum, maximum=pay.maximum, currency=pay.currency,
        period=pay.period.value if pay.period else None,
    )


def decision_task_view(task: Task) -> DecisionTaskView:
    error = task.error if task.state in ("FAILED", "INTERRUPTED") else None
    if task.state == "INTERRUPTED":
        error = _TASK_ERRORS["INTERRUPTED"]
    result = task.result or {}
    return DecisionTaskView(
        id=task.id,
        state=task.state,
        error=error or ("The decision could not be made." if task.state == "FAILED" else None),
        result_id=result.get("selection_id") if task.state == "DONE" else None,
        requested_at=iso(task.created_at),
        updated_at=iso(task.updated_at),
    )


def selection_view(record: DecisionRecord, *, stale: bool) -> SelectionView:
    s = record.selection
    model = s.model_decision
    return SelectionView(
        id=s.id,
        effective_choice=s.effective_choice.value,
        model_choice=model.choice.value if model else None,
        probabilities={k.value: v for k, v in model.probabilities.items()} if model else None,
        confidence=model.confidence if model else None,
        requested_model=s.requested_model,
        returned_model=s.returned_model,
        rubric_version=s.rubric_version,
        holds=[PolicyHoldView(code=h.code.value, detail=h.detail) for h in s.holds],
        reasons=list(s.reasons),
        unresolved=list(record.unresolved),
        provider_error=ProviderErrorView(
            code=s.provider_error.code, message=s.provider_error.message,
            retryable=s.provider_error.retryable,
        ) if s.provider_error else None,
        decided_at=iso(s.decided_at),
        stale=stale,
    )


# --- the API ---------------------------------------------------------------------------------


class JobsApi:
    def __init__(
        self,
        *,
        state: ServiceState,
        candidate_id: str,
        state_db: Any,
        pipeline: PipelineApi,
        profile_loader: ProfileLoader,
        listings: ListingRepository | None,
        search: SearchBackend | None,
        decisions: DecisionBackend | None,
        unavailable: dict[str, str] | None = None,
    ) -> None:
        self.state = state
        self.candidate_id = candidate_id
        self.state_db = state_db
        self.pipeline = pipeline
        self.profile_loader = profile_loader
        self.listings = listings
        self.search_backend = search
        self.decisions = decisions
        self.unavailable = unavailable or {}
        self._search_pool = ThreadPoolExecutor(1, thread_name_prefix="imx-search")
        self._decision_pool = ThreadPoolExecutor(1, thread_name_prefix="imx-decide")
        self._lock = threading.Lock()
        self._done: dict[str, threading.Event] = {}
        self.listing_limit = LISTING_LIMIT

    def shutdown(self) -> None:
        self._search_pool.shutdown(wait=True, cancel_futures=True)
        self._decision_pool.shutdown(wait=True, cancel_futures=True)

    def status(self) -> dict[str, str]:
        return {
            "jobs": "available" if self.listings and self.search_backend else "unavailable",
            "selection": "available" if self.decisions else "unavailable",
        }

    def _require_listings(self) -> ListingRepository:
        if self.listings is None:
            raise errors.unavailable(self.unavailable.get(
                "jobs", "Job search isn't installed in this service."))
        return self.listings

    # --- preferences --------------------------------------------------------------------

    def load_preferences(self) -> tuple[SelectionPreferences, SearchSettings]:
        stored = self.state.load_preferences(self.candidate_id)
        prefs, settings = _defaults()
        if stored is None:
            return prefs, settings
        try:
            prefs = SelectionPreferences.model_validate(stored["selection"])
            settings = SearchSettings(**stored["search"])
        except (ValidationError, KeyError, TypeError) as exc:
            raise errors.conflict(
                "Saved job preferences can't be read. Save them again to replace them."
            ) from exc
        return prefs, settings

    def get_preferences(self) -> SearchPreferencesView:
        return preferences_view(*self.load_preferences())

    def save_preferences(self, body: SearchPreferencesInput) -> SearchPreferencesView:
        current, _ = self.load_preferences()
        prefs, settings, _query = preferences_from_input(body, current=current)
        self.state.save_preferences(self.candidate_id, {
            "selection": prefs.model_dump(mode="json"),
            "search": {"keywords": settings.keywords, "sources": settings.sources,
                       "max_results_per_source": settings.max_results_per_source},
        })
        return preferences_view(prefs, settings)

    # --- search ---------------------------------------------------------------------------

    def start_search(self, body: SearchPreferencesInput) -> tuple[SearchRunView, bool]:
        self._require_listings()
        if self.search_backend is None:
            raise errors.unavailable(self.unavailable.get(
                "jobs", "Job search isn't installed in this service."))
        current, _ = self.load_preferences()
        _prefs, _settings, query = preferences_from_input(body, current=current)
        key = _query_key(query)
        with self._lock:
            for active in self.state.active(self.candidate_id, "search"):
                if active.dedupe_key != key:
                    raise errors.conflict(
                        "A different search is still running. Wait for it to finish, then "
                        "search again."
                    )
            progress = {"sources": {s: {"state": "QUEUED"} for s in query.sources}}
            task, created = self.state.create_or_join(
                candidate_id=self.candidate_id, kind="search", dedupe_key=key,
                request={"query": query.model_dump(mode="json")}, progress=progress,
                prefix="srch",
            )
            if created:
                self._search_pool.submit(self._run_search, task.id, query)
        return search_run_view(task), created

    def _run_search(self, task_id: str, query: JobSearchQuery) -> None:
        assert self.search_backend is not None
        task = self.state.update(task_id, state="RUNNING")
        sources: dict[str, dict[str, Any]] = dict(task.progress.get("sources", {}))
        listing_ids: list[str] = []
        for source in query.sources:
            now = datetime.now(UTC).isoformat()
            sources[source] = {"state": "RUNNING", "started_at": now}
            self.state.update(task_id, progress={"sources": sources})
            try:
                result = self.search_backend.search_source(query, source)
                sources[source] = {
                    "state": result.state.value,
                    "result_count": result.result_count,
                    "message": result.message,
                    "user_action": result.user_action,
                    "session_name": result.session_name,
                    "started_at": iso(result.started_at),
                    "finished_at": iso(result.finished_at) if result.finished_at else now,
                }
                listing_ids += [i for i in result.listing_ids if i not in listing_ids]
            except Exception as exc:
                log.warning("search of %s failed: %s", source, type(exc).__name__)
                sources[source] = {
                    "state": SourceSearchState.ERROR.value,
                    "message": f"The {SOURCE_LABELS.get(source, source)} search failed "
                               f"({type(exc).__name__}).",
                    "started_at": now, "finished_at": datetime.now(UTC).isoformat(),
                }
            self.state.update(task_id, progress={"sources": sources})
        self.state.update(task_id, state="DONE", result={"listing_ids": listing_ids})

    def search_status(self, run_id: str) -> SearchRunView:
        if not SAFE_ID.match(run_id):
            raise errors.invalid("That is not a search id.")
        task = self.state.get(run_id)
        if task is None or task.kind != "search" or task.candidate_id != self.candidate_id:
            raise errors.not_found("No search with that id.")
        return search_run_view(task)

    # --- listings -------------------------------------------------------------------------

    def _application_lookup(
        self, apps: ApplicationStore, items: Mapping[str, Any] | None = None
    ) -> ApplicationLookup:
        """``items`` is the candidate's pipeline cards by listing id, loaded once per
        request; it is loaded here only when not given."""
        cards = self.pipeline.items_by_listing() if items is None else items

        def lookup(listing: JobListing) -> str | None:
            item = cards.get(listing.id)
            if item is not None and item.application_id:
                return item.application_id
            for url in (listing.application_url, listing.posting_url):
                if url:
                    try:
                        app = apps.find_application(self.candidate_id, url)
                    except ValueError:
                        app = None
                    if app is not None:
                        return app.id
            return None
        return lookup

    def _listing_view(
        self,
        listing: JobListing,
        *,
        prefs: SelectionPreferences,
        profile: CandidateProfile | None,
        items: Mapping[str, Any],
        lookup: ApplicationLookup,
        rank: Rank | None,
        record: DecisionRecord | None,
        task: Task | None,
    ) -> ListingView:
        selection = None
        if self.decisions is not None and record is not None:
            stale = not self.decisions.is_current(record.selection, listing, prefs, profile)
            selection = selection_view(record, stale=stale)
        item = items.get(listing.id)
        return ListingView(
            id=listing.id,
            title=listing.title,
            company=listing.company,
            location=listing.location,
            work_arrangement=listing.work_arrangement.value,
            remote_eligibility=listing.remote_eligibility,
            compensation=_compensation(listing),
            description=listing.description,
            description_completeness=listing.description_completeness.value,
            status=listing.status.value,
            posted_text=listing.posted_text,
            observed_at=iso(listing.observed_at),
            provenance=[
                ListingSourceView(source=p.source, source_url=p.source_url,
                                  posting_url=p.posting_url,
                                  application_url=p.application_url,
                                  observed_at=iso(p.observed_at))
                for p in listing.provenance
            ],
            selection=selection,
            pipeline_entry_id=item.id if item is not None else None,
            application_id=lookup(listing),
            posting_url=listing.posting_url,
            decision_task=decision_task_view(task) if task is not None else None,
            location_tier=rank.location if rank else None,
            priority_tier=rank.tier if rank else None,
            rank_reason=rank.reason if rank else None,
        )

    def _rank(self, listing: JobListing, prefs: SelectionPreferences) -> Rank | None:
        return self.decisions.rank(listing, prefs) if self.decisions is not None else None

    @staticmethod
    def _sort_key(listing: JobListing, rank: Rank | None, prefs: SelectionPreferences) -> tuple[Any, ...]:
        tier = {"PREFERRED": 0, "EQUAL": 0, "SECONDARY": 1}.get(rank.tier if rank else "", 2)
        floor = prefs.minimum_compensation
        pay = meets_floor(listing.compensation, floor) if floor else None
        return (
            listing.status is ListingStatus.CLOSED,
            tier,
            {True: 0, None: 1, False: 2}[pay],
            -listing.observed_at.timestamp(),
            listing.id,
        )

    def list_listings(self) -> ListingsView:
        repo = self._require_listings()
        prefs, _ = self.load_preferences()
        profile = self.profile_loader()
        items = self.pipeline.items_by_listing()
        # Austin-first (the saved location priority) is applied before the limit, so
        # newer remote listings cannot crowd older target-city ones out.
        listings = repo.list_listings(limit=self.listing_limit, rank_for=prefs)
        ranks = {x.id: self._rank(x, prefs) for x in listings}
        listings.sort(key=lambda x: self._sort_key(x, ranks[x.id], prefs))
        ids = [x.id for x in listings]
        records = self._records(ids)
        tasks = self._tasks(ids)
        apps = ApplicationStore.open(self.state_db)
        try:
            lookup = self._application_lookup(apps, items)
            views = [
                self._listing_view(x, prefs=prefs, profile=profile, items=items, lookup=lookup,
                                   rank=ranks[x.id], record=records.get(x.id),
                                   task=tasks.get(x.id))
                for x in listings
            ]
        finally:
            apps.close()
        last = self.state.latest(self.candidate_id, "search")
        return ListingsView(listings=views, last_run=search_run_view(last) if last else None)

    def _records(self, listing_ids: Sequence[str]) -> dict[str, DecisionRecord]:
        if self.decisions is None or not listing_ids:
            return {}
        aliases = self.pipeline.aliases(listing_ids)
        found = self.decisions.latest_for(
            [alias for ids in aliases.values() for alias in ids], candidate_id=self.candidate_id
        )
        out: dict[str, DecisionRecord] = {}
        for canonical, ids in aliases.items():
            records = [found[alias] for alias in ids if alias in found]
            if records:
                out[canonical] = max(records, key=lambda record: record.selection.decided_at)
        return out

    def _tasks(self, listing_ids: Sequence[str]) -> dict[str, Task]:
        aliases = self.pipeline.aliases(listing_ids)
        found = self.state.latest_by_subject(
            self.candidate_id, "decision", [alias for ids in aliases.values() for alias in ids]
        )
        return {
            canonical: max((found[alias] for alias in ids if alias in found),
                           key=lambda task: task.created_at)
            for canonical, ids in aliases.items() if any(alias in found for alias in ids)
        }

    def _listing(self, listing_id: str) -> JobListing:
        if not SAFE_ID.match(listing_id):
            raise errors.invalid("That is not a listing id.")
        listing = self._require_listings().get_listing(listing_id)
        if listing is None:
            raise errors.not_found("No saved listing with that id.")
        return listing

    def listing(self, listing_id: str) -> ListingView:
        listing = self._listing(listing_id)
        prefs, _ = self.load_preferences()
        items = self.pipeline.items_by_listing()
        apps = ApplicationStore.open(self.state_db)
        try:
            return self._listing_view(
                listing, prefs=prefs, profile=self.profile_loader(),
                items=items, lookup=self._application_lookup(apps, items),
                rank=self._rank(listing, prefs), record=self._records([listing.id]).get(listing.id),
                task=self._tasks([listing.id]).get(listing.id),
            )
        finally:
            apps.close()

    # --- decisions --------------------------------------------------------------------------

    def decide(self, listing_id: str) -> tuple[ListingView, bool]:
        """Ask Jev about one listing (explicit user action only). Returns the listing
        view and whether the decision finished within the wait."""
        listing = self._listing(listing_id)
        if self.decisions is None:
            raise errors.unavailable(self.unavailable.get(
                "selection", "Jev selection isn't installed in this service."))
        prefs, _ = self.load_preferences()
        key = snapshot_hash({"listing": listing.id, "preferences": prefs.fingerprint})
        with self._lock:
            active = self.state.active(self.candidate_id, "decision")
            aliases = self.pipeline.aliases([listing.id]).get(listing.id, [listing.id])
            joined = next((task for task in active if task.subject in aliases
                           and task.request.get("preferences") == prefs.model_dump(mode="json")), None)
            if len(active) >= MAX_ACTIVE_DECISIONS and joined is None:
                raise errors.conflict("Several decisions are already queued. Try again shortly.")
            if joined is not None:
                task, created = joined, False
            else:
                task, created = self.state.create_or_join(
                    candidate_id=self.candidate_id, kind="decision", dedupe_key=key,
                    subject=listing.id, request={"listing_id": listing.id,
                                                "preferences": prefs.model_dump(mode="json")},
                    prefix="dec",
                )
            done = self._done.setdefault(task.id, threading.Event())
            if created:
                self._decision_pool.submit(self._run_decision, task.id, listing.id, prefs, done)
        finished = done.wait(DECISION_WAIT_S)
        return self.listing(listing.id), finished

    def _run_decision(
        self, task_id: str, listing_id: str, prefs: SelectionPreferences, done: threading.Event
    ) -> None:
        assert self.decisions is not None and self.listings is not None
        try:
            self.state.update(task_id, state="RUNNING")
            listing = self.listings.get_listing(listing_id)
            if listing is None:
                raise LookupError("listing disappeared")
            apps = ApplicationStore.open(self.state_db)
            try:
                record = self.decisions.decide(
                    listing, prefs, self.profile_loader(), candidate_id=self.candidate_id,
                    application_lookup=self._application_lookup(apps),
                )
            finally:
                apps.close()
            if record.selection.candidate_id != self.candidate_id:
                raise PermissionError("decision belongs to another candidate")
            self.state.update(task_id, state="DONE", result={"selection_id": record.selection.id})
        except Exception as exc:
            log.warning("decision failed: %s", type(exc).__name__)
            message = (
                "The listing is no longer saved." if isinstance(exc, LookupError)
                else f"The decision could not be made ({type(exc).__name__}). Nothing was "
                "recorded; ask again."
            )
            self.state.update(task_id, state="FAILED", error=message)
        finally:
            done.set()
            with self._lock:
                self._done.pop(task_id, None)

    def linked_selections(self, selection_ids: Sequence[str]) -> dict[str, LinkedSelectionView]:
        """Pipeline card links, looked up in one pass and only for this candidate."""
        if self.decisions is None or not selection_ids:
            return {}
        found = self.decisions.get_many(selection_ids, candidate_id=self.candidate_id)
        return {
            sid: LinkedSelectionView(
                selection_id=r.selection.id, effective_choice=r.selection.effective_choice.value,
                decided_at=iso(r.selection.decided_at),
            )
            for sid, r in found.items()
            if r.selection.candidate_id == self.candidate_id
        }

    # --- tracking --------------------------------------------------------------------------

    def track(self, listing_id: str) -> tuple[ListingView, bool]:
        """Add a listing to the pipeline tracker (the first lane). Never applies."""
        listing = self._listing(listing_id)
        pay = listing.compensation
        annual_usd = pay is not None and pay.currency == "USD" and pay.period is CompensationPeriod.YEAR
        arrangement = (
            None if listing.work_arrangement is WorkArrangement.UNKNOWN
            else listing.work_arrangement.value.capitalize()
        )
        record = self._records([listing.id]).get(listing.id)
        tracking = TrackingFields(
            company=listing.company,
            role=listing.title,
            work_arrangement=arrangement,
            location_commute=listing.location,
            compensation_low=pay.minimum if annual_usd and pay else None,
            compensation_high=pay.maximum if annual_usd and pay else None,
            compensation_basis=pay.raw_text if pay else None,
            source_recruiter=", ".join(
                dict.fromkeys(SOURCE_LABELS.get(p.source, p.source) for p in listing.provenance)
            ),
        )
        url = listing.application_url or listing.posting_url
        try:
            new = NewPipelineItem(
                tracking=tracking, listing_id=listing.id, application_url=url,
                selection_id=record.selection.id if record else None,
            )
        except ValidationError:  # an observed link that is not an http(s) URL is dropped
            new = NewPipelineItem(
                tracking=tracking, listing_id=listing.id,
                selection_id=record.selection.id if record else None,
            )
        _item, created = self.pipeline.track(new)
        return self.listing(listing.id), created

