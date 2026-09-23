# interviewmaxxing-pipeline

This package is the user's local job pipeline tracker. It stores a candidate-scoped
board of cards. Each card carries all 23 columns of the user's pipeline workbook,
an editable board lane, notes, a next-action due date and optional links. It
records the history of lane moves and stage/status edits. It imports the workbook
from validated JSON or CSV, with a preview and a receipt.

**A card is not an application.** Moving a card to "Applied" or "Offer" is the
user's own record:

- It never submits anything.
- It never creates an `ApplicationStore` record or receipt.
- It never counts as a site-confirmed submission.

`application_id` only links an existing canonical application.
`application_url` holds only a URL the user supplied; imports never fill it, and
an employer homepage is not an application URL.

The package depends only on `interviewmaxxing-core`. Storage is its own SQLite
file, `$IMX_HOME/state/pipeline.sqlite3`, separate from the application tables. The
file is readable by its owner only.

## Python API

```python
from interviewmaxxing_core import LocalPaths
from interviewmaxxing_pipeline import (
    PipelineStore, NewPipelineItem, PipelineUpdate, TrackingFields, BoardLanes, load_import,
)

store = PipelineStore.from_paths(LocalPaths.from_env())       # $IMX_HOME/state/pipeline.sqlite3
store = PipelineStore.open(path, *, clock=utc_now, busy_timeout=30.0)   # context manager
```

