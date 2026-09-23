"""The minimal evidence a selection uses, and what of it is sent to Jev.

Inputs are core D0 contracts (``JobListing``, ``SelectionPreferences``) and the
candidate's verified profile. This module projects them onto:

* ``job_evidence`` — every listing field the decision depends on (hashed as
  ``job_evidence_hash``); ``jev_listing_view`` is the part sent to Jev (stated pay as
  text only: numeric comparisons are done by code).
* ``CandidateEvidence`` — verified qualifications only (hashed as
  ``candidate_evidence_hash``).
* ``jev_preferences_view`` — the preferences Jev needs to judge fit.

Nothing is inferred: missing listing data stays ``None``/``UNKNOWN``.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from interviewmaxxing_core import (
    CandidateProfile,
    DescriptionCompleteness,
    JobListing,
    SelectionPreferences,
)
from interviewmaxxing_core._base import NonEmptyStr

MAX_DESCRIPTION_CHARS = 12_000
"""Descriptions are truncated (and marked incomplete) to bound request size and cost."""


def bounded_description(listing: JobListing) -> tuple[str | None, bool]:
    """``(description, complete)`` as sent: truncated text is never called complete."""
    text = listing.description
    complete = listing.description_completeness is DescriptionCompleteness.FULL
    if text and len(text) > MAX_DESCRIPTION_CHARS:
        return text[:MAX_DESCRIPTION_CHARS], False
    return text, complete


def jev_listing_view(listing: JobListing) -> dict[str, Any]:
    """The listing as Jev sees it: observed fields only, as untrusted data."""
    description, complete = bounded_description(listing)
    pay = listing.compensation
    return {
        "title": listing.title,
        "company": listing.company,
        "location": listing.location,
        "work_arrangement": listing.work_arrangement.value,
        "remote_eligibility_as_stated": listing.remote_eligibility,
        "compensation_as_stated": pay.raw_text if pay else None,
        "description": description,
        "description_complete": complete,
        "source": listing.source,
    }


def job_evidence(listing: JobListing) -> dict[str, Any]:
    """Everything about the listing the decision depends on: the Jev view plus the
    structured fields evaluated by code (pay bounds, status, application URL)."""
    return {
        "listing_id": listing.id,
        "jev_view": jev_listing_view(listing),
        "compensation": listing.compensation.model_dump(mode="json")
        if listing.compensation
        else None,
        "status": listing.status.value,
        "application_url": listing.application_url,
    }


def jev_preferences_view(preferences: SelectionPreferences) -> dict[str, Any]:
    """Preferences Jev needs to judge fit. The pay floor is applied by code only."""
    return {
        "target_titles": preferences.target_titles,
        "onsite_or_hybrid_targets": [
            {"location": t.location, "arrangements": [a.value for a in t.arrangements]}
            for t in preferences.onsite
        ],
        "remote_eligible_region": preferences.remote.eligible_region
        if preferences.remote
        else None,
        "location_priority": preferences.location_priority.value,
        "excluded_keywords": preferences.excluded_keywords,
        "notes": preferences.notes,
    }


class CandidateEvidence(BaseModel):
    """Verified qualifications only. Contact details, protected attributes, pay history,
    employer names and saved/generated answers are never included."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: NonEmptyStr
    verified_facts: dict[str, Any] = Field(default_factory=dict)
    experience: list[dict[str, Any]] = Field(default_factory=list)
    education: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_contact_details(self) -> CandidateEvidence:
        for text in _strings(self.for_jev()):
            if _CONTACT.search(text):
                raise ValueError("candidate evidence must not contain contact details")
        return self

    @property
    def has_qualifications(self) -> bool:
        return bool(self.verified_facts or self.experience or self.education)

    def for_jev(self) -> dict[str, Any]:
        return {
            "verified_facts": self.verified_facts,
            "experience": self.experience,
            "education": self.education,
        }

    @classmethod
    def from_profile(cls, profile: CandidateProfile) -> CandidateEvidence:
        """Project a candidate profile onto selection evidence.

        Only VERIFIED facts are used (resume facts count once the user confirmed them;
        raw resume text, saved answers and generated answers never do). A fact is
        dropped if its key looks like contact, identity, protected-attribute,
        compensation-history or employer data, or if its value contains the
        candidate's name, email, phone, address, profile URLs or any email/phone/URL.
        An experience or education entry is included only if it references at least
        one verified fact, and then without employer names or free-text summaries.
        """
        verified = profile.verified_only()
        needles = _identity_needles(profile)

        def clean(value: Any) -> bool:
            texts = [t.casefold() for t in _strings(value)]
            return not any(_CONTACT.search(t) or any(n in t for n in needles) for t in texts)

        facts = {
            f.key: f.value
            for f in verified.facts
            if not _EXCLUDED_FACT_KEYS.search(f.key) and clean(f.value)
        }
        experience = [
            {"title": e.title, "start": e.start, "end": e.end, "current": e.current}
            for e in verified.experience
            if e.fact_ids and clean(e.title)
        ]
        education = [
            {"degree": e.degree, "field_of_study": e.field_of_study, "graduation": e.graduation}
            for e in verified.education
            if e.fact_ids
        ]
        return cls(
            candidate_id=profile.id,
            verified_facts=facts,
            experience=experience,
            education=education,
        )


_EXCLUDED_FACT_KEYS = re.compile(
    r"(name|email|phone|address|street|postal|zip|linkedin|github|website|birth|"
    r"(?:^|[._])age(?:$|[._])|gender|sex|race|ethnic|veteran|disab|religio|citizen|visa|"
    r"ssn|social_security|salary|compensation|pay|wage|company|employer)",
    re.IGNORECASE,
)

_CONTACT = re.compile(
    r"[^@\s]+@[^@\s]+\.[a-z]{2,}"  # email
    r"|https?://|www\.|linkedin\.com|github\.com"  # profile URLs
    r"|(?:\+?1[\s.-]?)?\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b",  # phone
    re.IGNORECASE,
)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


def _identity_needles(profile: CandidateProfile) -> list[str]:
    """Identity strings that must never reach the provider, casefolded."""
    who = profile.identity
    values = [
        who.full_name,
        who.email,
        who.phone,
        who.linkedin_url,
        who.website_url,
        who.github_url,
        who.address.street,
        who.address.postal_code,
    ]
    if who.preferred_name:
        values.append(f"{who.preferred_name} {who.last_name}")
    return [v.strip().casefold() for v in values if v and len(v.strip()) >= 3]
