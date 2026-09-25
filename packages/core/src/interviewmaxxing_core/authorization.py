"""The applicant's stated U.S. work authorization status and pay-period option sets.

One closed vocabulary answers every work-authorization and sponsorship variant by
derivation (``DynamicPacketResolver``), instead of matching question wordings."""

from __future__ import annotations

from .forms import ApplicationField, ControlType, normalize_text

WORK_AUTHORIZATION_STATUS_QUESTION = "What is your U.S. work authorization status?"
"""The saved question of the stated status (an untyped GLOBAL saved answer), so it can
back both WORK_AUTHORIZATION and SPONSORSHIP answers."""

WORK_AUTHORIZATION_STATUSES: dict[str, str] = {
    "us_citizen": "U.S. citizen.",
    "us_permanent_resident": "U.S. lawful permanent resident (green card holder).",
    "ead_opt": ("Authorized to work in the U.S. with an Employment Authorization Document, "
                "such as OPT; the authorization is temporary."),
    "h1b": ("H-1B visa holder: authorized to work for a sponsoring employer; a new employer "
            "must sponsor a transfer."),
    "tn": ("TN visa holder (Canadian or Mexican professional): employer-specific and "
           "temporary; a new employer must file for it."),
    "other_visa": "Authorized to work in the U.S. on another visa (which one is not stated).",
    "not_authorized": "Not authorized to work in the U.S.",
}
"""Closed vocabulary of ``work_authorization_status`` and what each code means."""

PERMANENT_STATUSES = frozenset({"us_citizen", "us_permanent_resident"})
"""Statuses that are permanent and never need an employer's sponsorship."""

PAY_PERIOD_LABELS: dict[str, str] = {
    "hourly": "hour", "per hour": "hour", "hour": "hour", "an hour": "hour",
    "daily": "day", "per day": "day", "day": "day",
    "weekly": "week", "per week": "week", "week": "week",
    "biweekly": "biweek", "bi-weekly": "biweek",
    "monthly": "month", "per month": "month", "month": "month",
    "yearly": "year", "annual": "year", "annually": "year", "per year": "year",
    "year": "year", "per annum": "year",
}
"""Pay-period option labels (normalized) and the period each names."""


def pay_period_of(label: str) -> str | None:
    """The period a pay-period option label names ("Yearly" -> "year"), else None."""
    return PAY_PERIOD_LABELS.get(normalize_text(label).rstrip(".:"))


def is_pay_period_choice(field: ApplicationField) -> bool:
    """A single-choice field whose enabled options are all pay periods (at least two):
    the period that goes with a salary, whatever the field's own semantic type."""
    options = [o for o in field.options or [] if not o.disabled and o.value != ""]
    return (field.control_type in (ControlType.SELECT, ControlType.RADIO)
            and len(options) >= 2 and all(pay_period_of(o.label) is not None for o in options))
