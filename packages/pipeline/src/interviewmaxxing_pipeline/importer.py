"""Parse and validate pipeline imports (normalized JSON or CSV) without writing.

Two formats are accepted:

* **JSON** (the normalized workbook export)::

      {"schemaVersion": 1,
       "source": {"path": "...", "sha256": "...", "sheet": "Pipeline", ...},   # optional
       "records": [{"sourceRow": 2, "importKey": "...", "company": ..., ...}]}

  Every record carries all 23 reference keys (``null`` for blank); ``sourceRow`` and
  ``importKey`` are optional. Numbers are JSON numbers, dates ``YYYY-MM-DD``.
  Duplicate keys, ``NaN``/``Infinity`` and unknown keys are rejected.

* **CSV** with the 23 reference headers exactly as in the workbook (any order) and an
  optional ``Import key`` column. Row numbers count the header as row 1. Fully blank
  rows are skipped (and counted). Cells accept ``$120,000``-style numbers,
  ``YYYY-MM-DD`` or ``M/D/YYYY`` dates and ``14:30`` / ``2:30 PM`` times.

The whole document is validated. Any issue is reported with its row and column, and
``apply_import`` then refuses the document: there are no partial imports.

**Source identity.** Every import belongs to one logical source (``source_id``), and
cards are keyed by (candidate, source, import key), so rows from two different files
never overwrite each other. The source id is, in order: the ``source_id`` argument;
``source.sourceId`` in a JSON export; an opaque hash of the export's declared workbook
path, sheet and table; an opaque hash of the resolved file path (``load_import``).
A declared workbook path counts only when it is absolute, or relative and resolved
against the folder of the export file actually loaded: two exports in different
folders that both declare ``Pipeline.numbers`` are different sources. Bytes with only
a relative declared path (``parse_import`` without a file) need an explicit
``source_id`` or a declared ``sourceId``.
None of these is a content digest, so editing the workbook keeps its identity, and
two files with the same name in different folders stay distinct. Full paths are
never stored. Without any of them the document gets an issue asking for a
``source_id``.

**Import keys** make re-imports idempotent. A supplied key (``importKey`` or the
``Import key`` column) is used as is. Otherwise the key is derived from the row's
Company and Role (case-insensitive), so it stays stable as long as those stay the
same. Two rows with the same key are both reported.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from .fields import (
    FIELD_HEADERS,
    FIELD_KEYS,
    HEADER_BY_NAME,
    KEY_BY_NAME,
    NAME_BY_KEY,
    REFERENCE_FIELDS,
    TrackingFields,
)
from .models import ImportFormat, RowIssue, SourceIdOrigin, SourceInfo

MAX_IMPORT_BYTES: Final = 5 * 1024 * 1024
MAX_IMPORT_ROWS: Final = 5000
IMPORT_KEY_HEADER: Final = "Import key"
SUPPORTED_SCHEMA_VERSION: Final = 1
_IMPORT_KEY: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_NUMBER_FIELDS: Final = frozenset({"fit_score", "compensation_low", "compensation_high"})
_DATE_FIELDS: Final = frozenset(
    {"next_interview_date", "suggested_follow_up_date", "last_interview_date"})
_TIME_FIELD: Final = "interview_time_ct"


class ImportFileError(ValueError):
    """The file cannot be read as an import at all (size, encoding, format)."""


@dataclass(frozen=True, slots=True)
class ParsedRow:
    source_row: int
    import_key: str
    tracking: TrackingFields


@dataclass(frozen=True, slots=True)
class ImportDocument:
    """A parsed import: valid rows, every issue found, and the source identity."""

    source: SourceInfo
    rows: tuple[ParsedRow, ...]
    issues: tuple[RowIssue, ...]
    skipped_blank_rows: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues


# --- public entry points ----------------------------------------------------------------


def load_import(
    path: Path | str, *, format: ImportFormat | None = None, source_id: str | None = None
) -> ImportDocument:
    """Read and parse an import file. Only the file name (not its path) is recorded;
    the resolved path only feeds an opaque source id when nothing better is given."""
    file = Path(path).expanduser().resolve()
    size = file.stat().st_size
    if size > MAX_IMPORT_BYTES:
        raise ImportFileError(f"{file.name} is {size} bytes; the limit is {MAX_IMPORT_BYTES}")
    return parse_import(file.read_bytes(), name=file.name, format=format, source_id=source_id,
                        _file_identity=str(file))


def parse_import(
    data: bytes, *, name: str, format: ImportFormat | None = None,
    source_id: str | None = None, _file_identity: str | None = None,
) -> ImportDocument:
    """Parse import bytes. ``format`` defaults from the name's extension. Give
    ``source_id`` for bytes that do not declare their own source (see above)."""
    if len(data) > MAX_IMPORT_BYTES:
        raise ImportFileError(f"import is {len(data)} bytes; the limit is {MAX_IMPORT_BYTES}")
    fmt = format or _format_from_name(name)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportFileError(f"{name} is not UTF-8 text: {exc.reason} at byte {exc.start}") from exc
    digest = hashlib.sha256(data).hexdigest()
    if source_id is not None and not _SOURCE_ID.fullmatch(source_id):
        raise ImportFileError(
            "source_id must be 1-128 characters of letters, digits and . _ : -")
    if fmt == "json":
        base_dir = Path(_file_identity).parent if _file_identity is not None else None
        document = _parse_json(text, name=name, digest=digest, base_dir=base_dir)
    else:
        document = _parse_csv(text, name=name, digest=digest)
    return _with_source_id(document, explicit=source_id, file_identity=_file_identity)


def derive_source_id(kind: str, *parts: str) -> str:
    """Opaque, stable source id from identifying parts (never stored in clear)."""
    basis = "\x1f".join((kind, *parts))
    return "src-" + hashlib.sha256(basis.encode()).hexdigest()[:24]


def _with_source_id(
    document: ImportDocument, *, explicit: str | None, file_identity: str | None
) -> ImportDocument:
    source = document.source
    origin: SourceIdOrigin | None
    if explicit is not None:
        source_id, origin = explicit, "explicit"
    elif source.source_id is not None:
        source_id, origin = source.source_id, source.source_id_origin
    elif file_identity is not None:
        source_id, origin = derive_source_id("file", file_identity), "file-path"
    else:
        issue = RowIssue(row=None, message=(
            "the import has no source identity; pass a source_id (or declare "
            "source.sourceId) so rows from different files never overwrite each other"))
        return replace(document, issues=(*document.issues, issue))
    return replace(document, source=source.model_copy(
        update={"source_id": source_id, "source_id_origin": origin}))


def derive_import_key(company: str | None, role: str | None) -> str:
    """Stable key for a row without a supplied key: Company + Role, case-insensitive."""
    basis = f"{' '.join((company or '').split()).casefold()}\x1f" \
            f"{' '.join((role or '').split()).casefold()}"
    return "row-" + hashlib.sha256(basis.encode()).hexdigest()[:24]


def _format_from_name(name: str) -> ImportFormat:
    suffix = Path(name).suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix == ".csv":
        return "csv"
    raise ImportFileError(f"cannot tell the format of {name!r}; use a .json or .csv file")


# --- JSON -----------------------------------------------------------------------------


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite number {token} is not allowed")


def _parse_json(text: str, *, name: str, digest: str, base_dir: Path | None) -> ImportDocument:
    source = SourceInfo(name=name, format="json", document_sha256=digest)
    try:
        doc = json.loads(text, object_pairs_hook=_reject_duplicates,
                         parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        return _failed(source, f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}")
    except ValueError as exc:
        return _failed(source, f"invalid JSON: {exc}")
    if not isinstance(doc, dict):
        return _failed(source, "the document must be a JSON object with schemaVersion and records")
    issues: list[RowIssue] = []
    unknown = sorted(set(doc) - {"schemaVersion", "source", "records"})
    if unknown:
        issues.append(RowIssue(row=None, message=f"unknown top-level keys: {unknown}"))
    version = doc.get("schemaVersion")
    if version != SUPPORTED_SCHEMA_VERSION or isinstance(version, bool):
        issues.append(RowIssue(row=None, column="schemaVersion",
                               message=f"expected schemaVersion {SUPPORTED_SCHEMA_VERSION}"))
    source = _json_source(doc.get("source"), source, issues, base_dir)
    records = doc.get("records")
    if not isinstance(records, list):
        issues.append(RowIssue(row=None, column="records", message="records must be a list"))
        return ImportDocument(source=source, rows=(), issues=tuple(issues))
    if len(records) > MAX_IMPORT_ROWS:
        issues.append(RowIssue(row=None, message=f"{len(records)} rows; the limit is "
                                                  f"{MAX_IMPORT_ROWS}"))
        return ImportDocument(source=source, rows=(), issues=tuple(issues))

    allowed = set(FIELD_KEYS) | {"sourceRow", "importKey"}
    rows: list[ParsedRow] = []
    for index, record in enumerate(records):
        row_number = index + 2
        if not isinstance(record, dict):
            issues.append(RowIssue(row=row_number, message="record must be a JSON object"))
            continue
        declared = record.get("sourceRow", row_number)
        if isinstance(declared, bool) or not isinstance(declared, int) or declared < 2:
            issues.append(RowIssue(row=row_number, column="sourceRow",
                                   message="sourceRow must be an integer of at least 2"))
        else:
            row_number = declared
        extra = sorted(set(record) - allowed)
        missing = [k for k in FIELD_KEYS if k not in record]
        if extra:
            issues.append(RowIssue(row=row_number, message=f"unknown keys: {extra}"))
        if missing:
            issues.append(RowIssue(row=row_number, message=f"missing keys: {missing}"))
        if extra or missing:
            continue
        values: dict[str, Any] = {}
        row_issues: list[RowIssue] = []
        for field in REFERENCE_FIELDS:
            raw = record[field.key]
            if field.name == _TIME_FIELD and isinstance(raw, str):
                converted = _time(raw)
                if converted is _INVALID:
                    row_issues.append(RowIssue(row=row_number, column=field.key,
                                               message="must be a time such as 14:30 or 2:30 PM"))
                    continue
                raw = converted
            values[field.name] = raw
        tracking = _tracking(values, row_number, row_issues, header=False)
        key = _import_key(record.get("importKey"), tracking, row_number, "importKey", row_issues)
        issues.extend(row_issues)
        if tracking is not None and key is not None and not row_issues:
            rows.append(ParsedRow(source_row=row_number, import_key=key, tracking=tracking))
    return _finish(source, rows, issues, 0)


def _workbook_location(declared: str, base_dir: Path | None) -> str | None:
    """Where the declared workbook really is: an absolute (or ``~``) path as given, a
    relative one only against the loaded export's folder; None when unknowable."""
    path = Path(declared).expanduser()
    if path.is_absolute():
        return os.path.normpath(path)
    if base_dir is None:
        return None
    return os.path.normpath(base_dir / path)


