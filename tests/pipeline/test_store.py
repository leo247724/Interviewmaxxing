"""Pipeline repository: persistence, moves/history, edits, isolation, conflicts."""

from __future__ import annotations

import sqlite3
import stat
from datetime import date

import pytest
from pydantic import ValidationError

from interviewmaxxing_pipeline import (
    DEFAULT_BOARD_LANES,
    BoardLane,
    BoardLanes,
    ItemNotFound,
    LaneError,
    NewPipelineItem,
    PipelineStore,
    PipelineUpdate,
    RevisionConflict,
    TrackingFields,
    default_pipeline_db,
    suggest_lane,
)

CAND = "cand_fictional"
OTHER = "cand_other"


def _new(**tracking):
    values = {"company": "Fictional Widgets Co", "role": "Marketing Manager", **tracking}
    return NewPipelineItem(tracking=TrackingFields.model_validate(values))


def test_create_get_list_and_reload(pipeline_db, clock):
    with PipelineStore.open(pipeline_db, clock=clock) as store:
        item = store.create_item(CAND, _new(stage="First interview", status="Interviewing",
                                            nextAction="Prepare examples"))
        assert item.lane == "interviewing" and item.revision == 1
        assert item.needs_application_url and item.application_id is None
    with PipelineStore.open(pipeline_db, clock=clock) as reopened:
        assert reopened.get_item(CAND, item.id) == item
        assert reopened.list_items(CAND) == [item]
        assert reopened.list_items(CAND, lane="interviewing") == [item]
        assert reopened.list_items(CAND, lane="offer") == []


def test_database_is_private_and_separate_from_the_application_store(
    pipeline_db, clock, isolated_imx_home
):
    with PipelineStore.open(pipeline_db, clock=clock) as store:
        store.create_item(CAND, _new())
    assert stat.S_IMODE(pipeline_db.stat().st_mode) == 0o600
    assert stat.S_IMODE(pipeline_db.parent.stat().st_mode) == 0o700
    tables = {r[0] for r in sqlite3.connect(pipeline_db).execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"meta", "items", "history", "source_versions", "boards", "imports",
                      "sqlite_sequence"}
    assert default_pipeline_db(isolated_imx_home) == \
        isolated_imx_home.state_db.parent / "pipeline.sqlite3"
    assert default_pipeline_db(isolated_imx_home) != isolated_imx_home.state_db


def test_move_records_history_and_never_touches_applications(pipeline, clock):
    item = pipeline.create_item(CAND, _new(stage="Saved", status="Not yet applied"))
    assert item.lane == "saved"
    clock.advance(minutes=5)
    moved = pipeline.move_item(CAND, item.id, "applied", expected_revision=1,
                               note="Applied on the careers site")
    clock.advance(minutes=5)
    offered = pipeline.move_item(CAND, item.id, "offer", expected_revision=2)
    assert (moved.lane, moved.revision, offered.lane, offered.revision) == (
        "applied", 2, "offer", 3)
    # A lane is the user's record only: no application link or URL appears.
    assert offered.application_id is None and offered.application_url is None
    history = pipeline.history(CAND, item.id)
    assert [(h.kind, h.actor, h.from_lane, h.to_lane) for h in history] == [
        ("created", "user", None, "saved"),
        ("moved", "user", "saved", "applied"),
        ("moved", "user", "applied", "offer"),
    ]
    assert history[1].note == "Applied on the careers site"
    assert [h.sequence for h in history] == sorted(h.sequence for h in history)
    same = pipeline.move_item(CAND, item.id, "offer", expected_revision=3)
    assert same.revision == 3 and len(pipeline.history(CAND, item.id)) == 3
    with pytest.raises(LaneError, match="unknown lane"):
        pipeline.move_item(CAND, item.id, "submitted", expected_revision=3)


