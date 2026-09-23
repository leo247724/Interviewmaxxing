"""Fictional frontend acceptance edge over real service, stores, runner and Chromium.

Run with a Python environment containing the backend's dependencies. --backend-root
must be a committed integration checkout including tests/service. The real candidate
home and real providers are never read. Only source discovery and Jev's HTTP response
are fixtures; application execution uses the real runner and a separate localhost ATS.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import tempfile
import threading
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--backend-root", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--origin", required=True)
parser.add_argument("--port", type=int, default=4383)
args = parser.parse_args()
root = args.backend_root.resolve()
sys.path[:0] = [str(root), *[str(p) for group in ("packages", "apps") for p in (root / group).glob("*/src")]]

from interviewmaxxing_core import Compensation, DescriptionCompleteness, LocalPaths, SourceSearchState, WorkArrangement
import interviewmaxxing_jobs as jobs_pkg
import interviewmaxxing_selection as sel_pkg
from interviewmaxxing_service import Dispatcher, ServiceConfig
from interviewmaxxing_service.app import build_app
from interviewmaxxing_service.integration import LocalCandidateGateway, LocalJobsBackend, LocalSelectionBackend, runner_factory, runner_problem
from tests.service.test_acceptance import MockAts, write_fictional_profile
from tests.service.test_package_integration import FixtureAdapter, NoBrowser, _listing, jev_transport

args.output.mkdir(parents=True, exist_ok=True)
home = Path(tempfile.mkdtemp(prefix="imx-frontend-fictional-", dir=args.output.resolve()))
# An explicit empty environment prevents any IMX_* path override from reaching a
# user's profile, state or browser, even when the invoking shell contains one.
paths = LocalPaths.from_env({}, home=home)
paths.ensure()
write_fictional_profile(paths.profile_dir)
ats = MockAts(home / "mock-state")

def listing(key: str, title: str, location: str | None, arrangement: WorkArrangement, pay: bool):
    item = _listing("builtin", key, title, location, arrangement)
    url = ats.posting("standard") if key == "austin" else ats.posting("missing-required")
    provenance = item.provenance[0].model_copy(update={"source_url": ats.origin + "/jobs", "posting_url": url})
    return item.model_copy(update={
        "company": f"Fictional {key.title()} Marketing", "source_url": ats.origin + "/jobs",
        "posting_url": url, "provenance": [provenance],
        "description": "Own paid acquisition, paid search, paid social and pipeline measurement. Lead experiments and budget allocation.",
        "description_completeness": DescriptionCompleteness.FULL,
        "compensation": Compensation(minimum=125000, maximum=155000, currency="USD", period="YEAR", raw_text="$125,000–$155,000 per year") if pay else None,
    })

items = [listing("austin", "Paid Acquisition Lead", "Austin, TX", WorkArrangement.HYBRID, True),
         listing("remote", "Performance Marketing Manager", None, WorkArrangement.REMOTE, False)]
adapters = {
    "builtin": FixtureAdapter(jobs_pkg.SourceOutcome(state=SourceSearchState.OK,
        observations=[jobs_pkg.Observation(listing=item, raw={"fixture": True}) for item in reversed(items)])),
    "linkedin": FixtureAdapter(jobs_pkg.AccessProblem(SourceSearchState.NEEDS_USER,
        "Fictional source asks you to sign in", "Sign in to LinkedIn in the imx-jobs-linkedin window")),
    "indeed": FixtureAdapter(jobs_pkg.SourceOutcome(state=SourceSearchState.OK, observations=[])),
    "google": FixtureAdapter(jobs_pkg.SourceOutcome(state=SourceSearchState.OK, observations=[])),
}
repo = LocalJobsBackend(db_path=home / "jobs.sqlite3", transport=NoBrowser(), adapters=adapters, detail_limit=0)
key = sel_pkg.ApiKey("sk-fictional-not-a-credential", source="test")
decisions = LocalSelectionBackend(paths, client_factory=lambda: sel_pkg.JevClient(key, transport=jev_transport("APPLY")))
config = ServiceConfig(paths=paths, allowed_origin=args.origin, port=args.port, headless=True, application_mode="TEST_ONLY")
candidates = LocalCandidateGateway(config)
app = build_app(config, candidates=candidates, dispatcher=Dispatcher(runner_factory(config)),
    profile_loader=lambda: candidates.profile("default"), listings=repo, search=repo,
    decisions=decisions, runner_problem=runner_problem)
server = app.server()
ready = {"home": str(home), "atsOrigin": ats.origin, "serviceOrigin": f"http://127.0.0.1:{server.server_address[1]}",
    "frontendOrigin": args.origin, "fictional": True, "listingIds": [item.id for item in items]}
(args.output / "fictional-ready.json").write_text(json.dumps(ready, indent=2))
print(json.dumps(ready), flush=True)

def stop(*_):
    threading.Thread(target=server.shutdown, daemon=True).start()

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    server.serve_forever(poll_interval=0.2)
finally:
    server.server_close()
    app.close()
    ats.stop()
