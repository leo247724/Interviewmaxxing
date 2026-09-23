"""Fictional frontend acceptance over the real service, stores, runner and Chromium.

Run with backend dependencies and a committed --backend-root. The candidate home is
new for every invocation; real profiles and provider credentials are never read.
Discovery and Jev's HTTP response are fixtures. Applications reach a localhost ATS.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from fictional_ats import start_marketing_ats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--port", type=int, default=4383)
    args = parser.parse_args()
    root = args.backend_root.resolve()
    sys.path[:0] = [
        str(root),
        *[str(p) for group in ("packages", "apps") for p in (root / group).glob("*/src")],
    ]

    from tests.service.test_acceptance import write_fictional_profile
    from tests.service.test_package_integration import (
        FixtureAdapter,
        NoBrowser,
        _listing,
        jev_transport,
    )

    import interviewmaxxing_jobs as jobs_pkg
    import interviewmaxxing_selection as sel_pkg
    from interviewmaxxing_core import (
        Compensation,
        DescriptionCompleteness,
        IdentityEvidenceKind,
        JobIdentityObservation,
        LocalPaths,
        SourceSearchState,
        WorkArrangement,
    )
    from interviewmaxxing_service import Dispatcher, ServiceConfig
    from interviewmaxxing_service.app import build_app
    from interviewmaxxing_service.integration import (
        LocalCandidateGateway,
        LocalJobsBackend,
        LocalSelectionBackend,
        runner_factory,
        runner_problem,
    )

    args.output.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="imx-frontend-fictional-", dir=args.output.resolve()))
    # Ignore all inherited IMX_* overrides, including paths to a real profile.
    paths = LocalPaths.from_env({}, home=home)
    paths.ensure()
    write_fictional_profile(paths.profile_dir)
    ats, fixture_jobs = start_marketing_ats(root, home / "mock-state")
    profile_file = paths.profile_dir / "default" / "profile.json"
    profile = json.loads(profile_file.read_text())
    profile["identity"]["address"] = {
        "city": "Austin", "region": "TX", "country": "United States",
    }
    for answer in profile["saved_answers"]:
        if answer["id"] == "sa.skills":
            answer["value"] = ["Paid search", "Paid social", "Conversion optimization", "Experiment design"]
        elif answer["id"] == "sa.why":
            answer["question"] = "Why do you want to work at Fictional Austin Marketing?"
            answer["value"] = "I want to lead paid acquisition experiments and improve qualified pipeline."
    profile_file.write_text(json.dumps(profile, indent=2))

    def listing(slug: str, location: str | None, arrangement: WorkArrangement, pay: bool):
        job = fixture_jobs[slug]
        item = _listing("builtin", job["job_code"], job["title"], location, arrangement)
        url = ats.origin + job["posting_path"]
        identity = JobIdentityObservation(
            ats_type="generic", ats_tenant=urlsplit(ats.origin).netloc,
            external_job_id=job["job_code"], evidence_kind=IdentityEvidenceKind.STRUCTURED_DATA,
            evidence="The fictional ATS posting's schema.org JobPosting identifier.",
            observed_url=url, company=job["company"], title=job["title"],
        )
        provenance = item.provenance[0].model_copy(update={
            "source_url": ats.origin + "/jobs", "posting_url": url,
            "employer_job_key": identity.identity_key,
            "evidence": identity.evidence,
        })
        return item.model_copy(update={
            "company": job["company"], "source_url": ats.origin + "/jobs",
            "posting_url": url, "provenance": [provenance],
            "description": "Own paid acquisition, paid search, paid social and pipeline measurement. Lead experiments and budget allocation.",
            "description_completeness": DescriptionCompleteness.FULL,
            "compensation": Compensation(
                minimum=125000, maximum=155000, currency="USD", period="YEAR",
                raw_text="$125,000-$155,000 per year",
            ) if pay else None,
        })

    items = [
        listing("standard", "Austin, TX", WorkArrangement.HYBRID, True),
        listing("missing-required", None, WorkArrangement.REMOTE, False),
    ]
    adapters = {
        "builtin": FixtureAdapter(jobs_pkg.SourceOutcome(
            state=SourceSearchState.OK,
            observations=[jobs_pkg.Observation(listing=item, raw={"fixture": True}) for item in reversed(items)],
        )),
        "linkedin": FixtureAdapter(jobs_pkg.AccessProblem(
            SourceSearchState.NEEDS_USER,
            "Fictional source asks you to sign in",
            "Sign in to LinkedIn in the imx-jobs-linkedin window",
        )),
        "indeed": FixtureAdapter(jobs_pkg.SourceOutcome(state=SourceSearchState.OK, observations=[])),
        "google": FixtureAdapter(jobs_pkg.SourceOutcome(state=SourceSearchState.OK, observations=[])),
    }
    repo = LocalJobsBackend(db_path=home / "jobs.sqlite3", transport=NoBrowser(), adapters=adapters, detail_limit=0)
    key = sel_pkg.ApiKey("sk-fictional-not-a-credential", source="test")
    decisions = LocalSelectionBackend(paths, client_factory=lambda: sel_pkg.JevClient(key, transport=jev_transport("APPLY")))
    config = ServiceConfig(paths=paths, allowed_origin=args.origin, port=args.port, headless=True, application_mode="TEST_ONLY")
    candidates = LocalCandidateGateway(config)
    app = build_app(
        config, candidates=candidates, dispatcher=Dispatcher(runner_factory(config)),
        profile_loader=lambda: candidates.profile("default"), listings=repo, search=repo,
        decisions=decisions, runner_problem=runner_problem,
    )
    server = app.server()
    ready = {
        "home": str(home), "atsOrigin": ats.origin,
        "serviceOrigin": f"http://127.0.0.1:{server.server_address[1]}",
        "frontendOrigin": args.origin, "fictional": True,
        "listingIds": [item.id for item in items], "fixtureJobs": fixture_jobs,
    }
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


if __name__ == "__main__":
    main()
