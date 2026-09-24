"""Application packets: factual answers for an inspected form step.

``FactualPacketResolver`` implements ``interviewmaxxing_core.PacketResolver``. It
answers from the user's inputs, exactly matching saved answers, the supplied resume,
the verified identity and verified facts, and reports everything else that is
required as a scoped ``MissingInput``. See the package README. Lookup controls get
the candidate's own location from the verified identity (``contact``), and phone
controls with a country picker get the number in international form.
"""

from .contact import PhoneFormatError, international_phone, lookup_text
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
    StoredValue,
    missing_input_id,
    resolve_packet,
    stored_value,
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
    "PhoneFormatError",
    "ProposedClaim",
    "QuestionText",
    "ResumeVariantPlan",
    "StoredValue",
    "SupportedClaim",
    "WritingBrief",
    "WritingProposal",
    "WritingProvider",
    "build_document_bundle",
    "display_question",
    "document_scope",
    "international_phone",
    "is_neutral_hint",
    "lookup_text",
    "missing_input_id",
    "plan_resume_variant",
    "question_key",
    "resolve_packet",
    "saved_answer_matches",
    "stored_value",
    "wording_key",
]
