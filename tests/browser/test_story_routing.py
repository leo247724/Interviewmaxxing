"""Stories in the writer's evidence, grounding of story citations, story-versus-fact
consistency, the no-AI-slop rewrite and per-purpose effort. Fictional fixtures only;
every provider is scripted."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
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
from interviewmaxxing_browser.ai.humanize import (
    MAX_REWRITES,
    REJECTION_FEEDBACK,
    check_rewrite,
    lint,
)
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
    Experience,
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
        if (role == "review" and request["response_format"]["json_schema"]["name"] == "application_letter_review"
                and "rubric" not in payload):
            payload = {**payload, "rubric": "PASS", "rubric_issues": [], "owner_question": ""}
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
    assert grounding["state"]["sentences"]["s1"]["fact_ids"] == [chunk["id"]]
    cited = grounding["state"]["evidence"][chunk["id"]]
    assert cited["key"] == "story" and cited["value"] == chunk["text"] and cited["source"] == "user:story"
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


REVIEW_OK = {"verdict": "SUPPORTED", "issues": [], "reference_ids": []}


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
    transport = Transport(write=[ready(SLOPPY)], humanize=[ready(CLEAN)], review=[REVIEW_OK])
    budget = CallBudget()
    jev = Jev()
    packet, resolver, ctx = resolve(context(candidate, mock_job), Retriever([candidate.facts[0]]),
                                    real_writer(transport, budget=budget), jev, humanize=True)
    assert packet.is_complete and ctx.problems(packet) == []
    value = packet.answers[0].value
    assert isinstance(value, TextValue) and value.text == NarrativeDraft.model_validate(ready(CLEAN)).text
    assert lint(value.text) == []
    # The humanized draft always gets the independent review, even at certain scores.
    assert transport.roles == ["write", "humanize", "review"]
    rewrite = transport.requests[1]
    assert rewrite["reasoning"] == {"max_tokens": 2560} and rewrite["response_format"]["json_schema"]["strict"]
    assert rewrite["max_tokens"] == 2560 + 6000  # the humanize rewrite keeps a cover letter's room
    user = json.loads(rewrite["messages"][1]["content"])
    assert user["purpose"] == "answer" and user["voice_samples"] == ["I write short, plain sentences."]
    assert {f["pattern"] for f in user["findings"]} >= {"throat_clearing", "banned_word", "binary_contrast"}
    # The rewrite's wire carries short citation aliases, mapped back before any check (round 6).
    assert "facts" not in user and user["draft"]["sentences"][0]["fact_ids"] == ["F1"]
    assert len(jev.grounding_requests()) == 2
    assert jev.grounding_requests()[1]["state"]["answer"] == value.text
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "humanize")
    assert trace["status"] == "REWRITTEN" and trace["lint_after"] == []
    assert {f["pattern"] for f in trace["lint_before"]} >= {"throat_clearing", "banned_word"}
    assert trace["attempts"][0]["status"] == "REWRITTEN" and trace["attempts"][0]["grounding"]
    assert trace["attempts"][0]["independent_review"] == "SUPPORTED"
    # Pattern names, counts and citation ids only (round 6: the rewritten sentences' ids, so
    # the final letter's citations need no positional alignment), never text.
    assert "leverage" not in json.dumps(trace) and "regional bakery chain" not in json.dumps(trace)
    cited = [{"fact_ids": ["fact.bakery"], "job_evidence_ids": [], "paragraph": 0}] * 3
    assert trace["citations"] == cited and trace["attempts"][0]["citations"] == cited
    assert trace["discarded"] == []
    assert [r.purpose for r in budget.receipts if r.model == MODEL] == ["narrative", "humanize", "opus_draft_grounding"]
    assert [r.requested_reasoning_effort for r in budget.receipts if r.model == MODEL] == ["high", "high", "low"]
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
    transport = Transport(write=[ready(SLOPPY)], humanize=[None if failure == "transport" else ready(rewritten)],
                          review=[REVIEW_OK])
    # Three rewrite attempts reserve more than the fixed USD 0.50 default.
    packet, resolver, _ctx = resolve(context(candidate, mock_job), Retriever([candidate.facts[0]]),
                                    real_writer(transport, budget=CallBudget(max_usd=2.0)), jev, humanize=True)
    assert packet.is_complete
    value = packet.answers[0].value
    assert isinstance(value, TextValue) and value.text == NarrativeDraft.model_validate(ready(SLOPPY)).text
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "humanize")
    expected = {"dropped_citation": "REJECTED_DROPPED_CITATION", "new_number": "REJECTED_NEW_NUMBER",
                "unknown_citation": "REJECTED_UNKNOWN_CITATION", "grounding": "GROUNDING_REJECTED",
                "transport": "HELD"}[failure]
    # Round 6: a rejected rewrite is tried again with its reason (never a transport failure),
    # and every discarded attempt stays named in the trace and the answer's note.
    tries = 1 if failure == "transport" else MAX_REWRITES
    assert trace["status"] == "KEPT_ORIGINAL" and [a["status"] for a in trace["attempts"]] == [expected] * tries
    assert trace["discarded"] == [expected] * tries
    assert transport.roles == ["write"] + ["humanize"] * tries
    assert "no-AI-slop rewrite discarded: " + expected in packet.answers[0].provenance.note
    if failure in ("dropped_citation", "new_number", "unknown_citation"):
        second = json.loads(transport.requests[2]["messages"][1]["content"])
        assert second["rejected_rewrite"] == REJECTION_FEEDBACK[failure]
    elif failure == "grounding":
        second = json.loads(transport.requests[2]["messages"][1]["content"])
        assert second["rejected_rewrite"].startswith("The grounding check rejected the previous rewrite")


def test_a_residual_pattern_triggers_one_more_rewrite_at_most(candidate, mock_job):
    residual = [dict(s) for s in CLEAN]
    residual[0]["text"] = "I leverage paid search for a regional bakery chain in 2024."
    still = [dict(s) for s in CLEAN]
    still[1]["text"] = "Here's the thing: tracking mattered more than spend."
    transport = Transport(write=[ready(SLOPPY)], humanize=[ready(residual), ready(still)], review=[REVIEW_OK])
    # Each rewrite now costs its review too: two rewrites need more than a fixed USD 0.50.
    packet, resolver, _ctx = resolve(context(candidate, mock_job), Retriever([candidate.facts[0]]),
                                    real_writer(transport, budget=CallBudget(max_usd=1.0)), Jev(), humanize=True)
    assert packet.is_complete
    # A residual finding after the first accepted rewrite gets one more (two at most).
    assert transport.roles == ["write"] + ["humanize", "review"] * 2 and MAX_REWRITES == 2
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "humanize")
    assert [a["status"] for a in trace["attempts"]] == ["REWRITTEN", "REWRITTEN"]
    assert trace["attempts"][1]["findings"] == [{"pattern": "banned_word", "count": 1}]
    assert {f["pattern"] for f in trace["lint_after"]} == {"throat_clearing", "colon_reveal"}
    value = packet.answers[0].value
    assert isinstance(value, TextValue) and value.text == NarrativeDraft.model_validate(ready(still)).text
    second = json.loads(transport.requests[3]["messages"][1]["content"])
    assert second["attempt"] == 2 and second["draft"]["text"] == NarrativeDraft.model_validate(ready(residual)).text


def test_a_rewrite_reuses_the_reviewed_consistency_verdict(candidate, mock_job):
    """An uncertain consistency verdict that the Opus evidence review cleared before writing
    does not hold the rewrite: the re-check reuses that review instead of discarding every
    rewrite (live 2026-09-25: three cover letters kept their un-humanized drafts)."""
    rival = fact(candidate, "Grew repeat orders by 12% for a regional bakery chain (2023).", fid="fact.repeat")
    profile = candidate.model_copy(update={"facts": [*candidate.facts, rival]})
    transport = Transport(write=[ready(SLOPPY)], humanize=[ready(CLEAN)], review=[REVIEW_OK])
    jev = Jev(consistency=0.9)  # uncertain: the evidence goes to the Opus review once
    packet, resolver, _ctx = resolve(context(profile, mock_job), Retriever([profile.facts[0]]),
                                     real_writer(transport), jev, humanize=True)
    assert packet.is_complete
    value = packet.answers[0].value
    assert isinstance(value, TextValue) and value.text == NarrativeDraft.model_validate(ready(CLEAN)).text
    purposes = [json.loads(r["messages"][1]["content"])["purpose"]
                for role, r in zip(transport.roles, transport.requests, strict=True) if role == "review"]
    assert transport.roles == ["review", "write", "humanize", "review"]
    assert purposes == ["evidence_consistency", "draft_grounding"]  # one evidence review, reused
    assert len([r for r in jev.requests if "canonical_alternatives" in r["state"]]) == 1
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "humanize")
    assert trace["status"] == "REWRITTEN" and trace["attempts"][0]["status"] == "REWRITTEN"
    assert any(t["stage"] == "consistency_cache" for t in resolver.narrative_traces)


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
    assert request["state"]["prompt_version"] == "story-role-link-v2"
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
    assert "alignment" in details and "career_motivation statement restated when supplied" in details
    assert "A personal reason beyond that" in details  # round 5 addendum 2: M7's demand is superseded
    # Without a statement the applicant's own account of the work gives the reason (round 4).
    writer = Writer([
        {"text": "The role owns paid search strategy and reports results to sales.", "job_evidence_ids": [JOB_EVIDENCE["id"]]},
        {"text": "I managed paid search for a regional bakery chain and grew online orders by 35%.",
         "fact_ids": ["fact.bakery"], "job_evidence_ids": [JOB_EVIDENCE["id"]]},
        {"text": "I set up conversion tracking so the owner could see which campaigns paid off.", "fact_ids": [chunk["id"]]},
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


# --- round 4: the applicant's own reason, tiered comparisons, reviewed rewrites, one allowance --


def test_a_motivation_question_with_facts_but_no_statement_is_written_not_held(candidate, mock_job):
    """Round 5, addendum 2 supersedes round 4's M7: the reason is the alignment between the
    posting and the applicant's experience; neither a story passage nor a career_motivation
    statement is required, and the field holds only when no fact relates to the posting."""
    jev = Jev(scope="EXPLICIT_ANSWER", scope_probability=0.78)
    aligned = [
        {"text": "The role owns paid search strategy and reports results to sales.", "job_evidence_ids": [JOB_EVIDENCE["id"]]},
        {"text": "I managed paid search for a regional bakery chain and grew online orders by 35%.",
         "fact_ids": ["fact.bakery"], "job_evidence_ids": [JOB_EVIDENCE["id"]]},
    ]
    writer = Writer(aligned)
    packet, resolver, ctx = resolve(context(candidate, mock_job, question=INTEREST),
                                    Retriever([candidate.facts[0]], job_evidence=[JOB_EVIDENCE]), writer, jev)
    assert packet.is_complete and ctx.problems(packet) == [] and len(writer.calls) == 1
    assert writer.calls[0]["purpose"] == "motivation"
    assert all(item["key"] not in ("career_motivation", "story") for item in writer.calls[0]["facts"])
    assert next(t["status"] for t in resolver.narrative_traces if t["stage"] == "draft") == "READY"
    assert packet.answers[0].provenance.reference_ids == ["fact.bakery"]
    # Held only when nothing relates to the posting: retrieval found no fact at all.
    writer = Writer(aligned)
    packet, resolver, ctx = resolve(context(candidate, mock_job, question=INTEREST),
                                    Retriever([], job_evidence=[JOB_EVIDENCE]), writer, jev)
    assert held(packet, ctx) and not writer.calls


def test_a_global_counterclaim_beyond_the_bound_is_still_compared(fictional_candidate, mock_job):
    """A same-key non-additive difference holds deterministically before any comparison
    ("Relevant verified facts conflict"); a global counterclaim or explicit negative is
    Jev-judged and used to fall below the 40 compared behind many additive bullets about
    the same subject. Tier 0 puts it first."""
    from interviewmaxxing_browser.ai.routing import CONSISTENCY_COMPARISON_LIMIT
    from interviewmaxxing_core import Experience

    base = fictional_candidate.verified_facts()[0]
    selected = base.model_copy(update={"id": "fact.a_budget", "key": "experience",
                                       "value": "Managed a $5,000 paid search budget for client 3 in 2024.", "evidence": ["bullet"]})
    bullets = [base.model_copy(update={"id": f"fact.b{i:03d}", "key": "experience",
                                       "value": f"Managed a ${(i + 1) * 1000:,} paid search budget for client {i} in 2024.",
                                       "evidence": [f"bullet {i}"]}) for i in range(60)]
    never = base.model_copy(update={"id": "fact.zz_never", "key": "experience",
                                    "value": "I have never managed a paid search budget for any client.", "evidence": ["note"]})
    group = Experience(id="exp_bakery", company="Crumb & Co. Bakeries", title="Marketing Manager", start="2023-04",
                       end="2024-09", current=False, fact_ids=[selected.id, *(b.id for b in bullets), never.id])
    profile = fictional_candidate.model_copy(update={"facts": [selected, *bullets, never], "experience": [group], "education": []})
    writer = Writer([{"text": "I managed a $5,000 paid search budget for a client in 2024.", "fact_ids": ["fact.a_budget"]}])
    packet, resolver, _ctx = resolve(context(profile, mock_job), Retriever([selected]), writer, Jev())
    assert packet.is_complete
    [check] = [t for t in resolver.narrative_traces if t["stage"] == "consistency"]
    assert check["compared"] == CONSISTENCY_COMPARISON_LIMIT and check["competing_total"] == 61
    # By id alone the counterclaim would fall beyond the 40 compared; global claims tier first.
    assert check["canonical_alternative_ids"][0] == "fact.zz_never" and check["tiered_first"] == 1
    assert len(check["canonical_alternative_ids"]) == CONSISTENCY_COMPARISON_LIMIT
    # Judged, not assumed: an uncertain verdict on the counterclaim reaches the review with it.
    writer = ReviewingWriter([{"text": "I managed a $5,000 paid search budget for a client in 2024.", "fact_ids": ["fact.a_budget"]}])
    packet, resolver, _ctx = resolve(context(profile, mock_job), Retriever([selected]), writer, Jev(consistency=0.9))
    assert packet.is_complete
    review = next(r for r in writer.reviews if r["purpose"] == "evidence_consistency")
    assert [f["id"] for f in review["facts"]][:2] == ["fact.a_budget", "fact.zz_never"]


def test_a_rewrite_that_moves_a_citation_set_is_rejected_and_the_draft_kept(candidate, mock_job):
    two = [
        {"text": "Here's the thing: I leverage paid search for a regional bakery chain and grew orders by 35%.",
         "fact_ids": ["fact.bakery"]},
        {"text": "I wrote weekly reports for two store managers.", "fact_ids": ["fact.reports"]},
    ]
    moved = [
        {"text": "I ran paid search for a regional bakery chain.", "fact_ids": ["fact.bakery"]},
        {"text": "I wrote weekly reports for two store managers and grew orders by 35%.",
         "fact_ids": ["fact.reports", "fact.bakery"]},  # the metric travelled with a recombined citation set
    ]
    transport = Transport(write=[ready(two)], humanize=[ready(moved)], review=[REVIEW_OK])
    packet, resolver, _ctx = resolve(context(candidate, mock_job), Retriever(list(candidate.facts)),
                                    real_writer(transport, budget=CallBudget(max_usd=2.0)), Jev(), humanize=True)
    assert packet.is_complete
    value = packet.answers[0].value
    assert isinstance(value, TextValue) and value.text == NarrativeDraft.model_validate(ready(two)).text
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "humanize")
    assert trace["status"] == "KEPT_ORIGINAL" and trace["attempts"][0]["status"] == "REJECTED_MOVED_CITATION"
    # Rejected before grounding (no review call), and tried again with the reason each time.
    assert transport.roles == ["write"] + ["humanize"] * MAX_REWRITES
    retry = json.loads(transport.requests[2]["messages"][1]["content"])
    assert retry["rejected_rewrite"] == REJECTION_FEEDBACK["moved_citation"]
    original = NarrativeDraft.model_validate(ready(two))
    assert check_rewrite(original, NarrativeDraft.model_validate(ready(moved)), purpose="answer",
                         supplied_ids={"fact.bakery", "fact.reports"}, job_ids=set(), max_length=None) == "moved_citation"
    split = NarrativeDraft.model_validate(ready([
        {"text": "I ran paid search for a regional bakery chain.", "fact_ids": ["fact.bakery"]},
        {"text": "Orders grew by 35%.", "fact_ids": []},  # the metric left its citation behind
        {"text": "I wrote weekly reports for two store managers.", "fact_ids": ["fact.reports"]},
    ]))
    assert check_rewrite(original, split, purpose="answer", supplied_ids={"fact.bakery", "fact.reports"},
                         job_ids=set(), max_length=None) is None  # sets kept; grounding judges the uncited claim
    merged = NarrativeDraft.model_validate(ready([
        {"text": "I ran paid search for a regional bakery chain in 2024.", "fact_ids": ["fact.bakery"]},
        {"text": "Tracking mattered more than spend, so I set it up first, and online orders grew by 35%.",
         "fact_ids": ["fact.bakery"]}]))
    assert check_rewrite(NarrativeDraft.model_validate(ready(CLEAN)), merged, purpose="answer",
                         supplied_ids={"fact.bakery"}, job_ids=set(), max_length=None) is None  # same set, merged


def test_the_form_allowance_is_granted_once_per_step_per_run(candidate, mock_job):
    transport = Transport(write=[ready(CLEAN)])
    budget = CallBudget(scales_with_form=True)
    writer = real_writer(transport, budget=budget)
    decisions = BoundedDecisions(JevClient(ApiKey("synthetic-test-key", source="test"), transport=Jev(), max_attempts=1), budget)
    router = AIFormRouter(decisions)
    ctx = context(candidate, mock_job)
    ctx = replace(ctx, form=router.annotate(ctx.form, document_id="synthetic-stories"))
    resolver = DynamicPacketResolver(decisions, writer, router=router, retriever=Retriever([candidate.facts[0]]))
    assert (budget.max_calls, budget.max_usd) == (48, 0.50)
    first = asyncio.run(resolver.resolve(ctx))
    assert first.is_complete
    limits = (budget.max_calls, budget.max_usd)
    # One WRITER field: 24 + 24 calls and USD 0.30 + 0.75 on top of the classification call
    # made before the grant (its reservation included).
    assert limits[0] == 24 + 24 + 1 and 1.05 <= limits[1] < 1.06
    second = asyncio.run(resolver.resolve(ctx))  # the same step again: nothing more is granted
    assert second.is_complete and (budget.max_calls, budget.max_usd) == limits
    other = replace(ctx, application=ctx.application.model_copy(update={"id": "app-other"}))
    asyncio.run(resolver.resolve(other))  # another step: its own allowance on top of the use so far
    assert budget.max_calls > limits[0] and budget.max_usd > limits[1]


# --- round 5: distinctive names and stated periods; review pass 5 M1, M2 and L4; restated motivation --

RECRUITING = ("I ran paid social and email campaigns for a youth sports recruiting network. "
              "I grew free athlete sign-ups by 40% in one season.")


def _pair_story(title: str = "Growth Marketing Specialist, RecruitHubSports (Aug 2019 - May 2020)",
                body: str = RECRUITING) -> tuple[Any, Any]:
    from interviewmaxxing_generation.knowledge import stories as st

    story = st.Story(1, title, tuple(st.split_sentences(body)))
    return story, st.analyse_story(story)


def test_link_stories_never_links_by_a_common_word_and_asks_only_about_overlapping_roles() -> None:
    from datetime import UTC, date, datetime

    from interviewmaxxing_browser.ai.stories import link_stories
    from interviewmaxxing_generation.knowledge import stories as st

    today, now = date(2026, 9, 25), datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    shop = st.ResumeRole("exp_shop", "Shop Growth Solutions", "Marketing Manager", "2023-10", "2024-02", False,
                         ("Managed paid search for an online store.",))
    story, analysis = _pair_story()
    jev = Jev(link=("r0", 1.0, 1.0))
    decisions = _decide(jev)
    links, traces = link_stories([story], [analysis], [shop], decide=decisions.decide, model=decisions.model, today=today)
    # "Growth" is not the company's name, and no role overlaps Aug 2019 - May 2020: nothing is asked.
    assert links == {} and not jev.requests
    [trace] = traces
    assert trace["status"] == "NO_OVERLAPPING_ROLE" and trace["excluded_by_period"] == ["exp_shop"] and trace["role_ids"] == []
    index = st.build_story_index(st.StoryDocument("0" * 64, 1, (story,), (analysis,)), verified_at=now, links=links)
    assert all(" | period: 2019-08 to 2020-05 | " in c.text.split("\n")[0] and "2023-10" not in c.text for c in index.chunks)
    assert index.facts and all("2023-10" not in str(f.value) for f in index.facts)
    # With a role that does overlap, Jev is asked about that role only and told the stated period.
    early = st.ResumeRole("exp_early", "Northfield Athletics", "Growth Marketer", "2019-06", "2020-12", False,
                          ("Ran paid social for a sports recruiting network.",))
    jev = Jev(link=("r0", 0.97, 0.98))
    decisions = _decide(jev)
    links, [trace] = link_stories([story], [analysis], [shop, early], decide=decisions.decide,
                                  model=decisions.model, today=today)
    [request] = jev.requests
    assert list(request["state"]["resume_roles"]) == ["r0"]
    assert request["state"]["resume_roles"]["r0"]["company"] == "Northfield Athletics"
    assert request["state"]["story"]["period_stated"] == "2019-08 to 2020-05"
    assert request["state"]["story"]["years_stated"] == ["2019", "2020"]  # read from the heading
    assert links[story.story_id].resume_role_id == "exp_early" and trace["excluded_by_period"] == ["exp_shop"]
    # A name link the stated period contradicts is proposed, traced as a mismatch and dropped by the index.
    named, named_analysis = _pair_story("Paid search at Shop Growth Solutions (Aug 2019 - May 2020)",
                                        "I ran paid search at Shop Growth Solutions for an online store.")
    links, [trace] = link_stories([named], [named_analysis], [shop], today=today)
    assert links[named.story_id].method == "employer_name" and trace["status"] == "PERIOD_MISMATCH"
    index = st.build_story_index(st.StoryDocument("0" * 64, 1, (named,), (named_analysis,)), verified_at=now, links=links)
    assert index.link_for(named.story_id) is None and index.mismatch_for(named.story_id)["method"] == "employer_name"  # type: ignore[index]


def _extracted(profile: CandidateProfile, chunk: dict[str, Any], sentence: str, fid: str,
               *, key: str = "achievement") -> CandidateFact:
    """A story fact as the index extracts it: the sentence, its context and the chunk it came from."""
    return fact(profile, sentence + " (regional bakery chain; resume: Crumb & Co., 2023-04 to 2024-03)", fid=fid,
                key=key, source=chunk["id"])


def test_a_dropped_story_chunk_takes_the_facts_extracted_from_it(candidate, mock_job):
    profile = _bakery_profile(candidate)
    chunk = story_chunk(TENURE_TEXT, period="2023-04 to 2024-03", resume_role="Marketing Manager, Crumb & Co. Bakeries")
    extracted = _extracted(profile, chunk, TENURE_TEXT[:-1], "sf_tenure_sentence")
    profile = profile.model_copy(update={"facts": [*profile.facts, extracted]})
    writer = Writer([{"text": "I grew online orders by 35%.", "fact_ids": ["fact.bakery"]}])
    jev = Jev(story=0.1)  # the chunk contradicts the resume's tenure; the fact's own check passes
    packet, resolver, ctx = resolve(context(profile, mock_job), Retriever([profile.facts[0], extracted], [chunk]),
                                    writer, jev)
    assert packet.is_complete and ctx.problems(packet) == []
    assert [item["id"] for item in writer.calls[0]["facts"]] == ["fact.bakery"]  # neither the chunk nor its fact
    drops = [t for t in resolver.narrative_traces if t["stage"] == "story_evidence_dropped"]
    assert [(t["story_ids"], t["fact_ids"], t["status"]) for t in drops] == [
        ([chunk["id"]], [], "CONTINUED"), ([], ["sf_tenure_sentence"], "CONTINUED")]
    assert drops[1]["propagated_from"] == {"story_ids": [chunk["id"]], "fact_ids": []}
    assert "same story sentence" in drops[1]["reason"]
    check = next(t for t in resolver.narrative_traces if t["stage"] == "consistency")
    assert check["dropped_story_fact_ids"] == []  # the fact passed its own comparison: M1's failure case
    [grounding] = jev.grounding_requests()
    assert "sf_tenure_sentence" not in json.dumps(grounding["state"]) and chunk["id"] not in json.dumps(grounding["state"])


def test_a_dropped_story_fact_takes_the_chunks_that_carry_its_sentence(candidate, mock_job):
    profile = _bakery_profile(candidate)
    chunk = story_chunk(TENURE_TEXT, period="2023-04 to 2024-03", resume_role="Marketing Manager, Crumb & Co. Bakeries")
    # A summary chunk of the same story repeats the sentence; the fact names the section chunk as its source.
    summary = story_chunk("Summary of story 01. Outcomes: " + TENURE_TEXT, period="2023-04 to 2024-03",
                          resume_role="Marketing Manager, Crumb & Co. Bakeries")
    other = story_chunk("I wrote a weekly report that listed orders by campaign and by store for the owner.",
                        period="2023-04 to 2024-03", resume_role="Marketing Manager, Crumb & Co. Bakeries")
    extracted = _extracted(profile, chunk, TENURE_TEXT[:-1], "sf_tenure_sentence")
    profile = profile.model_copy(update={"facts": [*profile.facts, extracted]})
    writer = Writer([{"text": "I grew online orders by 35%.", "fact_ids": ["fact.bakery"]},
                     {"text": "I reported orders by campaign every week.", "fact_ids": [other["id"]]}])
    jev = Jev(consistency={"sf_tenure_sentence": 0.2})  # the fact contradicts the resume; the chunks pass
    packet, resolver, ctx = resolve(context(profile, mock_job),
                                    Retriever([profile.facts[0], extracted], [chunk, summary, other]), writer, jev)
    assert packet.is_complete and ctx.problems(packet) == []
    assert [item["id"] for item in writer.calls[0]["facts"]] == ["fact.bakery", other["id"]]
    drops = [t for t in resolver.narrative_traces if t["stage"] == "story_evidence_dropped"]
    assert [(t["story_ids"], t["fact_ids"]) for t in drops] == [
        ([], ["sf_tenure_sentence"]), ([chunk["id"], summary["id"]], [])]
    assert drops[1]["propagated_from"] == {"story_ids": [], "fact_ids": ["sf_tenure_sentence"]}
    assert next(t for t in resolver.narrative_traces if t["stage"] == "story_consistency")["status"] == "CONSISTENT"
    assert chunk["id"] not in packet.answers[0].provenance.note and other["id"] in packet.answers[0].provenance.note


def test_shares_story_evidence_needs_the_source_or_the_sentence(candidate) -> None:
    from interviewmaxxing_browser.ai.stories import shares_story_evidence

    chunk = story_chunk(TENURE_TEXT)
    assert shares_story_evidence(_extracted(candidate, chunk, "Anything at all", "sf_a"), chunk)
    elsewhere = {"id": "story:" + "2" * 64, "text": chunk["text"]}
    assert shares_story_evidence(_extracted(candidate, elsewhere, TENURE_TEXT[:-1], "sf_b"), chunk)
    assert not shares_story_evidence(_extracted(candidate, elsewhere, "I grew orders by 35%", "sf_c"), chunk)
    resume = fact(candidate, TENURE_TEXT, fid="fact.resume")  # a resume fact never shares story evidence
    assert not shares_story_evidence(resume, chunk)


def _two_field_context(candidate: CandidateProfile, job: JobRecord) -> PacketContext:
    base = context(candidate, job)
    second = base.form.fields[0].model_copy(update={"id": "second", "label": "Tell us about a result you are proud of.",
                                                     "selector": "#second"})
    return replace(base, form=base.form.model_copy(update={"fields": [base.form.fields[0], second]}))


def test_a_cached_strong_review_still_drops_a_story_fact_for_the_next_field(candidate, mock_job):
    profile = _bakery_profile(candidate)
    story_fact = fact(profile, "Over almost 2 years I grew online orders by 35% for the bakery chain (resume: Crumb & Co., 2023-04 to 2024-03)",
                      fid="sf_story_tenure", key="achievement", source="story:" + "1" * 64)
    profile = profile.model_copy(update={"facts": [*profile.facts, story_fact]})
    # The story fact is contradicted; the resume fact's verdict is uncertain, so the first
    # field's evidence goes to the strong review, which is cached for the candidate revision.
    jev = Jev(consistency={"sf_story_tenure": 0.2, "fact.bakery": 0.9})
    writer = ReviewingWriter([{"text": "I grew online orders by 35%.", "fact_ids": ["fact.bakery"]}])
    budget = CallBudget(max_calls=80, max_usd=1.0)
    decisions = BoundedDecisions(JevClient(ApiKey("synthetic-test-key", source="test"), transport=jev, max_attempts=1), budget)
    router = AIFormRouter(decisions)
    ctx = _two_field_context(profile, mock_job)
    ctx = replace(ctx, form=router.annotate(ctx.form, document_id="synthetic-stories"))
    resolver = DynamicPacketResolver(decisions, writer, router=router,
                                     retriever=Retriever([profile.facts[0], story_fact]), humanize=False)
    packet = asyncio.run(resolver.resolve(ctx))
    assert packet.is_complete and len(packet.answers) == 2 and ctx.problems(packet) == []
    # Neither field's writer sees the contradicted story fact: the second field used to take
    # the cached review's early return and keep it.
    assert len(writer.calls) == 2 and all([item["id"] for item in call["facts"]] == ["fact.bakery"] for call in writer.calls)
    assert len([r for r in writer.reviews if r["purpose"] == "evidence_consistency"]) == 1  # reviewed once
    drops = [t for t in resolver.narrative_traces if t["stage"] == "story_evidence_dropped"]
    assert [t["fact_ids"] for t in drops] == [["sf_story_tenure"], ["sf_story_tenure"]]
    [cached] = [t for t in resolver.narrative_traces if t["stage"] == "consistency_cache"]
    assert cached["status"] == "SUPPORTED" and cached["dropped_story_fact_ids"] == ["sf_story_tenure"]
    assert len([r for r in jev.requests if "canonical_alternatives" in r["state"]]) == 1  # verdicts reused
    # Fields that select no story fact still take the cached review directly, before any comparison.
    resolver.retriever = Retriever([profile.facts[0]])
    resolver.narrative_traces.clear()
    assert asyncio.run(resolver.resolve(ctx)).is_complete
    cached = [t for t in resolver.narrative_traces if t["stage"] == "consistency_cache"]
    assert len(cached) == 2 and all("dropped_story_fact_ids" not in t for t in cached)
    assert not any(t["stage"] in ("consistency", "story_evidence_dropped") for t in resolver.narrative_traces)
    assert len([r for r in writer.reviews if r["purpose"] == "evidence_consistency"]) == 1


@pytest.mark.parametrize(("entries", "stored", "requests"), [(0, 0, 2), (1, 0, 2), (2, 2, 1), (3, 2, 1)])
def test_the_story_verdict_cache_holds_whole_pairs_or_nothing(candidate, mock_job, entries, stored, requests):
    import threading

    from interviewmaxxing_browser.ai.stories import story_consistency

    chunk = story_chunk()
    money = fact(candidate, "Managed a $40,000 paid search budget for a florist in 2022.", fid="fact.florist")
    ctx = context(candidate.model_copy(update={"facts": [*candidate.facts, money]}), mock_job)
    jev = Jev()
    decisions = BoundedDecisions(JevClient(ApiKey("synthetic-test-key", source="test"), transport=jev, max_attempts=1),
                                 max_cache_entries=0)  # every request reaches the scripted Jev
    cache: dict[str, float] = {}
    lock = threading.RLock()
    for _ in range(2):
        confidence, kept = story_consistency(context=ctx, chunks=[chunk], decide=decisions.decide, trace=lambda t: t,
                                             model=decisions.model, min_probability=0.95, cache=cache, lock=lock,
                                             max_cache_entries=entries, max_facts=40, question=QUESTION)
        assert confidence == 1.0 and kept == [chunk]
    assert len(cache) == stored and len(jev.story_requests()) == requests
    # Half a pair (its asks flag evicted) is not a cached verdict: the chunk is asked again.
    if stored:
        cache.pop(next(key for key in cache if key.endswith(":asks")))
        story_consistency(context=ctx, chunks=[chunk], decide=decisions.decide, trace=lambda t: t,
                          model=decisions.model, min_probability=0.95, cache=cache, lock=lock,
                          max_cache_entries=entries, max_facts=40, question=QUESTION)
        assert len(jev.story_requests()) == requests + 1 and len(cache) <= max(entries, 2)


# Item 5: the person's statement is restated, never pasted; sentence openers vary.

QUOTING = [
    {"text": "The role owns paid search strategy and reports results to sales.", "job_evidence_ids": [JOB_EVIDENCE["id"]]},
    {"text": "I managed paid search for a regional bakery chain and grew online orders by 35%.",
     "fact_ids": ["fact.bakery"], "job_evidence_ids": [JOB_EVIDENCE["id"]]},
    {"text": CAREER_MOTIVATION, "fact_ids": ["career_motivation"]},
]
RESTATED = [*QUOTING[:2], {"text": "What I want next is a role that ties paid media spend to results I can measure, "
                                   "with room to build the tracking behind them.", "fact_ids": ["career_motivation"]}]


def test_a_draft_quoting_the_career_motivation_statement_is_rewritten(candidate, mock_job):
    from interviewmaxxing_browser.ai.humanize import QUOTED_STATEMENT_FEEDBACK

    jev = Jev(scope="EXPLICIT_ANSWER", scope_probability=0.78)
    profile = with_career_motivation(candidate)
    writer = DraftQueue(QUOTING, RESTATED)
    packet, resolver, ctx = resolve(context(profile, mock_job, question=INTEREST),
                                    Retriever([candidate.facts[0]], job_evidence=[JOB_EVIDENCE]), writer, jev)
    assert packet.is_complete and ctx.problems(packet) == []
    assert packet.answers[0].value.text == NarrativeDraft.model_validate(ready(RESTATED)).text
    first, second = writer.calls
    assert not first["review_feedback"] and second["review_feedback"] == [QUOTED_STATEMENT_FEEDBACK]
    drafts = [t for t in resolver.narrative_traces if t["stage"] == "draft"]
    assert drafts[0]["status"] == "STATEMENT_QUOTED" and drafts[0]["rejected_for"] == ["STATEMENT_QUOTED"]
    assert drafts[1]["status"] == "READY" and "rejected_for" not in drafts[1]
    assert len(jev.grounding_requests()) == 1  # the quoting draft never reached grounding
    # Pasted twice: the field holds after the one corrective rewrite.
    packet, resolver, ctx = resolve(context(profile, mock_job, question=INTEREST),
                                    Retriever([candidate.facts[0]], job_evidence=[JOB_EVIDENCE]), DraftQueue(QUOTING), jev)
    assert held(packet, ctx)
    # Twelve consecutive words are allowed; thirteen are not.
    twelve = [*QUOTING[:2], {"text": "I look for roles where paid media budgets are tied to measured results I can check.",
                             "fact_ids": ["career_motivation"]}]
    packet, _, ctx = resolve(context(profile, mock_job, question=INTEREST),
                             Retriever([candidate.facts[0]], job_evidence=[JOB_EVIDENCE]), DraftQueue(twelve), jev)
    assert packet.is_complete


def test_one_corrective_rewrite_names_every_deterministic_finding(candidate, mock_job):
    from interviewmaxxing_browser.ai.humanize import FIT_HEDGE_FEEDBACK, QUOTED_STATEMENT_FEEDBACK

    jev = Jev(scope="EXPLICIT_ANSWER", scope_probability=0.78)
    hedged_and_pasted = [QUOTING[0], {**QUOTING[1], "text": "While I have not managed retail media, I managed paid "
                                      "search for a regional bakery chain and grew online orders by 35%."}, QUOTING[2]]
    writer = DraftQueue(hedged_and_pasted, RESTATED)
    packet, resolver, _ctx = resolve(context(with_career_motivation(candidate), mock_job, question=INTEREST),
                                     Retriever([candidate.facts[0]], job_evidence=[JOB_EVIDENCE]), writer, jev)
    assert packet.is_complete
    assert writer.calls[1]["review_feedback"] == [FIT_HEDGE_FEEDBACK, QUOTED_STATEMENT_FEEDBACK]
    draft = next(t for t in resolver.narrative_traces if t["stage"] == "draft")
    assert draft["status"] == "FIT_HEDGED" and draft["rejected_for"] == ["FIT_HEDGED", "STATEMENT_QUOTED"]


def test_the_writer_prompt_restates_the_statement_and_varies_openers(candidate, mock_job):
    transport = Transport(write=[ready(CLEAN)])
    packet, _, _ = resolve(context(candidate, mock_job), Retriever([candidate.facts[0]]), real_writer(transport), Jev())
    assert packet.is_complete
    system = transport.requests[0]["messages"][0]["content"]
    assert "career_motivation is the applicant's own statement of what they look for" in system
    assert "restate it in different words each time, with the same meaning and no new claim" in system
    assert "no run of more than 12 consecutive words" in system
    assert "never begin two consecutive sentences with 'In that same role'" in system


def test_the_humanizer_flags_quotes_and_repeated_openers_and_rejects_a_quoting_rewrite() -> None:
    from interviewmaxxing_browser.ai.humanize import MAX_QUOTED_WORDS, quoted_run, quotes_statement

    pasted = ("Put simply, I LOOK for roles where paid-media budgets are tied to measured outcomes, and where I "
              "can build tracking.")
    assert len(quoted_run(pasted, CAREER_MOTIVATION)) == 18 > MAX_QUOTED_WORDS  # case and punctuation ignored
    assert quotes_statement(pasted, [CAREER_MOTIVATION]) and not quotes_statement(pasted, [])
    findings = {f.pattern: f for f in lint(pasted, statements=[CAREER_MOTIVATION])}
    assert findings["quoted_statement"].count == 1 and "quoted_statement" not in {f.pattern for f in lint(pasted)}
    repeated = "In that same role, I ran paid search. In that same role, I built the tracking. Orders grew by 35%."
    assert {f.pattern: f.count for f in lint(repeated)}["repeated_opener"] == 1
    assert "repeated_opener" not in {f.pattern for f in lint("I ran paid search. I built the tracking.")}
    original = NarrativeDraft.model_validate(ready(RESTATED))
    quoting = NarrativeDraft.model_validate(ready(QUOTING))
    ids = {"fact.bakery", "career_motivation"}
    assert check_rewrite(original, quoting, purpose="motivation", supplied_ids=ids, job_ids={JOB_EVIDENCE["id"]},
                         max_length=None, statements=[CAREER_MOTIVATION]) == "quoted_statement"
    assert check_rewrite(original, quoting, purpose="motivation", supplied_ids=ids, job_ids={JOB_EVIDENCE["id"]},
                         max_length=None) is None


def test_a_humanized_rewrite_that_pastes_the_statement_is_rejected(candidate, mock_job):
    sloppy = [dict(s) for s in RESTATED]
    sloppy[1] = {**sloppy[1], "text": "Here's the thing: I managed paid search for a regional bakery chain and grew online orders by 35%."}
    transport = Transport(write=[ready(sloppy)], humanize=[ready(QUOTING)], review=[REVIEW_OK])
    jev = Jev(scope="EXPLICIT_ANSWER", scope_probability=0.78)
    packet, resolver, _ctx = resolve(context(with_career_motivation(candidate), mock_job, question=INTEREST),
                                     Retriever([candidate.facts[0]], job_evidence=[JOB_EVIDENCE]),
                                     real_writer(transport, budget=CallBudget(max_usd=2.0)), jev, humanize=True)
    assert packet.is_complete
    assert packet.answers[0].value.text == NarrativeDraft.model_validate(ready(sloppy)).text
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "humanize")
    assert trace["status"] == "KEPT_ORIGINAL" and trace["attempts"][0]["status"] == "REJECTED_QUOTED_STATEMENT"
    assert trace["prompt_version"] == "no-ai-slop-v3" and transport.roles == ["write"] + ["humanize"] * MAX_REWRITES
    assert trace["discarded"] == ["REJECTED_QUOTED_STATEMENT"] * MAX_REWRITES
    rules = transport.requests[1]["messages"][0]["content"]
    assert "quoted_statement finding" in rules and "In that same role" in rules
    assert CAREER_MOTIVATION not in json.dumps(trace)


# --- round 5, addendum 2: fit is given; the writer builds the case; the review judges grounding --

POSTING = {"id": "job:" + "d" * 64, "source_url": "https://synthetic.test/jobs/2", "source_version": "e" * 64,
           "text": ("Own paid search strategy for enterprise brands and report results to the sales team. "
                    "Requirements: hands-on Google Ads management, conversion tracking, and TikTok Ads experience.")}
RUBRIC_LETTER: list[dict[str, Any]] = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "browser" / "rubric_letter.json").read_text(encoding="utf-8"))["sentences"]


def rubric_letter(fact_id: str, job_id: str, *, story_id: str | None = None,
                  contact: bool = True) -> list[dict[str, Any]]:
    """The fictional letter in the owner's rubric shape (tests/fixtures/browser), with these ids."""
    ids = {"$FACT": fact_id, "$JOB": job_id, "$STORY": story_id, "$CONTACT": "contact:links" if contact else None}
    return [{**sentence, "fact_ids": [ids[i] for i in sentence["fact_ids"] if ids[i]],
             "job_evidence_ids": [ids[i] for i in sentence["job_evidence_ids"] if ids[i]]}
            for sentence in RUBRIC_LETTER]


