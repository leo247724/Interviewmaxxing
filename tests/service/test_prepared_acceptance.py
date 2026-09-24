"""WP3 acceptance: the real I1 runner prepares the mock ATS's standard job through the
service, and the service presents it as a review, not as a request for input.

Same fictional setup as ``test_acceptance.py`` (temporary ``IMX_HOME``, the mock's
fictional candidate "Avery Quill", headless Chromium, the localhost mock ATS). The
runner stays preparation-only; nothing is submitted.
"""

from __future__ import annotations

import json

import pytest

from interviewmaxxing_core import ApplicationStore, LocalPaths

from .conftest import FictionalSite
from .test_acceptance import STATES_DONE, MockAts, real_service, start, wait_for
from .test_acceptance import ats as ats  # fixture
from .test_acceptance import home as home  # fixture

pytestmark = pytest.mark.slow


def test_prepared_application_is_presented_for_review(
    home: LocalPaths, ats: MockAts, fictional_site: FictionalSite
) -> None:
    with real_service(home, fictional_site, prepare_only=True) as h:
        linked = h.client.post("/pipeline/entries", {
            "lane": "saved", "fields": {"company": "Brambleway Analytics", "role": "Test Role"},
            "applicationUrl": ats.url("standard"),
        }).json
        # An unlinked card for the same job, saved with a tracking parameter.
        unlinked = h.client.post("/pipeline/entries", {
            "lane": "saved", "fields": {"company": "Brambleway Analytics", "role": "Saved copy"},
            "applicationUrl": ats.url("standard") + "?utm_source=newsletter",
        }).json
        unrelated = h.client.post("/pipeline/entries", {
            "lane": "saved", "fields": {"company": "Other Co", "role": "Other Role"},
            "applicationUrl": ats.url("rippling"),
        }).json
        response = start(h, ats.url("standard"), pipeline_entry_id=linked["id"])
        assert response.status == 201, response.json
        app_id = response.json["id"]
        view = wait_for(h, app_id, STATES_DONE)

        # A prepared review, not a request for input.
        assert view["state"] == "NEEDS_INPUT", view["events"][-5:]
        assert view["needs"] is None and view["receipt"] is None
        preparation = view["preparation"]
        assert preparation is not None
        assert preparation["ready"] is True and preparation["submitted"] is False
        assert preparation["captchaPending"] is False
        assert isinstance(preparation["formStep"], int) and preparation["formUrl"]
        assert [e["type"] for e in view["events"][-3:]] == [
            "preparation.ready", "application.inspecting", "application.needs_input"]
        assert view["events"][-1]["message"] == "Paused at the final review step for you to check."

        # The screenshot of the filled review page is served by the evidence route.
        shots = [e for e in preparation["evidence"] if e["kind"] == "screenshot"]
        assert shots and all(e["source"] == "site" for e in shots)
        image = h.client.get(shots[0]["href"].removeprefix("/api/imx"))
        assert image.status == 200 and image.headers["content-type"] == "image/png"

        # What was filled, with the saved answers' own wording and no internal ids.
        review = view["review"]
        assert {"identity", "resume", "saved_answer"} <= {r["source"] for r in review}
        by_question = {r["question"]: r for r in review}
        work_auth = by_question["Are you legally authorized to work in the United States?"]
        assert work_auth["wordingRecorded"] is True and work_auth["source"] == "saved_answer"
        assert work_auth["control"] == "single_select" and work_auth["page"] >= 1
        assert by_question["Resume"]["control"] == "file"
        assert by_question["Resume"]["wordingRecorded"] is False
        assert all(set(r) == {"question", "wordingRecorded", "page", "control", "value",
                              "source", "confidence"} for r in review)
        serialized = json.dumps(view)
        for private in ("sa.work_auth", "fact.years", "resume_supplied", str(home.profile_dir)):
            assert private not in serialized

        # The list finds it and the cards that point at it.
        listed = h.client.get("/applications")
        assert listed.status == 200
        [summary] = [a for a in listed.json["applications"] if a["id"] == app_id]
        assert summary["preparation"] == preparation
        assert sorted(summary["pipelineEntryIds"]) == sorted([linked["id"], unlinked["id"]])
        assert unrelated["id"] not in json.dumps(listed.json)

        # Nothing reached the employer.
        assert ats.submissions("standard")["accepted_count"] == 0
        with ApplicationStore.open(home.state_db) as store:
            assert store.list_attempts(app_id) == [] and store.get_receipt(app_id) is None
