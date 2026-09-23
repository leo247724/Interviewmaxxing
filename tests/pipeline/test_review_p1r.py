"""P1R regressions: source scoping, immutable original snapshots, blank CSV headers,
negated lane wording, oversized numbers, the D0 entry adapter and schema migration.
Fictional data only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from interviewmaxxing_core import PipelineEntry
from interviewmaxxing_pipeline import (
    FIELD_KEYS,
    LEGACY_IMPORT_ID,
    SCHEMA_VERSION,
    EntryUnavailable,
    ImportFileError,
    ImportRejected,
    NewPipelineItem,
    PipelineStore,
    PipelineUpdate,
    TrackingFields,
    imported_item_id,
    legacy_source_id,
    load_import,
    parse_import,
    suggest_lane,
    to_pipeline_entry,
)

CAND = "cand_fictional"


def _record(**values):
    base = dict.fromkeys(FIELD_KEYS)
    base.update(company="Alpha Fictional", role="Marketing Manager")
    base.update(values)
    return base


def _export(records, *, path="/fictional/a/Pipeline.numbers", **source):
    doc = {"schemaVersion": 1,
           "source": {"path": path, "sheet": "Pipeline", "table": "Table 1", **source},
           "records": records}
    return json.dumps(doc).encode()


# --- (1) imports are scoped to a stable logical source ------------------------------------


def test_same_explicit_key_in_two_files_never_overwrites(pipeline):
    alpha = parse_import(_export([{**_record(), "sourceRow": 2, "importKey": "row-2"}],
                                 path="/fictional/a/Pipeline.numbers"), name="export.json")
    beta = parse_import(_export([{**_record(company="Beta Fictional"), "sourceRow": 2,
                                  "importKey": "row-2"}],
                                path="/fictional/b/Pipeline.numbers"), name="export.json")
    # Same file name, same sheet/table, same key: still two different sources.
    assert alpha.source.name == beta.source.name == "Pipeline.numbers"
    assert alpha.source.source_id != beta.source.source_id
    pipeline.apply_import(CAND, alpha)
    receipt = pipeline.apply_import(CAND, beta)
    assert receipt.counts() == {"create": 1, "update": 0, "unchanged": 0}
    assert sorted(i.tracking.company for i in pipeline.list_items(CAND)) == [
        "Alpha Fictional", "Beta Fictional"]


def test_same_company_and_role_postings_from_two_files_stay_separate(pipeline, tmp_path):
    austin, nyc = tmp_path / "austin" / "jobs.json", tmp_path / "nyc" / "jobs.json"
    for file, where in ((austin, "Austin, TX"), (nyc, "New York, NY")):
        file.parent.mkdir()
        doc = {"schemaVersion": 1, "records": [_record(locationCommute=where)]}
        file.write_text(json.dumps(doc))  # no declared source: identity from file path
    first, second = load_import(austin), load_import(nyc)
    assert first.rows[0].import_key == second.rows[0].import_key  # same derived key
    assert first.source.source_id_origin == second.source.source_id_origin == "file-path"
    pipeline.apply_import(CAND, first)
    assert pipeline.apply_import(CAND, second).counts()["create"] == 1
    assert sorted(i.tracking.location_commute for i in pipeline.list_items(CAND)) == [
        "Austin, TX", "New York, NY"]


def test_editing_the_same_workbook_updates_rather_than_duplicates(pipeline):
    v1 = parse_import(_export([{**_record(), "importKey": "k1"}], sha256="a" * 64), name="e.json")
    v2 = parse_import(_export([{**_record(nextAction="Call back"), "importKey": "k1"}],
                              sha256="b" * 64), name="e.json")
    assert v1.source.document_sha256 != v2.source.document_sha256
    assert v1.source.source_id == v2.source.source_id  # a content change keeps the source
    pipeline.apply_import(CAND, v1)
    assert pipeline.apply_import(CAND, v2).counts() == {"create": 0, "update": 1, "unchanged": 0}
    assert pipeline.apply_import(CAND, v2).counts()["unchanged"] == 1
    assert len(pipeline.list_items(CAND)) == 1


def test_changed_identity_under_the_same_key_and_source_is_rejected(pipeline):
    pipeline.apply_import(CAND, parse_import(
        _export([{**_record(), "sourceRow": 2, "importKey": "row-2"}]), name="e.json"))
    changed = parse_import(_export([{**_record(company="Beta Fictional"), "sourceRow": 2,
                                     "importKey": "row-2"}]), name="e.json")
    preview = pipeline.preview_import(CAND, changed)
    assert [(i.row, "different Company/Role" in i.message) for i in preview.issues] == [(2, True)]
    with pytest.raises(ImportRejected):
        pipeline.apply_import(CAND, changed)
    [item] = pipeline.list_items(CAND)
    assert item.tracking.company == "Alpha Fictional" and item.revision == 1


def test_explicit_and_declared_source_ids(pipeline):
    declared = parse_import(_export([_record()], sourceId="my-pipeline"), name="e.json")
    assert (declared.source.source_id, declared.source.source_id_origin) == (
        "my-pipeline", "declared")
    explicit = parse_import(_export([_record()], sourceId="my-pipeline"), name="e.json",
                            source_id="override")
    assert (explicit.source.source_id, explicit.source.source_id_origin) == (
        "override", "explicit")
    with pytest.raises(ImportFileError, match="source_id"):
        parse_import(_export([_record()]), name="e.json", source_id="bad id/../x")
    bad = parse_import(_export([_record()], sourceId="no spaces allowed"), name="e.json")
    assert any(i.column == "source.sourceId" for i in bad.issues)


def test_bytes_without_any_source_identity_are_not_importable(pipeline):
    doc = parse_import(json.dumps({"schemaVersion": 1, "records": [_record()]}).encode(),
                       name="upload.json")
    assert doc.source.source_id is None
    assert any("no source identity" in i.message for i in doc.issues)
    with pytest.raises(ImportRejected, match="source identity"):
        pipeline.apply_import(CAND, doc)
    assert pipeline.list_items(CAND) == []


def test_private_paths_are_never_stored(pipeline, tmp_path):
    folder = tmp_path / "Private Folder Name"
    folder.mkdir()
    file = folder / "jobs.json"
    file.write_text(json.dumps({"schemaVersion": 1, "source": {
        "path": "/Users/fictional/Secret Dir/Pipeline.numbers"}, "records": [_record()]}))
    receipt = pipeline.apply_import(CAND, load_import(file))
    stored = receipt.model_dump_json() + "".join(
        i.model_dump_json() for i in pipeline.list_items(CAND))
    assert "Secret Dir" not in stored and "Private Folder Name" not in stored
    assert receipt.source.source_id.startswith("src-")


# --- (2) the original snapshot is immutable; every source version is kept ----------------


def test_initial_snapshot_survives_source_updates_and_manual_edits(pipeline, clock):
    def version(**changes):
        values = {"stage": "Applied", "status": "Applied", "nextAction": "Wait", **changes}
        return parse_import(_export([{**_record(**values), "importKey": "k1"}]), name="e.json")

    first = pipeline.apply_import(CAND, version())
    item_id = first.rows[0].item_id
    clock.advance(minutes=1)
    pipeline.update_item(CAND, item_id, PipelineUpdate.model_validate(
        {"tracking": {"nextAction": "My plan", "stage": "My stage"}}), expected_revision=1)
    clock.advance(minutes=1)
    pipeline.apply_import(CAND, version(nextAction="Source v2", status="Interviewing"))
    clock.advance(minutes=1)
    pipeline.apply_import(CAND, version(nextAction="Source v3", status="Offer received"))

    item = pipeline.get_item(CAND, item_id)
    assert (item.provenance.initial.status, item.provenance.initial.next_action,
            item.provenance.initial.stage) == ("Applied", "Wait", "Applied")
    assert (item.provenance.latest.status, item.provenance.latest.next_action) == (
        "Offer received", "Source v3")
    assert (item.tracking.status, item.tracking.next_action, item.tracking.stage) == (
        "Offer received", "My plan", "My stage")
    versions = pipeline.source_versions(CAND, item_id)
    assert [v.values.status for v in versions] == ["Applied", "Interviewing", "Offer received"]
    assert [v.import_id for v in versions] == [r.id for r in pipeline.list_imports(CAND)]
    raw = sqlite3.connect(pipeline._conn.execute("PRAGMA database_list").fetchone()[2])
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("DELETE FROM source_versions")
    raw.close()


# --- (3) CSV values under a blank header are rejected -------------------------------------


def _csv(headers, rows):
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue().encode()


def test_csv_value_under_a_blank_header_is_reported(pipeline_fixtures):
    import csv as csv_module
    with (pipeline_fixtures / "pipeline.csv").open(newline="") as fh:
        reader = csv_module.reader(fh)
        headers = next(reader)
        first = next(reader)
    headers = [*headers, ""]
    rows = [[*first, "dropped value"], [*first[:-1], "other-key", ""]]
    doc = parse_import(_csv(headers, rows), name="x.csv", source_id="s")
    assert [(i.row, i.column, i.message) for i in doc.issues] == [
        (2, "column Y (blank header)",
         "a value under a blank header would be dropped; name the column or clear the cell")]
    assert [r.source_row for r in doc.rows] == [3]  # blank cell under blank header is fine


# --- (4) negated or hedged wording never claims an outcome lane ---------------------------


@pytest.mark.parametrize(
    ("status", "lane"),
    [
        ("No offer yet", "saved"),
        ("Application not submitted", "saved"),
        ("Not rejected; awaiting decision", "decision"),
        ("Offer not received", "saved"),
        ("Pending offer", "saved"),
        ("Possible offer", "saved"),
        ("Rejected?", "saved"),
        ("Waiting to hear back, no rejection", "saved"),
        ("No interview yet", "saved"),
        ("Didn't apply", "saved"),
        ("Offer received", "offer"),
        ("Offer declined", "closed"),
        ("Rejected after panel", "closed"),
        ("Closed - not moving forward", "closed"),
        ("Applied via careers site", "applied"),
    ],
)
def test_negated_and_hedged_wording(status, lane):
    suggestion = suggest_lane(None, status)
    assert suggestion.lane == lane
    if lane == "saved" and status not in ("Application not submitted",):
        assert suggestion.rule is None  # left for review, not claimed


def test_raw_stage_and_status_stay_verbatim_whatever_the_lane(pipeline):
    receipt = pipeline.apply_import(CAND, parse_import(_export([_record(
        stage="Final round", status="Not rejected; awaiting decision")]), name="e.json"))
    item = pipeline.get_item(CAND, receipt.rows[0].item_id)
    assert item.lane == "decision"
    assert (item.tracking.stage, item.tracking.status) == (
        "Final round", "Not rejected; awaiting decision")


# --- (5) oversized numbers are row issues, not crashes -------------------------------------


@pytest.mark.parametrize("value", [10**400, -(10**400), 10**309])
def test_oversized_json_numbers_are_row_issues(value):
    doc = parse_import(_export([_record(compensationLow=value)]), name="e.json")
    assert not doc.rows
    assert [(i.row, i.column) for i in doc.issues] == [(2, "compensationLow")]
    assert "finite" in doc.issues[0].message


def test_oversized_numbers_in_models_and_csv():
    with pytest.raises(ValidationError, match="finite"):
        TrackingFields.model_validate({"fitScore": 10**400})
    with pytest.raises(ValidationError, match="finite"):
        TrackingFields.model_validate({"compensationHigh": 1e308 * 10})


# --- D0 PipelineEntry adapter --------------------------------------------------------------


def test_pipeline_entry_uses_the_lane_label_and_original_imported_cells(pipeline, clock):
    receipt = pipeline.apply_import(CAND, parse_import(_export([_record(
        stage="Recruiter screen", status="Scheduling hiring manager interview", fitScore=0,
        compensationLow=120000, compensationHigh=135500.5, lastInterviewDate="2026-09-15",
        interviewTimeCT="14:30")]), name="e.json"))
    item_id = receipt.rows[0].item_id
    clock.advance(minutes=1)
    pipeline.update_item(CAND, item_id, PipelineUpdate.model_validate({
        "tracking": {"stage": "Edited stage", "status": "Edited status"},
        "next_action_due": "2026-10-05", "notes": "mine"}), expected_revision=1)
    pipeline.move_item(CAND, item_id, "interviewing", expected_revision=2)

    entry = pipeline.entry(CAND, item_id)
    assert isinstance(entry, PipelineEntry)
    assert entry.stage == "Interviewing"                        # current lane label
    assert (entry.title, entry.company) == ("Marketing Manager", "Alpha Fictional")
    assert entry.next_action_due == date(2026, 10, 5)
    assert type(entry.next_action_due) is date                   # no invented midnight
    assert entry.notes == "mine" and entry.import_source == "Pipeline.numbers"
    assert entry.imported_values == {                            # original, nonblank, verbatim
        "Company": "Alpha Fictional", "Role": "Marketing Manager",
        "Stage": "Recruiter screen", "Status": "Scheduling hiring manager interview",
        "Fit / 10": "0", "Comp low (USD/year)": "120000",
        "Comp high (USD/year)": "135500.5", "Last interview date": "2026-09-15",
        "Time (CT)": "14:30",
    }
    item = pipeline.get_item(CAND, item_id)
    assert to_pipeline_entry(item, pipeline.lanes(CAND)) == entry


def test_pipeline_entry_for_manual_and_titleless_cards(pipeline):
    manual = pipeline.create_item(CAND, NewPipelineItem(tracking=TrackingFields(
        company="Manual Fictional", role="Director", status="Offer received")))
    entry = pipeline.entry(CAND, manual.id)
    assert (entry.stage, entry.imported_values, entry.import_source) == ("Offer", {}, None)
    assert entry.next_action_due is None
    company_only = pipeline.create_item(CAND, NewPipelineItem(
        tracking=TrackingFields(company="Only Company")))
    with pytest.raises(EntryUnavailable, match="title"):
        pipeline.entry(CAND, company_only.id)
    with_listing = pipeline.create_item(CAND, NewPipelineItem(
        tracking=TrackingFields(company="Only Company"), listing_id="lst_1"))
    assert pipeline.entry(CAND, with_listing.id).listing_id == "lst_1"


# --- schema-1 databases are migrated without losing anything --------------------------------

_V1 = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version', '1');
CREATE TABLE items (id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, import_key TEXT,
    lane TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, body TEXT NOT NULL, UNIQUE (candidate_id, import_key));
CREATE TABLE history (sequence INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL,
    item_id TEXT NOT NULL, changed_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE boards (candidate_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE imports (id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, imported_at TEXT NOT NULL,
    body TEXT NOT NULL);
"""