def test_partial_updates_edit_notes_next_action_and_links(pipeline, clock):
    item = pipeline.create_item(CAND, _new(priority="High", fitScore=7))
    clock.advance(minutes=1)
    update = PipelineUpdate.model_validate({
        "tracking": {"nextAction": "Email the recruiter", "fitScore": 0},
        "notes": "Own notes", "next_action_due": "2026-10-01",
        "application_url": "https://jobs.example.test/apply/123",
    })
    edited = pipeline.update_item(CAND, item.id, update, expected_revision=1)
    assert edited.revision == 2 and edited.updated_at > item.updated_at
    assert edited.tracking.next_action == "Email the recruiter"
    assert edited.tracking.fit_score == 0 and edited.tracking.priority == "High"
    assert (edited.notes, edited.next_action_due) == ("Own notes", date(2026, 10, 1))
    assert not edited.needs_application_url

    cleared = pipeline.update_item(
        CAND, item.id, PipelineUpdate.model_validate({"notes": None, "tracking": {"priority": None}}),
        expected_revision=2)
    assert cleared.notes is None and cleared.tracking.priority is None
    assert cleared.tracking.next_action == "Email the recruiter"
    unchanged = pipeline.update_item(CAND, item.id, PipelineUpdate(), expected_revision=3)
    assert unchanged.revision == 3

    stage = pipeline.update_item(
        CAND, item.id, PipelineUpdate.model_validate(
            {"tracking": {"stage": "Panel interview (3 people, 45 min)", "status": "Waiting"}}),
        expected_revision=3)
    last = pipeline.history(CAND, item.id)[-1]
    assert (last.kind, last.to_stage, last.to_status) == (
        "stage_edited", "Panel interview (3 people, 45 min)", "Waiting")
    assert stage.lane == item.lane  # editing wording never moves the card


@pytest.mark.parametrize(
    "update",
    [
        {"application_url": "jobs.example.test/apply"},
        {"application_url": "ftp://example.test/job"},
        {"tracking": {"compensationLow": -1}},
        {"lane": "offer"},
    ],
)
def test_invalid_updates_are_rejected(update):
    with pytest.raises(ValidationError):
        PipelineUpdate.model_validate(update)


def test_update_that_breaks_compensation_bounds_is_rejected(pipeline):
    item = pipeline.create_item(CAND, _new(compensationLow=100000, compensationHigh=120000))
    with pytest.raises(ValidationError, match="above"):
        pipeline.update_item(CAND, item.id, PipelineUpdate.model_validate(
            {"tracking": {"compensationLow": 130000}}), expected_revision=1)
    assert pipeline.get_item(CAND, item.id).revision == 1


def test_concurrent_updates_conflict_instead_of_overwriting(pipeline_db, clock):
    first = PipelineStore.open(pipeline_db, clock=clock)
    second = PipelineStore.open(pipeline_db, clock=clock)
    try:
        item = first.create_item(CAND, _new())
        seen_by_second = second.get_item(CAND, item.id)
        first.update_item(CAND, item.id, PipelineUpdate(notes="first"), expected_revision=1)
        with pytest.raises(RevisionConflict) as conflict:
            second.update_item(CAND, item.id, PipelineUpdate(notes="second"),
                               expected_revision=seen_by_second.revision)
        assert conflict.value.current.notes == "first" and conflict.value.current.revision == 2
        with pytest.raises(RevisionConflict):
            second.move_item(CAND, item.id, "closed", expected_revision=1)
        assert first.get_item(CAND, item.id).notes == "first"
    finally:
        first.close()
        second.close()


