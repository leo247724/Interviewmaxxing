"""WP11 round 3: the Prepared queue (``GET /review``).

Applications at their final review step, and those held only by a step the person does
in the browser, newest stop first, with the employer, title, backend, prepared time,
provider cost and a one-line hold summary; from one read of the store. The boards are
fictional (``board_support``): the runner's store operations, nothing opened.
"""

from __future__ import annotations

import time
from typing import Any

from interviewmaxxing_core import (
    ApplicationStore,
    TextValue,
    UserInput,
)

from .board_support import KINDS, PREPARED_KINDS, _page, _Run, count_statements, seed_board
from .conftest import Harness


def queue(h: Harness) -> list[dict[str, Any]]:
    got = h.client.get("/review")
    assert got.status == 200, got.json
    items: list[dict[str, Any]] = got.json["applications"]
    return items


def review(h: Harness, app_id: str) -> dict[str, Any]:
    got = h.client.get(f"/applications/{app_id}/review")
    assert got.status == 200, got.json
    body: dict[str, Any] = got.json
    return body


def test_queue_is_empty_without_applications(harness: Harness) -> None:
    assert queue(harness) == []


def test_queue_lists_prepared_and_browser_held_applications_newest_first(
    harness: Harness,
) -> None:
    board = seed_board(harness.paths, applications=2 * len(KINDS))
    ids = list(board.applications)  # creation order: the seeding index
    prepared = {app for app, kind in board.applications.items() if kind in PREPARED_KINDS}
    signing_in = {ids[9]}  # the idle application at index 9 stopped for a sign-in
    items = queue(harness)
    assert {item["id"] for item in items} == prepared | signing_in
    stopped = [item["stoppedAt"] for item in items]
    assert stopped == sorted(stopped, reverse=True)
    for item in items:
        detail = review(harness, item["id"])
        application = detail["application"]
        assert item["stage"] == detail["stage"]
        assert item["approved"] is (detail["approval"] is not None) is False
        assert item["job"] == application["job"]
        assert item["applicationUrl"] == application["applicationUrl"]
        assert item["state"] == "NEEDS_INPUT"
        if item["stage"] == "prepared":
            preparation = application["preparation"]
            assert item["preparedAt"] == preparation["preparedAt"]
            assert item["captchaPending"] is preparation["captchaPending"]
            summary = "Ready to review and approve" + (
                " · CAPTCHA to solve when submitting" if item["captchaPending"] else "")
            assert item["hold"] == {"kind": "ready", "summary": summary}
        else:
            assert item["preparedAt"] is None and item["captchaPending"] is False
            assert item["hold"] == {"kind": "sign_in", "summary": "Sign-in needed in the browser"}
        assert item["providerCost"] is None
    sample = next(item for item in items if item["stage"] == "prepared")
    number = int(sample["applicationUrl"].split("/")[-2]) - 7000
    assert sample["job"] == {"title": f"Fictional Marketer {number}",
                             "company": f"Fictional Board Co {number}", "ats": "Greenhouse"}
    assert any(item["captchaPending"] for item in items)


def _claimed(store: ApplicationStore, app_id: str) -> Any:
    return store.claim(app_id, "fictional-queue-test")


def _approve(store: ApplicationStore, app_id: str) -> None:
    claim = _claimed(store, app_id)
    try:
        packet_id = store.prepared_packet(app_id)
        assert packet_id is not None
        store.approve_submission(claim, packet_id=packet_id, approver="fictional")
    finally:
        store.release(claim)


