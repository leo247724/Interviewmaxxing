"""Pipeline tracker routes over the P1 ``PipelineStore``.

A pipeline card is the user's own tracking record. Moving it never submits and never
changes an application; the only thing that can show a submitted state or receipt is
the linked canonical application (``application``), read from ``ApplicationStore``.

Import is two-step. ``preview`` parses the uploaded text with P1, keeps the parsed
document in memory under an opaque ``previewId`` (bounded count, 30 minute expiry,
never written to disk or logs) and returns P1's plan. ``commit`` applies exactly that
document with its digest, all or nothing.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from interviewmaxxing_core import ApplicationStore, LocalPaths, NotFound
from interviewmaxxing_pipeline import (
    FIELD_KEYS,
    REFERENCE_FIELDS,
    ImportDocument,
    ImportFileError,
    ImportRejected,
    ItemNotFound,
    LaneError,
    NewPipelineItem,
    PipelineItem,
    PipelineStore,
    PipelineUpdate,
    RevisionConflict,
    StageChange,
    TrackingFields,
    parse_import,
)

from . import errors
from .discovery_models import (
    ImportCounts,
    ImportInput,
    ImportPreviewRow,
    ImportPreviewView,
    ImportReceiptView,
    ImportRowError,
    LinkedApplicationView,
    LinkedSelectionView,
    PipelineBoardView,
    PipelineEntryInput,
    PipelineEntryView,
    PipelineHistoryItem,
    PipelineLaneView,
    PipelineMoveInput,
    PipelineProvenanceView,
    PipelineUpdateInput,
)
from .views import SAFE_ID, iso

PREVIEW_TTL_S = 30 * 60
MAX_PREVIEWS = 8
_NAME_TO_KEY = {name: key for _header, key, name in REFERENCE_FIELDS}
_KEY_TO_HEADER = {key: header for header, key, _name in REFERENCE_FIELDS}
_HEADER_TO_KEY = {header: key for header, key, _name in REFERENCE_FIELDS}

SelectionLookup = Callable[[str], LinkedSelectionView | None]


@dataclass
class _Preview:
    candidate_id: str
    document: ImportDocument
    file_name: str
    created: float


def _field_errors(exc: ValidationError, *, prefix_map: Mapping[str, str] | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    for err in exc.errors():
        loc = [str(p) for p in err["loc"] if not isinstance(p, int)]
        raw = loc[0] if loc else "fields"
        key = _NAME_TO_KEY.get(raw, raw)
        if prefix_map:
            key = prefix_map.get(key, key)
        message = str(err.get("msg", "Check this value.")).removeprefix("Value error, ")
        out.setdefault(key, message[:1].upper() + message[1:])
    return out


def _tracking(fields: Mapping[str, Any]) -> TrackingFields:
    unknown = sorted(set(fields) - set(FIELD_KEYS))
    if unknown:
        raise errors.invalid(
            "Some fields aren't pipeline fields.",
            {k: "This field isn't part of the pipeline." for k in unknown},
        )
    try:
        return TrackingFields.model_validate(dict(fields))
    except ValidationError as exc:
        raise errors.invalid("Some fields are not valid.", _field_errors(exc)) from exc


class PipelineApi:
    def __init__(
        self,
        paths: LocalPaths,
        candidate_id: str,
        *,
        selection_lookup: SelectionLookup | None = None,
        listing_exists: Callable[[str], bool | None] | None = None,
    ) -> None:
        self.paths = paths
        self.candidate_id = candidate_id
        self.selection_lookup = selection_lookup
        self.listing_exists = listing_exists
        self._previews: dict[str, _Preview] = {}
        self._previews_lock = threading.Lock()
        self.track_lock = threading.Lock()
        """Serializes "track this listing" so one listing gets one card."""

    @contextmanager
    def store(self) -> Iterator[PipelineStore]:
        store = PipelineStore.from_paths(self.paths)
        try:
            yield store
        finally:
            store.close()

    # --- views ------------------------------------------------------------------------

    def _application(self, apps: ApplicationStore, app_id: str | None) -> LinkedApplicationView | None:
        if not app_id:
            return None
        try:
            app = apps.get_application(app_id)
        except NotFound:
            return None
        if app.candidate_id != self.candidate_id:
            return None
        receipt = apps.get_receipt(app.id)
        submitted = receipt.submitted_at if receipt else app.submitted_at
        return LinkedApplicationView(
            application_id=app.id,
            state=app.state.value,
            submitted_at=iso(submitted) if submitted else None,
            confirmation_reference=receipt.confirmation_reference if receipt else None,
        )

    def _history(self, changes: list[StageChange], labels: Mapping[str, str]) -> list[PipelineHistoryItem]:
        out = []
        for c in changes:
            src, dst = labels.get(c.from_lane or "", c.from_lane), labels.get(c.to_lane or "", c.to_lane)
            if c.kind == "created":
                summary, kind = f"Added to {dst}.", "created"
            elif c.kind == "imported":
                summary, kind = f"Imported into {dst}." if dst else "Imported.", "imported"
            elif c.kind == "moved":
                summary, kind = f"Moved from {src} to {dst}.", "moved"
            else:
                parts = []
                if c.from_stage != c.to_stage:
                    parts.append(f"stage “{c.from_stage or ''}” → “{c.to_stage or ''}”")
                if c.from_status != c.to_status:
                    parts.append(f"status “{c.from_status or ''}” → “{c.to_status or ''}”")
                summary, kind = "Edited " + ("; ".join(parts) or "stage/status") + ".", "edited"
            if c.note:
                summary += f" {c.note}"
            out.append(PipelineHistoryItem(
                at=iso(c.changed_at), kind=kind, summary=summary,
                from_lane=c.from_lane, to_lane=c.to_lane,
            ))
        return out

    def _provenance(
        self, store: PipelineStore, item: PipelineItem, receipts: Mapping[str, str]
    ) -> PipelineProvenanceView | None:
        p = item.provenance
        if p is None:
            return None

        def cells(values: TrackingFields) -> dict[str, str]:
            return {
                _KEY_TO_HEADER[k]: str(v)
                for k, v in values.by_key().items()
                if v is not None and k in _KEY_TO_HEADER
            }

        return PipelineProvenanceView(
            import_id=receipts.get(p.document_sha256, p.import_key),
            file_name=p.source_name,
            source_digest=p.source_sha256 or p.document_sha256,
            source_row=p.source_row or 0,
            imported_at=iso(p.last_imported_at),
            imported_values=cells(p.initial),
            source_id=p.source_id,
            latest_imported_values=cells(p.latest),
            first_imported_at=iso(p.first_imported_at),
            version_count=len(store.source_versions(self.candidate_id, item.id)),
        )

    def entry_view(
        self,
        store: PipelineStore,
        apps: ApplicationStore,
        item: PipelineItem,
        *,
        labels: Mapping[str, str] | None = None,
        receipts: Mapping[str, str] | None = None,
    ) -> PipelineEntryView:
        cid = self.candidate_id
        if labels is None:
            labels = {lane.id: lane.label for lane in store.lanes(cid).lanes}
        if receipts is None:
            receipts = {r.source.document_sha256: r.id for r in store.list_imports(cid)}
        origin = "import" if item.provenance else ("jobs" if item.listing_id else "manual")
        selection = (
            self.selection_lookup(item.selection_id)
            if item.selection_id and self.selection_lookup else None
        )
        return PipelineEntryView(
            id=item.id,
            lane=item.lane,
            revision=item.revision,
            fields=item.tracking.by_key(),
            application_url=item.application_url,
            listing_id=item.listing_id,
            origin=origin,
            application=self._application(apps, item.application_id),
            selection=selection,
            provenance=self._provenance(store, item, receipts),
            history=self._history(store.history(cid, item.id), labels),
            created_at=iso(item.created_at),
            updated_at=iso(item.updated_at),
        )

    @contextmanager
    def _stores(self) -> Iterator[tuple[PipelineStore, ApplicationStore]]:
        with self.store() as store:
            apps = ApplicationStore.open(self.paths.state_db)
            try:
                yield store, apps
            finally:
                apps.close()

    def _item_id(self, item_id: str) -> str:
        if not SAFE_ID.match(item_id):
            raise errors.invalid("That is not a pipeline entry id.")
        return item_id

    # --- routes -------------------------------------------------------------------------

    def board(self) -> PipelineBoardView:
        cid = self.candidate_id
        with self._stores() as (store, apps):
            lanes = store.lanes(cid).lanes
            labels = {lane.id: lane.label for lane in lanes}
            receipts = {r.source.document_sha256: r.id for r in store.list_imports(cid)}
            entries = [
                self.entry_view(store, apps, item, labels=labels, receipts=receipts)
                for item in store.list_items(cid)
            ]
        return PipelineBoardView(
            lanes=[PipelineLaneView(id=lane.id, label=lane.label) for lane in lanes],
            entries=entries,
        )

    def create(self, body: PipelineEntryInput) -> PipelineEntryView:
        tracking = _tracking(body.fields)
        if body.listing_id is not None:
            self._check_listing(body.listing_id)
        try:
            new = NewPipelineItem(
                tracking=tracking, lane=body.lane, application_url=body.application_url,
                listing_id=body.listing_id,
            )
        except ValidationError as exc:
            raise errors.invalid(
                "Some fields are not valid.",
                _field_errors(exc, prefix_map={"application_url": "applicationUrl"}),
            ) from exc
        return self._create(new)

    def _create(self, new: NewPipelineItem) -> PipelineEntryView:
        with self._stores() as (store, apps):
            try:
                item = store.create_item(self.candidate_id, new)
            except LaneError as exc:
                raise errors.invalid("Choose one of the board's lanes.", {"lane": str(exc)}) from exc
            except ValidationError as exc:
                raise errors.invalid("Some fields are not valid.", _field_errors(exc)) from exc
            return self.entry_view(store, apps, item)

    def _check_listing(self, listing_id: str) -> None:
        if not SAFE_ID.match(listing_id):
            raise errors.invalid("That is not a listing id.", {"listingId": "Unknown listing."})
        if self.listing_exists is not None and self.listing_exists(listing_id) is False:
            raise errors.invalid("That listing isn't saved.", {"listingId": "Unknown listing."})

    def update(self, item_id: str, body: PipelineUpdateInput) -> PipelineEntryView:
        item_id = self._item_id(item_id)
        changes: dict[str, Any] = {}
        if body.fields is not None:
            changes["tracking"] = _tracking(body.fields)
        if "application_url" in body.model_fields_set:
            changes["application_url"] = body.application_url
        try:
            update = PipelineUpdate(**changes)
        except ValidationError as exc:
            raise errors.invalid(
                "Some fields are not valid.",
                _field_errors(exc, prefix_map={"application_url": "applicationUrl"}),
            ) from exc
        with self._stores() as (store, apps):
            try:
                item = store.update_item(
                    self.candidate_id, item_id, update, expected_revision=body.revision
                )
            except ItemNotFound as exc:
                raise errors.not_found("No pipeline entry with that id.") from exc
            except RevisionConflict as exc:
                raise errors.conflict(
                    "This entry changed since you opened it. Reload it and make the change again."
                ) from exc
            except ValidationError as exc:
                raise errors.invalid("Some fields are not valid.", _field_errors(exc)) from exc
            return self.entry_view(store, apps, item)

    def move(self, item_id: str, body: PipelineMoveInput) -> PipelineEntryView:
        item_id = self._item_id(item_id)
        with self._stores() as (store, apps):
            try:
                item = store.move_item(
                    self.candidate_id, item_id, body.lane, expected_revision=body.revision
                )
            except ItemNotFound as exc:
                raise errors.not_found("No pipeline entry with that id.") from exc
            except RevisionConflict as exc:
                raise errors.conflict(
                    "This entry changed since you opened it. Reload it and move it again."
                ) from exc
            except LaneError as exc:
                raise errors.invalid("Choose one of the board's lanes.", {"lane": str(exc)}) from exc
            return self.entry_view(store, apps, item)

    # --- import -----------------------------------------------------------------------

    def _prune(self) -> None:
        cutoff = time.monotonic() - PREVIEW_TTL_S
        for key in [k for k, v in self._previews.items() if v.created < cutoff]:
            del self._previews[key]
        while len(self._previews) >= MAX_PREVIEWS:
            oldest = min(self._previews, key=lambda k: self._previews[k].created)
            del self._previews[oldest]

    def preview_import(self, body: ImportInput) -> ImportPreviewView:
        name = body.file_name.strip().replace("\\", "/").rsplit("/", 1)[-1]
        if not name or len(name) > 200 or any(ord(ch) < 32 for ch in name):
            raise errors.invalid("The file name isn't usable.", {"fileName": "Rename the file."})
        if body.source_id is not None and not SAFE_ID.match(body.source_id.strip()):
            raise errors.invalid(
                "The source name isn't usable.",
                {"sourceId": "Use letters, digits, dots, dashes or underscores."},
            )
        try:
            source_id = (body.source_id or "").strip() or None
            document = parse_import(
                body.content.encode(), name=name, format=body.format, source_id=source_id
            )
        except ImportFileError as exc:
            raise errors.invalid(str(exc), {"content": str(exc)}) from exc
        with self.store() as store:
            plan = store.preview_import(self.candidate_id, document)
        by_row: dict[int, list[ImportRowError]] = {}
        for issue in plan.issues:
            column = issue.column or "row"
            field = _HEADER_TO_KEY.get(column, column)
            by_row.setdefault(issue.row or 0, []).append(
                ImportRowError(field=field, message=issue.message)
            )
        parsed = {r.source_row: r.tracking for r in document.rows}
        rows = [
            ImportPreviewRow(
                row_number=r.source_row, action=r.action,
                company=parsed[r.source_row].company if r.source_row in parsed else None,
                role=parsed[r.source_row].role if r.source_row in parsed else None,
                errors=by_row.pop(r.source_row, []),
            )
            for r in plan.rows
        ]
        for row_number, row_errors in sorted(by_row.items()):
            tracking = parsed.get(row_number)
            rows.append(ImportPreviewRow(
                row_number=row_number, action="error",
                company=tracking.company if tracking else None,
                role=tracking.role if tracking else None,
                errors=row_errors,
            ))
        rows.sort(key=lambda r: r.row_number)
        counts = plan.counts()
        preview_id = f"imp_{uuid.uuid4().hex}"
        with self._previews_lock:
            self._prune()
            self._previews[preview_id] = _Preview(
                candidate_id=self.candidate_id, document=document, file_name=name,
                created=time.monotonic(),
            )
        return ImportPreviewView(
            preview_id=preview_id,
            file_name=name,
            source_digest=document.source.document_sha256,
            rows=rows,
            counts=ImportCounts(
                create=counts["create"], update=counts["update"], unchanged=counts["unchanged"],
                error=sum(1 for r in rows if r.action == "error"),
            ),
        )

    def commit_import(self, preview_id: str) -> ImportReceiptView:
        if not SAFE_ID.match(preview_id):
            raise errors.invalid("That is not a preview id.")
        with self._previews_lock:
            self._prune()
            preview = self._previews.get(preview_id)
        if preview is None or preview.candidate_id != self.candidate_id:
            raise errors.not_found("That preview has expired. Preview the file again.")
        document = preview.document
        with self.store() as store:
            try:
                receipt = store.apply_import(
                    self.candidate_id, document,
                    expected_document_sha256=document.source.document_sha256,
                )
            except ImportRejected as exc:
                raise errors.conflict(
                    "The import has rows with problems, so nothing was imported. Fix them "
                    "and preview again.",
                    {
                        f"row {i.row}" if i.row is not None else "file":
                        f"{i.column + ': ' if i.column else ''}{i.message}"
                        for i in exc.issues[:20]
                    },
                ) from exc
        with self._previews_lock:
            self._previews.pop(preview_id, None)
        counts = receipt.counts()
        return ImportReceiptView(
            import_id=receipt.id,
            file_name=receipt.source.name,
            source_digest=receipt.source.document_sha256,
            imported_at=iso(receipt.imported_at),
            created=counts["create"],
            updated=counts["update"],
            unchanged=counts["unchanged"],
        )

    # --- links from jobs ------------------------------------------------------------------

    def item_for_listing(self, listing_id: str) -> PipelineItem | None:
        with self.store() as store:
            return next(
                (i for i in store.list_items(self.candidate_id) if i.listing_id == listing_id),
                None,
            )

    def items_by_listing(self) -> dict[str, PipelineItem]:
        with self.store() as store:
            return {
                i.listing_id: i for i in store.list_items(self.candidate_id) if i.listing_id
            }

    def track(self, new: NewPipelineItem) -> tuple[PipelineItem, bool]:
        """Create a card for a listing unless one exists. Returns ``(item, created)``."""
        assert new.listing_id is not None
        with self.track_lock, self.store() as store:
            existing = next(
                (i for i in store.list_items(self.candidate_id) if i.listing_id == new.listing_id),
                None,
            )
            if existing is not None:
                return existing, False
            return store.create_item(self.candidate_id, new), True
