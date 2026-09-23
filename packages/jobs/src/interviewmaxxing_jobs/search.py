"""Run a JobSearchQuery across sources and store what was observed.

Each source runs in its own OpenCLI session (``imx-jobs-<source>``) and fails
independently: a sign-in wall or challenge becomes a NEEDS_USER result naming the
session to resolve it in, and the other sources still run. Nothing here applies,
messages an employer, or changes an account.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import datetime

from interviewmaxxing_core import (
    JobSearchQuery,
    JobSearchRun,
    SourceSearchResult,
    SourceSearchState,
    new_id,
    utc_now,
)

from .opencli import DEFAULT_PROFILE, BrowserTransport, TransportError
from .sources import AccessProblem, SearchContext, SourceAdapter, default_adapters
from .store import JobStore


def session_name(source: str) -> str:
    return f"imx-jobs-{source}"


class JobSearchService:
    """``run(query)`` searches every source in ``query.sources`` and persists listings.

    ``detail_limit`` bounds how many job pages are opened per source (the rest are
    stored from their search cards). ``close_sessions`` releases each owned tab when
    its source finishes, except a NEEDS_USER source, whose session stays for the user.
    """

    def __init__(
        self,
        store: JobStore,
        transport: BrowserTransport,
        *,
        adapters: Mapping[str, SourceAdapter] | None = None,
        clock: Callable[[], datetime] = utc_now,
        profile: str = DEFAULT_PROFILE,
        detail_limit: int = 10,
        max_pages_per_leg: int = 3,
        page_pause_s: float = 1.5,
        close_sessions: bool = True,
    ) -> None:
        self.store = store
        self.transport = transport
        self.adapters = dict(adapters) if adapters is not None else default_adapters()
        self.clock = clock
        self.profile = profile
        self.detail_limit = detail_limit
        self.max_pages_per_leg = max_pages_per_leg
        self.page_pause_s = page_pause_s
        self.close_sessions = close_sessions

    def run(self, query: JobSearchQuery, *, detail_limit: int | None = None) -> JobSearchRun:
        run_id = new_id("run")
        started = self.clock()
        results = [self._search_source(query, source, run_id,
                                       self.detail_limit if detail_limit is None else detail_limit)
                   for source in query.sources]
        run = JobSearchRun(id=run_id, query=query, results=results, started_at=started,
                           finished_at=max(self.clock(), started))
        self.store.save_run(run)
        return run

    def _search_source(self, query: JobSearchQuery, source: str, run_id: str,
                       detail_limit: int) -> SourceSearchResult:
        started = self.clock()
        session = session_name(source)
        adapter = self.adapters.get(source)
        if adapter is None:
            return SourceSearchResult(
                query_id=query.id, source=source, state=SourceSearchState.SKIPPED,
                message=f"no job-search adapter for source {source!r}", started_at=started,
                finished_at=self.clock())
        ctx = SearchContext(
            transport=self.transport, session=session, query=query,
            limit=query.max_results_per_source, detail_limit=max(0, detail_limit),
            clock=self.clock, profile=self.profile, max_pages_per_leg=self.max_pages_per_leg,
            page_pause_s=self.page_pause_s,
        )
        keep_session = False
        try:
            outcome = adapter.search(ctx)
        except AccessProblem as problem:
            keep_session = problem.state is SourceSearchState.NEEDS_USER
            return SourceSearchResult(
                query_id=query.id, source=source, state=problem.state, message=problem.message,
                user_action=problem.user_action,
                session_name=session if keep_session else None,
                started_at=started, finished_at=max(self.clock(), started))
        except (TransportError, ValueError, KeyError, TypeError) as exc:
            return SourceSearchResult(
                query_id=query.id, source=source, state=SourceSearchState.ERROR,
                message=f"{source} search failed: {exc}",
                started_at=started, finished_at=max(self.clock(), started))
        finally:
            if self.close_sessions and not keep_session:
                with suppress(TransportError):
                    self.transport.close(session)

        excluded = [k.casefold() for k in query.excluded_keywords]
        stored_ids: list[str] = []
        dropped = 0
        for obs in outcome.observations:
            title = obs.listing.title.casefold()
            if any(k in title for k in excluded):
                dropped += 1
                continue
            stored = self.store.upsert(obs.listing, raw=obs.raw, run_id=run_id)
            if stored.id not in stored_ids:
                stored_ids.append(stored.id)
        message = outcome.message
        if dropped:
            message = "; ".join(filter(None, [message, f"{dropped} titles matched excluded keywords"]))
        return SourceSearchResult(
            query_id=query.id, source=source, state=outcome.state, listing_ids=stored_ids,
            pages_visited=outcome.pages_visited, message=message,
            user_action=outcome.user_action, started_at=started,
            finished_at=max(self.clock(), started))