def test_schema_1_database_is_migrated_preserving_original_rows(tmp_path, clock):
    db = tmp_path / "pipeline.sqlite3"
    raw = sqlite3.connect(db)
    raw.executescript(_V1)
    original = _record(status="Interviewing", nextAction="Old plan")
    edited = {**original, "nextAction": "Edited plan"}
    at = datetime(2026, 9, 1, 12, tzinfo=UTC).isoformat()
    body = {
        "id": "pipe_legacy", "candidate_id": CAND, "lane": "interviewing",
        "tracking": edited, "revision": 2, "created_at": at, "updated_at": at,
        "provenance": {
            "import_key": "k1", "source_name": "Pipeline.numbers", "source_format": "json",
            "document_sha256": "c" * 64, "source_row": 2, "first_imported_at": at,
            "last_imported_at": at, "original": original, "suggested_lane": "interviewing"},
    }
    raw.execute("INSERT INTO items VALUES ('pipe_legacy', ?, 'k1', 'interviewing', 2, ?, ?, ?)",
                (CAND, at, at, json.dumps(body)))
    raw.commit()
    raw.close()

    with PipelineStore.open(db, clock=clock) as store:
        item = store.get_item(CAND, "pipe_legacy")
        assert item.tracking.next_action == "Edited plan" and item.revision == 2
        assert item.provenance.initial == item.provenance.latest
        assert item.provenance.initial.next_action == "Old plan"
        legacy = legacy_source_id("Pipeline.numbers")
        assert item.provenance.source_id == legacy
        [version] = store.source_versions(CAND, "pipe_legacy")
        assert (version.import_id, version.values.next_action) == (LEGACY_IMPORT_ID, "Old plan")
        version_row = store._conn.execute("SELECT value FROM meta").fetchone()[0]
        assert int(version_row) == SCHEMA_VERSION == 2
        # Re-importing under the legacy source id updates the same card.
        again = parse_import(_export([{**original, "importKey": "k1", "sourceRow": 2}]),
                             name="e.json", source_id=legacy)
        assert store.apply_import(CAND, again).counts()["unchanged"] == 1
        assert store.entry(CAND, "pipe_legacy").imported_values["Next action"] == "Old plan"
    with PipelineStore.open(db, clock=clock) as reopened:  # idempotent on reopen
        assert len(reopened.source_versions(CAND, "pipe_legacy")) == 1
        # Migrated cards keep their id; newly imported cards use the scoped id.
        assert reopened.get_item(CAND, "pipe_legacy").id != imported_item_id(CAND, legacy, "k1")
