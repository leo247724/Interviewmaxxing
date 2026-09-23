"""Assemble the service from its parts (used by ``__main__`` and the tests)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from interviewmaxxing_core import CandidateProfile

from .candidate import CandidateGateway
from .config import ServiceConfig
from .executor import Dispatcher
from .jobs_api import DecisionBackend, JobsApi, ListingRepository, SearchBackend
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

    def server(self) -> LoopbackHTTPServer:
        return make_server(self.service, pipeline=self.pipeline, jobs=self.jobs)

    def close(self) -> None:
        self.jobs.shutdown()
        self.service.dispatcher.shutdown()
        self.state.close()


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
    config.paths.ensure()
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

    pipeline = PipelineApi(
        config.paths, config.candidate_id,
        selection_lookup=selection_lookup, listing_exists=listing_exists,
    )
    jobs = JobsApi(
        state=state, candidate_id=config.candidate_id, state_db=config.paths.state_db,
        pipeline=pipeline, profile_loader=profile_loader, listings=listings, search=search,
        decisions=decisions, unavailable=unavailable,
    )
    jobs_ref["jobs"] = jobs
    return ServiceApp(config=config, service=service, pipeline=pipeline, jobs=jobs, state=state)