CASE_LETTER = rubric_letter("fact.bakery", POSTING["id"])
HEDGED_LETTER = [*CASE_LETTER[:10],
                 {"text": "While I have not run TikTok Ads, I am a quick learner and would be a strong fit.",
                  "fact_ids": [], "job_evidence_ids": [POSTING["id"]], "paragraph": 3}, *CASE_LETTER[10:]]


def test_a_letter_leaves_out_a_requirement_no_fact_supports_without_a_hedge(candidate, mock_job):
    from interviewmaxxing_browser.ai.humanize import FIT_HEDGE_FEEDBACK, fit_hedges

    jev = Jev(semantic="COVER_LETTER")
    writer = DraftQueue(HEDGED_LETTER, CASE_LETTER)
    packet, resolver, ctx = resolve(context(candidate, mock_job, question="Cover letter", semantic=SemanticType.COVER_LETTER),
                                    Retriever(list(candidate.facts), job_evidence=[POSTING]), writer, jev)
    assert packet.is_complete and ctx.problems(packet) == []
    first, second = writer.calls
    assert first["purpose"] == "cover_letter" and second["review_feedback"] == [FIT_HEDGE_FEEDBACK]
    text = packet.answers[0].value.text
    assert "TikTok" not in text and fit_hedges(text) == []  # the unsupported requirement is simply left out
    draft = next(t for t in resolver.narrative_traces if t["stage"] == "draft")
    assert draft["status"] == "FIT_HEDGED" and draft["rejected_for"] == ["FIT_HEDGED"]
    # A letter that makes the case from what the facts support is written on the first draft.
    writer = DraftQueue(CASE_LETTER)
    packet, _, ctx = resolve(context(candidate, mock_job, question="Cover letter", semantic=SemanticType.COVER_LETTER),
                             Retriever(list(candidate.facts), job_evidence=[POSTING]), writer, jev)
    assert packet.is_complete and len(writer.calls) == 1


