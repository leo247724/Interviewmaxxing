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
from interviewmaxxing_browser.ai.classification import PROMPT_VERSION, FieldRoute
from interviewmaxxing_browser.ai.providers import NarrativeDraft, NarrativeWriter
from interviewmaxxing_browser.ai.routing import CHOICE_PROMPT_VERSION
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
    # The consent field also gets the round-7 statement decision over the saved consent
    # (answered NONE here); it sends statement definitions, never candidate facts. The
    # scripted choices are consumed in order: the unknown field's type, then the statement.
    p = Provider(["CUSTOM_TEXT", "NONE"])
    resolver = DynamicPacketResolver(decisions(p))
    for semantic in (SemanticType.UNKNOWN, SemanticType.CONSENT, SemanticType.WORK_AUTHORIZATION):
        packet = asyncio.run(resolver.resolve(context(form(semantic=semantic), fictional_candidate, mock_job)))
        assert not packet.answers
        assert not packet.is_complete
    assert len(p.requests) == 4
    assert [list(r["questions"]) for r in p.requests].count(["statement"]) == 1
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
    # Round 6 retries a malformed decision once; an unoffered choice both times stays UNKNOWN.
    p = Provider(["EXECUTE_JAVASCRIPT", "EXECUTE_JAVASCRIPT"])
    d = decisions(p)
    result = AIFormRouter(d).annotate(form(semantic=SemanticType.UNKNOWN), document_id="d")
    assert result.fields[0].semantic_type is SemanticType.UNKNOWN
    assert [r.status for r in d.budget.receipts] == ["MALFORMED_RESPONSE", "MALFORMED_RESPONSE"]


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
    # Round 7 (L2): min(Jev's confidence 0.97, 1 - NOT_SOURCE 0.98), not 1 - NOT_SOURCE alone.
    assert answer.confidence == pytest.approx(0.97)
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
    # Round 7 (B): the saved answer's truth must carry over; unit, scope or extra clause is NONE.
    assert "is necessarily a truthful answer to observed_question" in criteria["q0"]
    assert "same scope, unit and timeframe" in criteria["q0"]
    assert "an extra clause (base and/or OTE)" in criteria["NONE"]
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


# Round 7 (A): with one same-type candidate the gate is 0.90 / 0.85 (type-anchored).
@pytest.mark.parametrize("probability,confidence", [(0.89, 0.97), (0.98, 0.84)])
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
    from interviewmaxxing_candidate.simple_answers import _REUSABLE_QUESTIONS, STATEMENT_KEYS

    typed = {semantic for key, (semantic, _) in _REUSABLE_QUESTIONS.items()
             if semantic is not None and key not in STATEMENT_KEYS}
    assert typed <= REUSABLE_TYPES
    # Round 7: consent/attestation statements are reused only by full coverage (_statement).
    assert {_REUSABLE_QUESTIONS[key][0] for key in STATEMENT_KEYS} <= {
        SemanticType.CONSENT, SemanticType.ATTESTATION}
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
    assert request["state"]["screener_version"] == "experience-screener-v2"
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
    assert classification["state"]["version"] == PROMPT_VERSION
    scopes = classification["questions"]["u0"]["criteria"]
    assert "whether they live in a named country or in one of listed states" in scopes["APPLICANT_CURRENT"]
    assert scopes["EXPLICIT_ANSWER"].endswith("or where the applicant currently lives.")


