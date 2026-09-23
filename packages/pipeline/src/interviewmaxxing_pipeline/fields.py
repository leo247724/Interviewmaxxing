"""The 23 reference tracking fields of the user's pipeline workbook.

``TrackingFields`` holds one row of the user's own tracking data. Python names are
snake_case; JSON uses the reference keys (``fitScore``, ``interviewTimeCT`` ...),
which are also accepted on input. Every field is optional: blank (``None``) is kept
distinct from zero, nothing is derived from another column, and free text (stage,
status, notes) is preserved verbatim.
"""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Any, Final, NamedTuple, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from interviewmaxxing_core import Contract

_TIME_24H: Final = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class ReferenceField(NamedTuple):
    header: str
    """The workbook column header, exactly as in the reference and CSV imports."""
    key: str
    """The JSON key (normalized export and API)."""
    name: str
    """The Python attribute on ``TrackingFields``."""


REFERENCE_FIELDS: Final[tuple[ReferenceField, ...]] = (
    ReferenceField("Company", "company", "company"),
    ReferenceField("Role", "role", "role"),
    ReferenceField("Stage", "stage", "stage"),
    ReferenceField("Status", "status", "status"),
    ReferenceField("Priority", "priority", "priority"),
    ReferenceField("Fit / 10", "fitScore", "fit_score"),
    ReferenceField("Next interview date", "nextInterviewDate", "next_interview_date"),
    ReferenceField("Time (CT)", "interviewTimeCT", "interview_time_ct"),
    ReferenceField("Interview format", "interviewFormat", "interview_format"),
    ReferenceField("Work arrangement", "workArrangement", "work_arrangement"),
    ReferenceField("Location / commute", "locationCommute", "location_commute"),
    ReferenceField("Comp low (USD/year)", "compensationLow", "compensation_low"),
    ReferenceField("Comp high (USD/year)", "compensationHigh", "compensation_high"),
    ReferenceField("Comp basis", "compensationBasis", "compensation_basis"),
    ReferenceField("Target assessment", "targetAssessment", "target_assessment"),
    ReferenceField("Source / recruiter", "sourceRecruiter", "source_recruiter"),
    ReferenceField("Follow-up date (suggested)", "suggestedFollowUpDate",
                   "suggested_follow_up_date"),
    ReferenceField("Next action", "nextAction", "next_action"),
    ReferenceField("Last interview date", "lastInterviewDate", "last_interview_date"),
    ReferenceField("Decision due", "decisionDueText", "decision_due_text"),
    ReferenceField("Comp / benefits notes", "compensationBenefitsNotes",
                   "compensation_benefits_notes"),
    ReferenceField("Fit rationale", "fitRationale", "fit_rationale"),
    ReferenceField("Process / source notes", "processSourceNotes", "process_source_notes"),
)
"""The reference schema, in workbook column order."""

FIELD_NAMES: Final[tuple[str, ...]] = tuple(f.name for f in REFERENCE_FIELDS)
FIELD_KEYS: Final[tuple[str, ...]] = tuple(f.key for f in REFERENCE_FIELDS)
FIELD_HEADERS: Final[tuple[str, ...]] = tuple(f.header for f in REFERENCE_FIELDS)
NAME_BY_KEY: Final[dict[str, str]] = {f.key: f.name for f in REFERENCE_FIELDS}
KEY_BY_NAME: Final[dict[str, str]] = {f.name: f.key for f in REFERENCE_FIELDS}
HEADER_BY_NAME: Final[dict[str, str]] = {f.name: f.header for f in REFERENCE_FIELDS}

_TEXT_FIELDS: Final = tuple(
    n for n in FIELD_NAMES
    if n not in {"fit_score", "compensation_low", "compensation_high", "next_interview_date",
                 "suggested_follow_up_date", "last_interview_date", "interview_time_ct"}
)


