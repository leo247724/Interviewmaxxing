"""Stories in the writer's evidence, grounding of story citations, story-versus-fact
consistency, the no-AI-slop rewrite and per-purpose effort. Fictional fixtures only;
every provider is scripted."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser import ai as ai_package
from interviewmaxxing_browser.ai import (
    AIFormRouter,
    AIHold,
    BoundedDecisions,
    DynamicPacketResolver,
    build_ai_runtime,
)
from interviewmaxxing_browser.ai.humanize import MAX_REWRITES, check_rewrite, lint
from interviewmaxxing_browser.ai.providers import CallBudget, NarrativeDraft, NarrativeWriter
from interviewmaxxing_core import (
    AnswerSource,
    Application,
    ApplicationField,
    ApplicationForm,
    ApplicationState,
    CandidateFact,
    CandidateProfile,
    ControlType,
    JobRecord,
    PacketContext,
    SemanticType,
    TextValue,
)
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient

MODEL = "anthropic/claude-opus-5.5"
QUESTION = "Describe a paid media campaign you led and what it achieved."
STORY_TEXT = ("I managed a $120,000 annual paid search budget for a regional bakery chain in 2024 "
              "and grew online orders by 35%. I set up conversion tracking in Google Ads so the "
              "owner could see which campaigns paid for themselves.")
FACT_TEXT = "Managed paid search for a regional bakery chain and grew online orders by 35% (2024)."
OTHER_FACT = "Wrote weekly reports for two store managers."


def story_chunk(text: str = STORY_TEXT, *, title: str = "Paid search for a regional bakery chain",
                employer: str | None = "regional bakery chain", period: str | None = "2024",
                score: float = 0.03) -> dict[str, Any]:
    header = f"Story 01: {title}" + (f" | employer: {employer}" if employer else "") \
        + (f" | period: {period}" if period else "") + " | themes: budgets, results"
    body = header + "\n" + text
    return {"id": "story:" + hashlib.sha256(body.encode()).hexdigest(), "text": body,
            "story_id": "26787ad7044bdb88", "title": title, "employer": employer, "period": period,
            "themes": ["budgets", "results"], "source_version": "c" * 64, "score": score, "rank": 1}


class Jev:
    """A scripted Jev: routes the field to the writer and answers the grounding,
    consistency and story-consistency nouls with the configured scores."""

    def __init__(self, *, semantic: str = "CUSTOM_LONG_TEXT", support: Any = 1.0,
                 complete: float = 1.0, consistency: float = 1.0, story: float = 1.0,
                 scope: str = "HISTORICAL_OR_CONTEXTUAL", scope_probability: float = 1.0,
                 link: tuple[str, float, float] | None = None) -> None:
        self.semantic, self.support, self.complete = semantic, support, complete
        self.consistency, self.story = consistency, story
        self.scope, self.scope_probability, self.link = scope, scope_probability, link
        self.requests: list[dict[str, Any]] = []

    def grounding_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if "sentences" in r["state"]]

    def story_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if "story_chunks" in r["state"]]

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        self.requests.append(request)
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            if question["type"] == "choice":
                if name == "role" and self.link is not None:
                    choice = self.link[0]
                elif name.startswith("r") and name != "route":
                    choice = "WRITER"
                elif name.startswith("n"):
                    choice = "prose"
                elif name.startswith("u"):
                    choice = self.scope
                elif name.startswith("s"):
                    choice = self.semantic
                elif name.startswith("d"):
                    choice = "APPLICATION_ATTACHMENT"
                else:
                    choice = next(iter(question["criteria"]))
                answers[name] = {"type": "choice", "choice": choice, "confidence": 1,
                    "probabilities": {option: float(option == choice) for option in question["criteria"]}}
                if name.startswith("u") and self.scope_probability < 1:
                    others = max(len(question["criteria"]) - 1, 1)
                    answers[name]["confidence"] = self.scope_probability
                    answers[name]["probabilities"] = {
                        option: self.scope_probability if option == choice
                        else (1 - self.scope_probability) / others for option in question["criteria"]}
                if name == "role" and self.link is not None:
                    _, confidence, probability = self.link
                    others = max(len(question["criteria"]) - 1, 1)
                    answers[name]["confidence"] = confidence
                    answers[name]["probabilities"] = {
                        option: probability if option == choice else (1 - probability) / others
                        for option in question["criteria"]}
            else:
                state = request["state"]
                if "sentences" in state:
                    if name == "complete":
                        score = self.complete
                    elif callable(self.support):
                        index = int(name[1:])
                        score = self.support(index, state["sentences"][f"s{index}"]["text"], len(self.grounding_requests()))
                    else:
                        score = self.support
                elif "story_chunks" in state:
                    score = self.story
                elif "canonical_alternatives" in state:
                    score = self.consistency
                else:
                    score = 1.0
                answers[name] = {"type": "noul", "noul": score}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())


@dataclass
class Retriever:
    facts: list[CandidateFact]
    story_chunks: list[dict[str, Any]] = field(default_factory=list)
    job_evidence: list[dict[str, str]] = field(default_factory=list)
    voice_samples: list[str] = field(default_factory=lambda: ["I write short, plain sentences."])
    calls: list[dict[str, Any]] = field(default_factory=list)

    def retrieve(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(facts=self.facts, job_evidence=self.job_evidence,
                               voice_samples=self.voice_samples, story_chunks=self.story_chunks,
                               receipt={"status": "OK", "story_ids": [c["id"] for c in self.story_chunks]})


@dataclass
class Writer:
    sentences: list[dict[str, Any]]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def write(self, **kwargs: Any) -> NarrativeDraft:
        self.calls.append(kwargs)
        return NarrativeDraft.model_validate({"status": "READY", "sentences": self.sentences,
                                              "missing_information": []})


class Transport:
    """The OpenRouter transport of a real ``NarrativeWriter``: answers by the request's
    role (write, humanize or review) from scripted queues; ``None`` is an HTTP 500."""

    def __init__(self, *, write: list[Any], humanize: list[Any] | None = None,
                 review: list[Any] | None = None) -> None:
        self.queues = {"write": list(write), "humanize": list(humanize or []), "review": list(review or [])}
        self.requests: list[dict[str, Any]] = []
        self.roles: list[str] = []

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        system = request["messages"][0]["content"]
        role = ("humanize" if system.startswith("You are a sharp human editor") else
                "review" if system.startswith("You are an independent evidence reviewer") else "write")
        self.requests.append(request)
        self.roles.append(role)
        queue = self.queues[role]
        payload = queue.pop(0) if len(queue) > 1 else queue[0]
        if payload is None:
            return HttpResponse(500, {}, b"{}")
        return HttpResponse(200, {}, json.dumps({"model": MODEL, "usage": {"cost": 0.01}, "choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps(payload)}}]}).encode())


def fact(candidate: CandidateProfile, value: str, *, fid: str, key: str = "experience",
         source: str | None = None) -> CandidateFact:
    base = candidate.verified_facts()[0].model_copy(update={"id": fid, "key": key, "value": value,
                                                            "evidence": [value]})
    return base.model_copy(update={"source": source}) if source else base


def context(candidate: CandidateProfile, job: JobRecord, *, question: str = QUESTION,
            semantic: SemanticType = SemanticType.CUSTOM_LONG_TEXT, max_length: int | None = None) -> PacketContext:
    form = ApplicationForm(url="https://synthetic.test/apply", fields=[ApplicationField(
        id="response", label=question, selector="#response", semantic_type=semantic,
        control_type=ControlType.TEXTAREA, required=True, max_length=max_length)])
    application = Application(id="app-stories", request_id="request-stories", job_id=job.id,
        candidate_id=candidate.id, state=ApplicationState.INSPECTING, version=1,
        created_at="2026-09-24T00:00:00Z", updated_at="2026-09-24T00:00:00Z")
    return PacketContext(form=form, application=application, candidate=candidate, job=job)


def resolve(ctx: PacketContext, retriever: Retriever, writer: Any, jev: Jev, *,
            humanize: bool = False) -> tuple[Any, DynamicPacketResolver, PacketContext]:
    budget = writer.budget if isinstance(writer, NarrativeWriter) else CallBudget()
    decisions = BoundedDecisions(JevClient(ApiKey("synthetic-test-key", source="test"),
                                          transport=jev, max_attempts=1), budget)
    router = AIFormRouter(decisions)
    ctx = replace(ctx, form=router.annotate(ctx.form, document_id="synthetic-stories"))
    resolver = DynamicPacketResolver(decisions, writer, router=router, retriever=retriever,
                                     humanize=humanize)
    return asyncio.run(resolver.resolve(ctx)), resolver, ctx


def held(packet: Any, ctx: PacketContext) -> bool:
    return not packet.answers and any(m.field_id == "response" for m in packet.missing_inputs)


@pytest.fixture
def candidate(fictional_candidate: CandidateProfile) -> CandidateProfile:
    facts = [fact(fictional_candidate, FACT_TEXT, fid="fact.bakery"),
             fact(fictional_candidate, OTHER_FACT, fid="fact.reports")]
    return fictional_candidate.model_copy(update={"facts": facts, "experience": [], "education": []})


# --- retrieval and evidence -------------------------------------------------------------------


def test_writer_evidence_carries_story_ids_and_provenance_keeps_fact_ids(candidate, mock_job):
    chunk = story_chunk()
    retriever = Retriever([candidate.facts[0]], [chunk])
    writer = Writer([
        {"text": "I managed paid search for a regional bakery chain and grew online orders by 35%.",
         "fact_ids": ["fact.bakery"]},
        {"text": "I set up conversion tracking so the owner could see which campaigns paid off.",
         "fact_ids": [chunk["id"]]},
    ])
    jev = Jev()
    packet, resolver, ctx = resolve(context(candidate, mock_job), retriever, writer, jev)
    assert packet.is_complete and ctx.problems(packet) == []
    assert retriever.calls[0]["narrative"] is True and retriever.calls[0]["query"] == QUESTION
    evidence = writer.calls[0]["facts"]
    assert [item["id"] for item in evidence] == ["fact.bakery", chunk["id"]]
    assert evidence[1] == {"id": chunk["id"], "key": "story", "value": chunk["text"],
                           "story": {"title": chunk["title"], "employer": "regional bakery chain", "period": "2024"}}
    answer = packet.answers[0]
    assert answer.provenance.source is AnswerSource.GENERATED_FROM_FACTS
    assert answer.provenance.reference_ids == ["fact.bakery"]
    assert chunk["id"] in answer.provenance.note and "story evidence" in answer.provenance.note
    receipt = resolver.retrieval_receipts[0]
    assert receipt["story_ids"] == [chunk["id"]] and receipt["story_scores"] == {chunk["id"]: 0.03}
    assert receipt["narrative"] is True and "c" * 64 in receipt["source_versions"]
    draft = next(t for t in resolver.narrative_traces if t["stage"] == "draft")
    assert draft["story_ids"] == [chunk["id"]] and draft["status"] == "READY"
    [grounding] = jev.grounding_requests()
    cited = grounding["state"]["sentences"]["s1"]["facts"]
    assert [item["id"] for item in cited] == [chunk["id"]] and cited[0]["key"] == "story"
    assert cited[0]["value"] == chunk["text"] and cited[0]["source"] == "user:story"
    assert not any(chunk["text"] in json.dumps(t) for t in resolver.narrative_traces)
    assert not any(chunk["text"] in json.dumps(r) for r in resolver.retrieval_receipts)


def test_story_consistency_compares_related_facts_and_a_contradiction_holds(candidate, mock_job):
    chunk = story_chunk()
    derived = fact(candidate, "I managed a $120,000 annual paid search budget for a regional bakery chain in 2024.",
                   fid="sf_derived", key="achievement", source=chunk["id"])
    money = fact(candidate, "Managed a $40,000 paid search budget for a florist in 2022.", fid="fact.florist")
    profile = candidate.model_copy(update={"facts": [*candidate.facts, derived, money]})
    retriever = Retriever([profile.facts[0]], [chunk])
    writer = Writer([{"text": "I grew online orders by 35%.", "fact_ids": ["fact.bakery", chunk["id"]]}])
    jev = Jev()
    packet, resolver, ctx = resolve(context(profile, mock_job), retriever, writer, jev)
    assert packet.is_complete
    [request] = jev.story_requests()
    assert request["state"]["prompt_version"] == "story-evidence-v1"
    assert set(request["state"]["story_chunks"]) == {"c0"}
    compared = request["state"]["comparison_ids"]["c0"]
    assert "fact.florist" in compared and "sf_derived" not in compared and "fact.reports" not in compared
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "story_consistency")
    assert trace["status"] == "CONSISTENT" and trace["story_ids"] == {"c0": chunk["id"]}
    assert set(trace) == {"stage", "prompt_version", "story_ids", "comparison_ids", "probabilities", "cached", "status"}

    contradicted = Jev(story=0.2)
    packet, resolver, ctx = resolve(context(profile, mock_job), Retriever([profile.facts[0]], [chunk]),
                                    Writer(writer.sentences), contradicted)
    assert held(packet, ctx)
    assert next(t for t in resolver.narrative_traces if t["stage"] == "story_consistency")["status"] == "HELD"
    assert not contradicted.grounding_requests()


def test_story_verdicts_are_cached_per_runtime(candidate, mock_job):
    chunk = story_chunk()
    money = fact(candidate, "Managed a $40,000 paid search budget for a florist in 2022.", fid="fact.florist")
    profile = candidate.model_copy(update={"facts": [*candidate.facts, money]})
    jev = Jev()
    decisions = BoundedDecisions(JevClient(ApiKey("synthetic-test-key", source="test"), transport=jev, max_attempts=1))
    router = AIFormRouter(decisions)
    writer = Writer([{"text": "I grew online orders by 35%.", "fact_ids": ["fact.bakery"]}])
    resolver = DynamicPacketResolver(decisions, writer, router=router,
                                     retriever=Retriever([profile.facts[0]], [chunk]), humanize=False)
    for _ in range(2):
        ctx = context(profile, mock_job)
        ctx = replace(ctx, form=router.annotate(ctx.form, document_id="synthetic-stories"))
        packet = asyncio.run(resolver.resolve(ctx))
        assert packet.is_complete
    assert len(jev.story_requests()) == 1
    traces = [t for t in resolver.narrative_traces if t["stage"] == "story_consistency"]
    assert [t["cached"] for t in traces] == [[], ["c0"]]


def test_unsupported_claim_citing_a_story_holds_the_field(candidate, mock_job):
    chunk = story_chunk()
    writer = Writer([
        {"text": "I grew online orders by 35%.", "fact_ids": ["fact.bakery"]},
        {"text": "I also doubled the chain's revenue.", "fact_ids": [chunk["id"]]},
    ])
    jev = Jev(support=lambda index, text, _: 0.02 if "doubled" in text else 1.0)
    packet, resolver, ctx = resolve(context(candidate, mock_job), Retriever([candidate.facts[0]], [chunk]), writer, jev)
    assert held(packet, ctx)
    assert next(t for t in resolver.narrative_traces if t["stage"] == "draft")["status"] == "GROUNDING_REJECTED"


def test_a_draft_citing_only_stories_needs_a_verified_fact(candidate, mock_job):
    chunk = story_chunk()
    writer = Writer([{"text": "I grew online orders by 35%.", "fact_ids": [chunk["id"]]}])
    packet, _, ctx = resolve(context(candidate, mock_job), Retriever([candidate.facts[0]], [chunk]), writer, Jev())
    assert held(packet, ctx)


@pytest.mark.parametrize("change", ["hash", "id", "fact_collision", "title", "count", "score"])
def test_malformed_story_evidence_holds(candidate, mock_job, change):
    chunk = story_chunk()
    if change == "hash":
        chunk["text"] += " tampered"
    elif change == "id":
        chunk["id"] = "story:not-a-digest"
    elif change == "fact_collision":
        chunk["id"] = "fact.bakery"
    elif change == "title":
        chunk["title"] = ""
    elif change == "score":
        chunk["score"] = float("nan")
    chunks = [chunk] if change != "count" else [story_chunk(f"Chunk {i} of a story." * 3) for i in range(5)]
    writer = Writer([{"text": "I grew online orders by 35%.", "fact_ids": ["fact.bakery"]}])
    packet, _, ctx = resolve(context(candidate, mock_job), Retriever([candidate.facts[0]], chunks), writer, Jev())
    assert held(packet, ctx) and not writer.calls


def test_non_narrative_retrieval_rejects_story_evidence(candidate, mock_job):
    decisions = BoundedDecisions(JevClient(ApiKey("synthetic-test-key", source="test"), transport=Jev(), max_attempts=1))
    resolver = DynamicPacketResolver(decisions, Writer([]), retriever=Retriever([candidate.facts[0]], [story_chunk()]))
    ctx = context(candidate, mock_job, question="First name")
    with pytest.raises(AIHold, match="non-narrative"):
        resolver._retrieve(ctx, ctx.form.fields[0])
    retriever = Retriever([candidate.facts[0]])
    resolver = DynamicPacketResolver(decisions, Writer([]), retriever=retriever)
    result = resolver._retrieve(ctx, ctx.form.fields[0])
    assert result.facts == [candidate.facts[0]] and retriever.calls[0]["narrative"] is False
    assert resolver.retrieval_receipts[-1]["story_ids"] == [] and resolver.retrieval_receipts[-1]["narrative"] is False


# --- the no-AI-slop pass ------------------------------------------------------------------------


SLOPPY = [
    {"text": "Here's the thing: I leverage paid search to drive results.", "fact_ids": ["fact.bakery"]},
    {"text": "It's not about spend. It's about tracking.", "fact_ids": ["fact.bakery"]},
    {"text": "I grew online orders by 35% for a regional bakery chain in 2024.", "fact_ids": ["fact.bakery"]},
]
CLEAN = [
    {"text": "I ran paid search for a regional bakery chain in 2024.", "fact_ids": ["fact.bakery"]},
    {"text": "Tracking mattered more than spend, so I set it up first.", "fact_ids": ["fact.bakery"]},
    {"text": "Online orders grew by 35%.", "fact_ids": ["fact.bakery"]},
]


def ready(sentences: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "READY", "missing_information": [], "sentences": sentences}


def real_writer(transport: Transport, *, budget: CallBudget | None = None, effort: str | None = "high") -> NarrativeWriter:
    return NarrativeWriter(ApiKey("synthetic-writer-key", source="test"), MODEL, budget or CallBudget(),
                           transport=transport, narrative_effort=effort)  # type: ignore[arg-type]


def test_lint_names_the_banned_constructions() -> None:
    text = ("Here's the thing: I leverage paid search. It's not about spend. It's about tracking. "
            "Experts agree this marks a pivotal moment, underscoring the importance of data. "
            "What nobody tells you is simple. In terms of results, orders grew — a lot — and "
            "the numbers show it, highlighting my impact. In conclusion, that's the whole game.")
    names = {finding.pattern for finding in lint(text)}
    assert {"throat_clearing", "banned_word", "binary_contrast", "weasel_attribution", "importance_puffery",
            "faux_insight", "empty_phrase", "em_dashes", "superficial_analysis", "summary_recap",
            "fake_profundity"} <= names
    assert "colon_reveal" in {f.pattern for f in lint("The best part: it learns from every click.")}
    assert lint("I ran paid search for a bakery chain in 2024. Online orders grew by 35%.") == []
    assert all("text" not in finding.__dict__ for finding in lint(text))


def test_humanizer_rewrites_the_draft_keeps_citations_and_grounds_again(candidate, mock_job):
    transport = Transport(write=[ready(SLOPPY)], humanize=[ready(CLEAN)])
    budget = CallBudget()
    jev = Jev()
    packet, resolver, ctx = resolve(context(candidate, mock_job), Retriever([candidate.facts[0]]),
                                    real_writer(transport, budget=budget), jev, humanize=True)
    assert packet.is_complete and ctx.problems(packet) == []
    value = packet.answers[0].value
    assert isinstance(value, TextValue) and value.text == NarrativeDraft.model_validate(ready(CLEAN)).text
    assert lint(value.text) == []
    assert transport.roles == ["write", "humanize"]
    rewrite = transport.requests[1]
    assert rewrite["reasoning"] == {"effort": "high"} and rewrite["response_format"]["json_schema"]["strict"]
    user = json.loads(rewrite["messages"][1]["content"])
    assert user["purpose"] == "answer" and user["voice_samples"] == ["I write short, plain sentences."]
    assert {f["pattern"] for f in user["findings"]} >= {"throat_clearing", "banned_word", "binary_contrast"}
    assert "facts" not in user and user["draft"]["sentences"][0]["fact_ids"] == ["fact.bakery"]
    assert len(jev.grounding_requests()) == 2
    assert jev.grounding_requests()[1]["state"]["answer"] == value.text
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "humanize")
    assert trace["status"] == "REWRITTEN" and trace["lint_after"] == []
    assert {f["pattern"] for f in trace["lint_before"]} >= {"throat_clearing", "banned_word"}
    assert trace["attempts"][0]["status"] == "REWRITTEN" and trace["attempts"][0]["grounding"]
    assert "leverage" not in json.dumps(trace) and "bakery" not in json.dumps(trace)
    assert [r.purpose for r in budget.receipts if r.model == MODEL] == ["narrative", "humanize"]
    assert [r.requested_reasoning_effort for r in budget.receipts if r.model == MODEL] == ["high", "high"]
    assert packet.answers[0].provenance.reference_ids == ["fact.bakery"]


@pytest.mark.parametrize("failure", ["dropped_citation", "new_number", "unknown_citation", "grounding", "transport"])
def test_a_failed_rewrite_keeps_the_grounded_draft(candidate, mock_job, failure):
    rewritten = [dict(s) for s in CLEAN]
    jev = Jev()
    if failure == "dropped_citation":
        rewritten[0]["fact_ids"] = []
        rewritten[1]["fact_ids"] = []
        rewritten[2]["fact_ids"] = []
    elif failure == "new_number":
        rewritten[2]["text"] = "Online orders grew by 35% to 4,000 a month."
    elif failure == "unknown_citation":
        rewritten[0]["fact_ids"] = ["fact.invented"]
    elif failure == "grounding":
        jev = Jev(support=lambda index, text, requests: 0.02 if requests >= 2 else 1.0)
    transport = Transport(write=[ready(SLOPPY)], humanize=[None if failure == "transport" else ready(rewritten)])
    packet, resolver, _ctx = resolve(context(candidate, mock_job), Retriever([candidate.facts[0]]),
                                    real_writer(transport), jev, humanize=True)
    assert packet.is_complete
    value = packet.answers[0].value
    assert isinstance(value, TextValue) and value.text == NarrativeDraft.model_validate(ready(SLOPPY)).text
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "humanize")
    assert trace["status"] == "KEPT_ORIGINAL" and len(trace["attempts"]) == 1
    expected = {"dropped_citation": "REJECTED_DROPPED_CITATION", "new_number": "REJECTED_NEW_NUMBER",
                "unknown_citation": "REJECTED_UNKNOWN_CITATION", "grounding": "GROUNDING_REJECTED",
                "transport": "HELD"}[failure]
    assert trace["attempts"][0]["status"] == expected
    assert transport.roles == ["write", "humanize"]


def test_a_residual_pattern_triggers_one_more_rewrite_at_most(candidate, mock_job):
    residual = [dict(s) for s in CLEAN]
    residual[0]["text"] = "I leverage paid search for a regional bakery chain in 2024."
    still = [dict(s) for s in CLEAN]
    still[1]["text"] = "Here's the thing: tracking mattered more than spend."
    transport = Transport(write=[ready(SLOPPY)], humanize=[ready(residual), ready(still)])
    packet, resolver, _ctx = resolve(context(candidate, mock_job), Retriever([candidate.facts[0]]),
                                    real_writer(transport), Jev(), humanize=True)
    assert packet.is_complete
    assert transport.roles == ["write", "humanize", "humanize"] and MAX_REWRITES == 2
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "humanize")
    assert [a["status"] for a in trace["attempts"]] == ["REWRITTEN", "REWRITTEN"]
    assert trace["attempts"][1]["findings"] == [{"pattern": "banned_word", "count": 1}]
    assert {f["pattern"] for f in trace["lint_after"]} == {"throat_clearing", "colon_reveal"}
    value = packet.answers[0].value
    assert isinstance(value, TextValue) and value.text == NarrativeDraft.model_validate(ready(still)).text
    second = json.loads(transport.requests[2]["messages"][1]["content"])
    assert second["attempt"] == 2 and second["draft"]["text"] == NarrativeDraft.model_validate(ready(residual)).text


def test_rewrite_checks_are_deterministic() -> None:
    original = NarrativeDraft.model_validate(ready(SLOPPY))
    clean = NarrativeDraft.model_validate(ready(CLEAN))
    ok = check_rewrite(original, clean, purpose="answer", supplied_ids={"fact.bakery"}, job_ids=set(), max_length=None)
    assert ok is None
    short = NarrativeDraft.model_validate(ready([CLEAN[0]]))
    assert check_rewrite(original, short, purpose="answer", supplied_ids={"fact.bakery"}, job_ids=set(), max_length=None) == "length_drift"
    assert check_rewrite(original, clean, purpose="answer", supplied_ids={"fact.bakery"}, job_ids=set(), max_length=40) == "field_length"
    assert check_rewrite(original, clean, purpose="cover_letter", supplied_ids={"fact.bakery"}, job_ids=set(), max_length=None) == "letter_shape"


def test_humanizer_can_be_switched_off(candidate, mock_job):
    transport = Transport(write=[ready(SLOPPY)], humanize=[ready(CLEAN)])
    packet, resolver, _ = resolve(context(candidate, mock_job), Retriever([candidate.facts[0]]),
                                  real_writer(transport), Jev(), humanize=False)
    assert packet.is_complete and transport.roles == ["write"]
    assert not any(t["stage"] == "humanize" for t in resolver.narrative_traces)


# --- effort by purpose --------------------------------------------------------------------------


def test_effort_follows_the_purpose_and_is_recorded(candidate, mock_job, monkeypatch):
    transport = Transport(write=[ready(SLOPPY)], humanize=[ready(CLEAN)],
                          review=[{"verdict": "SUPPORTED", "issues": [], "reference_ids": []}])
    budget = CallBudget()
    writer = real_writer(transport, budget=budget, effort="high")
    assert (writer.effort_for("answer"), writer.effort_for("cover_letter"), writer.effort_for("humanize"),
            writer.effort_for("draft_grounding"), writer.effort_for("evidence_consistency")) == (
        "high", "high", "high", "low", "low")
    jev = Jev(support=0.9)  # uncertain grounding: the low-effort Opus review runs each time
    packet, resolver, _ = resolve(context(candidate, mock_job), Retriever([candidate.facts[0]]), writer, jev, humanize=True)
    assert packet.is_complete
    efforts = {role: request["reasoning"]["effort"] for role, request in zip(transport.roles, transport.requests, strict=True)}
    assert efforts == {"write": "high", "humanize": "high", "review": "low"}
    usage = resolver.provider_usage()
    assert usage["reasoning_effort"] == {"humanize": "high", "narrative": "high", "opus_draft_grounding": "low"}
    assert usage["writer_effort"] == {"narrative": "high", "review": "low"}
    assert {r.purpose: r.requested_reasoning_effort for r in budget.receipts if r.model == MODEL} == {
        "narrative": "high", "opus_draft_grounding": "low", "humanize": "high"}

    monkeypatch.setattr(ai_package, "load_api_key", lambda **kwargs: ApiKey("synthetic-key", source="test"))
    _, default = build_ai_runtime(env_file=__import__("pathlib").Path("/tmp/synthetic.env"), writer_model=MODEL)
    assert default.writer is not None and default.writer.effort_for("cover_letter") == "high"
    assert default.writer.effort_for("draft_grounding") == "low"
    _, medium = build_ai_runtime(env_file=__import__("pathlib").Path("/tmp/synthetic.env"), writer_model=MODEL,
                                 writer_effort="medium")
    assert medium.writer is not None and medium.writer.effort_for("answer") == "medium"
    plain = NarrativeWriter(ApiKey("synthetic-key", source="test"), MODEL, CallBudget(), transport=transport)
    assert plain.effort_for("answer") == "low"  # a bare writer keeps its base effort
    with pytest.raises(ValueError, match="narrative effort"):
        NarrativeWriter(ApiKey("synthetic-key", source="test"), MODEL, CallBudget(), transport=transport,
                        narrative_effort="extreme")  # type: ignore[arg-type]


# --- round 2: resume role links and motivation narratives -------------------------------------


def _story_and_analysis() -> tuple[Any, Any]:
    from interviewmaxxing_generation.knowledge import stories as st

    runs = (st.Run("Stories 01 - Paid search for a regional bakery chain", True),
            st.Run("I managed a $120,000 annual paid search budget for a regional bakery chain in 2024 "
                   "and grew online orders by 35%. As the marketing manager I set up conversion tracking "
                   "in Google Ads. My team of 2 coordinators reported to me.", False))
    story = st.parse_stories([st.Paragraph(runs)])[0]
    return story, st.analyse_story(story)


def _roles() -> list[Any]:
    from interviewmaxxing_generation.knowledge.stories import ResumeRole

    return [ResumeRole("exp_old", "Glaze Agency", "PPC Specialist", "2021-01", "2023-03", False,
                       ("Ran paid search for auto repair shops.",)),
            ResumeRole("exp_bakery", "Crumb & Co. Bakeries", "Marketing Manager", "2023-04", "2024-09",
                       False, ("Managed paid search for twelve stores; online orders up 35%.",))]


def _decide(jev: Jev) -> Any:
    return BoundedDecisions(JevClient(ApiKey("synthetic-test-key", source="test"), transport=jev, max_attempts=1))


def test_a_story_links_to_a_resume_role_only_through_the_gates() -> None:
    from interviewmaxxing_browser.ai.stories import link_story_to_role

    story, analysis = _story_and_analysis()
    jev = Jev(link=("r1", 0.93, 0.97))
    decisions = _decide(jev)
    link, trace = link_story_to_role(story=story, analysis=analysis, roles=_roles(),
                                     decide=decisions.decide, model=decisions.model)
    assert link is not None and (link.resume_role_id, link.method) == ("exp_bakery", "jev_match")
    assert (link.company, link.start, link.end, link.confidence, link.probability) == (
        "Crumb & Co. Bakeries", "2023-04", "2024-09", 0.93, 0.97)
    assert trace["status"] == "LINKED" and trace["resume_role_id"] == "exp_bakery"
    assert trace["choice"] == "r1" and trace["role_ids"] == ["exp_old", "exp_bakery"]
    assert "Crumb" not in json.dumps(trace) and "regional bakery chain" not in json.dumps(trace)
    [request] = jev.requests
    assert request["state"]["prompt_version"] == "story-role-link-v1"
    assert set(request["questions"]["role"]["criteria"]) == {"r0", "r1", "NONE"}
    assert request["state"]["resume_roles"]["r1"]["company"] == "Crumb & Co. Bakeries"
    assert request["state"]["story"]["years_stated"] == ["2024"]
    for scores, status in ((("r1", 0.93, 0.9), "UNLINKED"), (("r1", 0.8, 0.99), "UNLINKED"),
                           (("NONE", 1.0, 1.0), "UNLINKED")):
        decisions = _decide(Jev(link=scores))
        link, trace = link_story_to_role(story=story, analysis=analysis, roles=_roles(),
                                         decide=decisions.decide, model=decisions.model)
        assert link is None and trace["status"] == status
    link, trace = link_story_to_role(story=story, analysis=analysis, roles=[],
                                     decide=decisions.decide, model=decisions.model)
    assert link is None and trace["status"] == "NO_ROLES"

    class Refusing:
        def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
            return HttpResponse(500, {}, b"{}")

    decisions = BoundedDecisions(JevClient(ApiKey("synthetic-test-key", source="test"), transport=Refusing(), max_attempts=1))
    link, trace = link_story_to_role(story=story, analysis=analysis, roles=_roles(),
                                     decide=decisions.decide, model=decisions.model)
    assert link is None and trace["status"] == "HELD"


INTEREST = "What interests you about Pacvue?"
JOB_EVIDENCE = {"id": "job:" + "a" * 64, "source_url": "https://synthetic.test/jobs/1", "source_version": "b" * 64,
                "text": "Own paid search and retail media strategy for enterprise brands and report results to the sales team."}


def test_interest_questions_are_cover_letter_narratives_despite_an_explicit_scope(candidate, mock_job):
    chunk = story_chunk()
    jev = Jev(scope="EXPLICIT_ANSWER", scope_probability=0.78)
    retriever = Retriever([candidate.facts[0]], [chunk], job_evidence=[JOB_EVIDENCE])
    writer = Writer([
        {"text": "Your description puts paid search at the center of the role.", "job_evidence_ids": [JOB_EVIDENCE["id"]]},
        {"text": "I managed paid search for a regional bakery chain and grew online orders by 35%.", "fact_ids": ["fact.bakery"]},
        {"text": "I set up conversion tracking so the owner could see which campaigns paid off.", "fact_ids": [chunk["id"]]},
    ])
    packet, resolver, ctx = resolve(context(candidate, mock_job, question=INTEREST), retriever, writer, jev)
    assert packet.is_complete and ctx.problems(packet) == []
    assert writer.calls[0]["purpose"] == "cover_letter"
    assert [e["id"] for e in writer.calls[0]["job_evidence"]] == [JOB_EVIDENCE["id"]]
    assert retriever.calls[0]["narrative"] is True and retriever.calls[0]["query"] == INTEREST
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "motivation_narrative")
    assert trace["status"] == "COVER_LETTER_PURPOSE" and trace["source_scope"] == "EXPLICIT_ANSWER"
    assert trace["source_scope_probabilities"]["EXPLICIT_ANSWER"] == 0.78
    assert not any("candidate_narrative" in r["questions"] for r in jev.requests)  # no writer-scope call
    answer = packet.answers[0]
    assert answer.provenance.reference_ids == ["fact.bakery"] and 0.9 <= answer.confidence <= 1.0
    assert "story evidence" in answer.provenance.note and "job context" in answer.provenance.note


@pytest.mark.parametrize(("question", "semantic"), [
    ("What are your salary expectations?", SemanticType.SALARY_EXPECTATION),
    ("Why do you want to relocate to Austin?", SemanticType.CUSTOM_LONG_TEXT),
    ("Describe a campaign you led and what it achieved.", SemanticType.CUSTOM_LONG_TEXT),
])
def test_preferences_and_ordinary_narratives_keep_the_explicit_scope_hold(candidate, mock_job, question, semantic):
    jev = Jev(scope="EXPLICIT_ANSWER", scope_probability=0.78)
    writer = Writer([{"text": "I grew online orders by 35%.", "fact_ids": ["fact.bakery"]}])
    packet, resolver, ctx = resolve(context(candidate, mock_job, question=question, semantic=semantic),
                                    Retriever([candidate.facts[0]], job_evidence=[JOB_EVIDENCE]), writer, jev)
    assert held(packet, ctx) and not writer.calls
    assert not any(t["stage"] == "motivation_narrative" for t in resolver.narrative_traces)


# --- round 2, items 1 (resume dates on the evidence) and 4 (bounded review evidence) -----------


def test_linked_story_evidence_carries_the_resume_dates_as_authoritative(candidate, mock_job):
    from interviewmaxxing_browser.ai.stories import (
        resume_dates_note,
        story_evidence,
        transient_story_facts,
        validate_story_chunks,
    )

    chunk = story_chunk(period="2024-03 to 2025-05")
    chunk["text"] = chunk["text"].replace("| period: 2024-03 to 2025-05", "| resume role: Marketing Manager, Crumb & Co. | period: 2024-03 to 2025-05")
    chunk["id"] = "story:" + hashlib.sha256(chunk["text"].encode()).hexdigest()
    chunk["resume_role"] = "Marketing Manager, Crumb & Co."
    [valid] = validate_story_chunks([chunk], reserved_ids=set())
    assert valid["resume_role"] == "Marketing Manager, Crumb & Co."
    note = resume_dates_note(valid)
    assert note == ("The resume dates this role (Marketing Manager, Crumb & Co.) 2024-03 to 2025-05; "
                    "these dates are authoritative for this passage and supersede any year the passage itself states.")
    [entry] = story_evidence([valid])
    assert entry["story"]["resume_role"] == "Marketing Manager, Crumb & Co." and entry["story"]["note"] == note
    stand_in = transient_story_facts([valid])[valid["id"]]
    assert "Resume role: Marketing Manager, Crumb & Co." in stand_in.evidence and note in stand_in.evidence
    assert resume_dates_note(story_chunk()) is None and "note" not in story_evidence([story_chunk()])[0]["story"]
    # The consistency check tells Jev the same, and the writer sees the note in its evidence.
    money = fact(candidate, "Managed a $40,000 paid search budget for a florist in 2022.", fid="fact.florist")
    profile = candidate.model_copy(update={"facts": [*candidate.facts, money]})
    writer = Writer([{"text": "I grew online orders by 35%.", "fact_ids": ["fact.bakery", valid["id"]]}])
    jev = Jev()
    packet, _, _ = resolve(context(profile, mock_job), Retriever([profile.facts[0]], [valid]), writer, jev)
    assert packet.is_complete
    [request] = jev.story_requests()
    state_chunk = request["state"]["story_chunks"]["c0"]
    assert state_chunk["resume_role"] == "Marketing Manager, Crumb & Co." and state_chunk["dates_note"] == note
    assert "supersede any year the passage itself states" in request["questions"]["c0"]["instructions"]
    assert writer.calls[0]["facts"][-1]["story"]["note"] == note


@dataclass
class ReviewingWriter(Writer):
    reviews: list[dict[str, Any]] = field(default_factory=list)

    def review(self, **kwargs: Any) -> Any:
        self.reviews.append(kwargs)
        return SimpleNamespace(verdict="SUPPORTED", issues=[], reference_ids=[])


def test_the_evidence_review_gets_the_selected_and_most_competing_facts_never_the_whole_store(fictional_candidate, mock_job):
    from interviewmaxxing_browser.ai.routing import REVIEW_EVIDENCE_LIMIT

    base = fictional_candidate.verified_facts()[0]
    facts = [base.model_copy(update={"id": f"fact.{i}", "key": "experience",
                                     "value": f"Managed a ${(i + 1) * 1000:,} paid search budget for client {i} in 2024.",
                                     "evidence": [f"bullet {i}"]}) for i in range(100)]
    profile = fictional_candidate.model_copy(update={"facts": facts, "experience": [], "education": []})
    selected = facts[:3]
    writer = ReviewingWriter([{"text": "I managed paid search budgets for several clients in 2024.",
                               "fact_ids": [f.id for f in selected]}])
    jev = Jev(consistency=0.9)  # uncertain: every comparison goes to the independent review
    packet, resolver, ctx = resolve(context(profile, mock_job), Retriever(selected), writer, jev)
    assert packet.is_complete and ctx.problems(packet) == []
    assert writer.reviews, "the uncertain consistency score must reach the Opus review"
    for review in writer.reviews:
        if review["purpose"] != "evidence_consistency":
            continue
        ids = [f["id"] for f in review["facts"]]
        assert len(ids) <= len(selected) + REVIEW_EVIDENCE_LIMIT < 128
        assert set(f.id for f in selected) <= set(ids) and len(set(ids)) == len(ids)
    scope = next(t for t in resolver.narrative_traces
                 if t["stage"] == "strong_review" and t["purpose"] == "evidence_consistency")
    assert scope["selected_fact_ids"] == [f.id for f in selected]
    assert len(scope["fact_ids"]) == len(selected) + REVIEW_EVIDENCE_LIMIT
    assert scope["competing_total"] == 97 and scope["limit"] == REVIEW_EVIDENCE_LIMIT
    assert len([f for f in writer.calls[0]["facts"]]) == len(selected)  # the writer never sees the store
