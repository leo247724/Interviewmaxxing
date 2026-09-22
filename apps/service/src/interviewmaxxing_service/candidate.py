"""Candidate profile and supplied-resume access, and their presentation.

``CandidateGateway`` is the narrow seam this service needs from the candidate package
(task C2P). ``integration.LocalCandidateGateway`` adapts
``interviewmaxxing_candidate.LocalCandidateStore``; tests use an in-memory
implementation. The service never writes profile or resume files itself.

Identity mapping. The frontend edits seven strings. They map onto the canonical
``CandidateIdentity`` without inventing anything:

* ``location`` is the rendering the packet resolver uses for LOCATION questions:
  ``"City, Region[, Country]"`` (empty parts omitted). An unchanged location keeps the
  stored ``PostalAddress`` exactly (street and postal code included). A new location
  is split on commas: one part is a city, two are city and region (``"Austin, TX"``),
  three are city, region and country. No country is inferred from a region. Anything
  else is rejected with a field error rather than guessed.
* ``preferred_name`` and ``github_url`` are not edited by the frontend and are kept.
* ``verified_at`` is the time the user confirmed the details on screen. It changes only
  when a value changed; resubmitting identical details keeps the original time.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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
    uploaded_at: datetime | None
    """None for a resume an imported profile already referenced (never uploaded)."""
    file_modified_at: datetime | None = None
    """The file's modification time, shown when there is no upload time."""


@dataclass(frozen=True, slots=True)
class CandidateSetupState:
    """What the setup screen shows (C2P ``candidate_setup``). Before setup, identity and
    the selected resume are None; nothing is invented to fill them."""

    identity: CandidateIdentity | None
    resumes: Sequence[ResumeEntry]
    selected_resume_id: str | None
    complete: bool
    """True when the canonical profile loads, i.e. it can be used to apply."""


class CandidateSetupError(ValueError):
    """The candidate package rejected an upload or profile write. ``field`` is the
    frontend field the problem belongs to (``resumeFile``, ``resumeId``, ``email``...)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


class CandidateDataInvalid(RuntimeError):
    """The stored profile exists but cannot be read or validated."""


@runtime_checkable
class CandidateGateway(Protocol):
    def setup(self, candidate_id: str) -> CandidateSetupState:
        """Contact details, supplied resumes and the selected one; works before any
        profile exists and for profiles that do not load."""
        ...

    def store_resume(self, candidate_id: str, *, filename: str, content: bytes) -> ResumeEntry:
        """Store an upload under a generated id. Raises ``CandidateSetupError``
        (``resumeFile``) when refused; nothing is stored then."""
        ...

    def upsert_profile(
        self, candidate_id: str, *, identity: CandidateIdentity, resume_id: str
    ) -> CandidateProfile:
        """Write contact details and select a listed resume, keeping facts, experience,
        education and saved answers. Raises ``CandidateSetupError`` (``resumeId``) or
        ``CandidateDataInvalid``."""
        ...

    def save_answer(self, candidate_id: str, answer: SavedAnswer) -> None: ...


# --- identity mapping -----------------------------------------------------------------


def render_location(address: PostalAddress) -> str:
    return ", ".join(p for p in (address.city, address.region, address.country) if p)


def _parse_location(text: str) -> PostalAddress:
    # The frontend labels this field "City and region" ("Austin, TX"). Two parts are
    # city and region; a country is never inferred from a region. Only an explicit
    # third part is a country.
    parts = [" ".join(p.split()) for p in text.split(",")]
    if any(not p for p in parts):
        raise CandidateSetupError("location", "Write the location as “City, Region”.")
    if len(parts) == 1:
        return PostalAddress(city=parts[0])
    if len(parts) == 2:
        return PostalAddress(city=parts[0], region=parts[1])
    if len(parts) == 3:
        return PostalAddress(city=parts[0], region=parts[1], country=parts[2])
    raise CandidateSetupError(
        "location", "Use “City, Region” or “City, Region, Country”."
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


_EPOCH = datetime.fromtimestamp(0, UTC)


def resume_view(entry: ResumeEntry) -> ResumeDocumentView:
    # A resume an imported profile referenced was never uploaded here; its file's own
    # modification time is the only truthful date to show.
    shown = entry.uploaded_at or entry.file_modified_at or _EPOCH
    return ResumeDocumentView(
        id=entry.id, file_name=entry.filename, size_bytes=entry.size_bytes, uploaded_at=iso(shown)
    )


def ordered_resumes(resumes: Sequence[ResumeEntry]) -> list[ResumeEntry]:
    """Newest upload first; a profile's own (never uploaded) resume last."""
    uploads = sorted(
        (r for r in resumes if r.uploaded_at is not None),
        key=lambda r: r.uploaded_at or _EPOCH,
        reverse=True,
    )
    return uploads + [r for r in resumes if r.uploaded_at is None]


def candidate_view(state: CandidateSetupState) -> CandidateView:
    resumes = ordered_resumes(state.resumes)
    ids = {r.id for r in resumes}
    selected = state.selected_resume_id
    return CandidateView(
        profile=profile_view(state.identity),
        resumes=[resume_view(r) for r in resumes],
        default_resume_id=selected if selected in ids else None,
    )

