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
    PERMANENT_STATUSES,
    WORK_AUTHORIZATION_STATUS_QUESTION,
    WORK_AUTHORIZATION_STATUSES,
    AnswerScope,
    CandidateFact,
    CandidateIdentity,
    CandidateProfile,
    FactVerification,
    PostalAddress,
    SavedAnswer,
    SemanticType,
    VerificationMethod,
    VerificationStatus,
    new_id,
)

from .answers import question_key, value_key

CAREER_MOTIVATION_KEY = "career_motivation"
"""Two or three sentences the person writes once about what they look for in a role; a
verified fact (not a saved answer) that motivation narratives may cite."""
CAREER_MOTIVATION_FACT_ID = "career_motivation"
CAREER_MOTIVATION_SOURCE = "user:simple-answers"

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
    "previously_employed_here": (None, "Have you previously been employed by this company?"),
    "previously_interviewed_here": (None, "Have you previously interviewed with this company?"),
    "related_to_employee": (None, "Are you related to any current employee of this company?"),
    "willing_to_relocate": (SemanticType.RELOCATION, "Are you willing to relocate?"),
    "open_to_other_positions": (None, "Would you like to be considered for other open positions?"),
    "willing_to_provide_references": (None, "Are you willing to provide references?"),
    "desired_salary": (SemanticType.SALARY_EXPECTATION, "What is your desired salary?"),
    "english_proficiency": (None, "What is your level of proficiency in English?"),
    "available_time_zones": (None, "Which time zones are you available to work in?"),
    "travel_willingness": (None, "How much are you willing to travel for work?"),
    "earliest_start_date": (SemanticType.START_DATE, "What is your earliest start date?"),
    # Round 8: one stated status (closed vocabulary) from which every work-authorization and
    # sponsorship question is derived. Untyped, so it backs answers of both types.
    "work_authorization_status": (None, WORK_AUTHORIZATION_STATUS_QUESTION),
    "race_ethnicity": (SemanticType.EEO_RACE_ETHNICITY, "Race/Ethnicity"),
    "disability_status": (SemanticType.EEO_DISABILITY_STATUS, "Disability Status"),
    "pronouns": (SemanticType.PRONOUNS, "What pronouns do you use?"),
    "family_government_official": (
        None, "Are you or anyone in your immediate family a government official?",
    ),
    "non_compete_agreement": (
        None, "Have you signed any non-competition or non-solicitation agreement that could "
        "restrict your work for this employer?",
    ),
    "uses_ai_tools": (None, "Have you used AI tools to help prepare this application?"),
    "familiar_with_company": (None, "Before applying, how familiar were you with this company?"),
    "county": (None, "County"),
    # Reusable statements (round 7): each is the definition a site's consent or attestation
    # must be fully covered by, with nothing added, before the person's answer is reused.
    "acknowledge_privacy_notice": (
        SemanticType.CONSENT,
        "I have read and understand the employer's applicant privacy notice and data "
        "processing terms.",
    ),
    "certify_information_true": (
        SemanticType.ATTESTATION,
        "The information I provide in this application is true, complete and accurate.",
    ),
    "consent_to_contact": (
        SemanticType.CONSENT, "The employer may contact me about this application.",
    ),
    "consent_reference_checks": (
        SemanticType.CONSENT, "The employer may contact the references I provide.",
    ),
    "consent_background_check": (
        SemanticType.CONSENT, "I consent to a background check, subject to applicable law.",
    ),
}
STATEMENT_KEYS = frozenset({
    "acknowledge_privacy_notice", "certify_information_true", "consent_to_contact",
    "consent_reference_checks", "consent_background_check",
})
"""Consent and attestation statements: reused only when a site's statement is fully covered
by exactly one of them (``DynamicPacketResolver._statement``), never by wording."""
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
    "previously_employed_here": [
        "Have you ever been employed by this company?",
        "Have you worked for this company before?",
        "Have you previously worked for us?",
        "Are you a former employee of this company?",
    ],
    "previously_interviewed_here": [
        "Have you interviewed with us before?",
        "Have you ever interviewed with this company?",
        "Have you interviewed here in the past?",
    ],
    "related_to_employee": [
        "Are you related to anyone who currently works here?",
        "Do you have any relatives currently employed by this company?",
    ],
    "willing_to_relocate": [
        "Are you open to relocation?",
        "Would you be willing to relocate for this role?",
        "Are you willing to relocate for this position?",
    ],
    "open_to_other_positions": [
        "Are you open to being considered for other roles?",
        "May we consider you for other open positions?",
        "Would you like to be considered for other roles at this company?",
    ],
    "willing_to_provide_references": [
        "Can you provide professional references upon request?",
        "Are you able to provide references?",
        "Will you provide references if requested?",
    ],
    "desired_salary": [
        "What are your salary expectations?",
        "Desired salary",
        "Expected salary",
        "What is your expected salary?",
        "What are your compensation expectations?",
    ],
    "english_proficiency": [
        "English proficiency",
        "What is your English proficiency level?",
        "How would you rate your English proficiency?",
        "Level of English",
    ],
    "available_time_zones": [
        "What time zones can you work in?",
        "Time zone availability",
        "Which time zones can you work?",
    ],
    "travel_willingness": [
        "What percentage of travel are you willing to do?",
        "Willingness to travel",
        "How much travel are you willing to do?",
    ],
    "earliest_start_date": [
        "When can you start?",
        "When are you available to start?",
        "Earliest available start date",
        "What is your earliest available start date?",
    ],
    "work_authorization_status": [
        "Work authorization status", "What is your work authorization status?",
        "What is your current U.S. work authorization?",
    ],
    "race_ethnicity": [
        "Race", "What is your race/ethnicity?", "Race and ethnicity", "Ethnicity",
        "Please identify your race",
    ],
    "disability_status": [
        "Do you have a disability?", "Disability", "Voluntary self-identification of disability",
    ],
    "pronouns": ["Pronouns", "Preferred pronouns", "What are your pronouns?"],
    "family_government_official": [
        "Are/were you or anyone in your immediate family a government official?",
        "Have you or an immediate family member ever been a government official?",
    ],
    "non_compete_agreement": [
        "Have you signed any non-competition or non-solicitation agreement?",
        "Are you bound by a non-compete agreement?",
        "Are you subject to any non-compete or non-solicitation agreements?",
    ],
    "uses_ai_tools": [
        "Have you used AI tools (e.g., ChatGPT) in preparing your application?",
        "Did you use AI to help write this application?",
    ],
    "familiar_with_company": [
        "How familiar are you with this company?",
        "Before applying, how familiar were you with our company?",
    ],
    "county": ["County of residence", "What county do you live in?"],
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
    previously_employed_here: str | None = None
    previously_interviewed_here: str | None = None
    related_to_employee: str | None = None
    willing_to_relocate: str | None = None
    open_to_other_positions: str | None = None
    willing_to_provide_references: str | None = None
    desired_salary: str | None = None
    english_proficiency: str | None = None
    available_time_zones: str | None = None
    travel_willingness: str | None = None
    earliest_start_date: str | None = None
    work_authorization_status: str | None = None
    race_ethnicity: str | None = None
    disability_status: str | None = None
    pronouns: str | None = None
    family_government_official: str | None = None
    non_compete_agreement: str | None = None
    uses_ai_tools: str | None = None
    familiar_with_company: str | None = None
    career_motivation: str | None = None
    county: str | None = None
    acknowledge_privacy_notice: str | None = None
    certify_information_true: str | None = None
    consent_to_contact: str | None = None
    consent_reference_checks: str | None = None
    consent_background_check: str | None = None

    @field_validator("*", mode="after")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        return (value.strip() or None) if value is not None else None

    @field_validator(
        "requires_visa_sponsorship", "referred_by_current_employee", "above_age_18",
        "authorized_to_work_us", "hispanic_latino", "previously_employed_here",
        "previously_interviewed_here", "related_to_employee", "willing_to_relocate",
        "open_to_other_positions", "willing_to_provide_references", "family_government_official",
        "non_compete_agreement", "uses_ai_tools", "acknowledge_privacy_notice",
        "certify_information_true", "consent_to_contact", "consent_reference_checks",
        "consent_background_check", mode="after",
    )
    @classmethod
    def _yes_or_no(cls, value: str | None) -> str | None:
        if value is None:
            return None
        choices = {"yes": "Yes", "no": "No"}
        if value.casefold() not in choices:
            raise ValueError('Use "Yes", "No", or null for this answer.')
        return choices[value.casefold()]

    @field_validator("work_authorization_status", mode="after")
    @classmethod
    def _authorization_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        code = value.casefold().replace("-", "_").replace(" ", "_")
        if code not in WORK_AUTHORIZATION_STATUSES:
            raise ValueError("Use one of " + ", ".join(WORK_AUTHORIZATION_STATUSES) + ", or null.")
        return code

    @model_validator(mode="after")
    def _authorization_consistent(self) -> Self:
        """The stated status never contradicts the two stated legal answers."""
        status = self.work_authorization_status
        if status is None:
            return self
        conflicts = []
        if status in PERMANENT_STATUSES:
            if self.requires_visa_sponsorship == "Yes":
                conflicts.append("requires_visa_sponsorship")
            if self.authorized_to_work_us == "No":
                conflicts.append("authorized_to_work_us")
        elif status == "not_authorized":
            if self.authorized_to_work_us == "Yes":
                conflicts.append("authorized_to_work_us")
            if self.requires_visa_sponsorship == "No":
                conflicts.append("requires_visa_sponsorship")
        if conflicts:
            raise ValueError(f"work_authorization_status {status!r} contradicts "
                             + " and ".join(f"{key} {getattr(self, key)!r}" for key in conflicts)
                             + "; correct one of them.")
        return self

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
        statement = profile.find_fact(CAREER_MOTIVATION_FACT_ID)
        if statement is not None and statement.is_verified and isinstance(statement.value, str):
            data[CAREER_MOTIVATION_KEY] = statement.value
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

    def career_motivation_fact(self, *, confirmed_at: datetime,
                               current: CandidateProfile | None = None) -> CandidateFact | None:
        """The ``career_motivation`` statement as a verified, user-stated fact, or None when
        the map has none or the profile already holds the same statement (the verification
        time is then kept). Null never erases an earlier statement."""
        if self.career_motivation is None:
            return None
        existing = current.find_fact(CAREER_MOTIVATION_FACT_ID) if current is not None else None
        if (existing is not None and existing.is_verified and existing.value == self.career_motivation
                and existing.key == CAREER_MOTIVATION_KEY):
            return None
        return CandidateFact(
            id=CAREER_MOTIVATION_FACT_ID, key=CAREER_MOTIVATION_KEY, value=self.career_motivation,
            source=CAREER_MOTIVATION_SOURCE,
            verification=FactVerification(status=VerificationStatus.VERIFIED,
                                          method=VerificationMethod.USER_STATED,
                                          verified_at=confirmed_at),
            evidence=["Written by the applicant in the simple answers map: what they look for in a role"])

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
