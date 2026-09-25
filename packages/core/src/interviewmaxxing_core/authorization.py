"""The applicant's stated U.S. work authorization status and pay-period option sets.

One closed vocabulary answers every work-authorization and sponsorship variant by
derivation (``DynamicPacketResolver``), instead of matching question wordings."""

from __future__ import annotations

from .candidate import SavedAnswer
from .forms import ApplicationField, ControlType, normalize_text

WORK_AUTHORIZATION_STATUS_QUESTION = "What is your U.S. work authorization status?"
"""The saved question of the stated status (an untyped GLOBAL saved answer), so it can
back both WORK_AUTHORIZATION and SPONSORSHIP answers."""

WORK_AUTHORIZATION_STATUSES: dict[str, str] = {
    "us_citizen": "U.S. citizen",
    "us_permanent_resident": "U.S. lawful permanent resident (green card holder)",
    "asylee": "Asylee (granted asylum in the U.S.)",
    "refugee": "Refugee admitted to the U.S.",
    "daca": "DACA recipient with an Employment Authorization Document",
    "tps": "Temporary Protected Status (TPS) holder with an Employment Authorization Document",
    "pending_adjustment": ("Pending green card application (adjustment of status) with an "
                           "Employment Authorization Document"),
    "dependent_ead": ("Dependent spouse (such as H-4 or L-2) with an Employment Authorization "
                      "Document"),
    "ead_opt": "F-1 student on OPT or STEM OPT with an Employment Authorization Document",
    "h1b": "H-1B visa holder",
    "tn": "TN visa holder",
    "other_visa": "Authorized to work in the U.S. on another visa",
    "not_authorized": "Not authorized to work in the U.S.",
}
"""Closed vocabulary of ``work_authorization_status``: each code and its meaning in the
applicant's own words, which is also what a text question about the status gets (never
the code)."""

WORK_AUTHORIZATION_IMPLICATIONS: dict[str, str] = {
    "us_citizen": ("Authorized to work for any U.S. employer, permanently; never needs an "
                   "employer's sponsorship, now or in the future."),
    "us_permanent_resident": ("Authorized to work for any U.S. employer, permanently; never "
                              "needs an employer's sponsorship, now or in the future."),
    "asylee": ("Authorized to work for any U.S. employer because of that status, with no end "
               "date; never needs an employer's sponsorship, now or in the future; not a "
               "permanent resident."),
    "refugee": ("Authorized to work for any U.S. employer because of that status, with no end "
                "date; never needs an employer's sponsorship, now or in the future; not a "
                "permanent resident."),
    "daca": ("Authorized to work for any U.S. employer while the Employment Authorization "
             "Document is valid; temporary and renewable; needs no employer sponsorship now; "
             "the status does not settle whether sponsorship will be needed in the future."),
    "tps": ("Authorized to work for any U.S. employer while the Employment Authorization "
            "Document is valid; temporary and renewable; needs no employer sponsorship now; "
            "the status does not settle whether sponsorship will be needed in the future."),
    "pending_adjustment": ("Authorized to work for any U.S. employer while the green card "
                           "application is pending; temporary; not yet a permanent resident; "
                           "the status does not settle whether sponsorship is needed, now or "
                           "in the future."),
    "dependent_ead": ("Authorized to work for any U.S. employer while the Employment "
                      "Authorization Document and the principal's status last; temporary; "
                      "needs no sponsorship now; the status does not settle whether "
                      "sponsorship will be needed in the future."),
    "ead_opt": ("Authorized to work now, during OPT or STEM OPT; temporary; needs no sponsorship "
                "to work now, but will need an employer's visa sponsorship (such as H-1B) in "
                "the future, so any question about needing sponsorship now or in the future is "
                "answered Yes."),
    "h1b": ("Authorized to work only for the sponsoring employer; temporary; a new employer "
            "must sponsor an H-1B transfer, so sponsorship is needed now and in the future."),
    "tn": ("Authorized to work only for the employer named in the TN approval; temporary; a "
           "new employer must support a new TN application."),
    "other_visa": ("Authorized to work in the U.S. on a visa that is not stated; the status does "
                   "not settle whether sponsorship is needed."),
    "not_authorized": ("Not authorized to work in the U.S.; would need an employer's sponsorship "
                       "to work in the U.S."),
}
"""What each status settles about work authorization and sponsorship, for the derivation
decision; "not settled" leaves the question to the person."""

STATED_ANSWER_QUESTIONS: dict[str, str] = {
    "authorized_to_work_us": "Are you currently authorized to work in the US?",
    "requires_visa_sponsorship": "Will you now or in the future require visa sponsorship for employment?",
}
"""The person's own two legal answers (simple-answers keys and their saved questions)."""

_NEVER_SPONSORED = {"requires_visa_sponsorship": "Yes", "authorized_to_work_us": "No"}
STATUS_CONTRADICTIONS: dict[str, dict[str, str]] = {
    **{code: _NEVER_SPONSORED
       for code in ("us_citizen", "us_permanent_resident", "asylee", "refugee")},
    **{code: {"authorized_to_work_us": "No"}
       for code in ("daca", "tps", "pending_adjustment", "dependent_ead")},
    "ead_opt": {"requires_visa_sponsorship": "No", "authorized_to_work_us": "No"},
    "h1b": {"requires_visa_sponsorship": "No"},
    "not_authorized": {"authorized_to_work_us": "Yes", "requires_visa_sponsorship": "No"},
}
"""The stated legal answers each status rules out: the importer rejects them together, and
the derivation holds when saved answers (for example from an older import) contradict the
status. TN and another visa settle neither, so nothing is checked for them."""

PERMANENT_STATUSES = frozenset({"us_citizen", "us_permanent_resident"})
"""Statuses that are permanent and never need an employer's sponsorship."""

SPONSORSHIP_UNSETTLED_STATUSES = frozenset({"other_visa"})
"""Statuses from which a sponsorship question is never derived, not even by Jev: which
visa is not stated, so only the person's own answer can say whether they need it."""


def stated_status(answer: SavedAnswer) -> str | None:
    """The status code an untyped saved answer to the status question states, else None."""
    if (answer.semantic_type is None and isinstance(answer.value, str)
            and normalize_text(answer.question) == normalize_text(WORK_AUTHORIZATION_STATUS_QUESTION)
            and answer.value in WORK_AUTHORIZATION_STATUSES):
        return answer.value
    return None


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
