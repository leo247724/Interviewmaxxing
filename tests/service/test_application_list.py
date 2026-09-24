"""WP3 ``GET /applications``: this candidate's applications, most recently updated first,
each with its preparation and the pipeline cards that point at it."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from interviewmaxxing_core import (
    AnswerSource,
    ApplicationState,
    ApplicationStore,
    ControlType,
    LocalPaths,
    SemanticType,
    TextValue,
)
from interviewmaxxing_pipeline import PipelineUpdate
from interviewmaxxing_service import ServiceInteraction

from .board_support import KINDS, PREPARED_KINDS, count_statements, seed_board
from .conftest import SITE_URL, FakeCandidates, FictionalSite, Harness, serve
from .preparation_support import RunContext, Scenario, answer, make_field, make_form

SUMMARY_KEYS = {
    "id", "state", "applicationUrl", "job", "requestedAt", "updatedAt", "preparation",
    "pipelineEntryIds",
}
SHARED_KEYS = SUMMARY_KEYS - {"pipelineEntryIds"}
"""Summary fields that mirror ``GET /applications/{id}``."""
URL_B = "http://127.0.0.1:9/fictional-co/5001/apply"
URL_C = "http://127.0.0.1:9/fictional-co/5002/apply"
URL_D = "http://127.0.0.1:9/fictional-co/5003/apply"
URL_OTHER_CANDIDATE = "http://127.0.0.1:9/fictional-co/5004/apply"
FORM = make_form(SITE_URL, 0, [
    make_field("fld_ls_email", "Email", ControlType.TEXT, SemanticType.EMAIL, required=True),
], final=True)


class Clock:
    """A settable clock for seeding the store at chosen times."""

    def __init__(self, start: datetime) -> None:
        self.start = start
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def at(self, minutes: int) -> None:
        self.now = self.start + timedelta(minutes=minutes)


def prepares(ctx: RunContext) -> None:
    ctx.bind_identity(title="Senior Lifecycle Marketer", company="Northwind Cartography")
    packet = ctx.fill(FORM, [answer(FORM, "fld_ls_email", TextValue(text="avery@example.test"),
                                    AnswerSource.PROFILE_IDENTITY)])
    ctx.prepare(FORM, packet, captcha_pending=True)


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


def listed(h: Harness) -> list[dict[str, Any]]:
    got = h.client.get("/applications")
    assert got.status == 200, got.json
    assert set(got.json) == {"applications"}
    rows: list[dict[str, Any]] = got.json["applications"]
    return rows


def view(h: Harness, app_id: str) -> dict[str, Any]:
    got = h.client.get(f"/applications/{app_id}")
    assert got.status == 200, got.json
    body: dict[str, Any] = got.json
    return body


def card(h: Harness, url: str | None) -> str:
    created = h.client.post("/pipeline/entries", {
        "lane": "saved", "fields": {"company": "Fictional Co", "role": "Lifecycle Marketer"},
        "applicationUrl": url,
    })
    assert created.status == 201, created.json
    return str(created.json["id"])


def seed(
    store: ApplicationStore, h: Harness, app_id: str, run: Callable[[RunContext], object]
) -> None:
    claim = store.claim(app_id, "fictional-seed")
    try:
        run(RunContext(store=store, claim=claim, paths=h.paths,
                       interaction=ServiceInteraction(allow_browser_action=False), index=1))
    finally:
        store.release(claim)


def test_list_is_most_recently_updated_first_and_this_candidates_only(
    served: Harness, scenario: Scenario
) -> None:
    clock = Clock(datetime.now(UTC) - timedelta(hours=3))
    cid = served.paths.candidate_id
    with ApplicationStore.open(served.paths.state_db, clock=clock) as store:
        clock.at(0)
        b = store.record_request(cid, URL_B).application.id
        clock.at(1)
        c = store.record_request(cid, URL_C).application.id
        clock.at(2)
        d = store.record_request(cid, URL_D).application.id
        clock.at(3)
        other = store.record_request("other-candidate", URL_OTHER_CANDIDATE).application.id
        clock.at(10)
        seed(store, served, b, lambda ctx: ctx.sign_in())
        clock.at(20)
        seed(store, served, c, lambda ctx: ctx.fail("Stopped by a fictional browser error."))
        clock.at(30)
        seed(store, served, other, lambda ctx: ctx.to(ApplicationState.INSPECTING))
    scenario.then(prepares)
    started = served.start()
    assert started.status == 201, started.json
    a = str(started.json["id"])
    scenario.settle(served)

    rows = listed(served)
    assert [row["id"] for row in rows] == [a, c, b, d]  # updated order, not creation order
    assert other not in {row["id"] for row in rows}
    updated = [row["updatedAt"] for row in rows]
    assert updated == sorted(updated, reverse=True)
    for row in rows:
        assert set(row) == SUMMARY_KEYS
        assert set(row["job"]) == {"title", "company", "ats"}
        assert row["pipelineEntryIds"] == []
        detail = view(served, row["id"])
        assert {k: row[k] for k in SHARED_KEYS} == {k: detail[k] for k in SHARED_KEYS}
    first = rows[0]
    assert first["state"] == "NEEDS_INPUT"
    assert first["job"] == {"title": "Senior Lifecycle Marketer",
                            "company": "Northwind Cartography", "ats": "Greenhouse"}
    assert first["preparation"] is not None
    assert first["preparation"]["ready"] is True
    assert first["preparation"]["captchaPending"] is True
    assert [row["state"] for row in rows[1:]] == ["FAILED_RETRYABLE", "NEEDS_INPUT", "REQUESTED"]
    assert all(row["preparation"] is None for row in rows[1:])


def test_list_names_linked_cards_and_unlinked_cards_for_the_same_application(
    served: Harness, scenario: Scenario
) -> None:
    linked = card(served, SITE_URL)
    scenario.then(prepares)
    started = served.client.post("/applications", {
        "applicationUrl": SITE_URL, "profile": served.profile(),
        "resumeId": served.setup_candidate(), "pipelineEntryId": linked,
    })
    assert started.status == 201, started.json
    a = str(started.json["id"])
    scenario.settle(served)

    tracked = card(served, SITE_URL + "&utm_source=newsletter")  # same application
    unrelated = card(served, "http://127.0.0.1:9/fictional-co/7777/apply")
    no_url = card(served, None)
    with served.store() as store:
        b = store.record_request(served.paths.candidate_id, URL_B).application.id
    # A card whose URL resolves to A but which is linked to B belongs to B only.
    elsewhere = card(served, SITE_URL)
    pipeline_api = served.app.pipeline
    with pipeline_api.store() as pipeline:
        item = pipeline.get_item(pipeline_api.candidate_id, elsewhere)
        pipeline.update_item(pipeline_api.candidate_id, elsewhere,
                             PipelineUpdate(application_id=b), expected_revision=item.revision)

    rows = {row["id"]: row for row in listed(served)}
    assert set(rows) == {a, b}
    assert sorted(rows[a]["pipelineEntryIds"]) == sorted([linked, tracked])
    assert rows[b]["pipelineEntryIds"] == [elsewhere]
    listed_ids = [i for row in rows.values() for i in row["pipelineEntryIds"]]
    assert len(listed_ids) == len(set(listed_ids))
    assert unrelated not in listed_ids and no_url not in listed_ids
    assert rows[a]["preparation"] == view(served, a)["preparation"]
    assert rows[a]["preparation"] is not None


def test_list_is_empty_without_applications(harness: Harness) -> None:
    assert listed(harness) == []


def test_list_refuses_a_query_string(harness: Harness) -> None:
    refused = harness.client.get("/applications?state=NEEDS_INPUT")
    assert refused.status == 400
    assert refused.json["error"]["code"] == "invalid"


def test_posting_still_starts_an_application_which_is_then_listed(harness: Harness) -> None:
    started = harness.start()
    assert started.status == 201
    assert {"id", "state", "needs", "events", "preparation", "review"} <= set(started.json)
    harness.wait_idle()
    [row] = listed(harness)
    assert row["id"] == started.json["id"]
    assert row["state"] == "NEEDS_INPUT"  # the scripted site asks questions
    assert row["preparation"] is None
    assert row["pipelineEntryIds"] == []


# --- one read per list, however large the board (WP11 M6) ---------------------------------------


def board_statements(h: Harness) -> tuple[list[dict[str, Any]], list[str]]:
    with count_statements() as statements:
        rows = h.service.list_applications().dump()["applications"]
    return rows, statements


def test_list_matches_each_detail_view_on_a_board_of_every_shape(harness: Harness) -> None:
    board = seed_board(harness.paths, applications=2 * len(KINDS))
    rows = listed(harness)
    assert {row["id"] for row in rows} == set(board.applications)
    updated = [row["updatedAt"] for row in rows]
    assert updated == sorted(updated, reverse=True)
    for row in rows:
        detail = view(harness, row["id"])
        assert {k: row[k] for k in SHARED_KEYS} == {k: detail[k] for k in SHARED_KEYS}, row["id"]
        assert (row["preparation"] is not None) == (board.applications[row["id"]] in PREPARED_KINDS)
    pointed = {card: app for card, app in board.cards.items() if app is not None}
    listed_cards = {card: row["id"] for row in rows for card in row["pipelineEntryIds"]}
    assert listed_cards == pointed
    # A prepared stop's review page address keeps no draft token (WP11 L8).
    prepared = [row["preparation"] for row in rows if row["preparation"] is not None]
    assert prepared and all("?" not in p["formUrl"] and "token" not in p["formUrl"] for p in prepared)


def test_list_reads_the_store_with_a_fixed_number_of_statements(harness: Harness) -> None:
    seed_board(harness.paths, applications=len(KINDS), cards=len(KINDS))
    small, few = board_statements(harness)
    seed_board(harness.paths, applications=3 * len(KINDS), cards=5 * len(KINDS),
               start=len(KINDS))
    large, many = board_statements(harness)
    assert len(small) == len(KINDS) and len(large) == 4 * len(KINDS)
    assert sum(len(row["pipelineEntryIds"]) for row in large) > sum(
        len(row["pipelineEntryIds"]) for row in small)
    # The same statements for four times the applications and six times the cards: no
    # per-application history read, no per-card alias lookup.
    assert len(many) == len(few), [s for s in many if s not in few][:5]
    assert not any("FROM events WHERE application_id = ?" in s for s in many)
    reads = [s for s in many if s.lstrip().upper().startswith(("SELECT", "WITH"))]
    assert len(reads) <= 30, len(reads)


def test_list_of_a_large_board_stays_fast(harness: Harness) -> None:
    seed_board(harness.paths, applications=200)
    harness.service.list_applications()  # warm the file cache
    started = time.perf_counter()
    rows = harness.service.list_applications().applications
    elapsed = time.perf_counter() - started
    assert len(rows) == 200
    assert elapsed < 0.5, f"listing 200 applications took {elapsed:.3f}s"
