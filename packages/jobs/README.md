# interviewmaxxing-jobs (J1)

Job discovery through the user's live Chrome (OpenCLI Browser Bridge, profile
`jgd7jms9`), producing D0/D0R2 `JobListing` records in a private local store. It
searches LinkedIn Jobs, Built In, Indeed and Google Jobs by reading their visible
UI. It never applies, messages employers, changes accounts, solves challenges, or
replays private APIs. A listing the user selects enters the existing application
flow through `application_url` or `posting_url`.

## Public API (for S1/S2 and the CLI)

```python
from interviewmaxxing_core import JobSearchQuery, LocationPriority, SelectionPreferences
from interviewmaxxing_jobs import (
    JobStore, JobSearchService, OpenCliTransport, rank_listings, location_tier,
)

store = JobStore.from_env()            # $IMX_JOBS_DB or $IMX_HOME/jobs/jobs.sqlite3 (0600, dir 0700)
service = JobSearchService(
    store, OpenCliTransport(profile="jgd7jms9", window="background"),
    detail_limit=10,                   # job pages opened per source; the rest are stored from cards
    max_pages_per_leg=3,
)
run = service.run(JobSearchQuery())    # -> JobSearchRun (saved); one SourceSearchResult per source

store.get_run(run_id) -> JobSearchRun | None
store.latest_run() -> JobSearchRun | None
store.list_runs(limit=20) -> list[JobSearchRun]
store.get_listing(listing_id) -> JobListing | None       # merged-away ids resolve to the survivor
store.list_listings(source=None, status=None, ids=None, limit=None,
                    rank_for=query_or_preferences) -> list[JobListing]
store.observations(listing_id) -> list[dict]            # raw per-source evidence, oldest first
store.upsert(listing, raw=..., run_id=...) -> JobListing # used by the service
rank_listings(listings, query_or_preferences) -> list[JobListing]
location_tier(listing, query_or_preferences) -> int
```

`JobSearchService.run` is synchronous (minutes for a full search). S1 should run it in
its background dispatcher and poll `store.get_run`. Per-source results are only
available when the run finishes; to show progress per source, run one source per
call (`JobSearchQuery(sources=["linkedin"], ...)`).

### Per-source states (never an empty "success" when blocked)

| state | meaning |
| --- | --- |
| `OK` | searched; results exhausted within the limit (0 is a real result) |
| `PARTIAL` | listings collected, then stopped (limit reached, page budget, unrendered LinkedIn cards, a failed detail page); `message` says which |
| `NEEDS_USER` | sign-in wall or verification challenge; `user_action` gives the exact `opencli ... --window foreground` command and `session_name` (`imx-jobs-<source>`) stays open for the user |
| `BLOCKED` | the site denied access |
| `ERROR` | OpenCLI/transport failure or unreadable page; `message` explains |
| `SKIPPED` | no adapter for that source slug |

### Location priority (user requirement 2026-09-22)

`JobSearchQuery.location_priority` defaults to `STRONGLY_PREFER_ONSITE_HYBRID`:

* All onsite/hybrid legs (Austin, TX) are searched before remote legs, and receive
  four times the remote share of `max_results_per_source`. Budget a leg does not use
  carries forward, so remote roles are still collected (never excluded) and cannot
  crowd Austin out of a bounded result limit.
* `rank_for=` / `rank_listings` order: stated onsite/hybrid in Austin, then Austin
  with unstated arrangement, then eligible remote, then the rest; closed last.
* `BALANCED` alternates legs 1:1; `PREFER_REMOTE` reverses the order and weights.
* The run stores the query, including `location_priority`.

### Identity and dedupe (D0R2)

Each observation carries the source's own job id (`source_listing_id`) and a
job-specific `posting_url`; `source_url` is where it was seen (for a card, the
results page), which is provenance only. Application URLs are not identity. A
cross-source merge happens only on a proven `employer_job_key`, read from a
job-specific ATS URL (Greenhouse, Lever, Ashby, SmartRecruiters, Workday) and named in
the evidence. Google results that link to LinkedIn/Indeed/Built In postings keep those
links as raw `linked_postings` evidence, not as identity. Repeated observations of a
posting refresh it and keep enriched keys/evidence; `CLOSED` stays closed; stated
numeric pay is not replaced by text-only pay.

## CLI

```bash
python -m interviewmaxxing_jobs search --query-file examples/job-search.example.json
python -m interviewmaxxing_jobs search --title "marketing manager" --onsite "Austin, TX" \
    --remote-region "United States" --sources linkedin,indeed --limit 20 --details 5
python -m interviewmaxxing_jobs listings --limit 20      # ranked by the last run's priority
python -m interviewmaxxing_jobs show <listing-id>
python -m interviewmaxxing_jobs runs
# Opt-in live smoke (temporary store, public job metadata only):
python -m interviewmaxxing_jobs smoke --limit 2 --details 1
IMX_JOBS_LIVE=1 pytest tests/jobs/test_cli.py -k live
```

## Sources (observed 2026-09-22)

| source | search | detail | notes |
| --- | --- | --- | --- |
| LinkedIn | `/jobs/search/?keywords&location&f_WT` (1 onsite, 2 remote, 3 hybrid) | `/jobs/view/<id>/` top card | The installed `opencli linkedin search` replays the internal Voyager API and is not used. Background tabs are `document.hidden`: only ~7 of 25 cards render and descriptions do not render, so the rest are read from job pages (budgeted) and descriptions stay `NONE`. External apply links are decoded from LinkedIn's redirect. |
| Built In | `/jobs/<remote\|hybrid\|office>?search&city&state&country=USA&allLocations=true&page` | schema.org JobPosting | Explicit salary bounds, `validThrough`, remote country. Mixed labels ("In-Office or Remote") stay `UNKNOWN`. APPLY is a Built In redirect and is not followed. |
| Indeed | `/jobs?q&l` (`l=Remote` for US-wide remote), `start` | `/viewjob?jk=` JobPosting | Commute estimates are stripped from locations; "Estimated" pay has no bounds; "Apply now" is Indeed-hosted, not an employer URL. |
| Google Jobs | `/search?q=<title> jobs in <place>&udm=8` | detail pane after a structured click on the result | First results page only; descriptions are usually truncated (`PARTIAL`). |

Extraction scripts in `src/interviewmaxxing_jobs/js/` are read-only (enforced by a
test). Sessions are restricted to `imx-jobs-<source>`; the transport never binds a
tab, so the user's own tabs (e.g. `imx-assessment-opencli`) are never touched.