@pytest.mark.parametrize("split,control", [
    # Not a residence reading: the yes/no shape outweighs the residence types, so WP10's
    # pooled gate (895e7b2) does not pool CUSTOM_BOOLEAN with them.
    ({"STATE": 0.40, "CUSTOM_BOOLEAN": 0.60}, ControlType.SELECT),
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


# --- round 6: route mass, relocation, typed candidates, city lookups, select-all referral ------

class RouteSplit(ChoiceProvider):
    """ChoiceProvider whose classification answers use explicit shares: ``route`` (r<i>),
    ``scope`` (u<i>) and ``semantic`` (s<i>); each choice is its map's top key."""

    def __init__(self, picks: dict[str, tuple[str, float]], *, route: dict[str, float] | None = None,
                 scope: dict[str, float] | None = None,
                 semantic: dict[str, float] | None = None) -> None:
        super().__init__(dict(picks))
        self.shares = {"r": route, "u": scope, "s": semantic}

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        response = super().__call__(url, headers, body, timeout)
        payload = json.loads(response.body)
        for name, answer in payload["answers"].items():
            shares = self.shares.get(name[0]) if name[1:].isdigit() else None
            if shares:
                answer.update(choice=max(shares, key=lambda key: shares[key]), confidence=0.97,
                              probabilities={key: shares.get(key, 0.0) for key in answer["probabilities"]})
        return HttpResponse(200, {}, json.dumps(payload).encode())


LIVE_ROUTE = {"COPY_KNOWN": 0.94, "HUMAN_INPUT": 0.06}
LIVE_SCOPE = {"APPLICANT_CURRENT": 0.99, "HISTORICAL_OR_CONTEXTUAL": 0.01}


@pytest.mark.parametrize("region,city,pick,expected", [
    ("OR", "Springfield", "o0", "Yes"),
    ("IN", "Indianapolis", "o1", "No"),
])
def test_the_live_split_route_label_no_longer_hides_the_rippling_residence_question(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    region: str, city: str, pick: str, expected: str,
) -> None:
    provider = RouteSplit({"residence": (pick, 0.99)}, route=LIVE_ROUTE, scope=LIVE_SCOPE,
                          semantic={"STATE": 0.98, "LOCATION": 0.02})
    candidate = with_address(fictional_candidate, region=region, city=city)
    packet, ctx, resolver = resolve_choice(provider, candidate, mock_job, rippling_field())
    assert resolver.router is not None
    report = resolver.router.report_for(ctx.form)
    assert report is not None and report.field("residence").route is FieldRoute.AMBIGUOUS
    assert packet.is_complete
    [answer] = packet.answers
    assert answer.value.label == expected
    assert answer.provenance.source is AnswerSource.PROFILE_IDENTITY
    [trace] = stage_traces(resolver, "residence_screener")
    assert trace["status"] == "ANSWERED" and trace["state_list_member"] is (expected == "Yes")


@pytest.mark.parametrize("route,asked", [
    ({"HUMAN_INPUT": 0.97, "COPY_KNOWN": 0.03}, True),  # a HUMAN_INPUT label with no other mass
    ({"COPY_KNOWN": 0.90, "WRITER": 0.05, "HUMAN_INPUT": 0.05}, False),  # WRITER mass 0.05
    ({"COPY_KNOWN": 0.93, "UNSUPPORTED": 0.02, "HUMAN_INPUT": 0.05}, False),
])
def test_the_residence_screener_follows_route_mass_not_the_label(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    route: dict[str, float], asked: bool,
) -> None:
    provider = RouteSplit({"residence": ("o0", 0.99)}, route=route, scope=LIVE_SCOPE,
                          semantic={"STATE": 0.98, "LOCATION": 0.02})
    packet, _, _ = resolve_choice(provider, fictional_candidate, mock_job, rippling_field())
    assert bool(provider.asked("residence")) is asked
    assert packet.is_complete is asked
    if not asked:
        [missing] = packet.missing_inputs
        assert missing.prompt.startswith("Required:")  # the factual hold stays


def test_a_residence_question_whose_scope_misses_its_own_gate_is_not_screened_on_a_split_label(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = RouteSplit({"residence": ("o0", 0.99)}, route=LIVE_ROUTE,
                          scope={"APPLICANT_CURRENT": 0.93, "HISTORICAL_OR_CONTEXTUAL": 0.07},
                          semantic={"STATE": 0.98, "LOCATION": 0.02})
    packet, _, _ = resolve_choice(provider, fictional_candidate, mock_job, rippling_field())
    assert not provider.asked("residence") and not packet.is_complete


RELOCATE_STATES = "Do you currently reside in or will you be relocating to any of the following states?"
AUSTIN_RELOCATION = ("This position is located in Austin, Texas. Are you currently located in "
                     "Austin or plan to relocate to Austin?")
WILLING = global_answer("sa.relocate", "Are you willing to relocate?", "Yes",
                        semantic=SemanticType.RELOCATION)


def relocation_field(label: str = RELOCATE_STATES, *options: str, help_text: str | None = "AZ, CA, OR, TX, WA",
                     control: ControlType = ControlType.SELECT) -> ApplicationField:
    field = choice_field(label, SemanticType.RELOCATION, *(options or YES_NO), control=control,
                         field_id="relocate")
    return field.model_copy(update={"help_text": help_text})


@pytest.mark.parametrize("region,city,pick,saved,expected,source", [
    ("OR", "Springfield", ("o0", 0.99), False, "Yes", AnswerSource.PROFILE_IDENTITY),  # lives there
    ("IN", "Indianapolis", ("UNKNOWN", 0.99), True, "Yes", AnswerSource.SAVED_ANSWER),  # willing
    ("IN", "Indianapolis", ("UNKNOWN", 0.99), False, None, None),  # nothing settles it: held
    ("OR", "Springfield", ("o1", 0.99), False, None, None),  # "No" from an address: never
    ("IN", "Indianapolis", ("o0", 0.99), False, None, None),  # "Yes" but not a listed state
])
def test_a_relocation_question_naming_states_uses_the_address_then_the_saved_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, region: str, city: str,
    pick: tuple[str, float], saved: bool, expected: str | None, source: AnswerSource | None,
) -> None:
    provider = RouteSplit({"relocation": pick})
    candidate = with_address(fictional_candidate, region=region, city=city)
    if saved:
        candidate = with_saved(candidate, WILLING)
    packet, _, resolver = resolve_choice(provider, candidate, mock_job, relocation_field())
    [request] = provider.asked("relocation")
    assert request["state"]["applicant_address"]["region"] == region
    assert set(request["questions"]["relocation"]["criteria"]) == {"o0", "o1", "UNKNOWN", "NOT_PLACE"}
    assert not provider.asked("wording")  # never the generic reworded path
    if expected is None:
        assert packet.answers == [] and not packet.is_complete
        return
    [answer] = packet.answers
    assert (answer.value.label, answer.provenance.source) == (expected, source)
    if source is AnswerSource.SAVED_ANSWER:
        assert answer.provenance.reference_ids == ["sa.relocate"]
        assert [t["status"] for t in stage_traces(resolver, "relocation_default")] == ["MAPPED"]


@pytest.mark.parametrize("pick,expected", [("o1", "Oregon"), ("o2", None)])
def test_a_relocation_select_of_states_picks_only_the_applicants_own_state(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, pick: str, expected: str | None,
) -> None:
    provider = RouteSplit({"relocation": (pick, 0.99)})
    field = relocation_field(RELOCATE_STATES, "Arizona", "Oregon", "Texas", "None of these",
                             help_text=None)
    packet, _, resolver = resolve_choice(provider, fictional_candidate, mock_job, field)
    if expected is None:
        assert packet.answers == []
        assert stage_traces(resolver, "relocation_screener")[0]["status"] == "ADDRESS_MISMATCH"
        return
    [answer] = packet.answers
    assert answer.value.label == expected and answer.provenance.source is AnswerSource.PROFILE_IDENTITY


def test_the_live_austin_relocation_radio_is_yes_for_an_applicant_in_austin(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    # The source scope reads the question as a preference (EXPLICIT_ANSWER); the address
    # decision and the state check are the gate.
    provider = RouteSplit({"relocation": ("o0", 0.99)},
                          scope={"EXPLICIT_ANSWER": 0.8, "APPLICANT_CURRENT": 0.2})
    candidate = with_address(fictional_candidate, city="Austin", region="TX")
    packet, _, _ = resolve_choice(provider, candidate, mock_job,
                                  relocation_field(AUSTIN_RELOCATION, help_text=None,
                                                   control=ControlType.RADIO))
    [answer] = packet.answers
    assert answer.value.label == "Yes" and answer.provenance.source is AnswerSource.PROFILE_IDENTITY


def test_a_relocation_question_about_another_person_is_not_answered_from_the_address(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = RouteSplit({"relocation": ("o0", 0.99)},
                          scope={"APPLICANT_CURRENT": 0.98, "OTHER_PERSON_OR_ENTITY": 0.02})
    packet, _, _ = resolve_choice(provider, fictional_candidate, mock_job, relocation_field())
    assert not provider.asked("relocation") and packet.answers == []


class WordingPick(ChoiceProvider):
    """ChoiceProvider whose ``wording`` pick is the saved question containing ``contains``."""

    def __init__(self, contains: str, picks: dict[str, tuple[str, float]] | None = None,
                 **kwargs: Any) -> None:
        super().__init__(dict(picks or {}), **kwargs)
        self.contains = contains

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        if "wording" in request["questions"]:
            saved = request["state"]["saved_questions"]
            self.picks["wording"] = (next(key for key, item in saved.items()
                                          if self.contains in item["question"]), 0.98)
        return super().__call__(url, headers, body, timeout)


UNTYPED_NOISE = (
    global_answer("sa.age", "Are you above the age of 18?", "Yes"),
    global_answer("sa.edu_start", "Education start date", "2014-09"),
    global_answer("sa.referred", "Were you referred to this position by a current employee?", "No"),
    global_answer("sa.interviewed", "Have you previously interviewed with this company?", "Yes"),
    global_answer("sa.english", "What is your level of proficiency in English?", "Fluent"),
)


def text_field(label: str, semantic: SemanticType) -> ApplicationField:
    return ApplicationField(id="answer", selector="#answer", label=label, semantic_type=semantic,
                            control_type=ControlType.TEXT, required=True)


@pytest.mark.parametrize("label", [
    "What salary are you looking for in this role?",
    "How much would you like to earn in this position (base and/or OTE, if applicable)?",
])
def test_a_typed_salary_question_is_offered_only_the_saved_salary_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
) -> None:
    # Round 10 moved base, annual, expected and target wordings to a deterministic rule
    # (the round-10 section below); these wordings still take the wording decision.
    salary = global_answer("sa.salary", "What is your desired salary?", "USD 95,000 per year",
                           semantic=SemanticType.SALARY_EXPECTATION)
    provider = WordingPick("desired salary")
    candidate = with_saved(fictional_candidate, salary, *UNTYPED_NOISE)
    packet, _, resolver = resolve_choice(provider, candidate, mock_job,
                                         text_field(label, SemanticType.SALARY_EXPECTATION))
    assert not stage_traces(resolver, "salary_wording")
    [wording] = provider.asked("wording")
    assert list(wording["state"]["saved_questions"]) == ["q0"]  # the untyped answers stay out
    assert wording["state"]["prompt_version"] == CHOICE_PROMPT_VERSION
    instructions = wording["questions"]["wording"]["instructions"]
    # Round 7 (B): an extra clause ("base and/or OTE") is for Jev to judge as NONE.
    assert "adds a clause the saved answer does not cover (for example base and/or OTE)" in instructions
    [answer] = packet.answers
    assert answer.value.text == "USD 95,000 per year"
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, ["sa.salary"])


START_OPTIONS = ("ASAP", "One week after offer acceptance", "Two weeks after offer acceptance",
                 "Three weeks after offer acceptance", "Other")


def test_a_saved_start_date_maps_onto_the_live_start_date_select(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    start = global_answer("sa.start", "What is your earliest start date?",
                          "Two weeks after an offer is accepted", semantic=SemanticType.START_DATE)
    provider = WordingPick("earliest start date", {"equivalent_0": ("o2", 0.98)})
    field = choice_field("Earliest Start Date?", SemanticType.START_DATE, *START_OPTIONS,
                         control=ControlType.SELECT)
    packet, _, _ = resolve_choice(provider, with_saved(fictional_candidate, start, *UNTYPED_NOISE),
                                  mock_job, field)
    [wording] = provider.asked("wording")
    assert list(wording["state"]["saved_questions"]) == ["q0"]
    [answer] = packet.answers
    assert answer.value.label == "Two weeks after offer acceptance"
    assert answer.provenance.reference_ids == ["sa.start"]


def test_a_typed_field_without_a_same_type_answer_is_offered_the_untyped_ones(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"wording": ("NONE", 0.99)})
    field = choice_field("Earliest Start Date?", SemanticType.START_DATE, *START_OPTIONS,
                         control=ControlType.SELECT)
    resolve_choice(provider, with_saved(fictional_candidate, *UNTYPED_NOISE), mock_job, field)
    [wording] = provider.asked("wording")
    offered = {item["question"] for item in wording["state"]["saved_questions"].values()}
    assert offered == {answer.question for answer in UNTYPED_NOISE}


TRAVEL = ("This role may require occasional business travel. What level of travel are you "
          "comfortable with? Select one")
TRAVEL_OPTIONS = ("No travel (0%)", "Up to 10%", "11-20%", "21-30%", "More than 30%")


def test_an_untyped_saved_travel_answer_maps_onto_the_live_travel_select(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    travel = global_answer("sa.travel", "How much are you willing to travel for work?",
                           "Up to 10% of the time")
    provider = WordingPick("travel", {"equivalent_0": ("o1", 0.98)})
    field = choice_field(TRAVEL, SemanticType.CUSTOM_SELECT, *TRAVEL_OPTIONS, control=ControlType.SELECT)
    packet, _, _ = resolve_choice(provider, with_saved(fictional_candidate, travel, *UNTYPED_NOISE),
                                  mock_job, field)
    [answer] = packet.answers
    assert answer.value.label == "Up to 10%"
    assert answer.provenance.reference_ids == ["sa.travel"]


def test_an_untyped_saved_employment_answer_maps_onto_a_question_naming_the_employer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    employed = global_answer("sa.employed", "Have you previously been employed by this company?", "No")
    provider = WordingPick("employed by this company")
    field = choice_field("Have you been employed by Upstart before?", SemanticType.CUSTOM_SELECT,
                         *YES_NO)
    packet, _, _ = resolve_choice(provider, with_saved(fictional_candidate, employed, *UNTYPED_NOISE),
                                  mock_job, field)
    [wording] = provider.asked("wording")
    assert "'This company' in a saved question means" in wording["questions"]["wording"]["instructions"]
    [answer] = packet.answers
    assert answer.value.label == "No" and answer.provenance.reference_ids == ["sa.employed"]


class NamedLookup(ChoiceProvider):
    """ChoiceProvider whose ``lookup`` pick is the suggestion containing ``name`` (else NONE)."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        if "lookup" in request["questions"]:
            suggestions = request["state"]["suggestions"]
            self.picks["lookup"] = (next((key for key, label in suggestions.items()
                                          if self.name in label), "NONE"), 0.99)
        return super().__call__(url, headers, body, timeout)


def city_lookup(candidate: CandidateProfile, job: JobRecord, *,
                semantic: SemanticType = SemanticType.CITY) -> PacketContext:
    return context(ApplicationForm(url="https://example.test/apply", fields=[ApplicationField(
        id="city", selector="#city", label="City", semantic_type=semantic,
        control_type=ControlType.TYPEAHEAD, required=True)]), candidate, job)


@pytest.mark.parametrize("suggestions,expected", [
    (["Paris, France", "Paris, Texas, United States"], "Paris, Texas, United States"),
    (["Paris, France", "Paris, Ontario, Canada"], None),  # none in the applicant's region
])
def test_a_city_lookup_decision_sees_the_applicants_region_and_country(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    suggestions: list[str], expected: str | None,
) -> None:
    provider = NamedLookup("Texas")
    resolver = DynamicPacketResolver(decisions(provider))
    ctx = city_lookup(with_address(fictional_candidate, city="Paris", region="TX"), mock_job)
    chosen = asyncio.run(resolver.choose_suggestion(ctx, ctx.form.fields[0], "Paris, TX", suggestions))
    assert chosen == expected
    [request] = provider.asked("lookup")
    assert request["state"]["applicant_address"] == {"city": "Paris", "region": "TX",
                                                     "country": "United States"}
    criteria = request["questions"]["lookup"]["criteria"]
    assert all("applicant's own region and country" in criteria[key] for key in criteria if key != "NONE")
    assert "same-named place elsewhere is NONE" in request["questions"]["lookup"]["instructions"]


def test_a_lookup_that_is_not_a_place_gets_no_address(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = NamedLookup("Fictional State")
    resolver = DynamicPacketResolver(decisions(provider))
    ctx = city_lookup(fictional_candidate, mock_job, semantic=SemanticType.UNIVERSITY)
    asyncio.run(resolver.choose_suggestion(ctx, ctx.form.fields[0], "Fictional State",
                                           ["Fictional State University", "Fictional State"]))
    [request] = provider.asked("lookup")
    assert "applicant_address" not in request["state"]


REFERRAL_OPTIONS = ("LinkedIn", "Indeed", "Glassdoor", "Company careers page", "Employee referral",
                    "Recruiter outreach", "Conference", "Meetup", "Podcast", "Newsletter",
                    "University career fair", "Built In", "AngelList", "Twitter", "Friend", "Other")


def test_a_select_all_referral_question_gets_exactly_one_option(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    careers = f"careers_o{REFERRAL_OPTIONS.index('Company careers page')}"
    provider = ChoiceProvider({"referral": (careers, 0.98)})
    field = choice_field("How did you learn about us? Select ALL that apply.",
                         SemanticType.REFERRAL_SOURCE, *REFERRAL_OPTIONS,
                         control=ControlType.CHECKBOX_GROUP)
    packet, _, _ = resolve_choice(provider, with_referral(fictional_candidate), mock_job, field)
    [answer] = packet.answers
    assert [choice.label for choice in answer.value.choices] == ["Company careers page"]
    assert "referral policy rule 1" in (answer.provenance.note or "")


# --- round 7 (A, B, D): type-anchored gate, truthful-answer criterion, confirmed untyped picks --

LIVE_SPONSORSHIP = ("Will you now or at any time in the future require employer sponsorship for "
                    "employment visa status?")


class WordingConfidence(ChoiceProvider):
    """ChoiceProvider whose ``wording`` decision alone has confidence ``wording_confidence``."""

    def __init__(self, picks: dict[str, tuple[str, float]], *, wording_confidence: float) -> None:
        super().__init__(dict(picks))
        self.wording_confidence = wording_confidence

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        response = super().__call__(url, headers, body, timeout)
        payload = json.loads(response.body)
        if "wording" in payload["answers"]:
            payload["answers"]["wording"]["confidence"] = self.wording_confidence
        return HttpResponse(200, {}, json.dumps(payload).encode())


@pytest.mark.parametrize("probability,confidence,answered", [
    (0.94, 0.97, True),   # the live start-date score ("Earliest Start Date?")
    (0.90, 0.85, True),   # exactly at the type-anchored gate
    (0.89, 0.97, False),
    (0.74, 0.97, False),
    (0.94, 0.84, False),
])
def test_one_same_type_candidate_is_accepted_at_the_type_anchored_gate(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    probability: float, confidence: float, answered: bool,
) -> None:
    # Round 9 (H2): only the low-stakes types are anchored; a start date is one of them.
    start = global_answer("sa.start", "What is your earliest start date?", "2026-10-15",
                          semantic=SemanticType.START_DATE)
    provider = WordingConfidence({"wording": ("q0", probability)}, wording_confidence=confidence)
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, start), mock_job,
                                         text_field("Earliest Start Date?", SemanticType.START_DATE))
    [trace] = stage_traces(resolver, "question_equivalence")
    assert (trace["candidate_count"], trace["gate"]) == (1, "type_anchored")
    assert trace["status"] == ("MAPPED" if answered else "BELOW_GATE")
    assert bool(packet.answers) is answered
    assert "question_confirmation" not in purposes(resolver) and len(provider.asked("wording")) == 1


def test_several_same_type_candidates_keep_the_standard_gate(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    other = global_answer("sa.sponsorship_other", "Do you need an employer to sponsor a work visa?",
                          "Yes", semantic=SemanticType.SPONSORSHIP)
    provider = ChoiceProvider({"wording": {"q0": 0.94, "q1": 0.05, "NONE": 0.01}})
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, other), mock_job,
                                         sponsorship_field(LIVE_SPONSORSHIP))
    [trace] = stage_traces(resolver, "question_equivalence")
    assert (trace["gate"], trace["status"]) == ("standard", "BELOW_GATE")
    assert packet.answers == []


class ConfirmingProvider(WordingPick):
    """``WordingPick`` whose pick is split (``pick`` probability) and whose single-candidate
    confirmation answers ``confirm`` (choice, probability)."""

    def __init__(self, contains: str, *, pick: float, confirm: tuple[str, float],
                 picks: dict[str, tuple[str, float]] | None = None) -> None:
        super().__init__(contains, picks)
        self.pick, self.confirm = pick, confirm

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        if "wording" in request["questions"]:
            saved = request["state"]["saved_questions"]
            if len(saved) == 1:  # the confirmation
                self.picks["wording"] = self.confirm
                return ChoiceProvider.__call__(self, url, headers, body, timeout)
            key = next(k for k, item in saved.items() if self.contains in item["question"])
            self.picks["wording"] = (key, self.pick)
            return ChoiceProvider.__call__(self, url, headers, body, timeout)
        return super().__call__(url, headers, body, timeout)


@pytest.mark.parametrize("confirm,answered", [
    (("q0", 0.95), True),
    (("q0", 0.90), True),
    (("q0", 0.89), False),
    (("NONE", 0.97), False),
])
def test_an_untyped_pick_below_the_gate_is_confirmed_on_its_own(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    confirm: tuple[str, float], answered: bool,
) -> None:
    # A travel pick among 17 untyped answers below the round-10 untyped gate (0.90).
    travel = global_answer("sa.travel", "How much are you willing to travel for work?",
                           "Up to 10% of the time")
    provider = ConfirmingProvider("travel", pick=0.88, confirm=confirm,
                                  picks={"equivalent_0": ("o1", 0.98)})
    field = choice_field(TRAVEL, SemanticType.CUSTOM_SELECT, *TRAVEL_OPTIONS, control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, travel, *UNTYPED_NOISE),
                                         mock_job, field)
    confirmations = [r for r in provider.asked("wording") if len(r["state"]["saved_questions"]) == 1]
    [confirmation] = confirmations
    assert confirmation["state"]["saved_questions"]["q0"]["question"] == travel.question
    [trace] = stage_traces(resolver, "question_equivalence")
    assert trace["gate"] == "confirmed" and trace["confirmation"]["choice"] == confirm[0]
    assert trace["status"] == ("MAPPED" if answered else "BELOW_GATE")
    assert [a.value.label for a in packet.answers] == (["Up to 10%"] if answered else [])


def test_an_untyped_pick_that_passes_the_standard_gate_needs_no_confirmation(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    travel = global_answer("sa.travel", "How much are you willing to travel for work?",
                           "Up to 10% of the time")
    provider = ConfirmingProvider("travel", pick=0.97, confirm=("NONE", 0.99),
                                  picks={"equivalent_0": ("o1", 0.98)})
    field = choice_field(TRAVEL, SemanticType.CUSTOM_SELECT, *TRAVEL_OPTIONS, control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, travel, *UNTYPED_NOISE),
                                         mock_job, field)
    assert len(provider.asked("wording")) == 1
    [trace] = stage_traces(resolver, "question_equivalence")
    assert (trace["gate"], trace["status"]) == ("untyped", "MAPPED")  # round 10: 0.90 / 0.85
    assert [a.value.label for a in packet.answers] == ["Up to 10%"]


# --- round 7 (items 1, 3, 4): statements, salary period, typed imports --------------------------

PRIVACY = global_answer("sa.privacy_notice", "I have read and understand the employer's applicant "
                        "privacy notice and data processing terms.", "Yes",
                        semantic=SemanticType.CONSENT)
CERTIFY = global_answer("sa.certify", "The information I provide in this application is true, "
                        "complete and accurate.", "Yes", semantic=SemanticType.ATTESTATION)
CONTACT = global_answer("sa.contact", "The employer may contact me about this application.",
                        "Yes", semantic=SemanticType.CONSENT)
REFERENCES = global_answer("sa.references", "The employer may contact the references I provide.",
                           "Yes", semantic=SemanticType.CONSENT)
VERCEL = ("By submitting my application, I acknowledge that I have read and understand Fictional "
          "Co's Job Applicant Privacy Notice")
GEM = ("I certify that the information I have provided in this application, in my resume, and in "
       "any other materials I have submitted is true, complete, and accurate")
AI_TOOLS = ("I understand and agree that Fictional Co does not permit the use of AI tools or "
            "assistance during the interview process")
KNOWBE4 = ("By submitting this application, I hereby provide consent to Fictional Co to communicate "
           "directly with me, and any employment references I provide")


def with_statements(candidate: CandidateProfile, *statements: SavedAnswer) -> CandidateProfile:
    return with_saved(candidate.model_copy(update={"saved_answers": []}), *statements)


@pytest.mark.parametrize("label,semantic,saved,pick", [
    (VERCEL, SemanticType.CONSENT, PRIVACY, "s0"),
    (GEM, SemanticType.ATTESTATION, CERTIFY, "s0"),
])
def test_a_statement_fully_covered_by_one_saved_statement_reuses_its_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
    semantic: SemanticType, saved: SavedAnswer, pick: str,
) -> None:
    provider = ChoiceProvider({"statement": (pick, 0.98)})
    candidate = with_statements(fictional_candidate, PRIVACY, CERTIFY, CONTACT)
    field = choice_field(label, semantic, "Yes", "No", control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(provider, candidate, mock_job, field)
    [request] = provider.asked("statement")
    # Only saved statements of the field's own type are offered.
    offered = set(request["state"]["saved_statements"].values())
    assert offered == {a.question for a in (PRIVACY, CERTIFY, CONTACT) if a.semantic_type is semantic}
    assert "no further obligation" in request["questions"]["statement"]["criteria"]["s0"]
    [answer] = packet.answers
    assert answer.value.label == "Yes"
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, [saved.id])
    [trace] = stage_traces(resolver, "statement_coverage")
    assert (trace["status"], trace["statement"]) == ("ANSWERED", label)


@pytest.mark.parametrize("label,semantic,pick", [
    (AI_TOOLS, SemanticType.ATTESTATION, ("s0", 0.99)),  # an extra obligation: held before Jev
    (KNOWBE4, SemanticType.CONSENT, ("NONE", 0.97)),  # two obligations, two saved statements
    (VERCEL, SemanticType.CONSENT, ("s0", 0.93)),  # below the gate
])
def test_a_statement_not_fully_covered_keeps_its_explicit_hold(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
    semantic: SemanticType, pick: tuple[str, float],
) -> None:
    provider = ChoiceProvider({"statement": pick})
    candidate = with_statements(fictional_candidate, PRIVACY, CERTIFY, CONTACT, REFERENCES)
    field = choice_field(label, semantic, "Yes", "No", control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(provider, candidate, mock_job, field)
    assert packet.answers == []
    [missing] = packet.missing_inputs
    # The factual hold stays: an uncovered attestation or a consent needing its own answer.
    assert missing.reason in (MissingReason.UNCOVERED_ATTESTATION,
                              MissingReason.EXPLICIT_ANSWER_REQUIRED)
    assert label[:40] in missing.prompt  # the statement is quoted for the person
    [trace] = stage_traces(resolver, "statement_coverage")
    if label == AI_TOOLS:  # round 9 (M6): the blocklist holds it without a call
        assert (trace["status"], trace["obligations"]) == ("ADDED_OBLIGATION", ["ai_tools"])
        assert not provider.asked("statement")
        return
    assert trace["status"] in ("NONE", "BELOW_GATE")


def test_a_statement_on_an_unsupported_control_is_never_read_or_answered(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"statement": ("s0", 0.99)})
    field = ApplicationField(id="answer", selector="#answer",
                             label="Please double-check all the information provided above",
                             semantic_type=SemanticType.ATTESTATION,
                             control_type=ControlType.UNSUPPORTED, required=True)
    packet, _, _ = resolve_choice(provider, with_statements(fictional_candidate, CERTIFY),
                                  mock_job, field)
    assert not provider.asked("statement") and packet.answers == []


def test_a_saved_no_to_a_statement_is_the_persons_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    declined = CERTIFY.model_copy(update={"value": "No"})
    provider = ChoiceProvider({"statement": ("s0", 0.98)})
    field = choice_field(GEM, SemanticType.ATTESTATION, "Yes", "No", control=ControlType.RADIO)
    packet, _, _ = resolve_choice(provider, with_statements(fictional_candidate, declined),
                                  mock_job, field)
    [answer] = packet.answers
    assert answer.value.label == "No"


def salary_form(period_label: str, *options: str) -> tuple[ApplicationField, ApplicationField]:
    salary = text_field("Desired Salary", SemanticType.SALARY_EXPECTATION).model_copy(
        update={"id": "salary", "selector": "#salary"})
    period = choice_field(period_label, SemanticType.SALARY_EXPECTATION,
                          *(options or ("Hourly", "Weekly", "Monthly", "Yearly")),
                          control=ControlType.SELECT, field_id="period")
    return salary, period


@pytest.mark.parametrize("value,label,expected", [
    ("USD 95,000 per year", "", "Yearly"),
    ("$95,000/yr", "Pay period", "Yearly"),
    ("95000 annual", "", "Annual"),
    ("$45/hr", "", "Hourly"),
    ("$8,000 per month", "Frequency", "Monthly"),
    ("95,000", "", None),  # no unit: held
])
def test_a_salary_period_select_is_answered_from_the_unit_in_the_saved_salary(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    value: str, label: str, expected: str | None,
) -> None:
    saved = global_answer("sa.salary", "What is your desired salary?", value,
                          semantic=SemanticType.SALARY_EXPECTATION)
    options = ("Hourly", "Weekly", "Monthly", "Annual") if expected == "Annual" else ()
    salary, period = salary_form(label, *options)
    provider = ChoiceProvider({"wording": ("q0", 0.98)})
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, saved),
                                         mock_job, salary, period)
    chosen = next((a for a in packet.answers if a.field_id == "period"), None)
    [trace] = stage_traces(resolver, "salary_period")
    if expected is None:
        assert chosen is None and trace["status"] == "NO_UNIT"
        return
    assert chosen is not None and chosen.value.label == expected
    assert (chosen.provenance.source, chosen.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, ["sa.salary"])
    assert trace["status"] == "ANSWERED" and "period" not in trace  # no value-derived trace
    # The salary itself keeps its own explicit rule (reworded saved answer here).
    assert next(a for a in packet.answers if a.field_id == "salary").value.text == value


def test_a_custom_typed_period_select_next_to_the_salary_is_answered(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    # Round 8 (live Lovevery): the unlabelled select is custom-typed; it pairs with the salary
    # field in the same section, and core lets the salary answer back a pay-period choice.
    saved = global_answer("sa.salary", "What is your desired salary?", "USD 95,000 per year",
                          semantic=SemanticType.SALARY_EXPECTATION)
    salary, period = salary_form("")
    period = period.model_copy(update={"semantic_type": SemanticType.CUSTOM_SELECT})
    provider = ChoiceProvider({"wording": ("NONE", 0.99)}, semantic="CUSTOM_SELECT")
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, saved),
                                         mock_job, salary, period)
    [trace] = stage_traces(resolver, "salary_period")
    assert trace["status"] == "ANSWERED"
    [answer] = [a for a in packet.answers if a.field_id == "period"]
    assert (answer.value.label, answer.semantic_type) == ("Yearly", SemanticType.CUSTOM_SELECT)
    assert answer.provenance.reference_ids == ["sa.salary"]


def test_an_untyped_answer_superseded_by_a_typed_one_is_not_offered_twice(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    # An import that typed a key leaves the older untyped answer to the same question behind.
    legacy = global_answer("sa.pronouns_old", "What pronouns do you use?", "they/them")
    typed = global_answer("sa.pronouns", "What pronouns do you use?", "they/them",
                          semantic=SemanticType.PRONOUNS)
    provider = ChoiceProvider({"wording": ("NONE", 0.99)})
    field = choice_field("Pronouns (optional)", SemanticType.CUSTOM_SELECT, "she/her", "he/him",
                         "they/them", control=ControlType.SELECT)
    resolve_choice(provider, with_saved(fictional_candidate, legacy, typed, *UNTYPED_NOISE),
                   mock_job, field)
    [wording] = provider.asked("wording")
    offered = [item["question"] for item in wording["state"]["saved_questions"].values()]
    assert "What pronouns do you use?" not in offered  # the typed one supersedes it


def test_a_typed_pronouns_answer_is_reused_by_wording(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    typed = global_answer("sa.pronouns", "What pronouns do you use?", "they/them",
                          semantic=SemanticType.PRONOUNS)
    provider = ChoiceProvider({"wording": ("q0", 0.96)})
    field = choice_field("Pronouns", SemanticType.PRONOUNS, "she/her", "he/him", "they/them",
                         control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, typed), mock_job, field)
    [answer] = packet.answers
    assert answer.value.label == "they/them"
    assert stage_traces(resolver, "question_equivalence")[0]["gate"] == "type_anchored"


# --- round 8: work authorization and sponsorship derived from the stated status -----------------

def with_status(candidate: CandidateProfile, code: str) -> CandidateProfile:
    from interviewmaxxing_core import WORK_AUTHORIZATION_STATUS_QUESTION

    return with_saved(candidate, global_answer("sa.status", WORK_AUTHORIZATION_STATUS_QUESTION, code))


ANY_EMPLOYER = "Are you legally authorized to work in the United States for any employer?"
AUTHORIZED_US = "Are you authorized to work in the US?"
SPONSOR_US = "Will you require sponsorship to work in the US now or in the future?"
PERMANENT_OR_TEMPORARY = "Is your authorization to work in the United States:"
WHERE_YOU_LIVE = ("Your authorization to work in the country where you live. Please choose the "
                  "option that describes your work authorization")


@pytest.mark.parametrize("label,semantic,options,code,expected", [
    (ANY_EMPLOYER, SemanticType.WORK_AUTHORIZATION, YES_NO, "us_citizen", "Yes"),
    (AUTHORIZED_US, SemanticType.WORK_AUTHORIZATION, YES_NO, "us_permanent_resident", "Yes"),
    (AUTHORIZED_US, SemanticType.WORK_AUTHORIZATION, YES_NO, "not_authorized", "No"),
    (SPONSOR_US, SemanticType.SPONSORSHIP, YES_NO, "us_citizen", "No"),
    (PERMANENT_OR_TEMPORARY, SemanticType.WORK_AUTHORIZATION,
     ("Permanent", "Temporary and subject to expiration"), "us_permanent_resident", "Permanent"),
])
def test_the_obvious_status_pairs_are_answered_from_the_table_without_a_call(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
    semantic: SemanticType, options: tuple[str, ...], code: str, expected: str,
) -> None:
    provider = ChoiceProvider()
    field = choice_field(label, semantic, *options, control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(provider, with_status(fictional_candidate, code),
                                         mock_job, field)
    [answer] = packet.answers
    assert answer.value.label == expected
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, ["sa.status"])
    assert not provider.asked("status") and not provider.asked("wording")
    [trace] = stage_traces(resolver, "status_derivation")
    assert (trace["status"], trace["via"]) == ("ANSWERED", "table")
    assert code not in json.dumps(trace)  # the stated value never reaches a trace


@pytest.mark.parametrize("label,semantic,options,code,pick,expected", [
    (WHERE_YOU_LIVE, SemanticType.WORK_AUTHORIZATION,
     ("I am a citizen", "I am a permanent resident", "I hold a work visa", "Other"),
     "us_citizen", "o0", "I am a citizen"),
    (SPONSOR_US, SemanticType.SPONSORSHIP, YES_NO, "h1b", "o0", "Yes"),  # not in the table
    (AUTHORIZED_US, SemanticType.WORK_AUTHORIZATION, YES_NO, "ead_opt", "o0", "Yes"),
])
def test_other_status_questions_take_one_truthful_option_decision(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
    semantic: SemanticType, options: tuple[str, ...], code: str, pick: str, expected: str,
) -> None:
    provider = ChoiceProvider({"status": (pick, 0.98)})
    field = choice_field(label, semantic, *options, control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(provider, with_status(fictional_candidate, code),
                                         mock_job, field)
    [request] = provider.asked("status")
    assert request["state"]["status"]["code"] == code
    assert set(request["state"]["stated_answers"]) == {"authorized_to_work_us",
                                                       "requires_visa_sponsorship"}
    assert "UNKNOWN" in request["questions"]["status"]["criteria"]
    [answer] = packet.answers
    assert answer.value.label == expected and answer.provenance.reference_ids == ["sa.status"]
    [trace] = stage_traces(resolver, "status_derivation")
    assert (trace["status"], trace["via"], trace["choice"]) == ("ANSWERED", "jev", pick)


def test_a_question_the_status_does_not_settle_falls_back_and_holds(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"status": ("UNKNOWN", 0.97), "wording": ("NONE", 0.99)})
    field = choice_field("Do you hold an active U.S. security clearance?",
                         SemanticType.WORK_AUTHORIZATION, *YES_NO, control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(provider, with_status(fictional_candidate, "us_citizen"),
                                         mock_job, field)
    assert packet.answers == []
    assert provider.asked("status")  # never the table: clearance is not the status
    [trace] = stage_traces(resolver, "status_derivation")
    assert trace["status"] == "UNKNOWN"
    assert provider.asked("wording")  # the saved answers' wording path is still tried


def test_a_text_yes_no_authorization_question_gets_a_yes_or_no(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider()
    field = text_field(ANY_EMPLOYER, SemanticType.WORK_AUTHORIZATION)
    packet, _, _ = resolve_choice(provider, with_status(fictional_candidate, "us_citizen"),
                                  mock_job, field)
    [answer] = packet.answers
    assert answer.value == TextValue(text="Yes")


def test_without_a_stated_status_the_saved_answers_are_matched_by_wording(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"wording": ("NONE", 0.99)})
    field = choice_field(ANY_EMPLOYER, SemanticType.WORK_AUTHORIZATION, *YES_NO,
                         control=ControlType.SELECT)
    _, _, resolver = resolve_choice(provider, fictional_candidate, mock_job, field)
    assert provider.asked("wording") and not stage_traces(resolver, "status_derivation")


@pytest.mark.parametrize("section,paired", [(["Compensation"], True), (["Availability"], False)])
def test_an_unlabelled_period_select_pairs_only_with_a_salary_in_its_section(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, section: list[str], paired: bool,
) -> None:
    saved = global_answer("sa.salary", "What is your desired salary?", "USD 95,000 per year",
                          semantic=SemanticType.SALARY_EXPECTATION)
    salary, period = salary_form("")
    salary = salary.model_copy(update={"section_context": ["Compensation"]})
    period = period.model_copy(update={"semantic_type": SemanticType.CUSTOM_SELECT,
                                       "section_context": section})
    provider = ChoiceProvider({"wording": ("q0", 0.98)}, semantic="CUSTOM_SELECT")
    packet, _, _ = resolve_choice(provider, with_saved(fictional_candidate, saved), mock_job,
                                  salary, period)
    chosen = [a.value.label for a in packet.answers if a.field_id == "period"]
    assert chosen == (["Yearly"] if paired else [])


# --- round 9: review pass 4 (work authorization derivation, gates, relocation city, statements) ---

from interviewmaxxing_browser.ai.routing import (  # noqa: E402
    TYPE_ANCHORED_TYPES,
    _option_keys,
    _places,
    _status_table,
)
from interviewmaxxing_core import (  # noqa: E402
    SPONSORSHIP_UNSETTLED_STATUSES,
    WORK_AUTHORIZATION_IMPLICATIONS,
    WORK_AUTHORIZATION_STATUS_QUESTION,
    WORK_AUTHORIZATION_STATUSES,
    stated_status,
)
from interviewmaxxing_generation.resolver import saved_value  # noqa: E402

STATUS_PHRASES = ("Work authorization status", "What is your work authorization status?",
                  "What is your current U.S. work authorization?")
"""The stated status's match phrases as the simple-answers import writes them."""
NEW_STATUSES = ("asylee", "refugee", "daca", "tps", "pending_adjustment", "dependent_ead")
BOTH_AT_ONCE = "Are you legally authorized to work in the US? Do you need sponsorship?"


def with_stated_status(candidate: CandidateProfile, code: str) -> CandidateProfile:
    """``with_status`` as the import writes it: the untyped GLOBAL status with its phrases."""
    return with_saved(candidate, SavedAnswer(
        id="sa.status", scope=AnswerScope.GLOBAL, semantic_type=None,
        question=WORK_AUTHORIZATION_STATUS_QUESTION, match_phrases=list(STATUS_PHRASES),
        value=code, confirmed_at="2026-09-02T12:00:00Z"))


def code_free(resolver: DynamicPacketResolver, code: str, stage: str) -> list[dict[str, Any]]:
    """The ``stage`` traces, once no trace of the resolver is found to carry the stated code."""
    assert code not in json.dumps(resolver.narrative_traces)
    return stage_traces(resolver, stage)


def purposes(resolver: DynamicPacketResolver) -> list[str]:
    return [receipt.purpose for receipt in resolver.decisions.budget.receipts]


def status_field(label: str, semantic: SemanticType, *options: str) -> ApplicationField:
    return choice_field(label, semantic, *(options or YES_NO), control=ControlType.SELECT)


def shown(answer: Any) -> str:
    return answer.value.text if isinstance(answer.value, TextValue) else answer.value.label


# H1: one question at a time.

@pytest.mark.parametrize("semantic", [SemanticType.WORK_AUTHORIZATION, SemanticType.SPONSORSHIP])
def test_a_question_asking_authorization_and_sponsorship_at_once_goes_to_jev_and_holds(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, semantic: SemanticType,
) -> None:
    field = status_field(BOTH_AT_ONCE, semantic)
    assert _status_table(field, "us_citizen", _option_keys(field)) is None
    provider = ChoiceProvider({"status": ("UNKNOWN", 0.97)})
    packet, _, resolver = resolve_choice(provider, with_status(fictional_candidate, "us_citizen"),
                                         mock_job, field)
    assert packet.answers == []  # never the table's bare "No" (not authorized) for a citizen
    assert [m.reason for m in packet.missing_inputs] == [MissingReason.EXPLICIT_ANSWER_REQUIRED]
    [request] = provider.asked("status")
    assert request["state"]["question"] == BOTH_AT_ONCE
    instructions = request["questions"]["status"]["instructions"]
    assert "asks two things at once" in instructions
    assert "a bare Yes or No that is true for one part and false for another is UNKNOWN" in instructions
    [trace] = code_free(resolver, "us_citizen", "status_derivation")
    assert (trace["via"], trace["status"]) == ("jev", "UNKNOWN")


@pytest.mark.parametrize("options", [YES_NO, ("Permanent", "Temporary and subject to expiration")])
@pytest.mark.parametrize("question", [
    BOTH_AT_ONCE,
    "Are you authorized to work in the U.S. and will you require visa sponsorship?",
    "Will you require sponsorship, and are you eligible to work in the United States?",
    "Are you permitted to work in the USA, and do you now or will you in the future need a visa sponsor?",
])
def test_the_status_table_never_answers_two_questions_at_once(
    question: str, options: tuple[str, ...],
) -> None:
    for semantic in (SemanticType.WORK_AUTHORIZATION, SemanticType.SPONSORSHIP):
        field = status_field(question, semantic, *options)
        keys = _option_keys(field)
        assert {code: _status_table(field, code, keys) for code in WORK_AUTHORIZATION_STATUSES} == (
            dict.fromkeys(WORK_AUTHORIZATION_STATUSES))


# Found by the round-9 tests: "legally work" and "legal right to work" are authorization
# wording too, so these compound questions never get the table's sponsorship "No".
@pytest.mark.parametrize("question", [
    "Can you legally work in the US? Do you need sponsorship?",
    "Do you have the legal right to work in the United States and will you require sponsorship?",
])
def test_a_compound_legally_work_and_sponsorship_question_is_left_to_jev(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, question: str,
) -> None:
    provider = ChoiceProvider({"status": ("UNKNOWN", 0.97)})
    packet, _, resolver = resolve_choice(provider, with_status(fictional_candidate, "us_citizen"),
        mock_job, status_field(question, SemanticType.WORK_AUTHORIZATION))
    [trace] = code_free(resolver, "us_citizen", "status_derivation")
    assert trace["via"] == "jev" and provider.asked("status")
    assert packet.answers == []  # never the table's "No" for a citizen


# Found by the round-9 tests: any question naming sponsorship beside authorization ("requiring",
# "needed") is Jev's, never the table's bare "Yes".
@pytest.mark.parametrize("question", [
    "Are you authorized to work in the US, or will you be requiring sponsorship?",
    "Are you authorized to work in the U.S.? Is visa sponsorship needed?",
])
def test_a_compound_authorized_and_requiring_sponsorship_question_is_left_to_jev(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, question: str,
) -> None:
    provider = ChoiceProvider({"status": ("UNKNOWN", 0.97)})
    packet, _, resolver = resolve_choice(provider, with_status(fictional_candidate, "us_citizen"),
        mock_job, status_field(question, SemanticType.SPONSORSHIP))
    [trace] = code_free(resolver, "us_citizen", "status_derivation")
    assert trace["via"] == "jev" and provider.asked("status")
    assert packet.answers == []  # never the table's bare "Yes" for a citizen


# H2: the type-anchored gate is for the low-stakes types only.

VETERAN_OPTIONS = ("I am a protected veteran", "I am not a protected veteran", "I don't wish to answer")
STANDARD_GATE = {  # field, its one same-type saved answer (the fixture's own for the first two), value
    "work_authorization": (status_field("Are you currently eligible to work in the United States?",
                                        SemanticType.WORK_AUTHORIZATION), None, "Yes"),
    # Round 10: EEO answers map by type (no wording decision) and base, annual, expected
    # and target salary wordings are deterministic; the wordings here stay on the decision.
    "sponsorship": (status_field(REWORDED_SPONSORSHIP, SemanticType.SPONSORSHIP), None, "No"),
    "salary": (text_field("What salary are you looking for?", SemanticType.SALARY_EXPECTATION),
               global_answer("sa.salary", "What is your desired salary?", "USD 95,000 per year",
                             semantic=SemanticType.SALARY_EXPECTATION), "USD 95,000 per year"),
    "relocation": (status_field("Would you consider relocating for this role?", SemanticType.RELOCATION),
                   WILLING, "Yes"),
}


@pytest.mark.parametrize("probability,mapped", [(0.94, False), (0.96, True)])
@pytest.mark.parametrize("case", list(STANDARD_GATE))
def test_one_same_type_candidate_of_a_high_stakes_type_keeps_the_standard_gate(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, case: str,
    probability: float, mapped: bool,
) -> None:
    field, saved, value = STANDARD_GATE[case]
    candidate = with_saved(fictional_candidate, saved) if saved else fictional_candidate
    provider = ChoiceProvider({"wording": ("q0", probability)})  # confidence 0.97
    packet, _, resolver = resolve_choice(provider, candidate, mock_job, field)
    [trace] = stage_traces(resolver, "question_equivalence")
    assert (trace["candidate_count"], trace["gate"]) == (1, "standard")
    assert trace["confidence"] == pytest.approx(0.97)
    assert trace["status"] == ("MAPPED" if mapped else "BELOW_GATE")
    assert "question_confirmation" not in purposes(resolver)
    assert [shown(a) for a in packet.answers] == ([value] if mapped else [])
    if mapped:
        [answer] = packet.answers
        assert answer.provenance.source is AnswerSource.SAVED_ANSWER
        assert answer.confidence == pytest.approx(0.96)


ANCHORED = {  # field, its one same-type saved answer, value
    SemanticType.REFERRAL_SOURCE: (
        text_field("How did you find out about this role?", SemanticType.REFERRAL_SOURCE),
        REFERRAL_DEFAULT, "Company career page"),
    SemanticType.LOCATION: (
        status_field("Which region do you work from?", SemanticType.LOCATION, *REGIONS),
        global_answer("sa.region", "Which US region are you based in?", "US - West",
                      semantic=SemanticType.LOCATION), "US - West"),
    SemanticType.UNIVERSITY: (
        text_field("Which university did you attend?", SemanticType.UNIVERSITY),
        global_answer("sa.school", "What school did you graduate from?", "Fictional State University",
                      semantic=SemanticType.UNIVERSITY), "Fictional State University"),
    SemanticType.DEGREE: (
        text_field("What is your highest degree?", SemanticType.DEGREE),
        global_answer("sa.degree", "What degree did you earn?", "B.A. Economics",
                      semantic=SemanticType.DEGREE), "B.A. Economics"),
    SemanticType.PRONOUNS: (
        status_field("Pronouns", SemanticType.PRONOUNS, "she/her", "he/him", "they/them"),
        global_answer("sa.pronouns", "What pronouns do you use?", "they/them",
                      semantic=SemanticType.PRONOUNS), "they/them"),
    SemanticType.START_DATE: (
        text_field("Earliest Start Date?", SemanticType.START_DATE),
        global_answer("sa.start", "What is your earliest start date?", "2026-10-15",
                      semantic=SemanticType.START_DATE), "2026-10-15"),
}


@pytest.mark.parametrize("semantic", list(ANCHORED), ids=lambda semantic: semantic.value)
def test_the_low_stakes_types_alone_pass_the_type_anchored_gate(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, semantic: SemanticType,
) -> None:
    assert set(ANCHORED) == TYPE_ANCHORED_TYPES
    field, saved, value = ANCHORED[semantic]
    provider = ChoiceProvider({"wording": ("q0", 0.94)})
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, saved), mock_job, field)
    [trace] = stage_traces(resolver, "question_equivalence")
    assert (trace["candidate_count"], trace["gate"], trace["status"]) == (1, "type_anchored", "MAPPED")
    [answer] = packet.answers
    assert (shown(answer), answer.provenance.reference_ids) == (value, [saved.id])


VETERAN_QUESTION = "Do you identify as a protected veteran?"


@pytest.mark.parametrize("field,semantic,gate,confirmed", [
    (status_field(VETERAN_QUESTION, SemanticType.EEO_VETERAN_STATUS), "CUSTOM_SELECT", "standard", False),
    (text_field("Earliest Start Date?", SemanticType.START_DATE), "CUSTOM_SELECT", "untyped", False),
    (status_field(VETERAN_QUESTION, SemanticType.CUSTOM_SELECT), "CUSTOM_SELECT", "confirmed", True),
    (status_field(VETERAN_QUESTION, SemanticType.CUSTOM_BOOLEAN), "CUSTOM_BOOLEAN", "confirmed", True),
])
def test_only_a_custom_field_gets_the_single_candidate_confirmation(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, field: ApplicationField,
    semantic: str, gate: str, confirmed: bool,
) -> None:
    # The pick (0.88) is below every gate: the explicit veteran type keeps 0.95, the typed
    # start date takes the round-10 untyped gate (0.90) without a confirmation, and only a
    # custom field gets the single-candidate confirmation.
    untyped = (global_answer("sa.veteran_untyped", "Are you a protected veteran?", "No"),
               global_answer("sa.start_untyped", "What is your earliest start date?", "2026-10-15"))
    start = field.semantic_type is SemanticType.START_DATE
    provider = ConfirmingProvider("earliest start date" if start else "protected veteran",
                                  pick=0.88, confirm=("q0", 0.95))
    provider.semantic = semantic
    packet, ctx, resolver = resolve_choice(
        provider, with_saved(fictional_candidate, *untyped, *UNTYPED_NOISE), mock_job, field)
    assert ctx.form.fields[0].semantic_type is field.semantic_type
    [trace] = stage_traces(resolver, "question_equivalence")
    assert trace["candidate_count"] == 7  # only untyped answers are offered
    assert ("question_confirmation" in purposes(resolver)) is confirmed
    assert len(provider.asked("wording")) == (2 if confirmed else 1)
    if confirmed:
        assert (trace["gate"], trace["status"]) == (gate, "MAPPED")
        assert [shown(a) for a in packet.answers] == ["No"]
    else:
        assert (trace["gate"], trace["status"]) == (gate, "BELOW_GATE")
        assert packet.answers == []


# H3: more statuses, their implications, and sponsorship never derived for another visa.

def test_the_status_vocabulary_is_closed_and_read_only_from_the_untyped_status_question() -> None:
    assert set(NEW_STATUSES) <= set(WORK_AUTHORIZATION_STATUSES)
    assert set(WORK_AUTHORIZATION_IMPLICATIONS) == set(WORK_AUTHORIZATION_STATUSES)
    assert set(SPONSORSHIP_UNSETTLED_STATUSES) == {"other_visa"}
    for code, meaning in WORK_AUTHORIZATION_STATUSES.items():
        status = global_answer("sa.status", WORK_AUTHORIZATION_STATUS_QUESTION, code)
        assert stated_status(status) == code
        assert saved_value(status) == meaning != code  # its answer is the meaning, never the code
    assert stated_status(global_answer("sa.status", WORK_AUTHORIZATION_STATUS_QUESTION,
                                       "green_card")) is None
    assert stated_status(global_answer("sa.status", WORK_AUTHORIZATION_STATUS_QUESTION, "us_citizen",
                                       semantic=SemanticType.WORK_AUTHORIZATION)) is None


@pytest.mark.parametrize("code", NEW_STATUSES)
def test_the_new_statuses_are_read_with_their_meaning_and_implications(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, code: str,
) -> None:
    settled = code in ("asylee", "refugee")  # never need sponsorship; the others leave it open
    provider = ChoiceProvider({"status": ("o1", 0.98) if settled else ("UNKNOWN", 0.97)})
    field = status_field(SPONSOR_US, SemanticType.SPONSORSHIP)
    assert _status_table(field, code, _option_keys(field)) is None  # only a citizen or LPR is
    packet, _, resolver = resolve_choice(provider, with_status(fictional_candidate, code),
                                         mock_job, field)
    [request] = provider.asked("status")
    assert request["state"]["status"] == {"code": code, "meaning": WORK_AUTHORIZATION_STATUSES[code],
                                          "implications": WORK_AUTHORIZATION_IMPLICATIONS[code]}
    assert request["state"]["stated_answers"] == {"authorized_to_work_us": "Yes",
                                                  "requires_visa_sponsorship": "No"}
    [trace] = code_free(resolver, code, "status_derivation")
    assert (trace["via"], trace["status"]) == ("jev", "ANSWERED" if settled else "UNKNOWN")
    assert [shown(a) for a in packet.answers] == (["No"] if settled else [])
    if settled:
        assert packet.answers[0].provenance.reference_ids == ["sa.status"]


@pytest.mark.parametrize("label", ["Will you now or in the future require sponsorship?", SPONSOR_US])
def test_opt_needs_sponsorship_in_the_future_and_is_always_asked_of_jev(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
) -> None:
    field = status_field(label, SemanticType.SPONSORSHIP)
    assert _status_table(field, "ead_opt", _option_keys(field)) is None
    provider = ChoiceProvider({"status": ("o0", 0.98)})
    packet, _, resolver = resolve_choice(provider, with_status(fictional_candidate, "ead_opt"),
                                         mock_job, field)
    [request] = provider.asked("status")
    implications = request["state"]["status"]["implications"]
    assert "will need an employer's visa sponsorship (such as H-1B) in the future" in implications
    assert "answered Yes" in implications
    assert ("An F-1 student on OPT or STEM OPT is authorized now but will need an employer's "
            "sponsorship in the future") in request["questions"]["status"]["instructions"]
    [answer] = packet.answers
    assert (answer.value.label, answer.provenance.reference_ids) == ("Yes", ["sa.status"])
    assert answer.provenance.note == "derived from the stated U.S. work authorization status (Jev)"
    [trace] = code_free(resolver, "ead_opt", "status_derivation")
    assert (trace["via"], trace["choice"], trace["status"]) == ("jev", "o0", "ANSWERED")


@pytest.mark.parametrize("label,semantic,wording,expected", [
    (REWORDED_SPONSORSHIP, SemanticType.SPONSORSHIP, ("q0", 0.96), "No"),  # the person's own answer
    (REWORDED_SPONSORSHIP, SemanticType.SPONSORSHIP, ("q0", 0.94), None),  # the standard gate
    (REWORDED_SPONSORSHIP, SemanticType.SPONSORSHIP, ("NONE", 0.99), None),
    (SPONSOR_US, SemanticType.WORK_AUTHORIZATION, ("NONE", 0.99), None),  # sponsorship by wording
])
def test_another_visa_never_derives_a_sponsorship_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
    semantic: SemanticType, wording: tuple[str, float], expected: str | None,
) -> None:
    provider = ChoiceProvider({"status": ("o1", 0.99), "wording": wording})
    packet, _, resolver = resolve_choice(provider, with_status(fictional_candidate, "other_visa"),
                                         mock_job, status_field(label, semantic))
    assert not provider.asked("status") and "status_derivation" not in purposes(resolver)
    [trace] = code_free(resolver, "other_visa", "status_derivation")
    assert trace["status"] == "NOT_SETTLED" and "via" not in trace
    [request] = provider.asked("wording")  # the fallback: the person's own answers by wording
    offered = [item["question"] for item in request["state"]["saved_questions"].values()]
    assert offered == [SPONSORSHIP if semantic is SemanticType.SPONSORSHIP else WORK_AUTH]
    [wording_trace] = code_free(resolver, "other_visa", "question_equivalence")
    assert wording_trace["status"] == ("NONE" if wording[0] == "NONE"
                                       else "MAPPED" if expected else "BELOW_GATE")
    assert wording_trace.get("gate", "standard") == "standard"  # no gate is recorded for NONE
    assert [shown(a) for a in packet.answers] == ([expected] if expected else [])
    if expected:
        assert packet.answers[0].provenance.reference_ids == ["sa.sponsorship"]