def test_fit_hedges_are_named_and_linted_and_ordinary_claims_are_not() -> None:
    from interviewmaxxing_browser.ai.humanize import fit_hedges

    for hedge in ("While I have not managed TikTok Ads, I ran paid social on Meta.",
                  "Although my background is in paid search, I learned social quickly.",
                  "I have limited experience with Amazon Ads.", "I'm a quick learner and eager to learn.",
                  "I believe I would be a strong fit for this role.", "I've never run TV, but my digital work is deep.",
                  "I don't have direct experience with retail media."):
        assert fit_hedges(hedge), hedge
        assert "fit_hedge" in {f.pattern for f in lint(hedge)}
    for claim in ("While I was at Glaze Agency, I managed Google Ads for twelve clients.",
                  "I trained a team with no prior experience in SEO to run audits.",
                  "We hired a strong candidate for the analytics seat.", "I helped the client ramp up spend to $50,000.",
                  "I do not rely on last-click attribution, but on incrementality tests."):
        assert fit_hedges(claim) == [], claim


@dataclass
class ConflictingReviewer(Writer):
    """A writer whose independent review finds a conflict and names the facts involved."""
    reviews: list[dict[str, Any]] = field(default_factory=list)
    reference_ids: list[str] = field(default_factory=list)

    def review(self, **kwargs: Any) -> Any:
        self.reviews.append(kwargs)
        return SimpleNamespace(verdict="CONFLICT", issues=["Two facts date the same role differently."],
                               reference_ids=self.reference_ids)


