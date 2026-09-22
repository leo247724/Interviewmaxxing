"""The result of selecting one listing: the canonical ``JobSelection`` plus audit detail.

``selection`` is the core D0 contract that S1/F3 and the pipeline consume. The other
fields keep what the contract has no place for (focused assessments, per-call model
IDs, the detailed provider failure) so decisions stay reproducible. None of it is an
application submission state or an interview/offer probability.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from interviewmaxxing_core import JobSelection

from .jev import ProviderFailure
from .policy import CompensationStatus, Hold, LocationStatus
from .rubric import Assessment


class SelectionOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    selection: JobSelection
    holds: list[Hold] = Field(default_factory=list)
    """Fine-grained reasons behind ``selection.holds``."""
    assessments: dict[str, Assessment] = Field(default_factory=dict)
    """Jev's focused answers (choice, probabilities, confidence) fed to the final question."""
    compensation: CompensationStatus
    location: LocationStatus
    existing_application_id: str | None = None
    returned_models: list[str] = Field(default_factory=list)
    """Model ID returned by each successful call, in order."""
    provider_generation_ids: list[str] = Field(default_factory=list)
    provider_calls: Annotated[int, Field(ge=0)] = 0
    latency_seconds: Annotated[FiniteFloat, Field(ge=0)] = 0.0
    provider_failure: ProviderFailure | None = None
    cache_key: str

    @property
    def cacheable(self) -> bool:
        """Only complete Jev decisions are reused; failures are retried."""
        return self.selection.model_decision is not None and self.provider_failure is None
