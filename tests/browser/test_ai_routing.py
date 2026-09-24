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
from interviewmaxxing_browser.semantics import classify as classify_semantics
from interviewmaxxing_core import (
    AnswerScope,
    AnswerSource,
    Application,
    ApplicationField,
    ApplicationForm,
    ApplicationState,
    CandidateProfile,
    ChoiceValue,
    ControlType,
    FieldOption,
    JobRecord,
    MissingReason,
    PacketContext,
    SavedAnswer,
    SemanticType,
    TextValue,
    UserInput,
)
from interviewmaxxing_core.interfaces import SuggestionChooser
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


# --- stored answers onto a site's own option wording, referral policy, lookups (WP2) -----------

class ChoiceProvider:
    """Classifies every field as an applicant's literal COPY_KNOWN datum and answers
    the option-choice questions from ``picks``: question name -> (choice, probability)
    or an explicit probability map. Unscripted fact routing holds."""

    def __init__(self, picks: dict[str, tuple[str, float] | dict[str, float]] | None = None, *,
                 confidence: float = 0.97, status: int = 200, semantic: str = "CUSTOM_SELECT",
                 scope: str = "APPLICANT_CURRENT") -> None:
        self.picks = picks or {}
        self.confidence, self.status, self.semantic = confidence, status, semantic
        self.scope = scope
        self.requests: list[dict[str, Any]] = []

    def asked(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if name in r["questions"]]

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        self.requests.append(request)
        if self.status != 200:
            return HttpResponse(self.status, {}, b"{}")
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 1.0}
                continue
            criteria = list(question["criteria"])
            confidence = self.confidence
            if name[0] in "rnusd" and name[1:].isdigit():
                choice = {"r": "COPY_KNOWN", "n": "literal", "u": self.scope,
                          "s": self.semantic, "d": "APPLICATION_ATTACHMENT"}[name[0]]
                probabilities = {key: float(key == choice) for key in criteria}
                confidence = 1.0
            elif name in self.picks:
                pick = self.picks[name]
                if isinstance(pick, dict):
                    probabilities = {key: pick.get(key, 0.0) for key in criteria}
                else:
                    rest = (1 - pick[1]) / (len(criteria) - 1)
                    probabilities = {key: pick[1] if key == pick[0] else rest for key in criteria}
            else:
                held = "hold" if "hold" in criteria else "NONE"
                probabilities = {key: float(key == held) for key in criteria}
            choice = max(probabilities, key=lambda key: probabilities[key])
            answers[name] = {"type": "choice", "choice": choice, "confidence": confidence,
                             "probabilities": probabilities}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())


def choice_field(label: str, semantic: SemanticType, *options: str | FieldOption,
                 control: ControlType = ControlType.RADIO, required: bool = True,
                 field_id: str = "answer") -> ApplicationField:
    return ApplicationField(id=field_id, selector=f"#{field_id}", label=label,
        semantic_type=semantic, control_type=control, required=required,
        options=[o if isinstance(o, FieldOption) else FieldOption(value=f"v{i}", label=o)
                 for i, o in enumerate(options)])


def resolve_choice(provider: ChoiceProvider, candidate: CandidateProfile, job: JobRecord,
                   *fields: ApplicationField) -> tuple[Any, PacketContext, DynamicPacketResolver]:
    router = AIFormRouter(decisions(provider))
    f = router.annotate(ApplicationForm(url="https://example.test/apply", fields=list(fields)),
                        document_id="option-choice")
    ctx = context(f, candidate, job)
    resolver = DynamicPacketResolver(router.decisions, router=router)
    packet = asyncio.run(resolver.resolve(ctx))
    assert ctx.problems(packet) == []
    return packet, ctx, resolver


WORK_AUTH = "Are you legally authorized to work in the United States?"
SPONSORSHIP = "Will you now or in the future require visa sponsorship?"


@pytest.mark.parametrize("yes_label", ["Yes, I am authorized to work in the US",
                                       "Yes - I am legally authorized to work in the United States"])
def test_a_stored_yes_maps_to_the_sites_equivalent_option(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, yes_label: str,
) -> None:
    provider = ChoiceProvider({"equivalent_0": ("o0", 0.98)})
    field = choice_field(WORK_AUTH, SemanticType.WORK_AUTHORIZATION, yes_label,
                         "No, I am not authorized to work in the US")
    packet, _, resolver = resolve_choice(provider, fictional_candidate, mock_job, field)
    assert packet.is_complete
    [answer] = packet.answers
    assert (answer.value.value, answer.value.label) == ("v0", yes_label)
    assert answer.provenance.source is AnswerSource.SAVED_ANSWER
    assert answer.provenance.reference_ids == ["sa.work_auth_us"]
    assert "Jev mapped it onto the site's option wording" in (answer.provenance.note or "")
    assert answer.confidence == pytest.approx(0.97)
    [request] = provider.asked("equivalent_0")
    assert request["state"]["stored_answers"] == {"equivalent_0": "Yes"}
    assert request["state"]["options"] == {"o0": yes_label,
                                           "o1": "No, I am not authorized to work in the US"}
    question = request["questions"]["equivalent_0"]
    assert set(question["criteria"]) == {"o0", "o1", "NONE"}
    assert "polarity" in question["instructions"] and "broader or narrower" in question["instructions"]
    [trace] = [t for t in resolver.narrative_traces if t["stage"] == "option_equivalence"]
    assert trace["status"] == "MAPPED" and trace["reference_ids"] == ["sa.work_auth_us"]
    assert yes_label not in json.dumps(trace) and '"Yes"' not in json.dumps(trace)


@pytest.mark.parametrize("pick", [("NONE", 0.99), ("o0", 0.90), ("o0", 0.99)])
def test_no_equivalent_option_or_a_weak_mapping_keeps_the_explicit_hold(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, pick: tuple[str, float],
) -> None:
    # The stored answer is "No"; the site only offers two "yes" variants.
    provider = ChoiceProvider({"equivalent_0": pick}, confidence=0.97 if pick[1] != 0.99 or pick[0] == "NONE" else 0.80)
    field = choice_field(SPONSORSHIP, SemanticType.SPONSORSHIP,
                         "Yes, I will require sponsorship", "Yes, but only in the future")
    packet, _, resolver = resolve_choice(provider, fictional_candidate, mock_job, field)
    assert packet.answers == [] and not packet.is_complete
    [missing] = packet.missing_inputs
    assert missing.reason is MissingReason.EXPLICIT_ANSWER_REQUIRED
    assert "Your saved answer 'No' cannot be used here" in missing.prompt
    [trace] = [t for t in resolver.narrative_traces if t["stage"] == "option_equivalence"]
    assert trace["status"] == ("NONE" if pick[0] == "NONE" else "BELOW_GATE")


def test_an_exact_option_needs_no_mapping_call(fictional_candidate: CandidateProfile,
                                               mock_job: JobRecord) -> None:
    provider = ChoiceProvider()
    field = choice_field(WORK_AUTH, SemanticType.WORK_AUTHORIZATION, "Yes", "No")
    packet, _, _ = resolve_choice(provider, fictional_candidate, mock_job, field)
    assert packet.answers[0].value.label == "Yes"
    assert len(provider.requests) == 1 and not provider.asked("equivalent_0")


def test_identity_value_maps_onto_a_country_option_through_the_copy_gate(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"equivalent_0": ("o1", 0.99)})
    field = choice_field("Country", SemanticType.COUNTRY, "Canada",
                         "United States of America (USA)", control=ControlType.SELECT)
    packet, _, _ = resolve_choice(provider, fictional_candidate, mock_job, field)
    [answer] = packet.answers
    assert answer.value.label == "United States of America (USA)"
    assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY
    assert provider.asked("equivalent_0")[0]["state"]["stored_answers"] == {
        "equivalent_0": "United States"}


def test_identity_mapping_is_not_attempted_for_another_subject(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"equivalent_0": ("o1", 0.99)}, scope="OTHER_PERSON_OR_ENTITY")
    field = choice_field("Country", SemanticType.COUNTRY, "Canada",
                         "United States of America (USA)", control=ControlType.SELECT)
    packet, _, _ = resolve_choice(provider, fictional_candidate, mock_job, field)
    assert packet.answers == [] and not provider.asked("equivalent_0")


