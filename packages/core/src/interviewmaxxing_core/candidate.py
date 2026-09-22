"""Verified candidate data: identity, factual evidence, resume and saved answers.

The candidate package loads these from the local profile directory. Nothing here is
inferred: identity and saved answers are supplied by the user, and each fact carries
an explicit ``FactVerification``. ``CandidateFact.confidence`` is extraction
confidence only and is never proof; only ``verification.status == VERIFIED`` is.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, field_validator, model_validator

from ._base import Confidence, Contract, NonEmptyStr, UtcDatetime
from .artifacts import ArtifactRef
from .forms import SemanticType
from .jobs import JobRecord
from .urls import normalize_application_url

JsonScalar = str | int | float | bool
FactValue = JsonScalar | list[str] | None
SavedAnswerValue = str | bool | int | float | list[str]


class PostalAddress(Contract):
    street: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country: str | None = None


class CandidateIdentity(Contract):
    """Contact details the user has verified. ``verified_at`` is required."""

    first_name: NonEmptyStr
    last_name: NonEmptyStr
    preferred_name: str | None = None
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str | None = None
    address: PostalAddress = Field(default_factory=PostalAddress)
    linkedin_url: str | None = None
    website_url: str | None = None
    github_url: str | None = None
    verified_at: UtcDatetime

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class VerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"


class VerificationMethod(StrEnum):
    USER_STATED = "USER_STATED"
    """The user entered the fact themselves."""
    USER_CONFIRMED = "USER_CONFIRMED"
    """The fact was extracted (e.g. from the resume) and the user explicitly confirmed it."""


class FactVerification(Contract):
    """Whether the user has confirmed a fact. VERIFIED requires the method and time;
    UNVERIFIED must carry neither. Loaders never upgrade UNVERIFIED to VERIFIED."""

    status: VerificationStatus
    method: VerificationMethod | None = None
    verified_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.status is VerificationStatus.VERIFIED:
            if self.method is None or self.verified_at is None:
                raise ValueError("VERIFIED facts need method and verified_at")
        elif self.method is not None or self.verified_at is not None:
            raise ValueError("UNVERIFIED facts must not carry method or verified_at")
        return self


class CandidateFact(Contract):
    """A factual claim about the candidate with its source. Every generated claim
    must trace back to one or more *verified* facts (ARCHITECTURE.md section 5)."""

    id: NonEmptyStr
    key: NonEmptyStr
    value: FactValue
    source: NonEmptyStr
    """Where the fact came from, e.g. ``resume``, ``user``."""
    verification: FactVerification
    """Required and explicit; there is no default."""
    confidence: Confidence = 1.0
    """Extraction confidence only. Never evidence that the fact is true."""
    evidence: list[str] = Field(default_factory=list)
    """Quotes or locations supporting the fact, e.g. a resume line."""

    @property
    def is_verified(self) -> bool:
        return self.verification.status is VerificationStatus.VERIFIED


class ResumeArtifact(ArtifactRef):
    """The resume the user supplied. The MVP uses it as-is."""

    variant: NonEmptyStr = "supplied"
    extracted_text: str | None = None


class AnswerScope(StrEnum):
    """Where a saved answer may be reused. There is no default: the user chooses."""

    GLOBAL = "GLOBAL"
    """Explicitly reusable for any employer and job (e.g. work authorization)."""
    JOB = "JOB"
    """Only for one job, identified by its ATS identity key or application URL."""


class SavedAnswer(Contract):
    """An answer the user explicitly gave for reuse.

    Only saved answers or direct user input may answer the types in
    ``forms.EXPLICIT_ANSWER_REQUIRED``. ``value`` is expressed in the user's terms
    (e.g. ``"Yes"``, ``False`` or a list of labels); the packet resolver maps it to a
    form's option values and must report ambiguity rather than guess.

    ``scope`` is explicit. A ``JOB`` answer applies only to that job
    (``applies_to``); it is never promoted to another job or employer. Answers given
    during one application stay local to it unless the user chose a reuse scope
    (``packets.UserInput.reuse``).
    """

    id: NonEmptyStr
    scope: AnswerScope
    job_identity_key: str | None = None
    """For JOB scope: ``JobIdentityObservation.identity_key`` of the job. Preferred."""
    job_url: str | None = None
    """For JOB scope: the job's application URL (stored normalized)."""
    employer: str | None = None
    """Informational label for JOB answers; never used for matching."""
    semantic_type: SemanticType | None = None
    question: NonEmptyStr
    """The question as the user answered it."""
    match_phrases: list[str] = Field(default_factory=list)
    """Extra question phrasings this answer is meant for (case-insensitive)."""
    value: SavedAnswerValue
    confirmed_at: UtcDatetime

    @field_validator("job_url")
    @classmethod
    def _normalize_url(cls, value: str | None) -> str | None:
        return normalize_application_url(value) if value else None

    @model_validator(mode="after")
    def _scope_target(self) -> Self:
        has_target = bool(self.job_identity_key or self.job_url)
        if self.scope is AnswerScope.JOB and not has_target:
            raise ValueError("JOB-scoped answers need job_identity_key or job_url")
        if self.scope is AnswerScope.GLOBAL and (has_target or self.employer):
            raise ValueError("GLOBAL answers must not name a job or employer")
        return self

    def applies_to(self, job: JobRecord) -> bool:
        """True if this answer may be used for ``job``. JOB answers with an identity
        key match only that identity; otherwise only the same normalized URL."""
        if self.scope is AnswerScope.GLOBAL:
            return True
        if self.job_identity_key:
            return job.identity_key == self.job_identity_key
        return job.normalized_url == self.job_url


