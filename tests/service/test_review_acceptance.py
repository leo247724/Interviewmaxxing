"""WP11 round 3 acceptance: the review lane end to end against the localhost mock ATS.

HTTP -> service -> the real I1 runner (preparation only) -> headless Chromium -> the mock
ATS (``scripts/mock_ats.py``), with the mock's fictional candidate "Avery Quill" in a
temporary ``IMX_HOME``, as ``test_acceptance.py``. The person reviews the prepared
application, changes an answer, prepares it again, approves it and submits it from the
dashboard; the submission runner is the CLI ``submit``'s, built only because the service
config enables submission. The only site that ever receives anything is the local mock.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from interviewmaxxing_core import ApplicationStore, LocalPaths
from interviewmaxxing_service import ServiceConfig
from interviewmaxxing_service.integration import (
    LocalCandidateGateway,
    runner_factory,
    runner_problem,
    submission_runner_factory,
)
from interviewmaxxing_service.service import SUBMISSION_OFF

from .conftest import FictionalSite, Harness, serve
from .test_acceptance import STATES_DONE, MockAts, start, wait_for
from .test_acceptance import ats as ats  # fixture
from .test_acceptance import home as home  # fixture

pytestmark = pytest.mark.slow

WHY = "Why do you want to work at Brambleway Analytics?"
EDITED = ("I want to build the fictional forecasting platform with a team that measures "
          "what it ships.")


@contextmanager
def reviewing_service(
    paths: LocalPaths, site: FictionalSite, *, allow_submission: bool
) -> Iterator[Harness]:
    """The production runners (headless): preparation-only, and the submission runner
    only when ``allow_submission``."""
    origin = "http://127.0.0.1:4317"
    production = ServiceConfig(paths=paths, allowed_origin=origin, headless=True,
                               allow_submission=allow_submission)
    submission = submission_runner_factory(production)
    assert (submission is not None) is allow_submission
    with serve(
        paths, site, LocalCandidateGateway(ServiceConfig(paths=paths, allowed_origin=origin)),
        executor_factory=lambda _config: runner_factory(production),
        submission_factory=(lambda _config: submission) if submission is not None else None,
        runner_problem=runner_problem, reconcile_wait_s=25.0, headless=True,
        allow_submission=allow_submission,
    ) as h:
        yield h


def review(h: Harness, app_id: str) -> dict[str, Any]:
    got = h.client.get(f"/applications/{app_id}/review")
    assert got.status == 200, got.json
    body: dict[str, Any] = got.json
    return body


def prepared(h: Harness, ats: MockAts) -> str:
    response = start(h, ats.url("standard"))
    assert response.status == 201, response.json
    app_id = str(response.json["id"])
    view = wait_for(h, app_id, STATES_DONE)
    assert view["state"] == "NEEDS_INPUT" and view["preparation"] is not None, view["events"][-5:]
    return app_id


def test_review_edit_approve_and_submit_from_the_dashboard(
    home: LocalPaths, ats: MockAts, fictional_site: FictionalSite
) -> None:
    with reviewing_service(home, fictional_site, allow_submission=True) as h:
        assert h.client.get("/healthz").json["submission"] == "enabled"
        app_id = prepared(h, ats)

        [item] = h.client.get("/review").json["applications"]
        assert (item["id"], item["stage"], item["hold"]["kind"]) == (app_id, "prepared", "ready")
        assert item["job"]["title"] == "Senior Data Platform Engineer"

        body = review(h, app_id)
        rows = {row["question"]: row for row in body["answers"]}
        # Every question of the form in its order, the optional ones nothing answered blank.
        assert list(rows)[:3] == ["First name", "Last name", "Email"]
        assert rows["Resume"]["provenance"]["kind"] == "resume"
        assert rows["First name"]["provenance"]["kind"] == "identity"
        assert rows[WHY]["provenance"]["kind"] == "saved_policy"  # saved for global reuse
        blanks = [q for q, row in rows.items() if row["provenance"]["kind"] == "blank"]
        assert blanks and all(rows[q]["required"] is False for q in blanks)
        why = rows[WHY]
        assert why["edit"]["control"] == "long_text"
        assert why["edit"]["reuse"] == ["application", "job", "global"]

        # Change the answer for this application only, then prepare it again.
        saved = h.client.post(f"/applications/{app_id}/answers", {
            "answers": {why["questionId"]: EDITED}, "attestations": {},
            "reuse": {why["questionId"]: "application"}})
        assert saved.status == 200, saved.json
        assert review(h, app_id)["changedSincePreparation"] is True
        again = h.client.post(f"/applications/{app_id}/resume", {})
        assert again.status == 200, again.json
        wait_for(h, app_id, STATES_DONE)
        body = review(h, app_id)
        assert body["stage"] == "prepared" and body["changedSincePreparation"] is False
        why = next(row for row in body["answers"] if row["question"] == WHY)
        assert (why["value"], why["provenance"]["kind"]) == (EDITED, "user")
        assert ats.submissions("standard")["accepted_count"] == 0

        approved = h.client.post(f"/applications/{app_id}/approve",
                                 {"packetId": body["preparedPacketId"]})
        assert approved.status == 200, approved.json
        approval = approved.json["approval"]
        assert approval["packetId"] == body["preparedPacketId"]
        assert approved.json["submit"]["allowed"] is True
        assert ats.submissions("standard")["accepted_count"] == 0

        submitted = h.client.post(f"/applications/{app_id}/submit",
                                  {"packetId": approval["packetId"], "confirm": True})
        assert submitted.status == 200, submitted.json
        view = wait_for(h, app_id, STATES_DONE)
        assert view["state"] == "SUBMITTED", view["events"][-5:]
        server = ats.submissions("standard")
        assert server["accepted_count"] == 1
        [record] = server["submissions"]
        assert record["fields"]["why_brambleway"] == EDITED
        assert view["receipt"]["confirmationReference"] == record["confirmation_reference"]
        with ApplicationStore.open(home.state_db) as store:
            [attempt] = store.list_attempts(app_id)
            assert attempt.packet_id == approval["packetId"]
        assert h.client.get("/review").json["applications"] == []


def test_a_service_without_submission_never_submits(
    home: LocalPaths, ats: MockAts, fictional_site: FictionalSite
) -> None:
    with reviewing_service(home, fictional_site, allow_submission=False) as h:
        assert h.client.get("/healthz").json["submission"] == "disabled"
        app_id = prepared(h, ats)
        packet_id = review(h, app_id)["preparedPacketId"]
        assert h.client.post(f"/applications/{app_id}/approve",
                             {"packetId": packet_id}).status == 200
        body = review(h, app_id)
        assert body["submit"]["problems"] == [SUBMISSION_OFF]
        refused = h.client.post(f"/applications/{app_id}/submit",
                                {"packetId": packet_id, "confirm": True})
        assert refused.status == 403
        assert ats.submissions("standard")["accepted_count"] == 0
        with ApplicationStore.open(home.state_db) as store:
            assert store.list_attempts(app_id) == []
            assert store.is_preparation_only(app_id)
