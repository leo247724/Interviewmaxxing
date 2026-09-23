"""The service over the real J1 jobs and J2 selection packages.

Fixture source adapters and a fixture Jev transport replace only the network/browser
edge; the packages' own stores, dedupe, policy, ranking and decision records are real."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import interviewmaxxing_jobs as jobs_pkg
import interviewmaxxing_selection as sel_pkg
from interviewmaxxing_core import (
    JobListing,
    ListingSource,
    LocalPaths,
    SourceSearchState,
    WorkArrangement,
    listing_id_for,
)
from interviewmaxxing_service.integration import LocalJobsBackend, LocalSelectionBackend

from .conftest import FakeCandidates, FictionalSite, Harness, serve


def _listing(source: str, job_id: str, title: str, location: str | None,
             arrangement: WorkArrangement) -> JobListing:
    now = datetime.now(UTC)
    url = f"https://jobs.example.test/{source}/{job_id}"
    record = ListingSource(source=source, source_listing_id=job_id, source_url=url,
                           observed_at=now, evidence="fictional fixture card")
    return JobListing(
        id=listing_id_for(source, job_id), source=source, source_listing_id=job_id,
        source_url=url, title=title, company="Fictional Co", location=location,
        work_arrangement=arrangement,
        remote_eligibility="United States" if arrangement is WorkArrangement.REMOTE else None,
        description="Lead fictional B2B marketing programs.",
        description_completeness="PARTIAL", observed_at=now, evidence="fictional fixture card",
        provenance=[record],
    )


class FixtureAdapter:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome

    def search(self, ctx: Any) -> Any:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class NoBrowser:
    """The adapters never touch it; ``close`` is called when a source finishes."""

    def close(self, session: str) -> None:
        pass


def jev_transport(choice: str = "REVIEW") -> Any:
    """Answers every requested question with its first option (``choice`` for the
    final selection question), with a valid probability distribution."""

    def transport(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> Any:
        request = json.loads(body)
        answers = {}
        for name, question in request["questions"].items():
            options = list(question["criteria"])
            chosen = choice if name == "selection" and choice in options else options[0]
            rest = [o for o in options if o != chosen]
            probs = {chosen: 0.9, **{o: round(0.1 / len(rest), 6) for o in rest}} if rest \
                else {chosen: 1.0}
            answers[name] = {"type": "choice", "choice": chosen, "probabilities": probs,
                             "confidence": 0.9}
        payload = {"model": "typesafe/jev-1.13-fixture", "answers": answers,
                   "usage": {"input_tokens": 10, "output_tokens": 2, "cost": 0.0001},
                   "id": "gen-fixture", "provider": "TypeSafe"}
        return sel_pkg.HttpResponse(status=200, headers={}, body=json.dumps(payload).encode())

    return transport


@pytest.fixture
def real(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, tmp_path: Path
) -> Iterator[tuple[Harness, Any]]:
    paths = isolated_imx_home
    austin = _listing("builtin", "b-1", "Marketing Manager", "Austin, TX",
                      WorkArrangement.HYBRID)
    remote = _listing("builtin", "b-2", "Marketing Director", None, WorkArrangement.REMOTE)
    adapters = {
        "builtin": FixtureAdapter(jobs_pkg.SourceOutcome(
            state=SourceSearchState.OK,
            observations=[jobs_pkg.Observation(listing=remote, raw={}),
                          jobs_pkg.Observation(listing=austin, raw={})],
        )),
        "linkedin": FixtureAdapter(jobs_pkg.AccessProblem(
            SourceSearchState.NEEDS_USER, "LinkedIn asks you to sign in",
            "Sign in to LinkedIn in the imx-jobs-linkedin window")),
    }
    jobs_backend = LocalJobsBackend(db_path=tmp_path / "jobs.sqlite3", transport=NoBrowser(),
                                    adapters=adapters, detail_limit=0)
    key = sel_pkg.ApiKey("sk-fictional", source="test")
    decisions = LocalSelectionBackend(
        paths, client_factory=lambda: sel_pkg.JevClient(key, transport=jev_transport()),
    )
    with serve(paths, fictional_site, FakeCandidates(tmp_path / "p"),
               listings=jobs_backend, search=jobs_backend, decisions=decisions) as h:
        yield h, jobs_backend


def _search(h: Harness, sources: list[str]) -> dict[str, Any]:
    body = dict(h.client.get("/selection/preferences").json)
    body.pop("fingerprint")
    body["sources"] = sources
    run = h.client.post("/jobs/search", body).json
    deadline = time.monotonic() + 15
    while not run["finishedAt"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)
        run = h.client.get(f"/jobs/search/{run['id']}").json
    return dict(run)


def test_real_j1_search_store_and_real_j2_ranking(real: Any) -> None:
    h, backend = real
    run = _search(h, ["linkedin", "builtin"])
    states = {r["source"]: r for r in run["results"]}
    assert states["linkedin"]["state"] == "NEEDS_USER"
    assert states["linkedin"]["sessionName"] == "imx-jobs-linkedin"
    assert states["builtin"]["state"] == "OK" and states["builtin"]["resultCount"] == 2
    # Stored by J1 itself, then ranked by J2's location tier: Austin hybrid first,
    # US-wide remote kept as an eligible secondary tier.
    assert len(backend.store.list_listings()) == 2
    listings = h.client.get("/jobs").json["listings"]
    assert [x["title"] for x in listings] == ["Marketing Manager", "Marketing Director"]
    assert [x["priorityTier"] for x in listings] == ["PREFERRED", "SECONDARY"]
    assert [x["locationTier"] for x in listings] == ["ONSITE_HYBRID_TARGET", "REMOTE_ELIGIBLE"]
    assert "not excluded" in listings[1]["rankReason"]


def test_real_j2_decision_records_and_holds_without_profile(real: Any) -> None:
    h, _backend = real
    _search(h, ["builtin"])
    listing = h.client.get("/jobs").json["listings"][0]
    r = h.client.post(f"/selection/jobs/{listing['id']}", {})
    assert r.status == 200
    sel = r.json["selection"]
    assert sel["requestedModel"] == "typesafe/jev-1.13"
    # No candidate profile: held for review, never an effective APPLY.
    assert sel["effectiveChoice"] != "APPLY"
    assert any(hold["code"] == "MISSING_PROFILE" for hold in sel["holds"])
    assert sel["stale"] is False
    # The decision is persisted by J2 and linked when the listing is tracked.
    tracked = h.client.post(f"/jobs/{listing['id']}/track", {}).json
    board = h.client.get("/pipeline").json
    entry = next(e for e in board["entries"] if e["id"] == tracked["pipelineEntryId"])
    assert entry["selection"]["selectionId"] == sel["id"]
    with h.store() as store:
        assert store.list_applications() == []


def test_missing_key_is_a_recorded_provider_hold(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, tmp_path: Path
) -> None:
    repo = LocalJobsBackend(db_path=tmp_path / "jobs.sqlite3", transport=NoBrowser(), adapters={})
    listing = _listing("builtin", "k-1", "Marketing Manager", "Austin, TX",
                       WorkArrangement.ONSITE)
    repo.store.upsert(listing)
    decisions = LocalSelectionBackend(isolated_imx_home, env={})  # no key anywhere
    with serve(isolated_imx_home, fictional_site, FakeCandidates(tmp_path / "p"),
               listings=repo, search=repo, decisions=decisions) as h:
        sel = h.client.post(f"/selection/jobs/{listing.id}", {}).json["selection"]
        assert sel["effectiveChoice"] == "REVIEW"
        assert sel["providerError"]["code"] == "NOT_CONFIGURED"
        assert "sk-" not in json.dumps(sel)