def test_another_visa_still_derives_an_authorization_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    field = status_field(AUTHORIZED_US, SemanticType.WORK_AUTHORIZATION)
    assert _status_table(field, "other_visa", _option_keys(field)) is None
    provider = ChoiceProvider({"status": ("o0", 0.98)})
    packet, _, resolver = resolve_choice(provider, with_status(fictional_candidate, "other_visa"),
                                         mock_job, field)
    [request] = provider.asked("status")
    assert request["state"]["status"]["code"] == "other_visa"
    [answer] = packet.answers
    assert (answer.value.label, answer.provenance.reference_ids) == ("Yes", ["sa.status"])
    [trace] = code_free(resolver, "other_visa", "status_derivation")
    assert (trace["via"], trace["status"]) == ("jev", "ANSWERED")


# M2 and M3: the United States as written, alone, and options that name a status.

@pytest.mark.parametrize("label,semantic,pick,expected", [
    (AUTHORIZED_US, SemanticType.WORK_AUTHORIZATION, None, "Yes"),
    ("are you authorized to work in the us?", SemanticType.WORK_AUTHORIZATION, None, "Yes"),
    ("Please let us know: will you need us to sponsor your visa?", SemanticType.SPONSORSHIP,
     ("o1", 0.98), "No"),  # the pronoun is not the country
    ("Are you authorized to work in America?", SemanticType.WORK_AUTHORIZATION, ("o0", 0.98), "Yes"),
    ("Are you authorized to work in the US or Canada?", SemanticType.WORK_AUTHORIZATION,
     ("UNKNOWN", 0.97), None),
    ("Will you need sponsorship to work in the US or anywhere in Latin America?",
     SemanticType.SPONSORSHIP, ("UNKNOWN", 0.97), None),
])
def test_the_table_reads_the_united_states_only_as_written_and_alone(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
    semantic: SemanticType, pick: tuple[str, float] | None, expected: str | None,
) -> None:
    table = pick is None
    field = status_field(label, semantic)
    assert (_status_table(field, "us_citizen", _option_keys(field)) is not None) is table
    provider = ChoiceProvider({"status": pick} if pick else {})
    packet, _, resolver = resolve_choice(provider, with_status(fictional_candidate, "us_citizen"),
                                         mock_job, field)
    assert bool(provider.asked("status")) is not table
    [trace] = code_free(resolver, "us_citizen", "status_derivation")
    assert trace["via"] == ("table" if table else "jev")
    assert [shown(a) for a in packet.answers] == ([expected] if expected else [])