def test_a_review_hold_names_the_fact_ids_to_correct_or_remove(candidate, mock_job):
    """Round 5, addendum item 8: the person removes confirmed facts that contradict their own
    evidence by id, so the review's hold message names them (story and job ids stay out)."""
    chunk = story_chunk()
    other = fact(candidate, "Grew online orders by 20% for the regional bakery chain in 2024.", fid="fact.bakery_other",
                 key="achievement")
    profile = candidate.model_copy(update={"facts": [*candidate.facts, other]})
    writer = ConflictingReviewer([{"text": "I grew online orders by 35%.", "fact_ids": ["fact.bakery"]}],
                                 reference_ids=["fact.bakery", "fact.bakery_other", chunk["id"]])
    jev = Jev(consistency=0.9)  # uncertain: the evidence goes to the independent review
    packet, resolver, ctx = resolve(context(profile, mock_job), Retriever([profile.facts[0]], [chunk]), writer, jev)
    assert held(packet, ctx) and not writer.calls
    [missing] = packet.missing_inputs
    assert missing.prompt.endswith("(facts: fact.bakery, fact.bakery_other)") and chunk["id"] not in missing.prompt
    assert "Two facts date the same role differently." in missing.prompt
    review = next(t for t in resolver.narrative_traces if t["stage"] == "strong_review")
    assert review["status"] == "CONFLICT" and review["reference_ids"] == ["fact.bakery", "fact.bakery_other", chunk["id"]]


# --- round 5, addendum item 7: case-study questions answered from the question's own data ---------

CASE_QUESTION = ("Calculate CPA and ROAS for each channel. Based on this information, respond to the above question - "
                 "Part B: Universal Data Reading & Optimization Identification")
CASE_TABLE = ("Channel | Spend | Conversions | Revenue\nSearch | $5,000 | 100 | $12,500\n"
              "Social | $3,000 | 40 | $4,800\nDisplay | $2,000 | 10 | $1,500")  # fictional figures


def case_context(candidate: CandidateProfile, job: JobRecord, *, section_context: list[str]) -> PacketContext:
    ctx = context(candidate, job, question=CASE_QUESTION)
    field_ = ctx.form.fields[0].model_copy(update={"section_context": section_context})
    return replace(ctx, form=ctx.form.model_copy(update={"fields": [field_]}))


def case_sentences(data_id: str, *, search_cpa: str = "$50") -> list[dict[str, Any]]:
    lines = [f"Search CPA = $5,000 / 100 conversions = {search_cpa}.", "Search ROAS = $12,500 / $5,000 = 2.5.",
             "Social CPA = $3,000 / 40 = $75.", "Social ROAS = $4,800 / $3,000 = 1.6.",
             "Display CPA = $2,000 / 10 = $200.", "Display ROAS = $1,500 / $2,000 = 0.75.",
             "Search has the lowest CPA and the highest ROAS, so the $2,000 on Display should move to Search."]
    return [{"text": line, "job_evidence_ids": [data_id]} for line in lines]


def test_case_study_wording_is_recognised() -> None:
    from interviewmaxxing_browser.ai.case_analysis import case_analysis_question

    for question in (CASE_QUESTION, "Using the table above, which channel should get more budget?",
                     "Review the following data and identify the weakest campaign.", "Case study: compute ROAS per channel."):
        assert case_analysis_question(question), question
    for question in ("How do you calculate ROAS?", "Describe a campaign you led and what it achieved.", INTEREST):
        assert not case_analysis_question(question), question


def test_the_working_is_checked_in_code() -> None:
    from interviewmaxxing_browser.ai.case_analysis import check_working

    good = " ".join(s["text"] for s in case_sentences("form:x"))
    assert check_working(good, CASE_TABLE) is None
    assert check_working("Total spend = $5,000 + $3,000 + $2,000 = $10,000, so blended ROAS is "
                         "(12,500 + 4,800 + 1,500) / 10,000 = 1.88, about 1.9.", CASE_TABLE) is None
    assert "is wrong: it computes 50.00" in check_working("Search CPA = $5,000 / 100 = $55.", CASE_TABLE)  # type: ignore[operator]
    assert "uses $6,000" in check_working("Search CPA = $6,000 / 100 = $60.", CASE_TABLE)  # type: ignore[operator]
    assert "states $80" in check_working("Search CPA = $5,000 / 100 = $50 and will reach $80.", CASE_TABLE)  # type: ignore[operator]
    assert check_working("Search conversion rate = 100 / 5,000 = 2%.", CASE_TABLE + "\nSearch clicks 5,000") is None


def test_a_case_question_is_computed_from_its_own_data_and_cites_it(candidate, mock_job):
    from interviewmaxxing_core import question_content_ref

    ctx = case_context(candidate, mock_job, section_context=["Part B", CASE_TABLE])
    data_id = question_content_ref(ctx.form.fields[0])
    writer = Writer(case_sentences(data_id))
    retriever = Retriever([candidate.facts[0]])
    jev = Jev(scope="EXPLICIT_ANSWER", scope_probability=0.78)  # the candidate scope does not apply
    packet, resolver, ctx = resolve(ctx, retriever, writer, jev)
    assert packet.is_complete and ctx.problems(packet) == []
    [answer] = packet.answers
    assert answer.provenance.source is AnswerSource.GENERATED_FROM_QUESTION
    assert answer.provenance.reference_ids == [data_id] and "no candidate fact" in answer.provenance.note
    [call] = writer.calls
    # No candidate fact ever; the owner's voice passages only, as style (round 6, addendum 3:
    # his technical explainer shows the working the way he does).
    assert call["purpose"] == "case_analysis" and call["facts"] == []
    assert call["voice_samples"] == ["I write short, plain sentences."]
    assert [e["id"] for e in call["job_evidence"]] == [data_id] and CASE_TABLE in call["job_evidence"][0]["text"]
    assert len(retriever.calls) == 1  # for the voice passages; its facts are never used
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "case_analysis")
    assert trace["status"] == "READY" and trace["data_numbers"] == 9 and "Search" not in json.dumps(trace)
    [grounding] = jev.grounding_requests()
    assert grounding["state"]["purpose"] == "case_analysis" and set(grounding["questions"]) == {f"q{i}" for i in range(7)} | {"complete"}


def test_a_case_question_without_its_table_holds_with_the_reason(candidate, mock_job):
    from interviewmaxxing_browser.ai.providers import CASE_DATA_MISSING

    writer = Writer(case_sentences("form:x"))
    packet, resolver, ctx = resolve(case_context(candidate, mock_job, section_context=[]), Retriever([candidate.facts[0]]),
                                    writer, Jev())
    assert held(packet, ctx) and not writer.calls
    [missing] = packet.missing_inputs
    assert CASE_DATA_MISSING in missing.prompt and CASE_DATA_MISSING == "The table referenced is not in the recorded question"
    assert next(t for t in resolver.narrative_traces if t["stage"] == "case_analysis")["status"] == "DATA_MISSING"


def test_wrong_working_gets_one_corrective_rewrite_then_holds(candidate, mock_job):
    from interviewmaxxing_core import question_content_ref

    ctx = case_context(candidate, mock_job, section_context=[CASE_TABLE])
    data_id = question_content_ref(ctx.form.fields[0])
    writer = DraftQueue(case_sentences(data_id, search_cpa="$55"), case_sentences(data_id))
    packet, resolver, ctx = resolve(ctx, Retriever([candidate.facts[0]]), writer, Jev())
    assert packet.is_complete and len(writer.calls) == 2
    [issue] = writer.calls[1]["review_feedback"]
    assert "is wrong: it computes 50.00" in issue and "show the working" in issue
    writer = DraftQueue(case_sentences(data_id, search_cpa="$55"))
    packet, resolver, ctx = resolve(case_context(candidate, mock_job, section_context=[CASE_TABLE]),
                                    Retriever([candidate.facts[0]]), writer, Jev())
    assert held(packet, ctx) and "working does not check out" in packet.missing_inputs[0].prompt
    # A draft that cites a candidate fact is no case analysis.
    cited = case_sentences(data_id)
    cited[0] = {**cited[0], "fact_ids": ["fact.bakery"]}
    packet, resolver, ctx = resolve(case_context(candidate, mock_job, section_context=[CASE_TABLE]),
                                    Retriever([candidate.facts[0]]), Writer(cited), Jev())
    assert held(packet, ctx)
    assert next(t for t in resolver.narrative_traces if t["stage"] == "case_analysis")["status"] == "CITATIONS_INVALID"


# --- round 6: story passages, the owner's letter rubric, the genre lint ---------------------------

LETTER = "Cover letter"


def letter_context(candidate: CandidateProfile, job: JobRecord) -> PacketContext:
    return context(candidate, job, question=LETTER, semantic=SemanticType.COVER_LETTER)


