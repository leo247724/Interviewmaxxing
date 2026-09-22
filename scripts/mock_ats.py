#!/usr/bin/env python3
"""Deterministic localhost mock ATS for Interviewmaxxing browser tests.

Standard library only. Serves the careers site of a fictional employer,
"Brambleway Analytics", with ordinary accessible HTML application forms and a
server-side record of every accepted submission.

Routes under ``/__test__/`` exist strictly for test assertions and fixture
control. Product runtime code must never call them; it reconciles through the
public pages, for example ``/jobs/<job>/application-status``.

Run::

    uv run --no-project --python 3.12 scripts/mock_ats.py --state-dir /tmp/mock-ats

See ``tests/browser/MOCK_ATS.md`` for scenarios, routes and how to stop it.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import json
import os
import re
import signal
import socketserver
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from email.utils import collapse_rfc2231_value
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

COMPANY = "Brambleway Analytics"
REFERENCE_PREFIX = "BWA"
MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 5000
RESUME_EXTENSIONS = (".pdf", ".doc", ".docx", ".txt")
SESSION_COOKIE = "bwa_session"
SIGNIN_EMAIL = "avery.quill@example.test"
SIGNIN_PASSWORD = "fixture-password-123"
CAPTCHA_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
INTERNAL_FIELDS = frozenset({"resume_upload_id", "captcha_token", "captcha_answer"})
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
US_PHONE_RE = re.compile(r"^\d{10}$")
US_PHONE_MESSAGE = "Enter a 10-digit US phone number using digits only, for example 3035550142."


# --------------------------------------------------------------------------
# Form and job definitions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Option:
    value: str
    label: str


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    # text, email, tel, url, textarea, select, radio, checkbox,
    # checkbox_group, multiselect or file
    kind: str
    required: bool = False
    options: tuple[Option, ...] = ()
    hint: str | None = None
    autocomplete: str | None = None
    accept: str | None = None

    @property
    def multi(self) -> bool:
        return self.kind in ("checkbox_group", "multiselect")

    def option_label(self, value: str) -> str:
        return next((o.label for o in self.options if o.value == value), value)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "options": [{"value": o.value, "label": o.label} for o in self.options],
        }


def _options(*pairs: tuple[str, str]) -> tuple[Option, ...]:
    return tuple(Option(value, label) for value, label in pairs)


FIRST_NAME = Field("first_name", "First name", "text", True, autocomplete="given-name")
LAST_NAME = Field("last_name", "Last name", "text", True, autocomplete="family-name")
EMAIL = Field("email", "Email", "email", True, autocomplete="email")
PHONE = Field("phone", "Phone", "tel", True, autocomplete="tel")
LINKEDIN = Field("linkedin_url", "LinkedIn profile URL", "url", autocomplete="url")
RESUME = Field(
    "resume",
    "Resume",
    "file",
    True,
    hint="PDF, DOC, DOCX or TXT, up to 5 MB.",
    accept=".pdf,.doc,.docx,.txt,application/pdf,text/plain",
)
WORK_AUTHORIZATION = Field(
    "work_authorization",
    "Are you legally authorized to work in the United States?",
    "select",
    True,
    _options(
        ("wa_authorized", "Yes, I am authorized to work in the US"),
        ("wa_not_authorized", "No, I am not authorized to work in the US"),
    ),
)
YEARS_EXPERIENCE = Field(
    "years_experience",
    "Years of professional experience",
    "select",
    True,
    _options(
        ("yrs_0_2", "0–2 years"),
        ("yrs_3_5", "3–5 years"),
        ("yrs_6_9", "6–9 years"),
        ("yrs_10_plus", "10 or more years"),
    ),
)
SPONSORSHIP = Field(
    "sponsorship",
    "Will you now or in the future require visa sponsorship?",
    "radio",
    True,
    _options(
        ("needs_sponsorship", "Yes, I will require sponsorship"),
        ("no_sponsorship", "No, I will not require sponsorship"),
    ),
)
SKILLS = Field(
    "skills",
    "Primary skills",
    "multiselect",
    True,
    _options(
        ("sk_python", "Python"),
        ("sk_sql", "SQL"),
        ("sk_spark", "Apache Spark"),
        ("sk_dbt", "dbt"),
        ("sk_k8s", "Kubernetes"),
        ("sk_go", "Go"),
    ),
    hint="Hold Ctrl or Command to select more than one.",
)
WORK_ARRANGEMENTS = Field(
    "work_arrangements",
    "Which work arrangements would you consider?",
    "checkbox_group",
    options=_options(
        ("arr_remote", "Remote"),
        ("arr_hybrid", "Hybrid"),
        ("arr_onsite", "On-site in Denver, CO"),
    ),
    hint="Select all that apply.",
)
OPEN_TO_RELOCATION = Field(
    "open_to_relocation", "I am open to relocating to Denver, CO", "checkbox"
)
WHY_BRAMBLEWAY = Field(
    "why_brambleway", "Why do you want to work at Brambleway Analytics?", "textarea", True
)
NOTICE_PERIOD = Field(
    "notice_period",
    "What is your notice period?",
    "select",
    True,
    _options(
        ("notice_immediate", "Immediately"),
        ("notice_2w", "2 weeks"),
        ("notice_1m", "1 month"),
        ("notice_2m_plus", "2 months or more"),
    ),
)
SALARY_EXPECTATION = Field(
    "salary_expectation", "Desired annual base salary (USD)", "text", True
)
FAA_CERTIFICATE = Field(
    "faa_part_107",
    "Do you hold an active FAA Part 107 remote pilot certificate?",
    "radio",
    True,
    _options(("faa_yes", "Yes"), ("faa_no", "No")),
)
ATTEST_ACCURACY = Field(
    "attest_accuracy",
    "I certify that the information in this application is true and complete "
    "to the best of my knowledge.",
    "checkbox",
    True,
)
ATTEST_PRIVACY = Field(
    "attest_privacy_notice",
    "I have read and acknowledge the Brambleway Analytics Applicant Privacy Notice.",
    "checkbox",
    True,
)

CORE_FIELDS = (
    FIRST_NAME,
    LAST_NAME,
    EMAIL,
    PHONE,
    LINKEDIN,
    RESUME,
    WORK_AUTHORIZATION,
    SPONSORSHIP,
)


@dataclass(frozen=True)
class Step:
    title: str
    fields: tuple[Field, ...]


@dataclass(frozen=True)
class Job:
    slug: str
    code: str
    title: str
    department: str
    location: str
    scenario: str
    steps: tuple[Step, ...]
    requires_signin: bool = False
    captcha: bool = False
    visible_confirmation: bool = True
    strict_phone: bool = False

    @property
    def multistep(self) -> bool:
        return len(self.steps) > 1

    @property
    def fields(self) -> tuple[Field, ...]:
        return tuple(f for step in self.steps for f in step.fields)

    def field(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)

    def describe(self) -> dict[str, Any]:
        return {
            "job_id": self.slug,
            "job_code": self.code,
            "title": self.title,
            "company": COMPANY,
            "scenario": self.scenario,
            "posting_path": f"/jobs/{self.slug}",
            "apply_path": f"/jobs/{self.slug}/apply",
            "status_path": f"/jobs/{self.slug}/application-status",
            "requires_signin": self.requires_signin,
            "captcha": self.captcha,
            "visible_confirmation": self.visible_confirmation,
            "multistep": self.multistep,
            "steps": [
                {"title": s.title, "fields": [f.describe() for f in s.fields]}
                for s in self.steps
            ],
        }


def _single(*fields: Field) -> tuple[Step, ...]:
    return (Step("Application", fields),)


JOBS: dict[str, Job] = {
    job.slug: job
    for job in (
        Job(
            "standard",
            "BWA-ENG-101",
            "Senior Data Platform Engineer",
            "Engineering",
            "Denver, CO (Hybrid)",
            "Single-page form with every native control type; accepted with a visible confirmation.",
            _single(
                FIRST_NAME,
                LAST_NAME,
                EMAIL,
                PHONE,
                LINKEDIN,
                RESUME,
                WORK_AUTHORIZATION,
                YEARS_EXPERIENCE,
                SPONSORSHIP,
                SKILLS,
                WORK_ARRANGEMENTS,
                OPEN_TO_RELOCATION,
                WHY_BRAMBLEWAY,
            ),
        ),
        Job(
            "multistep",
            "BWA-ML-102",
            "Machine Learning Engineer",
            "Engineering",
            "Remote (US)",
            "Three form steps plus a review page; accepted with a visible confirmation.",
            (
                Step("Contact information", (FIRST_NAME, LAST_NAME, EMAIL, PHONE, LINKEDIN)),
                Step(
                    "Resume and experience",
                    (RESUME, YEARS_EXPERIENCE, WORK_AUTHORIZATION, SPONSORSHIP),
                ),
                Step(
                    "Additional questions",
                    (SKILLS, WORK_ARRANGEMENTS, OPEN_TO_RELOCATION, WHY_BRAMBLEWAY),
                ),
            ),
        ),
        Job(
            "missing-required",
            "BWA-AE-103",
            "Analytics Engineer",
            "Data",
            "Denver, CO (Hybrid)",
            "Required questions the fixture candidate cannot answer (notice period, "
            "salary, certification); must surface as missing input.",
            _single(*CORE_FIELDS, NOTICE_PERIOD, SALARY_EXPECTATION, FAA_CERTIFICATE),
        ),
        Job(
            "attestation",
            "BWA-SEC-104",
            "Staff Security Engineer",
            "Security",
            "Remote (US)",
            "Required personal attestations that only the candidate may give.",
            _single(*CORE_FIELDS, ATTEST_ACCURACY, ATTEST_PRIVACY),
        ),
        Job(
            "validation",
            "BWA-BE-105",
            "Backend Engineer",
            "Engineering",
            "Denver, CO (On-site)",
            "Server-side phone rule (10 digits only) rejects the fixture phone with a "
            "visible error.",
            _single(*CORE_FIELDS),
            strict_phone=True,
        ),
        Job(
            "signin",
            "BWA-PE-106",
            "Product Engineer",
            "Product",
            "Remote (US)",
            "The application form requires signing in to a candidate account first.",
            _single(*CORE_FIELDS),
            requires_signin=True,
        ),
        Job(
            "captcha",
            "BWA-FE-107",
            "Frontend Engineer",
            "Engineering",
            "Remote (US)",
            "The application form includes an image CAPTCHA a person must solve.",
            _single(*CORE_FIELDS),
            captcha=True,
        ),
        Job(
            "uncertain",
            "BWA-SRE-108",
            "Site Reliability Engineer",
            "Infrastructure",
            "Denver, CO (Hybrid)",
            "Submission is recorded, then the response is an error page with no "
            "confirmation. A test-only reveal later exposes the receipt on the status page.",
            _single(*CORE_FIELDS),
            visible_confirmation=False,
        ),
    )
}


# --------------------------------------------------------------------------
# Persistent state
# --------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Upload:
    filename: str
    content_type: str
    data: bytes


class Store:
    """JSON-file state: submissions, rejections, uploads, drafts, captchas, sessions.

    Identifiers come from monotonically increasing counters, so a fresh state
    directory always produces the same ids and confirmation references.
    """

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.uploads_dir = self.state_dir / "uploads"
        self.path = self.state_dir / "state.json"
        self.lock = threading.RLock()
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.data = json.loads(self.path.read_text("utf-8"))
        else:
            self.data = self._empty()
            self._save()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema": 1,
            "counters": {
                "submission": 0,
                "rejection": 0,
                "upload": 0,
                "draft": 0,
                "captcha": 0,
                "session": 0,
            },
            "submissions": [],
            "rejections": [],
            "uploads": {},
            "drafts": {},
            "captchas": {},
            "sessions": {},
        }

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), "utf-8")
        os.replace(tmp, self.path)

    def _next(self, kind: str) -> int:
        self.data["counters"][kind] += 1
        return self.data["counters"][kind]

    def reset(self) -> None:
        with self.lock:
            for path in self.uploads_dir.iterdir():
                path.unlink()
            self.data = self._empty()
            self._save()

    # uploads
    def store_upload(self, upload: Upload) -> dict[str, Any]:
        with self.lock:
            upload_id = f"upl_{self._next('upload'):06d}"
            path = self.uploads_dir / f"{upload_id}.bin"
            path.write_bytes(upload.data)
            meta = {
                "upload_id": upload_id,
                "filename": upload.filename,
                "content_type": upload.content_type,
                "size": len(upload.data),
                "sha256": hashlib.sha256(upload.data).hexdigest(),
                "stored_path": str(path.resolve()),
            }
            self.data["uploads"][upload_id] = meta
            self._save()
            return meta

    def get_upload(self, upload_id: str | None) -> dict[str, Any] | None:
        with self.lock:
            return self.data["uploads"].get(upload_id or "")

    # submissions
    def add_submission(
        self,
        job: Job,
        fields: dict[str, Any],
        extra_fields: dict[str, Any],
        files: dict[str, dict[str, Any]],
        draft_id: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            n = self._next("submission")
            record = {
                "submission_id": f"sub_{n:06d}",
                "sequence": n,
                "confirmation_reference": f"{REFERENCE_PREFIX}-{n:06d}",
                "job_id": job.slug,
                "job_code": job.code,
                "job_title": job.title,
                "company": COMPANY,
                "received_at": _now(),
                "confirmation_visible": job.visible_confirmation,
                "revealed_at": None,
                "draft_id": draft_id,
                "fields": fields,
                "extra_fields": extra_fields,
                "files": files,
            }
            self.data["submissions"].append(record)
            self._save()
            return record

    def add_rejection(self, job: Job, errors: dict[str, str], step: int | None = None) -> None:
        with self.lock:
            n = self._next("rejection")
            self.data["rejections"].append(
                {
                    "rejection_id": f"rej_{n:06d}",
                    "job_id": job.slug,
                    "step": step,
                    "at": _now(),
                    "errors": errors,
                }
            )
            self._save()

    def get_submission(self, submission_id: str) -> dict[str, Any] | None:
        with self.lock:
            return next(
                (s for s in self.data["submissions"] if s["submission_id"] == submission_id),
                None,
            )

    def reveal(self, submission_id: str) -> dict[str, Any] | None:
        with self.lock:
            record = self.get_submission(submission_id)
            if record is not None and not record["confirmation_visible"]:
                record["confirmation_visible"] = True
                record["revealed_at"] = _now()
                self._save()
            return record

    def find_submissions(self, job_id: str, email: str) -> list[dict[str, Any]]:
        email = email.strip().lower()
        with self.lock:
            return [
                s
                for s in self.data["submissions"]
                if s["job_id"] == job_id and str(s["fields"].get("email", "")).lower() == email
            ]

    def summary(self, job_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            subs = [s for s in self.data["submissions"] if job_id in (None, s["job_id"])]
            rejs = [r for r in self.data["rejections"] if job_id in (None, r["job_id"])]
            return {
                "job_id": job_id,
                "accepted_count": len(subs),
                "rejected_count": len(rejs),
                "submissions": subs,
                "rejections": rejs,
            }

    # multistep drafts
    def save_step(
        self,
        job: Job,
        draft_id: str | None,
        step: int,
        values: dict[str, Any],
        files: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        with self.lock:
            if draft_id is None:
                draft_id = f"dft_{self._next('draft'):06d}"
                self.data["drafts"][draft_id] = {
                    "draft_id": draft_id,
                    "job_id": job.slug,
                    "created_at": _now(),
                    "steps": {},
                    "files": {},
                }
            draft = self.data["drafts"][draft_id]
            draft["steps"][str(step)] = values
            draft["files"].update(files)
            self._save()
            return draft

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self.lock:
            return self.data["drafts"].get(draft_id)

    # captcha
    def new_captcha(self) -> str:
        with self.lock:
            token = f"cap_{self._next('captcha'):06d}"
            digest = hashlib.sha256(f"brambleway-captcha:{token}".encode()).digest()
            answer = "".join(CAPTCHA_ALPHABET[b % len(CAPTCHA_ALPHABET)] for b in digest[:5])
            self.data["captchas"][token] = {"answer": answer, "used": False}
            self._save()
            return token

    def captcha(self, token: str) -> dict[str, Any] | None:
        with self.lock:
            return self.data["captchas"].get(token)

    def consume_captcha(self, token: str, answer: str) -> bool:
        """Single use: any attempt spends the challenge."""
        with self.lock:
            challenge = self.data["captchas"].get(token)
            if challenge is None or challenge["used"]:
                return False
            challenge["used"] = True
            self._save()
            return answer.strip().upper() == challenge["answer"]

    # sign-in sessions
    def new_session(self, email: str) -> str:
        with self.lock:
            n = self._next("session")
            token = hashlib.sha256(f"brambleway-session:{n}".encode()).hexdigest()[:32]
            self.data["sessions"][token] = {"email": email, "created_at": _now()}
            self._save()
            return token

    def has_session(self, token: str) -> bool:
        with self.lock:
            return token in self.data["sessions"]


# --------------------------------------------------------------------------
# Request parsing and validation
# --------------------------------------------------------------------------


class HttpError(Exception):
    def __init__(self, status: HTTPStatus, message: str | None = None):
        super().__init__(message or status.phrase)
        self.status = status
        self.message = message or status.phrase


def _header_param(header_value: str, header: str, param: str) -> str | None:
    msg = Message()
    msg[header] = header_value
    value = msg.get_param(param, header=header)
    if value is None:
        return None
    return collapse_rfc2231_value(value)


def parse_multipart(
    body: bytes, content_type: str
) -> tuple[dict[str, list[str]], dict[str, list[Upload]]]:
    """Parse a multipart/form-data body into text fields and file parts."""
    boundary = _header_param(content_type, "content-type", "boundary")
    if not boundary:
        raise HttpError(HTTPStatus.BAD_REQUEST, "multipart boundary missing")
    delimiter = b"\r\n--" + boundary.encode("latin-1")
    fields: dict[str, list[str]] = {}
    files: dict[str, list[Upload]] = {}
    chunks = (b"\r\n" + body).split(delimiter)
    for chunk in chunks[1:]:
        if chunk.startswith(b"--"):
            break
        if not chunk.startswith(b"\r\n"):
            raise HttpError(HTTPStatus.BAD_REQUEST, "malformed multipart body")
        head, sep, content = chunk[2:].partition(b"\r\n\r\n")
        if not sep:
            raise HttpError(HTTPStatus.BAD_REQUEST, "malformed multipart part")
        headers: dict[str, str] = {}
        for line in head.decode("utf-8", "replace").split("\r\n"):
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        name = _header_param(disposition, "content-disposition", "name")
        if name is None:
            continue
        filename = _header_param(disposition, "content-disposition", "filename")
        if filename is None:
            fields.setdefault(name, []).append(content.decode("utf-8", "replace"))
        else:
            filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
            files.setdefault(name, []).append(
                Upload(
                    filename,
                    headers.get("content-type", "application/octet-stream"),
                    content,
                )
            )
    return fields, files


def _required_message(f: Field) -> str:
    if f.kind in ("select", "radio"):
        return "Select an answer."
    if f.kind == "checkbox":
        return "Check this box to continue."
    if f.multi:
        return "Select at least one option."
    if f.kind == "file":
        return "Attach a file."
    return "This field is required."


def _upload_error(upload: Upload) -> str | None:
    if not upload.filename.lower().endswith(RESUME_EXTENSIONS):
        return "Upload a PDF, DOC, DOCX or TXT file."
    if not upload.data:
        return "The selected file is empty."
    if len(upload.data) > MAX_UPLOAD_BYTES:
        return "The selected file must be smaller than 5 MB."
    return None


def validate(
    fields: tuple[Field, ...],
    form: dict[str, list[str]],
    uploads: dict[str, list[Upload]],
    retained: dict[str, dict[str, Any]],
    strict_phone: bool = False,
) -> tuple[dict[str, Any], dict[str, Upload | dict[str, Any]], dict[str, str]]:
    """Return (clean values, files, errors).

    Files map to a new ``Upload`` or to retained upload metadata. Empty or
    absent optional values are omitted from the clean values.
    """
    values: dict[str, Any] = {}
    files: dict[str, Upload | dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for f in fields:
        if f.kind == "file":
            attached = [u for u in uploads.get(f.name, []) if u.filename]
            if attached:
                error = _upload_error(attached[0])
                if error:
                    errors[f.name] = error
                else:
                    files[f.name] = attached[0]
            elif f.name in retained:
                files[f.name] = retained[f.name]
            elif f.required:
                errors[f.name] = _required_message(f)
            continue

        raw = form.get(f.name, [])
        allowed = {o.value for o in f.options}
        if f.multi:
            chosen = [v for v in raw if v != ""]
            if any(v not in allowed for v in chosen):
                errors[f.name] = "Select only the listed options."
            elif chosen:
                values[f.name] = chosen
            elif f.required:
                errors[f.name] = _required_message(f)
            continue

        distinct = {v.strip() for v in raw if v.strip()}
        if len(distinct) > 1:
            errors[f.name] = "Provide a single answer."
            continue
        value = distinct.pop() if distinct else ""
        if not value:
            if f.required:
                errors[f.name] = _required_message(f)
            continue
        if f.options and value not in allowed:
            errors[f.name] = "Select one of the listed options."
        elif f.kind == "checkbox" and value != "yes":
            errors[f.name] = "Unexpected value for this checkbox."
        elif f.kind == "email" and not EMAIL_RE.match(value):
            errors[f.name] = "Enter a valid email address, like name@example.com."
        elif f.kind == "url" and not value.startswith(("https://", "http://")):
            errors[f.name] = "Enter a full web address starting with https://."
        elif f.kind == "tel" and strict_phone and not US_PHONE_RE.match(value):
            errors[f.name] = US_PHONE_MESSAGE
        elif len(value) > MAX_TEXT_CHARS:
            errors[f.name] = "Keep this answer under 5,000 characters."
        else:
            values[f.name] = value
    return values, files, errors


def _extra_fields(job: Job, form: dict[str, list[str]]) -> dict[str, Any]:
    declared = {f.name for f in job.fields} | INTERNAL_FIELDS
    return {
        name: vals if len(vals) > 1 else vals[0]
        for name, vals in form.items()
        if name not in declared and vals
    }


def _as_lists(values: dict[str, Any]) -> dict[str, list[str]]:
    return {k: (v if isinstance(v, list) else [v]) for k, v in values.items()}


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


STYLE = """
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;color:#1d2330;background:#f6f7f9;line-height:1.5}
header.site,footer.site{background:#20364f;color:#fff;padding:.8rem 1.5rem}
header.site a{color:#fff;font-weight:600;text-decoration:none}
footer.site{background:#e8ebf0;color:#4a5568;font-size:.85rem;margin-top:3rem}
main{max-width:46rem;margin:0 auto;padding:1.5rem;background:#fff}
.meta{color:#4a5568}
.field{margin:1.25rem 0;border:0;padding:0}
.field>label,legend{display:block;font-weight:600;margin-bottom:.3rem}
.choice{margin:.25rem 0}
.choice label{font-weight:400}
input[type=text],input[type=email],input[type=tel],input[type=url],input[type=password],select,textarea{width:100%;box-sizing:border-box;padding:.45rem;border:1px solid #8a94a6;border-radius:4px;font:inherit}
[aria-invalid=true]{border-color:#b42318;outline:2px solid #b42318}
.hint{color:#4a5568;font-size:.9rem;margin:.1rem 0 .3rem}
.error{color:#b42318;font-weight:600;margin:.2rem 0}
.error-summary{border:3px solid #b42318;padding:.5rem 1rem;margin-bottom:1.5rem}
.error-summary a{color:#b42318}
.optional{font-weight:400;color:#4a5568}
.visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
button,.button{background:#1f6f43;color:#fff;border:0;border-radius:4px;padding:.6rem 1.2rem;font:inherit;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
.progress{display:flex;gap:1rem;list-style:none;padding:0;font-size:.9rem;color:#4a5568}
.progress [aria-current=step]{font-weight:700;color:#1d2330}
dl.review dt{font-weight:600;margin-top:.6rem}
dl.review dd{margin:0}
.notice{border-left:4px solid #20364f;padding:.5rem 1rem;background:#eef2f7}
"""


def page(title: str, body: str, head_extra: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} – {COMPANY} Careers</title>
<style>{STYLE}</style>
{head_extra}
</head>
<body>
<header class="site"><a href="/">{COMPANY} Careers</a></header>
<main id="main">
{body}
</main>
<footer class="site">Fictional employer for local software testing. Applications here are not sent to anyone.</footer>
</body>
</html>
"""


def _job_heading(job: Job) -> str:
    return (
        f"<h1>{esc(job.title)}</h1>\n"
        f'<p class="meta">{COMPANY} · {esc(job.department)} · {esc(job.location)} · '
        f"Job ID {esc(job.code)}</p>"
    )


def render_field(
    f: Field,
    values: dict[str, list[str]],
    error: str | None,
    retained: dict[str, Any] | None = None,
) -> str:
    fid = f"f-{f.name}"
    posted = values.get(f.name, [])
    current = posted[0] if posted else ""
    described: list[str] = []
    hint = ""
    if f.hint:
        hint = f'<p class="hint" id="{fid}-hint">{esc(f.hint)}</p>'
        described.append(f"{fid}-hint")
    err = ""
    if error:
        err = (
            f'<p class="error" id="{fid}-error"><span class="visually-hidden">Error: </span>'
            f"{esc(error)}</p>"
        )
        described.append(f"{fid}-error")
    if f.kind == "file" and retained:
        described.append(f"{fid}-current")
    aria = f' aria-describedby="{" ".join(described)}"' if described else ""
    if error:
        aria += ' aria-invalid="true"'
    required = " required" if f.required else ""
    marker = (
        ' <span aria-hidden="true">*</span>'
        if f.required
        else ' <span class="optional">(optional)</span>'
    )
    label = f'<label for="{fid}">{esc(f.label)}{marker}</label>'

    if f.kind in ("text", "email", "tel", "url"):
        auto = f' autocomplete="{f.autocomplete}"' if f.autocomplete else ""
        control = (
            f'<input type="{f.kind}" id="{fid}" name="{f.name}" value="{esc(current)}"'
            f"{auto}{required}{aria}>"
        )
        return f'<div class="field">{label}{hint}{err}{control}</div>'

    if f.kind == "textarea":
        control = (
            f'<textarea id="{fid}" name="{f.name}" rows="6" maxlength="{MAX_TEXT_CHARS}"'
            f"{required}{aria}>{esc(current)}</textarea>"
        )
        return f'<div class="field">{label}{hint}{err}{control}</div>'

    if f.kind in ("select", "multiselect"):
        multiple = f' multiple size="{len(f.options)}"' if f.multi else ""
        opts = [] if f.multi else ['<option value="">Select an answer</option>']
        for o in f.options:
            selected = " selected" if o.value in posted else ""
            opts.append(f'<option value="{esc(o.value)}"{selected}>{esc(o.label)}</option>')
        control = (
            f'<select id="{fid}" name="{f.name}"{multiple}{required}{aria}>'
            + "".join(opts)
            + "</select>"
        )
        return f'<div class="field">{label}{hint}{err}{control}</div>'

    if f.kind in ("radio", "checkbox_group"):
        input_type = "radio" if f.kind == "radio" else "checkbox"
        legend_marker = marker
        choices = []
        for i, o in enumerate(f.options):
            oid = f"{fid}-{i}"
            checked = " checked" if o.value in posted else ""
            invalid = ' aria-invalid="true"' if error else ""
            choices.append(
                f'<div class="choice"><input type="{input_type}" id="{oid}" name="{f.name}" '
                f'value="{esc(o.value)}"{checked}{required}{invalid}>'
                f'<label for="{oid}">{esc(o.label)}</label></div>'
            )
        group_aria = f' aria-describedby="{" ".join(described)}"' if described else ""
        role = ' role="radiogroup"' if f.kind == "radio" else ""
        req = ' aria-required="true"' if f.required and f.kind == "radio" else ""
        return (
            f'<fieldset class="field" id="{fid}"{role}{req}{group_aria}>'
            f"<legend>{esc(f.label)}{legend_marker}</legend>{hint}{err}"
            + "".join(choices)
            + "</fieldset>"
        )

    if f.kind == "checkbox":
        checked = " checked" if current == "yes" else ""
        control = (
            f'<input type="checkbox" id="{fid}" name="{f.name}" value="yes"'
            f"{checked}{required}{aria}>"
        )
        return f'<div class="field choice">{control} {label}{hint}{err}</div>'

    if f.kind == "file":
        current_file = ""
        needs_file = f.required
        if retained:
            needs_file = False
            current_file = (
                f'<p class="hint" id="{fid}-current">Currently attached: '
                f'{esc(retained["filename"])} ({retained["size"]} bytes). '
                "Choose a new file only if you want to replace it.</p>"
                f'<input type="hidden" name="resume_upload_id" value="{esc(retained["upload_id"])}">'
            )
        accept = f' accept="{esc(f.accept)}"' if f.accept else ""
        control = (
            f'<input type="file" id="{fid}" name="{f.name}"{accept}'
            f'{" required" if needs_file else ""}{aria}>'
        )
        return f'<div class="field">{label}{hint}{err}{current_file}{control}</div>'

    raise ValueError(f"unknown field kind {f.kind}")


def render_error_summary(entries: list[tuple[str, str, str]]) -> str:
    """entries: (anchor id, label, message)."""
    if not entries:
        return ""
    items = "".join(
        f'<li><a href="#{anchor}">{esc(label)}: {esc(message)}</a></li>'
        for anchor, label, message in entries
    )
    return (
        '<div class="error-summary" role="alert" aria-labelledby="error-summary-title" '
        'tabindex="-1"><h2 id="error-summary-title">There is a problem with your application</h2>'
        f"<ul>{items}</ul></div>"
    )


def _summary_entries(fields: tuple[Field, ...], errors: dict[str, str]) -> list[tuple[str, str, str]]:
    return [(f"f-{f.name}", f.label, errors[f.name]) for f in fields if f.name in errors]


def render_captcha(token: str, error: str | None) -> str:
    err = ""
    aria = ' aria-describedby="f-captcha_answer-hint"'
    if error:
        err = (
            '<p class="error" id="f-captcha_answer-error"><span class="visually-hidden">Error: '
            f"</span>{esc(error)}</p>"
        )
        aria = ' aria-describedby="f-captcha_answer-hint f-captcha_answer-error" aria-invalid="true"'
    return (
        '<fieldset class="field" id="human-verification"><legend>Verify you are human (CAPTCHA)</legend>'
        f'<img src="/captcha/{esc(token)}.svg" width="180" height="56" '
        'alt="CAPTCHA image containing distorted characters">'
        f'<input type="hidden" name="captcha_token" value="{esc(token)}">'
        '<label for="f-captcha_answer">Characters shown in the image <span aria-hidden="true">*</span></label>'
        '<p class="hint" id="f-captcha_answer-hint">Letters are not case sensitive.</p>'
        f'{err}<input type="text" id="f-captcha_answer" name="captcha_answer" autocomplete="off" '
        f"required{aria}></fieldset>"
    )


def captcha_svg(answer: str) -> str:
    glyphs = []
    for i, ch in enumerate(answer):
        x = 20 + i * 30
        y = 38 if i % 2 else 32
        angle = -14 if i % 2 else 11
        glyphs.append(
            f'<text x="{x}" y="{y}" transform="rotate({angle} {x} {y})">{esc(ch)}</text>'
        )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="180" height="56" viewBox="0 0 180 56">'
        '<rect width="180" height="56" fill="#f1efe6"/>'
        '<path d="M0 30 C40 5, 80 55, 180 20" stroke="#8a7f65" stroke-width="2" fill="none"/>'
        '<path d="M0 12 C60 50, 120 0, 180 44" stroke="#b3a88c" stroke-width="1.5" fill="none"/>'
        '<g font-family="Georgia,serif" font-size="28" fill="#2f2a1f">'
        + "".join(glyphs)
        + "</g></svg>"
    )


def display_value(f: Field, value: Any) -> str:
    if f.kind == "checkbox":
        return "Yes" if value == "yes" else "No"
    if value in (None, "", []):
        return "Not provided"
    if f.kind == "file":
        return f"{value['filename']} ({value['size']} bytes)"
    if isinstance(value, list):
        return ", ".join(f.option_label(v) for v in value)
    return f.option_label(value)


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

SLUG = r"(?P<slug>[a-z0-9-]+)"
DRAFT = r"(?P<draft_id>dft_\d{6})"
ROUTES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(p), method, name)
    for p, method, name in (
        (r"/", "GET", "get_index"),
        (r"/favicon\.ico", "GET", "get_favicon"),
        (rf"/jobs/{SLUG}", "GET", "get_job"),
        (rf"/jobs/{SLUG}/apply", "GET", "get_apply"),
        (rf"/jobs/{SLUG}/apply", "POST", "post_apply"),
        (rf"/jobs/{SLUG}/apply/{DRAFT}/step/(?P<step>\d+)", "GET", "get_step"),
        (rf"/jobs/{SLUG}/apply/{DRAFT}/step/(?P<step>\d+)", "POST", "post_step"),
        (rf"/jobs/{SLUG}/apply/{DRAFT}/review", "GET", "get_review"),
        (rf"/jobs/{SLUG}/apply/{DRAFT}/submit", "POST", "post_submit"),
        (rf"/jobs/{SLUG}/application-status", "GET", "get_status"),
        (r"/applications/(?P<submission_id>sub_\d{6})", "GET", "get_confirmation"),
        (r"/login", "GET", "get_login"),
        (r"/login", "POST", "post_login"),
        (r"/captcha/(?P<token>cap_\d{6})\.svg", "GET", "get_captcha_svg"),
        (r"/__test__/health", "GET", "test_health"),
        (r"/__test__/jobs", "GET", "test_jobs"),
        (r"/__test__/submissions", "GET", "test_submissions"),
        (r"/__test__/submissions/(?P<submission_id>sub_\d{6})", "GET", "test_submission"),
        (r"/__test__/submissions/(?P<submission_id>sub_\d{6})/reveal", "POST", "test_reveal"),
        (r"/__test__/captcha/(?P<token>cap_\d{6})", "GET", "test_captcha"),
        (r"/__test__/reset", "POST", "test_reset"),
        (r"/__test__/shutdown", "POST", "test_shutdown"),
    )
]


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: "MockATS"):
        self.app = app
        super().__init__(address, Handler)

    def server_bind(self) -> None:
        # Skip HTTPServer's reverse DNS lookup (socket.getfqdn), which can stall.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "BramblewayMockATS/1.0"

    @property
    def app(self) -> "MockATS":
        return self.server.app

    @property
    def store(self) -> Store:
        return self.app.store

    def log_message(self, format: str, *args: Any) -> None:
        if self.app.verbose:
            super().log_message(format, *args)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        split = urlsplit(self.path)
        self.query = parse_qs(split.query, keep_blank_values=True)
        path = split.path
        try:
            matched_path = False
            for pattern, route_method, name in ROUTES:
                match = pattern.fullmatch(path)
                if match:
                    matched_path = True
                    if route_method == method:
                        getattr(self, name)(**match.groupdict())
                        return
            if matched_path:
                raise HttpError(HTTPStatus.METHOD_NOT_ALLOWED)
            raise HttpError(HTTPStatus.NOT_FOUND)
        except HttpError as exc:
            if path.startswith("/__test__/"):
                self._send_json(exc.status, {"error": exc.message})
            else:
                body = f"<h1>{esc(exc.status.phrase)}</h1><p>{esc(exc.message)}</p>"
                self._send_html(exc.status, page(exc.status.phrase, body))
        except (BrokenPipeError, ConnectionResetError):
            pass

    # response helpers
    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: HTTPStatus, document: str, headers=()) -> None:
        self._send(status, document.encode("utf-8"), "text/html; charset=utf-8", headers)

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self._send(status, body, "application/json")

    def _redirect(self, location: str, headers: tuple[tuple[str, str], ...] = ()) -> None:
        self._send(
            HTTPStatus.SEE_OTHER,
            b"",
            "text/plain; charset=utf-8",
            (("Location", location),) + headers,
        )

    # request helpers
    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if length is None:
            if self.headers.get("Transfer-Encoding"):
                raise HttpError(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
            return b""
        try:
            size = int(length)
        except ValueError:
            raise HttpError(HTTPStatus.BAD_REQUEST, "invalid Content-Length") from None
        if size < 0:
            raise HttpError(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
        if size > MAX_BODY_BYTES:
            raise HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        return self.rfile.read(size)

    def _read_form(self) -> tuple[dict[str, list[str]], dict[str, list[Upload]]]:
        content_type = self.headers.get("Content-Type", "")
        body = self._read_body()
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type == "multipart/form-data":
            return parse_multipart(body, content_type)
        if media_type == "application/x-www-form-urlencoded":
            return parse_qs(body.decode("ascii", "replace"), keep_blank_values=True), {}
        if not body:
            return {}, {}
        raise HttpError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "expected an HTML form submission")

    def _job(self, slug: str) -> Job:
        job = JOBS.get(slug)
        if job is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "No such job.")
        return job

    def _signed_in(self) -> bool:
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        morsel = cookie.get(SESSION_COOKIE)
        return bool(morsel and self.store.has_session(morsel.value))

    def _redirect_to_login(self, job: Job) -> None:
        self._redirect(f"/login?next={quote(f'/jobs/{job.slug}/apply')}")

    # public pages
    def get_index(self) -> None:
        items = "".join(
            f'<li><a href="/jobs/{job.slug}">{esc(job.title)}</a> '
            f'<span class="meta">— {esc(job.department)}, {esc(job.location)}</span></li>'
            for job in JOBS.values()
        )
        body = f"<h1>Open roles at {COMPANY}</h1><ul>{items}</ul>"
        self._send_html(HTTPStatus.OK, page("Open roles", body))

    def get_favicon(self) -> None:
        # Keeps browser consoles free of an unrelated 404.
        self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")

    def get_job(self, slug: str) -> None:
        job = self._job(slug)
        ld = {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": job.title,
            "identifier": {"@type": "PropertyValue", "name": COMPANY, "value": job.code},
            "hiringOrganization": {"@type": "Organization", "name": COMPANY},
            "jobLocation": {
                "@type": "Place",
                "address": {"@type": "PostalAddress", "addressLocality": job.location},
            },
            "employmentType": "FULL_TIME",
            "datePosted": "2026-09-01",
            "description": f"{job.title} on the {job.department} team at {COMPANY}.",
        }
        head = (
            f'<link rel="canonical" href="{esc(self.app.origin)}/jobs/{job.slug}">'
            '<script type="application/ld+json">'
            + json.dumps(ld).replace("</", "<\\/")
            + "</script>"
        )
        body = (
            _job_heading(job)
            + f"<h2>About the role</h2><p>{COMPANY} builds forecasting tools for regional "
            f"logistics networks. As a {esc(job.title)} on the {esc(job.department)} team, you "
            "will design, build and operate production systems with a small, collaborative "
            "group.</p>"
            "<h2>What we offer</h2><ul><li>Health, dental and vision coverage</li>"
            "<li>Flexible hours</li><li>Annual learning budget</li></ul>"
            f'<p><a class="button" href="/jobs/{job.slug}/apply">Apply for this job</a></p>'
            f'<p><a href="/jobs/{job.slug}/application-status">Already applied? '
            "Check your application status</a></p>"
        )
        self._send_html(HTTPStatus.OK, page(job.title, body, head))

    def get_apply(self, slug: str) -> None:
        job = self._job(slug)
        if job.requires_signin and not self._signed_in():
            self._redirect_to_login(job)
            return
        if job.multistep:
            self._render_step(job, None, 1, {}, {}, None, HTTPStatus.OK)
        else:
            self._render_single(job, {}, {}, {}, None, HTTPStatus.OK)

    def post_apply(self, slug: str) -> None:
        job = self._job(slug)
        if job.requires_signin and not self._signed_in():
            self._read_body()
            self._redirect_to_login(job)
            return
        form, uploads = self._read_form()
        if job.multistep:
            self._post_step(job, None, 1, form, uploads)
            return

        retained: dict[str, dict[str, Any]] = {}
        prior = self.store.get_upload((form.get("resume_upload_id") or [""])[0])
        if prior:
            retained["resume"] = prior
        values, files, errors = validate(
            job.fields, form, uploads, retained, strict_phone=job.strict_phone
        )
        files_meta = self._store_files(files)
        captcha_error = None
        if job.captcha:
            token = (form.get("captcha_token") or [""])[0]
            answer = (form.get("captcha_answer") or [""])[0]
            if not answer.strip():
                captcha_error = "Enter the characters shown in the image."
            elif not self.store.consume_captcha(token, answer):
                captcha_error = "The characters did not match. Try the new image."
            if captcha_error:
                errors["captcha_answer"] = captcha_error
        if errors:
            self.store.add_rejection(job, errors)
            self._render_single(
                job, form, errors, files_meta, captcha_error, HTTPStatus.UNPROCESSABLE_ENTITY
            )
            return

        record = self.store.add_submission(job, values, _extra_fields(job, form), files_meta)
        if job.visible_confirmation:
            self._redirect(f"/applications/{record['submission_id']}")
            return
        # Accepted and counted, but the response withholds any confirmation.
        body = (
            "<h1>Something went wrong</h1>"
            "<p>The server did not respond in time. Please try again later.</p>"
            f'<p><a href="/jobs/{job.slug}">Return to the job posting</a></p>'
        )
        self._send_html(HTTPStatus.BAD_GATEWAY, page("Error", body))

    def _store_files(
        self, files: dict[str, Upload | dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        return {
            name: self.store.store_upload(f) if isinstance(f, Upload) else f
            for name, f in files.items()
        }

    def _render_single(
        self,
        job: Job,
        values: dict[str, list[str]],
        errors: dict[str, str],
        retained: dict[str, dict[str, Any]],
        captcha_error: str | None,
        status: HTTPStatus,
    ) -> None:
        entries = _summary_entries(job.fields, errors)
        if captcha_error:
            entries.append(("f-captcha_answer", "Characters shown in the image", captcha_error))
        fields_html = "".join(
            render_field(f, values, errors.get(f.name), retained.get(f.name)) for f in job.fields
        )
        if job.captcha:
            fields_html += render_captcha(self.store.new_captcha(), captcha_error)
        body = (
            _job_heading(job)
            + render_error_summary(entries)
            + f'<form method="post" action="/jobs/{job.slug}/apply" enctype="multipart/form-data" '
            'aria-labelledby="form-title"><h2 id="form-title">Application form</h2>'
            '<p class="hint">Fields marked with * are required.</p>'
            + fields_html
            + '<button type="submit">Submit application</button></form>'
        )
        title = f"Apply: {job.title}" if not errors else f"Error: Apply: {job.title}"
        self._send_html(status, page(title, body))

    # multistep
    def _draft(self, job: Job, draft_id: str) -> dict[str, Any]:
        draft = self.store.get_draft(draft_id)
        if draft is None or draft["job_id"] != job.slug:
            raise HttpError(HTTPStatus.NOT_FOUND, "This application draft does not exist.")
        return draft

    @staticmethod
    def _first_incomplete(job: Job, draft: dict[str, Any]) -> int | None:
        return next(
            (n for n in range(1, len(job.steps) + 1) if str(n) not in draft["steps"]), None
        )

    def _step_number(self, job: Job, step: str) -> int:
        n = int(step)
        if not job.multistep or not 1 <= n <= len(job.steps):
            raise HttpError(HTTPStatus.NOT_FOUND, "No such step.")
        return n

    def get_step(self, slug: str, draft_id: str, step: str) -> None:
        job = self._job(slug)
        n = self._step_number(job, step)
        draft = self._draft(job, draft_id)
        first = self._first_incomplete(job, draft)
        if first is not None and n > first:
            self._redirect(f"/jobs/{slug}/apply/{draft_id}/step/{first}")
            return
        values = _as_lists(draft["steps"].get(str(n), {}))
        self._render_step(job, draft, n, values, {}, None, HTTPStatus.OK)

    def post_step(self, slug: str, draft_id: str, step: str) -> None:
        job = self._job(slug)
        n = self._step_number(job, step)
        draft = self._draft(job, draft_id)
        form, uploads = self._read_form()
        self._post_step(job, draft, n, form, uploads)

    def _post_step(
        self,
        job: Job,
        draft: dict[str, Any] | None,
        n: int,
        form: dict[str, list[str]],
        uploads: dict[str, list[Upload]],
    ) -> None:
        step_fields = job.steps[n - 1].fields
        retained = {
            f.name: draft["files"][f.name]
            for f in step_fields
            if draft and f.name in draft["files"]
        }
        values, files, errors = validate(step_fields, form, uploads, retained)
        files_meta = self._store_files(files)
        if errors:
            self.store.add_rejection(job, errors, step=n)
            self._render_step(
                job, draft, n, form, errors, retained | files_meta, HTTPStatus.UNPROCESSABLE_ENTITY
            )
            return
        draft = self.store.save_step(
            job, draft["draft_id"] if draft else None, n, values, files_meta
        )
        base = f"/jobs/{job.slug}/apply/{draft['draft_id']}"
        self._redirect(f"{base}/step/{n + 1}" if n < len(job.steps) else f"{base}/review")

    def _progress(self, job: Job, current: int) -> str:
        titles = [s.title for s in job.steps] + ["Review and submit"]
        current_attr = ' aria-current="step"'
        items = "".join(
            f"<li{current_attr if i == current else ''}>{esc(t)}</li>"
            for i, t in enumerate(titles, start=1)
        )
        return (
            f'<nav aria-label="Application progress"><ol class="progress">{items}</ol></nav>'
            f'<p class="meta">Step {current} of {len(titles)}</p>'
        )

    def _render_step(
        self,
        job: Job,
        draft: dict[str, Any] | None,
        n: int,
        values: dict[str, list[str]],
        errors: dict[str, str],
        retained: dict[str, dict[str, Any]] | None,
        status: HTTPStatus,
    ) -> None:
        step = job.steps[n - 1]
        if retained is None:
            retained = draft["files"] if draft else {}
        if draft is None:
            action = f"/jobs/{job.slug}/apply"
        else:
            action = f"/jobs/{job.slug}/apply/{draft['draft_id']}/step/{n}"
        has_file = any(f.kind == "file" for f in step.fields)
        enctype = "multipart/form-data" if has_file else "application/x-www-form-urlencoded"
        back = ""
        if n > 1 and draft is not None:
            back = f' <a href="/jobs/{job.slug}/apply/{draft["draft_id"]}/step/{n - 1}">Back</a>'
        body = (
            _job_heading(job)
            + self._progress(job, n)
            + render_error_summary(_summary_entries(step.fields, errors))
            + f'<form method="post" action="{action}" enctype="{enctype}" '
            f'aria-labelledby="form-title"><h2 id="form-title">{esc(step.title)}</h2>'
            '<p class="hint">Fields marked with * are required.</p>'
            + "".join(
                render_field(f, values, errors.get(f.name), retained.get(f.name))
                for f in step.fields
            )
            + f'<button type="submit">Continue</button>{back}</form>'
        )
        prefix = "Error: " if errors else ""
        self._send_html(status, page(f"{prefix}{step.title}: {job.title}", body))

    def get_review(self, slug: str, draft_id: str) -> None:
        job = self._job(slug)
        draft = self._draft(job, draft_id)
        first = self._first_incomplete(job, draft)
        if first is not None:
            self._redirect(f"/jobs/{slug}/apply/{draft_id}/step/{first}")
            return
        sections = []
        for n, step in enumerate(job.steps, start=1):
            stored = draft["steps"][str(n)]
            rows = "".join(
                f"<dt>{esc(f.label)}</dt><dd>"
                + esc(
                    display_value(
                        f, draft["files"].get(f.name) if f.kind == "file" else stored.get(f.name)
                    )
                )
                + "</dd>"
                for f in step.fields
            )
            sections.append(
                f"<section><h3>{esc(step.title)}</h3>"
                f'<p><a href="/jobs/{slug}/apply/{draft_id}/step/{n}">Edit {esc(step.title.lower())}</a></p>'
                f'<dl class="review">{rows}</dl></section>'
            )
        body = (
            _job_heading(job)
            + self._progress(job, len(job.steps) + 1)
            + '<h2 id="form-title">Review your application</h2>'
            + "".join(sections)
            + f'<form method="post" action="/jobs/{slug}/apply/{draft_id}/submit" '
            'aria-labelledby="form-title"><button type="submit">Submit application</button> '
            f'<a href="/jobs/{slug}/apply/{draft_id}/step/{len(job.steps)}">Back</a></form>'
        )
        self._send_html(HTTPStatus.OK, page(f"Review: {job.title}", body))

    def post_submit(self, slug: str, draft_id: str) -> None:
        job = self._job(slug)
        draft = self._draft(job, draft_id)
        self._read_body()
        first = self._first_incomplete(job, draft)
        if first is not None:
            self._redirect(f"/jobs/{slug}/apply/{draft_id}/step/{first}")
            return
        values: dict[str, Any] = {}
        for n in range(1, len(job.steps) + 1):
            values.update(draft["steps"][str(n)])
        record = self.store.add_submission(job, values, {}, dict(draft["files"]), draft_id)
        self._redirect(f"/applications/{record['submission_id']}")

    # confirmation and status
    def get_confirmation(self, submission_id: str) -> None:
        record = self.store.get_submission(submission_id)
        if record is None or not record["confirmation_visible"]:
            raise HttpError(HTTPStatus.NOT_FOUND, "Page not found.")
        job = JOBS[record["job_id"]]
        name = record["fields"].get("first_name", "")
        body = (
            "<h1>Application submitted</h1>"
            '<div role="status">'
            f"<p>Thank you{', ' + esc(name) if name else ''}. Your application for "
            f"<strong>{esc(job.title)}</strong> (Job ID {esc(job.code)}) at {COMPANY} was "
            f"received on {esc(record['received_at'])}.</p>"
            f"<p>Confirmation reference: <strong>{esc(record['confirmation_reference'])}</strong></p>"
            "</div>"
            f'<p><a href="/jobs/{job.slug}">Back to the job posting</a></p>'
        )
        self._send_html(HTTPStatus.OK, page("Application submitted", body))

    def get_status(self, slug: str) -> None:
        job = self._job(slug)
        email = (self.query.get("email") or [""])[0].strip()
        result = ""
        if email:
            records = self.store.find_submissions(job.slug, email)
            if not records:
                message = "<p>We could not find an application from this email address for this job.</p>"
            else:
                items = []
                for r in records:
                    if r["confirmation_visible"]:
                        items.append(
                            f"<li>Application received on {esc(r['received_at'])}. "
                            f"Confirmation reference: <strong>{esc(r['confirmation_reference'])}</strong>. "
                            f'<a href="/applications/{r["submission_id"]}">View confirmation</a></li>'
                        )
                    else:
                        items.append(
                            "<li>We are still processing a recent application from this email "
                            "address and cannot confirm it yet. Check back later.</li>"
                        )
                message = f"<ul>{''.join(items)}</ul>"
            result = (
                '<section role="status" aria-labelledby="status-result-title">'
                f'<h2 id="status-result-title">Status for {esc(email)}</h2>{message}</section>'
            )
        body = (
            _job_heading(job)
            + "<h2>Check your application status</h2>"
            + f'<form method="get" action="/jobs/{job.slug}/application-status">'
            '<div class="field"><label for="f-status-email">Email used on your application</label>'
            f'<input type="email" id="f-status-email" name="email" value="{esc(email)}" '
            'autocomplete="email" required></div>'
            '<button type="submit">Check status</button></form>'
            + result
        )
        self._send_html(HTTPStatus.OK, page(f"Application status: {job.title}", body))

    # sign-in
    def _login_page(self, next_path: str, error: str | None, email: str, status: HTTPStatus) -> None:
        alert = f'<div class="error-summary" role="alert"><p>{esc(error)}</p></div>' if error else ""
        body = (
            "<h1>Sign in to continue your application</h1>"
            + alert
            + '<form method="post" action="/login">'
            f'<input type="hidden" name="next" value="{esc(next_path)}">'
            '<div class="field"><label for="f-login-email">Email</label>'
            f'<input type="email" id="f-login-email" name="email" value="{esc(email)}" '
            'autocomplete="username" required></div>'
            '<div class="field"><label for="f-login-password">Password</label>'
            '<input type="password" id="f-login-password" name="password" '
            'autocomplete="current-password" required></div>'
            '<button type="submit">Sign in</button></form>'
        )
        self._send_html(status, page("Sign in", body))

    @staticmethod
    def _safe_next(value: str) -> str:
        return value if value.startswith("/") and not value.startswith("//") else "/"

    def get_login(self) -> None:
        next_path = self._safe_next((self.query.get("next") or ["/"])[0])
        self._login_page(next_path, None, "", HTTPStatus.OK)

    def post_login(self) -> None:
        form, _ = self._read_form()
        email = (form.get("email") or [""])[0].strip()
        password = (form.get("password") or [""])[0]
        next_path = self._safe_next((form.get("next") or ["/"])[0])
        if email.lower() != SIGNIN_EMAIL or password != SIGNIN_PASSWORD:
            self._login_page(
                next_path, "Incorrect email or password.", email, HTTPStatus.UNAUTHORIZED
            )
            return
        token = self.store.new_session(email.lower())
        cookie = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"
        self._redirect(next_path, (("Set-Cookie", cookie),))

    def get_captcha_svg(self, token: str) -> None:
        challenge = self.store.captcha(token)
        if challenge is None:
            raise HttpError(HTTPStatus.NOT_FOUND)
        self._send(HTTPStatus.OK, captcha_svg(challenge["answer"]).encode(), "image/svg+xml")

    # test-only API: assertions and fixture control, never product runtime
    def test_health(self) -> None:
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "origin": self.app.origin, "state_dir": str(self.store.state_dir)},
        )

    def test_jobs(self) -> None:
        self._send_json(
            HTTPStatus.OK,
            {
                "company": COMPANY,
                "signin": {"email": SIGNIN_EMAIL, "password": SIGNIN_PASSWORD},
                "jobs": [job.describe() for job in JOBS.values()],
            },
        )

    def test_submissions(self) -> None:
        job_id = (self.query.get("job_id") or [None])[0]
        self._send_json(HTTPStatus.OK, self.store.summary(job_id))

    def test_submission(self, submission_id: str) -> None:
        record = self.store.get_submission(submission_id)
        if record is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "no such submission")
        self._send_json(HTTPStatus.OK, record)

    def test_reveal(self, submission_id: str) -> None:
        self._read_body()
        record = self.store.reveal(submission_id)
        if record is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "no such submission")
        self._send_json(HTTPStatus.OK, record)

    def test_captcha(self, token: str) -> None:
        challenge = self.store.captcha(token)
        if challenge is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "no such captcha")
        self._send_json(HTTPStatus.OK, {"token": token, **challenge})

    def test_reset(self) -> None:
        self._read_body()
        self.store.reset()
        self._send_json(HTTPStatus.OK, {"reset": True})

    def test_shutdown(self) -> None:
        self._read_body()
        self._send_json(HTTPStatus.OK, {"stopping": True})
        threading.Thread(target=self.server.shutdown, daemon=True).start()


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------


def _check_loopback(host: str) -> None:
    if host == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(f"host must be localhost or an IPv4 loopback address, got {host!r}") from None
    if address.version != 4 or not address.is_loopback:
        raise ValueError(f"host must be localhost or an IPv4 loopback address, got {host!r}")


class MockATS:
    """In-process handle: ``MockATS(port=0, state_dir=...).start()`` then ``.origin``."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        state_dir: str | Path | None = None,
        verbose: bool = False,
    ):
        _check_loopback(host)
        self.state_dir = Path(state_dir or tempfile.mkdtemp(prefix="mock-ats-")).resolve()
        self.store = Store(self.state_dir)
        self.verbose = verbose
        self.httpd = _Server((host, port), self)
        self.port: int = self.httpd.server_address[1]
        self.origin = f"http://{host}:{self.port}"
        self._thread: threading.Thread | None = None

    def serve_forever(self) -> None:
        self.httpd.serve_forever(poll_interval=0.1)

    def start(self) -> "MockATS":
        self._thread = threading.Thread(target=self.serve_forever, name="mock-ats", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is not None:
            self.httpd.shutdown()
            self._thread.join()
            self._thread = None
        self.httpd.server_close()

    def __enter__(self) -> "MockATS":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic localhost mock ATS (fictional Brambleway Analytics)."
    )
    parser.add_argument("--host", default="127.0.0.1", help="loopback host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0, help="port; 0 picks a free one (default)")
    parser.add_argument(
        "--state-dir", help="directory for state.json and uploads (default: new temp directory)"
    )
    parser.add_argument(
        "--ready-file", help="write {origin, state_dir, pid} JSON here once listening"
    )
    parser.add_argument("--verbose", action="store_true", help="log requests to stderr")
    args = parser.parse_args(argv)

    try:
        ats = MockATS(args.host, args.port, args.state_dir, args.verbose)
    except (ValueError, OSError) as exc:
        print(f"mock_ats: {exc}", file=sys.stderr)
        return 2

    def request_stop(signum: int, frame: object) -> None:
        threading.Thread(target=ats.httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    ready_file = Path(args.ready_file).resolve() if args.ready_file else None
    if ready_file:
        tmp = ready_file.with_name(ready_file.name + ".tmp")
        tmp.write_text(
            json.dumps({"origin": ats.origin, "state_dir": str(ats.state_dir), "pid": os.getpid()}),
            "utf-8",
        )
        os.replace(tmp, ready_file)
    print(f"MOCK_ATS_ORIGIN={ats.origin}", flush=True)
    print(f"MOCK_ATS_STATE_DIR={ats.state_dir}", flush=True)
    print(f"MOCK_ATS_PID={os.getpid()}", flush=True)
    try:
        ats.serve_forever()
    finally:
        ats.httpd.server_close()
        if ready_file:
            ready_file.unlink(missing_ok=True)
    print("MOCK_ATS_STOPPED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
