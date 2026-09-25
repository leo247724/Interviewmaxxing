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
                score: float = 0.03, resume_role: str | None = None) -> dict[str, Any]:
    header = f"Story 01: {title}" + (f" | employer: {employer}" if employer else "") \
        + (f" | resume role: {resume_role}" if resume_role else "") \
        + (f" | period: {period}" if period else "") + " | themes: budgets, results"
    body = header + "\n" + text
    return {"id": "story:" + hashlib.sha256(body.encode()).hexdigest(), "text": body,
            "story_id": "26787ad7044bdb88", "title": title, "employer": employer, "period": period,
            "resume_role": resume_role, "themes": ["budgets", "results"], "source_version": "c" * 64,
            "score": score, "rank": 1}


class Jev:
    """A scripted Jev: routes the field to the writer and answers the grounding,
    consistency and story-consistency nouls with the configured scores."""

    def __init__(self, *, semantic: str = "CUSTOM_LONG_TEXT", support: Any = 1.0,
                 complete: float = 1.0, consistency: float = 1.0, story: float = 1.0,
                 scope: str = "HISTORICAL_OR_CONTEXTUAL", scope_probability: float = 1.0,
                 link: tuple[str, float, float] | None = None, story_asks: float = 0.0) -> None:
        self.semantic, self.support, self.complete = semantic, support, complete
        self.consistency, self.story, self.story_asks = consistency, story, story_asks
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
                    score = self.story_asks if name.startswith("asks_") else self.story
                elif "canonical_alternatives" in state:
                    if isinstance(self.consistency, dict):
                        score = self.consistency.get(state["selected_facts"][name]["id"], 1.0)
                    else:
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
                 review: list[Any] | None = None, finish: list[str] | None = None) -> None:
        self.queues = {"write": list(write), "humanize": list(humanize or []), "review": list(review or [])}
        self.finish = list(finish or ["stop"])  # write calls' finish reasons; the last repeats
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
        finish = "stop"
        if role == "write":
            finish = self.finish.pop(0) if len(self.finish) > 1 else self.finish[0]
        return HttpResponse(200, {}, json.dumps({"model": MODEL, "usage": {"cost": 0.01}, "choices": [{
            "finish_reason": finish, "message": {"content": json.dumps(payload)}}]}).encode())


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
    assert set(trace) == {"stage", "prompt_version", "story_ids", "comparison_ids", "probabilities",
                          "question_asks_about_it", "cached", "status"}

    # A contradicted chunk leaves the evidence; the narrative writes from the rest.
    contradicted = Jev(story=0.2)
    rest = Writer([{"text": "I grew online orders by 35%.", "fact_ids": ["fact.bakery"]}])
    packet, resolver, ctx = resolve(context(profile, mock_job), Retriever([profile.facts[0]], [chunk]),
                                    rest, contradicted)
    assert packet.is_complete and ctx.problems(packet) == []
    assert next(t for t in resolver.narrative_traces if t["stage"] == "story_consistency")["status"] == "DROPPED"
    dropped = next(t for t in resolver.narrative_traces if t["stage"] == "story_evidence_dropped")
    assert dropped["story_ids"] == [chunk["id"]] and dropped["fact_ids"] == [] and dropped["status"] == "CONTINUED"
    assert dropped["probabilities"] == {chunk["id"]: 0.2} and "contradicts" in dropped["reason"]
    assert not any(item["key"] == "story" for item in rest.calls[0]["facts"])
    assert "story evidence" not in packet.answers[0].provenance.note


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
    assert rewrite["reasoning"] == {"max_tokens": 2560} and rewrite["response_format"]["json_schema"]["strict"]
    assert rewrite["max_tokens"] == 2560 + 3000  # the humanize rewrite keeps a cover letter's room
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
    reasoning = {role: request["reasoning"] for role, request in zip(transport.roles, transport.requests, strict=True)}
    # Narrative calls carry an explicit high-effort reasoning budget; the review keeps effort.
    assert reasoning == {"write": {"max_tokens": 2560}, "humanize": {"max_tokens": 2560}, "review": {"effort": "low"}}
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
    assert writer.calls[0]["purpose"] == "motivation"
    assert [e["id"] for e in writer.calls[0]["job_evidence"]] == [JOB_EVIDENCE["id"]]
    assert retriever.calls[0]["narrative"] is True and retriever.calls[0]["query"] == INTEREST
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "motivation_narrative")
    assert trace["status"] == "MOTIVATION_PURPOSE" and trace["source_scope"] == "EXPLICIT_ANSWER"
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
    # The Jev consistency check compares a ranked top-k of competing facts in one request.
    from interviewmaxxing_browser.ai.routing import CONSISTENCY_COMPARISON_LIMIT

    checks = [t for t in resolver.narrative_traces if t["stage"] == "consistency"]
    assert len(checks) == 1 and checks[0]["competing_total"] == 97
    assert checks[0]["compared"] == CONSISTENCY_COMPARISON_LIMIT == len(checks[0]["canonical_alternative_ids"])
    consistency_requests = [r for r in jev.requests if "canonical_alternatives" in r["state"]]
    assert len(consistency_requests) == 1 and len(consistency_requests[0]["state"]["canonical_alternatives"]) == CONSISTENCY_COMPARISON_LIMIT


