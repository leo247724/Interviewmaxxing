"""``interviewmaxxing-jobs`` (also ``python -m interviewmaxxing_jobs``).

Commands:
  search    run a live search through OpenCLI and store the listings
  listings  list stored listings
  show      one stored listing with its raw per-source evidence
  runs      recent search runs with per-source outcomes
  smoke     opt-in live check: a tiny real search into a temporary store, printing
            only public job metadata

Live commands drive the user's connected Chrome profile through OpenCLI in background
windows. They only search and read; they never apply or contact anyone.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from interviewmaxxing_core import (
    CompensationFloor,
    CompensationPeriod,
    JobListing,
    JobSearchQuery,
    JobSearchRun,
    ListingStatus,
    LocationPriority,
    OnsiteTarget,
    RemoteTarget,
)

from .opencli import DEFAULT_PROFILE, OpenCliTransport
from .search import JobSearchService
from .store import JobStore, default_db_path


def build_query(args: argparse.Namespace) -> JobSearchQuery:
    base: dict[str, Any] = {}
    if args.query_file:
        base = json.loads(Path(args.query_file).read_text("utf-8"))
    if args.title:
        base["title_phrases"] = args.title
    if args.keyword:
        base["keywords"] = args.keyword
    if args.exclude:
        base["excluded_keywords"] = args.exclude
    if args.onsite:
        base["onsite"] = [OnsiteTarget(location=loc).model_dump() for loc in args.onsite]
    if args.no_onsite:
        base["onsite"] = []
    if args.remote_region:
        base["remote"] = RemoteTarget(eligible_region=args.remote_region).model_dump()
    if args.no_remote:
        base["remote"] = None
    if args.min_pay is not None:
        base["minimum_compensation"] = CompensationFloor(
            amount=args.min_pay, currency="USD", period=CompensationPeriod.YEAR).model_dump()
    if args.sources:
        base["sources"] = [s.strip() for s in args.sources.split(",") if s.strip()]
    if args.limit is not None:
        base["max_results_per_source"] = args.limit
    if args.posted_within_days is not None:
        base["posted_within_days"] = args.posted_within_days
    if args.location_priority:
        base["location_priority"] = args.location_priority
    if args.role_focus:
        base["role_focus"] = args.role_focus
    return JobSearchQuery.model_validate(base)


def public_listing(listing: JobListing) -> dict[str, Any]:
    """Public job metadata only (what the job source shows anyone)."""
    pay = listing.compensation
    return {
        "id": listing.id,
        "source": listing.source,
        "also_on": sorted({p.source for p in listing.provenance[1:]}),
        "title": listing.title,
        "company": listing.company,
        "location": listing.location,
        "work_arrangement": listing.work_arrangement.value,
        "remote_eligibility": listing.remote_eligibility,
        "pay": pay.raw_text if pay else None,
        "pay_bounds": [pay.minimum, pay.maximum, pay.currency, pay.period.value if pay.period else None]
        if pay and pay.is_comparable else None,
        "status": listing.status.value,
        "posted": listing.posted_text,
        "description_chars": len(listing.description or ""),
        "description_completeness": listing.description_completeness.value,
        "posting_url": listing.posting_url,
        "source_url": listing.source_url,
        "application_url": listing.application_url,
        "employer_job_keys": sorted({p.employer_job_key for p in listing.provenance
                                     if p.employer_job_key}),
    }


def run_summary(run: JobSearchRun) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "query_id": run.query.id,
        "location_priority": run.query.location_priority.value,
        "title_seeds": run.query.title_phrases,
        "role_focus": run.query.role_focus,
        "sources": [
            {"source": r.source, "state": r.state.value, "listings": r.result_count,
             "pages_visited": r.pages_visited, "message": r.message,
             "user_action": r.user_action, "session": r.session_name}
            for r in run.results
        ],
    }


def _print(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def _service(args: argparse.Namespace, store: JobStore) -> JobSearchService:
    transport = OpenCliTransport(profile=args.profile, window=args.window)
    return JobSearchService(store, transport, profile=args.profile, detail_limit=args.details,
                            max_pages_per_leg=args.max_pages)


def cmd_search(args: argparse.Namespace) -> int:
    store = JobStore(args.db or default_db_path())
    run = _service(args, store).run(build_query(args))
    summary = run_summary(run)
    summary["db"] = str(store.path)
    _print(summary)
    return 0 if all(r.state.value in ("OK", "PARTIAL") for r in run.results) else 2


def cmd_smoke(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="imx-jobs-smoke-") as tmp:
        store = JobStore(args.db or Path(tmp) / "jobs.sqlite3")
        run = _service(args, store).run(build_query(args))
        ids = [i for r in run.results for i in r.listing_ids]
        listings = store.list_listings(ids=ids, rank_for=run.query)
        _print({**run_summary(run), "listings": [public_listing(x) for x in listings]})
        store.close()
    return 0 if any(r.result_count for r in run.results) else 2


def cmd_listings(args: argparse.Namespace) -> int:
    store = JobStore(args.db or default_db_path())
    status = ListingStatus(args.status) if args.status else None
    last = store.latest_run()
    ranked = store.list_listings(source=args.source, status=status, limit=args.limit,
                                 rank_for=last.query if last else JobSearchQuery())
    _print([public_listing(x) for x in ranked])
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = JobStore(args.db or default_db_path())
    listing = store.get_listing(args.listing_id)
    if listing is None:
        print(f"no listing {args.listing_id}", file=sys.stderr)
        return 1
    _print({"listing": listing.model_dump(mode="json"),
            "observations": store.observations(listing.id)})
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    store = JobStore(args.db or default_db_path())
    _print([run_summary(r) for r in store.list_runs(args.limit)])
    return 0


def _search_options(p: argparse.ArgumentParser, *, limit: int | None, details: int) -> None:
    p.add_argument("--query-file", help="JSON JobSearchQuery (see examples/job-search.example.json)")
    p.add_argument("--title", action="append",
                   help="title seed (repeatable); seeds guide search, they are not a title filter")
    p.add_argument("--role-focus", help="semantic description of the roles wanted")
    p.add_argument("--keyword", action="append", help="extra keyword (repeatable)")
    p.add_argument("--exclude", action="append", help="excluded title keyword (repeatable)")
    p.add_argument("--onsite", action="append", help='onsite/hybrid location, e.g. "Austin, TX"')
    p.add_argument("--no-onsite", action="store_true")
    p.add_argument("--remote-region", help='remote eligibility region, e.g. "United States"')
    p.add_argument("--no-remote", action="store_true")
    p.add_argument("--min-pay", type=float, help="annual USD floor (ranking only)")
    p.add_argument("--sources", help="comma list: linkedin,builtin,indeed,google")
    p.add_argument("--limit", type=int, default=limit,
                   help="max listings per source (default: the query's, 50)")
    p.add_argument("--details", type=int, default=details, help="max job pages opened per source")
    p.add_argument("--max-pages", type=int, default=3, help="max result pages per search leg")
    p.add_argument("--posted-within-days", type=int)
    p.add_argument("--location-priority", choices=[x.value for x in LocationPriority],
                   help="default STRONGLY_PREFER_ONSITE_HYBRID (Austin before remote)")
    p.add_argument("--profile", default=DEFAULT_PROFILE, help="OpenCLI Browser Bridge profile")
    p.add_argument("--window", choices=["background", "foreground"], default="background")
    p.add_argument("--db", help="SQLite path (default $IMX_JOBS_DB or $IMX_HOME/jobs/jobs.sqlite3)")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="interviewmaxxing-jobs", description=__doc__,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = root.add_subparsers(dest="command", required=True)
    p = sub.add_parser("search", help="live search through OpenCLI")
    _search_options(p, limit=None, details=10)
    p.set_defaults(func=cmd_search)
    p = sub.add_parser("smoke", help="opt-in live smoke test (temporary store)")
    _search_options(p, limit=2, details=1)
    p.set_defaults(func=cmd_smoke)
    p = sub.add_parser("listings", help="stored listings")
    p.add_argument("--source")
    p.add_argument("--status", choices=[s.value for s in ListingStatus])
    p.add_argument("--limit", type=int)
    p.add_argument("--db")
    p.set_defaults(func=cmd_listings)
    p = sub.add_parser("show", help="one listing with raw evidence")
    p.add_argument("listing_id")
    p.add_argument("--db")
    p.set_defaults(func=cmd_show)
    p = sub.add_parser("runs", help="recent search runs")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--db")
    p.set_defaults(func=cmd_runs)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
