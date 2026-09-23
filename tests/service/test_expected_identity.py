"""HTTP handoffs persist their proven job identity before real runner execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_core import CandidateProfile, JobListing, LocalPaths, WorkArrangement

from ..core.test_runner import IDENTITY, Script, _form, _page, _runner
from .conftest import SITE_URL, FakeCandidates, FictionalSite, serve
from .test_jobs_routes import FakeListings, listing

EXPECTED = IDENTITY.identity_key


def repo_with_expected_job() -> tuple[FakeListings, JobListing]:
    repo = FakeListings()
    saved = listing("expected-job", "Fictional Analyst", location="Austin, TX",
                    arrangement=WorkArrangement.HYBRID)
    saved = saved.model_copy(update={
        "application_url": SITE_URL,
        "provenance": [source.model_copy(update={
            "application_url": SITE_URL, "employer_job_key": EXPECTED,
        }) for source in saved.provenance],
    })
    repo.items[saved.id] = saved
    return repo, saved


def form_page(identity: Any = IDENTITY) -> Any:
    return _page(_form(url=SITE_URL), identity=identity).model_copy(update={"observed_url": SITE_URL})


def executor(script: Script, candidate: CandidateProfile) -> Any:
    def factory(config: Any) -> Any:
        return lambda interaction: _runner(config.paths, candidate, script, interaction=interaction)
    return factory


@pytest.mark.parametrize("missing", [False, True])
def test_new_wrong_or_unidentified_page_never_fills_and_pin_survives_restart(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite,
    fictional_candidate: CandidateProfile, tmp_path: Path, missing: bool,
) -> None:
    repo, saved = repo_with_expected_job()
    observed = None if missing else IDENTITY.model_copy(update={"external_job_id": "different-job"})
    script = Script(pages=[form_page(observed)])
    candidates = FakeCandidates(tmp_path / "profile")
    with serve(isolated_imx_home, fictional_site, candidates, listings=repo,
               executor_factory=executor(script, fictional_candidate)) as h:
        response = h.client.post("/applications", {
            "applicationUrl": SITE_URL, "profile": h.profile(), "resumeId": h.setup_candidate(),
            "listingId": saved.id,
        })
        assert response.status == 201, response.json
        app_id = response.json["id"]
        h.wait_idle()
        final = h.client.get(f"/applications/{app_id}").json
        assert final["state"] == "FAILED_RETRYABLE" and final["failure"]
        assert "fill" not in script.calls and "submit" not in script.calls
        with h.store() as store:
            assert store.expected_job_identity(app_id) == EXPECTED
        [card] = h.client.get("/pipeline").json["entries"]
        assert card["application"]["confirmationReference"] is None

    script.calls.clear()
    with serve(isolated_imx_home, fictional_site, candidates, listings=repo,
               executor_factory=executor(script, fictional_candidate)) as h:
        assert h.client.post(f"/applications/{app_id}/resume", {}).status == 200
        h.wait_idle()
        assert h.client.get(f"/applications/{app_id}").json["state"] == "FAILED_RETRYABLE"
        assert "fill" not in script.calls and "submit" not in script.calls
        # A caller cannot replace the persisted expected job on a repeat request.
        repo.items[saved.id] = saved.model_copy(update={"provenance": [
            source.model_copy(update={"employer_job_key": "ats:mock:fictional:different-job"})
            for source in saved.provenance
        ]})
        conflict = h.client.post("/applications", {
            "applicationUrl": SITE_URL, "profile": h.profile(),
            "resumeId": h.client.get("/candidate").json["defaultResumeId"], "listingId": saved.id,
        })
        assert conflict.status == 409, conflict.json
        with h.store() as store:
            assert store.expected_job_identity(app_id) == EXPECTED


@pytest.mark.parametrize("changes_before_submit", [False, True])
def test_expected_identity_is_pinned_before_dispatch_and_checked_before_submit(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite,
    fictional_candidate: CandidateProfile, tmp_path: Path, changes_before_submit: bool,
) -> None:
    repo, saved = repo_with_expected_job()
    page = form_page()
    script = Script(pages=[page])
    if changes_before_submit:
        script.inspect_pages = [form_page(IDENTITY.model_copy(update={"external_job_id": "wrong-job"}))]
    with serve(isolated_imx_home, fictional_site, FakeCandidates(tmp_path / "profile"),
               listings=repo, executor_factory=executor(script, fictional_candidate)) as h:
        real_submit = h.service.dispatcher.submit

        def dispatch(application_id: str, *args: Any, **kwargs: Any) -> Any:
            with h.store() as store:
                assert store.expected_job_identity(application_id) == EXPECTED
            with h.app.pipeline.store() as store:
                [item] = store.list_items("default")
                assert item.application_id == application_id
            return real_submit(application_id, *args, **kwargs)

        h.service.dispatcher.submit = dispatch
        response = h.client.post("/applications", {
            "applicationUrl": SITE_URL, "profile": h.profile(), "resumeId": h.setup_candidate(),
            "listingId": saved.id,
        })
        assert response.status == 201, response.json
        h.wait_idle()
        final = h.client.get(f"/applications/{response.json['id']}").json
        if changes_before_submit:
            assert final["state"] == "FAILED_RETRYABLE"
            assert "submit" not in script.calls
        else:
            assert final["state"] == "SUBMITTED", final
            assert script.calls.count("submit") == 1
            assert final["receipt"]["confirmationAuthority"] == "site"