@pytest.mark.parametrize("label,options,code,pick,expected", [
    (PERMANENT_OR_TEMPORARY, ("Permanent resident", "Citizen", "Temporary visa"), "us_citizen",
     ("o1", 0.98), "Citizen"),
    (AUTHORIZED_US, ("Yes, I am a U.S. citizen", "No"), "us_permanent_resident",
     ("UNKNOWN", 0.97), None),
])
def test_options_that_name_a_status_are_never_picked_by_the_table(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
    options: tuple[str, ...], code: str, pick: tuple[str, float], expected: str | None,
) -> None:
    field = status_field(label, SemanticType.WORK_AUTHORIZATION, *options)
    assert _status_table(field, code, _option_keys(field)) is None
    provider = ChoiceProvider({"status": pick})
    packet, _, resolver = resolve_choice(provider, with_status(fictional_candidate, code),
                                         mock_job, field)
    [request] = provider.asked("status")
    assert request["state"]["options"] == {f"o{i}": option for i, option in enumerate(options)}
    [trace] = code_free(resolver, code, "status_derivation")
    assert trace["via"] == "jev"
    assert [shown(a) for a in packet.answers] == ([expected] if expected else [])


# M5: a named city must be the applicant's own.

RELOCATE_OFFICES = "Which of our offices do you currently live near or will you relocate to?"
LIVE_EXPLICIT_SCOPE = {"EXPLICIT_ANSWER": 0.8, "APPLICANT_CURRENT": 0.2}