def test_multi_choice_maps_only_the_items_without_an_exact_option(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    channels = SavedAnswer(id="sa.channels", scope=AnswerScope.GLOBAL, semantic_type=None,
                           question="Which channels have you managed?",
                           value=["Paid search", "Social ads"],
                           confirmed_at="2026-09-01T12:00:00Z")
    candidate = fictional_candidate.model_copy(update={"saved_answers": [channels]})
    provider = ChoiceProvider({"equivalent_1": ("o2", 0.99)})
    field = choice_field("Which channels have you managed?", SemanticType.CUSTOM_MULTISELECT,
                         "Paid search", "Email", "Paid social", control=ControlType.CHECKBOX_GROUP)
    packet, _, _ = resolve_choice(provider, candidate, mock_job, field)
    [answer] = packet.answers
    assert [c.label for c in answer.value.choices] == ["Paid search", "Paid social"]
    [request] = provider.asked("equivalent_1")
    assert list(request["questions"]) == ["equivalent_1"]
    assert request["state"]["stored_answers"] == {"equivalent_1": "Social ads"}


REFERRAL_DEFAULT = SavedAnswer(id="sa.referral", scope=AnswerScope.GLOBAL,
                               semantic_type=SemanticType.REFERRAL_SOURCE,
                               question="Where did you hear about us?",
                               match_phrases=["How did you hear about us?"],
                               value="Company career page", confirmed_at="2026-09-01T12:00:00Z")


def with_referral(candidate: CandidateProfile) -> CandidateProfile:
    return candidate.model_copy(update={"saved_answers": [*candidate.saved_answers, REFERRAL_DEFAULT]})


@pytest.mark.parametrize("options,pick,rule,label", [
    (["LinkedIn", "Company careers website", "Other"], "careers_o1", 1, "Company careers website"),
    (["LinkedIn", "Other", "Indeed"], "other_o1", 2, "Other"),
    (["Indeed", "LinkedIn", "Employee referral"], "board_o1", 3, "LinkedIn"),
    (["Employee referral", "Conference"], "NONE", 4, "Employee referral"),
])
def test_referral_follows_the_owner_order_and_records_the_rule(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    options: list[str], pick: str, rule: int, label: str,
) -> None:
    provider = ChoiceProvider({"referral": (pick, 0.98)})
    field = choice_field("How did you hear about us?", SemanticType.REFERRAL_SOURCE, *options,
                         control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(provider, with_referral(fictional_candidate), mock_job, field)
    assert packet.is_complete
    [answer] = packet.answers
    assert answer.value.label == label
    assert answer.provenance.source is AnswerSource.SAVED_ANSWER
    assert answer.provenance.reference_ids == ["sa.referral"]
    assert f"referral policy rule {rule}" in (answer.provenance.note or "")
    [request] = provider.asked("referral")
    question = request["questions"]["referral"]
    assert {"NONE", "NOT_SOURCE", *(f"{c}_o{i}" for c in ("careers", "other", "board")
                                    for i in range(len(options)))} == set(question["criteria"])
    assert "(1)" in question["instructions"] and "(3)" in question["instructions"]
    [trace] = [t for t in resolver.narrative_traces if t["stage"] == "referral_policy"]
    assert (trace["rule"], trace["status"]) == (rule, "MAPPED")
    assert label not in json.dumps(trace)


def test_referral_rule_four_is_the_first_enabled_real_option_even_when_jev_is_unsure(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    # Split between two careers-like options and nearly nothing on NOT_SOURCE: never held.
    provider = ChoiceProvider({"referral": {"careers_o1": 0.55, "careers_o2": 0.43,
                                            "NOT_SOURCE": 0.02}})
    field = choice_field("Source", SemanticType.REFERRAL_SOURCE,
                         FieldOption(value="", label="Select..."),
                         FieldOption(value="rec", label="Recruiter", disabled=True),
                         FieldOption(value="event", label="Conference"),
                         FieldOption(value="site", label="Careers page"),
                         FieldOption(value="web", label="Company website"),
                         control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(provider, with_referral(fictional_candidate), mock_job, field)
    [answer] = packet.answers
    assert (answer.value.value, answer.value.label) == ("event", "Conference")
    assert "referral policy rule 4" in (answer.provenance.note or "")
    assert answer.confidence == pytest.approx(0.98)
    [trace] = [t for t in resolver.narrative_traces if t["stage"] == "referral_policy"]
    assert trace["rule"] == 4 and trace["not_source_probability"] == pytest.approx(0.02)


@pytest.mark.parametrize("pick", [("NOT_SOURCE", 0.99), {"NOT_SOURCE": 0.6, "NONE": 0.4}])
def test_a_referrer_name_question_typed_as_referral_source_is_never_auto_answered(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    pick: tuple[str, float] | dict[str, float],
) -> None:
    provider = ChoiceProvider({"referral": pick})
    field = choice_field("Who referred you?", SemanticType.REFERRAL_SOURCE,
                         "Fictional Employee A", "Fictional Employee B", control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(provider, with_referral(fictional_candidate), mock_job, field)
    assert packet.answers == [] and not packet.is_complete
    [trace] = [t for t in resolver.narrative_traces if t["stage"] == "referral_policy"]
    assert trace["rule"] is None
    assert trace["status"] == ("NOT_REFERRAL_SOURCE" if isinstance(pick, tuple) else "HELD")


def test_referral_without_a_stored_referral_answer_or_provider_holds(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    field = choice_field("How did you hear about us?", SemanticType.REFERRAL_SOURCE,
                         "LinkedIn", "Other", control=ControlType.SELECT)
    provider = ChoiceProvider({"referral": ("other_o1", 0.99)})
    packet, _, _ = resolve_choice(provider, fictional_candidate, mock_job, field)
    assert packet.answers == [] and not provider.asked("referral")
    failing = ChoiceProvider({"referral": ("other_o1", 0.99)})
    router = AIFormRouter(decisions(ChoiceProvider()))
    annotated = router.annotate(ApplicationForm(url="https://example.test/apply", fields=[field]),
                                document_id="option-choice")
    resolver = DynamicPacketResolver(decisions(failing), router=router)
    failing.status = 503
    packet = asyncio.run(resolver.resolve(context(annotated, with_referral(fictional_candidate),
                                                  mock_job)))
    assert packet.answers == [] and len(failing.requests) == 1


LOOKUP_TEXAS = "Austin, Texas, United States"
LOOKUP_MINNESOTA = "Austin, Minnesota, United States"


def lookup_context(candidate: CandidateProfile, job: JobRecord) -> PacketContext:
    f = ApplicationForm(url="https://example.test/apply", fields=[ApplicationField(
        id="location", selector="#location", label="Location (City)",
        semantic_type=SemanticType.LOCATION, control_type=ControlType.TYPEAHEAD, required=True)])
    return context(f, candidate, job)


@pytest.mark.parametrize("pick,expected", [
    (("s1", 0.99), LOOKUP_TEXAS),
    (("NONE", 0.99), None),
    ({"s0": 0.5, "s1": 0.5}, None),
    (("s1", 0.93), None),
])
def test_lookup_choice_picks_the_candidates_own_city_or_none(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    pick: tuple[str, float] | dict[str, float], expected: str | None,
) -> None:
    provider = ChoiceProvider({"lookup": pick})
    resolver = DynamicPacketResolver(decisions(provider))
    assert isinstance(resolver, SuggestionChooser)  # what the runner looks for
    ctx = lookup_context(fictional_candidate, mock_job)
    chosen = asyncio.run(resolver.choose_suggestion(ctx, ctx.form.fields[0], "Austin, TX",
                                                    [LOOKUP_MINNESOTA, LOOKUP_TEXAS]))
    assert chosen == expected
    [request] = provider.requests
    assert request["state"]["typed_value"] == "Austin, TX"
    assert request["state"]["suggestions"] == {"s0": LOOKUP_MINNESOTA, "s1": LOOKUP_TEXAS}
    question = request["questions"]["lookup"]
    assert set(question["criteria"]) == {"s0", "s1", "NONE"}
    assert "same name in another state" in question["instructions"]
    decision = resolver.suggestion_decision("location")
    assert decision is not None and decision["suggestion_count"] == 2
    assert decision["status"] == ("CHOSEN" if expected else
                                  "NONE" if pick == ("NONE", 0.99) else "BELOW_GATE")
    assert "Austin" not in json.dumps(decision)


def test_lookup_choice_holds_without_a_provider_or_suggestions(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"lookup": ("s0", 0.99)}, status=402)
    resolver = DynamicPacketResolver(decisions(provider))
    ctx = lookup_context(fictional_candidate, mock_job)
    field = ctx.form.fields[0]
    assert asyncio.run(resolver.choose_suggestion(ctx, field, "Austin, TX", [LOOKUP_TEXAS])) is None
    assert resolver.suggestion_decision("location")["status"] == "HELD"  # type: ignore[index]
    assert asyncio.run(resolver.choose_suggestion(ctx, field, "Austin, TX", [])) is None
    assert len(provider.requests) == 1


def test_referral_on_a_select_all_question_picks_one_option(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"referral": ("board_o0", 0.99)})
    field = choice_field("How did you hear about us?", SemanticType.REFERRAL_SOURCE,
                         "LinkedIn", "Podcast", control=ControlType.CHECKBOX_GROUP, required=False)
    packet, _, _ = resolve_choice(provider, with_referral(fictional_candidate), mock_job, field)
    [answer] = packet.answers
    assert [(c.value, c.label) for c in answer.value.choices] == [("v0", "LinkedIn")]
    assert "referral policy rule 3" in (answer.provenance.note or "")


def test_protected_answers_map_only_from_the_users_saved_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    field = choice_field("Gender", SemanticType.EEO_GENDER, "Man", "Woman", "Decline to self-identify")
    provider = ChoiceProvider({"equivalent_0": ("o0", 0.99)})
    packet, _, _ = resolve_choice(provider, fictional_candidate, mock_job, field)
    assert packet.answers == [] and not provider.asked("equivalent_0")  # nothing stored: no call
    gender = SavedAnswer(id="sa.gender", scope=AnswerScope.GLOBAL, semantic_type=SemanticType.EEO_GENDER,
                         question="Gender", value="Male", confirmed_at="2026-09-01T12:00:00Z")
    stored = fictional_candidate.model_copy(update={
        "saved_answers": [*fictional_candidate.saved_answers, gender]})
    packet, _, _ = resolve_choice(provider, stored, mock_job, field)
    [answer] = packet.answers
    assert answer.value.label == "Man"
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, ["sa.gender"])


# --- round 2, deliverable A: reworded questions answered from GLOBAL saved answers (WP2) ------

REWORDED_SPONSORSHIP = "Will you require sponsorship in the future?"
SPONSORSHIP_OPTIONS = ("Yes, I will require sponsorship", "No, I will not require sponsorship")


def stage_traces(resolver: DynamicPacketResolver, stage: str) -> list[dict[str, Any]]:
    return [trace for trace in resolver.narrative_traces if trace["stage"] == stage]


def global_answer(answer_id: str, question: str, value: Any, *,
                  semantic: SemanticType | None = None,
                  confirmed_at: str = "2026-09-02T12:00:00Z") -> SavedAnswer:
    return SavedAnswer(id=answer_id, scope=AnswerScope.GLOBAL, semantic_type=semantic,
                       question=question, value=value, confirmed_at=confirmed_at)


def with_saved(candidate: CandidateProfile, *answers: SavedAnswer) -> CandidateProfile:
    return candidate.model_copy(update={"saved_answers": [*candidate.saved_answers, *answers]})


def sponsorship_field(label: str = REWORDED_SPONSORSHIP, *, control: ControlType = ControlType.RADIO,
                      required: bool = True) -> ApplicationField:
    if control in (ControlType.RADIO, ControlType.SELECT):
        return choice_field(label, SemanticType.SPONSORSHIP, *SPONSORSHIP_OPTIONS,
                            control=control, required=required)
    return ApplicationField(id="answer", selector="#answer", label=label,
                            semantic_type=SemanticType.SPONSORSHIP, control_type=control,
                            required=required)


@pytest.mark.parametrize("question", [
    REWORDED_SPONSORSHIP,
    "Will you now or in the future require employer sponsorship to obtain or maintain "
    "authorization to work in the United States?",
])
def test_a_reworded_sponsorship_question_uses_the_global_saved_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, question: str,
) -> None:
    provider = ChoiceProvider({"wording": ("q0", 0.98), "equivalent_0": ("o1", 0.98)})
    packet, _, resolver = resolve_choice(provider, fictional_candidate, mock_job,
                                         sponsorship_field(question))
    assert packet.is_complete
    [answer] = packet.answers
    assert (answer.value.value, answer.value.label) == ("v1", "No, I will not require sponsorship")
    assert answer.provenance.source is AnswerSource.SAVED_ANSWER
    assert answer.provenance.reference_ids == ["sa.sponsorship"]
    note = answer.provenance.note or ""
    assert note.startswith("question wording mapped by Jev from the saved answer for "
                           f"{SPONSORSHIP!r}")
    assert note.endswith("; Jev mapped it onto the site's option wording")
    assert answer.confidence == pytest.approx(0.97)
    [request] = provider.asked("wording")
    saved = request["state"]["saved_questions"]
    # Only the GLOBAL sponsorship answer is offered: no consent, work-authorization or
    # job-scoped answer, and never a saved value.
    assert saved == {"q0": {"question": SPONSORSHIP, "variants": []}}
    assert '"No"' not in json.dumps(saved) and "value" not in json.dumps(saved)
    observed = request["state"]["observed_question"]
    assert observed["label"] == question and observed["control"] == "RADIO"
    assert observed["options"] == list(SPONSORSHIP_OPTIONS)
    criteria = request["questions"]["wording"]["criteria"]
    assert set(criteria) == {"q0", "NONE"}
    assert "same timeframe" in criteria["q0"] and "which authorization you hold" in criteria["NONE"]
    [mapping] = provider.asked("equivalent_0")
    assert mapping["state"]["stored_answers"] == {"equivalent_0": "No"}
    [trace] = stage_traces(resolver, "question_equivalence")
    assert trace["status"] == "MAPPED" and trace["choice"] == "q0"
    assert trace["candidate_ids"] == [["sa.sponsorship"]]
    assert trace["reference_ids"] == ["sa.sponsorship"]
    assert trace["value_probability"] == pytest.approx(0.98)
    assert "No" not in trace.values() and '"No"' not in json.dumps(trace)
    assert SPONSORSHIP not in json.dumps(trace)


def test_a_question_about_a_different_status_is_not_answered_by_the_saved_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"wording": ("NONE", 0.99), "equivalent_0": ("o0", 0.99)})
    field = choice_field("Which work authorization do you hold?", SemanticType.WORK_AUTHORIZATION,
                         "US citizen", "Permanent resident", "H-1B", control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(provider, fictional_candidate, mock_job, field)
    assert packet.answers == [] and not packet.is_complete
    [missing] = packet.missing_inputs
    assert missing.reason is MissingReason.EXPLICIT_ANSWER_REQUIRED
    [request] = provider.asked("wording")
    assert request["state"]["saved_questions"] == {
        "q0": {"question": WORK_AUTH, "variants": ["authorized to work in the us"]}}
    assert not provider.asked("equivalent_0")
    [trace] = stage_traces(resolver, "question_equivalence")
    assert (trace["status"], trace["choice"]) == ("NONE", "NONE")


def test_a_job_scoped_answer_is_never_mapped_to_another_wording(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    job_answer = SavedAnswer(id="sa.job_sponsorship", scope=AnswerScope.JOB,
                             job_identity_key=mock_job.identity_key, employer="Mock Co",
                             semantic_type=SemanticType.SPONSORSHIP, question=SPONSORSHIP,
                             value="No", confirmed_at="2026-09-10T12:00:00Z")
    assert job_answer.applies_to(mock_job)
    candidate = fictional_candidate.model_copy(update={"saved_answers": [job_answer]})
    provider = ChoiceProvider({"wording": ("q0", 0.99), "equivalent_0": ("o1", 0.99)})
    packet, _, resolver = resolve_choice(provider, candidate, mock_job, sponsorship_field())
    assert packet.answers == [] and not provider.asked("wording")
    assert [m.reason for m in packet.missing_inputs] == [MissingReason.EXPLICIT_ANSWER_REQUIRED]
    assert not stage_traces(resolver, "question_equivalence")


@pytest.mark.parametrize("probability,confidence", [(0.90, 0.97), (0.98, 0.80)])
def test_a_low_scoring_wording_decision_keeps_the_hold(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    probability: float, confidence: float,
) -> None:
    provider = ChoiceProvider({"wording": ("q0", probability), "equivalent_0": ("o1", 0.99)},
                              confidence=confidence)
    packet, _, resolver = resolve_choice(provider, fictional_candidate, mock_job,
                                         sponsorship_field())
    assert packet.answers == [] and not packet.is_complete
    assert [m.reason for m in packet.missing_inputs] == [MissingReason.EXPLICIT_ANSWER_REQUIRED]
    assert not provider.asked("equivalent_0")
    [trace] = stage_traces(resolver, "question_equivalence")
    assert trace["status"] == "BELOW_GATE"
    assert trace["value_probability"] == pytest.approx(probability)
    assert trace["confidence"] == pytest.approx(confidence)


@pytest.mark.parametrize("mapping,label", [(("o1", 0.98), "No, I will not require sponsorship"),
                                           (("NONE", 0.99), None)])
def test_an_exact_wording_answer_takes_the_first_path_without_a_wording_request(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    mapping: tuple[str, float], label: str | None,
) -> None:
    provider = ChoiceProvider({"wording": ("q0", 0.99), "equivalent_0": mapping})
    packet, _, resolver = resolve_choice(provider, fictional_candidate, mock_job,
                                         sponsorship_field(SPONSORSHIP))
    assert not provider.asked("wording") and not stage_traces(resolver, "question_equivalence")
    assert provider.asked("equivalent_0")
    if label is None:
        assert packet.answers == [] and not packet.is_complete
    else:
        [answer] = packet.answers
        assert answer.value.label == label
        assert "question wording mapped by Jev" not in (answer.provenance.note or "")


@pytest.mark.parametrize("usable", [True, False])
def test_a_users_own_answer_is_never_reworded(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, usable: bool,
) -> None:
    provider = ChoiceProvider({"wording": ("q0", 0.99), "equivalent_0": ("o1", 0.99)})
    router = AIFormRouter(decisions(provider))
    f = router.annotate(ApplicationForm(url="https://example.test/apply",
                                        fields=[sponsorship_field()]), document_id="option-choice")
    field = f.fields[0]
    if usable:
        user_input = UserInput.for_field(f, field.id, ChoiceValue(value="v0",
                                                                  label=SPONSORSHIP_OPTIONS[0]))
    else:  # an earlier answer that no longer fits the control
        user_input = UserInput(form_url=f.url, form_step=f.step, field_id=field.id,
                               field_fingerprint=field.fingerprint, question=field.question_text,
                               value=TextValue(text="Not sure"))
    base = context(f, fictional_candidate, mock_job)
    ctx = PacketContext(application=base.application, job=base.job, form=base.form,
                        candidate=base.candidate, user_inputs=[user_input])
    resolver = DynamicPacketResolver(router.decisions, router=router)
    packet = asyncio.run(resolver.resolve(ctx))
    assert ctx.problems(packet) == []
    assert not provider.asked("wording") and not stage_traces(resolver, "question_equivalence")
    if usable:
        [answer] = packet.answers
        assert answer.provenance.source is AnswerSource.USER_INPUT
        assert answer.value.label == SPONSORSHIP_OPTIONS[0]
    else:
        assert packet.answers == []
        assert "Your earlier answer cannot be used" in packet.missing_inputs[0].prompt


def test_an_untyped_global_answer_answers_a_reworded_custom_question(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    candidate = with_saved(fictional_candidate,
                           global_answer("sa.age", "Are you above the age of 18?", "Yes"))
    provider = ChoiceProvider({"wording": ("q0", 0.98), "equivalent_0": ("o1", 0.99)},
                              semantic="CUSTOM_BOOLEAN")
    field = choice_field("Are you 18 years of age or older?", SemanticType.CUSTOM_BOOLEAN,
                         "Yes", "No")
    packet, ctx, resolver = resolve_choice(provider, candidate, mock_job, field)
    assert ctx.form.fields[0].semantic_type is SemanticType.CUSTOM_BOOLEAN
    [answer] = packet.answers
    assert (answer.value.value, answer.value.label) == ("v0", "Yes")
    assert answer.provenance.source is AnswerSource.SAVED_ANSWER
    assert answer.provenance.reference_ids == ["sa.age"]
    assert not provider.asked("equivalent_0")  # the saved "Yes" names the option exactly
    [request] = provider.asked("wording")
    # Typed answers (work authorization, sponsorship, consent) never reach a custom field.
    assert request["state"]["saved_questions"] == {
        "q0": {"question": "Are you above the age of 18?", "variants": []}}
    [trace] = stage_traces(resolver, "question_equivalence")
    assert trace["status"] == "MAPPED"


@pytest.mark.parametrize("second_value,mapped", [("No", True), ("Yes", False)])
def test_saved_questions_with_the_same_answer_pool_their_probability(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, second_value: str, mapped: bool,
) -> None:
    second = global_answer(
        "sa.sponsorship_status",
        "Do you now or will you in the future require sponsorship for employment visa status?",
        second_value, semantic=SemanticType.SPONSORSHIP, confirmed_at="2026-09-05T12:00:00Z")
    candidate = with_saved(fictional_candidate, second)
    provider = ChoiceProvider({"wording": {"q0": 0.50, "q1": 0.48, "NONE": 0.02},
                               "equivalent_0": ("o1", 0.99)})
    packet, _, resolver = resolve_choice(provider, candidate, mock_job, sponsorship_field())
    [request] = provider.asked("wording")
    assert [q["question"] for q in request["state"]["saved_questions"].values()] == [
        second.question, SPONSORSHIP]  # newest first
    [trace] = stage_traces(resolver, "question_equivalence")
    assert trace["candidate_ids"] == [["sa.sponsorship_status"], ["sa.sponsorship"]]
    if mapped:
        [answer] = packet.answers
        assert answer.value.label == "No, I will not require sponsorship"
        assert answer.provenance.reference_ids == ["sa.sponsorship_status"]
        assert trace["status"] == "MAPPED"
        assert trace["value_probability"] == pytest.approx(0.98)
    else:
        assert packet.answers == [] and not provider.asked("equivalent_0")
        assert trace["status"] == "BELOW_GATE"
        assert trace["value_probability"] == pytest.approx(0.50)


def test_saved_answers_that_disagree_on_one_wording_are_not_offered(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    changed = global_answer("sa.sponsorship_changed", SPONSORSHIP, "Yes",
                            semantic=SemanticType.SPONSORSHIP)
    provider = ChoiceProvider({"wording": ("q0", 0.99), "equivalent_0": ("o1", 0.99)})
    packet, _, _ = resolve_choice(provider, with_saved(fictional_candidate, changed), mock_job,
                                  sponsorship_field())
    assert packet.answers == [] and not provider.asked("wording")


def test_a_text_question_gets_the_saved_value_as_text(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"wording": ("q0", 0.98)})
    packet, _, _ = resolve_choice(provider, fictional_candidate, mock_job,
                                  sponsorship_field(control=ControlType.TEXT))
    [answer] = packet.answers
    assert answer.value == TextValue(text="No")
    assert answer.provenance.reference_ids == ["sa.sponsorship"]
    assert not provider.asked("equivalent_0")


@pytest.mark.parametrize("control,required", [(ControlType.TEXTAREA, True),
                                              (ControlType.RADIO, False)])
def test_long_text_and_optional_questions_are_not_reworded(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    control: ControlType, required: bool,
) -> None:
    provider = ChoiceProvider({"wording": ("q0", 0.99), "equivalent_0": ("o1", 0.99)})
    packet, _, resolver = resolve_choice(provider, fictional_candidate, mock_job,
                                         sponsorship_field(control=control, required=required))
    assert packet.answers == [] and not provider.asked("wording")
    assert not stage_traces(resolver, "question_equivalence")


def test_consent_is_never_answered_from_a_differently_worded_consent(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"wording": ("q0", 0.99)})
    field = ApplicationField(id="answer", selector="#answer", label="I consent to a background check",
                             semantic_type=SemanticType.CONSENT, control_type=ControlType.CHECKBOX,
                             required=True)
    packet, _, _ = resolve_choice(provider, fictional_candidate, mock_job, field)
    assert packet.answers == [] and not provider.asked("wording")


def test_a_reworded_eeo_question_uses_the_global_eeo_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    candidate = with_saved(fictional_candidate, global_answer(
        "sa.gender", "Gender", "Male", semantic=SemanticType.EEO_GENDER))
    provider = ChoiceProvider({"wording": ("q0", 0.99), "equivalent_0": ("o0", 0.99)})
    field = choice_field("What is your gender identity?", SemanticType.EEO_GENDER,
                         "Man", "Woman", "Decline to self-identify")
    packet, _, _ = resolve_choice(provider, candidate, mock_job, field)
    [answer] = packet.answers
    assert answer.value.label == "Man"
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, ["sa.gender"])


def test_every_typed_simple_answer_is_reusable_by_meaning() -> None:
    from interviewmaxxing_browser.ai.routing import REUSABLE_TYPES, UNTYPED_REUSE_TYPES
    from interviewmaxxing_candidate.simple_answers import _REUSABLE_QUESTIONS

    typed = {semantic for semantic, _ in _REUSABLE_QUESTIONS.values() if semantic is not None}
    assert typed <= REUSABLE_TYPES
    assert {SemanticType.WORK_AUTHORIZATION, SemanticType.SPONSORSHIP,
            SemanticType.REFERRAL_SOURCE} <= REUSABLE_TYPES
    # Consent and attestations are never answered from a differently worded question. The
    # desired-salary default (round 3) is reusable by meaning like the other defaults.
    assert not REUSABLE_TYPES & {SemanticType.CONSENT, SemanticType.ATTESTATION}
    assert {SemanticType.SALARY_EXPECTATION, SemanticType.RELOCATION,
            SemanticType.START_DATE} <= REUSABLE_TYPES
    assert SemanticType.CUSTOM_BOOLEAN in UNTYPED_REUSE_TYPES
    assert SemanticType.CUSTOM_LONG_TEXT not in UNTYPED_REUSE_TYPES


# --- round 3: residence screeners from the verified address (item 1) ---------------------

YES_NO = ("Yes", "No")
FIXTURE_ADDRESS = {"city": "Springfield", "region": "OR", "country": "United States"}
REGIONS = ("US - West", "US - East", "Remote / Outside the US")


def residence_field(label: str, semantic: SemanticType, *options: str,
                    control: ControlType = ControlType.RADIO,
                    required: bool = True) -> ApplicationField:
    return choice_field(label, semantic, *(options or YES_NO), control=control,
                        required=required, field_id="residence")


def with_address(candidate: CandidateProfile, **address: str | None) -> CandidateProfile:
    identity = candidate.identity
    return candidate.model_copy(update={"identity": identity.model_copy(update={
        "address": identity.address.model_copy(update=address)})})


@pytest.mark.parametrize("question", ["Do you currently live in the United States?",
                                      "Are you located in the US?"])
def test_a_country_residence_question_is_answered_from_the_verified_address(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, question: str,
) -> None:
    provider = ChoiceProvider({"residence": ("o0", 0.99)})
    packet, _, resolver = resolve_choice(provider, fictional_candidate, mock_job,
                                         residence_field(question, SemanticType.COUNTRY))
    assert packet.is_complete
    [answer] = packet.answers
    assert (answer.value.value, answer.value.label) == ("v0", "Yes")
    assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY
    assert answer.provenance.note == "verified identity address; residence question answered by Jev"
    assert answer.confidence == pytest.approx(0.97)  # min(confidence 0.97, probability 0.99)
    assert not provider.asked("equivalent_0")  # an address never means "Yes"
    [request] = provider.asked("residence")
    assert request["state"]["applicant_address"] == FIXTURE_ADDRESS
    assert request["state"]["options"] == {"o0": "Yes", "o1": "No"}
    assert set(request["questions"]["residence"]["criteria"]) == {
        "o0", "o1", "UNKNOWN", "NOT_RESIDENCE"}
    [trace] = stage_traces(resolver, "residence_screener")
    assert (trace["status"], trace["choice"]) == ("ANSWERED", "o0")
    shown = json.dumps(trace)
    assert "Springfield" not in shown and "United States" not in shown and '"OR"' not in shown


@pytest.mark.parametrize("states,pick,expected", [
    ("AL, AZ, CA, OR", "o0", "Yes"),
    ("AL, AZ, CA, TX", "o1", "No"),
])
def test_a_state_list_question_is_answered_and_checked_against_the_address(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    states: str, pick: str, expected: str,
) -> None:
    provider = ChoiceProvider({"residence": (pick, 0.99)})
    field = residence_field(f"Do you reside in any of the following states: {states}?",
                            SemanticType.STATE)
    packet, _, resolver = resolve_choice(provider, fictional_candidate, mock_job, field)
    assert packet.is_complete
    [answer] = packet.answers
    assert answer.value.label == expected
    assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY
    [trace] = stage_traces(resolver, "residence_screener")
    assert trace["status"] == "ANSWERED"
    assert trace["state_list_member"] is (expected == "Yes")


def test_a_state_list_answer_that_contradicts_the_address_is_held(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"residence": ("o0", 0.99)})  # "Yes", but OR is not listed
    field = residence_field("Do you reside in any of the following states: AL, AZ, CA, TX?",
                            SemanticType.STATE)
    packet, _, resolver = resolve_choice(provider, fictional_candidate, mock_job, field)
    assert packet.answers == [] and not packet.is_complete
    [missing] = packet.missing_inputs
    assert (missing.field_id, missing.reason) == ("residence", MissingReason.NO_ANSWER)
    [trace] = stage_traces(resolver, "residence_screener")
    assert trace["status"] == "STATE_LIST_MISMATCH" and trace["state_list_member"] is False


