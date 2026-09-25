"""``GENERATED_FROM_QUESTION`` (WP12 round 5, addendum item 7): an answer computed from the
data a question itself shows references that question's recorded content, and packet
validation re-derives the reference from the inspected field. Fictional fixtures only."""
from __future__ import annotations

from interviewmaxxing_core import (
    AnswerSource,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    CandidateProfile,
    ControlType,
    JobRecord,
    PacketAnswer,
    Provenance,
    SemanticType,
    TextValue,
    provenance_problems,
    question_content_ref,
)


def _form(section_context: list[str]) -> ApplicationForm:
    return ApplicationForm(url="https://synthetic.test/apply", fields=[ApplicationField(
        id="case", label="Calculate CPA and ROAS for each channel.", selector="#case",
        semantic_type=SemanticType.CUSTOM_LONG_TEXT, control_type=ControlType.TEXTAREA,
        section_context=section_context)])


def _packet(candidate: CandidateProfile, job: JobRecord, form: ApplicationForm, refs: list[str]) -> ApplicationPacket:
    answer = PacketAnswer(field_id="case", semantic_type=SemanticType.CUSTOM_LONG_TEXT,
                          value=TextValue(text="Search CPA = $5,000 / 100 = $50."),
                          provenance=Provenance(source=AnswerSource.GENERATED_FROM_QUESTION, reference_ids=refs))
    return ApplicationPacket(application_id="app-case", job_id=job.id, candidate_id=candidate.id,
                             form_url=form.url, form_step=0, form_fingerprint=form.fingerprint, answers=[answer])


def test_a_computed_answer_references_the_questions_own_content(fictional_candidate: CandidateProfile,
                                                                 mock_job: JobRecord) -> None:
    form = _form(["Search | $5,000 | 100 | $12,500"])
    field = form.fields[0]
    ref = question_content_ref(field)
    assert ref.startswith("form:") and len(ref) == len("form:") + 64
    assert provenance_problems(_packet(fictional_candidate, mock_job, form, [ref]), form=form,
                               candidate=fictional_candidate, job=mock_job) == []
    # Another recording of the question (the data changed) is another reference.
    other = _form(["Search | $6,000 | 100 | $12,500"])
    assert question_content_ref(other.fields[0]) != ref
    for refs in ([question_content_ref(other.fields[0])], [ref, ref], ["fact.bakery"]):
        [problem] = provenance_problems(_packet(fictional_candidate, mock_job, form, refs), form=form,
                                        candidate=fictional_candidate, job=mock_job)
        assert "the question's own recorded content" in problem