@pytest.mark.parametrize("city,saved,expected,source,status", [
    ("Dallas", True, "Yes", AnswerSource.SAVED_ANSWER, "CITY_MISMATCH"),  # the person is willing
    ("Dallas", False, None, None, "CITY_MISMATCH"),  # nothing settles it: held
    ("Austin", False, "Yes", AnswerSource.PROFILE_IDENTITY, "ANSWERED"),
])
def test_a_same_state_address_in_another_city_never_answers_yes_to_a_named_city(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, city: str, saved: bool,
    expected: str | None, source: AnswerSource | None, status: str,
) -> None:
    provider = RouteSplit({"relocation": ("o0", 0.99)}, scope=LIVE_EXPLICIT_SCOPE)
    candidate = with_address(fictional_candidate, city=city, region="TX")
    if saved:
        candidate = with_saved(candidate, WILLING)
    packet, _, resolver = resolve_choice(provider, candidate, mock_job, relocation_field(
        AUSTIN_RELOCATION, help_text=None, control=ControlType.RADIO))
    [trace] = stage_traces(resolver, "relocation_screener")
    assert (trace["choice"], trace["status"]) == ("o0", status)
    assert city not in json.dumps(trace)
    assert [(a.value.label, a.provenance.source) for a in packet.answers] == (
        [(expected, source)] if expected else [])
    if status == "CITY_MISMATCH":
        [default] = stage_traces(resolver, "relocation_default")
        assert default["status"] == ("MAPPED" if saved else "NONE")
    else:
        assert not stage_traces(resolver, "relocation_default")


@pytest.mark.parametrize("city,pick,expected,status", [
    ("Dallas", "o0", None, "CITY_MISMATCH"),  # Austin, Texas: the right state, another city
    ("Dallas", "o1", "Dallas, Texas", "ANSWERED"),
    ("Austin", "o0", "Austin, Texas", "ANSWERED"),
])
def test_a_city_option_is_answered_only_for_the_applicants_own_city(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, city: str, pick: str,
    expected: str | None, status: str,
) -> None:
    provider = RouteSplit({"relocation": (pick, 0.99)}, scope=LIVE_EXPLICIT_SCOPE)
    field = relocation_field(RELOCATE_OFFICES, "Austin, Texas", "Dallas, Texas", help_text=None)
    packet, _, resolver = resolve_choice(provider, with_address(fictional_candidate, city=city,
                                                                region="TX"), mock_job, field)
    [trace] = stage_traces(resolver, "relocation_screener")
    assert trace["status"] == status
    assert [(a.value.label, a.provenance.source) for a in packet.answers] == (
        [(expected, AnswerSource.PROFILE_IDENTITY)] if expected else [])


@pytest.mark.parametrize("question,places", [
    (AUSTIN_RELOCATION, ["Austin", "Austin", "Austin"]),
    ("Are you located in Washington, DC or willing to relocate?", ["Washington DC"]),
    ("Are you located in New York, NY or willing to relocate?", ["New York City"]),
    ("Are you located in New York or willing to relocate?", []),  # the state
    ("Are you currently located in the United States or Texas?", []),
])
def test_a_relocation_question_names_cities_but_not_states_or_the_country(
    question: str, places: list[str],
) -> None:
    assert _places(question, after_preposition=True) == places


# M6: an added obligation holds before any statement decision.

BACKGROUND = global_answer("sa.background", "I consent to a background check, subject to "
                           "applicable law.", "Yes", semantic=SemanticType.CONSENT)


@pytest.mark.parametrize("label,semantic,obligations", [
    ("I agree to take a pre-employment drug test.", SemanticType.CONSENT, ["drug_screening"]),
    ("I authorize Fictional Co to contact my previous or former employers.", SemanticType.CONSENT,
     ["previous_employers"]),
    ("I agree to sign a non-compete agreement before my start date.", SemanticType.CONSENT,
     ["non_compete"]),
    ("I agree to resolve any dispute with Fictional Co through binding arbitration.",
     SemanticType.CONSENT, ["arbitration"]),
    ("I agree not to use generative AI tools during my interviews.", SemanticType.CONSENT,
     ["ai_tools"]),
    ("I acknowledge that my employment with Fictional Co will be at-will.", SemanticType.ATTESTATION,
     ["at_will"]),
    ("I consent to a background check, including a credit check.", SemanticType.CONSENT,
     ["extended_screening"]),
    ("I consent to a background check and a review of my driving record.", SemanticType.CONSENT,
     ["extended_screening"]),
])
def test_a_statement_adding_an_obligation_holds_before_any_statement_decision(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
    semantic: SemanticType, obligations: list[str],
) -> None:
    provider = ChoiceProvider({"statement": ("s0", 0.99)})
    candidate = with_statements(fictional_candidate, PRIVACY, CERTIFY, CONTACT, REFERENCES, BACKGROUND)
    packet, ctx, resolver = resolve_choice(provider, candidate, mock_job, status_field(label, semantic))
    assert ctx.form.fields[0].semantic_type is semantic  # still a statement after classification
    assert packet.answers == [] and len(packet.missing_inputs) == 1
    assert not provider.asked("statement") and "statement_coverage" not in purposes(resolver)
    [trace] = stage_traces(resolver, "statement_coverage")
    assert (trace["status"], trace["obligations"]) == ("ADDED_OBLIGATION", obligations)


@pytest.mark.parametrize("label,options,saved,obligations", [
    ("I consent to a background check.", YES_NO, True, None),  # the saved consent covers it
    ("I consent to a background check.", YES_NO, False, ["background"]),
    ("I consent to a background check, including a credit check.", YES_NO, False,
     ["background", "extended_screening"]),
    ("I consent to a background check.", ("Yes, including a credit and driving record check", "No"),
     True, ["extended_screening"]),  # an option can add it too
])
def test_a_background_check_consent_needs_the_saved_background_consent_to_reach_jev(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
    options: tuple[str, ...], saved: bool, obligations: list[str] | None,
) -> None:
    provider = ChoiceProvider({"statement": ("s2", 0.98)})
    statements = (PRIVACY, CONTACT, BACKGROUND) if saved else (PRIVACY, CONTACT)
    packet, _, resolver = resolve_choice(provider, with_statements(fictional_candidate, *statements),
                                         mock_job, status_field(label, SemanticType.CONSENT, *options))
    [trace] = stage_traces(resolver, "statement_coverage")
    if obligations is not None:
        assert not provider.asked("statement") and packet.answers == []
        assert (trace["status"], trace["obligations"]) == ("ADDED_OBLIGATION", obligations)
        return
    [request] = provider.asked("statement")
    assert request["state"]["saved_statements"]["s2"] == BACKGROUND.question
    assert trace["status"] == "ANSWERED"
    [answer] = packet.answers
    assert (answer.value.label, answer.provenance.reference_ids) == ("Yes", ["sa.background"])


# L5 and item 10: the status in the person's words, never a wording candidate, derived for a select.

@pytest.mark.parametrize("label,semantic,code,expected", [
    ("Work authorization status", SemanticType.CUSTOM_TEXT, "us_citizen", "U.S. citizen"),
    ("Work authorization status", SemanticType.WORK_AUTHORIZATION, "us_citizen", "U.S. citizen"),
    ("What is your work authorization status?", SemanticType.WORK_AUTHORIZATION, "h1b",
     "H-1B visa holder"),
])
def test_a_text_question_asking_for_the_status_gets_its_meaning_never_the_code(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
    semantic: SemanticType, code: str, expected: str,
) -> None:
    provider = ChoiceProvider(semantic="CUSTOM_TEXT")
    packet, ctx, resolver = resolve_choice(provider, with_stated_status(fictional_candidate, code),
                                           mock_job, text_field(label, semantic))
    assert ctx.form.fields[0].semantic_type is semantic
    [answer] = packet.answers
    assert answer.value == TextValue(text=expected)
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, ["sa.status"])
    assert code not in packet.model_dump_json()
    assert not provider.asked("status") and not provider.asked("wording")
    assert code not in json.dumps(resolver.narrative_traces)


@pytest.mark.parametrize("field,pick", [
    (status_field("Are you able to work on weekends?", SemanticType.CUSTOM_SELECT), None),
    (status_field("Do you hold an active U.S. security clearance?", SemanticType.WORK_AUTHORIZATION),
     ("UNKNOWN", 0.97)),  # not settled by the status: the untyped answers by wording
])
def test_the_stated_status_is_never_offered_as_a_wording_candidate(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, field: ApplicationField,
    pick: tuple[str, float] | None,
) -> None:
    candidate = with_stated_status(with_statements(fictional_candidate, *UNTYPED_NOISE), "us_citizen")
    provider = ChoiceProvider({"status": pick} if pick else {})
    _, _, resolver = resolve_choice(provider, candidate, mock_job, field)
    [request] = provider.asked("wording")
    saved = request["state"]["saved_questions"]
    assert {item["question"] for item in saved.values()} == {a.question for a in UNTYPED_NOISE}
    shown_saved = json.dumps(saved)
    assert all(text not in shown_saved for text in (WORK_AUTHORIZATION_STATUS_QUESTION, *STATUS_PHRASES))
    assert "us_citizen" not in json.dumps(request) and "U.S. citizen" not in json.dumps(request)
    [trace] = code_free(resolver, "us_citizen", "question_equivalence")
    assert trace["candidate_ids"] == [[a.id] for a in UNTYPED_NOISE]
    assert "sa.status" not in json.dumps(trace)


CURRENT_AUTHORIZATION = "What is your current U.S. work authorization?"
AUTHORIZATION_KINDS = ("U.S. Citizen or Permanent Resident", "Visa holder", "Not authorized")


@pytest.mark.parametrize("code,pick,expected", [
    ("us_citizen", ("o0", 0.98), "U.S. Citizen or Permanent Resident"),
    ("h1b", ("o1", 0.98), "Visa holder"),
    ("us_citizen", ("UNKNOWN", 0.97), None),
])
def test_a_select_asking_for_the_status_is_derived_from_it_not_mapped(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, code: str,
    pick: tuple[str, float], expected: str | None,
) -> None:
    provider = ChoiceProvider({"status": pick, "equivalent_0": ("o0", 0.99), "wording": ("q0", 0.99)})
    field = status_field(CURRENT_AUTHORIZATION, SemanticType.WORK_AUTHORIZATION, *AUTHORIZATION_KINDS)
    packet, _, resolver = resolve_choice(provider, with_stated_status(fictional_candidate, code),
                                         mock_job, field)
    assert not provider.asked("equivalent_0") and not provider.asked("wording")
    [request] = provider.asked("status")
    assert request["state"]["status"]["code"] == code
    assert request["state"]["options"] == {f"o{i}": kind for i, kind in enumerate(AUTHORIZATION_KINDS)}
    [trace] = code_free(resolver, code, "status_derivation")
    assert not stage_traces(resolver, "exact_saved_answer")  # the status is not an own answer
    if expected is None:
        assert (trace["via"], trace["status"]) == ("jev", "UNKNOWN") and packet.answers == []
        [missing] = packet.missing_inputs
        assert "Your saved answer 'U.S. citizen' cannot be used here" in missing.prompt
        assert code not in missing.prompt
        return
    [answer] = packet.answers
    assert (answer.value.label, answer.provenance.reference_ids) == (expected, ["sa.status"])
    assert answer.provenance.note == "derived from the stated U.S. work authorization status (Jev)"
    assert (trace["via"], trace["choice"], trace["status"]) == ("jev", pick[0], "ANSWERED")


# L6: the person's own answer to exactly this wording comes first.

@pytest.mark.parametrize("case", ["work_authorization", "sponsorship", "statement", "relocation",
                                  "pay_period"])
def test_the_persons_own_answer_that_does_not_fit_is_never_replaced_by_a_derived_one(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, case: str,
) -> None:
    candidate = with_status(fictional_candidate, "us_citizen")
    derived = {"work_authorization": ["status_derivation"], "sponsorship": ["status_derivation"],
               "statement": ["statement_coverage"],
               "relocation": ["relocation_screener", "relocation_default"],
               "pay_period": ["salary_period"]}[case]
    if case == "work_authorization":
        fields = [status_field(WORK_AUTH, SemanticType.WORK_AUTHORIZATION, *AUTHORIZED)]
        reference, stored = "sa.work_auth_us", "Yes"
    elif case == "sponsorship":
        fields = [status_field(SPONSORSHIP, SemanticType.SPONSORSHIP, *SPONSORSHIP_OPTIONS)]
        reference, stored = "sa.sponsorship", "No"
    elif case == "statement":  # the fixture's own consent, saved as true
        fields = [status_field("I consent to the processing of my data for recruiting purposes",
                               SemanticType.CONSENT, "I agree", "I do not agree")]
        reference, stored = "sa.privacy_consent", "Yes"
    elif case == "relocation":
        fields = [relocation_field()]
        reference, stored = "sa.relocate_exact", "Only for the right role"
        candidate = with_saved(candidate, global_answer(reference, fields[0].question_text, stored,
                                                        semantic=SemanticType.RELOCATION))
    else:
        fields = list(salary_form("Pay period"))
        reference, stored = "sa.period", "Biweekly"
        candidate = with_saved(candidate, global_answer(
            "sa.salary", "What is your desired salary?", "USD 95,000 per year",
            semantic=SemanticType.SALARY_EXPECTATION), global_answer(
            reference, "Pay period", stored, semantic=SemanticType.SALARY_EXPECTATION))
    field = fields[-1]
    provider = ChoiceProvider({"status": ("o0", 0.99), "statement": ("s0", 0.99),
                               "relocation": ("o0", 0.99)})
    packet, _, resolver = resolve_choice(provider, candidate, mock_job, *fields)
    assert packet.answer_for(field.id) is None
    [missing] = [m for m in packet.missing_inputs if m.field_id == field.id]
    assert f"Your saved answer {stored!r} cannot be used here" in missing.prompt
    [trace] = [t for t in code_free(resolver, "us_citizen", "exact_saved_answer")
               if t["field_id"] == field.id]
    assert (trace["status"], trace["reference_ids"]) == ("NOT_PLACED", [reference])
    [mapping] = [t for t in stage_traces(resolver, "option_equivalence") if t["field_id"] == field.id]
    assert mapping["status"] == "NONE"
    assert all(not stage_traces(resolver, stage) for stage in derived)
    assert not any(provider.asked(name) for name in ("status", "statement", "relocation"))


# Found by the round-9 tests: a status saved under round 8's broader "ead_opt" (any EAD) can
# stand beside the person's own "No" to sponsorship; the derivation then holds, and the person's
# own answers decide through the wording path.

