"""Strict request boundaries for the private interview practice API."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr


class InterviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: StrictStr = Field(default="", max_length=300)
    title: StrictStr = Field(default="", max_length=300)
    companyUrl: StrictStr = Field(default="", max_length=2000)
    jobUrl: StrictStr = Field(default="", max_length=2000)
    jobDescription: StrictStr = Field(default="", max_length=100000)
    round: StrictInt = Field(default=2, ge=1, le=10)
    persona: StrictStr = Field(default="hiring manager", max_length=200)
    durationMinutes: StrictInt = Field(default=30, ge=1, le=180)
    resumeText: StrictStr = Field(default="", max_length=100000)
    linkedinText: StrictStr = Field(default="", max_length=50000)
    notes: StrictStr = Field(default="", max_length=100000)
    targetScore: StrictInt = Field(default=80, ge=0, le=100)
    previousSessionId: StrictStr | None = None


class InterviewDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["resume", "notes", "linkedin", "prior_transcript", "prior_recording"]
    name: StrictStr = Field(min_length=1, max_length=200)
    text: StrictStr = Field(min_length=1, max_length=300000)


class InterviewTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: StrictStr = Field(min_length=1, max_length=30000)
    requestId: StrictStr = Field(min_length=1, max_length=128)
    question: StrictStr | None = Field(default=None, max_length=10000)
