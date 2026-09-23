"""Application packets: factual answers for an inspected form step.

``FactualPacketResolver`` implements ``interviewmaxxing_core.PacketResolver``. It
answers from the user's inputs, exactly matching saved answers, the supplied resume,
the verified identity and verified facts, and reports everything else that is
required as a scoped ``MissingInput``. See the package README.
"""

from .documents import (
    DocumentArtifact,
    DocumentBundle,
    DocumentIssue,
    DocumentScope,
    FactCitation,
    JobDocumentEvidence,
    ProposedClaim,
    ResumeVariantPlan,
    SupportedClaim,
    WritingBrief,
    WritingProposal,
    WritingProvider,
    build_document_bundle,
    document_scope,
    plan_resume_variant,
)
from .questions import (
    QuestionText,
    display_question,
    is_neutral_hint,
    question_key,
    saved_answer_matches,
    wording_key,
)
from .resolver import (
    FactualPacketResolver,
    PacketResolutionError,
    missing_input_id,
    resolve_packet,
)

__all__ = [
    "DocumentArtifact",
    "DocumentBundle",
    "DocumentIssue",
    "DocumentScope",
    "FactCitation",
    "FactualPacketResolver",
    "JobDocumentEvidence",
    "PacketResolutionError",
    "ProposedClaim",
    "QuestionText",
    "ResumeVariantPlan",
    "SupportedClaim",
    "WritingBrief",
    "WritingProposal",
    "WritingProvider",
    "build_document_bundle",
    "display_question",
    "document_scope",
    "is_neutral_hint",
    "missing_input_id",
    "plan_resume_variant",
    "question_key",
    "resolve_packet",
    "saved_answer_matches",
    "wording_key",
]