@pytest.mark.parametrize("code,key,value", [
    ("ead_opt", "requires_visa_sponsorship", "No"),
    ("h1b", "requires_visa_sponsorship", "No"),
    ("us_citizen", "authorized_to_work_us", "No"),
])
def test_a_status_contradicting_the_persons_own_legal_answer_is_never_derived(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, code: str, key: str, value: str,
) -> None:
    from interviewmaxxing_core import STATED_ANSWER_QUESTIONS

    own = global_answer(f"sa.{key}", STATED_ANSWER_QUESTIONS[key], value,
                        semantic=(SemanticType.SPONSORSHIP if key == "requires_visa_sponsorship"
                                  else SemanticType.WORK_AUTHORIZATION))
    provider = ChoiceProvider({"status": ("o0", 0.99), "wording": ("NONE", 0.99)})
    candidate = with_saved(with_status(fictional_candidate, code), own)
    packet, _, resolver = resolve_choice(provider, candidate, mock_job,
                                         status_field(SPONSOR_US, SemanticType.SPONSORSHIP))
    assert not provider.asked("status") and packet.answers == []
    [trace] = stage_traces(resolver, "status_derivation")
    assert (trace["status"], trace["stated_keys"]) == ("CONTRADICTS_STATED", [key])
    assert code not in json.dumps(trace)
    assert provider.asked("wording")  # the person's own answers decide by wording


def test_a_consistent_own_answer_leaves_the_derivation_in_place(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    from interviewmaxxing_core import STATED_ANSWER_QUESTIONS

    own = global_answer("sa.sponsor", STATED_ANSWER_QUESTIONS["requires_visa_sponsorship"], "Yes",
                        semantic=SemanticType.SPONSORSHIP)
    provider = ChoiceProvider({"status": ("o0", 0.99)})
    candidate = with_saved(with_status(fictional_candidate, "ead_opt"), own)
    packet, _, _ = resolve_choice(provider, candidate, mock_job,
                                  status_field(SPONSOR_US, SemanticType.SPONSORSHIP))
    [answer] = packet.answers
    assert answer.value.label == "Yes" and answer.provenance.reference_ids == ["sa.status"]

# --- WP12 round 3: derived years facts and story facts select the platforms they name ---------

PAID_MEDIA_PLATFORMS = "Which paid media platforms have you directly managed? (Select all that apply)"
META_STORY = "I ran Meta Ads prospecting and retargeting for Crumb & Co. Bakeries, reporting monthly to the owner."
GOOGLE_ADS_YEARS = ("Years of Google Ads experience derived from the resume roles that name it: 40 months "
                    "with overlaps merged, rounded down to whole years")


class SourcedProvider(MultiProvider):
    """MultiProvider whose ``source_o<i>`` choices name the fact ``sources`` maps the option
    label to (by fact id), so a derived years fact or a story fact can be the source."""

    def __init__(self, picks: dict[str, tuple[str, float]], *, sources: dict[str, str],
                 scope: str = "HISTORICAL_OR_CONTEXTUAL") -> None:
        super().__init__(picks, option=by_label(dict.fromkeys(sources, 1.0)), scope=scope)
        self.sources = sources

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        response = super().__call__(url, headers, body, timeout)
        request, answers = self.requests[-1], json.loads(response.body)["answers"]
        state = request["state"]
        for name, question in request["questions"].items():
            if not name.startswith("source_"):
                continue
            wanted = self.sources.get(state["options"][name.removeprefix("source_")])
            key = next((k for k, f in state["facts"].items() if f["id"] == wanted), "NONE")
            answers[name] = {"type": "choice", "choice": key, "confidence": self.confidence,
                             "probabilities": {k: float(k == key) for k in question["criteria"]}}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())


def with_derived_and_story_facts(candidate: CandidateProfile) -> CandidateProfile:
    base = candidate.verified_facts()[0]
    derived = base.model_copy(update={
        "id": "derived_years_experience_google_ads", "key": "years_experience.google_ads", "value": 3,
        "source": "derived:experience_timeline",
        "evidence": [GOOGLE_ADS_YEARS, "roles: Fictional Widgets Co (2021-03 to 2024-06)"]})
    story = base.model_copy(update={
        "id": "sf_bakery_0002", "key": "experience", "value": META_STORY, "source": "story:" + "d" * 64,
        "evidence": [META_STORY, "period_source: resume_role", "resume_role_id: exp_bakery"]})
    languages = base.model_copy(update={"id": "fact.languages", "key": "languages",
                                        "value": "Speaks conversational Spanish",
                                        "evidence": ["Speaks conversational Spanish"]})
    return candidate.model_copy(update={"facts": [derived, story, languages], "experience": [], "education": []})


def paid_media_field() -> ApplicationField:
    return choice_field(PAID_MEDIA_PLATFORMS, SemanticType.CUSTOM_MULTISELECT, *PLATFORM_OPTIONS,
                        control=ControlType.MULTISELECT, field_id="platforms")


def test_derived_years_and_story_facts_select_the_platforms_they_name(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = SourcedProvider({"fact_select": ("SUPPORTED", 0.98)}, sources={
        "Google Ads": "derived_years_experience_google_ads", "Meta Ads": "sf_bakery_0002"})
    packet, _, resolver = resolve_choice(provider, with_derived_and_story_facts(fictional_candidate),
                                         mock_job, paid_media_field())
    assert packet.is_complete
    [answer] = packet.answers
    assert [(c.value, c.label) for c in answer.value.choices] == [("v0", "Google Ads"), ("v1", "Meta Ads")]
    assert answer.provenance.source is AnswerSource.GENERATED_FROM_FACTS
    assert set(answer.provenance.reference_ids) == {"derived_years_experience_google_ads", "sf_bakery_0002"}
    [request] = provider.asked("fact_select")
    assert request["state"]["screener_version"] == "experience-screener-v2"
    facts = request["state"]["facts"].values()
    assert {f["source"] for f in facts} >= {"derived:experience_timeline", "story:" + "d" * 64}
    assert any(f["key"] == "years_experience.google_ads" and f["value"] == 3 for f in facts)
    questions = json.dumps(request["questions"])
    assert "years_experience.<area>" in questions and "whose source starts with story:" in questions
    [trace] = stage_traces(resolver, "fact_screener")
    assert (trace["kind"], trace["status"], trace["selected"]) == ("multi", "ANSWERED", ["o0", "o1"])
    assert trace["sources"] == {"o0": "derived_years_experience_google_ads", "o1": "sf_bakery_0002"}
    assert not provider.asked("route")


def test_the_choice_screeners_retrieve_with_their_option_labels() -> None:
    query = DynamicPacketResolver._screener_query(paid_media_field())
    assert query == PAID_MEDIA_PLATFORMS + "\nOptions: " + ", ".join(PLATFORM_OPTIONS)
    plain = choice_field("How many people have you managed?", SemanticType.CUSTOM_SELECT,
                         control=ControlType.TEXT, field_id="count")
    assert DynamicPacketResolver._screener_query(plain) == "How many people have you managed?"
    wide = choice_field("Which tools?", SemanticType.CUSTOM_MULTISELECT, *[f"Tool {i}" for i in range(60)],
                        control=ControlType.MULTISELECT)
    assert DynamicPacketResolver._screener_query(wide).count("Tool") == 40


# --- round 10: the answers the person just imported reach the forms ---------------------------

from interviewmaxxing_browser.ai.routing import (  # noqa: E402
    UNTYPED_CONFIDENCE,
    UNTYPED_PROBABILITY,
)
from interviewmaxxing_browser.ai.salary import (  # noqa: E402
    parse_salary,
    range_options,
    salary_range,
    salary_wording,
)
from interviewmaxxing_core import WORK_LOCATION_PREFERENCE_QUESTION  # noqa: E402

DESIRED_SALARY = "What is your desired salary?"
SALARY_BANDS = ("Under $20,000",
                *(f"${low:,} - ${low + 9_999:,}" for low in range(20_000, 1_190_000, 10_000)),
                "$1,190,000 and above")
"""A live-shaped range select (Lovevery: 119 options), fictional bounds."""
assert len(SALARY_BANDS) == 119


def saved_salary(value: str = "USD 95,000 per year", *, answer_id: str = "sa.salary",
                 scope: AnswerScope = AnswerScope.GLOBAL, job: JobRecord | None = None,
                 confirmed_at: str = "2026-09-02T12:00:00Z") -> SavedAnswer:
    return SavedAnswer(id=answer_id, scope=scope, semantic_type=SemanticType.SALARY_EXPECTATION,
                       question=DESIRED_SALARY, value=value, confirmed_at=confirmed_at,
                       job_identity_key=job.identity_key if job else None)


def range_select(label: str, *options: str, semantic: SemanticType = SemanticType.SALARY_EXPECTATION,
                 field_id: str = "range", **updates: Any) -> ApplicationField:
    field = choice_field(label, semantic, *(options or SALARY_BANDS), control=ControlType.SELECT,
                         field_id=field_id)
    return field.model_copy(update=updates) if updates else field


# 1. Salary range selects: arithmetic, never a call.

def test_the_live_range_select_takes_the_one_band_containing_the_saved_salary(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    # Lovevery: question equivalence MAPPED (0.96), then option equivalence o0 at 0.58 held the
    # 119-option select. Now the band is chosen by its bounds and the saved unit (annual, read
    # from the bounds' magnitude); Jev is not asked about the options.
    provider = WordingPick("desired salary", {"equivalent_0": ("o0", 0.58)})
    field = range_select("What salary range are you targeting?")
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, saved_salary(),
                                                              *UNTYPED_NOISE), mock_job, field)
    assert not provider.asked("equivalent_0") and not stage_traces(resolver, "option_equivalence")
    [answer] = packet.answers
    assert answer.value.label == "$90,000 - $99,999"
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, ["sa.salary"])
    assert (answer.provenance.note or "").endswith(
        "; the one range containing the saved amount (no model call)")
    [trace] = stage_traces(resolver, "salary_range")
    assert (trace["status"], trace["unit_source"], trace["range_count"]) == ("MAPPED", "magnitude", 119)
    assert trace["choice"] == "o8" and "95" not in json.dumps(trace)  # never the amount
    [wording] = stage_traces(resolver, "question_equivalence")
    assert wording["status"] == "MAPPED"


def test_a_range_select_with_the_exact_desired_salary_wording_needs_no_call_at_all(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"equivalent_0": ("o0", 0.58)})
    field = range_select("Desired salary")  # a listed wording of the saved question
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, saved_salary()),
                                         mock_job, field)
    assert [r for r in provider.requests if "equivalent_0" in r["questions"]] == []
    assert not provider.asked("wording")
    [answer] = packet.answers
    assert answer.value.label == "$90,000 - $99,999"
    assert stage_traces(resolver, "salary_range")[0]["status"] == "MAPPED"


@pytest.mark.parametrize("value,options,label,status,unit_source", [
    ("$45/hr", SALARY_BANDS, "Salary range", "UNIT_MISMATCH", "magnitude"),  # hourly vs annual
    ("95,000", SALARY_BANDS, "Salary range", "NO_UNIT", None),  # the saved value states no unit
    ("USD 70,000 per year", ("$60,000 - $70,000", "$70,000 - $80,000"), "Salary range",
     "AMBIGUOUS", "magnitude"),  # a shared boundary
    ("USD 5,000,000 per year", SALARY_BANDS[:-1], "Salary range", "NOT_CONTAINED", "magnitude"),
    ("USD 95,000 per year", ("$40 - $45 per hour", "$45 - $50 per hour"), "Pay range",
     "UNIT_MISMATCH", "options"),  # the options state their unit
    ("USD 95,000 per year", ("$5,000 - $7,999", "$8,000 - $9,999"), "Salary range",
     "UNIT_UNSTATED", None),  # monthly-looking bounds: nothing states the unit
    ("USD 95,000 per year", ("€80,000 - €89,999", "€90,000 - €99,999"), "Salary range",
     "CURRENCY_MISMATCH", "magnitude"),
    ("USD 95,000 per year", ("$8,000 - $9,999", "$10,000 - $11,999"), "Monthly pay range",
     "UNIT_MISMATCH", "wording"),  # the field's wording states the unit
    ("$8,500 per month", ("$8,000 - $9,999", "$10,000 - $11,999"), "Monthly pay range",
     "MAPPED", "wording"),
    ("$45/hr", ("$40 - $45", "$46 - $50"), "Hourly pay range", "MAPPED", "wording"),
    ("$45/hr", ("Under $30", "$30 - $50", "$50+"), "Rate", "MAPPED", "magnitude"),
])
def test_a_range_select_holds_without_a_containing_band_in_the_saved_unit(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, value: str,
    options: tuple[str, ...], label: str, status: str, unit_source: str | None,
) -> None:
    provider = WordingPick("desired salary", {"equivalent_0": ("o0", 0.99)})
    field = range_select(label, *options)
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, saved_salary(value)),
                                         mock_job, field)
    assert not provider.asked("equivalent_0")  # settled either way: never a Jev option pick
    [trace] = stage_traces(resolver, "salary_range")
    assert (trace["status"], trace.get("unit_source")) == (status, unit_source)
    if status == "MAPPED":
        [answer] = packet.answers
        chosen, saved = salary_range(answer.value.label), parse_salary(value)
        assert chosen is not None and saved is not None and chosen.contains(saved.amount)
    else:
        assert packet.answers == []
        [missing] = packet.missing_inputs
        assert missing.field_id == "range" and "95,000" not in json.dumps(trace)


def test_a_custom_typed_years_select_is_not_read_as_a_salary_range(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    # Ranges without a currency on a field that is not a salary keep Jev's option equivalence.
    years = global_answer("sa.years", "How many years of marketing experience do you have?", "7")
    provider = WordingPick("years of marketing", {"equivalent_0": ("o1", 0.98)})
    field = range_select("Years of experience in marketing", "1-3 years", "4-7 years", "8+ years",
                         semantic=SemanticType.CUSTOM_SELECT)
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, years, *UNTYPED_NOISE),
                                         mock_job, field)
    assert provider.asked("equivalent_0") and not stage_traces(resolver, "salary_range")
    assert [a.value.label for a in packet.answers] == ["4-7 years"]


@pytest.mark.parametrize("label,expected", [
    ("$50,000 - $59,999", (50_000.0, 59_999.0, None, "USD")),
    ("$200K and above", (200_000.0, float("inf"), None, "USD")),
    ("$200,000+", (200_000.0, float("inf"), None, "USD")),
    ("Under $20,000", (float("-inf"), 20_000.0, None, "USD")),
    ("Less than 30k", (float("-inf"), 30_000.0, None, None)),
    ("Between $50k and $60k per year", (50_000.0, 60_000.0, "year", "USD")),
    ("$20 - $25 per hour", (20.0, 25.0, "hour", "USD")),
    ("€80,000 \u2013 €89,999", (80_000.0, 89_999.0, None, "EUR")),  # an en dash
    ("USD 95,000", (95_000.0, 95_000.0, None, "USD")),
    ("5-10 years", None),  # a years range is not a salary range
    ("Google Analytics 4", None),
    ("Prefer not to say", None),
    ("$50,000 - $60,000 base plus bonus", None),
])
def test_salary_range_reads_the_bounds_a_range_option_states(
    label: str, expected: tuple[float, float, str | None, str | None] | None,
) -> None:
    bounds = salary_range(label)
    assert (bounds is None) is (expected is None)
    if bounds is not None and expected is not None:
        assert (bounds.low, bounds.high, bounds.period, bounds.currency) == expected


@pytest.mark.parametrize("value,expected", [
    ("USD 95,000 per year", (95_000.0, "year", "USD", True)),
    ("$45/hr", (45.0, "hour", "USD", True)),
    ("95k annually", (95_000.0, "year", None, True)),
    ("95,000", (95_000.0, None, None, True)),
    ("€7,500 a month", (7_500.0, "month", "EUR", True)),
    ("$150,000 OTE", (150_000.0, None, "USD", False)),
    ("$120,000 base + bonus", None),  # "+" states no one amount
    ("$90,000 - $100,000", None),
    ("about $95,000", None),
    ("2026", None),  # a bare year
    ("negotiable", None),
])
def test_parse_salary_reads_one_exact_amount_with_its_unit(
    value: str, expected: tuple[float, str | None, str | None, bool] | None,
) -> None:
    parsed = parse_salary(value)
    assert (parsed is None) is (expected is None)
    if parsed is not None and expected is not None:
        assert (parsed.amount, parsed.period, parsed.currency, parsed.base) == expected