@dataclass
class LetterWriter:
    """A writer double for cover letters: scripted drafts (the last repeats) and a review that
    answers by purpose from scripted verdicts (SUPPORTED when a queue is empty)."""
    drafts: list[list[dict[str, Any]]]
    rubric: list[Any] = field(default_factory=list)
    evidence: list[Any] = field(default_factory=list)
    owner_question: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)

    def write(self, **kwargs: Any) -> NarrativeDraft:
        self.calls.append(kwargs)
        sentences = self.drafts.pop(0) if len(self.drafts) > 1 else self.drafts[0]
        return NarrativeDraft.model_validate({"status": "READY", "sentences": sentences, "missing_information": []})

    def review(self, **kwargs: Any) -> Any:
        self.reviews.append(kwargs)
        if kwargs["purpose"] == "letter_review":
            # Grounding SUPPORTED; the rubric grade from the queue (PASS when it is empty).
            grade = (self.rubric.pop(0) if len(self.rubric) > 1 else self.rubric[0]) if self.rubric else "PASS"
            if isinstance(grade, Exception):
                raise grade
            if isinstance(grade, tuple) and len(grade) == 2:  # ("FAIL", number of issues)
                return SimpleNamespace(verdict="SUPPORTED", issues=[], reference_ids=[], rubric="FAIL",
                                       rubric_issues=[f"Rubric line {n + 3}: an open issue." for n in range(grade[1])],
                                       owner_question="")
            if isinstance(grade, tuple):  # a grounding verdict, its issues and references
                verdict, issues, references = grade
                return SimpleNamespace(verdict=verdict, issues=issues, reference_ids=references, rubric="PASS",
                                       rubric_issues=[])
            return SimpleNamespace(verdict="SUPPORTED", issues=[], reference_ids=[], rubric=grade,
                                   rubric_issues=[] if grade == "PASS" else ["Rubric line 4: name one thing only "
                                                                             "true of this employer."],
                                   owner_question="" if grade == "PASS" else self.owner_question)
        queue = self.evidence if kwargs["purpose"] == "evidence_consistency" else []
        verdict = (queue.pop(0) if len(queue) > 1 else queue[0]) if queue else "SUPPORTED"
        if isinstance(verdict, tuple):
            verdict, issues, references = verdict
        else:
            issues, references = ([] if verdict == "SUPPORTED" else ["A contradiction."]), []
        return SimpleNamespace(verdict=verdict, issues=issues, reference_ids=references)

    def purposes(self) -> list[str]:
        return [review["purpose"] for review in self.reviews]


def test_uncertain_story_passages_get_one_review_and_stay_unless_it_finds_a_conflict(candidate, mock_job):
    chunk = story_chunk()
    money = fact(candidate, "Managed a $40,000 paid search budget for a florist in 2022.", fid="fact.florist")
    profile = candidate.model_copy(update={"facts": [*candidate.facts, money]})
    answer = [{"text": "I grew online orders by 35% for a regional bakery chain in 2024.",
               "fact_ids": ["fact.bakery", chunk["id"]]}]
    # Uncertain (0.05 < p < 0.95): one independent review; SUPPORTED keeps the passage.
    writer = LetterWriter([answer])
    packet, resolver, ctx = resolve(context(profile, mock_job), Retriever([profile.facts[0]], [chunk]),
                                    writer, Jev(story=0.6))
    assert packet.is_complete and ctx.problems(packet) == []
    assert any(item["id"] == chunk["id"] for item in writer.calls[0]["facts"])
    [review] = [r for r in writer.reviews if r["purpose"] == "evidence_consistency"]
    assert {f["id"] for f in review["facts"]} >= {chunk["id"], "fact.florist"}  # and what else it compares
    assert "story passages" in review["question"]
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "story_consistency")
    assert trace["status"] == "CONSISTENT" and trace["review"] == {"reviewed": ["c0"], "contradicted": [],
                                                                   "status": "REVIEWED"}
    assert "story evidence" in packet.answers[0].provenance.note
    # The same passages and comparison facts are reviewed once per runtime.
    ctx2 = replace(context(profile, mock_job), form=resolver.router.annotate(context(profile, mock_job).form,
                                                                           document_id="synthetic-stories"))
    assert asyncio.run(resolver.resolve(ctx2)).is_complete
    assert writer.purposes().count("evidence_consistency") == 1
    assert any(t["stage"] == "story_review_cache" and t["status"] == "CACHED" for t in resolver.narrative_traces)
    # A CONFLICT naming the passage drops it; the answer writes from the rest.
    rest = [{"text": "I grew online orders by 35% for a regional bakery chain in 2024.", "fact_ids": ["fact.bakery"]}]
    writer = LetterWriter([rest], evidence=[("CONFLICT", ["The passage's budget contradicts fact.florist."],
                                             [chunk["id"], "fact.florist"])])
    packet, resolver, _ = resolve(context(profile, mock_job), Retriever([profile.facts[0]], [chunk]),
                                  writer, Jev(story=0.6))
    assert packet.is_complete and not any(item["key"] == "story" for item in writer.calls[0]["facts"])
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "story_consistency")
    assert trace["status"] == "DROPPED" and trace["review"]["contradicted"] == ["c0"]
    # A real contradiction (p <= 0.05) is dropped without any review.
    writer = LetterWriter([rest])
    packet, resolver, _ = resolve(context(profile, mock_job), Retriever([profile.facts[0]], [chunk]),
                                  writer, Jev(story=0.05))
    assert packet.is_complete and "evidence_consistency" not in writer.purposes()
    assert "review" not in next(t for t in resolver.narrative_traces if t["stage"] == "story_consistency")
    # Without a reviewer the uncertain passage is dropped, as before round 6.
    packet, resolver, _ = resolve(context(profile, mock_job), Retriever([profile.facts[0]], [chunk]),
                                  Writer(rest), Jev(story=0.6))
    assert packet.is_complete
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "story_consistency")
    assert trace["status"] == "DROPPED" and trace["review"]["status"] == "NOT_REVIEWED"


def test_a_rubric_letter_is_written_first_time_with_the_profile_links(candidate, mock_job):
    chunk = story_chunk()
    letter = rubric_letter("fact.bakery", POSTING["id"], story_id=chunk["id"])
    writer = LetterWriter([letter])
    retriever = Retriever(list(candidate.facts), [chunk], job_evidence=[POSTING])
    jev = Jev(semantic="COVER_LETTER")
    packet, resolver, ctx = resolve(letter_context(candidate, mock_job), retriever, writer, jev)
    assert packet.is_complete and ctx.problems(packet) == [] and len(writer.calls) == 1
    assert retriever.calls[0]["limit"] == 12  # a cover letter's facts come per requirement, up to 12
    contact = next(item for item in writer.calls[0]["facts"] if item["id"] == "contact:links")
    assert contact == {"id": "contact:links", "key": "contact_links",
                       "value": "LinkedIn: https://www.linkedin.example/in/avery-example"}
    answer = packet.answers[0]
    assert answer.provenance.reference_ids == ["fact.bakery"]  # the links and the passage are not facts
    assert "profile links from the applicant's identity" in answer.provenance.note
    assert "story evidence" in answer.provenance.note
    assert answer.value.text.startswith("Dear Hiring Manager,\n\nIn 2024 I grew online orders")
    # The rubric review graded the grounded letter before anything else could change it.
    assert writer.purposes() == ["letter_review"]  # grounding and the rubric in one review
    rubric = writer.reviews[0]
    assert {f["id"] for f in rubric["facts"]} >= {"fact.bakery", chunk["id"], "contact:links"}
    assert rubric["job_evidence"] == [POSTING] and rubric["sentences"]
    assert next(t for t in resolver.narrative_traces if t["stage"] == "draft")["rubric"]["status"] == "PASSED"
    assert "rubric review passed" in answer.provenance.note
    # Jev's completeness is scoped to the letter's shape, and a first step is a plan, not a claim.
    [grounding] = jev.grounding_requests()
    assert "owner's shape" in grounding["questions"]["complete"]["instructions"]
    assert "plan, not a claim of fact" in grounding["questions"]["q0"]["instructions"]
    # Other narratives keep eight facts and get no contact entry.
    retriever = Retriever(list(candidate.facts))
    packet, _, _ = resolve(context(candidate, mock_job), retriever,
                           Writer([{"text": "I grew online orders by 35%.", "fact_ids": ["fact.bakery"]}]), Jev())
    assert packet.is_complete and retriever.calls[0]["limit"] == 8


@pytest.mark.parametrize("rule", ["GREETING", "LETTER_LENGTH", "OPENING", "CLOSING", "STORY_MISSING",
                                  "JOB_RESTATED", "EMPLOYER_NAME", "ATTRIBUTION", "DATE_RANGE", "FACTS_UNCITED",
                                  "PROOF_RETOLD", "COMPANY_FACT_COPIED", "HOOK_VOLUME", "EMPLOYER_REPEATED",
                                  "FIGURE_UNPAIRED", "AGE_REVEALED"])
def test_each_rubric_line_the_code_checks_gets_a_corrective_rewrite(candidate, mock_job, rule):
    from interviewmaxxing_browser.ai.routing import (
        AGE_FEEDBACK,
        ATTRIBUTION_FEEDBACK,
        COMPANY_FACT_FEEDBACK,
        DATE_RANGE_FEEDBACK,
        EMPLOYER_REPEATED_FEEDBACK,
        FACTS_UNCITED_FEEDBACK,
        HOOK_VOLUME_FEEDBACK,
        JOB_RESTATED_FEEDBACK,
        LETTER_CLOSING_FEEDBACK,
        LETTER_GREETING_FEEDBACK,
        LETTER_LENGTH_FEEDBACK,
        LETTER_OPENING_FEEDBACK,
        LETTER_STORY_FEEDBACK,
        PROOF_RETOLD_FEEDBACK,
    )

    chunk = story_chunk()
    good = rubric_letter("fact.bakery", POSTING["id"], story_id=chunk["id"])
    bad = [dict(sentence) for sentence in good]
    job = mock_job
    posting = POSTING
    if rule == "GREETING":
        bad = [{**s, "paragraph": s["paragraph"] - 1} for s in bad[1:]]
        bad[0]["text"] = "Dear team: " + bad[0]["text"]  # not a greeting line of its own
    elif rule == "LETTER_LENGTH":
        bad = [bad[0], bad[1], bad[3], bad[7], bad[10], bad[11], bad[12]]
        bad = [{**s, "paragraph": index} for index, s in zip([0, 1, 2, 3, 3, 4, 4], bad, strict=True)]
    elif rule == "OPENING":
        bad[1] = {**bad[1], "text": "I am writing to apply for the paid search role at your company, which I "
                                    "found through a friend and read closely twice.", "fact_ids": ["fact.bakery"]}
    elif rule == "CLOSING":
        bad[12] = {**bad[12], "text": "Thank you for considering my application."}
    elif rule == "STORY_MISSING":
        bad = [{**s, "fact_ids": [fid for fid in s["fact_ids"] if not fid.startswith("story:")]} for s in bad]
    elif rule == "JOB_RESTATED":
        bad[8] = {**bad[8], "text": "The role also calls for weekly reports to the sales team and to its managers.",
                  "fact_ids": [], "job_evidence_ids": [POSTING["id"]]}
        bad[9] = {**bad[9], "text": "The posting asks for hands-on Google Ads management with conversion "
                                    "tracking across every account.", "fact_ids": [],
                  "job_evidence_ids": [POSTING["id"]]}
    elif rule == "DATE_RANGE":
        bad[3] = {**bad[3], "text": "When I took over the bakery chain's account, which I ran from 2022 to 2024, its "
                                    "dashboard counted every phone call as an order."}
    elif rule == "FACTS_UNCITED":
        bad = [{**s, "fact_ids": [fid for fid in s["fact_ids"] if fid != "fact.bakery"] or
                ([chunk["id"]] if s["fact_ids"] else [])} for s in bad]
    elif rule == "ATTRIBUTION":
        bad[9] = {**bad[9], "text": "Reporting to a sales team is the weekly habit I kept, and Mock Co holds this "
                                    "role accountable for exactly that kind of reporting."}
    elif rule == "PROOF_RETOLD":  # the company paragraph tells the proof again, twice
        bad[8] = {**bad[8], "fact_ids": [*bad[8]["fact_ids"], chunk["id"]]}
        bad[9] = {**bad[9], "fact_ids": [*bad[9]["fact_ids"], chunk["id"]]}
    elif rule == "COMPANY_FACT_COPIED":  # the posting's own sentence pasted in
        bad[7] = {**bad[7], "text": "Mock Co wants someone to own paid search strategy for enterprise brands and "
                                    "report results to the sales team, which is the weekly rhythm I kept."}
    elif rule == "HOOK_VOLUME":  # lead volume as the headline metric while the fact states a result
        bad[1] = {**bad[1], "text": "In 2024 my paid search work for a regional bakery chain produced 12,000 online "
                                    "order leads from local searchers after I rebuilt how conversions were counted."}
    elif rule == "EMPLOYER_REPEATED":  # one employer named twice in a paragraph
        candidate = candidate.model_copy(update={"experience": [Experience(
            id="exp.glaze", company="Glaze Agency Inc.", title="Paid Search Lead")]})
        bad[3] = {**bad[3], "text": "When I took over the bakery chain's account at Glaze, its dashboard counted every "
                                    "phone call as an order, so the budget kept flowing to searches that never sold."}
        bad[4] = {**bad[4], "text": "At Glaze I rebuilt the tracking so that only paid orders counted, which halved "
                                    "the reported conversions for two months and made the owner nervous that spring."}
    elif rule == "FIGURE_UNPAIRED":  # the passage's figure printed without the verified fact that states it
        bad[6] = {**bad[6], "fact_ids": [chunk["id"]]}
    elif rule == "AGE_REVEALED":  # the passage gives his age; the letter never does
        bad[4] = {**bad[4], "text": "As a 24-year-old analyst I rebuilt the tracking so that only paid orders counted, "
                                    "which halved the reported conversions for two months and worried the owner."}
    else:  # EMPLOYER_NAME: the metadata carries a listing source's name, the description another
        job = mock_job.model_copy(update={"company": "Coda Fictional"})
        posting = {**POSTING, "text": "Superfictional Mail: " + POSTING["text"]}
        bad[2] = {**bad[2], "text": "Your paid search program at Coda Fictional ties spend decisions to reported "
                                    "results, and that rebuild is the work I would bring to it first."}
    writer = LetterWriter([bad, good])
    packet, resolver, ctx = resolve(letter_context(candidate, job), Retriever(list(candidate.facts), [chunk],
                                    job_evidence=[posting]), writer, Jev(semantic="COVER_LETTER"))
    assert packet.is_complete and ctx.problems(packet) == [] and len(writer.calls) == 2
    draft = next(t for t in resolver.narrative_traces if t["stage"] == "draft")
    assert rule in draft["rejected_for"]
    feedback = {"GREETING": LETTER_GREETING_FEEDBACK, "LETTER_LENGTH": LETTER_LENGTH_FEEDBACK,
                "OPENING": LETTER_OPENING_FEEDBACK, "CLOSING": LETTER_CLOSING_FEEDBACK,
                "STORY_MISSING": LETTER_STORY_FEEDBACK, "JOB_RESTATED": JOB_RESTATED_FEEDBACK,
                "ATTRIBUTION": ATTRIBUTION_FEEDBACK, "DATE_RANGE": DATE_RANGE_FEEDBACK,
                "FACTS_UNCITED": FACTS_UNCITED_FEEDBACK, "PROOF_RETOLD": PROOF_RETOLD_FEEDBACK,
                "COMPANY_FACT_COPIED": COMPANY_FACT_FEEDBACK, "HOOK_VOLUME": HOOK_VOLUME_FEEDBACK,
                "EMPLOYER_REPEATED": EMPLOYER_REPEATED_FEEDBACK, "AGE_REVEALED": AGE_FEEDBACK}.get(rule)
    issues = writer.calls[1]["review_feedback"]
    if rule == "FIGURE_UNPAIRED":
        assert any("verified fact that states the same figure (fact.bakery)" in issue for issue in issues)
    else:
        assert (feedback in issues) if feedback else any("Coda Fictional" in issue for issue in issues)
    # A letter still failing a checked line on its last attempt is held, never shipped.
    writer = LetterWriter([bad])
    packet, _, ctx = resolve(letter_context(candidate, job), Retriever(list(candidate.facts), [chunk],
                             job_evidence=[posting]), writer, Jev(semantic="COVER_LETTER"))
    assert held(packet, ctx) and len(writer.calls) == 3


