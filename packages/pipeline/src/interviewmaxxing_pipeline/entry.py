"""Map a pipeline card to the canonical core ``PipelineEntry`` (D0)."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import ValidationError

from interviewmaxxing_core import PipelineEntry

from .fields import FIELD_NAMES, HEADER_BY_NAME, TrackingFields
from .lanes import BoardLanes
from .models import PipelineItem


class EntryUnavailable(ValueError):
    """The card cannot be expressed as a ``PipelineEntry`` (e.g. no role or listing)."""


def _cell(value: Any) -> str:
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def imported_cells(values: TrackingFields) -> dict[str, str]:
    """Nonblank cells as text, keyed by workbook header (``"Stage"``, ``"Fit / 10"``)."""
    return {
        HEADER_BY_NAME[name]: _cell(values.value(name))
        for name in FIELD_NAMES if values.value(name) is not None
    }


def to_pipeline_entry(item: PipelineItem, lanes: BoardLanes) -> PipelineEntry:
    """The core ``PipelineEntry`` for ``item``.

    * ``stage`` is the label of the card's current board lane (the user's column),
      or the lane id when the lane is no longer configured.
    * ``title``/``company`` are the card's current Role/Company.
    * ``imported_values`` are the card's *originally* imported nonblank cells
      (``provenance.initial``), including the raw Stage and Status, keyed by workbook
      header; empty for manual cards. Later edits never change them.
    * ``next_action_due`` stays a calendar ``date``; no time of day is invented.

    Raises ``EntryUnavailable`` when the card has neither a Role nor a listing,
    since ``PipelineEntry`` needs a title or listing id."""
    lane = lanes.get(item.lane)
    provenance = item.provenance
    try:
        return PipelineEntry(
            id=item.id,
            candidate_id=item.candidate_id,
            listing_id=item.listing_id,
            title=item.tracking.role,
            company=item.tracking.company,
            stage=lane.label if lane else item.lane,
            notes=item.notes,
            next_action=item.tracking.next_action,
            next_action_due=item.next_action_due,
            application_id=item.application_id,
            selection_id=item.selection_id,
            import_source=provenance.source_name if provenance else None,
            imported_values=imported_cells(provenance.initial) if provenance else {},
            created_at=item.created_at,
            updated_at=item.updated_at,
        )
    except ValidationError as exc:
        raise EntryUnavailable(
            f"card {item.id} cannot be a PipelineEntry: "
            + "; ".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())
        ) from exc