Use one store per thread, as with SQLite connections. Every method takes the
candidate id first. Ids match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`. An item that
belongs to another candidate raises `ItemNotFound`.

| Method | Returns | Notes |
| --- | --- | --- |
| `list_items(candidate_id, *, lane=None)` | `list[PipelineItem]` | Creation order. |
| `get_item(candidate_id, item_id)` | `PipelineItem` | `ItemNotFound` |
| `create_item(candidate_id, new: NewPipelineItem)` | `PipelineItem` | Revision 1. The lane defaults to `suggest_lane(stage, status)`. |
| `update_item(candidate_id, item_id, update: PipelineUpdate, *, expected_revision: int)` | `PipelineItem` | Partial update: only the fields present change, and `null` clears a field. `tracking` is partial too. A stage/status change is written to the history. Raises `RevisionConflict`. |
| `move_item(candidate_id, item_id, lane: str, *, expected_revision: int, note: str \| None = None)` | `PipelineItem` | Writes a history entry. Raises `LaneError` for an unknown lane and `RevisionConflict`. Moving to the current lane is a no-op. |
| `history(candidate_id, item_id)` | `list[StageChange]` | Append-only, oldest first. Kinds: `created`, `moved`, `stage_edited`, `imported`. Actors: `user`, `import`. |
| `source_versions(candidate_id, item_id)` | `list[SourceVersion]` | Every imported version of the card's source row, oldest first. Append-only. |
| `entry(candidate_id, item_id)` | `interviewmaxxing_core.PipelineEntry` | The card as the core D0 contract (see below). Raises `EntryUnavailable`. |
| `entries(candidate_id)` | `list[PipelineEntry]` | Every card as the core contract. |
| `board(candidate_id)` | `BoardView` | Lanes in order, each with its `PipelineCard`s. |
| `lanes(candidate_id)` | `BoardLanes` | `DEFAULT_BOARD_LANES` until the candidate changes them. |
| `set_lanes(candidate_id, lanes: BoardLanes)` | `BoardLanes` | Rename, reorder, add or remove lanes. `LaneError` if a removed lane still holds cards. |
| `preview_import(candidate_id, document: ImportDocument)` | `ImportPreview` | Writes nothing. |
| `apply_import(candidate_id, document, *, expected_document_sha256: str \| None = None)` | `ImportReceipt` | All or nothing. `ImportRejected(.issues)` if the document has any issue or is not the previewed file. |
| `list_imports(candidate_id)` | `list[ImportReceipt]` | |

Parsing: `load_import(path, *, format=None, source_id=None) -> ImportDocument` and
`parse_import(data: bytes, *, name: str, format=None, source_id=None) -> ImportDocument`.
`source_id` names the logical source (see [Source identity](#source-identity)).
`format` is `"json"` or `"csv"`; by default it comes from the file extension. The
limits are 5 MiB (`MAX_IMPORT_BYTES`) and 5000 rows. An unreadable file (encoding,
size or format) raises `ImportFileError`.

Errors: `PipelineError` is the base class. `ItemNotFound` is also a `LookupError`.
`RevisionConflict` has `.current: PipelineItem` and `.expected`. `LaneError` and
`ImportRejected` (with `.issues: list[RowIssue]`) are also `ValueError`s.
`EntryUnavailable` (a `ValueError`) is raised when a card cannot be a `PipelineEntry`.
Validation errors from invalid input are pydantic `ValidationError`s.

### Models

All models are frozen pydantic models, JSON-serializable with `model_dump(mode="json")`.

- **`TrackingFields`** has the 23 reference fields. Python names are snake_case. JSON
  uses the reference keys (`by_alias=True`, or `.by_key()`), and both forms are
  accepted on input. Every field is optional, and `None` means blank; blank is
  distinct from `0`.
- **`PipelineItem`** has these fields: `id`, `candidate_id`, `lane`, `tracking`,
  `notes`, `next_action_due` (date), `listing_id`, `application_id`, `selection_id`,
  `application_url`, `provenance`, `revision`, `created_at` and `updated_at`.
  - `provenance` is an `ImportProvenance` or None. It holds:
    - the import key and `source_id`, the source file name and format;
    - the document and workbook digests, the source row, and the first and last
      import times;
    - `initial`, the row as first imported, which never changes;
    - `latest`, the row as most recently imported, the base for merging;
    - the suggested lane and rule.
  - `.needs_application_url` is true when there is neither a URL nor an
    application; the user must supply the URL before applying.
- **`NewPipelineItem(tracking, lane=None, notes, next_action_due, listing_id, application_id, selection_id, application_url)`**
- **`PipelineUpdate(tracking=None, notes, next_action_due, listing_id, application_id, selection_id, application_url)`**
  is partial. The lane is changed only with `move_item`.
- **`PipelineCard`** holds what a board card shows: id, lane, revision, company,
  role, stage, status, priority, fit score, compensation low/high/basis, next action
  and due date, application id, `needs_application_url` and `imported`.
- **`BoardView(candidate_id, lanes: [LaneView(id, label, description, cards)])`**
- **`BoardLane(id, label, description)`** and **`BoardLanes(lanes)`**. Lane ids match
  `^[a-z][a-z0-9_-]{0,39}$`. Ids and labels must be distinct.
- **`ImportPreview(candidate_id, source: SourceInfo, rows: [RowPlan], issues: [RowIssue], skipped_blank_rows)`**
  with `.ok` and `.counts()`.
  - `RowPlan` has `source_row`, `import_key`, `action` (`create`, `update` or
    `unchanged`), `item_id` and `lane`. Its `lane_rule`, `updated_fields` and
    `kept_manual_fields` fields use reference keys.
  - `RowIssue(row, column, message)`: `column` is the CSV header or the JSON key, and
    `row` is None for document-level problems.
- **`ImportReceipt(id, candidate_id, imported_at, source, rows)`**
- **`SourceInfo`** gains `source_id` and `source_id_origin` (`explicit`, `declared`,
  `workbook-path`, `file-path` or `legacy`).
- **`SourceVersion(sequence, candidate_id, item_id, source_id, import_id, document_sha256, source_row, imported_at, values)`**

`REFERENCE_FIELDS` lists the 23 fields in workbook column order as `(header, key, name)`
tuples, for building forms and tables. `FIELD_HEADERS` and `FIELD_KEYS` are the same
columns as plain lists.

## The 23 reference fields

| Workbook header | JSON key | Type |
| --- | --- | --- |
| Company | `company` | text |
| Role | `role` | text |
| Stage | `stage` | text, verbatim (may be a detailed interview description) |
| Status | `status` | text, verbatim |
| Priority | `priority` | text |
| Fit / 10 | `fitScore` | number 0-10, the user's own score (never a Jev confidence) |
| Next interview date | `nextInterviewDate` | date `YYYY-MM-DD` |
| Time (CT) | `interviewTimeCT` | `HH:MM` 24-hour, America/Chicago |
| Interview format | `interviewFormat` | text |
| Work arrangement | `workArrangement` | text (separate from commute) |
| Location / commute | `locationCommute` | text |
| Comp low (USD/year) | `compensationLow` | number ≥ 0 |
| Comp high (USD/year) | `compensationHigh` | number ≥ 0, ≥ low when both are set |
| Comp basis | `compensationBasis` | text |
| Target assessment | `targetAssessment` | text |
| Source / recruiter | `sourceRecruiter` | text (tracking only; never contacted) |
| Follow-up date (suggested) | `suggestedFollowUpDate` | date (historical; the app schedules nothing from it) |
| Next action | `nextAction` | text |
| Last interview date | `lastInterviewDate` | date |
| Decision due | `decisionDueText` | text, deliberately not parsed as a date |
| Comp / benefits notes | `compensationBenefitsNotes` | text |
| Fit rationale | `fitRationale` | text |
| Process / source notes | `processSourceNotes` | text |

Nothing is derived from another column:

- Notes and recruiter names are never read to set a date, an outcome or a lane.
- A missing compensation stays missing; a range is never completed from one side.
- The workbook's suggested follow-up date does not become `next_action_due`.

## Lanes

The default lanes are Saved, Applied, Scheduling, Interviewing, Assessment,
Follow-up, Decision, Offer and Closed. Their ids are `saved`, `applied`,
`scheduling`, `interviewing`, `assessment`, `follow-up`, `decision`, `offer` and
`closed`.

`suggest_lane(stage, status, lanes)` places a new card from its Status wording, and
then from its Stage wording if Status does not match. It checks conservative keyword
rules in order, and the first match wins:

1. closed / rejected / declined / withdrawn
2. offer
3. decision
4. assessment / take-home
5. scheduling
6. interview / screen / panel
7. follow-up / awaiting feedback
8. not yet applied
9. applied / submitted
10. saved / interested

A keyword counts only when its own clause states it. Clauses split at `; , . ! ? ( ) / |`,
at dashes between spaces and at "but". A keyword is ignored when a negation is
within a few words of it. For the outcome lanes (Applied, Offer, Closed), it is
also ignored when its clause is a question or hedged ("pending", "possible",
"maybe", "expected", "hoping", "if", "unless" ...). For example:

- "No offer yet" and "Offer not received" are not Offer.
- "Application not submitted" is not Applied.
- "Not rejected; awaiting decision" is Decision.
- "Rejected?" is not Closed.

Status is more current than Stage. When Status negates or hedges an outcome that
Stage asserts, the Stage wording is not used at all, and the card goes to review
with the reason "Status negates or questions what Stage says". Examples: Stage
"Offer" with Status "No offer yet"; Stage "Applied" with "Never applied"; Stage
"Rejected after panel" with "Rejection unlikely". The raw wording is kept either
way.

If nothing matches, the card goes to the first lane, and the suggestion has
`rule=None` and a "review the lane" reason. The raw stage and status wording stay
visible and editable in `tracking`. Their imported values stay in
`provenance.original`.

## Source identity

Every import belongs to one logical source, `source_id`. Cards are keyed by
(candidate, source, import key), so rows from two different files never overwrite
each other, even with the same file name, keys, Company or Role. The source id is
the first of these that exists:

1. The `source_id` argument (CLI `--source-id`). Use the same id for every file that
   updates the same cards, for example a CSV re-export of the workbook.
2. `source.sourceId` in a JSON export.
3. An opaque hash of the export's declared workbook `path`, sheet and table. Editing
   the workbook keeps this identity; a different folder is a different source.
   - An absolute (or `~`) path is used as given.
   - A relative path is resolved against the folder of the export file actually
     loaded, so `A/export.json` and `B/export.json` that both declare
     `Pipeline.numbers` are different sources.
   - Bytes with only a relative path (`parse_import` without a file) have no derived
     identity. They need `source_id` or a declared `sourceId`.
4. For `load_import`, an opaque hash of the resolved file path.

None of these is a content digest, and full paths are never stored. Uploaded bytes
with none of them get a document issue asking for a `source_id`.

Within one source, an import key whose Company/Role changes (case- and
space-insensitive) is rejected as ambiguous, and nothing is written.

## Import

- **JSON** is the normalized workbook export: `{"schemaVersion": 1, "source": {...},
  "records": [...]}`.
  - Each record has all 23 keys (`null` for blank), plus optional `sourceRow` and
    `importKey`.
  - Numbers are JSON numbers and dates are `YYYY-MM-DD`. Numbers that are not
    finite floats (for example `10**400`) are row issues.
  - Duplicate keys, `NaN`/`Infinity`, unknown keys and missing keys are rejected.
  - From `source`, only the file name (never its path), the workbook `sha256`, the
    sheet, the table and `sourceModifiedAt` are recorded.
  - See `examples/pipeline-import.example.json`.
- **CSV** has the 23 workbook headers in any order, plus an optional `Import key`
  column.
  - Cells accept `$120,000`, `YYYY-MM-DD` or `M/D/YYYY`, and `14:30` or `2:30 PM`.
  - Fully blank rows are skipped and counted.
  - A value under a blank header is reported as `column <letter> (blank header)`
    and never silently dropped.
- **Validation.** Issues report the row (the header is row 1) and the column. Any
  issue makes `apply_import` refuse the whole document; there are no partial imports.
- **Idempotence.** Each row's import key (supplied, or derived from Company + Role,
  case-insensitive) gives a stable card id, `imported_item_id(candidate_id, key)`.
  Importing unchanged data again creates nothing and changes nothing.
- **Manual edits survive re-imports.** Each field is merged against
  `provenance.latest`. A value that changed in the source is applied only if the
  user has not edited that field since the latest import. Otherwise the
  user's edit is kept, and the field is listed in `kept_manual_fields`. Re-imports
  never move a card. If a merge would be invalid (for example compensation low above
  high), the whole import is rejected with that row's issue.
- **Nothing is erased.** `provenance.initial` keeps the first imported values
  forever. Every imported version of a row is appended to `source_versions`.
- **Receipts.** Every applied import stores a receipt with the source id, the
  document digest, the workbook digest, the time and the action for each row.

### Command line (package-local)

```bash
interviewmaxxing-pipeline [--db PATH] [--candidate ID] preview FILE [--format json|csv]
interviewmaxxing-pipeline [--db PATH] [--candidate ID] import FILE --expect-sha256 <documentSha256 from preview>
interviewmaxxing-pipeline [--db PATH] [--candidate ID] board
interviewmaxxing-pipeline [--db PATH] [--candidate ID] lanes
```

`--db` defaults to `$IMX_HOME/state/pipeline.sqlite3`, and `--candidate` to
`IMX_CANDIDATE_ID`. Preview and import print row numbers, import keys, actions,
lanes and field names, and never cell values. The exit status is 0 on success, 1
on issues and 2 on a usage or file error.

## Core D0 `PipelineEntry`

`to_pipeline_entry(item, lanes) -> PipelineEntry` (also `store.entry` and
`store.entries`) maps a card to `interviewmaxxing_core.PipelineEntry`:

| `PipelineEntry` field | Value |
| --- | --- |
| `id`, `candidate_id`, `listing_id`, `application_id`, `selection_id`, `notes`, `created_at`, `updated_at` | copied from the card |
| `title` | `tracking.role` |
| `company` | `tracking.company` |
| `stage` | the label of the card's current board lane (the lane id if the lane was removed) |
| `next_action` | `tracking.next_action` |
| `next_action_due` | the card's calendar `date`, unchanged; no midnight is invented |
| `import_source` | `provenance.source_name` |
| `imported_values` | every nonblank cell of `provenance.initial`, as text, keyed by workbook header (`imported_cells`); includes the original Stage and Status; empty for manual cards |

The card's current raw Stage and Status stay in `tracking`. A card with neither a
Role nor a listing raises `EntryUnavailable`, because `PipelineEntry` needs a title
or listing id.

## Storage versions

`SCHEMA_VERSION` is 2. Opening a schema-1 database migrates it in one transaction:

- Imported cards keep their ids, values, lanes and revisions.
- Their single snapshot becomes both `initial` and `latest`.
- They get the source id `legacy_source_id(source_name)`, shown in their provenance,
  and a first source version with import id `legacy-v1`.

To keep updating those cards, import with that `source_id`. Migrating again is a
no-op.

## Tests

```bash
uv pip install --python .venv-task/bin/python -e packages/core -e packages/pipeline pytest
.venv-task/bin/python -m pytest tests/pipeline
IMX_PIPELINE_REFERENCE=/path/to/private/export.json .venv-task/bin/python -m pytest tests/pipeline -k private
```

Fixtures in `tests/fixtures/pipeline/` are fictional. The opt-in `private` test
previews a real export into a temporary database, and prints and writes nothing.