def test_the_rubric_review_improves_a_grounded_letter_and_never_costs_it(candidate, mock_job):
    chunk = story_chunk()
    letter = rubric_letter("fact.bakery", POSTING["id"], story_id=chunk["id"])
    improved = [dict(sentence) for sentence in letter]
    improved[2] = {**improved[2], "text": "The weekly report tied every search dollar to a paid order, and that "
                                          "rebuild at the bakery chain is the work I would bring first."}
    retriever = lambda: Retriever(list(candidate.facts), [chunk], job_evidence=[POSTING])  # noqa: E731
    issue = "Rubric line 4: name one thing only true of this employer."
    text = lambda sentences: NarrativeDraft.model_validate(ready(sentences)).text  # noqa: E731
    # A HARD line the reviewer finds failed: its issues are the improvement draft's feedback,
    # which is checked and grounded like the first, then graded again.
    writer = LetterWriter([letter, improved], rubric=["FAIL", "PASS"])
    packet, resolver, _ = resolve(letter_context(candidate, mock_job), retriever(), writer, Jev(semantic="COVER_LETTER"))
    from interviewmaxxing_browser.ai.routing import IMPROVEMENT_LENGTH_FEEDBACK

    assert packet.is_complete and len(writer.calls) == 2
    assert writer.calls[1]["review_feedback"] == [issue, IMPROVEMENT_LENGTH_FEEDBACK]
    assert packet.answers[0].value.text == text(improved)
    assert "rubric review passed" in packet.answers[0].provenance.note
    [draft] = [t for t in resolver.narrative_traces if t["stage"] == "draft"]
    rubric = draft["rubric"]
    assert rubric["status"] == "PASSED" and [p.get("improvement") for p in rubric["passes"]] == ["ACCEPTED", None]
    assert rubric["passes"][0]["grounding"]["independent_review"] == "SUPPORTED"
    assert issue not in json.dumps(ai_package_project(draft))  # review text never reaches the store
    # Still failing after the improvement pass: grade what ships, so the letter holds (the
    # judge's fifth fix) and asks the owner the reviewer's question when it has one.
    writer = LetterWriter([letter], rubric=["FAIL"])
    packet, resolver, ctx = resolve(letter_context(candidate, mock_job), retriever(), writer, Jev(semantic="COVER_LETTER"))
    assert held(packet, ctx) and len(writer.calls) == 2  # one improvement pass (RUBRIC_PASSES)
    assert "does not pass the owner's rubric: " + issue in packet.missing_inputs[0].prompt
    assert next(t for t in resolver.narrative_traces if t["stage"] == "draft")["rubric"]["status"] == "RESIDUAL"
    # An improvement that fails a checked line or its grounding is dropped: the grounded
    # letter stands, never held.
    # An improvement that fails a checked line or its grounding is dropped: the grounded
    # letter stands (and, graded FAIL, holds rather than ships).
    broken = [{**s, "paragraph": s["paragraph"] - 1} for s in improved[1:]]
    writer = LetterWriter([letter, broken], rubric=["FAIL"])
    packet, resolver, ctx = resolve(letter_context(candidate, mock_job), retriever(), writer, Jev(semantic="COVER_LETTER"))
    assert held(packet, ctx)
    rubric = next(t for t in resolver.narrative_traces if t["stage"] == "draft")["rubric"]
    assert rubric["status"] == "RESIDUAL" and rubric["passes"][0]["improvement"] == "DROPPED"
    assert "GREETING" in rubric["passes"][0]["failed"]
    ungrounded = Jev(semantic="COVER_LETTER", support=lambda index, text, requests: 0.01 if requests >= 2 else 1.0)
    writer = LetterWriter([letter, improved], rubric=["FAIL", "PASS"])
    packet, resolver, ctx = resolve(letter_context(candidate, mock_job), retriever(), writer, ungrounded)
    assert held(packet, ctx)
    assert next(t for t in resolver.narrative_traces if t["stage"] == "draft")["rubric"]["passes"][0][
        "improvement"] == "DROPPED"
    # The letter review is the draft's grounding review too: a failed review call holds the
    # field, as any independent review a draft needs does.
    writer = LetterWriter([letter], rubric=[AIHold("Review HTTP_500")])
    packet, resolver, ctx = resolve(letter_context(candidate, mock_job), retriever(), writer, Jev(semantic="COVER_LETTER"))
    assert held(packet, ctx)


def ai_package_project(trace: dict[str, Any]) -> dict[str, Any]:
    """The runner's trace projection: what the store keeps of a trace."""
    from interviewmaxxing_cli.runner import project_trace

    return project_trace(trace)


SLOP_LETTER_EDIT = "I simply rebuilt the tracking so that only paid orders counted, which halved the reported " \
    "conversions for two months and made the owner nervous about the spend for most of that spring."


def test_a_cover_letter_is_humanized_and_a_discard_is_never_silent(candidate, mock_job):
    """The rewrite of a grounded letter is kept (REWRITTEN), re-grounded and reviewed; a test
    that fails if the no-slop rewrite is discarded without a word (round 6 addendum)."""
    clean = rubric_letter("fact.bakery", POSTING["id"])
    sloppy = [dict(sentence) for sentence in clean]
    sloppy[4] = {**sloppy[4], "text": SLOP_LETTER_EDIT}
    transport = Transport(write=[ready(sloppy)], humanize=[ready(clean)], review=[REVIEW_OK])
    packet, resolver, ctx = resolve(letter_context(candidate, mock_job),
                                    Retriever(list(candidate.facts), job_evidence=[POSTING]),
                                    real_writer(transport, budget=CallBudget(max_usd=3.0)),
                                    Jev(semantic="COVER_LETTER"), humanize=True)
    assert packet.is_complete and ctx.problems(packet) == []
    value = packet.answers[0].value
    assert isinstance(value, TextValue) and value.text == NarrativeDraft.model_validate(ready(clean)).text
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "humanize")
    assert trace["status"] == "REWRITTEN" and trace["discarded"] == []
    assert {f["pattern"] for f in trace["lint_before"]} >= {"empty_adverb"} and trace["lint_after"] == []
    assert trace["citations"] == [{"fact_ids": s["fact_ids"], "job_evidence_ids": s["job_evidence_ids"],
                                   "paragraph": s["paragraph"]} for s in clean]
    purposes = [json.loads(r["messages"][1]["content"])["purpose"]
                for role, r in zip(transport.roles, transport.requests, strict=True) if role == "review"]
    # One combined grounding and rubric review per draft: the writer's and the rewrite's.
    assert transport.roles == ["write", "review", "humanize", "review"]
    assert purposes == ["letter_review", "letter_review"]
    assert "rewrite discarded" not in packet.answers[0].provenance.note
    system = transport.requests[0]["messages"][0]["content"]
    assert "280-380 words" in system and "Dear Hiring Manager," in system and "contact_links" in system
    assert "Thank you for considering my application." not in system.replace(
        "never 'Thank you for considering my application'", "")
    rubric = transport.requests[1]["messages"][0]["content"]
    assert "grade the cover letter in rubric and rubric_issues" in rubric and "40-employer test" in rubric
    rules = transport.requests[2]["messages"][0]["content"]
    assert "never preserved as the applicant's voice" in rules and "job_restated" in rules


def test_the_rewrite_may_delete_or_fold_a_job_only_sentence_but_never_move_a_fact_set() -> None:
    from interviewmaxxing_browser.ai.humanize import check_rewrite

    letter = rubric_letter("fact.bakery", POSTING["id"])
    job_only = {"text": "The role also asks for weekly reports to the sales team.", "fact_ids": [],
                "job_evidence_ids": [POSTING["id"]], "paragraph": 3}
    original = NarrativeDraft.model_validate(ready([*letter[:8], job_only, *letter[8:]]))
    ids = {"fact.bakery", "contact:links"}
    deleted = NarrativeDraft.model_validate(ready(letter))
    assert check_rewrite(original, deleted, purpose="cover_letter", supplied_ids=ids, job_ids={POSTING["id"]},
                         max_length=None) is None
    folded = [dict(s) for s in letter]
    folded[8] = {**folded[8], "job_evidence_ids": [POSTING["id"]]}  # the job clause joins a fact sentence
    assert check_rewrite(original, NarrativeDraft.model_validate(ready(folded)), purpose="cover_letter",
                         supplied_ids=ids, job_ids={POSTING["id"]}, max_length=None) is None
    added = NarrativeDraft.model_validate(ready([*letter[:8], job_only, dict(job_only), *letter[9:]]))
    assert check_rewrite(original, added, purpose="cover_letter", supplied_ids=ids, job_ids={POSTING["id"]},
                         max_length=None) == "added_job_sentence"
    moved = [dict(s) for s in letter]
    moved[11] = {**moved[11], "fact_ids": ["fact.bakery"]}  # the links' sentence now cites the bakery fact
    assert check_rewrite(deleted, NarrativeDraft.model_validate(ready(moved)), purpose="cover_letter",
                         supplied_ids=ids, job_ids={POSTING["id"]}, max_length=None) in ("moved_citation",
                                                                                          "dropped_citation")
    paired = [{"text": "At a bakery chain I rebuilt tracking for the search program the posting names.",
               "fact_ids": ["fact.bakery"], "job_evidence_ids": [POSTING["id"]]},
              {"text": "I wrote weekly reports for two store managers.", "fact_ids": ["fact.reports"]}]
    unpaired = [{**paired[0], "job_evidence_ids": []}, paired[1]]  # the fact sentence lost its job pairing
    assert check_rewrite(NarrativeDraft.model_validate(ready(paired)), NarrativeDraft.model_validate(ready(unpaired)),
                         purpose="answer", supplied_ids={"fact.bakery", "fact.reports"}, job_ids={POSTING["id"]},
                         max_length=None) == "dropped_citation"
    # The close keeps both its sentences: the profile link and the offer to talk (a live
    # rewrite cut the offer).
    no_offer = [*letter[:12], {**letter[12], "text": "My order data from the bakery chain is ready for review."}]
    assert check_rewrite(deleted, NarrativeDraft.model_validate(ready(no_offer)), purpose="cover_letter",
                         supplied_ids=ids, job_ids={POSTING["id"]}, max_length=None) == "letter_shape"
    no_greeting = [{**s, "paragraph": s["paragraph"] - 1} for s in letter[1:]]
    no_greeting[0] = {**no_greeting[0], "text": "Dear Hiring Manager, " + no_greeting[0]["text"]}
    assert check_rewrite(deleted, NarrativeDraft.model_validate(ready(no_greeting)), purpose="cover_letter",
                         supplied_ids=ids, job_ids={POSTING["id"]}, max_length=None) == "letter_shape"


