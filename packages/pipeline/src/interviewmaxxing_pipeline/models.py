"""Pipeline records and UI-facing views.

A ``PipelineItem`` is a candidate's own tracking card: the 23 reference fields
(``tracking``), a board ``lane``, free notes, a next-action due date and optional
links. It is not an application. ``application_id`` only *links* an existing
canonical application record; this package never creates one, never writes a
receipt, and a lane named "Applied" or "Offer" is the user's own record, not a
site-confirmed submission.
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from interviewmaxxing_core import Contract

from ._types import NonEmptyStr, UtcDatetime
from .fields import TrackingFields

_SHA256 = r"^[0-9a-f]{64}$"
ImportFormat = Literal["json", "csv"]
SourceIdOrigin = Literal["explicit", "declared", "workbook-path", "file-path", "legacy"]


def legacy_source_id(source_name: str) -> str:
    """Source id given to cards imported before source scoping (schema 1)."""
    return "legacy-" + hashlib.sha256(source_name.encode()).hexdigest()[:24]


def _check_url(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("must be an absolute http(s) URL")
    return text


class ImportProvenance(Contract):
    """Where an imported card came from.

    ``initial`` is the row as first imported and never changes. ``latest`` is the
    row as most recently imported, the base for merging later imports with the
    user's edits. Every imported version is also kept (``PipelineStore.source_versions``).
    The card's editable ``tracking`` may differ from both."""

    import_key: NonEmptyStr
    source_id: NonEmptyStr
    """The logical source the key belongs to (see ``importer``)."""
    source_name: NonEmptyStr
    """The imported file's name (or the workbook name the export declares)."""
    source_format: ImportFormat
    document_sha256: str = Field(pattern=_SHA256)
    """Digest of the imported file's bytes."""
    source_sha256: str | None = Field(default=None, pattern=_SHA256)
    """Digest of the original workbook, when the export declares one."""
    source_row: int | None = Field(default=None, ge=1)
    """Row number in the source table (the header is row 1)."""
    first_imported_at: UtcDatetime
    last_imported_at: UtcDatetime
    initial: TrackingFields
    latest: TrackingFields
    suggested_lane: str
    lane_rule: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _upgrade_schema_1(cls, data: Any) -> Any:
        """Schema-1 bodies had one ``original`` snapshot and no source id."""
        if isinstance(data, dict) and "original" in data:
            data = dict(data)
            original = data.pop("original")
            data.setdefault("initial", original)
            data.setdefault("latest", original)
            data.setdefault("source_id", legacy_source_id(str(data.get("source_name", ""))))
        return data