def test_a_negated_state_list_is_left_to_jev_without_the_membership_check(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"residence": ("o0", 0.99)})
    field = residence_field("Do you live outside the following states: AL, AZ, CA, TX?",
                            SemanticType.STATE)
    packet, _, resolver = resolve_choice(provider, fictional_candidate, mock_job, field)
    assert packet.answers[0].value.label == "Yes"
    [trace] = stage_traces(resolver, "residence_screener")
    assert trace["status"] == "ANSWERED" and "state_list_member" not in trace


def test_a_state_select_tries_identity_equivalence_before_the_residence_decision(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"equivalent_0": ("NONE", 0.99), "residence": ("o0", 0.99)})
    field = residence_field("Which state do you reside in?", SemanticType.STATE,
                            "OR - Oregon", "TX - Texas", "WA - Washington",
                            control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(provider, fictional_candidate, mock_job, field)
    [answer] = packet.answers
    assert (answer.value.value, answer.value.label) == ("v0", "OR - Oregon")
    assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY
    order = [name for request in provider.requests for name in request["questions"]
             if name in ("equivalent_0", "residence")]
    assert order == ["equivalent_0", "residence"]
    assert [t["status"] for t in stage_traces(resolver, "option_equivalence")] == ["NONE"]
    assert [t["status"] for t in stage_traces(resolver, "residence_screener")] == ["ANSWERED"]


def test_a_region_select_takes_the_region_that_contains_the_address(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"residence": ("o0", 0.99)})
    field = residence_field("What is your current location?", SemanticType.LOCATION, *REGIONS,
                            control=ControlType.SELECT)
    packet, _, _ = resolve_choice(provider, fictional_candidate, mock_job, field)
    assert packet.is_complete
    [answer] = packet.answers
    assert (answer.value.value, answer.value.label) == ("v0", "US - West")
    assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY
    [request] = provider.asked("residence")
    assert request["state"]["options"] == {"o0": "US - West", "o1": "US - East",
                                           "o2": "Remote / Outside the US"}