@pytest.mark.parametrize(("pattern", "text"), [
    ("job_restated", "The posting describes a growth team. I grew orders by 35% at a bakery chain in 2024."),
    ("stock_opener", "I am writing to apply for the growth role. I grew orders by 35% at a bakery chain."),
    ("stock_closer", "I grew orders by 35% at a bakery chain. Thank you for considering my application."),
    ("fit_commentary", "My paid search work at a bakery chain relates directly to your acquisition goals."),
    ("fit_commentary", "That tracking rebuild could apply to your funnel as well."),
    ("connective_tic", "I ran search for a bakery chain. In that same role, I wrote weekly reports."),
    ("connective_tic", "I ran search for a bakery chain. Separately, I wrote weekly reports."),
    ("identical_paragraph_openings", "At the bakery chain I ran search.\n\nAt the bakery chain I wrote reports.\n\n"
                                     "I can talk this week."),
    ("portable_sentence", "I would bring my paid acquisition and team leadership experience to that work."),
    ("fake_strong_verb", "The weekly report serves as a testing ground for new keywords."),
    ("empty_adverb", "I simply rebuilt the conversion tracking for a bakery chain."),
    ("self_answered_question", "The result? Online orders grew by 35% at a bakery chain."),
    ("and_fragments", "I built the funnel. And the tracking behind it. And the weekly reporting that followed."),
    ("colon_case", "One lesson stuck with me: The dashboard counted calls as orders."),
    ("decorative_formatting", "I **cut cost per order 31%** by rebuilding the tracking."),
    ("decorative_formatting", "What I did:\n- rebuilt tracking\n- moved budget"),
    ("synonym_cycling", "The role needs search. The posting asks for tracking. The position wants reports."),
    ("closing_recap", "I ran search at a bakery chain in 2024.\n\nI grew orders 35% there.\n\n"
                      "My background in paid media and testing prepares me to drive growth for your team."),
    ("empty_phrase", "Let's dive in: I ran paid search for a bakery chain."),
    ("attribution_clause", "I ran search, the kind of channel work that Fictional Co names."),
    ("attribution_clause", "I wrote the weekly report, and Fictional Co holds this role accountable for it."),
    ("attribution_clause", "I led tests as the role asks."),
    ("attribution_clause", "That close-rate problem at a bakery chain is where I've done my best work."),
    ("repeated_dates", "At a bakery chain from March 2022 to May 2024 I ran search."),
    ("repeated_dates", "Since June 2025 I ran search. Since June 2025 I wrote reports. Since June 2025 I led tests."),
])
def test_the_genre_lint_names_each_pattern(pattern, text):
    assert pattern in {finding.pattern for finding in lint(text, company="Fictional Co")}, text


def test_the_genre_lint_leaves_a_plain_letter_alone() -> None:
    from interviewmaxxing_browser.ai.humanize import portable, restates_job

    letter = NarrativeDraft.model_validate(ready(rubric_letter("fact.bakery", POSTING["id"])))
    assert lint(letter.text, company="Fictional Co") == []
    for claim in ("I cut cost per order 31% for a regional bakery chain in 2024.",
                  "I aligned the bakery chain's store managers on one weekly report in 2024.",
                  "My report went to two store managers at Crumb & Co. every Monday.",
                  "Fictional Co's retail media team runs search on three marketplaces, and I ran two of them at "
                  "a bakery chain in 2024."):
        assert not {f.pattern for f in lint(claim, company="Fictional Co")} & {
            "fit_commentary", "portable_sentence", "job_restated", "stock_opener", "connective_tic"}, claim
    assert restates_job("Fictional Co wants a growth lead for search.", "Fictional Co")
    assert not restates_job("Fictional Co wants search growth, which I delivered at a bakery chain.", "Fictional Co")
    assert not portable("I bring 7 years of search experience from Crumb & Co. to the role.")
    assert lint("Dear Hiring Manager,\n\nI am writing to apply.") != [] and "stock_opener" in {
        f.pattern for f in lint("Dear Hiring Manager,\n\nI am writing to apply for the role today.")}


def test_a_draft_with_a_clean_lint_is_kept_without_a_rewrite(candidate, mock_job):
    """The owner's rubric accepts a clean lint before the pass: no rewrite, grounding or review
    is spent on it (round 6, the lead's cost target)."""
    transport = Transport(write=[ready(CLEAN)], humanize=[ready(SLOPPY)], review=[REVIEW_OK])
    packet, resolver, _ctx = resolve(context(candidate, mock_job), Retriever([candidate.facts[0]]),
                                     real_writer(transport), Jev(), humanize=True)
    assert packet.is_complete and transport.roles == ["write"]
    assert packet.answers[0].value.text == NarrativeDraft.model_validate(ready(CLEAN)).text
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "humanize")
    assert trace["status"] == "CLEAN" and trace["lint_before"] == [] and trace["attempts"] == []
    assert trace["citations"] == [{"fact_ids": ["fact.bakery"], "job_evidence_ids": [], "paragraph": 0}] * 3
    assert "rewrite discarded" not in packet.answers[0].provenance.note


# --- round 6, addendum 3: the owner's voice samples are style only ---------------------------

BLOG_A = ("Fictional 2017 post. Let's be honest: most bakery sites waste their search budget. It's no secret that "
          "an awesome landing page can skyrocket your orders. Here's the kicker: I once grew a bakery's traffic "
          "400% in a month. Think of paid search like a delivery van, not a sports car.")
BLOG_B = ("Fictional 2017 post two. You might be wondering: what the hell is a negative keyword? Trust me when I say "
          "this, it is the cheapest fix you will ever make. The bottom line: cut the waste first.")


def test_voice_passages_reach_the_writer_and_the_rewrite_as_style_only(candidate, mock_job):
    from interviewmaxxing_browser.ai.providers import VOICE_RULE

    clean = rubric_letter("fact.bakery", POSTING["id"])
    sloppy = [dict(sentence) for sentence in clean]
    sloppy[4] = {**sloppy[4], "text": SLOP_LETTER_EDIT}
    transport = Transport(write=[ready(sloppy)], humanize=[ready(clean)], review=[REVIEW_OK])
    jev = Jev(semantic="COVER_LETTER")
    retriever = Retriever(list(candidate.facts), job_evidence=[POSTING], voice_samples=[BLOG_A, BLOG_B])
    packet, _resolver, _ctx = resolve(letter_context(candidate, mock_job), retriever,
                                      real_writer(transport, budget=CallBudget(max_usd=3.0)), jev, humanize=True)
    assert packet.is_complete and transport.roles == ["write", "review", "humanize", "review"]
    write, rewrite = transport.requests[0], transport.requests[2]
    for request in (write, rewrite):
        system, user = request["messages"][0]["content"], json.loads(request["messages"][1]["content"])
        assert user["voice_samples"] == [BLOG_A, BLOG_B]  # two passages per letter, as retrieved
        assert VOICE_RULE in system and "style only" in system
        assert "never their content, claims, numbers or phrases" in system and "'Here's the kicker:'" in system
    # Never evidence: not among the writer's facts or job evidence, never sent to Jev or the review.
    user = json.loads(write["messages"][1]["content"])
    assert not any(BLOG_A[:40] in json.dumps(item) for item in [*user["facts"], *user["job_evidence"]])
    reviews = [r for role, r in zip(transport.roles, transport.requests, strict=True) if role == "review"]
    assert reviews and not any(BLOG_A[:40] in json.dumps(r) or BLOG_B[:40] in json.dumps(r) for r in reviews)
    assert not any(BLOG_A[:40] in json.dumps(r) for r in jev.requests)
    # The blog's tics are lint findings the rewrite must cut.
    patterns = {f.pattern for f in lint("It's no secret that paid search works. Here's the kicker: CPA fell 31%. "
                                        "The bottom line is cost. The bottom line is also speed.")}
    assert "blog_tic" in patterns
    assert "blog_tic" not in {f.pattern for f in lint("I cut cost per order 31% at a bakery chain in 2024.")}


def test_a_rewrite_may_reformat_a_figure_but_never_add_one() -> None:
    original = NarrativeDraft.model_validate(ready([
        {"text": "At a bakery chain I managed deals averaging $50,000 and cut cost per order 31%.",
         "fact_ids": ["fact.bakery"]},
        {"text": "Each deal took 5 to 10 touch points before the owner signed off.", "fact_ids": ["fact.bakery"]}]))
    reformatted = NarrativeDraft.model_validate(ready([
        {"text": "At a bakery chain I ran deals of about $50K and cut cost per order by 31%.",
         "fact_ids": ["fact.bakery"]},
        {"text": "Each one took 5-10 touch points before the owner signed off.", "fact_ids": ["fact.bakery"]}]))
    assert check_rewrite(original, reformatted, purpose="answer", supplied_ids={"fact.bakery"}, job_ids=set(),
                         max_length=None) is None
    added = NarrativeDraft.model_validate(ready([
        {"text": "At a bakery chain I ran deals of about $50K and cut cost per order by 31% in 90 days.",
         "fact_ids": ["fact.bakery"]},
        {"text": "Each one took 5-10 touch points before the owner signed off.", "fact_ids": ["fact.bakery"]}]))
    assert check_rewrite(original, added, purpose="answer", supplied_ids={"fact.bakery"}, job_ids=set(),
                         max_length=None) == "new_number"


def test_uncertain_facts_and_passages_share_one_evidence_review(candidate, mock_job):
    chunk = story_chunk()
    rival = fact(candidate, "Grew repeat orders by 12% for a regional bakery chain (2023).", fid="fact.repeat")
    profile = candidate.model_copy(update={"facts": [*candidate.facts, rival]})
    answer = [{"text": "I grew online orders by 35% for a regional bakery chain in 2024.",
               "fact_ids": ["fact.bakery", chunk["id"]]}]
    jev = Jev(consistency=0.9, story=0.6)  # both uncertain
    writer = LetterWriter([answer])
    packet, resolver, _ = resolve(context(profile, mock_job), Retriever([profile.facts[0]], [chunk]), writer, jev)
    assert packet.is_complete and writer.purposes().count("evidence_consistency") == 1
    [review] = [r for r in writer.reviews if r["purpose"] == "evidence_consistency"]
    ids = {item["id"] for item in review["facts"]}
    assert {"fact.bakery", chunk["id"]} <= ids and review["question"].startswith("Two checks in one review.")
    merged = next(t for t in resolver.narrative_traces if t["stage"] == "strong_review" and t.get("merged_fact_review"))
    assert merged["story_ids"] == [chunk["id"]] and merged["status"] == "SUPPORTED"
    assert any(item["id"] == chunk["id"] for item in writer.calls[0]["facts"])  # the passage stays
    # A contradiction the review pins on the passage drops the passage; the facts stand.
    writer = LetterWriter([[{**answer[0], "fact_ids": ["fact.bakery"]}]],
                          evidence=[("CONFLICT", ["The passage's figure contradicts fact.bakery."],
                                     [chunk["id"], "fact.bakery"])])
    packet, resolver, _ = resolve(context(profile, mock_job), Retriever([profile.facts[0]], [chunk]), writer,
                                  Jev(consistency=0.9, story=0.6))
    assert packet.is_complete and writer.purposes().count("evidence_consistency") == 1
    assert not any(item["key"] == "story" for item in writer.calls[0]["facts"])
    # A contradiction among the facts themselves holds, as the fact review alone would.
    writer = LetterWriter([answer], evidence=[("CONFLICT", ["fact.bakery and fact.repeat disagree."],
                                               ["fact.bakery", "fact.repeat"])])
    packet, resolver, ctx = resolve(context(profile, mock_job), Retriever([profile.facts[0]], [chunk]), writer,
                                    Jev(consistency=0.9, story=0.6))
    assert held(packet, ctx) and not writer.calls
    # Without an uncertain passage the fact review runs alone, once.
    writer = LetterWriter([[{**answer[0], "fact_ids": ["fact.bakery"]}]])
    packet, resolver, _ = resolve(context(profile, mock_job), Retriever([profile.facts[0]], [chunk]), writer,
                                  Jev(consistency=0.9, story=1.0))
    assert packet.is_complete and writer.purposes().count("evidence_consistency") == 1
    assert not any(t.get("merged_fact_review") for t in resolver.narrative_traces if t["stage"] == "strong_review")



# --- round 6: the judge's batch-1 fixes ------------------------------------------------------


def test_the_employer_is_named_as_the_posting_names_it() -> None:
    from interviewmaxxing_browser.ai.humanize import attribution_clauses, employer_names

    assert employer_names("Maximus Health, Inc.", "Maximus is building the future of men's health.") == ["Maximus"]
    assert employer_names("Base Power Company", "Base is deploying a network of batteries.") == ["Base"]
    assert employer_names("Superhuman", "Superhuman Mail sits in the Superhuman suite.") == ["Superhuman"]
    assert employer_names("Fictional Labs LLC", "We build ovens.") == ["Fictional Labs LLC"]
    names = employer_names("Maximus Health, Inc.", "Maximus is hiring.")
    for clause in ("and Maximus asks its growth lead to back every allocation with data",
                   "the persona-driven acquisition Maximus puts at the center of its channel portfolio",
                   "the kind of spend defense to a CEO that Maximus names as a requirement",
                   "Maximus's lean team works the way my practice has"):
        assert attribution_clauses(clause, names), clause
    assert "attribution_clause" in {f.pattern for f in lint("I cut CPA 58%, and Maximus asks its lead to do that.",
                                                            company=names)}
    assert attribution_clauses("I rebuilt the tracking that turns calls into signed cases at a firm.", names) == []


def test_the_company_fact_is_structure_the_rewrite_rewords_but_never_deletes() -> None:
    from interviewmaxxing_browser.ai.humanize import check_rewrite

    letter = rubric_letter("fact.bakery", POSTING["id"])
    company_fact = {"text": "Mock Co runs paid search for enterprise brands across four regional markets.",
                    "fact_ids": [], "job_evidence_ids": [POSTING["id"]], "paragraph": 3}
    trimmed = [dict(s) for s in letter]
    original = NarrativeDraft.model_validate(ready([*trimmed[:7], company_fact, *trimmed[7:]]))
    ids = {"fact.bakery", "contact:links"}
    dropped = NarrativeDraft.model_validate(ready(trimmed))
    assert check_rewrite(original, dropped, purpose="cover_letter", supplied_ids=ids, job_ids={POSTING["id"]},
                         max_length=None) == "dropped_structure"
    reworded = [dict(s) for s in trimmed]
    reworded[7] = {**reworded[7], "job_evidence_ids": [POSTING["id"]],
                   "text": "Mock Co runs paid search for enterprise brands across four regional markets, and owning "
                           "that strategy while reporting results to a sales team is the weekly rhythm I kept for the "
                           "bakery chain's store managers."}
    assert check_rewrite(original, NarrativeDraft.model_validate(ready(reworded)), purpose="cover_letter",
                         supplied_ids=ids, job_ids={POSTING["id"]}, max_length=None) is None
    # The lint judges a sentence by its words, never by what it cites: a company fact or an
    # offer to talk citing job evidence alone is no restatement.
    assert "job_restated" not in {f.pattern for f in lint(original.text, company="Mock Co")}


