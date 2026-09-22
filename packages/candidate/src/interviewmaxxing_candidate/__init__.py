"""Verified candidate data from local storage (candidate-brain).

``LocalCandidateStore`` implements the core ``CandidateLoader`` and
``SavedAnswerWriter`` interfaces. See this package's README for the file layout.
"""

from .answers import (
    AnswerConflict,
    AnswerReconciliation,
    SupersededAnswer,
    question_key,
    reconcile_saved_answers,
)
from .resumes import (
    DEFAULT_MAX_RESUME_BYTES,
    RESUME_UPLOAD_TYPES,
    DamagedResume,
    ResumeNotFound,
    ResumeOrigin,
    ResumeRejected,
    StoredResume,
    safe_resume_filename,
)
from .store import (
    ANSWERS_FILENAME,
    PROFILE_FILENAME,
    RESUME_MEDIA_TYPES,
    CandidateLoadReport,
    CandidateSetup,
    LocalCandidateStore,
    SavedAnswerRejected,
)

__all__ = [
    "ANSWERS_FILENAME",
    "DEFAULT_MAX_RESUME_BYTES",
    "PROFILE_FILENAME",
    "RESUME_MEDIA_TYPES",
    "RESUME_UPLOAD_TYPES",
    "AnswerConflict",
    "AnswerReconciliation",
    "CandidateLoadReport",
    "CandidateSetup",
    "DamagedResume",
    "LocalCandidateStore",
    "ResumeNotFound",
    "ResumeOrigin",
    "ResumeRejected",
    "SavedAnswerRejected",
    "StoredResume",
    "SupersededAnswer",
    "question_key",
    "reconcile_saved_answers",
    "safe_resume_filename",
]