@pytest.mark.parametrize("pick,status", [
    (("UNKNOWN", 0.99), "UNKNOWN"),
    (("NOT_RESIDENCE", 0.99), "NOT_RESIDENCE"),
    (("o0", 0.90), "BELOW_GATE"),
])
def test_an_unsettled_or_weak_residence_decision_holds(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    pick: tuple[str, float], status: str,
) -> None:
    provider = ChoiceProvider({"residence": pick})
    field = residence_field("What is your current location?", SemanticType.LOCATION, *REGIONS,
                            control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(provider, fictional_candidate, mock_job, field)
    assert packet.answers == [] and not packet.is_complete
    assert [m.field_id for m in packet.missing_inputs] == ["residence"]
    [trace] = stage_traces(resolver, "residence_screener")
    assert trace["status"] == status


RESIDENCE_QUESTION = "Do you currently live in the United States?"


@pytest.mark.parametrize("case", ["optional", "other_person", "custom_type", "no_address"])
def test_residence_is_not_asked_outside_its_scope(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, case: str,
) -> None:
    candidate = fictional_candidate
    provider = ChoiceProvider({"residence": ("o0", 0.99)})
    field = residence_field(RESIDENCE_QUESTION, SemanticType.COUNTRY)
    if case == "optional":
        field = residence_field(RESIDENCE_QUESTION, SemanticType.COUNTRY, required=False)
    elif case == "other_person":
        provider = ChoiceProvider({"residence": ("o0", 0.99)}, scope="OTHER_PERSON_OR_ENTITY")
    elif case == "custom_type":  # PROFILE_IDENTITY can never fill a custom-typed field
        provider = ChoiceProvider({"residence": ("o0", 0.99)}, semantic="CUSTOM_BOOLEAN")
        field = residence_field(RESIDENCE_QUESTION, SemanticType.CUSTOM_BOOLEAN)
    else:
        candidate = with_address(fictional_candidate, city=None, region=None, country=None)
    packet, _, resolver = resolve_choice(provider, candidate, mock_job, field)
    assert not provider.asked("residence")
    assert packet.answers == []
    assert not stage_traces(resolver, "residence_screener")


# --- round 4, deliverable 2: select-all answers from stored answers and verified facts ----

class MultiProvider(ChoiceProvider):
    """ChoiceProvider for select-all questions: every field classifies as a literal
    COPY_KNOWN answer under ``scope`` (custom-typed ones as CUSTOM_MULTISELECT) and
    choices come from ``picks``; unscripted ones hold (``route`` holds, ``fact_select``
    is UNKNOWN, ``wording`` and ``equivalent_<i>`` are NONE). ``includes_o<i>`` nouls are
    ``option(label)``. A ``source_o<i>`` choice names the first fact whose value
    ``fact(value)`` accepts, with probability ``option(label)`` (NONE takes the rest), so no
    test depends on option or fact order; consistency nouls pass."""

    def __init__(self, picks: dict[str, tuple[str, float]] | None = None, *,
                 option: Any = lambda label: 0.0, fact: Any = lambda value: 0.0,
                 scope: str = "APPLICANT_CURRENT") -> None:
        super().__init__(dict(picks or {}), semantic="CUSTOM_MULTISELECT", scope=scope)
        self.option, self.fact = option, fact

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        self.requests.append(request)
        state = request["state"]
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            kind, _, key = name.partition("_")
            if question["type"] == "noul":
                score = self.option(state["options"][key]) if kind == "includes" else 1.0
                answers[name] = {"type": "noul", "noul": score}
                continue
            criteria = list(question["criteria"])
            confidence = self.confidence
            if kind == "source":
                stated = self.option(state["options"][key])
                facts = [k for k, f in state["facts"].items() if self.fact(f["value"]) >= 0.95]
                choice, probability = ((facts[0], stated) if facts and stated > 0
                                       else ("NONE", 1.0 - stated if facts else 1.0))
                rest = {k: 0.0 for k in criteria}
                rest["NONE"] = 1.0 - probability if choice != "NONE" else probability
                answers[name] = {"type": "choice", "choice": choice, "confidence": confidence,
                                 "probabilities": rest | {choice: probability}}
                continue
            if name[0] in "rnusd" and name[1:].isdigit():
                choice = {"r": "COPY_KNOWN", "n": "literal", "u": self.scope, "s": self.semantic,
                          "d": "APPLICATION_ATTACHMENT"}[name[0]]
                probability, confidence = 1.0, 1.0
            elif name in self.picks:
                choice, probability = self.picks[name]
            else:
                choice, probability = next(k for k in ("hold", "NONE", "UNKNOWN") if k in criteria), 1.0
            rest = (1 - probability) / (len(criteria) - 1)
            answers[name] = {"type": "choice", "choice": choice, "confidence": confidence,
                             "probabilities": {k: probability if k == choice else rest
                                               for k in criteria}}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())


def by_label(scores: dict[str, float]) -> Any:
    """A noul source over option labels: the listed score, 0.0 for any other option."""
    return lambda label: scores.get(label, 0.0)


def naming(item: str) -> Any:
    """A noul source over fact values: 1.0 for a fact whose text names ``item``."""
    return lambda value: 1.0 if item in str(value) else 0.0


TIME_ZONES = ("Which of the following U.S. time zones are you available to work in? "
              "(Select all that apply)")
SAVED_TIME_ZONES = "Which time zones are you available to work in?"
ZONES = ("Eastern", "Central", "Mountain", "Pacific", "Other")


def zones_field() -> ApplicationField:
    return choice_field(TIME_ZONES, SemanticType.CUSTOM_MULTISELECT, *ZONES,
                        control=ControlType.CHECKBOX_GROUP, field_id="zones")


def with_zones(candidate: CandidateProfile, question: str) -> CandidateProfile:
    """The simple-answers time-zone answer, saved as free text for ``question``."""
    return with_saved(candidate, global_answer("sa.time_zones", question, "US Central, US Eastern"))


@pytest.mark.parametrize("question", [SAVED_TIME_ZONES, TIME_ZONES])
def test_a_stored_time_zone_answer_selects_exactly_the_zones_it_includes(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, question: str,
) -> None:
    provider = MultiProvider({"wording": ("q0", 0.98)}, scope="EXPLICIT_ANSWER",
                             option=by_label({"Eastern": 0.98, "Central": 0.98}))
    packet, _, resolver = resolve_choice(provider, with_zones(fictional_candidate, question),
                                         mock_job, zones_field())
    assert packet.is_complete
    [answer] = packet.answers
    assert [(c.value, c.label) for c in answer.value.choices] == [("v0", "Eastern"),
                                                                  ("v1", "Central")]
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, ["sa.time_zones"])
    note = answer.provenance.note or ""
    assert note.endswith("; Jev selected every option the stored answer includes")
    [request] = provider.asked("includes_o0")
    # One noul per listed zone. "Other" is not an item a stored answer names: it is
    # never asked about and never selected.
    assert set(request["questions"]) == {"includes_o0", "includes_o1", "includes_o2",
                                         "includes_o3"}
    assert request["state"]["stored_answer"] == "US Central, US Eastern"
    assert request["state"]["options"] == {f"o{i}": label for i, label in enumerate(ZONES)}
    [trace] = stage_traces(resolver, "multi_select")
    assert (trace["status"], trace["selected"], trace["option_count"]) == (
        "MAPPED", ["o0", "o1"], 4)
    assert "US Central" not in json.dumps(trace)
    assert not provider.asked("equivalent_0") and not provider.asked("fact_select")
    if question == TIME_ZONES:  # the exact wording: the first path, no wording decision
        assert not provider.asked("wording")
        assert note.startswith(f"saved answer for {TIME_ZONES!r}")
        assert answer.confidence == pytest.approx(0.98)
    else:
        [wording] = provider.asked("wording")
        assert wording["state"]["saved_questions"] == {
            "q0": {"question": SAVED_TIME_ZONES, "variants": []}}
        assert wording["state"]["observed_question"]["options"] == list(ZONES)
        assert note.startswith("question wording mapped by Jev from the saved answer for "
                               f"{SAVED_TIME_ZONES!r}")
        assert answer.confidence == pytest.approx(0.97)  # the wording decision's confidence
        [equivalence] = stage_traces(resolver, "question_equivalence")
        assert (equivalence["status"], equivalence["reference_ids"]) == (
            "MAPPED", ["sa.time_zones"])


