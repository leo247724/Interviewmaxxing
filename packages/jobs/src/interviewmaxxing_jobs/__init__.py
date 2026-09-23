"""Job discovery for Interviewmaxxing (J1).

Searches LinkedIn Jobs, Built In, Indeed and Google Jobs through the user's live
Chrome (OpenCLI Browser Bridge, owned ``imx-jobs-<source>`` sessions), parses only
what each source shows into D0 ``JobListing`` records, and stores them locally with
per-source outcomes. Discovery never applies: a selected listing enters the
existing application flow through its ``application_url`` or ``posting_url``.
"""

from .opencli import DEFAULT_PROFILE, BrowserTransport, OpenCliTransport, TransportError
from .ranking import location_tier, place_match, rank_listings, remote_eligibility
from .search import JobSearchService, session_name
from .sources import AccessProblem, Observation, SourceAdapter, SourceOutcome, default_adapters
from .store import JobStore, ListingConflict, combine, default_db_path

__all__ = [
    "DEFAULT_PROFILE",
    "AccessProblem",
    "BrowserTransport",
    "JobSearchService",
    "JobStore",
    "ListingConflict",
    "Observation",
    "OpenCliTransport",
    "SourceAdapter",
    "SourceOutcome",
    "TransportError",
    "combine",
    "default_adapters",
    "default_db_path",
    "location_tier",
    "place_match",
    "rank_listings",
    "remote_eligibility",
    "session_name",
]
