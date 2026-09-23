"""The minimal evidence a selection uses, and what of it is sent to Jev.

Inputs are core D0 contracts (``JobListing``, ``SelectionPreferences``) and the
candidate's verified profile. This module projects them onto:

* ``job_evidence`` — every listing field the decision depends on (hashed as
  ``job_evidence_hash``); ``jev_listing_view`` is the part sent to Jev (stated pay as
  text only: numeric comparisons are done by code).
* ``CandidateEvidence`` — verified qualifications only (hashed as
  ``candidate_evidence_hash``), with the candidate's identity and employers kept out.
* ``jev_preferences_view`` — the preferences Jev needs to judge fit.

Nothing is inferred: missing listing data stays ``None``/``UNKNOWN``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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

CANDIDATE_PLACEHOLDER = "[candidate]"
EMPLOYER_PLACEHOLDER = "[employer]"
INSTITUTION_PLACEHOLDER = "[institution]"


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
        "role_focus": preferences.role_focus,
        "representative_titles": preferences.target_titles,
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
    """Verified qualifications only.

    Contact details (email, phone, URLs, street address), the candidate's names and
    employer/institution names never reach the provider: :meth:`from_profile` drops
    contact values and replaces names with ``[candidate]``, ``[employer]`` or
    ``[institution]`` inside free-text values. Pay history, protected attributes,
    saved answers and generated answers are never included.
    """

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
        compensation-history or employer data, or if its value contains an email,
        phone, URL, street address or postal code. Inside remaining text values the
        candidate's first, last, preferred and full names become ``[candidate]`` and
        the names of every employer and institution in the profile become
        ``[employer]`` / ``[institution]``, so the claim survives without the identity;
        a value left with nothing but placeholders is dropped. Experience keeps only
        title (scrubbed the same way) and dates; education keeps degree, field and
        year. Entries must reference at least one verified fact.
        """
        verified = profile.verified_only()
        scrubber = IdentityScrubber.for_profile(profile)
        facts: dict[str, Any] = {}
        for fact in verified.facts:
            if _EXCLUDED_FACT_KEYS.search(fact.key):
                continue
            value = scrubber.scrub_value(fact.value)
            if value is not None:
                key = scrubber.scrub(fact.key)
                if key is not None:
                    facts[key] = value
        experience: list[dict[str, Any]] = []
        for entry in verified.experience:
            if not entry.fact_ids:
                continue
            title = scrubber.scrub(entry.title)
            if title is not None:
                experience.append(
                    {
                        "title": title,
                        "start": scrubber.scrub_value(entry.start),
                        "end": scrubber.scrub_value(entry.end),
                        "current": entry.current,
                    }
                )
        education = [
            {"degree": scrubber.scrub_value(e.degree),
             "field_of_study": scrubber.scrub_value(e.field_of_study),
             "graduation": scrubber.scrub_value(e.graduation)}
            for e in verified.education
            if e.fact_ids and (scrubber.scrub_value(e.degree) or scrubber.scrub_value(e.field_of_study))
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
    r"ssn|social_security|salary|compensation|pay|wage|company|employer|institution)",
    re.IGNORECASE,
)

_CONTACT = re.compile(
    r"[^@\s]+@[^@\s]+\.[a-z]{2,}"  # email
    r"|https?://|www\.|linkedin\.com|github\.com"  # profile URLs
    r"|(?:\+?1[\s.-]?)?\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b",  # phone
    re.IGNORECASE,
)

_LEGAL_SUFFIX = re.compile(
    r"[\s,]+(?:co|inc|llc|l\.l\.c|ltd|plc|corp|corporation|company|incorporated|limited|"
    r"gmbh|group|holdings|lp|llp|pty|ag|sa|bv|nv)\.?$",
    re.IGNORECASE,
)
_PLACEHOLDERS = re.compile(r"\[(?:candidate|employer|institution)\]")


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in (*value.keys(), *value.values()) for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