class TrackingFields(Contract):
    """One pipeline row's reference fields. All optional; ``None`` means blank."""

    model_config = ConfigDict(populate_by_name=True)

    company: str | None = None
    role: str | None = None
    stage: str | None = None
    """The user's stage wording, verbatim (may be a detailed interview description)."""
    status: str | None = None
    """The user's status wording, verbatim."""
    priority: str | None = None
    fit_score: float | None = Field(default=None, alias="fitScore")
    """The user's own score out of 10. Never a Jev confidence or probability."""
    next_interview_date: date | None = Field(default=None, alias="nextInterviewDate")
    interview_time_ct: str | None = Field(default=None, alias="interviewTimeCT")
    """``HH:MM`` (24-hour) in America/Chicago, as the workbook states."""
    interview_format: str | None = Field(default=None, alias="interviewFormat")
    work_arrangement: str | None = Field(default=None, alias="workArrangement")
    location_commute: str | None = Field(default=None, alias="locationCommute")
    """Kept separate from ``work_arrangement``."""
    compensation_low: float | None = Field(default=None, alias="compensationLow")
    """USD per year."""
    compensation_high: float | None = Field(default=None, alias="compensationHigh")
    """USD per year."""
    compensation_basis: str | None = Field(default=None, alias="compensationBasis")
    target_assessment: str | None = Field(default=None, alias="targetAssessment")
    source_recruiter: str | None = Field(default=None, alias="sourceRecruiter")
    """Tracking text only; never used to contact anyone."""
    suggested_follow_up_date: date | None = Field(default=None, alias="suggestedFollowUpDate")
    """A date the user noted; the app does not schedule anything from it."""
    next_action: str | None = Field(default=None, alias="nextAction")
    last_interview_date: date | None = Field(default=None, alias="lastInterviewDate")
    decision_due_text: str | None = Field(default=None, alias="decisionDueText")
    """Free text, deliberately not parsed into a date."""
    compensation_benefits_notes: str | None = Field(default=None,
                                                    alias="compensationBenefitsNotes")
    fit_rationale: str | None = Field(default=None, alias="fitRationale")
    process_source_notes: str | None = Field(default=None, alias="processSourceNotes")

    @field_validator(*_TEXT_FIELDS, mode="before")
    @classmethod
    def _text(cls, value: Any) -> Any:
        """Whitespace-only text is blank; any other text is kept exactly."""
        if isinstance(value, str) and not value.strip():
            return None
        if value is not None and not isinstance(value, str):
            raise ValueError("must be text")
        return value

    @field_validator("fit_score", "compensation_low", "compensation_high", mode="before")
    @classmethod
    def _number(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("must be a number")
        try:
            number = float(value)  # huge integers overflow here instead of later
        except OverflowError:
            raise ValueError("must be a finite number") from None
        if not math.isfinite(number):
            raise ValueError("must be a finite number")
        return number

    @field_validator("fit_score")
    @classmethod
    def _score(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 10:
            raise ValueError("fit score must be between 0 and 10")
        return value

    @field_validator("compensation_low", "compensation_high")
    @classmethod
    def _compensation(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("compensation cannot be negative")
        return value

    @field_validator("next_interview_date", "suggested_follow_up_date", "last_interview_date",
                     mode="before")
    @classmethod
    def _iso_date(cls, value: Any) -> Any:
        if value is None or isinstance(value, date):
            return value
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
            raise ValueError("must be a date as YYYY-MM-DD")
        return value.strip()

    @field_validator("interview_time_ct", mode="before")
    @classmethod
    def _time(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str) or not _TIME_24H.fullmatch(value.strip()):
            raise ValueError("must be a 24-hour time as HH:MM (America/Chicago)")
        return value.strip()

    @model_validator(mode="after")
    def _bounds(self) -> Self:
        low, high = self.compensation_low, self.compensation_high
        if low is not None and high is not None and low > high:
            raise ValueError("compensation low is above compensation high")
        return self

    def by_key(self) -> dict[str, Any]:
        """JSON-ready values under the reference keys (dates as YYYY-MM-DD)."""
        return self.model_dump(mode="json", by_alias=True)

    def value(self, name: str) -> Any:
        return getattr(self, name)

    @property
    def is_identifiable(self) -> bool:
        return bool((self.company or "").strip() or (self.role or "").strip())
