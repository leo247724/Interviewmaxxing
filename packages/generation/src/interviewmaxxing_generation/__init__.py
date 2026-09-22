"""Application packets: factual answers for an inspected form step.

``FactualPacketResolver`` implements ``interviewmaxxing_core.PacketResolver``. It
answers from the user's inputs, exactly matching saved answers, the supplied resume,
the verified identity and verified facts, and reports everything else that is
required as a scoped ``MissingInput``. See the package README.
"""

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
    "FactualPacketResolver",
    "PacketResolutionError",
    "QuestionText",
    "display_question",
    "is_neutral_hint",
    "missing_input_id",
    "question_key",
    "resolve_packet",
    "saved_answer_matches",
    "wording_key",
]
