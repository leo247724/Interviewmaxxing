"""Assemble the service from its parts (used by ``__main__`` and the tests)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from interviewmaxxing_core import CandidateProfile

from .application_links import ApplicationLinks
from .candidate import CandidateGateway
from .config import ServiceConfig
from .executor import Dispatcher
from .jobs_api import DecisionBackend, JobsApi, ListingRepository, SearchBackend
from .ownership import ServiceOwnership
from .pipeline_api import PipelineApi
from .server import LoopbackHTTPServer, make_server
from .service import PresentationService
from .state import ServiceState


@dataclass
class ServiceApp:
    config: ServiceConfig
    service: PresentationService
    pipeline: PipelineApi
    jobs: JobsApi
    state: ServiceState
    ownership: ServiceOwnership

    def server(self) -> LoopbackHTTPServer:
        return make_server(self.service, pipeline=self.pipeline, jobs=self.jobs)

    def close(self) -> None:
        try:
            self.jobs.shutdown()
            self.service.dispatcher.shutdown()
        finally:
            self.state.close()
            self.ownership.close()


def build_app(
    config: ServiceConfig,
    *,
    candidates: CandidateGateway,
    dispatcher: Dispatcher,
    profile_loader: Callable[[], CandidateProfile | None],
    listings: ListingRepository | None = None,
    search: SearchBackend | None = None,
    decisions: DecisionBackend | None = None,
    runner_problem: Callable[[], str | None] | None = None,
    unavailable: dict[str, str] | None = None,
) -> ServiceApp:
    state: ServiceState | None = None
    ownership: ServiceOwnership | None = None
    try:
        config.paths.ensure()
        ownership = ServiceOwnership(config.paths.state_db.parent / "service.lock")
        state = ServiceState(config.paths.state_db.parent / "service.sqlite3")
        state.interrupt_active()
        service = PresentationService(
            config, candidates=candidates, dispatcher=dispatcher, runner_problem=runner_problem
        )
        service.recover()
        jobs_ref: dict[str, Any] = {}

        def selection_lookup(selection_ids: Any) -> Any:
            jobs = jobs_ref.get("jobs")
            return jobs.linked_selections(selection_ids) if jobs is not None else {}

        def listing_exists(listing_id: str) -> bool | None:
            return None if listings is None else listings.get_listing(listing_id) is not None

        def listing_aliases(ids: Any) -> dict[str, list[str]]:
            read = getattr(listings, "listing_aliases", None)
            if read is None:
                return {listing_id: [listing_id] for listing_id in ids}
            result: dict[str, list[str]] = read(ids)
            return result

        pipeline = PipelineApi(
            config.paths, config.candidate_id,
            selection_lookup=selection_lookup, listing_exists=listing_exists,
            listing_aliases=listing_aliases,
        )
        jobs = JobsApi(
            state=state, candidate_id=config.candidate_id, state_db=config.paths.state_db,
            pipeline=pipeline, profile_loader=profile_loader, listings=listings, search=search,
            decisions=decisions, unavailable=unavailable,
        )
        jobs_ref["jobs"] = jobs

        def track_listing(listing_id: str) -> str:
            view, _created = jobs.track(listing_id)
            assert view.pipeline_entry_id is not None
            return view.pipeline_entry_id

        service.application_links = ApplicationLinks(
            pipeline, get_listing=lambda lid: listings.get_listing(lid) if listings else None,
            track_listing=track_listing,
        )
        return ServiceApp(config=config, service=service, pipeline=pipeline, jobs=jobs, state=state,
                          ownership=ownership)
    except BaseException:
        dispatcher.shutdown()
        if state is not None:
            state.close()
        if ownership is not None:
            ownership.close()
        raise