@pytest.mark.parametrize("scores,status,selected", [
    ({"Eastern": 1.0, "Central": 0.5}, "UNDECIDED", ["o0"]),
    ({}, "NONE", []),
])
@pytest.mark.parametrize("question", [SAVED_TIME_ZONES, TIME_ZONES])
def test_an_undecided_or_empty_zone_selection_holds_the_field(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, question: str,
    scores: dict[str, float], status: str, selected: list[str],
) -> None:
    provider = MultiProvider({"wording": ("q0", 0.98)}, scope="EXPLICIT_ANSWER",
                             option=by_label(scores))
    packet, _, resolver = resolve_choice(provider, with_zones(fictional_candidate, question),
                                         mock_job, zones_field())
    assert packet.answers == [] and not packet.is_complete
    [missing] = packet.missing_inputs
    assert missing.field_id == "zones"
    [trace] = stage_traces(resolver, "multi_select")
    assert (trace["status"], trace["selected"]) == (status, selected)
    assert not provider.asked("fact_select")
    if question == TIME_ZONES:
        assert "Your saved answer 'US Central, US Eastern' cannot be used here" in missing.prompt
    else:
        [equivalence] = stage_traces(resolver, "question_equivalence")
        assert equivalence["status"] == "VALUE_DOES_NOT_FIT"


PLATFORMS = "Which platforms have you managed? (select all)"
PLATFORM_OPTIONS = ("Google Ads", "Meta Ads", "LinkedIn Ads", "TikTok Ads", "Other")
MANAGED_PLATFORMS = "Managed Google Ads and Meta Ads campaigns at Fictional Widgets Co"


def platform_field(*options: str, required: bool = True) -> ApplicationField:
    return choice_field(PLATFORMS, SemanticType.CUSTOM_MULTISELECT, *(options or PLATFORM_OPTIONS),
                        control=ControlType.MULTISELECT, required=required, field_id="platforms")


def with_platform_facts(candidate: CandidateProfile) -> CandidateProfile:
    """Two verified facts: one names Google Ads and Meta Ads, the other no platform."""
    base = candidate.verified_facts()[0]
    facts = [base.model_copy(update={"id": fact_id, "key": key, "value": value, "evidence": [value]})
             for fact_id, key, value in (("fact.platforms", "experience", MANAGED_PLATFORMS),
                                         ("fact.languages", "languages",
                                          "Speaks conversational Spanish"))]
    return candidate.model_copy(update={"facts": facts, "experience": [], "education": []})


def platform_provider(pick: tuple[str, float], option: dict[str, float], *,
                      names: str = "Google Ads") -> MultiProvider:
    return MultiProvider({"fact_select": pick}, option=by_label(option), fact=naming(names),
                         scope="HISTORICAL_OR_CONTEXTUAL")


def test_a_select_all_experience_question_selects_the_options_verified_facts_state(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = platform_provider(("SUPPORTED", 0.98), {"Google Ads": 1.0, "Meta Ads": 1.0})
    packet, _, resolver = resolve_choice(provider, with_platform_facts(fictional_candidate),
                                         mock_job, platform_field())
    assert packet.is_complete
    [answer] = packet.answers
    assert [(c.value, c.label) for c in answer.value.choices] == [("v0", "Google Ads"),
                                                                  ("v1", "Meta Ads")]
    assert answer.provenance.source is AnswerSource.GENERATED_FROM_FACTS
    assert answer.provenance.reference_ids == ["fact.platforms"]  # never the unrelated fact
    assert answer.provenance.note == "options selected by Jev from verified facts that state them"
    assert answer.confidence == pytest.approx(0.97)  # the fact_select confidence
    [request] = provider.asked("fact_select")
    # One source choice per listed platform (never "Other"), over the supplied facts.
    assert set(request["questions"]) == {"fact_select", "source_o0", "source_o1", "source_o2",
                                         "source_o3"}
    assert set(request["questions"]["fact_select"]["criteria"]) == {
        "SUPPORTED", "UNKNOWN", "NOT_EXPERIENCE"}
    assert set(request["questions"]["source_o1"]["criteria"]) == {"f0", "f1", "NONE"}
    assert request["state"]["options"] == {f"o{i}": label
                                           for i, label in enumerate(PLATFORM_OPTIONS)}
    assert [fact["id"] for fact in request["state"]["facts"].values()] == [
        "fact.platforms", "fact.languages"]
    assert request["state"]["screener_version"] == "experience-screener-v1"
    [trace] = stage_traces(resolver, "fact_screener")
    assert (trace["kind"], trace["status"], trace["selected"], trace["evidence_ids"]) == (
        "multi", "ANSWERED", ["o0", "o1"], ["fact.platforms"])
    assert trace["sources"] == {"o0": "fact.platforms", "o1": "fact.platforms"}
    assert not provider.asked("route") and not provider.asked("wording")


@pytest.mark.parametrize("pick,option,names,status", [
    (("UNKNOWN", 0.99), {}, "Google Ads", "UNKNOWN"),
    (("SUPPORTED", 0.98), {}, "Google Ads", "UNKNOWN"),  # no listed option is stated
    (("SUPPORTED", 0.98), {"Google Ads": 1.0, "Meta Ads": 0.5}, "Google Ads", "UNDECIDED"),
    (("SUPPORTED", 0.98), {"Meta Ads": 0.5}, "Google Ads", "UNDECIDED"),  # nothing else chosen
    (("SUPPORTED", 0.98), {"Google Ads": 1.0}, "Pinterest", "UNKNOWN"),  # no fact names one
    (("SUPPORTED", 0.90), {"Google Ads": 1.0}, "Google Ads", "UNKNOWN"),  # below the gate
    (("NOT_EXPERIENCE", 0.90), {}, "Google Ads", "UNKNOWN"),  # too weak to leave the screener
])
def test_an_unstated_or_undecided_platform_selection_holds_with_the_fact_prompt(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    pick: tuple[str, float], option: dict[str, float], names: str, status: str,
) -> None:
    provider = platform_provider(pick, option, names=names)
    packet, _, resolver = resolve_choice(provider, with_platform_facts(fictional_candidate),
                                         mock_job, platform_field())
    assert packet.answers == [] and not packet.is_complete
    [missing] = packet.missing_inputs
    assert (missing.field_id, missing.reason) == ("platforms", MissingReason.NO_ANSWER)
    assert missing.prompt.startswith(f"Add a verified fact that states the answer to {PLATFORMS!r}")
    assert "nothing is estimated" in missing.prompt
    [trace] = stage_traces(resolver, "fact_screener")
    assert (trace["kind"], trace["status"]) == ("multi", status)
    assert not provider.asked("route")


def test_a_select_all_question_that_is_not_about_experience_leaves_the_screener(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = platform_provider(("NOT_EXPERIENCE", 0.99), {"Google Ads": 1.0, "Meta Ads": 1.0})
    packet, _, resolver = resolve_choice(provider, with_platform_facts(fictional_candidate),
                                         mock_job, platform_field())
    assert packet.answers == []
    [missing] = packet.missing_inputs
    assert missing.prompt == "The question needs an explicit or unambiguous verified answer"
    [trace] = stage_traces(resolver, "fact_screener")
    assert (trace["kind"], trace["status"]) == ("multi", "NOT_SCREENER")
    order = [name for request in provider.requests for name in request["questions"]
             if name in ("fact_select", "route")]
    assert order == ["fact_select", "route"]  # the ordinary fact route, which holds here


RACE = "Race/ethnicity (select all that apply)"
RACE_OPTIONS = ("Hispanic or Latino", "Asian", "Black or African American", "White",
                "Decline to self-identify")
SITE_RACE_OPTIONS = ("Hispanic or Latino", "Asian (Not Hispanic or Latino)",
                     "Black or African American (Not Hispanic or Latino)",
                     "White (Not Hispanic or Latino)", "Decline to self-identify")


def race_field(*options: str) -> ApplicationField:
    return choice_field(RACE, SemanticType.EEO_RACE_ETHNICITY, *(options or RACE_OPTIONS),
                        control=ControlType.CHECKBOX_GROUP, field_id="race")


@pytest.mark.parametrize("scope", ["APPLICANT_CURRENT", "HISTORICAL_OR_CONTEXTUAL"])
def test_a_protected_select_all_question_is_never_answered_from_facts(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, scope: str,
) -> None:
    provider = MultiProvider({"fact_select": ("SUPPORTED", 0.99)}, scope=scope,
                             option=lambda label: 1.0, fact=lambda value: 1.0)
    assert fictional_candidate.verified_facts()
    packet, _, resolver = resolve_choice(provider, fictional_candidate, mock_job, race_field())
    assert packet.answers == [] and not packet.is_complete
    [missing] = packet.missing_inputs
    assert (missing.field_id, missing.reason) == ("race", MissingReason.EXPLICIT_ANSWER_REQUIRED)
    assert not provider.asked("fact_select") and not provider.asked("route")
    assert not stage_traces(resolver, "fact_screener")
    [classification] = provider.requests  # no fact ever reaches Jev for it
    assert "facts" not in classification["state"]


@pytest.mark.parametrize("value,options,picks,asked,suffix", [
    (["Asian", "White"], RACE_OPTIONS, {}, set(), ""),  # exact options: no mapping call
    (["Asian", "White"], SITE_RACE_OPTIONS,
     {"equivalent_0": ("o1", 0.99), "equivalent_1": ("o3", 0.99)},
     {"equivalent_0", "equivalent_1"}, "; Jev mapped it onto the site's option wording"),
    ("Asian; White", RACE_OPTIONS, {}, {f"includes_o{i}" for i in range(4)},
     "; Jev selected every option the stored answer includes"),
])
def test_a_protected_select_all_question_maps_the_users_own_saved_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, value: str | list[str],
    options: tuple[str, ...], picks: dict[str, tuple[str, float]], asked: set[str],
    suffix: str,
) -> None:
    saved = global_answer("sa.race", RACE, value, semantic=SemanticType.EEO_RACE_ETHNICITY)
    provider = MultiProvider(picks, scope="EXPLICIT_ANSWER",
                             option=by_label({"Asian": 0.99, "White": 0.99}))
    packet, _, _ = resolve_choice(provider, with_saved(fictional_candidate, saved), mock_job,
                                  race_field(*options))
    assert packet.is_complete
    [answer] = packet.answers
    assert [(c.value, c.label) for c in answer.value.choices] == [("v1", options[1]),
                                                                  ("v3", options[3])]
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, ["sa.race"])
    assert answer.provenance.note == f"saved answer for {RACE!r}{suffix}"
    # Only the saved answer's own mapping is asked: never a fact screener or a wording
    # decision ("Decline to self-identify" is not an item the answer can include).
    assert {name for request in provider.requests[1:] for name in request["questions"]} == asked


@pytest.mark.parametrize("case", ["optional", "no_items"])
def test_a_select_all_question_outside_the_screener_scope_is_not_fact_screened(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, case: str,
) -> None:
    provider = MultiProvider({"fact_select": ("SUPPORTED", 0.99)}, option=lambda label: 1.0,
                             fact=lambda value: 1.0, scope="HISTORICAL_OR_CONTEXTUAL")
    field = (platform_field(required=False) if case == "optional"
             else platform_field("Other", "None of the above"))
    packet, _, resolver = resolve_choice(provider, with_platform_facts(fictional_candidate),
                                         mock_job, field)
    assert packet.answers == []
    assert not provider.asked("fact_select") and not provider.asked("route")
    assert not stage_traces(resolver, "fact_screener")
    assert [m.field_id for m in packet.missing_inputs] == (
        [] if case == "optional" else ["platforms"])