# --- round 2b: the resume is canonical; contradicted story evidence is dropped, not held ----------


TENURE_TEXT = ("I spent almost 2 years as the marketing manager of a regional bakery chain, managing a "
               "$120,000 annual paid search budget, and I set up conversion tracking in Google Ads so "
               "the owner could see which campaigns paid for themselves.")


def _bakery_profile(candidate):
    tenure = fact(candidate, "Marketing Manager | Crumb & Co. Bakeries | Apr 2023 to Mar 2024", fid="fact.tenure",
                  key="employment")
    return candidate.model_copy(update={"facts": [*candidate.facts, tenure]})


def test_a_tenure_claim_contradicting_the_resume_is_dropped_and_the_narrative_still_writes(candidate, mock_job):
    profile = _bakery_profile(candidate)
    chunk = story_chunk(TENURE_TEXT, period="2023-04 to 2024-03", resume_role="Marketing Manager, Crumb & Co. Bakeries")
    writer = Writer([{"text": "I managed paid search for a regional bakery chain and grew online orders by 35%.",
                      "fact_ids": ["fact.bakery"]}])
    jev = Jev(story=0.1)
    packet, resolver, ctx = resolve(context(profile, mock_job), Retriever([profile.facts[0]], [chunk]), writer, jev)
    assert packet.is_complete and ctx.problems(packet) == []
    dropped = next(t for t in resolver.narrative_traces if t["stage"] == "story_evidence_dropped")
    assert dropped["story_ids"] == [chunk["id"]] and dropped["status"] == "CONTINUED"
    assert [item["id"] for item in writer.calls[0]["facts"]] == ["fact.bakery"]
    [request] = jev.story_requests()
    assert request["state"]["question"] == QUESTION and "asks_c0" in request["questions"]
    # The grounding and the note see only what remains.
    [grounding] = jev.grounding_requests()
    assert chunk["id"] not in json.dumps(grounding["state"])
    assert packet.answers[0].provenance.reference_ids == ["fact.bakery"]


def test_a_question_about_the_contradicted_tenure_holds(candidate, mock_job):
    profile = _bakery_profile(candidate)
    chunk = story_chunk(TENURE_TEXT, period="2023-04 to 2024-03", resume_role="Marketing Manager, Crumb & Co. Bakeries")
    writer = Writer([{"text": "I held the role for almost two years.", "fact_ids": ["fact.bakery"]}])
    jev = Jev(story=0.1, story_asks=1.0)
    ctx = context(profile, mock_job, question="How long did you work at the regional bakery chain, and what did you achieve there?")
    packet, resolver, ctx = resolve(ctx, Retriever([profile.facts[0]], [chunk]), writer, jev)
    assert held(packet, ctx) and not writer.calls
    dropped = next(t for t in resolver.narrative_traces if t["stage"] == "story_evidence_dropped")
    assert dropped["status"] == "HELD" and dropped["story_ids"] == [chunk["id"]]
    assert next(t for t in resolver.narrative_traces if t["stage"] == "story_consistency")["question_asks_about_it"] == {"c0": 1.0}
    # The same contradiction on a question about the work in general is dropped instead.
    calm = Jev(story=0.1, story_asks=0.0)
    packet, resolver, ctx = resolve(context(profile, mock_job), Retriever([profile.facts[0]], [chunk]),
                                    Writer([{"text": "I grew online orders by 35%.", "fact_ids": ["fact.bakery"]}]), calm)
    assert packet.is_complete


