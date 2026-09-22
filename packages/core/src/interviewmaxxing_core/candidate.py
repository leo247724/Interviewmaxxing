"""Verified candidate data: identity, factual evidence, resume and saved answers.

The candidate package loads these from the local profile directory. Everything here
is supplied or confirmed by the user; nothing is inferred.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from ._base import Confidence, Contract, NonEmptyStr, UtcDatetime
from .artifacts import ArtifactRef
from .forms import SemanticType

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


class CandidateFact(Contract):
    """A factual claim about the candidate with its source. Every generated claim
    must trace back to one or more facts (ARCHITECTURE.md section 5)."""

    id: NonEmptyStr
    key: NonEmptyStr
    value: FactValue
    source: NonEmptyStr
    """Where the fact came from, e.g. ``resume``, ``user``."""
    confidence: Confidence = 1.0
    evidence: list[str] = Field(default_factory=list)
    """Quotes or locations supporting the fact, e.g. a resume line."""


class ResumeArtifact(ArtifactRef):
    """The resume the user supplied. The MVP uses it as-is."""

    variant: NonEmptyStr = "supplied"
    extracted_text: str | None = None


class SavedAnswer(Contract):
    """An answer the user explicitly gave for reuse across applications.

    Only saved answers or direct user input may answer the types in
    ``forms.EXPLICIT_ANSWER_REQUIRED``. ``value`` is expressed in the user's terms
    (e.g. ``"Yes"``, ``False`` or a list of labels); the packet resolver maps it to a
    form's option values and must report ambiguity rather than guess.
    """

    id: NonEmptyStr
    semantic_type: SemanticType | None = None
    question: NonEmptyStr
    """The question as the user answered it."""
    match_phrases: list[str] = Field(default_factory=list)
    """Extra question phrasings this answer is meant for (case-insensitive)."""
    value: SavedAnswerValue
    confirmed_at: UtcDatetime


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

    def saved_answers_for(self, semantic_type: SemanticType) -> list[SavedAnswer]:
        return [a for a in self.saved_answers if a.semantic_type is semantic_type]
