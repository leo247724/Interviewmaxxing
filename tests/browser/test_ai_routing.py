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
    assert not REUSABLE_TYPES & {SemanticType.CONSENT, SemanticType.ATTESTATION,
                                 SemanticType.SALARY_EXPECTATION}
    assert SemanticType.CUSTOM_BOOLEAN in UNTYPED_REUSE_TYPES
    assert SemanticType.CUSTOM_LONG_TEXT not in UNTYPED_REUSE_TYPES
