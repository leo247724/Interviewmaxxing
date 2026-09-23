"""The 23 reference fields and import parsing/validation (fictional data only)."""

from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import ValidationError

from interviewmaxxing_pipeline import (
    FIELD_HEADERS,
    FIELD_KEYS,
    MAX_IMPORT_BYTES,
    REFERENCE_FIELDS,
    ImportFileError,
    TrackingFields,
    derive_import_key,
    parse_import,
)

REFERENCE_HEADERS = [
    "Company", "Role", "Stage", "Status", "Priority", "Fit / 10", "Next interview date",
    "Time (CT)", "Interview format", "Work arrangement", "Location / commute",
    "Comp low (USD/year)", "Comp high (USD/year)", "Comp basis", "Target assessment",
    "Source / recruiter", "Follow-up date (suggested)", "Next action", "Last interview date",
    "Decision due", "Comp / benefits notes", "Fit rationale", "Process / source notes",
]
REFERENCE_KEYS = [
    "company", "role", "stage", "status", "priority", "fitScore", "nextInterviewDate",
    "interviewTimeCT", "interviewFormat", "workArrangement", "locationCommute",
    "compensationLow", "compensationHigh", "compensationBasis", "targetAssessment",
    "sourceRecruiter", "suggestedFollowUpDate", "nextAction", "lastInterviewDate",
    "decisionDueText", "compensationBenefitsNotes", "fitRationale", "processSourceNotes",
]


def test_reference_schema_is_exactly_the_23_workbook_columns():
    assert len(REFERENCE_FIELDS) == 23
    assert list(FIELD_HEADERS) == REFERENCE_HEADERS
    assert list(FIELD_KEYS) == REFERENCE_KEYS
    dumped = TrackingFields().by_key()
    assert list(dumped) == REFERENCE_KEYS


def test_every_field_round_trips_through_json_and_python_names(export_doc):
    row = export_doc.rows[0].tracking
    assert len(row.model_fields_set) == 23
    again = TrackingFields.model_validate(row.by_key())
    assert again == row
    assert TrackingFields.model_validate(row.model_dump()) == row
    assert row.fit_score == 8.0 and row.interview_time_ct is None
    assert row.suggested_follow_up_date == date(2026, 9, 18)
    assert row.decision_due_text == "End of next week (per recruiter)"  # text, not a date
    assert row.location_commute == "Austin, TX - 20 min drive"
    assert row.work_arrangement == "Hybrid"


def test_blank_is_distinct_from_zero(export_doc):
    zero = next(r.tracking for r in export_doc.rows if r.tracking.company == "Placeholder Health")
    assert (zero.fit_score, zero.compensation_low, zero.compensation_high) == (0.0, 0.0, 0.0)
    assert zero.work_arrangement is None and zero.next_interview_date is None
    blank = next(r.tracking for r in export_doc.rows if r.tracking.company == "Example Analytics")
    assert blank.compensation_low is None and blank.fit_score == 6.0
    one_sided = next(r.tracking for r in export_doc.rows
                     if r.tracking.company == "Imaginary Robotics")
    assert (one_sided.compensation_low, one_sided.compensation_high) == (100000.0, None)
    assert TrackingFields(company="   ").company is None


@pytest.mark.parametrize(
    ("values", "fragment"),
    [
        ({"fitScore": 10.5}, "between 0 and 10"),
        ({"fitScore": -1}, "between 0 and 10"),
        ({"fitScore": True}, "must be a number"),
        ({"fitScore": float("nan")}, "finite"),
        ({"compensationLow": float("inf")}, "finite"),
        ({"compensationLow": -5}, "negative"),
        ({"compensationLow": 150000, "compensationHigh": 100000}, "above"),
        ({"compensationLow": "120000"}, "must be a number"),
        ({"nextInterviewDate": "9/1/2026"}, "YYYY-MM-DD"),
        ({"interviewTimeCT": "2pm"}, "HH:MM"),
        ({"company": 5}, "must be text"),
        ({"unknownColumn": "x"}, "Extra inputs"),
    ],
)
def test_tracking_fields_reject_invalid_values(values, fragment):
    with pytest.raises(ValidationError, match=fragment):
        TrackingFields.model_validate(values)


