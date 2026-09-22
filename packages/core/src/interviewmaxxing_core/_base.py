"""Shared base class, identifiers and time helpers for the canonical contracts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

CONTRACT_VERSION = "1"
"""Bumped whenever a published contract changes incompatibly."""


class Contract(BaseModel):
    """Base for every canonical contract.

    Contracts are immutable value objects: build a changed copy with
    ``model_copy(update=...)``. Unknown fields are rejected so that a typo in a
    downstream producer fails loudly instead of being silently dropped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)


def utc_now() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    """Return a new opaque identifier such as ``app_4f0c...``."""
    return f"{prefix}_{uuid.uuid4().hex}"


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
"""Timezone-aware datetime, normalized to UTC. Naive datetimes are rejected."""

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
"""A string that is not empty after stripping surrounding whitespace."""

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
"""A probability-like score in the closed interval [0, 1]."""