def test_range_options_allow_non_answers_beside_the_bands() -> None:
    options = [FieldOption(value=f"v{i}", label=label)
               for i, label in enumerate(("Under $20,000", "$20,000 - $29,999", "Prefer not to say"))]
    assert [o.label for o, _ in range_options(options)] == ["Under $20,000", "$20,000 - $29,999"]
    assert range_options(options[:1]) == []  # one band is not a range select
    assert range_options([*options, FieldOption(value="v9", label="Somewhere in the middle")]) == []


# The pay-period select labelled by the block heading ("Desired Salary").

def test_a_period_select_labelled_desired_salary_takes_the_saved_unit_without_a_call(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    # Lovevery: the sibling select's label came from the block heading, so it matched the saved
    # question exactly and Jev mapped "USD 95,000 per year" onto "Yearly" at 0.86 (below the
    # gate). A select whose enabled options are all pay periods is a pay-period choice
    # whatever its label.
    salary, period = salary_form("Desired Salary")
    provider = ChoiceProvider({"equivalent_0": ("o3", 0.86)})
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, saved_salary()),
                                         mock_job, salary, period)
    assert not provider.asked("equivalent_0") and not stage_traces(resolver, "option_equivalence")
    assert not stage_traces(resolver, "exact_saved_answer")
    chosen = next(a for a in packet.answers if a.field_id == "period")
    assert (chosen.value.label, chosen.provenance.reference_ids) == ("Yearly", ["sa.salary"])
    [trace] = stage_traces(resolver, "salary_period")
    assert trace["status"] == "ANSWERED"
    assert next(a for a in packet.answers if a.field_id == "salary").value.text == "USD 95,000 per year"


def test_a_period_select_with_an_unrelated_label_is_still_the_pay_period(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    salary, period = salary_form("How would you like your compensation expressed?")
    provider = ChoiceProvider({"wording": ("NONE", 0.99)})
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, saved_salary("$45/hr")),
                                         mock_job, salary, period)
    assert next(a for a in packet.answers if a.field_id == "period").value.label == "Hourly"
    assert stage_traces(resolver, "salary_period")[0]["status"] == "ANSWERED"


# 2. Base, annual, expected and target salary wordings from the desired salary.

@pytest.mark.parametrize("label,help_text,value,expected", [
    ("What is your desired base salary?", None, "USD 95,000 per year", "USD 95,000 per year"),
    ("Expected annual salary (USD)", None, "USD 95,000 per year", "USD 95,000 per year"),
    ("Target base salary", "Please enter a single figure.", "$95,000/yr", "$95,000/yr"),
    ("Base salary expectations", None, "95k annually", "95k annually"),
    ("What are your salary expectations?", None, "USD 95,000 per year", "USD 95,000 per year"),
    ("What is your desired salary?\nCompensation", None, "95,000", "95,000"),  # no unit named: as saved
    ("Desired monthly salary", None, "$8,000 per month", "$8,000 per month"),
    ("Expected hourly pay", None, "$45/hr", "$45/hr"),
])
def test_a_base_salary_wording_is_answered_from_the_desired_salary_without_a_call(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
    help_text: str | None, value: str, expected: str,
) -> None:
    # Lovevery: "What is your desired base salary?" was NONE (0.96) against the saved "What is
    # your desired salary?". A plain desired salary states a base figure.
    provider = ChoiceProvider({"wording": ("NONE", 0.96)})
    field = text_field(label, SemanticType.SALARY_EXPECTATION).model_copy(update={"help_text": help_text})
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, saved_salary(value),
                                                              *UNTYPED_NOISE), mock_job, field)
    assert not provider.asked("wording") and not stage_traces(resolver, "question_equivalence")
    [answer] = packet.answers
    assert answer.value.text == expected
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, ["sa.salary"])
    assert "a plain desired salary states the base figure" in (answer.provenance.note or "")
    [trace] = stage_traces(resolver, "salary_wording")
    assert (trace["wording"], trace["status"]) == ("PLAIN", "MAPPED")
    assert value not in json.dumps(trace)


@pytest.mark.parametrize("label,value,status", [
    ("What are your target compensation expectations (base and/or OTE, if applicable)?",
     "USD 95,000 per year", "COMPENSATION_CLAUSE"),
    ("Total compensation expectations", "USD 95,000 per year", "COMPENSATION_CLAUSE"),
    ("Desired base salary and bonus", "USD 95,000 per year", "COMPENSATION_CLAUSE"),
    ("What is your desired base salary?", "$150,000 OTE", "SAVED_NOT_BASE"),
    ("Desired monthly salary", "USD 95,000 per year", "UNIT_MISMATCH"),
    ("Expected hourly rate", "USD 95,000 per year", "UNIT_MISMATCH"),
    ("Expected annual salary", "$45/hr", "UNIT_MISMATCH"),
    ("Expected annual salary", "95,000", "UNIT_MISMATCH"),  # the saved value states no unit
    ("Expected annual salary (EUR)", "USD 95,000 per year", "CURRENCY_MISMATCH"),
])
def test_a_compensation_or_other_unit_wording_holds_without_a_call(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str, value: str, status: str,
) -> None:
    provider = ChoiceProvider({"wording": ("q0", 0.99)})
    field = text_field(label, SemanticType.SALARY_EXPECTATION)
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, saved_salary(value)),
                                         mock_job, field)
    assert not provider.asked("wording") and packet.answers == []
    [missing] = packet.missing_inputs
    assert missing.reason is MissingReason.EXPLICIT_ANSWER_REQUIRED
    [trace] = stage_traces(resolver, "salary_wording")
    assert trace["status"] == status and value not in json.dumps(trace)


@pytest.mark.parametrize("label", [
    "What is your current salary?",
    "What is your minimum salary requirement?",
    "What is your desired salary range?",
    "What salary are you looking for?",
])
def test_other_salary_wordings_keep_the_wording_decision(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str,
) -> None:
    provider = ChoiceProvider({"wording": ("NONE", 0.99)})
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, saved_salary()),
                                         mock_job, text_field(label, SemanticType.SALARY_EXPECTATION))
    assert provider.asked("wording") and packet.answers == []
    assert not stage_traces(resolver, "salary_wording")
    assert stage_traces(resolver, "question_equivalence")[0]["gate"] == "standard"


@pytest.mark.parametrize("label,value,expected,status", [
    ("Desired base salary", "USD 95,000 per year", "95000", "MAPPED"),
    ("Expected hourly rate", "$45/hr", "45", "MAPPED"),
    ("Expected hourly rate", "$45.50 per hour", "45.5", "MAPPED"),
    ("Desired base salary", "$45/hr", None, "NUMBER_UNIT_UNSTATED"),  # a bare number reads annual
    ("Desired base salary", "95,000", None, "NUMBER_UNIT_UNSTATED"),
])
def test_a_numeric_salary_input_takes_the_bare_amount_only_in_the_stated_unit(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str, value: str,
    expected: str | None, status: str,
) -> None:
    provider = ChoiceProvider({"wording": ("q0", 0.99)})
    field = text_field(label, SemanticType.SALARY_EXPECTATION).model_copy(update={"input_type": "number"})
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, saved_salary(value)),
                                         mock_job, field)
    assert not provider.asked("wording")
    assert [a.value.text for a in packet.answers] == ([expected] if expected else [])
    assert stage_traces(resolver, "salary_wording")[0]["status"] == status


def test_a_job_scoped_salary_answer_for_this_job_comes_before_the_global_one(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    scoped = saved_salary("USD 120,000 per year", answer_id="sa.salary_job", scope=AnswerScope.JOB,
                          job=mock_job, confirmed_at="2026-08-01T12:00:00Z")
    provider = ChoiceProvider({"wording": ("q0", 0.99)})
    packet, _, _ = resolve_choice(provider, with_saved(fictional_candidate, saved_salary(), scoped),
                                  mock_job, text_field("Desired base salary", SemanticType.SALARY_EXPECTATION))
    [answer] = packet.answers
    assert (answer.value.text, answer.provenance.reference_ids) == ("USD 120,000 per year", ["sa.salary_job"])


def test_a_base_salary_wording_on_a_range_select_takes_the_containing_band(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"wording": ("q0", 0.99), "equivalent_0": ("o0", 0.99)})
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, saved_salary()),
                                         mock_job, range_select("Expected annual base salary"))
    assert not provider.requests[1:]  # only the classification: no wording, no option pick
    [answer] = packet.answers
    assert answer.value.label == "$90,000 - $99,999"
    assert stage_traces(resolver, "salary_wording")[0]["status"] == "MAPPED"
    assert stage_traces(resolver, "salary_range")[0]["unit_source"] == "wording"


@pytest.mark.parametrize("question,kind", [
    ("What is your desired base salary?", "PLAIN"),
    ("Salary expectations", "PLAIN"),
    ("Expected annual compensation", "PLAIN"),
    ("Target base salary (USD)", "PLAIN"),
    ("Desired pay", "PLAIN"),
    ("Desired base salary and bonus", "COMPENSATION_CLAUSE"),
    ("Target OTE", "OTHER"),  # no plain-salary noun
    ("Expected total compensation", "COMPENSATION_CLAUSE"),
    ("What is your current base salary?", "OTHER"),
    ("Desired salary range", "OTHER"),
    ("Why do you deserve this salary?", "OTHER"),
    ("Where are you based?", "OTHER"),
])
def test_salary_wording_classifies_the_live_wordings(question: str, kind: str) -> None:
    assert salary_wording(question).value == kind


# 3. EEO answers map by type, not by wording.

RACE_HELP = ("For government reporting purposes, we ask candidates to respond to the below "
             "self-identification survey. Hispanic or Latino: a person of Cuban, Mexican, Puerto "
             "Rican, South or Central American, or other Spanish culture or origin. White (Not "
             "Hispanic or Latino): a person having origins in any of the original peoples of "
             "Europe, the Middle East, or North Africa.")
LIVE_RACE_OPTIONS = ("Hispanic or Latino", "White (Not Hispanic or Latino)",
                     "Black or African American (Not Hispanic or Latino)",
                     "Asian (Not Hispanic or Latino)", "Two or More Races (Not Hispanic or Latino)",
                     "Decline To Self Identify")
VETERAN_HELP = ("If you believe you belong to any of the categories of protected veterans listed "
                "below, please indicate by making the appropriate selection.")
LIVE_VETERAN_OPTIONS = ("I identify as one or more of the classifications of a protected veteran",
                        "I am not a protected veteran", "I don't wish to answer")
DISABILITY_OPTIONS = ("Yes, I have a disability, or have had one in the past",
                      "No, I do not have a disability and have not had one in the past",
                      "I do not want to answer")
RACE_ANSWER = global_answer("sa.race", "Race/Ethnicity", "White", semantic=SemanticType.EEO_RACE_ETHNICITY)
HISPANIC_ANSWER = global_answer("sa.hispanic", "Are you Hispanic/Latino?", "No",
                                semantic=SemanticType.EEO_RACE_ETHNICITY)
VETERAN_ANSWER = global_answer("sa.veteran", "Veteran Status", "I am not a protected veteran",
                               semantic=SemanticType.EEO_VETERAN_STATUS)
DISABILITY_ANSWER = global_answer("sa.disability", "Disability Status",
                                  "No, I do not have a disability",
                                  semantic=SemanticType.EEO_DISABILITY_STATUS)


def eeo_field(label: str, semantic: SemanticType, *options: str, help_text: str | None = None,
              required: bool = True) -> ApplicationField:
    return choice_field(label, semantic, *options, control=ControlType.SELECT,
                        required=required).model_copy(update={"help_text": help_text})


def test_the_live_race_question_takes_the_race_answer_by_type(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    # "Race / For government reporting purposes …": two same-type candidates and a wording
    # decision at 0.77 to 0.78. The type carries the question; the wording only picks the
    # sub-answer (the help text mentions Hispanic or Latino, the label asks for race).
    provider = ChoiceProvider({"wording": ("q0", 0.78), "equivalent_0": ("o1", 0.97)})
    field = eeo_field("Race", SemanticType.EEO_RACE_ETHNICITY, *LIVE_RACE_OPTIONS, help_text=RACE_HELP)
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, RACE_ANSWER,
                                                              HISPANIC_ANSWER, *UNTYPED_NOISE),
                                         mock_job, field)
    assert not provider.asked("wording") and not stage_traces(resolver, "question_equivalence")
    [request] = provider.asked("equivalent_0")
    assert request["state"]["stored_answers"] == {"equivalent_0": "White"}
    [answer] = packet.answers
    assert answer.value.label == "White (Not Hispanic or Latino)"
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, ["sa.race"])
    assert "the field's type carries the question (no wording decision)" in (answer.provenance.note or "")
    assert answer.confidence == pytest.approx(0.97)
    [trace] = stage_traces(resolver, "eeo_answer")
    assert (trace["sub_answer"], trace["status"], trace["reference_ids"]) == (
        "race_ethnicity", "MAPPED", ["sa.race"])
    assert set(trace["candidate_ids"]) == {"sa.race", "sa.hispanic"}
    assert "White" not in json.dumps(resolver.narrative_traces)


@pytest.mark.parametrize("label,help_text", [
    ("Are you Hispanic or Latino?", None),
    ("Hispanic/Latino", "Voluntary self-identification"),
    ("Ethnicity: Hispanic or Latino?", None),  # names race wording: the race answer
])
def test_a_hispanic_latino_question_takes_the_hispanic_sub_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str, help_text: str | None,
) -> None:
    provider = ChoiceProvider({"wording": ("q1", 0.78), "equivalent_0": ("NONE", 0.99)})
    field = eeo_field(label, SemanticType.EEO_RACE_ETHNICITY, "Yes", "No", "Decline to answer",
                      help_text=help_text)
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, RACE_ANSWER,
                                                              HISPANIC_ANSWER), mock_job, field)
    assert not provider.asked("wording")
    [trace] = stage_traces(resolver, "eeo_answer")
    if label.startswith("Ethnicity"):  # the race answer ("White") fits no Yes/No option: held
        assert (trace["sub_answer"], trace["status"]) == ("race_ethnicity", "VALUE_DOES_NOT_FIT")
        assert provider.asked("equivalent_0") and packet.answers == []
        return
    assert not provider.asked("equivalent_0")  # "No" is an option: no call
    [answer] = packet.answers
    assert (answer.value.label, answer.provenance.reference_ids) == ("No", ["sa.hispanic"])
    assert (trace["sub_answer"], trace["status"]) == ("hispanic_latino", "MAPPED")


def test_a_combined_race_ethnicity_question_takes_the_race_answer(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"equivalent_0": ("o3", 0.96)})
    field = eeo_field("Race / Ethnicity (voluntary)", SemanticType.EEO_RACE_ETHNICITY, *RACE_OPTIONS)
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, RACE_ANSWER,
                                                              HISPANIC_ANSWER), mock_job, field)
    [answer] = packet.answers
    assert (answer.value.label, answer.provenance.reference_ids) == ("White", ["sa.race"])
    assert not provider.asked("equivalent_0")  # exact option
    assert stage_traces(resolver, "eeo_answer")[0]["sub_answer"] == "race_ethnicity"


def test_a_missing_sub_answer_holds_the_race_question_without_a_call(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"wording": ("q0", 0.99), "equivalent_0": ("o1", 0.99)})
    field = eeo_field("Are you Hispanic or Latino?", SemanticType.EEO_RACE_ETHNICITY, "Yes", "No")
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, RACE_ANSWER),
                                         mock_job, field)
    assert len(provider.requests) == 1 and packet.answers == []  # only the classification
    [trace] = stage_traces(resolver, "eeo_answer")
    assert (trace["sub_answer"], trace["status"]) == ("hispanic_latino", "SUB_ANSWER_MISSING")
    [missing] = packet.missing_inputs
    assert missing.reason is MissingReason.EXPLICIT_ANSWER_REQUIRED


def test_the_live_veteran_question_takes_the_veteran_answer_without_any_call(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    # "Veteran Status / If you believe you belong to …" hit 0.92 / 0.84 on the wording gate.
    provider = ChoiceProvider({"wording": ("q0", 0.84)})
    field = eeo_field("Veteran Status", SemanticType.EEO_VETERAN_STATUS, *LIVE_VETERAN_OPTIONS,
                      help_text=VETERAN_HELP)
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, VETERAN_ANSWER,
                                                              *UNTYPED_NOISE), mock_job, field)
    assert len(provider.requests) == 1
    [answer] = packet.answers
    assert (answer.value.label, answer.provenance.reference_ids) == (
        "I am not a protected veteran", ["sa.veteran"])
    assert stage_traces(resolver, "eeo_answer")[0]["status"] == "MAPPED"