def test_json_and_csv_fixtures_import_the_same_rows(export_doc, csv_doc):
    assert export_doc.ok and csv_doc.ok
    assert len(export_doc.rows) == len(csv_doc.rows) == 8
    assert csv_doc.skipped_blank_rows == 1
    for json_row, csv_row in zip(export_doc.rows, csv_doc.rows, strict=True):
        assert json_row.import_key == csv_row.import_key
        assert json_row.source_row == csv_row.source_row
        assert json_row.tracking == csv_row.tracking
    timed = next(r.tracking for r in csv_doc.rows if r.tracking.interview_time_ct)
    assert timed.interview_time_ct == "14:30" and timed.next_interview_date == date(2026, 10, 2)


def test_json_source_identity_is_recorded(export_doc):
    source = export_doc.source
    assert source.format == "json"
    assert source.name == "Job pipeline (fictional).numbers"  # file name only, never its path
    assert source.source_sha256 == "0" * 63 + "1"
    assert len(source.document_sha256) == 64
    assert (source.sheet, source.table) == ("Pipeline", "Table 1")


def _json(records, **top):
    doc = {"schemaVersion": 1, "records": records, **top}
    return json.dumps(doc).encode()


def _record(**values):
    base = dict.fromkeys(REFERENCE_KEYS)
    base.update(company="Fictional Co", role="Marketing Manager")
    base.update(values)
    return base


def _issues(doc):
    return [(i.row, i.column, i.message) for i in doc.issues]


def test_malformed_json_rows_are_reported_precisely():
    records = [
        _record(),
        _record(fitScore=11),
        _record(compensationLow=200000, compensationHigh=100000),
        _record(nextInterviewDate="next Tuesday"),
        {**_record(), "extra": 1},
        {k: v for k, v in _record().items() if k != "priority"},
        _record(company=None, role=None),
        "not an object",
        _record(interviewTimeCT="25:00"),
    ]
    doc = parse_import(_json(records), name="bad.json")
    issues = _issues(doc)
    assert not doc.ok and [r.source_row for r in doc.rows] == [2]
    assert (3, "fitScore", "fit score must be between 0 and 10") in issues
    assert any(r == 4 and c == "compensationLow" and "above" in m for r, c, m in issues)
    assert (5, "nextInterviewDate", "must be a date as YYYY-MM-DD") in issues
    assert (6, None, "unknown keys: ['extra']") in issues
    assert (7, None, "missing keys: ['priority']") in issues
    assert (8, None, "row has neither a Company nor a Role") in issues
    assert (9, None, "record must be a JSON object") in issues
    assert any(r == 10 and c == "interviewTimeCT" for r, c, _ in issues)


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        (b'{"schemaVersion": 1, "records": [], "records": []}', "duplicate key"),
        (b'{"schemaVersion": 1, "records": [{"fitScore": NaN}]}', "non-finite"),
        (b'{"schemaVersion": 1, "records": [{"fitScore": Infinity}]}', "non-finite"),
        (b'{"schemaVersion": 1, "records": [', "invalid JSON at line 1"),
        (b'[]', "must be a JSON object"),
        (b'{"schemaVersion": 2, "records": []}', "expected schemaVersion 1"),
        (b'{"schemaVersion": 1, "records": {}}', "records must be a list"),
        (b'{"schemaVersion": 1, "records": [], "other": 1}', "unknown top-level keys"),
    ],
)
def test_malformed_json_documents_are_rejected(raw, fragment):
    doc = parse_import(raw, name="bad.json")
    assert not doc.ok and not doc.rows
    assert any(fragment in i.message for i in doc.issues)


