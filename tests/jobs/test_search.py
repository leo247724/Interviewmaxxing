from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from interviewmaxxing_core import JobSearchQuery, LocationPriority, SourceSearchState
from interviewmaxxing_jobs import JobSearchService, JobStore, TransportError

from .conftest import FakeTransport, fixture_routes

Clock = Callable[[], datetime]


def service(tmp_path: Path, transport: Any, clock: Clock) -> JobSearchService:
    return JobSearchService(JobStore(tmp_path / "jobs.sqlite3"), transport, clock=clock,
                            profile="test-profile", detail_limit=5)


def query(**overrides: Any) -> JobSearchQuery:
    base: dict[str, Any] = {"title_phrases": ["marketing manager"], "max_results_per_source": 5}
    return JobSearchQuery.model_validate({**base, **overrides})


def by_source(run: Any) -> dict[str, Any]:
    return {r.source: r for r in run.results}


def test_all_four_sources_are_searched_and_stored(tmp_path: Path, clock: Clock) -> None:
    transport = FakeTransport(fixture_routes())
    svc = service(tmp_path, transport, clock)
    run = svc.run(query())
    results = by_source(run)
    assert [r.source for r in run.results] == ["linkedin", "builtin", "indeed", "google"]
    assert {r.state for r in run.results} <= {SourceSearchState.OK, SourceSearchState.PARTIAL}
    assert results["linkedin"].result_count == 3
    assert results["builtin"].result_count == 2
    assert results["indeed"].result_count == 2

    # The Google result and the LinkedIn posting share a proven Greenhouse job key.
    linkedin_manager = svc.store.list_listings(ids=results["linkedin"].listing_ids)
    merged = next(x for x in linkedin_manager if x.source_listing_id == "4000000001")
    assert merged.id in results["google"].listing_ids
    assert {p.source for p in merged.provenance} == {"linkedin", "google"}
    # Indeed's posting with the same title is not merged without proof.
    indeed_ids = set(results["indeed"].listing_ids)
    assert merged.id not in indeed_ids

    stored = svc.store.get_run(run.id)
    assert stored is not None
    assert stored.query.location_priority is LocationPriority.STRONGLY_PREFER_ONSITE_HYBRID
    assert all(("close", f"imx-jobs-{s}") in transport.calls
               for s in ("linkedin", "builtin", "indeed", "google"))
    # Search only: no clicks except selecting Google results in the results list.
    clicks = [c for c in transport.calls if c[0] == "click"]
    assert clicks and all(c[1] == "imx-jobs-google" and c[2] == "[data-share-url]" for c in clicks)


def test_austin_legs_run_before_remote_legs(tmp_path: Path, clock: Clock) -> None:
    transport = FakeTransport(fixture_routes())
    service(tmp_path, transport, clock).run(query(sources=["indeed"], max_results_per_source=10))
    searches = [c[2] for c in transport.calls if c[0] == "open" and "/jobs?" in c[2]]
    assert searches[0] == "https://www.indeed.com/jobs?q=marketing+manager&l=Austin%2C+TX"
    assert "l=Remote" in searches[-1]


def test_sign_in_wall_is_needs_user_and_other_sources_continue(tmp_path: Path, clock: Clock) -> None:
    transport = FakeTransport(fixture_routes({"linkedin": "login_wall"}))
    run = service(tmp_path, transport, clock).run(query(sources=["linkedin", "builtin"]))
    results = by_source(run)
    blocked = results["linkedin"]
    assert blocked.state is SourceSearchState.NEEDS_USER
    assert blocked.listing_ids == []
    assert blocked.session_name == "imx-jobs-linkedin"
    assert blocked.user_action and "Sign in to LinkedIn" in blocked.user_action
    assert ("close", "imx-jobs-linkedin") not in transport.calls  # left for the user
    assert results["builtin"].state is SourceSearchState.OK
    assert results["builtin"].result_count == 2


def test_transport_failure_is_an_explained_error(tmp_path: Path, clock: Clock) -> None:
    class Broken(FakeTransport):
        def open(self, session: str, url: str) -> None:
            if session == "imx-jobs-indeed":
                raise TransportError("opencli open: extension disconnected")
            super().open(session, url)

    run = service(tmp_path, Broken(fixture_routes()), clock).run(query(sources=["indeed", "builtin"]))
    results = by_source(run)
    assert results["indeed"].state is SourceSearchState.ERROR
    assert results["indeed"].message and "extension disconnected" in results["indeed"].message
    assert results["builtin"].state is SourceSearchState.OK


def test_unknown_source_is_skipped_and_excluded_titles_are_counted(tmp_path: Path, clock: Clock) -> None:
    transport = FakeTransport(fixture_routes())
    run = service(tmp_path, transport, clock).run(
        query(sources=["builtin", "ziprecruiter"], excluded_keywords=["director"]))
    results = by_source(run)
    assert results["ziprecruiter"].state is SourceSearchState.SKIPPED
    assert results["builtin"].result_count == 1
    assert results["builtin"].message and "1 titles matched excluded keywords" in results["builtin"].message