def test_each_option_selected_from_facts_cites_the_fact_that_states_it(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    candidate = with_platform_facts(fictional_candidate)
    base = candidate.verified_facts()[0]
    tiktok = base.model_copy(update={"id": "fact.tiktok", "key": "experience",
        "value": "Ran TikTok Ads for Example Labs", "evidence": ["Ran TikTok Ads for Example Labs"]})
    candidate = candidate.model_copy(update={"facts": [*candidate.facts, tiktok]})

    class PerOption(MultiProvider):
        def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
            response = super().__call__(url, headers, body, timeout)
            request = self.requests[-1]
            payload = json.loads(response.body)
            for name in request["questions"]:
                kind, _, key = name.partition("_")
                if kind != "source":
                    continue
                label = request["state"]["options"][key]
                facts = [k for k, f in request["state"]["facts"].items() if label in f["value"]]
                choice = facts[0] if facts else "NONE"
                payload["answers"][name] = {"type": "choice", "choice": choice, "confidence": 0.97,
                    "probabilities": {k: 1.0 if k == choice else 0.0
                                      for k in request["questions"][name]["criteria"]}}
            return HttpResponse(200, {}, json.dumps(payload).encode())

    provider = PerOption({"fact_select": ("SUPPORTED", 0.98)}, scope="HISTORICAL_OR_CONTEXTUAL")
    packet, _, resolver = resolve_choice(provider, candidate, mock_job, platform_field())
    [answer] = packet.answers
    assert [c.label for c in answer.value.choices] == ["Google Ads", "Meta Ads", "TikTok Ads"]
    # Every selected option cites the fact that states it; the unrelated fact never.
    assert answer.provenance.reference_ids == ["fact.platforms", "fact.tiktok"]
    [trace] = stage_traces(resolver, "fact_screener")
    assert trace["sources"] == {"o0": "fact.platforms", "o1": "fact.platforms", "o3": "fact.tiktok"}


def test_a_confirmed_reworded_answer_that_does_not_fit_holds_instead_of_a_fact_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    # Regression: the saved answer asks the same question, but one of its platforms is
    # undecided. The field holds for the user; the fact screener never answers it partially.
    saved = global_answer("sa.platforms", "Which ad platforms have you managed?",
                          "Google Ads, Meta Ads")
    provider = MultiProvider({"wording": ("q0", 0.98), "fact_select": ("SUPPORTED", 0.99)},
                             option=by_label({"Google Ads": 1.0, "Meta Ads": 0.5}),
                             fact=naming("Google Ads"), scope="HISTORICAL_OR_CONTEXTUAL")
    candidate = with_saved(with_platform_facts(fictional_candidate), saved)
    packet, _, resolver = resolve_choice(provider, candidate, mock_job, platform_field())
    assert packet.answers == [] and not packet.is_complete
    [missing] = packet.missing_inputs
    assert (missing.field_id, missing.reason) == ("platforms", MissingReason.AMBIGUOUS)
    assert missing.prompt.startswith(
        "Your saved answer to 'Which ad platforms have you managed?' answers this question")
    [equivalence] = stage_traces(resolver, "question_equivalence")
    assert equivalence["status"] == "VALUE_DOES_NOT_FIT"
    [multi] = stage_traces(resolver, "multi_select")
    assert multi["status"] == "UNDECIDED"
    assert not provider.asked("fact_select") and not provider.asked("route")
    assert not stage_traces(resolver, "fact_screener")


def test_an_optional_field_with_a_confirmed_reworded_answer_that_does_not_fit_stays_empty(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    saved = global_answer("sa.platforms", "Which ad platforms have you managed?",
                          "Google Ads, Meta Ads")
    provider = MultiProvider({"wording": ("q0", 0.98), "fact_select": ("SUPPORTED", 0.99)},
                             option=by_label({"Google Ads": 1.0, "Meta Ads": 0.5}),
                             fact=naming("Google Ads"), scope="HISTORICAL_OR_CONTEXTUAL")
    candidate = with_saved(with_platform_facts(fictional_candidate), saved)
    packet, _, _ = resolve_choice(provider, candidate, mock_job, platform_field(required=False))
    assert packet.answers == [] and packet.missing_inputs == []
    assert not provider.asked("fact_select") and not provider.asked("route")



# --- round 4 addendum B: the live Rippling state-list residence question -------------------

RIPPLING_STATES = ("Do you reside in any of the following states: AL, AZ, CA, CO, CT, DC, FL, GA, "
                   "ID, IL, KS, KY, LA, ME, MD, MA, MI, MN, MS, MO, NE, NH, NJ, NY, NC, OH, OR, "
                   "PA, SC, SD, TN, TX, UT, VT, VA, WA, WV, WI?")


class SemanticSplit(ChoiceProvider):
    """ChoiceProvider whose semantic answers (``s<i>``) split their mass as ``split``."""

    def __init__(self, split: dict[str, float], picks: dict[str, tuple[str, float]]) -> None:
        super().__init__(dict(picks))
        self.split = split

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        response = super().__call__(url, headers, body, timeout)
        payload = json.loads(response.body)
        for name, answer in payload["answers"].items():
            if name[0] == "s" and name[1:].isdigit():
                answer.update(choice=max(self.split, key=lambda key: self.split[key]),
                              confidence=0.97,
                              probabilities={key: self.split.get(key, 0.0)
                                             for key in answer["probabilities"]})
        return HttpResponse(200, {}, json.dumps(payload).encode())


def rippling_field(control: ControlType = ControlType.SELECT) -> ApplicationField:
    """The bare label and Yes/No options, typed as the browser's heuristics type it."""
    heuristic = classify_semantics(label=RIPPLING_STATES, control_type=control)
    options = [FieldOption(value=v, label=v) for v in YES_NO] if control is ControlType.SELECT else None
    return ApplicationField(id="residence", selector="#residence", label=RIPPLING_STATES,
                            semantic_type=heuristic, control_type=control, required=True,
                            options=options)


STATE_OR_LOCATION = {"STATE": 0.62, "LOCATION": 0.36, "CUSTOM_BOOLEAN": 0.02}


@pytest.mark.parametrize("region,city,pick,expected", [
    ("OR", "Springfield", "o0", "Yes"),  # Oregon is listed
    ("IN", "Indianapolis", "o1", "No"),  # Indiana is not
])
def test_the_live_rippling_state_list_question_is_answered_from_the_address(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    region: str, city: str, pick: str, expected: str,
) -> None:
    field = rippling_field()
    # The label says "states": the browser's heuristics leave it custom-typed, so its
    # type comes from Jev, who reads it as both STATE and LOCATION.
    assert field.semantic_type is SemanticType.CUSTOM_SELECT
    provider = SemanticSplit(STATE_OR_LOCATION, {"residence": (pick, 0.99)})
    candidate = with_address(fictional_candidate, region=region, city=city)
    packet, ctx, resolver = resolve_choice(provider, candidate, mock_job, field)
    assert ctx.form.field("residence").semantic_type is SemanticType.STATE
    assert packet.is_complete
    [answer] = packet.answers
    assert (answer.value.label, answer.semantic_type) == (expected, SemanticType.STATE)
    assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY
    assert answer.confidence == pytest.approx(0.97)  # the pooled residence mass is 0.98
    [trace] = stage_traces(resolver, "residence_screener")
    assert trace["status"] == "ANSWERED"
    assert trace["state_list_member"] is (expected == "Yes")
    [classification] = [r for r in provider.requests if "s0" in r["questions"]]
    assert classification["state"]["version"] == "full-form-routing-v12"
    scopes = classification["questions"]["u0"]["criteria"]
    assert "whether they live in a named country or in one of listed states" in scopes["APPLICANT_CURRENT"]
    assert scopes["EXPLICIT_ANSWER"].endswith("or where the applicant currently lives.")


@pytest.mark.parametrize("split,control", [
    ({"STATE": 0.60, "CUSTOM_BOOLEAN": 0.40}, ControlType.SELECT),  # not a residence reading
    (STATE_OR_LOCATION, ControlType.TEXT),  # a text box copies one exact datum: never pooled
])
def test_a_split_that_is_not_one_residence_reading_on_a_choice_stays_unknown(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    split: dict[str, float], control: ControlType,
) -> None:
    provider = SemanticSplit(split, {"residence": ("o0", 0.99)})
    packet, ctx, resolver = resolve_choice(provider, fictional_candidate, mock_job,
                                           rippling_field(control))
    assert ctx.form.field("residence").semantic_type is SemanticType.UNKNOWN
    assert packet.answers == [] and not provider.asked("residence")
    assert not stage_traces(resolver, "residence_screener")
    [missing] = packet.missing_inputs
    assert missing.prompt.startswith("Required:")


# --- round 5: independent fields resolved concurrently within one form ----------------------

LOOKUPS = {  # typed value -> (the site's suggestions, the one that denotes the typed place)
    "United States": (["United States Minor Outlying Islands", "United States of America"],
                      "United States of America"),
    "Springfield, OR": (["Springfield, Illinois, United States",
                         "Springfield, Oregon, United States"], "Springfield, Oregon, United States"),
    "Oregon": (["Oregon, Wisconsin, United States", "Oregon, United States"],
               "Oregon, United States"),
}


def content_pick(name: str, state: dict[str, Any]) -> tuple[str, float] | None:
    """An option pick read from one request alone, as Jev would make it: the option with
    the stored answer's yes/no polarity, the careers option, the "Yes" that is true for the
    verified address, the saved sponsorship wording, or the suggestion naming the typed
    place. None keeps the scripted ``ChoiceProvider`` answer."""
    options: dict[str, str] = state.get("options", {})
    if name.startswith("equivalent_"):
        polarity = str(state["stored_answers"][name]).casefold() + ","
        return next(((key, 0.98) for key, label in options.items()
                     if label.casefold().startswith(polarity)), None)
    if name == "referral":
        return next(((f"careers_{key}", 0.98) for key, label in options.items()
                     if "careers" in label), None)
    if name == "residence":
        return next(((key, 0.99) for key, label in options.items() if label == "Yes"), None)
    if name == "wording":
        return next(((key, 0.98) for key, saved in state["saved_questions"].items()
                     if "sponsorship" in saved["question"]), None)
    if name == "lookup":
        wanted = LOOKUPS[state["typed_value"]][1]
        return next(((key, 0.99) for key, label in state["suggestions"].items()
                     if label == wanted), None)
    return None


class ByContent(ChoiceProvider):
    """ChoiceProvider whose option choices come from ``content_pick``: every answer depends
    on its own request only, never on call order, so concurrent callers get exactly the
    answers of a sequential run."""

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        response = super().__call__(url, headers, body, timeout)
        request, payload = json.loads(body), json.loads(response.body)
        for name, question in request["questions"].items():
            pick = content_pick(name, request["state"]) if question["type"] == "choice" else None
            if pick is not None:
                choice, probability = pick
                rest = (1 - probability) / (len(question["criteria"]) - 1)
                payload["answers"][name] = {"type": "choice", "choice": choice,
                    "confidence": self.confidence, "probabilities": {
                        key: probability if key == choice else rest for key in question["criteria"]}}
        return HttpResponse(200, {}, json.dumps(payload).encode())


class Overlapping:
    """A thread-safe transport around a provider whose answers depend on request content
    only. Every decision but the full-form classification waits for ``gate`` (if any, 5 s
    at most), sleeps ``delay(request)`` seconds and counts as in flight meanwhile
    (``peak``: the most at once); one that ``fails`` then gets HTTP 503."""

    def __init__(self, inner: Any, *, delay: Any = lambda request: 0.0,
                 fails: Any = lambda request: False, gate: Any = None) -> None:
        import threading

        self.inner, self.delay, self.fails, self.gate = inner, delay, fails, gate
        self.lock = threading.Lock()
        self.calls = self.in_flight = self.peak = 0

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        import time

        request = json.loads(body)
        if any(name[0] in "rnusd" and name[1:].isdigit() for name in request["questions"]):
            return self.inner(url, headers, body, timeout)  # the one batched classification
        with self.lock:
            self.calls += 1
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            if self.gate is not None:
                self.gate.wait(5)
            time.sleep(self.delay(request))
            if self.fails(request):
                return HttpResponse(503, {}, b"{}")
            return self.inner(url, headers, body, timeout)
        finally:
            with self.lock:
                self.in_flight -= 1


def resolve_concurrently(transport: Any, candidate: CandidateProfile, job: JobRecord,
                         fields: list[ApplicationField], *, max_concurrency: int,
                         ) -> tuple[Any, DynamicPacketResolver]:
    """``resolve_choice`` resolving at most ``max_concurrency`` fields at once; a hang
    fails the test after 20 s."""
    router = AIFormRouter(decisions(transport))
    f = router.annotate(ApplicationForm(url="https://example.test/apply", fields=fields),
                        document_id="concurrent-fields")
    ctx = context(f, candidate, job)
    resolver = DynamicPacketResolver(router.decisions, router=router,
                                     max_concurrency=max_concurrency)
    packet = asyncio.run(asyncio.wait_for(resolver.resolve(ctx), timeout=20))
    assert ctx.problems(packet) == []
    return packet, resolver


def packet_view(packet: Any) -> dict[str, Any]:
    """The packet without the ids and timestamp that every resolution generates."""
    data = packet.model_dump(mode="json", exclude={"id", "created_at"})
    for missing in data["missing_inputs"]:
        del missing["id"]
    return data


def receipt_view(resolver: DynamicPacketResolver) -> list[tuple[Any, ...]]:
    """The provider receipts in budget order, without their measured latency."""
    return [(r.purpose, r.status, r.resolved_model, r.cost_usd, r.reserved_usd)
            for r in resolver.decisions.budget.receipts]


AUTHORIZED = ("Yes, I am authorized to work in the US", "No, I am not authorized to work in the US")


def stored_answer_fields() -> list[ApplicationField]:
    """Four fields that each need their own Jev decision while stored answers are placed:
    option equivalence, the referral policy, a residence screener and a reworded saved
    answer (a wording decision, then option equivalence)."""
    return [choice_field(WORK_AUTH, SemanticType.WORK_AUTHORIZATION, *AUTHORIZED,
                         field_id="work_auth"),
            choice_field("How did you hear about us?", SemanticType.REFERRAL_SOURCE, "LinkedIn",
                         "Company careers website", "Other", control=ControlType.SELECT,
                         field_id="referral"),
            choice_field(RESIDENCE_QUESTION, SemanticType.COUNTRY, *YES_NO, field_id="residence"),
            choice_field(REWORDED_SPONSORSHIP, SemanticType.SPONSORSHIP, *SPONSORSHIP_OPTIONS,
                         field_id="sponsorship")]


@pytest.mark.parametrize("limit", [3, 2])
def test_stored_answer_decisions_overlap_and_match_a_sequential_run(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, limit: int,
) -> None:
    candidate = with_referral(fictional_candidate)

    def delay(request: dict[str, Any]) -> float:
        # The first field's decision is the slowest, so the fields finish out of form order.
        return 0.3 if request["state"].get("question") == WORK_AUTH else 0.1

    provider = Overlapping(ByContent(), delay=delay)
    packet, resolver = resolve_concurrently(provider, candidate, mock_job, stored_answer_fields(),
                                            max_concurrency=limit)
    one_at_a_time = Overlapping(ByContent())
    sequential, reference = resolve_concurrently(one_at_a_time, candidate, mock_job,
                                                 stored_answer_fields(), max_concurrency=1)
    assert 2 <= provider.peak <= limit and one_at_a_time.peak == 1
    assert provider.calls == one_at_a_time.calls == 5
    assert packet.is_complete
    assert [(a.field_id, a.value.label, a.provenance.source) for a in packet.answers] == [
        ("work_auth", AUTHORIZED[0], AnswerSource.SAVED_ANSWER),
        ("referral", "Company careers website", AnswerSource.SAVED_ANSWER),
        ("residence", "Yes", AnswerSource.PROFILE_IDENTITY),
        ("sponsorship", SPONSORSHIP_OPTIONS[1], AnswerSource.SAVED_ANSWER)]
    # Traces and receipts come out in form order, exactly as with one field at a time.
    assert [(t["field_id"], t["stage"], t["status"]) for t in resolver.narrative_traces] == [
        ("work_auth", "option_equivalence", "MAPPED"),
        ("referral", "referral_policy", "MAPPED"),
        ("residence", "residence_screener", "ANSWERED"),
        ("sponsorship", "option_equivalence", "MAPPED"),
        ("sponsorship", "question_equivalence", "MAPPED")]
    assert [r.purpose for r in resolver.decisions.budget.receipts] == [
        "full_form_routes", "option_equivalence", "referral_policy", "residence_screener",
        "question_equivalence", "option_equivalence"]
    assert packet_view(packet) == packet_view(sequential)
    assert resolver.narrative_traces == reference.narrative_traces
    assert receipt_view(resolver) == receipt_view(reference)


CHANNELS = "Which paid channels have you run campaigns on? (select all)"
NETWORKS = "Which ad networks have you bought media on? (select all that apply)"


def screener_fields() -> list[ApplicationField]:
    """Three select-all experience questions, each a fact-grounded screener."""
    return [platform_field(),
            choice_field(CHANNELS, SemanticType.CUSTOM_MULTISELECT, "Meta Ads", "Google Ads",
                         "Pinterest Ads", "Other", control=ControlType.CHECKBOX_GROUP,
                         field_id="channels"),
            choice_field(NETWORKS, SemanticType.CUSTOM_MULTISELECT, "Google Ads", "Reddit Ads",
                         "Meta Ads", control=ControlType.CHECKBOX_GROUP, field_id="networks")]


def with_search_fact(candidate: CandidateProfile) -> CandidateProfile:
    """The platform facts plus a second Google Ads fact: the fact every screener selects
    then needs one Jev consistency verdict, which later fields reuse."""
    candidate = with_platform_facts(candidate)
    text = "Ran Google Ads search campaigns for Example Labs"
    search = candidate.facts[0].model_copy(update={"id": "fact.search", "key": "experience",
                                                   "value": text, "evidence": [text]})
    return candidate.model_copy(update={"facts": [*candidate.facts, search]})


def test_fact_screeners_overlap_and_reuse_one_consistency_verdict_in_form_order(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    candidate = with_search_fact(fictional_candidate)

    def screeners(**kwargs: Any) -> Overlapping:
        return Overlapping(platform_provider(("SUPPORTED", 0.98),
                                             {"Google Ads": 1.0, "Meta Ads": 1.0}), **kwargs)

    def delay(request: dict[str, Any]) -> float:
        # The first screener is the slowest: the others reach the consistency check first.
        return 0.25 if request["state"].get("field", {}).get("question") == PLATFORMS else 0.1

    provider = screeners(delay=delay)
    packet, resolver = resolve_concurrently(provider, candidate, mock_job, screener_fields(),
                                            max_concurrency=3)
    one_at_a_time = screeners()
    sequential, reference = resolve_concurrently(one_at_a_time, candidate, mock_job,
                                                 screener_fields(), max_concurrency=1)
    assert 2 <= provider.peak <= 3 and one_at_a_time.peak == 1
    assert packet.is_complete
    assert [(a.field_id, [c.label for c in a.value.choices], a.provenance.reference_ids)
            for a in packet.answers] == [
        ("platforms", ["Google Ads", "Meta Ads"], ["fact.platforms"]),
        ("channels", ["Meta Ads", "Google Ads"], ["fact.platforms"]),
        ("networks", ["Google Ads", "Meta Ads"], ["fact.platforms"])]
    # The first field in form order asks for the verdict; the later ones reuse it.
    assert [t["cached"] for t in stage_traces(resolver, "consistency")] == [[], ["f0"], ["f0"]]
    assert [r.purpose for r in resolver.decisions.budget.receipts] == [
        "full_form_routes", "fact_screener", "narrative_consistency", "fact_screener",
        "fact_screener"]
    assert packet_view(packet) == packet_view(sequential)
    assert resolver.narrative_traces == reference.narrative_traces
    assert receipt_view(resolver) == receipt_view(reference)


def test_lookup_suggestions_are_decided_concurrently_and_reported_in_the_given_order(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    fields = {field_id: ApplicationField(id=field_id, selector=f"#{field_id}", label=label,
                                         semantic_type=semantic, required=True,
                                         control_type=ControlType.TYPEAHEAD)
              for field_id, label, semantic in (("city", "City", SemanticType.CITY),
                                                ("state", "State", SemanticType.STATE),
                                                ("country", "Country", SemanticType.COUNTRY))}
    ctx = context(ApplicationForm(url="https://example.test/apply", fields=list(fields.values())),
                  fictional_candidate, mock_job)
    # Given out of form order; the first decision is the slowest and the city's fails.
    lookups = [(fields[field_id], typed, LOOKUPS[typed][0]) for field_id, typed in (
        ("country", "United States"), ("city", "Springfield, OR"), ("state", "Oregon"))]
    delays = {"United States": 0.25, "Springfield, OR": 0.15, "Oregon": 0.05}

    def fails(request: dict[str, Any]) -> bool:
        return request["state"]["typed_value"] == "Springfield, OR"

    def choose(transport: Overlapping, limit: int) -> tuple[list[str | None], DynamicPacketResolver]:
        resolver = DynamicPacketResolver(decisions(transport), max_concurrency=limit)
        labels = asyncio.run(asyncio.wait_for(resolver.choose_suggestions(ctx, lookups), timeout=20))
        return labels, resolver

    provider = Overlapping(ByContent(), fails=fails,
                           delay=lambda request: delays[request["state"]["typed_value"]])
    labels, resolver = choose(provider, 3)
    one_at_a_time = Overlapping(ByContent(), fails=fails)
    sequential, reference = choose(one_at_a_time, 1)
    assert provider.peak >= 2 and one_at_a_time.peak == 1
    assert provider.calls == one_at_a_time.calls == 3
    assert labels == sequential == ["United States of America", None, "Oregon, United States"]
    traces = stage_traces(resolver, "suggestion_choice")
    assert [(t["field_id"], t["status"]) for t in traces] == [
        ("country", "CHOSEN"), ("city", "HELD"), ("state", "CHOSEN")]
    assert resolver.narrative_traces == reference.narrative_traces
    assert [(r.purpose, r.status) for r in resolver.decisions.budget.receipts] == [
        ("lookup_suggestion", "OK"), ("lookup_suggestion", "UNAVAILABLE"),
        ("lookup_suggestion", "OK")]
    assert receipt_view(resolver) == receipt_view(reference)
    for field_id, status in (("country", "CHOSEN"), ("city", "HELD"), ("state", "CHOSEN")):
        decision = resolver.suggestion_decision(field_id)
        assert decision is not None
        assert (decision["status"], decision["suggestion_count"]) == (status, 2)
        assert "Springfield" not in json.dumps(decision) and "Oregon" not in json.dumps(decision)
    held = resolver.suggestion_decision("city")
    assert held is not None and held["reason"] == "Jev UNAVAILABLE"


class GatedTransport:
    """A Jev transport that holds every call until ``release`` is set (5 s at most) and
    counts the calls; the calls numbered in ``failing`` (from 1) get HTTP 503."""

    def __init__(self, *failing: int) -> None:
        import threading

        self.lock, self.release = threading.Lock(), threading.Event()
        self.failing, self.calls = set(failing), 0

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        with self.lock:
            self.calls += 1
            call = self.calls
        self.release.wait(5)
        if call in self.failing:
            return HttpResponse(503, {}, b"{}")
        questions = json.loads(body)["questions"]
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": {name: {"type": "noul", "noul": 0.5} for name in questions},
            "usage": {"cost": 0.0001}}).encode())


def open_transport() -> GatedTransport:
    transport = GatedTransport()
    transport.release.set()
    return transport


def probe(topic: str) -> Any:
    """A one-question decision request about ``topic``."""
    from interviewmaxxing_selection.jev import DecisionRequest, NoulQuestion

    return DecisionRequest(model="typesafe/jev-1.13", state={"topic": topic}, questions={
        "probe": NoulQuestion(instructions="Is this a synthetic probe?")})


def decide_in_thread(d: BoundedDecisions, request: Any, results: dict[str, Any], name: str) -> Any:
    """A started daemon thread that stores ``d.decide(request)`` (or its hold) in ``results``."""
    import threading

    def run() -> None:
        try:
            results[name] = d.decide(request, purpose="single_flight")
        except AIHold as exc:
            results[name] = exc

    thread = threading.Thread(target=run, name=f"decide-{name}", daemon=True)
    thread.start()
    return thread


def wait_until(condition: Any, timeout: float = 5.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.002)
    return True


def parked_in_decide(thread: Any) -> bool:
    """Whether ``thread`` blocks in a ``threading`` wait called by ``BoundedDecisions.decide``
    itself (another caller's flight), not by the transport of a call of its own."""
    import sys

    frame = sys._current_frames().get(thread.ident)
    waiting = False
    while frame is not None and frame.f_globals.get("__name__") == "threading":
        waiting, frame = True, frame.f_back
    return waiting and frame is not None and frame.f_code is BoundedDecisions.decide.__code__


def test_identical_concurrent_decisions_share_one_provider_call_and_its_cache() -> None:
    from interviewmaxxing_selection.jev import DecisionResponse

    transport = GatedTransport()
    d = decisions(transport)
    results: dict[str, Any] = {}
    first = decide_in_thread(d, probe("shared"), results, "first")
    assert wait_until(lambda: transport.calls == 1)  # the first caller is with the provider
    second = decide_in_thread(d, probe("shared"), results, "second")
    assert wait_until(lambda: transport.calls > 1 or parked_in_decide(second))
    assert transport.calls == 1  # the identical request waits for the call in flight
    transport.release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert isinstance(results["first"], DecisionResponse)
    assert results["second"] == results["first"]
    assert transport.calls == 1 and d.budget.calls == 1
    assert [(r.purpose, r.status) for r in d.budget.receipts] == [("single_flight", "OK")]
    # The shared response is cached: an identical request later makes no call.
    assert d.decide(probe("shared"), purpose="again") == results["first"]
    assert transport.calls == 1 and len(d.budget.receipts) == 1


def test_without_a_cache_identical_concurrent_decisions_each_call_the_provider() -> None:
    transport = GatedTransport()
    d = decisions(transport)
    d.max_cache_entries = 0
    results: dict[str, Any] = {}
    threads = [decide_in_thread(d, probe("shared"), results, name) for name in ("first", "second")]
    assert wait_until(lambda: transport.calls == 2, timeout=2)  # both with the provider at once
    transport.release.set()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert results["first"] == results["second"]
    assert transport.calls == 2 and d.budget.calls == 2 and len(d.budget.receipts) == 2
    d.decide(probe("shared"), purpose="again")
    assert transport.calls == 3  # nothing was cached either


def test_after_a_failed_flight_the_waiting_caller_calls_the_provider_itself() -> None:
    from interviewmaxxing_selection.jev import DecisionResponse

    transport = GatedTransport(1)  # the first call fails with HTTP 503
    d = decisions(transport)
    results: dict[str, Any] = {}
    first = decide_in_thread(d, probe("shared"), results, "first")
    assert wait_until(lambda: transport.calls == 1)
    second = decide_in_thread(d, probe("shared"), results, "second")
    assert wait_until(lambda: transport.calls > 1 or parked_in_decide(second))
    assert transport.calls == 1
    transport.release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert isinstance(results["first"], AIHold) and str(results["first"]) == "Jev UNAVAILABLE"
    assert isinstance(results["second"], DecisionResponse)  # never the first caller's failure
    assert results["second"].answers["probe"].noul == 0.5
    assert transport.calls == 2  # the failure is not shared: the waiter asked again, as in sequence
    assert [r.status for r in d.budget.receipts] == ["UNAVAILABLE", "OK"]
    assert d.decide(probe("shared"), purpose="again") == results["second"]
    assert transport.calls == 2  # the successful answer is cached


def test_buffered_receipts_wait_for_their_flush_and_keep_the_buffer_order() -> None:
    import contextvars
    import threading

    from interviewmaxxing_browser.ai.providers import buffered_receipts, flush_receipts

    transport = open_transport()
    one, two = decisions(transport), decisions(transport)

    def ask(d: BoundedDecisions, topic: str) -> None:
        d.decide(probe(topic), purpose=topic)

    async def off_the_loop() -> None:
        await asyncio.to_thread(ask, one, "mike")

    buffer: list[Any] = []
    with buffered_receipts(buffer):
        ask(one, "zulu")
        worker = threading.Thread(target=contextvars.copy_context().run, args=(ask, two, "alpha"),
                                  daemon=True)
        worker.start()
        worker.join(5)
        asyncio.run(off_the_loop())
        # Every call is counted at once, but no receipt has reached its budget yet.
        assert (one.budget.calls, two.budget.calls) == (2, 1)
        assert one.budget.receipts == [] and two.budget.receipts == []
    assert [(budget is one.budget, receipt.purpose) for budget, receipt in buffer] == [
        (True, "zulu"), (False, "alpha"), (True, "mike")]
    ask(one, "outside")  # outside the context a receipt goes straight to its budget
    flush_receipts(buffer)
    assert buffer == []
    assert [r.purpose for r in one.budget.receipts] == ["outside", "zulu", "mike"]
    assert [r.purpose for r in two.budget.receipts] == ["alpha"]
    # The flush order, not the recording order, decides the order in the budget.
    recorded_first: list[Any] = []
    recorded_second: list[Any] = []
    with buffered_receipts(recorded_first):
        ask(two, "echo")
    with buffered_receipts(recorded_second):
        ask(two, "bravo")
    flush_receipts(recorded_second)
    flush_receipts(recorded_first)
    assert [r.purpose for r in two.budget.receipts] == ["alpha", "bravo", "echo"]


def test_a_buffered_context_still_refuses_a_call_over_the_limit_at_once() -> None:
    from interviewmaxxing_browser.ai.providers import buffered_receipts

    transport = open_transport()
    capped = decisions(transport, CallBudget(max_calls=1))
    held: list[Any] = []
    with buffered_receipts(held):
        capped.decide(probe("allowed"), purpose="allowed")
        with pytest.raises(AIHold, match="budget exhausted"):
            capped.decide(probe("refused"), purpose="refused")
        assert transport.calls == 1  # the refused call never reached the provider
        assert capped.budget.calls == 1 and capped.budget.receipts == []
    assert [(budget is capped.budget, r.purpose, r.status) for budget, r in held] == [
        (True, "allowed", "OK")]
    # Its cost was accounted at once, exactly as without a buffer.
    unbuffered = decisions(open_transport(), CallBudget(max_calls=1))
    unbuffered.decide(probe("allowed"), purpose="allowed")
    assert capped.budget.reserved_usd == pytest.approx(unbuffered.budget.reserved_usd)
    assert capped.budget.reserved_usd > 0


def test_provider_usage_lists_purposes_sorted_whatever_the_call_order() -> None:
    resolver = DynamicPacketResolver(decisions(open_transport()))
    for topic, purpose in (("a", "zeta_probe"), ("b", "alpha_probe"), ("c", "mid_probe"),
                           ("d", "alpha_probe")):
        resolver.decisions.decide(probe(topic), purpose=purpose)
    assert [r.purpose for r in resolver.decisions.budget.receipts] == [
        "zeta_probe", "alpha_probe", "mid_probe", "alpha_probe"]
    usage = resolver.provider_usage()
    assert list(usage["by_purpose"]) == ["alpha_probe", "mid_probe", "zeta_probe"]
    assert [bucket["calls"] for bucket in usage["by_purpose"].values()] == [2, 1, 1]
    assert (usage["calls"], usage["known_cost_usd"]) == (4, pytest.approx(0.0004))
    assert list(resolver.provider_usage(2)["by_purpose"]) == ["alpha_probe", "mid_probe"]


class NearScope(ByContent):
    """ByContent whose applicant-current source scope (``u<i>``) is 0.94, just under the
    copy gate: every identity copy then needs its own clarification decision in the gate
    pass (``applicant_current_identity``, approved at 1.0)."""

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        payload = json.loads(super().__call__(url, headers, body, timeout).body)
        for name, answer in payload["answers"].items():
            if name[0] == "u" and name[1:].isdigit():
                answer["probabilities"] = {key: {"APPLICANT_CURRENT": 0.94,
                                                 "HISTORICAL_OR_CONTEXTUAL": 0.06}.get(key, 0.0)
                                           for key in answer["probabilities"]}
        return HttpResponse(200, {}, json.dumps(payload).encode())


def address_fields() -> list[ApplicationField]:
    return [ApplicationField(id=field_id, selector=f"#{field_id}", label=label,
                             semantic_type=semantic, control_type=ControlType.TEXT, required=True)
            for field_id, label, semantic in (("city", "City", SemanticType.CITY),
                                              ("state", "State", SemanticType.STATE),
                                              ("zip", "ZIP code", SemanticType.ZIP))]


def test_identity_clarifications_overlap_in_the_gate_pass_and_match_a_sequential_run(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    def delay(request: dict[str, Any]) -> float:
        # The first field's clarification is the slowest: the others finish first.
        return 0.25 if request["state"].get("target_field") == "f0" else 0.1

    provider = Overlapping(NearScope(), delay=delay)
    packet, resolver = resolve_concurrently(provider, fictional_candidate, mock_job,
                                            address_fields(), max_concurrency=3)
    one_at_a_time = Overlapping(NearScope())
    sequential, reference = resolve_concurrently(one_at_a_time, fictional_candidate, mock_job,
                                                 address_fields(), max_concurrency=1)
    assert 2 <= provider.peak <= 3 and one_at_a_time.peak == 1
    assert provider.calls == one_at_a_time.calls == 3
    assert packet.is_complete
    assert [(a.field_id, a.value.text, a.provenance.source) for a in packet.answers] == [
        ("city", "Springfield", AnswerSource.PROFILE_IDENTITY),
        ("state", "OR", AnswerSource.PROFILE_IDENTITY),
        ("zip", "97477", AnswerSource.PROFILE_IDENTITY)]
    clarifications = stage_traces(resolver, "identity_source_clarification")
    assert [(t["field_id"], t["status"]) for t in clarifications] == [
        ("city", "APPROVED"), ("state", "APPROVED"), ("zip", "APPROVED")]
    assert [r.purpose for r in resolver.decisions.budget.receipts] == [
        "full_form_routes", *["identity_source_clarification"] * 3]
    assert packet_view(packet) == packet_view(sequential)
    assert resolver.narrative_traces == reference.narrative_traces
    assert receipt_view(resolver) == receipt_view(reference)


def test_a_cancelled_pass_still_records_the_receipt_of_a_call_that_finishes_after_it(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    import threading

    gate = threading.Event()
    provider = Overlapping(ByContent(), gate=gate)
    router = AIFormRouter(decisions(provider))
    f = router.annotate(ApplicationForm(url="https://example.test/apply",
                                        fields=stored_answer_fields()[:1]),
                        document_id="cancelled-pass")
    ctx = context(f, fictional_candidate, mock_job)
    resolver = DynamicPacketResolver(router.decisions, router=router)

    async def cancel_while_with_the_provider() -> None:
        task = asyncio.create_task(resolver.resolve(ctx))
        if not await asyncio.to_thread(wait_until, lambda: provider.in_flight == 1):
            pytest.fail("the option-equivalence call never started")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()  # the call, already made and counted, now finishes in its worker thread

    asyncio.run(cancel_while_with_the_provider())  # returns once that worker thread is done
    budget = router.decisions.budget
    if (budget.calls, provider.calls, provider.in_flight) != (2, 1, 0):
        pytest.fail("expected the classification and one finished option-equivalence call")
    # Every call made has its receipt, as when the passes ran in one worker thread.
    assert [r.purpose for r in budget.receipts] == ["full_form_routes", "option_equivalence"]
    assert resolver.provider_usage()["calls"] == budget.calls
