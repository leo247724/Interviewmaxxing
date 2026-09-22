"""Annotated field types (equivalent to core's, which are not public)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, StringConstraints


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
"""Timezone-aware datetime, normalized to UTC; naive datetimes are rejected."""

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
