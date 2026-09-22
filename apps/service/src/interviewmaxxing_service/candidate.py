"""Candidate profile and supplied-resume access, and their presentation.

``CandidateGateway`` is the narrow seam this service needs from the candidate package
(task C2P). ``LocalCandidateGateway`` adapts ``interviewmaxxing_candidate``; tests use
an in-memory implementation. The service never writes profile files itself.

Identity mapping. The frontend edits seven strings. They map onto the canonical
``CandidateIdentity`` without inventing anything:

* ``location`` is the rendering the packet resolver uses for LOCATION questions:
  ``"City, Region, Country"`` (empty parts omitted). An unchanged location keeps the
  stored ``PostalAddress`` exactly (street and postal code included). A new location
  is split on commas: one part is a city, two are city and country, three are city,
  region and country. Anything else is rejected with a field error rather than guessed.
* ``preferred_name`` and ``github_url`` are not edited by the frontend and are kept.
* ``verified_at`` is the time the user confirmed the details on screen. It changes only
  when a value changed; resubmitting identical details keeps the original time.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from interviewmaxxing_core import (
    CandidateIdentity,
    CandidateProfile,
    PostalAddress,
    SavedAnswer,
)

from .models import CandidateProfileInput, CandidateProfileView, CandidateView, ResumeDocumentView
from .views import iso


@dataclass(frozen=True, slots=True)
class ResumeEntry:
    """One supplied resume in the candidate's private catalog."""

    id: str
    filename: str
    size_bytes: int
    uploaded_at: datetime
    sha256: str
    media_type: str


class CandidateSetupError(ValueError):
    """The candidate package rejected an upload or profile write. ``field`` is the
    frontend field the problem belongs to (``resumeFile``, ``resumeId``, ``email``...)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


class CandidateDataInvalid(RuntimeError):
    """The stored profile exists but cannot be read."""


@runtime_checkable
class CandidateGateway(Protocol):
    def load_profile(self, candidate_id: str) -> CandidateProfile | None:
        """The canonical profile, or None when the candidate has not been set up.
        Raises ``CandidateDataInvalid`` when stored data is unreadable."""
        ...

    def list_resumes(self, candidate_id: str) -> list[ResumeEntry]:
        """Supplied resumes, newest first, including the profile's current one."""
        ...

    def store_resume(
        self, candidate_id: str, *, filename: str, content: bytes, media_type: str
    ) -> ResumeEntry: ...

    def upsert_profile(
        self, candidate_id: str, *, identity: CandidateIdentity, resume_id: str
    ) -> CandidateProfile: ...

    def save_answer(self, candidate_id: str, answer: SavedAnswer) -> None: ...


# --- identity mapping -----------------------------------------------------------------


def render_location(address: PostalAddress) -> str:
    return ", ".join(p for p in (address.city, address.region, address.country) if p)


def _parse_location(text: str) -> PostalAddress:
    parts = [" ".join(p.split()) for p in text.split(",")]
    if any(not p for p in parts):
        raise CandidateSetupError("location", "Write the location as “City, Region, Country”.")
    if len(parts) == 1:
        return PostalAddress(city=parts[0])
    if len(parts) == 2:
        return PostalAddress(city=parts[0], country=parts[1])
    if len(parts) == 3:
        return PostalAddress(city=parts[0], region=parts[1], country=parts[2])
    raise CandidateSetupError(
        "location", "Use at most three parts: “City, Region, Country”."
    )


def _blank_to_none(value: str) -> str | None:
    value = value.strip()
    return value or None


_IDENTITY_FIELD_NAMES = {
    "first_name": "firstName",
    "last_name": "lastName",
    "email": "email",
    "phone": "phone",
    "linkedin_url": "linkedinUrl",
    "website_url": "websiteUrl",
}


def identity_from_input(
    data: CandidateProfileInput, *, current: CandidateIdentity | None, now: datetime
) -> CandidateIdentity:
    """The identity the user confirmed. Raises ``CandidateSetupError`` for the first
    field that cannot be represented."""
    location = " ".join(data.location.split())
    if current is not None and location == render_location(current.address):
        address = current.address
    elif location:
        address = _parse_location(location)
    else:
        address = PostalAddress()
    values: dict[str, Any] = {
        "first_name": data.first_name.strip(),
        "last_name": data.last_name.strip(),
        "email": data.email.strip(),
        "phone": _blank_to_none(data.phone),
        "linkedin_url": _blank_to_none(data.linkedin_url),
        "website_url": _blank_to_none(data.website_url),
        "address": address,
        "preferred_name": current.preferred_name if current else None,
        "github_url": current.github_url if current else None,
    }
    if current is not None and all(getattr(current, k) == v for k, v in values.items()):
        return current
    try:
        return CandidateIdentity(**values, verified_at=now)
    except ValidationError as exc:
        loc = exc.errors()[0]["loc"]
        field = _IDENTITY_FIELD_NAMES.get(str(loc[0]) if loc else "", "profile")
        messages = {
            "firstName": "Enter your first name.",
            "lastName": "Enter your last name.",
            "email": "Enter a valid email address.",
        }
        raise CandidateSetupError(field, messages.get(field, "Check this value.")) from exc


def profile_view(identity: CandidateIdentity | None) -> CandidateProfileView:
    if identity is None:
        return CandidateProfileView(
            first_name="", last_name="", email="", phone="", location="",
            linkedin_url="", website_url="",
        )
    return CandidateProfileView(
        first_name=identity.first_name,
        last_name=identity.last_name,
        email=identity.email,
        phone=identity.phone or "",
        location=render_location(identity.address),
        linkedin_url=identity.linkedin_url or "",
        website_url=identity.website_url or "",
    )


def resume_view(entry: ResumeEntry) -> ResumeDocumentView:
    return ResumeDocumentView(
        id=entry.id, file_name=entry.filename, size_bytes=entry.size_bytes,
        uploaded_at=iso(entry.uploaded_at),
    )


def candidate_view(
    profile: CandidateProfile | None, resumes: Sequence[ResumeEntry]
) -> CandidateView:
    ids = {r.id for r in resumes}
    default = profile.resume.id if profile is not None and profile.resume.id in ids else None
    return CandidateView(
        profile=profile_view(profile.identity if profile else None),
        resumes=[resume_view(r) for r in resumes],
        default_resume_id=default,
    )


# --- upload validation ------------------------------------------------------------------

RESUME_MEDIA_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".rtf": "application/rtf",
    ".txt": "text/plain",
    ".odt": "application/vnd.oasis.opendocument.text",
}


def resume_media_type(filename: str) -> str:
    """Media type from the file extension; only document types are accepted."""
    suffix = Path(filename).suffix.lower()
    media = RESUME_MEDIA_TYPES.get(suffix)
    if media is None:
        allowed = ", ".join(sorted(RESUME_MEDIA_TYPES))
        raise CandidateSetupError("resumeFile", f"Upload the resume as one of: {allowed}.")
    return media