def test_queue_approval_follows_the_store(harness: Harness) -> None:
    board = seed_board(harness.paths, applications=2 * len(KINDS))
    ids = [app for app, kind in board.applications.items() if kind == "prepared"]
    approved, edited, invalidated, prepared_again = ids[:4]
    index_of = {app: n for n, app in enumerate(board.applications)}
    with ApplicationStore.open(harness.paths.state_db) as store:
        for app_id in (approved, edited, invalidated, prepared_again):
            _approve(store, app_id)
        # An answer saved after the preparation: the approval lapses until it is prepared again.
        claim = _claimed(store, edited)
        form = _page(index_of[edited], 2)
        store.save_user_inputs(claim, [UserInput.for_field(
            form, form.fields[0].id, TextValue(text="A fictional change"))])
        store.release(claim)
        claim = _claimed(store, invalidated)
        store.invalidate_approval(claim, reason="fictional form change")
        store.release(claim)
        claim = _claimed(store, prepared_again)
        run = _Run(store, claim, index_of[prepared_again])
        run.pages()
        run.prepare()
        store.release(claim)
        expected = {app: store.submission_approval(app) is not None for app in board.applications}
    items = {item["id"]: item for item in queue(harness)}
    assert expected[approved] and not expected[edited] and not expected[invalidated]
    assert not expected[prepared_again]
    for app_id, item in items.items():
        assert item["approved"] is expected[app_id], app_id
    assert items[approved]["hold"]["kind"] == "approved"
    assert items[edited]["hold"] == {
        "kind": "edited", "summary": "Answers changed since this preparation: prepare it again"}
    assert items[invalidated]["hold"]["kind"] == "ready"
    assert items[prepared_again]["hold"]["kind"] == "ready"


def test_queue_sums_each_applications_provider_cost(harness: Harness) -> None:
    board = seed_board(harness.paths, applications=len(KINDS))
    app_id = next(app for app, kind in board.applications.items() if kind == "prepared")
    with ApplicationStore.open(harness.paths.state_db) as store:
        claim = _claimed(store, app_id)
        for usage in ({"calls": 12, "known_cost_usd": 0.31, "unknown_cost_calls": 0},
                      {"calls": 5, "known_cost_usd": 0.02, "unknown_cost_calls": 2}):
            store.append_event(claim, "provider.budget", usage)
        store.release(claim)
    items = {item["id"]: item for item in queue(harness)}
    assert items[app_id]["providerCost"] == {"knownUsd": 0.33, "calls": 17, "unknownCostCalls": 2}
    assert review(harness, app_id)["providerCost"] == items[app_id]["providerCost"]


def test_queue_lists_this_candidates_applications_only(harness: Harness) -> None:
    board = seed_board(harness.paths, applications=len(KINDS))
    with ApplicationStore.open(harness.paths.state_db) as store:
        other = store.record_request(
            "fictional-other-candidate", "http://127.0.0.1:9/fictional-other/1/apply").application
        claim = _claimed(store, other.id)
        run = _Run(store, claim, 900)
        run.pages()
        run.prepare()
        store.release(claim)
    listed = {item["id"] for item in queue(harness)}
    assert other.id not in listed and listed <= set(board.applications)


def queue_statements(h: Harness) -> tuple[list[dict[str, Any]], list[str]]:
    with count_statements() as statements:
        items = h.service.review_queue().dump()["applications"]
    return items, statements


def test_queue_reads_the_store_with_a_fixed_number_of_statements(harness: Harness) -> None:
    seed_board(harness.paths, applications=len(KINDS))
    small, few = queue_statements(harness)
    seed_board(harness.paths, applications=3 * len(KINDS), start=len(KINDS))
    large, many = queue_statements(harness)
    assert 0 < len(small) < len(large)
    assert len(many) == len(few), [s for s in many if s not in few][:5]
    assert not any("FROM events WHERE application_id = ?" in s for s in many)


def test_queue_of_a_large_store_stays_fast(harness: Harness) -> None:
    seed_board(harness.paths, applications=200)
    harness.service.review_queue()  # warm the file cache
    started = time.perf_counter()
    items = harness.service.review_queue().applications
    elapsed = time.perf_counter() - started
    assert len(items) >= 100
    assert elapsed < 0.5, f"the queue of 200 applications took {elapsed:.3f}s"