def _json_source(
    raw: Any, source: SourceInfo, issues: list[RowIssue], base_dir: Path | None
) -> SourceInfo:
    if raw is None:
        return source
    if not isinstance(raw, dict):
        issues.append(RowIssue(row=None, column="source", message="source must be an object"))
        return source
    update: dict[str, Any] = {}
    path = raw.get("path")
    if isinstance(path, str) and path.strip():
        update["name"] = Path(path).name
    declared = raw.get("sha256")
    if declared is not None:
        if isinstance(declared, str) and _SHA256.fullmatch(declared):
            update["source_sha256"] = declared
        else:
            issues.append(RowIssue(row=None, column="source.sha256",
                                   message="must be a lowercase hex SHA-256 digest"))
    for key, attr in (("sheet", "sheet"), ("table", "table"),
                      ("sourceModifiedAt", "source_modified_at")):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            update[attr] = value
    declared_id = raw.get("sourceId")
    if declared_id is not None:
        if isinstance(declared_id, str) and _SOURCE_ID.fullmatch(declared_id):
            update["source_id"], update["source_id_origin"] = declared_id, "declared"
        else:
            issues.append(RowIssue(row=None, column="source.sourceId", message=(
                "must be 1-128 characters of letters, digits and . _ : -")))
    elif isinstance(path, str) and path.strip():
        location = _workbook_location(path.strip(), base_dir)
        if location is not None:
            update["source_id"] = derive_source_id(
                "workbook", location, str(raw.get("sheet") or ""), str(raw.get("table") or ""))
            update["source_id_origin"] = "workbook-path"
    return source.model_copy(update=update)