def test_a_story_fact_contradicting_the_resume_is_dropped_but_a_resume_conflict_still_holds(candidate, mock_job):
    profile = _bakery_profile(candidate)
    story_fact = fact(profile, "Over almost 2 years I grew online orders by 35% for the bakery chain (resume: Crumb & Co., 2023-04 to 2024-03)",
                      fid="sf_story_tenure", key="achievement", source="story:" + "1" * 64)
    profile = profile.model_copy(update={"facts": [*profile.facts, story_fact]})
    writer = Writer([{"text": "I grew online orders by 35%.", "fact_ids": ["fact.bakery"]}])
    jev = Jev(consistency={"sf_story_tenure": 0.2})
    packet, resolver, ctx = resolve(context(profile, mock_job), Retriever([profile.facts[0], story_fact]), writer, jev)
    assert packet.is_complete and ctx.problems(packet) == []
    dropped = next(t for t in resolver.narrative_traces if t["stage"] == "story_evidence_dropped")
    assert dropped["fact_ids"] == ["sf_story_tenure"] and dropped["story_ids"] == [] and dropped["status"] == "CONTINUED"
    assert [item["id"] for item in writer.calls[0]["facts"]] == ["fact.bakery"]
    check = next(t for t in resolver.narrative_traces if t["stage"] == "consistency")
    assert check["dropped_story_fact_ids"] == ["sf_story_tenure"] and "sf_story_tenure" not in check["probabilities"].values()
    # A resume fact in conflict is canonical evidence disagreeing with itself: still a hold.
    jev = Jev(consistency={"fact.bakery": 0.2})
    packet, resolver, ctx = resolve(context(profile, mock_job), Retriever([profile.facts[0], story_fact]),
                                    Writer(writer.sentences), jev)
    assert held(packet, ctx)
    assert not any(t["stage"] == "story_evidence_dropped" for t in resolver.narrative_traces)


def test_nothing_usable_left_holds(candidate, mock_job):
    profile = _bakery_profile(candidate)
    story_fact = fact(profile, "Over almost 2 years I grew online orders by 35% (resume: Crumb & Co., 2023-04 to 2024-03)",
                      fid="sf_story_tenure", key="achievement", source="story:" + "1" * 64)
    profile = profile.model_copy(update={"facts": [*profile.facts, story_fact]})
    chunk = story_chunk(TENURE_TEXT, period="2023-04 to 2024-03", resume_role="Marketing Manager, Crumb & Co. Bakeries")
    writer = Writer([{"text": "I grew online orders by 35%.", "fact_ids": ["sf_story_tenure"]}])
    jev = Jev(consistency={"sf_story_tenure": 0.2}, story=0.1)
    packet, _resolver, ctx = resolve(context(profile, mock_job), Retriever([story_fact], [chunk]), writer, jev)
    assert held(packet, ctx) and not writer.calls
    missing = next(m for m in packet.missing_inputs if m.field_id == "response")
    assert "No usable verified evidence remains" in (missing.prompt or "")


# --- round 3: motivation as alignment, budgets and finish reasons, enumerations ----------------

CAREER_MOTIVATION = ("I look for roles where paid media budgets are tied to measured outcomes and "
                     "where I can build the tracking that shows what worked.")
DIRECT_REPORTS = ("How many direct reports do you currently manage, or have you managed in previous "
                  "roles? Please describe the teams.")
TEAM_FACTS = [("fact.team_bakery", "Managed a team of 2 marketing coordinators at Crumb & Co. Bakeries (2023-04 to 2024-09)."),
              ("fact.team_agency", "Led 5 SEO specialists at Glaze Agency (2021-01 to 2023-03).")]


def with_career_motivation(candidate: CandidateProfile) -> CandidateProfile:
    statement = fact(candidate, CAREER_MOTIVATION, fid="career_motivation", key="career_motivation",
                     source="user:simple-answers")
    return candidate.model_copy(update={"facts": [*candidate.facts, statement]})