def test_candidates_are_isolated(pipeline):
    mine = pipeline.create_item(CAND, _new())
    theirs = pipeline.create_item(OTHER, _new(role="Marketing Director"))
    assert [i.id for i in pipeline.list_items(CAND)] == [mine.id]
    assert [i.id for i in pipeline.list_items(OTHER)] == [theirs.id]
    with pytest.raises(ItemNotFound):
        pipeline.get_item(CAND, theirs.id)
    with pytest.raises(ItemNotFound):
        pipeline.update_item(CAND, theirs.id, PipelineUpdate(notes="x"), expected_revision=1)
    with pytest.raises(ItemNotFound):
        pipeline.move_item(CAND, theirs.id, "closed", expected_revision=1)
    with pytest.raises(ItemNotFound):
        pipeline.history(CAND, theirs.id)
    pipeline.set_lanes(OTHER, BoardLanes(lanes=[
        BoardLane(id="saved", label="Saved"), BoardLane(id="later", label="Later")]))
    assert pipeline.lanes(CAND) == DEFAULT_BOARD_LANES
    for bad in ("", "../x", "a b", "-lead"):
        with pytest.raises(ValueError, match="invalid candidate id"):
            pipeline.list_items(bad)


def test_lanes_are_configurable_and_protect_cards(pipeline):
    item = pipeline.create_item(CAND, _new(status="Interviewing"))
    custom = BoardLanes(lanes=[
        BoardLane(id="interviewing", label="In process"), BoardLane(id="archive", label="Archive")])
    assert pipeline.set_lanes(CAND, custom) == custom
    assert [lane.id for lane in pipeline.board(CAND).lanes] == ["interviewing", "archive"]
    with pytest.raises(LaneError, match="still hold cards"):
        pipeline.set_lanes(CAND, BoardLanes(lanes=[BoardLane(id="archive", label="Archive")]))
    pipeline.move_item(CAND, item.id, "archive", expected_revision=1)
    pipeline.set_lanes(CAND, BoardLanes(lanes=[BoardLane(id="archive", label="Archive")]))
    with pytest.raises(ValidationError, match="distinct"):
        BoardLanes(lanes=[BoardLane(id="a", label="Same"), BoardLane(id="b", label="same")])
    with pytest.raises(ValidationError):
        BoardLane(id="Has Space", label="x")


def test_board_view_groups_cards_in_lane_order(pipeline):
    a = pipeline.create_item(CAND, _new(status="Offer received", compensationLow=1,
                                        compensationHigh=2, nextAction="Decide"))
    b = pipeline.create_item(CAND, _new(role="Director", status="Scheduling a call"))
    board = pipeline.board(CAND)
    lanes = {lane.id: [c.id for c in lane.cards] for lane in board.lanes}
    assert lanes["offer"] == [a.id] and lanes["scheduling"] == [b.id]
    assert [lane.id for lane in board.lanes] == DEFAULT_BOARD_LANES.ids()
    card = board.lanes[DEFAULT_BOARD_LANES.ids().index("offer")].cards[0]
    assert (card.next_action, card.compensation_low, card.needs_application_url,
            card.imported) == ("Decide", 1, True, False)


def test_history_is_append_only(pipeline, pipeline_db):
    item = pipeline.create_item(CAND, _new())
    raw = sqlite3.connect(pipeline_db)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("DELETE FROM history")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("UPDATE history SET body = '{}'")
    raw.close()
    assert len(pipeline.history(CAND, item.id)) == 1


@pytest.mark.parametrize(
    ("stage", "status", "lane"),
    [
        (None, "Offer declined", "closed"),
        (None, "Awaiting decision after final interview", "decision"),
        (None, "Take-home assessment due", "assessment"),
        (None, "Scheduling recruiter screen", "scheduling"),
        (None, "Interview scheduled", "interviewing"),
        (None, "Awaiting feedback", "follow-up"),
        (None, "Not yet applied", "saved"),
        (None, "Applied via careers site", "applied"),
        ("Offer", None, "offer"),
        ("Something unusual", "Pending", "saved"),
    ],
)
def test_conservative_lane_suggestions(stage, status, lane):
    suggestion = suggest_lane(stage, status)
    assert suggestion.lane == lane
    if stage == "Something unusual":
        assert suggestion.rule is None and "review" in suggestion.reason