def _phrase(text: str) -> re.Pattern[str]:
    """Case-insensitive whole-phrase match (not inside a longer word)."""
    phrase = r"[\s_]+".join(re.escape(part) for part in text.split())
    return re.compile(rf"(?<![^\W_]){phrase}(?![^\W_])", re.IGNORECASE)


def _name_variants(name: str) -> list[str]:
    """A company name with and without its legal suffix, when the remainder is still
    distinctive (at least two words or five characters)."""
    variants = [name]
    stripped = name
    while True:
        shorter = _LEGAL_SUFFIX.sub("", stripped).strip(" ,.")
        if shorter == stripped or not shorter:
            break
        stripped = shorter
        if len(stripped.split()) >= 2 or len(stripped) >= 5:
            variants.append(stripped)
    return variants


@dataclass(frozen=True, slots=True)
class IdentityScrubber:
    """Removes a candidate's identity from free text before it reaches the provider."""

    contact: tuple[str, ...]
    """Casefolded contact strings (email, phone, URLs, street, postal code); a value
    containing any of them is dropped."""
    contact_digits: tuple[str, ...]
    """The phone number's digits, with and without its country code, matched against
    the value's digits."""
    names: tuple[re.Pattern[str], ...]
    employers: tuple[re.Pattern[str], ...]
    institutions: tuple[re.Pattern[str], ...]

    @classmethod
    def for_profile(cls, profile: CandidateProfile) -> IdentityScrubber:
        who = profile.identity
        contact = [
            who.email,
            who.phone,
            who.linkedin_url,
            who.website_url,
            who.github_url,
            who.address.street,
            who.address.postal_code,
        ]
        digits = re.sub(r"\D", "", who.phone or "")
        names = [who.full_name, who.first_name, who.last_name, who.preferred_name]
        if who.preferred_name:
            names.append(f"{who.preferred_name} {who.last_name}")
        employers = [v for e in profile.experience for v in _name_variants(e.company)]
        institutions = [v for e in profile.education for v in _name_variants(e.institution)]
        for fact in profile.facts:
            if isinstance(fact.value, str):
                if re.search(r"company|employer", fact.key, re.IGNORECASE):
                    employers.extend(_name_variants(fact.value))
                elif re.search(r"institution", fact.key, re.IGNORECASE):
                    institutions.extend(_name_variants(fact.value))
        return cls(
            contact=tuple(" ".join(v.split()).casefold() for v in contact if v and len(v.strip()) >= 3),
            contact_digits=tuple(sorted({digits, digits[-10:]})) if len(digits) >= 7 else (),
            names=tuple(_phrase(n) for n in names if n and len(n.strip()) >= 2),
            employers=tuple(_phrase(n) for n in employers if len(n.strip()) >= 2),
            institutions=tuple(_phrase(n) for n in institutions if len(n.strip()) >= 2),
        )

    def scrub(self, text: str) -> str | None:
        """``text`` with identity replaced by placeholders, or ``None`` when it carries
        contact details or nothing but identity."""
        text = " ".join(text.split())
        folded = text.casefold()
        if _CONTACT.search(text) or any(c in folded for c in self.contact):
            return None
        value_digits = re.sub(r"\D", "", text)
        if any(d in value_digits for d in self.contact_digits):
            return None
        out = text
        for pattern in self.employers:
            out = pattern.sub(EMPLOYER_PLACEHOLDER, out)
        for pattern in self.institutions:
            out = pattern.sub(INSTITUTION_PLACEHOLDER, out)
        for pattern in self.names:
            out = pattern.sub(CANDIDATE_PLACEHOLDER, out)
        if not re.search(r"[A-Za-z0-9]", _PLACEHOLDERS.sub("", out)):
            return None
        return " ".join(out.split())

    def scrub_value(self, value: Any) -> Any:
        """A fact value with identity removed; ``None`` when nothing usable remains."""
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, list | tuple):
            kept = [s for s in (self.scrub(v) for v in value if isinstance(v, str)) if s]
            return kept or None
        if isinstance(value, bool | int | float):
            return value
        return None