class DraftQueue:
    """A writer double answering each call with the next scripted draft (the last repeats)
    and supporting every independent review (a corrective rewrite is reviewed)."""

    def __init__(self, *drafts: list[dict[str, Any]]) -> None:
        self.drafts, self.calls = list(drafts), []  # type: ignore[var-annotated]
        self.reviews: list[dict[str, Any]] = []

    def write(self, **kwargs: Any) -> NarrativeDraft:
        self.calls.append(kwargs)
        sentences = self.drafts.pop(0) if len(self.drafts) > 1 else self.drafts[0]
        return NarrativeDraft.model_validate({"status": "READY", "sentences": sentences, "missing_information": []})

    def review(self, **kwargs: Any) -> Any:
        self.reviews.append(kwargs)
        return SimpleNamespace(verdict="SUPPORTED", issues=[], reference_ids=[])


def test_motivation_narratives_state_alignment_and_cite_the_career_motivation_fact(candidate, mock_job):
    from interviewmaxxing_browser.ai.routing import _required_details

    chunk = story_chunk()
    jev = Jev(scope="EXPLICIT_ANSWER", scope_probability=0.78)
    # Retrieval did not surface the statement; the resolver adds it for a motivation question.
    retriever = Retriever([candidate.facts[0]], [chunk], job_evidence=[JOB_EVIDENCE])
    writer = Writer([
        {"text": "The role owns paid search strategy and reports results to sales.", "job_evidence_ids": [JOB_EVIDENCE["id"]]},
        {"text": "I managed paid search for a regional bakery chain and grew online orders by 35%.",
         "fact_ids": ["fact.bakery"], "job_evidence_ids": [JOB_EVIDENCE["id"]]},
        {"text": "I look for roles where budgets are tied to measured outcomes.", "fact_ids": ["career_motivation"]},
    ])
    profile = with_career_motivation(candidate)
    packet, resolver, ctx = resolve(context(profile, mock_job, question=INTEREST), retriever, writer, jev)
    assert packet.is_complete and ctx.problems(packet) == []
    call = writer.calls[0]
    assert call["purpose"] == "motivation" and call["guidance"] == []
    assert [f["id"] for f in call["facts"]] == ["fact.bakery", "career_motivation", chunk["id"]]
    assert call["facts"][1]["key"] == "career_motivation" and call["facts"][1]["value"] == CAREER_MOTIVATION
    answer = packet.answers[0]
    assert answer.provenance.reference_ids == ["fact.bakery", "career_motivation"]
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "motivation_narrative")
    assert trace["status"] == "MOTIVATION_PURPOSE"
    draft = next(t for t in resolver.narrative_traces if t["stage"] == "draft")
    assert draft["attempts"] == []  # the double makes no provider call
    [details] = _required_details(ctx.form.fields[0], "motivation")
    assert "alignment" in details and "personal reason for interest in the company is not required" in details
    # Without a statement the alignment alone is the reason: nothing is demanded.
    writer = Writer([
        {"text": "The role owns paid search strategy and reports results to sales.", "job_evidence_ids": [JOB_EVIDENCE["id"]]},
        {"text": "I managed paid search for a regional bakery chain and grew online orders by 35%.",
         "fact_ids": ["fact.bakery"], "job_evidence_ids": [JOB_EVIDENCE["id"]]},
    ])
    packet, resolver, ctx = resolve(context(candidate, mock_job, question=INTEREST),
                                    Retriever([candidate.facts[0]], [chunk], job_evidence=[JOB_EVIDENCE]), writer, jev)
    assert packet.is_complete and ctx.problems(packet) == []
    assert [f["id"] for f in writer.calls[0]["facts"]] == ["fact.bakery", chunk["id"]]


