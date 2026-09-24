"""An editable contact answer map backed by the existing candidate identity.

This is an import/export format, not a second runtime answer store. Jev still
classifies the question's meaning and subject before the resolver reads the
canonical profile. No aliases here can authorize a different person's answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Self

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from interviewmaxxing_core import (
    AnswerScope,
    CandidateIdentity,
    CandidateProfile,
    PostalAddress,
    SavedAnswer,
    SemanticType,
    new_id,
)

from .answers import question_key, value_key

_CONTACT_KEYS = frozenset({
    "first_name", "last_name", "preferred_name", "email", "phone", "linkedin_url",
    "website_url", "github_url", "street_address", "city", "state", "postal_code", "country",
})
_REUSABLE_QUESTIONS: dict[str, tuple[SemanticType | None, str]] = {
    "gender": (SemanticType.EEO_GENDER, "Gender"),
    "where_are_you_based": (SemanticType.LOCATION, "Where are you based?"),
    "requires_visa_sponsorship": (
        SemanticType.SPONSORSHIP,
        "Will you now or in the future require visa sponsorship for employment?",
    ),
    "referral_source": (
        SemanticType.REFERRAL_SOURCE, "Where did you hear about us?",
    ),
    "referred_by_current_employee": (
        None, "Were you referred to this position by a current employee?",
    ),
    "above_age_18": (None, "Are you above the age of 18?"),
    "authorized_to_work_us": (
        SemanticType.WORK_AUTHORIZATION, "Are you currently authorized to work in the US?",
    ),
    "school": (SemanticType.UNIVERSITY, "School"),
    "degree": (SemanticType.DEGREE, "Degree"),
    "hispanic_latino": (SemanticType.EEO_RACE_ETHNICITY, "Are you Hispanic/Latino?"),
    "veteran_status": (SemanticType.EEO_VETERAN_STATUS, "Veteran Status"),
    "education_discipline": (None, "Education Discipline"),
    "education_start_date": (None, "Education start date"),
    "education_end_date": (None, "Education end date"),
}
_REUSABLE_PHRASES = {
    "referral_source": [
        "Where did you first hear about the company?",
        "Where did you first hear about company?",
        "How did you hear about us?",
        "Where did you first hear about us?",
        "How did you hear about this job listing?",
        "How did you hear about this job?",
        "How did you hear about this position?",
    ],
    # Exact wordings observed on live Lever, Greenhouse and Rippling forms (2026-09-24).
    "requires_visa_sponsorship": [
        "Will you now or in the future require sponsorship for employment visa status "
        "(e.g. E, F-1 STEM OPT, H-1B, J-1, L-1, O-1, TN)",
        "Will you now or in the future require sponsorship for employment authorization "
        "(for example, H-1B visa status)",
        "Will you now or in the future require employer sponsorship for an employment visa "
        "or work authorization",
        "Will you now or in the future require sponsorship for employment visa status?",
        "Will you now or in the future require visa sponsorship?",
        "Do you now or will you in the future require sponsorship for employment visa status?",
    ],
    "above_age_18": ["Are you at least 18 years old?", "Are you 18 years of age or older?"],
    "authorized_to_work_us": [
        "Are you currently authorized to work in the United States?",
        "Are you legally authorized to work in the United States?",
        "Are you legally authorized to work in the US?",
        "Are you authorized to work in the United States?",
        "Are you authorized to work lawfully in the United States?",
    ],
    "school": ["University", "College or university", "School name"],
    "degree": ["Degree type"],
    "hispanic_latino": ["Are you Hispanic or Latino?"],
    "veteran_status": ["Protected veteran status"],
    "education_discipline": ["Field of study", "Major\nEducation", "Discipline\nEducation"],
    "education_start_date": ["Start date\nEducation"],
    "education_end_date": ["End date\nEducation"],
}
_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


class SimpleAnswers(BaseModel):
    """All keys are present in a complete snapshot; null means no stored answer.

    A blank template is valid, but importing requires first name, last name and
    email. Contact nulls clear an optional identity value. Additional answer nulls
    mean no new saved answer; they never delete a previously confirmed answer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    first_name: str | None
    last_name: str | None
    preferred_name: str | None
    email: str | None
    phone: str | None
    linkedin_url: str | None
    website_url: str | None
    github_url: str | None
    street_address: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country: str | None
    gender: str | None = None
    where_are_you_based: str | None = None
    requires_visa_sponsorship: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "requires_visa_sponsorship",
            "will_you_now_or_in_the_future_require _visa_sponsorship_for_employment",
            "will_you_now_or_in_the_future_require_visa_sponsorship_for_employment",
        ),
    )
    referral_source: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "referral_source", "Where_did_you_first_hear_about_company",
            "where_did_you_first_hear_about_company",
        ),
    )
    referred_by_current_employee: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "referred_by_current_employee",
            "Were_you_referred_to_this_position_by_a_current_employee",
            "were_you_referred_to_this_position_by_a_current_employee",
        ),
    )
    above_age_18: str | None = Field(
        default=None,
        validation_alias=AliasChoices("above_age_18", "are_you_above_the_age_of_18"),
    )
    authorized_to_work_us: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "authorized_to_work_us", "Are_you_currently_authorized_to_work_in_the_US",
            "are_you_currently_authorized_to_work_in_the_US",
        ),
    )
    school: str | None = Field(default=None, validation_alias=AliasChoices("school", "School"))
    degree: str | None = Field(default=None, validation_alias=AliasChoices("degree", "Degree"))
    hispanic_latino: str | None = None
    veteran_status: str | None = None
    education_discipline: str | None = None
    education_start_date: str | None = None
    education_end_date: str | None = None

    @field_validator("*", mode="after")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        return (value.strip() or None) if value is not None else None

    @field_validator(
        "requires_visa_sponsorship", "referred_by_current_employee", "above_age_18",
        "authorized_to_work_us", "hispanic_latino", mode="after",
    )
    @classmethod
    def _yes_or_no(cls, value: str | None) -> str | None:
        if value is None:
            return None
        choices = {"yes": "Yes", "no": "No"}
        if value.casefold() not in choices:
            raise ValueError('Use "Yes", "No", or null for this answer.')
        return choices[value.casefold()]

    @field_validator("education_start_date", "education_end_date", mode="after")
    @classmethod
    def _education_month(cls, value: str | None) -> str | None:
        if value is None:
            return None
        for pattern in ("%Y-%m", "%B %Y", "%b %Y"):
            try:
                return datetime.strptime(value, pattern).strftime("%Y-%m")
            except ValueError:
                pass
        raise ValueError('Use YYYY-MM or a month and year, such as "August 2017".')

    @model_validator(mode="after")
    def _education_date_order(self) -> Self:
        if (self.education_start_date is not None and self.education_end_date is not None
                and self.education_end_date < self.education_start_date):
            raise ValueError("Education end date cannot be before education start date.")
        return self

    @classmethod
    def from_identity(cls, identity: CandidateIdentity) -> Self:
        return cls(
            first_name=identity.first_name,
            last_name=identity.last_name,
            preferred_name=identity.preferred_name,
            email=identity.email,
            phone=identity.phone,
            linkedin_url=identity.linkedin_url,
            website_url=identity.website_url,
            github_url=identity.github_url,
            street_address=identity.address.street,
            city=identity.address.city,
            state=identity.address.region,
            postal_code=identity.address.postal_code,
            country=identity.address.country,
        )

    @classmethod
    def from_profile(cls, profile: CandidateProfile) -> Self:
        """Export the map's exact question scopes, without borrowing job answers."""
        data = cls.from_identity(profile.identity).model_dump()
        for key, (semantic, question) in _REUSABLE_QUESTIONS.items():
            prototype = SavedAnswer(
                id="map_export", scope=AnswerScope.GLOBAL, semantic_type=semantic, question=question,
                value="", confirmed_at=profile.identity.verified_at,
            )
            matching = [a for a in profile.saved_answers if question_key(a) == question_key(prototype)]
            if not matching:
                continue
            latest = max(a.confirmed_at for a in matching)
            newest = [a for a in matching if a.confirmed_at == latest]
            if len({value_key(a.value) for a in newest}) != 1:
                raise ValueError(f"Resolve conflicting saved answers for {key} before export.")
            value = newest[0].value
            if isinstance(value, str):
                data[key] = value
        return cls.model_validate(data)

    def saved_answer_updates(
        self, *, confirmed_at: datetime, current: Sequence[SavedAnswer] = ()
    ) -> list[SavedAnswer]:
        """User-supplied defaults, retaining exact wording and job boundaries.

        Validate every answer before the caller writes. Repeated identical imports
        are no-ops. A later changed answer uses normal saved-answer reconciliation.
        """
        pending: list[SavedAnswer] = []
        specs: list[tuple[SemanticType | None, str, str | None, list[str]]] = [
            (semantic, question, getattr(self, key), _REUSABLE_PHRASES.get(key, []))
            for key, (semantic, question) in _REUSABLE_QUESTIONS.items()
        ]
        # Common education controls split a supplied YYYY-MM into month and year.
        # Keep education in every wording; bare Month/Year/Start date is ambiguous.
        for boundary in ("start", "end"):
            date_value = getattr(self, f"education_{boundary}_date")
            if date_value is None:
                continue
            year, month = date_value.split("-")
            for part, value in (("month", _MONTH_NAMES[int(month) - 1]), ("year", year)):
                specs.append((None, f"Education {boundary} {part}", value, [
                    f"Education {boundary} date {part}",
                    f"{boundary.title()} {part}\nEducation",
                    f"{boundary.title()} date {part}\nEducation",
                    f"{part.title()}\nEducation {boundary} date",
                ]))
        for semantic, question, value, phrases in specs:
            if value is None:
                continue
            answer = SavedAnswer(
                id=new_id("simple_answer"),
                scope=AnswerScope.GLOBAL,
                semantic_type=semantic, question=question, value=value,
                match_phrases=phrases,
                confirmed_at=confirmed_at,
            )
            matching = [a for a in current if question_key(a) == question_key(answer)]
            latest = max((a.confirmed_at for a in matching), default=None)
            newest = [a for a in matching if a.confirmed_at == latest]
            if newest and all(
                value_key(a.value) == value_key(answer.value)
                and set(answer.match_phrases) <= set(a.match_phrases) for a in newest
            ):
                continue
            pending.append(answer)
        return pending

    def to_identity(
        self, *, confirmed_at: datetime, current: CandidateIdentity | None = None
    ) -> CandidateIdentity:
        """Convert user-confirmed values; preserve the timestamp of an unchanged map."""
        required = ("first_name", "last_name", "email")
        missing = [key for key in required if getattr(self, key) is None]
        if missing:
            raise ValueError("Fill these keys before import: " + ", ".join(missing))
        if current is not None:
            contact = self.model_dump(include=set(_CONTACT_KEYS))
            if contact == type(self).from_identity(current).model_dump(include=set(_CONTACT_KEYS)):
                return current
        # Missing required values were rejected above; there are no placeholders.
        assert self.first_name is not None and self.last_name is not None
        assert self.email is not None
        return CandidateIdentity(
            first_name=self.first_name,
            last_name=self.last_name,
            preferred_name=self.preferred_name,
            email=self.email,
            phone=self.phone,
            linkedin_url=self.linkedin_url,
            website_url=self.website_url,
            github_url=self.github_url,
            address=PostalAddress(
                street=self.street_address,
                city=self.city,
                region=self.state,
                postal_code=self.postal_code,
                country=self.country,
            ),
            verified_at=confirmed_at,
        )
