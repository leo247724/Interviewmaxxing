"""Applying imports: idempotence, preserved manual edits, all-or-nothing, receipts."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from interviewmaxxing_pipeline import (
    FIELD_KEYS,
    ImportRejected,
    PipelineStore,
    PipelineUpdate,
    imported_item_id,
    load_import,
    parse_import,
)
from interviewmaxxing_pipeline.cli import main

CAND = "cand_fictional"
SOURCE = load_import(Path(__file__).parents[1] / "fixtures" / "pipeline"
                     / "pipeline_export.json").source.source_id


def _changed_export(pipeline_fixtures, **changes_by_key):
    """The fixture export with some cells changed (``{"fixture-01": {...}}``)."""
    doc = json.loads((pipeline_fixtures / "pipeline_export.json").read_text())
    for record in doc["records"]:
        record.update(changes_by_key.get(record["importKey"], {}))
    return parse_import(json.dumps(doc).encode(), name="pipeline_export.json")


def test_preview_writes_nothing_and_apply_creates_every_row(pipeline, export_doc):
    preview = pipeline.preview_import(CAND, export_doc)
    assert preview.ok and preview.counts() == {"create": 8, "update": 0, "unchanged": 0}
    assert pipeline.list_items(CAND) == [] and pipeline.list_imports(CAND) == []

    receipt = pipeline.apply_import(CAND, export_doc,
                                    expected_document_sha256=preview.source.document_sha256)
    assert receipt.rows == preview.rows
    items = pipeline.list_items(CAND)
    assert len(items) == 8
    by_key = {i.provenance.import_key: i for i in items}
    for row in export_doc.rows:
        item = by_key[row.import_key]
        assert item.id == imported_item_id(CAND, export_doc.source.source_id, row.import_key)
        assert item.tracking == row.tracking == item.provenance.initial == item.provenance.latest
        assert item.tracking.by_key().keys() == set(FIELD_KEYS)
        assert item.provenance.source_row == row.source_row
        assert item.provenance.source_sha256 == export_doc.source.source_sha256
        assert item.provenance.document_sha256 == export_doc.source.document_sha256
        # Nothing is invented: no URL, no application, no due date from the
        # workbook's suggested follow-up, no interview date from notes.
        assert item.application_url is None and item.application_id is None
        assert item.next_action_due is None and item.needs_application_url
    assert by_key["fixture-01"].tracking.next_interview_date is None
    lanes = {k: i.lane for k, i in by_key.items()}
    assert lanes == {"fixture-01": "decision", "fixture-02": "closed", "fixture-03": "scheduling",
                     "fixture-04": "assessment", "fixture-05": "interviewing",
                     "fixture-06": "offer", "fixture-07": "saved", "fixture-08": "applied"}
    history = pipeline.history(CAND, by_key["fixture-01"].id)
    assert [(h.kind, h.actor, h.to_lane) for h in history] == [
        ("imported", "import", "decision")]
    assert pipeline.list_imports(CAND) == [receipt]


def test_reimporting_unchanged_data_is_a_no_op(
    pipeline, export_doc, pipeline_fixtures, clock
):
    pipeline.apply_import(CAND, export_doc)
    before = pipeline.list_items(CAND)
    clock.advance(hours=1)
    again = pipeline.apply_import(CAND, export_doc)
    assert again.counts() == {"create": 0, "update": 0, "unchanged": 8}
    # The same rows as CSV (other formatting, same values and keys), explicitly
    # declared to be the same logical source, are unchanged too.
    same_source_csv = load_import(pipeline_fixtures / "pipeline.csv", source_id=SOURCE)
    assert pipeline.apply_import(CAND, same_source_csv).counts()["unchanged"] == 8
    assert pipeline.list_items(CAND) == before
    assert all(len(pipeline.history(CAND, i.id)) == 1 for i in before)
    assert len(pipeline.list_imports(CAND)) == 3


def test_reimport_keeps_manual_edits_and_lane(pipeline, export_doc, pipeline_fixtures, clock):
    pipeline.apply_import(CAND, export_doc)
    item_id = imported_item_id(CAND, SOURCE, "fixture-03")
    clock.advance(minutes=1)
    pipeline.update_item(CAND, item_id, PipelineUpdate.model_validate({
        "tracking": {"nextAction": "My own next step", "priority": "Medium"},
        "notes": "private note"}), expected_revision=1)
    pipeline.move_item(CAND, item_id, "interviewing", expected_revision=2)

    changed = _changed_export(pipeline_fixtures, **{"fixture-03": {
        "nextAction": "Source changed next step",   # user edited: kept
        "priority": "Medium",                        # same as the user's edit: no conflict
        "status": "Interviewing with panel",          # unedited: taken from source
        "fitRationale": "Updated rationale",          # unedited: taken from source
    }})
    preview = pipeline.preview_import(CAND, changed)
    plan = next(r for r in preview.rows if r.import_key == "fixture-03")
    assert plan.action == "update"
    assert plan.kept_manual_fields == ["nextAction"]
    assert plan.updated_fields == ["status", "fitRationale"]
    assert preview.counts() == {"create": 0, "update": 1, "unchanged": 7}

    clock.advance(minutes=1)
    pipeline.apply_import(CAND, changed)
    item = pipeline.get_item(CAND, item_id)
    assert item.tracking.next_action == "My own next step"
    assert item.tracking.priority == "Medium"
    assert item.tracking.status == "Interviewing with panel"
    assert item.tracking.fit_rationale == "Updated rationale"
    assert item.notes == "private note"
    assert item.lane == "interviewing"                      # reimport never moves a card
    assert item.provenance.latest.next_action == "Source changed next step"
    assert item.provenance.initial.next_action == "Confirm availability"  # never erased
    assert item.provenance.first_imported_at < item.provenance.last_imported_at
    last = pipeline.history(CAND, item_id)[-1]
    assert (last.kind, last.actor, last.to_status) == (
        "stage_edited", "import", "Interviewing with panel")

    # Importing the same changed file again changes nothing further.
    assert pipeline.apply_import(CAND, changed).counts()["unchanged"] == 8


def test_new_rows_are_added_without_duplicating_existing_cards(
    pipeline, export_doc, pipeline_fixtures
):
    pipeline.apply_import(CAND, export_doc)
    doc = json.loads((pipeline_fixtures / "pipeline_export.json").read_text())
    extra = {**doc["records"][0], "sourceRow": 10, "importKey": "fixture-09",
             "company": "Another Fictional Co"}
    doc["records"].append(extra)
    receipt = pipeline.apply_import(CAND, parse_import(json.dumps(doc).encode(), name="x.json"))
    assert receipt.counts() == {"create": 1, "update": 0, "unchanged": 8}
    assert len(pipeline.list_items(CAND)) == 9


def test_malformed_documents_write_nothing(pipeline, export_doc, pipeline_fixtures):
    doc = json.loads((pipeline_fixtures / "pipeline_export.json").read_text())
    doc["records"][5]["fitScore"] = 12
    bad = parse_import(json.dumps(doc).encode(), name="bad.json")
    preview = pipeline.preview_import(CAND, bad)
    assert not preview.ok and len(preview.rows) == 7
    assert [(i.row, i.column) for i in preview.issues] == [(7, "fitScore")]
    with pytest.raises(ImportRejected, match="row 7 fitScore"):
        pipeline.apply_import(CAND, bad)
    assert pipeline.list_items(CAND) == [] and pipeline.list_imports(CAND) == []


def test_apply_refuses_a_file_other_than_the_previewed_one(pipeline, export_doc, csv_doc):
    with pytest.raises(ImportRejected, match="changed since it was previewed"):
        pipeline.apply_import(CAND, csv_doc,
                              expected_document_sha256=export_doc.source.document_sha256)
    assert pipeline.list_items(CAND) == []


def test_a_merge_that_would_be_invalid_rejects_the_whole_import(
    pipeline, export_doc, pipeline_fixtures
):
    pipeline.apply_import(CAND, export_doc)
    item_id = imported_item_id(CAND, SOURCE, "fixture-01")
    pipeline.update_item(CAND, item_id, PipelineUpdate.model_validate(
        {"tracking": {"compensationHigh": 125000}}), expected_revision=1)
    changed = _changed_export(pipeline_fixtures, **{
        "fixture-01": {"compensationLow": 130000.0, "compensationHigh": 150000.0},
        "fixture-02": {"nextAction": "Would change"}})
    preview = pipeline.preview_import(CAND, changed)
    assert [i.row for i in preview.issues] == [2]
    with pytest.raises(ImportRejected, match="merging this row"):
        pipeline.apply_import(CAND, changed)
    assert pipeline.get_item(CAND, imported_item_id(CAND, SOURCE, "fixture-02")).tracking.next_action \
        == "None"


def test_imports_are_candidate_scoped(pipeline, export_doc):
    pipeline.apply_import(CAND, export_doc)
    receipt = pipeline.apply_import("cand_other", export_doc)
    assert receipt.counts()["create"] == 8
    mine = {i.id for i in pipeline.list_items(CAND)}
    theirs = {i.id for i in pipeline.list_items("cand_other")}
    assert len(mine) == len(theirs) == 8 and not mine & theirs


def test_cli_preview_and_import_print_no_cell_values(tmp_path, pipeline_fixtures, capsys):
    db = tmp_path / "pipeline.sqlite3"
    export = pipeline_fixtures / "pipeline_export.json"
    assert main(["--db", str(db), "--candidate", CAND, "preview", str(export)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["ok"] and preview["counts"]["create"] == 8
    text = json.dumps(preview)
    for record in json.loads(export.read_text())["records"]:
        assert record["company"] not in text and record["nextAction"] not in text

    assert main(["--db", str(db), "--candidate", CAND, "import", str(export),
                 "--expect-sha256", "0" * 64]) == 1
    assert "previewed" in capsys.readouterr().out
    assert main(["--db", str(db), "--candidate", CAND, "import", str(export),
                 "--expect-sha256", preview["source"]["documentSha256"]]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["ok"] and applied["receiptId"].startswith("pimp_")
    assert main(["--db", str(db), "--candidate", CAND, "board"]) == 0
    board = json.loads(capsys.readouterr().out)
    assert sum(len(lane["cards"]) for lane in board["lanes"]) == 8
    assert main(["--db", str(db), "preview", str(tmp_path / "missing.json")]) == 2


def test_example_file_is_a_valid_import():
    example = Path(__file__).parents[2] / "examples" / "pipeline-import.example.json"
    doc = load_import(example)
    assert doc.ok and len(doc.rows) == 2


@pytest.mark.skipif("IMX_PIPELINE_REFERENCE" not in os.environ,
                    reason="set IMX_PIPELINE_REFERENCE to a private normalized export")
def test_private_reference_export_previews_cleanly(tmp_path):
    """Opt-in: validates the user's private export without writing or printing it."""
    doc = load_import(os.environ["IMX_PIPELINE_REFERENCE"])
    with PipelineStore.open(tmp_path / "pipeline.sqlite3") as store:
        preview = store.preview_import("default", doc)
        assert preview.ok, [(i.row, i.column) for i in preview.issues]
        assert preview.rows and all(r.action == "create" for r in preview.rows)
        assert store.list_items("default") == []