def test_the_draft_trace_records_each_writer_attempt_and_its_finish_reason(candidate, mock_job):
    transport = Transport(write=[ready(CLEAN)], finish=["length", "stop"])
    budget = CallBudget()
    packet, resolver, ctx = resolve(context(candidate, mock_job), Retriever([candidate.facts[0]]),
                                    real_writer(transport, budget=budget), Jev())
    assert packet.is_complete and ctx.problems(packet) == []
    assert transport.roles == ["write", "write"]  # one retry at the same effort, a larger budget
    assert [r["reasoning"] for r in transport.requests] == [{"max_tokens": 2560}, {"max_tokens": 3840}]
    assert [r["max_tokens"] for r in transport.requests] == [2560 + 2000, 3840 + 4000]
    draft = next(t for t in resolver.narrative_traces if t["stage"] == "draft")
    assert [(a["attempt"], a["status"], a["finish_reason"], a["reasoning_budget_tokens"]) for a in draft["attempts"]] == [
        (1, "OUTPUT_LIMIT", "length", 2560), (2, "OK", "stop", 3840)]
    assert [r.status for r in budget.receipts if r.purpose == "narrative"] == ["OUTPUT_LIMIT", "OK"]
    # A second cut holds after the retry, never before it.
    transport = Transport(write=[ready(CLEAN)], finish=["length"])
    packet, resolver, ctx = resolve(context(candidate, mock_job), Retriever([candidate.facts[0]]),
                                    real_writer(transport), Jev())
    assert held(packet, ctx) and transport.roles == ["write", "write"]
    [missing] = packet.missing_inputs
    assert "output token limit twice" in missing.prompt
    draft = next(t for t in resolver.narrative_traces if t["stage"] == "draft")
    assert [a["finish_reason"] for a in draft["attempts"]] == ["length", "length"]


@pytest.mark.parametrize("question,expected", [
    (DIRECT_REPORTS, True),
    ("How many people have you managed?", True),
    ("Which platforms have you managed?", True),
    ("List the tools you use daily.", True),
    ("Describe a campaign you led and what it achieved.", False),
    ("How many years of paid media experience do you have?", False),
    (INTEREST, False),
])
def test_enumeration_questions_are_recognised(question: str, expected: bool) -> None:
    from interviewmaxxing_browser.ai.routing import _enumeration_question

    assert _enumeration_question(question) is expected


def test_enumeration_questions_write_from_the_facts_at_hand_without_totality_words(candidate, mock_job):
    from interviewmaxxing_browser.ai.routing import ENUMERATION_GUIDANCE, _required_details

    teams = [fact(candidate, text, fid=fid) for fid, text in TEAM_FACTS]
    profile = candidate.model_copy(update={"facts": teams})
    listed = [
        {"text": "At Crumb & Co. Bakeries I managed a team of 2 marketing coordinators from April 2023 to September 2024.",
         "fact_ids": ["fact.team_bakery"]},
        {"text": "At Glaze Agency I led 5 SEO specialists from January 2021 to March 2023.", "fact_ids": ["fact.team_agency"]},
    ]
    totalled = [*listed, {"text": "In total I have managed 7 people across all my roles.",
                          "fact_ids": ["fact.team_bakery", "fact.team_agency"]}]
    writer = DraftQueue(totalled, listed)
    packet, resolver, ctx = resolve(context(profile, mock_job, question=DIRECT_REPORTS), Retriever(teams), writer, Jev())
    assert packet.is_complete and ctx.problems(packet) == []
    assert packet.answers[0].value.text == NarrativeDraft.model_validate(ready(listed)).text
    first, second = writer.calls
    assert first["guidance"] == [ENUMERATION_GUIDANCE] and not first["review_feedback"]
    assert "do not use totality words" in ENUMERATION_GUIDANCE and "do not return NEEDS_INPUT for completeness" in ENUMERATION_GUIDANCE
    [issue] = second["review_feedback"]
    assert issue.startswith("Remove totality words") and "the supplied facts state no total" in issue
    assert next(t["status"] for t in resolver.narrative_traces if t["stage"] == "draft") == "TOTALITY_REJECTED"
    [rewrite] = [t for t in resolver.narrative_traces if t["stage"] == "corrective_rewrite"]
    assert rewrite["review_verdict"] == "UNSUPPORTED" and rewrite["status"] == "ONE_REWRITE_ALLOWED"
    assert len(writer.reviews) == 1  # the rewrite gets the independent review, as any rewrite does
    [details] = _required_details(ctx.form.fields[0], "answer")
    assert details.startswith("Present each item the cited facts state") and "no total may be claimed" in details
    # A fact that states the total allows the word.
    total = fact(candidate, "I have managed a total of 7 direct reports across the two teams.", fid="fact.total")
    writer = DraftQueue([*totalled[:2], {"text": "In total I have managed 7 people across all my roles.", "fact_ids": ["fact.total"]}])
    packet, resolver, ctx = resolve(context(profile.model_copy(update={"facts": [*teams, total]}), mock_job, question=DIRECT_REPORTS),
                                    Retriever([*teams, total]), writer, Jev())
    assert packet.is_complete and len(writer.calls) == 1
