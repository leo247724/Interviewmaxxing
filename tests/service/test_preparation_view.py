"""WP3 prepared applications: the ``preparation`` view of a prepared final-review stop,
every other stop that is not a preparation, and the presentation version."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_core import (
    AnswerSource,
    ApplicationEvent,
    ApplicationState,
    ControlType,
    LocalPaths,
    MissingInput,
    PacketAnswer,
    SemanticType,
    TextValue,
)

from .conftest import SITE_URL, FakeCandidates, FictionalSite, Harness, serve
from .preparation_support import (
    RunBody,
    RunContext,
    Scenario,
    answer,
    iso,
    make_field,
    make_form,
    question,
)
from .test_http_flow import VIEW_KEYS, answer_all, poll

REVIEW_URL = "http://127.0.0.1:9/fictional-co/4012/apply/review?src=desk"
REVIEW_ADDRESS = "http://127.0.0.1:9/fictional-co/4012/apply/review"
"""``formUrl`` of ``REVIEW_URL``: scheme, host and path only (WP11 L8)."""
CONTACT = make_form(SITE_URL, 0, [
    make_field("fld_pv_first", "First name", ControlType.TEXT, SemanticType.FIRST_NAME,
               required=True),
    make_field("fld_pv_email", "Email", ControlType.TEXT, SemanticType.EMAIL, required=True),
], final=False)
REVIEW = make_form(REVIEW_URL, 1, [
    make_field("fld_pv_start", "When can you start?", ControlType.TEXT, SemanticType.START_DATE,
               required=True),
    make_field("fld_pv_note", "Anything else we should know?", ControlType.TEXTAREA,
               SemanticType.CUSTOM_LONG_TEXT),
], final=True)
REVIEW_WITH_PORTFOLIO = make_form(REVIEW_URL, 1, [
    *REVIEW.fields,
    make_field("fld_pv_portfolio", "Portfolio link", ControlType.TEXT, SemanticType.CUSTOM_TEXT,
               required=True),
], final=True)

READY_TEXT = ("Ready for final review. Nothing was submitted.", "attention")
PREPARED_STOP_TEXT = ("Paused at the final review step for you to check.", "info")
PREPARATION_KEYS = {
    "ready", "formStep", "formUrl", "captchaPending", "preparedAt", "submitted", "evidence",
}
FAILURE = "Stopped by a browser error (fictional timeout). Nothing was submitted; resume to retry."


# --- fixtures and helpers ------------------------------------------------------------------


@pytest.fixture
def scenario(isolated_imx_home: LocalPaths) -> Scenario:
    return Scenario(isolated_imx_home)


@pytest.fixture
def served(
    isolated_imx_home: LocalPaths, fictional_site: FictionalSite, scenario: Scenario,
    tmp_path: Path,
) -> Iterator[Harness]:
    with serve(isolated_imx_home, fictional_site, FakeCandidates(tmp_path / "private-profile"),
               executor_factory=scenario.factory) as h:
        yield h


def start(h: Harness, scenario: Scenario) -> str:
    started = h.start()
    assert started.status == 201, started.json
    scenario.settle(h)
    return str(started.json["id"])


def resume(h: Harness, scenario: Scenario, app_id: str) -> None:
    resumed = h.client.post(f"/applications/{app_id}/resume", {})
    assert resumed.status == 200, resumed.json
    scenario.settle(h)


def view(h: Harness, app_id: str) -> dict[str, Any]:
    got = h.client.get(f"/applications/{app_id}")
    assert got.status == 200, got.json
    body: dict[str, Any] = got.json
    return body


def stored_events(h: Harness, app_id: str, name: str) -> list[ApplicationEvent]:
    with h.store() as store:
        return [e for e in store.list_events(app_id) if e.event == name]


def contact_answers() -> list[PacketAnswer]:
    return [
        answer(CONTACT, "fld_pv_first", TextValue(text="Avery"), AnswerSource.PROFILE_IDENTITY),
        answer(CONTACT, "fld_pv_email", TextValue(text="avery@example.test"),
               AnswerSource.PROFILE_IDENTITY),
    ]


def start_answer(form: Any = REVIEW) -> PacketAnswer:
    return answer(form, "fld_pv_start", TextValue(text="2026-11-02"), AnswerSource.CANDIDATE_FACT,
                  ["fact_pv_start"], confidence=0.92)


def prepared(
    *, captcha_pending: bool = False, remaining: tuple[MissingInput, ...] = ()
) -> RunBody:
    """A two-step run filled to its final review step and stopped there."""

    def run(ctx: RunContext) -> None:
        ctx.bind_identity(title="Senior Lifecycle Marketer", company="Northwind Cartography")
        ctx.fill(CONTACT, contact_answers())
        packet = ctx.fill(REVIEW, [start_answer()])
        ctx.prepare(REVIEW, packet, captcha_pending=captcha_pending, remaining=remaining)

    return run


def asks_for_start(ctx: RunContext) -> None:
    ctx.fill(CONTACT, contact_answers())
    ctx.ask(REVIEW, [], [question(REVIEW, "fld_pv_start")])


def needs_sign_in(ctx: RunContext) -> None:
    ctx.sign_in()


def fails(ctx: RunContext) -> None:
    ctx.fill(CONTACT, contact_answers())
    ctx.fail(FAILURE)


def text_of(event: dict[str, Any]) -> tuple[str, str]:
    return event["message"], event["tone"]


# --- a prepared stop ---------------------------------------------------------------------------


@pytest.mark.parametrize("captcha_pending", [True, False])
def test_prepared_stop_is_a_review_not_a_request_for_input(
    served: Harness, scenario: Scenario, captcha_pending: bool
) -> None:
    scenario.then(prepared(captcha_pending=captcha_pending))
    app_id = start(served, scenario)
    body = view(served, app_id)

    assert set(body) == VIEW_KEYS | {"preparation", "review"}
    assert body["state"] == "NEEDS_INPUT"
    assert body["needs"] is None  # not the old "Waiting for you" interaction
    assert body["receipt"] is None
    preparation = body["preparation"]
    assert set(preparation) == PREPARATION_KEYS
    [ready] = stored_events(served, app_id, "preparation.ready")
    assert {k: v for k, v in preparation.items() if k != "evidence"} == {
        "ready": True,
        "formStep": 1,
        "formUrl": REVIEW_ADDRESS,
        "captchaPending": captcha_pending,
        "preparedAt": iso(ready.timestamp),
        "submitted": False,
    }

    # The screenshot of the filled review page, served through the evidence route.
    [shot] = scenario.contexts[0].evidence
    [evidence] = preparation["evidence"]
    assert evidence["kind"] == "screenshot"
    assert evidence["href"] == f"/api/imx/applications/{app_id}/evidence/{shot.id}"
    assert shot.path is not None
    served_file = served.client.get(evidence["href"].removeprefix("/api/imx"))
    assert served_file.status == 200
    assert served_file.headers["content-type"] == "image/png"
    assert served_file.body == (served.paths.artifacts_dir / shot.path).read_bytes()

    # The docket: ready for review, and the stop is a pause for the user's check.
    events = body["events"]
    types = [e["type"] for e in events]
    at = types.index("preparation.ready")
    assert text_of(events[at]) == READY_TEXT
    assert events[at]["at"] == preparation["preparedAt"]
    stop = next(e for e in events[at:] if e["type"] == "application.needs_input")
    assert text_of(stop) == PREPARED_STOP_TEXT
    with served.store() as store:
        assert store.list_attempts(app_id) == []  # nothing was submitted


def test_preparation_evidence_comes_from_the_preparing_run_only(
    served: Harness, scenario: Scenario
) -> None:
    scenario.then(fails, prepared())
    app_id = start(served, scenario)
    failed = view(served, app_id)
    assert failed["state"] == "FAILED_RETRYABLE"
    assert failed["preparation"] is None

    resume(served, scenario, app_id)
    body = view(served, app_id)
    [error_shot] = scenario.contexts[0].evidence
    [review_shot] = scenario.contexts[1].evidence
    assert body["state"] == "NEEDS_INPUT"
    assert body["preparation"] is not None
    assert [e["href"] for e in body["preparation"]["evidence"]] == [
        f"/api/imx/applications/{app_id}/evidence/{review_shot.id}"
    ]
    assert error_shot.id not in str(body["preparation"])
    with served.store() as store:  # the earlier screenshot stays on record
        assert {e.id for e in store.list_evidence(app_id)} == {error_shot.id, review_shot.id}


def test_asking_again_for_a_prepared_application_keeps_it_prepared(
    served: Harness, scenario: Scenario
) -> None:
    scenario.then(prepared())
    app_id = start(served, scenario)
    before = view(served, app_id)["preparation"]
    assert before is not None
    again = served.start()  # the same link again: recorded as a repeated request, no run
    assert again.status == 200, again.json
    assert again.json["id"] == app_id
    assert again.json["preparation"] == before
    assert again.json["needs"] is None
    assert len(scenario.contexts) == 1


def test_resumed_preparation_is_prepared_again_only_when_it_stops_again(
    served: Harness, scenario: Scenario
) -> None:
    running, release = threading.Event(), threading.Event()

    def prepares_again(ctx: RunContext) -> None:
        ctx.to(ApplicationState.INSPECTING)
        running.set()
        assert release.wait(10), "the test never released the run"
        ctx.fill(CONTACT, contact_answers())
        packet = ctx.fill(REVIEW, [start_answer()])
        ctx.prepare(REVIEW, packet)

    scenario.then(prepared(), prepares_again)
    app_id = start(served, scenario)
    assert view(served, app_id)["preparation"] is not None

    resumed = served.client.post(f"/applications/{app_id}/resume", {})
    assert resumed.status == 200, resumed.json
    try:
        assert running.wait(5)
        during = view(served, app_id)
        assert during["state"] == "INSPECTING"
        assert during["preparation"] is None
        [row] = served.client.get("/applications").json["applications"]
        assert row["preparation"] is None
    finally:
        release.set()
    scenario.settle(served)

    after = view(served, app_id)["preparation"]
    _first, second = stored_events(served, app_id, "preparation.ready")
    [second_shot] = scenario.contexts[1].evidence
    assert after["preparedAt"] == iso(second.timestamp)
    # Only the new run's screenshot: the earlier preparation's evidence is not this one's.
    assert [e["href"] for e in after["evidence"]] == [
        f"/api/imx/applications/{app_id}/evidence/{second_shot.id}"
    ]


def test_prepared_stop_with_a_remaining_question_keeps_the_question(
    served: Harness, scenario: Scenario
) -> None:
    scenario.then(prepared(remaining=(question(REVIEW, "fld_pv_note"),)))
    app_id = start(served, scenario)
    body = view(served, app_id)
    assert body["preparation"] is not None
    assert body["preparation"]["ready"] is True
    need = body["needs"]
    assert need is not None
    assert need["kind"] == "questions"
    assert [q["label"] for q in need["questions"]] == ["Anything else we should know?"]


# --- stops that are not preparations ----------------------------------------------------------


@pytest.mark.parametrize("kind", ["questions", "sign_in", "failure"])
def test_other_stops_are_not_preparations(
    served: Harness, scenario: Scenario, kind: str
) -> None:
    runs: dict[str, RunBody] = {
        "questions": asks_for_start, "sign_in": needs_sign_in, "failure": fails,
    }
    scenario.then(runs[kind])
    app_id = start(served, scenario)
    body = view(served, app_id)
    assert body["preparation"] is None
    assert PREPARED_STOP_TEXT[0] not in [e["message"] for e in body["events"]]
    if kind == "questions":
        assert body["state"] == "NEEDS_INPUT"
        assert body["needs"]["kind"] == "questions"
        assert [q["label"] for q in body["needs"]["questions"]] == ["When can you start?"]
    elif kind == "sign_in":
        assert body["state"] == "NEEDS_INPUT"
        assert body["needs"]["kind"] == "interaction"
        assert body["needs"]["interaction"] == "SIGN_IN"
    else:
        assert body["state"] == "FAILED_RETRYABLE"
        assert body["needs"] is None
        assert body["failure"]["retryable"] is True


def test_submitted_application_has_no_preparation(harness: Harness) -> None:
    app_id = harness.start().json["id"]
    asked = poll(harness, app_id, {"NEEDS_INPUT"})
    assert asked["preparation"] is None
    answer_all(harness, app_id, asked)
    harness.client.post(f"/applications/{app_id}/resume", {})
    done = poll(harness, app_id, {"SUBMITTED"})
    assert done["receipt"] is not None
    assert done["preparation"] is None
    assert done["needs"] is None


def test_questions_after_an_earlier_preparation_end_it(
    served: Harness, scenario: Scenario
) -> None:
    def asks_for_portfolio(ctx: RunContext) -> None:
        ctx.fill(CONTACT, contact_answers())
        ctx.ask(REVIEW_WITH_PORTFOLIO, [start_answer(REVIEW_WITH_PORTFOLIO)],
                [question(REVIEW_WITH_PORTFOLIO, "fld_pv_portfolio")])

    scenario.then(prepared(), asks_for_portfolio)
    app_id = start(served, scenario)
    assert view(served, app_id)["preparation"] is not None

    resume(served, scenario, app_id)
    body = view(served, app_id)
    assert body["state"] == "NEEDS_INPUT"
    assert body["preparation"] is None  # the latest stop wins
    assert body["needs"]["kind"] == "questions"
    assert [q["label"] for q in body["needs"]["questions"]] == ["Portfolio link"]
    stops = [e for e in body["events"] if e["type"] == "application.needs_input"]
    assert len(stops) == 2
    assert text_of(stops[0]) == PREPARED_STOP_TEXT  # history keeps the earlier review stop
    assert stops[1]["message"] != PREPARED_STOP_TEXT[0]
    # Not prepared: the review is the latest packet only (the step that asked).
    assert [(r["page"], r["source"]) for r in body["review"]] == [(2, "fact")]


# --- health ------------------------------------------------------------------------------------


def test_health_reports_the_presentation_version(harness: Harness) -> None:
    health = harness.client.get("/healthz")
    assert health.status == 200
    assert health.json["presentationVersion"] == "2"
    assert health.json["contractVersion"] == "2"