# --- CSV ------------------------------------------------------------------------------


def _parse_csv(text: str, *, name: str, digest: str) -> ImportDocument:
    source = SourceInfo(name=name, format="csv", document_sha256=digest)
    try:
        table = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        return _failed(source, f"invalid CSV: {exc}")
    if not table:
        return _failed(source, "the CSV file is empty")
    headers = [h.strip() for h in table[0]]
    issues: list[RowIssue] = []
    known = set(FIELD_HEADERS) | {IMPORT_KEY_HEADER}
    duplicated = sorted({h for h in headers if headers.count(h) > 1 and h})
    unknown = [h for h in headers if h and h not in known]
    missing = [h for h in FIELD_HEADERS if h not in headers]
    if duplicated:
        issues.append(RowIssue(row=1, message=f"duplicate headers: {duplicated}"))
    if unknown:
        issues.append(RowIssue(row=1, message=f"unknown headers: {unknown}"))
    if missing:
        issues.append(RowIssue(row=1, message=f"missing headers: {missing}"))
    if issues:
        return ImportDocument(source=source, rows=(), issues=tuple(issues))
    column = {h: i for i, h in enumerate(headers) if h}
    blank_columns = [i for i, h in enumerate(headers) if not h]
    data_rows = table[1:]
    if len(data_rows) > MAX_IMPORT_ROWS:
        return _failed(source, f"{len(data_rows)} rows; the limit is {MAX_IMPORT_ROWS}")

    rows: list[ParsedRow] = []
    skipped = 0
    for offset, cells in enumerate(data_rows):
        row_number = offset + 2
        if not any(c.strip() for c in cells):
            skipped += 1
            continue
        if len(cells) > len(headers) and any(c.strip() for c in cells[len(headers):]):
            issues.append(RowIssue(row=row_number,
                                   message=f"{len(cells)} cells but {len(headers)} headers"))
            continue
        cells = cells + [""] * (len(headers) - len(cells))
        row_issues = [
            RowIssue(row=row_number, column=f"column {_column_letter(i)} (blank header)",
                     message="a value under a blank header would be dropped; name the "
                             "column or clear the cell")
            for i in blank_columns if cells[i].strip()
        ]
        values: dict[str, Any] = {}
        for field in REFERENCE_FIELDS:
            cell = cells[column[field.header]]
            converted = _cell(field.name, cell)
            if converted is _INVALID:
                row_issues.append(RowIssue(row=row_number, column=field.header,
                                           message=_CELL_HINT.get(field.name, "invalid value")))
            else:
                values[field.name] = converted
        tracking = None if row_issues else _tracking(values, row_number, row_issues, header=True)
        raw_key = cells[column[IMPORT_KEY_HEADER]] if IMPORT_KEY_HEADER in column else None
        key = _import_key(raw_key or None, tracking, row_number, IMPORT_KEY_HEADER, row_issues)
        issues.extend(row_issues)
        if tracking is not None and key is not None and not row_issues:
            rows.append(ParsedRow(source_row=row_number, import_key=key, tracking=tracking))
    return _finish(source, rows, issues, skipped)


