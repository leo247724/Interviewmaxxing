"""Full-form policy gates, including already deterministically typed fields."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from interviewmaxxing_browser.ai import (
    AIFormRouter,
    BoundedDecisions,
    DynamicPacketResolver,
    FieldRoute,
)
from interviewmaxxing_core import (
    Application,
    ApplicationField,
    ApplicationForm,
    ApplicationState,
    CandidateProfile,
    ControlType,
    JobRecord,
    PacketContext,
    SemanticType,
)
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient


class BatchProvider:
    def __init__(self, *, route: str = "COPY_KNOWN", narrative: str = "literal",
                 semantic: str = "CUSTOM_TEXT", scope: str = "APPLICANT_CURRENT",
                 purpose: str = "APPLICATION_ATTACHMENT") -> None:
        self.route, self.narrative, self.semantic = route, narrative, semantic
        self.scope, self.purpose = scope, purpose
        self.requests: list[dict[str, Any]] = []

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        self.requests.append(request)
        answers = {}
        for key, q in request["questions"].items():
            assert q["type"] == "choice"
            choice = (self.purpose if key.startswith("d") else self.scope if key.startswith("u") else self.route if key.startswith("r")
                      else self.narrative if key.startswith("n") else self.semantic)
            answers[key] = {"type": "choice", "choice": choice, "confidence": 1,
                            "probabilities": {option: float(option == choice) for option in q["criteria"]}}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())


def router(provider: BatchProvider) -> AIFormRouter:
    return AIFormRouter(BoundedDecisions(JevClient(ApiKey("synthetic-test", source="fixture"),
        transport=provider, max_attempts=1)), batch_size=2)


def make_form(semantic: SemanticType = SemanticType.FIRST_NAME, *, count: int = 1) -> ApplicationForm:
    return ApplicationForm(url="https://synthetic.test/apply", fields=[ApplicationField(
        id=f"field{i}", selector=f"#field{i}", label="Given name", semantic_type=semantic,
        control_type=ControlType.TEXT, required=True) for i in range(count)])


def make_context(f: ApplicationForm, candidate: CandidateProfile, job: JobRecord) -> PacketContext:
    return PacketContext(form=f, candidate=candidate, job=job, application=Application(
        id="app-full-form", request_id="request-full-form", job_id=job.id,
        candidate_id=candidate.id, state=ApplicationState.INSPECTING, version=1,
        created_at="2026-09-23T00:00:00Z", updated_at="2026-09-23T00:00:00Z"))


def test_all_fields_are_classified_in_bounded_batches_with_full_context() -> None:
    provider = BatchProvider()
    r = router(provider)
    f = make_form(count=5)
    report = r.classify_form(f, document_id="document")
    assert len(provider.requests) == 3
    assert len(report.fields) == 5
    assert all(len(q["state"]["fields"]) == 5 for q in provider.requests)
    assert all(d.route is FieldRoute.COPY_KNOWN for d in report.fields)
    assert all(d.probabilities["COPY_KNOWN"] == 1 for d in report.fields)
    assert report.provider_calls == 3
    assert r.classify_form(f, document_id="document").provider_calls == 0
    assert len(provider.requests) == 3
    assert r.report_for(f) == report


def test_narrative_gate_blocks_model_copy_even_for_known_identity_type() -> None:
    r = router(BatchProvider(route="COPY_KNOWN", narrative="prose"))
    decision = r.classify_form(make_form(), document_id="d").fields[0]
    assert decision.proposed_route is FieldRoute.COPY_KNOWN
    assert decision.narrative_probability == 1
    assert decision.route is FieldRoute.WRITER


def test_ambiguous_route_cannot_leak_deterministic_identity_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    r = router(BatchProvider(route="COPY_KNOWN", narrative="prose"))
    f = r.annotate(make_form(), document_id="d")
    packet = asyncio.run(DynamicPacketResolver(r.decisions, router=r).resolve(
        make_context(f, fictional_candidate, mock_job)))
    assert not packet.answers
    assert packet.missing_inputs[0].field_id == "field0"


def test_writer_route_cannot_reuse_scalar_factual_resolver_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = BatchProvider(route="WRITER", narrative="prose")
    r = router(provider)
    f = make_form(SemanticType.CURRENT_TITLE)
    f = f.model_copy(update={"fields": [f.fields[0].model_copy(update={
        "label": "Describe your career background and why it fits this role"})]})
    annotated = r.annotate(f, document_id="d")
    assert annotated.fields[0].semantic_type is SemanticType.CUSTOM_LONG_TEXT
    packet = asyncio.run(DynamicPacketResolver(r.decisions, router=r).resolve(
        make_context(annotated, fictional_candidate, mock_job)))
    assert not packet.answers  # Writer not configured; current title cannot stand in for prose.
    assert "writer is not configured" in packet.missing_inputs[0].prompt


def test_demographics_require_explicit_saved_answer_not_identity_inference(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    r = router(BatchProvider())
    f = make_form(SemanticType.EEO_GENDER)
    f = f.model_copy(update={"fields": [f.fields[0].model_copy(update={"label": "Gender"})]})
    annotated = r.annotate(f, document_id="d")
    report = r.report_for(annotated)
    assert report.fields[0].route is FieldRoute.COPY_KNOWN
    assert "only explicit" in report.fields[0].source_requirement
    packet = asyncio.run(DynamicPacketResolver(r.decisions, router=r).resolve(
        make_context(annotated, fictional_candidate, mock_job)))
    assert not packet.answers
    assert not packet.is_complete


def test_disabled_options_and_document_identity_invalidate_report_cache() -> None:
    r = router(BatchProvider())
    f = make_form()
    r.classify_form(f, document_id="one")
    r.classify_form(f, document_id="two")
    changed = f.model_copy(update={"fields": [f.fields[0].model_copy(update={"max_length": 10})]})
    assert r.report_for(changed) is None
    r.classify_form(changed, document_id="two")
    assert len(r.decisions.client.transport.requests) == 3


def test_readiness_uses_canonical_scoped_answer_for_demographic() -> None:
    from interviewmaxxing_browser.ai.evaluation import canonical_readiness
    fld = make_form(SemanticType.EEO_GENDER).fields[0].model_copy(update={"label": "Gender"})
    absent = canonical_readiness({"facts_available": [{"source_kind": "missing"}]},
                                 fld, FieldRoute.COPY_KNOWN, url="https://synthetic.test/apply")
    assert absent["final_route"] == "HUMAN_INPUT"
    supplied = canonical_readiness({"facts_available": [{"source_kind": "saved_answer",
        "fictional": True, "verified": True, "value": "Prefer not to say",
        "scope": "GLOBAL", "question": "Gender", "semantic_type": "EEO_GENDER"}]},
        fld, FieldRoute.COPY_KNOWN, url="https://synthetic.test/apply")
    assert supplied["final_route"] == "COPY_KNOWN"
    assert supplied["answer_source"] == "SAVED_ANSWER"
    assert supplied["canonical_problems"] == []


def test_readiness_never_copies_placeholder_or_wrong_source() -> None:
    from interviewmaxxing_browser.ai.evaluation import canonical_readiness
    fld = make_form().fields[0]
    answer = canonical_readiness({"facts_available": [{"source_kind": "identity",
        "semantic_type": "EMAIL", "fictional": True, "verified": True,
        "value": "fictional@example.test"}]}, fld, FieldRoute.COPY_KNOWN,
        url="https://synthetic.test/apply")
    assert answer["final_route"] == "HUMAN_INPUT"


def test_corpus_does_not_invent_missing_option_values() -> None:
    from interviewmaxxing_browser.ai.evaluation import corpus_form
    f, hints = corpus_form([{"id": "blank-select", "control_type": "SELECT", "question": "Country",
        "options": [{"label": "United States", "value": None}], "options_complete": False,
        "source_url": "https://synthetic.test/apply"}])
    assert f.fields[0].control_type is ControlType.UNSUPPORTED
    assert f.fields[0].options is None
    assert hints["observed_widget_metadata"]["blank-select"]["observed_options"][0]["value"] is None


def test_readiness_does_not_manufacture_saved_answer_scope_or_semantics() -> None:
    from interviewmaxxing_browser.ai.evaluation import canonical_readiness
    fld = make_form(SemanticType.CONSENT).fields[0].model_copy(update={
        "label": "I authorize a background check."})
    base = {"source_kind": "saved_answer", "fictional": True, "verified": True,
        "value": "Yes", "scope": "GLOBAL", "question": fld.question_text,
        "semantic_type": "CONSENT"}
    for change in ({"semantic_type": "EMAIL"}, {"question": "What is your prior recruiter email?"},
                   {"scope": "job:unrelated"}, {"scope": "JOB", "job_identity_key": "wrong:job"},
                   {"verified": False}, {"scope": None}):
        result = canonical_readiness({"facts_available": [base | change]}, fld,
                                      FieldRoute.HUMAN_INPUT, url="https://synthetic.test/apply")
        assert result["final_route"] == "HUMAN_INPUT", change
    valid = canonical_readiness({"facts_available": [base]}, fld,
                                FieldRoute.HUMAN_INPUT, url="https://synthetic.test/apply")
    assert valid["final_route"] == "COPY_KNOWN"
    assert valid["answer_source"] == "SAVED_ANSWER"


def test_other_subject_cannot_copy_applicant_identity_or_current_employer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    from interviewmaxxing_generation.resolver import resolve_packet
    cases = [(SemanticType.EMAIL, "Email address of your most recent supervisor", "email", "OTHER_PERSON_OR_ENTITY"),
             (SemanticType.CURRENT_TITLE, "Current title of your professional reference", None, "OTHER_PERSON_OR_ENTITY"),
             (SemanticType.CURRENT_COMPANY, "Previous employer", None, "HISTORICAL_OR_CONTEXTUAL"),
             (SemanticType.CITY, "City", None, "OTHER_PERSON_OR_ENTITY")]
    for semantic, label, input_type, scope in cases:
        provider = BatchProvider(scope=scope)
        r = router(provider)
        f = make_form(semantic)
        f = f.model_copy(update={"fields": [f.fields[0].model_copy(update={"label": label,
            "input_type": input_type, "help_text": "Employment history: employer location" if semantic is SemanticType.CITY else None})]})
        ctx = make_context(f, fictional_candidate, mock_job)
        assert resolve_packet(ctx).answers  # Confirms the old deterministic seam supplies a value.
        annotated = r.annotate(f, document_id=scope + semantic.value)
        assert not r.report_for(annotated).fields[0].profile_copy_allowed
        # Suppress only the fact-selection transport: no fact expressly names these other subjects.
        r.decisions.budget.max_calls = r.decisions.budget.calls
        packet = asyncio.run(DynamicPacketResolver(r.decisions, router=r).resolve(
            make_context(annotated, fictional_candidate, mock_job)))
        assert not packet.answers, semantic
        assert not packet.is_complete


def test_autofill_resume_parser_is_not_an_application_attachment(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    for label, purpose, should_answer in [("Autofill from resume", "AUTOFILL_PARSER", False),
                                         ("Attach Resume/CV", "APPLICATION_ATTACHMENT", True)]:
        r = router(BatchProvider(route="APPROVED_DOCUMENT", purpose=purpose))
        f = make_form(SemanticType.RESUME)
        f = f.model_copy(update={"fields": [f.fields[0].model_copy(update={
            "label": label, "control_type": ControlType.FILE})]})
        annotated = r.annotate(f, document_id=purpose)
        packet = asyncio.run(DynamicPacketResolver(r.decisions, router=r).resolve(
            make_context(annotated, fictional_candidate, mock_job)))
        assert bool(packet.answers) is should_answer
        if not should_answer:
            assert r.report_for(annotated).fields[0].route is FieldRoute.UNSUPPORTED


def test_approved_attachment_gate_does_not_invent_missing_file(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    r = router(BatchProvider(route="APPROVED_DOCUMENT"))
    f = make_form(SemanticType.RESUME)
    f = f.model_copy(update={"fields": [f.fields[0].model_copy(update={
        "label": "Resume", "control_type": ControlType.FILE})]})
    candidate = fictional_candidate.model_copy(update={"resume": fictional_candidate.resume.model_copy(
        update={"path": "/nonexistent/synthetic-resume.pdf"})})
    annotated = r.annotate(f, document_id="file")
    packet = asyncio.run(DynamicPacketResolver(r.decisions, router=r).resolve(make_context(annotated, candidate, mock_job)))
    assert not packet.answers and not packet.is_complete


def test_explicit_or_unclear_source_gate_blocks_writer_even_with_verified_fact(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    class NeverWriter:
        def write(self, **kwargs: Any) -> Any:
            raise AssertionError("Writer must not be called for an explicit-only source")

    for source in ("EXPLICIT_ANSWER", "UNCLEAR"):
        r = router(BatchProvider(route="WRITER", narrative="prose", scope=source))
        f = make_form(SemanticType.CUSTOM_LONG_TEXT)
        f = f.model_copy(update={"fields": [f.fields[0].model_copy(update={
            "label": "Why do you prefer remote work?"})]})
        annotated = r.annotate(f, document_id=source)
        packet = asyncio.run(DynamicPacketResolver(r.decisions, NeverWriter(), router=r).resolve(
            make_context(annotated, fictional_candidate, mock_job)))
        assert not packet.answers and not packet.is_complete


def test_field_payload_carries_section_context_separately_from_the_question() -> None:
    from interviewmaxxing_browser.ai.classification import field_data
    from interviewmaxxing_core import ApplicationField, ControlType

    fld = ApplicationField(id="email", label="Email", control_type=ControlType.TEXT, selector="#email",
                           section_context=["Supervisor contact"])
    data = field_data(fld)
    assert data["question"] == "Email"
    assert data["section_context"] == ["Supervisor contact"]