class PipelineItem(Contract):
    """One card on a candidate's board."""

    id: NonEmptyStr
    candidate_id: NonEmptyStr
    lane: NonEmptyStr
    tracking: TrackingFields
    notes: str | None = None
    """The user's own notes (separate from the reference notes columns)."""
    next_action_due: date | None = None
    """Set by the user. Imports never fill it (the workbook's suggested follow-up date
    stays in ``tracking``)."""
    listing_id: str | None = None
    """Optional link to a discovered ``JobListing``."""
    application_id: str | None = None
    """Optional link to an existing canonical application. Never created here."""
    selection_id: str | None = None
    application_url: str | None = None
    """The job's application URL, only as the user supplied it. Never invented; an
    employer homepage is not an application URL."""
    provenance: ImportProvenance | None = None
    revision: int = Field(ge=1)
    """Incremented on every change; updates must quote the revision they started from."""
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @field_validator("application_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return _check_url(value)

    @model_validator(mode="after")
    def _identifiable(self) -> Self:
        if not (self.tracking.is_identifiable or self.listing_id):
            raise ValueError("a pipeline item needs a company, a role or a listing")
        return self

    @property
    def needs_application_url(self) -> bool:
        """True when applying would first need the user to supply the URL."""
        return self.application_url is None and self.application_id is None


class NewPipelineItem(Contract):
    """A manually created card."""

    tracking: TrackingFields
    lane: str | None = None
    """Defaults to the lane ``suggest_lane`` gives for the stage/status wording."""
    notes: str | None = None
    next_action_due: date | None = None
    listing_id: str | None = None
    application_id: str | None = None
    selection_id: str | None = None
    application_url: str | None = None

    @field_validator("application_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return _check_url(value)


class PipelineUpdate(Contract):
    """A partial edit. Only fields present in the input change; an explicit
    ``null`` clears a field. ``tracking`` is itself partial (reference keys)."""

    tracking: TrackingFields | None = None
    notes: str | None = None
    next_action_due: date | None = None
    listing_id: str | None = None
    application_id: str | None = None
    selection_id: str | None = None
    application_url: str | None = None

    @field_validator("application_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return _check_url(value)


HistoryKind = Literal["created", "moved", "stage_edited", "imported"]


class StageChange(Contract):
    """One append-only history entry for a card's lane or stage/status wording."""

    sequence: int = Field(ge=1)
    item_id: NonEmptyStr
    candidate_id: NonEmptyStr
    kind: HistoryKind
    actor: Literal["user", "import"]
    from_lane: str | None = None
    to_lane: str | None = None
    from_stage: str | None = None
    to_stage: str | None = None
    from_status: str | None = None
    to_status: str | None = None
    note: str | None = None
    changed_at: UtcDatetime


# --- UI views ---------------------------------------------------------------------------


class PipelineCard(Contract):
    """The fields a board card shows. Full detail is the ``PipelineItem``."""

    id: str
    lane: str
    revision: int
    company: str | None
    role: str | None
    stage: str | None
    status: str | None
    priority: str | None
    fit_score: float | None
    compensation_low: float | None
    compensation_high: float | None
    compensation_basis: str | None
    next_action: str | None
    next_action_due: date | None
    application_id: str | None
    needs_application_url: bool
    imported: bool

    @classmethod
    def of(cls, item: PipelineItem) -> PipelineCard:
        t = item.tracking
        return cls(
            id=item.id, lane=item.lane, revision=item.revision, company=t.company,
            role=t.role, stage=t.stage, status=t.status, priority=t.priority,
            fit_score=t.fit_score, compensation_low=t.compensation_low,
            compensation_high=t.compensation_high, compensation_basis=t.compensation_basis,
            next_action=t.next_action, next_action_due=item.next_action_due,
            application_id=item.application_id,
            needs_application_url=item.needs_application_url,
            imported=item.provenance is not None,
        )


class LaneView(Contract):
    id: str
    label: str
    description: str | None
    cards: list[PipelineCard]


class BoardView(Contract):
    candidate_id: str
    lanes: list[LaneView]


# --- import preview and receipt ---------------------------------------------------------

RowAction = Literal["create", "update", "unchanged"]


class RowIssue(Contract):
    """A precise problem with one source row (or the whole document when ``row`` is
    None). ``column`` is the reference header or JSON key."""

    row: int | None
    column: str | None = None
    message: str


class RowPlan(Contract):
    source_row: int
    import_key: str
    action: RowAction
    item_id: str
    lane: str
    """The lane the card is (or will be) in. Reimports never move a card."""
    lane_rule: str | None
    updated_fields: list[str] = Field(default_factory=list)
    """Reference keys taken from the source because the user had not edited them."""
    kept_manual_fields: list[str] = Field(default_factory=list)
    """Reference keys the source changed but the user had edited: the edit is kept."""


class SourceInfo(Contract):
    name: str
    format: ImportFormat
    source_id: str | None = None
    """Logical source identity; None only when the document has an issue for it."""
    source_id_origin: SourceIdOrigin | None = None
    document_sha256: str
    source_sha256: str | None = None
    sheet: str | None = None
    table: str | None = None
    source_modified_at: str | None = None


class ImportPreview(Contract):
    """What ``apply_import`` would do. Nothing is written by a preview."""

    candidate_id: str
    source: SourceInfo
    rows: list[RowPlan]
    issues: list[RowIssue]
    skipped_blank_rows: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues

    def counts(self) -> dict[str, int]:
        result = {"create": 0, "update": 0, "unchanged": 0}
        for row in self.rows:
            result[row.action] += 1
        return result


class SourceVersion(Contract):
    """One imported version of a card's source row (append-only)."""

    sequence: int = Field(ge=1)
    candidate_id: NonEmptyStr
    item_id: NonEmptyStr
    source_id: NonEmptyStr
    import_id: NonEmptyStr
    """The ``ImportReceipt`` that brought this version (``legacy-v1`` for migrated rows)."""
    document_sha256: str | None = Field(default=None, pattern=_SHA256)
    source_row: int | None = None
    imported_at: UtcDatetime
    values: TrackingFields


class ImportReceipt(Contract):
    """The durable record of an applied import."""

    id: NonEmptyStr
    candidate_id: NonEmptyStr
    imported_at: UtcDatetime
    source: SourceInfo
    rows: list[RowPlan]

    def counts(self) -> dict[str, int]:
        result = {"create": 0, "update": 0, "unchanged": 0}
        for row in self.rows:
            result[row.action] += 1
        return result
