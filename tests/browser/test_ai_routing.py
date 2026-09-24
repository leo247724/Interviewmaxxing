"""Synthetic semantic routing, provenance and provider boundaries."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from interviewmaxxing_browser.ai import (
    AIFormRouter,
    AIHold,
    BoundedDecisions,
    CallBudget,
    DynamicPacketResolver,
)
from interviewmaxxing_browser.ai.providers import NarrativeDraft, NarrativeWriter
from interviewmaxxing_core import (
    AnswerSource,
    Application,
    ApplicationField,
    ApplicationForm,
    ApplicationState,
    CandidateProfile,
    ControlType,
    FieldOption,
    JobRecord,
    PacketContext,
    SemanticType,
)
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient


class Provider:
    def __init__(self, choices: list[str], *, probability: float = 1.0,
                 support: float = 1.0, status: int = 200) -> None:
        self.choices = choices
        self.probability = probability
        self.support = support
        self.status = status
        self.requests: list[dict[str, Any]] = []

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        self.requests.append(request)
        assert url == "https://openrouter.ai/api/alpha/decisions"
        answers = {}
        for name, question in request["questions"].items():
            if question["type"] == "choice":
                criteria = question["criteria"]
                pending = self.choices[0] if self.choices else ""
                if name.startswith("u") and name[1:].isdigit():
                    choice = "APPLICANT_CURRENT"
                elif name.startswith("n") and name[1:].isdigit():
                    choice = "prose" if pending == "narrative" else "literal"
                elif name.startswith("r") and name[1:].isdigit():
                    choice = "WRITER" if pending == "narrative" else "COPY_KNOWN"
                elif name.startswith("s") and name[1:].isdigit():
                    choice = self.choices.pop(0) if pending.isupper() else "CUSTOM_TEXT"
                else:
                    choice = self.choices.pop(0)
                answers[name] = {"type": "choice", "choice": choice,
                    "confidence": self.probability, "probabilities": {
                        k: self.probability if k == choice else (1 - self.probability) / (len(criteria) - 1)
                        for k in criteria}}
            else:
                answers[name] = {"type": "noul", "noul": self.support}
        return HttpResponse(self.status, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())


def decisions(provider: Provider, budget: CallBudget | None = None) -> BoundedDecisions:
    return BoundedDecisions(JevClient(ApiKey("synthetic-key", source="test"),
        transport=provider, max_attempts=1), budget or CallBudget())


def form(*, label: str = "Describe a campaign result", semantic: SemanticType = SemanticType.CUSTOM_LONG_TEXT,
         **kwargs: Any) -> ApplicationForm:
    return ApplicationForm(url="https://example.test/apply", fields=[ApplicationField(
        id="answer", selector="#answer", label=label, semantic_type=semantic,
        control_type=kwargs.pop("control_type", ControlType.TEXTAREA), required=True, **kwargs)])


def context(f: ApplicationForm, candidate: CandidateProfile, job: JobRecord) -> PacketContext:
    app = Application(id="app-ai-test", request_id="request-ai-test", job_id=job.id,
        candidate_id=candidate.id, state=ApplicationState.INSPECTING, version=2,
        created_at="2026-09-23T00:00:00Z", updated_at="2026-09-23T00:00:00Z")
    return PacketContext(application=app, job=job, form=f, candidate=candidate)


def test_annotation_preserves_bindings_and_invalidates_cache() -> None:
    provider = Provider(["FIRST_NAME"] * 5)
    router = AIFormRouter(decisions(provider))
    f = form(label="Given name", semantic=SemanticType.UNKNOWN)
    mapped = router.annotate(f, document_id="document-1", schema_hints={"name": "hint"})
    assert mapped.field("answer").semantic_type is SemanticType.FIRST_NAME
    assert mapped.fingerprint == f.fingerprint
    assert mapped.field("answer").selector == "#answer"
    router.annotate(f, document_id="document-1", schema_hints={"name": "hint"})
    assert len(provider.requests) == 1
    router.annotate(f, document_id="document-2", schema_hints={"name": "hint"})
    router.annotate(f, document_id="document-2", schema_hints={"name": "changed"})
    changed = f.model_copy(update={"fields": [f.fields[0].model_copy(update={"selector": "#new"})]})
    router.annotate(changed, document_id="document-2", schema_hints={"name": "changed"})
    assert len(provider.requests) == 4
    assert "#answer" not in json.dumps(provider.requests)


def test_uncertain_semantic_is_held_and_sensitive_is_never_downgraded() -> None:
    router = AIFormRouter(decisions(Provider(["EMAIL"], probability=0.7)))
    assert router.annotate(form(), document_id="d").fields[0].semantic_type is SemanticType.UNKNOWN
    sensitive = form(semantic=SemanticType.ATTESTATION)
    assert router.annotate(sensitive, document_id="d") == sensitive


def test_exact_identity_is_copied_locally(fictional_candidate: CandidateProfile, mock_job: JobRecord) -> None:
    router = AIFormRouter(decisions(Provider(["FIRST_NAME"])))
    f = router.annotate(form(label="Given name", semantic=SemanticType.UNKNOWN), document_id="d")
    packet = asyncio.run(DynamicPacketResolver(router.decisions, router=router).resolve(context(f, fictional_candidate, mock_job)))
    answer = packet.answers[0]
    assert answer.value.text == fictional_candidate.identity.first_name
    assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY
    assert len(router.decisions.client.transport.requests) == 1


def test_fact_choice_copies_exact_value_and_checks_provenance(fictional_candidate: CandidateProfile,
                                                            mock_job: JobRecord) -> None:
    fact = fictional_candidate.verified_facts()[0]
    candidate = fictional_candidate.model_copy(update={"facts": [fact]})
    d = decisions(Provider(["f0"]))
    ctx = context(form(label="Name the verified specialty"), candidate, mock_job)
    packet = asyncio.run(DynamicPacketResolver(d).resolve(ctx))
    assert packet.answers[0].value.text == str(fact.value)
    assert packet.answers[0].provenance.reference_ids == [fact.id]
    assert ctx.problems(packet) == []
    changed = ctx.form.model_copy(update={"step": 1})
    assert packet.problems_against(changed)


def test_unknown_and_explicit_fields_never_send_candidate_facts(fictional_candidate: CandidateProfile,
                                                       mock_job: JobRecord) -> None:
    p = Provider([])
    resolver = DynamicPacketResolver(decisions(p))
    for semantic in (SemanticType.UNKNOWN, SemanticType.CONSENT, SemanticType.WORK_AUTHORIZATION):
        packet = asyncio.run(resolver.resolve(context(form(semantic=semantic), fictional_candidate, mock_job)))
        assert not packet.answers
        assert not packet.is_complete
    assert len(p.requests) == 3
    assert all("facts" not in request["state"] for request in p.requests)


def test_option_copy_rejects_disabled_or_nonmatching_option(fictional_candidate: CandidateProfile,
                                                         mock_job: JobRecord) -> None:
    fact = fictional_candidate.verified_facts()[0]
    candidate = fictional_candidate.model_copy(update={"facts": [fact]})
    f = form(label="Choose the verified specialty", semantic=SemanticType.CUSTOM_SELECT,
        control_type=ControlType.SELECT,
        options=[FieldOption(value="known", label=str(fact.value), disabled=True),
                 FieldOption(value="other", label="Something else")])
    packet = asyncio.run(DynamicPacketResolver(decisions(Provider(["f0"]))).resolve(context(f, candidate, mock_job)))
    assert not packet.answers and not packet.is_complete


class Writer:
    def __init__(self, fact_id: str, text: str) -> None:
        self.fact_id, self.text = fact_id, text
        self.calls: list[dict[str, Any]] = []

    def write(self, **kwargs: Any) -> NarrativeDraft:
        self.calls.append(kwargs)
        return NarrativeDraft.model_validate({"status": "READY", "missing_information": [], "sentences": [{"text": self.text, "fact_ids": [self.fact_id]}]})


def test_narrative_relevance_and_claim_validation(fictional_candidate: CandidateProfile,
                                                mock_job: JobRecord) -> None:
    fact = fictional_candidate.verified_facts()[0]
    candidate = fictional_candidate.model_copy(update={"facts": [fact]})
    writer = Writer(fact.id, "I have relevant campaign experience.")
    d = decisions(Provider(["narrative"]))
    ctx = context(form(), candidate, mock_job)
    packet = asyncio.run(DynamicPacketResolver(d, writer).resolve(ctx))
    assert packet.answers[0].provenance.source is AnswerSource.GENERATED_FROM_FACTS
    assert ctx.problems(packet) == []
    assert writer.calls[0]["facts"] == [{"id": fact.id, "key": fact.key, "value": fact.value}]
    assert "identity" not in writer.calls[0]
    assert [r.purpose for r in d.budget.receipts] == ["full_form_routes", "narrative_relevance", "narrative_grounding"]


def test_narrative_unknown_citation_is_held(fictional_candidate: CandidateProfile,
                                         mock_job: JobRecord) -> None:
    fact = fictional_candidate.verified_facts()[0]
    candidate = fictional_candidate.model_copy(update={"facts": [fact]})
    packet = asyncio.run(DynamicPacketResolver(decisions(Provider(["narrative"])),
        Writer("invented-fact", "Unsupported")).resolve(context(form(), candidate, mock_job)))
    assert not packet.answers and not packet.is_complete
    assert "unknown or irrelevant" in packet.missing_inputs[0].prompt


def test_budget_stops_calls_without_retry_or_raw_prompt_receipt() -> None:
    p = Provider(["FIRST_NAME"])
    d = decisions(p, CallBudget(max_calls=1))
    router = AIFormRouter(d)
    f = form(label="Given name", semantic=SemanticType.UNKNOWN)
    router.annotate(f, document_id="one")
    assert router.annotate(f, document_id="two").fields[0].semantic_type is SemanticType.UNKNOWN
    assert len(p.requests) == 1
    metadata = json.dumps(d.budget.metadata())
    assert "Given name" not in metadata and "synthetic-key" not in metadata
    assert d.budget.calls == 1


def test_writer_model_is_explicit_and_error_body_not_exposed() -> None:
    key = ApiKey("synthetic-key", source="test")
    with pytest.raises(ValueError):
        NarrativeWriter(key, "anthropic/claude-opus-latest", CallBudget())
    writer = NarrativeWriter(key, "anthropic/claude-opus-5.5", CallBudget(),
        transport=lambda *args: HttpResponse(401, {}, b'synthetic-key private profile'))
    with pytest.raises(AIHold, match="HTTP_401") as caught:
        writer.write(question="Explain", facts=[], job={}, max_length=100)
    assert "private" not in str(caught.value) and "synthetic-key" not in str(caught.value)


def test_narrative_unsupported_claim_is_held_despite_valid_citation(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    class RejectGrounding(Provider):
        def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
            if "sentences" in json.loads(body)["state"]:
                self.support = 0.10
            return super().__call__(url, headers, body, timeout)

    fact = fictional_candidate.verified_facts()[0]
    candidate = fictional_candidate.model_copy(update={"facts": [fact]})
    writer = Writer(fact.id, "I increased revenue by 900 percent.")
    d = decisions(RejectGrounding(["narrative"]))
    packet = asyncio.run(DynamicPacketResolver(d, writer).resolve(context(form(), candidate, mock_job)))
    assert writer.calls  # The prose was actually generated, then rejected.
    assert not packet.answers and not packet.is_complete
    assert "not fully supported" in packet.missing_inputs[0].prompt


def test_unverified_facts_never_reach_jev(fictional_candidate: CandidateProfile,
                                       mock_job: JobRecord) -> None:
    p = Provider(["hold"])
    packet = asyncio.run(DynamicPacketResolver(decisions(p)).resolve(
        context(form(), fictional_candidate, mock_job)))
    assert not packet.answers
    assert all(f["key"] != "team_size_managed" for f in p.requests[-1]["state"]["facts"].values())


def test_conflicting_facts_hold_even_when_model_picks_one(fictional_candidate: CandidateProfile,
                                                       mock_job: JobRecord) -> None:
    fact = fictional_candidate.verified_facts()[0]
    conflict = fact.model_copy(update={"id": "conflict", "value": "A different value"})
    candidate = fictional_candidate.model_copy(update={"facts": [fact, conflict]})
    packet = asyncio.run(DynamicPacketResolver(decisions(Provider(["f0"]))).resolve(
        context(form(label="Name the verified specialty"), candidate, mock_job)))
    assert not packet.answers
    assert "disagree" in packet.missing_inputs[0].prompt


def test_failing_provider_holds_without_retry() -> None:
    p = Provider(["FIRST_NAME"], status=402)
    d = decisions(p)
    result = AIFormRouter(d).annotate(form(semantic=SemanticType.UNKNOWN), document_id="d")
    assert result.fields[0].semantic_type is SemanticType.UNKNOWN
    assert len(p.requests) == 1
    assert d.budget.receipts[0].status == "PAYMENT_REQUIRED"


def test_unoffered_model_choice_cannot_become_a_field_type() -> None:
    p = Provider(["EXECUTE_JAVASCRIPT"])
    d = decisions(p)
    result = AIFormRouter(d).annotate(form(semantic=SemanticType.UNKNOWN), document_id="d")
    assert result.fields[0].semantic_type is SemanticType.UNKNOWN
    assert d.budget.receipts[0].status == "MALFORMED_RESPONSE"


def test_writer_reservation_prevents_call_exceeding_budget() -> None:
    called = []
    writer = NarrativeWriter(ApiKey("synthetic-key", source="test"),
        "anthropic/claude-opus-5.5", CallBudget(max_usd=0.001),
        transport=lambda *args: called.append(args))
    with pytest.raises(AIHold, match="budget exhausted"):
        writer.write(question="Explain", facts=[], job={}, max_length=100)
    assert not called


def test_writer_cannot_choose_between_conflicting_verified_facts(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    fact = fictional_candidate.verified_facts()[0]
    conflict = fact.model_copy(update={"id": "conflicting-title", "value": "Junior Intern"})
    candidate = fictional_candidate.model_copy(update={"facts": [fact, conflict]})
    writer = Writer(fact.id, "I currently work as a Paid Media Lead.")
    packet = asyncio.run(DynamicPacketResolver(decisions(Provider(["narrative"])), writer).resolve(
        context(form(), candidate, mock_job)))
    assert not writer.calls
    assert not packet.answers
    assert "conflict" in packet.missing_inputs[0].prompt


def test_model_routed_packet_preserves_nonperfect_confidence(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    r = AIFormRouter(decisions(Provider(["FIRST_NAME"], probability=0.96)))
    f = r.annotate(form(label="Given name", semantic=SemanticType.UNKNOWN), document_id="confidence")
    packet = asyncio.run(DynamicPacketResolver(r.decisions, router=r).resolve(context(f, fictional_candidate, mock_job)))
    assert packet.answers[0].confidence == 0.96


def test_missing_provider_cost_is_not_reported_as_measured_zero() -> None:
    r = AIFormRouter(decisions(Provider(["FIRST_NAME"], status=402)))
    report = r.classify_form(form(semantic=SemanticType.UNKNOWN), document_id="unknown-cost")
    assert report.cost_usd is None
    assert report.known_cost_usd == 0
    assert report.unknown_cost_calls == 1


def test_writer_missing_mandatory_details_is_typed_hold() -> None:
    def transport(url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        payload = json.loads(body)
        schema = payload["response_format"]["json_schema"]["schema"]
        assert set(schema["required"]) == {"status", "sentences", "missing_information"}
        content = {"status": "NEEDS_INPUT", "sentences": [],
                   "missing_information": ["The applicant's personal reason for this career change"]}
        return HttpResponse(200, {}, json.dumps({"model": "anthropic/claude-opus-5.5",
            "usage": {"cost": .001}, "choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(content)}}]}).encode())

    budget = CallBudget()
    writer = NarrativeWriter(ApiKey("synthetic-key", source="test"),
        "anthropic/claude-opus-5.5", budget, transport=transport)
    with pytest.raises(AIHold, match="explicit facts"):
        writer.write(question="Explain your career change", facts=[{
            "id": "f1", "key": "years_experience", "value": "7"}], job={}, max_length=500)
    assert budget.receipts[0].status == "NEEDS_INPUT"
    assert budget.receipts[0].cost_usd == .001
    for invalid in (
        {"status": "READY", "sentences": [], "missing_information": []},
        {"status": "NEEDS_INPUT", "sentences": [], "missing_information": []},
        {"status": "NEEDS_INPUT", "sentences": [{"text": "Invented", "fact_ids": ["f1"]}],
         "missing_information": ["Reason"]},
    ):
        with pytest.raises(ValueError):
            NarrativeDraft.model_validate(invalid)


def test_writer_needs_input_cannot_produce_packet(fictional_candidate: CandidateProfile,
                                                mock_job: JobRecord) -> None:
    class MissingWriter:
        def write(self, **kwargs: Any) -> NarrativeDraft:
            return NarrativeDraft(status="NEEDS_INPUT", sentences=[],
                                  missing_information=["Explicit client example"])
    packet = asyncio.run(DynamicPacketResolver(decisions(Provider(["narrative"])),
        MissingWriter()).resolve(context(form(), fictional_candidate, mock_job)))
    assert not packet.answers and not packet.is_complete
    assert "explicit facts" in packet.missing_inputs[0].prompt