def test_what_ships_is_graded_and_a_rewrite_that_breaks_the_rubric_is_not_kept(candidate, mock_job):
    clean = rubric_letter("fact.bakery", POSTING["id"])
    sloppy = [dict(sentence) for sentence in clean]
    sloppy[4] = {**sloppy[4], "text": SLOP_LETTER_EDIT}
    failing = {**REVIEW_OK, "rubric": "FAIL", "rubric_issues": ["Line 9: the rewrite added fit commentary."],
               "owner_question": ""}
    # The rewrite's review fails a line the draft passed: tried again with the issues, and the
    # second rewrite, graded PASS, ships.
    transport = Transport(write=[ready(sloppy)], humanize=[ready(clean)], review=[REVIEW_OK, failing, REVIEW_OK])
    jev = Jev(semantic="COVER_LETTER")
    packet, resolver, _ = resolve(letter_context(candidate, mock_job),
                                  Retriever(list(candidate.facts), job_evidence=[POSTING]),
                                  real_writer(transport, budget=CallBudget(max_usd=4.0)), jev, humanize=True)
    assert packet.is_complete and "rubric review passed" in packet.answers[0].provenance.note
    trace = next(t for t in resolver.narrative_traces if t["stage"] == "humanize")
    assert [a["status"] for a in trace["attempts"]] == ["REJECTED_RUBRIC", "REWRITTEN"]
    retry = json.loads(transport.requests[4]["messages"][1]["content"])
    assert "Line 9: the rewrite added fit commentary." in retry["rejected_rewrite"]
    # Sentences the draft's review supported are settled for the rewrite's review, and Jev is
    # asked again only about the sentence the rewrite changed.
    review = json.loads(transport.requests[3]["messages"][1]["content"])
    assert review["purpose"] == "letter_review" and 4 not in review["settled_sentences"]
    assert set(review["settled_sentences"]) == set(range(len(clean))) - {4}
    second = jev.grounding_requests()[1]
    assert set(second["questions"]) == {"complete", "q4"}
    # Failing again, the grounded draft that passed ships and says the rewrite was discarded.
    transport = Transport(write=[ready(sloppy)], humanize=[ready(clean)], review=[REVIEW_OK, failing])
    packet, resolver, _ = resolve(letter_context(candidate, mock_job),
                                  Retriever(list(candidate.facts), job_evidence=[POSTING]),
                                  real_writer(transport, budget=CallBudget(max_usd=4.0)), Jev(semantic="COVER_LETTER"),
                                  humanize=True)
    assert packet.is_complete and packet.answers[0].value.text == NarrativeDraft.model_validate(ready(sloppy)).text
    assert "no-AI-slop rewrite discarded: REJECTED_RUBRIC, REJECTED_RUBRIC" in packet.answers[0].provenance.note


def test_a_letter_without_a_passing_grade_holds_and_asks_the_owner(candidate, mock_job):
    letter = rubric_letter("fact.bakery", POSTING["id"])
    question = "What did the rep-letter play cost, and who pushed back?"
    writer = LetterWriter([letter], rubric=["FAIL"], owner_question=question)
    packet, _, ctx = resolve(letter_context(candidate, mock_job),
                             Retriever(list(candidate.facts), job_evidence=[POSTING]), writer,
                             Jev(semantic="COVER_LETTER"))
    assert held(packet, ctx) and question in packet.missing_inputs[0].prompt
    # No reviewer at all: no grade, so no letter ships.
    packet, _, ctx = resolve(letter_context(candidate, mock_job),
                                    Retriever(list(candidate.facts), job_evidence=[POSTING]), Writer(letter),
                                    Jev(semantic="COVER_LETTER"))
    assert held(packet, ctx) and "no rubric grade" in packet.missing_inputs[0].prompt



def test_a_second_improvement_runs_only_while_the_letter_converges(candidate, mock_job):
    letter = rubric_letter("fact.bakery", POSTING["id"])
    retriever = lambda: Retriever(list(candidate.facts), job_evidence=[POSTING])  # noqa: E731
    writer = LetterWriter([letter], rubric=[("FAIL", 2), ("FAIL", 1), "PASS"])
    packet, resolver, _ = resolve(letter_context(candidate, mock_job), retriever(), writer, Jev(semantic="COVER_LETTER"))
    assert packet.is_complete and len(writer.calls) == 3  # two improvements: 2 issues, then 1, then none
    rubric = next(t for t in resolver.narrative_traces if t["stage"] == "draft")["rubric"]
    assert rubric["status"] == "PASSED" and [p.get("improvement") for p in rubric["passes"]] == [
        "ACCEPTED", "ACCEPTED", None]


def test_a_letter_that_will_hold_spends_nothing_on_the_no_slop_pass(candidate, mock_job):
    letter = rubric_letter("fact.bakery", POSTING["id"])
    sloppy = [dict(sentence) for sentence in letter]
    sloppy[4] = {**sloppy[4], "text": SLOP_LETTER_EDIT}
    failing = {**REVIEW_OK, "rubric": "FAIL", "rubric_issues": ["Line 4: name what the employer sells."],
               "owner_question": ""}
    transport = Transport(write=[ready(sloppy)], humanize=[ready(letter)], review=[failing])
    packet, _, ctx = resolve(letter_context(candidate, mock_job), Retriever(list(candidate.facts), job_evidence=[POSTING]),
                             real_writer(transport, budget=CallBudget(max_usd=4.0)), Jev(semantic="COVER_LETTER"),
                             humanize=True)
    assert held(packet, ctx) and "humanize" not in transport.roles
    assert transport.roles == ["write", "review", "write", "review"]


# --- round 7: the judge's batch 2-4 fixes and the owner's age rule -----------------------------

AGE_PASSAGE = ("Pitching the bakery chain's owner meant asking a 24 year old analyst to take over a budget the "
               "print buyers had held for a decade; I showed the order data and the owner approved it.")


def test_no_narrative_ever_states_the_applicants_age(candidate, mock_job):
    from interviewmaxxing_browser.ai.humanize import age_revealed, check_rewrite
    from interviewmaxxing_browser.ai.routing import AGE_FEEDBACK

    chunk = story_chunk(AGE_PASSAGE, title="The print budget")
    with_age = [{"text": "As a 24-year-old analyst I moved the bakery chain's print budget into paid search, and "
                         "online orders grew by 35%.", "fact_ids": ["fact.bakery", chunk["id"]]}]
    without = [{"text": "As the far less senior analyst, I moved the bakery chain's print budget into paid search, "
                        "and online orders grew by 35%.", "fact_ids": ["fact.bakery", chunk["id"]]}]
    # An answer: the passage keeps the age as written; the answer loses it after one rewrite.
    writer = DraftQueue(with_age, without)
    packet, resolver, _ = resolve(context(candidate, mock_job), Retriever([candidate.facts[0]], [chunk]), writer, Jev())
    assert packet.is_complete and not age_revealed(packet.answers[0].value.text)
    assert writer.calls[1]["review_feedback"] == [AGE_FEEDBACK]
    assert next(t for t in resolver.narrative_traces if t["stage"] == "draft")["rejected_for"] == ["AGE_REVEALED"]
    # A motivation answer too, and an age on the last attempt holds.
    motivation = [{"text": "At 24, I moved a regional bakery chain's print budget into paid search, and I want "
                           "that kind of measured budget at your company.",
                   "fact_ids": ["fact.bakery"], "job_evidence_ids": [JOB_EVIDENCE["id"]]}]
    writer = DraftQueue(motivation)
    packet, _, ctx = resolve(context(with_career_motivation(candidate), mock_job, question=INTEREST),
                             Retriever([candidate.facts[0]], job_evidence=[JOB_EVIDENCE]), writer,
                             Jev(scope="EXPLICIT_ANSWER", scope_probability=0.78))
    assert held(packet, ctx) and len(writer.calls) == 2
    # The no-slop rewrite can never bring an age in.
    original = NarrativeDraft.model_validate(ready(without))
    rewrite = NarrativeDraft.model_validate(ready(with_age))
    assert check_rewrite(original, rewrite, purpose="answer", supplied_ids={"fact.bakery", chunk["id"]},
                         job_ids=set(), max_length=None) == "age_revealed"
    assert "age_revealed" in {f.pattern for f in lint("I was the youngest in the room when I pitched the owner.")}
    for plain in ("I cut cost per order 24% at a bakery chain.", "We opened at 24 locations.",
                  "I was 26 years into a family business."):
        assert age_revealed(plain) == [], plain


def test_the_lint_names_a_pasted_about_line_and_an_employer_named_twice() -> None:
    posting = ("Fictional Ovens is a mission-driven kitchen robotics company that gives bakers speed, safety, and "
               "consistency to delight customers, grow margins, and scale.")
    pasted = ("Fictional Ovens is a mission-driven kitchen robotics company that gives bakers speed, safety, and "
              "consistency. I cut cost per order 31% at a bakery chain.")
    assert "job_restated" in {f.pattern for f in lint(pasted, company="Fictional Ovens", posting=[posting])}
    assert "job_restated" not in {f.pattern for f in lint(
        "Fictional Ovens builds kitchen robots for bakeries. I cut cost per order 31% at a bakery chain.",
        company="Fictional Ovens", posting=[posting])}
    employers = [["Glaze Agency Inc.", "Glaze"]]
    twice = "At Glaze I rebuilt the tracking. Glaze clients then bid on paid orders.\\n\\nI can talk this week."
    assert "repeated_employer" in {f.pattern for f in lint(twice, employers=employers)}
    once = "At Glaze I rebuilt the tracking, and the clients then bid on paid orders.\\n\\nI can talk this week."
    assert "repeated_employer" not in {f.pattern for f in lint(once, employers=employers)}


def test_a_rewrite_may_not_narrow_a_claims_scope() -> None:
    from interviewmaxxing_browser.ai.humanize import check_rewrite

    draft = [{"text": "At a regional bakery chain I grew online orders by 35% after rebuilding the tracking.",
              "fact_ids": ["fact.bakery"]},
             {"text": "The weekly report I built for Glaze clients went to two store managers.",
              "fact_ids": ["fact.reports"]}]
    narrowed = [dict(draft[0]), {**draft[1], "text": "The weekly report I built for those clients went to two store "
                                                     "managers."}]
    kept = [dict(draft[0]), {**draft[1], "text": "The weekly report I built for Glaze clients reached two store "
                                                 "managers."}]
    ids = {"fact.bakery", "fact.reports"}
    original = NarrativeDraft.model_validate(ready(draft))
    assert check_rewrite(original, NarrativeDraft.model_validate(ready(narrowed)), purpose="answer",
                         supplied_ids=ids, job_ids=set(), max_length=None) == "narrowed_scope"
    assert check_rewrite(original, NarrativeDraft.model_validate(ready(kept)), purpose="answer",
                         supplied_ids=ids, job_ids=set(), max_length=None) is None


def test_the_first_move_is_built_from_the_chunk_that_matches_the_proof(candidate, mock_job):
    from interviewmaxxing_browser.ai.routing import _proof_chunk_guidance

    chunk = story_chunk()  # conversion tracking and online orders for a bakery chain
    salary = {**POSTING, "id": "job:" + "e" * 64, "text": "Salary and benefits: we offer a generous package, remote "
                                                          "work and learning budgets for everyone who joins."}
    matching = {**POSTING, "id": "job:" + "f" * 64, "text": "You will connect campaign performance to conversion "
                                                            "tracking and online orders across direct mail and search."}
    guidance = _proof_chunk_guidance([chunk], [salary, matching])
    assert guidance is not None and "job_evidence entry 2 of 2" in guidance and "channel" in guidance
    assert _proof_chunk_guidance([chunk], [matching]) is None and _proof_chunk_guidance([], [salary, matching]) is None
    writer = LetterWriter([rubric_letter("fact.bakery", matching["id"], story_id=chunk["id"])])
    resolve(letter_context(candidate, mock_job),
            Retriever(list(candidate.facts), [chunk], job_evidence=[salary, matching]), writer,
            Jev(semantic="COVER_LETTER"))
    assert any("job_evidence entry 2 of 2" in rule for rule in writer.calls[0]["guidance"])


def test_the_letter_prompts_carry_the_judges_rules() -> None:
    from interviewmaxxing_browser.ai.providers import (
        AGE_RULE,
        COVER_LETTER_RULES,
        LETTER_RUBRIC_LINES,
    )

    for phrase in ("told once", "never lead or call volume", "dropped, not moved into the proof",
                   "never retelling the proof", "one plain clause in the posting's own nouns",
                   "never the posting's About or mission sentence", "however it is worded",
                   "at most once per paragraph", "print the figure the way that fact prints it",
                   "a direct-mail role says 'direct mail'", "end it on that result with its number"):
        assert phrase in COVER_LETTER_RULES, phrase
    for phrase in ("(c) at least one sentence of his own", "never against the long-form stories",
                   "judged by its content, not its wording", "a retelling in the company paragraph or the close fails",
                   "this is for line 8 only", "never the applicant's age"):
        assert phrase in LETTER_RUBRIC_LINES, phrase
    assert "the far less senior buyer" in AGE_RULE