class Experience(Contract):
    id: NonEmptyStr
    company: NonEmptyStr
    title: NonEmptyStr
    start: str | None = None
    """``YYYY-MM`` or ``YYYY``."""
    end: str | None = None
    current: bool = False
    summary: str | None = None
    fact_ids: list[str] = Field(default_factory=list)


class Education(Contract):
    id: NonEmptyStr
    institution: NonEmptyStr
    degree: str | None = None
    field_of_study: str | None = None
    graduation: str | None = None
    fact_ids: list[str] = Field(default_factory=list)


class CandidateProfile(Contract):
    id: NonEmptyStr
    identity: CandidateIdentity
    resume: ResumeArtifact
    facts: list[CandidateFact] = Field(default_factory=list)
    saved_answers: list[SavedAnswer] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)

    @model_validator(mode="after")
    def _references_resolve(self) -> Self:
        fact_ids = [f.id for f in self.facts]
        if len(set(fact_ids)) != len(fact_ids):
            raise ValueError("duplicate candidate fact ids")
        answer_ids = [a.id for a in self.saved_answers]
        if len(set(answer_ids)) != len(answer_ids):
            raise ValueError("duplicate saved answer ids")
        known = set(fact_ids)
        refs = [(e.id, e.fact_ids) for e in self.experience]
        refs += [(e.id, e.fact_ids) for e in self.education]
        for item_id, ids in refs:
            missing = [i for i in ids if i not in known]
            if missing:
                raise ValueError(f"{item_id} references unknown facts {missing}")
        return self

    def fact(self, fact_id: str) -> CandidateFact:
        for f in self.facts:
            if f.id == fact_id:
                return f
        raise KeyError(fact_id)

    def find_fact(self, fact_id: str) -> CandidateFact | None:
        return next((f for f in self.facts if f.id == fact_id), None)

    def find_saved_answer(self, answer_id: str) -> SavedAnswer | None:
        return next((a for a in self.saved_answers if a.id == answer_id), None)

    def verified_facts(self) -> list[CandidateFact]:
        return [f for f in self.facts if f.is_verified]

    def verified_only(self) -> CandidateProfile:
        """A copy without unverified facts (and without references to them)."""
        keep = {f.id for f in self.verified_facts()}
        return self.model_copy(
            update={
                "facts": self.verified_facts(),
                "experience": [
                    e.model_copy(update={"fact_ids": [i for i in e.fact_ids if i in keep]})
                    for e in self.experience
                ],
                "education": [
                    e.model_copy(update={"fact_ids": [i for i in e.fact_ids if i in keep]})
                    for e in self.education
                ],
            }
        )

    def saved_answers_for(
        self, semantic_type: SemanticType, *, job: JobRecord
    ) -> list[SavedAnswer]:
        """Saved answers of this type that may be used for ``job`` (scope-checked)."""
        return [
            a for a in self.saved_answers if a.semantic_type is semantic_type and a.applies_to(job)
        ]

    def applicable_saved_answers(self, job: JobRecord) -> list[SavedAnswer]:
        return [a for a in self.saved_answers if a.applies_to(job)]