def test_duplicate_import_keys_and_source_rows_are_reported():
    records = [_record(), _record(), {**_record(role="Director"), "sourceRow": 2}]
    doc = parse_import(_json(records), name="dupes.json")
    messages = _issues(doc)
    assert any(r == 3 and "same import key as row 2" in m for r, _, m in messages)
    assert (2, "sourceRow", "duplicate sourceRow") in messages


def test_derived_import_key_is_stable_and_case_insensitive():
    key = derive_import_key("Fictional Co", "Marketing  Manager")
    assert key == derive_import_key("fictional co", "marketing manager")
    assert key != derive_import_key("Fictional Co", "Marketing Director")
    doc = parse_import(_json([_record()]), name="x.json")
    assert doc.rows[0].import_key == derive_import_key("Fictional Co", "Marketing Manager")


def _csv(rows, headers=None):
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers or REFERENCE_HEADERS)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode()


def _csv_row(**cells):
    row = dict.fromkeys(REFERENCE_HEADERS, "")
    row.update({"Company": "Fictional Co", "Role": "Marketing Manager"})
    row.update(cells)
    return [row[h] for h in REFERENCE_HEADERS]


def test_malformed_csv_cells_are_reported_by_row_and_header():
    doc = parse_import(_csv([
        _csv_row(**{"Comp low (USD/year)": "120k"}),
        _csv_row(**{"Fit / 10": "eight"}),
        _csv_row(**{"Last interview date": "2026-02-30"}),
        _csv_row(**{"Time (CT)": "13:30 PM"}),
        _csv_row(**{"Comp low (USD/year)": "$150,000", "Comp high (USD/year)": "$100,000"}),
        _csv_row(Company="", Role="", Priority="High"),
        [*_csv_row(**{"Role": "Manager 2"}), "unexpected"],
    ]), name="bad.csv")
    issues = _issues(doc)
    assert not doc.rows
    assert (2, "Comp low (USD/year)",
            "expected a yearly USD amount such as 120000 or $120,000") in issues
    assert (3, "Fit / 10", "expected a number from 0 to 10") in issues
    assert (4, "Last interview date", "expected a date as YYYY-MM-DD or M/D/YYYY") in issues
    assert (5, "Time (CT)", "expected a time such as 14:30 or 2:30 PM") in issues
    assert any(r == 6 and c == "Comp low (USD/year)" and "above" in m for r, c, m in issues)
    assert (7, None, "row has neither a Company nor a Role") in issues
    assert (8, None, "24 cells but 23 headers") in issues


def test_csv_headers_must_be_the_reference_headers():
    missing = parse_import(_csv([], headers=REFERENCE_HEADERS[:-1]), name="x.csv")
    assert missing.issues[0].message == "missing headers: ['Process / source notes']"
    unknown = parse_import(_csv([], headers=[*REFERENCE_HEADERS, "Salary"]), name="x.csv")
    assert unknown.issues[0].message == "unknown headers: ['Salary']"
    dupes = parse_import(_csv([], headers=[*REFERENCE_HEADERS, "Company"]), name="x.csv")
    assert "duplicate headers" in dupes.issues[0].message
    reordered = parse_import(_csv([_csv_row()[::-1]], headers=REFERENCE_HEADERS[::-1]),
                             name="x.csv", source_id="test-source")
    assert reordered.ok and reordered.rows[0].tracking.company == "Fictional Co"


def test_unreadable_files_raise():
    with pytest.raises(ImportFileError, match="not UTF-8"):
        parse_import(b"\xff\xfe\x00bad", name="x.csv")
    with pytest.raises(ImportFileError, match="limit"):
        parse_import(b" " * (MAX_IMPORT_BYTES + 1), name="x.json")
    with pytest.raises(ImportFileError, match="format"):
        parse_import(b"{}", name="x.xlsx")
