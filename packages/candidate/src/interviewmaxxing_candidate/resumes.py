"""Supplied resume uploads: safe names, content checks and immutable storage.

Each upload lives in its own directory under the candidate's private directory::

    <candidate_id>/resumes/<resume_id>/
        <safe filename>   the bytes exactly as uploaded (mode 0400)
        meta.json         id, filename, media type, digest, size, upload time (0400)

The directory is assembled under a hidden staging name and renamed into place, so
an upload is either complete or absent. Uploads are never modified or reused under
another id; nothing is extracted from their content.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from interviewmaxxing_core import CandidateProfileInvalid, ResumeArtifact, new_id, sha256_file

from .files import read_json, write_json_private

RESUMES_DIRNAME = "resumes"
RESUME_META_FILENAME = "meta.json"
DEFAULT_MAX_RESUME_BYTES = 10 * 1024 * 1024
MAX_FILENAME_CHARS = 255
_STEM_CHARS = 100

UPLOAD_ID = re.compile(r"resume_[0-9a-f]{32}")
"""Generated upload ids; anything else is never used as a path component."""

RESUME_UPLOAD_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".rtf": "application/rtf",
    ".txt": "text/plain",
    ".md": "text/markdown",
}
"""Accepted upload extensions and the media type each is stored with."""

_GENERIC_MEDIA_TYPES = {"", "application/octet-stream"}
_MAGIC: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    ".docx": (b"PK\x03\x04",),
    ".odt": (b"PK\x03\x04",),
    ".rtf": (b"{\\rtf",),
}
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._() -]+")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ResumeRejected(ValueError):
    """An upload was refused (empty, too large, wrong type or unusable name).
    Nothing was stored."""


class ResumeNotFound(LookupError):
    """No resume with this id belongs to the candidate."""


class ResumeOrigin(StrEnum):
    UPLOADED = "UPLOADED"
    """Stored by ``store_resume`` under the candidate's ``resumes/`` directory."""
    PROFILE = "PROFILE"
    """The resume an imported or hand-written ``profile.json`` already references."""


@dataclass(frozen=True)
class StoredResume:
    """A supplied resume the user can select."""

    artifact: ResumeArtifact
    """Canonical reference: ``id``, absolute ``path``, ``filename``, digest, size."""
    origin: ResumeOrigin
    uploaded_at: datetime | None
    """Upload time; ``None`` for a PROFILE resume (it was not uploaded here)."""

    @property
    def id(self) -> str:
        return self.artifact.id


def safe_resume_filename(filename: str) -> str:
    """The stored name for an uploaded file: last path component only, visible ASCII
    letters, digits and ``._()- ``; no leading dots; an accepted extension."""
    if not isinstance(filename, str):
        raise ResumeRejected("filename must be text")
    if len(filename) > MAX_FILENAME_CHARS:
        raise ResumeRejected(f"filename is longer than {MAX_FILENAME_CHARS} characters")
    name = re.split(r"[/\\]", filename)[-1]
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"\s+", " ", _UNSAFE_CHARS.sub("_", name))
    stem, dot, ext = name.rpartition(".")
    suffix = f".{ext.strip().lower()}"
    if not dot or suffix not in RESUME_UPLOAD_TYPES:
        accepted = ", ".join(RESUME_UPLOAD_TYPES)
        raise ResumeRejected(f"unsupported resume file type {filename!r}; use one of {accepted}")
    stem = stem.strip(" ._-")[:_STEM_CHARS].rstrip(" ._-") or "resume"
    return f"{stem}{suffix}"


def check_resume_content(
    filename: str, content: bytes, media_type: str | None, *, max_bytes: int
) -> str:
    """Validate an upload for ``filename`` (already made safe); return its media type."""
    if not isinstance(content, bytes):
        raise ResumeRejected("resume content must be bytes")
    if not content:
        raise ResumeRejected("the resume file is empty")
    if len(content) > max_bytes:
        raise ResumeRejected(f"the resume is larger than the {max_bytes}-byte limit")
    suffix = Path(filename).suffix
    expected = RESUME_UPLOAD_TYPES[suffix]
    given = (media_type or "").split(";")[0].strip().lower()
    if given not in _GENERIC_MEDIA_TYPES and given != expected:
        raise ResumeRejected(
            f"media type {media_type!r} does not match a {suffix} file ({expected})"
        )
    signatures = _MAGIC.get(suffix)
    if signatures is not None and not content.startswith(signatures):
        raise ResumeRejected(f"the content is not a {suffix} file")
    if signatures is None:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            raise ResumeRejected(f"a {suffix} resume must be UTF-8 text") from None
        if "\x00" in text:
            raise ResumeRejected(f"a {suffix} resume must be text")
    return expected


