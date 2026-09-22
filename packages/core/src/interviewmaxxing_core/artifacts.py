"""References to local files: candidate documents and submission evidence.

Contracts carry references, never file contents. Documents such as a resume live in
the local profile directory; evidence lives under the artifacts directory (see
``interviewmaxxing_core.config.LocalPaths``). Neither belongs in source control.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Self

from pydantic import Field, field_validator, model_validator

from ._base import Contract, NonEmptyStr, UtcDatetime, new_id, utc_now


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactRef(Contract):
    """A local document the browser may upload (resume, cover letter)."""

    id: NonEmptyStr
    path: NonEmptyStr
    """Absolute local path, resolved by the loader that produced the reference."""
    filename: NonEmptyStr
    media_type: NonEmptyStr
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)

    @classmethod
    def from_file(cls, path: Path, *, id: str, media_type: str) -> Self:
        resolved = path.expanduser().resolve()
        return cls(
            id=id,
            path=str(resolved),
            filename=resolved.name,
            media_type=media_type,
            sha256=sha256_file(resolved),
            size_bytes=resolved.stat().st_size,
        )

    def verify(self) -> bool:
        """True when the file still exists with the recorded digest."""
        p = Path(self.path)
        return p.is_file() and sha256_file(p) == self.sha256


class EvidenceKind(StrEnum):
    SCREENSHOT = "SCREENSHOT"
    HTML_SNAPSHOT = "HTML_SNAPSHOT"
    PAGE_TEXT = "PAGE_TEXT"
    CONFIRMATION_URL = "CONFIRMATION_URL"
    CONFIRMATION_EMAIL = "CONFIRMATION_EMAIL"
    USER_STATEMENT = "USER_STATEMENT"
    OTHER = "OTHER"


class EvidenceRef(Contract):
    """Something observed that supports a state claim (usually submission acceptance)."""

    id: NonEmptyStr = Field(default_factory=lambda: new_id("ev"))
    kind: EvidenceKind
    path: str | None = None
    """POSIX path relative to the artifacts directory, e.g. ``app_x/confirm.png``."""
    uri: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    description: str = ""
    captured_at: UtcDatetime = Field(default_factory=utc_now)

    @field_validator("path")
    @classmethod
    def _relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        p = PurePosixPath(value)
        if p.is_absolute() or ".." in p.parts or "\\" in value or not value.strip():
            raise ValueError("evidence path must be a relative POSIX path inside the artifacts dir")
        return value

    @model_validator(mode="after")
    def _has_content(self) -> Self:
        if not (self.path or self.uri or self.description.strip()):
            raise ValueError("evidence needs a path, a uri or a description")
        return self

    def resolve(self, artifacts_dir: Path) -> Path | None:
        return artifacts_dir / self.path if self.path else None