@pytest.mark.parametrize("probability,mapped", [(0.97, True), (0.94, False)])
def test_gender_and_disability_answers_map_by_type_through_option_equivalence(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, probability: float, mapped: bool,
) -> None:
    gender = global_answer("sa.gender", "Gender", "Male", semantic=SemanticType.EEO_GENDER)
    provider = ChoiceProvider({"wording": ("q0", 0.99), "equivalent_0": ("o1", probability)})
    fields = [eeo_field("What is your gender identity?", SemanticType.EEO_GENDER, "Woman", "Man",
                        "Non-binary", "Decline to self-identify", required=False),
              eeo_field("Voluntary Self-Identification of Disability", SemanticType.EEO_DISABILITY_STATUS,
                        *DISABILITY_OPTIONS, help_text="Please check one of the boxes below.").model_copy(
                  update={"id": "disability", "selector": "#disability"})]
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, gender,
                                                              DISABILITY_ANSWER), mock_job, *fields)
    assert not provider.asked("wording") and len(provider.asked("equivalent_0")) == 2
    labels = {a.field_id: a.value.label for a in packet.answers}
    assert labels == ({"answer": "Man", "disability": DISABILITY_OPTIONS[1]} if mapped else {})
    traces = {t["field_id"]: t["status"] for t in stage_traces(resolver, "eeo_answer")}
    assert traces == {"answer": "MAPPED" if mapped else "VALUE_DOES_NOT_FIT",
                      "disability": "MAPPED" if mapped else "VALUE_DOES_NOT_FIT"}
    assert not stage_traces(resolver, "question_equivalence")  # settled either way
    assert {t["status"] for t in stage_traces(resolver, "option_equivalence")} == (
        {"MAPPED"} if mapped else {"BELOW_GATE"})


def test_conflicting_same_type_eeo_answers_hold_the_field(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    other = VETERAN_ANSWER.model_copy(update={"id": "sa.veteran2", "question": "Protected veteran status",
                                              "value": "I am a protected veteran"})
    provider = ChoiceProvider({"wording": ("q0", 0.99), "equivalent_0": ("o1", 0.99)})
    field = eeo_field("Protected veteran self-identification", SemanticType.EEO_VETERAN_STATUS,
                      *LIVE_VETERAN_OPTIONS)
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, VETERAN_ANSWER, other),
                                         mock_job, field)
    assert len(provider.requests) == 1 and packet.answers == []
    [trace] = stage_traces(resolver, "eeo_answer")
    assert trace["status"] == "CONFLICT" and set(trace["reference_ids"]) == {"sa.veteran", "sa.veteran2"}


def test_a_newer_eeo_answer_wins_and_a_job_scoped_one_comes_first(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    newer = global_answer("sa.veteran_new", "Veteran Status", "I don't wish to answer",
                          semantic=SemanticType.EEO_VETERAN_STATUS, confirmed_at="2026-09-20T12:00:00Z")
    scoped = SavedAnswer(id="sa.veteran_job", scope=AnswerScope.JOB, job_identity_key=mock_job.identity_key,
                         semantic_type=SemanticType.EEO_VETERAN_STATUS, question="Veteran status at Mock Co",
                         value="I am not a protected veteran", confirmed_at="2026-08-01T12:00:00Z")
    field = eeo_field("Protected veteran self-identification", SemanticType.EEO_VETERAN_STATUS,
                      *LIVE_VETERAN_OPTIONS)
    packet, _, _ = resolve_choice(ChoiceProvider(), with_saved(fictional_candidate, VETERAN_ANSWER, newer),
                                  mock_job, field)
    assert [(a.value.label, a.provenance.reference_ids) for a in packet.answers] == [
        ("I don't wish to answer", ["sa.veteran_new"])]
    packet, _, _ = resolve_choice(ChoiceProvider(), with_saved(fictional_candidate, VETERAN_ANSWER, newer,
                                                               scoped), mock_job, field)
    assert [(a.value.label, a.provenance.reference_ids) for a in packet.answers] == [
        ("I am not a protected veteran", ["sa.veteran_job"])]


def test_without_a_same_type_answer_an_eeo_field_keeps_the_wording_path(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    untyped = global_answer("sa.veteran_untyped", "Are you a protected veteran?", "No")
    provider = ChoiceProvider({"wording": ("q0", 0.99), "equivalent_0": ("o1", 0.99)})
    field = eeo_field("Veteran status", SemanticType.EEO_VETERAN_STATUS, *LIVE_VETERAN_OPTIONS)
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, untyped), mock_job, field)
    assert stage_traces(resolver, "eeo_answer")[0]["status"] == "NONE"
    assert provider.asked("wording")
    [trace] = stage_traces(resolver, "question_equivalence")
    assert (trace["gate"], trace["status"]) == ("standard", "MAPPED")
    assert [a.value.label for a in packet.answers] == ["I am not a protected veteran"]


def test_the_persons_own_eeo_answer_to_this_wording_comes_before_the_type(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    own = global_answer("sa.veteran_own", VETERAN_QUESTION, "I don't wish to answer",
                        semantic=SemanticType.EEO_VETERAN_STATUS, confirmed_at="2026-08-01T12:00:00Z")
    field = eeo_field(VETERAN_QUESTION, SemanticType.EEO_VETERAN_STATUS, *LIVE_VETERAN_OPTIONS)
    packet, _, resolver = resolve_choice(ChoiceProvider(), with_saved(fictional_candidate, VETERAN_ANSWER, own),
                                         mock_job, field)
    assert [(a.value.label, a.provenance.reference_ids) for a in packet.answers] == [
        ("I don't wish to answer", ["sa.veteran_own"])]
    assert not stage_traces(resolver, "eeo_answer")


def test_the_round_9_rule_for_work_authorization_and_sponsorship_is_untouched(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    from interviewmaxxing_browser.ai.routing import EEO_TYPES

    assert {SemanticType.EEO_GENDER, SemanticType.EEO_VETERAN_STATUS,
                         SemanticType.EEO_DISABILITY_STATUS, SemanticType.EEO_RACE_ETHNICITY} == EEO_TYPES
    assert not EEO_TYPES & {SemanticType.WORK_AUTHORIZATION, SemanticType.SPONSORSHIP}
    provider = ChoiceProvider({"wording": ("q0", 0.94)})
    packet, _, resolver = resolve_choice(provider, fictional_candidate, mock_job,
                                         status_field(REWORDED_SPONSORSHIP, SemanticType.SPONSORSHIP))
    [trace] = stage_traces(resolver, "question_equivalence")
    assert (trace["gate"], trace["status"]) == ("standard", "BELOW_GATE") and packet.answers == []
    assert not stage_traces(resolver, "eeo_answer")


# 4. Untyped reusable answers: a pick at 0.90 / 0.85 on a field whose type is not explicit.

@pytest.mark.parametrize("probability,confidence,answered", [
    (0.93, 0.97, True),   # the live travel pick (q8 of 17)
    (0.90, 0.85, True),   # exactly at the gate
    (0.89, 0.97, False),
    (0.90, 0.84, False),
])
def test_an_untyped_pick_on_a_custom_field_passes_at_the_untyped_gate(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
    probability: float, confidence: float, answered: bool,
) -> None:
    assert (UNTYPED_PROBABILITY, UNTYPED_CONFIDENCE) == (0.90, 0.85)
    class WordingOnlyConfidence(ConfirmingProvider):
        def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
            self.confidence = confidence if "wording" in json.loads(body)["questions"] else 0.97
            return super().__call__(url, headers, body, timeout)

    travel = global_answer("sa.travel", "How much are you willing to travel for work?",
                           "Up to 10% of the time")
    provider = WordingOnlyConfidence("travel", pick=probability, confirm=("q0", 0.79),
                                     picks={"equivalent_0": ("o1", 0.98)})
    field = choice_field(TRAVEL, SemanticType.CUSTOM_SELECT, *TRAVEL_OPTIONS, control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, travel, *UNTYPED_NOISE),
                                         mock_job, field)
    [trace] = stage_traces(resolver, "question_equivalence")
    if answered:
        assert (trace["gate"], trace["status"]) == ("untyped", "MAPPED")
        assert len(provider.asked("wording")) == 1  # no confirmation needed
        [answer] = packet.answers
        assert answer.value.label == "Up to 10%"
        assert answer.confidence == pytest.approx(min(probability, confidence))
    else:  # below the untyped gate: the confirmation still runs, and hedges here
        assert (trace["gate"], trace["status"]) == ("confirmed", "BELOW_GATE")
        assert len(provider.asked("wording")) == 2 and packet.answers == []


def test_an_untyped_pick_on_an_explicit_field_keeps_the_standard_gate(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    # No typed salary answer: the untyped answers are offered, and the explicit type keeps 0.95.
    untyped_salary = global_answer("sa.salary_untyped", "What is your desired salary?", "USD 95,000 per year")
    provider = ConfirmingProvider("desired salary", pick=0.93, confirm=("q0", 0.99))
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, untyped_salary,
                                                              *UNTYPED_NOISE), mock_job,
                                         text_field("What salary are you looking for?",
                                                    SemanticType.SALARY_EXPECTATION))
    [trace] = stage_traces(resolver, "question_equivalence")
    assert (trace["gate"], trace["status"]) == ("standard", "BELOW_GATE")
    assert len(provider.asked("wording")) == 1 and packet.answers == []


# 5. The work-location preference.

UPSTART_LABEL = "Location Preference"
UPSTART_OPTIONS = ("Remote", "Hybrid", "On-site")
PREFERENCE = global_answer("sa.work_location", WORK_LOCATION_PREFERENCE_QUESTION, "Remote")


def test_the_live_location_preference_select_takes_the_saved_preference(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    # Upstart: typed LOCATION, so the residence screener read it and held. A single choice
    # among work modes is the work-location preference whatever its type.
    provider = ChoiceProvider({"residence": ("UNKNOWN", 0.99), "wording": ("NONE", 0.99)},
                              semantic="LOCATION")
    field = choice_field(UPSTART_LABEL, SemanticType.LOCATION, *UPSTART_OPTIONS, control=ControlType.SELECT)
    packet, ctx, resolver = resolve_choice(provider, with_saved(fictional_candidate, PREFERENCE,
                                                                *UNTYPED_NOISE), mock_job, field)
    assert ctx.form.fields[0].semantic_type is SemanticType.LOCATION
    assert len(provider.requests) == 1  # "Remote" is an option: no call, and no residence screener
    assert not stage_traces(resolver, "residence_screener")
    [answer] = packet.answers
    assert (answer.value.label, answer.semantic_type) == ("Remote", SemanticType.LOCATION)
    assert (answer.provenance.source, answer.provenance.reference_ids) == (
        AnswerSource.SAVED_ANSWER, ["sa.work_location"])
    assert "saved work-location preference" in (answer.provenance.note or "")
    [trace] = stage_traces(resolver, "work_location_preference")
    assert (trace["status"], trace["option_count"]) == ("MAPPED", 3)
    assert "Remote" not in json.dumps(trace)


@pytest.mark.parametrize("semantic", ["CUSTOM_SELECT", "LOCATION"])
def test_a_work_mode_select_with_its_own_wording_maps_the_preference_through_option_equivalence(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, semantic: str,
) -> None:
    provider = ChoiceProvider({"equivalent_0": ("o0", 0.97), "wording": ("NONE", 0.99)}, semantic=semantic)
    field = choice_field("Which work arrangement do you prefer?", SemanticType[semantic],
                         "Fully remote", "Hybrid (2-3 days in office)", "In-office", "No preference",
                         control=ControlType.RADIO)
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, PREFERENCE,
                                                              *UNTYPED_NOISE), mock_job, field)
    assert not provider.asked("wording")
    [request] = provider.asked("equivalent_0")
    assert request["state"]["stored_answers"] == {"equivalent_0": "Remote"}
    [answer] = packet.answers
    assert (answer.value.label, answer.provenance.reference_ids) == ("Fully remote", ["sa.work_location"])
    assert stage_traces(resolver, "work_location_preference")[0]["status"] == "MAPPED"


def test_without_a_saved_preference_a_work_mode_select_holds_without_a_call(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    provider = ChoiceProvider({"residence": ("o0", 0.99), "wording": ("q0", 0.99)}, semantic="LOCATION")
    field = choice_field(UPSTART_LABEL, SemanticType.LOCATION, *UPSTART_OPTIONS, control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, *UNTYPED_NOISE),
                                         mock_job, field)
    assert packet.answers == []  # the address never answers it, and nothing asks Jev about it
    assert not any(name in request["questions"] for request in provider.requests
                   for name in ("residence", "equivalent_0", "wording"))
    assert not stage_traces(resolver, "residence_screener")
    assert stage_traces(resolver, "work_location_preference")[0]["status"] == "NONE"
    [missing] = packet.missing_inputs
    assert missing.field_id == "answer"


@pytest.mark.parametrize("label,options", [
    ("Location Preference", ("Remote", "Austin, TX")),  # a place is not a work mode
    ("What is your current work arrangement?", UPSTART_OPTIONS),  # a fact, not a preference
    ("Location Preference", ("Remote",)),  # one option is no choice
])
def test_only_a_preference_among_work_modes_takes_the_saved_preference(
    fictional_candidate: CandidateProfile, mock_job: JobRecord, label: str, options: tuple[str, ...],
) -> None:
    provider = ChoiceProvider({"residence": ("NOT_RESIDENCE", 0.99), "wording": ("NONE", 0.99),
                               "equivalent_0": ("NONE", 0.99)}, semantic="CUSTOM_SELECT")
    field = choice_field(label, SemanticType.CUSTOM_SELECT, *options, control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(provider, with_saved(fictional_candidate, PREFERENCE), mock_job, field)
    assert not stage_traces(resolver, "work_location_preference") and packet.answers == []


def test_the_persons_own_answer_to_the_preference_wording_comes_first(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    own = global_answer("sa.own_pref", UPSTART_LABEL, "Hybrid", confirmed_at="2026-08-01T12:00:00Z")
    field = choice_field(UPSTART_LABEL, SemanticType.CUSTOM_SELECT, *UPSTART_OPTIONS, control=ControlType.SELECT)
    packet, _, resolver = resolve_choice(ChoiceProvider(), with_saved(fictional_candidate, PREFERENCE, own),
                                         mock_job, field)
    assert [(a.value.label, a.provenance.reference_ids) for a in packet.answers] == [("Hybrid", ["sa.own_pref"])]
    assert not stage_traces(resolver, "work_location_preference")


def test_the_imported_preference_answers_the_live_select(
    fictional_candidate: CandidateProfile, mock_job: JobRecord,
) -> None:
    from datetime import UTC, datetime

    from interviewmaxxing_candidate.simple_answers import SimpleAnswers

    data = SimpleAnswers.from_identity(fictional_candidate.identity).model_dump()
    data["work_location_preference"] = "Remote or hybrid"
    updates = SimpleAnswers.model_validate(data).saved_answer_updates(
        confirmed_at=datetime(2026, 9, 25, tzinfo=UTC))
    [saved] = updates
    assert (saved.semantic_type, saved.question) == (None, WORK_LOCATION_PREFERENCE_QUESTION)
    assert UPSTART_LABEL in saved.match_phrases
    provider = ChoiceProvider({"equivalent_0": ("o0", 0.96)}, semantic="LOCATION")
    field = choice_field("Preferred work arrangement", SemanticType.LOCATION, *UPSTART_OPTIONS,
                         control=ControlType.SELECT)
    packet, _, _ = resolve_choice(provider, with_saved(fictional_candidate, saved), mock_job, field)
    [answer] = packet.answers
    assert (answer.value.label, answer.provenance.reference_ids) == ("Remote", [saved.id])
    [request] = provider.asked("equivalent_0")
    assert request["state"]["stored_answers"] == {"equivalent_0": "Remote or hybrid"}