def write_upload(
    resumes_dir: Path, *, filename: str, content: bytes, media_type: str, uploaded_at: datetime
) -> StoredResume:
    """Atomically create ``resumes_dir/<new id>/`` holding the file and its metadata."""
    resumes_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    resume_id = new_id("resume")
    staging = Path(tempfile.mkdtemp(prefix=f".{resume_id}.", dir=resumes_dir))
    try:
        _write_readonly(staging / filename, content)
        meta = {
            "id": resume_id,
            "filename": filename,
            "media_type": media_type,
            "sha256": sha256_file(staging / filename),
            "size_bytes": len(content),
            "uploaded_at": uploaded_at.isoformat().replace("+00:00", "Z"),
        }
        write_json_private(staging / RESUME_META_FILENAME, meta)
        (staging / RESUME_META_FILENAME).chmod(0o400)
        _fsync_dir(staging)
        staging.rename(resumes_dir / resume_id)
        _fsync_dir(resumes_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return read_upload(resumes_dir, resume_id)


def read_upload(resumes_dir: Path, resume_id: str) -> StoredResume:
    """The stored upload ``resume_id``; its file must still match its digest."""
    if not UPLOAD_ID.fullmatch(resume_id):
        raise ResumeNotFound(f"no uploaded resume {resume_id!r}")
    directory = resumes_dir / resume_id
    meta_path = directory / RESUME_META_FILENAME
    if not meta_path.is_file():
        raise ResumeNotFound(f"no uploaded resume {resume_id!r}")
    meta: Any = read_json(meta_path)
    try:
        if not isinstance(meta, dict) or meta.get("id") != resume_id:
            raise ValueError("metadata does not describe this upload")
        filename = meta["filename"]
        if not isinstance(filename, str) or safe_resume_filename(filename) != filename:
            raise ValueError("unsafe stored filename")
        uploaded_at = datetime.fromisoformat(meta["uploaded_at"])
        artifact = ResumeArtifact(
            id=resume_id,
            path=str(directory / filename),
            filename=filename,
            media_type=meta["media_type"],
            sha256=meta["sha256"],
            size_bytes=meta["size_bytes"],
        )
    except (KeyError, TypeError, ValueError, ResumeRejected, ValidationError) as exc:
        raise CandidateProfileInvalid(f"{meta_path}: unreadable resume metadata ({exc})") from None
    if not artifact.verify():
        raise CandidateProfileInvalid(
            f"uploaded resume {resume_id} ({directory / filename}) is missing or no longer "
            "matches its recorded sha256; upload it again"
        )
    return StoredResume(artifact=artifact, origin=ResumeOrigin.UPLOADED, uploaded_at=uploaded_at)


@dataclass(frozen=True)
class DamagedResume:
    """An upload directory that can no longer be offered: its file or metadata is
    missing, unreadable or does not match the recorded sha256."""

    resume_id: str
    problem: str
    """Why, as ``read_upload`` reports it (may contain local paths)."""


def scan_uploads(resumes_dir: Path) -> tuple[list[StoredResume], list[DamagedResume]]:
    """Every upload directory, checked one by one: usable uploads oldest first, and
    damaged ones by id. One damaged upload never hides the others. Hidden staging
    directories are ignored."""
    if not resumes_dir.is_dir():
        return [], []
    uploads: list[StoredResume] = []
    damaged: list[DamagedResume] = []
    for entry in sorted(resumes_dir.iterdir()):
        if not (entry.is_dir() and UPLOAD_ID.fullmatch(entry.name)):
            continue
        if not (entry / RESUME_META_FILENAME).is_file():
            # Complete uploads are renamed into place with their metadata, so a
            # published directory without it is damaged, not absent.
            damaged.append(DamagedResume(entry.name, f"{entry}: resume metadata is missing"))
            continue
        try:
            uploads.append(read_upload(resumes_dir, entry.name))
        except (ResumeNotFound, CandidateProfileInvalid) as exc:
            damaged.append(DamagedResume(entry.name, str(exc)))
    uploads.sort(key=lambda r: (r.uploaded_at or _EPOCH, r.id))
    return uploads, damaged


def list_uploads(resumes_dir: Path) -> list[StoredResume]:
    """Usable uploads, oldest first; damaged ones are skipped (see ``scan_uploads``)."""
    return scan_uploads(resumes_dir)[0]


def _write_readonly(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    path.chmod(0o400)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