def _column_letter(index: int) -> str:
    """Spreadsheet column letter for a zero-based index (0 -> A, 26 -> AA)."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


class _Invalid:
    pass


_INVALID: Final = _Invalid()

_CELL_HINT: Final = {
    "fit_score": "expected a number from 0 to 10",
    "compensation_low": "expected a yearly USD amount such as 120000 or $120,000",
    "compensation_high": "expected a yearly USD amount such as 120000 or $120,000",
    "next_interview_date": "expected a date as YYYY-MM-DD or M/D/YYYY",
    "suggested_follow_up_date": "expected a date as YYYY-MM-DD or M/D/YYYY",
    "last_interview_date": "expected a date as YYYY-MM-DD or M/D/YYYY",
    "interview_time_ct": "expected a time such as 14:30 or 2:30 PM",
}

_CSV_NUMBER: Final = re.compile(r"^\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?$")
_US_DATE: Final = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_TIME: Final = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?$", re.IGNORECASE)


def _cell(name: str, cell: str) -> Any:
    text = cell.strip()
    if not text:
        return None
    if name in _NUMBER_FIELDS:
        match = _CSV_NUMBER.fullmatch(text)
        if match is None:
            return _INVALID
        return float(match.group(1).replace(",", "") + (match.group(2) or ""))
    if name in _DATE_FIELDS:
        return _date(text)
    if name == _TIME_FIELD:
        return _time(text)
    return cell


def _date(text: str) -> Any:
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text).isoformat()
        match = _US_DATE.fullmatch(text)
        if match:
            month, day, year = (int(g) for g in match.groups())
            return date(year, month, day).isoformat()
    except ValueError:
        return _INVALID
    return _INVALID


def _time(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return None
    match = _TIME.fullmatch(stripped)
    if match is None:
        return _INVALID
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    meridiem = (match.group(3) or "").replace(".", "").lower()
    if meridiem:
        if not 1 <= hour <= 12:
            return _INVALID
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    elif match.group(2) is None:
        return _INVALID  # a bare "14" is not a time
    if hour > 23 or minute > 59:
        return _INVALID
    return f"{hour:02d}:{minute:02d}"


# --- shared -----------------------------------------------------------------------------


def _tracking(
    values: dict[str, Any], row: int, issues: list[RowIssue], *, header: bool
) -> TrackingFields | None:
    try:
        tracking = TrackingFields.model_validate(values)
    except ValidationError as exc:
        for error in exc.errors():
            loc = error.get("loc") or ()
            name = str(loc[0]) if loc else None
            name = NAME_BY_KEY.get(name, name) if name else None
            if name is None and "compensation" in error["msg"]:
                name = "compensation_low"
            column = (HEADER_BY_NAME.get(name, name) if header else KEY_BY_NAME.get(name, name)) \
                if name else None
            message = str(error["msg"]).removeprefix("Value error, ")
            issues.append(RowIssue(row=row, column=column, message=message))
        return None
    if not tracking.is_identifiable:
        issues.append(RowIssue(row=row, message="row has neither a Company nor a Role"))
        return None
    return tracking


def _import_key(
    raw: Any, tracking: TrackingFields | None, row: int, column: str, issues: list[RowIssue]
) -> str | None:
    if raw is not None:
        if not isinstance(raw, str) or not _IMPORT_KEY.fullmatch(raw.strip()):
            issues.append(RowIssue(row=row, column=column, message=(
                "import key must be 1-128 characters of letters, digits and . _ : -")))
            return None
        return raw.strip()
    if tracking is None:
        return None
    return derive_import_key(tracking.company, tracking.role)


def _finish(
    source: SourceInfo, rows: list[ParsedRow], issues: list[RowIssue], skipped: int
) -> ImportDocument:
    seen: dict[str, int] = {}
    for row in rows:
        if row.import_key in seen:
            issues.append(RowIssue(
                row=row.source_row, message=f"same import key as row {seen[row.import_key]}; "
                "give each row a distinct Company/Role or an explicit import key"))
        else:
            seen[row.import_key] = row.source_row
    source_rows = [r.source_row for r in rows]
    for number in sorted({n for n in source_rows if source_rows.count(n) > 1}):
        issues.append(RowIssue(row=number, column="sourceRow", message="duplicate sourceRow"))
    return ImportDocument(source=source, rows=tuple(rows), issues=tuple(issues),
                          skipped_blank_rows=skipped)


def _failed(source: SourceInfo, message: str) -> ImportDocument:
    return ImportDocument(source=source, rows=(), issues=(RowIssue(row=None, message=message),))
